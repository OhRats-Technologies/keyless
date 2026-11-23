"""Suppress click-learning for clicks synthesized by Keyless itself.

Physical clicks are ground truth for gaze; a click Keyless posts at the eye
cursor is circular evidence and must not bias the model. Backends mark their
own clicks here so the global harvesters can ignore them briefly.
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_LAST_SYNTHETIC_TS = 0.0

# A physical click can follow a synthesized one quickly; a short window is
# enough to cover the event delivery delay without muting the user for long.
SUPPRESS_WINDOW_S = 0.25


def mark_synthetic_click() -> None:
    global _LAST_SYNTHETIC_TS
    with _LOCK:
        _LAST_SYNTHETIC_TS = time.monotonic()


def synthetic_click_recent(window_s: float = SUPPRESS_WINDOW_S) -> bool:
    with _LOCK:
        last = _LAST_SYNTHETIC_TS
    return last > 0.0 and (time.monotonic() - last) <= max(0.0, window_s)


__all__ = ["mark_synthetic_click", "synthetic_click_recent", "SUPPRESS_WINDOW_S"]
