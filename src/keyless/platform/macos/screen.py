"""macOS screen capture via the built-in `screencapture` CLI (no deps).

Returns BGR numpy arrays matching the Linux capturer's contract so the shared
grounding tiers (DOM/OCR/VLM) work unchanged. Screen Recording permission is
required; denial surfaces as RuntimeError with a TCC action.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _load_png(path: Path):  # type: ignore[no-untyped-def]
    try:
        import cv2  # type: ignore[import-not-found]

        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is not None and img.size > 0:
            return img
    except Exception:
        pass
    try:
        from PIL import Image  # type: ignore[import-not-found]
        import numpy as np

        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB"))
        return rgb[:, :, ::-1].copy()
    except Exception as exc:
        raise RuntimeError(f"Could not decode screencapture output: {exc}") from exc


class macOSScreenSource:
    def __init__(self) -> None:
        self._tmp = Path(tempfile.gettempdir()) / "keyless-screencap.png"

    def permission_state(self):  # type: ignore[no-untyped-def]
        from keyless.platform.base import PermissionState

        return PermissionState.UNKNOWN

    def capture_full_desktop(self):  # type: ignore[no-untyped-def]
        res = subprocess.run(
            ["screencapture", "-x", "-t", "png", str(self._tmp)],
            capture_output=True, timeout=5.0, check=False,
        )
        if res.returncode != 0 or not self._tmp.exists():
            raise RuntimeError(
                "screencapture failed; grant Screen Recording in System Settings > Privacy & Security."
            )
        return _load_png(self._tmp)

    def capture_rect(self, x: int, y: int, width: int, height: int):  # type: ignore[no-untyped-def]
        full = self.capture_full_desktop()
        h, w = full.shape[:2]
        x0 = max(0, min(w - 1, int(x)))
        y0 = max(0, min(h - 1, int(y)))
        x1 = max(x0 + 1, min(w, int(x + width)))
        y1 = max(y0 + 1, min(h, int(y + height)))
        return full[y0:y1, x0:x1].copy()

    def close(self) -> None:
        return None


def monitor_at_point(layout, x: float, y: float):  # type: ignore[no-untyped-def]
    for mon in layout.monitors:
        if mon.x <= x < (mon.x + mon.width) and mon.y <= y < (mon.y + mon.height):
            return mon
    return layout.primary
