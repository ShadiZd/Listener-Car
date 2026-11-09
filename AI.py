"""AI.py - turn a spoken text command into a validated car plan.

Sends the text to Gemini (``gemini-3.1-flash-lite`` by default), asks for a
short ordered list of low-level car actions, validates/clamps every value, and
stores the plan in ``action.JSON`` for action.py to execute.

The action vocabulary matches main_code.py exactly:
    forward, reverse, left, right, straight, stop, horn

Standalone usage:
    python AI.py "drive forward for two seconds then turn left and honk"
    python AI.py --show-json "go backward slowly"
    python AI.py --no-write "stop"          # validate only, don't touch the file

As a module (used by action.py later):
    import AI
    plan = AI.plan("turn right then drive forward")
    plan["commands"]   # validated list, already written to action.JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional at import time
    load_dotenv = None

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
ACTION_FILE = ROOT / "action.JSON"

DEFAULT_MODEL = "gemini-3.1-flash-lite"
API_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "{model}:generateContent")
DEFAULT_TIMEOUT_S = 20.0
MAX_RETRIES = 2                    # extra attempts on 429/5xx
RETRY_DELAY_S = 1.0

MAX_COMMANDS = 5
# Speed is in motor-speed units (deg/s-like scale), not percent:
# 800 = normal driving speed, 1000 = full speed.
MAX_SPEED = 1000
MIN_SPEED = 1
NORMAL_SPEED = 800
MAX_DURATION_MS = 15000        # hard time limit: 15 seconds
MIN_DURATION_MS = 100
DEFAULT_SPEED = NORMAL_SPEED
DEFAULT_DURATION_MS = 8000
MAX_ANGLE = 45
DEFAULT_ANGLE = 45

ALLOWED_ACTIONS = {
    "forward", "reverse", "left", "right", "straight", "stop", "horn",
}

SYSTEM_PROMPT = """\
You are the driving brain of a small LEGO SPIKE Prime car. The driver speaks a
command; you translate it into a short, ordered list of low-level car actions.
The car has a drive motor pair, a steering motor, and ultrasonic sensors that
automatically stop it near obstacles.

Allowed actions (use these exact names):
- "forward": drive forward.  parameters {"speed": <1-1000>, "duration_ms": <100-15000>}
- "reverse": drive backward. parameters {"speed": <1-1000>, "duration_ms": <100-15000>}
- "left":    turn the steering wheels left.  parameters {"angle": <1-45>}
- "right":   turn the steering wheels right. parameters {"angle": <1-45>}
- "straight": center the steering wheels.   parameters {}
- "stop":    stop the car and center the steering. parameters {}
- "horn":    beep the horn.                  parameters {}

Rules:
1. "speed" is a motor-speed value, not km/h: 800 = normal speed, 1000 = full
   speed. Use below 800 when the driver says slow, and 1000 only for "full
   speed" / "as fast as possible".
2. "duration_ms": "a second" = 1000, "two seconds" = 2000. Default is 8000.
   The maximum is 15000 (15 seconds); never exceed it.
3. left/right ONLY move the steering wheels; the car keeps whatever drive
   command it had. To make the car turn, emit the steering action and then a
   forward action (or forward, then steering). Example: "turn left" ->
   [{"action": "left", "parameters": {"angle": 45}},
    {"action": "forward", "parameters": {"speed": 800, "duration_ms": 1000}}]
4. "go straight" -> straight. "stop", "halt", "brake" -> stop.
5. If the driver asks for several things, keep the order they were spoken.
6. Use at most 5 actions. Prefer the smallest plan that does the job.
7. If the instruction is not about driving this car (weather, general
   questions, chit-chat), return an empty command list.
8. Never invent action names or parameters.

Examples:
- "drive forward for two seconds" ->
  {"reasoning": "Drive forward at normal speed for two seconds.",
   "commands": [{"action": "forward",
                 "parameters": {"speed": 800, "duration_ms": 2000}}]}
- "turn right then go" ->
  {"reasoning": "Steer right, then drive forward.",
   "commands": [{"action": "right", "parameters": {"angle": 45}},
                {"action": "forward",
                 "parameters": {"speed": 800, "duration_ms": 1500}}]}
- "drive forward at full speed for fifteen seconds" ->
  {"reasoning": "Drive forward at full speed for the maximum time.",
   "commands": [{"action": "forward",
                 "parameters": {"speed": 1000, "duration_ms": 15000}}]}
- "what is the weather" ->
  {"reasoning": "Not a driving command.", "commands": []}

Reply with JSON only, exactly this shape:
{"reasoning": "<one short sentence>",
 "commands": [{"action": "<name>", "parameters": {...}}]}
"""


class AIError(Exception):
    """LLM request or response failure."""


class PlanError(AIError):
    """The model's answer was not a valid car plan."""


# ---------------------------------------------------------------- key

def load_api_key() -> str:
    """Read the Gemini key from .env (variable ``LLM_api``)."""
    if load_dotenv is not None:
        load_dotenv(ENV_FILE, override=False)
    for name in ("LLM_api", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = (os.environ.get(name) or "").strip()
        if key:
            return key
    raise AIError(f"no API key found: set LLM_api in {ENV_FILE}")


# ---------------------------------------------------------------- request

def _build_body(text: str, context: Optional[str] = None) -> dict[str, Any]:
    user = f"Driver said: {text!r}"
    if context:
        user += f"\nCurrent car state: {context}"
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 1024,
        },
    }


def _api_error(response: requests.Response) -> str:
    message = response.text
    try:
        data = response.json()
        message = (data.get("error", {}) or {}).get("message") or message
    except ValueError:
        pass
    return f"Gemini API error {response.status_code}: {str(message)[:300]}"


def _extract_text(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise AIError("unexpected Gemini response (not a JSON object)")
    if payload.get("promptFeedback", {}).get("blockReason"):
        reason = payload["promptFeedback"]["blockReason"]
        raise AIError(f"Gemini blocked the request ({reason})")
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise AIError(f"unexpected Gemini response: {str(payload)[:300]}") from None
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise AIError("Gemini returned an empty answer")
    return text


def ask_gemini(text: str, api_key: str, model: str = DEFAULT_MODEL,
               timeout: float = DEFAULT_TIMEOUT_S,
               context: Optional[str] = None) -> str:
    """Send the command to Gemini and return its raw JSON text."""
    url = API_URL.format(model=model)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = _build_body(text, context)

    last_error = "unknown error"
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, json=body,
                                     timeout=timeout)
        except requests.RequestException as exc:
            raise AIError(f"network error talking to Gemini: {exc}") from None

        if response.status_code == 200:
            return _extract_text(response.json())

        last_error = _api_error(response)
        if response.status_code in (429, 500, 502, 503, 504) \
                and attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_S * (attempt + 1))
            continue
        raise AIError(last_error)

    raise AIError(last_error)


def parse_json_text(text: str) -> Any:
    """Parse JSON from the model, tolerating ```json fences and stray prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(cleaned[start:end + 1])
        except ValueError:
            pass
    raise PlanError(f"Gemini did not return valid JSON: {text[:300]!r}")


# ---------------------------------------------------------------- validation

def _to_int(value: Any, name: str) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise PlanError(f"{name} is not a number: {value!r}") from None


def _clamp(value: Any, low: int, high: int, name: str) -> int:
    return max(low, min(high, _to_int(value, name)))


def validate_command(raw: Any, command_id: int) -> dict[str, Any]:
    """Validate one model command; returns the action.JSON entry."""
    if not isinstance(raw, dict):
        raise PlanError(f"command {command_id} is not an object: {raw!r}")

    action = str(raw.get("action", "")).strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise PlanError(f"unknown action {action!r} "
                        f"(allowed: {', '.join(sorted(ALLOWED_ACTIONS))})")

    raw_params = raw.get("parameters")
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise PlanError(f"{action}: parameters must be an object")

    if action in ("forward", "reverse"):
        speed = _clamp(raw_params.get("speed", DEFAULT_SPEED),
                       MIN_SPEED, MAX_SPEED, "speed")
        duration = _clamp(raw_params.get("duration_ms", DEFAULT_DURATION_MS),
                          MIN_DURATION_MS, MAX_DURATION_MS, "duration_ms")
        parameters = {"speed": speed, "duration_ms": duration}

    elif action in ("left", "right"):
        magnitude = abs(_to_int(raw_params.get("angle", DEFAULT_ANGLE), "angle"))
        magnitude = max(1, min(MAX_ANGLE, magnitude))
        # main_code.py: negative angle = left, positive = right.
        parameters = {"angle": -magnitude if action == "left" else magnitude}

    else:  # straight / stop / horn take no parameters
        parameters = {}

    return {
        "command_id": command_id,
        "mode": "autonomous",
        "action": action,
        "parameters": parameters,
    }


def next_plan_id() -> int:
    """Continue numbering from the existing action.JSON, if any."""
    try:
        data = json.loads(ACTION_FILE.read_text(encoding="utf-8") or "{}")
        return int(data.get("plan_id", 0)) + 1
    except (OSError, ValueError, TypeError, AttributeError):
        return 1


def validate_plan(raw: Any, source_text: str) -> dict[str, Any]:
    """Turn the model's answer into the final, clamped plan dictionary."""
    if isinstance(raw, list):          # tolerate a bare command list
        raw = {"commands": raw}
    if not isinstance(raw, dict):
        raise PlanError(f"plan is not an object: {raw!r}")

    commands_raw = raw.get("commands", [])
    if not isinstance(commands_raw, list):
        raise PlanError("'commands' must be a list")
    if len(commands_raw) > MAX_COMMANDS:
        commands_raw = commands_raw[:MAX_COMMANDS]

    commands = [validate_command(item, index + 1)
                for index, item in enumerate(commands_raw)]

    reasoning = str(raw.get("reasoning", "")).strip()[:300]
    return {
        "plan_id": next_plan_id(),
        "source_text": source_text,
        "reasoning": reasoning,
        "commands": commands,
    }


# ---------------------------------------------------------------- output

def write_plan(plan: dict[str, Any], path: Path = ACTION_FILE) -> None:
    """Write the plan atomically so action.py never reads a half-written file."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def plan(text: str, context: Optional[str] = None,
         model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT_S,
         write: bool = True, api_key: Optional[str] = None) -> dict[str, Any]:
    """Text command -> validated plan (and, by default, action.JSON)."""
    text = (text or "").strip()
    if not text:
        raise AIError("empty command")

    key = api_key or load_api_key()
    raw_text = ask_gemini(text, key, model=model, timeout=timeout,
                          context=context)
    result = validate_plan(parse_json_text(raw_text), text)
    if write:
        write_plan(result)
    return result


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Turn a spoken command into a validated action.JSON plan.")
    parser.add_argument("text", nargs="*",
                        help="command text (omit to type it interactively)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"request timeout in seconds "
                             f"(default: {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--no-write", action="store_true",
                        help="validate only; leave action.JSON untouched")
    parser.add_argument("--show-json", action="store_true",
                        help="print the complete action.JSON content")
    args = parser.parse_args(argv)

    text = " ".join(args.text).strip()
    if not text:
        try:
            text = input("Command> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
    if not text:
        print("No command given.", file=sys.stderr)
        return 2

    try:
        result = plan(text, model=args.model, timeout=args.timeout,
                      write=not args.no_write)
    except AIError as exc:
        print(f"AI error: {exc}", file=sys.stderr)
        return 1

    print(f"Plan #{result['plan_id']} for: {result['source_text']}")
    if result["reasoning"]:
        print(f"  reasoning: {result['reasoning']}")
    if not result["commands"]:
        print("  (no car command understood)")
        return 0

    for command in result["commands"]:
        params = json.dumps(command["parameters"])
        print(f"  {command['command_id']}. {command['action']:<9} {params}")

    if args.show_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.no_write:
        print(f"Saved to {ACTION_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
