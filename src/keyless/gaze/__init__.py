"""Greenfield gaze interface.

The old calibration/regression stack is gone. A gaze source now publishes
plain screen-space frames; any model that produces them (geometric, learned,
or an external research process) plugs in here without the daemon knowing how
the mapping was obtained.

Phase 0 adds the data flywheel seam: record session-keyed events
(:mod:`keyless.gaze.collect`), replay them offline (:mod:`keyless.gaze.replay`),
and score decisions (:mod:`keyless.gaze.metrics`).  No model lives here.
"""

from keyless.gaze.types import GazeFrame, GazeSource
from keyless.gaze.shared_memory import SharedMemoryGazeSource
from keyless.gaze.runtime import run_gaze_loop
from keyless.gaze.schema import SCHEMA_VERSION, make_event, validate_event
from keyless.gaze.collect import SessionCollector
from keyless.gaze.replay import (
    SchemaError,
    Session,
    iter_sessions,
    load_session,
    split_sessions,
)
from keyless.gaze.metrics import (
    NearestCandidateToPixelBaseline,
    metric_table,
)
from keyless.gaze.candidates import (
    NULL_CANDIDATE_ID,
    SOURCE_PRIORITY,
    Candidate,
    deduplicate,
    iou,
    null_candidate,
    with_null,
)
from keyless.gaze.candidate_generator import CandidateGenerator, GenerationResult
from keyless.gaze.recorder import ScreenSnapshot, SessionRecorder
from keyless.gaze.experiment import (
    EmbeddingExtractor,
    EmbeddingHead,
    IdentityExtractor,
    evaluate_experiment,
)

__all__ = [
    "GazeFrame",
    "GazeSource",
    "SharedMemoryGazeSource",
    "run_gaze_loop",
    "SCHEMA_VERSION",
    "make_event",
    "validate_event",
    "SessionCollector",
    "SchemaError",
    "Session",
    "iter_sessions",
    "load_session",
    "split_sessions",
    "NearestCandidateToPixelBaseline",
    "metric_table",
    "NULL_CANDIDATE_ID",
    "SOURCE_PRIORITY",
    "Candidate",
    "deduplicate",
    "iou",
    "null_candidate",
    "with_null",
    "CandidateGenerator",
    "GenerationResult",
    "ScreenSnapshot",
    "SessionRecorder",
    "EmbeddingExtractor",
    "EmbeddingHead",
    "IdentityExtractor",
    "evaluate_experiment",
]
