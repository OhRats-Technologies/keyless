"""Live session recorder for the Gaze v2 data flywheel (GREENFIELD.md §7).

Wires real interaction events (clicks, voice commands, optional gaze frames)
together with the unified :class:`~keyless.gaze.candidate_generator.CandidateGenerator`
and writes schema-v0 events through :class:`~keyless.gaze.collect.SessionCollector`.

Hard invariants (AGENTS.md):
- Never moves the real pointer or blocks voice-only startup.
- No model, no calibration, no legacy bias.
- Unsupported OS perception or click hooks degrade honestly without crashing.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from keyless.gaze.candidate_generator import CandidateGenerator
from keyless.gaze.candidates import Candidate
from keyless.gaze.collect import SessionCollector
from keyless.gaze.metrics import bbox_contains
from keyless.gaze.schema import make_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenSnapshot:
    """One captured screen context for candidate generation and logging."""

    display_id: int = 0
    app: str = "unknown"
    window_id: int | None = None
    image_bgr: Any = None
    png_bytes: bytes | None = None
    screenshot_hash: str | None = None


def _encode_png(image_bgr: Any) -> bytes | None:
    if image_bgr is None:
        return None
    try:
        import cv2

        ok, buf = cv2.imencode(".png", image_bgr)
        if not ok:
            return None
        return bytes(buf.tobytes())
    except Exception:
        return None


def _snapshot_digest(snapshot: ScreenSnapshot, png_bytes: bytes | None) -> str:
    if snapshot.screenshot_hash:
        return snapshot.screenshot_hash
    if png_bytes is not None:
        return hashlib.sha256(png_bytes).hexdigest()
    if snapshot.image_bgr is not None and hasattr(snapshot.image_bgr, "tobytes"):
        return hashlib.sha256(snapshot.image_bgr.tobytes()).hexdigest()
    key = f"{snapshot.display_id}:{snapshot.app}:{snapshot.window_id}:{time.time_ns()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _match_clicked_candidate(
    candidates: list[Candidate],
    x: float,
    y: float,
) -> str | None:
    """Return the most specific candidate bbox containing ``(x, y)``, or None."""
    hits = [c for c in candidates if bbox_contains(c.bbox, x, y)]
    if not hits:
        return None
    # Prefer smallest non-zero area (most specific control), breaking ties by confidence.
    hits.sort(key=lambda c: (max(1, c.bbox[2] * c.bbox[3]), -c.confidence, c.id))
    return hits[0].id


def _candidate_to_schema_dict(candidate: Candidate) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": candidate.id,
        "bbox": [int(v) for v in candidate.bbox],
        "role": candidate.role,
        "text": candidate.text,
        "score": float(candidate.confidence),
        "source": candidate.source,
    }
    if candidate.visual_hash is not None:
        data["visual"] = candidate.visual_hash
    return data


class SessionRecorder:
    """Coordinates live screen candidate generation and schema-v0 recording."""

    def __init__(
        self,
        collector: SessionCollector,
        generator: CandidateGenerator,
        *,
        screen_provider: Callable[[], ScreenSnapshot | None] | None = None,
        frame_provider: Callable[[], Mapping[str, Any] | None] | None = None,
        save_screenshots: bool = False,
    ) -> None:
        self._collector = collector
        self._generator = generator
        self._screen_provider = screen_provider
        self._frame_provider = frame_provider
        self._save_screenshots = bool(save_screenshots)
        self._lock = threading.Lock()
        self._clicks = 0
        self._voice = 0
        self._frames = 0
        self._last_sources: dict[str, bool] = {}

    @property
    def session_dir(self):
        return self._collector.session_dir

    @property
    def session_id(self) -> str:
        return self._collector.session_id

    def record_click(
        self,
        x: float,
        y: float,
        *,
        kind: str = "click",
        accepted: bool | None = True,
        latency_ms: float | None = None,
        snapshot: ScreenSnapshot | None = None,
        gaze_pixel: tuple[float, float] | None = None,
    ) -> bool:
        """Capture candidates for the current screen and record a click event."""
        snap = snapshot
        if snap is None and self._screen_provider is not None:
            try:
                snap = self._screen_provider()
            except Exception:
                logger.debug("screen_provider failed during click recording", exc_info=True)
                snap = None
        if snap is None:
            snap = ScreenSnapshot()

        png_bytes = snap.png_bytes
        if png_bytes is None and self._save_screenshots and snap.image_bgr is not None:
            png_bytes = _encode_png(snap.image_bgr)

        screenshot_digest: str | None = None
        if self._save_screenshots and png_bytes is not None:
            screenshot_digest = self._collector.put_blob(png_bytes, "png")
        cache_digest = screenshot_digest or _snapshot_digest(snap, png_bytes)

        gen = self._generator.generate(
            snap.display_id,
            snap.window_id,
            cache_digest,
            gaze=(float(x), float(y)),
            image=snap.image_bgr,
            include_null=False,
        )

        target_id = _match_clicked_candidate(gen.candidates, x, y)
        screen_section: dict[str, Any] = {
            "display_id": int(snap.display_id),
            "app": str(snap.app or "unknown"),
            "window_id": snap.window_id,
            "candidates": [_candidate_to_schema_dict(c) for c in gen.candidates],
        }
        if screenshot_digest is not None:
            screen_section["screenshot"] = screenshot_digest

        interaction: dict[str, Any] = {
            "kind": str(kind),
            "mouse": [float(x), float(y)],
            "target": target_id,
            "voice": None,
            "accepted": accepted,
        }
        if latency_ms is not None:
            interaction["latency_ms"] = float(latency_ms)

        prediction: dict[str, Any] | None = None
        if gaze_pixel is not None:
            prediction = {
                "top": target_id,
                "p": 1.0 if target_id is not None else 0.0,
                "pixel": [float(gaze_pixel[0]), float(gaze_pixel[1])],
            }

        frame_section: Mapping[str, Any] | None = None
        if self._frame_provider is not None:
            try:
                frame_section = self._frame_provider()
            except Exception:
                logger.debug("frame_provider failed during click recording", exc_info=True)

        event = make_event(
            self._collector.session_id,
            frame=frame_section,
            screen=screen_section,
            prediction=prediction,
            interaction=interaction,
        )
        ok = self._collector.record(event)
        with self._lock:
            if ok:
                self._clicks += 1
            self._last_sources = dict(gen.sources)
        return ok

    def record_voice(
        self,
        transcript: str,
        *,
        target: str | None = None,
        mouse: tuple[float, float] | None = None,
        accepted: bool | None = None,
        latency_ms: float | None = None,
    ) -> bool:
        """Record a spoken command/confirmation event."""
        interaction: dict[str, Any] = {
            "kind": "voice",
            "mouse": [float(mouse[0]), float(mouse[1])] if mouse is not None else None,
            "target": target,
            "voice": str(transcript),
            "accepted": accepted,
        }
        if latency_ms is not None:
            interaction["latency_ms"] = float(latency_ms)
        event = make_event(self._collector.session_id, interaction=interaction)
        ok = self._collector.record(event)
        if ok:
            with self._lock:
                self._voice += 1
        return ok

    def record_gaze_frame(
        self,
        x: float,
        y: float,
        *,
        confidence: float = 1.0,
        features: list[float] | None = None,
    ) -> bool:
        """Record one gaze observation frame without modifying pointer state."""
        prediction: dict[str, Any] = {
            "p": max(0.0, min(1.0, float(confidence))),
            "pixel": [float(x), float(y)],
        }
        frame_section: dict[str, Any] | None = None
        if features is not None:
            frame_section = {"features": [float(v) for v in features]}
        event = make_event(
            self._collector.session_id,
            frame=frame_section,
            prediction=prediction,
        )
        ok = self._collector.record(event)
        if ok:
            with self._lock:
                self._frames += 1
        return ok

    def stats(self) -> dict[str, Any]:
        cstats = self._collector.stats()
        with self._lock:
            return {
                **cstats,
                "clicks": self._clicks,
                "voice": self._voice,
                "frames": self._frames,
                "sources": dict(self._last_sources),
            }

    def close(self) -> None:
        self._collector.close()


def build_live_generator(
    *,
    ax_bridge: Any | None = None,
    dom_bridge: Any | None = None,
    use_ocr: bool = True,
    use_visual: bool = False,
) -> CandidateGenerator:
    """Build a CandidateGenerator from whatever perception sources exist on this host."""
    if ax_bridge is None:
        from keyless.platform.router import current_host

        host = current_host()
        if host == "linux":
            try:
                from keyless.platform.linux.accessibility import (
                    LinuxDesktopAccessibilityBridge,
                )

                ax_bridge = LinuxDesktopAccessibilityBridge()
            except Exception:
                ax_bridge = None
        else:
            from keyless.platform.stub import UnsupportedAccessibilityBridge

            ax_bridge = UnsupportedAccessibilityBridge(host)

    ocr_fn = None
    if use_ocr:
        try:
            from rapidocr_onnxruntime import RapidOCR

            engine = RapidOCR()

            def _ocr(image: Any):
                result, _elapsed = engine(image)
                items = []
                for box, text, score in result or []:
                    xs = [pt[0] for pt in box]
                    ys = [pt[1] for pt in box]
                    x1, y1 = int(min(xs)), int(min(ys))
                    items.append(
                        ((x1, y1, int(max(xs)) - x1, int(max(ys)) - y1), text, float(score))
                    )
                return items

            ocr_fn = _ocr
        except Exception:
            ocr_fn = None

    visual_fn = None
    if use_visual:
        try:
            from keyless.vision.ui_detector import GazeUIDetector

            detector = GazeUIDetector()

            def _visual(image: Any):
                return detector._detect_visual_ui_contours(image, 0, 0, [])

            visual_fn = _visual
        except Exception:
            visual_fn = None

    return CandidateGenerator(
        ax_bridge=ax_bridge,
        dom_bridge=dom_bridge,
        ocr_fn=ocr_fn,
        visual_fn=visual_fn,
    )


def _linux_desktop_env() -> dict[str, str]:
    """Populate WAYLAND_DISPLAY / DISPLAY / XAUTHORITY from /run/user/<uid> if unset."""
    import os
    from pathlib import Path

    env = dict(os.environ)
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    runtime_dir = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    if not env.get("WAYLAND_DISPLAY"):
        sockets = [
            s for s in sorted(runtime_dir.glob("wayland-[0-9]*")) if not s.name.endswith(".lock")
        ]
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0].name
    if not env.get("DISPLAY"):
        x11_sockets = sorted(Path("/tmp/.X11-unix").glob("X[0-9]*"))
        if x11_sockets:
            num = x11_sockets[0].name.lstrip("X")
            env["DISPLAY"] = f":{num}"
    if not env.get("XAUTHORITY"):
        xauths = sorted(runtime_dir.glob("xauth_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if xauths:
            env["XAUTHORITY"] = str(xauths[0])
    return env


def _kde_kwin_cursor_xy(env: dict[str, str]) -> tuple[float, float] | None:
    """Query global cursor position on KDE Plasma Wayland via a transient KWin script."""
    import os
    import shutil
    import tempfile
    from pathlib import Path

    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    if qdbus is None:
        return None
    marker = f"KEYLESS_CURSOR_{time.monotonic_ns()}:"
    js = f'print("{marker}" + workspace.cursorPos.x + "," + workspace.cursorPos.y);\n'
    fd, script_path = tempfile.mkstemp(prefix="kk_cursor_", suffix=".js")
    os.close(fd)
    plugin_name = f"kk_cursor_{os.getpid()}"
    try:
        Path(script_path).write_text(js, encoding="utf-8")
        load_res = subprocess.run(
            [
                qdbus,
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting.loadScript",
                script_path,
                plugin_name,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=0.6,
            check=False,
        )
        sid = load_res.stdout.strip()
        if load_res.returncode != 0 or not sid.isdigit():
            return None
        subprocess.run(
            [qdbus, "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
            env=env,
            capture_output=True,
            timeout=0.6,
            check=False,
        )
        subprocess.run(
            [qdbus, "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", plugin_name],
            env=env,
            capture_output=True,
            timeout=0.6,
            check=False,
        )
        journal = subprocess.run(
            ["journalctl", "--user", "-u", "plasma-kwin_wayland.service", "-n", "12", "--no-pager"],
            env=env,
            capture_output=True,
            text=True,
            timeout=0.6,
            check=False,
        )
        for line in reversed(journal.stdout.splitlines()):
            if marker in line:
                coords = line.split(marker, 1)[1].strip()
                if "," in coords:
                    xs, ys = coords.split(",", 1)
                    return float(xs.strip()), float(ys.strip())
    except Exception:
        return None
    finally:
        try:
            Path(script_path).unlink(missing_ok=True)
        except OSError:
            pass
    return None


def build_live_screen_provider(*, capture_pixels: bool = False) -> Callable[[], ScreenSnapshot | None]:
    """Return a best-effort screen/active-window provider for the current host."""
    from keyless.platform.router import current_host, get_display_layout

    host = current_host()
    if host == "linux":
        try:
            from keyless.platform.linux.accessibility import LinuxDesktopAccessibilityBridge
            from keyless.platform.linux.screen import WaylandScreenCapturer

            capturer = WaylandScreenCapturer()
            ax_bridge = LinuxDesktopAccessibilityBridge()
            layout = get_display_layout()

            def _linux_snap() -> ScreenSnapshot:
                ctx = None
                try:
                    ctx = capturer.get_active_window_context(
                        layout.total_width,
                        layout.total_height,
                        desktop_atspi=ax_bridge,
                    )
                except Exception:
                    ctx = None
                img = None
                if capture_pixels:
                    try:
                        img = capturer.capture_full_desktop()
                    except Exception:
                        img = None
                app = ctx.app_id if ctx and ctx.app_id else "unknown"
                win_id = abs(hash((app, ctx.title))) % (2**31) if ctx else None
                return ScreenSnapshot(display_id=0, app=app, window_id=win_id, image_bgr=img)

            return _linux_snap
        except Exception:
            pass
    elif host == "macos":
        try:
            from keyless.platform.macos.screen import macOSScreenSource

            src = macOSScreenSource()

            def _macos_snap() -> ScreenSnapshot:
                img = None
                if capture_pixels:
                    try:
                        img = src.capture_full_desktop()
                    except Exception:
                        img = None
                return ScreenSnapshot(display_id=0, app="unknown", window_id=None, image_bgr=img)

            return _macos_snap
        except Exception:
            pass

    return lambda: ScreenSnapshot(display_id=0, app="unknown", window_id=None)


def _best_effort_cursor_xy() -> tuple[float, float] | None:
    """Read OS pointer position without moving it; None when unavailable."""
    from keyless.platform.router import current_host, pointer_position

    pos = pointer_position()
    if pos is not None:
        return pos
    if current_host() == "linux":
        env = _linux_desktop_env()
        # 1. Hyprland Wayland
        try:
            proc = subprocess.run(
                ["hyprctl", "cursorpos"],
                env=env,
                capture_output=True,
                text=True,
                timeout=0.4,
                check=False,
            )
            if proc.returncode == 0 and "," in proc.stdout:
                parts = [p.strip() for p in proc.stdout.strip().split(",", 1)]
                return float(parts[0]), float(parts[1])
        except Exception:
            pass
        # 2. KDE Plasma Wayland (KWin scripting)
        kwin_pos = _kde_kwin_cursor_xy(env)
        if kwin_pos is not None:
            return kwin_pos
        # 3. X11 / XWayland fallback
        try:
            proc = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                env=env,
                capture_output=True,
                text=True,
                timeout=0.4,
                check=False,
            )
            if proc.returncode == 0:
                vals: dict[str, float] = {}
                for line in proc.stdout.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k in ("X", "Y"):
                            try:
                                vals[k] = float(v)
                            except ValueError:
                                pass
                if "X" in vals and "Y" in vals:
                    return vals["X"], vals["Y"]
        except Exception:
            pass
    return None


class LinuxClickObserver:
    """Non-grabbing evdev left-click listener for live session recording.

    Degrades honestly when ``evdev`` is missing or ``/dev/input/event*`` lacks
    read permissions.  Never injects or moves the pointer.
    """

    def __init__(
        self,
        on_click: Callable[[float, float], None],
        *,
        position_fn: Callable[[], tuple[float, float] | None] | None = None,
    ) -> None:
        self._on_click = on_click
        self._position_fn = position_fn or _best_effort_cursor_xy
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._devices: list[Any] = []
        self.unavailable_reason: str = ""

    def _discover_devices(self) -> list[Any]:
        try:
            import evdev
            from evdev import ecodes
        except ImportError:
            self.unavailable_reason = "evdev package not installed"
            return []

        found: list[Any] = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                keys = caps.get(ecodes.EV_KEY, [])
                if ecodes.BTN_LEFT in keys:
                    found.append(dev)
                else:
                    dev.close()
            except Exception:
                continue
        if not found:
            self.unavailable_reason = (
                "no readable /dev/input/event* pointer devices (check input group permissions)"
            )
        return found

    def start(self) -> bool:
        self._devices = self._discover_devices()
        if not self._devices:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="gaze-click-observer", daemon=True
        )
        self._thread.start()
        return True

    def _loop(self) -> None:
        import select
        from evdev import ecodes

        while not self._stop.is_set():
            try:
                rlist, _, _ = select.select(self._devices, [], [], 0.2)
            except Exception:
                break
            for dev in rlist:
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_LEFT and ev.value == 1:
                            pos = self._position_fn()
                            if pos is not None:
                                try:
                                    self._on_click(pos[0], pos[1])
                                except Exception:
                                    logger.debug("click observer callback failed", exc_info=True)
                except BlockingIOError:
                    continue
                except Exception:
                    continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
        self._devices = []


__all__ = [
    "LinuxClickObserver",
    "ScreenSnapshot",
    "SessionRecorder",
    "build_live_generator",
    "build_live_screen_provider",
]
