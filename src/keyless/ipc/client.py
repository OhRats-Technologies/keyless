"""Typed stdlib client for the loopback control plane.

Every first-party shell talks to the engine through these methods instead of
re-implementing HTTP. ``HttpSessionController`` adapts the client to the
``SessionController`` protocol used by the Qt window.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from keyless.config.paths import AppPaths, current_paths
from keyless.core.lifecycle import (
    DaemonFailureReport,
    SessionState,
    SessionStatus,
    inspect_daemon_error,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8766"


class ControlError(RuntimeError):
    """An HTTP or connection failure from the control plane."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: dict[str, Any] | None = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}
        self.url = url


@dataclass(frozen=True)
class StatusInfo:
    state: str
    detail: str = ""


@dataclass(frozen=True)
class ConfigPatchResult:
    config: dict[str, Any]
    restart_required: bool
    changed: list[str]


class ControlClient:
    """Blocking JSON client for ``keyless serve``."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self, method: str, path: str, payload: object | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            parsed = self._parse(body)
            message = str(parsed.get("error") or f"HTTP {exc.code}")
            raise ControlError(
                message, status=exc.code, payload=parsed, url=url
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ControlError(f"control plane unreachable: {reason}", url=url) from exc
        parsed = self._parse(body)
        if not isinstance(parsed, dict):
            raise ControlError("expected a JSON object", url=url)
        return parsed

    @staticmethod
    def _parse(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body)
        except ValueError as exc:
            raise ControlError("invalid JSON from control plane") from exc
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _status_info(body: dict[str, Any]) -> StatusInfo:
        return StatusInfo(state=str(body.get("state", "stopped")), detail=str(body.get("detail", "")))

    def status(self) -> StatusInfo:
        return self._status_info(self._request("GET", "/v1/status"))

    def get_config(self) -> dict[str, Any]:
        return self._request("GET", "/v1/config")

    def config_schema(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/v1/config/schema")
        settings = body.get("settings", [])
        return [item for item in settings if isinstance(item, dict)]

    def patch_config(self, patch: dict[str, object]) -> ConfigPatchResult:
        body = self._request("POST", "/v1/config", patch)
        config = body.get("config", {})
        return ConfigPatchResult(
            config=config if isinstance(config, dict) else {},
            restart_required=bool(body.get("restart_required", False)),
            changed=[str(key) for key in body.get("changed", [])],
        )

    def setup(self) -> dict[str, Any]:
        return self._request("GET", "/v1/setup")

    def ui_state(self) -> dict[str, Any]:
        return self._request("GET", "/v1/ui/state")

    def ui_action(self, action: str, arg: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if arg is not None:
            payload["arg"] = arg
        return self._request("POST", "/v1/ui/action", payload)

    def models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/models")

    def events(self):
        """Yield parsed ``state`` change payloads from ``GET /v1/events`` (SSE).

        The stream is long-lived; callers iterate it on a worker thread and
        refetch full state on each event. Raises ``ControlError`` when the
        stream cannot be opened or breaks, so callers can fall back to polling.
        """
        url = f"{self.base_url}/v1/events"
        request = urllib.request.Request(
            url, headers={"Accept": "text/event-stream"}, method="GET"
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise ControlError(f"events stream failed: HTTP {exc.code}", status=exc.code, url=url) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ControlError(f"events stream unreachable: {reason}", url=url) from exc
        data_lines: list[str] = []
        try:
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    if data_lines:
                        try:
                            value = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            value = None
                        data_lines = []
                        if isinstance(value, dict):
                            yield value
        except (OSError, ValueError) as exc:
            raise ControlError(f"events stream closed: {exc}", url=url) from exc
        finally:
            response.close()

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"limit": max(1, min(200, int(limit)))})
        body = self._request("GET", f"/v1/history?{query}")
        actions = body.get("actions", [])
        return [item for item in actions if isinstance(item, dict)]

    def start(self) -> StatusInfo:
        return self._status_info(self._request("POST", "/v1/start"))

    def pause(self) -> StatusInfo:
        return self._status_info(self._request("POST", "/v1/pause"))

    def ping(self) -> bool:
        try:
            self.status()
        except ControlError:
            return False
        return True


_STATE_MAP = {
    "stopped": SessionState.STOPPED,
    "listening": SessionState.LISTENING,
    "paused": SessionState.PAUSED,
    "error": SessionState.ERROR,
}


class HttpSessionController:
    """Drive the daemon through the control plane instead of spawning a child."""

    def __init__(self, client: ControlClient, *, paths: AppPaths | None = None) -> None:
        self.client = client
        self.paths = paths or current_paths()

    @staticmethod
    def _to_status(info: StatusInfo) -> SessionStatus:
        state = _STATE_MAP.get(info.state.lower())
        if state is None:
            return SessionStatus(
                SessionState.ERROR,
                info.detail or f"Unknown daemon state {info.state!r}.",
            )
        return SessionStatus(state, info.detail)

    @property
    def status(self) -> SessionStatus:
        try:
            return self._to_status(self.client.status())
        except ControlError as exc:
            return SessionStatus(SessionState.ERROR, str(exc))

    def start(self) -> SessionStatus:
        try:
            return self._to_status(self.client.start())
        except ControlError as exc:
            return SessionStatus(SessionState.ERROR, str(exc))

    def pause(self) -> SessionStatus:
        try:
            return self._to_status(self.client.pause())
        except ControlError as exc:
            return SessionStatus(SessionState.ERROR, str(exc))

    def quit(self) -> None:
        try:
            self.client.pause()
        except ControlError:
            pass

    @property
    def failure_report(self) -> DaemonFailureReport | None:
        if self.status.state is not SessionState.ERROR:
            return None
        return inspect_daemon_error(self.paths.logs_dir / "daemon.log", 1)
