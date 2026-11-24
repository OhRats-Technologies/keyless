"""Unit tests for monocular gaze cold-start prior, passive ridge adaptation, and SHM seam."""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import pytest

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.candidate_generator import CandidateGenerator
from keyless.gaze.collect import SessionCollector
from keyless.gaze.monocular import (
    FEATURE_DIM,
    MonocularGazeEstimator,
    observation_to_frame_section,
)
from keyless.gaze.recorder import ScreenSnapshot, SessionRecorder
from keyless.gaze.replay import load_session
from keyless.gaze.schema import validate_event
from keyless.gaze.shared_memory import SharedMemoryGazeSource
from keyless.ipc.state import FLAG_FACE_DETECTED, SharedGazeWriter
from keyless.vision.landmarks import CameraObservation


def _dummy_observation(yaw: float = 0.1, pitch: float = -0.05) -> CameraObservation:
    return CameraObservation(
        iris_a_dx=0.05,
        iris_a_dy=-0.02,
        iris_b_dx=0.04,
        iris_b_dy=-0.01,
        yaw=yaw,
        pitch=pitch,
        roll=0.01,
        tx=0.0,
        ty=0.0,
        tz=0.6,
        nose_x=0.5,
        nose_y=0.5,
        blink_left=0.05,
        blink_right=0.06,
        jaw_open=0.0,
        confidence_a=0.95,
        confidence_b=0.95,
        iris_a_px=(300, 200),
        iris_b_px=(360, 200),
        nose_px=(330, 240),
        eye_corners_px=((280, 200), (320, 200), (340, 200), (380, 200)),
    )


def test_cold_start_prior_stays_within_display_bounds_and_moves_monotonically() -> None:
    est = MonocularGazeEstimator(1920, 1080, smooth=False)
    center = np.zeros(FEATURE_DIM, dtype=np.float64)
    cx, cy, _, _, using_ridge = est.predict(center)
    assert using_ridge is False
    assert cx == pytest.approx(960.0)
    assert cy == pytest.approx(540.0)

    look_right = np.array([0.3, 0.0, 0.3, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0, 0.6])
    rx, ry, _, _, _ = est.predict(look_right)
    assert rx > cx
    assert 0.0 <= rx < 1920.0 and 0.0 <= ry < 1080.0

    extreme = np.full(FEATURE_DIM, 50.0)
    ex, ey, _, _, _ = est.predict(extreme)
    assert 0.0 <= ex <= 1919.0
    assert 0.0 <= ey <= 1079.0


def test_passive_click_ridge_adaptation_converges_and_is_reversible() -> None:
    rng = np.random.default_rng(42)
    est = MonocularGazeEstimator(
        1920,
        1080,
        min_clicks=5,
        blend_clicks=5,
        ridge_alpha=1e-4,
        smooth=False,
    )
    true_w = np.zeros((FEATURE_DIM, 2), dtype=np.float64)
    true_w[0, 0] = 1400.0
    true_w[2, 0] = 1400.0
    true_w[4, 0] = 700.0
    true_w[1, 1] = 900.0
    true_w[3, 1] = 900.0
    true_w[5, 1] = 500.0
    bias = np.array([960.0, 540.0], dtype=np.float64)

    for _ in range(20):
        f = rng.uniform(-0.2, 0.2, size=FEATURE_DIM)
        xy = f @ true_w + bias
        est.add_click(f, float(xy[0]), float(xy[1]))

    assert est.is_adapted is True
    assert est.sample_count == 20

    test_f = np.array([0.1, -0.05, 0.1, -0.05, 0.05, -0.04, 0.0, 0.0, 0.0, 0.5])
    expected = test_f @ true_w + bias
    px, py, _, _, using_ridge = est.predict(test_f)
    assert using_ridge is True
    assert abs(px - expected[0]) < 15.0
    assert abs(py - expected[1]) < 15.0

    # Reversibility: undo_last and reset cleanly return to cold-start prior
    assert est.undo_last() == 19
    est.reset()
    assert est.sample_count == 0
    assert est.is_adapted is False
    _, _, _, _, using_ridge_after = est.predict(test_f)
    assert using_ridge_after is False


def test_observation_to_frame_section_validates_in_session_recorder(tmp_path: Path) -> None:
    paths: AppPaths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    obs = _dummy_observation()
    frame_dict = observation_to_frame_section(obs, cam_id="uvc-0")

    collector = SessionCollector(paths, session_id="mono-rec-1")
    recorder = SessionRecorder(
        collector,
        CandidateGenerator(),
        screen_provider=lambda: ScreenSnapshot(display_id=0, app="Firefox", window_id=1),
        frame_provider=lambda: frame_dict,
    )
    assert recorder.record_click(400.0, 300.0, gaze_pixel=(410.0, 295.0))
    recorder.close()

    session = load_session(collector.session_dir)
    assert len(session.events) == 1
    ev = session.events[0]
    assert validate_event(ev) == []
    assert ev["frame"]["cams"] == ["uvc-0"]
    assert len(ev["frame"]["features"]) == FEATURE_DIM
    assert ev["frame"]["head_pose"]["yaw"] == pytest.approx(0.1)
    assert ev["prediction"]["pixel"] == [410.0, 295.0]


def test_shared_memory_writer_and_source_roundtrip() -> None:
    shm_name = f"keyless_mono_test_{uuid.uuid4().hex[:8]}"
    writer = SharedGazeWriter(shm_name=shm_name)
    try:
        from keyless.ipc.state import SharedGazeReader

        reader = SharedGazeReader(shm_name=shm_name)
        try:
            writer.write(1234.5, 678.25, FLAG_FACE_DETECTED, timestamp_ns=123_000_000)
            state = reader.read()
            assert state is not None
            assert state.x == pytest.approx(1234.5)
            assert state.y == pytest.approx(678.25)
            assert state.flags == FLAG_FACE_DETECTED
        finally:
            reader.close()
    finally:
        writer.close()


def test_binocular_20d_cold_start_and_ridge_adaptation() -> None:
    from keyless.gaze.monocular import BINOCULAR_FEATURE_DIM

    rng = np.random.default_rng(7)
    est = MonocularGazeEstimator(
        3840,
        1080,
        feature_dim=BINOCULAR_FEATURE_DIM,
        min_clicks=5,
        blend_clicks=5,
        ridge_alpha=1e-4,
        smooth=False,
    )
    cx, cy, _, _, using_ridge = est.predict(np.zeros(BINOCULAR_FEATURE_DIM))
    assert using_ridge is False
    assert cx == pytest.approx(1920.0)
    assert cy == pytest.approx(540.0)

    true_w = np.zeros((BINOCULAR_FEATURE_DIM, 2), dtype=np.float64)
    true_w[0, 0] = 1600.0
    true_w[10, 0] = 1600.0
    true_w[1, 1] = 800.0
    true_w[11, 1] = 800.0
    bias = np.array([1920.0, 540.0], dtype=np.float64)

    for _ in range(30):
        f = rng.uniform(-0.2, 0.2, size=BINOCULAR_FEATURE_DIM)
        xy = f @ true_w + bias
        est.add_click(f, float(xy[0]), float(xy[1]))

    assert est.is_adapted is True
    assert len(est.sample_points()) == 30
    test_f = np.zeros(BINOCULAR_FEATURE_DIM)
    test_f[0] = 0.1
    test_f[10] = 0.1
    test_f[1] = -0.05
    test_f[11] = -0.05
    expected = test_f @ true_w + bias
    px, py, _, _, using_ridge = est.predict(test_f)
    assert using_ridge is True
    assert abs(px - expected[0]) < 20.0
    assert abs(py - expected[1]) < 20.0

    # Verify binocular observation_to_frame_section produces 20D features + 2 cams
    obs1 = _dummy_observation(yaw=0.1, pitch=-0.05)
    obs2 = _dummy_observation(yaw=0.2, pitch=0.05)
    frame = observation_to_frame_section(obs1, "cam1", obs2, "cam2")
    assert frame["cams"] == ["cam1", "cam2"]
    assert len(frame["features"]) == BINOCULAR_FEATURE_DIM
    assert frame["head_pose"]["yaw"] == pytest.approx(0.15)
