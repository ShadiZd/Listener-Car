"""STT.py - convert live speech to text for the smart car.

Captures microphone audio with ``sounddevice`` (no PyAudio needed, which keeps
it working on Python 3.14) and transcribes it with SpeechRecognition's free
Google Web Speech API. Needs internet for transcription.

Standalone usage:
    python STT.py                  listen continuously, print each phrase
    python STT.py --once           listen for one phrase and exit
    python STT.py --text           type commands instead of speaking
    python STT.py --file rec.wav   transcribe a WAV file (mic not needed)
    python STT.py --list-devices   show microphones and their indexes
    python STT.py --mic 2          choose a mic by index
    python STT.py --mic Realtek    choose a mic by name substring
    python STT.py --language ar-EG choose the recognition language
    python STT.py --timeout 8      seconds to wait for speech to start
    python STT.py --no-calibrate   skip ambient-noise calibration
    python STT.py --debug          show levels / thresholds

As a module (used by action.py later):
    import STT
    text = STT.listen(timeout_s=8.0)   # None when nothing was understood

    # or keep one calibrated listener alive:
    listener = STT.Listener()
    text = listener.listen()
"""
from __future__ import annotations

import argparse
import atexit
import queue
import sys
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

# ---------------------------------------------------------------- constants

SAMPLE_RATE = 16000          # preferred rate; falls back to the device default
SAMPLE_WIDTH = 2             # bytes per sample (int16)
CHANNELS = 1
BLOCK_MS = 50                # audio callback block size
PHRASE_LIMIT_S = 10.0        # longest single phrase we record
SILENCE_S = 0.8              # silence that ends a phrase
PREROLL_S = 0.6              # audio kept from before speech was detected
TAIL_PAD_S = 0.5             # silence kept after the last voiced block
MIN_SPEECH_S = 0.2           # shorter recordings are treated as noise
CALIBRATION_S = 0.5          # ambient-noise measurement window
SETTLE_S = 0.25              # discard audio while the mic device starts up
ENERGY_MULTIPLIER = 3.0      # speech threshold = ambient RMS * this
MIN_SPEECH_RMS = 120.0       # absolute floor so a silent room does not trigger
DEFAULT_TIMEOUT_S = 8.0      # wait for speech to start
DEFAULT_LANGUAGE = "en-US"


class SpeechError(Exception):
    """Microphone or recognition failure."""


# ---------------------------------------------------------------- helpers

def _rms(block: np.ndarray) -> float:
    """Root-mean-square level of an int16 audio block."""
    if block.size == 0:
        return 0.0
    data = block.astype(np.float32).flatten()
    return float(np.sqrt(np.mean(data * data)))


def list_microphones() -> None:
    """Print available input devices (index, name, default marker)."""
    try:
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover
        raise SpeechError("sounddevice is not installed: pip install sounddevice") from exc

    devices = sd.query_devices()
    default_in = sd.default.device[0]
    found = False
    for index, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        found = True
        marker = " (default)" if index == default_in else ""
        print(f"[{index}] {dev['name']}{marker}")
    if not found:
        print("No input devices found. Is a microphone connected?")


def _resolve_device(mic: Optional[object]) -> Optional[int]:
    """Turn a mic index / name substring / None into a device index."""
    if mic is None or mic == "":
        return None
    if isinstance(mic, int):
        return mic
    text = str(mic)
    if text.isdigit():
        return int(text)
    try:
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover
        raise SpeechError("sounddevice is not installed: pip install sounddevice") from exc

    needle = text.lower()
    for index, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0 and needle in dev["name"].lower():
            return index
    raise SpeechError(f"microphone {text!r} not found; run: python STT.py --list-devices")


def _pick_sample_rate(device: Optional[int]) -> int:
    """Use 16 kHz when the device supports it, else the device default."""
    try:
        import sounddevice as sd
    except ImportError as exc:  # pragma: no cover
        raise SpeechError("sounddevice is not installed: pip install sounddevice") from exc

    try:
        sd.check_input_settings(device=device, samplerate=SAMPLE_RATE,
                                channels=CHANNELS, dtype="int16")
        return SAMPLE_RATE
    except Exception:
        try:
            info = sd.query_devices(device)
            rate = int(info["default_samplerate"])
            return rate if rate > 0 else 44100
        except Exception:
            return 44100


def transcribe(samples: np.ndarray, sample_rate: int = SAMPLE_RATE,
               language: str = DEFAULT_LANGUAGE) -> Optional[str]:
    """Transcribe int16 PCM samples with the free Google Web Speech API.

    Returns the text, or None when speech was unintelligible.
    Raises SpeechError when the recognizer is unavailable (offline, quota...).
    """
    try:
        import speech_recognition as sr
    except ImportError as exc:  # pragma: no cover
        raise SpeechError(
            "SpeechRecognition is not installed: pip install SpeechRecognition") from exc

    raw = np.ascontiguousarray(samples, dtype=np.int16).tobytes()
    audio = sr.AudioData(raw, sample_rate, SAMPLE_WIDTH)
    recognizer = sr.Recognizer()
    try:
        text = recognizer.recognize_google(audio, language=language)
    except sr.UnknownValueError:
        return None
    except sr.RequestError as exc:
        raise SpeechError(f"Google Web Speech unreachable: {exc}") from None
    text = (text or "").strip()
    return text or None


def transcribe_file(path: str, language: str = DEFAULT_LANGUAGE) -> Optional[str]:
    """Transcribe an audio file (WAV/AIFF/FLAC) without using the microphone."""
    try:
        import speech_recognition as sr
    except ImportError as exc:  # pragma: no cover
        raise SpeechError(
            "SpeechRecognition is not installed: pip install SpeechRecognition") from exc

    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(path) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_google(audio, language=language)
    except sr.UnknownValueError:
        return None
    except sr.RequestError as exc:
        raise SpeechError(f"Google Web Speech unreachable: {exc}") from None
    except (OSError, ValueError) as exc:
        raise SpeechError(f"cannot read {path!r}: {exc}") from None
    text = (text or "").strip()
    return text or None


# ---------------------------------------------------------------- listener

class Listener:
    """Push-to-talk style listener: waits for speech, records a phrase, stops
    after a short silence, and returns the transcribed text.

    Ambient noise is calibrated once, on the first call.
    """

    def __init__(self, mic: Optional[object] = None,
                 language: str = DEFAULT_LANGUAGE,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 phrase_limit_s: float = PHRASE_LIMIT_S,
                 calibrate: bool = True,
                 debug: bool = False) -> None:
        self.language = language
        self.timeout_s = timeout_s
        self.phrase_limit_s = phrase_limit_s
        self.debug = debug
        self._device = _resolve_device(mic)
        self._sample_rate = _pick_sample_rate(self._device)
        self._calibrate = calibrate
        self._calibrated = False
        self._threshold = MIN_SPEECH_RMS
        self._stream = None
        self._queue: Optional["queue.Queue[np.ndarray]"] = None
        self._cancel = threading.Event()
        self._listen_lock = threading.Lock()

    # -- audio stream (opened once, kept warm) ---------------------------

    def _ensure_stream(self) -> None:
        """Open the microphone once and keep it running between phrases.

        Reopening the stream for every phrase drops the first ~0.5 s of audio
        while PortAudio initialises - that used to eat the first word of every
        command (and all of a short one-word command).
        """
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover
            raise SpeechError(
                "sounddevice is not installed: pip install sounddevice") from exc

        q: "queue.Queue[np.ndarray]" = queue.Queue()

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            q.put(indata.copy())

        block = max(1, self._sample_rate * BLOCK_MS // 1000)
        try:
            stream = sd.InputStream(samplerate=self._sample_rate,
                                    channels=CHANNELS, dtype="int16",
                                    blocksize=block, device=self._device,
                                    callback=callback)
            stream.start()
        except Exception as exc:
            raise SpeechError(f"microphone unavailable: {exc}") from None

        self._stream = stream
        self._queue = q
        # Let the device settle, then throw away its startup audio.
        time.sleep(SETTLE_S)
        self._drain()

    def _drain(self) -> None:
        """Discard audio buffered before we started listening."""
        q = self._queue
        if q is None:
            return
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def warm_up(self) -> None:
        """Open the mic and calibrate before prompting the user to speak.

        Calibration must finish before the prompt: otherwise the first words
        you say are measured as ambient noise and can be cut off.
        """
        self._ensure_stream()
        if not self._calibrated:
            if self._calibrate:
                self._calibrate_noise()
            else:
                self._calibrated = True

    def close(self) -> None:
        """Stop and release the microphone."""
        stream, self._stream = self._stream, None
        self._queue = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def cancel(self) -> None:
        """Abort an in-progress listen() from another thread.

        The next listen() starts fresh; this only makes the current capture
        return None quickly so action.py can stop waiting for a phrase.
        """
        self._cancel.set()

    def __enter__(self) -> "Listener":
        self.warm_up()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _calibrate_noise(self) -> None:
        """Measure the room and set the speech threshold."""
        self._ensure_stream()
        q = self._queue
        assert q is not None

        readings: list[float] = []
        deadline = time.monotonic() + CALIBRATION_S
        while time.monotonic() < deadline:
            try:
                readings.append(_rms(q.get(timeout=0.2)))
            except queue.Empty:
                continue

        ambient = float(np.median(readings)) if readings else 0.0
        self._threshold = max(ambient * ENERGY_MULTIPLIER, MIN_SPEECH_RMS)
        self._calibrated = True
        if self.debug:
            print(f"[stt] ambient RMS {ambient:.1f} -> threshold "
                  f"{self._threshold:.1f} @ {self._sample_rate} Hz", file=sys.stderr)

    def _capture(self) -> Optional[np.ndarray]:
        """Blocking capture of one phrase. Returns int16 samples or None."""
        if not self._calibrated:
            if self._calibrate:
                self._calibrate_noise()
            else:
                self._calibrated = True

        try:
            self._ensure_stream()
            self._drain()
            return self._record_phrase()
        except SpeechError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise SpeechError(f"microphone unavailable: {exc}") from None

    def _record_phrase(self) -> Optional[np.ndarray]:
        q = self._queue
        assert q is not None
        block_s = BLOCK_MS / 1000.0
        samples_per_block = max(1, int(self._sample_rate * block_s))
        preroll_blocks = max(1, int(PREROLL_S / block_s))
        silence_blocks_needed = max(1, int(SILENCE_S / block_s))
        tail_pad_blocks = max(1, int(TAIL_PAD_S / block_s))

        deadline = time.monotonic() + self.timeout_s
        preroll: deque[np.ndarray] = deque(maxlen=preroll_blocks)
        collected: list[np.ndarray] = []
        started = False
        silence_blocks = 0
        speech_started_at = 0.0

        while True:
            if self._cancel.is_set():
                self._cancel.clear()
                return None
            if not started and time.monotonic() >= deadline:
                return None
            try:
                block = q.get(timeout=0.2)
            except queue.Empty:
                continue

            level = _rms(block)
            if self.debug and started:
                print(f"[stt] level {level:.0f}", file=sys.stderr)

            if not started:
                preroll.append(block)
                if level >= self._threshold:
                    started = True
                    speech_started_at = time.monotonic()
                    collected.extend(preroll)
                    if self.debug:
                        print(f"[stt] speech started (level {level:.0f} >= "
                              f"{self._threshold:.0f})", file=sys.stderr)
                continue

            collected.append(block)
            if level < self._threshold:
                silence_blocks += 1
                if silence_blocks >= silence_blocks_needed:
                    break
            else:
                silence_blocks = 0

            if time.monotonic() - speech_started_at >= self.phrase_limit_s:
                break

        if not collected:
            return None

        samples = np.concatenate(collected).flatten()

        # Trim trailing silence, keeping a short pad after the last voice.
        keep_blocks = len(collected) - silence_blocks + tail_pad_blocks
        if silence_blocks and 0 < keep_blocks < len(collected):
            samples = samples[: keep_blocks * samples_per_block]

        if samples.size / self._sample_rate < MIN_SPEECH_S:
            return None
        if self.debug:
            print(f"[stt] captured {samples.size / self._sample_rate:.2f} s",
                  file=sys.stderr)
        return samples

    # -- public API ------------------------------------------------------

    def listen(self) -> Optional[str]:
        """Wait for one phrase and return its text (None if unintelligible).

        Safe to cancel from another thread with cancel(); the call then
        returns None. A lock keeps two listeners from sharing the mic.
        """
        with self._listen_lock:
            samples = self._capture()
            if samples is None:
                return None
            return transcribe(samples, self._sample_rate, self.language)


# Module-level convenience: keeps one calibrated listener alive for action.py.
_default_listener: Optional[Listener] = None


def listen(timeout_s: float = DEFAULT_TIMEOUT_S,
           language: str = DEFAULT_LANGUAGE,
           mic: Optional[object] = None,
           debug: bool = False) -> Optional[str]:
    """Listen for one phrase with a shared, auto-calibrated listener."""
    global _default_listener
    if (_default_listener is None
            or _default_listener.timeout_s != timeout_s
            or _default_listener.language != language
            or _default_listener.debug != debug):
        if _default_listener is not None:
            _default_listener.close()
        _default_listener = Listener(mic=mic, language=language,
                                     timeout_s=timeout_s, debug=debug)
        atexit.register(_default_listener.close)
    return _default_listener.listen()


# ---------------------------------------------------------------- CLI

def _run_text_mode() -> int:
    print("Text mode - type a command, Ctrl+C to quit.")
    while True:
        try:
            text = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text:
            print(f"Heard: {text}")


def _run_mic_mode(listener: Listener, once: bool) -> int:
    try:
        listener.warm_up()   # open the mic BEFORE prompting, so no word is lost
    except SpeechError as exc:
        print(f"STT error: {exc}", file=sys.stderr)
        return 1
    print("Speak now (Ctrl+C to quit).")
    try:
        while True:
            try:
                text = listener.listen()
            except KeyboardInterrupt:
                print()
                return 0
            except SpeechError as exc:
                print(f"STT error: {exc}", file=sys.stderr)
                return 1
            if text:
                print(f"Heard: {text}")
            else:
                print("(nothing heard)")
            if once:
                return 0
    finally:
        listener.close()


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Convert speech to text for the smart car.")
    parser.add_argument("--once", action="store_true",
                        help="listen for one phrase, then exit")
    parser.add_argument("--text", action="store_true",
                        help="type commands instead of using the microphone")
    parser.add_argument("--file", metavar="PATH",
                        help="transcribe an audio file and exit")
    parser.add_argument("--list-devices", action="store_true",
                        help="list microphones and exit")
    parser.add_argument("--mic", metavar="INDEX|NAME",
                        help="microphone index or name substring")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        help=f"recognition language (default: {DEFAULT_LANGUAGE})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"seconds to wait for speech to start "
                             f"(default: {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--no-calibrate", action="store_true",
                        help="skip ambient-noise calibration")
    parser.add_argument("--debug", action="store_true",
                        help="print audio levels and thresholds")
    args = parser.parse_args(argv)

    try:
        if args.list_devices:
            list_microphones()
            return 0
        if args.file:
            text = transcribe_file(args.file, args.language)
            print(f"Heard: {text}" if text else "(nothing understood)")
            return 0
        if args.text:
            return _run_text_mode()

        listener = Listener(mic=args.mic, language=args.language,
                            timeout_s=args.timeout,
                            calibrate=not args.no_calibrate,
                            debug=args.debug)
        return _run_mic_mode(listener, once=args.once)
    except SpeechError as exc:
        print(f"STT error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
