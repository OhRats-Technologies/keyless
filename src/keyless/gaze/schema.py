"""Gaze v2 event schema v0 (GREENFIELD.md §7.1).

One event is one observation of the whole loop: the frame the cameras saw, the
candidate set the screen offered, the model's target distribution, and what the
user actually did.  Collection (:mod:`keyless.gaze.collect`) and replay
(:mod:`keyless.gaze.replay`) share this validator so a recorded session is the
same contract everywhere.

Sections are optional but at least one must be present.  Validation is strict
about the fields that downstream metrics depend on (ids, ``ts_ns``, bbox, pixel,
``accepted``) and permissive about unknown keys so the schema can grow without
invalidating old sessions.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_VERSION = 0

SECTIONS: tuple[str, ...] = ("frame", "screen", "prediction", "interaction")


# --- primitive checks ---------------------------------------------------


def _is_number(value: Any) -> bool:
    """A finite real number; booleans are rejected despite being ints."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_point(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
        and all(_is_number(item) for item in value)
    )


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_number(errors: list[str], where: str, container: Mapping[str, Any], key: str) -> None:
    if key in container and not _is_number(container[key]):
        errors.append(f"{where}.{key} must be a finite number")


def _check_optional_str(
    errors: list[str], where: str, container: Mapping[str, Any], key: str
) -> None:
    value = container.get(key)
    if value is not None and not isinstance(value, str):
        errors.append(f"{where}.{key} must be a string or null")


# --- per-section validation --------------------------------------------


def _validate_frame(errors: list[str], frame: Any) -> None:
    if not isinstance(frame, Mapping):
        errors.append("frame must be an object")
        return
    if not frame:
        errors.append("frame must contain at least one field")
    if "cams" in frame and not _is_string_list(frame["cams"]):
        errors.append("frame.cams must be a list of strings")
    if "landmarks" in frame and not isinstance(frame["landmarks"], list):
        errors.append("frame.landmarks must be a list")
    if "features" in frame and not isinstance(frame["features"], list):
        errors.append("frame.features must be a list")
    if "head_pose" in frame:
        head_pose = frame["head_pose"]
        if not isinstance(head_pose, Mapping):
            errors.append("frame.head_pose must be an object")
        else:
            for axis in ("yaw", "pitch", "roll"):
                _check_number(errors, "frame.head_pose", head_pose, axis)
            for axis in ("yaw", "pitch", "roll"):
                if axis not in head_pose:
                    errors.append(f"frame.head_pose.{axis} is required")
    if "eye_crops" in frame:
        crops = frame["eye_crops"]
        if not isinstance(crops, Mapping):
            errors.append("frame.eye_crops must be an object")
        else:
            for side in ("left", "right"):
                _check_optional_str(errors, "frame.eye_crops", crops, side)


def _validate_candidate(errors: list[str], index: int, candidate: Any) -> None:
    where = f"screen.candidates[{index}]"
    if not isinstance(candidate, Mapping):
        errors.append(f"{where} must be an object")
        return
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        errors.append(f"{where}.id must be a non-empty string")
    bbox = candidate.get("bbox")
    if (
        not isinstance(bbox, Sequence)
        or isinstance(bbox, (str, bytes))
        or len(bbox) != 4
        or not all(_is_number(item) for item in bbox)
    ):
        errors.append(f"{where}.bbox must be [x, y, w, h] with four numbers")
    _check_optional_str(errors, where, candidate, "role")
    _check_optional_str(errors, where, candidate, "text")
    _check_optional_str(errors, where, candidate, "visual")
    _check_number(errors, where, candidate, "score")


def _validate_screen(errors: list[str], screen: Any) -> None:
    if not isinstance(screen, Mapping):
        errors.append("screen must be an object")
        return
    if not screen:
        errors.append("screen must contain at least one field")
    if "display_id" in screen and not _is_int(screen["display_id"]):
        errors.append("screen.display_id must be an integer")
    _check_optional_str(errors, "screen", screen, "app")
    if "window_id" in screen and screen["window_id"] is not None and not _is_int(
        screen["window_id"]
    ):
        errors.append("screen.window_id must be an integer or null")
    _check_optional_str(errors, "screen", screen, "screenshot")
    if "candidates" in screen:
        candidates = screen["candidates"]
        if not isinstance(candidates, list):
            errors.append("screen.candidates must be a list")
        else:
            for index, candidate in enumerate(candidates):
                _validate_candidate(errors, index, candidate)


def _validate_prediction(errors: list[str], prediction: Any) -> None:
    if not isinstance(prediction, Mapping):
        errors.append("prediction must be an object")
        return
    if not prediction:
        errors.append("prediction must contain at least one field")
    _check_optional_str(errors, "prediction", prediction, "top")
    if "p" in prediction:
        value = prediction["p"]
        if not _is_number(value) or not 0.0 <= float(value) <= 1.0:
            errors.append("prediction.p must be a number in [0, 1]")
    _check_number(errors, "prediction", prediction, "entropy")
    if "pixel" in prediction and not _is_point(prediction["pixel"]):
        errors.append("prediction.pixel must be [x, y] with two numbers")


def _validate_interaction(errors: list[str], interaction: Any) -> None:
    if not isinstance(interaction, Mapping):
        errors.append("interaction must be an object")
        return
    kind = interaction.get("kind")
    if not isinstance(kind, str) or not kind:
        errors.append("interaction.kind must be a non-empty string")
    if "mouse" in interaction and interaction["mouse"] is not None:
        if not _is_point(interaction["mouse"]):
            errors.append("interaction.mouse must be [x, y] with two numbers or null")
    _check_optional_str(errors, "interaction", interaction, "target")
    _check_optional_str(errors, "interaction", interaction, "voice")
    if "accepted" in interaction and interaction["accepted"] is not None:
        if not isinstance(interaction["accepted"], bool):
            errors.append("interaction.accepted must be a boolean or null")
    _check_number(errors, "interaction", interaction, "latency_ms")


_VALIDATORS = {
    "frame": _validate_frame,
    "screen": _validate_screen,
    "prediction": _validate_prediction,
    "interaction": _validate_interaction,
}


# --- public API ---------------------------------------------------------


def validate_event(event: Mapping[str, Any]) -> list[str]:
    """Return every schema violation in ``event``; an empty list means valid.

    A section that is present but ``None`` counts as absent.  Unknown keys are
    ignored on purpose: old readers must not reject newer writers.
    """
    if not isinstance(event, Mapping):
        return ["event must be an object"]

    errors: list[str] = []

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        errors.append("session_id must be a non-empty string")
    ts_ns = event.get("ts_ns")
    if not _is_int(ts_ns):
        errors.append("ts_ns must be an integer")
    elif ts_ns < 0:
        errors.append("ts_ns must be non-negative")

    present = [
        name for name in SECTIONS if event.get(name) is not None
    ]
    if not present:
        errors.append(
            "event needs at least one section: frame, screen, prediction, interaction"
        )
    for name in present:
        _VALIDATORS[name](errors, event[name])

    return errors


def make_event(
    session_id: str,
    ts_ns: int | None = None,
    *,
    frame: Mapping[str, Any] | None = None,
    screen: Mapping[str, Any] | None = None,
    prediction: Mapping[str, Any] | None = None,
    interaction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one schema-v0 event and validate it.

    ``ts_ns`` defaults to the current wall clock.  Raises ``ValueError`` when the
    event would not survive :func:`validate_event`, so a producer cannot record
    a malformed session by accident.
    """
    event: dict[str, Any] = {
        "session_id": session_id,
        "ts_ns": time.time_ns() if ts_ns is None else ts_ns,
    }
    for name, section in (
        ("frame", frame),
        ("screen", screen),
        ("prediction", prediction),
        ("interaction", interaction),
    ):
        if section is not None:
            event[name] = dict(section)

    errors = validate_event(event)
    if errors:
        raise ValueError("; ".join(errors))
    return event
