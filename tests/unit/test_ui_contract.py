"""Schema-v1 UI contract: row kinds, capability gating, and one primary CTA."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from keyless.config.schema import AppConfig
from keyless.config.specs import SETTING_SPECS
from keyless.ui.contract import (
    ROW_KINDS,
    SCHEMA_VERSION,
    ActionRow,
    PermissionRow,
    PickerRow,
    ProgressRow,
    StatusRow,
    TextRow,
    build_ui_state,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ui"

_NAMES = ("cameras", "audio", "stt", "llm_provider", "permissions")


def _capabilities(available: bool) -> dict:
    status = "available" if available else "unavailable"
    return {
        "platform": "linux",
        "paths": {},
        "capabilities": {
            name: {"status": status, "detail": "", "permission": None, "action": ""}
            for name in _NAMES
            if name != "permissions"
        },
        "permissions": (
            {"camera": {"state": "granted", "detail": "", "action": ""}}
            if available
            else {}
        ),
    }


def _doctor() -> dict:
    return {
        "cameras": {
            "discovered": [
                {"id": "cam1", "name": "Logitech Webcam", "path": "/dev/video0"},
                {"id": "cam2", "name": "Studio Cam", "path": "/dev/video1"},
            ]
        },
        "audio": {"inputs": [{"name": "Razer Seiren Mini"}, {"name": "Webcam"}]},
    }


def _state(**overrides) -> dict:
    kwargs = dict(
        capabilities=_capabilities(True),
        doctor=_doctor(),
        config=asdict(AppConfig()),
        history=[],
        model={
            "id": "parakeet",
            "name": "Parakeet Unified EN",
            "display_name": "Parakeet Unified EN",
            "ready": True,
            "job": None,
        },
        setup={
            "steps": ["welcome", "devices", "calibration", "ready"],
            "state": {"step": "welcome", "completed": []},
            "complete": False,
        },
        status={"state": "stopped", "detail": ""},
        specs=[spec.as_dict() for spec in SETTING_SPECS],
    )
    kwargs.update(overrides)
    return build_ui_state(**kwargs)


def _rows(screen: dict) -> list[dict]:
    return [row for section in screen["sections"] for row in section["rows"]]


def test_document_shape_and_version() -> None:
    document = _state()
    assert document["schema_version"] == SCHEMA_VERSION == 1
    assert set(document["screens"]) == {"home", "activity", "settings", "setup"}
    assert set(document["actions"]) >= {
        "start",
        "pause",
        "set_config",
        "setup",
        "select_camera",
    }
    assert document["screens"]["setup"]["steps"] == [
        "welcome",
        "devices",
        "calibration",
        "ready",
    ]


def test_six_row_kinds_serialize_with_kind() -> None:
    rows = [
        StatusRow("Engine", "ok", "ready"),
        ActionRow("Start listening", "start", "primary"),
        PermissionRow("camera", "Camera", "granted"),
        ProgressRow("Downloading", 0.5),
        PickerRow("Camera", ({"label": "Cam", "value": "/dev/video0"},), "/dev/video0"),
        TextRow("Speech", "ready"),
    ]
    kinds = [row.as_dict()["kind"] for row in rows]
    assert kinds == ["status", "action", "permission", "progress", "picker", "text"]
    assert rows[0].as_dict()["state"] == "ok"
    assert rows[1].as_dict()["style"] == "primary"
    assert rows[2].as_dict()["needs_restart"] is False
    assert rows[3].as_dict()["fraction"] == 0.5
    assert rows[4].as_dict()["selected"] == "/dev/video0"
    assert rows[5].as_dict()["detail"] == "ready"


def test_home_has_exactly_one_primary_action() -> None:
    for status in ("stopped", "paused", "listening", "error"):
        home = _state(status={"state": status, "detail": ""})["screens"]["home"]
        actions = [row for row in _rows(home) if row["kind"] == "action"]
        assert len([row for row in actions if row["style"] == "primary"]) == 1
        assert len([row for row in actions if row["style"] == "primary"]) == len(actions)


def test_all_capabilities_present_render_pickers_and_readiness() -> None:
    home = _state()["screens"]["home"]
    kinds = {row["kind"] for row in _rows(home)}
    assert {"status", "action", "picker"} <= kinds
    titles = {row["title"] for row in _rows(home) if row["kind"] == "picker"}
    assert {"Camera", "Microphone", "Provider"} <= titles
    assert any(row["kind"] == "permission" for row in _rows(_state()["screens"]["setup"]))


def test_all_capabilities_absent_omit_rows_never_disable_imitations() -> None:
    document = _state(capabilities=_capabilities(False), doctor={})
    home = document["screens"]["home"]
    rows = _rows(home)
    assert not [row for row in rows if row["kind"] == "picker"]
    assert not [row for row in rows if row["kind"] == "permission"]
    assert not [row for row in rows if row["kind"] == "progress"]
    actions = [row for row in rows if row["kind"] == "action"]
    assert len([row for row in actions if row["style"] == "primary"]) == 1
    assert all(row["enabled"] for row in actions)
    # No readiness row for a capability the engine says is unavailable.
    status_titles = {row["title"] for row in rows if row["kind"] == "status"}
    assert not ({"Camera", "Microphone", "Permissions"} & status_titles)
    # No gaze/calibration launch row anywhere.
    assert not [row for row in rows if row.get("action") == "calibrate"]


def test_active_model_job_adds_progress_row() -> None:
    model = {
        "id": "parakeet",
        "display_name": "Parakeet Unified EN",
        "ready": False,
        "job": {"state": "downloading", "percent": 42.0},
    }
    home = _state(model=model)["screens"]["home"]
    progress = [row for row in _rows(home) if row["kind"] == "progress"]
    assert len(progress) == 1
    assert progress[0]["fraction"] == 0.42
    assert progress[0]["indeterminate"] is False


def test_recent_activity_is_capped_at_three_on_home() -> None:
    history = [
        {"transcript": f"command {index}", "target": f"target {index}"}
        for index in range(5)
    ]
    home = _state(history=history)["screens"]["home"]
    recent = [row for row in _rows(home) if row["kind"] == "text"]
    assert len(recent) == 3
    assert recent[0]["title"] == "command 0"


def test_activity_and_settings_are_honest() -> None:
    document = _state()
    activity = document["screens"]["activity"]
    assert _rows(activity)[0]["title"] == "No activity yet"

    settings = document["screens"]["settings"]
    settings_rows = _rows(settings)
    assert any(row["kind"] == "action" and row["action"] == "diagnostics" for row in settings_rows)
    labels = [row["title"] for row in settings_rows]
    assert "Push-to-talk key" in labels
    assert "Camera setup" in labels


def test_setup_marks_current_and_completed_steps() -> None:
    setup = {
        "steps": ["welcome", "devices", "calibration", "ready"],
        "state": {"step": "devices", "completed": ["welcome"]},
        "complete": False,
    }
    screen = _state(setup=setup)["screens"]["setup"]
    assert screen["current"] == "devices"
    steps = _rows(screen)
    by_title = {row["title"]: row for row in steps if row["kind"] == "status"}
    assert by_title["Welcome"]["state"] == "ok"
    assert by_title["Devices"]["detail"] == "Current step"
    assert by_title["Ready"]["state"] == "idle"


# ---------------------------------------------------------------------------
# Golden contract fixtures (shared with the Swift parity tests).
# ---------------------------------------------------------------------------


def _fixture_inputs(available: bool) -> dict:
    return dict(
        capabilities=_capabilities(available),
        doctor=_doctor() if available else {},
        config=asdict(AppConfig()),
        history=[{"transcript": "click the red button", "target": "button"}] if available else [],
        model=(
            {
                "id": "parakeet",
                "name": "Parakeet Unified EN",
                "display_name": "Parakeet Unified EN",
                "ready": True,
                "job": None,
            }
            if available
            else {}
        ),
        setup={
            "steps": ["welcome", "devices", "permissions", "ready"],
            "state": {"step": "welcome", "completed": []},
            "complete": False,
        },
        status={"state": "stopped", "detail": ""},
        specs=[spec.as_dict() for spec in SETTING_SPECS],
    )


def test_golden_fixture_all_capabilities() -> None:
    golden = json.loads((FIXTURES / "contract_full.json").read_text())
    assert build_ui_state(**_fixture_inputs(True)) == golden


def test_golden_fixture_no_capabilities() -> None:
    golden = json.loads((FIXTURES / "contract_cold.json").read_text())
    assert build_ui_state(**_fixture_inputs(False)) == golden


def regenerate_golden_fixtures() -> None:
    """Rewrite the golden fixtures from the engine builder (run manually after
    intentional contract changes; the parity tests then pin the new shape)."""
    for name, available in (
        ("contract_full.json", True),
        ("contract_cold.json", False),
    ):
        payload = build_ui_state(**_fixture_inputs(available))
        (FIXTURES / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def test_golden_fixtures_use_closed_row_kind_set() -> None:
    allowed = set(ROW_KINDS)
    for name in ("contract_full.json", "contract_cold.json"):
        document = json.loads((FIXTURES / name).read_text())
        for screen in document["screens"].values():
            for row in _rows(screen):
                assert row["kind"] in allowed, (name, row["kind"])


def test_golden_cold_fixture_omits_capability_gated_rows() -> None:
    cold = json.loads((FIXTURES / "contract_cold.json").read_text())
    home_rows = _rows(cold["screens"]["home"])
    assert not [row for row in home_rows if row["kind"] in ("picker", "permission", "progress")]
    setup_rows = _rows(cold["screens"]["setup"])
    assert not [row for row in setup_rows if row["kind"] == "permission"]
