"""macOS camera discovery: AVFoundation uniqueID when available, OpenCV fallback.

Stable identity (D5) is the AVFoundation `uniqueID`; without pyobjc we fall
back to `avf:<index>:<name>` probed via OpenCV so calibration can still key
on a stable string per host.
"""

from __future__ import annotations

import json
import subprocess

from keyless.platform.base import CameraInfo


def _avfoundation_cameras() -> list[CameraInfo]:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeVideo  # type: ignore[import-not-found]

        devices = AVCaptureDevice.devicesWithMediaType_(AVMediaTypeVideo())
        out: list[CameraInfo] = []
        for dev in devices or []:
            try:
                uid = str(dev.uniqueID())
                name = str(dev.localizedName())
            except Exception:
                continue
            out.append(CameraInfo(id=uid, name=name or uid, path=f"avf:{uid}",
                                  capabilities={"backend": "avfoundation", "unique_id": True}))
        if out:
            return out
    except Exception:
        pass
    return []


def _profiler_cameras() -> list[CameraInfo]:
    try:
        res = subprocess.run(["system_profiler", "SPCameraDataType", "-json"],
                             capture_output=True, text=True, timeout=5.0, check=True)
        data = json.loads(res.stdout or "{}")
        out: list[CameraInfo] = []
        for entry in data.get("SPCameraDataType", []):
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
        if slot in paths:
            return slot
        return ""

    cam1 = resolve(cam1_by_id) if cam1_by_id else ""
    if not cam1:
        # Stored id is from another host (e.g. Linux by-id defaults) or empty:
        # fall back to the first discovered camera like the Linux resolver.
        cam1 = paths[0] if paths else ""
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
    # auto
    if cam2_by_id:
        cam2 = resolve(cam2_by_id)
        if cam2 and cam2 != cam1:
            return "stereo", cam1, cam2
    other = next((p for p in paths if p != cam1), "")
    if other:
        return "stereo", cam1, other
    return ("mono" if cam1 else "stereo"), cam1, ""


class macOSCameraSource:
    def list_devices(self) -> list[CameraInfo]:
        return list_cameras()
