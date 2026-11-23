"""Always-on VAD command listener + 75ms speech-vetoed acoustic pop detector."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from keyless.voice.stt.base import SttEngine

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_SIZE = 512  # 32ms per audio frame at 16kHz

# Common STT hallucinations on silence, sighs, or mic bumps
_WHISPER_HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "subtitles by",
    "you",
    "okay",
    "oh",
    "uh",
    "um",
    "hmm",
    "bye",
    "mbc",
}

# Valid command prefixes for hands-free execution without a wake word
_VALID_COMMAND_PREFIXES = (
    "click",
    "left click",
    "double click",
    "right click",
    "middle click",
    "press",
    "select",
    "tap",
    "hover",
    "move to",
    "type",
    "dictate",
    "enter",
    "write",
    "scroll",
    "auto scroll",
    "start scrolling",
    "stop scrolling",
    "stop scroll",
    "freeze scroll",
    "keep scrolling",
    "faster scroll",
    "slower scroll",
    "go to top",
    "go to bottom",
    "top of page",
    "bottom of page",
    "save icon",
    "remember icon",
    "recenter gaze",
    "recenter",
    "calibrate center",
    "sync window",
    "align window",
    "page up",
    "page down",
    "next page",
    "previous page",
    "back",
    "go back",
    "forward",
    "go forward",
    "pause",
    "play",
    "resume",
    "mute",
    "unmute",
    "fullscreen",
    "exit fullscreen",
    "copy",
    "paste",
    "cut",
    "undo",
    "redo",
    "select all",
    "delete line",
    "escape",
    "tab",
    "space",
    "backspace",
    "close",
    "new tab",
    "create new tab",
    "next tab",
    "previous tab",
    "prev tab",
    "last tab",
    "first tab",
    "second tab",
    "third tab",
    "fourth tab",
    "fifth tab",
    "switch",
    "alt tab",
    "alt grave",
    "focus",
    "open",
    "previous window",
    "prev window",
    "next window",
    "last window",
    "overview",
    "show windows",
    "next workspace",
    "previous workspace",
    "workspace",
    "go to tab",
    "open tab",
    "tab ",
    "terminal",
    "shell",
    "bash",
    "browser",
    "firefox",
    "web",
    "what's this",
    "what is this",
    "summarize this",
    "fill this",
    "ask",
    "assistant",
    "hey assistant",
    "hey keyless",
    "answer",
    "explain",
    "stop listening",
    "go to sleep",
    "wake up",
)


@dataclass(frozen=True)
class VoiceEvent:
    """Emitted by ActiveVoiceListener when a verified pop or voice command completes."""

    kind: str  # "pop_click" or "voice_command"
    transcript: str = ""
    confidence: float = 1.0
    speech_onset_ts: float = 0.0


class _SpeechVetoedPopDetector:
    """High-precision lip-pop / tongue-click detector with 75ms post-impulse speech veto.

    A candidate pop is ONLY emitted if:
    1. Preceded by >= 32ms of quiet background floor.
    2. Peak impulse duration is narrow (3..140 samples) with high crest factor.
    3. Bandpass energy (650Hz..3600Hz) dominates low-frequency (<360Hz) desk-thud energy.
    4. Followed by >= 75ms (2+ quiet frames) of silence (vetoes spoken plosives like 'click'/'press').
    """

    def __init__(
        self,
        enabled: bool = True,
        min_peak_amp: float = 0.14,
        snr_ratio: float = 9.5,
        cooldown_sec: float = 0.38,
    ) -> None:
        self.enabled = enabled
        self.min_peak_amp = float(min_peak_amp)
        self.snr_ratio = float(snr_ratio)
        self.cooldown_sec = float(cooldown_sec)

        self._noise_rms = 0.006
        self._quiet_frames = 0
        self._pending_pop_ts: float | None = None
        self._post_quiet_frames = 0
        self._last_fire_ts = 0.0

    def cancel_pending(self) -> None:
        """Immediately cancel any pending pop when continuous speech is detected."""
        self._pending_pop_ts = None
        self._post_quiet_frames = 0

    def process_frame(self, samples: np.ndarray, now: float, is_speech_active: bool) -> bool:
        if not self.enabled:
            return False

        rms = float(np.sqrt(np.mean(samples * samples) + 1e-12))
        peak = float(np.max(np.abs(samples)))

        # Wait inside the 75ms post-impulse veto window
        if self._pending_pop_ts is not None:
            if is_speech_active or rms > max(0.022, self._noise_rms * 2.6):
                self._pending_pop_ts = None
                self._post_quiet_frames = 0
                return False

            self._post_quiet_frames += 1
            if self._post_quiet_frames >= 2:
                self._pending_pop_ts = None
                self._post_quiet_frames = 0
                self._last_fire_ts = now
                return True
            return False

        # Update adaptive background noise floor during quiet frames
        if rms < self._noise_rms * 2.2 and peak < self.min_peak_amp * 0.6:
            self._noise_rms = 0.96 * self._noise_rms + 0.04 * max(0.002, rms)
            self._quiet_frames = min(50, self._quiet_frames + 1)
            return False

        # Require >= 1 quiet frame (32ms) before the impulse and cooldown elapsed
        if (
            self._quiet_frames < 1
            or (now - self._last_fire_ts) < self.cooldown_sec
            or peak < self.min_peak_amp
            or (peak / max(1e-4, self._noise_rms)) < self.snr_ratio
        ):
            self._quiet_frames = 0
            return False

        # Check 1: Narrow impulse width (3..140 samples above 35% peak = ~0.2..9ms)
        above_half = int(np.sum(np.abs(samples) > (0.35 * peak)))
        if above_half < 3 or above_half > 140:
            self._quiet_frames = 0
            return False

        # Check 2: Spectral ratio (reject low-frequency desk bumps & mug thuds < 360Hz)
        spec = np.abs(np.fft.rfft(samples))
        freqs = np.fft.rfftfreq(len(samples), d=1.0 / SAMPLE_RATE)
        low_energy = float(np.sum(spec[(freqs >= 30) & (freqs < 360)] ** 2))
        mid_energy = float(np.sum(spec[(freqs >= 650) & (freqs <= 3600)] ** 2))
        if mid_energy < low_energy * 1.35:
            self._quiet_frames = 0
            return False

        # Check 3: Final 10ms of this 32ms frame must already be decaying
        tail_rms = float(np.sqrt(np.mean(samples[-160:] ** 2) + 1e-12))
        if tail_rms > peak * 0.35:
            self._quiet_frames = 0
            return False

        # Enter 75ms speech-veto window
        self._pending_pop_ts = now
        self._post_quiet_frames = 0
        self._quiet_frames = 0
        return False


class ActiveVoiceListener:
    """Hands-free continuous VAD listener with a speech-onset gaze hook."""

    def __init__(
        self,
        on_speech_onset: Callable[[], None] | None = None,
        is_user_attending: Callable[[], bool] | None = None,
        enable_pop_click: bool = False,
        require_command_prefix: bool = True,
        audio_device: int | str | None = None,
        stt_engine: SttEngine | None = None,
        on_transcript: Callable[[str], None] | None = None,
    ) -> None:
        self.on_speech_onset = on_speech_onset
        self.is_user_attending = is_user_attending
        self.require_command_prefix = require_command_prefix
        self.on_transcript = on_transcript
        self.audio_device = audio_device
        self.sleeping = False
        self._muted = False
        if stt_engine is None:
            raise ValueError("ActiveVoiceListener requires an SttEngine.")
        self.stt_engine = stt_engine
        self.device = getattr(self.stt_engine, "device", "custom")
        self.compute_type = getattr(self.stt_engine, "compute_type", "custom")

        self.pop_detector = _SpeechVetoedPopDetector(enabled=enable_pop_click)

        self._audio_q: queue.Queue[tuple[float, np.ndarray]] = queue.Queue(maxsize=256)
        self.events: queue.Queue[VoiceEvent] = queue.Queue()
        self._stop_event = threading.Event()
        self._stream: Any = None
        self._worker_thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=1,
            dtype="float32",
            device=self.audio_device,
            latency="low",
            callback=self._sd_callback,
        )
        self._stream.start()
        self._worker_thread = threading.Thread(
            target=self._process_loop,
            name="active-voice-listener",
            daemon=True,
        )
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
            self._worker_thread = None
        self.stt_engine.close()

    def _sd_callback(
        self,
        indata: np.ndarray,
        _frames: int,
        _time_info: object,
        _status: Any,
    ) -> None:
        try:
            self._audio_q.put_nowait((time.monotonic(), indata[:, 0].copy()))
        except queue.Full:
            pass

    @staticmethod
    def _is_speech_frame(samples: np.ndarray, noise_floor: float) -> bool:
        """Fast spectral voice-band detector (250Hz..3400Hz human speech formants)."""
        rms = float(np.sqrt(np.mean(samples * samples) + 1e-12))
        if rms < max(0.015, noise_floor * 2.8):
            return False
        spec = np.abs(np.fft.rfft(samples))
        freqs = np.fft.rfftfreq(len(samples), d=1.0 / SAMPLE_RATE)
        voice_band = float(np.sum(spec[(freqs >= 250) & (freqs <= 3400)] ** 2))
        total_band = float(np.sum(spec**2) + 1e-9)
        return (voice_band / total_band) > 0.42

    def set_muted(self, muted: bool) -> None:
        """Temporarily suppress capture (e.g. while local TTS is speaking)."""
        self._muted = bool(muted)

    def _process_loop(self) -> None:
        # 14 frames = 448ms pre-roll ring buffer so initial consonant is never clipped
        pre_roll: deque[np.ndarray] = deque(maxlen=14)
        speech_buf: list[np.ndarray] = []

        in_speech = False
        speech_onset_ts = 0.0
        consecutive_speech = 0
        trailing_silence_frames = 0
        noise_floor = 0.005

        # Adaptive endpointing: 25 frames (800ms) for short (<1.1s) utterances, 15 frames (480ms) after
        max_speech_frames = int(8.0 * SAMPLE_RATE / BLOCK_SIZE)

        while not self._stop_event.is_set():
            try:
                now, frame = self._audio_q.get(timeout=0.05)
            except queue.Empty:
                continue

            if self._muted:
                pre_roll.clear()
                speech_buf.clear()
                in_speech = False
                consecutive_speech = 0
                trailing_silence_frames = 0
                continue

            rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
            speech_like = self._is_speech_frame(frame, noise_floor)

            if not in_speech and not speech_like:
                noise_floor = 0.98 * noise_floor + 0.02 * max(0.002, rms)

            # 1. Speech-vetoed pop detector when not mid-utterance
            if not in_speech and not self.sleeping:
                if self.pop_detector.process_frame(frame, now, is_speech_active=speech_like):
                    if self.is_user_attending is None or self.is_user_attending():
                        self.events.put(VoiceEvent(kind="pop_click", speech_onset_ts=now))
                    continue

            # 2. Hands-free continuous command capture state machine
            if not in_speech:
                pre_roll.append(frame)
                if speech_like:
                    consecutive_speech += 1
                    # 2 consecutive speech frames (64ms) confirms speech onset
                    if consecutive_speech >= 2:
                        if self.is_user_attending is not None and not self.is_user_attending():
                            consecutive_speech = 0
                            continue

                        in_speech = True
                        speech_onset_ts = now
                        trailing_silence_frames = 0
                        self.pop_detector.cancel_pending()
                        speech_buf = list(pre_roll)

                        # Freeze gaze & start background 680x480 OCR capture while user speaks
                        if not self.sleeping and self.on_speech_onset is not None:
                            try:
                                self.on_speech_onset()
                            except Exception as exc:
                                logger.debug("on_speech_onset callback error: %s", exc)
                else:
                    consecutive_speech = 0
            else:
                speech_buf.append(frame)
                if speech_like:
                    trailing_silence_frames = 0
                else:
                    trailing_silence_frames += 1

                # Adaptive endpointing:
                # If utterance so far is short (< 1.1s, e.g. user said "click" and paused to think),
                # wait 25 frames (800ms) so we never chop "click ... gemini ask question box" in half.
                # Once utterance is >= 1.1s (35 frames), finish snappily after 15 frames (480ms).
                spoken_frames = len(speech_buf) - trailing_silence_frames
                required_silence = 25 if spoken_frames < 35 else 15

                if (
                    trailing_silence_frames >= required_silence
                    or len(speech_buf) >= max_speech_frames
                ):
                    audio_np = np.concatenate(speech_buf, axis=0)
                    in_speech = False
                    consecutive_speech = 0
                    trailing_silence_frames = 0
                    speech_buf.clear()
                    pre_roll.clear()

                    if len(audio_np) >= int(0.20 * SAMPLE_RATE):
                        self._transcribe_and_filter(audio_np, speech_onset_ts)

    def _transcribe_and_filter(self, audio_np: np.ndarray, onset_ts: float) -> None:
        """Transcribe utterance and apply anti-hallucination & grammar gates."""
        result = self.stt_engine.transcribe_detailed(audio_np)
        text = result.text
        if not text:
            return

        # Gate 1: Reject high no_speech_prob or low logprob (coughs, mic rustle, mumbles)
        if result.no_speech_prob > 0.48 or result.avg_logprob < -0.85:
            return

        clean = text.lower().strip(" .,!?\"'")
        clean = clean.replace("-", " ")
        clean = " ".join(clean.split())
        clean = clean.replace("re center", "recenter")
        if not clean or clean in _WHISPER_HALLUCINATIONS or len(clean) < 3:
            return
        if self.on_transcript is not None:
            try:
                self.on_transcript(text.strip())
            except Exception:
                logger.debug("Transcript callback failed", exc_info=True)

        # Handle sleep / wake voice toggle
        if clean in ("go to sleep", "stop listening"):
            self.sleeping = True
            logger.info("Voice listener sleeping (say 'wake up' to resume).")
            return
        if clean == "wake up":
            self.sleeping = False
            logger.info("Voice listener awake and listening.")
            return
        if self.sleeping:
            return

        # Strip common spoken hesitation fillers before checking command intent
        stripped = clean
        for _ in range(3):
            prev = stripped
            for filler in ("uh ", "um ", "mm ", "hmm ", "hey ", "please ", "can you ", "could you "):
                if stripped.startswith(filler):
                    stripped = stripped[len(filler) :].strip()
            if stripped == prev:
                break

        # Gate 2: Allow known command prefixes, "<app> window/app" suffixes, OR natural phrases
        # containing desktop action keywords so the LLM intent router can handle them.
        is_window_suffix = (
            stripped.endswith((" window", " app", " tab")) and 2 <= len(stripped.split()) <= 6
        )
        _action_keywords = (
            "click",
            "press",
            "tap",
            "scroll",
            "recenter",
            "window",
            "tab",
            "minimize",
            "maximize",
            "play",
            "pause",
            "mute",
            "fullscreen",
            "focus",
            "switch",
            "open",
            "close",
            "type",
            "terminal",
            "ask",
            "explain",
        )
        has_action_keyword = any(kw in stripped.split() for kw in _action_keywords)
        if (
            self.require_command_prefix
            and not stripped.startswith(_VALID_COMMAND_PREFIXES)
            and not is_window_suffix
            and not has_action_keyword
        ):
            logger.debug("Voice ignored (not a command prefix): %r", text)
            return

        conf = result.confidence
        self.events.put(
            VoiceEvent(
                kind="voice_command",
                transcript=text,
                confidence=conf,
                speech_onset_ts=onset_ts,
            )
        )
