"""Tests for the typed control-plane client and the HTTP session controller."""

from __future__ import annotations

from pathlib import Path

import pytest

from keyless.config.paths import HOME_ENV, default_paths
from keyless.core.lifecycle import NullController, SessionState, SessionStatus
from keyless.ipc.client import (
    ConfigPatchResult,
    ControlClient,
    ControlError,
    HttpSessionController,
    StatusInfo,
)
from keyless.ipc.control import ControlServer


def _paths(tmp_path: Path):
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    return paths


@pytest.fixture()
def control(tmp_path: Path):
    paths = _paths(tmp_path)
    srv = ControlServer(paths=paths)
    controller = NullController()
    srv.controller = controller
    port = srv.start(host="127.0.0.1", port=0)
    client = ControlClient(f"http://127.0.0.1:{port}", timeout=5.0)
    try:
        yield srv, controller, paths, client
    finally:
        srv.stop()


def test_status_roundtrip(control) -> None:
    _, controller, _, client = control
    info = client.status()
    assert isinstance(info, StatusInfo)
    assert info.state == "stopped"
    started = client.start()
    assert started.state == "listening"
    assert controller.history == ["start"]
    paused = client.pause()
    assert paused.state == "paused"


def test_config_roundtrip_and_patch(control) -> None:
    _, _, paths, client = control
    config = client.get_config()
    assert config["tts_enabled"] is False

    schema = client.config_schema()
    keys = {item["key"] for item in schema}
    assert "fps" in keys and "audio_device" in keys

    result = client.patch_config({"tts_enabled": True})
    assert isinstance(result, ConfigPatchResult)
    assert result.changed == ["tts_enabled"]
    assert result.restart_required is False
    assert result.config["tts_enabled"] is True

    restart = client.patch_config({"fps": 60})
    assert restart.restart_required is True
    assert restart.changed == ["fps"]
    assert client.get_config()["fps"] == 60


def test_patch_invalid_value_raises_control_error(control) -> None:
    _, _, paths, client = control
    with pytest.raises(ControlError) as info:
        client.patch_config({"fps": "fast"})
    assert info.value.status == 400
    assert "fps" in info.value.payload["errors"]


def test_history_setup_and_models(control) -> None:
    _, _, paths, client = control
    assert client.history(limit=5) == []
    setup = client.setup()
    assert setup["schema_version"] == 1
    assert "steps" in setup
    models = client.models()
    assert "model" in models
    assert models["model"]["id"]


def test_ping_true_and_false(control) -> None:
    _, _, _, client = control
    assert client.ping() is True
    offline = ControlClient("http://127.0.0.1:1", timeout=0.5)
    assert offline.ping() is False
    with pytest.raises(ControlError):
        offline.status()


def test_http_session_controller_maps_states(control) -> None:
    _, _, paths, client = control
    session = HttpSessionController(client, paths=paths)
    assert session.status.state is SessionState.STOPPED
    assert session.start().state is SessionState.LISTENING
    assert session.status.state is SessionState.LISTENING
    assert session.pause().state is SessionState.PAUSED
    session.quit()
    assert session.status.state is SessionState.PAUSED


def test_http_session_controller_reports_errors(control) -> None:
    srv, _, paths, client = control

    class ErrorController(NullController):
        @property
        def status(self) -> SessionStatus:
            return SessionStatus(SessionState.ERROR, "daemon crashed")

    srv.controller = ErrorController()
    (paths.logs_dir / "daemon.log").write_text(
        "boot\nRuntimeError: Failed to open V4L2 capture on /dev/video0\n"
    )
    session = HttpSessionController(client, paths=paths)
    assert session.status.state is SessionState.ERROR
    report = session.failure_report
    assert report is not None
    assert report.summary == "Camera is busy"


def test_http_session_controller_handles_offline_plane(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    session = HttpSessionController(ControlClient("http://127.0.0.1:1", timeout=0.5), paths=paths)
    status = session.status
    assert status.state is SessionState.ERROR
    assert "unreachable" in status.detail
