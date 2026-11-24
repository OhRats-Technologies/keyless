"""Backend service layer backing the unified Keyless HTTP web UI.

Manages the live monocular gaze publisher, live session recording, dataset
curation, and host permission probes behind thread-safe methods consumed by
``ControlServer`` routes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from keyless.config.paths import AppPaths
from keyless.config.schema import AppConfig
from keyless.gaze.curate import (
    delete_event,
    delete_session,
    evaluate_recall_summary,
    export_curated_split,
    grant_linux_input_access,
    list_session_summaries,
    probe_lab_environment,
    seed_demo_session,
    update_event,
)
from keyless.gaze.metrics import metric_table
from keyless.gaze.replay import load_session

logger = logging.getLogger(__name__)

MANUAL_INPUT_CMD = (
    'sudo usermod -aG input "$USER" && '
    'sudo setfacl -m "u:$USER:rw" /dev/input/event*'
)


class WebGazeManager:
    """Owns live monocular gaze publishing and session recording for the web UI."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        self._publisher: Any = None
        self._recorder: Any = None
        self._click_observer: Any = None
        self._last_error: str = ""
        # TTL caches to keep SSE streaming strictly zero-I/O
        self._cache_ts: float = 0.0
        self._cached_env: Any = None
        self._cached_cameras: list[dict[str, Any]] = []
        self._cached_layout: Any = None
        self._rig_version: int = 1
        self._llm_options_cache: dict[str, dict[str, Any]] = {}
        # One probe per preset at a time: concurrent option requests wait for
        # the in-flight probe instead of each starting their own agent spawn.
        self._llm_options_locks: dict[str, threading.Lock] = {}
        self._calibration: Any = None

    def _llm_options_lock(self, preset_id: str) -> threading.Lock:
        with self._lock:
            return self._llm_options_locks.setdefault(preset_id, threading.Lock())

    @property
    def sessions_dir(self) -> Path:
        return self.paths.data_dir / "sessions"

    # --- Monocular gaze publisher ---------------------------------------

    def _refresh_hw_cache(self, *, force: bool = False) -> tuple[Any, list[dict[str, Any]], Any]:
        now = time.monotonic()
        if not force and self._cached_env is not None and (now - self._cache_ts) < 5.0:
            return self._cached_env, self._cached_cameras, self._cached_layout
        env = probe_lab_environment()
        cameras: list[dict[str, Any]] = []
        try:
            from keyless.platform.router import list_cameras

            cameras = [{"id": c.id, "name": c.name, "path": c.path} for c in list_cameras()]
        except Exception:
            pass
        layout = None
        try:
            from keyless.platform.router import get_display_layout

            layout = get_display_layout()
        except Exception:
            pass
        self._cached_env = env
        self._cached_cameras = cameras
        self._cached_layout = layout
        self._cache_ts = now
        return env, cameras, layout

    def gaze_status(self, *, force_hw: bool = False) -> dict[str, Any]:
        with self._lock:
            pub = self._publisher
            rec = self._recorder
            last_err = self._last_error

        env, cameras, cached_layout = self._refresh_hw_cache(force=force_hw)
        running = pub is not None
        pred = pub.latest_prediction() if pub is not None else None
        stats = pub.stats() if pub is not None else None
        layout = pub.layout if pub is not None else cached_layout

        monitors = []
        if layout is not None:
            monitors = [
                {
                    "connector": m.connector,
                    "x": m.x,
                    "y": m.y,
                    "width": m.width,
                    "height": m.height,
                    "scale": m.scale,
                    "is_primary": m.is_primary,
                }
                for m in layout.monitors
            ]

        return {
            "running": running,
            "mode": pub.active_mode if pub is not None else ("binocular" if len(cameras) >= 2 else "monocular"),
            "available_cameras": cameras,
            "recording": rec is not None,
            "recording_session_id": rec.session_id if rec is not None else None,
            "recording_stats": rec.stats() if rec is not None else None,
            "display": {
                "width": layout.total_width if layout is not None else 1920,
                "height": layout.total_height if layout is not None else 1080,
                "monitors": monitors,
            },
            "prediction": asdict(pred) if pred is not None else None,
            "stats": stats,
            "rig": self.get_display_rig(),
            "rig_version": self._rig_version + (stats["samples"] if stats else 0),
            "overlay_active": bool(pub is not None and getattr(pub, "overlay_active", False)),
            "environment": asdict(env),
            "manual_input_command": MANUAL_INPUT_CMD,
            "last_error": last_err,
        }

    def gaze_stream_tick(self, last_rig_ver: int = -1) -> tuple[dict[str, Any], int]:
        """Ultra-lightweight zero-I/O frame snapshot for 30 Hz SSE streaming."""
        with self._lock:
            pub = self._publisher
            rec = self._recorder
            rig_v_base = self._rig_version

        if pub is None:
            return {
                "running": False,
                "recording": rec is not None,
                "overlay_active": False,
                "prediction": None,
            }, last_rig_ver

        pred = pub.latest_prediction()
        st = pub.stats()
        rig_ver = rig_v_base + int(st.get("samples", 0))
        compact_stats = {
            "mode": st.get("mode", "monocular"),
            "feature_dim": st.get("feature_dim", 10),
            "frames_read": st.get("frames_read", 0),
            "faces_detected": st.get("faces_detected", 0),
            "clicks_adapted": st.get("clicks_adapted", 0),
            "samples": st.get("samples", 0),
            "min_clicks": st.get("min_clicks", 5),
            "using_ridge": st.get("using_ridge", False),
            "measured_fps": st.get("measured_fps", 0.0),
            "blinks_suppressed": st.get("blinks_suppressed", 0),
            "outliers_suppressed": st.get("outliers_suppressed", 0),
            "clicks_ignored": st.get("clicks_ignored", 0),
            "latency_ms": st.get("latency_ms"),
            "profile": st.get("profile"),
            "calibrating": st.get("calibrating", False),
        }
        payload: dict[str, Any] = {
            "running": True,
            "mode": pub.active_mode,
            "recording": rec is not None,
            "overlay_active": bool(getattr(pub, "overlay_active", False)),
            "prediction": asdict(pred) if pred is not None else None,
            "stats": compact_stats,
        }
        if rig_ver != last_rig_ver:
            payload["rig"] = self.get_display_rig()
            payload["sample_points"] = st.get("sample_points", [])
        return payload, rig_ver

    def start_gaze(
        self,
        *,
        camera_mode: str = "auto",
        min_clicks: int = 5,
        ridge_alpha: float = 0.01,
        adapt_clicks: bool = True,
    ) -> dict[str, Any]:
        from keyless.gaze.monocular import MonocularGazePublisher

        with self._lock:
            if self._publisher is not None:
                return self.gaze_status()

        cfg = AppConfig.load(self.paths.settings_path)
        pub = MonocularGazePublisher(
            cfg,
            camera_mode=camera_mode,
            min_clicks=max(2, int(min_clicks)),
            ridge_alpha=max(1e-6, float(ridge_alpha)),
            enable_click_adaptation=bool(adapt_clicks),
            publish_shm=True,
        )
        try:
            pub.start()
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            raise RuntimeError(f"Could not start monocular gaze publisher: {exc}") from exc

        with self._lock:
            self._publisher = pub
            self._last_error = ""
        return self.gaze_status()

    def stop_gaze(self) -> dict[str, Any]:
        with self._lock:
            pub = self._publisher
            self._publisher = None
        if pub is not None:
            try:
                pub.stop()
            except Exception:
                logger.debug("Error stopping monocular publisher", exc_info=True)
        return self.gaze_status()

    def reset_adaptation(self) -> dict[str, Any]:
        with self._lock:
            pub = self._publisher
        if pub is not None:
            pub.estimator.reset()
        return self.gaze_status()

    def undo_last_click(self) -> dict[str, Any]:
        with self._lock:
            pub = self._publisher
        if pub is not None:
            pub.estimator.undo_last()
        return self.gaze_status()

    # --- Pursuit warm-up (onboarding calibration) -------------------------

    def calibration_profile(self) -> dict[str, Any]:
        from keyless.gaze.profile import GazeProfile, profile_path

        prof = GazeProfile.load(profile_path(self.paths.config_dir))
        return {"has_profile": prof is not None, "profile": prof.public() if prof else None}

    def start_calibration(self, *, connector: str | None = None, use_overlay: bool = True) -> dict[str, Any]:
        from keyless.gaze.calibration import CalibrationSession

        with self._lock:
            sess = self._calibration
        if sess is not None and not sess.finished:
            return sess.status()
        with self._lock:
            pub = self._publisher
        if pub is None:
            self.start_gaze()
            with self._lock:
                pub = self._publisher
        if pub is None:
            raise RuntimeError(self._last_error or "Camera is not available.")
        sess = CalibrationSession(pub, connector=connector, use_overlay=use_overlay)
        with self._lock:
            self._calibration = sess
        sess.start()
        return sess.status()

    def calibration_status(self) -> dict[str, Any]:
        with self._lock:
            sess = self._calibration
        if sess is None:
            return {"state": "idle", **self.calibration_profile()}
        out = sess.status()
        if sess.finished:
            out.update(self.calibration_profile())
        return out

    def set_calibration_viewport(self, fullscreen: bool) -> dict[str, Any]:
        with self._lock:
            sess = self._calibration
        if sess is not None:
            sess.set_viewport(fullscreen)
        return self.calibration_status()

    def cancel_calibration(self) -> dict[str, Any]:
        with self._lock:
            sess = self._calibration
        if sess is not None:
            sess.cancel()
        return self.calibration_status()

    def forget_calibration(self) -> dict[str, Any]:
        """Delete the warm-up profile; tracking falls back to the cold-start prior."""
        from keyless.gaze.profile import profile_path

        try:
            profile_path(self.paths.config_dir).unlink(missing_ok=True)
        except OSError:
            pass
        with self._lock:
            pub = self._publisher
        if pub is not None:
            pub.estimator.profile = None
            pub.estimator.learner = pub.estimator._cold_start_learner()
            pub.estimator.reset()
        return self.calibration_profile()

    # --- Live session recording -----------------------------------------

    def start_recording(
        self,
        *,
        save_screenshots: bool = True,
        redact_text: bool = False,
        use_ocr: bool = True,
    ) -> dict[str, Any]:
        from keyless.gaze.collect import SessionCollector
        from keyless.gaze.recorder import (
            LinuxClickObserver,
            SessionRecorder,
            build_live_generator,
            build_live_screen_provider,
        )
        from keyless.platform.router import current_host

        with self._lock:
            if self._recorder is not None:
                return self.gaze_status()
            pub = self._publisher

        collector = SessionCollector(self.paths, redact_text=bool(redact_text))
        recorder = SessionRecorder(
            collector,
            build_live_generator(use_ocr=bool(use_ocr)),
            screen_provider=build_live_screen_provider(
                capture_pixels=bool(save_screenshots or use_ocr)
            ),
            frame_provider=(lambda: self._latest_frame()),
            save_screenshots=bool(save_screenshots),
        )

        def _handle_click(x: float, y: float) -> None:
            with self._lock:
                active_pub = self._publisher
            gaze_px = active_pub.latest_pixel() if active_pub is not None else None
            recorder.record_click(x, y, gaze_pixel=gaze_px)
            if active_pub is not None:
                active_pub.on_click(x, y)

        observer = None
        if current_host() == "linux":
            observer = LinuxClickObserver(_handle_click)
            if not observer.start():
                reason = observer.unavailable_reason
                recorder.close()
                with self._lock:
                    self._last_error = reason
                raise PermissionError(reason)

        with self._lock:
            self._recorder = recorder
            self._click_observer = observer
            self._last_error = ""
        return self.gaze_status()

    def _latest_frame(self) -> dict[str, Any] | None:
        with self._lock:
            pub = self._publisher
        return pub.latest_frame_section() if pub is not None else None

    def stop_recording(self) -> dict[str, Any]:
        with self._lock:
            obs = self._click_observer
            rec = self._recorder
            self._click_observer = None
            self._recorder = None
        if obs is not None:
            try:
                obs.stop()
            except Exception:
                pass
        saved_id = None
        saved_stats = None
        if rec is not None:
            rec.close()
            saved_id = rec.session_id
            saved_stats = rec.stats()
        status = self.gaze_status()
        status["last_saved"] = {"session_id": saved_id, "stats": saved_stats}
        return status

    # --- Sessions & Curation --------------------------------------------

    def list_sessions(self) -> dict[str, Any]:
        summaries = list_session_summaries(self.sessions_dir)
        return {
            "sessions_dir": str(self.sessions_dir),
            "sessions": [
                {
                    "session_id": s.session_id,
                    "started_ts_ns": s.started_ts_ns,
                    "event_count": s.event_count,
                    "click_count": s.click_count,
                    "covered_clicks": s.covered_clicks,
                    "accepted_clicks": s.accepted_clicks,
                    "rejected_clicks": s.rejected_clicks,
                    "recall": s.recall,
                    "apps": list(s.apps),
                    "has_screenshots": s.has_screenshots,
                    "redact_text": s.redact_text,
                    "dropped": s.dropped,
                }
                for s in summaries
            ],
        }

    def recall_summary(self) -> dict[str, Any]:
        summary = evaluate_recall_summary(self.sessions_dir)
        return {
            "sessions": summary.sessions,
            "clicks": summary.clicks,
            "hits": summary.hits,
            "overall_recall": summary.overall_recall,
            "gate_met": summary.gate_met,
            "per_app": [
                {
                    "app": row.app,
                    "clicks": row.clicks,
                    "hits": row.hits,
                    "recall": row.recall,
                }
                for row in summary.per_app
            ],
        }

    def _safe_session_dir(self, session_id: str) -> Path:
        root = self.sessions_dir.resolve()
        target = (root / session_id).resolve()
        if target == root or root not in target.parents:
            raise ValueError("invalid session id")
        if not target.exists():
            raise FileNotFoundError(f"session {session_id!r} not found")
        return target

    def get_session(self, session_id: str) -> dict[str, Any]:
        sdir = self._safe_session_dir(session_id)
        session = load_session(sdir)
        return {
            "session_id": session.session_id,
            "meta": session.meta,
            "events": session.events,
            "metrics": metric_table(session.events),
        }

    def patch_event(
        self, session_id: str, index: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        sdir = self._safe_session_dir(session_id)
        kwargs: dict[str, Any] = {}
        if "accepted" in payload:
            kwargs["accepted"] = payload["accepted"]
        if "target" in payload:
            kwargs["target"] = payload["target"]
        updated = update_event(sdir, index, **kwargs)
        return {"event": updated, "index": index}

    def remove_event(self, session_id: str, index: int) -> dict[str, Any]:
        sdir = self._safe_session_dir(session_id)
        remaining = delete_event(sdir, index)
        return {"remaining": remaining}

    def remove_session(self, session_id: str) -> dict[str, Any]:
        sdir = self._safe_session_dir(session_id)
        delete_session(self.sessions_dir, sdir)
        return {"deleted": session_id}

    def read_blob(self, session_id: str, filename: str) -> bytes:
        sdir = self._safe_session_dir(session_id)
        blobs_root = (sdir / "blobs").resolve()
        target = (blobs_root / filename).resolve()
        if blobs_root not in target.parents or not target.exists():
            raise FileNotFoundError(filename)
        return target.read_bytes()

    def create_sample_session(self) -> dict[str, Any]:
        sdir = seed_demo_session(self.paths)
        return {"session_id": sdir.name}

    def export_split(self, *, out_dir: str | None = None, seed: int = 42) -> dict[str, Any]:
        target = Path(out_dir) if out_dir else (self.paths.data_dir / "exports" / "curated-split")
        manifest = export_curated_split(self.sessions_dir, target, seed=seed)
        return {"out_dir": str(target), "manifest": manifest}

    def permission_setup(self) -> dict[str, Any]:
        """OS grants eye tracking needs, for the onboarding permission screens."""
        from keyless.platform.router import current_host

        host = current_host()
        if host != "macos":
            return {"host": host, "steps": []}
        from keyless.platform.macos.permissions import eye_setup_status

        steps = eye_setup_status()
        with self._lock:
            pub = self._publisher
        granted = {s["id"] for s in steps if s["state"] == "granted"}
        if pub is not None and "input_monitoring" in granted:
            pub.start_click_capture()  # picks up a grant made in System Settings
        return {"host": host, "steps": steps}

    def request_setup_permission(self, step: str) -> dict[str, Any]:
        from keyless.platform.router import current_host

        if current_host() != "macos":
            raise ValueError("permission setup steps exist only on macOS")
        from keyless.platform.macos.permissions import request_eye_permission

        outcome = request_eye_permission(step)
        with self._lock:
            pub = self._publisher
        if step == "input_monitoring" and pub is not None:
            pub.start_click_capture()
        return {"step": step, "outcome": outcome, **self.permission_setup()}

    def grant_input_permission(self) -> dict[str, Any]:
        ok, message = grant_linux_input_access()
        return {
            "ok": ok,
            "message": message,
            "manual_command": MANUAL_INPUT_CMD,
            "environment": asdict(probe_lab_environment()),
        }


    # --- 3D Multi-Display Rig & Overlay ---------------------------------

    @property
    def rig_path(self) -> Path:
        return self.paths.config_dir / "display_rig.json"

    def get_display_rig(self) -> dict[str, Any]:
        from keyless.gaze.displays import DisplayRig
        from keyless.platform.router import get_display_layout

        with self._lock:
            pub = self._publisher
        if pub is not None:
            data = pub.rig.as_dict()
            data["auto_rig"] = bool(getattr(pub, "auto_rig", True))
            data["rig_meta"] = pub.estimator.rig_meta
            return data

        layout = get_display_layout()
        rig = DisplayRig.load(self.rig_path, layout)
        data = rig.as_dict()
        data["auto_rig"] = True
        data["rig_meta"] = {"inferred": False, "clicks_per_display": {}, "swapped": False}
        return data

    def save_display_rig(self, payload: dict[str, Any]) -> dict[str, Any]:
        from keyless.gaze.displays import DisplayPlane, DisplayRig
        from keyless.platform.router import get_display_layout

        layout = get_display_layout()
        current = DisplayRig.load(self.rig_path, layout)
        by_conn = {p.connector: p for p in current.planes}

        items = payload.get("displays")
        if not isinstance(items, list) or not items:
            raise ValueError("displays must be a non-empty list")

        new_planes: list[DisplayPlane] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("connector"):
                continue
            conn = str(item["connector"])
            base = by_conn.get(conn)
            if base is None:
                continue
            new_planes.append(
                DisplayPlane(
                    connector=base.connector,
                    x=base.x,
                    y=base.y,
                    width=base.width,
                    height=base.height,
                    scale=base.scale,
                    is_primary=base.is_primary,
                    width_mm=max(80.0, float(item.get("width_mm", base.width_mm))),
                    height_mm=max(50.0, float(item.get("height_mm", base.height_mm))),
                    cx_mm=float(item.get("cx_mm", base.cx_mm)),
                    cy_mm=float(item.get("cy_mm", base.cy_mm)),
                    cz_mm=max(200.0, float(item.get("cz_mm", base.cz_mm))),
                    yaw_deg=max(-75.0, min(75.0, float(item.get("yaw_deg", base.yaw_deg)))),
                    pitch_deg=max(-60.0, min(60.0, float(item.get("pitch_deg", base.pitch_deg)))),
                    roll_deg=max(-90.0, min(90.0, float(item.get("roll_deg", base.roll_deg)))),
                )
            )
        if not new_planes:
            raise ValueError("no matching display connectors found")

        rig = DisplayRig(new_planes)
        rig.save(self.rig_path)
        with self._lock:
            self._rig_version += 1
            pub = self._publisher
        if pub is not None:
            pub.set_display_rig(rig, auto_rig=False)
        return self.get_display_rig()

    def apply_rig_preset(self, preset: str = "flat", *, gap_mm: float = 25.0, distance_mm: float = 600.0) -> dict[str, Any]:
        from keyless.gaze.displays import DisplayPlane, DisplayRig
        from keyless.platform.router import get_display_layout

        layout = get_display_layout()
        if preset == "auto":
            rig = DisplayRig.from_layout(layout, gap_mm=gap_mm, distance_mm=distance_mm)
            try:
                self.rig_path.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                pub = self._publisher
            if pub is not None:
                pub.estimator.reset()
                pub.set_display_rig(rig, auto_rig=True)
            return self.get_display_rig()
        if preset == "swap":
            cur = DisplayRig.load(self.rig_path, layout)
            planes = list(cur.planes)
            if len(planes) >= 2:
                # Swap physical (cx_mm, cy_mm, cz_mm, yaw_deg, pitch_deg, roll_deg) of first two
                a, b = planes[0], planes[1]
                planes[0] = DisplayPlane(
                    **{**a.as_dict(), "cx_mm": b.cx_mm, "cy_mm": b.cy_mm, "cz_mm": b.cz_mm, "yaw_deg": b.yaw_deg, "pitch_deg": b.pitch_deg, "roll_deg": b.roll_deg}
                )
                planes[1] = DisplayPlane(
                    **{**b.as_dict(), "cx_mm": a.cx_mm, "cy_mm": a.cy_mm, "cz_mm": a.cz_mm, "yaw_deg": a.yaw_deg, "pitch_deg": a.pitch_deg, "roll_deg": a.roll_deg}
                )
            rig = DisplayRig(planes)
        else:
            rig = DisplayRig.from_layout(layout, gap_mm=gap_mm, distance_mm=distance_mm)
            if preset == "curved" and len(rig.planes) >= 2:
                n = len(rig.planes)
                curved: list[DisplayPlane] = []
                for idx, p in enumerate(rig.planes):
                    frac = (idx / max(1, n - 1)) * 2.0 - 1.0  # -1..+1
                    yaw = round(frac * 18.0, 1)
                    # Pull outer monitors slightly closer along Z to form a clean cockpit arc
                    dz = -abs(frac) * 55.0
                    curved.append(DisplayPlane(**{**p.as_dict(), "yaw_deg": yaw, "cz_mm": round(p.cz_mm + dz, 1)}))
                rig = DisplayRig(curved)

        rig.save(self.rig_path)
        with self._lock:
            self._rig_version += 1
            pub = self._publisher
        if pub is not None:
            pub.set_display_rig(rig)
        return rig.as_dict()

    def set_overlay_enabled(self, enabled: bool) -> dict[str, Any]:
        cfg = AppConfig.load(self.paths.settings_path)
        cfg.gaze_cursor_overlay = bool(enabled)
        cfg.save(self.paths.settings_path)
        with self._lock:
            pub = self._publisher
        if pub is not None:
            pub.enable_overlay = bool(enabled)
            if enabled and not pub.overlay_active:
                pub.start_overlay()
            elif not enabled:
                pub.stop_overlay()
        return self.gaze_status()

    def set_dev_monitor_border(self, enabled: bool) -> dict[str, Any]:
        cfg = AppConfig.load(self.paths.settings_path)
        cfg.dev_active_monitor_border = bool(enabled)
        cfg.save(self.paths.settings_path)
        with self._lock:
            pub = self._publisher
        if pub is not None:
            pub.set_dev_monitor_border(bool(enabled))
        return self.gaze_status()


    # --- LLM / ACP Auto-Detection & 1-Click Selection -------------------

    def llm_providers_summary(self) -> dict[str, Any]:
        import os
        import socket
        from keyless.models.providers.acp_registry import resolve_agent_binary

        cfg = AppConfig.load(self.paths.settings_path)

        # Fast 120ms local Ollama check
        ollama_up = False
        try:
            with socket.create_connection(("127.0.0.1", 11434), timeout=0.12):
                ollama_up = True
        except OSError:
            ollama_up = resolve_agent_binary("ollama") is not None

        has_npx = resolve_agent_binary("npx") is not None
        candidates = [
            {
                "id": "acp:opencode",
                "name": "OpenCode",
                "kind": "ACP Agent",
                "description": "Open-source terminal coding agent via ACP",
                "ready": resolve_agent_binary("opencode") is not None,
                "patch": {"llm_provider": "acp", "llm_backend": "acp", "acp_agent_id": "opencode"},
            },
            {
                "id": "acp:claude-acp",
                "name": "Claude Code",
                "kind": "ACP Agent",
                "description": "Anthropic Claude via ACP adapter",
                "ready": resolve_agent_binary("claude") is not None or has_npx,
                "patch": {"llm_provider": "acp", "llm_backend": "acp", "acp_agent_id": "claude-acp"},
            },
            {
                "id": "acp:codex-acp",
                "name": "Codex",
                "kind": "ACP Agent",
                "description": "OpenAI Codex via ACP adapter",
                "ready": resolve_agent_binary("codex") is not None or has_npx,
                "patch": {"llm_provider": "acp", "llm_backend": "acp", "acp_agent_id": "codex-acp"},
            },
            {
                "id": "ollama",
                "name": "Ollama (Local)",
                "kind": "Local LLM",
                "description": f"Offline local inference ({cfg.ollama_model})",
                "ready": ollama_up,
                "patch": {"llm_provider": "ollama", "llm_backend": "ollama"},
            },
            {
                "id": "gemini",
                "name": "Gemini API",
                "kind": "Cloud API",
                "description": f"Direct Google API ({cfg.gemini_model})",
                "ready": bool(os.environ.get("GEMINI_API_KEY")),
                "patch": {"llm_provider": "gemini", "llm_backend": "gemini", "llm_preset": "gemini"},
            },
            {
                "id": "openai_compat",
                "name": "OpenAI / Custom API",
                "kind": "Cloud API",
                "description": f"OpenAI-compatible endpoint ({cfg.llm_preset})",
                "ready": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")),
                "patch": {"llm_provider": "openai_compat", "llm_backend": "openai_compat"},
            },
        ]

        # Determine currently selected preset id
        active_id = "auto"
        prov = cfg.llm_provider if cfg.llm_provider != "auto" else cfg.llm_backend
        if prov == "acp":
            active_id = f"acp:{cfg.acp_agent_id}"
        elif prov in ("ollama", "gemini", "openai_compat"):
            active_id = prov

        recommended = next((c["id"] for c in candidates if c["ready"]), "ollama")
        active_model = (
            cfg.ollama_model if active_id == "ollama"
            else cfg.gemini_model if active_id == "gemini"
            else (cfg.llm_model or "")
        )
        return {
            "active_id": active_id,
            "active_model": active_model,
            "active_effort": cfg.llm_reasoning_effort or "low",
            "recommended_id": recommended,
            "providers": candidates,
        }

    def audio_input_devices(self) -> dict[str, Any]:
        """Microphone inputs enumerated from the host runtime (PortAudio).

        Never raises: enumeration failures return an empty list plus an error
        string so the picker can show an explicit error state.
        """
        devices: list[dict[str, Any]] = []
        error = ""
        default_index: int | None = None
        try:
            import sounddevice as sd

            raw_default = sd.default.device
            if isinstance(raw_default, (list, tuple)) and raw_default:
                candidate = raw_default[0]
                if isinstance(candidate, int) and candidate >= 0:
                    default_index = candidate
            for index, device in enumerate(sd.query_devices()):
                channels = int(device.get("max_input_channels", 0))
                if channels > 0:
                    devices.append(
                        {
                            "id": index,
                            "name": str(device.get("name", "")),
                            "channels": channels,
                            "default": index == default_index,
                        }
                    )
        except Exception as exc:
            error = f"Audio device enumeration failed: {exc}"
        cfg = AppConfig.load(self.paths.settings_path)
        return {
            "devices": devices,
            "selected": cfg.audio_device,
            "default_index": default_index,
            "error": error,
        }

    def camera_devices(self) -> dict[str, Any]:
        """Cameras enumerated from the host platform layer (stable by-id paths).

        Never raises: enumeration failures return an empty list plus an error
        string so the picker can show an explicit error state.
        """
        devices: list[dict[str, Any]] = []
        error = ""
        try:
            from keyless.platform.router import list_cameras

            for cam in list_cameras():
                # macOS indexes shift when cameras come and go; store the AVFoundation uniqueID.
                stable = cam.id if cam.capabilities.get("unique_id") else cam.path
                devices.append({"id": str(stable), "name": str(cam.name)})
        except Exception as exc:
            error = f"Camera enumeration failed: {exc}"
        cfg = AppConfig.load(self.paths.settings_path)
        return {
            "devices": devices,
            "selected": {"cam1_by_id": cfg.cam1_by_id, "cam2_by_id": cfg.cam2_by_id},
            "camera_mode": cfg.camera_mode,
            "error": error,
        }

    def select_llm_provider(self, preset_id: str) -> dict[str, Any]:
        summary = self.llm_providers_summary()
        target_id = summary["recommended_id"] if preset_id == "auto" else preset_id
        match = next((p for p in summary["providers"] if p["id"] == target_id), None)
        if match is None:
            raise ValueError(f"Unknown LLM preset: {preset_id}")
        cfg = AppConfig.load(self.paths.settings_path)
        for k, v in match["patch"].items():
            setattr(cfg, k, v)
        cfg.save(self.paths.settings_path)
        out = self.llm_providers_summary()
        out["config"] = asdict(cfg)
        return out

    def llm_provider_options(self, preset_id: str, *, force: bool = False) -> dict[str, Any]:
        """Probe available models and reasoning effort levels for the selected provider.

        Always returns a JSON-serialisable options document; probe failures
        land in ``error`` with empty option lists, never as a raised exception.
        Concurrent requests for the same preset share one probe.
        """
        if not force:
            cached = self._llm_options_cache.get(preset_id)
            if cached is not None:
                return dict(cached)
        with self._llm_options_lock(preset_id):
            # Another request may have completed the probe while we waited.
            if not force:
                cached = self._llm_options_cache.get(preset_id)
                if cached is not None:
                    return dict(cached)
            return self._probe_llm_options(preset_id)

    def _probe_llm_options(self, preset_id: str) -> dict[str, Any]:
        models: list[dict[str, str]] = []
        efforts: list[dict[str, str]] = []
        current_model = ""
        current_effort = ""
        error = ""

        if preset_id.startswith("acp:"):
            agent_id = preset_id.split(":", 1)[1]
            try:
                from keyless.models.providers.acp import AcpClient
                from keyless.models.providers.acp_registry import OFFLINE_AGENTS, resolve_agent_binary

                spec = next((s for s in OFFLINE_AGENTS if s.id == agent_id), None)
                if spec is None or not spec.command:
                    error = f"Unknown ACP agent {agent_id}"
                elif resolve_agent_binary(spec.command[0]) is None:
                    error = f"{spec.command[0]} is not installed on this machine."
                else:
                    client = AcpClient(spec.command, timeout=12.0)
                    try:
                        client.start()
                        client.initialize()
                        client.new_session()
                        for co in client.config_options:
                            cid = str(co.get("id", ""))
                            cat = str(co.get("category", ""))
                            raw_opts = [o for o in (co.get("options") or []) if isinstance(o, dict)]
                            if cat == "model" or cid == "model":
                                current_model = str(co.get("currentValue", ""))
                                models = [
                                    {"value": str(o.get("value", "")), "name": str(o.get("name", o.get("value", "")))}
                                    for o in raw_opts
                                    if o.get("value")
                                ]
                            elif cat == "thought_level" or cid in ("thought_level", "effort", "reasoning_effort"):
                                current_effort = str(co.get("currentValue", ""))
                                efforts = [
                                    {"value": str(o.get("value", "")), "name": str(o.get("name", o.get("value", "")))}
                                    for o in raw_opts
                                    if o.get("value")
                                ]
                        if not models and client.available_models:
                            models = [
                                {"value": str(m.get("modelId", "")), "name": str(m.get("name", m.get("modelId", "")))}
                                for m in client.available_models
                                if m.get("modelId")
                            ]
                    finally:
                        client.close()
            except Exception as exc:
                error = str(exc)
        elif preset_id == "ollama":
            try:
                from keyless.models.providers.ollama import OllamaProvider

                cfg = AppConfig.load(self.paths.settings_path)
                st = OllamaProvider(cfg.ollama_model).status()
                avail = list(st.extra.get("models") or [])
                models = [{"value": str(m), "name": str(m)} for m in avail]
                current_model = cfg.ollama_model
            except Exception as exc:
                error = str(exc)
        elif preset_id == "gemini":
            models = [
                {"value": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
                {"value": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
            ]
            current_model = AppConfig.load(self.paths.settings_path).gemini_model
        else:
            models = [
                {"value": "gpt-4.1-mini", "name": "GPT-4.1 mini"},
                {"value": "gpt-4.1", "name": "GPT-4.1"},
            ]
            current_model = AppConfig.load(self.paths.settings_path).llm_model

        if not efforts:
            efforts = [
                {"value": "low", "name": "Low (Fast)"},
                {"value": "medium", "name": "Medium"},
                {"value": "high", "name": "High"},
            ]

        result = {
            "id": preset_id,
            "models": models,
            "current_model": current_model,
            "efforts": efforts,
            "current_effort": current_effort,
            "error": error,
        }
        if not error:
            self._llm_options_cache[preset_id] = result
        return result

    def configure_llm(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        cfg = AppConfig.load(self.paths.settings_path)
        prov = cfg.llm_provider if cfg.llm_provider != "auto" else cfg.llm_backend
        if model is not None:
            clean = str(model).strip()
            if prov == "ollama":
                cfg.ollama_model = clean or cfg.ollama_model
            elif prov == "gemini":
                cfg.gemini_model = clean or cfg.gemini_model
            else:
                cfg.llm_model = clean
        if reasoning_effort is not None:
            cfg.llm_reasoning_effort = str(reasoning_effort).strip() or "low"
        cfg.save(self.paths.settings_path)
        out = self.llm_providers_summary()
        out["config"] = asdict(cfg)
        return out

    def shutdown(self) -> None:
        with self._lock:
            sess = self._calibration
        if sess is not None:
            sess.cancel()
        self.stop_recording()
        self.stop_gaze()
