"""Offline decision metrics for gaze sessions (GREENFIELD.md §9).

Every function takes an iterable of schema-v0 events (already validated) and is
pure: no model, no training, no legacy calibration.  Metrics return ``None``
rather than ``0.0`` when a population is empty, so "no data" and "always wrong"
stay distinguishable in a report.

Definitions live in each docstring because the numbers are the contract between
instrumentation and every later model claim.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Event = Mapping[str, Any]


# --- shared helpers -----------------------------------------------------


def _candidates(event: Event) -> list[Mapping[str, Any]]:
    screen = event.get("screen")
    if not isinstance(screen, Mapping):
        return []
    candidates = screen.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, Mapping)]


def _score(candidate: Mapping[str, Any]) -> float:
    value = candidate.get("score", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("id", ""))


def _ranked(candidates: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Sort by score descending; ties break on id so order is deterministic."""
    return sorted(candidates, key=lambda candidate: (-_score(candidate), _candidate_id(candidate)))


def _click_events(events: Iterable[Event]) -> list[Event]:
    clicks: list[Event] = []
    for event in events:
        interaction = event.get("interaction")
        if isinstance(interaction, Mapping) and interaction.get("kind") == "click":
            clicks.append(event)
    return clicks


def _point(value: Any) -> tuple[float, float] | None:
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
        and all(
            not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(item)
            for item in value
        )
    ):
        return float(value[0]), float(value[1])
    return None


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 4
        and all(
            not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(item)
            for item in value
        )
    ):
        x, y, w, h = (float(item) for item in value)
        return x, y, w, h
    return None


def bbox_contains(bbox: Sequence[float], x: float, y: float) -> bool:
    """Closed-interval containment test for a ``[x, y, w, h]`` box."""
    parsed = _bbox(bbox)
    if parsed is None:
        return False
    left, top, width, height = parsed
    return left <= x <= left + width and top <= y <= top + height


def bbox_centroid(bbox: Sequence[float]) -> tuple[float, float]:
    """Centre of a ``[x, y, w, h]`` box.  Raises ``ValueError`` when malformed."""
    parsed = _bbox(bbox)
    if parsed is None:
        raise ValueError(f"bbox must be [x, y, w, h] with four numbers, got {bbox!r}")
    left, top, width, height = parsed
    return left + width / 2.0, top + height / 2.0


# --- decision metrics ---------------------------------------------------


def candidate_recall_at_k(events: Iterable[Event], k: int = 3) -> float | None:
    """Fraction of labelled clicks whose target is among the top ``k`` candidates.

    Population: ``interaction.kind == "click"`` with a non-null
    ``interaction.target`` and at least one ``screen.candidates`` entry.
    Candidates rank by ``score`` descending (ties by ``id``).  Returns ``None``
    when there are no labelled clicks — the candidate generator's recall gate
    (§8 Phase 1) has no meaning without labels.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    events = list(events)
    hits = 0
    total = 0
    for event in _click_events(events):
        target = event["interaction"].get("target")
        candidates = _candidates(event)
        if target is None or not candidates:
            continue
        total += 1
        if any(_candidate_id(candidate) == target for candidate in _ranked(candidates)[:k]):
            hits += 1
    if total == 0:
        return None
    return hits / total


def top1_target_accuracy(events: Iterable[Event]) -> float | None:
    """Fraction of labelled clicks where ``prediction.top`` equals the target.

    Population: click events with a non-null ``interaction.target`` and a
    ``prediction`` section.  This is the decision metric (§5.1), not pixel
    error.  Returns ``None`` when there are no such clicks.
    """
    events = list(events)
    hits = 0
    total = 0
    for event in _click_events(events):
        target = event["interaction"].get("target")
        prediction = event.get("prediction")
        if target is None or not isinstance(prediction, Mapping):
            continue
        total += 1
        if prediction.get("top") == target:
            hits += 1
    if total == 0:
        return None
    return hits / total


def top3_recall(events: Iterable[Event]) -> float | None:
    """Fraction of labelled clicks whose target is in the top 3 candidates.

    Same population and ranking as :func:`candidate_recall_at_k` with ``k=3``.
    """
    return candidate_recall_at_k(events, 3)


def hit_rate_at_px(events: Iterable[Event], px: float) -> float | None:
    """Fraction of clicks whose predicted pixel lands within ``px`` pixels.

    Population: click events with ``prediction.pixel`` and ``interaction.mouse``.
    Distance is Euclidean in screen pixels and compared with ``<=``.  ``None``
    when no click has both points.
    """
    if px < 0:
        raise ValueError("px must be non-negative")
    events = list(events)
    hits = 0
    total = 0
    for event in _click_events(events):
        prediction = event.get("prediction")
        mouse = event["interaction"].get("mouse")
        if not isinstance(prediction, Mapping):
            continue
        pixel = _point(prediction.get("pixel"))
        actual = _point(mouse)
        if pixel is None or actual is None:
            continue
        total += 1
        if math.hypot(pixel[0] - actual[0], pixel[1] - actual[1]) <= px:
            hits += 1
    if total == 0:
        return None
    return hits / total


def hit_rates(events: Iterable[Event]) -> dict[int, float | None]:
    """:func:`hit_rate_at_px` at the standard 25/50/100 px tolerances."""
    events = list(events)
    return {px: hit_rate_at_px(events, float(px)) for px in (25, 50, 100)}


def misclick_rate_per_hour(events: Iterable[Event]) -> float | None:
    """Misclicks per hour of session time.

    A misclick is a click event with ``interaction.accepted is False``.
    Duration is ``(max ts_ns - min ts_ns)`` over *all* events, converted to
    hours.  Returns ``None`` when there are no events or the duration is zero.
    """
    events = list(events)
    if not events:
        return None
    timestamps = [
        event["ts_ns"]
        for event in events
        if isinstance(event.get("ts_ns"), int) and not isinstance(event.get("ts_ns"), bool)
    ]
    if not timestamps:
        return None
    duration_hours = (max(timestamps) - min(timestamps)) / 1e9 / 3600.0
    if duration_hours <= 0:
        return None
    misclicks = sum(
        1 for event in _click_events(events) if event["interaction"].get("accepted") is False
    )
    return misclicks / duration_hours


def abstention_rate(events: Iterable[Event]) -> float | None:
    """Fraction of predictions that abstain (``prediction.top is None``).

    Population: every event with a ``prediction`` section.  Abstention is a
    feature under uncertainty (§5.5), so it is reported, not penalised here.
    ``None`` when there are no predictions.
    """
    total = 0
    abstained = 0
    for event in events:
        prediction = event.get("prediction")
        if not isinstance(prediction, Mapping):
            continue
        total += 1
        if prediction.get("top") is None:
            abstained += 1
    if total == 0:
        return None
    return abstained / total


def actions_per_correction(events: Iterable[Event]) -> float | None:
    """Accepted actions divided by user corrections, floored at 1 correction.

    Actions: interaction events with ``kind`` in ``{"click", "voice"}`` and
    ``accepted is True``.  Corrections: ``kind == "correction"`` or a click with
    ``accepted is False``.  The denominator is ``max(1, corrections)`` so a
    perfect session reports its action count instead of dividing by zero.
    ``None`` only when there are neither actions nor corrections.
    """
    actions = 0
    corrections = 0
    for event in events:
        interaction = event.get("interaction")
        if not isinstance(interaction, Mapping):
            continue
        kind = interaction.get("kind")
        accepted = interaction.get("accepted")
        if kind in ("click", "voice") and accepted is True:
            actions += 1
        if kind == "correction" or (kind == "click" and accepted is False):
            corrections += 1
    if actions == 0 and corrections == 0:
        return None
    return actions / max(1, corrections)


# --- Baseline 0 ---------------------------------------------------------


@dataclass(frozen=True)
class NearestCandidateToPixelBaseline:
    """Baseline 0: pick the candidate nearest to the predicted pixel.

    The decision rule a model must beat is deliberately dumb:

    1. if one or more candidate boxes contain the pixel, choose the one with the
       highest score (ties by smallest id);
    2. otherwise choose the nearest box centroid by Euclidean distance (ties by
       higher score, then id);
    3. ``None`` when there is no pixel or no candidate.

    It is a *decision* rule, not a fit.  It has no parameters and never learns.
    """

    def predict(self, event: Event) -> str | None:
        prediction = event.get("prediction")
        if not isinstance(prediction, Mapping):
            return None
        pixel = _point(prediction.get("pixel"))
        if pixel is None:
            return None

        candidates = [
            candidate for candidate in _candidates(event) if _bbox(candidate.get("bbox")) is not None
        ]
        if not candidates:
            return None

        x, y = pixel
        containing = [
            candidate for candidate in candidates if bbox_contains(candidate["bbox"], x, y)
        ]
        if containing:
            best = min(
                containing, key=lambda candidate: (-_score(candidate), _candidate_id(candidate))
            )
            return _candidate_id(best)

        def distance(candidate: Mapping[str, Any]) -> float:
            centre_x, centre_y = bbox_centroid(candidate["bbox"])
            return math.hypot(centre_x - x, centre_y - y)

        nearest = min(
            candidates,
            key=lambda candidate: (distance(candidate), -_score(candidate), _candidate_id(candidate)),
        )
        return _candidate_id(nearest)

    def evaluate(self, events: Iterable[Event]) -> dict[str, float | None]:
        """Run the metric suite with ``prediction.top`` replaced by this baseline.

        The predicted pixel is left untouched, so hit rates stay comparable with
        the model's.  Input events are not mutated.
        """
        derived: list[dict[str, Any]] = []
        for event in events:
            prediction = event.get("prediction")
            if isinstance(prediction, Mapping):
                derived.append(
                    {**event, "prediction": {**prediction, "top": self.predict(event)}}
                )
            else:
                derived.append(dict(event))
        return _metric_suite(derived)


# --- suite --------------------------------------------------------------


def _metric_suite(events: Sequence[Event]) -> dict[str, float | None]:
    events = list(events)
    rates = hit_rates(events)
    return {
        "candidate_recall@3": candidate_recall_at_k(events, 3),
        "top1": top1_target_accuracy(events),
        "top3": top3_recall(events),
        "hit@25": rates[25],
        "hit@50": rates[50],
        "hit@100": rates[100],
        "misclicks_per_hour": misclick_rate_per_hour(events),
        "abstention": abstention_rate(events),
        "actions_per_correction": actions_per_correction(events),
    }


def metric_table(events: Iterable[Event]) -> dict[str, float | None]:
    """All session metrics plus ``baseline_top1`` for the nearest-candidate baseline.

    A model is only worth training if its ``top1`` beats ``baseline_top1`` on
    session-held-out data (§9).
    """
    events = list(events)
    table = _metric_suite(events)
    table["baseline_top1"] = NearestCandidateToPixelBaseline().evaluate(events)["top1"]
    return table
