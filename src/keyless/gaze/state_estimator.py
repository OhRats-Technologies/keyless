"""Physiological 3D angular gaze state estimator (Kalman + I-VT + Blink Rollback + REM Gate).

Eliminates the three primary artifacts of webcam eye tracking:
1. Blink dives (eyelid droop + Bell's phenomenon dragging iris landmarks toward
   screen corners):
   - Early blink entry via low blendshape threshold (default 0.25) + positive
     blink-velocity trigger so closing-eyelid frames are caught immediately.
   - Pre-blink rollback: reverts the filter state to the cleanest angular
     estimate ~85 ms before blink onset and zeros angular velocity.
   - Hysteresis exit + post-blink RECOVERY holdoff (~115 ms) while eyelids
     reopen and Bell's phenomenon settles.
2. Involuntary microsaccades / foveal tremor during fixation:
   - 4-state constant-velocity Kalman filter [az, el, v_az, v_el] with dual
     noise covariance regimes (tight FIXATION vs. responsive SACCADE) and a
     foveal deadband.
3. Single-frame landmark glitches & involuntary REM twitches:
   - Ballistic jumps above the saccade threshold require 2-frame directional
     confirmation before the filter jumps; unconfirmed 1-frame spikes are
     rejected completely and counted in ``outliers_suppressed``.
"""

from __future__ import annotations

import enum
import math
from collections import deque
from dataclasses import dataclass

import numpy as np


class GazeKinematics(str, enum.Enum):
    FIXATION = "fixation"
    SACCADE = "saccade"
    BLINK_HOLD = "blink_hold"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class GazeStateEstimate:
    """Filtered 3D gaze ray angles and physiological kinematic state."""

    az: float
    el: float
    vel_deg_s: float
    kinematics: GazeKinematics
    suppressed: bool = False


class GazeStateEstimator:
    """4-state 3D angular Kalman filter with blink rollback and 2-frame saccade confirmation."""

    def __init__(
        self,
        *,
        blink_enter: float = 0.25,
        blink_exit: float = 0.16,
        blink_vel_enter: float = 2.0,
        blink_vel_min_score: float = 0.14,
        pre_blink_rollback_s: float = 0.085,
        recovery_holdoff_s: float = 0.115,
        fixation_deadband_deg: float = 0.45,
        saccade_jump_deg: float = 2.4,
        saccade_confirm_deg: float = 3.5,
        saccade_exit_vel_deg_s: float = 18.0,
    ) -> None:
        self.blink_enter = float(blink_enter)
        self.blink_exit = float(blink_exit)
        self.blink_vel_enter = float(blink_vel_enter)
        self.blink_vel_min_score = float(blink_vel_min_score)
        self.pre_blink_rollback_s = float(pre_blink_rollback_s)
        self.recovery_holdoff_s = float(recovery_holdoff_s)
        self.fixation_deadband_rad = math.radians(float(fixation_deadband_deg))
        self.saccade_jump_rad = math.radians(float(saccade_jump_deg))
        self.saccade_confirm_rad = math.radians(float(saccade_confirm_deg))
        self.saccade_exit_vel_rad_s = math.radians(float(saccade_exit_vel_deg_s))

        # Kalman state: [az, el, v_az, v_el]
        self._x: np.ndarray | None = None
        self._P: np.ndarray = np.eye(4, dtype=np.float64) * 1e-3
        self._t_prev: float | None = None

        # Blink state machine
        self._in_blink: bool = False
        self._reopened_at: float | None = None
        self._prev_blink: float = 0.0
        self._clean_history: deque[tuple[float, float, float]] = deque(maxlen=24)

        # Saccade / outlier confirmation state
        self._kin: GazeKinematics = GazeKinematics.FIXATION
        self._pending_jump: tuple[float, float, float] | None = None
        self._slow_frames: int = 0

        # Telemetry counters
        self.blinks_suppressed: int = 0
        self.outliers_suppressed: int = 0

    def reset(self) -> None:
        self._x = None
        self._P = np.eye(4, dtype=np.float64) * 1e-3
        self._t_prev = None
        self._in_blink = False
        self._reopened_at = None
        self._prev_blink = 0.0
        self._clean_history.clear()
        self._kin = GazeKinematics.FIXATION
        self._pending_jump = None
        self._slow_frames = 0

    def _rollback_to_pre_blink(self, now_sec: float) -> None:
        if self._x is None or not self._clean_history:
            return
        target_t = now_sec - self.pre_blink_rollback_s
        chosen = self._clean_history[0]
        for sample in self._clean_history:
            if sample[0] <= target_t:
                chosen = sample
            else:
                break
        self._x[0] = chosen[1]
        self._x[1] = chosen[2]
        self._x[2] = 0.0
        self._x[3] = 0.0
        self._pending_jump = None

    def _kalman_step(
        self,
        z_az: float,
        z_el: float,
        dt: float,
        *,
        q_pos: float,
        q_vel: float,
        r_meas: float,
    ) -> None:
        assert self._x is not None
        damp = math.exp(-dt * 8.0) if self._kin is GazeKinematics.FIXATION else math.exp(-dt * 2.5)
        F = np.array(
            [
                [1.0, 0.0, dt, 0.0],
                [0.0, 1.0, 0.0, dt],
                [0.0, 0.0, damp, 0.0],
                [0.0, 0.0, 0.0, damp],
            ],
            dtype=np.float64,
        )
        Q = np.diag([q_pos * dt, q_pos * dt, q_vel * dt, q_vel * dt])
        H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=np.float64)
        R = np.diag([r_meas, r_meas])

        x_pred = F @ self._x
        P_pred = F @ self._P @ F.T + Q

        z = np.array([z_az, z_el], dtype=np.float64)
        y = z - (H @ x_pred)
        S = H @ P_pred @ H.T + R
        K = P_pred @ H.T @ np.linalg.inv(S)
        self._x = x_pred + K @ y
        I = np.eye(4, dtype=np.float64)
        self._P = (I - K @ H) @ P_pred

    def update(
        self,
        raw_az: float,
        raw_el: float,
        t_sec: float,
        *,
        blink_level: float = 0.0,
        confidence: float = 1.0,
    ) -> GazeStateEstimate:
        """Ingest one raw 3D angular observation and return the stabilized estimate."""
        t = float(t_sec)
        az = float(raw_az)
        el = float(raw_el)
        b = max(0.0, min(1.0, float(blink_level)))
        conf = max(0.1, min(1.0, float(confidence)))

        if self._x is None or self._t_prev is None:
            self._x = np.array([az, el, 0.0, 0.0], dtype=np.float64)
            self._t_prev = t
            self._prev_blink = b
            if b < self.blink_enter:
                self._clean_history.append((t, az, el))
            return GazeStateEstimate(az, el, 0.0, GazeKinematics.FIXATION, suppressed=False)

        dt = max(1e-3, min(0.25, t - self._t_prev))
        self._t_prev = t
        b_vel = (b - self._prev_blink) / dt
        self._prev_blink = b

        # 1. Blink / eyelid-droop state machine with pre-blink rollback + recovery holdoff
        blink_trigger = (
            b >= self.blink_enter
            or (b >= self.blink_vel_min_score and b_vel >= self.blink_vel_enter)
        )
        if blink_trigger:
            if not self._in_blink:
                self._in_blink = True
                self.blinks_suppressed += 1
                self._rollback_to_pre_blink(t)
            self._reopened_at = None
            return GazeStateEstimate(
                float(self._x[0]),
                float(self._x[1]),
                0.0,
                GazeKinematics.BLINK_HOLD,
                suppressed=True,
            )

        if self._in_blink:
            if b > self.blink_exit:
                return GazeStateEstimate(
                    float(self._x[0]),
                    float(self._x[1]),
                    0.0,
                    GazeKinematics.BLINK_HOLD,
                    suppressed=True,
                )
            if self._reopened_at is None:
                self._reopened_at = t
            if (t - self._reopened_at) < self.recovery_holdoff_s:
                return GazeStateEstimate(
                    float(self._x[0]),
                    float(self._x[1]),
                    0.0,
                    GazeKinematics.RECOVERY,
                    suppressed=True,
                )
            # Recovery complete -> resume clean fixation
            self._in_blink = False
            self._reopened_at = None
            self._x[2] = 0.0
            self._x[3] = 0.0
            self._pending_jump = None
            self._kin = GazeKinematics.FIXATION

        # 2. Innovation gating & 2-frame saccade confirmation (rejects 1-frame REM/glitch spikes)
        cur_az, cur_el = float(self._x[0]), float(self._x[1])
        innov_dist = math.hypot(az - cur_az, el - cur_el)

        if innov_dist >= self.saccade_jump_rad and self._kin is not GazeKinematics.SACCADE:
            if self._pending_jump is None:
                self._pending_jump = (t, az, el)
                self.outliers_suppressed += 1
                return GazeStateEstimate(
                    cur_az,
                    cur_el,
                    math.degrees(math.hypot(float(self._x[2]), float(self._x[3]))),
                    GazeKinematics.FIXATION,
                    suppressed=True,
                )
            pt, paz, pel = self._pending_jump
            confirm_dist = math.hypot(az - paz, el - pel)
            same_dir = ((az - cur_az) * (paz - cur_az) + (el - cur_el) * (pel - cur_el)) > 0.0
            if (t - pt) <= 0.15 and (confirm_dist <= self.saccade_confirm_rad or same_dir):
                # Confirmed intentional saccade
                self._pending_jump = None
                self._kin = GazeKinematics.SACCADE
                self._slow_frames = 0
            else:
                self._pending_jump = (t, az, el)
                self.outliers_suppressed += 1
                return GazeStateEstimate(
                    cur_az,
                    cur_el,
                    math.degrees(math.hypot(float(self._x[2]), float(self._x[3]))),
                    GazeKinematics.FIXATION,
                    suppressed=True,
                )
        else:
            self._pending_jump = None

        # 3. Dual-regime Kalman update weighted by landmark confidence
        r_scale = 1.0 / (conf * conf)
        if self._kin is GazeKinematics.SACCADE:
            self._kalman_step(az, el, dt, q_pos=1.8e-2, q_vel=4.0, r_meas=5e-5 * r_scale)
            vel_rad_s = math.hypot(float(self._x[2]), float(self._x[3]))
            if vel_rad_s < self.saccade_exit_vel_rad_s and innov_dist < self.saccade_jump_rad * 0.65:
                self._slow_frames += 1
                if self._slow_frames >= 2:
                    self._kin = GazeKinematics.FIXATION
                    self._x[2] *= 0.2
                    self._x[3] *= 0.2
            else:
                self._slow_frames = 0
        else:
            if innov_dist < self.fixation_deadband_rad:
                blend = 0.20 * (innov_dist / max(1e-6, self.fixation_deadband_rad))
                target_az = cur_az + blend * (az - cur_az)
                target_el = cur_el + blend * (el - cur_el)
                self._kalman_step(target_az, target_el, dt, q_pos=2e-5, q_vel=2e-3, r_meas=6e-4 * r_scale)
            else:
                self._kalman_step(az, el, dt, q_pos=2.5e-4, q_vel=4e-2, r_meas=3e-4 * r_scale)

        out_az, out_el = float(self._x[0]), float(self._x[1])
        vel_deg_s = math.degrees(math.hypot(float(self._x[2]), float(self._x[3])))
        if b < self.blink_exit:
            self._clean_history.append((t, out_az, out_el))

        return GazeStateEstimate(out_az, out_el, vel_deg_s, self._kin, suppressed=False)


__all__ = ["GazeKinematics", "GazeStateEstimate", "GazeStateEstimator"]
