"""Pull gaze frames and render them through the active cursor backend.

This loop is model-agnostic. It never calibrates, predicts, or smooths beyond
what the source already did; it is the boundary where the new target model
will eventually publish frames.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from keyless.gaze.types import GazeFrame, GazeSource


def run_gaze_loop(
    source: GazeSource,
    pointer: object,
    *,
    on_frame: Callable[[GazeFrame], None] | None = None,
    stop_event: threading.Event | None = None,
    duration_sec: float | None = None,
    hz: float = 60.0,
    move_enabled: bool = True,
) -> None:
    """Read ``source`` at ``hz`` and, when enabled, move ``pointer``."""
    period = 1.0 / max(1.0, float(hz))
    started = time.monotonic()
    while stop_event is None or not stop_event.is_set():
        frame = source.read()
        if frame is not None:
            if move_enabled:
                move = getattr(pointer, "move_absolute", None)
                if callable(move):
                    move(frame.x, frame.y)
            if on_frame is not None:
                on_frame(frame)
        if duration_sec is not None and time.monotonic() - started >= duration_sec:
            break
        time.sleep(period)
