"""60Hz hybrid saccade-jump + One-Euro head-gimbal cursor controller."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from keyless.config import AppConfig, get_display_layout
from keyless.input.pointer import DryRunPointerBackend, PointerBackend
from keyless.platform.router import create_pointer_backend, resolve_camera_config
from keyless.ipc.state import (
    FLAG_BLINKING,
    FLAG_FACE_DETECTED,
    FLAG_SACCADE_JUMP,
    SharedGazeWriter,
)
from keyless.vision.calibrator import StereoGazeModel
from keyless.vision.geo_gaze import GeoGazeModel, GeoGazeTracker
from keyless.vision.landmarks import (
    CameraObservation,
    build_stereo_feature_vector,
)
from keyless.vision.one_euro import OneEuroFilter2D
from keyless.vision.stereo_capture import StereoCapture


@dataclass
class GimbalStepResult:
    """Output of a single 60Hz cursor/gaze update tick."""

    gaze_x: float
    gaze_y: float
    cursor_x: float
    cursor_y: float
    flags: int
    jumped: bool
    rel_dx: float
    rel_dy: float
    stereo_feat: np.ndarray | None = None
    yaw_deg: float = 0.0


class GazeGimbalController:
    """Pure state machine turning 30Hz gaze into stable 60Hz absolute pointer output."""

    def __init__(
        self,
        screen_width: int,
        screen_height: int,
        saccade_threshold_px: float = 120.0,
        saccade_hold_ms: float = 65.0,
        gimbal_gain: float = 0.0,
        min_cutoff: float = 0.85,
        beta: float = 0.0025,
        gimbal_deadband_px: float = 12.0,
        move_cursor: bool = True,
    ) -> None:
        self.screen_width = float(screen_width)
        self.screen_height = float(screen_height)
        self.saccade_threshold_px = float(saccade_threshold_px)
        # Enforce at least 60ms so two distinct 30Hz camera frames must agree before jumping
        self.saccade_hold_sec = max(0.060, float(saccade_hold_ms) / 1000.0)
        self.gimbal_gain = float(gimbal_gain)
        self.gimbal_deadband_px = max(8.0, float(gimbal_deadband_px))
        self.move_cursor = bool(move_cursor)

        self._gaze_filter = OneEuroFilter2D(min_cutoff=min_cutoff, beta=beta)

        self.cursor_x = self.screen_width * 0.5
        self.cursor_y = self.screen_height * 0.5
        self._raw_gaze_x = self.cursor_x
        self._raw_gaze_y = self.cursor_y
        self._prev_raw_gaze: tuple[float, float] | None = None
        self._smooth_gaze_x = self.cursor_x
        self._smooth_gaze_y = self.cursor_y

        self._vision_seq = 0
        self._saccade_candidate: tuple[float, float] | None = None
        self._saccade_start_t: float = 0.0
        self._saccade_start_seq: int = 0
        self._has_fixation = False
        self._state_lock = threading.Lock()
        self._hold_until: float = 0.0

    def _clamp_screen(self, x: float, y: float) -> tuple[float, float]:
        return (
            max(0.0, min(self.screen_width - 1.0, x)),
            max(0.0, min(self.screen_height - 1.0, y)),
        )

    def warp_and_hold(self, x: float, y: float, hold_sec: float = 0.25) -> None:
        """Snap controller state to `(x, y)` and pause gaze drift during a voice click."""
        with self._state_lock:
            cx, cy = self._clamp_screen(x, y)
            self.cursor_x, self.cursor_y = cx, cy
            self._raw_gaze_x, self._raw_gaze_y = cx, cy
            self._smooth_gaze_x, self._smooth_gaze_y = cx, cy
            self._gaze_filter.reset()
            self._saccade_candidate = None
            self._hold_until = time.monotonic() + max(0.0, float(hold_sec))

    def update_vision(
        self,
        raw_gaze_xy: tuple[float, float] | None,
        nose_xy: tuple[float, float] | None,
        is_blinking: bool,
        t_sec: float,
    ) -> None:
        """Ingest a 30Hz vision observation."""
        if is_blinking or raw_gaze_xy is None:
            return
        with self._state_lock:
            if self._hold_until > 0.0 and time.monotonic() < self._hold_until:
                return
            rx, ry = self._clamp_screen(raw_gaze_xy[0], raw_gaze_xy[1])
            self._prev_raw_gaze = (
                (self._raw_gaze_x, self._raw_gaze_y) if self._has_fixation else (rx, ry)
            )
            self._raw_gaze_x, self._raw_gaze_y = rx, ry
            sx, sy = self._gaze_filter(rx, ry, t_sec)
            self._smooth_gaze_x, self._smooth_gaze_y = self._clamp_screen(sx, sy)
            self._vision_seq += 1
            self._has_fixation = True

    def step_output(
        self,
        pointer: PointerBackend,
        face_detected: bool,
        is_blinking: bool,
        t_sec: float,
    ) -> GimbalStepResult:
        """Advance the 60Hz output loop using drift-free absolute positioning."""
        flags = 0
        if face_detected:
            flags |= FLAG_FACE_DETECTED
        if is_blinking:
            flags |= FLAG_BLINKING

        with self._state_lock:
            if self._hold_until > 0.0:
                if time.monotonic() < self._hold_until:
                    return GimbalStepResult(
                        gaze_x=self._smooth_gaze_x,
                        gaze_y=self._smooth_gaze_y,
                        cursor_x=self.cursor_x,
                        cursor_y=self.cursor_y,
                        flags=flags,
                        jumped=False,
                        rel_dx=0.0,
                        rel_dy=0.0,
                    )
                self._hold_until = 0.0

            if not self.move_cursor:
                self.cursor_x, self.cursor_y = self._smooth_gaze_x, self._smooth_gaze_y
                return GimbalStepResult(
                    gaze_x=self._smooth_gaze_x,
                    gaze_y=self._smooth_gaze_y,
                    cursor_x=self.cursor_x,
                    cursor_y=self.cursor_y,
                    flags=flags,
                    jumped=False,
                    rel_dx=0.0,
                    rel_dy=0.0,
                )

            if not face_detected or is_blinking or not self._has_fixation:
                self._saccade_candidate = None
                return GimbalStepResult(
                    gaze_x=self._smooth_gaze_x,
                    gaze_y=self._smooth_gaze_y,
                    cursor_x=self.cursor_x,
                    cursor_y=self.cursor_y,
                    flags=flags,
                    jumped=False,
                    rel_dx=0.0,
                    rel_dy=0.0,
                )

            # Confirm saccade only after >= 2 distinct camera frames agree (stops 1-frame jitter teleports)
            dist = math.hypot(self._raw_gaze_x - self.cursor_x, self._raw_gaze_y - self.cursor_y)
            if dist > self.saccade_threshold_px:
                if self._saccade_candidate is None:
                    self._saccade_candidate = (self._raw_gaze_x, self._raw_gaze_y)
                    self._saccade_start_t = t_sec
                    self._saccade_start_seq = self._vision_seq
                else:
                    cand_dist = math.hypot(
                        self._raw_gaze_x - self._saccade_candidate[0],
                        self._raw_gaze_y - self._saccade_candidate[1],
                    )
                    if cand_dist > max(140.0, self.saccade_threshold_px * 1.2):
                        self._saccade_candidate = (self._raw_gaze_x, self._raw_gaze_y)
                        self._saccade_start_t = t_sec
                        self._saccade_start_seq = self._vision_seq
                    elif (
                        (self._vision_seq - self._saccade_start_seq) >= 2
                        and (t_sec - self._saccade_start_t) >= self.saccade_hold_sec
                    ):
                        prev_rx, prev_ry = self._prev_raw_gaze or (self._raw_gaze_x, self._raw_gaze_y)
                        tx = 0.5 * (self._raw_gaze_x + prev_rx)
                        ty = 0.5 * (self._raw_gaze_y + prev_ry)
                        self.cursor_x, self.cursor_y = self._clamp_screen(tx, ty)
                        self._gaze_filter.reset()
                        self._gaze_filter(self.cursor_x, self.cursor_y, t_sec)
                        self._smooth_gaze_x, self._smooth_gaze_y = self.cursor_x, self.cursor_y
                        pointer.move_absolute(self.cursor_x, self.cursor_y)
                        self._saccade_candidate = None
                        flags |= FLAG_SACCADE_JUMP
                        return GimbalStepResult(
                            gaze_x=self._smooth_gaze_x,
                            gaze_y=self._smooth_gaze_y,
                            cursor_x=self.cursor_x,
                            cursor_y=self.cursor_y,
                            flags=flags,
                            jumped=True,
                            rel_dx=0.0,
                            rel_dy=0.0,
                        )
            else:
                self._saccade_candidate = None

            # Smoothly glide cursor via move_absolute (zero Wayland pointer-acceleration drift)
            dx = self._smooth_gaze_x - self.cursor_x
            dy = self._smooth_gaze_y - self.cursor_y
            err = math.hypot(dx, dy)
            if err < self.gimbal_deadband_px:
                return GimbalStepResult(
                    gaze_x=self._smooth_gaze_x,
                    gaze_y=self._smooth_gaze_y,
                    cursor_x=self.cursor_x,
                    cursor_y=self.cursor_y,
                    flags=flags,
                    jumped=False,
                    rel_dx=0.0,
                    rel_dy=0.0,
                )

            step_x = dx * 0.28
            step_y = dy * 0.28
            self.cursor_x, self.cursor_y = self._clamp_screen(
                self.cursor_x + step_x, self.cursor_y + step_y
            )
            pointer.move_absolute(self.cursor_x, self.cursor_y)
            return GimbalStepResult(
                gaze_x=self._smooth_gaze_x,
                gaze_y=self._smooth_gaze_y,
                cursor_x=self.cursor_x,
                cursor_y=self.cursor_y,
                flags=flags,
                jumped=False,
                rel_dx=step_x,
                rel_dy=step_y,
            )


def _averaged_nose(
    o1: CameraObservation | None,
    o2: CameraObservation | None,
) -> tuple[float, float] | None:
    pts: list[tuple[float, float]] = []
    if o1 is not None:
        pts.append((o1.nose_x, o1.nose_y))
    if o2 is not None:
        pts.append((o2.nose_x, o2.nose_y))
    if not pts:
        return None
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def _eye_cursor_sink(
    config: AppConfig,
    action_pointer: PointerBackend,
    dry_run: bool,
) -> tuple[PointerBackend, object | None, bool]:
    """Pick the eye-cursor sink and say whether the gaze loop may move it."""
    from keyless.platform.cua.agent_cursor import create_gaze_sink

    return create_gaze_sink(config, action_pointer, dry_run)


def run_vision_loop(
    config: AppConfig,
    model: StereoGazeModel | GeoGazeModel,
    dry_run: bool = False,
    duration_sec: float | None = None,
    pointer: PointerBackend | None = None,
    on_step: Callable[[GimbalStepResult], None] | None = None,
    stop_event: threading.Event | None = None,
    on_controller_ready: Callable[[GazeGimbalController], None] | None = None,
    on_tracker_ready: Callable[[GeoGazeTracker], None] | None = None,
    predict_gaze: Callable[[np.ndarray, float], tuple[float, float]] | None = None,
) -> None:
    """Run the 30Hz dual-camera vision + 60Hz cursor/IPC loop until interrupted."""
    config.camera_mode, config.cam1_by_id, config.cam2_by_id = resolve_camera_config(
        config.camera_mode, config.cam1_by_id, config.cam2_by_id
    )
    if model.cam1_by_id != config.cam1_by_id or model.cam2_by_id != config.cam2_by_id:
        raise ValueError(
            "Calibration camera IDs do not match active config! "
            f"Calibrated=({model.cam1_by_id}, {model.cam2_by_id}) vs "
            f"Config=({config.cam1_by_id}, {config.cam2_by_id}). Re-run calibration."
        )

    layout = get_display_layout()
    tracker: GeoGazeTracker | None = None
    if isinstance(model, GeoGazeModel):
        from keyless.vision.geo_gaze import monitor_specs_from_layout

        model.with_layout(monitor_specs_from_layout(layout))
        tracker = GeoGazeTracker(
            model,
            enable_chase=False,  # Stop unverified scan patterns from poisoning camera pose EKF
            enable_ekf=bool(getattr(config, "gaze_online_ekf", False)),
        )
        if on_tracker_ready is not None:
            on_tracker_ready(tracker)
    owns_pointer = pointer is None
    active_pointer: PointerBackend = (
        pointer
        if pointer is not None
        else (
            DryRunPointerBackend(layout.total_width, layout.total_height)
            if dry_run
            else create_pointer_backend(
                layout.total_width, layout.total_height,
                action_backend=getattr(config, "action_backend", "native"),
            )
        )
    )
    gaze_pointer, overlay, move_eye_cursor = _eye_cursor_sink(
        config, active_pointer, dry_run
    )
    shm_writer = SharedGazeWriter()
    controller = GazeGimbalController(
        screen_width=layout.total_width,
        screen_height=layout.total_height,
        saccade_threshold_px=config.saccade_threshold_px,
        saccade_hold_ms=config.saccade_hold_ms,
        gimbal_gain=config.gimbal_gain,
        min_cutoff=config.one_euro_min_cutoff,
        beta=config.one_euro_beta,
        move_cursor=move_eye_cursor,
    )
    if on_controller_ready is not None:
        on_controller_ready(controller)

    last_seq1 = -1
    last_seq2 = -1
    last_face_detected = False
    last_blinking = False
    last_stereo_feat: np.ndarray | None = None
    last_yaw_deg = 0.0
    tick_period = 1.0 / 60.0
    start_t = time.monotonic()

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
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                loop_start = time.monotonic()
                if duration_sec is not None and (loop_start - start_t) >= duration_sec:
                    break

                pair = stereo.read_pair(timeout_s=0.02)
                if pair is not None and (pair.cam1.seq != last_seq1 or pair.cam2.seq != last_seq2):
                    last_seq1 = pair.cam1.seq
                    last_seq2 = pair.cam2.seq
                    o1 = pair.cam1.observation
                    o2 = pair.cam2.observation
                    last_face_detected = o1 is not None or o2 is not None
                    yaws = [math.degrees(o.yaw) for o in (o1, o2) if o is not None]
                    if yaws:
                        last_yaw_deg = sum(yaws) / len(yaws)
                    last_blinking = (
                        (o1 is not None and o1.max_blink >= config.blink_threshold)
                        or (o2 is not None and o2.max_blink >= config.blink_threshold)
                    )

                    raw_gaze: tuple[float, float] | None = None
                    if tracker is not None:
                        if o1 is not None or o2 is not None:
                            feat, _ = build_stereo_feature_vector(o1, o2)
                            last_stereo_feat = feat
                            if not last_blinking:
                                pred = tracker.update(feat, loop_start)
                                # Use FixationSegmenter's denoised fixation mean instead of raw single-frame pred
                                out = tracker.smoothed if tracker.smoothed is not None else pred
                                if np.all(np.isfinite(out)):
                                    raw_gaze = (
                                        float(out[0]) + model.session_offset_x,
                                        float(out[1]) + model.session_offset_y,
                                    )
                    elif o1 is not None and o2 is not None:
                        feat, both_ok = build_stereo_feature_vector(o1, o2)
                        if both_ok:
                            last_stereo_feat = feat
                            raw_gaze = (
                                predict_gaze(feat, loop_start)
                                if predict_gaze is not None
                                else model.predict(feat)
                            )

                    controller.update_vision(
                        raw_gaze_xy=raw_gaze,
                        nose_xy=_averaged_nose(o1, o2),
                        is_blinking=last_blinking,
                        t_sec=loop_start,
                    )

                step = controller.step_output(
                    pointer=gaze_pointer,
                    face_detected=last_face_detected,
                    is_blinking=last_blinking,
                    t_sec=loop_start,
                )
                step.stereo_feat = last_stereo_feat
                step.yaw_deg = last_yaw_deg
                shm_writer.write(step.cursor_x, step.cursor_y, step.flags)
                if on_step is not None:
                    on_step(step)

                elapsed = time.monotonic() - loop_start
                sleep_s = tick_period - elapsed
                if sleep_s > 0.0005:
                    time.sleep(sleep_s)
    finally:
        shm_writer.close(unlink=True)
        if overlay is not None:
            overlay.close()
        if owns_pointer:
            active_pointer.close()
