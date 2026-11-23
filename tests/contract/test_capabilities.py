"""Contract tests for capability probes and the shared report."""

from __future__ import annotations

import os
from pathlib import Path

from keyless.platform.base import CapabilityStatus, PermissionState
from keyless.platform.capabilities import (
    Capability,
    CapabilityReport,
    detect_capabilities,
    probe_audio,
    probe_cameras,
    probe_hotkeys,
    probe_pointer,
    probe_screen,
)


class _FakeSoundDevice:
    def __init__(self, devices: list[dict[str, object]]) -> None:
        self._devices = devices

    def query_devices(self) -> list[dict[str, object]]:
        return self._devices


class _BrokenSoundDevice:
    def query_devices(self) -> list[dict[str, object]]:
        raise RuntimeError("PortAudio unavailable")


def test_pointer_probe_reports_missing_device(tmp_path: Path) -> None:
    info = probe_pointer(tmp_path / "missing-uinput")
    assert info.status is CapabilityStatus.NEEDS_PERMISSION
    assert info.permission is PermissionState.DENIED
    assert "missing" in info.detail


def test_pointer_probe_reports_write_access(tmp_path: Path) -> None:
    device = tmp_path / "uinput"
    device.touch()
    assert probe_pointer(device).status is CapabilityStatus.AVAILABLE

    device.chmod(0o444)
    if os.access(device, os.W_OK):
        # Running as a user that bypasses permission bits (e.g. root).
        return
    denied = probe_pointer(device)
    assert denied.status is CapabilityStatus.NEEDS_PERMISSION
    assert "cannot write" in denied.detail


def test_camera_probe_degrades_to_one_camera(tmp_path: Path) -> None:
    root = tmp_path / "by-id"
    root.mkdir()
    (root / "usb-cam-a-video-index0").touch()
    one = probe_cameras(root)
    assert one.status is CapabilityStatus.AVAILABLE
    assert "mono gaze is available" in one.detail

    (root / "usb-cam-b-video-index0").touch()
    assert probe_cameras(root).status is CapabilityStatus.AVAILABLE


def test_audio_probe_uses_injected_sounddevice() -> None:
    good = probe_audio(_FakeSoundDevice([{"name": "USB Mic", "max_input_channels": 1}]))
    assert good.status is CapabilityStatus.AVAILABLE
    assert "USB Mic" in good.detail

    none = probe_audio(_FakeSoundDevice([{"name": "Speakers", "max_input_channels": 0}]))
    assert none.status is CapabilityStatus.NEEDS_PERMISSION

    broken = probe_audio(_BrokenSoundDevice())
    assert broken.status is CapabilityStatus.UNAVAILABLE
    assert "PortAudio" in broken.detail


def test_hotkey_probe_does_not_assume_devices(tmp_path: Path, monkeypatch) -> None:
    import keyless.platform.capabilities as caps

    monkeypatch.setattr(caps.Path, "glob", lambda self, pattern: [])
    info = probe_hotkeys()
    assert info.status is CapabilityStatus.NEEDS_PERMISSION


def test_screen_probe_reports_missing_gdbus() -> None:
    info = probe_screen({"XDG_SESSION_TYPE": "wayland"}, has_gdbus=False)
    assert info.status is CapabilityStatus.UNAVAILABLE
    assert "gdbus" in info.detail


def test_detect_capabilities_non_linux_probes_real_backends() -> None:
    report = detect_capabilities(platform_name="darwin")
    assert report.platform == "macos"
    assert report.get(Capability.POINTER) is not None
    # macOS backends are really probed now (Hex-style): never a blanket stub.
    assert report.status(Capability.POINTER) in (
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.NEEDS_PERMISSION,
        CapabilityStatus.UNAVAILABLE,
    )
    assert report.get(Capability.SWIFTUI) is not None


def test_capability_report_serializes_for_doctor() -> None:
    report = CapabilityReport(platform="linux")
    report.permissions["microphone"] = type(
        "P", (), {"state": PermissionState.GRANTED, "detail": "ok", "action": ""}
    )()
    payload = report.as_dict()
    assert payload["platform"] == "linux"
    assert payload["permissions"]["microphone"]["state"] == "granted"
