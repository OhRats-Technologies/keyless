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
  GET  /v1/config          -> AppConfig (asdict)
  GET  /v1/config/schema   -> {schema_version, settings: [SettingSpec...]}
  POST /v1/config          -> validated patch; {config, restart_required, changed}
  GET  /v1/history?limit=N -> {actions: [...]}
  GET  /v1/ui/state     -> schema-v1 UI document (503 until a builder is injected)
  POST /v1/ui/action    -> {action, arg?} -> {ok, action, result}
  POST /v1/start        -> start daemon child
  POST /v1/pause        -> pause daemon child
  GET  /v1/setup        -> canonical setup flow + gaze model readiness
  POST /v1/setup        -> {action: reset|go_to|advance|back|complete, step?}
  POST /v1/calibrate    -> 410-style notice: the legacy calibration engine is gone
  GET  /v1/models       -> pinned speech model status + active download job
  POST /v1/models/download -> start verified background download (409 if busy)
  POST /v1/models/cancel   -> cooperative stop; partial files stay resumable
  GET  /v1/audio/devices -> mic inputs enumerated from the host runtime
  GET  /v1/cameras/devices -> cameras enumerated from the host platform layer
  GET  /v1/runtime    -> python executable + args for native-host launches

Setup is engine-owned: the step order and persisted step come from
``keyless.core.onboarding.OnboardingFlow`` — the same state machine every shell
uses — so every shell sees one canonical flow.

Two UI layers stay separate on purpose. The *screen document* (schema v1) is
built by ``keyless.ui.contract.build_ui_state`` and injected through
``self.ui_contract_builder``; until a host registers it, ``GET /v1/ui/state``
returns 503. The *action registry* lives here because it only needs engine
state (controller, config specs, setup flow, doctor) and must not import
``keyless.ui`` at any depth. Hosts that want different side effects can inject
``self.ui_action_handler`` instead.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from keyless.config.paths import AppPaths, current_paths
from keyless.config.specs import SETTING_SPECS, spec_for, validate_patch
from keyless.core.onboarding import STEPS, OnboardingFlow, OnboardingStep


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


class _PatchRejected(Exception):
    """A config patch failed spec validation."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("invalid config patch")
        self.errors = errors


class _ActionRejected(Exception):
    """A /v1/ui/action request cannot be executed as sent."""

    def __init__(
        self, message: str, *, status: int = 400, errors: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors = errors or {}


class ControlServer:
    """Owns one DaemonController + HistoryStore behind a loopback HTTP server."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        from keyless.core.history import HistoryStore
        from keyless.core.lifecycle import DaemonController

        self.paths = paths or current_paths()
        self.controller = DaemonController(paths=self.paths)
        self.history = HistoryStore(self.paths)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._model_job: _ModelDownloadJob | None = None
        self._model_lock = threading.Lock()
        # Live change stream (GET /v1/events): bounded subscriber set; each
        # stream polls its own snapshot on its request thread.
        self._event_streams: set[BaseHTTPRequestHandler] = set()
        self._event_lock = threading.Lock()
        self._events_stop = threading.Event()
        # Injected by a UI host (`keyless serve`, `run_ui`). The engine keeps
        # zero `keyless.ui` imports; see the module docstring for the split.
        self.ui_contract_builder = None
        self.ui_action_handler = None
        self._gaze_manager = None

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
            "name": asset.display_name,
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

    def apply_config_patch(self, patch: dict[str, object]) -> dict[str, object]:
        """Validate and persist one partial config update, shared by HTTP routes."""
        from keyless.config.schema import AppConfig

        errors = validate_patch(patch)
        if errors:
            raise _PatchRejected(errors)
        cfg = AppConfig.load(self.paths.settings_path)
        changed = [key for key, value in patch.items() if getattr(cfg, key) != value]
        for key in changed:
            setattr(cfg, key, patch[key])
        if changed:
            cfg.save(self.paths.settings_path)
        restart_required = any(
            (spec := spec_for(key)) is not None and spec.apply == "restart"
            for key in changed
        )
        return {
            "config": asdict(cfg),
            "restart_required": restart_required,
            "changed": changed,
        }

    def ui_state(self) -> dict[str, object] | None:
        """Build the schema-v1 UI document, or ``None`` when no builder is registered."""
        builder = self.ui_contract_builder
        if builder is None:
            return None
        from keyless.config.schema import AppConfig
        from keyless.diagnostics.doctor import collect_doctor
        from keyless.platform.capabilities import detect_capabilities

        status = self.controller.status
        return builder(
            capabilities=detect_capabilities(paths=self.paths).as_dict(),
            doctor=collect_doctor(paths=self.paths, check_providers=False).as_dict(),
            config=asdict(AppConfig.load(self.paths.settings_path)),
            history=self.history.recent(10),
            model=self.model_snapshot(),
            setup=self.setup_snapshot(),
            status={"state": status.state.value, "detail": status.detail},
            specs=[spec.as_dict() for spec in SETTING_SPECS],
        )

    def run_ui_action(self, payload: dict[str, object]) -> dict[str, object]:
        """Default engine-side action registry for ``POST /v1/ui/action``."""
        action = str(payload.get("action") or "")
        arg = payload.get("arg")
        if action in ("start", "pause"):
            status = getattr(self.controller, action)()
            return {"state": status.state.value, "detail": status.detail}
        if action == "set_config":
            if not isinstance(arg, dict):
                raise _ActionRejected("set_config requires an 'arg' object")
            return self.apply_config_patch(arg)
        if action == "setup":
            if not isinstance(arg, dict):
                raise _ActionRejected("setup requires an 'arg' object")
            setup_action = str(arg.get("action") or "")
            if not setup_action:
                raise _ActionRejected("setup 'arg' requires an action")
            try:
                return self.apply_setup_action(setup_action, arg.get("step"))
            except ValueError as exc:
                raise _ActionRejected(str(exc)) from exc
        if action == "select_camera":
            if not isinstance(arg, dict):
                raise _ActionRejected("select_camera requires an 'arg' object")
            allowed = {"camera_mode", "cam1_by_id", "cam2_by_id"}
            unknown = sorted(set(arg) - allowed)
            if unknown:
                raise _ActionRejected(
                    "select_camera accepts camera_mode, cam1_by_id, cam2_by_id",
                    errors={key: "unknown setting" for key in unknown},
                )
            return self.apply_config_patch(arg)
        if action == "calibrate":
            return self.launch_calibration()
        if action == "allow":
            if not isinstance(arg, dict):
                raise _ActionRejected("allow requires an 'arg' object")
            permission_id = str(arg.get("permission") or "")
            if not permission_id:
                raise _ActionRejected("allow 'arg' requires a permission id")
            try:
                return self.request_permission(permission_id)
            except ValueError as exc:
                raise _ActionRejected(str(exc)) from exc
        if action == "models_download":
            return self.start_model_download()
        if action == "models_cancel":
            return self.cancel_model_download()
        if action == "diagnostics":
            from keyless.diagnostics.doctor import collect_doctor

            return collect_doctor(paths=self.paths, check_providers=False).as_dict()
        raise _ActionRejected(f"unknown action {action!r}")

    def request_permission(self, permission_id: str) -> dict[str, object]:
        """Ask the platform to prompt for one permission row's grant.

        Where an OS prompt exists (macOS Accessibility) it is triggered;
        otherwise the row's remediation text is returned so the shell can
        show the same guidance the capability probe already carries.
        """
        from keyless.platform.capabilities import detect_capabilities
        from keyless.platform.router import current_host

        report = detect_capabilities(paths=self.paths)
        info = report.permissions.get(permission_id)
        if info is None:
            raise ValueError(f"unknown permission {permission_id!r}")
        prompted = False
        if current_host() == "macos":
            from keyless.platform.macos import permissions as mac_permissions

            if permission_id in ("accessibility", "input_injection"):
                prompted = mac_permissions.prompt_accessibility_trust() is not None
        return {
            "permission": permission_id,
            "state": info.state.value,
            "prompted": prompted,
            "action": info.action,
            "detail": info.detail,
        }

    # -- live change stream (GET /v1/events) --------------------------------

    _EVENT_POLL_S = 1.0
    _MAX_EVENT_STREAMS = 4

    def _event_snapshot(self) -> dict[str, object]:
        """Compact change-detector payload for SSE; shells refetch full state."""
        from keyless.platform.capabilities import detect_capabilities

        status = self.controller.status
        caps = detect_capabilities(paths=self.paths).as_dict()
        with self._model_lock:
            job = self._model_job.as_dict() if self._model_job is not None else None
        recent = self.history.recent(1)
        return {
            "status": {"state": status.state.value, "detail": status.detail},
            "capabilities": caps.get("capabilities", {}),
            "permissions": caps.get("permissions", {}),
            "setup": self.setup_snapshot()["state"],
            "model": {**self.model_snapshot(), "job": job},
            "history_latest": recent[0] if recent else None,
        }

    def _serve_events(self, handler: BaseHTTPRequestHandler) -> None:
        """Push one SSE ``state`` event whenever the snapshot changes.

        Each stream polls its own snapshot once a second on its request
        thread; the concurrency cap keeps the loopback server bounded. A
        comment line every ~15 s detects client disconnects.
        """
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        last: dict[str, object] | None = None
        ticks = 0
        while not self._events_stop.is_set():
            try:
                snapshot = self._event_snapshot()
            except Exception:
                snapshot = None
            if snapshot is not None and snapshot != last:
                payload = "event: state\ndata: " + json.dumps(
                    snapshot, default=str
                ) + "\n\n"
                handler.wfile.write(payload.encode())
                handler.wfile.flush()
                last = snapshot
            ticks += 1
            if ticks % 15 == 0:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
            self._events_stop.wait(self._EVENT_POLL_S)

    @property
    def gaze_manager(self):
        if self._gaze_manager is None:
            from keyless.web.runtime import WebGazeManager

            self._gaze_manager = WebGazeManager(self.paths)
        return self._gaze_manager

    def _serve_gaze_stream(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        last_rig_ver = -1
        while not self._events_stop.is_set():
            snap, last_rig_ver = self.gaze_manager.gaze_stream_tick(last_rig_ver)
            payload = json.dumps(snap, separators=(",", ":"), default=str).encode()
            handler.wfile.write(b"event: gaze\ndata: " + payload + b"\n\n")
            handler.wfile.flush()
            delay = 0.033 if snap.get("running") else 0.75
            if self._events_stop.wait(delay):
                break

    def _serve_calibration_stream(self, handler: BaseHTTPRequestHandler) -> None:
        """60 Hz while a warm-up runs (browser stage renders from it), 2 Hz otherwise."""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        while not self._events_stop.is_set():
            snap = self.gaze_manager.calibration_status()
            payload = json.dumps(snap, separators=(",", ":"), default=str).encode()
            handler.wfile.write(b"event: calibration\ndata: " + payload + b"\n\n")
            handler.wfile.flush()
            active = snap.get("state") in ("starting", "intro", "running", "paused", "solving")
            if self._events_stop.wait(1 / 60 if active else 0.5):
                break

    @staticmethod
    def _serve_static(handler: BaseHTTPRequestHandler, rel_path: str) -> None:
        from keyless.web import STATIC_DIR

        root = STATIC_DIR.resolve()
        clean = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
        target = (root / clean).resolve()
        if target != root and root not in target.parents:
            _json(handler, {"error": "forbidden"}, status=403)
            return
        if not target.exists() or not target.is_file():
            _json(handler, {"error": f"asset not found: {clean}"}, status=404)
            return
        body = target.read_bytes()
        ctype, _ = mimetypes.guess_type(target.name)
        if not ctype:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype = f"{ctype}; charset=utf-8"
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _read_json_body(handler: BaseHTTPRequestHandler) -> dict | None:
        length = int(handler.headers.get("Content-Length", "0") or 0)
        raw = handler.rfile.read(length) if length > 0 else b"{}"
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            _json(handler, {"error": "invalid JSON"}, status=400)
            return None
        if not isinstance(parsed, dict):
            _json(handler, {"error": "expected a JSON object"}, status=400)
            return None
        return parsed

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
                elif self.command == "GET" and path == "/v1/config/schema":
                    from keyless.platform.router import current_host

                    _json(
                        self,
                        {
                            "schema_version": 1,
                            "host": current_host(),
                            "settings": [spec.as_dict() for spec in SETTING_SPECS],
                        },
                    )
                elif self.command == "POST" and path == "/v1/config":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    try:
                        patch = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        _json(self, {"error": "invalid JSON"}, status=400)
                        return
                    if not isinstance(patch, dict):
                        _json(self, {"error": "expected a JSON object"}, status=400)
                        return
                    try:
                        _json(self, server.apply_config_patch(patch))
                    except _PatchRejected as exc:
                        _json(
                            self,
                            {"error": "invalid config patch", "errors": exc.errors},
                            status=400,
                        )
                elif self.command == "GET" and path == "/v1/ui/state":
                    document = server.ui_state()
                    if document is None:
                        _json(
                            self,
                            {"error": "ui contract builder not registered"},
                            status=503,
                        )
                    else:
                        _json(self, document)
                elif self.command == "POST" and path == "/v1/ui/action":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    try:
                        payload = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        _json(self, {"error": "invalid JSON"}, status=400)
                        return
                    if not isinstance(payload, dict):
                        _json(self, {"error": "expected a JSON object"}, status=400)
                        return
                    handler = server.ui_action_handler or server.run_ui_action
                    try:
                        result = handler(payload)
                    except _ActionRejected as exc:
                        body: dict[str, object] = {"ok": False, "error": exc.message}
                        if exc.errors:
                            body["errors"] = exc.errors
                        _json(self, body, status=exc.status)
                        return
                    except _PatchRejected as exc:
                        _json(
                            self,
                            {
                                "ok": False,
                                "error": "invalid config patch",
                                "errors": exc.errors,
                            },
                            status=400,
                        )
                        return
                    _json(
                        self,
                        {
                            "ok": True,
                            "action": str(payload.get("action") or ""),
                            "result": result,
                        },
                    )
                elif self.command == "GET" and path == "/v1/events":
                    with server._event_lock:
                        if len(server._event_streams) >= server._MAX_EVENT_STREAMS:
                            _json(
                                self,
                                {"error": "too many concurrent event streams"},
                                status=503,
                            )
                            return
                        server._event_streams.add(self)
                    try:
                        server._serve_events(self)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with server._event_lock:
                            server._event_streams.discard(self)
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
                elif self.command == "GET" and path == "/":
                    server._serve_static(self, "index.html")
                elif self.command == "GET" and path.startswith("/assets/"):
                    server._serve_static(self, path[len("/assets/"):])
                elif self.command == "GET" and path == "/v1/commands":
                    from keyless.core.commands import commands_as_dicts

                    _json(self, {"categories": commands_as_dicts()})
                elif self.command == "GET" and path == "/v1/gaze/status":
                    _json(self, server.gaze_manager.gaze_status())
                elif self.command == "POST" and path == "/v1/gaze/start":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    try:
                        _json(
                            self,
                            server.gaze_manager.start_gaze(
                                camera_mode=str(payload.get("camera_mode", "auto")),
                                min_clicks=int(payload.get("min_clicks", 5)),
                                ridge_alpha=float(payload.get("ridge_alpha", 0.01)),
                                adapt_clicks=bool(payload.get("adapt_clicks", True)),
                            ),
                        )
                    except Exception as exc:
                        _json(self, {"error": str(exc)}, status=500)
                elif self.command == "POST" and path == "/v1/gaze/stop":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.stop_gaze())
                elif self.command == "POST" and path == "/v1/gaze/reset":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.reset_adaptation())
                elif self.command == "POST" and path == "/v1/gaze/undo":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.undo_last_click())
                elif self.command == "POST" and path == "/v1/gaze/overlay":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    _json(self, server.gaze_manager.set_overlay_enabled(bool(payload.get("enabled", True))))
                elif self.command == "POST" and path == "/v1/gaze/dev-border":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    _json(self, server.gaze_manager.set_dev_monitor_border(bool(payload.get("enabled", False))))
                elif self.command == "GET" and path == "/v1/llm/providers":
                    _json(self, server.gaze_manager.llm_providers_summary())
                elif self.command == "POST" and path == "/v1/llm/select":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    try:
                        _json(
                            self,
                            server.gaze_manager.select_llm_provider(str(payload.get("id", "auto"))),
                        )
                    except ValueError as exc:
                        _json(self, {"error": str(exc)}, status=400)
                elif self.command == "GET" and path == "/v1/llm/options":
                    try:
                        query = parse_qs(parsed.query)
                        preset_id = (
                            query.get("id", [""])[0]
                            or server.gaze_manager.llm_providers_summary()["active_id"]
                        )
                        force = query.get("force", ["0"])[0] in ("1", "true", "yes")
                        _json(self, server.gaze_manager.llm_provider_options(preset_id, force=force))
                    except Exception as exc:
                        _json(self, {"error": f"Provider probe failed: {exc}", "models": [], "efforts": []}, status=502)
                elif self.command == "GET" and path == "/v1/audio/devices":
                    _json(self, server.gaze_manager.audio_input_devices())
                elif self.command == "GET" and path == "/v1/cameras/devices":
                    _json(self, server.gaze_manager.camera_devices())
                elif self.command == "POST" and path == "/v1/llm/configure":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    _json(
                        self,
                        server.gaze_manager.configure_llm(
                            model=payload.get("model") if "model" in payload else None,
                            reasoning_effort=(
                                payload.get("reasoning_effort")
                                if "reasoning_effort" in payload
                                else None
                            ),
                        ),
                    )
                elif self.command == "GET" and path == "/v1/displays/rig":
                    _json(self, server.gaze_manager.get_display_rig())
                elif self.command == "POST" and path == "/v1/displays/rig":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    try:
                        _json(self, server.gaze_manager.save_display_rig(payload))
                    except Exception as exc:
                        _json(self, {"error": str(exc)}, status=400)
                elif self.command == "POST" and path == "/v1/displays/rig/preset":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    _json(
                        self,
                        server.gaze_manager.apply_rig_preset(
                            str(payload.get("preset", "flat")),
                            gap_mm=float(payload.get("gap_mm", 25.0)),
                            distance_mm=float(payload.get("distance_mm", 600.0)),
                        ),
                    )
                elif self.command == "GET" and path == "/v1/calibration":
                    _json(self, server.gaze_manager.calibration_status())
                elif self.command == "POST" and path == "/v1/calibration/start":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    try:
                        _json(
                            self,
                            server.gaze_manager.start_calibration(
                                connector=payload.get("connector") or None,
                                use_overlay=bool(payload.get("use_overlay", True)),
                            ),
                        )
                    except Exception as exc:
                        _json(self, {"error": str(exc)}, status=500)
                elif self.command == "POST" and path == "/v1/calibration/cancel":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.cancel_calibration())
                elif self.command == "POST" and path == "/v1/calibration/forget":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.forget_calibration())
                elif self.command == "GET" and path == "/v1/calibration/stream":
                    with server._event_lock:
                        if len(server._event_streams) >= server._MAX_EVENT_STREAMS:
                            _json(self, {"error": "too many concurrent streams"}, status=503)
                            return
                        server._event_streams.add(self)
                    try:
                        server._serve_calibration_stream(self)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with server._event_lock:
                            server._event_streams.discard(self)
                elif self.command == "GET" and path == "/v1/gaze/stream":
                    with server._event_lock:
                        if len(server._event_streams) >= server._MAX_EVENT_STREAMS:
                            _json(self, {"error": "too many concurrent streams"}, status=503)
                            return
                        server._event_streams.add(self)
                    try:
                        server._serve_gaze_stream(self)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with server._event_lock:
                            server._event_streams.discard(self)
                elif self.command == "GET" and path == "/v1/sessions":
                    _json(self, server.gaze_manager.list_sessions())
                elif self.command == "GET" and path == "/v1/sessions/recall":
                    _json(self, server.gaze_manager.recall_summary())
                elif self.command == "POST" and path == "/v1/sessions/sample":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.create_sample_session())
                elif self.command == "POST" and path == "/v1/sessions/record/start":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    try:
                        _json(
                            self,
                            server.gaze_manager.start_recording(
                                save_screenshots=bool(payload.get("save_screenshots", True)),
                                redact_text=bool(payload.get("redact_text", False)),
                                use_ocr=bool(payload.get("use_ocr", True)),
                            ),
                        )
                    except PermissionError as exc:
                        from keyless.web.runtime import MANUAL_INPUT_CMD

                        _json(
                            self,
                            {
                                "error": str(exc),
                                "needs_input_permission": True,
                                "manual_command": MANUAL_INPUT_CMD,
                            },
                            status=403,
                        )
                    except Exception as exc:
                        _json(self, {"error": str(exc)}, status=500)
                elif self.command == "POST" and path == "/v1/sessions/record/stop":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.stop_recording())
                elif self.command == "POST" and path == "/v1/sessions/export":
                    payload = server._read_json_body(self)
                    if payload is None:
                        return
                    try:
                        _json(
                            self,
                            server.gaze_manager.export_split(
                                out_dir=payload.get("out_dir"),
                                seed=int(payload.get("seed", 42)),
                            ),
                        )
                    except Exception as exc:
                        _json(self, {"error": str(exc)}, status=400)
                elif self.command == "POST" and path == "/v1/permissions/input":
                    server._read_json_body(self)
                    _json(self, server.gaze_manager.grant_input_permission())
                elif path.startswith("/v1/sessions/"):
                    parts = [p for p in path[len("/v1/sessions/"):].split("/") if p]
                    try:
                        if self.command == "GET" and len(parts) == 1:
                            _json(self, server.gaze_manager.get_session(parts[0]))
                        elif self.command == "DELETE" and len(parts) == 1:
                            _json(self, server.gaze_manager.remove_session(parts[0]))
                        elif self.command == "GET" and len(parts) == 3 and parts[1] == "blobs":
                            blob = server.gaze_manager.read_blob(parts[0], parts[2])
                            ctype, _ = mimetypes.guess_type(parts[2])
                            self.send_response(200)
                            self.send_header("Content-Type", ctype or "application/octet-stream")
                            self.send_header("Content-Length", str(len(blob)))
                            self.end_headers()
                            self.wfile.write(blob)
                        elif self.command == "PATCH" and len(parts) == 3 and parts[1] == "events":
                            payload = server._read_json_body(self)
                            if payload is None:
                                return
                            _json(self, server.gaze_manager.patch_event(parts[0], int(parts[2]), payload))
                        elif self.command == "DELETE" and len(parts) == 3 and parts[1] == "events":
                            _json(self, server.gaze_manager.remove_event(parts[0], int(parts[2])))
                        else:
                            _json(self, {"error": f"unknown route {path}"}, status=404)
                    except FileNotFoundError as exc:
                        _json(self, {"error": str(exc)}, status=404)
                    except (ValueError, IndexError) as exc:
                        _json(self, {"error": str(exc)}, status=400)
                else:
                    _json(self, {"error": f"unknown route {path}"}, status=404)

            do_GET = _route  # type: ignore[assignment]
            do_POST = _route  # type: ignore[assignment]
            do_PATCH = _route  # type: ignore[assignment]
            do_DELETE = _route  # type: ignore[assignment]

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
        self._events_stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._gaze_manager is not None:
            try:
                self._gaze_manager.shutdown()
            except Exception:
                pass
            self._gaze_manager = None
        try:
            self.controller.quit()
        except Exception:
            pass
