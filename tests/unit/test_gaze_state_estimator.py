"""Unit tests for 3D angular Kalman state estimator: blink rollback, REM gating, and saccades."""

from __future__ import annotations

import math

import pytest

from keyless.gaze.state_estimator import GazeKinematics, GazeStateEstimator


def test_blink_to_bottom_corner_is_completely_suppressed_with_rollback() -> None:
    est = GazeStateEstimator()
    t = 0.0
    dt = 1.0 / 30.0

    # 10 frames of calm fixation at (0.05, -0.02)
    for _ in range(10):
        st = est.update(0.05, -0.02, t, blink_level=0.04)
        t += dt
    assert st.kinematics is GazeKinematics.FIXATION
    assert abs(st.az - 0.05) < 0.01
    assert abs(st.el - (-0.02)) < 0.01

    # Simulate realistic blink with pre-blink eyelid occlusion dragging gaze to bottom-left corner
    blink_seq = [
        (0.28, -0.45, 0.35),   # pre-blink eyelid touches iris -> huge downward/left pull
        (0.65, -0.75, 0.55),   # eye closed -> corner artifact
        (0.92, -0.80, 0.60),   # peak closure
        (0.55, -0.60, 0.45),   # re-opening
        (0.18, -0.35, 0.25),   # post-blink recovery frame 1 (Bell's phenomenon)
        (0.08, -0.20, 0.15),   # post-blink recovery frame 2
        (0.05, -0.10, 0.08),   # post-blink recovery frame 3
    ]
    for blink, raw_az, raw_el in blink_seq:
        st = est.update(raw_az, raw_el, t, blink_level=blink)
        t += dt
        assert st.kinematics in (GazeKinematics.BLINK_HOLD, GazeKinematics.RECOVERY)
        assert st.suppressed is True
        # Gaze MUST stay anchored near pre-blink fixation, never dragged toward corner
        assert abs(st.az - 0.05) < 0.02
        assert abs(st.el - (-0.02)) < 0.02

    assert est.blinks_suppressed == 1

    # After recovery window (>115 ms), normal fixation resumes cleanly
    t += 0.15
    st = est.update(0.05, -0.02, t, blink_level=0.04)
    assert st.kinematics is GazeKinematics.FIXATION
    assert st.suppressed is False


def test_single_frame_involuntary_rem_spike_is_rejected() -> None:
    est = GazeStateEstimator()
    t = 0.0
    dt = 1.0 / 30.0
    for _ in range(8):
        est.update(0.0, 0.0, t, blink_level=0.02)
        t += dt

    # 1-frame involuntary REM / landmark glitch jump
    st_spike = est.update(0.45, -0.30, t, blink_level=0.02)
    t += dt
    assert st_spike.suppressed is True
    assert abs(st_spike.az) < 0.02
    assert abs(st_spike.el) < 0.02
    assert est.outliers_suppressed >= 1

    # Next frame back at fixation -> never jumped
    st_back = est.update(0.002, -0.001, t, blink_level=0.02)
    assert st_back.kinematics is GazeKinematics.FIXATION
    assert abs(st_back.az) < 0.02
    assert abs(st_back.el) < 0.02


def test_confirmed_intentional_saccade_snaps_rapidly() -> None:
    est = GazeStateEstimator()
    t = 0.0
    dt = 1.0 / 30.0
    for _ in range(8):
        est.update(0.0, 0.0, t, blink_level=0.02)
        t += dt

    # Intentional look to (+0.35, +0.10) sustained across frames
    st1 = est.update(0.35, 0.10, t, blink_level=0.02)
    t += dt
    assert st1.suppressed is True  # frame 1 held pending confirmation

    st2 = est.update(0.35, 0.10, t, blink_level=0.02)
    t += dt
    assert st2.kinematics is GazeKinematics.SACCADE
    assert st2.suppressed is False

    for _ in range(4):
        st = est.update(0.35, 0.10, t, blink_level=0.02)
        t += dt
    assert math.hypot(st.az - 0.35, st.el - 0.10) < 0.03
