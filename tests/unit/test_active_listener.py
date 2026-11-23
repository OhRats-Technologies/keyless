"""Always-on listener tests that do not need cameras or the gaze engine."""

from __future__ import annotations

import threading
import time

import numpy as np

from keyless.audio.active_listener import BLOCK_SIZE, SAMPLE_RATE, ActiveVoiceListener
from keyless.voice.stt.base import SttResult


def _quiet_frame(amp: float = 0.002) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.normal(0.0, amp, size=BLOCK_SIZE).astype(np.float32)


def _tone_frame(freq_hz: float, amp: float = 0.12) -> np.ndarray:
    t = np.arange(BLOCK_SIZE, dtype=np.float32) / SAMPLE_RATE
    return (amp * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)


def test_active_voice_listener_publishes_provisional_captions() -> None:
    calls: list[int] = []

    class _StubStt:
        def transcribe(self, audio: np.ndarray) -> str:
            calls.append(len(audio))
            return "click send"

        def transcribe_detailed(self, audio: np.ndarray) -> SttResult:
            return SttResult(text="click send", no_speech_prob=0.02, avg_logprob=-0.1)

    partials: list[str] = []
    listener = ActiveVoiceListener(
        is_user_attending=lambda: True,
        enable_pop_click=False,
        require_command_prefix=True,
        stt_engine=_StubStt(),
        on_partial=partials.append,
    )
    worker = threading.Thread(target=listener._process_loop, daemon=True)
    worker.start()

    try:
        t = 1.0
        for _ in range(3):
            listener._audio_q.put((t, _quiet_frame()))
            t += 0.032
        for _ in range(40):
            listener._audio_q.put((t, _tone_frame(950.0, amp=0.14)))
            t += 0.032

        deadline = time.monotonic() + 2.0
        while not partials and time.monotonic() < deadline:
            time.sleep(0.02)

        assert calls
        assert partials == ["click send"]
    finally:
        listener._stop_event.set()
        worker.join(timeout=1.0)


def test_captions_gate_controls_meter_and_partial_transcription() -> None:
    gate_open = {"value": False}
    levels: list[float] = []
    partials: list[str] = []

    class _StubStt:
        def transcribe(self, audio: np.ndarray) -> str:
            return "live words"

        def transcribe_detailed(self, audio: np.ndarray) -> SttResult:
            return SttResult(text="live words", no_speech_prob=0.0, avg_logprob=-0.1)

    listener = ActiveVoiceListener(
        is_user_attending=lambda: True,
        enable_pop_click=False,
        require_command_prefix=True,
        stt_engine=_StubStt(),
        on_partial=partials.append,
        on_level=levels.append,
        captions_enabled=lambda: gate_open["value"],
    )
    worker = threading.Thread(target=listener._process_loop, daemon=True)
    worker.start()

    def _feed(frames: list[tuple[float, np.ndarray]]) -> None:
        for item in frames:
            listener._audio_q.put(item)

    try:
        t = 1.0
        gated_off = [(t + i * 0.032, _quiet_frame()) for i in range(3)]
        t += 0.1
        gated_off += [(t + i * 0.032, _tone_frame(950.0, amp=0.14)) for i in range(40)]
        _feed(gated_off)
        time.sleep(0.5)
        assert levels == []
        assert partials == []

        gate_open["value"] = True
        t += 2.0
        _feed([(t + i * 0.032, _tone_frame(950.0, amp=0.14)) for i in range(40)])
        deadline = time.monotonic() + 2.0
        while not partials and time.monotonic() < deadline:
            time.sleep(0.02)

        assert levels, "meter should publish while captions are on"
        assert all(level > 0.0 for level in levels)
        assert partials == ["live words"]
    finally:
        listener._stop_event.set()
        worker.join(timeout=1.0)
