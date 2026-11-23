"""Contract tests for the settings registry and patch validation."""

from __future__ import annotations

import json

from keyless.config.schema import AppConfig
from keyless.config.specs import (
    SETTING_SPECS,
    SettingSpec,
    spec_for,
    specs_by_key,
    validate_patch,
)


def test_every_appconfig_field_has_exactly_one_spec() -> None:
    fields = AppConfig.__dataclass_fields__
    keys = [spec.key for spec in SETTING_SPECS]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(fields)


def test_spec_defaults_match_appconfig_defaults() -> None:
    cfg = AppConfig()
    for spec in SETTING_SPECS:
        assert spec.default == getattr(cfg, spec.key), spec.key


def test_registry_lookup_helpers() -> None:
    by_key = specs_by_key()
    assert len(by_key) == len(SETTING_SPECS)
    assert spec_for("fps") is by_key["fps"]
    assert spec_for("not_a_setting") is None


def test_spec_as_dict_is_json_serializable() -> None:
    payload = [spec.as_dict() for spec in SETTING_SPECS]
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert len(decoded) == len(SETTING_SPECS)
    assert decoded[0]["platforms"] == ["all"]


def test_validate_patch_accepts_valid_values() -> None:
    assert validate_patch({}) == {}
    assert validate_patch({"tts_enabled": True}) == {}
    assert validate_patch({"fps": 60}) == {}
    assert validate_patch({"blink_threshold": 0.4}) == {}
    assert validate_patch({"blink_threshold": 1}) == {}
    assert validate_patch({"camera_mode": "stereo"}) == {}
    assert validate_patch({"llm_provider": "ollama"}) == {}
    assert validate_patch({"audio_device": 3}) == {}
    assert validate_patch({"audio_device": "USB microphone"}) == {}
    assert validate_patch({"audio_device": None}) == {}


def test_validate_patch_rejects_wrong_types() -> None:
    assert "fps" in validate_patch({"fps": "sixty"})
    assert "fps" in validate_patch({"fps": True})
    assert "fps" in validate_patch({"fps": 30.5})
    assert "fps" in validate_patch({"fps": None})
    assert "tts_enabled" in validate_patch({"tts_enabled": 1})
    assert "tts_enabled" in validate_patch({"tts_enabled": "yes"})
    assert "blink_threshold" in validate_patch({"blink_threshold": True})
    assert "llm_model" in validate_patch({"llm_model": 7})
    assert "audio_device" in validate_patch({"audio_device": 1.5})
    assert "audio_device" in validate_patch({"audio_device": True})
    assert "audio_device" in validate_patch({"audio_device": ["hw:0"]})


def test_validate_patch_rejects_out_of_range_numbers() -> None:
    assert "fps" in validate_patch({"fps": 0})
    assert "fps" in validate_patch({"fps": 1000})
    assert "blink_threshold" in validate_patch({"blink_threshold": 1.5})
    assert "gaze_keyword_threshold" in validate_patch({"gaze_keyword_threshold": -1})
    assert "firefox_bridge_port" in validate_patch({"firefox_bridge_port": 70000})


def test_validate_patch_rejects_non_finite_floats() -> None:
    # NaN compares false against every bound, so only an explicit finiteness
    # check can stop it from being persisted.
    assert "blink_threshold" in validate_patch({"blink_threshold": float("nan")})
    assert "blink_threshold" in validate_patch({"blink_threshold": float("inf")})
    assert "blink_threshold" in validate_patch({"blink_threshold": float("-inf")})
    assert "pop_min_rms" in validate_patch({"pop_min_rms": float("nan")})


def test_validate_patch_rejects_bad_options() -> None:
    assert "camera_mode" in validate_patch({"camera_mode": "triple"})
    assert "camera_backend" in validate_patch({"camera_backend": "gstreamer"})
    assert "action_backend" in validate_patch({"action_backend": "robot"})
    assert "update_channel" in validate_patch({"update_channel": "nightly"})


def test_validate_patch_rejects_unknown_keys() -> None:
    errors = validate_patch({"nope": 1, "tts_enabled": True})
    assert errors == {"nope": "unknown setting"}


def test_live_and_restart_classification() -> None:
    assert spec_for("tts_enabled").apply == "live"
    assert spec_for("audio_device").apply == "live"
    assert spec_for("llm_provider").apply == "live"
    assert spec_for("fps").apply == "restart"
    assert spec_for("camera_mode").apply == "restart"
    assert spec_for("camera_backend").apply == "restart"
    assert spec_for("stt_provider").apply == "restart"
    assert spec_for("gaze_cursor_overlay").platforms == ("macos",)
    assert spec_for("camera_backend").platforms == ("linux",)


def test_setting_spec_is_frozen() -> None:
    import pytest

    spec = spec_for("fps")
    assert isinstance(spec, SettingSpec)
    with pytest.raises(Exception):
        spec.key = "other"  # type: ignore[misc]
