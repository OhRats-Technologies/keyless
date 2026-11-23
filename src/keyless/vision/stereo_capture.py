"""Threaded low-latency dual-camera stereo capture with parallel GPU landmarking."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from keyless.vision.landmarks import CameraObservation, FaceFeatureExtractor
from keyless.platform.linux.pipewire import open_pipewire_capture, pipewire_available
from keyless.platform.linux.v4l2 import CameraLockReport, open_locked_capture

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraFrame:
    """Single timestamped camera frame and optional extracted facial observation."""

    image: np.ndarray | None
    timestamp_ns: int
    seq: int
    measured_fps: float
    observation: CameraObservation | None = None


@dataclass(frozen=True)
class StereoFrame:
    """Paired latest frames/observations from Camera 1 and Camera 2."""

    cam1: CameraFrame
    cam2: CameraFrame

    @property
    def skew_ms(self) -> float:
        return abs(self.cam1.timestamp_ns - self.cam2.timestamp_ns) / 1_000_000.0


class _CameraReaderThread:
    """Continuously drains one V4L2 device and optionally runs GPU MediaPipe on-thread."""

    def __init__(
        self,
        by_id_path: str,
        width: int,
        height: int,
        fps: int,
        exposure_absolute: int,
        gain: int | None,
        extract_landmarks: bool = False,
        use_gpu: bool = True,
        keep_images: bool = True,
        manual_exposure: bool = True,
        capture_backend: str = "v4l2",
        capture_face_mesh: bool = False,
    ) -> None:
        if capture_backend == "opencv":
            from keyless.platform.macos.capture import open_opencv_capture

            self.cap, self.report = open_opencv_capture(
                by_id_path, width=width, height=height, fps=fps
            )
        elif capture_backend == "pipewire":
            self.cap, self.report = open_pipewire_capture(by_id_path)
        else:
            try:
                self.cap, self.report = open_locked_capture(
                    by_id_path=by_id_path,
                    width=width,
                    height=height,
                    fps=fps,
                    exposure_absolute=exposure_absolute,
                    gain=gain,
                    manual_exposure=manual_exposure,
                )
            except Exception as exc:
                if pipewire_available():
                    logger.warning(
                        "Exclusive V4L2 camera open failed (%s); falling back to shared PipeWire capture.",
                        exc,
                    )
                    self.cap, self.report = open_pipewire_capture(by_id_path)
                else:
                    raise RuntimeError(
                        f"Camera is unavailable or busy in another app ({exc}). "
                        "Close the other app or enable 'Share camera' in Settings."
                    ) from exc
        self._extract_landmarks = extract_landmarks
        self._use_gpu = self._use_gpu = use_gpu
        self._keep_images = keep_images
        self._capture_face_mesh = capture_face_mesh
        self.delegate_name = "OFF"
        self._lock = threading.Lock()
        self._latest: CameraFrame | None = None
        self._timestamps: deque[int] = deque(maxlen=30)
        self._seq = 0
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"cam-reader-{self.report.resolved_dev}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        # Create FaceFeatureExtractor inside the worker thread so EGL GPU contexts stay thread-affine.
        extractor = (
            FaceFeatureExtractor(use_gpu=self._use_gpu) if self._extract_landmarks else None
        )
        if extractor is not None:
            self.delegate_name = extractor.delegate_name
        try:
            while not self._stop_event.is_set():
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    time.sleep(0.002)
                    continue
                now_ns = time.monotonic_ns()
                self._timestamps.append(now_ns)
                measured_fps = 0.0
                if len(self._timestamps) >= 2:
                    span_s = (self._timestamps[-1] - self._timestamps[0]) / 1e9
                    if span_s > 0:
                        measured_fps = (len(self._timestamps) - 1) / span_s

                obs = extractor.process(frame, now_ns) if extractor is not None else None
                self._seq += 1
                cam_frame = CameraFrame(
                    image=frame if self._keep_images else None,
                    timestamp_ns=now_ns,
                    seq=self._seq,
                    measured_fps=measured_fps,
                    observation=obs,
                )
                with self._lock:
                    self._latest = cam_frame
        finally:
            if extractor is not None:
                extractor.close()

    def get_latest(self) -> CameraFrame | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.cap.release()


class StereoCapture:
    """Manages synchronized dual-camera threads bound by `/dev/v4l/by-id/`."""

    def __init__(
        self,
        cam1_by_id: str,
        cam2_by_id: str | None = None,
        mono: bool = False,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        cam1_exposure: int = 300,
        cam1_gain: int | None = 180,
        cam2_exposure: int = 300,
        cam2_gain: int | None = 160,
        extract_landmarks: bool = False,
        use_gpu: bool = True,
        keep_images: bool = True,
        auto_exposure: bool = True,
        capture_backend: str = "v4l2",
    ) -> None:
        if capture_backend == "pipewire" and not (mono or not cam2_by_id):
            raise RuntimeError(
                "PipeWire camera sharing is mono-only; switch camera mode to mono "
                "or use the V4L2 backend for stereo."
            )
        # macOS has no /dev/v4l/by-id; always use the AVFoundation-backed
        # OpenCV reader unless the caller explicitly chose another backend.
        if capture_backend == "v4l2" and sys.platform == "darwin":
            capture_backend = "opencv"
        self.mono = bool(mono or not cam2_by_id)
        self.capture_backend = capture_backend
        self._r1 = _CameraReaderThread(
            cam1_by_id,
            width,
            height,
            fps,
            cam1_exposure,
            cam1_gain,
            extract_landmarks=extract_landmarks,
            use_gpu=use_gpu,
            keep_images=keep_images,
            manual_exposure=not auto_exposure,
            capture_backend=capture_backend,
        )
        self._r2: _CameraReaderThread | None = None
        if not self.mono:
            try:
                assert cam2_by_id is not None
                self._r2 = _CameraReaderThread(
                    cam2_by_id,
                    width,
                    height,
                    fps,
                    cam2_exposure,
                    cam2_gain,
                    extract_landmarks=extract_landmarks,
                    use_gpu=use_gpu,
                    keep_images=keep_images,
                    manual_exposure=not auto_exposure,
                    capture_backend=capture_backend,
                )
            except Exception:
                self._r1.stop()
                raise

    @property
    def reports(self) -> tuple[CameraLockReport, CameraLockReport]:
        second = self._r2.report if self._r2 is not None else self._r1.report
        return self._r1.report, second

    @property
    def delegates(self) -> tuple[str, str]:
        second = self._r2.delegate_name if self._r2 is not None else self._r1.delegate_name
        return self._r1.delegate_name, second

    def start(self) -> None:
        self._r1.start()
        if self._r2 is not None:
            self._r2.start()

    def read_pair(self, timeout_s: float = 2.0) -> StereoFrame | None:
        """Return latest paired frames, duplicating the mono camera when needed."""
        deadline = time.monotonic() + timeout_s
        while True:
            f1 = self._r1.get_latest()
            f2 = self._r2.get_latest() if self._r2 is not None else f1
            if f1 is not None and f2 is not None:
                return StereoFrame(cam1=f1, cam2=f2)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def close(self) -> None:
        self._r1.stop()
        if self._r2 is not None:
            self._r2.stop()

    def __enter__(self) -> StereoCapture:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
