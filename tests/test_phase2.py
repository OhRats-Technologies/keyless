"""Unit tests for Phase 2 audio pop-detector, STT helpers, and voice command router."""

from __future__ import annotations

import numpy as np
import pytest

from keyless.audio.pop_detector import PopTriggerStateMachine
from keyless.input.pointer import DryRunPointerBackend
from keyless.ipc.state import FLAG_FACE_DETECTED, SharedGazeReader, SharedGazeWriter
from keyless.router.dispatcher import VoiceCommandRouter
from keyless.routing.llm import LLMAgent, _strip_code_fences


def _quiet_chunk(n: int = 256, amp: float = 0.002) -> np.ndarray:
    rng = np.random.default_rng(1)
    return rng.normal(0.0, amp, size=n).astype(np.float32)


def test_pop_detector_triggers_on_isolated_lip_pop() -> None:
    pop = PopTriggerStateMachine(sample_rate=16000, chunk_samples=256)
    for _ in range(10):
        assert not pop.process_chunk(_quiet_chunk()).triggered

    # Sharp 1.5ms transient impulse followed by silence -> triggers after 2 trailing chunks (~32-48ms)
    impulse = _quiet_chunk()
    impulse[100:124] = np.array([0.45 * ((-1) ** i) for i in range(24)], dtype=np.float32)
    assert not pop.process_chunk(impulse).triggered
    assert not pop.process_chunk(_quiet_chunk()).triggered
    res = pop.process_chunk(_quiet_chunk())
    assert res.triggered
    assert pop.total_triggers == 1


def test_pop_detector_rejects_spoken_plosive_followed_by_vowel() -> None:
    pop = PopTriggerStateMachine(sample_rate=16000, chunk_samples=256)
    for _ in range(10):
        pop.process_chunk(_quiet_chunk())

    # Spoken "pa": initial burst followed by sustained vowel energy in next 32ms
    burst = _quiet_chunk()
    burst[180:204] = np.array([0.40 * ((-1) ** i) for i in range(24)], dtype=np.float32)
    t = np.arange(256, dtype=np.float32)
    vowel = (0.18 * np.sin(2.0 * np.pi * 1400.0 * t / 16000.0)).astype(np.float32)

    assert not pop.process_chunk(burst).triggered
    assert not pop.process_chunk(vowel).triggered
    res = pop.process_chunk(vowel)
    assert not res.triggered
    assert res.rejected_plosive
    assert pop.total_triggers == 0
    assert pop.total_rejected_plosives == 1


def test_pop_detector_rejects_electrical_tick_and_mid_speech_spike() -> None:
    pop = PopTriggerStateMachine(sample_rate=16000, chunk_samples=256)
    for _ in range(10):
        pop.process_chunk(_quiet_chunk())

    # 1-sample USB/mic tick has high peak (0.30) but low chunk RMS (<0.032) -> rejected
    tick = _quiet_chunk()
    tick[50] = 0.28
    for chunk in (tick, _quiet_chunk(), _quiet_chunk()):
        assert not pop.process_chunk(chunk).triggered
    assert pop.total_triggers == 0

    # Spike immediately preceded by loud speech/rustling (high pre_rms) -> rejected
    speech_chunk = _quiet_chunk(amp=0.05)
    impulse = _quiet_chunk()
    impulse[100:124] = np.array([0.45 * ((-1) ** i) for i in range(24)], dtype=np.float32)
    pop.process_chunk(speech_chunk)
    assert not pop.process_chunk(impulse).triggered
    assert not pop.process_chunk(_quiet_chunk()).triggered
    assert not pop.process_chunk(_quiet_chunk()).triggered
    assert pop.total_triggers == 0


def test_pop_detector_respects_ptt_mute_lock() -> None:
    pop = PopTriggerStateMachine()
    pop.set_muted(True)
    impulse = _quiet_chunk()
    impulse[50:80] = 0.5
    assert not pop.process_chunk(impulse).triggered
    assert not pop.process_chunk(_quiet_chunk()).triggered
    assert not pop.process_chunk(_quiet_chunk()).triggered
    assert pop.total_triggers == 0


def test_voice_router_deterministic_type_terminal_and_browser() -> None:
    ptr = DryRunPointerBackend(3440, 1440)
    shm_name = "kk_test_router_gaze"
    writer = SharedGazeWriter(shm_name=shm_name)
    try:
        writer.write(2450.0, 810.0, FLAG_FACE_DETECTED)
        reader = SharedGazeReader(shm_name=shm_name)
        try:
            class _StubLLM(LLMAgent):
                def generate_shell_command(self, instruction: str, cwd=None, files_preview=None) -> str:
                    return f"ls -lh # {instruction}"

            browser_events = []
            router = VoiceCommandRouter(
                pointer=ptr,
                llm_agent=_StubLLM(backend="ollama"),
                gaze_reader=reader,
                browser_callback=browser_events.append,
            )

            r1 = router.dispatch("Scroll down.")
            assert r1.route == "deterministic"
            assert ptr.scrolls == [(0, -4)]

            r2 = router.dispatch("Copy!")
            assert r2.route == "deterministic"
            assert ptr.combos[-1] == ("LEFTCTRL", "C")

            r3 = router.dispatch("Type: Hello, world!")
            assert r3.route == "type"
            assert ptr.typed_texts[-1] == "Hello, world!"

            r4 = router.dispatch("Terminal show hidden files")
            assert r4.route == "terminal"
            assert ptr.typed_texts[-1] == "ls -lh # show hidden files"

            r5 = router.dispatch("Browser summarize this table")
            assert r5.route == "browser"
            assert len(browser_events) == 1
            assert browser_events[0]["gaze"] == {"x": 2450.0, "y": 810.0}

            # In-process gaze_provider takes precedence when running unified daemon
            inproc_router = VoiceCommandRouter(
                pointer=ptr,
                gaze_reader=reader,
                gaze_provider=lambda: (3100.0, 420.0),
                browser_callback=browser_events.append,
            )
            inproc_router.dispatch("What's this?")
            assert browser_events[-1]["gaze"] == {"x": 3100.0, "y": 420.0}
        finally:
            reader.close()
    finally:
        writer.close(unlink=True)


def test_llm_helpers_and_hotkey_resolution(monkeypatch) -> None:
    evdev = pytest.importorskip("evdev")
    from keyless.platform.linux.hotkeys import resolve_hotkey_code

    assert _strip_code_fences("```bash\ntar -xzvf a.tar.gz\n```") == "tar -xzvf a.tar.gz"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert LLMAgent(backend="auto").backend == "ollama"
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert LLMAgent(backend="auto").backend == "gemini"
    assert resolve_hotkey_code("RIGHTCTRL") == evdev.ecodes.KEY_RIGHTCTRL
    assert resolve_hotkey_code("BTN_SIDE") == evdev.ecodes.BTN_SIDE
