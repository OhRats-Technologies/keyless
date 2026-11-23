"""Greenfield gaze interface.

The old calibration/regression stack is gone. A gaze source now publishes
plain screen-space frames; any model that produces them (geometric, learned,
or an external research process) plugs in here without the daemon knowing how
the mapping was obtained.
"""

from keyless.gaze.types import GazeFrame, GazeSource
from keyless.gaze.shared_memory import SharedMemoryGazeSource
from keyless.gaze.runtime import run_gaze_loop

__all__ = [
    "GazeFrame",
    "GazeSource",
    "SharedMemoryGazeSource",
    "run_gaze_loop",
]
