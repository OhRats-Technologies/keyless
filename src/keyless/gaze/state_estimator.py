"""3D angular gaze state estimator: Kalman filter + pre-blink rollback + REM suppression.

Eliminates the two dominant artifacts in webcam eye tracking:
1. Blink-induced downward/corner drag (eyelid occlusion + Bell's phenomenon):
   Eyelid closure begins corrupting MediaPipe iris landmarks 60-90 ms before
   ``eyeBlink`` crosses 0.55 and lingers 80-120 ms after re-opening. A short
   ring buffer of clean pre-blink states lets the estimator rewind to the last
   clean state as soon as ``blink >= 0.24`` or ``d(blink)/dt > 2.2 /s``, hold
   rock-steady through the blink, and wait out a 115 ms recovery window.
2. Involuntary micro-saccades / REM / 1-frame landmark glitches:
   A 4-state Constant-Velocity Kalman filter ``[az, el, v_az, v_el]`` paired
   with a 2-frame directional saccade confirmation buffer and Mahalanobis
   innovation gating keeps the gaze point calmly anchored during fixation
   while snapping briskly on confirmed intentional saccades.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

import numpy as np


class GazeKinematics(str, Enum):
    FIXATION = "fixation"
    SACCADE = "saccade"
    BLINK_HOLD = "blink_hold"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class EstimatedGazeState:
    """Filtered 3D angular gaze state."""

    az: float
    el: float
    vel_az: float
    vel_el: float
    kinematics: GazeKinematics
    suppressed: bool
    blinks_suppressed: int
    outliers_suppressed: int


class GazeStateEstimator:
    """Kalman + blink-rollback + 2-frame saccade-confirmed angular state estimator."""

    def __init__(
        self,
        *,
        pre_blink_threshold: float = 0.24,
        blink_velocity_threshold: float = 2.2,
        pre_blink_lookback_sec: float = 0.090,
        post_blink_recovery_sec: float = 0.115,
        saccade_deg: float = 2.2,
        fixation_deadband_deg: float = 0.45,
        confirm_frames: int = 2,
        mahalanobis_gate: float = 4.2,
    ) -> None:
        self.pre_blink_threshold = float(pre_blink_threshold)
        self.blink_velocity_threshold = float(blink_velocity_threshold)
        self.pre_blink_lookback_sec = float(pre_blink_lookback_sec)
        self.post_blink_recovery_sec = float(post_blink_recovery_sec)
        self.saccade_rad = math.radians(float(saccade_deg))
        self.deadband_rad = math.radians(float(fixation_deadband_deg))
        self.confirm_frames = max(2, int(confirm_frames))
        self.mahalanobis_gate = float(mahalanobis_gate)

        self._x: np.ndarray | None = None
        self._P: np.ndarray = np.eye(4, dtype=np.float64) * 1e-2
        self._last_t: float | None = None
        self._last_blink: float = 0.0

        self._clean_history: deque[tuple[float, float, float]] = deque(maxlen=16)

        self._in_blink: bool = False
        self._recovery_until: float = 0.0
        self._hold_az: float = 0.0
        self._hold_el: float = 0.0

        self._pending_saccade: list[tuple[float, float]] = []
        self._kinematics: GazeKinematics = GazeKinematics.FIXATION

        self.blinks_suppressed: int = 0
        self.outliers_suppressed: int = 0

    def reset(self) -> None:
        self._x = None
        self._P = np.eye(4, dtype=np.float64) * 1e-2
        self._last_t = None
        self._last_blink = 0.0
        self._clean_history.clear()
        self._in_blink = False
        self._recovery_until = 0.0
        self._pending_saccade.clear()
        self._kinematics = GazeKinematics.FIXATION

    def _rewind_to_clean_pre_blink(self, t_sec: float) -> tuple[float, float]:
        assert self._x is not None
        target_t = t_sec - self.pre_blink_lookback_sec
        best_az, best_el = float(self._x[0]), float(self._x[1])
        for ht, haz, hel in self._clean_history:
            best_az, best_el = haz, hel
            if ht >= target_t:
                break
        self._x[0] = best_az
        self._x[1] = best_el
        self._x[2] = 0.0
        self._x[3] = 0.0
        return best_az, best_el

    def _kalman_predict(self, dt: float, process_q: float) -> None:
        assert self._x is not None
        F = np.array(
            [
                [1.0, 0.0, dt, 0.0],
                [0.0, 1.0, 0.0, dt],
                [0.0, 0.0, 0.72, 0.0],
                [0.0, 0.0, 0.0, 0.72],
            ],
            dtype=np.float64,
        )
        dt2 = dt * dt
        Q = np.diag([process_q * dt2, process_q * dt2, process_q * dt, process_q * dt])
        self._x = F @ self._x
        self._P = F @ self._P @ F.T + Q

    def _kalman_update(self, az: float, el: float, meas_r: float) -> None:
        assert self._x is not None
        H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float64)
        R = np.eye(2, dtype=np.float64) * meas_r
        z = np.array([az, el], dtype=np.float64)
        y = z - H @ self._x
        S = H @ self._P @ H.T + R
        K = self._P @ H.T @ np.linalg.inv(S)
        self._x = self._x + K @ y
        I = np.eye(4, dtype=np.float64)
        self._P = (I - K @ H) @ self._P

    def _mahalanobis(self, az: float, el: float, meas_r: float) -> float:
        assert self._x is not None
        H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float64)
        R = np.eye(2, dtype=np.float64) * meas_r
        z = np.array([az, el], dtype=np.float64)
        y = z - H @ self._x
        S = H @ self._P @ H.T + R
        try:
            d2 = float(y.T @ np.linalg.inv(S) @ y)
        except np.linalg.LinAlgError:
            d2 = float(np.dot(y, y)) / max(1e-6, meas_r)
        return math.sqrt(max(0.0, d2))

    def update(
        self,
        raw_az: float,
        raw_el: float,
        t_sec: float,
        *,
        blink_level: float = 0.0,
        confidence: float = 1.0,
    ) -> EstimatedGazeState:
        az = float(raw_az)
        el = float(raw_el)
        t = float(t_sec)
        blink = max(0.0, min(1.0, float(blink_level)))

        if self._x is None or self._last_t is None:
            self._x = np.array([az, el, 0.0, 0.0], dtype=np.float64)
            self._P = np.eye(4, dtype=np.float64) * 5e-3
            self._last_t = t
            self._last_blink = blink
            self._hold_az, self._hold_el = az, el
            if blink < self.pre_blink_threshold:
                self._clean_history.append((t, az, el))
            return EstimatedGazeState(
                az=az,
                el=el,
                vel_az=0.0,
                vel_el=0.0,
                kinematics=GazeKinematics.FIXATION,
                suppressed=False,
                blinks_suppressed=self.blinks_suppressed,
                outliers_suppressed=self.outliers_suppressed,
            )

        dt = max(1e-3, min(0.20, t - self._last_t))
        blink_vel = (blink - self._last_blink) / dt
        self._last_t = t
        self._last_blink = blink

        blink_closing = blink >= self.pre_blink_threshold or (
            blink >= 0.14 and blink_vel >= self.blink_velocity_threshold
        )
        if blink_closing:
            if not self._in_blink:
                self._in_blink = True
                self.blinks_suppressed += 1
                self._pending_saccade.clear()
                self._hold_az, self._hold_el = self._rewind_to_clean_pre_blink(t)
            self._kinematics = GazeKinematics.BLINK_HOLD
            return EstimatedGazeState(
                az=self._hold_az,
                el=self._hold_el,
                vel_az=0.0,
                vel_el=0.0,
                kinematics=GazeKinematics.BLINK_HOLD,
                suppressed=True,
                blinks_suppressed=self.blinks_suppressed,
                outliers_suppressed=self.outliers_suppressed,
            )

        if self._in_blink:
            self._in_blink = False
            self._recovery_until = t + self.post_blink_recovery_sec
            self._pending_saccade.clear()

        if t < self._recovery_until:
            self._kinematics = GazeKinematics.RECOVERY
            return EstimatedGazeState(
                az=self._hold_az,
                el=self._hold_el,
                vel_az=0.0,
                vel_el=0.0,
                kinematics=GazeKinematics.RECOVERY,
                suppressed=True,
                blinks_suppressed=self.blinks_suppressed,
                outliers_suppressed=self.outliers_suppressed,
            )

        if confidence < 0.25 or not (math.isfinite(az) and math.isfinite(el)):
            self.outliers_suppressed += 1
            return EstimatedGazeState(
                az=float(self._x[0]),
                el=float(self._x[1]),
                vel_az=0.0,
                vel_el=0.0,
                kinematics=self._kinematics,
                suppressed=True,
                blinks_suppressed=self.blinks_suppressed,
                outliers_suppressed=self.outliers_suppressed,
            )

        cur_az, cur_el = float(self._x[0]), float(self._x[1])
        disp = math.hypot(az - cur_az, el - cur_el)
        meas_r_fix = 2.2e-3 / max(0.3, min(1.0, float(confidence)))

        if disp >= self.saccade_rad:
            m_dist = self._mahalanobis(az, el, meas_r_fix)
            if self._kinematics != GazeKinematics.SACCADE and m_dist >= self.mahalanobis_gate * 0.45:
                if self._pending_saccade:
                    prev_az, prev_el = self._pending_saccade[-1]
                    v1x, v1y = prev_az - cur_az, prev_el - cur_el
                    v2x, v2y = az - cur_az, el - cur_el
                    n1 = max(1e-6, math.hypot(v1x, v1y))
                    n2 = max(1e-6, math.hypot(v2x, v2y))
                    cos_dir = (v1x * v2x + v1y * v2y) / (n1 * n2)
                    if cos_dir >= 0.5:
                        self._pending_saccade.append((az, el))
                    else:
                        self.outliers_suppressed += 1
                        self._pending_saccade = [(az, el)]
                        return EstimatedGazeState(
                            az=cur_az,
                            el=cur_el,
                            vel_az=float(self._x[2]),
                            vel_el=float(self._x[3]),
                            kinematics=GazeKinematics.FIXATION,
                            suppressed=True,
                            blinks_suppressed=self.blinks_suppressed,
                            outliers_suppressed=self.outliers_suppressed,
                        )
                else:
                    self._pending_saccade.append((az, el))

                if len(self._pending_saccade) < self.confirm_frames:
                    self.outliers_suppressed += 1
                    return EstimatedGazeState(
                        az=cur_az,
                        el=cur_el,
                        vel_az=0.0,
                        vel_el=0.0,
                        kinematics=GazeKinematics.FIXATION,
                        suppressed=True,
                        blinks_suppressed=self.blinks_suppressed,
                        outliers_suppressed=self.outliers_suppressed,
                    )

                self._kinematics = GazeKinematics.SACCADE
                self._pending_saccade.clear()
                self._kalman_predict(dt, process_q=18.0)
                self._kalman_update(az, el, meas_r=4e-4)
            else:
                self._kinematics = GazeKinematics.SACCADE
                self._pending_saccade.clear()
                self._kalman_predict(dt, process_q=14.0)
                self._kalman_update(az, el, meas_r=5e-4)
        else:
            if self._pending_saccade:
                self._pending_saccade.clear()
            self._kinematics = GazeKinematics.FIXATION
            if disp < self.deadband_rad:
                self._kalman_predict(dt, process_q=0.08)
                self._kalman_update(az, el, meas_r=meas_r_fix * 3.5)
            else:
                self._kalman_predict(dt, process_q=0.45)
                self._kalman_update(az, el, meas_r=meas_r_fix)

        out_az, out_el = float(self._x[0]), float(self._x[1])
        self._hold_az, self._hold_el = out_az, out_el

        if blink < self.pre_blink_threshold * 0.85:
            self._clean_history.append((t, out_az, out_el))

        return EstimatedGazeState(
            az=out_az,
            el=out_el,
            vel_az=float(self._x[2]),
            vel_el=float(self._x[3]),
            kinematics=self._kinematics,
            suppressed=False,
            blinks_suppressed=self.blinks_suppressed,
            outliers_suppressed=self.outliers_suppressed,
        )


__all__ = ["EstimatedGazeState", "GazeKinematics", "GazeStateEstimator"]
