"""Hybrid Huber-Polynomial + Multi-Scale RBF Kernel Stereo Calibrator with Persistent Replay & Pursuit."""

from __future__ import annotations

import json
import math
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from sklearn.preprocessing import StandardScaler

from keyless.config import (
    DEFAULT_CALIB_PATH,
    AppConfig,
    DisplayLayout,
    MonitorInfo,
    get_display_layout,
)
from keyless.platform.linux.cameras import resolve_camera_config
from keyless.vision.landmarks import (
    STEREO_FEATURE_DIM,
    build_stereo_feature_vector,
)
from keyless.vision.one_euro import OneEuroFilter2D
from keyless.vision.stereo_capture import StereoCapture

# 20 linear + 8 pose-iris cross terms + 6 quadratic/keystone screen terms
EXPANDED_FEATURE_DIM = 34
MAX_REPLAY_SAMPLES = 12000
MAX_RBF_CENTERS = 420
_IRIS_COLS = [0, 1, 2, 3, 10, 11, 12, 13]


@dataclass(frozen=True)
class CalibrationPoint:
    """Single static anchor target in Stage 1 before continuous smooth-pursuit sweeps."""

    rel_x: float
    rel_y: float
    prompt: str
    is_pose_variation: bool = False


# Fast 9-point anchor grid + 4 cardinal head-pose anchors (followed by 2 continuous gliding sweeps)
ANCHOR_CALIBRATION_POINTS: tuple[CalibrationPoint, ...] = (
    CalibrationPoint(0.50, 0.50, "Anchor 1/13: Center"),
    CalibrationPoint(0.06, 0.08, "Anchor 2/13: Top-Left"),
    CalibrationPoint(0.94, 0.92, "Anchor 3/13: Bottom-Right"),
    CalibrationPoint(0.94, 0.08, "Anchor 4/13: Top-Right"),
    CalibrationPoint(0.06, 0.92, "Anchor 5/13: Bottom-Left"),
    CalibrationPoint(0.06, 0.50, "Anchor 6/13: Mid-Left"),
    CalibrationPoint(0.94, 0.50, "Anchor 7/13: Mid-Right"),
    CalibrationPoint(0.50, 0.08, "Anchor 8/13: Top-Center"),
    CalibrationPoint(0.50, 0.92, "Anchor 9/13: Bottom-Center"),
    CalibrationPoint(0.50, 0.50, "Anchor 10/13: TURN HEAD LEFT ~15 deg (eyes on dot)", True),
    CalibrationPoint(0.50, 0.50, "Anchor 11/13: TURN HEAD RIGHT ~15 deg (eyes on dot)", True),
    CalibrationPoint(0.50, 0.50, "Anchor 12/13: TILT CHIN UP (eyes on dot)", True),
    CalibrationPoint(0.50, 0.50, "Anchor 13/13: TILT CHIN DOWN (eyes on dot)", True),
)

# Physical minimum std-dev floors: preserves full vertical iris sensitivity (0.004)
_MIN_FEATURE_SCALE = np.array(
    [
        # Cam 1: iris_a_dx, iris_a_dy, iris_b_dx, iris_b_dy, yaw, pitch, roll, tx, ty, tz
        0.012, 0.004, 0.012, 0.004, 0.025, 0.020, 0.040, 0.030, 0.030, 0.030,
        # Cam 2: iris_a_dx, iris_a_dy, iris_b_dx, iris_b_dy, yaw, pitch, roll, tx, ty, tz
        0.012, 0.004, 0.012, 0.004, 0.025, 0.020, 0.040, 0.030, 0.030, 0.030,
    ],
    dtype=np.float64,
)

# Expanded 34-D indices for raw head translation (tx, ty, tz), roll, and iris*tz cross-terms.
# Zeroing these stops chair slouches 1 minute after calibration from linearly pushing gaze off-target;
# yaw_los and pitch_los in landmarks.py already geometrically compensate for camera viewing ray angles.
_POSE_TRANSLATION_EXPANDED_COLS: tuple[int, ...] = (6, 7, 8, 9, 16, 17, 18, 19, 21, 23, 25, 27)
_POSE_TRANSLATION_RAW_COLS: tuple[int, ...] = (6, 7, 8, 9, 16, 17, 18, 19)

# Stage 1 Axis-aware feature weights (34-D polynomial base, pose-translation invariant)
_PRIOR_WEIGHTS_X = np.array(
    [
        # Cam 1: ax, ay, bx, by, yaw, pitch, roll, tx, ty, tz
        1.00, 0.25, 1.00, 0.25, 0.85, 0.20, 0.00, 0.00, 0.00, 0.00,
        # Cam 2: ax, ay, bx, by, yaw, pitch, roll, tx, ty, tz
        1.00, 0.25, 1.00, 0.25, 0.85, 0.20, 0.00, 0.00, 0.00, 0.00,
        # 8 pose-iris interaction terms (keep iris*yaw and iris*pitch, zero iris*tz)
        0.55, 0.00, 0.55, 0.00, 0.18, 0.00, 0.18, 0.00,
        # 6 quadratic & keystone terms
        0.35, 0.15, 0.45, 0.35, 0.15, 0.45,
    ],
    dtype=np.float64,
)

_PRIOR_WEIGHTS_Y = np.array(
    [
        # Cam 1: ax, ay, bx, by, yaw, pitch, roll, tx, ty, tz
        0.25, 1.00, 0.25, 1.00, 0.20, 0.85, 0.00, 0.00, 0.00, 0.00,
        # Cam 2: ax, ay, bx, by, yaw, pitch, roll, tx, ty, tz
        0.25, 1.00, 0.25, 1.00, 0.20, 0.85, 0.00, 0.00, 0.00, 0.00,
        # 8 pose-iris interaction terms (keep iris*yaw and iris*pitch, zero iris*tz)
        0.18, 0.00, 0.18, 0.00, 0.55, 0.00, 0.55, 0.00,
        # 6 quadratic & keystone terms
        0.15, 0.50, 0.45, 0.15, 0.50, 0.45,
    ],
    dtype=np.float64,
)

# Stage-2 RBF Metric Weights (Pose-Translation Invariant):
# Zero weight on (roll, tx, ty, tz) so slouching or leaning 5cm in your chair a minute
# after calibration NEVER pushes your feature vector off the 420 RBF support centers!
_RBF_METRIC_WEIGHTS = np.array(
    [
        # Cam 1: ax, ay, bx, by, yaw, pitch, roll, tx, ty, tz
        0.95, 1.50, 0.95, 1.50, 0.22, 0.30, 0.00, 0.00, 0.00, 0.00,
        # Cam 2: ax, ay, bx, by, yaw, pitch, roll, tx, ty, tz
        0.95, 1.50, 0.95, 1.50, 0.22, 0.30, 0.00, 0.00, 0.00, 0.00,
    ],
    dtype=np.float64,
)


def _default_dataset_path(calib_path: Path) -> Path:
    return calib_path.with_name("calib_dataset.npz")


def expand_stereo_features(Z: np.ndarray) -> np.ndarray:
    """Expand standardized 20-D features into 34-D with soft-clamped head translation."""
    single = Z.ndim == 1
    arr = np.array(Z[np.newaxis, :] if single else Z, dtype=np.float64, copy=True)

    # Soft-saturate head translation (tx, ty, tz) & roll at +/- 2.2 sigma via tanh
    # so chair slouches after calibration cannot extrapolate linearly to infinity.
    pose_trans_cols = [6, 7, 8, 9, 16, 17, 18, 19]
    arr[:, pose_trans_cols] = 2.2 * np.tanh(arr[:, pose_trans_cols] / 2.2)
    arr_c = np.clip(arr, -4.0, 4.0)

    c1_dx = 0.5 * (arr_c[:, 0] + arr_c[:, 2])
    c1_dy = 0.5 * (arr_c[:, 1] + arr_c[:, 3])
    c2_dx = 0.5 * (arr_c[:, 10] + arr_c[:, 12])
    c2_dy = 0.5 * (arr_c[:, 11] + arr_c[:, 13])

    interactions = np.column_stack(
        [
            c1_dx * arr_c[:, 4],   # cam1 iris_dx * cam1 yaw
            c1_dx * arr_c[:, 9],   # cam1 iris_dx * cam1 tz
            c2_dx * arr_c[:, 14],  # cam2 iris_dx * cam2 yaw
            c2_dx * arr_c[:, 19],  # cam2 iris_dx * cam2 tz
            c1_dy * arr_c[:, 5],   # cam1 iris_dy * cam1 pitch
            c1_dy * arr_c[:, 9],   # cam1 iris_dy * cam1 tz
            c2_dy * arr_c[:, 15],  # cam2 iris_dy * cam2 pitch
            c2_dy * arr_c[:, 19],  # cam2 iris_dy * cam2 tz
            c1_dx * c1_dx,
            c1_dy * c1_dy,
            c1_dx * c1_dy,
            c2_dx * c2_dx,
            c2_dy * c2_dy,
            c2_dx * c2_dy,
        ]
    )
    out = np.hstack([arr_c, interactions])
    return out[0] if single else out


def _iris_step_distance(f_prev: np.ndarray, f_curr: np.ndarray) -> float:
    """Compute inter-frame 8-D iris jump magnitude to gate out active saccades."""
    return float(np.linalg.norm(f_curr[_IRIS_COLS] - f_prev[_IRIS_COLS]))


def _trim_dot_outliers(features: list[np.ndarray], keep_ratio: float = 0.82) -> list[np.ndarray]:
    """Drop frames with the largest iris deviation from the per-dot median."""
    if len(features) <= 6:
        return features
    mat = np.vstack(features)
    med = np.median(mat[:, _IRIS_COLS], axis=0)
    mad = np.median(np.abs(mat[:, _IRIS_COLS] - med), axis=0) + 1e-4
    dists = np.linalg.norm((mat[:, _IRIS_COLS] - med) / mad, axis=1)
    n_keep = max(5, int(round(len(features) * keep_ratio)))
    keep_idx = np.argsort(dists)[:n_keep]
    return [features[int(i)] for i in sorted(keep_idx)]


def _fit_irls_huber_ridge(
    X_w: np.ndarray,
    y: np.ndarray,
    sample_weights: np.ndarray,
    alpha: float = 8.0,
    huber_delta: float = 35.0,
    n_iter: int = 8,
) -> tuple[np.ndarray, float, np.ndarray]:
    """Fit L2-regularized regression using IRLS with smooth Charbonnier/Huber loss."""
    n_samples, n_features = X_w.shape
    X_aug = np.column_stack([X_w, np.ones(n_samples, dtype=np.float64)])
    reg_mat = np.eye(n_features + 1, dtype=np.float64) * alpha
    reg_mat[-1, -1] = 0.0

    w_robust = np.ones(n_samples, dtype=np.float64)
    theta = np.zeros(n_features + 1, dtype=np.float64)

    for _ in range(n_iter):
        w_eff = sample_weights * w_robust
        XtW = X_aug.T * w_eff
        lhs = XtW @ X_aug + reg_mat
        rhs = XtW @ y
        theta = np.linalg.solve(lhs, rhs)
        residuals = y - (X_aug @ theta)
        w_robust = 1.0 / np.sqrt(1.0 + (residuals / huber_delta) ** 2)

    return theta[:-1], float(theta[-1]), w_robust


def _select_rbf_prototypes(Z_metric: np.ndarray, max_centers: int = MAX_RBF_CENTERS) -> np.ndarray:
    """Select diverse support prototypes across the gaze/pose manifold via farthest-point sampling."""
    n = Z_metric.shape[0]
    if n <= max_centers:
        return Z_metric.copy()

    rng = np.random.default_rng(42)
    selected = [int(rng.integers(0, n))]
    min_sq_dists = np.sum((Z_metric - Z_metric[selected[0]]) ** 2, axis=1)

    for _ in range(1, max_centers):
        threshold = np.percentile(min_sq_dists, 98.5)
        candidates = np.flatnonzero(min_sq_dists >= threshold)
        next_idx = int(candidates[0]) if len(candidates) > 0 else int(np.argmax(min_sq_dists))
        selected.append(next_idx)
        sq_d = np.sum((Z_metric - Z_metric[next_idx]) ** 2, axis=1)
        min_sq_dists = np.minimum(min_sq_dists, sq_d)

    return Z_metric[selected].copy()


def _multi_scale_rbf_kernel(
    A: np.ndarray,
    B: np.ndarray,
    gamma_broad: float,
    gamma_local: float,
) -> np.ndarray:
    """Compute dual-scale RBF kernel `0.55 * exp(-gamma_broad * d^2) + 0.45 * exp(-gamma_local * d^2)`."""
    a_sq = np.sum(A * A, axis=1, keepdims=True)
    b_sq = np.sum(B * B, axis=1, keepdims=True).T
    sq_dist = np.maximum(0.0, a_sq + b_sq - 2.0 * (A @ B.T))
    return 0.55 * np.exp(-gamma_broad * sq_dist) + 0.45 * np.exp(-gamma_local * sq_dist)


def _fit_irls_rbf_residual(
    Z_metric: np.ndarray,
    centers: np.ndarray,
    res_x: np.ndarray,
    res_y: np.ndarray,
    sample_weights: np.ndarray,
    gamma_broad: float,
    gamma_local: float,
    l2_reg: float = 1.8,
    huber_delta: float = 28.0,
    n_iter: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """Fit local multi-scale RBF dual weights `(dual_x, dual_y)` on Stage-1 residuals with IRLS."""
    K_nm = _multi_scale_rbf_kernel(Z_metric, centers, gamma_broad, gamma_local)
    K_mm = _multi_scale_rbf_kernel(centers, centers, gamma_broad, gamma_local)
    reg = l2_reg * K_mm + 1e-3 * np.eye(len(centers), dtype=np.float64)

    wx_rob = np.ones(len(res_x), dtype=np.float64)
    wy_rob = np.ones(len(res_y), dtype=np.float64)
    dual_x = np.zeros(len(centers), dtype=np.float64)
    dual_y = np.zeros(len(centers), dtype=np.float64)

    for _ in range(n_iter):
        eff_x = sample_weights * wx_rob
        eff_y = sample_weights * wy_rob
        KtWx = K_nm.T * eff_x
        KtWy = K_nm.T * eff_y
        dual_x = np.linalg.solve(KtWx @ K_nm + reg, KtWx @ res_x)
        dual_y = np.linalg.solve(KtWy @ K_nm + reg, KtWy @ res_y)
        err_x = res_x - (K_nm @ dual_x)
        err_y = res_y - (K_nm @ dual_y)
        wx_rob = 1.0 / np.sqrt(1.0 + (err_x / huber_delta) ** 2)
        wy_rob = 1.0 / np.sqrt(1.0 + (err_y / huber_delta) ** 2)

    return dual_x, dual_y


class StereoGazeModel:
    """Two-stage gaze predictor: Global IRLS Huber Polynomial + Multi-Scale Local RBF Kernel Warp."""

    def __init__(
        self,
        cam1_by_id: str,
        cam2_by_id: str,
        scaler_mean: np.ndarray,
        scaler_scale: np.ndarray,
        coef_x: np.ndarray,
        intercept_x: float,
        coef_y: np.ndarray,
        intercept_y: float,
        rbf_centers: np.ndarray,
        rbf_dual_x: np.ndarray,
        rbf_dual_y: np.ndarray,
        gamma_broad: float,
        gamma_local: float,
        rmse_x: float,
        rmse_y: float,
        monitor_connector: str,
        bounds_min_x: float,
        bounds_min_y: float,
        bounds_max_x: float,
        bounds_max_y: float,
        cv_rmse_x: float = 0.0,
        cv_rmse_y: float = 0.0,
        n_train_samples: int = 0,
        camera_mode: str = "stereo",
    ) -> None:
        self.cam1_by_id = cam1_by_id
        self.cam2_by_id = cam2_by_id
        self.scaler_mean = np.asarray(scaler_mean, dtype=np.float64)
        self.scaler_scale = np.asarray(scaler_scale, dtype=np.float64)
        self.coef_x = np.asarray(coef_x, dtype=np.float64)
        self.intercept_x = float(intercept_x)
        self.coef_y = np.asarray(coef_y, dtype=np.float64)
        self.intercept_y = float(intercept_y)
        self.rbf_centers = np.asarray(rbf_centers, dtype=np.float64)
        self.rbf_dual_x = np.asarray(rbf_dual_x, dtype=np.float64)
        self.rbf_dual_y = np.asarray(rbf_dual_y, dtype=np.float64)
        self.gamma_broad = float(gamma_broad)
        self.gamma_local = float(gamma_local)
        self.rmse_x = float(rmse_x)
        self.rmse_y = float(rmse_y)
        self.cv_rmse_x = float(cv_rmse_x)
        self.cv_rmse_y = float(cv_rmse_y)
        self.n_train_samples = int(n_train_samples)
        self.monitor_connector = monitor_connector
        self.bounds_min_x = float(bounds_min_x)
        self.bounds_min_y = float(bounds_min_y)
        self.bounds_max_x = float(bounds_max_x)
        self.bounds_max_y = float(bounds_max_y)
        self.camera_mode = camera_mode
        self.session_offset_x = 0.0
        self.session_offset_y = 0.0

    @classmethod
    def fit(
        cls,
        X: np.ndarray,
        targets_xy: np.ndarray,
        cam1_by_id: str,
        cam2_by_id: str,
        monitor: MonitorInfo,
        layout: DisplayLayout,
        groups: np.ndarray | None = None,
        sample_weights: np.ndarray | None = None,
        camera_mode: str = "stereo",
    ) -> StereoGazeModel:
        """Fit Stage-1 IRLS Huber Global Polynomial + Stage-2 Multi-Scale RBF Residual Field."""
        if X.ndim != 2 or X.shape[0] < 30:
            raise ValueError(f"Insufficient calibration samples: {X.shape}")
        if targets_xy.shape != (X.shape[0], 2):
            raise ValueError(f"Target shape mismatch: {targets_xy.shape} vs {X.shape}")

        n_samples = X.shape[0]
        weights = (
            np.ones(n_samples, dtype=np.float64)
            if sample_weights is None
            else np.asarray(sample_weights, dtype=np.float64)
        )

        scaler = StandardScaler().fit(X)
        clamped_scale = np.maximum(scaler.scale_, _MIN_FEATURE_SCALE)
        Z = np.clip((X - scaler.mean_) / clamped_scale, -5.0, 5.0)
        X_phi = expand_stereo_features(Z)

        # Stage 1: Fit robust IRLS Huber global polynomial trend
        X_wx = X_phi * _PRIOR_WEIGHTS_X
        X_wy = X_phi * _PRIOR_WEIGHTS_Y
        raw_coef_x, intercept_x, inlier_wx = _fit_irls_huber_ridge(
            X_wx, targets_xy[:, 0], weights, alpha=6.0, huber_delta=38.0
        )
        raw_coef_y, intercept_y, inlier_wy = _fit_irls_huber_ridge(
            X_wy, targets_xy[:, 1], weights, alpha=6.0, huber_delta=38.0
        )
        coef_x = raw_coef_x * _PRIOR_WEIGHTS_X
        coef_y = raw_coef_y * _PRIOR_WEIGHTS_Y

        base_pred_x = X_phi @ coef_x + intercept_x
        base_pred_y = X_phi @ coef_y + intercept_y
        res_x = targets_xy[:, 0] - base_pred_x
        res_y = targets_xy[:, 1] - base_pred_y

        # Stage 2: Fit Multi-Scale Local RBF Kernel on the non-linear manifold residuals
        Z_metric = Z * _RBF_METRIC_WEIGHTS
        inlier_mask = (inlier_wx > 0.35) & (inlier_wy > 0.35)
        Z_clean = Z_metric[inlier_mask] if np.sum(inlier_mask) >= 40 else Z_metric
        centers = _select_rbf_prototypes(Z_clean, max_centers=MAX_RBF_CENTERS)

        sub = centers[: min(160, len(centers))]
        sq_d = np.sum((sub[:, None, :] - sub[None, :, :]) ** 2, axis=2)
        med_sq = float(np.median(sq_d[sq_d > 0])) if np.any(sq_d > 0) else 10.0
        gamma_broad = 1.0 / max(2.5, 1.6 * med_sq)
        gamma_local = 1.0 / max(0.8, 0.38 * med_sq)

        combined_weights = weights * np.minimum(inlier_wx, inlier_wy)
        rbf_dual_x, rbf_dual_y = _fit_irls_rbf_residual(
            Z_metric=Z_metric,
            centers=centers,
            res_x=res_x,
            res_y=res_y,
            sample_weights=combined_weights,
            gamma_broad=gamma_broad,
            gamma_local=gamma_local,
            l2_reg=1.6,
            huber_delta=26.0,
        )

        K_all = _multi_scale_rbf_kernel(Z_metric, centers, gamma_broad, gamma_local)
        pred_x = base_pred_x + K_all @ rbf_dual_x
        pred_y = base_pred_y + K_all @ rbf_dual_y

        err_x_sq = (pred_x - targets_xy[:, 0]) ** 2
        err_y_sq = (pred_y - targets_xy[:, 1]) ** 2
        q90_x = np.percentile(err_x_sq, 90)
        q90_y = np.percentile(err_y_sq, 90)
        rmse_x = float(np.sqrt(np.mean(err_x_sq[err_x_sq <= q90_x])))
        rmse_y = float(np.sqrt(np.mean(err_y_sq[err_y_sq <= q90_y])))

        val_stride = 7
        val_mask = np.zeros(n_samples, dtype=bool)
        val_mask[::val_stride] = True
        tr_mask = ~val_mask
        if np.sum(tr_mask) >= 40 and np.sum(val_mask) >= 10:
            K_tr = K_all[tr_mask]
            K_val = K_all[val_mask]
            K_mm = _multi_scale_rbf_kernel(centers, centers, gamma_broad, gamma_local)
            reg = 2.0 * K_mm + 1e-3 * np.eye(len(centers))
            dx_cv = np.linalg.solve(
                (K_tr.T * combined_weights[tr_mask]) @ K_tr + reg,
                (K_tr.T * combined_weights[tr_mask]) @ res_x[tr_mask],
            )
            dy_cv = np.linalg.solve(
                (K_tr.T * combined_weights[tr_mask]) @ K_tr + reg,
                (K_tr.T * combined_weights[tr_mask]) @ res_y[tr_mask],
            )
            vx_err = (base_pred_x[val_mask] + K_val @ dx_cv - targets_xy[val_mask, 0]) ** 2
            vy_err = (base_pred_y[val_mask] + K_val @ dy_cv - targets_xy[val_mask, 1]) ** 2
            cv_rmse_x = float(np.sqrt(np.median(vx_err) * 1.4826))
            cv_rmse_y = float(np.sqrt(np.median(vy_err) * 1.4826))
        else:
            cv_rmse_x, cv_rmse_y = rmse_x, rmse_y

        return cls(
            cam1_by_id=cam1_by_id,
            cam2_by_id=cam2_by_id,
            scaler_mean=scaler.mean_,
            scaler_scale=clamped_scale,
            coef_x=coef_x,
            intercept_x=intercept_x,
            coef_y=coef_y,
            intercept_y=intercept_y,
            rbf_centers=centers,
            rbf_dual_x=rbf_dual_x,
            rbf_dual_y=rbf_dual_y,
            gamma_broad=gamma_broad,
            gamma_local=gamma_local,
            rmse_x=rmse_x,
            rmse_y=rmse_y,
            cv_rmse_x=cv_rmse_x,
            cv_rmse_y=cv_rmse_y,
            n_train_samples=n_samples,
            camera_mode=camera_mode,
            monitor_connector=monitor.connector,
            bounds_min_x=0.0,
            bounds_min_y=0.0,
            bounds_max_x=float(layout.total_width - 1),
            bounds_max_y=float(layout.total_height - 1),
        )

    def _predict_raw(self, feature_vec: np.ndarray) -> tuple[float, float]:
        fv = np.asarray(feature_vec, dtype=np.float64)
        z = np.clip((fv - self.scaler_mean) / self.scaler_scale, -5.0, 5.0)
        phi = expand_stereo_features(z)

        x = float(np.dot(self.coef_x, phi) + self.intercept_x)
        y = float(np.dot(self.coef_y, phi) + self.intercept_y)

        if len(self.rbf_centers) > 0:
            z_m = z * _RBF_METRIC_WEIGHTS
            diff = self.rbf_centers - z_m
            sq_dist = np.einsum("ij,ij->i", diff, diff)
            k_vec = 0.55 * np.exp(-self.gamma_broad * sq_dist) + 0.45 * np.exp(
                -self.gamma_local * sq_dist
            )
            x += float(np.dot(k_vec, self.rbf_dual_x))
            y += float(np.dot(k_vec, self.rbf_dual_y))
        return x, y

    def recenter_to(
        self,
        target_x: float,
        target_y: float,
        feature_vec: np.ndarray,
        max_offset_px: float = 300.0,
    ) -> tuple[float, float]:
        """Snap session offset `(dx, dy)` so `feature_vec` predicts `(target_x, target_y)`."""
        raw_x, raw_y = self._predict_raw(feature_vec)
        dx = max(-max_offset_px, min(max_offset_px, float(target_x) - raw_x))
        dy = max(-max_offset_px, min(max_offset_px, float(target_y) - raw_y))
        self.session_offset_x = dx
        self.session_offset_y = dy
        return dx, dy

    def predict(self, feature_vec: np.ndarray) -> tuple[float, float]:
        """Predict clamped global desktop `(x, y)` from a 20-D stereo feature vector (<0.15ms)."""
        x, y = self._predict_raw(feature_vec)
        x += self.session_offset_x
        y += self.session_offset_y
        x = max(self.bounds_min_x, min(self.bounds_max_x, x))
        y = max(self.bounds_min_y, min(self.bounds_max_y, y))
        return x, y

    def save(self, path: Path = DEFAULT_CALIB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 5,
            "feature_dim": int(len(self.scaler_mean)),
            "camera_mode": self.camera_mode,
            "expanded_dim": int(len(self.coef_x)),
            "cam1_by_id": self.cam1_by_id,
            "cam2_by_id": self.cam2_by_id,
            "monitor_connector": self.monitor_connector,
            "n_train_samples": self.n_train_samples,
            "bounds": {
                "min_x": self.bounds_min_x,
                "min_y": self.bounds_min_y,
                "max_x": self.bounds_max_x,
                "max_y": self.bounds_max_y,
            },
            "scaler_mean": self.scaler_mean.tolist(),
            "scaler_scale": self.scaler_scale.tolist(),
            "coef_x": self.coef_x.tolist(),
            "intercept_x": self.intercept_x,
            "coef_y": self.coef_y.tolist(),
            "intercept_y": self.intercept_y,
            "gamma_broad": self.gamma_broad,
            "gamma_local": self.gamma_local,
            "rbf_centers": self.rbf_centers.tolist(),
            "rbf_dual_x": self.rbf_dual_x.tolist(),
            "rbf_dual_y": self.rbf_dual_y.tolist(),
            "rmse_x": self.rmse_x,
            "rmse_y": self.rmse_y,
            "cv_rmse_x": self.cv_rmse_x,
            "cv_rmse_y": self.cv_rmse_y,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(path)

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_CALIB_PATH,
        strip_pose_translation: bool = True,
    ) -> StereoGazeModel:
        data = json.loads(path.read_text())
        if int(data.get("version", 1)) < 5 or len(data.get("coef_x", [])) != EXPANDED_FEATURE_DIM:
            raise ValueError(
                f"Calibration file {path} uses older schema; please re-run `calibrate`."
            )
        bounds = data["bounds"]
        coef_x = np.asarray(data["coef_x"], dtype=np.float64)
        coef_y = np.asarray(data["coef_y"], dtype=np.float64)
        rbf_centers = np.asarray(data.get("rbf_centers", []), dtype=np.float64)
        if strip_pose_translation:
            coef_x[list(_POSE_TRANSLATION_EXPANDED_COLS)] = 0.0
            coef_y[list(_POSE_TRANSLATION_EXPANDED_COLS)] = 0.0
            if rbf_centers.ndim == 2 and rbf_centers.shape[1] == STEREO_FEATURE_DIM:
                rbf_centers[:, list(_POSE_TRANSLATION_RAW_COLS)] = 0.0
        return cls(
            cam1_by_id=data["cam1_by_id"],
            cam2_by_id=data["cam2_by_id"],
            scaler_mean=np.asarray(data["scaler_mean"], dtype=np.float64),
            scaler_scale=np.asarray(data["scaler_scale"], dtype=np.float64),
            coef_x=coef_x,
            intercept_x=float(data["intercept_x"]),
            coef_y=coef_y,
            intercept_y=float(data["intercept_y"]),
            rbf_centers=rbf_centers,
            rbf_dual_x=np.asarray(data.get("rbf_dual_x", []), dtype=np.float64),
            rbf_dual_y=np.asarray(data.get("rbf_dual_y", []), dtype=np.float64),
            gamma_broad=float(data.get("gamma_broad", 0.1)),
            gamma_local=float(data.get("gamma_local", 0.4)),
            rmse_x=float(data["rmse_x"]),
            rmse_y=float(data["rmse_y"]),
            cv_rmse_x=float(data.get("cv_rmse_x", 0.0)),
            cv_rmse_y=float(data.get("cv_rmse_y", 0.0)),
            n_train_samples=int(data.get("n_train_samples", 0)),
            camera_mode=str(data.get("camera_mode", "stereo")),
            monitor_connector=data["monitor_connector"],
            bounds_min_x=float(bounds["min_x"]),
            bounds_min_y=float(bounds["min_y"]),
            bounds_max_x=float(bounds["max_x"]),
            bounds_max_y=float(bounds["max_y"]),
        )


def load_gaze_model(
    path: Path = DEFAULT_CALIB_PATH,
    layout: DisplayLayout | None = None,
    strip_pose_translation: bool = True,
) -> StereoGazeModel | object:
    """Load either a geometric (``geo-1``) or legacy ``StereoGazeModel`` calibration file.

    When ``layout`` is provided for a geometric model, monitors are re-placed automatically so
    plugged/unplugged or rearranged screens work without recalibrating.
    """
    data = json.loads(Path(path).read_text())
    if str(data.get("version") or "").startswith("geo-"):
        from keyless.vision.geo_gaze import GeoGazeModel, monitor_specs_from_layout

        model = GeoGazeModel.load(Path(path))
        if layout is not None:
            model.with_layout(monitor_specs_from_layout(layout))
        return model
    return StereoGazeModel.load(Path(path), strip_pose_translation=strip_pose_translation)


def _fit_gaze_model(
    X: np.ndarray,
    targets_xy: np.ndarray,
    config: AppConfig,
    target_mon: MonitorInfo,
    layout: DisplayLayout,
    groups: np.ndarray,
    sample_weights: np.ndarray,
) -> StereoGazeModel | object:
    if getattr(config, "gaze_model", "geo") == "geo":
        from keyless.vision.geo_gaze import fit_from_dataset

        return fit_from_dataset(
            X=X,
            Y=targets_xy,
            layout=layout,
            ref_connector=target_mon.connector,
            sample_weights=sample_weights,
            cam1_by_id=config.cam1_by_id,
            cam2_by_id=config.cam2_by_id,
        )
    return StereoGazeModel.fit(
        X=X,
        targets_xy=targets_xy,
        cam1_by_id=config.cam1_by_id,
        cam2_by_id=config.cam2_by_id,
        monitor=target_mon,
        layout=layout,
        groups=groups,
        sample_weights=sample_weights,
        camera_mode=config.camera_mode,
    )


def _save_replay_dataset(
    dataset_path: Path,
    X: np.ndarray,
    Y: np.ndarray,
    groups: np.ndarray,
    weights: np.ndarray,
    cam1_by_id: str,
    cam2_by_id: str,
) -> None:
    """Persist training dataset to disk so pursuit training accumulates across sessions."""
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    if len(X) > MAX_REPLAY_SAMPLES:
        keep_idx = np.argsort(weights * np.linspace(0.5, 1.5, len(X)))[-MAX_REPLAY_SAMPLES:]
        keep_idx.sort()
        X = X[keep_idx]
        Y = Y[keep_idx]
        groups = groups[keep_idx]
        weights = weights[keep_idx]

    np.savez_compressed(
        dataset_path,
        X=X.astype(np.float64),
        Y=Y.astype(np.float64),
        groups=groups.astype(np.intp),
        weights=weights.astype(np.float64),
        meta=np.array([cam1_by_id, cam2_by_id], dtype=object),
    )


def _load_replay_dataset(
    dataset_path: Path,
    cam1_by_id: str,
    cam2_by_id: str,
) -> tuple[list[np.ndarray], list[tuple[float, float]], list[int], list[float]] | None:
    """Load persisted training samples if matching camera IDs exist on disk."""
    if not dataset_path.exists():
        return None
    try:
        with np.load(dataset_path, allow_pickle=True) as data:
            meta = data["meta"]
            if len(meta) < 2 or str(meta[0]) != cam1_by_id or str(meta[1]) != cam2_by_id:
                return None
            X = data["X"]
            Y = data["Y"]
            groups = data["groups"]
            weights = data["weights"]
            if X.ndim != 2 or X.shape[1] != STEREO_FEATURE_DIM or len(X) < 30:
                return None
            feats = [X[i] for i in range(len(X))]
            targs = [(float(Y[i, 0]), float(Y[i, 1])) for i in range(len(Y))]
            grps = [int(groups[i]) for i in range(len(groups))]
            wgts = [float(max(0.5, weights[i] * 0.85)) for i in range(len(weights))]
            return feats, targs, grps, wgts
    except Exception:
        return None


def append_harvested_calibration_samples(
    calib_path: Path,
    samples: list[tuple[np.ndarray, tuple[float, float]]],
    cam1_by_id: str,
    cam2_by_id: str,
) -> int:
    """Append passively harvested `(20-D feature -> clicked center)` pairs to `calib_dataset.npz`."""
    if not samples:
        return 0
    dataset_path = _default_dataset_path(calib_path)
    existing = _load_replay_dataset(dataset_path, cam1_by_id, cam2_by_id)
    if existing is not None:
        feats, targs, grps, wgts = existing
    else:
        feats, targs, grps, wgts = [], [], [], []

    next_group = (max(grps) + 1) if grps else 200
    added = 0
    for feat, (tx, ty) in samples:
        arr = np.asarray(feat, dtype=np.float64).reshape(-1)
        if arr.shape != (STEREO_FEATURE_DIM,):
            continue
        feats.append(arr)
        targs.append((float(tx), float(ty)))
        grps.append(next_group)
        wgts.append(2.2)
        added += 1

    if added > 0:
        _save_replay_dataset(
            dataset_path=dataset_path,
            X=np.vstack(feats),
            Y=np.asarray(targs, dtype=np.float64),
            groups=np.asarray(grps, dtype=np.intp),
            weights=np.asarray(wgts, dtype=np.float64),
            cam1_by_id=cam1_by_id,
            cam2_by_id=cam2_by_id,
        )
    return added


def _align_and_filter_pursuit_stroke(
    stroke_times: list[float],
    stroke_feats: list[np.ndarray],
    cursor_history: list[tuple[float, int, int, float]],
    mon_x: int,
    mon_y: int,
) -> tuple[list[np.ndarray], list[tuple[float, float]]]:
    """Cross-correlate iris velocity against mouse velocity to find optimal lag and drop saccades."""
    if len(stroke_feats) < 10 or len(cursor_history) < 5:
        return [], []

    hist_t = np.array([h[0] for h in cursor_history], dtype=np.float64)
    hist_x = np.array([h[1] for h in cursor_history], dtype=np.float64)
    hist_y = np.array([h[2] for h in cursor_history], dtype=np.float64)

    f_mat = np.vstack(stroke_feats)
    t_arr = np.asarray(stroke_times, dtype=np.float64)

    iris_x = 0.25 * (f_mat[:, 0] + f_mat[:, 2] + f_mat[:, 10] + f_mat[:, 12])
    iris_y = 0.25 * (f_mat[:, 1] + f_mat[:, 3] + f_mat[:, 11] + f_mat[:, 13])
    d_iris_x = np.gradient(iris_x)
    d_iris_y = np.gradient(iris_y)

    best_lag = 0.055
    best_score = -1.0
    if len(stroke_feats) >= 22:
        for cand_lag in np.linspace(-0.04, 0.13, 12):
            cx = np.interp(t_arr - cand_lag, hist_t, hist_x)
            cy = np.interp(t_arr - cand_lag, hist_t, hist_y)
            dcx = np.gradient(cx)
            dcy = np.gradient(cy)
            if np.std(dcx) > 0.3 and np.std(d_iris_x) > 1e-4:
                corr_x = abs(float(np.corrcoef(d_iris_x, dcx)[0, 1]))
            else:
                corr_x = 0.0
            if np.std(dcy) > 0.3 and np.std(d_iris_y) > 1e-4:
                corr_y = abs(float(np.corrcoef(d_iris_y, dcy)[0, 1]))
            else:
                corr_y = 0.0
            score = corr_x + corr_y
            if score > best_score:
                best_score = score
                best_lag = float(cand_lag)

    aligned_x = np.interp(t_arr - best_lag, hist_t, hist_x) + mon_x
    aligned_y = np.interp(t_arr - best_lag, hist_t, hist_y) + mon_y

    iris_speed = np.hypot(d_iris_x, d_iris_y)
    cutoff = float(np.percentile(iris_speed, 88)) + 1e-4
    clean_feats: list[np.ndarray] = []
    clean_targets: list[tuple[float, float]] = []
    for i in range(len(stroke_feats)):
        if iris_speed[i] <= cutoff:
            clean_feats.append(stroke_feats[i])
            clean_targets.append((float(aligned_x[i]), float(aligned_y[i])))

    return clean_feats, clean_targets


def _eval_continuous_sweep_path(phase_idx: int, u: float) -> tuple[float, float]:
    """Compute normalized `(rel_x, rel_y)` in `[0.06..0.94]` for automated smooth-pursuit sweeps."""
    u = max(0.0, min(1.0, u))
    margin_x, margin_y = 0.06, 0.08
    span_x = 1.0 - 2.0 * margin_x
    span_y = 1.0 - 2.0 * margin_y

    if phase_idx == 1:
        if u < 0.40:
            p = u / 0.40 * 4.0
            if p < 1.0:
                rx, ry = margin_x + span_x * p, margin_y
            elif p < 2.0:
                rx, ry = 1.0 - margin_x, margin_y + span_y * (p - 1.0)
            elif p < 3.0:
                rx, ry = 1.0 - margin_x - span_x * (p - 2.0), 1.0 - margin_y
            else:
                rx, ry = margin_x, 1.0 - margin_y - span_y * (p - 3.0)
            return rx, ry
        t = (u - 0.40) / 0.60 * (2.0 * math.pi)
        rx = 0.50 + 0.43 * math.sin(2.0 * t)
        ry = 0.50 + 0.41 * math.sin(3.0 * t)
        return rx, ry

    t = u * (2.0 * math.pi)
    rx = 0.50 + 0.40 * math.cos(t)
    ry = 0.50 + 0.38 * math.sin(2.0 * t)
    return rx, ry


def _ui_canvas_spec(target_mon: MonitorInfo) -> tuple[int, int, float]:
    """Use half-resolution canvas on >2K displays so Qt5 CPU blit runs at 150+ FPS."""
    if target_mon.width > 2000:
        return target_mon.width // 2, target_mon.height // 2, 0.5
    return target_mon.width, target_mon.height, 1.0


def _run_automated_pursuit_sweep(
    win_name: str,
    stereo: StereoCapture,
    target_mon: MonitorInfo,
    blink_threshold: float,
    sweep_idx: int,
    duration_sec: float,
    prompt_title: str,
    prompt_sub: str,
    lead_in_sec: float = 2.5,
) -> tuple[list[np.ndarray], list[tuple[float, float]]]:
    """Run an automated gliding target that collects continuous smooth-pursuit samples."""
    feats_raw: list[np.ndarray] = []
    times_raw: list[float] = []
    cursor_hist: list[tuple[float, int, int, float]] = []
    trail: deque[tuple[int, int]] = deque(maxlen=45)

    ui_w, ui_h, ui_scale = _ui_canvas_spec(target_mon)
    canvas = np.zeros((ui_h, ui_w, 3), dtype=np.uint8)

    start_t = time.monotonic()
    last_seq1 = -1
    last_seq2 = -1

    while True:
        pair = stereo.read_pair()
        now = time.monotonic()
        elapsed = now - start_t
        if elapsed >= lead_in_sec + duration_sec:
            break

        u = 0.0 if elapsed < lead_in_sec else (elapsed - lead_in_sec) / duration_sec
        rx, ry = _eval_continuous_sweep_path(sweep_idx, u)
        lx = int(round(rx * target_mon.width))
        ly = int(round(ry * target_mon.height))
        ux = int(round(lx * ui_scale))
        uy = int(round(ly * ui_scale))
        cursor_hist.append((now, lx, ly, 200.0))
        trail.append((ux, uy))

        canvas.fill(0)
        if len(trail) >= 2:
            pts = np.array(trail, dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(canvas, [pts], False, (80, 90, 120), 1 if ui_scale < 1.0 else 2, cv2.LINE_AA)

        is_settling = elapsed < lead_in_sec
        dot_col = (0, 200, 255) if is_settling else (80, 255, 120)
        r_outer = max(8, int(round(20 * ui_scale)))
        r_inner = max(5, int(round(10 * ui_scale)))
        cv2.circle(canvas, (ux, uy), r_outer, dot_col, 2, cv2.LINE_AA)
        cv2.circle(canvas, (ux, uy), r_inner, dot_col, -1, cv2.LINE_AA)
        cv2.circle(canvas, (ux, uy), 2, (10, 10, 10), -1, cv2.LINE_AA)
        cv2.circle(canvas, (ux, uy), 1, (255, 255, 255), -1, cv2.LINE_AA)

        if pair is not None and (pair.cam1.seq != last_seq1 or pair.cam2.seq != last_seq2):
            last_seq1 = pair.cam1.seq
            last_seq2 = pair.cam2.seq
            o1 = pair.cam1.observation
            o2 = pair.cam2.observation
            if (
                not is_settling
                and o1 is not None
                and o2 is not None
                and o1.max_blink < blink_threshold
                and o2.max_blink < blink_threshold
            ):
                feat, both_ok = build_stereo_feature_vector(o1, o2)
                if both_ok and feat.shape == (STEREO_FEATURE_DIM,):
                    feats_raw.append(feat)
                    times_raw.append(now)

        prog_w = int(round(u * ui_w))
        cv2.rectangle(canvas, (0, 0), (prog_w, max(3, int(round(6 * ui_scale)))), (80, 255, 120), -1)

        if is_settling:
            msg = f"{prompt_title} - Starting in {max(0.0, lead_in_sec - elapsed):.1f}s (Lock eyes on dot)"
            f1 = 0.78 * ui_scale
            f2 = 0.65 * ui_scale
            (mw, _), _ = cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX, f1, 1 if ui_scale < 1.0 else 2)
            cv2.putText(
                canvas,
                msg,
                (max(15, (ui_w - mw) // 2), int(round(45 * ui_scale))),
                cv2.FONT_HERSHEY_SIMPLEX,
                f1,
                (0, 215, 255),
                1 if ui_scale < 1.0 else 2,
                cv2.LINE_AA,
            )
            (sw, _), _ = cv2.getTextSize(prompt_sub, cv2.FONT_HERSHEY_SIMPLEX, f2, 1 if ui_scale < 1.0 else 2)
            cv2.putText(
                canvas,
                prompt_sub,
                (max(15, (ui_w - sw) // 2), int(round(82 * ui_scale))),
                cv2.FONT_HERSHEY_SIMPLEX,
                f2,
                (200, 200, 200),
                1 if ui_scale < 1.0 else 2,
                cv2.LINE_AA,
            )

        cv2.imshow(win_name, canvas)
        key = cv2.waitKey(1) & 0xFF
        if key == 27:
            raise KeyboardInterrupt("Calibration cancelled by user.")

    return _align_and_filter_pursuit_stroke(
        stroke_times=times_raw,
        stroke_feats=feats_raw,
        cursor_history=cursor_hist,
        mon_x=target_mon.x,
        mon_y=target_mon.y,
    )


@dataclass
class _RefinementBatch:
    """Stores a single interactive cursor-pursuit stroke or spot-fix burst for undo support."""

    features: list[np.ndarray]
    targets: list[tuple[float, float]]
    groups: list[int]
    weights: list[float]


def _preview_and_refine_calibration(
    win_name: str,
    stereo: StereoCapture,
    base_features: list[np.ndarray],
    base_targets: list[tuple[float, float]],
    base_groups: list[int],
    base_weights: list[float],
    initial_model: StereoGazeModel,
    config: AppConfig,
    target_mon: MonitorInfo,
    layout: DisplayLayout,
) -> tuple[
    bool,
    StereoGazeModel,
    list[np.ndarray],
    list[tuple[float, float]],
    list[int],
    list[float],
]:
    """Interactive verification & cursor-pursuit manifold trainer."""
    model = initial_model
    flt = OneEuroFilter2D(min_cutoff=0.85, beta=0.003)
    gx, gy = target_mon.x + target_mon.width * 0.5, target_mon.y + target_mon.height * 0.5
    raw_gx, raw_gy = gx, gy
    last_seq1 = -1
    last_seq2 = -1

    ui_w, ui_h, ui_scale = _ui_canvas_spec(target_mon)
    canvas = np.zeros((ui_h, ui_w, 3), dtype=np.uint8)
    thick = 1 if ui_scale < 1.0 else 2

    mouse_state = {
        "x": target_mon.width // 2,
        "y": target_mon.height // 2,
        "left_down": False,
        "spot_trigger": False,
    }
    cursor_history: list[tuple[float, int, int, float]] = []
    refinement_batches: list[_RefinementBatch] = []

    active_stroke_feats: list[np.ndarray] = []
    active_stroke_times: list[float] = []
    active_stroke_trail: list[tuple[int, int]] = []
    spot_burst_remaining = 0
    next_group_id = (max(base_groups) + 1) if base_groups else 100
    status_msg = (
        "Hold LEFT-CLICK & move mouse while looking at the purple cursor to paint local RBF training data!"
    )
    status_until = time.monotonic() + 8.0

    fit_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rbf-refit")
    pending_fit: Future[StereoGazeModel] | None = None
    pending_fit_msg: str = ""

    def _on_mouse(event: int, x: int, y: int, _flags: int, _userdata: object) -> None:
        now_t = time.monotonic()
        mon_x_pos = int(round(x / ui_scale))
        mon_y_pos = int(round(y / ui_scale))
        prev_t, prev_x, prev_y, _ = (
            cursor_history[-1] if cursor_history else (now_t - 0.016, mon_x_pos, mon_y_pos, 0.0)
        )
        dt = max(1e-3, now_t - prev_t)
        speed = math.hypot(mon_x_pos - prev_x, mon_y_pos - prev_y) / dt
        cursor_history.append((now_t, mon_x_pos, mon_y_pos, speed))
        if len(cursor_history) > 1200:
            del cursor_history[:600]
        mouse_state["x"] = mon_x_pos
        mouse_state["y"] = mon_y_pos
        if event == cv2.EVENT_LBUTTONDOWN:
            mouse_state["left_down"] = True
        elif event == cv2.EVENT_LBUTTONUP:
            mouse_state["left_down"] = False
        elif event == cv2.EVENT_RBUTTONDOWN:
            mouse_state["spot_trigger"] = True

    cv2.setMouseCallback(win_name, _on_mouse)

    def _gather_all_data() -> tuple[
        list[np.ndarray], list[tuple[float, float]], list[int], list[float]
    ]:
        feats = list(base_features)
        targs = list(base_targets)
        grps = list(base_groups)
        wgts = list(base_weights)
        for b in refinement_batches:
            feats.extend(b.features)
            targs.extend(b.targets)
            grps.extend(b.groups)
            wgts.extend(b.weights)
        return feats, targs, grps, wgts

    def _fit_snapshot(
        feats: list[np.ndarray],
        targs: list[tuple[float, float]],
        grps: list[int],
        wgts: list[float],
    ) -> StereoGazeModel | object:
        return _fit_gaze_model(
            X=np.vstack(feats),
            targets_xy=np.asarray(targs, dtype=np.float64),
            config=config,
            target_mon=target_mon,
            layout=layout,
            groups=np.asarray(grps, dtype=np.intp),
            sample_weights=np.asarray(wgts, dtype=np.float64),
        )

    def _schedule_refit(done_message: str) -> None:
        nonlocal pending_fit, pending_fit_msg
        feats, targs, grps, wgts = _gather_all_data()
        pending_fit_msg = done_message
        pending_fit = fit_executor.submit(_fit_snapshot, feats, targs, grps, wgts)

    prev_feat: np.ndarray | None = None
    was_left_down = False

    try:
        while True:
            if pending_fit is not None and pending_fit.done():
                model = pending_fit.result()
                pending_fit = None
                extra_tag = (
                    f"Monitors={len(model.monitors)}"
                    if hasattr(model, "monitors")
                    else f"RBF={len(model.rbf_centers)}"
                )
                status_msg = f"{pending_fit_msg} (Samples={model.n_train_samples}, {extra_tag})"
                status_until = time.monotonic() + 3.5

            pair = stereo.read_pair()
            now = time.monotonic()
            mx, my = mouse_state["x"], mouse_state["y"]

            if mouse_state["spot_trigger"]:
                mouse_state["spot_trigger"] = False
                spot_burst_remaining = 20
                prev_feat = None
                active_stroke_feats.clear()
                active_stroke_times.clear()

            if pair is not None and (pair.cam1.seq != last_seq1 or pair.cam2.seq != last_seq2):
                last_seq1 = pair.cam1.seq
                last_seq2 = pair.cam2.seq
                o1 = pair.cam1.observation
                o2 = pair.cam2.observation
                if (
                    o1 is not None
                    and o2 is not None
                    and o1.max_blink < config.blink_threshold
                    and o2.max_blink < config.blink_threshold
                ):
                    feat, both_ok = build_stereo_feature_vector(o1, o2)
                    if both_ok and feat.shape == (STEREO_FEATURE_DIM,):
                        raw_gx, raw_gy = model.predict(feat)
                        gx, gy = flt(raw_gx, raw_gy, now)

                        eye_step = 0.0 if prev_feat is None else _iris_step_distance(prev_feat, feat)
                        prev_feat = feat

                        if mouse_state["left_down"] and spot_burst_remaining == 0:
                            cur_speed = cursor_history[-1][3] if cursor_history else 0.0
                            if cur_speed < 950.0 and eye_step < 0.120:
                                active_stroke_feats.append(feat)
                                active_stroke_times.append(now)
                                active_stroke_trail.append(
                                    (int(round(mx * ui_scale)), int(round(my * ui_scale)))
                                )

                        elif spot_burst_remaining > 0 and eye_step < 0.095:
                            active_stroke_feats.append(feat)
                            spot_burst_remaining -= 1
                            if spot_burst_remaining == 0 and len(active_stroke_feats) >= 6:
                                cleaned = _trim_dot_outliers(active_stroke_feats, keep_ratio=0.85)
                                t_xy = (float(target_mon.x + mx), float(target_mon.y + my))
                                refinement_batches.append(
                                    _RefinementBatch(
                                        features=cleaned,
                                        targets=[t_xy] * len(cleaned),
                                        groups=[next_group_id] * len(cleaned),
                                        weights=[3.5] * len(cleaned),
                                    )
                                )
                                next_group_id += 1
                                active_stroke_feats.clear()
                                active_stroke_times.clear()
                                _schedule_refit(f"Spot-fix applied ({len(cleaned)} frames)!")

            if was_left_down and not mouse_state["left_down"]:
                aligned_feats, aligned_targets = _align_and_filter_pursuit_stroke(
                    stroke_times=active_stroke_times,
                    stroke_feats=active_stroke_feats,
                    cursor_history=cursor_history,
                    mon_x=target_mon.x,
                    mon_y=target_mon.y,
                )
                if len(aligned_feats) >= 10:
                    refinement_batches.append(
                        _RefinementBatch(
                            features=aligned_feats,
                            targets=aligned_targets,
                            groups=[next_group_id] * len(aligned_feats),
                            weights=[2.6] * len(aligned_feats),
                        )
                    )
                    next_group_id += 1
                    _schedule_refit(f"Added {len(aligned_feats)} lag-aligned pursuit samples!")
                else:
                    status_msg = "Stroke too short (<10 frames) - hold LEFT-CLICK & move smoothly."
                    status_until = now + 2.5
                active_stroke_feats.clear()
                active_stroke_times.clear()
                active_stroke_trail.clear()

            was_left_down = bool(mouse_state["left_down"])

            canvas.fill(0)
            for rx, ry in [
                (0.06, 0.08), (0.50, 0.08), (0.94, 0.08),
                (0.06, 0.50), (0.50, 0.50), (0.94, 0.50),
                (0.06, 0.92), (0.50, 0.92), (0.94, 0.92),
            ]:
                tx = int(round(rx * ui_w))
                ty = int(round(ry * ui_h))
                cv2.circle(canvas, (tx, ty), max(8, int(round(18 * ui_scale))), (60, 60, 80), thick, cv2.LINE_AA)

            if len(active_stroke_trail) >= 2:
                pts = np.array(active_stroke_trail, dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(canvas, [pts], False, (255, 220, 60), thick, cv2.LINE_AA)

            umx, umy = int(round(mx * ui_scale)), int(round(my * ui_scale))
            is_capturing_cursor = mouse_state["left_down"] or spot_burst_remaining > 0
            cur_col = (0, 215, 255) if is_capturing_cursor else (215, 110, 255)
            r_cur = int(round((18 if is_capturing_cursor else 13) * ui_scale))
            cv2.circle(canvas, (umx, umy), max(5, r_cur), cur_col, thick, cv2.LINE_AA)
            cv2.circle(canvas, (umx, umy), 2, (255, 255, 255), -1, cv2.LINE_AA)

            rlx = int(round((raw_gx - target_mon.x) * ui_scale))
            rly = int(round((raw_gy - target_mon.y) * ui_scale))
            if 0 <= rlx < ui_w and 0 <= rly < ui_h:
                cv2.circle(canvas, (rlx, rly), max(3, int(round(7 * ui_scale))), (255, 200, 0), 1, cv2.LINE_AA)

            lx = int(round((gx - target_mon.x) * ui_scale))
            ly = int(round((gy - target_mon.y) * ui_scale))
            if 0 <= lx < ui_w and 0 <= ly < ui_h:
                cv2.circle(canvas, (lx, ly), max(10, int(round(24 * ui_scale))), (80, 255, 120), thick + 1, cv2.LINE_AA)
                cv2.circle(canvas, (lx, ly), 2, (80, 255, 120), -1, cv2.LINE_AA)

            refit_tag = " [Fitting...]" if pending_fit is not None else ""
            if hasattr(model, "monitors"):
                banner1 = (
                    f"GEOMETRIC 3D GAZE | RMSE=({model.rmse_x:.0f}, {model.rmse_y:.0f})px | "
                    f"Monitors={len(model.monitors)} | Samples={model.n_train_samples}{refit_tag}"
                )
            else:
                banner1 = (
                    f"HYBRID HUBER + RBF MANIFOLD | Train RMSE=({model.rmse_x:.0f}, {model.rmse_y:.0f})px | "
                    f"CV RMSE=({model.cv_rmse_x:.0f}, {model.cv_rmse_y:.0f})px | "
                    f"Samples={model.n_train_samples} (RBF={len(model.rbf_centers)}){refit_tag}"
                )
            banner2 = (
                "Hold LEFT-CLICK & move mouse (look at cursor) to train | "
                "F / RIGHT-CLICK = Spot-Fix | U = Undo | SPACE = Save & Exit | R = Full Redo"
            )
            banner3 = status_msg if now < status_until else ""

            for by, btext, bscale, bcolor in [
                (int(round(48 * ui_scale)), banner1, 0.72 * ui_scale, (240, 240, 240)),
                (int(round(84 * ui_scale)), banner2, 0.64 * ui_scale, (130, 230, 255)),
                (int(round(118 * ui_scale)), banner3, 0.66 * ui_scale, (100, 255, 160)),
            ]:
                if not btext:
                    continue
                (bw, _), _ = cv2.getTextSize(btext, cv2.FONT_HERSHEY_SIMPLEX, bscale, thick)
                bx = max(15, (ui_w - bw) // 2)
                cv2.putText(
                    canvas, btext, (bx, by), cv2.FONT_HERSHEY_SIMPLEX, bscale, bcolor, thick, cv2.LINE_AA
                )

            cv2.imshow(win_name, canvas)
            key = cv2.waitKey(1) & 0xFF
            if key in (13, 32):
                if pending_fit is not None:
                    model = pending_fit.result()
                    pending_fit = None
                feats, targs, grps, wgts = _gather_all_data()
                return True, model, feats, targs, grps, wgts
            if key == ord("f"):
                spot_burst_remaining = 20
                prev_feat = None
                active_stroke_feats.clear()
                active_stroke_times.clear()
            if key == ord("u") and refinement_batches:
                refinement_batches.pop()
                _schedule_refit(f"Undid last stroke ({len(refinement_batches)} active)")
            if key == ord("r"):
                if pending_fit is not None:
                    model = pending_fit.result()
                    pending_fit = None
                feats, targs, grps, wgts = _gather_all_data()
                return False, model, feats, targs, grps, wgts
            if key == 27:
                raise KeyboardInterrupt("Calibration cancelled by user.")
    finally:
        fit_executor.shutdown(wait=False, cancel_futures=True)


def _draw_anchored_label(
    canvas: np.ndarray,
    dot_x: int,
    dot_y: int,
    title: str,
    subtitle: str,
    title_color: tuple[int, int, int],
    dim_for_capture: bool = False,
    ui_scale: float = 1.0,
) -> None:
    """Draw calibration instructions; dims heavily during active capture so eyes stay on pinhole."""
    h, w = canvas.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    thick = 1 if ui_scale < 1.0 else 2
    title_scale = 0.80 * ui_scale
    sub_scale = 0.65 * ui_scale

    (tw, th), _ = cv2.getTextSize(title, font, title_scale, thick)
    (sw, sh), _ = cv2.getTextSize(subtitle, font, sub_scale, thick)

    place_below = dot_y < int(round(190 * ui_scale))
    pad_y = int(round(85 * ui_scale))
    off_y = int(round(95 * ui_scale))
    title_y = (dot_y + pad_y + th) if place_below else (dot_y - off_y)
    sub_y = title_y + sh + int(round(16 * ui_scale))

    margin_x = max(15, int(round(30 * ui_scale)))
    title_x = max(margin_x, min(w - tw - margin_x, dot_x - tw // 2))
    sub_x = max(margin_x, min(w - sw - margin_x, dot_x - sw // 2))

    t_col = tuple(int(c * 0.25) for c in title_color) if dim_for_capture else title_color
    s_col = (55, 55, 55) if dim_for_capture else (190, 190, 190)

    cv2.putText(canvas, title, (title_x, title_y), font, title_scale, t_col, thick, cv2.LINE_AA)
    cv2.putText(canvas, subtitle, (sub_x, sub_y), font, sub_scale, s_col, thick, cv2.LINE_AA)


def run_calibration_ui(
    config: AppConfig,
    calib_path: Path = DEFAULT_CALIB_PATH,
    samples_per_point: int = 22,
    settle_sec: float = 2.0,
    pose_settle_sec: float = 3.8,
    sweep1_duration_sec: float = 52.0,
    sweep2_duration_sec: float = 44.0,
    connector: str | None = None,
    legacy_cv: bool = False,
) -> StereoGazeModel:
    """Launch interactive gaze calibration (translucent desktop overlay by default)."""
    config.camera_mode, config.cam1_by_id, config.cam2_by_id = resolve_camera_config(
        config.camera_mode, config.cam1_by_id, config.cam2_by_id
    )
    layout = get_display_layout()
    target_mon = layout.primary
    if connector is not None:
        matches = [m for m in layout.monitors if m.connector == connector]
        if not matches:
            avail = ", ".join(m.connector for m in layout.monitors)
            raise ValueError(f"Monitor '{connector}' not found. Available: {avail}")
        target_mon = matches[0]

    if not legacy_cv:
        try:
            from keyless.ui.calibration_overlay import run_translucent_calibration_overlay

            return run_translucent_calibration_overlay(
                config=config,
                calib_path=calib_path,
                target_mon=target_mon,
                layout=layout,
                samples_per_point=samples_per_point,
                settle_sec=settle_sec,
                pose_settle_sec=pose_settle_sec,
                sweep1_duration_sec=sweep1_duration_sec,
                sweep2_duration_sec=sweep2_duration_sec,
            )
        except ImportError:
            pass

    dataset_path = _default_dataset_path(calib_path)
    saved_replay = _load_replay_dataset(dataset_path, config.cam1_by_id, config.cam2_by_id)

    ui_w, ui_h, ui_scale = _ui_canvas_spec(target_mon)
    canvas = np.zeros((ui_h, ui_w, 3), dtype=np.uint8)
    thick = 1 if ui_scale < 1.0 else 2

    win_name = "Keyless Calibration"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.moveWindow(win_name, target_mon.x, target_mon.y)
    cv2.resizeWindow(win_name, target_mon.width, target_mon.height)
    cv2.setWindowProperty(win_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    try:
        with StereoCapture(
            cam1_by_id=config.cam1_by_id,
            cam2_by_id=config.cam2_by_id or None,
            mono=config.camera_mode == "mono",
            width=config.frame_width,
            height=config.frame_height,
            fps=config.fps,
            cam1_exposure=config.cam1_exposure,
            cam1_gain=config.cam1_gain,
            cam2_exposure=config.cam2_exposure,
            cam2_gain=config.cam2_gain,
            extract_landmarks=True,
            use_gpu=config.use_gpu,
            keep_images=False,
            capture_backend=getattr(config, "camera_backend", "v4l2"),
        ) as stereo:
            cam2_label = "Mono camera" if stereo.mono else "Camera 2"
            while True:
                neutral_yaw = 0.0
                neutral_pitch = 0.0
                skip_to_pursuit = False

                while True:
                    pair = stereo.read_pair()
                    canvas.fill(0)
                    cam1_ok = cam2_ok = False
                    if pair is not None:
                        o1 = pair.cam1.observation
                        o2 = pair.cam2.observation
                        cam1_ok = o1 is not None
                        cam2_ok = o2 is not None
                        if o1 is not None and o2 is not None:
                            neutral_yaw = 0.5 * (o1.yaw + o2.yaw)
                            neutral_pitch = 0.5 * (o1.pitch + o2.pitch)

                    replay_count = len(saved_replay[0]) if saved_replay is not None else 0
                    cam_ready = cam1_ok and cam2_ok
                    lines = [
                        f"Eye Calibration · {target_mon.connector}",
                        f"Face detected — ready to begin" if cam_ready else "Position your face in view of the camera",
                        "1. Look at each dot on screen as it appears.",
                        "2. Follow the gliding green dot smoothly with your eyes.",
                        "3. Gently turn and tilt your head while keeping eyes on the dot.",
                        "Press SPACE to start   ·   ESC to cancel"
                        + ("   ·   P = quick refine" if replay_count > 0 else ""),
                    ]
                    f_intro = 0.74 * ui_scale
                    for idx, line in enumerate(lines):
                        color = (
                            (120, 255, 140)
                            if (idx == 1 and cam1_ok and cam2_ok)
                            else ((110, 225, 255) if idx == 6 else (235, 235, 235))
                        )
                        (lw, _), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, f_intro, thick)
                        lx = max(15, (ui_w - lw) // 2)
                        ly_txt = ui_h // 2 - int(round(175 * ui_scale)) + idx * int(round(48 * ui_scale))
                        cv2.putText(
                            canvas,
                            line,
                            (lx, ly_txt),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            f_intro,
                            color,
                            thick,
                            cv2.LINE_AA,
                        )
                    cv2.imshow(win_name, canvas)
                    key = cv2.waitKey(1) & 0xFF
                    if key == 27:
                        raise KeyboardInterrupt("Calibration cancelled by user.")
                    if key == ord("x") and saved_replay is not None:
                        saved_replay = None
                        if dataset_path.exists():
                            dataset_path.unlink()
                    if key == ord("p") and saved_replay is not None and cam1_ok and cam2_ok:
                        skip_to_pursuit = True
                        break
                    if key == 32 and cam1_ok and cam2_ok:
                        break

                all_features: list[np.ndarray] = []
                all_targets: list[tuple[float, float]] = []
                all_groups: list[int] = []
                all_weights: list[float] = []

                if saved_replay is not None:
                    all_features.extend(saved_replay[0])
                    all_targets.extend(saved_replay[1])
                    all_groups.extend(saved_replay[2])
                    all_weights.extend(saved_replay[3])

                if not skip_to_pursuit:
                    last_seq1 = -1
                    last_seq2 = -1
                    live_yaw = neutral_yaw
                    live_pitch = neutral_pitch

                    for pt_idx, pt in enumerate(ANCHOR_CALIBRATION_POINTS):
                        local_x = int(round(pt.rel_x * target_mon.width))
                        local_y = int(round(pt.rel_y * target_mon.height))
                        ux = int(round(local_x * ui_scale))
                        uy = int(round(local_y * ui_scale))
                        global_x = float(target_mon.x + local_x)
                        global_y = float(target_mon.y + local_y)
                        cur_settle = pose_settle_sec if pt.is_pose_variation else settle_sec

                        pt_features: list[np.ndarray] = []
                        start_t = time.monotonic()
                        manual_trigger = False
                        prev_yaw = live_yaw
                        prev_pitch = live_pitch
                        pose_speed = 0.0
                        prev_feat_vec: np.ndarray | None = None

                        while len(pt_features) < samples_per_point:
                            pair = stereo.read_pair()
                            now = time.monotonic()
                            elapsed = now - start_t
                            canvas.fill(0)

                            head_moving = pt.is_pose_variation and pose_speed > 0.012
                            is_settling = ((elapsed < cur_settle) or head_moving) and not manual_trigger
                            dot_color = (0, 200, 255) if is_settling else (80, 255, 120)

                            if is_settling:
                                frac = max(0.0, 1.0 - elapsed / cur_settle)
                                ring_r = max(6, int(round((14 + 42 * frac) * ui_scale)))
                                cv2.circle(canvas, (ux, uy), ring_r, (0, 160, 255), thick, cv2.LINE_AA)
                            else:
                                prog_angle = int(round(360.0 * len(pt_features) / samples_per_point))
                                r_arc = max(10, int(round(24 * ui_scale)))
                                cv2.ellipse(
                                    canvas,
                                    (ux, uy),
                                    (r_arc, r_arc),
                                    -90,
                                    0,
                                    prog_angle,
                                    (80, 255, 120),
                                    thick + 1,
                                    cv2.LINE_AA,
                                )

                            pulse = max(6, int(round((14 + 3 * math.sin(now * 8.0)) * ui_scale)))
                            cv2.circle(canvas, (ux, uy), pulse, dot_color, -1, cv2.LINE_AA)
                            cv2.circle(canvas, (ux, uy), 2, (10, 10, 10), -1, cv2.LINE_AA)
                            cv2.circle(canvas, (ux, uy), 1, (255, 255, 255), -1, cv2.LINE_AA)

                            if pair is not None and (
                                pair.cam1.seq != last_seq1 or pair.cam2.seq != last_seq2
                            ):
                                last_seq1 = pair.cam1.seq
                                last_seq2 = pair.cam2.seq
                                o1 = pair.cam1.observation
                                o2 = pair.cam2.observation
                                if o1 is not None and o2 is not None:
                                    prev_yaw, prev_pitch = live_yaw, live_pitch
                                    live_yaw = 0.5 * (o1.yaw + o2.yaw)
                                    live_pitch = 0.5 * (o1.pitch + o2.pitch)
                                    pose_speed = math.hypot(
                                        live_yaw - prev_yaw, live_pitch - prev_pitch
                                    )
                                    if (
                                        not is_settling
                                        and o1.max_blink < config.blink_threshold
                                        and o2.max_blink < config.blink_threshold
                                    ):
                                        feat, both_ok = build_stereo_feature_vector(o1, o2)
                                        if both_ok and feat.shape == (STEREO_FEATURE_DIM,):
                                            eye_step = (
                                                0.0
                                                if prev_feat_vec is None
                                                else _iris_step_distance(prev_feat_vec, feat)
                                            )
                                            prev_feat_vec = feat
                                            if eye_step < 0.095:
                                                pt_features.append(feat)

                            if pt.is_pose_variation and is_settling:
                                dyaw_deg = math.degrees(live_yaw - neutral_yaw)
                                dpitch_deg = math.degrees(live_pitch - neutral_pitch)
                                hx = int(round((local_x - dyaw_deg * 5.0) * ui_scale))
                                hy = int(round((local_y + dpitch_deg * 5.0) * ui_scale))
                                cv2.arrowedLine(
                                    canvas,
                                    (ux, uy),
                                    (hx, hy),
                                    (255, 180, 60),
                                    thick,
                                    cv2.LINE_AA,
                                    tipLength=0.25,
                                )

                            if elapsed < cur_settle and not manual_trigger:
                                progress_txt = f"Look at center pinhole ({max(0.0, cur_settle - elapsed):.1f}s | SPACE=now)"
                            elif is_settling:
                                progress_txt = "Hold head & eyes still on center pinhole..."
                            else:
                                progress_txt = "Keep eyes locked on center pinhole"

                            _draw_anchored_label(
                                canvas=canvas,
                                dot_x=ux,
                                dot_y=uy,
                                title=pt.prompt,
                                subtitle=progress_txt,
                                title_color=(255, 255, 255)
                                if not pt.is_pose_variation
                                else (100, 225, 255),
                                dim_for_capture=not is_settling,
                                ui_scale=ui_scale,
                            )

                            cv2.imshow(win_name, canvas)
                            key = cv2.waitKey(1) & 0xFF
                            if key == 27:
                                raise KeyboardInterrupt("Calibration cancelled by user.")
                            if key == 32 and is_settling:
                                manual_trigger = True
                            if key == ord("r"):
                                pt_features.clear()
                                prev_feat_vec = None
                                manual_trigger = False
                                start_t = time.monotonic()

                        cleaned = _trim_dot_outliers(pt_features, keep_ratio=0.82)
                        all_features.extend(cleaned)
                        all_targets.extend([(global_x, global_y)] * len(cleaned))
                        all_groups.extend([pt_idx] * len(cleaned))
                        all_weights.extend([1.5] * len(cleaned))

                    s1_feats, s1_targs = _run_automated_pursuit_sweep(
                        win_name=win_name,
                        stereo=stereo,
                        target_mon=target_mon,
                        blink_threshold=config.blink_threshold,
                        sweep_idx=1,
                        duration_sec=sweep1_duration_sec,
                        prompt_title="Stage 2: Continuous Perimeter & Figure-8 Glide",
                        prompt_sub="Keep head relaxed and follow the moving green dot smoothly with your eyes!",
                    )
                    all_features.extend(s1_feats)
                    all_targets.extend(s1_targs)
                    all_groups.extend([50] * len(s1_feats))
                    all_weights.extend([1.8] * len(s1_feats))

                    s2_feats, s2_targs = _run_automated_pursuit_sweep(
                        win_name=win_name,
                        stereo=stereo,
                        target_mon=target_mon,
                        blink_threshold=config.blink_threshold,
                        sweep_idx=2,
                        duration_sec=sweep2_duration_sec,
                        prompt_title="Stage 3: Head-Turn & Lean Smooth Pursuit",
                        prompt_sub="Keep eyes locked on the moving dot while slowly turning/tilting your head & leaning!",
                    )
                    all_features.extend(s2_feats)
                    all_targets.extend(s2_targs)
                    all_groups.extend([51] * len(s2_feats))
                    all_weights.extend([1.8] * len(s2_feats))

                X = np.vstack(all_features)
                Y = np.asarray(all_targets, dtype=np.float64)
                groups = np.asarray(all_groups, dtype=np.intp)
                weights = np.asarray(all_weights, dtype=np.float64)

                model = _fit_gaze_model(
                    X=X,
                    targets_xy=Y,
                    config=config,
                    target_mon=target_mon,
                    layout=layout,
                    groups=groups,
                    sample_weights=weights,
                )
                accepted, refined_model, final_f, final_t, final_g, final_w = (
                    _preview_and_refine_calibration(
                        win_name=win_name,
                        stereo=stereo,
                        base_features=all_features,
                        base_targets=all_targets,
                        base_groups=all_groups,
                        base_weights=all_weights,
                        initial_model=model,
                        config=config,
                        target_mon=target_mon,
                        layout=layout,
                    )
                )
                if accepted:
                    _save_replay_dataset(
                        dataset_path=dataset_path,
                        X=np.vstack(final_f),
                        Y=np.asarray(final_t, dtype=np.float64),
                        groups=np.asarray(final_g, dtype=np.intp),
                        weights=np.asarray(final_w, dtype=np.float64),
                        cam1_by_id=config.cam1_by_id,
                        cam2_by_id=config.cam2_by_id,
                    )
                    refined_model.save(calib_path)
                    return refined_model
                saved_replay = (final_f, final_t, final_g, final_w)
    finally:
        cv2.destroyAllWindows()
