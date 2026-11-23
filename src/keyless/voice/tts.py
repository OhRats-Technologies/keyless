"""On-device English text-to-speech via ConwayResearch/Underdog-Woof (24 kHz CPU numpy)."""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from keyless.config.paths import AppPaths, current_paths
from keyless.models.catalog import UNDERDOG_WOOF
from keyless.models.store import ModelState, ModelStore

logger = logging.getLogger(__name__)

TTS_SAMPLE_RATE = 24000
TTS_VOICES: tuple[str, ...] = ("juno", "mira")


def normalize_voice(voice: str | None) -> str:
    val = (voice or "juno").strip().lower()
    return val if val in TTS_VOICES else "juno"


def clean_text_for_speech(text: str, *, max_chars: int = 750) -> str:
    """Strip markdown, URLs, and code fences so LLM replies sound natural aloud."""
    cleaned = text or ""
    cleaned = re.sub(r"```[\s\S]*?```", " code snippet omitted. ", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"[*_>#~]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        cutoff = cleaned.rfind(".", 0, max_chars)
        if cutoff >= max_chars // 2:
            cleaned = cleaned[: cutoff + 1]
        else:
            cleaned = cleaned[:max_chars].rstrip(" ,;:-") + "…"
    return cleaned


def tts_report(paths: AppPaths | None = None, *, verify_hashes: bool = False) -> dict[str, Any]:
    """Return on-disk availability and attribution metadata for Underdog Woof."""
    resolved = paths or current_paths()
    store = ModelStore(resolved)
    status = store.status(UNDERDOG_WOOF, verify_hashes=verify_hashes)
    return {
        "asset": UNDERDOG_WOOF.id,
        "display_name": UNDERDOG_WOOF.display_name,
        "state": status.state.value,
        "ready": status.state is ModelState.READY,
        "path": str(status.path),
        "detail": status.detail,
        "total_mb": round(UNDERDOG_WOOF.total_bytes / (1024 * 1024), 1),
        "voices": list(TTS_VOICES),
        "license": UNDERDOG_WOOF.license,
        "attribution": UNDERDOG_WOOF.attribution,
    }


def load_woof_engine(paths: AppPaths | None = None, *, voice: str = "juno") -> Any:
    """Import ``underdog_woof.Woof`` from the app-managed model store directory."""
    resolved = paths or current_paths()
    store = ModelStore(resolved)
    status = store.status(UNDERDOG_WOOF, verify_hashes=False)
    if not status.ready:
        raise RuntimeError(
            f"Underdog Woof TTS is not downloaded ({status.detail}). "
            "Run `keyless models download underdog-woof`."
        )
    model_dir = store.asset_dir(UNDERDOG_WOOF)
    weights_path = model_dir / "woof_int8.npz"
    if not weights_path.exists():
        raise RuntimeError(f"Missing Underdog Woof weights at {weights_path}")
    dir_str = str(model_dir)
    if dir_str not in sys.path:
        sys.path.insert(0, dir_str)
    from underdog_woof import Woof  # type: ignore[import-not-found]

    return Woof(weights=str(weights_path), voice=normalize_voice(voice))


_PA_LOCK = threading.Lock()


def _default_play_audio(samples: np.ndarray, sample_rate: int, stop_event: threading.Event) -> None:
    import sounddevice as sd

    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0 or stop_event.is_set():
        return
    with _PA_LOCK:
        sd.play(arr, samplerate=sample_rate, blocking=False)
    try:
        duration_s = float(arr.size) / float(max(1, sample_rate))
        deadline = time.monotonic() + duration_s + 0.08
        while time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(0.03)
    finally:
        with _PA_LOCK:
            try:
                sd.stop()
            except Exception:
                pass


@dataclass(frozen=True)
class _SpeechJob:
    text: str
    voice: str


class WoofSpeaker:
    """Serialized non-blocking TTS speaker backed by ConwayResearch/Underdog-Woof."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        voice: str = "juno",
        enabled: bool = True,
        on_speaking_changed: Callable[[bool], None] | None = None,
        synth_fn: Callable[[str, str], np.ndarray] | None = None,
        play_fn: Callable[[np.ndarray, int, threading.Event], None] | None = None,
    ) -> None:
        self.paths = paths or current_paths()
        self.voice = normalize_voice(voice)
        self.enabled = bool(enabled)
        self._on_speaking_changed = on_speaking_changed
        self._synth_fn = synth_fn
        self._play_fn = play_fn or _default_play_audio

        self._engines: dict[str, Any] = {}
        self._engine_lock = threading.Lock()
        self._queue: queue.Queue[_SpeechJob | None] = queue.Queue()
        self._interrupt = threading.Event()
        self._closed = False
        self._speaking = False
        self._worker: threading.Thread | None = None

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    def available(self) -> bool:
        if self._synth_fn is not None:
            return True
        return bool(tts_report(self.paths)["ready"])

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="keyless-woof-tts", daemon=True)
        self._worker.start()

    def speak(
        self,
        text: str,
        *,
        voice: str | None = None,
        interrupt: bool = True,
        clean: bool = True,
    ) -> bool:
        """Queue ``text`` for asynchronous spoken playback. Returns False when disabled/empty."""
        if not self.enabled or self._closed:
            return False
        spoken_text = clean_text_for_speech(text) if clean else (text or "").strip()
        if not spoken_text:
            return False
        chosen_voice = normalize_voice(voice or self.voice)
        if interrupt:
            self._clear_pending()
            self._interrupt.set()
        self._ensure_worker()
        self._queue.put(_SpeechJob(text=spoken_text, voice=chosen_voice))
        return True

    def stop(self) -> None:
        """Stop active playback immediately and drop queued speech."""
        self._clear_pending()
        self._interrupt.set()
        if self._worker is None or not self._worker.is_alive():
            with _PA_LOCK:
                try:
                    import sounddevice as sd

                    sd.stop()
                except Exception:
                    pass

    def close(self) -> None:
        self._closed = True
        self.stop()
        self._queue.put(None)
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=1.5)
        self._worker = None

    def _clear_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _set_speaking(self, state: bool) -> None:
        if self._speaking == state:
            return
        self._speaking = state
        if self._on_speaking_changed is not None:
            try:
                self._on_speaking_changed(state)
            except Exception:
                logger.debug("TTS speaking callback failed", exc_info=True)

    def _synthesize(self, text: str, voice: str) -> np.ndarray:
        if self._synth_fn is not None:
            return np.asarray(self._synth_fn(text, voice), dtype=np.float32)
        with self._engine_lock:
            engine = self._engines.get(voice)
            if engine is None:
                engine = load_woof_engine(self.paths, voice=voice)
                self._engines[voice] = engine
        return np.asarray(engine(text), dtype=np.float32)

    def _run(self) -> None:
        while not self._closed:
            job = self._queue.get()
            if job is None:
                return
            self._interrupt.clear()
            self._set_speaking(True)
            try:
                wav = self._synthesize(job.text, job.voice)
                if not self._interrupt.is_set() and wav.size > 0:
                    self._play_fn(wav, TTS_SAMPLE_RATE, self._interrupt)
            except Exception as exc:
                logger.warning("Underdog Woof TTS failed: %s", exc)
            finally:
                if self._queue.empty():
                    # Allow speaker room echo to settle before reopening active STT.
                    if not self._closed and not self._interrupt.is_set():
                        time.sleep(0.18)
                    self._set_speaking(False)
