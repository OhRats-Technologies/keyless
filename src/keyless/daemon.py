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
from keyless.browser.planner import ProviderEndpoint, build_browser_planner
from keyless.browser.runtime import BrowserRuntime
from keyless.config import AppConfig, current_paths, get_display_layout
from keyless.gaze import GazeFrame, SharedMemoryGazeSource, run_gaze_loop
from keyless.input.pointer import DryRunPointerBackend, PointerBackend
from keyless.platform.router import (
    create_pointer_backend,
    current_host,
    monitor_at_point,
)
from keyless.ipc.firefox_bridge import FirefoxDOMBridge
from keyless.ipc.state import FLAG_FACE_DETECTED
from keyless.ipc.transcript import level_path_for, write_level, write_transcript
from keyless.router.dispatcher import VoiceCommandRouter
from keyless.router.gaze_vlm_clicker import GazeVLMClicker
from keyless.models.providers.roles import resolve_gaze_vlm_role, resolve_text_role
from keyless.routing.llm import LLMAgent
from keyless.voice.stt.factory import SttModelMissing, create_stt_engine
from keyless.vision.ui_detector import GazeUIDetector


def _desktop_bridge():  # type: ignore[no-untyped-def]
    if current_host() == "macos":
        from keyless.platform.stub import UnsupportedAccessibilityBridge

        return UnsupportedAccessibilityBridge("macos")
    from keyless.platform.linux.accessibility import LinuxDesktopAccessibilityBridge

    return LinuxDesktopAccessibilityBridge()


class _CaptionsGate:
    """Read the live `transcript_overlay` setting so captions toggled in the UI apply at once.

    The setting is cached for a second because the listener calls this every audio frame.
    """

    def __init__(self, settings_path: Path, *, ttl_s: float = 1.0) -> None:
        self._settings_path = settings_path
        self._ttl_s = ttl_s
        self._checked_at = float("-inf")
        self._enabled = False

    def __call__(self) -> bool:
        now = time.monotonic()
        if now - self._checked_at >= self._ttl_s:
            self._checked_at = now
            try:
                self._enabled = bool(AppConfig.load(self._settings_path).transcript_overlay)
            except Exception:
                self._enabled = False
        return self._enabled


def create_browser_runtime(
    config: AppConfig,
    text_provider: object | None = None,
    vision_provider: object | None = None,
    speak: Callable[[str], object] | None = None,
) -> BrowserRuntime:
    """Build a BrowserRuntime wired to keyless's resolved text and vision providers."""
    resolved_text = (
        text_provider
        if text_provider is not None
        else ProviderEndpoint(
            kind="ollama",
            model=config.llm_model or config.ollama_model or "qwen2.5:14b",
            base_url="http://127.0.0.1:11434",
        )
    )
    resolved_vision = (
        vision_provider
        if vision_provider is not None
        else ProviderEndpoint(
            kind="ollama",
            model=config.gaze_vlm_model or "gemma4:e4b",
            base_url="http://127.0.0.1:11434",
        )
    )
    planner = build_browser_planner(
        text_provider=resolved_text,
        vision_provider=resolved_vision,
    )
    return BrowserRuntime(
        planner=planner,
        speak=speak,
        auto_seed_on_first_run=config.playwright_auto_seed_firefox,
    )


def create_control_server(paths: "AppPaths | None" = None):  # type: ignore[name-defined]
    """Engine-owned control plane with the schema-v1 UI contract registered.

    Every host (headless ``keyless serve``, the Qt shell) gets this factory
    instead of wiring the builder itself, so no shell can forget the contract
    and no engine module imports ``keyless.ui``. The ``ControlServer`` itself
    keeps the builder injectable so tests can exercise the 503 path.
    """
    from keyless.ipc.control import ControlServer
    from keyless.ui.contract import build_ui_state

    server = ControlServer(paths=paths)
    server.ui_contract_builder = build_ui_state
    return server


def run_combined_daemon(
    config: AppConfig,
    enable_vision: bool = True,
    enable_audio: bool = True,
    dictate_unprefixed: bool = False,
    dry_run: bool = False,
    duration_sec: float | None = None,
    console: Console | None = None,
) -> None:
    """Run the gaze-overlay/voice daemon around the new model-agnostic seam.

    The legacy in-process calibration stack is gone. ``enable_vision`` now
    means "attach to the gaze frame channel published by a model process";
    if no publisher exists, the daemon runs voice-only instead of failing.
    """
    out = console or Console()
    if not enable_vision and not enable_audio:
        raise ValueError("At least one of vision or audio must be enabled.")

    layout = get_display_layout()
    gaze_source = None
    if enable_vision:
        try:
            gaze_source = SharedMemoryGazeSource()
        except FileNotFoundError:
            out.print(
                "[yellow]No gaze model is publishing frames yet; "
                "running voice-only until one starts.[/yellow]"
            )
            enable_vision = False

    pointer: PointerBackend = (
        DryRunPointerBackend(layout.total_width, layout.total_height)
        if dry_run
        else create_pointer_backend(
            layout.total_width, layout.total_height,
            action_backend=config.action_backend,
        )
    )
    gaze_move = False
    if enable_vision:
        # Eye motion goes to cua-driver's independent agent cursor. The same
        # sink delegates clicks/typing to the action pointer, so the router and
        # the gaze loop agree on where the eye cursor is without warping the
        # system mouse.
        from keyless.platform.cua.agent_cursor import create_gaze_sink

        pointer, gaze_overlay, gaze_move = create_gaze_sink(config, pointer, dry_run)
    else:
        gaze_overlay = None
    pointer_mode = None
    if gaze_overlay is not None and current_host() == "macos":
        from keyless.platform.macos.pointer_mode import MacOSPointerVisibility

        pointer_mode = MacOSPointerVisibility(gaze_overlay)
        pointer_mode.start()

    latest_gaze_lock = threading.Lock()
    latest_gaze: tuple[float, float] | None = None
    latest_vlm_state: tuple[float, float, np.ndarray | None] | None = None
    latest_attention: tuple[bool, float] = (False, 0.0)
    firefox_bridge: FirefoxDOMBridge | None = None

    def _on_gaze_frame(frame: GazeFrame) -> None:
        nonlocal latest_gaze, latest_vlm_state, latest_attention
        with latest_gaze_lock:
            latest_gaze = (frame.x, frame.y)
            latest_vlm_state = (frame.x, frame.y, frame.feat)
            latest_attention = (bool(frame.flags & FLAG_FACE_DETECTED), 0.0)
        if firefox_bridge is not None and firefox_bridge.is_connected:
            mon = monitor_at_point(layout, frame.x, frame.y)
            firefox_bridge.broadcast_gaze(
                frame.x,
                frame.y,
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
            face_visible, _ = latest_attention
        return face_visible

    gaze_reader = None
    audio_svc: UnifiedAudioService | None = None
    ptt_mon = None  # EvdevHotkeyMonitor on Linux; None on macOS (SwiftUI host owns PTT)
    active_listener: ActiveVoiceListener | None = None
    speaker = None
    stt = None
    event_stop = threading.Event()
    event_thread: threading.Thread | None = None
    vlm_clicker: GazeVLMClicker | None = None
    vlm_provider = None
    session_recorder = None
    click_observer = None
    browser_runtime: BrowserRuntime | None = None

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
        if config.firefox_bridge_enabled:
            firefox_bridge = FirefoxDOMBridge(port=config.firefox_bridge_port)
            firefox_bridge.start()
            out.print(
                f"[green]Firefox DOM bridge listening on ws://127.0.0.1:{firefox_bridge.bound_port}[/green]"
            )

        if config.gaze_collect_enabled:
            from keyless.gaze.collect import SessionCollector
            from keyless.gaze.recorder import (
                LinuxClickObserver,
                SessionRecorder,
                build_live_generator,
                build_live_screen_provider,
            )

            collector = SessionCollector(
                current_paths(),
                redact_text=config.gaze_collect_redact_text,
            )
            session_recorder = SessionRecorder(
                collector,
                build_live_generator(dom_bridge=firefox_bridge, use_ocr=False),
                screen_provider=build_live_screen_provider(capture_pixels=False),
            )
            out.print(
                f"[cyan]Gaze v2 session recording enabled -> {session_recorder.session_dir}[/cyan]"
            )
            if current_host() == "linux":
                click_observer = LinuxClickObserver(
                    lambda x, y: session_recorder.record_click(
                        x, y, gaze_pixel=_inproc_gaze()
                    )
                )
                if not click_observer.start():
                    out.print(
                        f"[yellow]Click observer unavailable ({click_observer.unavailable_reason}); "
                        "recording voice/gaze events only.[/yellow]"
                    )
                    click_observer = None

        if enable_audio:
            gaze_provider: Callable[[], tuple[float, float] | None] | None = (
                _inproc_gaze if enable_vision else None
            )

            vlm_provider = None
            browser_vision_provider = None
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
                    browser_vision_provider = vlm_role.provider
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

            def _speak_browser(msg: str) -> None:
                if speaker is not None and config.tts_speak_replies:
                    speaker.speak(msg, interrupt=True)

            if config.playwright_browser_enabled:
                browser_runtime = create_browser_runtime(
                    config,
                    text_provider=text_role.provider,
                    vision_provider=browser_vision_provider,
                    speak=_speak_browser,
                )

            router = VoiceCommandRouter(
                pointer=pointer,
                llm_agent=llm,
                gaze_reader=gaze_reader,
                gaze_provider=gaze_provider,
                dictate_unprefixed=dictate_unprefixed,
                gaze_vlm_clicker=vlm_clicker,
                gaze_state_provider=_inproc_vlm_state if enable_vision else None,
                on_before_click=None,
                firefox_bridge=firefox_bridge,
                browser_runtime=browser_runtime,
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
                payload = res.payload or {}
                if payload.get("spoken") or res.route == "assistant":
                    speaker.speak(res.detail, interrupt=True)
                elif res.route == "terminal" and payload.get("command"):
                    speaker.speak("Typed shell command.", interrupt=True)

            if config.always_on_voice:
                transcript_path = current_paths().transcript_path

                def _publish_transcript(text: str) -> None:
                    write_transcript(transcript_path, text)

                def _publish_partial(text: str) -> None:
                    write_transcript(transcript_path, text, final=False)

                level_path = level_path_for(transcript_path)

                def _publish_level(rms: float) -> None:
                    write_level(level_path, rms)

                active_listener = ActiveVoiceListener(
                    on_speech_onset=_prewarm_gaze_vlm,
                    is_user_attending=_is_user_attending if enable_vision else None,
                    enable_pop_click=config.enable_pop_click,
                    require_command_prefix=(config.require_command_prefix and not dictate_unprefixed),
                    audio_device=config.audio_device,
                    stt_engine=stt,
                    on_transcript=_publish_transcript,
                    on_partial=_publish_partial,
                    on_level=_publish_level,
                    captions_enabled=_CaptionsGate(current_paths().settings_path),
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
                                if session_recorder is not None:
                                    session_recorder.record_voice(ev.transcript, accepted=True)
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
                transcript_path = current_paths().transcript_path

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
                    write_transcript(transcript_path, transcript)
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
            assert gaze_source is not None
            if getattr(pointer, "is_agent_cursor_sink", False):
                gaze_mode = "eye cursor on cua-driver's independent overlay"
            elif config.gaze_cursor_control and not gaze_move:
                gaze_mode = "internal gaze guide (agent cursor unavailable; mouse untouched)"
            elif config.gaze_cursor_control:
                gaze_mode = "eye-mouse cursor control"
            else:
                gaze_mode = "internal gaze guide (physical mouse untouched)"
            out.print(
                f"Attached to gaze frames ({gaze_mode}, dry_run={dry_run}). "
                "Press Ctrl+C to stop."
            )
            run_gaze_loop(
                gaze_source,
                pointer,
                on_frame=_on_gaze_frame,
                duration_sec=duration_sec,
                move_enabled=gaze_move,
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
            vlm_clicker.shutdown()
        if click_observer is not None:
            click_observer.stop()
        if session_recorder is not None:
            session_recorder.close()
        if browser_runtime is not None:
            browser_runtime.shutdown()
        if firefox_bridge is not None:
            firefox_bridge.stop()
        if gaze_source is not None:
            gaze_source.close()
        pointer.close()
