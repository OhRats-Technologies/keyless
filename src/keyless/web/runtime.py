"""Backend service layer backing the unified Keyless HTTP web UI.

Manages the live monocular gaze publisher, live session recording, dataset
curation, and host permission probes behind thread-safe methods consumed by
``ControlServer`` routes.
"""

from __future__ import annotations

import logging
import threading
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

    @property
    def sessions_dir(self) -> Path:
        return self.paths.data_dir / "sessions"

    # --- Monocular gaze publisher ---------------------------------------

    def gaze_status(self) -> dict[str, Any]:
        with self._lock:
            pub = self._publisher
            rec = self._recorder
            last_err = self._last_error

        env = probe_lab_environment()
        running = pub is not None
        pred = pub.latest_prediction() if pub is not None else None
        stats = pub.stats() if pub is not None else None
        layout = pub.layout if pub is not None else None
        if layout is None:
            try:
                from keyless.platform.router import get_display_layout

                layout = get_display_layout()
            except Exception:
                layout = None

        return {
            "running": running,
            "recording": rec is not None,
            "recording_session_id": rec.session_id if rec is not None else None,
            "recording_stats": rec.stats() if rec is not None else None,
            "display": {
                "width": layout.total_width if layout is not None else 1920,
                "height": layout.total_height if layout is not None else 1080,
            },
            "prediction": asdict(pred) if pred is not None else None,
            "stats": stats,
            "environment": asdict(env),
            "manual_input_command": MANUAL_INPUT_CMD,
            "last_error": last_err,
        }

    def start_gaze(
        self,
        *,
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

    def grant_input_permission(self) -> dict[str, Any]:
        ok, message = grant_linux_input_access()
        return {
            "ok": ok,
            "message": message,
            "manual_command": MANUAL_INPUT_CMD,
            "environment": asdict(probe_lab_environment()),
        }

    def shutdown(self) -> None:
        self.stop_recording()
        self.stop_gaze()
