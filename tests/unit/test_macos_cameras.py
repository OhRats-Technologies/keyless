"""macOS camera choice: built-in first, never auto-pair stray cameras into stereo."""

from __future__ import annotations

import keyless.platform.macos.cameras as mac_cameras
from keyless.platform.base import CameraInfo


def _cam(index: int, name: str, transport: str) -> CameraInfo:
    return CameraInfo(id=f"uid-{index}", name=name, path=str(index),
                      capabilities={"index": index, "transport": transport, "unique_id": True})


def _patch(monkeypatch) -> None:
    cams = [_cam(0, "OBS Virtual Camera", "virt"), _cam(1, "MacBook Air Camera", "bltn"),
            _cam(2, "iPhone Camera", "othr")]
    monkeypatch.setattr(mac_cameras, "list_cameras", lambda: cams)


def test_auto_picks_builtin_mono_over_virtual_and_continuity(monkeypatch) -> None:
    _patch(monkeypatch)
    assert mac_cameras.resolve_camera_config("auto", "/dev/v4l/by-id/linux-cam", "") == ("mono", "1", "")


def test_auto_goes_stereo_only_when_second_camera_is_configured(monkeypatch) -> None:
    _patch(monkeypatch)
    assert mac_cameras.resolve_camera_config("auto", "uid-1", "uid-2") == ("stereo", "1", "2")


def test_explicit_camera_choice_is_respected(monkeypatch) -> None:
    _patch(monkeypatch)
    assert mac_cameras.resolve_camera_config("auto", "uid-0", "") == ("mono", "0", "")


def test_stale_bare_index_falls_back_to_builtin(monkeypatch) -> None:
    _patch(monkeypatch)
    assert mac_cameras.resolve_camera_config("mono", "0", "") == ("mono", "1", "")
