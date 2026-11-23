"""The only contract between a gaze model and the rest of Keyless."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class GazeFrame:
    """One screen-space gaze observation.

    ``feat`` is optional and model-defined. It exists so a future target model
    can consume the same stream without the daemon serializing feature arrays.
    ``confidence`` is 1.0 when the source has no calibrated uncertainty.
    """

    x: float
    y: float
    ts: float = 0.0
    flags: int = 0
    confidence: float = 1.0
    feat: np.ndarray | None = None

    def __post_init__(self) -> None:
        if not self.ts:
            object.__setattr__(self, "ts", time.monotonic())


class GazeSource(Protocol):
    """A pull-based gaze stream."""

    def read(self) -> GazeFrame | None:
        """Return the newest frame, or None when no writer is active."""

    def close(self) -> None:
        """Release any OS resources held by the source."""
