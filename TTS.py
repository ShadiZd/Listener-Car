"""TTS.py - spoken feedback for the smart car using gTTS + miniaudio.

The hub's speaker can only beep, so the voice comes out of the laptop's
speakers. Phrases are synthesized online with Google Translate TTS (gTTS)
and played with miniaudio (MP3, no ffmpeg needed).

A background thread plays a queue of phrases so driving is never blocked.
Call ``wait()`` before you start listening on the microphone, otherwise the
mic will hear the car talking.

Environment (.env):
    TTS_ENABLED=1        set to 0 to mute
    TTS_LANG=en          gTTS language code (en, ar, fr, ...)
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

DEFAULT_LANGUAGE = "en"
CACHE_DIR = Path(tempfile.gettempdir()) / "smart_car_tts"
MAX_CACHE_FILES = 60

log = logging.getLogger("tts")


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ENV_FILE, override=False)


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


class Speaker:
    """Non-blocking text-to-speech player."""

    def __init__(self, language: Optional[str] = None,
                 enabled: bool = True) -> None:
        _load_env()
        self.language = (language
                         or os.environ.get("TTS_LANG", DEFAULT_LANGUAGE)
                         or DEFAULT_LANGUAGE).strip()
        self.enabled = enabled and _env_flag("TTS_ENABLED", True)

        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._condition = threading.Condition()
        self._pending = 0
        self._closed = False
        self._thread: Optional[threading.Thread] = None

        if self.enabled:
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self._thread = threading.Thread(
                target=self._worker, name="tts-speaker", daemon=True)
            self._thread.start()

    # -- public API ------------------------------------------------------

    def speak(self, text: Optional[str]) -> None:
        """Queue a phrase. Returns immediately; never blocks driving."""
        text = " ".join((text or "").split())
        if not text or not self.enabled or self._closed:
            return
        with self._condition:
            self._pending += 1
        self._queue.put(text)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until every queued phrase has finished playing."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending > 0:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._condition.wait(remaining)
        return True

    def close(self, timeout: float = 3.0) -> None:
        """Stop the worker after the current phrase finishes."""
        if not self.enabled or self._closed:
            return
        self._closed = True
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- worker ----------------------------------------------------------

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                break
            try:
                self._play(text)
            except Exception as exc:  # offline, no audio device, bad text...
                log.warning("speech failed: %s", exc)
            finally:
                with self._condition:
                    self._pending = max(0, self._pending - 1)
                    self._condition.notify_all()

    def _cache_path(self, text: str) -> Path:
        digest = hashlib.sha1(
            (self.language + "\x00" + text).encode("utf-8")).hexdigest()[:20]
        return CACHE_DIR / f"{digest}.mp3"

    def _play(self, text: str) -> None:
        path = self._cache_path(text)
        if not path.exists():
            from gtts import gTTS
            gTTS(text=text, lang=self.language).save(str(path))
            self._prune_cache()

        import miniaudio
        info = miniaudio.get_file_info(str(path))
        duration = (info.num_frames or 0) / float(info.sample_rate or 1)
        device = miniaudio.PlaybackDevice()
        try:
            device.start(miniaudio.stream_file(str(path)))
            time.sleep(duration + 0.12)
        finally:
            device.close()

    @staticmethod
    def _prune_cache() -> None:
        try:
            files = sorted(CACHE_DIR.glob("*.mp3"),
                           key=lambda p: p.stat().st_mtime)
            for old in files[:-MAX_CACHE_FILES]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass


if __name__ == "__main__":  # quick manual test: python TTS.py "hello"
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    phrase = " ".join(sys.argv[1:]) or "Hello, I am your smart car."
    speaker = Speaker()
    speaker.speak(phrase)
    speaker.wait()
    speaker.close()
    print("spoken:", phrase)
