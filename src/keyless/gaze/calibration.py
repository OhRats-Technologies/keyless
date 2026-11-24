"""Pursuit warm-up session: drives the stage, collects frames, solves, installs.

State machine (``status()["state"]``):

    starting -> intro -> running <-> paused -> solving -> done | retry | failed
                                   \\-> cancelled (any time)

- The plan clock only advances while a face is visible. Losing the face for
  250 ms pauses the target where it is (the stage dims it); the run resumes
  400 ms after the face returns. Frames within 300 ms of a resume are dropped.
- ``mode`` is ``"overlay"`` when the native stage covers every display, else
  ``"browser"``: the web page renders the stimulus full-screen on its own
  display and the plan covers that display only.
- On success the result is installed as the learner prior + display rig and
  persisted; it is never a startup requirement.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from keyless.gaze.displays import DisplayRig
from keyless.gaze.profile import GazeProfile
from keyless.gaze.pursuit import FrameSample, PursuitPlan, PursuitResult, build_plan, solve_pursuit
from keyless.gaze.stage import (
    STAGE_ACTIVE,
    STAGE_PAUSED,
    STAGE_TARGET,
    LinuxStageOverlay,
    MacStageOverlay,
    StageWriter,
)

logger = logging.getLogger(__name__)

TICK_S = 1.0 / 120.0
FADE_IN_S = 0.6
FADE_OUT_S = 0.5
FACE_LOST_S = 0.25
FACE_BACK_S = 0.4
RESUME_DROP_S = 0.3
BURST_S = 0.38


class CalibrationSession:
    def __init__(
        self,
        publisher: Any,
        *,
        connector: str | None = None,
        use_overlay: bool = True,
    ) -> None:
        self.pub = publisher
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.state = "starting"
        self.message = ""
        self.mode = "overlay"
        self.result: PursuitResult | None = None
        self._frames: list[FrameSample] = []
        # Running intervals mapping monotonic time -> plan time: (mono0, plan0)
        self._interval: tuple[float, float] | None = None
        self._resumed_at = 0.0
        self._t_plan = 0.0
        self._target = (0.0, 0.0, 0.0, "")
        self._face_ok = False
        self._burst = -1.0

        base_rig: DisplayRig = publisher.estimator._base_rig
        self._overlay: LinuxStageOverlay | MacStageOverlay | None = None
        self._writer: StageWriter | None = None
        if use_overlay:
            try:
                from keyless.platform.router import current_host

                self._writer = StageWriter()
                stage_cls = MacStageOverlay if current_host() == "macos" else LinuxStageOverlay
                ov = stage_cls(publisher.paths)
                if ov.start():
                    self._overlay = ov
                else:
                    logger.info("Stage overlay unavailable (%s); browser stage", ov.error)
            except Exception as exc:
                logger.info("Stage overlay failed: %s", exc)
        if self._overlay is None:
            self.mode = "browser"
            planes = [p for p in base_rig.planes if connector and p.connector == connector]
            if not planes:
                planes = [p for p in base_rig.planes if p.is_primary] or [base_rig.planes[0]]
            base_rig = DisplayRig(planes[:1])
        self.base_rig = base_rig
        # Browser stage: labels assume the page viewport is exactly the display,
        # so the run only advances while the page reports true full screen.
        self._viewport_ok = self.mode != "browser"
        dist = 600.0
        pred = publisher.latest_prediction()
        if pred is not None and 0.25 < abs(getattr(pred, "tz", 0.0)) < 1.5:
            dist = abs(pred.tz) * 1000.0
        self.distance_mm = dist
        self.plan: PursuitPlan = build_plan(base_rig, distance_mm=dist)
        self._thread = threading.Thread(target=self._run, name="keyless-pursuit", daemon=True)

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.pub.calibrating = True
        self.pub.add_frame_listener(self._on_frame)
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()
        with self._lock:
            if self.state not in ("done", "retry", "failed"):
                self.state = "cancelled"

    @property
    def finished(self) -> bool:
        return self.state in ("done", "retry", "failed", "cancelled")

    # --- frames -----------------------------------------------------------

    def _on_frame(self, t_mono: float, feat, blink: float, conf: float, dist_mm: float) -> None:
        with self._lock:
            iv = self._interval
            if iv is None or self.state != "running":
                return
            if t_mono - self._resumed_at < RESUME_DROP_S and self._resumed_at > iv[0]:
                return
            t_plan = iv[1] + (t_mono - iv[0])
            if t_plan < 0.0:
                return
            self._frames.append(FrameSample(t_plan, feat.copy(), float(blink), float(conf), float(dist_mm)))

    def set_viewport(self, fullscreen: bool) -> None:
        """Browser stage only: the page reports whether its viewport covers the display."""
        self._viewport_ok = bool(fullscreen) or self.mode != "browser"

    def _face_visible(self) -> bool:
        pred = self.pub.latest_prediction()
        if pred is None or not pred.face_detected:
            return False
        return (time.monotonic_ns() - int(pred.timestamp_ns)) < 300_000_000

    # --- main loop --------------------------------------------------------

    def _write(self, x: float, y: float, size: float, dim: float, flags: int, phase: float) -> None:
        if self._writer is not None:
            self._writer.write(x, y, size=size, phase=phase, dim=dim, burst=self._burst, flags=flags)

    def _run(self) -> None:
        try:
            self._run_inner()
        except Exception as exc:  # never leave the screen dimmed
            logger.exception("Pursuit calibration crashed")
            with self._lock:
                self.state = "failed"
                self.message = str(exc)
        finally:
            self._teardown()

    def _run_inner(self) -> None:
        t0 = time.monotonic()
        first = self.plan.segments[0]
        cx, cy = first.p0
        with self._lock:
            self.state = "intro"
            self._target = (cx, cy, 0.0, first.connector)

        # Fade the theatre in.
        while not self._stop.is_set():
            u = (time.monotonic() - t0) / FADE_IN_S
            if u >= 1.0:
                break
            self._write(cx, cy, 0.0, u, STAGE_ACTIVE, time.monotonic() - t0)
            time.sleep(TICK_S)

        lost_since: float | None = None
        back_since: float | None = None
        now = time.monotonic()
        with self._lock:
            self.state = "running"
            self._interval = (now, 0.0)
            self._resumed_at = now
        last_seg = None
        burst_t0: float | None = None

        while not self._stop.is_set():
            now = time.monotonic()
            face = self._face_visible() and self._viewport_ok
            with self._lock:
                state = self.state
                iv = self._interval
            if state == "running":
                if face:
                    lost_since = None
                else:
                    lost_since = lost_since or now
                    if now - lost_since >= FACE_LOST_S:
                        with self._lock:
                            self._t_plan = iv[1] + (now - iv[0])
                            self._interval = None
                            self.state = "paused"
                            self.message = "face_lost" if self._viewport_ok else "not_fullscreen"
                        back_since = None
                        continue
                t_plan = iv[1] + (now - iv[0])
            else:  # paused
                t_plan = self._t_plan
                if face:
                    back_since = back_since or now
                    if now - back_since >= FACE_BACK_S:
                        with self._lock:
                            self._interval = (now, self._t_plan)
                            self._resumed_at = now
                            self.state = "running"
                            self.message = ""
                        lost_since = None
                        continue
                else:
                    back_since = None

            if t_plan >= self.plan.duration:
                break
            x, y, size, seg = self.plan.at(t_plan)
            if seg is not last_seg:
                if last_seg is not None and last_seg.kind == "hold":
                    burst_t0 = now
                last_seg = seg
            self._burst = -1.0
            if burst_t0 is not None:
                b = (now - burst_t0) / BURST_S
                self._burst = b if b < 1.0 else -1.0
                if b >= 1.0:
                    burst_t0 = None
            flags = STAGE_ACTIVE | STAGE_TARGET | (STAGE_PAUSED if state == "paused" else 0)
            with self._lock:
                self._t_plan = t_plan
                self._face_ok = face
                self._target = (x, y, size, seg.connector if seg else "")
            self._write(x, y, size, 1.0, flags, now - t0)
            time.sleep(TICK_S)

        if self._stop.is_set():
            return

        # Fade out while solving.
        with self._lock:
            self.state = "solving"
            self._interval = None
            frames = list(self._frames)
        fade_t0 = time.monotonic()
        while time.monotonic() - fade_t0 < FADE_OUT_S:
            u = (time.monotonic() - fade_t0) / FADE_OUT_S
            self._write(0.0, 0.0, 0.0, 1.0 - u, STAGE_ACTIVE, time.monotonic() - t0)
            time.sleep(TICK_S)

        result = solve_pursuit(self.plan, frames, self.base_rig, distance_mm=self.distance_mm)
        if result.ok and result.weights is not None and result.rig is not None:
            full_rig = self._merge_rig(result.rig)
            profile = GazeProfile(
                feature_dim=int(result.weights.shape[0] - 1),
                M0=result.weights.tolist(),
                P0=(result.covariance if result.covariance is not None else _default_cov(result)).tolist(),
                latency_ms=float(result.latency_ms),
                blink_enter=float(result.blink_enter),
                blink_exit=float(result.blink_exit),
                precision_deg=float(result.precision_deg) if math.isfinite(result.precision_deg) else 1.0,
                accuracy_deg=float(result.accuracy_deg),
                accuracy_px=float(result.accuracy_px),
                grade=result.grade,
                rig_from_pursuit=self.mode == "overlay",
            )
            self.pub.apply_calibration(profile, full_rig)
        with self._lock:
            self.result = result
            self.state = "done" if result.ok else "retry"
            self.message = result.reason

    def _merge_rig(self, solved: DisplayRig) -> DisplayRig:
        """Browser mode solves one display; keep the others from the current rig."""
        if self.mode == "overlay":
            return solved
        current = {p.connector: p for p in self.pub.estimator._base_rig.planes}
        for p in solved.planes:
            current[p.connector] = p
        return DisplayRig(list(current.values()))

    def _teardown(self) -> None:
        try:
            self.pub.remove_frame_listener(self._on_frame)
        except Exception:
            pass
        self.pub.calibrating = False
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._overlay is not None:
            self._overlay.stop()
            self._overlay = None

    # --- status -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            x, y, size, conn = self._target
            plane = next((p for p in self.base_rig.planes if p.connector == conn), None)
            u = v = None
            if plane is not None:
                u, v = plane.pixel_to_uv(x, y)
            out: dict[str, Any] = {
                "state": self.state,
                "mode": self.mode,
                "message": self.message,
                "progress": round(self.plan.progress(self._t_plan), 4),
                "duration": round(self.plan.duration, 1),
                "face": self._face_ok,
                "frames": len(self._frames),
                "displays": self.plan.order,
                "target": {
                    "x": round(x, 1),
                    "y": round(y, 1),
                    "size": round(size, 3),
                    "u": None if u is None else round(u, 4),
                    "v": None if v is None else round(v, 4),
                    "connector": conn,
                    "burst": round(self._burst, 3),
                },
            }
            if self.result is not None:
                out["result"] = self.result.summary()
            return out


def _default_cov(result: PursuitResult):
    import numpy as np

    assert result.weights is not None
    return np.eye(result.weights.shape[0]) * 0.05


__all__ = ["CalibrationSession"]
