"""Canonical candidates and coordinate geometry (GREENFIELD.md §6, §7.1).

A *candidate* is any region the screen offers as a possible target: an
accessibility node, a DOM element, an OCR word, or a visual contour.  Every
source reports the same box, so the candidate set is the union of what each
perception path saw and the scorer has one vocabulary to rank.

Coordinates are **canonical global top-left-origin pixels**: the space
:class:`keyless.platform.base.DisplayTopology` calls ``global_rect``.  Sources
that work in OS-native units (macOS points, for example) convert once with
:func:`to_global_bbox`; nothing downstream re-derives scale.

No model, no calibration, no legacy bias lives here.  This module is pure
geometry plus a frozen value type.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from keyless.gaze.metrics import bbox_contains
from keyless.platform.base import DisplayTopology

BBox = tuple[int, int, int, int]

#: Source order used when two boxes describe the same region.  Accessibility
#: and DOM nodes carry explicit semantics, OCR carries text, and visual
#: contours are the weakest evidence (they are inferred from pixels).
SOURCE_PRIORITY: dict[str, int] = {"ax": 3, "dom": 3, "ocr": 2, "visual": 1}

#: Id of the explicit "no target / free space" option.  The scorer needs a
#: candidate for abstention, not an absence it cannot rank.
NULL_CANDIDATE_ID = "__null__"


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number, got {value!r}")
    if not math.isfinite(value) or float(value) != int(value):
        raise ValueError(f"{where} must be a finite integer, got {value!r}")
    return int(value)


def _parse_bbox(bbox: Sequence[Any]) -> BBox | None:
    if isinstance(bbox, (str, bytes)) or not isinstance(bbox, Sequence) or len(bbox) != 4:
        return None
    try:
        return (
            _integer(bbox[0], "bbox.x"),
            _integer(bbox[1], "bbox.y"),
            _integer(bbox[2], "bbox.w"),
            _integer(bbox[3], "bbox.h"),
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class Candidate:
    """One possible target in canonical global top-left-origin pixels.

    ``bbox`` is ``(x, y, w, h)`` with non-negative extents.  ``source`` is one
    of ``SOURCE_PRIORITY`` and is part of the value's identity for dedup.
    ``confidence`` is the source's own certainty in ``[0, 1]``; it is not a
    probability over targets.
    """

    id: str
    bbox: BBox
    role: str
    text: str
    source: str
    confidence: float = 1.0
    visual_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("candidate id must be a non-empty string")
        if self.source not in SOURCE_PRIORITY:
            raise ValueError(
                f"candidate source must be one of {sorted(SOURCE_PRIORITY)}, got {self.source!r}"
            )
        parsed = _parse_bbox(self.bbox)
        if parsed is None:
            raise ValueError(f"candidate bbox must be four integers, got {self.bbox!r}")
        x, y, w, h = parsed
        if w < 0 or h < 0:
            raise ValueError(f"candidate bbox extents must be >= 0, got {self.bbox!r}")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError(f"candidate confidence must be in [0, 1], got {self.confidence!r}")
        object.__setattr__(self, "bbox", (x, y, w, h))
        object.__setattr__(self, "confidence", float(self.confidence))

    def as_event_dict(self) -> dict[str, Any]:
        """Schema-v0 ``screen.candidates`` entry (metrics/replay shape)."""
        return {
            "id": self.id,
            "bbox": list(self.bbox),
            "role": self.role,
            "text": self.text,
            "source": self.source,
            "score": self.confidence,
            "visual": self.visual_hash,
        }


# --- geometry -----------------------------------------------------------


def iou(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Intersection over union of two ``[x, y, w, h]`` boxes.

    Returns ``0.0`` for a disjoint pair, a zero-area box, or a malformed box:
    an empty region cannot overlap anything.
    """
    first = _parse_bbox(a)
    second = _parse_bbox(b)
    if first is None or second is None:
        return 0.0
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0

    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0

    intersection = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union


def offset_bbox(bbox: Sequence[Any], dx: int, dy: int) -> BBox:
    """Translate a box by ``(dx, dy)`` in canonical global pixels."""
    parsed = _parse_bbox(bbox)
    if parsed is None:
        raise ValueError(f"bbox must be four integers, got {bbox!r}")
    x, y, w, h = parsed
    return (_integer(dx, "dx") + x, _integer(dy, "dy") + y, w, h)


def scale_bbox(bbox: Sequence[Any], scale: float) -> BBox:
    """Multiply a box by ``scale`` and round to whole pixels.

    Rounding is half-to-even (Python's ``round``); the caller must not rely on
    half-pixel tie direction.  Used when a source reports native units and the
    canonical space is pixels (or the reverse).
    """
    parsed = _parse_bbox(bbox)
    if parsed is None:
        raise ValueError(f"bbox must be four integers, got {bbox!r}")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise ValueError(f"scale must be a number, got {scale!r}")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"scale must be finite and > 0, got {scale!r}")
    x, y, w, h = parsed
    return (
        round(x * scale),
        round(y * scale),
        round(w * scale),
        round(h * scale),
    )


def to_global_bbox(bbox: Sequence[Any], topology: DisplayTopology) -> BBox:
    """Convert a display-native box to canonical global pixels.

    Contract: ``bbox`` is expressed in ``topology.native_rect`` units (macOS
    points on a Retina display, for example) and relative to that display's
    origin.  The result is ``global_rect`` pixels: each edge is scaled by
    ``topology.scale`` and then translated by ``global_rect``'s origin.  The
    translation happens after scaling because the origin is already in global
    pixels and must not be multiplied by the display scale.
    """
    scaled = scale_bbox(bbox, topology.scale)
    origin_x, origin_y, _width, _height = topology.global_rect
    return (scaled[0] + origin_x, scaled[1] + origin_y, scaled[2], scaled[3])


# --- null option and dedup ----------------------------------------------


def null_candidate() -> Candidate:
    """The explicit free-space option: zero area, zero confidence, role null.

    It is a real candidate so a scorer can *choose* to act on nothing instead
    of being forced to rank a region the user did not mean.
    """
    return Candidate(
        id=NULL_CANDIDATE_ID,
        bbox=(0, 0, 0, 0),
        role="null",
        text="",
        source="visual",
        confidence=0.0,
    )


def with_null(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Append the null candidate unless it is already present.

    Idempotent and order-preserving: calling it twice returns the same ids in
    the same order, so a caller can apply it defensively.
    """
    items = list(candidates)
    if any(candidate.id == NULL_CANDIDATE_ID for candidate in items):
        return items
    items.append(null_candidate())
    return items


def deduplicate(candidates: Iterable[Candidate], iou_threshold: float = 0.5) -> list[Candidate]:
    """Drop overlapping duplicates, keeping the strongest source's box.

    Order is greedy: ``(SOURCE_PRIORITY desc, confidence desc, id asc)``.  A
    candidate is dropped when its IoU with any already-kept box is
    ``>= iou_threshold``.  The winner keeps its own fields unchanged — no
    field is merged across sources, so the surviving box is always traceable
    to one perception path.  Output preserves the sort order, which makes it
    deterministic.
    """
    if (
        isinstance(iou_threshold, bool)
        or not isinstance(iou_threshold, (int, float))
        or not math.isfinite(iou_threshold)
        or not 0.0 <= float(iou_threshold) <= 1.0
    ):
        raise ValueError(f"iou_threshold must be in [0, 1], got {iou_threshold!r}")
    threshold = float(iou_threshold)

    ordered = sorted(
        candidates,
        key=lambda candidate: (
            -SOURCE_PRIORITY[candidate.source],
            -candidate.confidence,
            candidate.id,
        ),
    )
    kept: list[Candidate] = []
    for candidate in ordered:
        if any(iou(candidate.bbox, winner.bbox) >= threshold for winner in kept):
            continue
        kept.append(candidate)
    return kept


__all__ = [
    "BBox",
    "Candidate",
    "NULL_CANDIDATE_ID",
    "SOURCE_PRIORITY",
    "bbox_contains",
    "deduplicate",
    "iou",
    "null_candidate",
    "offset_bbox",
    "scale_bbox",
    "to_global_bbox",
    "with_null",
]
