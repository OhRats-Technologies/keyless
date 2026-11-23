"""Headless Qt tests for the translucent interactive calibration overlay."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from keyless.config import AppConfig, DisplayLayout, MonitorInfo
from keyless.config.paths import HOME_ENV, default_paths
from keyless.ui.calibration_overlay import CalibrationOverlayWindow
from keyless.vision.landmarks import CameraObservation


def _fake_obs(iris_x: float = 0.01, iris_y: float = 0.01) -> CameraObservation:
    return CameraObservation(
        iris_a_dx=iris_x,
        iris_a_dy=iris_y,
        iris_b_dx=iris_x,
        iris_b_dy=iris_y,
        yaw=0.02,
        pitch=-0.01,
        roll=0.0,
        tx=0.0,
        ty=0.0,
        tz=0.5,
        nose_x=0.5,
        nose_y=0.5,
        blink_left=0.05,
        blink_right=0.05,
        jaw_open=0.0,
        confidence_a=0.95,
        confidence_b=0.95,
        iris_a_px=(300, 200),
        iris_b_px=(340, 200),
        nose_px=(320, 240),
        eye_corners_px=((280, 200), (310, 200), (330, 200), (360, 200)),
    )


class _FakeStereo:
    def __init__(self) -> None:
        self._seq = 0

    def read_pair(self):
        self._seq += 1
        obs = _fake_obs(0.01 + 0.002 * float(np.sin(self._seq * 0.2)), 0.01)
        return SimpleNamespace(
            cam1=SimpleNamespace(seq=self._seq, observation=obs),
            cam2=SimpleNamespace(seq=self._seq, observation=obs),
        )


def test_translucent_overlay_calibration_end_to_end(qapp, tmp_path, monkeypatch) -> None:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    mon = MonitorInfo(
        connector="DP-1",
        x=0,
        y=0,
        width=1280,
        height=720,
        physical_width=1280,
        physical_height=720,
        scale=1.0,
        is_primary=True,
        width_mm=530,
        height_mm=300,
    )
    layout = DisplayLayout(monitors=[mon], total_width=1280, total_height=720)
    calib_path = paths.calibration_path

    saved: list[object] = []

    class _DummyModel:
        rmse_x = 11.2
        rmse_y = 14.5
        cv_rmse_x = 15.0
        cv_rmse_y = 18.0
        n_train_samples = 40
        monitors = {"DP-1": mon}

        def predict(self, feat: np.ndarray) -> tuple[float, float]:
            return (640.0 + float(feat[0]) * 10.0, 360.0)

        def save(self, path) -> None:
            path.write_text(json.dumps({"version": "geo-1", "rmse_x": self.rmse_x}) + "\n")

    monkeypatch.setattr("keyless.vision.calibrator._fit_gaze_model", lambda *a, **k: _DummyModel())

    win = CalibrationOverlayWindow(
        stereo=_FakeStereo(),
        config=AppConfig(gaze_model="geo", tts_onboarding_help=False),
        target_mon=mon,
        layout=layout,
        calib_path=calib_path,
        samples_per_point=4,
        settle_sec=0.01,
        pose_settle_sec=0.01,
        sweep1_duration_sec=0.08,
        sweep2_duration_sec=0.08,
        sweep_lead_in_sec=0.05,
        on_finished=saved.append,
    )
    try:
        assert bool(win.windowFlags() & Qt.WindowType.FramelessWindowHint)
        assert bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        assert win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True

        # Tick once so face detection turns ready
        win._tick()
        assert win.primary_btn.isEnabled() is True
        win._toggle_veil()
        assert win._veil_mode == "medium"

        # Start calibration and tick quickly through anchors + 2 short glides -> refine
        win._on_primary_clicked()
        assert win._phase == "anchors"
        deadline = time.monotonic() + 4.0
        while win._phase != "refine" and time.monotonic() < deadline:
            win._tick()
            qapp.processEvents()
            time.sleep(0.015)

        assert win._phase == "refine"

        # Interactive Spot-fix + save
        win.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(500.0, 360.0),
                QPointF(500.0, 360.0),
                Qt.MouseButton.RightButton,
                Qt.MouseButton.RightButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        for _ in range(25):
            win._tick()
            qapp.processEvents()

        win._save_and_finish()
        assert len(saved) == 1
        assert calib_path.exists()
    finally:
        win.close()


def test_pursuit2_transitions_to_fitting_and_submits_once(qapp, tmp_path, monkeypatch) -> None:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    mon = MonitorInfo(
        connector="DP-1",
        x=0,
        y=0,
        width=1280,
        height=720,
        physical_width=1280,
        physical_height=720,
        scale=1.0,
        is_primary=True,
        width_mm=530,
        height_mm=300,
    )
    layout = DisplayLayout(monitors=[mon], total_width=1280, total_height=720)
    fit_calls: list[int] = []

    class _SlowModel:
        rmse_x = 12.0
        rmse_y = 14.0
        n_train_samples = 40

        def predict(self, feat: np.ndarray) -> tuple[float, float]:
            return (640.0, 360.0)

    def _slow_fit(X, *args, **kwargs):
        fit_calls.append(len(X))
        time.sleep(0.15)
        return _SlowModel()

    monkeypatch.setattr("keyless.vision.calibrator._fit_gaze_model", _slow_fit)

    win = CalibrationOverlayWindow(
        stereo=_FakeStereo(),
        config=AppConfig(gaze_model="geo", tts_onboarding_help=False),
        target_mon=mon,
        layout=layout,
        calib_path=paths.calibration_path,
        samples_per_point=4,
        settle_sec=0.01,
        pose_settle_sec=0.01,
        sweep1_duration_sec=0.05,
        sweep2_duration_sec=0.05,
        sweep_lead_in_sec=0.02,
    )
    try:
        win._tick()
        win._on_primary_clicked()
        saw_fitting = False
        deadline = time.monotonic() + 5.0
        while win._phase != "refine" and time.monotonic() < deadline:
            win._tick()
            if win._phase == "fitting":
                saw_fitting = True
            qapp.processEvents()
            time.sleep(0.01)

        assert saw_fitting is True
        assert win._phase == "refine"
        assert len(fit_calls) == 1
    finally:
        win.close()
