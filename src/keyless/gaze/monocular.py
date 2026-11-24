"""Unified monocular (1-cam, 10D) and binocular (2-cam, 20D) webcam gaze publisher.

Implements the Gaze v2 cold-start path (GREENFIELD.md §6.3):
- Frame 1: projects head yaw/pitch and iris offsets through a bounded geometric
  prior onto the active DisplayTopology so live display-only preview works
  immediately with zero calibration clicks.
- Passive adaptation: natural desktop clicks pair the freshest 10D (monocular)
  or 20D (binocular/stereo) feature vector with the click coordinate in a
  bounded ring buffer. Once ``min_clicks`` (default 5) are collected, a
  regularized ridge regression fits ``(feat, 1) -> (x, y)`` and blends smoothly
  from the prior. Every update is reversible via ``undo_last()`` or ``reset()``.
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
from keyless.vision.landmarks import CameraObservation, STEREO_FEATURE_DIM
from keyless.vision.one_euro import OneEuroFilter2D
from keyless.vision.stereo_capture import StereoCapture

logger = logging.getLogger(__name__)

FEATURE_DIM = 10
BINOCULAR_FEATURE_DIM = STEREO_FEATURE_DIM  # 20


def observation_to_frame_section(
    obs: CameraObservation,
    cam_id: str = "mono",
    obs2: CameraObservation | None = None,
    cam2_id: str | None = None,
) -> dict[str, Any]:
    """Convert one (monocular) or two (binocular) observations into a schema-v0 ``frame`` section."""
    if obs2 is not None:
        cams = [str(cam_id), str(cam2_id or "cam2")]
        feats = np.concatenate([obs.to_feature_vector(), obs2.to_feature_vector()])
        yaw = 0.5 * (float(obs.yaw) + float(obs2.yaw))
        pitch = 0.5 * (float(obs.pitch) + float(obs2.pitch))
        roll = 0.5 * (float(obs.roll) + float(obs2.roll))
    else:
        cams = [str(cam_id)]
        feats = obs.to_feature_vector()
        yaw = float(obs.yaw)
        pitch = float(obs.pitch)
        roll = float(obs.roll)
    return {
        "cams": cams,
        "head_pose": {"yaw": yaw, "pitch": pitch, "roll": roll},
        "features": [float(v) for v in feats.tolist()],
    }


@dataclass(frozen=True)
class MonocularPrediction:
    """One smoothed screen-space gaze prediction (monocular or binocular)."""

    x: float
    y: float
    raw_x: float
    raw_y: float
    face_detected: bool
    blinking: bool
    samples: int
    using_ridge: bool
    timestamp_ns: int
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    mode: str = "monocular"
    skew_ms: float = 0.0


class MonocularGazeEstimator:
    """Geometric cold-start prior + reversible online ridge adaptation (10D mono or 20D stereo)."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        origin_x: int = 0,
        origin_y: int = 0,
        feature_dim: int = FEATURE_DIM,
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
        if feature_dim not in (FEATURE_DIM, BINOCULAR_FEATURE_DIM):
            raise ValueError(f"feature_dim must be {FEATURE_DIM} or {BINOCULAR_FEATURE_DIM}")

        self.width = int(width)
        self.height = int(height)
        self.origin_x = int(origin_x)
        self.origin_y = int(origin_y)
        self.feature_dim = int(feature_dim)
        self.min_clicks = int(min_clicks)
        self.blend_clicks = max(1, int(blend_clicks))
        self.ridge_alpha = float(ridge_alpha)
        self._samples: deque[tuple[np.ndarray, float, float]] = deque(
            maxlen=max(min_clicks, int(max_samples))
        )
        self._weights: np.ndarray | None = None
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

    def sample_points(self) -> list[list[float]]:
        """Return ``[[x, y], ...]`` of passive click coordinates currently in the buffer."""
        with self._lock:
            return [[round(sx, 1), round(sy, 1)] for _, sx, sy in self._samples]

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        cx = max(float(self.origin_x), min(float(self.origin_x + self.width - 1), float(x)))
        cy = max(float(self.origin_y), min(float(self.origin_y + self.height - 1), float(y)))
        return cx, cy

    def _prior_xy(self, feat: np.ndarray) -> tuple[float, float]:
        """Zero-click geometric prior mapping iris offsets + head yaw/pitch to screen pixels."""
        if feat.shape[0] == BINOCULAR_FEATURE_DIM:
            iris_dx = 0.25 * (float(feat[0]) + float(feat[2]) + float(feat[10]) + float(feat[12]))
            iris_dy = 0.25 * (float(feat[1]) + float(feat[3]) + float(feat[11]) + float(feat[13]))
            yaw = 0.5 * (float(feat[4]) + float(feat[14]))
            pitch = 0.5 * (float(feat[5]) + float(feat[15]))
        else:
            iris_dx = 0.5 * (float(feat[0]) + float(feat[2]))
            iris_dy = 0.5 * (float(feat[1]) + float(feat[3]))
            yaw = float(feat[4])
            pitch = float(feat[5])

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
        d = self.feature_dim
        X = np.ones((n, d + 1), dtype=np.float64)
        Y = np.empty((n, 2), dtype=np.float64)
        for i, (feat, sx, sy) in enumerate(self._samples):
            X[i, :d] = feat
            Y[i, 0] = sx
            Y[i, 1] = sy

        reg = np.eye(d + 1, dtype=np.float64) * self.ridge_alpha
        reg[d, d] = 1e-8
        try:
            self._weights = np.linalg.solve(X.T @ X + reg, X.T @ Y)
        except np.linalg.LinAlgError:
            self._weights = np.linalg.lstsq(X, Y, rcond=None)[0]

    def add_click(self, feat: np.ndarray, x: float, y: float) -> int:
        """Record one natural (feature, click_xy) pair and refit if ready."""
        arr = np.asarray(feat, dtype=np.float64).reshape(-1)
        if arr.shape[0] != self.feature_dim or not np.all(np.isfinite(arr)):
            raise ValueError(f"feature vector must be finite length {self.feature_dim}")
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
        if arr.shape[0] != self.feature_dim:
            raise ValueError(f"feature vector must have length {self.feature_dim}")

        prior_x, prior_y = self._prior_xy(arr)
        with self._lock:
            weights = self._weights
            n = len(self._samples)

        if weights is not None:
            d = self.feature_dim
            x_aug = np.empty(d + 1, dtype=np.float64)
            x_aug[:d] = arr
            x_aug[d] = 1.0
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


class MonocularGazePublisher:
    """Unified gaze publisher supporting monocular (1 cam, 10D) and binocular (2 cams, 20D)."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        camera_id: str | None = None,
        camera_mode: str | None = None,
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
        self.requested_mode = (camera_mode or self.config.camera_mode or "auto").lower()
        self.shm_name = shm_name
        self.min_clicks = int(min_clicks)
        self.ridge_alpha = float(ridge_alpha)
        self.enable_click_adaptation = bool(enable_click_adaptation)
        self.publish_shm = bool(publish_shm)
        self._on_prediction = on_prediction

        self.active_mode: str = "monocular"
        self.estimator = MonocularGazeEstimator(
            self.layout.total_width,
            self.layout.total_height,
            feature_dim=FEATURE_DIM,
            min_clicks=self.min_clicks,
            ridge_alpha=self.ridge_alpha,
        )

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: StereoCapture | None = None
        self._resolved_cams: list[str] = []
        self._writer: SharedGazeWriter | None = None
        self._click_observer: Any = None

        self._latest_feat: np.ndarray | None = None
        self._latest_frame_dict: dict[str, Any] | None = None
        self._latest_obs_mono: float = 0.0
        self._latest_blinking: bool = False
        self._latest_pred: MonocularPrediction | None = None
        self._frames_read: int = 0
        self._faces_detected: int = 0
        self._clicks_adapted: int = 0
        self._measured_fps: float = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        from keyless.platform.router import current_host, resolve_camera_config

        self._stop.clear()
        if self.camera_id:
            resolved_mode, cam1, cam2 = "mono", self.camera_id, ""
        else:
            resolved_mode, cam1, cam2 = resolve_camera_config(
                self.requested_mode, self.config.cam1_by_id, self.config.cam2_by_id
            )
            if not cam1:
                raise RuntimeError("No webcam detected. Connect a camera and try again.")
            if self.requested_mode == "stereo" and not cam2:
                raise RuntimeError(
                    "Binocular (stereo) mode requested, but only one camera is connected."
                )

        is_binocular = resolved_mode == "stereo" and bool(cam2)
        self.active_mode = "binocular" if is_binocular else "monocular"
        self._resolved_cams = [cam1, cam2] if is_binocular else [cam1]
        feat_dim = BINOCULAR_FEATURE_DIM if is_binocular else FEATURE_DIM
        self.estimator = MonocularGazeEstimator(
            self.layout.total_width,
            self.layout.total_height,
            feature_dim=feat_dim,
            min_clicks=self.min_clicks,
            ridge_alpha=self.ridge_alpha,
        )

        backend = "opencv" if current_host() == "macos" else (
            "pipewire" if (not is_binocular and self.config.camera_backend == "pipewire") else "v4l2"
        )
        self._capture = StereoCapture(
            cam1_by_id=cam1,
            cam2_by_id=cam2 if is_binocular else None,
            mono=not is_binocular,
            width=self.config.frame_width,
            height=self.config.frame_height,
            fps=self.config.fps,
            cam1_exposure=self.config.cam1_exposure,
            cam1_gain=self.config.cam1_gain,
            cam2_exposure=self.config.cam2_exposure,
            cam2_gain=self.config.cam2_gain,
            extract_landmarks=True,
            use_gpu=self.config.use_gpu,
            keep_images=False,
            auto_exposure=self.config.cam_auto_exposure,
            capture_backend=backend,
        )
        self._capture.start()

        if self.publish_shm:
            self._writer = SharedGazeWriter(shm_name=self.shm_name)

        if self.enable_click_adaptation and current_host() == "linux":
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
            target=self._loop, name="keyless-gaze-pub", daemon=True
        )
        self._thread.start()

    def on_click(self, x: float, y: float, *, max_age_sec: float = 0.25) -> bool:
        """Pair a physical click with the freshest face feature vector if recent."""
        with self._lock:
            feat = self._latest_feat
            age = time.monotonic() - self._latest_obs_mono
            blinking = self._latest_blinking
        if feat is None or age > max_age_sec or blinking:
            return False
        self.estimator.add_click(feat, x, y)
        with self._lock:
            self._clicks_adapted += 1
        return True

    def latest_frame_section(self, *, max_age_sec: float = 0.5) -> dict[str, Any] | None:
        """Return the latest schema-v0 ``frame`` dict for ``SessionRecorder``."""
        with self._lock:
            frame_dict = self._latest_frame_dict
            age = time.monotonic() - self._latest_obs_mono
        if frame_dict is None or age > max_age_sec:
            return None
        return dict(frame_dict)

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
                "mode": self.active_mode,
                "feature_dim": self.estimator.feature_dim,
                "cameras": list(self._resolved_cams),
                "camera": self._resolved_cams[0] if self._resolved_cams else "mono",
                "frames_read": self._frames_read,
                "faces_detected": self._faces_detected,
                "clicks_adapted": self._clicks_adapted,
                "samples": self.estimator.sample_count,
                "min_clicks": self.estimator.min_clicks,
                "using_ridge": self.estimator.is_adapted,
                "measured_fps": round(self._measured_fps, 1),
                "sample_points": self.estimator.sample_points(),
            }

    def _loop(self) -> None:
        assert self._capture is not None
        last_seq = -1
        while not self._stop.is_set():
            pair = self._capture.read_pair(timeout_s=0.15)
            if pair is None:
                continue
            if pair.cam1.seq == last_seq:
                time.sleep(0.004)
                continue
            last_seq = pair.cam1.seq

            ts_ns = pair.cam1.timestamp_ns or time.monotonic_ns()
            t_sec = ts_ns / 1e9
            obs1 = pair.cam1.observation
            obs2 = None if self._capture.mono else pair.cam2.observation
            skew_ms = 0.0 if self._capture.mono else float(pair.skew_ms)

            with self._lock:
                self._frames_read += 1
                self._measured_fps = float(pair.cam1.measured_fps)

            if self.active_mode == "binocular":
                if obs1 is None or obs2 is None:
                    self._publish_no_face(ts_ns, skew_ms)
                    continue
                feat = np.concatenate([obs1.to_feature_vector(), obs2.to_feature_vector()])
                blinking = max(obs1.max_blink, obs2.max_blink) >= self.config.blink_threshold
                yaw = 0.5 * (float(obs1.yaw) + float(obs2.yaw))
                pitch = 0.5 * (float(obs1.pitch) + float(obs2.pitch))
                roll = 0.5 * (float(obs1.roll) + float(obs2.roll))
                frame_dict = observation_to_frame_section(
                    obs1, self._resolved_cams[0], obs2, self._resolved_cams[1]
                )
            else:
                if obs1 is None:
                    self._publish_no_face(ts_ns, 0.0)
                    continue
                feat = obs1.to_feature_vector()
                blinking = obs1.max_blink >= self.config.blink_threshold
                yaw = float(obs1.yaw)
                pitch = float(obs1.pitch)
                roll = float(obs1.roll)
                frame_dict = observation_to_frame_section(obs1, self._resolved_cams[0])

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
                yaw=yaw,
                pitch=pitch,
                roll=roll,
                mode=self.active_mode,
                skew_ms=skew_ms,
            )
            with self._lock:
                self._latest_feat = feat
                self._latest_frame_dict = frame_dict
                self._latest_obs_mono = time.monotonic()
                self._latest_blinking = blinking
                self._latest_pred = pred
                self._faces_detected += 1

            if self._writer is not None:
                self._writer.write(sx, sy, flags, timestamp_ns=ts_ns)
            if self._on_prediction is not None:
                try:
                    self._on_prediction(pred)
                except Exception:
                    logger.debug("on_prediction callback failed", exc_info=True)

    def _publish_no_face(self, ts_ns: int, skew_ms: float) -> None:
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
            mode=self.active_mode,
            skew_ms=skew_ms,
        )
        with self._lock:
            self._latest_pred = pred
        if self._writer is not None:
            self._writer.write(pred.x, pred.y, 0, timestamp_ns=ts_ns)

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
        if self._capture is not None:
            try:
                self._capture.close()
            except Exception:
                pass
            self._capture = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None


__all__ = [
    "BINOCULAR_FEATURE_DIM",
    "FEATURE_DIM",
    "MonocularGazeEstimator",
    "MonocularGazePublisher",
    "MonocularPrediction",
    "observation_to_frame_section",
]
