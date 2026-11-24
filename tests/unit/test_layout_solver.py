"""Layout solver: feasibility (Lean-proved), optimality (checked here), order choice."""

from __future__ import annotations

import itertools
import math
from types import SimpleNamespace as NS

import numpy as np
import pytest

from keyless.gaze.displays import DisplayRig, place_displays
from keyless.gaze.layout_solver import os_rows, pava, solve_chain, solve_row


def _kkt_optimal(x, t, w, g, tol=1e-7) -> bool:
    """Projected-gradient check: no feasible direction lowers the cost."""
    # Brute force over active sets is exact for small n: compare with the best
    # feasible solution found by solving every active-set equality system.
    n = len(t)
    best = math.inf
    offs = np.concatenate([[0.0], np.cumsum(g)])
    for mask in itertools.product([0, 1], repeat=n - 1):
        # Tied blocks: consecutive indices joined where mask=1.
        blocks, cur = [], [0]
        for i, m in enumerate(mask):
            if m:
                cur.append(i + 1)
            else:
                blocks.append(cur)
                cur = [i + 1]
        blocks.append(cur)
        y = np.empty(n)
        for b in blocks:
            y[b] = sum(w[i] * (t[i] - offs[i]) for i in b) / sum(w[i] for i in b)
        cand = y + offs
        if np.all(np.diff(cand) >= g - tol):
            best = min(best, float(np.sum(w * (cand - t) ** 2)))
    return float(np.sum(w * (np.asarray(x) - t) ** 2)) <= best + tol


@pytest.mark.parametrize("seed", range(40))
def test_chain_is_feasible_and_optimal(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(2, 6))
    t = rng.normal(scale=0.4, size=n)
    w = rng.uniform(0.1, 5.0, size=n)
    g = rng.uniform(0.0, 0.6, size=n - 1)
    x = solve_chain(t, w, g)
    assert np.all(np.diff(x) >= g - 1e-12)
    assert _kkt_optimal(np.array(x), t, w, g)


def test_valid_layout_is_unchanged() -> None:
    t = [-1.0, 0.0, 1.5]
    assert solve_chain(t, [1, 1, 1], [0.5, 0.5]) == pytest.approx(t)


def test_pava_monotone() -> None:
    out = pava([3.0, 1.0, 2.0, 0.0, 5.0], [1, 1, 1, 1, 1])
    assert all(a <= b + 1e-12 for a, b in zip(out, out[1:]))


def test_overlapping_estimates_get_split() -> None:
    """The reported bug: two displays estimated at the same angle must not overlap."""
    sol = solve_row([0.1, 0.1], [10.0, 10.0], [0.4, 0.4], gap_min=0.01)
    a, b = sol.centers
    assert b - a >= 0.81 - 1e-9
    assert not sol.reordered


def test_swap_requires_strong_evidence() -> None:
    weak = solve_row([0.3, -0.3], [1.0, 1.0], [0.3, 0.3], swap_penalty=12.0)
    strong = solve_row([0.4, -0.4], [500.0, 500.0], [0.3, 0.3], swap_penalty=12.0)
    assert not weak.reordered
    assert strong.reordered
    assert strong.centers[0] > strong.centers[1]


def test_os_rows_groups_side_by_side_and_stacked() -> None:
    rects = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (0, 1080, 1920, 1080)]
    assert os_rows(rects) == [[0, 1], [2]]


def _layout():
    mons = [
        NS(connector="A", x=0, y=0, width=2560, height=1440, scale=1, is_primary=True, width_mm=597, height_mm=336),
        NS(connector="B", x=2560, y=0, width=1920, height=1080, scale=1, is_primary=False, width_mm=531, height_mm=299),
        NS(connector="C", x=4480, y=0, width=1920, height=1080, scale=1, is_primary=False, width_mm=531, height_mm=299),
    ]
    return NS(monitors=mons, total_width=6400, total_height=1440)


@pytest.mark.parametrize("seed", range(10))
def test_placed_rig_never_overlaps(seed: int) -> None:
    rng = np.random.default_rng(seed)
    base = DisplayRig.from_layout(_layout())
    est = {c: (float(rng.normal(scale=0.2)), float(rng.normal(scale=0.05)), float(rng.uniform(1, 200))) for c in "ABC"}
    rig, _ = place_displays(base, est, distance_mm=650)
    # Sample each display's horizontal edge rays; intervals must be disjoint.
    spans = []
    for p in rig.planes:
        l, _ = rig.pixel_to_ray_angles(p.x + 1, p.y + p.height / 2)[:2]
        r, _ = rig.pixel_to_ray_angles(p.x + p.width - 2, p.y + p.height / 2)[:2]
        spans.append((l, r))
    spans.sort()
    for (l1, r1), (l2, _r2) in zip(spans, spans[1:]):
        assert r1 <= l2 + 1e-6
    # Every pixel round-trips to its own display.
    for p in rig.planes:
        az, el, *_ = rig.pixel_to_ray_angles(p.x + 0.4 * p.width, p.y + 0.5 * p.height)
        assert rig.ray_angles_to_pixel(az, el).connector == p.connector
