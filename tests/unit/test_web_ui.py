"""End-to-end HTTP tests for the unified Keyless local web UI and Gaze/Session API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.core.lifecycle import NullController
from keyless.core.ui_contract import build_ui_state
from keyless.ipc.control import ControlServer

LOOPBACK = "127.0.0.1"


@pytest.fixture
def web_server(tmp_path: Path):
    paths: AppPaths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    srv = ControlServer(paths=paths)
    srv.controller = NullController()
    srv.ui_contract_builder = build_ui_state
    port = srv.start(host=LOOPBACK, port=0)
    try:
        yield srv, f"http://{LOOPBACK}:{port}", paths
    finally:
        srv.stop()


def _req(base: str, method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_static_web_app_assets_and_path_traversal_guard(web_server) -> None:
    _srv, base, _paths = web_server
    st, ctype, html = _req(base, "GET", "/")
    assert st == 200
    assert "text/html" in ctype
    assert b"Keyless" in html
    assert b"/assets/app.js" in html

    st, ctype, css = _req(base, "GET", "/assets/styles.css")
    assert st == 200
    assert "text/css" in ctype
    assert b"#efede5" in css and b"#c95b2b" in css and b"#634936" in css

    st, ctype, js = _req(base, "GET", "/assets/app.js")
    assert st == 200
    assert "javascript" in ctype
    assert b"/v1/gaze/stream" in js

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _req(base, "GET", "/assets/../../pyproject.toml")
    assert exc_info.value.code in (403, 404)


def test_gaze_status_commands_and_session_curation_http_roundtrip(web_server) -> None:
    _srv, base, _paths = web_server

    st, _, raw = _req(base, "GET", "/v1/gaze/status")
    assert st == 200
    gaze = json.loads(raw)
    assert gaze["running"] is False
    assert "display" in gaze and "environment" in gaze

    st, _, raw = _req(base, "GET", "/v1/commands")
    assert st == 200
    assert len(json.loads(raw)["categories"]) >= 3

    st, _, raw = _req(base, "POST", "/v1/sessions/sample", {})
    assert st == 200
    sid = json.loads(raw)["session_id"]

    st, _, raw = _req(base, "GET", "/v1/sessions")
    assert st == 200
    sessions = json.loads(raw)["sessions"]
    assert len(sessions) == 1 and sessions[0]["session_id"] == sid

    st, _, raw = _req(base, "GET", f"/v1/sessions/{sid}")
    assert st == 200
    detail = json.loads(raw)
    assert len(detail["events"]) == 4

    st, _, raw = _req(
        base,
        "PATCH",
        f"/v1/sessions/{sid}/events/3",
        {"target": "btn-settings", "accepted": True},
    )
    assert st == 200
    patched = json.loads(raw)["event"]
    assert patched["interaction"]["target"] == "btn-settings"
    assert patched["interaction"]["accepted"] is True

    st, _, raw = _req(base, "DELETE", f"/v1/sessions/{sid}/events/3")
    assert st == 200
    assert json.loads(raw)["remaining"] == 3

    st, _, raw = _req(base, "GET", "/v1/sessions/recall")
    assert st == 200
    recall = json.loads(raw)
    assert recall["clicks"] == 3 and recall["hits"] == 3 and recall["gate_met"] is True

    st, _, raw = _req(base, "POST", "/v1/sessions/export", {"seed": 7})
    assert st == 200
    exported = json.loads(raw)
    assert Path(exported["out_dir"]).exists()

    st, _, raw = _req(base, "DELETE", f"/v1/sessions/{sid}")
    assert st == 200
    assert json.loads(raw)["deleted"] == sid


def test_open_browser_url_prefers_xdg_open_on_linux(monkeypatch) -> None:
    from keyless import web

    launched: list[list[str]] = []
    monkeypatch.setattr(web.sys, "platform", "linux")
    monkeypatch.setattr(web.shutil, "which", lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None)

    def _fake_popen(cmd, **_kwargs):
        launched.append(cmd)
        return object()

    monkeypatch.setattr(web.subprocess, "Popen", _fake_popen)
    target = f"http://{LOOPBACK}:8766"
    assert web.open_browser_url(target) is True
    assert launched == [["/usr/bin/xdg-open", target]]


def test_keyless_ui_cli_starts_and_stops_cleanly(monkeypatch) -> None:
    from typer.testing import CliRunner

    from keyless.main import app

    def _interrupt_immediately(_secs: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("keyless.main.time.sleep", _interrupt_immediately)
    runner = CliRunner()
    res = runner.invoke(app, ["ui", "--port", "0", "--no-open", "--no-tray"])
    assert res.exit_code == 0, res.output
    assert "Keyless web UI listening at" in res.output


def test_displays_rig_presets_schema_host_and_overlay_toggle(web_server) -> None:
    _srv, base, _paths = web_server

    # Schema exposes active host platform for OS-filtered Settings
    st, _, raw = _req(base, "GET", "/v1/config/schema")
    assert st == 200
    schema = json.loads(raw)
    assert schema["host"] in ("linux", "macos", "windows")
    ov_spec = next(s for s in schema["settings"] if s["key"] == "gaze_cursor_overlay")
    assert "linux" in ov_spec["platforms"] and "macos" in ov_spec["platforms"]
    assert "macOS" not in ov_spec["label"]

    # 3D Display Rig GET / preset / POST persistence
    st, _, raw = _req(base, "GET", "/v1/displays/rig")
    assert st == 200
    rig = json.loads(raw)
    assert len(rig["displays"]) >= 1

    st, _, raw = _req(base, "POST", "/v1/displays/rig/preset", {"preset": "curved"})
    assert st == 200
    curved = json.loads(raw)
    assert len(curved["displays"]) == len(rig["displays"])

    curved["displays"][0]["yaw_deg"] = -22.5
    st, _, raw = _req(base, "POST", "/v1/displays/rig", curved)
    assert st == 200
    saved = json.loads(raw)
    assert saved["displays"][0]["yaw_deg"] == pytest.approx(-22.5)

    # Overlay toggle persists config cleanly
    st, _, raw = _req(base, "POST", "/v1/gaze/overlay", {"enabled": False})
    assert st == 200
    assert json.loads(raw)["overlay_active"] is False


def test_developer_active_monitor_border_toggle_and_auto_rig_preset(web_server) -> None:
    _srv, base, _paths = web_server

    st, _, raw = _req(base, "GET", "/v1/config/schema")
    assert st == 200
    schema = json.loads(raw)
    dev_spec = next(s for s in schema["settings"] if s["key"] == "dev_active_monitor_border")
    assert dev_spec["group"] == "Developer"

    st, _, raw = _req(base, "POST", "/v1/gaze/dev-border", {"enabled": True})
    assert st == 200

    st, _, raw = _req(base, "GET", "/v1/config")
    assert st == 200
    assert json.loads(raw)["dev_active_monitor_border"] is True

    st, _, raw = _req(base, "POST", "/v1/displays/rig/preset", {"preset": "auto"})
    assert st == 200
    assert json.loads(raw)["auto_rig"] is True


def test_viewport3d_asset_and_fast_stream_tick(web_server) -> None:
    srv, base, _paths = web_server
    st, ctype, js = _req(base, "GET", "/assets/viewport3d.js")
    assert st == 200
    assert "javascript" in ctype
    assert b"export class Viewport3D" in js

    tick, ver = srv.gaze_manager.gaze_stream_tick(-1)
    assert tick["running"] is False
    assert "prediction" in tick


def test_llm_providers_auto_detect_and_one_click_select(web_server) -> None:
    _srv, base, _paths = web_server
    st, _, raw = _req(base, "GET", "/v1/llm/providers")
    assert st == 200
    data = json.loads(raw)
    ids = {p["id"] for p in data["providers"]}
    assert {"acp:opencode", "acp:claude-acp", "acp:codex-acp", "ollama", "gemini"} <= ids

    st, _, raw = _req(base, "POST", "/v1/llm/select", {"id": "acp:opencode"})
    assert st == 200
    selected = json.loads(raw)
    assert selected["active_id"] == "acp:opencode"
    assert selected["config"]["llm_provider"] == "acp"
    assert selected["config"]["acp_agent_id"] == "opencode"


def test_llm_configure_model_and_reasoning_effort_and_no_gemini_cli(web_server) -> None:
    _srv, base, _paths = web_server
    st, _, raw = _req(base, "GET", "/v1/llm/providers")
    assert st == 200
    ids = {p["id"] for p in json.loads(raw)["providers"]}
    assert "acp:agy" not in ids
    assert "acp:gemini" not in ids

    st, _, raw = _req(base, "POST", "/v1/llm/select", {"id": "acp:opencode"})
    assert st == 200

    st, _, raw = _req(
        base,
        "POST",
        "/v1/llm/configure",
        {"model": "gpt-5.1", "reasoning_effort": "low"},
    )
    assert st == 200
    res = json.loads(raw)
    assert res["active_model"] == "gpt-5.1"
    assert res["active_effort"] == "low"
    assert res["config"]["llm_model"] == "gpt-5.1"
    assert res["config"]["llm_reasoning_effort"] == "low"


def test_device_endpoints_enumerate_with_explicit_error_state(web_server) -> None:
    _srv, base, _paths = web_server
    st, _, raw = _req(base, "GET", "/v1/audio/devices")
    assert st == 200
    audio = json.loads(raw)
    assert isinstance(audio["devices"], list)
    assert isinstance(audio["error"], str)

    st, _, raw = _req(base, "GET", "/v1/cameras/devices")
    assert st == 200
    cameras = json.loads(raw)
    assert isinstance(cameras["devices"], list)
    assert isinstance(cameras["error"], str)
    assert set(cameras["selected"]) == {"cam1_by_id", "cam2_by_id"}


def test_llm_options_returns_json_on_probe_failure(web_server) -> None:
    _srv, base, _paths = web_server
    try:
        st, _, raw = _req(base, "GET", "/v1/llm/options?id=acp:opencode")
    except urllib.error.HTTPError as exc:
        st, raw = exc.code, exc.read()
    assert st in (200, 502)
    data = json.loads(raw)
    assert isinstance(data["models"], list)
    assert isinstance(data["efforts"], list)
    assert isinstance(data.get("error", ""), str)


def test_config_schema_hides_retired_internal_keys(web_server) -> None:
    _srv, base, _paths = web_server
    st, _, raw = _req(base, "GET", "/v1/config/schema")
    assert st == 200
    settings = {s["key"]: s for s in json.loads(raw)["settings"]}
    for key in (
        "stt_model_id",
        "stt_quantization",
        "stt_provider",
        "skipped_update_version",
    ):
        assert settings[key]["hidden"] is True
