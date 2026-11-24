"""Control-plane tests for GET /v1/ui/state and POST /v1/ui/action."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from keyless.config.paths import HOME_ENV, default_paths
from keyless.config.schema import AppConfig
from keyless.core.lifecycle import NullController
from keyless.ipc.control import ControlServer
from keyless.ui.contract import build_ui_state


def _paths(tmp_path: Path):
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    return paths


def _start(tmp_path: Path, *, builder) -> tuple[ControlServer, Path, str]:
    paths = _paths(tmp_path)
    srv = ControlServer(paths=paths)
    srv.controller = NullController()
    srv.ui_contract_builder = builder
    port = srv.start(host="127.0.0.1", port=0)
    return srv, paths, f"http://127.0.0.1:{port}"


@pytest.fixture()
def ui_server(tmp_path: Path):
    srv, paths, base = _start(tmp_path, builder=build_ui_state)
    try:
        yield srv, paths, base
    finally:
        srv.stop()


def _request(method: str, url: str, payload: object | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else {}


def test_ui_state_requires_a_registered_builder(tmp_path: Path) -> None:
    srv, _, base = _start(tmp_path, builder=None)
    try:
        status, body = _request("GET", f"{base}/v1/ui/state")
        assert status == 503
        assert body["error"] == "ui contract builder not registered"
    finally:
        srv.stop()


def test_ui_state_returns_document(ui_server) -> None:
    _, _, base = ui_server
    status, body = _request("GET", f"{base}/v1/ui/state")
    assert status == 200
    assert body["schema_version"] == 1
    assert set(body["screens"]) == {"home", "activity", "settings", "setup"}
    home_rows = [row for section in body["screens"]["home"]["sections"] for row in section["rows"]]
    assert any(row["kind"] == "action" for row in home_rows)
    assert sum(row.get("style") == "primary" for row in home_rows) == 1


def test_action_start_and_pause(ui_server) -> None:
    srv, _, base = ui_server
    status, body = _request("POST", f"{base}/v1/ui/action", {"action": "start"})
    assert status == 200
    assert body["ok"] is True
    assert body["result"]["state"] == "listening"
    assert srv.controller.history == ["start"]

    status, body = _request("POST", f"{base}/v1/ui/action", {"action": "pause"})
    assert status == 200
    assert body["result"]["state"] == "paused"
    assert srv.controller.history == ["start", "pause"]


def test_action_set_config_valid_and_invalid(ui_server) -> None:
    _, paths, base = ui_server
    status, body = _request(
        "POST",
        f"{base}/v1/ui/action",
        {"action": "set_config", "arg": {"tts_enabled": True}},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["result"]["changed"] == ["tts_enabled"]
    assert AppConfig.load(paths.settings_path).tts_enabled is True

    status, body = _request(
        "POST",
        f"{base}/v1/ui/action",
        {"action": "set_config", "arg": {"fps": "fast"}},
    )
    assert status == 400
    assert body["ok"] is False
    assert "fps" in body["errors"]
    assert AppConfig.load(paths.settings_path).fps == 30


def test_action_setup_and_select_camera(ui_server) -> None:
    _, paths, base = ui_server
    status, body = _request(
        "POST",
        f"{base}/v1/ui/action",
        {"action": "setup", "arg": {"action": "go_to", "step": "devices"}},
    )
    assert status == 200
    assert body["result"]["state"]["step"] == "devices"

    status, body = _request(
        "POST",
        f"{base}/v1/ui/action",
        {"action": "select_camera", "arg": {"camera_mode": "mono"}},
    )
    assert status == 200
    assert body["result"]["changed"] == ["camera_mode"]
    assert AppConfig.load(paths.settings_path).camera_mode == "mono"

    status, body = _request(
        "POST",
        f"{base}/v1/ui/action",
        {"action": "select_camera", "arg": {"bogus": 1}},
    )
    assert status == 400
    assert body["errors"] == {"bogus": "unknown setting"}


def test_action_unknown_and_bad_json(ui_server) -> None:
    _, _, base = ui_server
    status, body = _request(
        "POST", f"{base}/v1/ui/action", {"action": "does_not_exist"}
    )
    assert status == 400
    assert body["ok"] is False
    assert "unknown action" in body["error"]

    status, body = _request("POST", f"{base}/v1/ui/action", {"action": "set_config"})
    assert status == 400
    assert "arg" in body["error"]


def test_action_calibrate_is_a_disabled_notice(ui_server) -> None:
    _, _, base = ui_server
    status, body = _request(
        "POST", f"{base}/v1/ui/action", {"action": "calibrate"}
    )
    assert status == 200
    assert body["result"]["disabled"] is True
    assert body["result"]["started"] is False


def test_action_diagnostics_returns_doctor(ui_server) -> None:
    _, _, base = ui_server
    status, body = _request(
        "POST", f"{base}/v1/ui/action", {"action": "diagnostics"}
    )
    assert status == 200
    assert "capabilities" in body["result"]
    assert "cameras" in body["result"]


def test_action_allow_unknown_permission_is_rejected(ui_server) -> None:
    _, _, base = ui_server
    status, body = _request(
        "POST", f"{base}/v1/ui/action",
        {"action": "allow", "arg": {"permission": "no_such_permission"}},
    )
    assert status == 400
    assert body["ok"] is False
    assert "unknown permission" in body["error"]

    status, body = _request("POST", f"{base}/v1/ui/action", {"action": "allow"})
    assert status == 400
    assert "arg" in body["error"]


def test_action_models_cancel_without_job_is_inert(ui_server) -> None:
    _, _, base = ui_server
    status, body = _request(
        "POST", f"{base}/v1/ui/action", {"action": "models_cancel"}
    )
    assert status == 200
    assert body["ok"] is True
    assert body["result"]["cancelled"] is False


def test_events_stream_pushes_state_events(ui_server) -> None:
    """GET /v1/events emits an SSE ``state`` event carrying the change snapshot."""
    _, _, base = ui_server
    with urllib.request.urlopen(f"{base}/v1/events", timeout=10) as response:
        assert response.status == 200
        assert response.headers.get("Content-Type") == "text/event-stream"
        event_line = response.readline().decode()
        data_line = response.readline().decode()
        response.readline()  # blank separator
    assert event_line.strip() == "event: state"
    assert data_line.startswith("data: ")
    payload = json.loads(data_line[len("data: ") :])
    assert {"status", "capabilities", "permissions", "setup", "model"} <= set(payload)


def test_events_streams_are_bounded(ui_server) -> None:
    """Beyond the cap, new streams get a structured 503 instead of piling up."""
    srv, _, base = ui_server
    held = []
    try:
        for _ in range(srv._MAX_EVENT_STREAMS):
            held.append(urllib.request.urlopen(f"{base}/v1/events", timeout=10))
        status, body = _request("GET", f"{base}/v1/events")
        assert status == 503
        assert "event streams" in body["error"]
    finally:
        for stream in held:
            stream.close()
