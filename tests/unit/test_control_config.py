"""End-to-end tests for the config HTTP endpoints."""

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


def _paths(tmp_path: Path):
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    return paths


@pytest.fixture()
def server(tmp_path: Path):
    paths = _paths(tmp_path)
    srv = ControlServer(paths=paths)
    srv.controller = NullController()
    port = srv.start(host="127.0.0.1", port=0)
    try:
        yield srv, paths, f"http://127.0.0.1:{port}"
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
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else {}


def test_schema_lists_every_setting(server) -> None:
    _, _, base = server
    status, body = _request("GET", f"{base}/v1/config/schema")
    assert status == 200
    assert body["schema_version"] == 1
    keys = {item["key"] for item in body["settings"]}
    assert keys == set(AppConfig.__dataclass_fields__)
    for item in body["settings"]:
        assert {"key", "value_type", "default", "label", "help_text", "group", "control", "options", "minimum", "maximum", "apply", "platforms"} <= set(item)


def test_post_valid_live_patch_persists_and_returns_changed(server) -> None:
    srv, paths, base = server
    status, body = _request("POST", f"{base}/v1/config", {"tts_enabled": True})
    assert status == 200
    assert body["changed"] == ["tts_enabled"]
    assert body["restart_required"] is False
    assert body["config"]["tts_enabled"] is True
    assert AppConfig.load(paths.settings_path).tts_enabled is True
    assert srv.controller.history == []


def test_post_restart_patch_sets_restart_required(server) -> None:
    _, paths, base = server
    status, body = _request("POST", f"{base}/v1/config", {"fps": 60})
    assert status == 200
    assert body["changed"] == ["fps"]
    assert body["restart_required"] is True
    assert AppConfig.load(paths.settings_path).fps == 60


def test_unchanged_values_are_not_reported(server) -> None:
    _, _, base = server
    status, body = _request("POST", f"{base}/v1/config", {"tts_enabled": False})
    assert status == 200
    assert body["changed"] == []
    assert body["restart_required"] is False


def test_post_invalid_value_returns_400_and_leaves_disk_unchanged(server) -> None:
    _, paths, base = server
    _request("POST", f"{base}/v1/config", {"fps": 24})
    before = AppConfig.load(paths.settings_path).fps
    status, body = _request("POST", f"{base}/v1/config", {"fps": "fast"})
    assert status == 400
    assert "fps" in body["errors"]
    assert AppConfig.load(paths.settings_path).fps == before == 24


def test_post_nan_returns_400_and_leaves_disk_unchanged(server) -> None:
    _, paths, base = server
    # json.dumps emits a bare NaN token by default; the server must reject it.
    status, body = _request("POST", f"{base}/v1/config", {"blink_threshold": float("nan")})
    assert status == 400
    assert "blink_threshold" in body["errors"]
    assert not paths.settings_path.exists()


def test_post_unknown_key_returns_400(server) -> None:
    _, paths, base = server
    status, body = _request("POST", f"{base}/v1/config", {"bogus_setting": 1})
    assert status == 400
    assert body["errors"] == {"bogus_setting": "unknown setting"}
    assert not paths.settings_path.exists()


def test_post_invalid_json_returns_400(server) -> None:
    _, _, base = server
    req = urllib.request.Request(
        f"{base}/v1/config", data=b"{not json", headers={"Content-Type": "application/json"}, method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(req, timeout=5)
    assert info.value.code == 400


def test_post_non_object_returns_400(server) -> None:
    _, _, base = server
    status, body = _request("POST", f"{base}/v1/config", ["tts_enabled"])
    assert status == 400
    assert "expected a JSON object" in body["error"]


def test_get_config_returns_config_object(server) -> None:
    _, _, base = server
    status, body = _request("GET", f"{base}/v1/config")
    assert status == 200
    assert set(body) == set(AppConfig.__dataclass_fields__)
