"""Unit and integration tests for Phase 3 Gaze-Foveated Voice + Vision Grounding."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest
from keyless.input.keys import BUTTON_LEFT, BUTTON_MIDDLE, BUTTON_RIGHT

from keyless.audio.active_listener import (
    BLOCK_SIZE,
    SAMPLE_RATE,
    ActiveVoiceListener,
    _SpeechVetoedPopDetector,
)
from keyless.config import DisplayLayout, MonitorInfo
from keyless.input.pointer import DryRunPointerBackend
from keyless.ipc.firefox_bridge import FirefoxDOMBridge
from keyless.router.dispatcher import VoiceCommandRouter
from keyless.router.gaze_vlm_clicker import (
    GazeVLMClicker,
    GroundingSelection,
    parse_voice_click_command,
)
from keyless.vision.calibrator import (
    _load_replay_dataset,
    append_harvested_calibration_samples,
)
from keyless.vision.cursor_gimbal import GazeGimbalController
from keyless.vision.landmarks import STEREO_FEATURE_DIM
from keyless.vision.ui_detector import GazeUIDetector, WaylandScreenCapturer
from keyless.voice.stt.base import SttResult


def _make_desktop_image() -> np.ndarray:
    """Create a 1920x1080 synthetic desktop with labeled buttons and an unlabeled icon."""
    img = np.full((1080, 1920, 3), 245, dtype=np.uint8)
    # Near center (960, 540): "Subscribe" button centered around (980, 520)
    cv2.rectangle(img, (890, 490), (1070, 550), (30, 30, 210), -1)
    cv2.putText(
        img,
        "Subscribe",
        (908, 530),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    # Near (1100, 520): unlabeled square icon contour (36x36)
    cv2.rectangle(img, (1085, 502), (1121, 538), (40, 40, 40), 2)
    cv2.circle(img, (1103, 520), 8, (40, 40, 40), 2)

    # Near (980, 620): "Search" input label
    cv2.rectangle(img, (880, 595), (1160, 645), (210, 210, 210), -1)
    cv2.rectangle(img, (880, 595), (1160, 645), (80, 80, 80), 2)
    cv2.putText(
        img,
        "Search",
        (892, 628),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (20, 20, 20),
        2,
        cv2.LINE_AA,
    )

    # Far outside 680x480 gaze crop (top-left): "Settings" button centered around (140, 80)
    cv2.rectangle(img, (60, 50), (220, 110), (50, 90, 180), -1)
    cv2.putText(
        img,
        "Settings",
        (80, 90),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return img


def test_parse_voice_click_command() -> None:
    i1 = parse_voice_click_command("Click on the subscribe button.")
    assert i1 is not None
    assert i1.action == "click"
    assert i1.target_query == "subscribe button"

    i2 = parse_voice_click_command("double click main.py")
    assert i2 is not None
    assert i2.action == "double_click"
    assert i2.target_query == "main.py"

    i3 = parse_voice_click_command("right click the tab")
    assert i3 is not None
    assert i3.action == "right_click"
    assert i3.target_query == "tab"

    i4 = parse_voice_click_command("hover over settings")
    assert i4 is not None
    assert i4.action == "hover"
    assert i4.target_query == "settings"

    i5 = parse_voice_click_command("type hello world in the search box")
    assert i5 is not None
    assert i5.action == "type_into"
    assert i5.text_to_type == "hello world"
    assert i5.target_query == "search box"

    i6 = parse_voice_click_command("click gemini ask question box and type how do I use systemd")
    assert i6 is not None
    assert i6.action == "type_into"
    assert i6.target_query == "gemini ask question box"
    assert i6.text_to_type == "how do I use systemd"

    assert parse_voice_click_command("scroll down") is None
    assert parse_voice_click_command("click") is None


def test_detector_and_three_tier_cascade_with_prewarm_and_harvest() -> None:
    desktop = _make_desktop_image()

    def _fake_capture(x: int, y: int, w: int, h: int) -> np.ndarray:
        return desktop[y : y + h, x : x + w].copy()

    detector = GazeUIDetector(capture_fn=_fake_capture, enable_ocr=True)
    layout = DisplayLayout(
        monitors=(MonitorInfo("DP-1", 0, 0, 1920, 1080, 1920, 1080, 1.0, True),),
        total_width=1920,
        total_height=1080,
    )

    ollama_calls: list[dict[str, Any]] = []

    class _FakeOllama:
        def chat(self, **kwargs: Any) -> Any:
            ollama_calls.append(kwargs)
            user_txt = kwargs["messages"][-1]["content"]
            # Pick the first <icon> box in the box directory when asked for gear icon
            target_id = 0
            if "gear icon" in user_txt:
                for line in user_txt.splitlines():
                    if "<icon>" in line and line.startswith("["):
                        target_id = int(line.split("]")[0][1:])
                        break
            return SimpleNamespace(
                message=SimpleNamespace(
                    content=GroundingSelection(target_id=target_id).model_dump_json()
                )
            )

    clicker = GazeVLMClicker(
        detector=detector,
        layout=layout,
        ollama_client=_FakeOllama(),
    )
    try:
        feat = np.linspace(-0.2, 0.2, STEREO_FEATURE_DIM, dtype=np.float64)
        # Hook 1: Pre-warm at gaze=(960, 530)
        clicker.on_speech_start_prewarm(960.0, 530.0, stereo_feat=feat)

        # Tier 1: Keyword fast-path ("Subscribe") -> NO Ollama call!
        res1 = clicker.resolve_voice_target("click on subscribe button", 960.0, 530.0, feat)
        assert res1 is not None
        intent1, (tx1, ty1), tier1 = res1
        assert intent1.action == "click"
        assert "Tier-1 Keyword" in tier1
        assert 890 <= tx1 <= 1070 and 490 <= ty1 <= 550
        assert len(ollama_calls) == 0
        assert len(clicker.harvested_calib_samples) == 1

        # Tier 2: Unlabeled icon -> calls gemma4:e4b SoM with think=False and PNG bytes
        res2 = clicker.resolve_voice_target("click the gear icon", 1090.0, 520.0, feat)
        assert res2 is not None
        intent2, (tx2, ty2), tier2 = res2
        assert intent2.action == "click"
        assert "Tier-2" in tier2
        assert 1080 <= tx2 <= 1126 and 498 <= ty2 <= 542
        assert len(ollama_calls) == 1
        assert ollama_calls[0]["think"] is False
        assert len(ollama_calls[0]["messages"][-1]["images"][0]) > 100

        # Tier 3: Target "Settings" is far outside gaze crop around (1600, 900)
        res3 = clicker.resolve_voice_target("click settings", 1600.0, 900.0, feat)
        assert res3 is not None
        _, (tx3, ty3), tier3 = res3
        assert "Tier-3" in tier3
        assert 60 <= tx3 <= 220 and 50 <= ty3 <= 110
    finally:
        clicker.shutdown()


def test_router_and_gimbal_warp_hold_and_harvest_persistence(tmp_path) -> None:
    desktop = _make_desktop_image()
    detector = GazeUIDetector(
        capture_fn=lambda x, y, w, h: desktop[y : y + h, x : x + w].copy(),
        enable_ocr=True,
    )
    clicker = GazeVLMClicker(detector=detector, screen_w=1920, screen_h=1080)
    ptr = DryRunPointerBackend(1920, 1080)
    ctrl = GazeGimbalController(1920, 1080)
    feat = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)

    try:
        router = VoiceCommandRouter(
            pointer=ptr,
            gaze_vlm_clicker=clicker,
            gaze_state_provider=lambda: (950.0, 530.0, feat),
            on_before_click=lambda tx, ty: ctrl.warp_and_hold(tx, ty, hold_sec=0.2),
        )

        # Bare "click" and "click here" click at the live gaze point
        r_bare = router.dispatch("click")
        assert r_bare.route == "deterministic"
        assert ptr.last_abs == (950, 530)
        assert ptr.clicks == [BUTTON_LEFT]

        r_det = router.dispatch("click here")
        assert r_det.route == "deterministic"
        assert ptr.last_abs == (950, 530)
        assert ptr.clicks == [BUTTON_LEFT, BUTTON_LEFT]

        # Explicit "Type: ..." stays literal typing
        r_type = router.dispatch("Type: Hello in world")
        assert r_type.route == "type"
        assert ptr.typed_texts[-1] == "Hello in world"

        # "click subscribe" warps cursor, holds gimbal, and clicks BTN_LEFT
        r_vlm = router.dispatch("click subscribe button")
        assert r_vlm.route == "gaze_vlm_click"
        assert ptr.last_abs is not None
        assert 890 <= ptr.last_abs[0] <= 1070
        assert ptr.clicks[-1] == BUTTON_LEFT
        assert 890 <= ctrl.cursor_x <= 1070

        # "type openai in search" clicks search box and types "openai"
        r_type_into = router.dispatch("type openai in search")
        assert r_type_into.route == "gaze_vlm_click"
        assert 880 <= ptr.last_abs[0] <= 1160
        assert ptr.typed_texts[-1] == "openai"

        # Harvested samples persist cleanly into calib_dataset.npz
        calib_path = tmp_path / "stereo_calib.json"
        harvested = clicker.pop_harvested_samples()
        assert len(harvested) >= 2
        # Pad >= 30 samples to test _load_replay_dataset threshold
        padded = harvested * 16
        added = append_harvested_calibration_samples(calib_path, padded, "cam1", "cam2")
        assert added == len(padded)
        reloaded = _load_replay_dataset(tmp_path / "calib_dataset.npz", "cam1", "cam2")
        assert reloaded is not None
        assert len(reloaded[0]) == len(padded)
    finally:
        clicker.shutdown()


def test_wayland_capturer_crop_from_full_scaling() -> None:
    full = np.zeros((2160, 3840, 3), dtype=np.uint8)
    full[200:400, 600:1000] = (0, 255, 0)
    crop = WaylandScreenCapturer._crop_from_full(
        full, x=300, y=100, w=200, h=100, desktop_w=1920, desktop_h=1080
    )
    assert crop.shape == (100, 200, 3)
    assert int(crop[50, 100, 1]) == 255


def _quiet_frame(amp: float = 0.002) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.normal(0.0, amp, size=BLOCK_SIZE).astype(np.float32)


def _tone_frame(freq_hz: float, amp: float = 0.12) -> np.ndarray:
    t = np.arange(BLOCK_SIZE, dtype=np.float32) / SAMPLE_RATE
    return (amp * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)


def _pop_impulse_frame(freq_hz: float = 1600.0, amp: float = 0.35) -> np.ndarray:
    frame = _quiet_frame()
    # 5ms damped mid-band impulse ending well before final 10ms tail
    n = 80
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    env = np.exp(-t * 450.0).astype(np.float32)
    frame[60 : 60 + n] += (amp * env * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)
    return frame


def test_speech_vetoed_pop_detector_filters_plosives_and_thuds() -> None:
    det = _SpeechVetoedPopDetector(enabled=True)
    t = 1.0

    # 1. Warm up noise floor with quiet frames
    for _ in range(5):
        assert not det.process_frame(_quiet_frame(), t, is_speech_active=False)
        t += 0.032

    # 2. Clean mid-band pop + 2 quiet frames (~75ms) -> triggers
    assert not det.process_frame(_pop_impulse_frame(1600.0), t, is_speech_active=False)
    t += 0.032
    assert not det.process_frame(_quiet_frame(), t, is_speech_active=False)
    t += 0.032
    assert det.process_frame(_quiet_frame(), t, is_speech_active=False)
    t += 0.50

    # 3. Spoken plosive ("c" in "click") followed by vowel speech -> vetoed!
    assert not det.process_frame(_pop_impulse_frame(1600.0), t, is_speech_active=False)
    t += 0.032
    assert not det.process_frame(_tone_frame(900.0, amp=0.14), t, is_speech_active=True)
    t += 0.032
    assert not det.process_frame(_quiet_frame(), t, is_speech_active=False)
    t += 0.50

    # 4. Low-frequency desk thud (120Hz) -> rejected!
    assert not det.process_frame(_pop_impulse_frame(120.0, amp=0.45), t, is_speech_active=False)
    t += 0.032
    assert not det.process_frame(_quiet_frame(), t, is_speech_active=False)
    t += 0.032
    assert not det.process_frame(_quiet_frame(), t, is_speech_active=False)


def test_active_voice_listener_attention_onset_gates_and_sleep() -> None:
    import threading

    scripted_results: list[tuple[str, float, float]] = [
        ("Click subscribe button.", 0.05, -0.15),
        ("Thank you.", 0.05, -0.15),  # Hallucination rejected
        ("How are you doing today?", 0.05, -0.15),  # Non-command rejected
        ("Click hidden", 0.72, -0.15),  # High no_speech_prob rejected
        ("Go to sleep.", 0.05, -0.15),  # Enters sleep
        ("Click ignored while sleeping.", 0.05, -0.15),  # Ignored while sleeping
        ("Wake up.", 0.05, -0.15),  # Resumes
        ("Type hello world.", 0.05, -0.15),  # Accepted after wake up
    ]

    class _StubStt:
        def transcribe_detailed(self, _audio: np.ndarray) -> SttResult:
            txt, no_sp, lp = scripted_results.pop(0)
            return SttResult(text=txt, no_speech_prob=no_sp, avg_logprob=lp)

    attending = [False]
    onsets: list[float] = []
    listener = ActiveVoiceListener(
        on_speech_onset=lambda: onsets.append(1.0),
        is_user_attending=lambda: attending[0],
        enable_pop_click=False,
        require_command_prefix=True,
        stt_engine=_StubStt(),
    )

    worker = threading.Thread(target=listener._process_loop, daemon=True)
    worker.start()

    try:
        t = 1.0

        def _push_utterance(pause_frames: int = 26) -> None:
            nonlocal t
            for _ in range(3):
                listener._audio_q.put((t, _quiet_frame()))
                t += 0.032
            for _ in range(6):
                listener._audio_q.put((t, _tone_frame(950.0, amp=0.14)))
                t += 0.032
            for _ in range(pause_frames):
                listener._audio_q.put((t, _quiet_frame()))
                t += 0.032

        # 1. Head turned away -> ignored completely (no onset, no transcribe consumed)
        attending[0] = False
        _push_utterance()
        import time

        time.sleep(0.08)
        assert len(onsets) == 0
        assert len(scripted_results) == 8

        # 2. User looking at monitor -> runs through all 8 scripted utterances
        attending[0] = True
        for _ in range(8):
            _push_utterance()

        deadline = time.monotonic() + 3.0
        while scripted_results and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)

        events = []
        while not listener.events.empty():
            events.append(listener.events.get_nowait())

        assert [e.transcript for e in events] == [
            "Click subscribe button.",
            "Type hello world.",
        ]
        assert not listener.sleeping
        assert len(onsets) == 6  # Onset fired for the 6 non-sleeping utterances
    finally:
        listener._stop_event.set()
        worker.join(timeout=1.0)


def test_active_voice_listener_adaptive_pause_prevents_chopping() -> None:
    import threading
    import time

    transcribed_lengths: list[int] = []

    class _StubStt:
        def transcribe_detailed(self, audio: np.ndarray) -> SttResult:
            transcribed_lengths.append(len(audio))
            return SttResult(
                text="Click gemini ask question box.",
                no_speech_prob=0.04,
                avg_logprob=-0.12,
            )

    listener = ActiveVoiceListener(
        is_user_attending=lambda: True,
        enable_pop_click=False,
        require_command_prefix=True,
        stt_engine=_StubStt(),
    )
    worker = threading.Thread(target=listener._process_loop, daemon=True)
    worker.start()

    try:
        t = 1.0
        for _ in range(3):
            listener._audio_q.put((t, _quiet_frame()))
            t += 0.032
        # Say "click" (4 frames) then pause 512ms (16 frames) -> MUST NOT split!
        for _ in range(4):
            listener._audio_q.put((t, _tone_frame(950.0, amp=0.14)))
            t += 0.032
        for _ in range(16):
            listener._audio_q.put((t, _quiet_frame()))
            t += 0.032
        # Continue "...gemini ask question box" (18 frames) then end with 800ms silence
        for _ in range(18):
            listener._audio_q.put((t, _tone_frame(950.0, amp=0.14)))
            t += 0.032
        for _ in range(26):
            listener._audio_q.put((t, _quiet_frame()))
            t += 0.032

        deadline = time.monotonic() + 2.0
        while not transcribed_lengths and time.monotonic() < deadline:
            time.sleep(0.02)

        assert len(transcribed_lengths) == 1
        assert listener.events.get_nowait().transcript == "Click gemini ask question box."
    finally:
        listener._stop_event.set()
        worker.join(timeout=1.0)


def test_dom_role_token_bag_and_llm_fallback_and_one_breath_type() -> None:
    from keyless.ipc.firefox_bridge import DOMCandidate

    cands = [
        DOMCandidate(
            dom_id=3,
            tag="button",
            role="button",
            primary_label="New chat",
            visible_text="New chat",
            aria_label="New chat",
            icon_hints="add",
            aliases=["new chat", "new conversation"],
            gaze_dist_px=40.0,
            screen_x=200,
            screen_y=120,
        ),
        DOMCandidate(
            dom_id=9,
            tag="div",
            role="textbox",
            primary_label="Enter a prompt here",
            visible_text="",
            aria_label="Enter a prompt here",
            icon_hints="",
            aliases=["prompt", "input", "textbox", "box", "field", "ask", "question", "gemini"],
            gaze_dist_px=90.0,
            screen_x=960,
            screen_y=920,
        ),
    ]

    class _FakeOllama:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, **_kwargs: Any) -> Any:
            self.calls += 1
            return SimpleNamespace(message=SimpleNamespace(content='{"dom_id": 9}'))

    fake_ollama = _FakeOllama()
    bridge = FirefoxDOMBridge(host="127.0.0.1", port=0, ollama_client=fake_ollama)

    # 1. Role-aware token-bag matches "gemini ask question box" immediately with 0 LLM calls
    hit = bridge.match_voice_target_in_dom("gemini ask question box", cands)
    assert hit is not None
    assert hit.dom_id == 9
    assert fake_ollama.calls == 0

    # 2. Unusual conversational description falls back to Tier-0.5 text-only LLM selection
    hit_llm = bridge.match_voice_target_in_dom("place where I talk to the assistant", cands)
    assert hit_llm is not None
    assert hit_llm.dom_id == 9
    assert fake_ollama.calls == 1


def test_firefox_dom_bridge_tier0_websocket_and_router() -> None:
    import asyncio
    import json
    import threading
    import time
    from pathlib import Path
    from websockets.asyncio.client import connect as ws_connect

    manifest = json.loads(Path("firefox_ext/manifest.json").read_text())
    assert manifest["manifest_version"] == 2
    assert manifest["background"]["persistent"] is True

    bridge = FirefoxDOMBridge(host="127.0.0.1", port=0)
    bridge.start()
    received_gaze: list[dict[str, Any]] = []
    executed_actions: list[dict[str, Any]] = []
    client_ready = threading.Event()
    client_stop = threading.Event()

    async def _mock_firefox_ext() -> None:
        uri = f"ws://127.0.0.1:{bridge.bound_port}"
        async with ws_connect(uri) as ws:
            client_ready.set()
            while not client_stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg["type"] == "gaze_update":
                    received_gaze.append(msg)
                elif msg["type"] == "get_dom_candidates":
                    await ws.send(
                        json.dumps(
                            {
                                "req_id": msg["req_id"],
                                "ok": True,
                                "data": {
                                    "candidates": [
                                        {
                                            "dom_id": 7,
                                            "tag": "button",
                                            "role": "button",
                                            "primary_label": "Send message",
                                            "visible_text": "",
                                            "aria_label": "Send message",
                                            "icon_hints": "send",
                                            "aliases": ["submit", "send", "send message", "arrow_upward"],
                                            "gaze_dist_px": 25.0,
                                            "screen_x": 2840,
                                            "screen_y": 1120,
                                        },
                                        {
                                            "dom_id": 8,
                                            "tag": "div",
                                            "role": "textbox",
                                            "primary_label": "Ask Gemini",
                                            "visible_text": "",
                                            "aria_label": "Ask Gemini",
                                            "icon_hints": "",
                                            "aliases": ["prompt", "input", "textbox", "ask gemini"],
                                            "gaze_dist_px": 60.0,
                                            "screen_x": 2500,
                                            "screen_y": 1120,
                                        },
                                    ]
                                },
                            }
                        )
                    )
                elif msg["type"] == "execute_dom_action":
                    executed_actions.append(msg)
                    sx, sy = (2840, 1120) if msg["dom_id"] == 7 else (2500, 1120)
                    await ws.send(
                        json.dumps(
                            {
                                "req_id": msg["req_id"],
                                "ok": True,
                                "data": {"ok": True, "screen_x": sx, "screen_y": sy},
                            }
                        )
                    )

    ext_thread = threading.Thread(target=lambda: asyncio.run(_mock_firefox_ext()), daemon=True)
    ext_thread.start()
    assert client_ready.wait(timeout=2.0)

    # Wait briefly for server _handler to register client
    deadline = time.monotonic() + 1.0
    while not bridge.is_connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge.is_connected

    capture_calls = 0

    def _fail_if_ocr_needed(_x: int, _y: int, _w: int, _h: int) -> np.ndarray:
        nonlocal capture_calls
        capture_calls += 1
        return np.zeros((480, 680, 3), dtype=np.uint8)

    detector = GazeUIDetector(capture_fn=_fail_if_ocr_needed, enable_ocr=False)
    clicker = GazeVLMClicker(
        detector=detector,
        screen_w=5360,
        screen_h=1440,
        firefox_bridge=bridge,
    )
    ptr = DryRunPointerBackend(5360, 1440)
    feat = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)

    try:
        # 1. Gaze HUD broadcast reaches Firefox
        bridge.broadcast_gaze(2830.0, 1110.0, mon_x=1920, mon_y=0)
        deadline = time.monotonic() + 1.0
        while not received_gaze and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(received_gaze) == 1
        assert received_gaze[0]["mon_x"] == 1920

        # 2. Tier-0 Voice Click ("click submit button" -> aria-label "Send message")
        router = VoiceCommandRouter(
            pointer=ptr,
            gaze_vlm_clicker=clicker,
            gaze_state_provider=lambda: (2830.0, 1110.0, feat),
        )
        clicker.on_speech_start_prewarm(2830.0, 1110.0, feat)
        r1 = router.dispatch("click submit button")
        assert r1.route == "gaze_vlm_click"
        assert "Tier-0 Firefox DOM" in r1.detail
        assert ptr.last_abs == (2840.0, 1120.0)
        assert ptr.clicks == []  # No duplicate uinput click since DOM executed it
        assert executed_actions[-1]["dom_id"] == 7
        assert executed_actions[-1]["action"] == "click"
        assert len(clicker.harvested_calib_samples) == 1

        # 3. Tier-0 Voice Type Into ("type hello world into prompt")
        r2 = router.dispatch("type hello world into prompt")
        assert r2.route == "gaze_vlm_click"
        assert "Tier-0 Firefox DOM" in r2.detail
        assert ptr.last_abs == (2500.0, 1120.0)
        assert ptr.typed_texts == []  # Executed directly in contenteditable DOM
        assert executed_actions[-1]["dom_id"] == 8
        assert executed_actions[-1]["action"] == "type_into"
        assert executed_actions[-1]["text"] == "hello world"
    finally:
        client_stop.set()
        ext_thread.join(timeout=1.5)
        clicker.shutdown()
        bridge.stop()


def test_internal_gaze_guide_mode_does_not_move_physical_mouse() -> None:
    ptr = DryRunPointerBackend(1920, 1080)
    ctrl = GazeGimbalController(1920, 1080, move_cursor=False)
    ctrl.update_vision((1450.0, 320.0), (0.52, 0.48), is_blinking=False, t_sec=0.1)
    step = ctrl.step_output(ptr, face_detected=True, is_blinking=False, t_sec=0.12)
    assert step.gaze_x > 1000.0
    assert step.gaze_y < 500.0
    assert ptr.last_abs is None
    assert ptr.rel_accum == (0.0, 0.0)


def test_pause_video_and_box_that_says_ask_gemini() -> None:
    from keyless.audio.active_listener import _VALID_COMMAND_PREFIXES
    from keyless.ipc.firefox_bridge import DOMCandidate
    from keyless.router.gaze_vlm_clicker import _clean_target_keywords
    from keyless.vision.ui_detector import GazeROISnapshot, UIBoundingBox

    # 1. "pause video" prefix allowed and routes cleanly via DOM or media key fallback
    assert "pause video".startswith(_VALID_COMMAND_PREFIXES)
    ptr = DryRunPointerBackend(1920, 1080)

    class _FakeBridge:
        is_connected = True

        def __init__(self) -> None:
            self.media_actions: list[str] = []

        def execute_media_action(self, action: str) -> bool:
            self.media_actions.append(action)
            return True

    fb = _FakeBridge()
    r_dom = VoiceCommandRouter(pointer=ptr, firefox_bridge=fb)  # type: ignore[arg-type]
    res_dom = r_dom.dispatch("pause video")
    assert res_dom.route == "media_dom"
    assert fb.media_actions == ["pause"]

    r_fallback = VoiceCommandRouter(pointer=ptr)
    res_fb = r_fallback.dispatch("pause video")
    assert res_fb.route == "deterministic"
    assert ptr.combos[-1] == ("PLAYPAUSE",)

    # 2. "click box that says ask gemini" strips filler words in BOTH Tier-1 OCR and Tier-0 DOM
    assert _clean_target_keywords("box that says ask gemini") == "ask gemini"
    snap = GazeROISnapshot(
        crop_x=0,
        crop_y=0,
        crop_w=680,
        crop_h=480,
        gaze_x=300.0,
        gaze_y=300.0,
        stereo_feat=None,
        raw_bgr=np.zeros((480, 680, 3), dtype=np.uint8),
        som_png_bytes=b"",
        boxes=[
            UIBoundingBox(
                mark_id=1,
                global_x1=200,
                global_y1=280,
                global_x2=420,
                global_y2=320,
                label="Ask Gemini",
                kind="text",
                gaze_dist_px=10.0,
            )
        ],
    )
    clicker = GazeVLMClicker(GazeUIDetector(capture_fn=lambda *_: snap.raw_bgr, enable_ocr=False))
    try:
        ocr_hit = clicker._tier1_fast_keyword_match("box that says ask gemini", snap)
        assert ocr_hit is not None
        assert ocr_hit.label == "Ask Gemini"
    finally:
        clicker.shutdown()

    bridge = FirefoxDOMBridge(host="127.0.0.1", port=0)
    dom_hit = bridge.match_voice_target_in_dom(
        "box that says ask gemini",
        [
            DOMCandidate(
                dom_id=5,
                tag="div",
                role="textbox",
                primary_label="Ask Gemini",
                visible_text="",
                aria_label="Enter a prompt here",
                icon_hints="",
                aliases=["prompt", "textbox", "box", "ask", "question", "gemini"],
                gaze_dist_px=50.0,
                screen_x=500,
                screen_y=800,
            )
        ],
        use_llm_fallback=False,
    )
    assert dom_hit is not None
    assert dom_hit.dom_id == 5


def test_os_window_and_browser_tab_switching() -> None:
    from keyless.audio.active_listener import _VALID_COMMAND_PREFIXES

    for phrase in (
        "switch tab",
        "switch window",
        "previous window",
        "switch to terminal",
        "focus vscode",
        "next workspace",
        "next tab",
    ):
        assert phrase.startswith(_VALID_COMMAND_PREFIXES)

    ptr = DryRunPointerBackend(1920, 1080)

    class _FakeTabBridge:
        is_connected = True

        def __init__(self) -> None:
            self.switched: list[str] = []

        def switch_tab(self, target: str) -> str | None:
            self.switched.append(target)
            if target == "youtube":
                return "YouTube - Music"
            return None

    tb = _FakeTabBridge()
    router = VoiceCommandRouter(pointer=ptr, firefox_bridge=tb)  # type: ignore[arg-type]

    # Individual window stepping via Alt+Tab (hold Alt, tap Tab N times) so tiled/paired windows
    # are never collapsed into one and repeated calls step through every window without ping-pong.
    assert router.dispatch("next window").detail == "os_window_next"
    assert ptr.hold_taps[-1] == ("LEFTALT", "TAB", 1)

    assert router.dispatch("the next window").detail == "os_window_next"
    assert ptr.hold_taps[-1] == ("LEFTALT", "TAB", 2)

    assert router.dispatch("go to next window").detail == "os_window_next"
    assert ptr.hold_taps[-1] == ("LEFTALT", "TAB", 3)

    for variant in ("a next window", "uh next window", "mm next window", "move to next window", "text window", "net window"):
        assert router.dispatch(variant).detail == "os_window_next"
        assert ptr.hold_taps[-1][0:2] == ("LEFTALT", "TAB")

    assert router.dispatch("switch tab").detail == "os_window_next"
    assert ptr.hold_taps[-1][0:2] == ("LEFTALT", "TAB")

    assert router.dispatch("previous window").detail == "os_window_prev"
    assert ptr.combos[-1] == ("LEFTALT", "LEFTSHIFT", "TAB")

    router.dispatch("scroll down")
    assert router.dispatch("next window").detail == "os_window_next"
    assert ptr.hold_taps[-1] == ("LEFTALT", "TAB", 1)

    assert router.dispatch("alt tab").detail == "os_alt_tab"
    assert ptr.combos[-1] == ("LEFTALT", "TAB")

    assert router.dispatch("switch back").detail == "os_alt_shift_tab"
    assert ptr.combos[-1] == ("LEFTALT", "LEFTSHIFT", "TAB")

    assert router.dispatch("switch same app").detail == "os_alt_grave"
    assert ptr.combos[-1] == ("LEFTALT", "GRAVE")

    assert router.dispatch("next workspace").detail == "os_workspace_next"
    assert ptr.combos[-1] == ("LEFTCTRL", "LEFTALT", "RIGHT")

    assert router.dispatch("close window").detail == "close_window"
    assert ptr.combos[-1] == ("LEFTALT", "F4")
    assert router.dispatch("close this app").detail == "close_window"
    assert ptr.combos[-1] == ("LEFTALT", "F4")

    # Named OS app focus via "<app> window" and "switch to <app>"
    for spoken, expected_query in (
        ("antigravity window", "antigravity"),
        ("firefox window", "firefox"),
        ("terminal window", "terminal"),
        ("vscode window", "visual studio code"),
        ("switch to terminal", "terminal"),
    ):
        r_app = router.dispatch(spoken)
        assert r_app.route == "os_window_switch"
        assert ptr.combos[-2] == ("LEFTMETA",)
        assert ptr.typed_texts[-1] == expected_query
        assert ptr.combos[-1] == ("ENTER",)

    # Named browser tab switch when matching Firefox tab exists ("switch to youtube")
    r_yt = router.dispatch("switch to youtube")
    assert r_yt.route == "tab_switch"
    assert "YouTube - Music" in r_yt.detail

    # Explicit ordinal tab fallback when Firefox not connected
    router_no_fx = VoiceCommandRouter(pointer=ptr)
    r_ord = router_no_fx.dispatch("second tab")
    assert r_ord.route == "deterministic"
    assert ptr.combos[-1] == ("LEFTALT", "2")


def test_new_tab_via_firefox_bridge_and_uinput_fallback() -> None:
    from keyless.audio.active_listener import _VALID_COMMAND_PREFIXES

    for phrase in ("new tab", "open a new tab", "create new tab", "new tab github.com"):
        assert phrase.startswith(_VALID_COMMAND_PREFIXES)

    ptr = DryRunPointerBackend(1920, 1080)

    class _FakeNewTabBridge:
        is_connected = True

        def __init__(self) -> None:
            self.opened: list[str | None] = []

        def new_tab(self, query_or_url: str | None = None) -> bool:
            self.opened.append(query_or_url)
            return True

    nb = _FakeNewTabBridge()
    router = VoiceCommandRouter(pointer=ptr, firefox_bridge=nb)  # type: ignore[arg-type]

    r1 = router.dispatch("new tab")
    assert r1.route == "tab_new"
    assert nb.opened[-1] is None

    r2 = router.dispatch("open a new tab")
    assert r2.route == "tab_new"
    assert nb.opened[-1] is None

    r3 = router.dispatch("open new tab to github.com")
    assert r3.route == "tab_new"
    assert nb.opened[-1] == "github.com"

    # URL resolution helper check
    assert FirefoxDOMBridge._resolve_tab_url("github.com") == "https://github.com"
    assert (
        FirefoxDOMBridge._resolve_tab_url("python asyncio")
        == "https://www.google.com/search?q=python+asyncio"
    )

    # Without Firefox bridge connected, "new tab" falls back to Ctrl+T
    router_no_fx = VoiceCommandRouter(pointer=ptr)
    r4 = router_no_fx.dispatch("open a new tab")
    assert r4.route == "deterministic"
    assert ptr.combos[-1] == ("LEFTCTRL", "T")


def test_popup_disambiguation_nested_contours_and_tier_order() -> None:
    from keyless.ipc.firefox_bridge import DOMCandidate
    from keyless.vision.ui_detector import GazeROISnapshot, UIBoundingBox

    # 1. Dialog box enclosing an inner checkbox and inner button -> RETR_LIST finds inner widgets
    dlg = np.full((300, 500, 3), 35, dtype=np.uint8)
    cv2.rectangle(dlg, (20, 20), (480, 280), (140, 140, 140), 2)  # Outer popup frame (h=260 > 110)
    cv2.rectangle(dlg, (50, 160), (70, 180), (200, 200, 200), 2)  # Inner checkbox (20x20)
    cv2.rectangle(dlg, (350, 210), (440, 248), (60, 110, 230), -1)  # Inner button (90x38)
    det = GazeUIDetector(capture_fn=lambda *_: dlg, enable_ocr=False)
    try:
        snap_dlg = det.capture_and_annotate(250.0, 150.0, 500, 300, crop_w=500, crop_h=300)
        kinds = {b.kind for b in snap_dlg.boxes}
        assert "icon" in kinds and "button" in kinds
    finally:
        det.close()

    # 2. Exact "Allow" button beats closer concatenated heading "Allowgemini.google.comtouseyourmicrophone?"
    snap = GazeROISnapshot(
        crop_x=0,
        crop_y=0,
        crop_w=800,
        crop_h=260,
        gaze_x=400.0,
        gaze_y=120.0,
        stereo_feat=None,
        raw_bgr=np.zeros((260, 800, 3), dtype=np.uint8),
        som_png_bytes=b"",
        boxes=[
            UIBoundingBox(
                mark_id=1,
                global_x1=224,
                global_y1=81,
                global_x2=566,
                global_y2=100,
                label="Allowgemini.google.comtouseyourmicrophone?",
                kind="text",
                gaze_dist_px=30.0,
            ),
            UIBoundingBox(
                mark_id=2,
                global_x1=266,
                global_y1=119,
                global_x2=469,
                global_y2=135,
                label="LiveStreamerCAM313Mono",
                kind="text",
                gaze_dist_px=35.0,
            ),
            UIBoundingBox(
                mark_id=3,
                global_x1=593,
                global_y1=191,
                global_x2=635,
                global_y2=207,
                label="Allow",
                kind="text",
                gaze_dist_px=229.0,
            ),
        ],
    )

    class _CountingBridge:
        is_connected = True

        def __init__(self) -> None:
            self.llm_calls = 0

        def get_dom_candidates(self, *_args: Any) -> list[DOMCandidate]:
            return [
                DOMCandidate(
                    dom_id=1,
                    tag="button",
                    role="button",
                    primary_label="New chat",
                    visible_text="New chat",
                    aria_label="New chat",
                    icon_hints="",
                    aliases=["new chat"],
                    gaze_dist_px=100.0,
                    screen_x=100,
                    screen_y=100,
                )
            ]

        def match_voice_target_in_dom(self, *_args: Any, **_kwargs: Any) -> DOMCandidate | None:
            return None

        def _llm_select_dom_candidate(self, *_args: Any, **_kwargs: Any) -> DOMCandidate | None:
            self.llm_calls += 1
            return None

    cb = _CountingBridge()
    clicker = GazeVLMClicker(
        GazeUIDetector(capture_fn=lambda *_: snap.raw_bgr, enable_ocr=False),
        firefox_bridge=cb,  # type: ignore[arg-type]
    )
    try:
        # Mock capture_and_annotate to return our exact popup snapshot
        clicker.detector.capture_and_annotate = lambda *_a, **_kw: snap  # type: ignore[method-assign]
        res = clicker.resolve_voice_target("click allow", 400.0, 120.0)
        assert res is not None
        _, (tx, ty), tier = res
        assert "Tier-1 Keyword ('Allow')" == tier
        assert (tx, ty) == (614, 199)
        assert cb.llm_calls == 0  # Tier-1 popup OCR ran BEFORE Tier-0.5 background DOM LLM!

        hit_ns = clicker._tier1_fast_keyword_match("live streamer", snap)
        assert hit_ns is not None
        assert hit_ns.label == "LiveStreamerCAM313Mono"
    finally:
        clicker.shutdown()


def test_active_listener_allows_app_window_suffix() -> None:
    import threading
    import time

    class _StubStt:
        def transcribe_detailed(self, _audio: np.ndarray) -> SttResult:
            return SttResult(
                text="Antigravity window.",
                no_speech_prob=0.05,
                avg_logprob=-0.12,
            )

    listener = ActiveVoiceListener(
        is_user_attending=lambda: True,
        enable_pop_click=False,
        require_command_prefix=True,
        stt_engine=_StubStt(),
    )
    worker = threading.Thread(target=listener._process_loop, daemon=True)
    worker.start()

    try:
        t = 1.0
        for _ in range(3):
            listener._audio_q.put((t, _quiet_frame()))
            t += 0.032
        for _ in range(6):
            listener._audio_q.put((t, _tone_frame(950.0, amp=0.14)))
            t += 0.032
        for _ in range(26):
            listener._audio_q.put((t, _quiet_frame()))
            t += 0.032

        deadline = time.monotonic() + 2.0
        while listener.events.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        ev = listener.events.get_nowait()
        assert ev.transcript == "Antigravity window."
    finally:
        listener._stop_event.set()
        worker.join(timeout=1.0)


def test_teams_ckeditor_type_a_message_click_triggers_trusted_focus() -> None:
    from keyless.ipc.firefox_bridge import DOMCandidate

    bridge = FirefoxDOMBridge(host="127.0.0.1", port=0)
    teams_box = DOMCandidate(
        dom_id=12,
        tag="div",
        role="textbox",
        primary_label="Type a message",
        visible_text="",
        aria_label="Type a message",
        icon_hints="",
        aliases=["prompt", "input", "textbox", "box", "message", "type", "teams"],
        gaze_dist_px=45.0,
        screen_x=2400,
        screen_y=1280,
        is_editable=True,
    )
    hit = bridge.match_voice_target_in_dom("type a message", [teams_box], use_llm_fallback=False)
    assert hit is not None
    assert hit.dom_id == 12

    class _FakeTeamsBridge:
        is_connected = True

        def get_dom_candidates(self, *_args: Any) -> list[DOMCandidate]:
            return [teams_box]

        def match_voice_target_in_dom(self, *_args: Any, **_kwargs: Any) -> DOMCandidate | None:
            return teams_box

        def execute_dom_action(self, **_kwargs: Any) -> tuple[int, int]:
            return (2400, 1280)

    ptr = DryRunPointerBackend(5360, 1440)
    clicker = GazeVLMClicker(
        GazeUIDetector(capture_fn=lambda *_: np.zeros((480, 680, 3), dtype=np.uint8), enable_ocr=False),
        screen_w=5360,
        screen_h=1440,
        firefox_bridge=_FakeTeamsBridge(),  # type: ignore[arg-type]
    )
    try:
        router = VoiceCommandRouter(
            pointer=ptr,
            gaze_vlm_clicker=clicker,
            gaze_state_provider=lambda: (2400.0, 1280.0, None),
        )
        res = router.dispatch("click type a message")
        assert res.route == "gaze_vlm_click"
        assert "Tier-0 Firefox DOM Focus" in res.detail
        assert ptr.last_abs == (2400.0, 1280.0)
        assert ptr.clicks == [BUTTON_LEFT]  # Emits trusted OS click on CKEditor textbox!
    finally:
        clicker.shutdown()


def test_firefox_dom_number_word_matching() -> None:
    from keyless.ipc.firefox_bridge import DOMCandidate, FirefoxDOMBridge

    bridge = FirefoxDOMBridge()
    r1 = DOMCandidate(
        dom_id=101,
        tag="a",
        role="link",
        primary_label="Rust Programming Language",
        visible_text="Rust Programming Language",
        aria_label="",
        icon_hints="",
        aliases=["result 1", "first result", "option 1", "1"],
        gaze_dist_px=120.0,
        screen_x=400,
        screen_y=300,
    )
    r2 = DOMCandidate(
        dom_id=102,
        tag="a",
        role="link",
        primary_label="Zig Programming Language",
        visible_text="Zig Programming Language",
        aria_label="",
        icon_hints="",
        aliases=["result 2", "second result", "option 2", "2"],
        gaze_dist_px=160.0,
        screen_x=400,
        screen_y=360,
    )
    hit2 = bridge.match_voice_target_in_dom("result two", [r1, r2], use_llm_fallback=False)
    assert hit2 is not None and hit2.dom_id == 102

    hit1 = bridge.match_voice_target_in_dom("first option", [r1, r2], use_llm_fallback=False)
    assert hit1 is not None and hit1.dom_id == 101


def test_desktop_atspi_matching_and_non_browser_routing() -> None:
    from keyless.platform.linux.accessibility import (
        DesktopAccessibleNode,
        LinuxDesktopAccessibilityBridge,
    )

    bridge = LinuxDesktopAccessibilityBridge.__new__(LinuxDesktopAccessibilityBridge)
    invoked_ids: list[str] = []
    nodes = [
        DesktopAccessibleNode(
            node_id="atspi_1",
            role="push button",
            name="Save Changes",
            description="Save file",
            cx=900,
            cy=200,
            w=100,
            h=36,
            aliases=["save changes", "save file"],
        ),
        DesktopAccessibleNode(
            node_id="atspi_2",
            role="list item",
            name="Downloads",
            description="",
            cx=220,
            cy=420,
            w=160,
            h=32,
            aliases=["downloads", "result 2", "second result", "option 2", "2"],
            ordinal=2,
        ),
    ]
    bridge._direct = None  # type: ignore[attr-defined]
    bridge._worker = None  # type: ignore[attr-defined]
    bridge._invoke = lambda node_id, action="click": (invoked_ids.append(f"{node_id}:{action}") or True)  # type: ignore[method-assign]

    res = bridge.match_and_execute("second option", nodes=nodes, gaze_x=220.0, gaze_y=420.0)
    assert res is not None
    matched, invoked = res
    assert matched.node_id == "atspi_2"
    assert invoked is True
    assert invoked_ids == ["atspi_2:click"]

    class _FakeDesktopBridge:
        available = True

        def get_active_window_info(self) -> tuple[str, str]:
            return ("code", "main.py - Visual Studio Code")

        def match_and_execute(
            self,
            target_query: str,
            **_kwargs: Any,
        ) -> tuple[DesktopAccessibleNode, bool] | None:
            if "save" in target_query.lower():
                return nodes[0], True
            return None

        def close(self) -> None:
            pass

    class _FailIfCalledFirefoxBridge:
        is_connected = True

        def get_dom_candidates(self, *_args: Any) -> list[DOMCandidate]:
            raise AssertionError("Firefox DOM should not run when VS Code is active")

    ptr = DryRunPointerBackend(1920, 1080)
    clicker = GazeVLMClicker(
        GazeUIDetector(capture_fn=lambda *_: np.zeros((480, 680, 3), dtype=np.uint8), enable_ocr=False),
        screen_w=1920,
        screen_h=1080,
        firefox_bridge=_FailIfCalledFirefoxBridge(),  # type: ignore[arg-type]
        desktop_atspi=_FakeDesktopBridge(),  # type: ignore[arg-type]
    )
    try:
        router = VoiceCommandRouter(
            pointer=ptr,
            gaze_vlm_clicker=clicker,
            gaze_state_provider=lambda: (900.0, 200.0, None),
        )
        out = router.dispatch("click save changes")
        assert out.route == "gaze_vlm_click"
        assert "Tier-0.5 Desktop AT-SPI" in out.detail
        assert ptr.last_abs == (900.0, 200.0)
        assert ptr.clicks == []  # Already invoked over AT-SPI2 D-Bus; no duplicate click
    finally:
        clicker.shutdown()


def test_command_splitter_and_compound_dispatch() -> None:
    from keyless.ipc.firefox_bridge import DOMCandidate
    from keyless.router.command_splitter import split_trailing_voice_command

    assert split_trailing_voice_command("type rust vs zig into search bar and press enter") == [
        "type rust vs zig into search bar",
        "press enter",
    ]
    assert split_trailing_voice_command("type hello world click submit") == [
        "type hello world",
        "click submit",
    ]
    assert split_trailing_voice_command("click search bar and type mechanical keyboards and press enter") == [
        "click search bar and type mechanical keyboards",
        "press enter",
    ]

    search_box = DOMCandidate(
        dom_id=50,
        tag="input",
        role="searchbox",
        primary_label="Search",
        visible_text="",
        aria_label="Search",
        icon_hints="",
        aliases=["search", "search bar", "input"],
        gaze_dist_px=30.0,
        screen_x=640,
        screen_y=120,
        is_editable=True,
    )

    class _FakeSearchBridge:
        is_connected = True

        def __init__(self) -> None:
            self.actions: list[tuple[int, str, str]] = []

        def get_dom_candidates(self, *_args: Any) -> list[DOMCandidate]:
            return [search_box]

        def match_voice_target_in_dom(self, *_args: Any, **_kwargs: Any) -> DOMCandidate | None:
            return search_box

        def execute_dom_action(self, dom_id: int, action: str = "click", text: str = "") -> tuple[int, int]:
            self.actions.append((dom_id, action, text))
            return (640, 120)

    ff_bridge = _FakeSearchBridge()
    ptr = DryRunPointerBackend(1920, 1080)
    clicker = GazeVLMClicker(
        GazeUIDetector(capture_fn=lambda *_: np.zeros((480, 680, 3), dtype=np.uint8), enable_ocr=False),
        screen_w=1920,
        screen_h=1080,
        firefox_bridge=ff_bridge,  # type: ignore[arg-type]
    )
    try:
        router = VoiceCommandRouter(
            pointer=ptr,
            gaze_vlm_clicker=clicker,
            gaze_state_provider=lambda: (640.0, 120.0, None),
        )
        res = router.dispatch("type rust vs zig into search bar and press enter")
        assert ff_bridge.actions == [(50, "type_into", "rust vs zig")]
        assert ("ENTER",) in ptr.combos
        assert "gaze_vlm_click:" in res.detail
        assert "deterministic:enter" in res.detail
    finally:
        clicker.shutdown()


def test_word_slicing_and_visual_icon_bank_and_save_icon(tmp_path) -> None:
    from keyless.vision.ui_detector import (
        VisualIconBank,
        _slice_line_into_word_boxes,
    )

    # 1. Projection-valley word slicing splits merged menu bar into distinct word boxes
    gray = np.full((40, 300), 240, dtype=np.uint8)
    cv2.putText(gray, "File   Edit   Terminal", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, 20, 2)
    sliced = _slice_line_into_word_boxes(gray, 10, 8, 280, 34, "File Edit Terminal")
    words_map = {label: (x1, y1, x2, y2) for x1, y1, x2, y2, label, kind in sliced if kind == "word"}
    assert {"File", "Edit", "Terminal"} <= set(words_map.keys())
    assert words_map["File"][0] < words_map["Edit"][0] < words_map["Terminal"][0]

    # 2. VisualIconBank learns a 36x36 gear icon patch and matches it in <3ms
    desktop = _make_desktop_image()
    bank = VisualIconBank(cache_dir=tmp_path)
    gear_patch = desktop[502:538, 1085:1121].copy()
    bank.save_icon_patch("gear icon", gear_patch)
    edges = cv2.Canny(cv2.cvtColor(desktop, cv2.COLOR_BGR2GRAY), 45, 135)
    hits = bank.find_matching_icons(desktop, edges)
    assert any(lbl == "gear icon" and abs(x1 - 1085) <= 6 and abs(y1 - 502) <= 6 for x1, y1, _x2, _y2, lbl in hits)

    # 3. "save icon as custom star" voice command saves 44x44 patch under gaze
    det = GazeUIDetector(
        capture_fn=lambda x, y, w, h: desktop[y : y + h, x : x + w].copy(),
        enable_ocr=False,
        icon_cache_dir=tmp_path,
    )
    clicker = GazeVLMClicker(detector=det, screen_w=1920, screen_h=1080)
    ptr = DryRunPointerBackend(1920, 1080)
    try:
        router = VoiceCommandRouter(
            pointer=ptr,
            gaze_vlm_clicker=clicker,
            gaze_state_provider=lambda: (1103.0, 520.0, None),
        )
        r_save = router.dispatch("save icon as custom gear")
        assert r_save.route == "save_icon"
        assert (tmp_path / "custom_gear.png").exists()
    finally:
        clicker.shutdown()


def test_pose_invariant_rbf_and_online_gaze_learner(tmp_path) -> None:
    from keyless.config import AppConfig
    from keyless.vision.calibrator import (
        _RBF_METRIC_WEIGHTS,
        StereoGazeModel,
        expand_stereo_features,
    )
    from keyless.vision.online_learner import OnlineGazeLearner

    # 1. Pose translation & roll weights are zeroed in _RBF_METRIC_WEIGHTS
    for idx in (6, 7, 8, 9, 16, 17, 18, 19):
        assert _RBF_METRIC_WEIGHTS[idx] == 0.0

    # 2. expand_stereo_features soft-saturates large head translations via tanh
    z_extreme = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)
    z_extreme[7] = 10.0  # 10-sigma chair slouch
    exp = expand_stereo_features(z_extreme)
    assert abs(exp[7]) <= 2.21

    # 3. OnlineGazeLearner EW-RLS corrects a 90px posture slouch after 1-2 clicks
    rng = np.random.default_rng(7)
    X = rng.normal(0.0, 0.1, size=(48, STEREO_FEATURE_DIM))
    Y = np.column_stack([960.0 + X[:, 0] * 1200.0, 540.0 + X[:, 1] * 900.0])
    mon = MonitorInfo("DP-1", 0, 0, 1920, 1080, 1920, 1080, 1.0, True)
    layout = DisplayLayout(monitors=(mon,), total_width=1920, total_height=1080)
    base_model = StereoGazeModel.fit(
        X=X,
        targets_xy=Y,
        cam1_by_id="cam1",
        cam2_by_id="cam2",
        monitor=mon,
        layout=layout,
    )
    learner = OnlineGazeLearner(
        model=base_model,
        config=AppConfig(cam1_by_id="cam1", cam2_by_id="cam2"),
        monitor=mon,
        layout=layout,
        calib_path=tmp_path / "stereo_calib.json",
        refit_interval=100,
    )
    feat0 = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)
    bx, by = learner.predict(feat0, now_ts=10.0)
    learner.predict(feat0, now_ts=10.1)
    true_tx, true_ty = bx + 80.0, by - 60.0
    ok = learner.record_verified_interaction(
        true_x=true_tx,
        true_y=true_ty,
        anchor_ts=10.2,
        source="voice",
    )
    assert ok is True
    nx, ny = learner.predict(feat0, now_ts=10.3)
    assert abs(true_tx - nx) < abs(true_tx - bx) * 0.65
    assert abs(true_ty - ny) < abs(true_ty - by) * 0.65
    # Second click snaps error cleanly below 45% of initial slouch
    learner.record_verified_interaction(
        true_x=true_tx,
        true_y=true_ty,
        anchor_ts=10.4,
        source="voice",
    )
    nx2, ny2 = learner.predict(feat0, now_ts=10.5)
    assert abs(true_tx - nx2) < abs(true_tx - bx) * 0.48
    assert abs(true_ty - ny2) < abs(true_ty - by) * 0.48

    # Outlier click >240px away is rejected and never corrupts bias
    bias_before = learner.bias_xy
    rejected = learner.record_verified_interaction(
        true_x=nx2 + 400.0,
        true_y=ny2 + 400.0,
        anchor_ts=10.6,
        source="voice",
    )
    assert rejected is False
    assert learner.bias_xy == bias_before


@pytest.mark.skipif(sys.platform != "linux", reason="Wayland cursor harvesting is Linux-only")
def test_wayland_cursor_sprite_diff_and_scroll_handler() -> None:
    from keyless.input.click_harvest import WaylandCursorPositionResolver
    from keyless.router.scroll_handler import parse_voice_scroll_command

    # 1. Cursor sprite diff finds hardware cursor hotspot on synthetic frame pair
    resolver = WaylandCursorPositionResolver(allow_screenshot_diff=True)
    img0 = np.full((400, 600, 3), 50, dtype=np.uint8)
    img1 = img0.copy()
    cv2.rectangle(img1, (320, 180), (336, 200), (250, 250, 250), -1)
    calls = [img0, img1]
    resolver._dbus_full_screenshot = lambda **_kw: calls.pop(0)  # type: ignore[method-assign]
    hotspot = resolver.resolve_cursor_xy(300.0, 190.0)
    assert hotspot is not None
    assert 320 <= hotspot[0] <= 336 and 180 <= hotspot[1] <= 200

    # 2. Voice scroll parser + Firefox DOM scroll + uinput fallback
    s_auto = parse_voice_scroll_command("auto scroll down")
    assert s_auto is not None and s_auto.continuous == "start" and s_auto.direction == "down"

    s_little = parse_voice_scroll_command("scroll down a little")
    assert s_little is not None and s_little.amount == "small" and s_little.wheel_ticks == -3

    s_top = parse_voice_scroll_command("scroll to top")
    assert s_top is not None and s_top.direction == "top" and s_top.wheel_ticks == 25

    class _FakeScrollFirefoxBridge:
        is_connected = True

        def __init__(self) -> None:
            self.scroll_calls: list[tuple[float, float, str, str, str]] = []

        def scroll_page(
            self,
            gaze_x: float,
            gaze_y: float,
            direction: str = "down",
            amount: str = "normal",
            continuous: str = "",
        ) -> bool:
            self.scroll_calls.append((gaze_x, gaze_y, direction, amount, continuous))
            return True

    ff = _FakeScrollFirefoxBridge()
    ptr = DryRunPointerBackend(1920, 1080)
    router = VoiceCommandRouter(
        pointer=ptr,
        gaze_provider=lambda: (750.0, 420.0),
        firefox_bridge=ff,  # type: ignore[arg-type]
    )
    res_dom = router.dispatch("scroll down a little")
    assert res_dom.route == "scroll_dom"
    assert ff.scroll_calls == [(750.0, 420.0, "down", "small", "")]

    # Native app fallback when Firefox is not connected
    ff.is_connected = False
    res_native = router.dispatch("scroll down a little")
    assert res_native.route == "scroll"
    assert ptr.last_abs == (750.0, 420.0)
    assert ptr.scrolls[-1] == (0, -3)


def test_pose_translation_stripping_on_load_recenter_and_gaze_warped_scroll(tmp_path) -> None:
    from keyless.vision.calibrator import (
        _POSE_TRANSLATION_EXPANDED_COLS,
        StereoGazeModel,
    )

    rng = np.random.default_rng(11)
    X = rng.normal(0.0, 0.1, size=(48, STEREO_FEATURE_DIM))
    Y = np.column_stack([960.0 + X[:, 0] * 1200.0, 540.0 + X[:, 1] * 900.0])
    mon = MonitorInfo("DP-1", 0, 0, 1920, 1080, 1920, 1080, 1.0, True)
    layout = DisplayLayout(monitors=(mon,), total_width=1920, total_height=1080)
    m = StereoGazeModel.fit(X, Y, "cam1", "cam2", mon, layout)
    # Simulate an older stereo_calib.json with parasitic tx/ty/tz weights baked in
    m.coef_x[7] = 250.0
    m.coef_y[8] = -250.0
    calib_file = tmp_path / "stereo_calib.json"
    m.save(calib_file)

    loaded = StereoGazeModel.load(calib_file, strip_pose_translation=True)
    for col in _POSE_TRANSLATION_EXPANDED_COLS:
        assert loaded.coef_x[col] == 0.0
        assert loaded.coef_y[col] == 0.0

    # Chair slouch (+5 sigma tx/ty/tz) produces zero drift on loaded model
    f_neutral = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)
    f_slouch = f_neutral.copy()
    f_slouch[[7, 8, 9, 17, 18, 19]] = 0.25
    assert loaded.predict(f_neutral) == loaded.predict(f_slouch)

    # Manual "recenter gaze" snaps prediction to monitor center (960, 540)
    ptr = DryRunPointerBackend(5360, 1440)
    router = VoiceCommandRouter(
        pointer=ptr,
        gaze_provider=lambda: (2600.0, 700.0),
        on_recenter_gaze=lambda: f"offset={loaded.recenter_to(960.0, 540.0, f_neutral)}",
    )
    r_rec = router.dispatch("recenter gaze")
    assert r_rec.route == "recenter_gaze"
    rx, ry = loaded.predict(f_neutral)
    assert abs(rx - 960.0) < 1.0 and abs(ry - 540.0) < 1.0

    # "sync window" routes to sync_window (never hijacked by "<app> window" OS overview search)
    r_sync = router.dispatch("sync window")
    assert r_sync.route == "sync_window"
    assert ptr.last_abs == (2600.0, 700.0)

    # Non-browser "scroll down" warps pointer to gaze region before scrolling
    r_scroll = router.dispatch("scroll down")
    assert r_scroll.route == "scroll"
    assert ptr.last_abs == (2600.0, 700.0)
    assert ptr.scrolls[-1] == (0, -6)
