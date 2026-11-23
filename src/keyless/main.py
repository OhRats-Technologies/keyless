"""CLI entrypoint for keyless (Phase 1 vision, calibration, and diagnostics)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from keyless.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    discover_v4l_by_id,
    get_display_layout,
)
from keyless.daemon import run_combined_daemon
from keyless.platform.router import resolve_camera_config
from keyless.vision.landmarks import CameraObservation
from keyless.vision.stereo_capture import StereoCapture

app = typer.Typer(
    name="keyless",
    help="Dual-camera stereo gaze tracking, pop-click detection, and voice/AI control.",
    no_args_is_help=True,
)
console = Console()


def _draw_overlay(
    frame: np.ndarray,
    obs: CameraObservation | None,
    label: str,
    fps: float,
    skew_ms: float,
) -> np.ndarray:
    out = frame.copy()
    luma = float(frame.mean())
    cv2.putText(
        out,
        f"{label} | {fps:.1f} fps | luma {luma:.0f} | skew {skew_ms:.1f} ms",
        (16, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.70,
        (80, 255, 120) if obs is not None else (80, 140, 255),
        2,
        cv2.LINE_AA,
    )
    if obs is None:
        cv2.putText(
            out,
            "NO FACE DETECTED",
            (16, 68),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (80, 80, 255),
            2,
            cv2.LINE_AA,
        )
        return out

    for pt in obs.eye_corners_px:
        cv2.circle(out, pt, 3, (255, 220, 0), -1, cv2.LINE_AA)
    cv2.circle(out, obs.iris_a_px, 4, (0, 255, 120), -1, cv2.LINE_AA)
    cv2.circle(out, obs.iris_b_px, 4, (0, 255, 120), -1, cv2.LINE_AA)
    cv2.circle(out, obs.nose_px, 5, (0, 165, 255), -1, cv2.LINE_AA)

    cv2.putText(
        out,
        f"irisA=({obs.iris_a_dx:+.2f},{obs.iris_a_dy:+.2f}) "
        f"irisB=({obs.iris_b_dx:+.2f},{obs.iris_b_dy:+.2f}) "
        f"yaw={obs.yaw:+.2f} blink={obs.max_blink:.2f}",
        (16, 68),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (240, 240, 240),
        1,
        cv2.LINE_AA,
    )
    return out


@app.command("check-system")
def check_system() -> None:
    """Inspect deterministic /dev/v4l/by-id/ cameras, /dev/uinput access, and Mutter layout."""
    cfg = AppConfig.load()
    layout = get_display_layout()
    by_ids = discover_v4l_by_id()

    console.print("[bold]Detected /dev/v4l/by-id/ capture devices:[/bold]")
    for dev in by_ids:
        console.print(f"  • {dev} -> {os.path.realpath(dev)}")
    if not by_ids:
        console.print("  [red]No /dev/v4l/by-id/*-video-index0 devices found.[/red]")

    table = Table(title="Configured Camera Bindings")
    table.add_column("Slot")
    table.add_column("By-ID Path")
    table.add_column("Status")
    for slot, path in [("Camera 1", cfg.cam1_by_id), ("Camera 2", cfg.cam2_by_id)]:
        exists = Path(path).exists()
        status = f"[green]OK ({os.path.realpath(path)})[/green]" if exists else "[red]MISSING[/red]"
        table.add_row(slot, path, status)
    console.print(table)

    mon_table = Table(title=f"GNOME Mutter Layout (Total {layout.total_width}x{layout.total_height})")
    mon_table.add_column("Connector")
    mon_table.add_column("Origin")
    mon_table.add_column("Logical Size")
    mon_table.add_column("Scale")
    mon_table.add_column("Primary")
    for m in layout.monitors:
        mon_table.add_row(
            m.connector,
            f"({m.x}, {m.y})",
            f"{m.width}x{m.height}",
            f"{m.scale:.2f}",
            "yes" if m.is_primary else "no",
        )
    console.print(mon_table)

    uinput_ok = os.access("/dev/uinput", os.W_OK)
    if uinput_ok:
        console.print("[green]/dev/uinput is writable.[/green]")
    else:
        console.print(
            "[yellow]/dev/uinput is not writable by current user. "
            "Run `sudo ./scripts/setup_uinput_udev.sh` before `run-vision`.[/yellow]"
        )


@app.command("test-cams")
def test_cams(
    exp1: int | None = typer.Option(None, help="Camera 1 manual exposure"),
    gain1: int | None = typer.Option(None, help="Camera 1 sensor gain (0-255)"),
    exp2: int | None = typer.Option(None, help="Camera 2 manual exposure"),
    gain2: int | None = typer.Option(None, help="Camera 2 sensor gain (16-255)"),
    headless: bool = typer.Option(False, help="Run without opening a GUI preview window"),
    seconds: float = typer.Option(0.0, help="Auto-stop after N seconds (0 = until 'q'/ESC)"),
) -> None:
    """Verify locked 30Hz dual-camera MJPG capture and MediaPipe iris/head landmarks."""
    cfg = AppConfig.load()
    if exp1 is not None:
        cfg.cam1_exposure = exp1
    if gain1 is not None:
        cfg.cam1_gain = gain1
    if exp2 is not None:
        cfg.cam2_exposure = exp2
    if gain2 is not None:
        cfg.cam2_gain = gain2

    cfg.camera_mode, cfg.cam1_by_id, cfg.cam2_by_id = resolve_camera_config(
        cfg.camera_mode, cfg.cam1_by_id, cfg.cam2_by_id
    )
    try:
        with StereoCapture(
            cam1_by_id=cfg.cam1_by_id,
            cam2_by_id=cfg.cam2_by_id or None,
            mono=cfg.camera_mode == "mono",
            width=cfg.frame_width,
            height=cfg.frame_height,
            fps=cfg.fps,
            cam1_exposure=cfg.cam1_exposure,
            cam1_gain=cfg.cam1_gain,
            cam2_exposure=cfg.cam2_exposure,
            cam2_gain=cfg.cam2_gain,
            extract_landmarks=True,
            use_gpu=cfg.use_gpu,
            keep_images=True,
            auto_exposure=getattr(cfg, "cam_auto_exposure", True),
        ) as stereo:
            # Wait briefly for initial frames so delegates initialize before printing
            stereo.read_pair(timeout_s=2.0)
            for idx, (rep, delg) in enumerate(zip(stereo.reports, stereo.delegates), start=1):
                if stereo.mono and idx == 2:
                    continue
                console.print(
                    f"[cyan]Cam{idx}[/cyan] [{delg}] {rep.resolved_dev} ({rep.fourcc} {rep.width}x{rep.height} "
                    f"@ {rep.fps:.1f}fps) | locked={rep.applied} | unsupported={rep.unsupported}"
                )
            console.print(f"[cyan]Camera mode:[/cyan] {cfg.camera_mode}")

            start_t = time.monotonic()
            last_seq1 = -1
            last_seq2 = -1
            frames_checked = 0
            faces_cam1 = 0
            faces_cam2 = 0
            last_fps1 = 0.0
            last_fps2 = 0.0
            last_luma1 = 0.0
            last_luma2 = 0.0

            while True:
                if seconds > 0 and (time.monotonic() - start_t) >= seconds:
                    break
                pair = stereo.read_pair(timeout_s=1.0)
                if pair is None or pair.cam1.image is None or pair.cam2.image is None:
                    continue
                if pair.cam1.seq == last_seq1 and pair.cam2.seq == last_seq2:
                    time.sleep(0.004)
                    continue
                last_seq1 = pair.cam1.seq
                last_seq2 = pair.cam2.seq
                frames_checked += 1
                last_fps1 = pair.cam1.measured_fps
                last_fps2 = pair.cam2.measured_fps
                last_luma1 = float(pair.cam1.image.mean())
                last_luma2 = float(pair.cam2.image.mean())

                obs1 = pair.cam1.observation
                obs2 = pair.cam2.observation
                if obs1 is not None:
                    faces_cam1 += 1
                if obs2 is not None:
                    faces_cam2 += 1

                if not headless:
                    v1 = _draw_overlay(
                        pair.cam1.image, obs1, "Camera 1", pair.cam1.measured_fps, pair.skew_ms
                    )
                    v2 = _draw_overlay(
                        pair.cam2.image, obs2, "Camera 2", pair.cam2.measured_fps, pair.skew_ms
                    )
                    v1_s = cv2.resize(v1, (800, 450))
                    v2_s = cv2.resize(v2, (800, 450))
                    combined = np.hstack([v1_s, v2_s])
                    cv2.imshow("Keyless Dual-Cam Diagnostic (q/ESC to quit)", combined)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q")):
                        break

            if cfg.camera_mode == "mono":
                console.print(
                    f"Checked {frames_checked} frames in mono mode | "
                    f"fps={last_fps1:.2f} | luma={last_luma1:.1f} | "
                    f"faces={faces_cam1}/{frames_checked}"
                )
            else:
                console.print(
                    f"Checked {frames_checked} paired frames | "
                    f"measured fps: camera1={last_fps1:.2f}, camera2={last_fps2:.2f} | "
                    f"luma: camera1={last_luma1:.1f}, camera2={last_luma2:.1f} | "
                    f"faces: camera1={faces_cam1}/{frames_checked}, camera2={faces_cam2}/{frames_checked}"
                )
    finally:
        if not headless:
            cv2.destroyAllWindows()


def _apply_runtime_overrides(
    cfg: AppConfig,
    camera_backend: str | None = None,
    exp1: int | None = None,
    gain1: int | None = None,
    exp2: int | None = None,
    gain2: int | None = None,
    hotkey: str | None = None,
    stt_provider: str | None = None,
    llm_backend: str | None = None,
    llm_provider: str | None = None,
    llm_preset: str | None = None,
    llm_model: str | None = None,
    acp_agent: str | None = None,
    vlm_model: str | None = None,
    vlm_provider: str | None = None,
    action_backend: str | None = None,
    vlm_remote_consent: bool | None = None,
    no_vlm: bool = False,
    ptt: bool = False,
    pop_click: bool | None = None,
    eye_mouse: bool | None = None,
    firefox_port: int | None = None,
    no_firefox_bridge: bool = False,
    pop_peak: float | None = None,
    pop_rms: float | None = None,
) -> AppConfig:
    if camera_backend in ("v4l2", "pipewire"):
        cfg.camera_backend = camera_backend
    if exp1 is not None:
        cfg.cam1_exposure = exp1
    if gain1 is not None:
        cfg.cam1_gain = gain1
    if exp2 is not None:
        cfg.cam2_exposure = exp2
    if gain2 is not None:
        cfg.cam2_gain = gain2
    if hotkey is not None:
        cfg.ptt_hotkey = hotkey
    if stt_provider is not None:
        cfg.stt_provider = stt_provider
    if llm_backend is not None:
        cfg.llm_backend = llm_backend
    if llm_provider is not None:
        cfg.llm_provider = llm_provider
    if llm_preset is not None:
        cfg.llm_preset = llm_preset
    if llm_model is not None:
        cfg.llm_model = llm_model
    if acp_agent is not None:
        cfg.acp_agent_id = acp_agent
    if vlm_model is not None:
        cfg.gaze_vlm_model = vlm_model
    if action_backend in ("native", "cua"):
        cfg.action_backend = action_backend
    if vlm_provider is not None:
        cfg.gaze_vlm_provider = vlm_provider
    if vlm_remote_consent is not None:
        cfg.gaze_vlm_remote_consent = vlm_remote_consent
    if no_vlm:
        cfg.gaze_vlm_enabled = False
    if ptt:
        cfg.always_on_voice = False
    if pop_click is not None:
        cfg.enable_pop_click = pop_click
    if eye_mouse is not None:
        cfg.gaze_cursor_control = eye_mouse
    if firefox_port is not None:
        cfg.firefox_bridge_port = firefox_port
    if no_firefox_bridge:
        cfg.firefox_bridge_enabled = False
    if pop_peak is not None:
        cfg.pop_min_peak = pop_peak
    if pop_rms is not None:
        cfg.pop_min_rms = pop_rms
    return cfg


@app.command("run")
def run_all(
    exp1: int | None = typer.Option(None, help="Cam1 manual exposure"),
    gain1: int | None = typer.Option(None, help="Cam1 sensor gain"),
    exp2: int | None = typer.Option(None, help="Cam2 manual exposure"),
    gain2: int | None = typer.Option(None, help="Cam2 sensor gain"),
    hotkey: str | None = typer.Option(None, help="Evdev PTT hotkey (default KEY_RIGHTCTRL)"),
    stt_provider: str | None = typer.Option(None, help="STT execution provider: cpu | cuda"),
    llm_backend: str | None = typer.Option(None, help="LLM backend: auto | ollama | gemini | acp"),
    llm_provider: str | None = typer.Option(None, help="Provider: auto | ollama | gemini | openai_compat | acp"),
    llm_preset: str | None = typer.Option(None, help="OpenAI-compatible preset (openai, openrouter, groq, ...)"),
    llm_model: str | None = typer.Option(None, help="Text model name for the selected provider"),
    acp_agent: str | None = typer.Option(None, help="ACP agent id (default opencode)"),
    vlm_model: str | None = typer.Option(None, help="Vision grounding model (default gemma4:e4b)"),
    vlm_provider: str | None = typer.Option(None, help="Gaze VLM provider: auto | ollama | openai_compat"),
    vlm_remote_consent: bool = typer.Option(
        False, help="Allow screenshots to leave this device for the gaze VLM"
    ),
    no_vlm: bool = typer.Option(False, help="Disable gaze-foveated OCR/VLM voice clicking"),
    ptt: bool = typer.Option(False, help="Use Push-to-Talk hotkey instead of always-on VAD"),
    pop_click: bool | None = typer.Option(
        None,
        "--pop-click/--no-pop-click",
        help="Enable/disable 75ms speech-vetoed acoustic pop click",
    ),
    eye_mouse: bool | None = typer.Option(
        None,
        "--eye-mouse/--no-eye-mouse",
        help="Let gaze drive OS cursor (default: internal gaze guide only)",
    ),
    camera_backend: str | None = typer.Option(
        None,
        help="Camera capture backend: v4l2 | pipewire (mono sharing)",
    ),
    action_backend: str | None = typer.Option(
        None,
        help="Discrete input actions: native | cua (pinned cua-driver binary)",
    ),
    firefox_port: int | None = typer.Option(None, help="Firefox DOM WebSocket bridge port (default 8765)"),
    no_firefox_bridge: bool = typer.Option(False, help="Disable Tier-0 Firefox WebSocket DOM bridge"),
    pop_peak: float | None = typer.Option(None, help="Pop detector peak threshold"),
    pop_rms: float | None = typer.Option(None, help="Pop detector RMS threshold"),
    dictate_unprefixed: bool = typer.Option(False, help="Type unprefixed speech directly"),
    no_vision: bool = typer.Option(False, help="Disable dual-camera gaze tracking"),
    no_audio: bool = typer.Option(False, help="Disable pop-click & voice STT"),
    dry_run: bool = typer.Option(False, help="Run without emitting /dev/uinput events"),
    seconds: float | None = typer.Option(None, help="Optional run duration in seconds"),
) -> None:
    """Run the combined gaze-overlay + always-on VAD/VLM voice daemon."""
    cfg = _apply_runtime_overrides(
        AppConfig.load(),
        camera_backend=camera_backend,
        action_backend=action_backend,
        exp1=exp1,
        gain1=gain1,
        exp2=exp2,
        gain2=gain2,
        hotkey=hotkey,
        stt_provider=stt_provider,
        llm_backend=llm_backend,
        llm_provider=llm_provider,
        llm_preset=llm_preset,
        llm_model=llm_model,
        acp_agent=acp_agent,
        vlm_model=vlm_model,
        vlm_provider=vlm_provider,
        vlm_remote_consent=vlm_remote_consent,
        no_vlm=no_vlm,
        ptt=ptt,
        pop_click=pop_click,
        eye_mouse=eye_mouse,
        firefox_port=firefox_port,
        no_firefox_bridge=no_firefox_bridge,
        pop_peak=pop_peak,
        pop_rms=pop_rms,
    )
    run_combined_daemon(
        config=cfg,
        enable_vision=not no_vision,
        enable_audio=not no_audio,
        dictate_unprefixed=dictate_unprefixed,
        dry_run=dry_run,
        duration_sec=seconds,
        console=console,
    )


@app.command("test-audio")
def test_audio(
    seconds: float = typer.Option(5.0, help="Seconds to monitor live mic for pops & plosive rejection"),
    min_peak: float | None = typer.Option(None, help="Minimum high-pass peak threshold (default 0.25)"),
    min_rms: float | None = typer.Option(None, help="Minimum chunk RMS threshold (default 0.032)"),
    transcribe: bool = typer.Option(False, help="Also test STT transcription on captured audio"),
) -> None:
    """Test the mic stream, plosive-gated lip-pop detector, and optional STT."""
    import numpy as np

    from keyless.audio.pop_detector import PopEvalResult, PopTriggerStateMachine
    from keyless.audio.stt_engine import UnifiedAudioService
    from keyless.voice.stt.factory import create_stt_engine

    cfg = AppConfig.load()
    pop = PopTriggerStateMachine(
        min_peak=min_peak if min_peak is not None else cfg.pop_min_peak,
        min_rms=min_rms if min_rms is not None else cfg.pop_min_rms,
    )
    recorded: list[np.ndarray] = []
    max_stats = {"peak": 0.0, "rms": 0.0}

    def _on_pop() -> None:
        console.print("[bold green]POP! -> click triggered[/bold green]")

    def _on_eval(res: PopEvalResult) -> None:
        if res.peak > max_stats["peak"]:
            max_stats["peak"] = res.peak
        if res.rms > max_stats["rms"]:
            max_stats["rms"] = res.rms
        if res.rejected_plosive:
            console.print(f"[yellow]Rejected plosive/speech[/yellow] (rms={res.rms:.3f})")

    svc = UnifiedAudioService(
        pop_detector=pop,
        on_pop=_on_pop,
        on_eval=_on_eval,
        device=cfg.audio_device,
    )
    if transcribe:
        svc.begin_ptt()
        svc.pop_detector.set_muted(False)

    console.print(
        f"Listening on mic for {seconds:.1f}s (min_peak={pop.min_peak:.2f}, min_rms={pop.min_rms:.3f})..."
    )
    svc.start()
    try:
        t0 = time.monotonic()
        while (time.monotonic() - t0) < seconds:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        if transcribe:
            recorded = list(svc._ptt_chunks)
        svc.stop()

    console.print(
        f"Pop triggers: [green]{pop.total_triggers}[/green] | "
        f"Rejected plosives: [yellow]{pop.total_rejected_plosives}[/yellow] | "
        f"Max peak={max_stats['peak']:.3f}, Max rms={max_stats['rms']:.3f}, Floor={pop._noise_floor:.4f}"
    )
    if transcribe and recorded:
        try:
            stt = create_stt_engine(cfg).engine
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        started = time.perf_counter()
        text = stt.transcribe(np.concatenate(recorded))
        dt_ms = (time.perf_counter() - started) * 1000.0
        info = stt.status()
        console.print(
            f"STT {info.name} ({info.device}/{info.compute_type}, {dt_ms:.0f}ms): "
            f"[cyan]{text!r}[/cyan]"
        )
        stt.close()


@app.command("run-voice")
def run_voice(
    hotkey: str | None = typer.Option(None, help="Evdev PTT hotkey (default KEY_RIGHTCTRL)"),
    stt_provider: str | None = typer.Option(None, help="STT execution provider: cpu | cuda"),
    llm_backend: str | None = typer.Option(None, help="LLM backend: auto | ollama | gemini | acp"),
    llm_provider: str | None = typer.Option(None, help="Provider: auto | ollama | gemini | openai_compat | acp"),
    llm_preset: str | None = typer.Option(None, help="OpenAI-compatible preset (openai, openrouter, groq, ...)"),
    llm_model: str | None = typer.Option(None, help="Text model name for the selected provider"),
    acp_agent: str | None = typer.Option(None, help="ACP agent id (default opencode)"),
    pop_peak: float | None = typer.Option(None, help="Pop detector peak threshold (default 0.25)"),
    pop_rms: float | None = typer.Option(None, help="Pop detector RMS threshold (default 0.032)"),
    dictate_unprefixed: bool = typer.Option(False, help="Type unprefixed speech directly"),
    dry_run: bool = typer.Option(False, help="Log actions without emitting /dev/uinput events"),
    seconds: float | None = typer.Option(None, help="Optional run duration in seconds"),
) -> None:
    """Run only the pop-click + evdev PTT voice router."""
    cfg = _apply_runtime_overrides(
        AppConfig.load(),
        hotkey=hotkey,
        stt_provider=stt_provider,
        llm_backend=llm_backend,
        llm_provider=llm_provider,
        llm_preset=llm_preset,
        llm_model=llm_model,
        acp_agent=acp_agent,
        pop_peak=pop_peak,
        pop_rms=pop_rms,
    )
    run_combined_daemon(
        config=cfg,
        enable_vision=False,
        enable_audio=True,
        dictate_unprefixed=dictate_unprefixed,
        dry_run=dry_run,
        duration_sec=seconds,
        console=console,
    )


@app.command("ui")
def ui(
    theme: str = typer.Option("system", help="Theme: system | light | dark"),
    no_previews: bool = typer.Option(False, help="Disable live camera previews (headless/screenshots)"),
) -> None:
    """Open the main window, tray, setup, settings, history, and diagnostics."""
    from keyless.ui.app import run_ui

    raise typer.Exit(
        run_ui(theme=None if theme == "system" else theme, enable_previews=not no_previews)
    )


@app.command("serve")
def serve(
    port: int = typer.Option(8766, help="Loopback port for the SwiftUI/Qt control plane"),
    host: str = typer.Option("127.0.0.1", help="Bind address (loopback only)"),
) -> None:
    """Run the loopback control plane for native UI hosts (SwiftUI MenuBarExtra)."""
    from keyless.ipc.control import ControlServer
    from keyless.ui.contract import build_ui_state

    server = ControlServer()
    server.ui_contract_builder = build_ui_state
    bound = server.start(host=host, port=port)
    console.print(
        f"[green]Keyless control plane on http://127.0.0.1:{bound}[/green] "
        "(GET /v1/status, /v1/doctor, /v1/history; POST /v1/start, /v1/pause)"
    )
    try:
        import time

        while True:
            time.sleep(3600.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


@app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON document for support/UI"),
    no_provider_check: bool = typer.Option(
        False, help="Skip provider network/process probes (keeps STT and capability checks)"
    ),
) -> None:
    """Report paths, cameras, audio, permissions, STT, providers, and capabilities."""
    from keyless.diagnostics.doctor import collect_doctor, format_text

    report = collect_doctor(check_providers=not no_provider_check)
    if json_output:
        typer.echo(report.to_json())
    else:
        console.print(format_text(report))


providers_app = typer.Typer(
    name="providers",
    help="Bring-your-own AI providers: OpenAI-compatible, Ollama, and ACP agents.",
    no_args_is_help=True,
)
app.add_typer(providers_app, name="providers")


@providers_app.command("presets")
def providers_presets() -> None:
    """List OpenAI-compatible endpoints and their key variables."""
    from keyless.models.providers.presets import load_presets

    table = Table(title="OpenAI-compatible presets")
    table.add_column("Id")
    table.add_column("Name")
    table.add_column("Base URL")
    table.add_column("Key")
    table.add_column("Local")
    for preset in load_presets():
        table.add_row(
            preset.id,
            preset.name,
            preset.base_url,
            preset.api_key_env or "none",
            "yes" if preset.local else "no",
        )
    console.print(table)


@providers_app.command("acp")
def providers_acp() -> None:
    """List ACP agents from the registry (or the offline fallback list)."""
    from keyless.models.providers.acp_registry import fetch_registry

    specs = fetch_registry()
    table = Table(title="ACP agents")
    table.add_column("Id")
    table.add_column("Name")
    table.add_column("Command")
    table.add_column("Source")
    table.add_column("Installed")
    for spec in specs:
        table.add_row(
            spec.id,
            spec.name,
            " ".join(spec.command) or "(no launch command)",
            spec.source,
            "yes" if spec.installed else "no",
        )
    console.print(table)


@providers_app.command("status")
def providers_status() -> None:
    """Show the configured text and gaze-VLM provider status."""
    from keyless.models.providers.roles import resolve_gaze_vlm_role, resolve_text_role

    cfg = AppConfig.load()
    for label, resolution in (
        ("text", resolve_text_role(cfg)),
        ("gaze VLM", resolve_gaze_vlm_role(cfg)),
    ):
        status = resolution.provider.status()
        console.print(
            f"[bold]{label}[/bold]: {resolution.provider.display_name} "
            f"({resolution.provider.model or 'model not set'}) -> [bold]{status.state.value}[/bold]"
        )
        if status.detail:
            console.print(f"  {status.detail}")
        for reason in resolution.reasons:
            console.print(f"  [yellow]{reason}[/yellow]")


@providers_app.command("set-key")
def providers_set_key(
    provider_id: str = typer.Argument(..., help="Preset id, e.g. openai or gemini"),
) -> None:
    """Store an API key in the OS keychain (never in settings.json)."""
    from keyless.models.providers.secrets import keyring_available, set_secret

    if not keyring_available():
        console.print("[red]The OS keychain is unavailable. Install the keyring backend.[/red]")
        raise typer.Exit(code=1)
    key = typer.prompt("API key", hide_input=True)
    set_secret(provider_id, key.strip())
    console.print(f"[green]Stored {provider_id} key in the OS keychain.[/green]")


@providers_app.command("delete-key")
def providers_delete_key(provider_id: str = typer.Argument(...)) -> None:
    """Remove an API key from the OS keychain."""
    from keyless.models.providers.secrets import delete_secret

    delete_secret(provider_id)
    console.print(f"Removed {provider_id} from the OS keychain (if present).")


models_app = typer.Typer(
    name="models",
    help="Manage app-owned model assets (pinned, resumable, SHA-256 verified).",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


def _model_asset(which: str):
    from keyless.models.catalog import get_asset, stt_assets, tts_assets

    if which in ("stt", "default"):
        assets = stt_assets()
        if not assets:
            raise typer.BadParameter("No STT assets are in the catalog.")
        return assets[0]
    if which in ("tts", "woof", "underdog"):
        assets = tts_assets()
        if not assets:
            raise typer.BadParameter("No TTS assets are in the catalog.")
        return assets[0]
    try:
        return get_asset(which)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("commands")
def commands_cmd(
    search: str = typer.Option("", "--search", "-s", help="Filter voice commands by keyword"),
) -> None:
    """List all built-in voice commands grouped by category."""
    from keyless.ui.commands_help import filter_command_categories

    categories = filter_command_categories(search)
    if not categories:
        console.print(f"[yellow]No voice commands matching {search!r}.[/yellow]")
        return
    for cat in categories:
        table = Table(title=f"{cat.title} — {cat.summary}")
        table.add_column("Say", style="bold cyan", width=38)
        table.add_column("What it does")
        table.add_column("Examples", style="dim")
        for cmd in cat.commands:
            table.add_row(cmd.phrase, cmd.description, cmd.example)
        console.print(table)


@app.command("update")
def update_cmd(
    check: bool = typer.Option(False, "--check", help="Only check for a newer release"),
    prerelease: bool = typer.Option(False, "--pre", help="Include prerelease builds"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply an available update without prompting"),
) -> None:
    """Check for Keyless updates and install new releases (source, AppImage, or .deb)."""
    from keyless.updater import Updater

    cfg = AppConfig.load()
    channel = "prerelease" if prerelease else getattr(cfg, "update_channel", "stable")
    updater = Updater(channel=channel)  # type: ignore[arg-type]
    state = updater.check_for_updates(channel=channel)  # type: ignore[arg-type]
    if state.status == "error":
        console.print(f"[red]{state.message}[/red]")
        raise typer.Exit(code=1)
    if state.status != "available" or not state.available_version:
        console.print(f"[green]{state.message}[/green] (install mode: {state.install_mode})")
        return

    console.print(
        f"[bold green]Keyless {state.available_version} is available[/bold green] "
        f"(current: {state.current_version}, mode: {state.install_mode})"
    )
    for note in state.release_notes[:5]:
        console.print(f"  • {note}")
    if check:
        return
    if not yes and not typer.confirm(f"Install Keyless {state.available_version} now?", default=True):
        return
    ok, msg = updater.apply_update()
    if not ok:
        console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]{msg}[/green]")


@models_app.command("status")
def models_status() -> None:
    """Show pinned assets, local state, and whether a receipt verifies."""
    from keyless.models import runtimes
    from keyless.models.catalog import ASSETS
    from keyless.models.store import ModelStore

    store = ModelStore()
    table = Table(title="App-owned models")
    table.add_column("Asset")
    table.add_column("State")
    table.add_column("Size")
    table.add_column("Path")
    for asset in ASSETS.values():
        status = store.status(asset, verify_hashes=False)
        table.add_row(
            asset.display_name,
            status.state.value,
            f"{asset.total_bytes / (1024 * 1024):.1f} MB",
            str(status.path),
        )
    console.print(table)

    runtime = runtimes.status()
    rtable = Table(title="Pinned runtimes")
    rtable.add_column("Runtime")
    rtable.add_column("State")
    rtable.add_column("Version")
    rtable.add_column("Path")
    rtable.add_row(
        f"cua-driver ({runtime.get('target')})",
        "ready" if runtime.get("ready") else "missing",
        str(runtime.get("version")),
        str(runtime.get("path")),
    )
    console.print(rtable)


@models_app.command("download")
def models_download(
    which: str = typer.Argument("stt", help="stt, cua-driver, or a pinned asset id"),
) -> None:
    """Download and verify a pinned model (or the pinned cua-driver runtime)."""
    if which == "cua-driver":
        from keyless.models import runtimes

        target = runtimes.current_target()
        console.print(
            f"Downloading [bold]cua-driver {runtimes.CUA_DRIVER_VERSION}[/bold] "
            f"({target.asset}, {target.bytes / (1024 * 1024):.1f} MB)..."
        )
        last = {"value": -10.0}

        def _progress(done: int, total: int) -> None:
            percent = (done / total * 100.0) if total else 0.0
            if percent - last["value"] >= 5.0 or percent >= 100.0:
                last["value"] = percent
                console.print(f"  {percent:5.1f}%  ({done / (1024 * 1024):.1f}/{total / (1024 * 1024):.1f} MB)")

        try:
            exe = runtimes.download(progress=_progress)
        except Exception as exc:
            console.print(f"[red]Download failed: {exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]Verified cua-driver at {exe}[/green] (receipt written).")
        return

    from keyless.models.downloader import download_asset

    asset = _model_asset(which)
    console.print(
        f"Downloading [bold]{asset.display_name}[/bold] "
        f"({asset.total_bytes / (1024 * 1024):.1f} MB, revision {asset.revision[:12]})..."
    )
    last_percent = {"value": -10.0}

    def _progress(update) -> None:
        if update.percent - last_percent["value"] >= 5.0 or update.percent >= 100.0:
            last_percent["value"] = update.percent
            console.print(
                f"  {update.percent:5.1f}%  {update.file_name} "
                f"({update.overall_done / (1024 * 1024):.1f}/"
                f"{update.overall_total / (1024 * 1024):.1f} MB)"
            )

    try:
        receipt = download_asset(asset, progress=_progress)
    except Exception as exc:
        console.print(f"[red]Download failed: {exc}[/red]")
        console.print("Partial files are kept; run the command again to resume.")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Verified and promoted {asset.id}[/green] "
        f"({len(receipt.files)} files, receipt written)."
    )


@models_app.command("verify")
def models_verify(which: str = typer.Argument("stt", help="stt or a pinned asset id")) -> None:
    """Re-hash every file and check the receipt."""
    from keyless.models.store import ModelStore

    asset = _model_asset(which)
    status = ModelStore().status(asset, verify_hashes=True)
    console.print(f"{asset.id}: [bold]{status.state.value}[/bold] — {status.detail}")
    if not status.ready:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
