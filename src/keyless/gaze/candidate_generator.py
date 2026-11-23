"""Unified candidate generator (GREENFIELD.md Phase 1).

One candidate list per screen state, united from every perception path that
is actually available: the platform accessibility tree, the Firefox DOM
bridge, OCR, and visual contours.  Sources are INJECTED, never constructed
here, so the generator runs identically on a laptop, in a test, and offline
against a stored screenshot.

Honesty rules:

- An unsupported OS bridge (``available()`` false, or ``NotSupportedError``)
  is reported as ``sources["ax"] = False`` and the remaining sources still
  run.  Nothing is faked and nothing crashes the union.
- OCR and visual sources need pixels; without an image they are reported
  unavailable, not silently skipped.
- The cache is keyed by ``(display_id, active_window_id, screenshot_hash)``
  plus an image-present flag, and bounded (LRU).  A cache hit reuses the
  deduplicated list AND the source availability report from the generation
  that produced it.
- The explicit null/free-space candidate is appended on return, never stored
  in the cache, so scoring always has an abstention option.

No model, no calibration, no legacy bias.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from keyless.gaze.candidates import (
    BBox,
    Candidate,
    deduplicate,
    with_null,
)

#: Default synthesized extent for DOM candidates: the bridge reports a point,
#: not a rect.  This is a documented approximation, centered on the point.
DEFAULT_DOM_BBOX_SIZE: tuple[int, int] = (120, 32)

SourceItem = Any  # (bbox, text[, confidence]) tuple or UIBoundingBox-like


@dataclass(frozen=True)
class GenerationResult:
    """One generation outcome: candidates, honest source report, timing."""

    candidates: list[Candidate]
    sources: dict[str, bool]
    latency_ms: float
    cache_hit: bool


def _coerce_item(item: SourceItem, source: str, default_confidence: float) -> Candidate | None:
    """Accept a ``(bbox, text[, confidence])`` tuple or a UIBoundingBox-like.

    UIBoundingBox (``keyless.vision.ui_detector``) carries global x1/y1/x2/y2
    corners and a ``label``; tuples carry ``[x, y, w, h]`` extents.  Malformed
    items are dropped (return None) rather than crashing the union.
    """
    try:
        if hasattr(item, "global_x1"):
            bbox: BBox = (
                int(item.global_x1),
                int(item.global_y1),
                max(0, int(item.global_x2) - int(item.global_x1)),
                max(0, int(item.global_y2) - int(item.global_y1)),
            )
            text = str(getattr(item, "label", "") or "")
            confidence = default_confidence
            cid = f"{source}-{getattr(item, 'mark_id', text)}"
        elif isinstance(item, (tuple, list)) and len(item) in (2, 3):
            raw_bbox = item[0]
            bbox = (int(raw_bbox[0]), int(raw_bbox[1]), int(raw_bbox[2]), int(raw_bbox[3]))
            text = str(item[1] or "")
            confidence = float(item[2]) if len(item) == 3 else default_confidence
            cid = f"{source}-{text or bbox}"
        else:
            return None
        return Candidate(
            id=cid,
            bbox=bbox,
            role="text" if source == "ocr" else "region",
            text=text,
            source=source,
            confidence=confidence,
        )
    except (TypeError, ValueError, IndexError, KeyError):
        return None


class CandidateGenerator:
    """Union of available perception sources, deduplicated and cached.

    Parameters
    ----------
    ax_bridge:
        Object with ``available() -> bool`` and ``scan_active_window()``
        returning nodes with ``node_id, role, name, cx, cy, w, h``.
    dom_bridge:
        Object with ``is_connected`` and ``get_dom_candidates(gx, gy)``
        returning candidates with ``dom_id, tag, role, primary_label,
        visible_text, aria_label, screen_x, screen_y``.
    ocr_fn / visual_fn:
        Callables ``(image_bgr) -> iterable`` of source items.  OCR items
        become ``source="ocr"`` (default confidence 0.8); visual items become
        ``source="visual"`` (default confidence 0.5).
    """

    def __init__(
        self,
        *,
        ax_bridge: Any | None = None,
        dom_bridge: Any | None = None,
        ocr_fn: Callable[[Any], Iterable[SourceItem]] | None = None,
        visual_fn: Callable[[Any], Iterable[SourceItem]] | None = None,
        dom_bbox_size: tuple[int, int] = DEFAULT_DOM_BBOX_SIZE,
        iou_threshold: float = 0.5,
        cache_size: int = 32,
    ) -> None:
        self._ax = ax_bridge
        self._dom = dom_bridge
        self._ocr_fn = ocr_fn
        self._visual_fn = visual_fn
        self._dom_bbox_size = (int(dom_bbox_size[0]), int(dom_bbox_size[1]))
        self._iou_threshold = float(iou_threshold)
        self._cache_size = max(1, int(cache_size))
        self._cache: OrderedDict[tuple, tuple[list[Candidate], dict[str, bool]]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # --- source adapters -------------------------------------------------

    def _collect_ax(self, sources: dict[str, bool]) -> list[Candidate]:
        if self._ax is None:
            return []
        try:
            avail = self._ax.available() if callable(self._ax.available) else bool(self._ax.available)
            if not avail:
                return []
            nodes = self._ax.scan_active_window()
        except Exception:
            # UnsupportedAccessibilityBridge raises NotSupportedError; any
            # bridge failure is an unavailable source, never a crash.
            return []
        out: list[Candidate] = []
        for node in nodes:
            try:
                out.append(
                    Candidate(
                        id=str(node.node_id),
                        bbox=(
                            int(node.cx) - int(node.w) // 2,
                            int(node.cy) - int(node.h) // 2,
                            int(node.w),
                            int(node.h),
                        ),
                        role=str(node.role or ""),
                        text=str(node.name or ""),
                        source="ax",
                        confidence=1.0,
                    )
                )
            except (TypeError, ValueError, AttributeError):
                continue
        sources["ax"] = True
        return out

    def _collect_dom(self, gaze: tuple[float, float], sources: dict[str, bool]) -> list[Candidate]:
        if self._dom is None:
            return []
        try:
            if not self._dom.is_connected:
                return []
            dom_candidates = self._dom.get_dom_candidates(float(gaze[0]), float(gaze[1]))
        except Exception:
            return []
        width, height = self._dom_bbox_size
        out: list[Candidate] = []
        for cand in dom_candidates:
            try:
                out.append(
                    Candidate(
                        id=f"dom-{cand.dom_id}",
                        bbox=(
                            int(cand.screen_x) - width // 2,
                            int(cand.screen_y) - height // 2,
                            width,
                            height,
                        ),
                        role=str(cand.role or cand.tag or ""),
                        text=str(cand.primary_label or cand.visible_text or cand.aria_label or ""),
                        source="dom",
                        confidence=0.9,
                    )
                )
            except (TypeError, ValueError, AttributeError):
                continue
        sources["dom"] = True
        return out

    def _collect_pixels(
        self,
        fn: Callable[[Any], Iterable[SourceItem]] | None,
        image: Any,
        source: str,
        default_confidence: float,
        sources: dict[str, bool],
    ) -> list[Candidate]:
        if fn is None or image is None:
            return []
        try:
            items = list(fn(image))
        except Exception:
            return []
        out: list[Candidate] = []
        for item in items:
            candidate = _coerce_item(item, source, default_confidence)
            if candidate is not None:
                out.append(candidate)
        sources[source] = True
        return out

    # --- generation ------------------------------------------------------

    def generate(
        self,
        display_id: Any,
        active_window_id: Any,
        screenshot_hash: str,
        *,
        gaze: tuple[float, float] = (0.0, 0.0),
        image: Any = None,
        include_null: bool = True,
    ) -> GenerationResult:
        """Return the candidate list for one screen state.

        The cache key is exactly ``(display_id, active_window_id,
        screenshot_hash)``; any component changing is a miss.  ``latency_ms``
        measures this call (a hit is near zero).
        """
        started = time.perf_counter()
        # The screen-state identity is (display, window, screenshot hash);
        # image presence is a generation-mode flag: the same screen state
        # generated without pixels cannot reuse a pixel-backed result.
        key = (display_id, active_window_id, screenshot_hash, image is not None)
        cached = key in self._cache
        if cached:
            self._hits += 1
            self._cache.move_to_end(key)
            candidates, sources = self._cache[key]
        else:
            self._misses += 1
            sources = {"ax": False, "dom": False, "ocr": False, "visual": False}
            collected: list[Candidate] = []
            collected.extend(self._collect_ax(sources))
            collected.extend(self._collect_dom(gaze, sources))
            collected.extend(self._collect_pixels(self._ocr_fn, image, "ocr", 0.8, sources))
            collected.extend(self._collect_pixels(self._visual_fn, image, "visual", 0.5, sources))
            candidates = deduplicate(collected, self._iou_threshold)
            self._cache[key] = (candidates, dict(sources))
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
                self._evictions += 1
        latency_ms = (time.perf_counter() - started) * 1000.0
        result = with_null(candidates) if include_null else list(candidates)
        return GenerationResult(
            candidates=result,
            sources=dict(sources),
            latency_ms=latency_ms,
            cache_hit=cached,
        )

    def stats(self) -> dict[str, int]:
        """Cache counters for diagnostics and tests."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "size": len(self._cache),
        }


__all__ = [
    "DEFAULT_DOM_BBOX_SIZE",
    "CandidateGenerator",
    "GenerationResult",
]
