"""Unit tests for Phase 1 components (no live cameras or /dev/uinput required)."""

from __future__ import annotations

import numpy as np

from keyless.config import (
    DisplayLayout,
    MonitorInfo,
    parse_kscreen_layout,
    parse_mutter_display_state,
)
from keyless.input.pointer import DryRunPointerBackend
from keyless.ipc.state import (
    FLAG_BLINKING,
    FLAG_FACE_DETECTED,
    FLAG_SACCADE_JUMP,
    SharedGazeReader,
    SharedGazeWriter,
)
from keyless.vision.calibrator import (
    EXPANDED_FEATURE_DIM,
    StereoGazeModel,
    _align_and_filter_pursuit_stroke,
    _iris_step_distance,
    _load_replay_dataset,
    _save_replay_dataset,
    _trim_dot_outliers,
)
from keyless.vision.cursor_gimbal import GazeGimbalController
from keyless.vision.landmarks import STEREO_FEATURE_DIM
from keyless.vision.one_euro import OneEuroFilter2D


def test_parse_mutter_display_state_dual_monitor() -> None:
    sample = """(uint32 1, [
        (('HDMI-1', 'SAM', 'SAMSUNG', '0x0'), [('1920x1080@60', 1920, 1080, 60.0, 1.0, [1.0], {'is-current': <true>})], {}),
        (('DP-1', 'DEL', 'DELL S3422DW', '9V0'), [('3440x1440@60', 3440, 1440, 60.0, 1.0, [1.0, 1.25], {'is-current': <true>})], {})
    ], [
        (0, 210, 1.0, uint32 0, false, [('HDMI-1', 'SAM', 'SAMSUNG', '0x0')], {}),
        (1920, 0, 1.0, 0, true, [('DP-1', 'DEL', 'DELL S3422DW', '9V0')], {})
    ], {'layout-mode': <uint32 1>})"""
    layout = parse_mutter_display_state(sample)
    assert layout.total_width == 5360
    assert layout.total_height == 1440
    assert layout.primary.connector == "DP-1"
    assert layout.primary.x == 1920
    assert layout.primary.width == 3440


def test_parse_mutter_fractional_scale() -> None:
    sample = """(uint32 1, [
        (('DP-1', 'DEL', 'DELL', '1'), [('2560x1440@60', 2560, 1440, 60.0, 1.25, [1.0, 1.25], {'is-current': <true>})], {})
    ], [
        (0, 0, 1.25, uint32 0, true, [('DP-1', 'DEL', 'DELL', '1')], {})
    ], {'layout-mode': <uint32 1>})"""
    layout = parse_mutter_display_state(sample)
    assert layout.total_width == 2048
    assert layout.total_height == 1152


def test_parse_kscreen_layout_dual_monitor() -> None:
    sample = """{
        "outputs": [
            {"name": "DP-2", "enabled": true, "priority": 1, "scale": 1,
             "pos": {"x": 0, "y": 0}, "size": {"width": 1920, "height": 1080}},
            {"name": "HDMI-A-1", "enabled": true, "priority": 2, "scale": 1,
             "pos": {"x": 1920, "y": 0}, "size": {"width": 1920, "height": 1080}},
            {"name": "DP-3", "enabled": false, "priority": 3, "scale": 1,
             "pos": {"x": 0, "y": 0}, "size": {"width": 2560, "height": 1440}}
        ],
        "screen": {"currentSize": {"width": 3840, "height": 1080}}
    }"""
    layout = parse_kscreen_layout(sample)
    assert layout.total_width == 3840
    assert layout.total_height == 1080
    assert layout.primary.connector == "DP-2"
    assert [mon.connector for mon in layout.monitors] == ["DP-2", "HDMI-A-1"]


def test_parse_kscreen_layout_scaled_output() -> None:
    sample = """{
        "outputs": [
            {"name": "DP-1", "enabled": true, "priority": 1, "scale": 2,
             "pos": {"x": 0, "y": 0}, "size": {"width": 3840, "height": 2160}}
        ]
    }"""
    layout = parse_kscreen_layout(sample)
    assert layout.primary.width == 1920
    assert layout.primary.height == 1080
    assert layout.primary.physical_width == 3840


def test_shared_gaze_seqlock_ipc(tmp_path) -> None:
    shm_name = "kk_test_gaze_ipc"
    json_path = tmp_path / "gaze.json"
    writer = SharedGazeWriter(shm_name=shm_name, json_snapshot_path=json_path)
    try:
        reader = SharedGazeReader(shm_name=shm_name)
        try:
            writer.write(1234.5, 678.25, FLAG_FACE_DETECTED, timestamp_ns=999_000_000)
            snap = reader.read()
            assert snap is not None
            assert snap.x == 1234.5
            assert snap.y == 678.25
            assert snap.flags == FLAG_FACE_DETECTED
            assert snap.timestamp_ns == 999_000_000
            assert snap.seq == 2
            assert json_path.exists()
        finally:
            reader.close()
    finally:
        writer.close(unlink=True)


def test_stereo_degree2_model_fits_interactions_and_roundtrips(tmp_path) -> None:
    rng = np.random.default_rng(42)
    # 17 calibration groups x 24 samples, with near-constant translation cols (7,8,9,17,18,19)
    groups = np.repeat(np.arange(17), 24)
    X = rng.normal(0.0, 0.15, size=(len(groups), STEREO_FEATURE_DIM))
    X[:, [7, 8, 9, 17, 18, 19]] = rng.normal(0.0, 0.0005, size=(len(groups), 6))
    c1_dx = 0.5 * (X[:, 0] + X[:, 2])
    c1_dy = 0.5 * (X[:, 1] + X[:, 3])
    # Nonlinear interaction: yaw (col 4) scales c1_dx and pitch (col 5) scales c1_dy
    y_x = 2500.0 + 900.0 * c1_dx + 450.0 * c1_dx * X[:, 4] - 300.0 * X[:, 10]
    y_y = 700.0 + 450.0 * c1_dy + 350.0 * c1_dy * X[:, 5] + 200.0 * X[:, 11]
    targets = np.column_stack([y_x, y_y])

    mon = MonitorInfo("DP-1", 1920, 0, 3440, 1440, 3440, 1440, 1.0, True)
    layout = DisplayLayout(monitors=(mon,), total_width=5360, total_height=1440)

    model = StereoGazeModel.fit(
        X=X,
        targets_xy=targets,
        cam1_by_id="/dev/v4l/by-id/cam1",
        cam2_by_id="/dev/v4l/by-id/cam2",
        monitor=mon,
        layout=layout,
        groups=groups,
        camera_mode="mono",
    )
    assert model.rmse_x < 15.0
    assert model.rmse_y < 15.0
    assert model.cv_rmse_x < 25.0
    assert model.cv_rmse_y < 25.0
    assert len(model.coef_x) == EXPANDED_FEATURE_DIM == 34

    # Simulate weighted cursor-pursuit refinement refit
    weights = np.ones(len(X), dtype=np.float64)
    weights[-24:] = 2.5
    refined = StereoGazeModel.fit(
        X=X,
        targets_xy=targets,
        cam1_by_id="/dev/v4l/by-id/cam1",
        cam2_by_id="/dev/v4l/by-id/cam2",
        monitor=mon,
        layout=layout,
        groups=groups,
        sample_weights=weights,
    )
    assert refined.rmse_x < 20.0
    assert refined.rmse_y < 20.0

    # Simulate a 6cm posture shift on ty/tz later — must NOT blow up out of bounds!
    shifted = X[0].copy()
    shifted[8] += 0.06
    shifted[9] += 0.08
    shifted[18] -= 0.06
    shifted[19] += 0.08
    sx, sy = model.predict(shifted)
    assert abs(sx - targets[0, 0]) < 150.0
    assert abs(sy - targets[0, 1]) < 150.0

    calib_file = tmp_path / "stereo_calib.json"
    model.save(calib_file)
    loaded = StereoGazeModel.load(calib_file)
    assert loaded.camera_mode == "mono"
    pred_orig = model.predict(X[0])
    pred_loaded = loaded.predict(X[0])
    assert np.allclose(pred_orig, pred_loaded, atol=1e-6)


def test_iris_step_distance() -> None:
    a = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)
    b = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)
    b[0] = 0.03
    b[11] = 0.04
    assert np.isclose(_iris_step_distance(a, b), 0.05)


def test_replay_dataset_roundtrip_and_camera_guard(tmp_path) -> None:
    ds_path = tmp_path / "calib_dataset.npz"
    rng = np.random.default_rng(7)
    X = rng.normal(0.0, 0.1, size=(50, STEREO_FEATURE_DIM))
    Y = rng.uniform(0.0, 1000.0, size=(50, 2))
    groups = np.zeros(50, dtype=np.intp)
    weights = np.ones(50, dtype=np.float64) * 2.0

    _save_replay_dataset(ds_path, X, Y, groups, weights, "cam1_id", "cam2_id")
    loaded = _load_replay_dataset(ds_path, "cam1_id", "cam2_id")
    assert loaded is not None
    feats, targs, grps, wgts = loaded
    assert len(feats) == 50
    assert len(targs) == 50
    assert len(grps) == 50
    assert np.isclose(wgts[0], 1.7)  # 0.85 decay factor

    assert _load_replay_dataset(ds_path, "other_cam1", "cam2_id") is None


def test_align_and_filter_pursuit_stroke() -> None:
    times = [i * 0.033 for i in range(30)]
    cursor_hist = [(t, int(500 + 400 * t), int(300 + 200 * t), 200.0) for t in times]
    feats = []
    for t in times:
        v = np.zeros(STEREO_FEATURE_DIM, dtype=np.float64)
        # Eye lags cursor by ~66ms
        lag_t = max(0.0, t - 0.066)
        v[[0, 2, 10, 12]] = lag_t * 0.4
        v[[1, 3, 11, 13]] = lag_t * 0.2
        feats.append(v)

    clean_f, clean_t = _align_and_filter_pursuit_stroke(
        stroke_times=times,
        stroke_feats=feats,
        cursor_history=cursor_hist,
        mon_x=1920,
        mon_y=0,
    )
    assert 20 <= len(clean_f) <= 30
    assert len(clean_f) == len(clean_t)
    assert clean_t[0][0] >= 1920.0


def test_trim_dot_outliers_removes_saccade_tails() -> None:
    base = [np.zeros(STEREO_FEATURE_DIM, dtype=np.float64) for _ in range(8)]
    outliers = [np.ones(STEREO_FEATURE_DIM, dtype=np.float64) * 5.0 for _ in range(2)]
    trimmed = _trim_dot_outliers(base + outliers, keep_ratio=0.80)
    assert len(trimmed) == 8
    for vec in trimmed:
        assert np.allclose(vec, 0.0)


def test_one_euro_filter_reduces_noise() -> None:
    flt = OneEuroFilter2D(min_cutoff=0.75, beta=0.04)
    rng = np.random.default_rng(0)
    raw = 500.0 + rng.normal(0.0, 15.0, size=60)
    out = [flt(float(v), 600.0, i / 30.0)[0] for i, v in enumerate(raw)]
    assert np.std(out[10:]) < np.std(raw[10:])


def test_one_euro_alternating_jitter_deadband_and_saccade_release() -> None:
    flt = OneEuroFilter2D(min_cutoff=0.75, beta=0.04)
    # Alternating +/-50px webcam jitter around (1500, 700) should stay tightly damped
    xs, ys = [], []
    for i in range(40):
        sign = 1.0 if i % 2 == 0 else -1.0
        ox, oy = flt(1500.0 + sign * 50.0, 700.0 + sign * 50.0, i / 30.0)
        xs.append(ox)
        ys.append(oy)
    assert np.std(xs[20:]) < 10.0
    assert np.std(np.diff(ys[20:])) < np.std(np.diff(xs[20:]))  # Y has tighter jitter damping than X

    # Sustained 700px saccade jump: 1-frame spike is damped, then confirmed saccade releases rapidly
    for j in range(6):
        ox, oy = flt(2200.0, 1100.0, (40 + j) / 30.0)
    assert ox > 1900.0
    assert oy > 880.0
    for j in range(6, 10):
        ox, oy = flt(2200.0, 1100.0, (40 + j) / 30.0)
    assert ox > 2050.0
    assert oy > 980.0


def test_gimbal_saccade_deadzone_and_blink_suppression() -> None:
    pointer = DryRunPointerBackend(3440, 1440)
    ctrl = GazeGimbalController(
        screen_width=3440,
        screen_height=1440,
        saccade_threshold_px=120.0,
        saccade_hold_ms=65.0,
    )

    # Frame 0: sets saccade candidate without jumping
    ctrl.update_vision((2400.0, 720.0), (0.50, 0.50), is_blinking=False, t_sec=0.00)
    res0 = ctrl.step_output(pointer, face_detected=True, is_blinking=False, t_sec=0.00)
    assert not res0.jumped

    # Stale-frame tick at 60Hz (no new update_vision) must NOT jump even if time passes
    res_stale = ctrl.step_output(pointer, face_detected=True, is_blinking=False, t_sec=0.08)
    assert not res_stale.jumped

    # 2 distinct camera frames agreeing after >=65ms -> triggers absolute saccade jump
    ctrl.update_vision((2400.0, 720.0), (0.50, 0.50), is_blinking=False, t_sec=0.033)
    res1 = ctrl.step_output(pointer, face_detected=True, is_blinking=False, t_sec=0.033)
    assert not res1.jumped

    ctrl.update_vision((2400.0, 720.0), (0.50, 0.50), is_blinking=False, t_sec=0.070)
    res2 = ctrl.step_output(pointer, face_detected=True, is_blinking=False, t_sec=0.070)
    assert res2.jumped
    assert res2.flags & FLAG_SACCADE_JUMP
    assert pointer.last_abs is not None
    assert abs(pointer.last_abs[0] - 2400.0) < 5.0

    # Micro-jitter inside deadband (<12px) stays completely still
    ctrl.update_vision((2404.0, 722.0), (0.49, 0.505), is_blinking=False, t_sec=0.10)
    res_still = ctrl.step_output(pointer, face_detected=True, is_blinking=False, t_sec=0.10)
    assert not res_still.jumped
    assert res_still.rel_dx == 0.0 and res_still.rel_dy == 0.0

    # Blink freezes both saccade and glide motion
    ctrl.update_vision((500.0, 200.0), (0.60, 0.60), is_blinking=True, t_sec=0.15)
    res3 = ctrl.step_output(pointer, face_detected=True, is_blinking=True, t_sec=0.15)
    assert not res3.jumped
    assert res3.flags & FLAG_BLINKING
    assert res3.rel_dx == 0.0 and res3.rel_dy == 0.0
