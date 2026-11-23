"""OpenCV/AVFoundation frame capture for macOS.

Linux binds cameras through ``/dev/v4l/by-id`` (see ``platform/linux/v4l2``).
macOS has no V4L2: OpenCV's AVFoundation backend opens a camera by index, and
the index is exactly what ``platform/macos/cameras.list_cameras`` publishes as
``CameraInfo.path``. This module presents the same tiny read/release interface
``StereoCapture`` expects, so the vision code stays platform-neutral.
"""

from __future__ import annotations

from typing import Any

import cv2

from keyless.platform.linux.v4l2 import CameraLockReport


def open_opencv_capture(
    path: str,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> tuple[Any, CameraLockReport]:
    """Open a discovered macOS camera through OpenCV's AVFoundation backend."""
    try:
        index = int(path)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Not a macOS camera index: {path!r}") from exc

    capture = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(
            f"Camera {index} could not be opened. Allow Camera access for Keyless "
            "in System Settings > Privacy & Security > Camera, then try again."
        )
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    capture.set(cv2.CAP_PROP_FPS, fps)

    actual_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or width
    actual_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or height
    actual_fps = float(capture.get(cv2.CAP_PROP_FPS)) or float(fps)
    report = CameraLockReport(
        by_id_path=path,
        resolved_dev=f"avf:{index}",
        applied={},
        unsupported=[],
        fourcc="AVFOUNDATION",
        width=actual_w,
        height=actual_h,
        fps=actual_fps,
    )
    return capture, report
