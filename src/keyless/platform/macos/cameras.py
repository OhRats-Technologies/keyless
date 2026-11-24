"""macOS camera discovery: AVFoundation uniqueID when available, OpenCV fallback.

Stable identity (D5) is the AVFoundation `uniqueID`; without pyobjc we fall
back to `avf:<index>:<name>` probed via OpenCV so calibration can still key
on a stable string per host.
"""

from __future__ import annotations

import json
import subprocess

from keyless.platform.base import CameraInfo


def _transport(dev) -> str:  # type: ignore[no-untyped-def]
    """AVFoundation transport FourCC: 'bltn' built-in, 'virt' virtual (OBS), 'usb ', ..."""
    try:
        code = int(dev.transportType()) & 0xFFFFFFFF
        return code.to_bytes(4, "big").decode("ascii", "replace").strip()
    except Exception:
        return ""


def _avfoundation_cameras() -> list[CameraInfo]:
    """Cameras indexed the way OpenCV's AVFoundation backend indexes them.

    OpenCV sorts capture devices by ``uniqueID`` (not ``devicesWithMediaType``
    order), so the rank in that sort is the index we publish as
    ``CameraInfo.path``. Verified on real hardware with OpenCV 5.1 on macOS 26:
    built-in (6C70...)=0, OBS virtual (7626...)=1, iPhone (C639...)=2, by
    capturing a frame from each index.
    """
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeVideo  # type: ignore[import-not-found]

        devices = sorted(AVCaptureDevice.devicesWithMediaType_(AVMediaTypeVideo) or [],
                         key=lambda d: str(d.uniqueID()))
        out: list[CameraInfo] = []
        for index, dev in enumerate(devices):
            try:
                uid = str(dev.uniqueID())
                name = str(dev.localizedName())
            except Exception:
                continue
            out.append(CameraInfo(id=uid, name=name or uid, path=str(index), capabilities={
                "backend": "avfoundation", "unique_id": True, "index": index,
                "transport": _transport(dev),
            }))
        return out
    except Exception:
        return []


def _profiler_cameras() -> list[CameraInfo]:
    try:
        res = subprocess.run(["system_profiler", "SPCameraDataType", "-json"],
                             capture_output=True, text=True, timeout=5.0, check=True)
        data = json.loads(res.stdout or "{}")
        out: list[CameraInfo] = []
        # Same uniqueID sort as OpenCV, so list_cameras()' indexes line up.
        entries = sorted(data.get("SPCameraDataType", []),
                         key=lambda e: str(e.get("spcamera_unique-id", "")))
        for entry in entries:
            name = str(entry.get("_name", "")).strip()
            if not name:
                continue
            out.append(CameraInfo(id=name, name=name, path=f"avf:{name}",
                                  capabilities={"backend": "avfoundation", "unique_id": False}))
        return out
    except Exception:
        return []


def _opencv_cameras(max_probe: int = 4) -> list[CameraInfo]:
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception:
        return []
    found: list[CameraInfo] = []
    for idx in range(max_probe):
        cap = None
        try:
            cap = cv2.VideoCapture(idx)
            if cap is not None and cap.isOpened():
                found.append(CameraInfo(id=f"avf:{idx}", name=f"Camera {idx}", path=str(idx),
                                        capabilities={"backend": "avfoundation", "index": idx}))
        except Exception:
            continue
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
    return found


def list_cameras() -> list[CameraInfo]:
    cams = _avfoundation_cameras()
    if cams:
        return cams
    cams = _profiler_cameras()
    if cams:
        # Annotate with indexes so StereoCapture can open them via OpenCV.
        indexed = []
        for i, cam in enumerate(cams):
            indexed.append(CameraInfo(id=cam.id, name=cam.name, path=str(i), capabilities={
                **cam.capabilities, "index": i}))
        return indexed
    return _opencv_cameras()


def _preference(cam: CameraInfo) -> int:
    transport = str(cam.capabilities.get("transport", ""))
    if transport == "bltn":
        return 0
    if transport == "virt" or "virtual" in cam.name.lower():
        return 2
    return 1


def resolve_camera_config(requested_mode: str, cam1_by_id: str, cam2_by_id: str,
                          *, discovered: list[str] | None = None,
                          exists=None) -> tuple[str, str, str]:  # type: ignore[no-untyped-def]
    cams = list_cameras()
    by_id = {c.id: c for c in cams}
    paths = [c.path for c in cams]
    if discovered is None:
        discovered = [c.id for c in cams]

    def resolve(slot: str) -> str:
        if slot in by_id:
            return by_id[slot].path
        # A bare index is not a stable identity (it shifts when a camera is
        # plugged in), so only honour it when no uniqueIDs are available.
        if slot in paths and not (slot.isdigit() and any(c.capabilities.get("unique_id") for c in cams)):
            return slot
        return ""

    cam1 = resolve(cam1_by_id) if cam1_by_id else ""
    if not cam1:
        # Stored id is from another host (e.g. Linux by-id defaults) or empty:
        # pick the best real camera (built-in first, virtual cameras last).
        ranked = sorted(cams, key=_preference)
        cam1 = ranked[0].path if ranked else ""
    mode = (requested_mode or "auto").lower()
    if mode not in {"auto", "mono", "stereo"}:
        mode = "auto"
    if mode == "mono":
        return "mono", cam1, ""
    if mode == "stereo":
        cam2 = resolve(cam2_by_id) if cam2_by_id else ""
        if not cam2 or cam2 == cam1:
            cam2 = next((p for p in paths if p != cam1), "")
        return ("stereo", cam1, cam2) if cam2 else ("mono", cam1, "")
    # auto: a Mac's extra cameras (OBS virtual camera, iPhone Continuity Camera)
    # are not a stereo rig, so auto means one camera unless cam2 is configured.
    if cam2_by_id:
        cam2 = resolve(cam2_by_id)
        if cam2 and cam2 != cam1:
            return "stereo", cam1, cam2
    return "mono", cam1, ""


class macOSCameraSource:
    def list_devices(self) -> list[CameraInfo]:
        return list_cameras()
