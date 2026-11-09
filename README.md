# Smart Car — Voice-Controlled LEGO SPIKE Prime

A small LEGO SPIKE Prime car that listens to spoken commands, asks Google
Gemini to turn them into a short driving plan, and executes that plan over
Bluetooth (or USB). It talks back through the laptop speakers, warns about
obstacles, and can be interrupted mid-drive with a word like **"wait"**.

---

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Hardware](#hardware)
- [Project files](#project-files)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [Usage](#usage)
- [Voice commands](#voice-commands)
- [Interrupting a command](#interrupting-a-command)
- [Spoken feedback](#spoken-feedback)
- [Obstacle warning](#obstacle-warning)
- [Safety behaviour](#safety-behaviour)
- [Hub protocol](#hub-protocol)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

---

## How it works

```
You speak
   │
   ▼
STT.py  ──text──►  AI.py (Gemini)  ──plan──►  action.JSON
   ▲                                            │
   │                                            ▼
   │                                       action.py ──► TTS.py (laptop speakers)
   │                                            │
   │                                 JSON lines over BLE / USB
   │                                            ▼
   └── "wait" / "stop" ◄──────────────  main_code.py on the hub
                                         drive motors A + E
                                         steering motor B
                                         ultrasonic F + C
                                         speaker + light
```

1. **STT.py** records one phrase and transcribes it with the free Google Web
   Speech API.
2. **AI.py** sends the text to Gemini (`gemini-3.1-flash-lite`), validates and
   clamps the answer, and writes the plan to **action.JSON**.
3. **action.py** deploys/connects to the hub, sends each command, narrates it
   with **TTS.py**, and keeps listening for an interrupt while the car moves.
4. **main_code.py** runs on the hub, drives the motors, reads the ultrasonic
   sensors, beeps, and replies `ack` / `done` / `error`.

---

## Features

- Voice, typed, one-shot, and fully simulated modes.
- Gemini plans up to **5** low-level actions per request.
- Spoken narration for every action ("Driving forward.", "Turning left.",
  "Honking.") plus "Done.".
- **Interruptible driving** — say "wait", "stop", "pause", "emergency stop",
  or "quit" while the car is moving.
- Obstacle warnings: a soft, low, slow hub beep plus spoken
  "Obstacle ahead." / "Obstacle behind." / "Path clear.".
- Emergency-stop state that blocks new commands until "reset".
- 2-second hub watchdog: the car stops if the laptop goes silent.
- Automatic reconnect retries and `--wait-hub` scanning for a sleeping hub.
- No ffmpeg needed (TTS uses miniaudio).

---

## Hardware

| Part | Port | Notes |
|---|---|---|
| Drive motor (one side) | **A** | |
| Drive motor (other side) | **E** | `Direction.COUNTERCLOCKWISE` |
| Steering motor | **B** | Straight ahead is **-10°** |
| Ultrasonic sensor (front) | **F** | Distances in mm |
| Ultrasonic sensor (back) | **C** | Distances in mm |
| Hub | — | LEGO SPIKE Prime with **Pybricks** firmware |

The hub must run the **Pybricks** firmware, not the stock LEGO firmware.
Flash it with [Pybricks Code](https://pybricks.com/).

The laptop needs a microphone and speakers (or headphones). Speech comes out of
the **laptop**, because the hub speaker can only beep.

---

## Project files

| File | Runs on | Purpose |
|---|---|---|
| `action.py` | Laptop | Main entry point: mic loop, hub link, interrupts, TTS |
| `AI.py` | Laptop | Gemini planner + validation, writes `action.JSON` |
| `STT.py` | Laptop | Microphone capture and speech-to-text |
| `TTS.py` | Laptop | Spoken feedback (gTTS + miniaudio) |
| `main_code.py` | Hub | Motor/sensor control and the JSON-lines protocol |
| `action.JSON` | Laptop | Latest validated plan (hand-off between AI and action) |
| `req.txt` | Laptop | Python dependencies |
| `.env` | Laptop | API key and local settings (**do not share**) |

---

## Requirements

- **Python 3.10+** (tested on 3.14.6).
- Internet access for:
  - Gemini (planning),
  - Google Web Speech (transcription),
  - gTTS (speech synthesis).
- A Google **Gemini API key**.
- Bluetooth LE or a USB data cable.
- LEGO SPIKE Prime hub running Pybricks.

---

## Setup

### 1. Flash Pybricks

Install the Pybricks firmware on the hub using
[Pybricks Code](https://pybricks.com/). `main_code.py` is deployed
automatically by `action.py` on every connect, so you do not need to upload it
manually.

### 2. Install the Python packages

```powershell
cd "smart_car_1.0"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r req.txt
```

On macOS/Linux use `source .venv/bin/activate` instead.

### 3. Create `.env`

Create a file named `.env` next to `action.py`:

```dotenv
# Required
LLM_api=your_gemini_api_key_here

# Optional: skip Bluetooth discovery by giving the hub address
HUB_ADDRESS=FD:1D:06:0F:94:1C

# Optional settings (defaults shown)
# HUB_NAME=
# HUB_LINK=ble
# HUB_SCRIPT=main_code.py
# HEARTBEAT_S=0.5
# ACK_TIMEOUT_S=2.0
# TTS_ENABLED=1
# TTS_LANG=en
```

Never commit `.env` or share the API key.

### 4. Prepare the Bluetooth connection

- The hub serves **one host at a time**. Close **Pybricks Code**, the **LEGO
  app**, and any phone connection before running `action.py`.
- The hub **sleeps** after inactivity. Press its **center button** to wake it.
- If Windows cannot find it, toggle Bluetooth off/on, or remove and re-pair the
  hub in Windows Bluetooth settings.
- To find the hub address:

  ```powershell
  python action.py --scan
  ```

- To start the laptop first and wake the hub afterwards:

  ```powershell
  python action.py --wait-hub 30
  ```

---

## Configuration

Settings are read from `.env` (or real environment variables).

| Variable | Default | Meaning |
|---|---|---|
| `LLM_api` | — | **Required.** Gemini API key |
| `HUB_NAME` | empty | Optional Bluetooth name filter |
| `HUB_ADDRESS` | empty | Hub MAC address; skips discovery |
| `HUB_LINK` | `ble` | `ble` or `usb` |
| `HUB_SCRIPT` | `main_code.py` | Program deployed to the hub |
| `HEARTBEAT_S` | `0.5` | Seconds between `ping` messages |
| `ACK_TIMEOUT_S` | `2.0` | Seconds to wait for a hub `ack` |
| `TTS_ENABLED` | `1` | `0` mutes spoken feedback |
| `TTS_LANG` | `en` | gTTS language code (`en`, `ar`, `fr`, …) |

Command-line flags override the `.env` values.

---

## Usage

### Voice mode (normal use)

```powershell
python action.py
```

Say a command when the laptop prints `Listening...`.

### Other modes

```powershell
# Type commands instead of speaking
python action.py --text

# No hub at all: print and pace the plan
python action.py --simulate

# Run one command, then exit
python action.py --once "drive forward for two seconds then honk"

# Connect over USB instead of Bluetooth
python action.py --usb

# List nearby Bluetooth devices and exit
python action.py --scan

# Wait up to 30 s for the hub to wake
python action.py --wait-hub 30

# Disable spoken feedback
python action.py --no-tts
```

### Flags

| Flag | Description |
|---|---|
| `--text` | Type commands instead of speaking |
| `--simulate` | No hub; print and pace commands |
| `--once COMMAND` | Run one command, then exit |
| `--usb` | Use USB instead of BLE |
| `--scan` | List nearby BLE devices and exit |
| `--hub-name NAME` | Bluetooth name filter |
| `--hub-address ADDR` | Bluetooth address (skips discovery) |
| `--script PATH` | Hub program to deploy |
| `--mic INDEX_OR_NAME` | Choose a microphone |
| `--language CODE` | Speech language (default `en-US`) |
| `--stt-timeout SECONDS` | Wait for speech to start (default `8`) |
| `--ack-timeout SECONDS` | Wait for a hub ack (default `2`) |
| `--wait-hub SECONDS` | Keep scanning for a sleeping hub |
| `--no-tts` | Disable spoken feedback |
| `--tts-lang CODE` | gTTS language (e.g. `en`, `ar`) |
| `--verbose` | Debug logging |

### Standalone tools

```powershell
# Microphone diagnostics
python STT.py --list-devices
python STT.py --once
python STT.py --file recording.wav

# Test the planner without driving
python AI.py "turn left then drive forward"
python AI.py --no-write "stop"
python AI.py --show-json "reverse slowly"

# Test the laptop speakers
python TTS.py "hello, I am your smart car"
```

---

## Voice commands

The planner understands natural phrases, for example:

- "drive forward for two seconds then honk then turn left"
- "go backward slowly"
- "turn right and go straight"
- "stop"
- "what is the weather" → no car command (nothing moves)

It maps them to these actions:

| Action | Parameters | Defaults / limits |
|---|---|---|
| `forward` | `speed` 1–1000, `duration_ms` 100–15000 | speed `800`, duration `8000` |
| `reverse` | `speed` 1–1000, `duration_ms` 100–15000 | speed `800`, duration `8000` |
| `left` | `angle` 1–45 | `45` |
| `right` | `angle` 1–45 | `45` |
| `straight` | none | centre the steering |
| `stop` | none | stop and centre the steering |
| `horn` | none | two short beeps |

- **Speed scale:** `800` = normal, `1000` = full speed. The hub divides by 10,
  so `800` → 80% motor power.
- **Duration:** default `8000 ms`, maximum `15000 ms`.
- **Plan size:** at most **5** actions per spoken command.
- Steering angles are offsets from the centre position (`-10°`).

The latest plan is always written to `action.JSON`, for example:

```json
{
  "plan_id": 171,
  "source_text": "reverse full speed full time",
  "reasoning": "Reverse at maximum speed for the maximum allowed duration.",
  "commands": [
    {
      "command_id": 1,
      "mode": "autonomous",
      "action": "reverse",
      "parameters": { "speed": 1000, "duration_ms": 15000 }
    }
  ]
}
```

---

## Interrupting a command

While a plan is running in **voice mode**, the laptop keeps listening. Say one
of these at any time:

| Type | Words |
|---|---|
| Stop / pause | `stop`, `halt`, `brake`, `wait`, `pause`, `hold`, `hold on`, `cancel`, `abort` |
| Emergency | `estop`, `e-stop`, `emergency stop` |
| Quit | `quit`, `exit`, `goodbye` |

What happens:

1. The running command is cancelled.
2. An immediate `stop` is sent to the hub.
3. The rest of the plan is skipped.
4. The laptop says **"Stopped."** and listens for a new command.

Special cases:

- **`emergency stop`** also enters the emergency state. Driving commands are
  refused until you say **`reset`**.
- **`quit`** says "Goodbye." and exits the program.

Example: a 10-second drive can be stopped at second 6 by saying **"wait"**.

> Interruption is available in the normal voice loop. `--text` and `--once`
> do not open the interrupt listener.

---

## Spoken feedback

Speech is synthesized with gTTS and played with miniaudio on the laptop.

| Event | Spoken phrase |
|---|---|
| Startup | "Car ready." |
| Forward | "Driving forward." |
| Reverse | "Reversing." |
| Left / right | "Turning left." / "Turning right." |
| Straight | "Going straight." |
| Stop | "Stopping." |
| Horn | "Honking." |
| Plan finished | "Done." |
| Interrupted | "Stopped." |
| Obstacle in front | "Obstacle ahead." |
| Obstacle behind | "Obstacle behind." |
| Obstacle gone | "Path clear." |

Generated MP3s are cached in `%TEMP%\smart_car_tts\` so repeated phrases start
instantly.

---

## Obstacle warning

The hub checks both ultrasonic sensors (front F, back C).

| Setting | Value |
|---|---|
| Warning starts | closer than **400 mm** |
| Warning clears | farther than **500 mm** (hysteresis) |
| Beep frequency | **440 Hz** |
| Beep length | **60 ms** |
| Beep volume | **35%** (restored to 100% afterwards) |
| Beep interval | 1600 ms / 1100 ms / 700 ms for < 400 / < 250 / < 150 mm |

When the warning starts (or switches side), the hub sends an alert to the
laptop so it can speak "Obstacle ahead." or "Obstacle behind.". When the path
clears it sends a clear alert and the laptop says "Path clear.".

**Hard avoidance:** at **≤ 100 mm** the hub takes over and backs the car away
from the obstacle; it releases at **> 130 mm**. The horn and startup beep are
unchanged.

---

## Safety behaviour

- **Watchdog:** if the laptop stops sending heartbeats for **2000 ms**, the hub
  stops the car and reports a watchdog error.
- **Maximum command duration:** 15 seconds per drive command.
- **Ack timeout:** commands without an acknowledgement fail after 2 seconds by
  default.
- **Plan abort:** if any command fails, the rest of the plan is skipped.
- **Emergency stop:** blocks new driving commands until `reset`.
- **Connection loss:** the hub watchdog stops the car even if the laptop dies.

---

## Hub protocol

The laptop and hub exchange **JSON lines** over the Pybricks stdin/stdout
channel.

Laptop → hub:

```json
{"id": 1, "action": "forward", "parameters": {"speed": 800, "duration_ms": 8000}}
{"type": "mode", "mode": "autonomous"}
ping
```

Hub → laptop:

```json
{"type": "ready"}
{"type": "mode", "mode": "autonomous"}
{"id": 1, "type": "ack"}
{"id": 1, "type": "done"}
{"id": 1, "type": "error", "error": "watchdog timeout (host silent)"}
```

Unsolicited obstacle alerts:

```json
{"type": "alert", "kind": "obstacle", "side": "front", "distance": 320}
{"type": "alert", "kind": "clear"}
```

`action.py` sends a `ping` heartbeat every 0.5 s. The hub resets its watchdog
on every line, including pings.

---

## Troubleshooting

| Problem | Try this |
|---|---|
| `no Pybricks hub found` | Press the hub's center button; close Pybricks Code / LEGO app / phone links; run `python action.py --scan` |
| Hub connects then disappears | Toggle Windows Bluetooth off/on; remove and re-pair the hub; use `--wait-hub 30` |
| Connection times out repeatedly | The hub serves one host at a time — disconnect other apps/devices |
| No microphone found | `python STT.py --list-devices`, then `--mic INDEX` or `--mic NAME` |
| Speech is not recognized | Move closer to the mic; reduce background noise; try `--language` |
| No spoken feedback | Check `TTS_ENABLED=1`, internet access, and laptop volume; try `python TTS.py "test"` |
| Gemini errors | Check `LLM_api` in `.env` and internet access |
| Car stops by itself | Obstacle avoidance (≤ 10 cm) or the 2-second watchdog |
| Commands do nothing | The car may be in emergency-stop state — say `reset` |
| `--once`/`--text` cannot be interrupted | Expected; interruption is voice-mode only |

---

## Limitations

- Internet is required for transcription, Gemini, and speech synthesis.
- The hub speaker only beeps; all voice comes from the laptop.
- One Bluetooth host at a time; close other apps while driving.
- Up to 5 actions per plan and 15 seconds per drive command.
- Interruption works in the normal voice loop only, not in `--text` or
  `--once`.
- Motor noise can occasionally trigger false speech detection; a quieter
  environment improves reliability.

---

## Quick start

```powershell
cd "smart_car_1.0"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r req.txt

# create .env with LLM_api=...
python action.py --scan        # find the hub address (optional)
python action.py               # drive with your voice
```

Then say:

> "Drive forward for ten seconds."

…and halfway through, say:

> "Wait."

The car stops, the laptop says "Stopped.", and it listens for your next
command.
