"""Pursuit warm-up on a simulated user with known latency, layout, blinks, glances."""

from __future__ import annotations

import math
from types import SimpleNamespace as NS

import numpy as np
import pytest

from keyless.gaze.displays import DisplayRig, place_displays
from keyless.gaze.learner import GazeLearner, inlier_weight
from keyless.gaze.pursuit import FrameSample, adaptive_blink_thresholds, build_plan, solve_pursuit


def _simulate(*, swap: bool, tau: float = 0.09, seed: int = 0):
    rng = np.random.default_rng(seed)
    mons = [
        NS(connector="DP-1", x=0, y=0, width=2560, height=1440, scale=1, is_primary=True, width_mm=597, height_mm=336),
        NS(connector="HDMI-1", x=2560, y=0, width=1920, height=1080, scale=1, is_primary=False, width_mm=531, height_mm=299),
    ]
    base = DisplayRig.from_layout(NS(monitors=mons, total_width=4480, total_height=1440))
    est = {"DP-1": (0.42 if swap else -0.45, 0.0, 1e6), "HDMI-1": (-0.40 if swap else 0.40, 0.03, 1e6)}
    truth, _ = place_displays(base, est, distance_mm=650)
    plan = build_plan(base, distance_mm=650)
    B = rng.normal(size=(10, 2)) * 0.4
    c = rng.normal(size=10) * 0.1
    frames, t, look = [], 0.0, 0
    while t < plan.duration:
        x, y, _, seg = plan.at(t - tau)
        if seg is None:
            x, y, _, _ = plan.at(max(0.0, t))
        a = np.array(truth.pixel_to_ray_angles(x, y)[:2]) + rng.normal(scale=math.radians(0.8), size=2)
        if look > 0:
            look -= 1
            a = a + np.array([0.5, 0.3])
        elif rng.random() < 0.05 / 30:
            look = 15
        blink = 0.9 if rng.random() < 0.01 else 0.05 + abs(rng.normal(scale=0.02))
        frames.append(FrameSample(t, B @ a + c + rng.normal(scale=0.004, size=10), blink, 0.95, 650.0))
        t += 1 / 30
    return plan, frames, base, truth


@pytest.mark.parametrize("swap", [False, True])
def test_pursuit_recovers_latency_layout_and_accuracy(swap: bool) -> None:
    plan, frames, base, truth = _simulate(swap=swap)
    r = solve_pursuit(plan, frames, base)
    assert r.ok and r.grade == "great"
    assert abs(r.latency_ms - 90.0) <= 20.0
    assert r.accuracy_deg < 1.5
    assert r.swapped is swap
    for got, want in zip(r.rig.planes, truth.planes):
        assert got.connector == want.connector
        assert abs(got.yaw_deg - want.yaw_deg) < 4.0


def test_slow_camera_pipeline_latency_is_not_clipped() -> None:
    # macOS AVFoundation + browser stage measured >= 320 ms on real hardware (2026-11-24);
    # a lag past the search window must be found, not reported as the window edge.
    from keyless.gaze.pursuit import estimate_latency

    plan, frames, base, _ = _simulate(swap=False, tau=0.42)
    tau_ms, conf, _ = estimate_latency(plan, frames, base, distance_mm=650)
    assert abs(tau_ms - 420.0) <= 30.0
    assert conf > 0.0


def test_latency_at_search_edge_has_zero_confidence() -> None:
    from keyless.gaze.pursuit import estimate_latency

    plan, frames, base, _ = _simulate(swap=False, tau=0.42)
    tau_ms, conf, _ = estimate_latency(plan, frames, base, distance_mm=650, max_ms=200.0)
    assert tau_ms == 200.0 and conf == 0.0


def test_not_following_is_reported() -> None:
    plan, frames, base, _ = _simulate(swap=False)
    rng = np.random.default_rng(5)
    noise = [FrameSample(f.t, rng.normal(size=10), f.blink, f.confidence) for f in frames]
    r = solve_pursuit(plan, noise, base)
    assert not r.ok


def test_plan_is_continuous_and_bounded() -> None:
    _, _, base, _ = _simulate(swap=False)
    plan = build_plan(base)
    prev = None
    for t in np.arange(0.0, plan.duration, 1 / 120):
        x, y, size, seg = plan.at(float(t))
        assert seg is not None and 0.0 <= size <= 0.62 + 1e-9
        # Travel crosses displays (never labelled); everything else stays on its display.
        p = base.plane_for_pixel(x, y) if seg.kind == "travel" else base.plane_for_connector(seg.connector)
        assert p.x <= x <= p.x + p.width and p.y <= y <= p.y + p.height
        if prev is not None and seg.kind != "travel":
            assert math.hypot(x - prev[0], y - prev[1]) < 60.0  # no teleports at 120 Hz
        prev = (x, y)


def test_blink_thresholds_follow_baseline() -> None:
    rng = np.random.default_rng(0)
    droopy = list(0.22 + np.abs(rng.normal(scale=0.01, size=600)))
    enter, exit_ = adaptive_blink_thresholds(droopy)
    assert enter > 0.33 and 0.22 < exit_ < enter


def test_gate_weight_is_probability_and_monotone() -> None:
    ws = [inlier_weight(np.array([r, 0.0]), 1e-3, 0.4) for r in (0.0, 0.02, 0.05, 0.1, 0.3)]
    assert all(0.0 <= w <= 1.0 for w in ws)
    assert ws == sorted(ws, reverse=True)


def test_learner_undo_restores_exact_state() -> None:
    L = GazeLearner(M0=np.zeros((3, 2)), P0=np.eye(3))
    L.observe(np.array([0.1, 0.2]), (0.05, 0.01), area_sr=0.4, t=0.0)
    M1 = L.M.copy()
    L.observe(np.array([-0.1, 0.0]), (-0.04, 0.0), area_sr=0.4, t=1.0)
    L.undo_last()
    np.testing.assert_allclose(L.M, M1)
