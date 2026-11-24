"""Monocular webcam gaze publisher with zero-grid passive click adaptation.

Implements the Gaze v2 cold-start path (GREENFIELD.md §6.3):
- Frame 1: projects head yaw/pitch and iris offsets through a bounded geometric
  prior onto the active DisplayTopology so live display-only preview works
  immediately with zero calibration clicks.
- Passive adaptation: natural desktop clicks pair the freshest 10D
  ``CameraObservation`` feature vector with the click coordinate in a bounded
  ring buffer. Once ``min_clicks`` (default 5) are collected, a regularized
  ridge regression fits ``(feat, 1) -> (x, y)`` and blends smoothly from the
  prior. Every update is reversible via ``undo_last()`` or ``reset()``.
- Seam: publishes plain ``(x, y, flags, timestamp_ns)`` on ``SharedGazeWriter``
  (``keyless_gaze_v1``) so the daemon, Firefox bridge, and UI consume it without
  knowing anything about the model.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from keyless.config.schema import AppConfig
from keyless.ipc.state import (
    FLAG_BLINKING,
    FLAG_FACE_DETECTED,
    SHM_NAME,
    SharedGazeWriter,
)
from keyless.vision.landmarks import CameraObservation, FaceFeatureExtractor
from keyless.vision.one_euro import OneEuroFilter2D

logger = logging.getLogger(__name__)

FEATURE_DIM = 10


def observation_to_frame_section(
    obs: CameraObservation,
    cam_id: str = "mono",
) -> dict[str, Any]:
    """Convert a ``CameraObservation`` into a schema-v0 ``frame`` section."""
    return {
        "cams": [str(cam_id)],
        "head_pose": {
            "yaw": float(obs.yaw),
            "pitch": float(obs.pitch),
            "roll": float(obs.roll),
        },
        "features": [float(v) for v in obs.to_feature_vector().tolist()],
    }


@dataclass(frozen=True)
class MonocularPrediction:
    """One smoothed monocular screen-space gaze prediction."""

    x: float
    y: float
    raw_x: float
    raw_y: float
    face_detected: bool
    blinking: bool
    samples: int
    using_ridge: bool
    timestamp_ns: int


class MonocularGazeEstimator:
    """Geometric cold-start prior + reversible online ridge adaptation."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        origin_x: int = 0,
        origin_y: int = 0,
        min_clicks: int = 5,
        blend_clicks: int = 10,
        ridge_alpha: float = 1e-2,
        max_samples: int = 256,
        smooth: bool = True,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if min_clicks < 2:
            raise ValueError("min_clicks must be >= 2")

        self.width = int(width)
        self.height = int(height)
        self.origin_x = int(origin_x)
        self.origin_y = int(origin_y)
        self.min_clicks = int(min_clicks)
        self.blend_clicks = max(1, int(blend_clicks))
        self.ridge_alpha = float(ridge_alpha)
        self._samples: deque[tuple[np.ndarray, float, float]] = deque(maxlen=max(min_clicks, int(max_samples)))
        self._weights: np.ndarray | None = None  # shape (FEATURE_DIM + 1, 2)
        self._filter = OneEuroFilter2D() if smooth else None
        self._lock = threading.Lock()

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)

    @property
    def is_adapted(self) -> bool:
        with self._lock:
            return self._weights is not None

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        cx = max(float(self.origin_x), min(float(self.origin_x + self.width - 1), float(x)))
        cy = max(float(self.origin_y), min(float(self.origin_y + self.height - 1), float(y)))
        return cx, cy

    def _prior_xy(self, feat: np.ndarray) -> tuple[float, float]:
        """Zero-click geometric prior mapping iris offsets + head yaw/pitch to screen pixels."""
        iris_dx = 0.5 * (float(feat[0]) + float(feat[2]))
        iris_dy = 0.5 * (float(feat[1]) + float(feat[3]))
        yaw = float(feat[4])
        pitch = float(feat[5])

        # Normalize to [-1, 1] around display center; conservative gains prevent edge slamming.
        nx = max(-1.0, min(1.0, 2.2 * iris_dx + 1.4 * yaw))
        ny = max(-1.0, min(1.0, 2.6 * iris_dy + 1.4 * pitch))

        px = self.origin_x + (0.5 + 0.45 * nx) * self.width
        py = self.origin_y + (0.5 + 0.45 * ny) * self.height
        return self._clamp(px, py)

    def _refit_locked(self) -> None:
        n = len(self._samples)
        if n < self.min_clicks:
            self._weights = None
            return
        X = np.ones((n, FEATURE_DIM + 1), dtype=np.float64)
        Y = np.empty((n, 2), dtype=np.float64)
        for i, (feat, sx, sy) in enumerate(self._samples):
            X[i, :FEATURE_DIM] = feat
            Y[i, 0] = sx
            Y[i, 1] = sy

        # Regularize feature weights but leave the bias term unpenalized.
        reg = np.eye(FEATURE_DIM + 1, dtype=np.float64) * self.ridge_alpha
        reg[FEATURE_DIM, FEATURE_DIM] = 1e-8
        try:
            self._weights = np.linalg.solve(X.T @ X + reg, X.T @ Y)
        except np.linalg.LinAlgError:
            self._weights = np.linalg.lstsq(X, Y, rcond=None)[0]

    def add_click(self, feat: np.ndarray, x: float, y: float) -> int:
        """Record one natural (feature, click_xy) pair and refit if ready."""
        arr = np.asarray(feat, dtype=np.float64).reshape(-1)
        if arr.shape[0] != FEATURE_DIM or not np.all(np.isfinite(arr)):
            raise ValueError(f"feature vector must be finite length {FEATURE_DIM}")
        cx, cy = self._clamp(x, y)
        with self._lock:
            self._samples.append((arr.copy(), cx, cy))
            self._refit_locked()
            return len(self._samples)

    def undo_last(self) -> int:
        """Reversibly remove the most recent click sample and refit."""
        with self._lock:
            if self._samples:
                self._samples.pop()
                self._refit_locked()
            return len(self._samples)

    def reset(self) -> None:
        """Discard all adapted samples and return cleanly to the cold-start prior."""
        with self._lock:
            self._samples.clear()
            self._weights = None
            if self._filter is not None:
                self._filter.reset()

    def predict(
        self,
        feat: np.ndarray,
        *,
        t_sec: float | None = None,
    ) -> tuple[float, float, float, float, bool]:
        """Return ``(smooth_x, smooth_y, raw_x, raw_y, using_ridge)``."""
        arr = np.asarray(feat, dtype=np.float64).reshape(-1)
        if arr.shape[0] != FEATURE_DIM:
            raise ValueError(f"feature vector must have length {FEATURE_DIM}")

        prior_x, prior_y = self._prior_xy(arr)
        with self._lock:
            weights = self._weights
            n = len(self._samples)

        if weights is not None:
            x_aug = np.empty(FEATURE_DIM + 1, dtype=np.float64)
            x_aug[:FEATURE_DIM] = arr
            x_aug[FEATURE_DIM] = 1.0
            pred = x_aug @ weights
            rx, ry = self._clamp(float(pred[0]), float(pred[1]))
            blend = min(1.0, (n - self.min_clicks + 1) / float(self.blend_clicks))
            raw_x = (1.0 - blend) * prior_x + blend * rx
            raw_y = (1.0 - blend) * prior_y + blend * ry
            using_ridge = True
        else:
            raw_x, raw_y = prior_x, prior_y
            using_ridge = False

        if self._filter is not None:
            now = time.monotonic() if t_sec is None else float(t_sec)
            sx, sy = self._filter(raw_x, raw_y, now)
            sx, sy = self._clamp(sx, sy)
        else:
            sx, sy = raw_x, raw_y

        return sx, sy, raw_x, raw_y, using_ridge


def _open_monocular_capture(
    config: AppConfig,
    camera_override: str | None = None,
) -> tuple[Any, str]:
    """Open one webcam cross-platform using existing platform adapters."""
    from keyless.platform.router import current_host, resolve_camera_config

    host = current_host()
    if camera_override:
        cam_id = camera_override
    else:
        _mode, cam1, _cam2 = resolve_camera_config(
            "mono", config.cam1_by_id, config.cam2_by_id
        )
        cam_id = cam1 or config.cam1_by_id

    if host == "linux":
        from keyless.platform.linux.v4l2 import open_locked_capture

        cap, _report = open_locked_capture(
            by_id_path=cam_id,
            width=config.frame_width,
            height=config.frame_height,
            fps=config.fps,
            exposure_absolute=config.cam1_exposure,
            gain=config.cam1_gain,
            manual_exposure=not config.cam_auto_exposure,
        )
        return cap, cam_id

    if host == "macos":
        from keyless.platform.macos.capture import open_opencv_capture

        cap, _report = open_opencv_capture(
            cam_id,
            width=config.frame_width,
            height=config.frame_height,
            fps=config.fps,
        )
        return cap, cam_id

    import cv2

    cap = cv2.VideoCapture(int(cam_id) if str(cam_id).isdigit() else cam_id)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera {cam_id!r}")
    return cap, str(cam_id)


class MonocularGazePublisher:
    """Reads one webcam, extracts MediaPipe landmarks, and publishes to shared memory."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        camera_id: str | None = None,
        shm_name: str = SHM_NAME,
        min_clicks: int = 5,
        ridge_alpha: float = 1e-2,
        enable_click_adaptation: bool = True,
        publish_shm: bool = True,
        on_prediction: Callable[[MonocularPrediction], None] | None = None,
    ) -> None:
        from keyless.platform.router import get_display_layout

        self.config = config or AppConfig.load()
        self.layout = get_display_layout()
        self.camera_id = camera_id
        self.shm_name = shm_name
        self.enable_click_adaptation = bool(enable_click_adaptation)
        self.publish_shm = bool(publish_shm)
        self._on_prediction = on_prediction

        self.estimator = MonocularGazeEstimator(
            self.layout.total_width,
            self.layout.total_height,
            min_clicks=min_clicks,
            ridge_alpha=ridge_alpha,
        )

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap: Any = None
        self._resolved_cam_id: str = camera_id or "mono"
        self._extractor: FaceFeatureExtractor | None = None
        self._writer: SharedGazeWriter | None = None
        self._click_observer: Any = None

        self._latest_obs: CameraObservation | None = None
        self._latest_obs_mono: float = 0.0
        self._latest_pred: MonocularPrediction | None = None
        self._frames_read: int = 0
        self._faces_detected: int = 0
        self._clicks_adapted: int = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._cap, self._resolved_cam_id = _open_monocular_capture(
            self.config, self.camera_id
        )
        self._extractor = FaceFeatureExtractor(use_gpu=self.config.use_gpu)
        if self.publish_shm:
            self._writer = SharedGazeWriter(shm_name=self.shm_name)

        if self.enable_click_adaptation:
            from keyless.platform.router import current_host

            if current_host() == "linux":
                from keyless.gaze.recorder import LinuxClickObserver

                obs = LinuxClickObserver(self.on_click)
                if obs.start():
                    self._click_observer = obs
                else:
                    logger.info(
                        "Passive click adaptation unavailable (%s); running prior-only.",
                        obs.unavailable_reason,
                    )

        self._thread = threading.Thread(
            target=self._loop, name="monocular-gaze-pub", daemon=True
        )
        self._thread.start()

    def on_click(self, x: float, y: float, *, max_age_sec: float = 0.25) -> bool:
        """Pair a physical click with the freshest face observation if recent."""
        with self._lock:
            obs = self._latest_obs
            age = time.monotonic() - self._latest_obs_mono
        if obs is None or age > max_age_sec:
            return False
        if obs.max_blink >= self.config.blink_threshold:
            return False
        self.estimator.add_click(obs.to_feature_vector(), x, y)
        with self._lock:
            self._clicks_adapted += 1
        return True

    def latest_frame_section(self, *, max_age_sec: float = 0.5) -> dict[str, Any] | None:
        """Return the latest schema-v0 ``frame`` dict for ``SessionRecorder``."""
        with self._lock:
            obs = self._latest_obs
            age = time.monotonic() - self._latest_obs_mono
            cam_id = self._resolved_cam_id
        if obs is None or age > max_age_sec:
            return None
        return observation_to_frame_section(obs, cam_id=cam_id)

    def latest_prediction(self) -> MonocularPrediction | None:
        with self._lock:
            return self._latest_pred

    def latest_pixel(self) -> tuple[float, float] | None:
        pred = self.latest_prediction()
        if pred is None or not pred.face_detected:
            return None
        return pred.x, pred.y

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "camera": self._resolved_cam_id,
                "frames_read": self._frames_read,
                "faces_detected": self._faces_detected,
                "clicks_adapted": self._clicks_adapted,
                "samples": self.estimator.sample_count,
                "using_ridge": self.estimator.is_adapted,
            }

    def _loop(self) -> None:
        assert self._cap is not None and self._extractor is not None
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok or frame is None:
                time.sleep(0.01)
                continue
            ts_ns = time.monotonic_ns()
            t_sec = ts_ns / 1e9
            obs = self._extractor.process(frame, ts_ns)
            with self._lock:
                self._frames_read += 1

            if obs is None:
                pred = MonocularPrediction(
                    x=self.layout.total_width * 0.5,
                    y=self.layout.total_height * 0.5,
                    raw_x=self.layout.total_width * 0.5,
                    raw_y=self.layout.total_height * 0.5,
                    face_detected=False,
                    blinking=False,
                    samples=self.estimator.sample_count,
                    using_ridge=self.estimator.is_adapted,
                    timestamp_ns=ts_ns,
                )
                with self._lock:
                    self._latest_pred = pred
                if self._writer is not None:
                    self._writer.write(pred.x, pred.y, 0, timestamp_ns=ts_ns)
                continue

            blinking = obs.max_blink >= self.config.blink_threshold
            feat = obs.to_feature_vector()
            sx, sy, rx, ry, using_ridge = self.estimator.predict(feat, t_sec=t_sec)
            flags = FLAG_FACE_DETECTED | (FLAG_BLINKING if blinking else 0)
            pred = MonocularPrediction(
                x=sx,
                y=sy,
                raw_x=rx,
                raw_y=ry,
                face_detected=True,
                blinking=blinking,
                samples=self.estimator.sample_count,
                using_ridge=using_ridge,
                timestamp_ns=ts_ns,
            )
            with self._lock:
                self._latest_obs = obs
                self._latest_obs_mono = time.monotonic()
                self._latest_pred = pred
                self._faces_detected += 1

            if self._writer is not None:
                self._writer.write(sx, sy, flags, timestamp_ns=ts_ns)
            if self._on_prediction is not None:
                try:
                    self._on_prediction(pred)
                except Exception:
                    logger.debug("on_prediction callback failed", exc_info=True)

    def run(self, duration_sec: float | None = None) -> dict[str, Any]:
        """Start the publisher and block until ``duration_sec`` elapses or Ctrl+C."""
        self.start()
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                if duration_sec is not None and (time.monotonic() - started) >= duration_sec:
                    break
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return self.stats()

    def stop(self) -> None:
        self._stop.set()
        if self._click_observer is not None:
            try:
                self._click_observer.stop()
            except Exception:
                pass
            self._click_observer = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self._extractor is not None:
            try:
                self._extractor.close()
            except Exception:
                pass
            self._extractor = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None


__all__ = [
    "FEATURE_DIM",
    "MonocularGazeEstimator",
    "MonocularGazePublisher",
    "MonocularPrediction",
    "observation_to_frame_section",
]
