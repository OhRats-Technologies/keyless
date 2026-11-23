"""Canonical candidate model, geometry, null option, and deterministic dedup."""

from __future__ import annotations

import pytest

from keyless.gaze.candidates import (
    NULL_CANDIDATE_ID,
    SOURCE_PRIORITY,
    Candidate,
    bbox_contains,
    deduplicate,
    iou,
    null_candidate,
    offset_bbox,
    scale_bbox,
    to_global_bbox,
    with_null,
)
from keyless.platform.base import DisplayTopology


def _candidate(cid: str, bbox, source: str = "ax", confidence: float = 1.0) -> Candidate:
    return Candidate(
        id=cid,
        bbox=bbox,
        role="button",
        text=cid,
        source=source,
        confidence=confidence,
    )


# --- Candidate validation ------------------------------------------------


def test_candidate_accepts_valid_source_and_bbox() -> None:
    candidate = _candidate("c1", (1, 2, 3, 4), "dom", 0.9)
    assert candidate.bbox == (1, 2, 3, 4)
    assert candidate.source == "dom"
    assert candidate.confidence == 0.9


@pytest.mark.parametrize("source", ["", "AX", "ocr ", "mouse", "null", "visual2"])
def test_candidate_rejects_unknown_source(source: str) -> None:
    with pytest.raises(ValueError, match="source"):
        _candidate("c1", (0, 0, 10, 10), source)


def test_candidate_rejects_negative_extents() -> None:
    with pytest.raises(ValueError, match="extents"):
        _candidate("c1", (0, 0, -1, 10))
    with pytest.raises(ValueError, match="extents"):
        _candidate("c1", (0, 0, 10, -1))


@pytest.mark.parametrize(
    "bbox",
    [(0, 0, 10), (0, 0, 10, 10, 10), ("0", 0, 10, 10), (0.5, 0, 10, 10), (0, True, 10, 10)],
)
def test_candidate_rejects_malformed_bbox(bbox) -> None:
    with pytest.raises(ValueError):
        _candidate("c1", bbox)


def test_candidate_rejects_empty_id_and_bad_confidence() -> None:
    with pytest.raises(ValueError, match="id"):
        _candidate("", (0, 0, 1, 1))
    with pytest.raises(ValueError, match="confidence"):
        _candidate("c", (0, 0, 1, 1), "ax", 1.5)
    with pytest.raises(ValueError, match="confidence"):
        _candidate("c", (0, 0, 1, 1), "ax", float("nan"))


def test_candidate_event_dict_shape() -> None:
    candidate = Candidate("cup", (1, 2, 3, 4), "button", "cup", "ocr", 0.8, "abc")
    assert candidate.as_event_dict() == {
        "id": "cup",
        "bbox": [1, 2, 3, 4],
        "role": "button",
        "text": "cup",
        "source": "ocr",
        "score": 0.8,
        "visual": "abc",
    }


def test_source_priority_matches_the_documented_order() -> None:
    assert SOURCE_PRIORITY == {"ax": 3, "dom": 3, "ocr": 2, "visual": 1}


# --- geometry ------------------------------------------------------------


def test_iou_identical_is_one_and_disjoint_is_zero() -> None:
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 0, 10, 10)) == 0.0


def test_iou_half_overlap_is_hand_computed_one_third() -> None:
    # intersection 5*10 = 50; union 100 + 100 - 50 = 150.
    assert iou((0, 0, 10, 10), (5, 0, 10, 10)) == pytest.approx(50 / 150)


def test_iou_zero_area_and_malformed_is_zero() -> None:
    assert iou((0, 0, 0, 10), (0, 0, 10, 10)) == 0.0
    assert iou((0, 0, 10), (0, 0, 10, 10)) == 0.0


def test_offset_bbox() -> None:
    assert offset_bbox((10, 20, 30, 40), 5, -5) == (15, 15, 30, 40)


def test_scale_bbox_doubles_and_rounds() -> None:
    assert scale_bbox((10, 20, 30, 40), 2.0) == (20, 40, 60, 80)
    assert scale_bbox((1, 1, 1, 1), 1.5) == (2, 2, 2, 2)
    with pytest.raises(ValueError, match="scale"):
        scale_bbox((0, 0, 1, 1), 0)


def _retina_topology() -> DisplayTopology:
    return DisplayTopology(
        display_id="main",
        global_rect=(100, 50, 1920, 1080),
        native_rect=(50, 25, 960, 540),
        work_area=(100, 50, 1920, 1040),
        scale=2.0,
        primary=True,
    )


def test_to_global_bbox_scales_then_translates() -> None:
    topology = _retina_topology()
    assert to_global_bbox((10, 20, 30, 40), topology) == (120, 90, 60, 80)


def test_to_global_bbox_with_non_zero_origin_and_unit_scale() -> None:
    topology = DisplayTopology(
        display_id="left",
        global_rect=(-1920, 0, 1920, 1080),
        native_rect=(-1920, 0, 1920, 1080),
        work_area=(-1920, 0, 1920, 1080),
        scale=1.0,
    )
    assert to_global_bbox((10, 10, 100, 20), topology) == (-1910, 10, 100, 20)


def test_bbox_contains_is_re_exported_from_metrics() -> None:
    assert bbox_contains((0, 0, 10, 10), 10, 10) is True
    assert bbox_contains((0, 0, 10, 10), 11, 10) is False


# --- dedup ---------------------------------------------------------------


def test_dedup_prefers_ax_over_ocr_over_visual() -> None:
    box = (0, 0, 10, 10)
    kept = deduplicate(
        [
            _candidate("v", box, "visual", 0.5),
            _candidate("o", box, "ocr", 0.8),
            _candidate("a", box, "ax", 1.0),
        ]
    )
    assert [candidate.id for candidate in kept] == ["a"]


def test_dedup_keeps_boxes_below_threshold() -> None:
    kept = deduplicate(
        [_candidate("a", (0, 0, 10, 10), "ax"), _candidate("o", (9, 0, 10, 10), "ocr", 0.8)]
    )
    assert [candidate.id for candidate in kept] == ["a", "o"]


def test_dedup_confidence_breaks_ties_within_a_source() -> None:
    kept = deduplicate(
        [
            _candidate("low", (0, 0, 10, 10), "ocr", 0.6),
            _candidate("high", (0, 0, 10, 10), "ocr", 0.9),
        ]
    )
    assert [candidate.id for candidate in kept] == ["high"]
    assert kept[0].confidence == 0.9


def test_dedup_same_priority_ties_break_on_id() -> None:
    kept = deduplicate(
        [_candidate("b", (0, 0, 10, 10), "ax"), _candidate("a", (0, 0, 10, 10), "dom")]
    )
    assert [candidate.id for candidate in kept] == ["a"]


def test_dedup_is_deterministic_across_input_order() -> None:
    items = [
        _candidate("z", (0, 0, 10, 10), "visual", 0.5),
        _candidate("b", (0, 0, 10, 10), "ocr", 0.8),
        _candidate("a", (0, 0, 10, 10), "ax", 1.0),
        _candidate("far", (100, 100, 5, 5), "ocr", 0.8),
    ]
    assert [c.id for c in deduplicate(items)] == [c.id for c in deduplicate(reversed(items))]


def test_dedup_threshold_is_inclusive() -> None:
    # Identical boxes give IoU 1.0, which is >= any threshold in [0, 1].
    kept = deduplicate(
        [_candidate("a", (0, 0, 10, 10), "ax"), _candidate("o", (0, 0, 10, 10), "ocr", 0.8)],
        iou_threshold=1.0,
    )
    assert [candidate.id for candidate in kept] == ["a"]


def test_dedup_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError, match="iou_threshold"):
        deduplicate([], iou_threshold=1.5)


# --- null candidate ------------------------------------------------------


def test_null_candidate_is_zero_area_free_space() -> None:
    null = null_candidate()
    assert null.id == NULL_CANDIDATE_ID
    assert null.bbox == (0, 0, 0, 0)
    assert null.role == "null"
    assert null.confidence == 0.0


def test_with_null_appends_once_and_is_idempotent() -> None:
    items = [_candidate("a", (0, 0, 10, 10))]
    once = with_null(items)
    twice = with_null(once)
    assert [candidate.id for candidate in once] == ["a", NULL_CANDIDATE_ID]
    assert [candidate.id for candidate in twice] == ["a", NULL_CANDIDATE_ID]
    assert len(twice) == 2


def test_with_null_can_receive_an_iterator() -> None:
    items = (_candidate(cid, (0, 0, 1, 1)) for cid in ("a", "b"))
    assert [candidate.id for candidate in with_null(items)] == ["a", "b", NULL_CANDIDATE_ID]
