"""Two-timescale online gaze adaptation: Huber-gated 2-D posture bias + CV-validated background refit."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time

import numpy as np

from keyless.config import (
    DEFAULT_CALIB_PATH,
    AppConfig,
    DisplayLayout,
    MonitorInfo,
)
from keyless.vision.calibrator import (
    STEREO_FEATURE_DIM,
    StereoGazeModel,
    _default_dataset_path,
    _iris_step_distance,
    _load_replay_dataset,
    _save_replay_dataset,
)


@dataclass(frozen=True)
class _GazeHistoryFrame:
    """Single timestamped stereo observation in the 2.5-second lookback ring buffer."""

    ts: float
    feat: np.ndarray
    base_x: float
    base_y: float
    iris_speed: float


class OnlineGazeLearner:
    """Safely adapts gaze predictions to posture drift without distorting or corrupting calibration.

    - Fast layer (<0.05ms): Huber-gated 2-D translation bias filter `(dx, dy)` bounded to
      `+/- max_bias_px` with per-click step capping. Because it only adapts translation offset
      (not multi-dimensional screen/pose slopes), a few clicks can never tilt or destabilize the
      gaze plane across the rest of the screen.
    - Slow layer (optional, opt-in `allow_disk_overwrite=True`): Merges verified samples and only
      hot-swaps a refitted `StereoGazeModel` if cross-validation RMSE does not degrade.
    """

    def __init__(
        self,
        model: StereoGazeModel,
        config: AppConfig,
        monitor: MonitorInfo,
        layout: DisplayLayout,
        calib_path: Path = DEFAULT_CALIB_PATH,
        forgetting_factor: float = 0.94,
        refit_interval: int = 16,
        max_bias_px: float = 160.0,
        max_step_px: float = 38.0,
        allow_disk_overwrite: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.model = model
        self.config = config
        self.monitor = monitor
        self.layout = layout
        self.calib_path = calib_path
        self.dataset_path = _default_dataset_path(calib_path)
        self.lam = float(forgetting_factor)
        self.refit_interval = int(refit_interval)
        self.max_bias_px = float(max_bias_px)
        self.max_step_px = float(max_step_px)
        self.allow_disk_overwrite = bool(allow_disk_overwrite)

        # Recent 3.0 seconds of 30Hz gaze frames for pre-click / pre-speech fixation extraction
        self._history: deque[_GazeHistoryFrame] = deque(maxlen=90)
        self._prev_feat: np.ndarray | None = None

        # Fast 2-D scalar EW-RLS posture bias filter (impossible to cause plane-tilt divergence)
        self._bias_x = 0.0
        self._bias_y = 0.0
        self._var = 1.0

        # Slow background full-model refit buffer
        self._pending_samples: list[tuple[np.ndarray, tuple[float, float], float]] = []
        self._refit_in_progress = False
        self.total_online_updates = 0
        self.last_outcome: dict[str, object] | None = None

    @property
    def cam1_by_id(self) -> str:
        return self.model.cam1_by_id

    @property
    def cam2_by_id(self) -> str:
        return self.model.cam2_by_id

    @property
    def rmse_x(self) -> float:
        return self.model.rmse_x

    @property
    def rmse_y(self) -> float:
        return self.model.rmse_y

    @property
    def bias_xy(self) -> tuple[float, float]:
        with self._lock:
            return self._bias_x, self._bias_y

    def reset_bias(self) -> None:
        """Reset session posture offset bias back to zero."""
        with self._lock:
            self._bias_x = 0.0
            self._bias_y = 0.0
            self._var = 1.0

    def predict(self, feat: np.ndarray, now_ts: float | None = None) -> tuple[float, float]:
        """Predict drift-compensated `(x, y)` and record frame into the fixation lookback buffer."""
        ts = time.monotonic() if now_ts is None else float(now_ts)
        fv = np.asarray(feat, dtype=np.float64)

        iris_speed = 0.0 if self._prev_feat is None else _iris_step_distance(self._prev_feat, fv)
        self._prev_feat = fv.copy()

        with self._lock:
            base_x, base_y = self.model.predict(fv)
            corr_x = self._bias_x
            corr_y = self._bias_y
            min_x, max_x = self.model.bounds_min_x, self.model.bounds_max_x
            min_y, max_y = self.model.bounds_min_y, self.model.bounds_max_y

        with self._lock:
            self._history.append(
                _GazeHistoryFrame(
                    ts=ts,
                    feat=fv.copy(),
                    base_x=base_x,
                    base_y=base_y,
                    iris_speed=iris_speed,
                )
            )

        out_x = max(min_x, min(max_x, base_x + corr_x))
        out_y = max(min_y, min(max_y, base_y + corr_y))
        return out_x, out_y

    def _find_best_fixation_frame(
        self,
        anchor_ts: float,
        lookback_min_sec: float = 0.06,
        lookback_max_sec: float = 0.35,
        preferred_pre_click_sec: float = 0.18,
    ) -> _GazeHistoryFrame | None:
        """Find the stable eye frame just before a click or voice onset.

        People often start a post-click saccade immediately, so the label comes
        from ~180 ms before the button went down (a typical visual-reaction
        interval) rather than from the click moment. For deliberate resting
        clicks that is still the target the pointer was sitting on.
        """
        if not self._history:
            return None

        t_hi = anchor_ts - lookback_min_sec
        t_lo = anchor_ts - lookback_max_sec
        with self._lock:
            history = list(self._history)
        candidates = [f for f in history if t_lo <= f.ts <= t_hi and f.iris_speed < 0.020]

        if not candidates:
            near = [
                f
                for f in history
                if abs(f.ts - anchor_ts) <= 0.40 and f.iris_speed < 0.025
            ]
            if not near:
                return None
            return min(near, key=lambda f: f.iris_speed)

        preferred_ts = anchor_ts - preferred_pre_click_sec
        candidates.sort(key=lambda f: (abs(f.ts - preferred_ts), f.iris_speed))
        top = candidates[:3]
        mean_feat = np.mean([f.feat for f in top], axis=0)
        with self._lock:
            bx, by = self.model.predict(mean_feat)
        return _GazeHistoryFrame(
            ts=top[0].ts,
            feat=mean_feat,
            base_x=bx,
            base_y=by,
            iris_speed=top[0].iris_speed,
        )

    def record_verified_interaction(
        self,
        true_x: float,
        true_y: float,
        anchor_ts: float | None = None,
        explicit_feat: np.ndarray | None = None,
        source: str = "voice",
        confidence_weight: float = 2.0,
    ) -> bool:
        """Ingest a verified ground-truth interaction (`voice` or `mouse`).

        Uses a tight spatial gate + Huber-weighted bounded 2-D bias step so single
        outliers or blind clicks can never degrade calibration.
        """
        ts = time.monotonic() if anchor_ts is None else float(anchor_ts)

        if explicit_feat is not None:
            fv = np.asarray(explicit_feat, dtype=np.float64)
            with self._lock:
                bx, by = self.model.predict(fv)
            fix_frame = _GazeHistoryFrame(ts=ts, feat=fv, base_x=bx, base_y=by, iris_speed=0.0)
        else:
            fix_frame = self._find_best_fixation_frame(ts)
            if fix_frame is None:
                self.last_outcome = {
                    "accepted": False,
                    "reason": "no_fixation",
                    "source": source,
                }
                return False

        with self._lock:
            cur_pred_x = fix_frame.base_x + self._bias_x
            cur_pred_y = fix_frame.base_y + self._bias_y
            live_err = math.hypot(true_x - cur_pred_x, true_y - cur_pred_y)

            # Strict spatial gate: reject clicks where user wasn't looking near the target
            max_gate_px = 200.0 if source == "mouse" else 240.0
            if live_err > max_gate_px:
                self.last_outcome = {
                    "accepted": False,
                    "reason": "gate",
                    "source": source,
                    "error_px": round(live_err, 1),
                }
                return False

            # Smooth Huber weight + bounded Kalman gain (max 0.40 per click, max +/-38px step)
            huber_delta = 70.0
            w_huber = 1.0 / math.sqrt(1.0 + (live_err / huber_delta) ** 2)

            denom = (self.lam / max(0.20, w_huber)) + self._var
            k_gain = min(0.42, self._var / denom)

            err_x = float(true_x) - cur_pred_x
            err_y = float(true_y) - cur_pred_y
            step_x = float(np.clip(k_gain * err_x, -self.max_step_px, self.max_step_px))
            step_y = float(np.clip(k_gain * err_y, -self.max_step_px, self.max_step_px))

            self._bias_x = float(np.clip(self._bias_x + step_x, -self.max_bias_px, self.max_bias_px))
            self._bias_y = float(np.clip(self._bias_y + step_y, -self.max_bias_px, self.max_bias_px))

            self._var = ((1.0 - k_gain) * self._var) / self.lam
            self._var = float(np.clip(0.95 * self._var + 0.05 * 1.0, 0.15, 1.2))

            self.total_online_updates += 1
            if self.allow_disk_overwrite:
                self._pending_samples.append(
                    (
                        fix_frame.feat.copy(),
                        (float(true_x), float(true_y)),
                        float(confidence_weight * w_huber),
                    )
                )

                should_refit = (
                    len(self._pending_samples) >= self.refit_interval
                    and not self._refit_in_progress
                )
                if should_refit:
                    self._refit_in_progress = True
                    batch_to_merge = list(self._pending_samples)
                    self._pending_samples.clear()
                    threading.Thread(
                        target=self._background_refit_worker,
                        args=(batch_to_merge,),
                        daemon=True,
                    ).start()

        print(
            f"[OnlineGaze] +1 {source} sample (err={live_err:.0f}px -> "
            f"bias=({self._bias_x:+.0f}, {self._bias_y:+.0f})px | "
            f"updates={self.total_online_updates})"
        )
        self.last_outcome = {
            "accepted": True,
            "reason": "accepted",
            "source": source,
            "error_px": round(live_err, 1),
            "bias_x": round(self._bias_x, 1),
            "bias_y": round(self._bias_y, 1),
        }
        return True

    def _background_refit_worker(
        self,
        new_samples: list[tuple[np.ndarray, tuple[float, float], float]],
    ) -> None:
        """Merge verified clicks into calib_dataset.npz and hot-swap ONLY if CV RMSE improves."""
        try:
            saved = _load_replay_dataset(
                self.dataset_path,
                self.config.cam1_by_id,
                self.config.cam2_by_id,
            )
            feats: list[np.ndarray] = []
            targs: list[tuple[float, float]] = []
            grps: list[int] = []
            wgts: list[float] = []

            if saved is not None:
                feats.extend(saved[0])
                targs.extend(saved[1])
                grps.extend(saved[2])
                wgts.extend([max(0.6, w * 0.98) for w in saved[3]])

            next_grp = (max(grps) + 1) if grps else 500
            for fv, xy, w in new_samples:
                feats.append(fv)
                targs.append(xy)
                grps.append(next_grp)
                wgts.append(w)

            if len(feats) < 40:
                return

            X_all = np.vstack(feats)
            Y_all = np.asarray(targs, dtype=np.float64)
            G_all = np.asarray(grps, dtype=np.intp)
            W_all = np.asarray(wgts, dtype=np.float64)

            new_model = StereoGazeModel.fit(
                X=X_all,
                targets_xy=Y_all,
                cam1_by_id=self.config.cam1_by_id,
                cam2_by_id=self.config.cam2_by_id,
                monitor=self.monitor,
                layout=self.layout,
                groups=G_all,
                sample_weights=W_all,
                camera_mode=self.model.camera_mode,
            )

            with self._lock:
                old_cv = self.model.cv_rmse_x + self.model.cv_rmse_y
            new_cv = new_model.cv_rmse_x + new_model.cv_rmse_y
            if old_cv > 0.0 and new_cv > old_cv + 2.0:
                print(
                    f"[OnlineGaze] Rejected refit (CV RMSE {new_cv:.1f}px > current {old_cv:.1f}px)."
                )
                return

            _save_replay_dataset(
                dataset_path=self.dataset_path,
                X=X_all,
                Y=Y_all,
                groups=G_all,
                weights=W_all,
                cam1_by_id=self.config.cam1_by_id,
                cam2_by_id=self.config.cam2_by_id,
            )
            new_model.save(self.calib_path)

            with self._lock:
                self.model = new_model
                self._bias_x *= 0.35
                self._bias_y *= 0.35

            print(
                f"[OnlineGaze] Hot-swapped re-trained manifold ({len(feats)} total samples, "
                f"CV RMSE=({new_model.cv_rmse_x:.0f}, {new_model.cv_rmse_y:.0f})px)!"
            )
        except Exception as e:
            print(f"[OnlineGaze Refit Warning]: {e}")
        finally:
            with self._lock:
                self._refit_in_progress = False
