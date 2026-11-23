"""Adapter for the POSIX shared-memory gaze channel.

The channel is intentionally model-agnostic: a model process publishes
``(x, y, flags, timestamp)`` and the daemon consumes it. Nothing about
calibration, features, or regression leaks across the boundary.
"""

from __future__ import annotations

from keyless.gaze.types import GazeFrame
from keyless.ipc.state import SharedGazeReader


class SharedMemoryGazeSource:
    """Read gaze frames published on the Keyless shared-memory channel."""

    def __init__(self) -> None:
        self._reader = SharedGazeReader()

    def read(self) -> GazeFrame | None:
        state = self._reader.read()
        if state is None:
            return None
        return GazeFrame(
            x=state.x,
            y=state.y,
            ts=state.timestamp_ns / 1e9,
            flags=state.flags,
        )

    def close(self) -> None:
        self._reader.close()
