"""Unified generator: source union, honest fallbacks, dedup, cache, null."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from keyless.gaze.candidate_generator import CandidateGenerator
from keyless.gaze.candidates import NULL_CANDIDATE_ID
from keyless.platform.base import NotSupportedError


class FakeAX:
    def __init__(self, nodes=None, available=True, raises=False):
        self._nodes = nodes or []
        self._available = available
        self._raises = raises

    def available(self) -> bool:
        return self._available

    def scan_active_window(self, max_nodes: int = 160):
        if self._raises:
            raise NotSupportedError("Accessibility is not implemented for this OS.")
        return self._nodes


class FakeDOM:
    def __init__(self, candidates=None, connected=True):
        self._candidates = candidates or []
        self.is_connected = connected

    def get_dom_candidates(self, gaze_x, gaze_y, **kwargs):
        return self._candidates


def _ax_node(node_id="n1", cx=100, cy=100, w=40, h=20, role="button", name="Send"):
    return SimpleNamespace(node_id=node_id, cx=cx, cy=cy, w=w, h=h, role=role, name=name)


def _dom_candidate(dom_id=7, screen_x=300, screen_y=200, role="link", label="Reply"):
    return SimpleNamespace(
        dom_id=dom_id,
        tag="a",
        role=role,
        primary_label=label,
        visible_text="",
        aria_label="",
        screen_x=screen_x,
        screen_y=screen_y,
    )


def test_ax_nodes_convert_center_size_to_bbox() -> None:
    gen = CandidateGenerator(ax_bridge=FakeAX([_ax_node()]))
    result = gen.generate(1, 10, "hash-a")
    assert result.sources == {"ax": True, "dom": False, "ocr": False, "visual": False}
    real = [c for c in result.candidates if c.id != NULL_CANDIDATE_ID]
    assert len(real) == 1
    assert real[0].bbox == (80, 90, 40, 20)  # cx-w//2, cy-h//2
    assert real[0].source == "ax"
    assert real[0].text == "Send"


def test_unavailable_ax_is_reported_and_others_still_run() -> None:
    gen = CandidateGenerator(
        ax_bridge=FakeAX(available=False),
        ocr_fn=lambda image: [((10, 10, 50, 20), "Inbox", 0.8)],
    )
    result = gen.generate(1, 10, "hash-b", image=object())
    assert result.sources["ax"] is False
    assert result.sources["ocr"] is True
    assert any(c.source == "ocr" and c.text == "Inbox" for c in result.candidates)


def test_raising_ax_bridge_falls_back_cleanly() -> None:
    gen = CandidateGenerator(
        ax_bridge=FakeAX(raises=True),
        dom_bridge=FakeDOM([_dom_candidate()]),
    )
    result = gen.generate(1, 10, "hash-c")
    assert result.sources["ax"] is False
    assert result.sources["dom"] is True
    assert any(c.source == "dom" for c in result.candidates)


def test_dom_point_synthesizes_documented_default_bbox() -> None:
    gen = CandidateGenerator(dom_bridge=FakeDOM([_dom_candidate(screen_x=300, screen_y=200)]))
    result = gen.generate(1, 10, "hash-d")
    dom = next(c for c in result.candidates if c.source == "dom")
    assert dom.bbox == (300 - 60, 200 - 16, 120, 32)
    assert dom.id == "dom-7"
    assert dom.text == "Reply"


def test_dom_bbox_size_is_configurable() -> None:
    gen = CandidateGenerator(dom_bridge=FakeDOM([_dom_candidate()]), dom_bbox_size=(80, 24))
    dom = next(c for c in gen.generate(1, 10, "h").candidates if c.source == "dom")
    assert dom.bbox == (300 - 40, 200 - 12, 80, 24)


def test_disconnected_dom_is_unavailable() -> None:
    gen = CandidateGenerator(dom_bridge=FakeDOM(connected=False))
    result = gen.generate(1, 10, "hash-e")
    assert result.sources["dom"] is False


def test_ocr_and_visual_need_pixels() -> None:
    gen = CandidateGenerator(
        ocr_fn=lambda image: [((0, 0, 10, 10), "word")],
        visual_fn=lambda image: [((100, 100, 30, 30), "icon", 0.5)],
    )
    without = gen.generate(1, 10, "hash-f")
    assert without.sources["ocr"] is False and without.sources["visual"] is False
    with_image = gen.generate(1, 10, "hash-f", image=object())
    assert with_image.sources["ocr"] is True and with_image.sources["visual"] is True


def test_union_dedups_overlapping_sources_by_priority() -> None:
    # AX box (80,90,40,20); OCR reports the same region -> AX wins.
    gen = CandidateGenerator(
        ax_bridge=FakeAX([_ax_node()]),
        ocr_fn=lambda image: [((80, 90, 40, 20), "Send", 0.8)],
    )
    result = gen.generate(1, 10, "hash-g", image=object())
    real = [c for c in result.candidates if c.id != NULL_CANDIDATE_ID]
    assert len(real) == 1
    assert real[0].source == "ax"


def test_uiboundingbox_like_items_are_accepted() -> None:
    box = SimpleNamespace(mark_id=3, global_x1=5, global_y1=6, global_x2=25, global_y2=16, label="OK")
    gen = CandidateGenerator(ocr_fn=lambda image: [box])
    result = gen.generate(1, 10, "hash-h", image=object())
    ocr = next(c for c in result.candidates if c.source == "ocr")
    assert ocr.bbox == (5, 6, 20, 10)
    assert ocr.text == "OK"


def test_null_option_present_exactly_once() -> None:
    gen = CandidateGenerator(ax_bridge=FakeAX([_ax_node()]))
    result = gen.generate(1, 10, "hash-i")
    nulls = [c for c in result.candidates if c.id == NULL_CANDIDATE_ID]
    assert len(nulls) == 1
    without = gen.generate(1, 10, "hash-i", include_null=False)
    assert all(c.id != NULL_CANDIDATE_ID for c in without.candidates)


def test_cache_hit_on_same_key_and_stats() -> None:
    gen = CandidateGenerator(ax_bridge=FakeAX([_ax_node()]))
    first = gen.generate(1, 10, "hash-j")
    second = gen.generate(1, 10, "hash-j")
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.sources == first.sources
    assert [c.id for c in second.candidates] == [c.id for c in first.candidates]
    assert gen.stats() == {"hits": 1, "misses": 1, "evictions": 0, "size": 1}
    assert second.latency_ms >= 0.0


@pytest.mark.parametrize("change", ["display", "window", "screenshot"])
def test_cache_miss_when_any_key_component_changes(change: str) -> None:
    gen = CandidateGenerator(ax_bridge=FakeAX([_ax_node()]))
    gen.generate(1, 10, "hash-k")
    args = {"display": (2, 10, "hash-k"), "window": (1, 11, "hash-k"), "screenshot": (1, 10, "hash-k2")}
    result = gen.generate(*args[change])
    assert result.cache_hit is False
    assert gen.stats()["misses"] == 2


def test_cache_is_bounded_lru() -> None:
    gen = CandidateGenerator(cache_size=2)
    gen.generate(1, 10, "a")
    gen.generate(1, 10, "b")
    gen.generate(1, 10, "a")  # hit refreshes "a"; "b" becomes oldest
    gen.generate(1, 10, "c")  # evicts "b"
    stats = gen.stats()
    assert stats["size"] == 2
    assert stats["evictions"] == 1
    assert gen.generate(1, 10, "b").cache_hit is False  # evicted; re-insert evicts "a"
    assert gen.generate(1, 10, "c").cache_hit is True


def test_no_sources_yields_only_null() -> None:
    gen = CandidateGenerator()
    result = gen.generate(1, 10, "hash-empty")
    assert [c.id for c in result.candidates] == [NULL_CANDIDATE_ID]
    assert result.sources == {"ax": False, "dom": False, "ocr": False, "visual": False}
