"""Calm, progressive-enhancement 4-step setup flow (embeddable or dialog)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QRectF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from keyless.audio.quick_test import (
    LiveRecorder,
    MicLevel,
    input_devices,
    run_speech_test,
)
from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.diagnostics.doctor import DoctorReport, collect_doctor
from keyless.models.catalog import get_asset
from keyless.models.downloader import DownloadProgress, download_asset
from keyless.platform.linux.cameras import discover_v4l_by_id, friendly_camera_name
from keyless.ui.components import Card, divider, eyebrow
from keyless.ui.onboarding.flow import (
    OPTIONAL_STEPS,
    OnboardingFlow,
    OnboardingStep,
    STEPS,
)
from keyless.ui.theme.tokens import load_bundled_fonts
from keyless.ui.tray import make_icon
from keyless.voice.stt.factory import stt_report


class CameraPreview(QWidget):
    """Cropped, rounded live camera preview with zero letterboxing or inline style clashes."""

    def __init__(self, name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.setFixedSize(240, 135)
        self._cap = None
        self._pixmap: QPixmap | None = None
        self._placeholder = "Camera preview"
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._read)

    def setText(self, text: str) -> None:  # noqa: N802
        self._placeholder = text
        if text:
            self._pixmap = None
        self.update()

    def start(self, path: str) -> None:
        self.stop()
        if not path:
            self.setText("Select a camera")
            return
        try:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                self.setText("Camera unavailable")
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 480)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 270)
            self._cap = cap
            self._placeholder = ""
            self._timer.start()
            self.update()
        except Exception:
            self.setText("Preview unavailable")

    def _read(self) -> None:
        if self._cap is None:
            return
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return
        frame = cv2.flip(frame, 1)  # Mirror like FaceTime / Photo Booth
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        image = QImage(rgb.data, width, height, channels * width, QImage.Format.Format_RGB888).copy()
        scaled = QPixmap.fromImage(image).scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Center-crop to exact widget dimensions so there are zero black bars.
        x = max(0, (scaled.width() - self.width()) // 2)
        y = max(0, (scaled.height() - self.height()) // 2)
        self._pixmap = scaled.copy(x, y, self.width(), self.height())
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QColor

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 8.0, 8.0)
        painter.setClipPath(clip)
        tokens = getattr(self.window(), "theme_tokens", None)
        bg_hex = tokens.surface_alt if tokens is not None else "#252528"
        fg_hex = tokens.muted if tokens is not None else "#928f87"
        painter.fillRect(rect, QColor(bg_hex))
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)
        elif self._placeholder:
            painter.setPen(QColor(fg_hex))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._placeholder)
        painter.end()

    def stop(self) -> None:
        self._timer.stop()
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class StepPage(QWidget):
    step: OnboardingStep
    next_label = "Continue"
    skippable = False

    def __init__(self) -> None:
        super().__init__()
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 4, 0, 0)
        self.layout.setSpacing(14)
        self.title = QLabel("")
        self.title.setObjectName("Title")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("Muted")
        self.subtitle.setWordWrap(True)
        self.layout.addWidget(self.title)
        self.layout.addWidget(self.subtitle)
        self.body = QVBoxLayout()
        self.body.setSpacing(14)
        self.layout.addLayout(self.body, 1)
        self.problem = QLabel("")
        self.problem.setObjectName("Warning")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        self.layout.addWidget(self.problem)

    def set_problem(self, message: str) -> None:
        self.problem.setText(message)
        self.problem.setVisible(bool(message))

    def on_enter(self) -> None:
        return None

    def validate(self) -> tuple[bool, str]:
        return True, ""

    @property
    def unresolved(self) -> bool:
        return False


class WelcomePage(StepPage):
    step = OnboardingStep.WELCOME
    next_label = "Get started"

    def __init__(self) -> None:
        super().__init__()
        self.title.setText("Welcome to Keyless")
        self.subtitle.setText(
            "Speak to click, scroll, and type anywhere on your desktop. "
            "Everything runs privately on this device."
        )
        card = Card()
        heading = QLabel("Voice first")
        heading.setObjectName("Heading")
        description = QLabel(
            "Gaze v2 learns from your natural clicks when it ships. "
            "There is no calibration step."
        )
        description.setObjectName("Muted")
        description.setWordWrap(True)
        card.layout.addWidget(heading)
        card.layout.addWidget(description)
        self.body.addWidget(card)
        self.body.addStretch(1)


class _DownloadWorker(QThread):
    progress = Signal(object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, asset_id: str = "parakeet-tdt-0.6b-v2", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.asset_id = asset_id

    def run(self) -> None:
        try:
            asset = get_asset(self.asset_id)
            receipt = download_asset(asset, progress=lambda p: self.progress.emit(p))
            self.done.emit(receipt)
        except Exception as exc:
            self.failed.emit(str(exc))


class _MicCheckWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, device: int | str | None) -> None:
        super().__init__()
        self.recorder = LiveRecorder(device)

    def run(self) -> None:
        try:
            audio = self.recorder.record(1.5)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
            self.done.emit(MicLevel(peak=peak, rms=rms, seconds=1.5))
        except Exception as exc:
            self.failed.emit(str(exc))


class _SpeechTestWorker(QThread):
    stage = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig, paths: AppPaths) -> None:
        super().__init__()
        self.config = config
        self.paths = paths
        self.recorder = LiveRecorder(config.audio_device)

    def run(self) -> None:
        try:
            result = run_speech_test(
                self.config,
                paths=self.paths,
                seconds=4.0,
                recorder=self.recorder,
                on_stage=self.stage.emit,
            )
            self.done.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class DevicesPage(StepPage):
    """Unified, progressive-enhancement device setup (Camera + Mic + Speech + missing permissions)."""

    step = OnboardingStep.DEVICES
    next_label = "Continue"

    def __init__(
        self,
        config: AppConfig,
        paths: AppPaths,
        report_provider: Callable[[], DoctorReport] | None = None,
        *,
        enable_previews: bool = True,
    ) -> None:
        super().__init__()
        self.config = config
        self.paths = paths
        self.report_provider = report_provider
        self.enable_previews = enable_previews
        self._report: DoctorReport | None = None
        self._dl_worker: _DownloadWorker | None = None
        self._mic_worker: _MicCheckWorker | None = None
        self._speech_worker: _SpeechTestWorker | None = None

        self.title.setText("Devices")
        self.subtitle.setText("Confirm your camera and microphone look and sound right.")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(12)
        scroll.setWidget(inner)
        self.body.addWidget(scroll, 1)

        # ---- Camera Card (progressive enhancement: stereo controls only when >=2 cameras)
        cam_card = Card()
        cam_card.layout.addWidget(eyebrow("Camera"))
        cam_row = QHBoxLayout()
        cam_row.setSpacing(16)
        self.preview1 = CameraPreview("cam1")
        self.preview1.setFixedSize(200, 120)
        self.preview2 = CameraPreview("cam2")
        self.preview2.setFixedSize(200, 120)
        self.preview2.setVisible(False)
        previews = QVBoxLayout()
        previews.setSpacing(8)
        previews.addWidget(self.preview1)
        previews.addWidget(self.preview2)
        previews.addStretch(1)
        cam_row.addLayout(previews)

        cam_controls = QFormLayout()
        cam_controls.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        cam_controls.setVerticalSpacing(10)

        self.mode_label = QLabel("Setup")
        self.mode = QComboBox()
        self.mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.mode.addItem("Automatic", "auto")
        self.mode.addItem("One camera", "mono")
        self.mode.addItem("Two cameras (stereo)", "stereo")
        idx = self.mode.findData(self.config.camera_mode)
        self.mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.mode.currentIndexChanged.connect(self.refresh_devices)
        cam_controls.addRow(self.mode_label, self.mode)

        self.cam1 = QComboBox()
        self.cam1.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cam1.currentIndexChanged.connect(self._on_camera_changed)
        cam_controls.addRow("Camera", self.cam1)

        self.cam2_label = QLabel("Second camera")
        self.cam2 = QComboBox()
        self.cam2.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cam2.currentIndexChanged.connect(self._on_camera_changed)
        cam_controls.addRow(self.cam2_label, self.cam2)

        self.swap_button = QPushButton("Swap cameras")
        self.swap_button.clicked.connect(self.swap)
        cam_controls.addRow("", self.swap_button)

        self.cam_hint = QLabel("")
        self.cam_hint.setObjectName("Muted")
        self.cam_hint.setWordWrap(True)
        cam_controls.addRow("", self.cam_hint)

        cam_row.addLayout(cam_controls, 1)
        cam_card.layout.addLayout(cam_row)
        col.addWidget(cam_card)

        # ---- Voice & Speech Card
        voice_card = Card()
        voice_card.layout.addWidget(eyebrow("Microphone & speech"))
        mic_row = QHBoxLayout()
        mic_row.setSpacing(8)
        self.mic = QComboBox()
        self.mic.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._populate_microphones()
        self.mic.currentIndexChanged.connect(self._save_microphone)
        mic_row.addWidget(self.mic, 1)
        self.check_button = QPushButton("Check mic")
        self.check_button.clicked.connect(self.check_mic)
        mic_row.addWidget(self.check_button)
        self.test_button = QPushButton("Test speech")
        self.test_button.clicked.connect(self.test_speech)
        mic_row.addWidget(self.test_button)
        voice_card.layout.addLayout(mic_row)

        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setValue(0)
        self.level.setFixedHeight(4)
        self.level.setTextVisible(False)
        voice_card.layout.addWidget(self.level)

        self.voice_note = QLabel("")
        self.voice_note.setObjectName("Muted")
        self.voice_note.setWordWrap(True)
        self.voice_note.setVisible(False)
        voice_card.layout.addWidget(self.voice_note)

        voice_card.layout.addWidget(divider())

        stt_row = QHBoxLayout()
        self.stt_status = QLabel("Local speech model")
        self.stt_status.setWordWrap(True)
        stt_row.addWidget(self.stt_status, 1)
        self.download_button = QPushButton("Download (630 MB)")
        self.download_button.setObjectName("Primary")
        self.download_button.clicked.connect(self.download_model)
        stt_row.addWidget(self.download_button)
        voice_card.layout.addLayout(stt_row)

        self.dl_progress = QProgressBar()
        self.dl_progress.setRange(0, 100)
        self.dl_progress.setFixedHeight(4)
        self.dl_progress.setTextVisible(False)
        self.dl_progress.setVisible(False)
        voice_card.layout.addWidget(self.dl_progress)
        col.addWidget(voice_card)

        # ---- Permissions Card (progressive: ONLY shown if something actually needs attention!)
        self.perm_card = Card(subtle=True)
        self.perm_card.layout.addWidget(eyebrow("Permissions needed"))
        self.perm_rows = QVBoxLayout()
        self.perm_rows.setSpacing(6)
        self.perm_card.layout.addLayout(self.perm_rows)
        perm_recheck = QPushButton("Check again")
        perm_recheck.clicked.connect(self.refresh_permissions)
        self.perm_card.layout.addWidget(perm_recheck, 0, Qt.AlignmentFlag.AlignLeft)
        self.perm_card.setVisible(False)
        col.addWidget(self.perm_card)
        col.addStretch(1)

        self._level_timer = QTimer(self)
        self._level_timer.setInterval(50)
        self._level_timer.timeout.connect(self._level_tick)

        self.refresh_devices()
        self.refresh_stt()

    def _populate_microphones(self) -> None:
        self.mic.blockSignals(True)
        self.mic.clear()
        self.mic.addItem("System default", None)
        for device in input_devices():
            self.mic.addItem(device.name, device.index)
        current = self.config.audio_device
        if current is not None:
            index = self.mic.findData(current)
            if index < 0 and isinstance(current, str):
                index = self.mic.findText(current)
            if index >= 0:
                self.mic.setCurrentIndex(index)
        self.mic.blockSignals(False)

    def _save_microphone(self, _index: int) -> None:
        self.config.audio_device = self.mic.currentData()
        self.config.save(self.paths.settings_path)

    def refresh_devices(self) -> None:
        devices = discover_v4l_by_id()
        has_two = len(devices) >= 2
        self.config.camera_mode = str(self.mode.currentData() or "auto") if has_two else "mono"
        show_stereo = has_two and self.config.camera_mode != "mono"
        self._second_visible = show_stereo

        # Progressive enhancement: hide stereo/mode clutter completely when only 1 camera exists
        self.mode_label.setVisible(has_two)
        self.mode.setVisible(has_two)
        self.cam2_label.setVisible(show_stereo)
        self.cam2.setVisible(show_stereo)
        self.swap_button.setVisible(show_stereo)
        self.preview2.setVisible(show_stereo)

        for combo, current in ((self.cam1, self.config.cam1_by_id), (self.cam2, self.config.cam2_by_id)):
            combo.blockSignals(True)
            combo.clear()
            if not devices:
                combo.addItem("No camera detected", "")
            for device in devices:
                combo.addItem(friendly_camera_name(device), device)
            index = combo.findData(current)
            if index < 0 and current and not devices:
                combo.addItem(friendly_camera_name(current), current)
                index = combo.count() - 1
            elif index < 0 and devices:
                # Auto-pick first available (or second for cam2)
                index = 1 if (combo is self.cam2 and len(devices) >= 2) else 0
            combo.setCurrentIndex(max(0, index))
            combo.setEnabled(show_stereo if combo is self.cam2 else bool(devices or current))
            combo.blockSignals(False)

        if not devices and not self.config.cam1_by_id:
            self.cam_hint.setText("Connect a webcam to enable gaze tracking.")
        elif not has_two:
            self.cam_hint.setText("Using 1 camera. Plug in a second camera anytime for stereo tracking.")
        else:
            self.cam_hint.setText("")

        self._on_camera_changed()

    def _on_camera_changed(self) -> None:
        self.config.cam1_by_id = str(self.cam1.currentData() or "")
        self.config.cam2_by_id = str(self.cam2.currentData() or "") if self._second_visible else ""
        if not self.enable_previews:
            self.preview1.setText("Camera ready" if self.config.cam1_by_id else "No camera")
            return
        self.preview1.start(self.config.cam1_by_id)
        if self._second_visible and self.config.cam2_by_id:
            self.preview2.start(self.config.cam2_by_id)
        else:
            self.preview2.stop()

    def swap(self) -> None:
        first = self.cam1.currentIndex()
        self.cam1.setCurrentIndex(self.cam2.currentIndex())
        self.cam2.setCurrentIndex(first)
        self._on_camera_changed()

    def refresh_stt(self) -> None:
        report = stt_report(self.config, paths=self.paths)
        ready = report.get("selected") == "parakeet"
        if ready:
            self.stt_status.setText("Local speech model ready")
            self.stt_status.setObjectName("Success")
        else:
            self.stt_status.setText("Local speech model required (630 MB, runs offline)")
            self.stt_status.setObjectName("Warning")
        self.stt_status.style().unpolish(self.stt_status)
        self.stt_status.style().polish(self.stt_status)
        self.download_button.setVisible(not ready)
        self.test_button.setEnabled(ready)

    def refresh_permissions(self) -> None:
        while self.perm_rows.count():
            item = self.perm_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        try:
            report = (
                self.report_provider()
                if self.report_provider is not None
                else collect_doctor(check_providers=False)
            )
        except Exception:
            self.perm_card.setVisible(False)
            return
        self._report = report
        missing = [
            (name, info)
            for name, info in report.permissions.items()
            if info.get("state") in ("denied", "needs_restart")
        ]
        for name, info in missing:
            title = name.replace("_", " ").capitalize()
            detail = info.get("action") or info.get("detail") or info.get("state", "")
            lbl = QLabel(f"{title}: {detail}")
            lbl.setWordWrap(True)
            lbl.setObjectName("Muted")
            self.perm_rows.addWidget(lbl)
        self.perm_card.setVisible(bool(missing))

    def download_model(self) -> None:
        if self._dl_worker is not None:
            return
        self.dl_progress.setVisible(True)
        self.dl_progress.setValue(0)
        self.download_button.setEnabled(False)
        self.download_button.setText("Downloading…")
        worker = _DownloadWorker(parent=self)
        worker.progress.connect(lambda p: self.dl_progress.setValue(int(p.percent)))
        worker.done.connect(self._on_dl_done)
        worker.failed.connect(self._on_dl_failed)
        self._dl_worker = worker
        worker.start()

    def _on_dl_done(self, _receipt: object) -> None:
        self._dl_worker = None
        self.dl_progress.setVisible(False)
        self.download_button.setEnabled(True)
        self.download_button.setText("Download (630 MB)")
        self.refresh_stt()
        self.set_problem("")

    def _on_dl_failed(self, message: str) -> None:
        self._dl_worker = None
        self.dl_progress.setVisible(False)
        self.download_button.setEnabled(True)
        self.download_button.setText("Retry download")
        self.set_problem(f"Download failed: {message}")

    def _worker_recorder(self):
        for worker in (self._speech_worker, self._mic_worker):
            if worker is not None and worker.isRunning():
                return worker.recorder
        return None

    def _level_tick(self) -> None:
        rec = self._worker_recorder()
        if rec is None:
            self._level_timer.stop()
            self.level.setValue(0)
            return
        self.level.setValue(min(100, int(float(rec.level) * 1200.0)))

    def check_mic(self) -> None:
        if self._mic_worker is not None and self._mic_worker.isRunning():
            return
        self.check_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self.voice_note.setText("Listening for 1.5 seconds…")
        self.voice_note.setVisible(True)
        worker = _MicCheckWorker(self.mic.currentData())
        worker.done.connect(self._on_mic_done)
        worker.failed.connect(self._on_mic_failed)
        self._mic_worker = worker
        self._level_timer.start()
        worker.start()

    def _on_mic_done(self, level: MicLevel) -> None:
        self._level_timer.stop()
        self.level.setValue(0)
        self.check_button.setEnabled(True)
        self.refresh_stt()
        self.voice_note.setVisible(True)
        if level.heard:
            self.voice_note.setText("Microphone heard your voice clearly.")
            self.voice_note.setObjectName("Success")
        else:
            self.voice_note.setText("No audio detected — try picking another microphone.")
            self.voice_note.setObjectName("Warning")
        self.voice_note.style().unpolish(self.voice_note)
        self.voice_note.style().polish(self.voice_note)

    def _on_mic_failed(self, message: str) -> None:
        self._level_timer.stop()
        self.level.setValue(0)
        self.check_button.setEnabled(True)
        self.refresh_stt()
        self.voice_note.setVisible(True)
        self.voice_note.setText(f"Microphone error: {message}")
        self.voice_note.setObjectName("Warning")

    def test_speech(self) -> None:
        if self._speech_worker is not None and self._speech_worker.isRunning():
            return
        self.check_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self.voice_note.setVisible(True)
        self.voice_note.setText("Preparing speech engine…")
        worker = _SpeechTestWorker(self.config, self.paths)
        worker.stage.connect(
            lambda s: self.voice_note.setText(
                "Speak now (4 seconds)…" if s == "recording" else "Transcribing…" if s == "transcribing" else "Loading speech model…"
            )
        )
        worker.done.connect(self._on_speech_done)
        worker.failed.connect(self._on_mic_failed)
        self._speech_worker = worker
        self._level_timer.start()
        worker.start()

    def _on_speech_done(self, result: object) -> None:
        self._level_timer.stop()
        self.level.setValue(0)
        self.check_button.setEnabled(True)
        self.refresh_stt()
        transcript = getattr(result, "transcript", "")
        self.voice_note.setVisible(True)
        if transcript:
            self.voice_note.setText(f"Heard: “{transcript}”")
            self.voice_note.setObjectName("Success")
        else:
            self.voice_note.setText("No words recognized — try speaking closer to the mic.")
            self.voice_note.setObjectName("Warning")
        self.voice_note.style().unpolish(self.voice_note)
        self.voice_note.style().polish(self.voice_note)

    def on_enter(self) -> None:
        self.refresh_devices()
        self.refresh_stt()
        self.refresh_permissions()

    def validate(self) -> tuple[bool, str]:
        if not self.config.cam1_by_id:
            return False, "Connect and select a camera to continue."
        if self._second_visible and self.config.camera_mode == "stereo":
            if not self.config.cam2_by_id or self.config.cam1_by_id == self.config.cam2_by_id:
                return False, "Choose two different cameras, or switch to one camera."
        report = stt_report(self.config, paths=self.paths)
        if report.get("selected") != "parakeet":
            return False, "Download the local speech model to continue."
        return True, ""

    @property
    def unresolved(self) -> bool:
        if self._report is None:
            return False
        return any(
            info.get("state") in ("denied", "needs_restart")
            for info in self._report.permissions.values()
        )

    def close_resources(self) -> None:
        self.preview1.stop()
        self.preview2.stop()
        self._level_timer.stop()
        for worker in (self._speech_worker, self._mic_worker):
            if worker is not None and worker.isRunning():
                worker.recorder.stop()
                worker.wait(2000)
        if self._dl_worker is not None and self._dl_worker.isRunning():
            self._dl_worker.wait(1000)


class CalibrationPage(StepPage):
    step = OnboardingStep.CALIBRATION
    next_label = "Continue"

    def __init__(self) -> None:
        super().__init__()

        self.title.setText("Gaze engine v2")
        self.subtitle.setText(
            "Voice control works now. Gaze v2 learns from your own clicks when it "
            "ships — there is no calibration grid."
        )

        card = Card()
        self.status = QLabel("Voice control is ready.")
        self.status.setObjectName("Heading")
        card.layout.addWidget(self.status)
        self.hint = QLabel(
            "Gaze chooses among buttons, links, and fields from your attention. "
            "Natural clicks and voice corrections will train it."
        )
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        card.layout.addWidget(self.hint)

        self.body.addWidget(card)
        self.body.addStretch(1)

    def refresh(self) -> None:
        pass

    def on_enter(self) -> None:
        pass

    def validate(self) -> tuple[bool, str]:
        return True, ""


class ReadyPage(StepPage):
    step = OnboardingStep.READY
    next_label = "Start listening"

    def __init__(self) -> None:
        super().__init__()
        self.title.setText("You're ready")
        self.subtitle.setText("Keyless starts voice-only. Gaze v2 follows when it ships.")
        card = Card()
        self.summary = QLabel("Camera, microphone, and speech model are set.")
        self.summary.setObjectName("Heading")
        self.summary.setWordWrap(True)
        card.layout.addWidget(self.summary)
        hint = QLabel("No calibration. Natural clicks and corrections will train gaze v2.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        card.layout.addWidget(hint)
        self.body.addWidget(card)
        self.body.addStretch(1)


_STEP_VOICE_HELP = {
    OnboardingStep.WELCOME: "Welcome to Keyless. Everything runs privately on your device. Press Get started to check your camera and microphone.",
    OnboardingStep.DEVICES: "Check that your microphone and camera are ready, then download the offline speech model if needed.",
    OnboardingStep.CALIBRATION: "Voice control is ready. Gaze v2 will learn from your clicks when it ships. No calibration is needed.",
    OnboardingStep.READY: "You are ready to go hands-free. Press Start listening whenever you want Keyless active.",
}


class OnboardingWidget(QWidget):
    """Single-column, distraction-free setup widget (embeds directly inside MainWindow)."""

    completed = Signal()
    cancelled = Signal()

    def __init__(
        self,
        *,
        flow: OnboardingFlow | None = None,
        config: AppConfig | None = None,
        paths: AppPaths | None = None,
        report_provider: Callable[[], DoctorReport] | None = None,
        enable_previews: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths or current_paths()
        self.config = config or AppConfig.load(self.paths.settings_path)
        self.flow = flow or OnboardingFlow(self.paths)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 24)

        column = QWidget()
        column.setMinimumWidth(580)
        column.setMaximumWidth(680)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # Slim top header: step indicator + optional voice guide + thin 4px progress bar
        top = QHBoxLayout()
        self.step_label = QLabel("")
        self.step_label.setObjectName("Muted")
        top.addWidget(self.step_label)
        top.addStretch(1)
        self._tts_dl_worker: _DownloadWorker | None = None
        self._speaker = None
        self.voice_help_button = QPushButton("")
        self.voice_help_button.setObjectName("Ghost")
        self.voice_help_button.setFlat(True)
        self.voice_help_button.setFixedHeight(20)
        self.voice_help_button.setStyleSheet("padding: 0px 4px; border: none; font-size: 12px;")
        self.voice_help_button.clicked.connect(self._toggle_voice_help)
        top.addWidget(self.voice_help_button)
        layout.addLayout(top)
        self._sync_voice_help_button()

        self.progress = QProgressBar()
        self.progress.setRange(0, len(STEPS) - 1)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.stack = QStackedWidget()
        self.pages: dict[OnboardingStep, StepPage] = {
            OnboardingStep.WELCOME: WelcomePage(),
            OnboardingStep.DEVICES: DevicesPage(
                self.config,
                self.paths,
                report_provider=report_provider,
                enable_previews=enable_previews,
            ),
            OnboardingStep.CALIBRATION: CalibrationPage(),
            OnboardingStep.READY: ReadyPage(),
        }
        for step in STEPS:
            self.stack.addWidget(self.pages[step])
        layout.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.setObjectName("Ghost")
        self.back_button.clicked.connect(self.back)
        footer.addWidget(self.back_button)
        self.skip_button = QPushButton("Skip")
        self.skip_button.setObjectName("Ghost")
        self.skip_button.clicked.connect(self.skip)
        footer.addWidget(self.skip_button)
        footer.addStretch(1)
        self.next_button = QPushButton("Continue")
        self.next_button.setObjectName("Primary")
        self.next_button.clicked.connect(self.next)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)

        outer.addStretch(1)
        outer.addWidget(column, 6)
        outer.addStretch(1)

        self._show(self.flow.step)

    def _show(self, step: OnboardingStep) -> None:
        page = self.pages[step]
        devices_page = self.pages[OnboardingStep.DEVICES]
        if isinstance(devices_page, DevicesPage) and page is not devices_page:
            devices_page.close_resources()
        self.stack.setCurrentWidget(page)
        index = STEPS.index(step)
        self.progress.setValue(index)
        self.step_label.setText(f"Step {index + 1} of {len(STEPS)}")
        self.back_button.setVisible(step is not OnboardingStep.WELCOME)
        self.next_button.setText(page.next_label)
        self.skip_button.setVisible(step in OPTIONAL_STEPS)
        page.on_enter()
        self._maybe_speak_step(step)

    def _voice_ready(self) -> bool:
        try:
            from keyless.voice.tts import tts_report

            return bool(tts_report(self.paths)["ready"])
        except Exception:
            return False

    def _sync_voice_help_button(self) -> None:
        if not self._voice_ready():
            self.voice_help_button.setText("Voice help (40 MB)")
        elif getattr(self.config, "tts_onboarding_help", True):
            self.voice_help_button.setText("Voice help: On")
        else:
            self.voice_help_button.setText("Voice help: Off")

    def _ensure_speaker(self):
        if self._speaker is None:
            from keyless.voice.tts import WoofSpeaker

            self._speaker = WoofSpeaker(
                paths=self.paths,
                voice=getattr(self.config, "tts_voice", "juno"),
                enabled=True,
            )
        return self._speaker

    def _maybe_speak_step(self, step: OnboardingStep) -> None:
        if not getattr(self.config, "tts_onboarding_help", True) or not self._voice_ready():
            return
        cue = _STEP_VOICE_HELP.get(step, "")
        if cue:
            try:
                self._ensure_speaker().speak(cue, interrupt=True)
            except Exception:
                pass

    def _toggle_voice_help(self) -> None:
        if not self._voice_ready():
            if self._tts_dl_worker is not None and self._tts_dl_worker.isRunning():
                return
            self.voice_help_button.setEnabled(False)
            self.voice_help_button.setText("Downloading voice…")
            worker = _DownloadWorker("underdog-woof")
            worker.progress.connect(
                lambda p: self.voice_help_button.setText(f"Voice {int(p.percent)}%")
            )
            worker.done.connect(lambda _r: self._finish_tts_download(""))
            worker.failed.connect(self._finish_tts_download)
            self._tts_dl_worker = worker
            worker.start()
            return
        self.config.tts_onboarding_help = not getattr(self.config, "tts_onboarding_help", True)
        if self.config.tts_onboarding_help:
            self.config.tts_enabled = True
        self.config.save(self.paths.settings_path)
        self._sync_voice_help_button()
        if self.config.tts_onboarding_help:
            self._maybe_speak_step(self.flow.step)
        elif self._speaker is not None:
            self._speaker.stop()

    def _finish_tts_download(self, err: str) -> None:
        self._tts_dl_worker = None
        self.voice_help_button.setEnabled(True)
        if not err:
            self.config.tts_onboarding_help = True
            self.config.tts_enabled = True
            self.config.save(self.paths.settings_path)
            self._sync_voice_help_button()
            self._maybe_speak_step(self.flow.step)
        else:
            self.voice_help_button.setText("Voice download failed")

    def next(self) -> None:
        page = self.pages[self.flow.step]
        ok, reason = page.validate()
        if not ok:
            page.set_problem(reason)
            return
        page.set_problem("")
        self.config.save(self.paths.settings_path)
        if self.flow.step is OnboardingStep.READY:
            self.flow.advance()
            self.close_resources()
            self.completed.emit()
            return
        self.flow.advance(unresolved=page.unresolved)
        self._show(self.flow.step)

    def back(self) -> None:
        self.flow.back()
        self._show(self.flow.step)

    def skip(self) -> None:
        if self.flow.step not in OPTIONAL_STEPS:
            return
        self.flow.advance(skipped=True)
        self._show(self.flow.step)

    def close_resources(self) -> None:
        devices_page = self.pages.get(OnboardingStep.DEVICES)
        if isinstance(devices_page, DevicesPage):
            devices_page.close_resources()
        if self._speaker is not None:
            self._speaker.close()
            self._speaker = None
        if self._tts_dl_worker is not None and self._tts_dl_worker.isRunning():
            self._tts_dl_worker.wait(2000)


class OnboardingWindow(QDialog):
    """Standalone dialog wrapper around OnboardingWidget (used by tests or direct invocation)."""

    def __init__(
        self,
        *,
        flow: OnboardingFlow | None = None,
        config: AppConfig | None = None,
        paths: AppPaths | None = None,
        report_provider: Callable[[], DoctorReport] | None = None,
        enable_previews: bool = True,
    ) -> None:
        super().__init__()
        load_bundled_fonts()
        self.setWindowTitle("Set up Keyless")
        self.setWindowIcon(make_icon())
        self.resize(740, 580)
        self.widget = OnboardingWidget(
            flow=flow,
            config=config,
            paths=paths,
            report_provider=report_provider,
            enable_previews=enable_previews,
            parent=self,
        )
        self.flow = self.widget.flow
        self.config = self.widget.config
        self.paths = self.widget.paths
        self.stack = self.widget.stack
        self.pages = self.widget.pages
        self.widget.completed.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.widget)

    def next(self) -> None:
        self.widget.next()

    def back(self) -> None:
        self.widget.back()

    def skip(self) -> None:
        self.widget.skip()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.widget.close_resources()
        super().closeEvent(event)
