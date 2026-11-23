"""Combined dual-camera gaze, pop-click, and voice-routing daemon."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from rich.console import Console

from keyless.audio.active_listener import ActiveVoiceListener
from keyless.audio.pop_detector import PopTriggerStateMachine
from keyless.audio.stt_engine import UnifiedAudioService
from keyless.config import AppConfig, DEFAULT_CALIB_PATH, current_paths, get_display_layout
from keyless.input.pointer import DryRunPointerBackend, PointerBackend
from keyless.platform.router import (
    create_pointer_backend,
    current_host,
    monitor_at_point,
    pointer_position,
)
from keyless.ipc.firefox_bridge import FirefoxDOMBridge
from keyless.ipc.state import FLAG_FACE_DETECTED, SharedGazeReader
from keyless.ipc.transcript import write_transcript
from keyless.router.dispatcher import VoiceCommandRouter
from keyless.router.gaze_vlm_clicker import GazeVLMClicker
from keyless.models.providers.roles import resolve_gaze_vlm_role, resolve_text_role
from keyless.routing.llm import LLMAgent
from keyless.voice.stt.factory import SttModelMissing, create_stt_engine
from keyless.vision.calibrator import (
    StereoGazeModel,
    append_harvested_calibration_samples,
    load_gaze_model,
)
from keyless.vision.cursor_gimbal import (
    GazeGimbalController,
    GimbalStepResult,
    run_vision_loop,
)
from keyless.vision.geo_gaze import GeoGazeModel, GeoGazeTracker
from keyless.vision.online_learner import OnlineGazeLearner
from keyless.vision.ui_detector import GazeUIDetector


def _desktop_bridge():  # type: ignore[no-untyped-def]
    if current_host() == "macos":
        from keyless.platform.stub import UnsupportedAccessibilityBridge

        return UnsupportedAccessibilityBridge("macos")
    from keyless.platform.linux.accessibility import LinuxDesktopAccessibilityBridge

    return LinuxDesktopAccessibilityBridge()


def run_combined_daemon(
    config: AppConfig,
    calib_path: Path | None = None,
    enable_vision: bool = True,
    enable_audio: bool = True,
    dictate_unprefixed: bool = False,
    dry_run: bool = False,
    duration_sec: float | None = None,
    console: Console | None = None,
) -> None:
    """Run vision gaze tracking, acoustic pop-click, and voice STT in one process."""
    out = console or Console()
    if not enable_vision and not enable_audio:
        raise ValueError("At least one of vision or audio must be enabled.")

    layout = get_display_layout()
    model: StereoGazeModel | GeoGazeModel | None = None
    if enable_vision:
        if calib_path is None or not calib_path.exists():
            raise FileNotFoundError(
                f"Calibration file not found: {calib_path}. Run `uv run keyless calibrate` first."
            )
        model = load_gaze_model(calib_path, layout=layout, strip_pose_translation=True)
    pointer: PointerBackend = (
        DryRunPointerBackend(layout.total_width, layout.total_height)
        if dry_run
        else create_pointer_backend(
            layout.total_width, layout.total_height,
            action_backend=config.action_backend,
        )
    )
    if enable_vision:
        # Eye motion goes to cua-driver's independent agent cursor. The same
        # sink delegates clicks/typing to the action pointer, so the router and
        # the gaze loop agree on where the eye cursor is without warping the
        # system mouse.
        from keyless.platform.cua.agent_cursor import create_gaze_sink

        pointer, gaze_overlay, _gaze_move = create_gaze_sink(config, pointer, dry_run)
    else:
        gaze_overlay = None
    pointer_mode = None
    if gaze_overlay is not None and current_host() == "macos":
        from keyless.platform.macos.mouse_events import MacOSPointerVisibility

        pointer_mode = MacOSPointerVisibility(gaze_overlay)
        pointer_mode.start()

    online_learner: OnlineGazeLearner | None = None
    click_harvester = None
    if (
        enable_vision
        and not dry_run
        and isinstance(model, StereoGazeModel)
        and bool(getattr(config, "gaze_click_learning", True))
    ):
        online_learner = OnlineGazeLearner(
            model=model,
            config=config,
            monitor=layout.primary,
            layout=layout,
            calib_path=calib_path or DEFAULT_CALIB_PATH,
            allow_disk_overwrite=True,
        )
        if current_host() == "macos":
            try:
                from keyless.platform.macos.mouse_events import MacOSMouseClickHarvester

                click_harvester = MacOSMouseClickHarvester(
                    online_learner,
                    log_path=current_paths().logs_dir / "gaze_clicks.jsonl",
                )
                click_harvester.start()
                out.print(
                    "[green]Learning gaze from stationary mouse clicks "
                    "(~180 ms pre-click; quick drags ignored).[/green]"
                )
            except Exception as exc:
                click_harvester = None
                out.print(f"[yellow]Click learning unavailable: {exc}[/yellow]")

    latest_gaze_lock = threading.Lock()
    latest_gaze: tuple[float, float] | None = None
    latest_vlm_state: tuple[float, float, np.ndarray | None] | None = None
    latest_attention: tuple[bool, float] = (False, 0.0)
    active_controller: GazeGimbalController | None = None
    active_tracker: GeoGazeTracker | None = None
    firefox_bridge: FirefoxDOMBridge | None = None

    def _on_vision_step(step: GimbalStepResult) -> None:
        nonlocal latest_gaze, latest_vlm_state, latest_attention
        with latest_gaze_lock:
            latest_gaze = (step.cursor_x, step.cursor_y)
            latest_vlm_state = (step.gaze_x, step.gaze_y, step.stereo_feat)
            latest_attention = (bool(step.flags & FLAG_FACE_DETECTED), float(step.yaw_deg))
        if firefox_bridge is not None and firefox_bridge.is_connected:
            mon = monitor_at_point(layout, step.gaze_x, step.gaze_y)
            firefox_bridge.broadcast_gaze(
                step.gaze_x,
                step.gaze_y,
                mon_x=mon.x,
                mon_y=mon.y,
                mon_w=mon.width,
                mon_h=mon.height,
            )

    def _inproc_gaze() -> tuple[float, float] | None:
        with latest_gaze_lock:
            return latest_gaze

    def _inproc_vlm_state() -> tuple[float, float, np.ndarray | None] | None:
        with latest_gaze_lock:
            return latest_vlm_state

    def _is_user_attending() -> bool:
        with latest_gaze_lock:
            face_visible, yaw_deg = latest_attention
        return face_visible and abs(yaw_deg) < config.attention_max_yaw_deg

    def _on_controller_ready(ctrl: GazeGimbalController) -> None:
        nonlocal active_controller
        active_controller = ctrl

    def _on_tracker_ready(tracker: GeoGazeTracker) -> None:
        nonlocal active_tracker
        active_tracker = tracker

    def _on_before_click(tx: int, ty: int) -> None:
        if active_controller is not None:
            active_controller.warp_and_hold(float(tx), float(ty), hold_sec=0.25)
        if active_tracker is not None:
            active_tracker.record_click(float(tx), float(ty), time.monotonic())

    gaze_reader: SharedGazeReader | None = None
    audio_svc: UnifiedAudioService | None = None
    ptt_mon = None  # EvdevHotkeyMonitor on Linux; None on macOS (SwiftUI host owns PTT)
    active_listener: ActiveVoiceListener | None = None
    speaker = None
    stt = None
    event_stop = threading.Event()
    event_thread: threading.Thread | None = None
    vlm_clicker: GazeVLMClicker | None = None
    vlm_provider = None

    def _current_gaze_xy_fallback() -> tuple[float, float]:
        st = _inproc_vlm_state()
        if st is not None:
            return float(st[0]), float(st[1])
        gz = _inproc_gaze()
        if gz is not None:
            return float(gz[0]), float(gz[1])
        mon = layout.primary
        return float(mon.x + mon.width * 0.5), float(mon.y + mon.height * 0.5)

    try:
        # Automatic online learning and passive click harvesting are disabled by default so
        # look-then-speak saccades never degrade calibration. Use "recenter gaze" for manual snap.
        if config.firefox_bridge_enabled:
            firefox_bridge = FirefoxDOMBridge(port=config.firefox_bridge_port)
            firefox_bridge.start()
            out.print(
                f"[green]Firefox DOM bridge listening on ws://127.0.0.1:{firefox_bridge.bound_port}[/green]"
            )

        if enable_audio:
            gaze_provider: Callable[[], tuple[float, float] | None] | None = (
                _inproc_gaze if enable_vision else None
            )
            if not enable_vision:
                try:
                    gaze_reader = SharedGazeReader()
                    out.print("[green]Attached to external gaze shared memory.[/green]")
                except FileNotFoundError:
                    out.print("[dim]Vision loop not active; gaze coordinates unavailable.[/dim]")

            vlm_provider = None
            detector = GazeUIDetector()
            desktop_atspi = _desktop_bridge()
            vlm_enabled = False
            if config.gaze_vlm_enabled:
                from keyless.models.providers.ollama import OllamaProvider

                vlm_role = resolve_gaze_vlm_role(config)
                if vlm_role.reasons:
                    out.print(
                        "[yellow]Gaze VLM disabled: "
                        + " ".join(vlm_role.reasons)
                        + " Tier-0 DOM, Tier-1 OCR, and AT-SPI still active.[/yellow]"
                    )
                else:
                    vlm_enabled = True
                    # For local Ollama, leave vlm_provider=None so GazeVLMClicker uses its
                    # strict Pydantic JSON-schema ollama.chat(..., format=schema) path.
                    if not isinstance(vlm_role.provider, OllamaProvider):
                        vlm_provider = vlm_role.provider
            vlm_clicker = GazeVLMClicker(
                detector=detector,
                model_name=config.gaze_vlm_model,
                layout=layout,
                crop_w=config.gaze_crop_w,
                crop_h=config.gaze_crop_h,
                keyword_threshold=config.gaze_keyword_threshold,
                firefox_bridge=firefox_bridge,
                desktop_atspi=desktop_atspi,
                vlm_provider=vlm_provider,
                enable_vlm=vlm_enabled,
            )
            if vlm_enabled:
                def _warm_vlm() -> None:
                    assert vlm_clicker is not None
                    if not vlm_clicker.warm_model_in_vram():
                        out.print(
                            f"[yellow]Warning: VLM '{config.gaze_vlm_model}' warmup failed. "
                            "Tier-0 DOM & Tier-1 OCR still active.[/yellow]"
                        )

                threading.Thread(
                    target=_warm_vlm,
                    name="vlm-warmup",
                    daemon=True,
                ).start()

            text_role = resolve_text_role(config)
            if text_role.reasons:
                out.print(
                    "[yellow]Text LLM unavailable: "
                    + " ".join(text_role.reasons)
                    + " Deterministic commands still run.[/yellow]"
                )
            llm = LLMAgent(
                backend="auto",
                provider=text_role.provider,
            )
            def _on_recenter_gaze() -> str:
                if model is None:
                    return "Vision loop not active."
                st = _inproc_vlm_state()
                if st is None or st[2] is None:
                    return "No current face/eye observation to recenter."
                active_model = online_learner.model if online_learner is not None else model
                mon = monitor_at_point(layout, st[0], st[1])
                cx = mon.x + mon.width * 0.5
                cy = mon.y + mon.height * 0.5
                dx, dy = active_model.recenter_to(cx, cy, st[2])
                return f"Recentered gaze to monitor center ({cx:.0f}, {cy:.0f}) with offset=({dx:+.0f}, {dy:+.0f})px"

            def _on_steer_to_mouse() -> str:
                if model is None:
                    return "Vision loop not active."
                st = _inproc_vlm_state()
                if st is None or st[2] is None:
                    return "No current face/eye observation to steer."
                mouse = pointer_position()
                if mouse is None:
                    return "Could not read the real pointer position on this host."
                mx, my = mouse
                active_model = online_learner.model if online_learner is not None else model
                dx, dy = active_model.recenter_to(mx, my, st[2])
                if active_controller is not None:
                    active_controller.warp_and_hold(mx, my, hold_sec=0.35)
                acknowledge = getattr(pointer, "acknowledge", None)
                if callable(acknowledge):
                    acknowledge(mx, my)
                if pointer_mode is not None:
                    pointer_mode.force_eye_mode()
                return (
                    f"Eye cursor snapped to the mouse at ({mx:.0f}, {my:.0f}); "
                    f"offset=({dx:+.0f}, {dy:+.0f})px"
                )

            router = VoiceCommandRouter(
                pointer=pointer,
                llm_agent=llm,
                gaze_reader=gaze_reader,
                gaze_provider=gaze_provider,
                dictate_unprefixed=dictate_unprefixed,
                gaze_vlm_clicker=vlm_clicker,
                gaze_state_provider=_inproc_vlm_state if enable_vision else None,
                on_before_click=_on_before_click,
                firefox_bridge=firefox_bridge,
                on_recenter_gaze=_on_recenter_gaze if enable_vision else None,
                on_steer_to_mouse=_on_steer_to_mouse if enable_vision else None,
            )

            def _prewarm_gaze_vlm() -> None:
                if vlm_clicker is None:
                    return
                st = _inproc_vlm_state()
                if st is not None:
                    vlm_clicker.on_speech_start_prewarm(st[0], st[1], st[2])
                elif gaze_reader is not None:
                    snap = gaze_reader.read()
                    if snap is not None:
                        vlm_clicker.on_speech_start_prewarm(snap.x, snap.y, None)
                else:
                    mon = layout.primary
                    vlm_clicker.on_speech_start_prewarm(
                        mon.x + mon.width * 0.5, mon.y + mon.height * 0.5, None
                    )

            try:
                stt = create_stt_engine(config).engine
            except SttModelMissing as exc:
                out.print(f"[red]{exc}[/red]")
                return
            out.print(
                f"STT: {stt.status().name} ({stt.status().device}/{stt.status().compute_type})"
            )

            if config.tts_enabled:
                try:
                    from keyless.voice.tts import WoofSpeaker, tts_report

                    if tts_report()["ready"]:
                        speaker = WoofSpeaker(voice=config.tts_voice, enabled=True)
                        out.print(
                            f"[green]TTS ready (Voice by Underdog Woof, voice={config.tts_voice})[/green]"
                        )
                    else:
                        out.print(
                            "[yellow]TTS enabled but Underdog Woof weights missing "
                            "(run `keyless models download underdog-woof`).[/yellow]"
                        )
                except Exception as exc:
                    out.print(f"[yellow]TTS unavailable: {exc}[/yellow]")

            def _speak_result(res) -> None:
                if speaker is None or not config.tts_speak_replies:
                    return
                if res.payload.get("spoken") or res.route == "assistant":
                    speaker.speak(res.detail, interrupt=True)
                elif res.route == "terminal" and res.payload.get("command"):
                    speaker.speak("Typed shell command.", interrupt=True)

            if config.always_on_voice:
                transcript_path = current_paths().transcript_path

                def _publish_transcript(text: str) -> None:
                    write_transcript(transcript_path, text)

                active_listener = ActiveVoiceListener(
                    on_speech_onset=_prewarm_gaze_vlm,
                    is_user_attending=_is_user_attending if enable_vision else None,
                    enable_pop_click=config.enable_pop_click,
                    require_command_prefix=(config.require_command_prefix and not dictate_unprefixed),
                    audio_device=config.audio_device,
                    stt_engine=stt,
                    on_transcript=_publish_transcript,
                )
                if speaker is not None:
                    speaker._on_speaking_changed = active_listener.set_muted
                out.print(
                    f"[green]ActiveVoiceListener ready ({active_listener.device}/{active_listener.compute_type}) "
                    f"| LLM={llm.backend} | pop_click={config.enable_pop_click}[/green]"
                )

                def _drain_voice_events() -> None:
                    assert active_listener is not None
                    while not event_stop.is_set():
                        try:
                            ev = active_listener.events.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        if ev.kind == "pop_click":
                            out.print("[green]POP (75ms-vetoed) -> click[/green]")
                            pointer.click(1)
                        elif ev.kind == "voice_command":
                            out.print(
                                f"[cyan]Voice ({ev.confidence:.2f}):[/cyan] {ev.transcript!r}"
                            )
                            try:
                                res = router.dispatch(ev.transcript)
                                out.print(f"  -> [bold]{res.route}[/bold]: {res.detail}")
                                _speak_result(res)
                            except Exception as exc:
                                out.print(f"  -> [red]Dispatch error: {exc}[/red]")

                active_listener.start()
                event_thread = threading.Thread(
                    target=_drain_voice_events,
                    name="voice-event-dispatcher",
                    daemon=True,
                )
                event_thread.start()
            else:
                info = stt.status()
                out.print(
                    f"[green]STT ready ({info.device}/{info.compute_type}) | LLM={llm.backend}[/green]"
                )

                pop = PopTriggerStateMachine(
                    min_peak=config.pop_min_peak,
                    min_rms=config.pop_min_rms,
                )

                def _on_pop() -> None:
                    out.print("[green]POP -> click[/green]")
                    pointer.click(1)

                def _on_utterance(audio: np.ndarray) -> None:
                    t0 = time.perf_counter()
                    transcript = stt.transcribe(audio)
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                    if not transcript:
                        return
                    out.print(f"[cyan]STT ({dt_ms:.0f}ms):[/cyan] {transcript!r}")
                    try:
                        res = router.dispatch(transcript)
                        out.print(f"  -> [bold]{res.route}[/bold]: {res.detail}")
                        _speak_result(res)
                    except Exception as exc:
                        out.print(f"  -> [red]Dispatch error: {exc}[/red]")

                audio_svc = UnifiedAudioService(
                    pop_detector=pop,
                    on_pop=_on_pop,
                    on_utterance=_on_utterance,
                    device=config.audio_device,
                )

                def _on_ptt_press() -> None:
                    audio_svc.begin_ptt()
                    _prewarm_gaze_vlm()

                if current_host() == "macos":
                    # macOS PTT via CGEventTap lands with the SwiftUI host;
                    # voice runs always-on here until the tap ships.
                    out.print("[yellow]PTT hotkey is handled by the SwiftUI host on macOS; "
                              "using always-on voice in the engine.[/yellow]")
                    ptt_mon = None
                else:
                    from keyless.platform.linux.hotkeys import EvdevHotkeyMonitor, resolve_hotkey_code

                    hotkey_code = resolve_hotkey_code(config.ptt_hotkey)
                    ptt_mon = EvdevHotkeyMonitor(
                        hotkey_code=hotkey_code,
                        on_press=_on_ptt_press,
                        on_release=audio_svc.end_ptt,
                    )
                    n_devs = ptt_mon.start()
                    if n_devs == 0:
                        out.print(
                            f"[yellow]Warning: no readable /dev/input/event* device has {config.ptt_hotkey}. "
                            "Pop-click active, PTT hotkey unavailable.[/yellow]"
                        )
                    else:
                        out.print(
                            f"PTT={config.ptt_hotkey} ({n_devs} device(s)) | "
                            f"Pop(peak>={config.pop_min_peak:.2f}, rms>={config.pop_min_rms:.3f})"
                        )
                audio_svc.start()

        if enable_vision:
            assert model is not None
            if getattr(pointer, "is_agent_cursor_sink", False):
                gaze_mode = "eye cursor on cua-driver's independent overlay"
            elif config.gaze_cursor_control and not _gaze_move:
                gaze_mode = "internal gaze guide (agent cursor unavailable; mouse untouched)"
            elif config.gaze_cursor_control:
                gaze_mode = "eye-mouse cursor control"
            else:
                gaze_mode = "internal gaze guide (physical mouse untouched)"
            out.print(
                f"Starting gaze loop ({gaze_mode}, dry_run={dry_run}, "
                f"RMSE X={model.rmse_x:.1f}px Y={model.rmse_y:.1f}px). Press Ctrl+C to stop."
            )
            run_vision_loop(
                config=config,
                model=model,
                dry_run=dry_run,
                duration_sec=duration_sec,
                pointer=pointer,
                on_step=_on_vision_step,
                on_controller_ready=_on_controller_ready,
                on_tracker_ready=_on_tracker_ready,
                predict_gaze=online_learner.predict if online_learner is not None else None,
            )
        else:
            start_t = time.monotonic()
            while True:
                if duration_sec is not None and (time.monotonic() - start_t) >= duration_sec:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        out.print("\nStopped.")
    finally:
        event_stop.set()
        if pointer_mode is not None:
            pointer_mode.stop()
        if click_harvester is not None:
            click_harvester.stop()
        if event_thread is not None and event_thread.is_alive():
            event_thread.join(timeout=1.0)
        if active_listener is not None:
            active_listener.stop()
        if speaker is not None:
            speaker.close()
        if stt is not None:
            stt.close()
        if ptt_mon is not None:
            ptt_mon.stop()
        if audio_svc is not None:
            audio_svc.stop()
        if vlm_provider is not None:
            vlm_provider.close()
        if vlm_clicker is not None:
            if config.harvest_calib and calib_path is not None:
                harvested = vlm_clicker.pop_harvested_samples()
                if harvested:
                    added = append_harvested_calibration_samples(
                        calib_path, harvested, config.cam1_by_id, config.cam2_by_id
                    )
                    if added > 0:
                        out.print(
                            f"[green]Saved {added} harvested voice-click calibration sample(s).[/green]"
                        )
            vlm_clicker.shutdown()
        if firefox_bridge is not None:
            firefox_bridge.stop()
        if gaze_reader is not None:
            gaze_reader.close()
        pointer.close()
