"""Non-overlapping multi-display layout solver.

Problem. Gaze evidence (pursuit calibration or natural clicks) yields a noisy
angular centre ``c_i`` for each display, with weight ``w_i`` (sample count or
inverse variance). Estimating each display independently lets two physically
adjacent displays collapse onto the same angular region; that produced the
"overlapping monitors that had to be split" failure.

Model. Displays that share a row in the OS layout are rigid intervals on the
azimuth axis with known angular half-widths ``h_i`` (from EDID size and viewing
distance). For a chosen left-to-right order, a valid layout satisfies

    x_{i+1} - x_i >= g_i,   g_i = h_i + h_{i+1} + gap_min.

We solve ``min sum_i w_i (x_i - c_i)^2`` subject to those constraints.
Substituting ``y_i = x_i - G_i`` with ``G_i = sum_{j<i} g_j`` turns the
constraints into ``y_0 <= y_1 <= ...``: a weighted isotonic regression of
``c_i - G_i``, solved exactly by the pool-adjacent-violators algorithm (PAVA).

Guarantees (Lean: ``proofs/lean/KeylessProofs/Layout.lean``):
- the change of variables maps monotone ``y`` exactly to spaced ``x``;
- PAVA's block stack keeps strictly increasing block means, so its output is
  monotone, hence the returned layout never overlaps;
- a layout that is already valid is returned unchanged.

Order selection. The OS order is the prior. Another permutation wins only if
it lowers the constrained cost by more than ``swap_penalty``. The caller scales
that penalty to the strength of its evidence: low for dense pursuit data, high
for a few clicks.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass


def pava(values: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Weighted isotonic (non-decreasing) least-squares fit via PAVA. O(n)."""
    if len(values) != len(weights):
        raise ValueError("values and weights must have equal length")
    # Each block: [weighted_sum, weight, count]
    blocks: list[list[float]] = []
    for v, w in zip(values, weights):
        w = max(float(w), 1e-12)
        blocks.append([float(v) * w, w, 1.0])
        while len(blocks) >= 2 and (
            blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
        ):
            s, ww, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += ww
            blocks[-1][2] += c
    out: list[float] = []
    for s, ww, c in blocks:
        out.extend([s / ww] * int(c))
    return out


def solve_chain(
    targets: Sequence[float],
    weights: Sequence[float],
    gaps: Sequence[float],
) -> list[float]:
    """Closest spaced positions: ``x[i+1] - x[i] >= gaps[i]`` (weighted L2)."""
    n = len(targets)
    if n == 0:
        return []
    if len(gaps) != n - 1:
        raise ValueError("gaps must have length len(targets) - 1")
    offsets = [0.0]
    for g in gaps:
        offsets.append(offsets[-1] + max(0.0, float(g)))
    y = pava([float(t) - o for t, o in zip(targets, offsets)], weights)
    x = [yi + oi for yi, oi in zip(y, offsets)]
    # Floating-point guard: PAVA is exact in real arithmetic, but block means
    # can differ in the last ulp. A forward sweep restores the invariant and
    # is the identity on any already-valid layout (proved in Lean).
    for i in range(1, n):
        x[i] = max(x[i], x[i - 1] + max(0.0, float(gaps[i - 1])))
    return x


def chain_cost(xs: Sequence[float], targets: Sequence[float], weights: Sequence[float]) -> float:
    return float(sum(w * (x - t) ** 2 for x, t, w in zip(xs, targets, weights)))


@dataclass(frozen=True)
class RowSolution:
    order: tuple[int, ...]       # indices into the row, left to right
    centers: tuple[float, ...]   # solved centre for each original index
    cost: float
    reordered: bool


def solve_row(
    targets: Sequence[float],
    weights: Sequence[float],
    half_widths: Sequence[float],
    *,
    os_order: Sequence[int] | None = None,
    gap_min: float = 0.0,
    swap_penalty: float = 0.0,
    max_exhaustive: int = 6,
) -> RowSolution:
    """Solve one row of displays, choosing the physical order.

    ``targets``/``weights``/``half_widths`` are indexed by display. Displays with
    zero weight carry no evidence; they keep their OS-relative slot because the
    order search only permutes observed displays.
    """
    n = len(targets)
    if n == 0:
        return RowSolution((), (), 0.0, False)
    base = tuple(os_order) if os_order is not None else tuple(range(n))

    def evaluate(order: Sequence[int]) -> tuple[float, list[float]]:
        t = [targets[i] for i in order]
        w = [weights[i] for i in order]
        g = [
            half_widths[order[k]] + half_widths[order[k + 1]] + gap_min
            for k in range(n - 1)
        ]
        xs = solve_chain(t, w, g)
        return chain_cost(xs, t, w), xs

    observed = [i for i in base if weights[i] > 0.0]
    candidates: list[tuple[int, ...]] = [base]
    if 2 <= len(observed) <= max_exhaustive:
        slots = [k for k, i in enumerate(base) if weights[i] > 0.0]
        for perm in itertools.permutations(observed):
            order = list(base)
            for slot, idx in zip(slots, perm):
                order[slot] = idx
            if tuple(order) != base:
                candidates.append(tuple(order))
    elif len(observed) > max_exhaustive:
        # Too many for exhaustive search: try the evidence-sorted order only.
        slots = [k for k, i in enumerate(base) if weights[i] > 0.0]
        by_evidence = sorted(observed, key=lambda i: targets[i])
        order = list(base)
        for slot, idx in zip(slots, by_evidence):
            order[slot] = idx
        candidates.append(tuple(order))

    best: tuple[float, tuple[int, ...], list[float]] | None = None
    for order in candidates:
        cost, xs = evaluate(order)
        penalised = cost + (swap_penalty if order != base else 0.0)
        if best is None or penalised < best[0] - 1e-12:
            best = (penalised, order, xs)
    assert best is not None
    _, order, xs = best
    centers = [0.0] * n
    for slot, idx in enumerate(order):
        centers[idx] = xs[slot]
    return RowSolution(
        order=tuple(order),
        centers=tuple(centers),
        cost=chain_cost([centers[i] for i in order], [targets[i] for i in order], [weights[i] for i in order]),
        reordered=tuple(order) != base,
    )


def os_rows(rects: Sequence[tuple[int, int, int, int]]) -> list[list[int]]:
    """Group display indices into rows by vertical overlap in the OS layout.

    Each row is sorted left to right by OS x. ``rects`` are ``(x, y, w, h)``.
    """
    order = sorted(range(len(rects)), key=lambda i: (rects[i][1], rects[i][0]))
    rows: list[list[int]] = []
    spans: list[tuple[int, int]] = []
    for i in order:
        _, y, _, h = rects[i]
        placed = False
        for r, (y0, y1) in enumerate(spans):
            overlap = min(y1, y + h) - max(y0, y)
            if overlap > 0.5 * min(h, y1 - y0):
                rows[r].append(i)
                spans[r] = (min(y0, y), max(y1, y + h))
                placed = True
                break
        if not placed:
            rows.append([i])
            spans.append((y, y + h))
    for row in rows:
        row.sort(key=lambda i: rects[i][0])
    return rows


def angular_half_width(size_mm: float, distance_mm: float) -> float:
    return math.atan2(0.5 * float(size_mm), max(1.0, float(distance_mm)))


__all__ = [
    "RowSolution",
    "angular_half_width",
    "chain_cost",
    "os_rows",
    "pava",
    "solve_chain",
    "solve_row",
]
