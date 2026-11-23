"""MediaPipe FaceLandmarker high-SNR feature extraction for dual-camera stereo gaze."""

from __future__ import annotations

import logging
import math
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from keyless.config import DEFAULT_LANDMARKER_MODEL_PATH, FACE_LANDMARKER_URL

logger = logging.getLogger(__name__)

# MediaPipe 478-point face mesh anatomical clusters
# Eye A (Subject's Right Eye / Left side of unflipped image)
_EYE_A_OUTER_CLUSTER = (33, 130, 246)
_EYE_A_INNER_CLUSTER = (133, 243, 173)
_EYE_A_UPPER_CLUSTER = (159, 158, 160)
_EYE_A_LOWER_CLUSTER = (145, 144, 153)
_EYE_A_IRIS_RING = (468, 469, 470, 471, 472)  # 468 = center, 469..472 = limbus cardinal points

# Eye B (Subject's Left Eye / Right side of unflipped image)
_EYE_B_INNER_CLUSTER = (362, 463, 398)
_EYE_B_OUTER_CLUSTER = (263, 359, 466)
_EYE_B_UPPER_CLUSTER = (386, 385, 387)
_EYE_B_LOWER_CLUSTER = (374, 373, 380)
_EYE_B_IRIS_RING = (473, 474, 475, 476, 477)  # 473 = center, 474..477 = limbus cardinal points

_NOSE_TIP = 1

# Weights for 5-point iris centroid (center landmark + 4 cardinal limbus points)
_IRIS_RING_WEIGHTS = np.array([0.36, 0.16, 0.16, 0.16, 0.16], dtype=np.float64)

STEREO_FEATURE_DIM = 20  # 10 features per camera x 2 cameras


@dataclass(frozen=True)
class CameraObservation:
    """Extracted gaze, head-pose, and blink/gesture features from one camera frame."""

    iris_a_dx: float
    iris_a_dy: float
    iris_b_dx: float
    iris_b_dy: float
    yaw: float
    pitch: float
    roll: float
    tx: float
    ty: float
    tz: float
    nose_x: float
    nose_y: float
    blink_left: float
    blink_right: float
    jaw_open: float
    confidence_a: float
    confidence_b: float
    # Pixel coordinates for debug overlay rendering
    iris_a_px: tuple[int, int]
    iris_b_px: tuple[int, int]
    nose_px: tuple[int, int]
    eye_corners_px: tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]

    def to_feature_vector(self) -> np.ndarray:
        return np.array(
            [
                self.iris_a_dx,
                self.iris_a_dy,
                self.iris_b_dx,
                self.iris_b_dy,
                self.yaw,
                self.pitch,
                self.roll,
                self.tx,
                self.ty,
                self.tz,
            ],
            dtype=np.float64,
        )

    @property
    def max_blink(self) -> float:
        return max(self.blink_left, self.blink_right)


def ensure_landmarker_model(model_path: Path = DEFAULT_LANDMARKER_MODEL_PATH) -> Path:
    """Download the official MediaPipe `face_landmarker.task` bundle if not cached."""
    if model_path.exists() and model_path.stat().st_size > 100_000:
        return model_path
    model_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = model_path.with_suffix(".tmp")
    urllib.request.urlretrieve(FACE_LANDMARKER_URL, tmp_path)
    tmp_path.replace(model_path)
    return model_path


def _extract_los_euler_and_translation(
    matrix: np.ndarray,
) -> tuple[float, float, float, float, float, float]:
    """Extract camera-line-of-sight `(yaw_los, pitch_los, roll, tx, ty, tz)` from 4x4 pose matrix.

    Adjusting Euler yaw/pitch by the camera viewing ray angle `atan2(tx, |tz|)` ensures that
    translating the head laterally (leaning in the chair) accounts for the oblique viewing angle
    on the eye sockets identically to head rotation.
    """
    r = matrix[:3, :3]
    sy = math.sqrt(float(r[0, 0] * r[0, 0] + r[1, 0] * r[1, 0]))
    if sy > 1e-6:
        pitch = math.atan2(float(r[2, 1]), float(r[2, 2]))
        yaw = math.atan2(float(-r[2, 0]), sy)
        roll = math.atan2(float(r[1, 0]), float(r[0, 0]))
    else:
        pitch = math.atan2(float(-r[1, 2]), float(r[1, 1]))
        yaw = math.atan2(float(-r[2, 0]), sy)
        roll = 0.0

    raw_tx = float(matrix[0, 3])
    raw_ty = float(matrix[1, 3])
    raw_tz = float(matrix[2, 3])
    depth = max(15.0, abs(raw_tz))

    ray_yaw = math.atan2(raw_tx, depth)
    ray_pitch = math.atan2(raw_ty, depth)

    yaw_los = yaw - 0.65 * ray_yaw
    pitch_los = pitch - 0.65 * ray_pitch

    tx, ty, tz = raw_tx / 100.0, raw_ty / 100.0, raw_tz / 100.0
    return yaw_los, pitch_los, roll, tx, ty, tz


def _iris_offset_high_snr(
    c_outer: np.ndarray,
    c_inner: np.ndarray,
    iris: np.ndarray,
    upper_lid: np.ndarray,
    lower_lid: np.ndarray,
    bino_v_hat: np.ndarray,
    rigid_vert_scale: float,
    horizontal_sign: float,
    blend_dx: float,
    blend_dy: float,
    squint: float,
) -> tuple[float, float, float]:
    """Compute binocular-roll-stabilized `(dx, dy)` anchored to the bony inner canthus."""
    u_local = (c_inner - c_outer) if horizontal_sign > 0 else (c_outer - c_inner)
    width = float(np.linalg.norm(u_local))
    if width < 1e-5 or rigid_vert_scale < 1e-5:
        return 0.0, 0.0, 0.0

    u_local_hat = u_local / width
    mid = 0.5 * (c_outer + c_inner)

    # 1. Horizontal offset: local palpebral midpoint + ARKit horizontal gaze blendshape
    dx_geom = float(np.dot(iris - mid, u_local_hat) / width)
    dx = dx_geom + 0.18 * blend_dx

    # 2. Vertical offset: project along the wide ~100px binocular vertical axis (bino_v_hat)
    # and anchor 70% to the bony inner canthus (c_inner) which does not move with eyelids.
    anchor_y = 0.70 * c_inner + 0.30 * c_outer
    dy_bony = float(np.dot(iris - anchor_y, bino_v_hat) / rigid_vert_scale)

    # Squint-gated upper/lower eyelid synkinesis projected on stable binocular vertical axis
    lid_mid = 0.62 * upper_lid + 0.38 * lower_lid
    dy_lid = float(np.dot(lid_mid - c_inner, bino_v_hat) / rigid_vert_scale)
    squint_gate = max(0.0, 1.0 - 1.6 * squint)

    dy = 0.68 * dy_bony + 0.22 * dy_lid * squint_gate + 0.28 * blend_dy
    return dx, dy, width


class FaceFeatureExtractor:
    """Wraps one MediaPipe FaceLandmarker in VIDEO mode per camera stream (GPU-accelerated when available)."""

    def __init__(
        self,
        model_path: Path = DEFAULT_LANDMARKER_MODEL_PATH,
        use_gpu: bool = True,
    ) -> None:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        resolved = ensure_landmarker_model(model_path)
        self.delegate_name = "CPU"
        if use_gpu and hasattr(mp_python.BaseOptions, "Delegate"):
            try:
                opts = mp_vision.FaceLandmarkerOptions(
                    base_options=mp_python.BaseOptions(
                        model_asset_path=str(resolved),
                        delegate=mp_python.BaseOptions.Delegate.GPU,
                    ),
                    running_mode=mp_vision.RunningMode.VIDEO,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                    output_face_blendshapes=True,
                    output_facial_transformation_matrixes=True,
                )
                self._landmarker = mp_vision.FaceLandmarker.create_from_options(opts)
                self.delegate_name = "GPU"
                self._last_ts_ms = -1
                return
            except Exception as exc:
                logger.warning("GPU delegate unavailable (%s); falling back to CPU.", exc)

        opts = mp_vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(resolved)),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = mp_vision.FaceLandmarker.create_from_options(opts)
        self._last_ts_ms = -1

    def process(self, bgr_frame: np.ndarray, timestamp_ns: int) -> CameraObservation | None:
        """Run FaceLandmarker on a BGR frame and return high-SNR extracted features or None."""
        import mediapipe as mp

        ts_ms = int(timestamp_ns // 1_000_000)
        if ts_ms <= self._last_ts_ms:
            ts_ms = self._last_ts_ms + 1
        self._last_ts_ms = ts_ms

        # MediaPipe's Metal delegate on macOS only accepts 4-channel textures;
        # Linux CPU delegates accept the 3-channel RGB we historically passed.
        if sys.platform == "darwin":
            data = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGBA)
            image_format = mp.ImageFormat.SRGBA
        else:
            data = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
            image_format = mp.ImageFormat.SRGB
        mp_image = mp.Image(image_format=image_format, data=data)
        result = self._landmarker.detect_for_video(mp_image, ts_ms)

        if not result.face_landmarks or len(result.face_landmarks[0]) < 478:
            return None

        lms = result.face_landmarks[0]
        h, w = bgr_frame.shape[:2]

        def cluster_mean(indices: tuple[int, ...]) -> np.ndarray:
            xs = [lms[i].x * w for i in indices]
            ys = [lms[i].y * h for i in indices]
            return np.array([sum(xs) / len(xs), sum(ys) / len(ys)], dtype=np.float64)

        def iris_centroid(indices: tuple[int, ...]) -> np.ndarray:
            pts = np.array([[lms[i].x * w, lms[i].y * h] for i in indices], dtype=np.float64)
            return np.dot(_IRIS_RING_WEIGHTS, pts)

        a_out = cluster_mean(_EYE_A_OUTER_CLUSTER)
        a_in = cluster_mean(_EYE_A_INNER_CLUSTER)
        a_up = cluster_mean(_EYE_A_UPPER_CLUSTER)
        a_lo = cluster_mean(_EYE_A_LOWER_CLUSTER)
        a_iris = iris_centroid(_EYE_A_IRIS_RING)

        b_in = cluster_mean(_EYE_B_INNER_CLUSTER)
        b_out = cluster_mean(_EYE_B_OUTER_CLUSTER)
        b_up = cluster_mean(_EYE_B_UPPER_CLUSTER)
        b_lo = cluster_mean(_EYE_B_LOWER_CLUSTER)
        b_iris = iris_centroid(_EYE_B_IRIS_RING)

        nose = lms[_NOSE_TIP]

        width_a_raw = float(np.linalg.norm(a_in - a_out))
        width_b_raw = float(np.linalg.norm(b_out - b_in))
        if width_a_raw < 4.0 or width_b_raw < 4.0:
            return None

        if result.facial_transformation_matrixes:
            mat = np.asarray(result.facial_transformation_matrixes[0], dtype=np.float64)
            yaw, pitch, roll, tx, ty, tz = _extract_los_euler_and_translation(mat)
        else:
            yaw = pitch = roll = tx = ty = tz = 0.0

        blend_map: dict[str, float] = {}
        if result.face_blendshapes:
            for cat in result.face_blendshapes[0]:
                blend_map[cat.category_name] = float(cat.score)

        # Neural ARKit eye-gaze differentials
        blend_a_dx = blend_map.get("eyeLookInRight", 0.0) - blend_map.get("eyeLookOutRight", 0.0)
        blend_a_dy = blend_map.get("eyeLookDownRight", 0.0) - blend_map.get("eyeLookUpRight", 0.0)
        squint_a = blend_map.get("eyeSquintRight", 0.0)

        blend_b_dx = blend_map.get("eyeLookOutLeft", 0.0) - blend_map.get("eyeLookInLeft", 0.0)
        blend_b_dy = blend_map.get("eyeLookDownLeft", 0.0) - blend_map.get("eyeLookUpLeft", 0.0)
        squint_b = blend_map.get("eyeSquintLeft", 0.0)

        # Yaw-invariant binocular scale
        inter_canthal = float(np.linalg.norm(b_in - a_in))
        rms_eye_width = math.sqrt(0.5 * (width_a_raw * width_a_raw + width_b_raw * width_b_raw))
        cos_yaw = max(0.72, math.cos(yaw))
        rigid_vert_scale = 0.65 * (rms_eye_width / cos_yaw) + 0.35 * inter_canthal

        # Wide ~100px binocular axis across both eyes (4.5x more stable than single-eye corner tilt)
        bino_vec = (0.5 * (b_in + b_out)) - (0.5 * (a_in + a_out))
        bino_len = max(1e-5, float(np.linalg.norm(bino_vec)))
        bino_u_hat = bino_vec / bino_len
        bino_v_hat = np.array([-bino_u_hat[1], bino_u_hat[0]], dtype=np.float64)

        a_dx, a_dy, width_a = _iris_offset_high_snr(
            c_outer=a_out,
            c_inner=a_in,
            iris=a_iris,
            upper_lid=a_up,
            lower_lid=a_lo,
            bino_v_hat=bino_v_hat,
            rigid_vert_scale=rigid_vert_scale,
            horizontal_sign=1.0,
            blend_dx=blend_a_dx,
            blend_dy=blend_a_dy,
            squint=squint_a,
        )
        b_dx, b_dy, width_b = _iris_offset_high_snr(
            c_outer=b_out,
            c_inner=b_in,
            iris=b_iris,
            upper_lid=b_up,
            lower_lid=b_lo,
            bino_v_hat=bino_v_hat,
            rigid_vert_scale=rigid_vert_scale,
            horizontal_sign=-1.0,
            blend_dx=blend_b_dx,
            blend_dy=blend_b_dy,
            squint=squint_b,
        )

        ratio_ab = min(width_a, width_b) / max(width_a, width_b)
        conf_a = 1.0 if width_a >= width_b else ratio_ab**1.5
        conf_b = 1.0 if width_b >= width_a else ratio_ab**1.5

        return CameraObservation(
            iris_a_dx=a_dx,
            iris_a_dy=a_dy,
            iris_b_dx=b_dx,
            iris_b_dy=b_dy,
            yaw=yaw,
            pitch=pitch,
            roll=roll,
            tx=tx,
            ty=ty,
            tz=tz,
            nose_x=float(nose.x),
            nose_y=float(nose.y),
            blink_left=blend_map.get("eyeBlinkLeft", 0.0),
            blink_right=blend_map.get("eyeBlinkRight", 0.0),
            jaw_open=blend_map.get("jawOpen", 0.0),
            confidence_a=conf_a,
            confidence_b=conf_b,
            iris_a_px=(int(round(a_iris[0])), int(round(a_iris[1]))),
            iris_b_px=(int(round(b_iris[0])), int(round(b_iris[1]))),
            nose_px=(int(round(nose.x * w)), int(round(nose.y * h))),
            eye_corners_px=(
                (int(round(a_out[0])), int(round(a_out[1]))),
                (int(round(a_in[0])), int(round(a_in[1]))),
                (int(round(b_in[0])), int(round(b_in[1]))),
                (int(round(b_out[0])), int(round(b_out[1]))),
            ),
        )

    def close(self) -> None:
        self._landmarker.close()


def _stabilize_occluded_eye(obs: CameraObservation) -> np.ndarray:
    """Softly pull a nose-occluded far eye toward the high-confidence near eye under head yaw."""
    vec = obs.to_feature_vector()
    ca, cb = obs.confidence_a, obs.confidence_b
    if ca < 0.85 and cb >= 0.95:
        w = max(0.35, ca)
        vec[0] = w * obs.iris_a_dx + (1.0 - w) * obs.iris_b_dx
        vec[1] = w * obs.iris_a_dy + (1.0 - w) * obs.iris_b_dy
    elif cb < 0.85 and ca >= 0.95:
        w = max(0.35, cb)
        vec[2] = w * obs.iris_b_dx + (1.0 - w) * obs.iris_a_dx
        vec[3] = w * obs.iris_b_dy + (1.0 - w) * obs.iris_a_dy
    return vec


def build_stereo_feature_vector(
    obs1: CameraObservation | None,
    obs2: CameraObservation | None,
) -> tuple[np.ndarray, bool]:
    """Concatenate deterministic `[cam1, cam2]` 20-D stereo feature vector with occlusion stabilization."""
    v1 = _stabilize_occluded_eye(obs1) if obs1 is not None else np.zeros(10, dtype=np.float64)
    v2 = _stabilize_occluded_eye(obs2) if obs2 is not None else np.zeros(10, dtype=np.float64)
    both_valid = obs1 is not None and obs2 is not None
    return np.concatenate([v1, v2]), both_valid
