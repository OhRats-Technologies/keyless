"""Decision metrics and Baseline 0, including the §5.1 MSE counterexample."""

from __future__ import annotations

import copy

import pytest

from keyless.gaze.metrics import (
    NearestCandidateToPixelBaseline,
    abstention_rate,
    actions_per_correction,
    bbox_centroid,
    bbox_contains,
    candidate_recall_at_k,
    hit_rate_at_px,
    hit_rates,
    metric_table,
    misclick_rate_per_hour,
    top1_target_accuracy,
    top3_recall,
)
from keyless.gaze.schema import make_event


def _screen(candidates: list[dict]) -> dict:
    return {
        "display_id": 1,
        "app": "Chrome",
        "window_id": 42,
        "screenshot": "a" * 64,
        "candidates": candidates,
    }


def _candidate(cid: str, score: float, bbox: list[float]) -> dict:
    return {"id": cid, "bbox": bbox, "role": "button", "text": cid, "score": score}


def _session() -> list[dict]:
    """Five events with hand-computed metric values.

    E1 click, target c1 ranked #1, 2.24 px from the predicted pixel, accepted.
    E2 click, target c1 ranked #3, 40 px from the predicted pixel, rejected.
    E3 abstaining voice action, accepted.
    E4 correction with a prediction and no screen.
    E5 frame-only event so the session spans 4 s.
    """
    return [
        make_event(
            "metrics",
            1_000_000_000,
            screen=_screen(
                [
                    _candidate("c1", 0.9, [0.0, 0.0, 20.0, 20.0]),
                    _candidate("c2", 0.5, [30.0, 0.0, 20.0, 20.0]),
                    _candidate("c3", 0.3, [60.0, 0.0, 20.0, 20.0]),
                ]
            ),
            prediction={"top": "c1", "p": 0.9, "entropy": 0.3, "pixel": [10.0, 10.0]},
            interaction={
                "kind": "click",
                "mouse": [12.0, 11.0],
                "target": "c1",
                "accepted": True,
                "latency_ms": 100,
            },
        ),
        make_event(
            "metrics",
            2_000_000_000,
            screen=_screen(
                [
                    _candidate("c1", 0.2, [90.0, 0.0, 20.0, 20.0]),
                    _candidate("c2", 0.95, [30.0, 0.0, 20.0, 20.0]),
                    _candidate("c3", 0.6, [60.0, 0.0, 20.0, 20.0]),
                ]
            ),
            prediction={"top": "c3", "p": 0.6, "entropy": 1.0, "pixel": [100.0, 100.0]},
            interaction={
                "kind": "click",
                "mouse": [140.0, 100.0],
                "target": "c1",
                "accepted": False,
                "latency_ms": 80,
            },
        ),
        make_event(
            "metrics",
            3_000_000_000,
            screen=_screen(
                [
                    _candidate("c1", 0.1, [0.0, 0.0, 10.0, 10.0]),
                    _candidate("c2", 0.2, [10.0, 0.0, 10.0, 10.0]),
                    _candidate("c3", 0.3, [20.0, 0.0, 10.0, 10.0]),
                    _candidate("c4", 0.95, [30.0, 0.0, 10.0, 10.0]),
                ]
            ),
            prediction={"top": None, "p": 0.0, "entropy": 2.0, "pixel": [50.0, 50.0]},
            interaction={"kind": "voice", "voice": "click reply", "accepted": True},
        ),
        make_event(
            "metrics",
            4_000_000_000,
            prediction={"top": "c1", "p": 0.4, "entropy": 0.9},
            interaction={"kind": "correction", "target": "c2", "accepted": False},
        ),
        make_event("metrics", 5_000_000_000, frame={"cams": ["cam1"]}),
    ]


def test_metric_values_are_exact() -> None:
    events = _session()
    assert candidate_recall_at_k(events, 1) == 0.5
    assert candidate_recall_at_k(events, 2) == 0.5
    assert candidate_recall_at_k(events, 3) == 1.0
    assert candidate_recall_at_k(events) == 1.0
    assert top1_target_accuracy(events) == 0.5
    assert top3_recall(events) == 1.0
    assert hit_rate_at_px(events, 0.0) == 0.0
    assert hit_rate_at_px(events, 25.0) == 0.5
    assert hit_rate_at_px(events, 50.0) == 1.0
    assert hit_rate_at_px(events, 100.0) == 1.0
    assert hit_rates(events) == {25: 0.5, 50: 1.0, 100: 1.0}
    assert misclick_rate_per_hour(events) == 900.0
    assert abstention_rate(events) == 0.25
    assert actions_per_correction(events) == 1.0


def test_metric_table_includes_baseline() -> None:
    assert metric_table(_session()) == {
        "candidate_recall@3": 1.0,
        "top1": 0.5,
        "top3": 1.0,
        "hit@25": 0.5,
        "hit@50": 1.0,
        "hit@100": 1.0,
        "misclicks_per_hour": 900.0,
        "abstention": 0.25,
        "actions_per_correction": 1.0,
        "baseline_top1": 1.0,
    }


def test_empty_populations_return_none_not_zero() -> None:
    assert candidate_recall_at_k([]) is None
    assert top1_target_accuracy([]) is None
    assert top3_recall([]) is None
    assert hit_rate_at_px([], 25.0) is None
    assert hit_rates([]) == {25: None, 50: None, 100: None}
    assert misclick_rate_per_hour([]) is None
    assert abstention_rate([]) is None
    assert actions_per_correction([]) is None
    assert set(metric_table([]).values()) == {None}


def test_unlabelled_clicks_are_not_a_zero_score() -> None:
    unlabelled = make_event(
        "m",
        1,
        screen=_screen([_candidate("c1", 0.9, [0.0, 0.0, 10.0, 10.0])]),
        prediction={"top": "c1", "p": 0.9, "pixel": [1.0, 1.0]},
        interaction={"kind": "click", "mouse": [1.0, 1.0], "target": None, "accepted": True},
    )
    assert candidate_recall_at_k([unlabelled]) is None
    assert top1_target_accuracy([unlabelled]) is None
    assert hit_rate_at_px([unlabelled], 25.0) == 1.0


def test_single_event_has_no_duration() -> None:
    event = make_event("m", 7, prediction={"top": None})
    assert misclick_rate_per_hour([event]) is None


def test_misclick_counts_only_rejected_clicks() -> None:
    start = 0
    end = 3_600_000_000_000  # one hour
    events = [
        make_event("m", start, interaction={"kind": "correction", "accepted": False}),
        make_event("m", start + 1, interaction={"kind": "click", "accepted": False}),
        make_event("m", start + 2, interaction={"kind": "click", "accepted": True}),
        make_event("m", end, frame={"cams": ["cam1"]}),
    ]
    assert misclick_rate_per_hour(events) == 1.0


def test_actions_per_correction_floors_the_denominator() -> None:
    clean = [
        make_event("m", 1, interaction={"kind": "click", "accepted": True}),
        make_event("m", 2, interaction={"kind": "voice", "accepted": True}),
    ]
    assert actions_per_correction(clean) == 2.0

    only_corrections = [
        make_event("m", 1, interaction={"kind": "correction", "accepted": True}),
        make_event("m", 2, interaction={"kind": "click", "accepted": False}),
    ]
    assert actions_per_correction(only_corrections) == 0.0


# --- Baseline 0 ---------------------------------------------------------


def _pixel_event(candidates: list[dict], pixel: list[float] | None) -> dict:
    prediction: dict = {"top": "x"}
    if pixel is not None:
        prediction["pixel"] = pixel
    return make_event("b", 1, screen=_screen(candidates), prediction=prediction)


def test_baseline_prefers_the_containing_candidate() -> None:
    event = _pixel_event(
        [
            _candidate("a", 0.2, [0.0, 0.0, 10.0, 10.0]),
            _candidate("b", 0.9, [20.0, 0.0, 10.0, 10.0]),
        ],
        [5.0, 5.0],
    )
    assert NearestCandidateToPixelBaseline().predict(event) == "a"


def test_baseline_containment_tie_breaks_on_score() -> None:
    event = _pixel_event(
        [
            _candidate("outer", 0.1, [0.0, 0.0, 20.0, 20.0]),
            _candidate("inner", 0.9, [4.0, 4.0, 8.0, 8.0]),
        ],
        [5.0, 5.0],
    )
    assert NearestCandidateToPixelBaseline().predict(event) == "inner"


def test_baseline_falls_back_to_nearest_centroid() -> None:
    event = _pixel_event(
        [
            _candidate("a", 0.2, [0.0, 0.0, 10.0, 10.0]),  # centroid (5, 5)
            _candidate("b", 0.9, [20.0, 0.0, 10.0, 10.0]),  # centroid (25, 5)
        ],
        [12.0, 5.0],
    )
    assert NearestCandidateToPixelBaseline().predict(event) == "a"


def test_baseline_distance_tie_breaks_on_score_then_id() -> None:
    candidates = [
        _candidate("a", 0.2, [2.0, 0.0, 2.0, 2.0]),  # centroid (3, 1)
        _candidate("b", 0.8, [0.0, 2.0, 2.0, 2.0]),  # centroid (1, 3)
    ]
    event = _pixel_event(candidates, [0.0, 0.0])
    # Both centroids are sqrt(10) away; the higher score wins.
    assert NearestCandidateToPixelBaseline().predict(event) == "b"

    tied = [
        _candidate("a", 0.5, [2.0, 0.0, 2.0, 2.0]),
        _candidate("b", 0.5, [0.0, 2.0, 2.0, 2.0]),
    ]
    assert NearestCandidateToPixelBaseline().predict(_pixel_event(tied, [0.0, 0.0])) == "a"


def test_baseline_returns_none_without_pixel_or_candidates() -> None:
    baseline = NearestCandidateToPixelBaseline()
    assert baseline.predict(_pixel_event([], [1.0, 1.0])) is None
    assert baseline.predict(_pixel_event([_candidate("a", 0.5, [0.0, 0.0, 2.0, 2.0])], None)) is None
    assert baseline.predict(make_event("b", 1, prediction={"top": "a"})) is None


def test_baseline_evaluate_substitutes_top_without_mutating() -> None:
    events = _session()
    before = copy.deepcopy(events)
    result = NearestCandidateToPixelBaseline().evaluate(events)

    assert events == before
    # E2's model top was wrong (c3 vs target c1); the baseline pixel lands in c1.
    assert result["top1"] == 1.0
    assert result["abstention"] == 0.25  # E4 has no screen, so the baseline abstains
    assert result["hit@25"] == 0.5


# --- GREENFIELD §5.1 ----------------------------------------------------


def test_pixel_mse_is_not_decision_sufficient() -> None:
    """Two equal-mass targets A=[-10,-2]x[-1,1] and B=[2,10]x[-1,1] (§5.1, A.1).

    The posterior mean (0, 0) has zero pixel MSE but lies in neither target
    (target risk 1.0); (-6, 0) lies in A (risk 0.5) with pixel MSE 36.  The
    decision metric therefore prefers the higher-MSE point, which is why the
    offline suite scores decisions, never pixel error.
    """
    candidates = [
        {"id": "A", "bbox": [-10.0, -1.0, 8.0, 2.0], "score": 0.5},
        {"id": "B", "bbox": [2.0, -1.0, 8.0, 2.0], "score": 0.5},
    ]

    def risk(x: float, y: float) -> float:
        # 0-1 target risk: 1 - sum_c 1[x in R_c] P(c), with P(A) = P(B) = 0.5.
        return 1.0 - sum(0.5 for candidate in candidates if bbox_contains(candidate["bbox"], x, y))

    mean_point = (0.0, 0.0)
    in_target_point = (-6.0, 0.0)

    assert not bbox_contains(candidates[0]["bbox"], *mean_point)
    assert not bbox_contains(candidates[1]["bbox"], *mean_point)
    assert bbox_contains(candidates[0]["bbox"], *in_target_point)
    assert not bbox_contains(candidates[1]["bbox"], *in_target_point)

    assert risk(*mean_point) == 1.0
    assert risk(*in_target_point) == 0.5
    assert risk(*in_target_point) < risk(*mean_point)

    # Pixel MSE about the posterior mean: 0 for the mean, 36 for (-6, 0).
    assert mean_point[0] ** 2 + mean_point[1] ** 2 == 0.0
    assert (in_target_point[0] - 0.0) ** 2 + (in_target_point[1] - 0.0) ** 2 == 36.0

    # Baseline 0 on the mean pixel: equidistant centroids (6.0 each), so the
    # tie falls to the smallest id. A tie is arbitrary evidence — the mean
    # still belongs to no target, which is exactly the §5.1 failure.
    baseline_pick = NearestCandidateToPixelBaseline().predict(
        make_event(
            "counterexample",
            1,
            screen=_screen(candidates),
            prediction={"top": None, "pixel": list(mean_point)},
        )
    )
    assert baseline_pick == "A"
    assert bbox_centroid(candidates[0]["bbox"]) == (-6.0, 0.0)
    assert bbox_centroid(candidates[1]["bbox"]) == (6.0, 0.0)
