"""Loopback control plane shared by the Qt shell and the native SwiftUI host.

The Python engine is headless: ``keyless serve`` exposes a localhost-only HTTP
API that any first-party UI host can drive. The Linux PySide6 shell uses it
indirectly today (via DaemonController); the macOS SwiftUI host in
``packaging/macos/`` uses it over URLSession. No auth beyond loopback bind —
bind 127.0.0.1 only and never 0.0.0.0.

Endpoints (all JSON):
  GET  /v1/status       -> {state, detail}
  GET  /v1/capabilities -> CapabilityReport.as_dict()
  GET  /v1/doctor       -> DoctorReport.as_dict() (providers not probed)
  GET  /v1/config       -> AppConfig (asdict)
  POST /v1/config       -> patch AppConfig, returns the updated config
  GET  /v1/history?limit=N -> {actions: [...]}
  POST /v1/start        -> start daemon child
  POST /v1/pause        -> pause daemon child
  GET  /v1/setup        -> canonical setup flow + gaze model readiness
  POST /v1/setup        -> {action: reset|go_to|advance|back|complete, step?}
  POST /v1/calibrate    -> 410-style notice: the legacy calibration engine is gone
  GET  /v1/models       -> pinned speech model status + active download job
  POST /v1/models/download -> start verified background download (409 if busy)
  POST /v1/models/cancel   -> cooperative stop; partial files stay resumable
  GET  /v1/runtime    -> python executable + args for native-host launches

Setup is engine-owned: the step order and persisted step come from
``keyless.ui.onboarding.flow.OnboardingFlow`` — the same state machine the Qt
shell uses — so every shell sees one canonical flow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from keyless.config.paths import AppPaths, current_paths
from keyless.ui.onboarding.flow import STEPS, OnboardingFlow, OnboardingStep


@dataclass
class _ModelDownloadJob:
    """One engine-owned background model download, polled by every shell."""

    state: str = "downloading"
    percent: float = 0.0
    bytes_done: int = 0
    bytes_total: int = 0
    file_name: str = ""
    file_index: int = 0
    file_count: int = 0
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "percent": round(self.percent, 1),
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "file_name": self.file_name,
            "file_index": self.file_index,
            "file_count": self.file_count,
            "error": self.error,
        }


def _json(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ControlServer:
    """Owns one DaemonController + HistoryStore behind a loopback HTTP server."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        from keyless.ui.history.db import HistoryStore
        from keyless.ui.session import DaemonController

        self.paths = paths or current_paths()
        self.controller = DaemonController(paths=self.paths)
        self.history = HistoryStore(self.paths)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._model_job: _ModelDownloadJob | None = None
        self._model_lock = threading.Lock()

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return int(self._httpd.server_address[1])

    def model_snapshot(self) -> dict[str, object]:
        """Status of the pinned offline speech model, plus its active job."""
        from keyless.config.schema import AppConfig
        from keyless.models.catalog import DEFAULT_STT_ASSET_ID, get_asset
        from keyless.models.store import ModelStore

        asset = get_asset(DEFAULT_STT_ASSET_ID)
        status = ModelStore(self.paths).status(asset)
        config = AppConfig.load(self.paths.settings_path)
        with self._model_lock:
            job = self._model_job.as_dict() if self._model_job is not None else None
        return {
            "id": asset.id,
            "name": "Parakeet Unified EN",
            "display_name": asset.display_name,
            "size": asset.total_bytes,
            "state": status.state.value,
            "ready": status.ready,
            "selected": config.stt_model_id,
            "job": job,
        }

    def start_model_download(self) -> dict[str, object]:
        """Start the verified download on a daemon thread, never the HTTP thread."""
        from keyless.models.catalog import DEFAULT_STT_ASSET_ID, get_asset
        from keyless.models.downloader import (
            DownloadCancelled,
            DownloadProgress,
            download_asset,
        )
        from keyless.models.store import ModelStore

        with self._model_lock:
            if self._model_job is not None and self._model_job.state in (
                "downloading", "verifying"
            ):
                return {"started": False, "busy": True}
            job = _ModelDownloadJob()
            self._model_job = job
        asset = get_asset(DEFAULT_STT_ASSET_ID)

        def run() -> None:
            def on_progress(update: DownloadProgress) -> None:
                with self._model_lock:
                    job.percent = update.percent
                    job.bytes_done = update.overall_done
                    job.bytes_total = update.overall_total
                    job.file_name = update.file_name
                    job.file_index = update.file_index
                    job.file_count = update.file_count

            try:
                download_asset(asset, paths=self.paths, store=ModelStore(self.paths),
                               progress=on_progress, cancel=job.cancel)
                with self._model_lock:
                    job.state = "ready"
                    job.percent = 100.0
            except DownloadCancelled:
                with self._model_lock:
                    job.state = "cancelled"
                    job.error = "Stopped"
            except Exception as exc:
                with self._model_lock:
                    job.state = "failed"
                    job.error = str(exc)

        threading.Thread(target=run, name="keyless-model-download", daemon=True).start()
        return {"started": True, "busy": False}

    def cancel_model_download(self) -> dict[str, object]:
        with self._model_lock:
            if self._model_job is None or self._model_job.state not in (
                "downloading", "verifying"
            ):
                return {"cancelled": False}
            self._model_job.cancel.set()
            return {"cancelled": True}

    def setup_snapshot(self) -> dict[str, object]:
        """One engine-owned view of setup for every shell."""
        flow = OnboardingFlow(self.paths)
        return {
            "schema_version": 1,
            "steps": [step.value for step in STEPS],
            "state": flow.state.as_dict(),
            "complete": flow.is_complete,
            # The legacy calibration gate is retired. The target model will
            # self-calibrate online; until it ships, voice control is ready.
            "calibration": {
                "path": "",
                "ready": True,
                "running": False,
                "legacy_removed": True,
            },
            "model": self.model_snapshot(),
        }

    def apply_setup_action(self, action: str, step: str | None) -> dict[str, object]:
        flow = OnboardingFlow(self.paths)
        if action == "reset":
            flow.reset()
        elif action == "go_to" and step in {item.value for item in STEPS}:
            flow.go_to(OnboardingStep(step))
        elif action == "advance":
            flow.advance(skipped=step == "skipped")
        elif action == "back":
            flow.back()
        elif action == "complete":
            flow.go_to(OnboardingStep.READY)
            flow.advance()
        else:
            raise ValueError(f"unknown setup action {action!r}")
        return self.setup_snapshot()

    def launch_calibration(self) -> dict[str, object]:
        """The legacy calibration engine is gone; the new model is not shipped yet."""
        return {
            "started": False,
            "running": False,
            "disabled": True,
            "message": (
                "The legacy calibration engine has been removed. "
                "The target-level gaze model v2 is not installed yet; "
                "voice control remains available."
            ),
        }

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # noqa: D102 - quiet
                return

            def _route(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if self.command == "GET" and path == "/v1/status":
                    status = server.controller.status
                    _json(self, {"state": status.state.value, "detail": status.detail})
                elif self.command == "GET" and path == "/v1/capabilities":
                    from keyless.platform.capabilities import detect_capabilities

                    _json(self, detect_capabilities(paths=server.paths).as_dict())
                elif self.command == "GET" and path == "/v1/doctor":
                    from keyless.diagnostics.doctor import collect_doctor

                    _json(
                        self,
                        collect_doctor(
                            paths=server.paths, check_providers=False
                        ).as_dict(),
                    )
                elif self.command == "GET" and path == "/v1/config":
                    from keyless.config.schema import AppConfig

                    _json(self, asdict(AppConfig.load(server.paths.settings_path)))
                elif self.command == "POST" and path == "/v1/config":
                    from keyless.config.schema import AppConfig

                    length = int(self.headers.get("Content-Length", "0") or 0)
                    try:
                        patch = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        _json(self, {"error": "invalid JSON"}, status=400)
                        return
                    cfg = AppConfig.load(server.paths.settings_path)
                    allowed = {
                        "camera_mode", "camera_backend", "cam1_by_id", "cam2_by_id",
                        "gaze_cursor_control", "gaze_cursor_overlay",
                        "always_on_voice", "require_command_prefix", "enable_pop_click",
                        "audio_device", "ptt_hotkey", "llm_provider", "llm_preset",
                        "llm_model", "acp_agent_id", "gaze_vlm_remote_consent",
                        "tts_enabled", "tts_voice", "auto_update_checks",
                        "action_backend",
                    }
                    for key, value in patch.items():
                        if key in allowed and hasattr(cfg, key):
                            setattr(cfg, key, value)
                    cfg.save(server.paths.settings_path)
                    _json(self, asdict(cfg))
                elif self.command == "GET" and path == "/v1/history":
                    qs = parse_qs(parsed.query)
                    try:
                        limit = max(1, min(200, int(qs.get("limit", ["50"])[0])))
                    except ValueError:
                        limit = 50
                    _json(self, {"actions": server.history.recent(limit)})
                elif self.command == "POST" and path == "/v1/start":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length:
                        self.rfile.read(length)
                    status = server.controller.start()
                    _json(self, {"state": status.state.value, "detail": status.detail})
                elif self.command == "POST" and path == "/v1/pause":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length:
                        self.rfile.read(length)
                    status = server.controller.pause()
                    _json(self, {"state": status.state.value, "detail": status.detail})
                elif self.command == "GET" and path == "/v1/setup":
                    _json(self, server.setup_snapshot())
                elif self.command == "POST" and path == "/v1/setup":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    try:
                        payload = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        _json(self, {"error": "invalid JSON"}, status=400)
                        return
                    if not isinstance(payload, dict):
                        _json(self, {"error": "expected a JSON object"}, status=400)
                        return
                    try:
                        _json(
                            self,
                            server.apply_setup_action(
                                str(payload.get("action", "")), payload.get("step")
                            ),
                        )
                    except ValueError as exc:
                        _json(self, {"error": str(exc)}, status=400)
                elif self.command == "POST" and path == "/v1/calibrate":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length:
                        self.rfile.read(length)
                    result = server.launch_calibration()
                    _json(
                        self,
                        result,
                        status=200
                        if result.get("started")
                        or result.get("running")
                        or result.get("disabled")
                        else 500,
                    )
                elif self.command == "GET" and path == "/v1/runtime":
                    from pathlib import Path

                    _json(self, {
                        "python": sys.executable,
                        "args": [],
                        "cwd": str(Path(__file__).resolve().parents[2]),
                    })
                elif self.command == "GET" and path == "/v1/models":
                    _json(self, {"model": server.model_snapshot()})
                elif self.command == "POST" and path == "/v1/models/download":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length:
                        self.rfile.read(length)
                    result = server.start_model_download()
                    _json(self, result, status=409 if result.get("busy") else 200)
                elif self.command == "POST" and path == "/v1/models/cancel":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length:
                        self.rfile.read(length)
                    _json(self, server.cancel_model_download())
                else:
                    _json(self, {"error": f"unknown route {path}"}, status=404)

            do_GET = _route  # type: ignore[assignment]
            do_POST = _route  # type: ignore[assignment]

        return Handler

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Control plane binds loopback only.")
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="keyless-control", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.controller.quit()
        except Exception:
            pass
