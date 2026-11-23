"""Greenfield gaze interface.

The old calibration/regression stack is gone. A gaze source now publishes
plain screen-space frames; any model that produces them (geometric, learned,
or an external research process) plugs in here without the daemon knowing how
the mapping was obtained.

Phase 0 adds the data flywheel seam: record session-keyed events
(:mod:`keyless.gaze.collect`) and, in time, replay them offline.  No model
lives here.
"""

from keyless.gaze.types import GazeFrame, GazeSource
from keyless.gaze.shared_memory import SharedMemoryGazeSource
from keyless.gaze.runtime import run_gaze_loop
from keyless.gaze.schema import SCHEMA_VERSION, make_event, validate_event
from keyless.gaze.collect import SessionCollector

__all__ = [
    "GazeFrame",
    "GazeSource",
    "SharedMemoryGazeSource",
    "run_gaze_loop",
    "SCHEMA_VERSION",
    "make_event",
    "validate_event",
    "SessionCollector",
]
