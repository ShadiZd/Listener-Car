"""action.py - laptop entry point for the voice-controlled car.

Pipeline:
    STT.py (speech -> text) -> AI.py (Gemini -> action.JSON) -> this file
    sends the validated plan to the SPIKE Prime hub over BLE/USB, where
    main_code.py executes it and replies ack / done / error.

Modes:
    python action.py                    voice loop, real hub over BLE
    python action.py --text             type commands instead of speaking
    python action.py --simulate         no hub: pace and print the commands
    python action.py --once "go forward for two seconds"
    python action.py --usb              connect with the USB cable
    python action.py --scan             list nearby BLE devices and exit
    python action.py --no-tts           disable spoken feedback

Spoken feedback uses gTTS + miniaudio on the laptop's speakers (the hub can
only beep). Each command is narrated as it starts ("Driving forward.",
"Turning left.", "Honking."), the plan ends with "Done.", and obstacle
warnings from the hub are spoken too ("Obstacle ahead.", "Path clear.").
While a plan runs, the laptop keeps listening: saying "wait", "stop",
"pause", "emergency stop" or "quit" stops the car immediately and returns
to the command prompt (voice mode only).
Configure it in .env:
    TTS_LANG=en                 gTTS language code (en, ar, fr, ...)
    TTS_ENABLED=1               set to 0 to mute

Hub settings can come from .env:
    HUB_NAME=Pybricks Hub       (optional name filter)
    HUB_ADDRESS=xx:xx:...       (optional; skips discovery)
    HUB_LINK=ble                (ble or usb)
    HUB_SCRIPT=main_code.py     (program deployed to the hub)
    HEARTBEAT_S=0.5             (stdin ping period)
    ACK_TIMEOUT_S=2.0           (wait for the hub's ack)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import AI
import STT
import TTS

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

DEFAULT_SCRIPT = str(ROOT / "main_code.py")
DEFAULT_HEARTBEAT_S = 0.5
DEFAULT_ACK_TIMEOUT_S = 2.0
DISCOVERY_TIMEOUT_S = 10.0
ADDRESS_TIMEOUT_S = 15.0
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_S = 2.0
DONE_MARGIN_S = 2.0          # extra wait on top of the command duration
MAX_DONE_WAIT_S = 20.0       # 15 s max command + margin

log = logging.getLogger("action")

STOP_WORDS = {"stop", "halt", "brake", "stop the car",
              "wait", "pause", "hold", "hold on", "cancel", "abort"}
ESTOP_WORDS = {"estop", "e-stop", "emergency stop"}
RESET_WORDS = {"reset", "clear", "clear emergency"}
QUIT_WORDS = {"quit", "exit", "goodbye"}

# Words that abort a plan that is already running. Saying one of these
# mid-drive stops the car immediately and returns to the command prompt.
INTERRUPT_WORDS = STOP_WORDS | ESTOP_WORDS | QUIT_WORDS


def _normalize(text: Optional[str]) -> str:
    return " ".join((text or "").lower().strip(" .!?").split())


def matches_words(text: Optional[str], words: set[str]) -> bool:
    """Exact match, or the phrase starts with one of the words."""
    low = _normalize(text)
    if not low:
        return False
    if low in words:
        return True
    # Tolerate extra words: "wait a second", "stop right now", ...
    return any(low.startswith(word + " ") for word in words)


def is_interrupt(text: Optional[str]) -> bool:
    """True when the phrase is a stop/wait/quit command (not a drive command)."""
    return matches_words(text, INTERRUPT_WORDS)


# ---------------------------------------------------------------- config

def load_config() -> dict[str, Any]:
    if load_dotenv is not None:
        load_dotenv(ENV_FILE, override=False)
    env = os.environ.get
    return {
        "hub_name": env("HUB_NAME", "").strip(),
        "hub_address": env("HUB_ADDRESS", "").strip(),
        "hub_link": env("HUB_LINK", "ble").strip().lower(),
        "hub_script": env("HUB_SCRIPT", DEFAULT_SCRIPT).strip(),
        "heartbeat_s": float(env("HEARTBEAT_S", str(DEFAULT_HEARTBEAT_S))),
        "ack_timeout_s": float(env("ACK_TIMEOUT_S", str(DEFAULT_ACK_TIMEOUT_S))),
    }


# ---------------------------------------------------------------- BLE/USB link

class HubLink:
    """JSON-lines link to the SPIKE Prime hub (same protocol as main_code.py)."""

    def __init__(self, script: str, name: str = "", address: str = "",
                 transport: str = "ble", heartbeat_s: float = DEFAULT_HEARTBEAT_S,
                 ack_timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
                 wait_hub_s: float = 0.0) -> None:
        if transport not in ("ble", "usb"):
            raise ValueError(f"unknown transport {transport!r}")
        self._script = script
        self._name = name
        self._address = address
        self._transport = transport
        self._heartbeat_s = heartbeat_s
        self._ack_timeout_s = ack_timeout_s
        self._wait_hub_s = wait_hub_s
        self._hub: Any = None
        self._connected = False
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._buffer = bytearray()
        self._tasks: list[asyncio.Task] = []
        self._subscription = None
        self._next_command_id = 0
        self._alert_handler: Optional[Any] = None

    @property
    def connected(self) -> bool:
        return self._connected

    def set_alert_handler(self, handler) -> None:
        """Register a callback for unsolicited hub alerts (e.g. obstacles)."""
        self._alert_handler = handler

    # -- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        log.info("connecting to the hub over %s ...", self._transport.upper())

        # Discovery. With --wait-hub, keep scanning until the user wakes the
        # hub (the hub sleeps after a couple of minutes of inactivity).
        deadline = (time.monotonic() + self._wait_hub_s
                    if self._wait_hub_s > 0 else None)
        while True:
            try:
                hub = await self._make_hub()
                break
            except ConnectionError:
                if deadline is None or time.monotonic() >= deadline:
                    raise                 # discovery failed: message is useful
                log.info("hub not found yet - press its center button to wake "
                         "it (%.0f s left) ...",
                         max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(2.0)

        last_exc: Optional[Exception] = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                await hub.connect()
            except Exception as exc:
                last_exc = exc
                log.warning("connection attempt %d/%d failed: %s: %s",
                            attempt, CONNECT_ATTEMPTS, type(exc).__name__, exc)
                try:
                    await hub.disconnect()
                except Exception:
                    pass
                if attempt < CONNECT_ATTEMPTS:
                    await asyncio.sleep(CONNECT_RETRY_DELAY_S)
                continue

            self._hub = hub
            hub.print_output = False
            # Subscribe BEFORE deploying: main_code.py prints "ready" the moment
            # it starts, and stdout_observable is a Subject with no replay, so
            # early lines (including any startup traceback) would be lost.
            self._subscription = hub.stdout_observable.subscribe(self._on_stdout)
            log.info("deploying %s ...", Path(self._script).name)
            await hub.run(self._script, wait=False, print_output=False,
                          line_handler=False)
            self._connected = True
            self._tasks = [asyncio.create_task(self._heartbeat())]
            await asyncio.sleep(0.4)      # let the hub program start
            await self.send({"type": "mode", "mode": "autonomous"})
            log.info("hub ready (autonomous mode)")
            return

        raise ConnectionError(
            f"could not connect after {CONNECT_ATTEMPTS} attempts: "
            f"{type(last_exc).__name__}: {last_exc}. Wake the hub with its "
            "center button, close Pybricks Code / the LEGO app / any phone "
            "connection, then toggle Windows Bluetooth off and on and retry."
        ) from last_exc

    async def disconnect(self) -> None:
        self._connected = False
        if self._subscription is not None:
            self._subscription.dispose()
            self._subscription = None
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        hub, self._hub = self._hub, None
        if hub is None:
            return
        try:
            await hub.stop_user_program()
        except Exception as exc:  # hub may already be gone
            log.debug("could not stop hub program: %s", exc)
        try:
            await hub.disconnect()
        except Exception as exc:
            log.debug("could not close hub link: %s", exc)

    # -- discovery -------------------------------------------------------

    async def _make_hub(self):
        try:
            from pybricksdev.connections.pybricks import (PybricksHubBLE,
                                                          PybricksHubUSB)
        except ImportError as exc:  # pragma: no cover
            raise ConnectionError(
                "pybricksdev is not installed: pip install pybricksdev") from exc

        if self._transport == "usb":
            return self._make_usb_hub(PybricksHubUSB)

        device = await self._discover_ble()
        return PybricksHubBLE(device)

    @staticmethod
    def _make_usb_hub(PybricksHubUSB):
        from pybricksdev.usb import (LEGO_USB_VID, MINDSTORMS_INVENTOR_USB_PID,
                                     NXT_USB_PID, SPIKE_ESSENTIAL_USB_PID,
                                     SPIKE_PRIME_USB_PID)
        from usb.core import find as find_usb

        pids = (NXT_USB_PID, SPIKE_PRIME_USB_PID, SPIKE_ESSENTIAL_USB_PID,
                MINDSTORMS_INVENTOR_USB_PID, 0x0005)

        def is_pybricks(device) -> bool:
            return (device.idVendor == LEGO_USB_VID
                    and device.idProduct in pids)

        device = find_usb(custom_match=is_pybricks)
        if device is None:
            raise ConnectionError(
                "no Pybricks hub found over USB. Plug in the car, wake the "
                "hub, then retry.")
        return PybricksHubUSB(device)

    async def _discover_ble(self):
        from bleak import BleakScanner
        from pybricksdev.ble import PYBRICKS_SERVICE_UUID

        service = str(PYBRICKS_SERVICE_UUID).lower()

        if self._address:
            try:
                device = await BleakScanner.find_device_by_address(
                    self._address, timeout=ADDRESS_TIMEOUT_S)
            except Exception as exc:
                raise ConnectionError(self._link_error(exc)) from None
            if device is None:
                raise ConnectionError(
                    f"hub at {self._address} did not answer in "
                    f"{ADDRESS_TIMEOUT_S:.0f} s. Press the hub's center button "
                    "to wake it, close Pybricks Code / the LEGO app, then retry.")
            return device

        try:
            found = await BleakScanner.discover(
                timeout=DISCOVERY_TIMEOUT_S, return_adv=True)
        except TypeError:  # bleak < 0.20
            found = {}
            for device in await BleakScanner.discover(
                    timeout=DISCOVERY_TIMEOUT_S):
                found[device.address] = (device, None)
        except Exception as exc:
            raise ConnectionError(self._link_error(exc)) from None

        named = []
        unnamed = []
        for address, pair in found.items():
            device, adv = pair if isinstance(pair, tuple) else (pair, None)
            services = {str(s).lower()
                        for s in (getattr(adv, "service_uuids", None) or [])}
            if service not in services:
                continue
            local_name = (getattr(adv, "local_name", None)
                          or getattr(device, "name", None) or "")
            if self._name and local_name.casefold() == self._name.casefold():
                named.append(device)
            elif self._name and local_name:
                continue          # a different, named hub
            elif local_name:
                named.append(device)
            else:
                unnamed.append(device)   # Windows often omits the name

        candidates = named or unnamed
        if not candidates:
            hint = f" named {self._name!r}" if self._name else ""
            raise ConnectionError(
                f"no Pybricks hub{hint} found in {DISCOVERY_TIMEOUT_S:.0f} s. "
                "Press the hub's center button to wake it; make sure Pybricks "
                "Code / the LEGO app is not connected (a hub serves one host "
                "at a time). If it still does not appear, run "
                "'python action.py --scan' and set HUB_ADDRESS in .env.")
        log.info("found hub at %s", candidates[0].address)
        return candidates[0]

    @staticmethod
    def _link_error(exc: Exception) -> str:
        try:
            from bleak.exc import BleakBluetoothNotAvailableError
        except ImportError:  # pragma: no cover
            BleakBluetoothNotAvailableError = ()
        if isinstance(exc, BleakBluetoothNotAvailableError):
            return ("Bluetooth is not available on this PC. Turn Bluetooth on "
                    "(Windows action center) and retry.")
        return f"Bluetooth scan failed: {type(exc).__name__}: {exc}"

    # -- transport -------------------------------------------------------

    def _on_stdout(self, data: bytes) -> None:
        self._buffer.extend(data)
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                return
            raw = bytes(self._buffer[:index])
            del self._buffer[:index + 1]
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # Usually a hub traceback or stray print() - surface it so
                # startup crashes are not silently swallowed.
                log.warning("hub said (non-JSON): %s", line.decode("utf-8", "replace"))
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "alert":
                # Unsolicited hub message (obstacle warning). Handle it here
                # instead of the inbox so command waits never swallow it.
                handler = self._alert_handler
                if handler is not None:
                    try:
                        handler(message)
                    except Exception as exc:
                        log.warning("alert handler failed: %s", exc)
                continue
            self._inbox.put_nowait(message)

    async def send(self, message: dict[str, Any]) -> None:
        if not self._connected:
            raise ConnectionError("hub not connected")
        try:
            await self._hub.write_line(json.dumps(message))
        except Exception as exc:
            raise ConnectionError(f"hub write failed: {exc}") from exc

    async def receive(self, timeout: float) -> Optional[dict[str, Any]]:
        try:
            return await asyncio.wait_for(self._inbox.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def _heartbeat(self) -> None:
        """Keeps the hub's 2 s watchdog fed while we are connected."""
        try:
            while self._connected:
                await self.send({"type": "ping"})
                await asyncio.sleep(self._heartbeat_s)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("heartbeat stopped: %s", exc)

    async def _wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            message = await self.receive(remaining)
            if message is not None and predicate(message):
                return message

    # -- commands --------------------------------------------------------

    def _command_id(self) -> int:
        self._next_command_id += 1
        return self._next_command_id

    def _done_timeout(self, command: dict[str, Any]) -> float:
        params = command.get("parameters") or {}
        try:
            duration = float(params.get("duration_ms", 0)) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0
        return min(MAX_DONE_WAIT_S, DONE_MARGIN_S + max(0.0, duration))

    async def run_command(self, command: dict[str, Any]) -> tuple[bool, str]:
        """Send one plan command and wait for its done/error reply."""
        cid = self._command_id()
        action = command.get("action")
        parameters = command.get("parameters") or {}
        await self.send({"id": cid, "action": action, "parameters": parameters})

        ack = await self._wait_for(lambda m: m.get("id") == cid,
                                   self._ack_timeout_s)
        if ack is None:
            return False, "no ack from hub (is the car connected and awake?)"
        if ack.get("type") == "error":
            return False, str(ack.get("error", "hub error"))

        result = await self._wait_for(
            lambda m: m.get("id") == cid and m.get("type") in ("done", "error"),
            self._done_timeout(command))
        if result is None:
            return False, "timed out waiting for 'done'"
        if result.get("type") == "error":
            return False, str(result.get("error", "hub error"))
        return True, "ok"

    async def stop(self) -> bool:
        """Immediate stop (does not wait for the full protocol round-trip)."""
        try:
            cid = self._command_id()
            await self.send({"id": cid, "action": "stop", "parameters": {}})
            await self._wait_for(
                lambda m: m.get("id") == cid and m.get("type") in ("done", "error"),
                2.0)
            return True
        except ConnectionError:
            return False


# ---------------------------------------------------------------- simulation

class SimHub:
    """Stand-in hub for trying the voice/AI pipeline without Bluetooth."""

    def __init__(self) -> None:
        self._connected = False
        self._next_command_id = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def set_alert_handler(self, handler) -> None:
        """Simulation has no sensors, so alerts are never produced."""

    async def connect(self) -> None:
        self._connected = True
        print("[sim] simulated hub ready (no Bluetooth)")

    async def disconnect(self) -> None:
        self._connected = False

    async def run_command(self, command: dict[str, Any]) -> tuple[bool, str]:
        self._next_command_id += 1
        cid = self._next_command_id
        action = command.get("action")
        parameters = command.get("parameters") or {}
        print(f"[sim] -> #{cid} {action} {json.dumps(parameters)}")
        try:
            duration = float(parameters.get("duration_ms", 0)) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            await asyncio.sleep(min(duration, 10.0))
        print(f"[sim] <- #{cid} done")
        return True, "simulated"

    async def stop(self) -> bool:
        self._next_command_id += 1
        print(f"[sim] -> #{self._next_command_id} stop")
        print(f"[sim] <- #{self._next_command_id} done")
        return True


# ---------------------------------------------------------------- session

def _print_plan(plan: dict[str, Any]) -> None:
    print(f"Plan #{plan['plan_id']}: {plan.get('reasoning', '')}")
    for command in plan["commands"]:
        print(f"  {command['command_id']}. {command['action']:<9} "
              f"{json.dumps(command['parameters'])}")


def describe_command(command: dict[str, Any]) -> str:
    """Short phrase spoken as each command starts."""
    action = command.get("action")
    params = command.get("parameters") or {}
    if action == "forward":
        return "Driving forward."
    if action == "reverse":
        return "Reversing."
    if action == "left":
        return "Turning left."
    if action == "right":
        return "Turning right."
    if action == "straight":
        return "Going straight."
    if action == "stop":
        return "Stopping."
    if action == "horn":
        return "Honking."
    return ""


def describe_alert(message: dict[str, Any]) -> str:
    """Phrase for an unsolicited hub alert (obstacle warning)."""
    kind = message.get("kind")
    if kind == "clear":
        return "Path clear."
    if kind == "obstacle":
        side = message.get("side")
        if side == "front":
            return "Obstacle ahead."
        if side == "back":
            return "Obstacle behind."
        return "Obstacle nearby."
    return ""


async def _listen_for_interrupt(listener: STT.Listener,
                                command_task: asyncio.Task) -> Optional[str]:
    """Wait for a command, but return early if the driver says stop/wait/quit.

    Returns the interrupt phrase, or None when the command finished first.
    """
    listen_task = asyncio.create_task(asyncio.to_thread(listener.listen))
    try:
        while True:
            done, _ = await asyncio.wait(
                {command_task, listen_task},
                return_when=asyncio.FIRST_COMPLETED)

            if listen_task in done:
                try:
                    text = listen_task.result()
                except STT.SpeechError as exc:
                    log.warning("interrupt listener stopped: %s", exc)
                    return None
                if text and is_interrupt(text):
                    return text
                if command_task in done:
                    return None
                # Heard something else: keep watching for an interrupt.
                listen_task = asyncio.create_task(
                    asyncio.to_thread(listener.listen))
                continue

            # The command finished first; stop the microphone wait.
            return None
    finally:
        if not listen_task.done():
            listener.cancel()
        try:
            # Always await the worker thread so it releases the mic lock and
            # so an unused result/exception is not left dangling.
            await listen_task
        except STT.SpeechError:
            pass
        except Exception:
            pass


async def execute_plan(link, commands: list[dict[str, Any]],
                       speaker: Optional[TTS.Speaker] = None,
                       listener: Optional[STT.Listener] = None
                       ) -> tuple[bool, Optional[str]]:
    """Run a plan, narrating it and listening for an interrupt.

    Returns (ok, interrupt_text). When interrupt_text is set, the driver said
    stop/wait/quit while the car was moving: the command was cancelled, the
    car was stopped, and the remaining commands must not run.
    """
    for command in commands:
        # Narrate each action as it starts, so the driver always knows what
        # the car is doing (the hub itself can only beep).
        if speaker is not None:
            phrase = describe_command(command)
            if phrase:
                speaker.speak(phrase)

        command_task = asyncio.create_task(link.run_command(command))

        if listener is not None:
            # Let the narration finish before opening the mic, otherwise the
            # microphone hears the car's own voice and interrupts itself.
            if speaker is not None:
                await asyncio.to_thread(speaker.wait, 10.0)
            interrupt_text = await _listen_for_interrupt(listener, command_task)
            if interrupt_text:
                command_task.cancel()
                try:
                    await command_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                await link.stop()
                # The caller decides what to say (and whether to quit or
                # enter emergency-stop mode) from the interrupt phrase.
                print(f"  -> {command['action']:<9} INTERRUPTED "
                      f"({interrupt_text})")
                return False, interrupt_text

        ok, detail = await command_task
        status = "OK" if ok else f"FAILED ({detail})"
        print(f"  -> {command['action']:<9} {status}")
        if not ok:
            print("Aborting the rest of the plan for safety.")
            return False, None

    if speaker is not None:
        speaker.speak("Done.")
    return True, None


async def run_session(link, args: argparse.Namespace) -> int:
    await link.connect()
    listener: Optional[STT.Listener] = None
    speaker = TTS.Speaker(language=getattr(args, "tts_lang", None),
                          enabled=not getattr(args, "no_tts", False))
    emergency = False

    def on_alert(message: dict[str, Any]) -> None:
        phrase = describe_alert(message)
        if phrase:
            speaker.speak(phrase)

    link.set_alert_handler(on_alert)

    try:
        if args.once is None and not args.text:
            listener = STT.Listener(timeout_s=args.stt_timeout,
                                    mic=args.mic,
                                    language=args.language)
            listener.warm_up()

        speaker.speak("Car ready.")
        print("Car ready. Say 'stop', 'estop', 'reset' or 'quit'.")
        while True:
            if args.once is not None:
                text = args.once
            elif listener is not None:
                # Let the car finish talking before opening the microphone,
                # otherwise the mic hears the car's own voice.
                speaker.wait(10.0)
                print("Listening...")
                try:
                    text = await asyncio.to_thread(listener.listen)
                except STT.SpeechError as exc:
                    print(f"STT error: {exc}", file=sys.stderr)
                    return 1
            else:
                try:
                    text = await asyncio.to_thread(input, "Command> ")
                except EOFError:
                    return 0

            text = (text or "").strip()
            if not text:
                if args.once is not None:
                    return 0
                print("(nothing heard)")
                continue

            print(f"Heard: {text}")

            if matches_words(text, QUIT_WORDS):
                speaker.speak("Goodbye.")
                return 0
            if matches_words(text, ESTOP_WORDS):
                await link.stop()
                emergency = True
                speaker.speak("Emergency stop.")
                print("EMERGENCY STOP. Say 'reset' to clear.")
            elif matches_words(text, RESET_WORDS):
                emergency = False
                speaker.speak("Emergency stop cleared.")
                print("Emergency stop cleared.")
            elif matches_words(text, STOP_WORDS):
                await link.stop()
                speaker.speak("Stopping.")
                print("Stopped.")
            elif emergency:
                speaker.speak("Emergency stop is active. Say reset first.")
                print("Emergency stop is active - say 'reset' first.")
            else:
                try:
                    plan = await asyncio.to_thread(AI.plan, text)
                except AI.AIError as exc:
                    print(f"AI error: {exc}", file=sys.stderr)
                    speaker.speak("Sorry, I could not understand that command.")
                else:
                    if not plan["commands"]:
                        print("No car command understood.")
                        speaker.speak("Sorry, I did not understand that.")
                    else:
                        _print_plan(plan)
                        ok, interrupt_text = await execute_plan(
                            link, plan["commands"], speaker, listener)
                        if interrupt_text:
                            if matches_words(interrupt_text, QUIT_WORDS):
                                speaker.speak("Goodbye.")
                                return 0
                            if matches_words(interrupt_text, ESTOP_WORDS):
                                emergency = True
                                speaker.speak("Emergency stop.")
                                print("EMERGENCY STOP. Say 'reset' to clear.")
                            else:
                                speaker.speak("Stopped.")
                                print("Stopped. Listening for a new command.")
                        elif not ok:
                            speaker.speak("Command failed.")

            if args.once is not None:
                return 0
    finally:
        if listener is not None:
            listener.close()
        speaker.wait(3.0)
        speaker.close()
        await link.disconnect()


# ---------------------------------------------------------------- diagnostics

async def scan_devices(timeout: float = DISCOVERY_TIMEOUT_S) -> None:
    try:
        from bleak import BleakScanner
    except ImportError:
        print("bleak is not installed: pip install bleak", file=sys.stderr)
        return

    print(f"Scanning for BLE devices ({timeout:.0f} s) ...")
    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return

    for address, pair in sorted(found.items()):
        device, adv = pair if isinstance(pair, tuple) else (pair, None)
        name = (getattr(device, "name", None)
                or getattr(adv, "local_name", None) or "(no name)")
        services = getattr(adv, "service_uuids", None) or []
        print(f"  {address}  {name}")
        for service in services:
            print(f"      service {service}")


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config()

    parser = argparse.ArgumentParser(
        description="Drive the LEGO car with voice commands.")
    parser.add_argument("--text", action="store_true",
                        help="type commands instead of speaking")
    parser.add_argument("--simulate", action="store_true",
                        help="no hub: print and pace the commands")
    parser.add_argument("--once", metavar="COMMAND",
                        help="run one command, then exit")
    parser.add_argument("--usb", action="store_true",
                        help="connect over USB instead of BLE")
    parser.add_argument("--scan", action="store_true",
                        help="list nearby BLE devices and exit")
    parser.add_argument("--hub-name", default=cfg["hub_name"],
                        help="Bluetooth name filter for the hub")
    parser.add_argument("--hub-address", default=cfg["hub_address"],
                        help="Bluetooth address (skips discovery)")
    parser.add_argument("--script", default=cfg["hub_script"],
                        help="hub program to deploy")
    parser.add_argument("--mic", default=None,
                        help="microphone index or name substring")
    parser.add_argument("--language", default=STT.DEFAULT_LANGUAGE,
                        help=f"speech language (default: {STT.DEFAULT_LANGUAGE})")
    parser.add_argument("--stt-timeout", type=float,
                        default=STT.DEFAULT_TIMEOUT_S,
                        help="seconds to wait for speech to start")
    parser.add_argument("--ack-timeout", type=float,
                        default=cfg["ack_timeout_s"],
                        help="seconds to wait for a hub ack")
    parser.add_argument("--wait-hub", type=float, default=0.0, metavar="SECONDS",
                        help="keep scanning up to SECONDS for the hub to wake "
                             "(press its center button after starting)")
    parser.add_argument("--no-tts", action="store_true",
                        help="disable spoken feedback")
    parser.add_argument("--tts-lang", default=None, metavar="CODE",
                        help="gTTS language code, e.g. en or ar "
                             "(default: TTS_LANG from .env, else en)")
    parser.add_argument("--verbose", action="store_true",
                        help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    if args.scan:
        asyncio.run(scan_devices())
        return 0

    if args.simulate:
        link = SimHub()
    else:
        transport = "usb" if args.usb else cfg["hub_link"]
        link = HubLink(script=args.script, name=args.hub_name,
                       address=args.hub_address, transport=transport,
                       heartbeat_s=cfg["heartbeat_s"],
                       ack_timeout_s=args.ack_timeout,
                       wait_hub_s=args.wait_hub)

    try:
        return asyncio.run(run_session(link, args))
    except KeyboardInterrupt:
        print("\nGoodbye.")
        return 0
    except ConnectionError as exc:
        print(f"Hub connection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
