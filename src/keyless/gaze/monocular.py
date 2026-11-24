"""Unified monocular (1-cam, 10D) and binocular (2-cam, 20D) multi-display gaze publisher.

Implements the Gaze v2 cold-start path (GREENFIELD.md §6.3) with full 3D
multi-display geometry (swapped monitors, bezel gaps, angled/tilted screens,
different physical dimensions):
- Frame 1: projects head yaw/pitch and iris offsets into 3D gaze angles
  ``(az, el)`` and intersects all 3D ``DisplayPlane``s in ``DisplayRig``.
- Passive adaptation: natural desktop clicks on any monitor are projected onto
  that monitor's 3D physical plane to obtain ground-truth ``(az, el)`` ray
  angles. A regularized global 3D angular ridge regression fits
  ``(feat, 1) -> (az, el)`` across all monitors, so clicks on Monitor 1 improve
  estimates on Monitor 2/3 even across gaps and angles. Once a specific display
  accumulates ``>= 4`` local clicks, a per-display residual ridge layer refines
  local ``(du, dv)`` coordinates on that monitor. Every update is reversible via
  ``undo_last()`` or ``reset()``.
- Seam: publishes plain ``(x, y, flags, timestamp_ns)`` on ``SharedGazeWriter``
  (``keyless_gaze_v1``) so the daemon, Firefox bridge, Linux Wayland overlay,
  and web UI consume it without knowing anything about the model.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.gaze.displays import DisplayRig, infer_rig_from_clicks
from keyless.gaze.learner import GazeLearner
from keyless.gaze.profile import GazeProfile, profile_path
from keyless.ipc.state import (
    FLAG_BLINKING,
    FLAG_DEV_MONITOR_BORDER,
    FLAG_FACE_DETECTED,
    SHM_NAME,
    SharedGazeWriter,
)
from keyless.vision.landmarks import CameraObservation, STEREO_FEATURE_DIM
from keyless.gaze.state_estimator import GazeKinematics, GazeStateEstimator
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
    """One smoothed screen-space gaze prediction (monocular or binocular, multi-display)."""

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
    display: str = ""
    in_gap: bool = False
    kinematics: str = "fixation"
    blinks_suppressed: int = 0
    outliers_suppressed: int = 0
    az: float = 0.0
    el: float = 0.0
    tx: float = 0.0
    ty: float = 0.0
    tz: float = 0.0
    blink_left: float = 0.0
    blink_right: float = 0.0


class MonocularGazeEstimator:
    """Linear gaze model with a Bayesian online learner and 3D ray-plane output.

    ``angles = [feat, 1] @ M`` where ``M`` is the posterior mean of
    ``GazeLearner``. The prior is the pursuit profile when one exists, else a
    linear form of the anatomical cold-start prior. Natural clicks are gated
    labels (see ``learner``); there is no click-count switch or blend ramp.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        origin_x: int = 0,
        origin_y: int = 0,
        rig: DisplayRig | None = None,
        auto_rig: bool = True,
        feature_dim: int = FEATURE_DIM,
        min_clicks: int = 5,
        blend_clicks: int = 10,
        ridge_alpha: float = 1e-2,
        max_samples: int = 256,
        smooth: bool = True,
        profile: GazeProfile | None = None,
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
        self.rig = rig or DisplayRig.from_single_rect(
            self.width, self.height, origin_x=self.origin_x, origin_y=self.origin_y
        )
        self._base_rig = self.rig
        self.auto_rig = bool(auto_rig)
        self._rig_meta: dict[str, Any] = {"inferred": False, "clicks_per_display": {}, "swapped": False}
        self.feature_dim = int(feature_dim)
        self.min_clicks = int(min_clicks)
        self.ridge_alpha = float(ridge_alpha)
        # Accepted click samples: (feat, click_x, click_y, t, weight)
        self._samples: deque[tuple[np.ndarray, float, float, float, float]] = deque(
            maxlen=max(min_clicks, int(max_samples))
        )
        self._state_est = GazeStateEstimator() if smooth else None
        self._lock = threading.Lock()
        self._last_display: str = self.rig.planes[0].connector
        self._last_in_gap: bool = False
        self._last_kinematics: str = GazeKinematics.FIXATION.value
        self._last_az: float = 0.0
        self._last_el: float = 0.0
        self.profile: GazeProfile | None = None
        self.latency_ms: float = 80.0
        self.precision_deg: float = 1.0
        self.clicks_seen: int = 0
        self.clicks_ignored: int = 0
        self.learner = self._cold_start_learner()
        if profile is not None:
            self.apply_profile(profile)

    # --- priors ---------------------------------------------------------

    def _cold_start_learner(self) -> GazeLearner:
        """Linear form of the anatomical prior with wide uncertainty."""
        d = self.feature_dim
        min_az, max_az, min_el, max_el = self.rig.angular_bounds()
        az_mid, el_mid = 0.5 * (min_az + max_az), 0.5 * (min_el + max_el)
        az_half = max(0.15, 0.5 * (max_az - min_az))
        el_half = max(0.10, 0.5 * (max_el - min_el))
        M0 = np.zeros((d + 1, 2))
        if d == BINOCULAR_FEATURE_DIM:
            ix, iy, yaw, pitch = (0, 2, 10, 12), (1, 3, 11, 13), (4, 14), (5, 15)
        else:
            ix, iy, yaw, pitch = (0, 2), (1, 3), (4,), (5,)
        # Horizontal terms negated: +iris_dx / +yaw in camera space = user's left.
        for j in ix:
            M0[j, 0] = -0.9 * az_half * 2.2 / len(ix)
        for j in yaw:
            M0[j, 0] = -0.9 * az_half * 1.4 / len(yaw)
        for j in iy:
            M0[j, 1] = 0.9 * el_half * 2.6 / len(iy)
        for j in pitch:
            M0[j, 1] = 0.9 * el_half * 1.4 / len(pitch)
        M0[d, 0], M0[d, 1] = az_mid, el_mid
        P0 = np.eye(d + 1) * 0.5
        P0[d, d] = 0.3 ** 2
        return GazeLearner(M0=M0, P0=P0)

    def apply_profile(self, profile: GazeProfile) -> None:
        """Adopt a pursuit profile as the learner prior (clicks keep refining it)."""
        if profile.feature_dim != self.feature_dim:
            return
        with self._lock:
            self.profile = profile
            self.learner = GazeLearner(M0=profile.m0, P0=profile.p0)
            self.latency_ms = float(profile.latency_ms)
            self.precision_deg = float(profile.precision_deg)
            if profile.rig_from_pursuit:
                self.auto_rig = False
            self._samples.clear()
        if self._state_est is not None:
            self._state_est.tune(
                blink_enter=profile.blink_enter,
                blink_exit=profile.blink_exit,
                precision_deg=profile.precision_deg,
            )

    # --- properties -----------------------------------------------------

    @property
    def blink_enter(self) -> float:
        return self._state_est.blink_enter if self._state_est is not None else 0.25

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)

    @property
    def is_adapted(self) -> bool:
        with self._lock:
            return self.profile is not None or self.learner.n_updates > 0

    @property
    def last_display(self) -> str:
        with self._lock:
            return self._last_display

    @property
    def last_in_gap(self) -> bool:
        with self._lock:
            return self._last_in_gap

    @property
    def last_kinematics(self) -> str:
        with self._lock:
            return self._last_kinematics

    @property
    def last_angles(self) -> tuple[float, float]:
        with self._lock:
            return self._last_az, self._last_el

    @property
    def blinks_suppressed(self) -> int:
        return self._state_est.blinks_suppressed if self._state_est is not None else 0

    @property
    def outliers_suppressed(self) -> int:
        return self._state_est.outliers_suppressed if self._state_est is not None else 0

    @property
    def rig_meta(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._rig_meta)

    def set_rig(self, rig: DisplayRig, *, auto_rig: bool | None = None) -> None:
        """Update physical 3D display geometry and replay click labels on the new rig."""
        with self._lock:
            self.rig = rig
            self._base_rig = rig
            if auto_rig is not None:
                self.auto_rig = bool(auto_rig)
            self._replay_locked()

    def sample_points(self) -> list[list[float]]:
        """Return ``[[x, y], ...]`` of accepted click labels currently in the buffer."""
        with self._lock:
            return [[round(sx, 1), round(sy, 1)] for _, sx, sy, _, _ in self._samples]

    # --- learning -------------------------------------------------------

    def _click_target(self, x: float, y: float) -> tuple[tuple[float, float], float]:
        """Ray angles of a click and the angular area (sr) of its display."""
        az, el, conn, _u, _v = self.rig.pixel_to_ray_angles(x, y)
        plane = self.rig.plane_for_connector(conn) or self.rig.planes[0]
        dist = max(300.0, float(np.linalg.norm(plane.uv_to_3d(0.5, 0.5))))
        area = (plane.width_mm * plane.height_mm) / (dist * dist)
        return (az, el), area

    def _replay_locked(self) -> None:
        if self.auto_rig and len(self._samples) >= 2:
            pairs = [(f, sx, sy) for f, sx, sy, _, _ in self._samples]
            self.rig, self._rig_meta = infer_rig_from_clicks(self._base_rig, pairs)
        self.learner.reset()
        for feat, sx, sy, t, _w in self._samples:
            target, area = self._click_target(sx, sy)
            self.learner.observe(feat, target, area_sr=area, t=t, min_weight=0.0)

    def add_click(self, feat: np.ndarray, x: float, y: float, *, t: float | None = None) -> int:
        """Offer one (feature, click) pair; it becomes a label only if plausibly looked at."""
        arr = np.asarray(feat, dtype=np.float64).reshape(-1)
        if arr.shape[0] != self.feature_dim or not np.all(np.isfinite(arr)):
            raise ValueError(f"feature vector must be finite length {self.feature_dim}")
        now = time.monotonic() if t is None else float(t)
        with self._lock:
            self.clicks_seen += 1
            target, area = self._click_target(x, y)
            w = self.learner.observe(arr, target, area_sr=area, t=now)
            if w <= 0.0:
                self.clicks_ignored += 1
                return len(self._samples)
            self._samples.append((arr.copy(), float(x), float(y), now, w))
            if self.auto_rig and len(self._samples) in (2, 3, 4, 6, 8) or (
                self.auto_rig and len(self._samples) % 5 == 0
            ):
                self._replay_locked()
            return len(self._samples)

    def undo_last(self) -> int:
        """Reversibly remove the most recent accepted click label."""
        with self._lock:
            if self._samples:
                self._samples.pop()
                self._replay_locked()
            return len(self._samples)

    def reset(self) -> None:
        """Discard click labels and return to the profile (or cold-start) prior."""
        with self._lock:
            self._samples.clear()
            self.learner.reset()
            if self._state_est is not None:
                self._state_est.reset()

    def angles(self, feat: np.ndarray) -> tuple[float, float]:
        """Unsmoothed model angles for one feature vector."""
        x = GazeLearner.augment(feat)
        with self._lock:
            a = x @ self.learner.M
        min_az, max_az, min_el, max_el = self.rig.angular_bounds()
        return (
            max(min_az - 0.25, min(max_az + 0.25, float(a[0]))),
            max(min_el - 0.25, min(max_el + 0.25, float(a[1]))),
        )

    def predict(
        self,
        feat: np.ndarray,
        *,
        t_sec: float | None = None,
        blink_level: float = 0.0,
        confidence: float = 1.0,
    ) -> tuple[float, float, float, float, bool]:
        """Return ``(smooth_x, smooth_y, raw_x, raw_y, adapted)`` via Kalman + ray-plane."""
        arr = np.asarray(feat, dtype=np.float64).reshape(-1)
        if arr.shape[0] != self.feature_dim:
            raise ValueError(f"feature vector must have length {self.feature_dim}")

        az, el = self.angles(arr)
        with self._lock:
            rig = self.rig
        adapted = self.is_adapted

        raw_hit = rig.ray_angles_to_pixel(az, el)
        raw_x, raw_y = raw_hit.x, raw_hit.y

        if self._state_est is not None:
            now = time.monotonic() if t_sec is None else float(t_sec)
            est = self._state_est.update(
                az, el, now, blink_level=blink_level, confidence=confidence
            )
            smooth_hit = rig.ray_angles_to_pixel(est.az, est.el)
            sx, sy = smooth_hit.x, smooth_hit.y
            kin = est.kinematics.value
            active_disp = smooth_hit.connector
            active_gap = smooth_hit.in_gap
            out_az, out_el = float(est.az), float(est.el)
        else:
            sx, sy = raw_x, raw_y
            kin = GazeKinematics.FIXATION.value
            active_disp = raw_hit.connector
            active_gap = raw_hit.in_gap
            out_az, out_el = az, el

        with self._lock:
            self._last_display = active_disp
            self._last_in_gap = active_gap
            self._last_kinematics = kin
            self._last_az = out_az
            self._last_el = out_el

        return sx, sy, raw_x, raw_y, adapted


class MonocularGazePublisher:
    """Unified gaze publisher supporting monocular (10D) + binocular (20D) across N 3D displays."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        paths: AppPaths | None = None,
        camera_id: str | None = None,
        camera_mode: str | None = None,
        shm_name: str = SHM_NAME,
        min_clicks: int = 5,
        ridge_alpha: float = 1e-2,
        enable_click_adaptation: bool = True,
        publish_shm: bool = True,
        enable_overlay: bool | None = None,
        on_prediction: Callable[[MonocularPrediction], None] | None = None,
    ) -> None:
        from keyless.platform.router import get_display_layout

        self.paths = paths or current_paths()
        self.config = config or AppConfig.load(self.paths.settings_path)
        self.layout = get_display_layout()
        self.rig = DisplayRig.load(self.paths.config_dir / "display_rig.json", self.layout)
        self.camera_id = camera_id
        self.requested_mode = (camera_mode or self.config.camera_mode or "auto").lower()
        self.shm_name = shm_name
        self.min_clicks = int(min_clicks)
        self.ridge_alpha = float(ridge_alpha)
        self.enable_click_adaptation = bool(enable_click_adaptation)
        self.publish_shm = bool(publish_shm)
        self.enable_overlay = (
            bool(self.config.gaze_cursor_overlay)
            if enable_overlay is None
            else bool(enable_overlay)
        )
        self.dev_monitor_border = bool(self.config.dev_active_monitor_border)
        self.auto_rig = True
        self._on_prediction = on_prediction

        self.active_mode: str = "monocular"
        self.estimator = MonocularGazeEstimator(
            self.layout.total_width,
            self.layout.total_height,
            rig=self.rig,
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
        self.click_capture_reason = ""
        self._overlay: Any = None

        self._latest_feat: np.ndarray | None = None
        self._latest_frame_dict: dict[str, Any] | None = None
        self._latest_obs_mono: float = 0.0
        self._latest_blinking: bool = False
        self._latest_pred: MonocularPrediction | None = None
        self._frames_read: int = 0
        self._faces_detected: int = 0
        self._clicks_adapted: int = 0
        self._clicks_captured: int = 0  # raw OS clicks, before fixation pairing
        self._measured_fps: float = 0.0
        # Recent frames for fixation-aware click pairing: (t_mono, feat, blinking)
        self._recent: deque[tuple[float, np.ndarray, bool]] = deque(maxlen=120)
        self._frame_listeners: list[Callable[[float, np.ndarray, float, float, float], None]] = []
        self.calibrating = False
        self.profile_path = profile_path(self.paths.config_dir)

    def set_display_rig(self, rig: DisplayRig, *, auto_rig: bool = False) -> None:
        self.rig = rig
        self.auto_rig = bool(auto_rig)
        self.estimator.set_rig(rig, auto_rig=self.auto_rig)

    def set_dev_monitor_border(self, enabled: bool) -> None:
        self.dev_monitor_border = bool(enabled)

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
            rig=self.rig,
            auto_rig=self.auto_rig,
            feature_dim=feat_dim,
            min_clicks=self.min_clicks,
            ridge_alpha=self.ridge_alpha,
            profile=GazeProfile.load(self.profile_path),
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

        if self.enable_overlay and self.publish_shm:
            self.start_overlay()

        if self.enable_click_adaptation:
            self.start_click_capture()

        self._thread = threading.Thread(
            target=self._loop, name="keyless-gaze-pub", daemon=True
        )
        self._thread.start()

    def start_overlay(self) -> None:
        """Eye cursor fed from the shm channel: Wayland reticle or the macOS cua cursor."""
        from keyless.platform.router import current_host

        if self._overlay is not None or self._writer is None:
            return
        host = current_host()
        try:
            if host == "linux":
                from keyless.platform.linux.gaze_overlay import LinuxGazeOverlay

                ov: Any = LinuxGazeOverlay(self.paths, shm_name=self.shm_name)
            elif host == "macos":
                from keyless.platform.macos.gaze_cursor import MacGazeCursor

                ov = MacGazeCursor(self.paths.cache_dir, shm_name=self.shm_name,
                                   hold=lambda: self.calibrating)
            else:
                return
            if ov.start():
                self._overlay = ov
            else:
                logger.info("Eye cursor unavailable: %s", ov.error)
        except Exception as exc:
            logger.info("Eye cursor skipped: %s", exc)

    def stop_overlay(self) -> None:
        overlay, self._overlay = self._overlay, None
        if overlay is not None:
            overlay.stop()

    def start_click_capture(self) -> bool:
        """(Re)start the passive click listener; safe to call after a permission grant."""
        from keyless.platform.router import current_host

        if self._click_observer is not None:
            return True
        host = current_host()
        if host == "linux":
            from keyless.gaze.recorder import LinuxClickObserver

            obs: Any = LinuxClickObserver(self.on_click)
        elif host == "macos":
            from keyless.platform.macos.clicks import MacClickObserver

            obs = MacClickObserver(self.on_click)
        else:
            self.click_capture_reason = f"no click listener for {host}"
            return False
        if obs.start():
            self._click_observer = obs
            self.click_capture_reason = ""
            return True
        self.click_capture_reason = obs.unavailable_reason
        logger.info("Passive click adaptation unavailable (%s); running prior-only.", obs.unavailable_reason)
        return False

    @property
    def overlay_active(self) -> bool:
        return bool(self._overlay is not None and self._overlay.running)

    def on_click(self, x: float, y: float, *, t_click: float | None = None) -> bool:
        """Queue a physical click for fixation-aware pairing.

        Pairing runs ~150 ms later so frames on both sides of the click are
        available. Returns False when the click is ignored outright.
        """
        with self._lock:
            self._clicks_captured += 1
        if self.calibrating:
            return False
        t_c = time.monotonic() if t_click is None else float(t_click)
        timer = threading.Timer(0.15, self._pair_click, args=(float(x), float(y), t_c))
        timer.daemon = True
        timer.start()
        return True

    def pair_click_now(self, x: float, y: float, t_c: float) -> bool:
        return self._pair_click(x, y, t_c)

    def _fixation_feature(self, t_c: float) -> np.ndarray | None:
        """Median feature of the stable fixation around the click, or None.

        People look at a control before and while clicking it, but their eyes
        may already leave as the button goes down. We take the most recent
        stable run of open-eye frames in [t_c - 350 ms, t_c + 100 ms]; if the
        eye was moving throughout, there is no label.
        """
        with self._lock:
            recent = [r for r in self._recent if t_c - 0.35 <= r[0] <= t_c + 0.10]
        frames = [(t, f) for t, f, blink in recent if not blink]
        if len(frames) < 3:
            return None
        angs = np.array([self.estimator.angles(f) for _, f in frames])
        tol = math.radians(max(1.2, 2.5 * self.estimator.precision_deg))
        best: tuple[int, int] | None = None
        start = 0
        for end in range(1, len(frames) + 1):
            # Shrink the window from the left until it is a fixation.
            while start < end - 1:
                seg = angs[start:end]
                if float(np.max(np.linalg.norm(seg - np.median(seg, axis=0), axis=1))) <= tol:
                    break
                start += 1
            if end - start >= 3 and (best is None or end - start >= best[1] - best[0] - 1):
                best = (start, end)
        if best is None:
            return None
        feats = np.array([f for _, f in frames[best[0]:best[1]]])
        return np.median(feats, axis=0)

    def _pair_click(self, x: float, y: float, t_c: float) -> bool:
        feat = self._fixation_feature(t_c)
        if feat is None:
            return False
        before = self.estimator.sample_count
        count = self.estimator.add_click(feat, x, y, t=t_c)
        if count == before and self.estimator.sample_count == before:
            return False
        if self.estimator.auto_rig:
            self.rig = self.estimator.rig
            if count in (2, 4, 6, 10) or count % 10 == 0:
                try:
                    self.rig.save(self.paths.config_dir / "display_rig.json")
                except Exception:
                    pass
        with self._lock:
            self._clicks_adapted += 1
        return True

    def add_frame_listener(self, cb: Callable[[float, np.ndarray, float, float, float], None]) -> None:
        """Register ``cb(t_mono, feat, blink_level, confidence, distance_mm)`` per face frame."""
        with self._lock:
            self._frame_listeners.append(cb)

    def remove_frame_listener(self, cb: Callable[..., None]) -> None:
        with self._lock:
            if cb in self._frame_listeners:
                self._frame_listeners.remove(cb)

    def apply_calibration(self, profile: GazeProfile, rig: DisplayRig) -> None:
        """Install a pursuit result: new rig + profile prior, persisted."""
        self.rig = rig
        self.auto_rig = False
        self.estimator.set_rig(rig, auto_rig=False)
        self.estimator.apply_profile(profile)
        try:
            rig.save(self.paths.config_dir / "display_rig.json")
            profile.save(self.profile_path)
        except Exception:
            logger.warning("Could not persist gaze profile", exc_info=True)

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
                "overlay_active": self.overlay_active,
                "kinematics": self.estimator.last_kinematics,
                "blinks_suppressed": self.estimator.blinks_suppressed,
                "outliers_suppressed": self.estimator.outliers_suppressed,
                "clicks_seen": self.estimator.clicks_seen,
                "click_capture": self._click_observer is not None,
                "clicks_captured": self._clicks_captured,
                "click_capture_reason": self.click_capture_reason,
                "clicks_ignored": self.estimator.clicks_ignored,
                "latency_ms": round(self.estimator.latency_ms),
                "profile": self.estimator.profile.public() if self.estimator.profile else None,
                "calibrating": self.calibrating,
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
                blink_level = float(max(obs1.max_blink, obs2.max_blink))
                confidence = float(min(obs1.confidence_a, obs1.confidence_b, obs2.confidence_a, obs2.confidence_b))
                yaw = 0.5 * (float(obs1.yaw) + float(obs2.yaw))
                pitch = 0.5 * (float(obs1.pitch) + float(obs2.pitch))
                roll = 0.5 * (float(obs1.roll) + float(obs2.roll))
                tx = 0.5 * (float(obs1.tx) + float(obs2.tx))
                ty = 0.5 * (float(obs1.ty) + float(obs2.ty))
                tz = 0.5 * (float(obs1.tz) + float(obs2.tz))
                blink_left = max(float(obs1.blink_left), float(obs2.blink_left))
                blink_right = max(float(obs1.blink_right), float(obs2.blink_right))
                frame_dict = observation_to_frame_section(
                    obs1, self._resolved_cams[0], obs2, self._resolved_cams[1]
                )
            else:
                if obs1 is None:
                    self._publish_no_face(ts_ns, 0.0)
                    continue
                feat = obs1.to_feature_vector()
                blink_level = float(obs1.max_blink)
                confidence = float(min(obs1.confidence_a, obs1.confidence_b))
                yaw = float(obs1.yaw)
                pitch = float(obs1.pitch)
                roll = float(obs1.roll)
                tx = float(obs1.tx)
                ty = float(obs1.ty)
                tz = float(obs1.tz)
                blink_left = float(obs1.blink_left)
                blink_right = float(obs1.blink_right)
                frame_dict = observation_to_frame_section(obs1, self._resolved_cams[0])

            sx, sy, rx, ry, using_ridge = self.estimator.predict(
                feat, t_sec=t_sec, blink_level=blink_level, confidence=confidence
            )
            kin = self.estimator.last_kinematics
            blinking = blink_level >= self.estimator.blink_enter or kin in ("blink_hold", "recovery")
            flags = FLAG_FACE_DETECTED | (FLAG_BLINKING if blinking else 0) | (FLAG_DEV_MONITOR_BORDER if self.dev_monitor_border else 0)
            if self.calibrating:
                flags = 0  # hide the reticle: the stage owns the screen
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
                display=self.estimator.last_display,
                in_gap=self.estimator.last_in_gap,
                kinematics=kin,
                blinks_suppressed=self.estimator.blinks_suppressed,
                outliers_suppressed=self.estimator.outliers_suppressed,
                az=self.estimator.last_angles[0],
                el=self.estimator.last_angles[1],
                tx=tx,
                ty=ty,
                tz=tz,
                blink_left=blink_left,
                blink_right=blink_right,
            )
            now_mono = time.monotonic()
            with self._lock:
                self._recent.append((now_mono, feat, blinking))
                listeners = list(self._frame_listeners)
            for cb in listeners:
                try:
                    cb(t_sec, feat, blink_level, confidence, abs(tz) * 1000.0)
                except Exception:
                    logger.debug("frame listener failed", exc_info=True)
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
            display=self.estimator.last_display,
            in_gap=False,
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
        try:
            self.stop_overlay()
        except Exception:
            logger.warning("Eye cursor did not stop cleanly", exc_info=True)
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
