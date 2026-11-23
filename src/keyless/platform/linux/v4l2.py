"""Deterministic /dev/v4l/by-id/ camera binding and pure-Python V4L2 UVC control locking."""

from __future__ import annotations

import fcntl
import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

# Linux V4L2 ioctl constants (x86_64 / standard Linux ABI)
_VIDIOC_QUERYCTRL = 0xC0445624  # _IOWR('V', 36, struct v4l2_queryctrl)
_VIDIOC_G_CTRL = 0xC008561B     # _IOWR('V', 27, struct v4l2_control)
_VIDIOC_S_CTRL = 0xC008561C     # _IOWR('V', 28, struct v4l2_control)

_QUERYCTRL_STRUCT = struct.Struct("=II32siiiiI2I")  # 68 bytes
_CTRL_STRUCT = struct.Struct("=Ii")                # 8 bytes

# V4L2 camera & user class control IDs
V4L2_CID_GAIN = 0x00980913
V4L2_CID_EXPOSURE_AUTO = 0x009A0901
V4L2_CID_EXPOSURE_ABSOLUTE = 0x009A0902
V4L2_CID_EXPOSURE_AUTO_PRIORITY = 0x009A0903
V4L2_CID_FOCUS_AUTO = 0x009A090C


@dataclass(frozen=True)
class V4L2ControlSpec:
    """Metadata for a single V4L2 camera control."""

    cid: int
    name: str
    minimum: int
    maximum: int
    step: int
    default: int


@dataclass(frozen=True)
class CameraLockReport:
    """Summary of V4L2 controls applied to a camera."""

    by_id_path: str
    resolved_dev: str
    applied: dict[str, int]
    unsupported: list[str]
    fourcc: str
    width: int
    height: int
    fps: float


def resolve_by_id_device(by_id_path: str) -> str:
    """Resolve a deterministic `/dev/v4l/by-id/...` symlink to its current `/dev/videoN`."""
    p = Path(by_id_path)
    if not p.exists():
        raise FileNotFoundError(
            f"Camera symlink not found: {by_id_path}. "
            "Check `/dev/v4l/by-id/` and update your camera configuration."
        )
    if "/dev/v4l/by-id/" not in str(p):
        logger.warning(
            "Camera path %s is not under /dev/v4l/by-id/; USB enumeration order may swap on reboot.",
            by_id_path,
        )
    return os.path.realpath(p)


def query_v4l2_control(fd: int, cid: int) -> V4L2ControlSpec | None:
    """Query whether `cid` is supported on `fd`; returns None if unsupported."""
    buf = bytearray(_QUERYCTRL_STRUCT.pack(cid, 0, b"", 0, 0, 0, 0, 0, 0, 0))
    try:
        fcntl.ioctl(fd, _VIDIOC_QUERYCTRL, buf)
    except OSError:
        return None
    q_id, _, q_name, q_min, q_max, q_step, q_def, _, _, _ = _QUERYCTRL_STRUCT.unpack(buf)
    name = q_name.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return V4L2ControlSpec(
        cid=q_id,
        name=name,
        minimum=q_min,
        maximum=q_max,
        step=q_step,
        default=q_def,
    )


def set_v4l2_control(fd: int, cid: int, value: int) -> int | None:
    """Set a V4L2 control clamped to its supported bounds; returns readback value or None."""
    spec = query_v4l2_control(fd, cid)
    if spec is None:
        return None
    clamped = max(spec.minimum, min(spec.maximum, int(value)))
    buf = bytearray(_CTRL_STRUCT.pack(cid, clamped))
    fcntl.ioctl(fd, _VIDIOC_S_CTRL, buf)
    read_buf = bytearray(_CTRL_STRUCT.pack(cid, 0))
    fcntl.ioctl(fd, _VIDIOC_G_CTRL, read_buf)
    _, actual = _CTRL_STRUCT.unpack(read_buf)
    return actual


def lock_v4l2_controls(
    by_id_path: str,
    exposure_absolute: int = 300,
    gain: int | None = 160,
    manual_exposure: bool = True,
) -> tuple[dict[str, int], list[str]]:
    """Lock exposure priority, manual exposure, gain, and autofocus on a V4L2 camera via ioctl."""
    real_dev = resolve_by_id_device(by_id_path)
    applied: dict[str, int] = {}
    unsupported: list[str] = []

    fd = os.open(real_dev, os.O_RDWR)
    try:
        desired: list[tuple[str, int, int]] = [
            ("exposure_auto_priority", V4L2_CID_EXPOSURE_AUTO_PRIORITY, 0),
            ("focus_automatic_continuous", V4L2_CID_FOCUS_AUTO, 0),
        ]
        if manual_exposure:
            desired.append(("auto_exposure", V4L2_CID_EXPOSURE_AUTO, 1))
            desired.append(("exposure_time_absolute", V4L2_CID_EXPOSURE_ABSOLUTE, exposure_absolute))
        else:
            desired.append(("auto_exposure", V4L2_CID_EXPOSURE_AUTO, 3))
        if gain is not None:
            desired.append(("gain", V4L2_CID_GAIN, gain))

        for label, cid, val in desired:
            try:
                actual = set_v4l2_control(fd, cid, val)
            except OSError as exc:
                logger.warning("Failed to set %s=%s on %s: %s", label, val, real_dev, exc)
                actual = None
            if actual is None:
                unsupported.append(label)
            else:
                applied[label] = actual
    finally:
        os.close(fd)

    return applied, unsupported


def open_locked_capture(
    by_id_path: str,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    exposure_absolute: int = 300,
    gain: int | None = 160,
    manual_exposure: bool = True,
) -> tuple[cv2.VideoCapture, CameraLockReport]:
    """Open an MJPG V4L2 capture bound by `/dev/v4l/by-id/` with locked exposure/gain/fps."""
    real_dev = resolve_by_id_device(by_id_path)
    lock_v4l2_controls(
        by_id_path,
        exposure_absolute=exposure_absolute,
        gain=gain,
        manual_exposure=manual_exposure,
    )

    cap = cv2.VideoCapture(real_dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open V4L2 capture on {by_id_path} ({real_dev})")

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
    cap.set(cv2.CAP_PROP_FPS, int(fps))
    # Use 2 MMAP buffers (double-buffering): BUFFERSIZE=1 halves 30fps cameras to 15fps
    # because V4L2 cannot fill a second buffer while OpenCV decodes the current JPEG.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

    # Read one warm-up frame and re-apply controls so UVC drivers don't reset on STREAMON.
    cap.grab()
    applied, unsupported = lock_v4l2_controls(
        by_id_path,
        exposure_absolute=exposure_absolute,
        gain=gain,
        manual_exposure=manual_exposure,
    )

    fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
    fourcc_str = "".join(chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4))
    report = CameraLockReport(
        by_id_path=by_id_path,
        resolved_dev=real_dev,
        applied=applied,
        unsupported=unsupported,
        fourcc=fourcc_str,
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        fps=float(cap.get(cv2.CAP_PROP_FPS)),
    )
    return cap, report
