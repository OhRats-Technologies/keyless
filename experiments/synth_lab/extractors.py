"""Toy text extractor for the synthetic lab's probe smoke test.

Turns a candidate's text into a deterministic pseudo-embedding (hash-seeded
random vector).  It carries no real semantics, so probe ``top1`` is expected
to tie or lose to ``baseline_top1``; the point is to exercise the full
``gaze probe`` CLI — extractor spec, session split, fit/predict, verdict —
on real schema-v0 sessions without loading a 740M-parameter backbone.

Swap in an EmbeddingGemma 2 wrapper (same ``(event, candidate) -> vector``
signature) when the real experiment runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import hashlib

import numpy as np

_DIM = 32


class HashTextExtractor:
    """Deterministic hash-seeded vector per candidate text."""

    def __init__(self, dim: int = _DIM) -> None:
        self._dim = dim

    def __call__(self, event: Mapping[str, Any], candidate: Mapping[str, Any]) -> np.ndarray:
        text = str(candidate.get("text") or "")
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(self._dim)
