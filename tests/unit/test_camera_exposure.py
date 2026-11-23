"""Exposure-mode plumbing from config to the V4L2 reader."""

from __future__ import annotations

from keyless.platform.linux.v4l2 import CameraLockReport
from keyless.vision import stereo_capture


def _report() -> CameraLockReport:
    return CameraLockReport(
        by_id_path="/dev/v4l/by-id/cam",
        resolved_dev="/dev/video0",
        applied={},
        unsupported=[],
        fourcc="MJPG",
        width=1280,
        height=720,
        fps=30.0,
    )


class _FakeCap:
    def release(self) -> None:
        pass


def test_stereo_capture_forwards_exposure_mode(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_open(**kwargs):
        calls.append(kwargs)
        return _FakeCap(), _report()

    monkeypatch.setattr(stereo_capture, "open_locked_capture", fake_open)

    stereo_capture.StereoCapture(cam1_by_id="/dev/v4l/by-id/cam", mono=True, auto_exposure=True)
    assert calls[-1]["manual_exposure"] is False

    stereo_capture.StereoCapture(cam1_by_id="/dev/v4l/by-id/cam", mono=True, auto_exposure=False)
    assert calls[-1]["manual_exposure"] is True
