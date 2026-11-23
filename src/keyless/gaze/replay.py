"""Deterministic replay of recorded sessions (GREENFIELD.md §7.3).

Replay reads what :mod:`keyless.gaze.collect` wrote, validates every event with
the shared schema, and returns events in timestamp order so two runs over the
same session produce identical metrics.

Splits are **session-level**: frames from one recording never appear in two
splits, because leakage between adjacent frames would make offline numbers
optimistic.  Never split frames.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keyless.gaze.schema import validate_event


class SchemaError(ValueError):
    """A recorded event or ``meta.json`` does not satisfy the v0 schema."""


@dataclass(frozen=True)
class Session:
    """One recorded session: its metadata and its validated events."""

    session_id: str
    meta: dict[str, Any]
    events: list[dict[str, Any]]


def _read_meta(meta_path: Path) -> dict[str, Any]:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"meta.json is not valid JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise SchemaError("meta.json must contain an object")
    return meta


def _read_events(events_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"event {index}: invalid JSON: {exc}") from exc
            errors = validate_event(event)
            if errors:
                raise SchemaError(f"event {index}: {errors}")
            events.append(event)
    return events


def load_session(session_dir: Path) -> Session:
    """Load and validate one session directory, sorted by ``ts_ns`` (stable)."""
    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        raise FileNotFoundError(f"session directory not found: {session_dir}")
    meta_path = session_dir / "meta.json"
    events_path = session_dir / "events.jsonl"
    if not meta_path.is_file():
        raise FileNotFoundError(f"session is missing meta.json: {meta_path}")
    if not events_path.is_file():
        raise FileNotFoundError(f"session is missing events.jsonl: {events_path}")

    meta = _read_meta(meta_path)
    events = _read_events(events_path)
    # sorted() is stable: equal timestamps keep their recorded order.
    events.sort(key=lambda event: event["ts_ns"])

    session_id = meta.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = events[0]["session_id"] if events else session_dir.name
    return Session(session_id=session_id, meta=meta, events=events)


def iter_sessions(root: Path) -> list[Path]:
    """Session directories under ``root``, sorted by name for determinism.

    A directory qualifies when it holds both ``meta.json`` and ``events.jsonl``.
    A missing ``root`` yields an empty list: discovery on an empty data
    directory is normal, not an error.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    found = [
        entry
        for entry in root.iterdir()
        if entry.is_dir()
        and (entry / "meta.json").is_file()
        and (entry / "events.jsonl").is_file()
    ]
    return sorted(found, key=lambda entry: entry.name)


def _floor_count(fraction: float, total: int) -> int:
    # 1e-9 absorbs float representation error such as 0.29 * 100 = 28.999... .
    return max(0, math.floor(fraction * total + 1e-9))


def split_sessions(
    session_ids: Sequence[str],
    *,
    train: float = 0.6,
    val: float = 0.2,
    test: float = 0.2,
    seed: int = 0,
) -> dict[str, list[str]]:
    """Split whole sessions into train/val/test (never frames).

    Ids are sorted, shuffled with ``random.Random(seed)``, then sliced: train
    and val take ``floor(fraction * n)`` each and test takes the remainder, so
    every id lands in exactly one split and the splits are disjoint by
    construction.  The same seed yields the same split on every machine.

    Raises ``ValueError`` for negative fractions, fractions that do not sum to
    1.0 (within 1e-6), or duplicate ids (which would break coverage).
    """
    for name, fraction in (("train", train), ("val", val), ("test", test)):
        if not math.isfinite(fraction) or fraction < 0.0:
            raise ValueError(f"{name} fraction must be a finite non-negative number")
    total_fraction = train + val + test
    if abs(total_fraction - 1.0) > 1e-6:
        raise ValueError(f"fractions must sum to 1.0 (got {total_fraction})")

    ids = list(session_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("session ids must be unique")

    ordered = sorted(ids)
    random.Random(seed).shuffle(ordered)

    total = len(ordered)
    n_train = _floor_count(train, total)
    n_val = min(_floor_count(val, total), total - n_train)
    n_test = total - n_train - n_val

    return {
        "train": ordered[:n_train],
        "val": ordered[n_train : n_train + n_val],
        "test": ordered[n_train + n_val : n_train + n_val + n_test],
    }
