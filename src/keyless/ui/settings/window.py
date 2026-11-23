"""Calm, instant-apply Settings surface with progressive enhancement."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from keyless.audio.quick_test import (
    DEFAULT_SECONDS,
    MicLevel,
    SpeechTestResult,
    check_microphone,
    input_devices,
    run_speech_test,
)
from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.diagnostics.doctor import collect_doctor, format_text
from keyless.models.catalog import get_asset
from keyless.models.downloader import download_asset
from keyless.models.providers.acp_registry import fetch_registry
from keyless.models.providers.presets import load_presets
from keyless.models.providers.secrets import keyring_available, set_secret
from keyless.platform.linux.cameras import (
    discover_v4l_by_id,
    friendly_camera_name,
    resolve_camera_config,
)
from keyless.platform.linux.pipewire import pipewire_available
from keyless.ui.components import (
    Card,
    DisclosureSection,
    KeyCaptureButton,
    divider,
    eyebrow,
)
from keyless.voice.stt.factory import stt_report

VERSION = "0.1.2"


class MicCheckWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, device: int | str | None) -> None:
        super().__init__()
        self.device = device

    def run(self) -> None:
        try:
            self.done.emit(check_microphone(self.device))
        except Exception as exc:
            self.failed.emit(str(exc))


class _SpeechCheckWorker(QThread):
    stage = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, config: AppConfig, paths: AppPaths) -> None:
        super().__init__()
        self.config = config
        self.paths = paths

    def run(self) -> None:
        try:
            self.done.emit(
                run_speech_test(
                    self.config,
                    paths=self.paths,
                    seconds=DEFAULT_SECONDS,
                    on_stage=self.stage.emit,
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class _ModelDownloadWorker(QThread):
    progress = Signal(object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None, asset_id: str = "parakeet-tdt-0.6b-v2") -> None:
        super().__init__(parent)
        self.asset_id = asset_id

    def run(self) -> None:
        try:
            asset = get_asset(self.asset_id)
            self.done.emit(download_asset(asset, progress=lambda p: self.progress.emit(p)))
        except Exception as exc:
            self.failed.emit(str(exc))


class _ContainedScrollArea(QScrollArea):
    """Scroll area that strictly caps child content width to the visible viewport."""

    def _sync_child_width(self) -> None:
        widget = self.widget()
        if widget is not None:
            vw = max(240, self.viewport().width())
            widget.setMaximumWidth(vw)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_child_width()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_child_width()


def _compact_combo(combo: QComboBox) -> QComboBox:
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(10)
    combo.setMinimumWidth(0)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return combo


def _wrap_label(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return label


class SettingsPanel(QWidget):
    """Instant-apply Settings: Voice, Camera & Gaze, Optional AI, and collapsible Diagnostics."""

    def __init__(
        self,
        config: AppConfig | None = None,
        paths: AppPaths | None = None,
        *,
        on_config_changed: Callable[[], None] | None = None,
        on_calibrate: Callable[[], None] | None = None,
        on_run_setup: Callable[[], None] | None = None,
        on_check_updates: Callable[[], None] | None = None,
        on_apply_update: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths or current_paths()
        self.config = config or AppConfig.load(self.paths.settings_path)
        self._on_config_changed = on_config_changed
        self._on_calibrate = on_calibrate
        self._on_run_setup = on_run_setup
        self._on_check_updates = on_check_updates
        self._on_apply_update = on_apply_update
        self._loading = True
        self._mic_worker: MicCheckWorker | None = None
        self._speech_worker: _SpeechCheckWorker | None = None
        self._dl_worker: _ModelDownloadWorker | None = None
        self._tts_dl_worker: _ModelDownloadWorker | None = None
        self._tts_speaker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = _ContainedScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(0)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 12, 16)
        layout.setSpacing(16)
        scroll.setWidget(content)
        self.scroll = scroll
        root.addWidget(scroll)

        title = QLabel("Settings")
        title.setObjectName("Title")
        layout.addWidget(title)

        # ------------------------------------------------------------------ Voice
        voice_card = Card()
        voice_card.layout.addWidget(eyebrow("Voice"))
        vform = QFormLayout()
        vform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        vform.setVerticalSpacing(10)

        self.microphone = _compact_combo(QComboBox())
        self._populate_microphones()
        self.microphone.currentIndexChanged.connect(self._auto_save)
        vform.addRow("Microphone", self.microphone)

        mic_actions = QHBoxLayout()
        mic_actions.setSpacing(8)
        self.mic_test = QPushButton("Check mic")
        self.mic_test.clicked.connect(self.check_mic)
        mic_actions.addWidget(self.mic_test)
        self.speech_test = QPushButton("Test speech")
        self.speech_test.clicked.connect(self.test_speech)
        mic_actions.addWidget(self.speech_test)
        mic_actions.addStretch(1)
        vform.addRow("", mic_actions)

        self.mic_result = _wrap_label(QLabel(""))
        self.mic_result.setObjectName("Muted")
        self.mic_result.setVisible(False)
        vform.addRow("", self.mic_result)

        self.always_on = QCheckBox("Always listen for voice commands")
        self.always_on.setChecked(self.config.always_on_voice)
        self.always_on.toggled.connect(self._on_always_on_toggled)
        vform.addRow("", self.always_on)

        self.hotkey_label = QLabel("Push-to-talk key")
        self.hotkey = KeyCaptureButton(self.config.ptt_hotkey)
        self.hotkey.key_changed.connect(self._auto_save)
        vform.addRow(self.hotkey_label, self.hotkey)

        self.require_prefix = QCheckBox("Only act on command phrases (click, scroll, type…)")
        self.require_prefix.setChecked(self.config.require_command_prefix)
        self.require_prefix.toggled.connect(self._auto_save)
        vform.addRow("", self.require_prefix)

        self.pop_click = QCheckBox("Pop-click (lip pop triggers left click)")
        self.pop_click.setChecked(self.config.enable_pop_click)
        self.pop_click.toggled.connect(self._auto_save)
        vform.addRow("", self.pop_click)

        voice_card.layout.addLayout(vform)
        voice_card.layout.addWidget(divider())

        stt_row = QHBoxLayout()
        stt_row.setSpacing(10)
        self.stt_status = _wrap_label(QLabel(""))
        self.stt_status.setObjectName("Muted")
        stt_row.addWidget(self.stt_status, 1)
        self.dl_button = QPushButton("Download speech model (630 MB)")
        self.dl_button.clicked.connect(self._download_speech_model)
        stt_row.addWidget(self.dl_button)
        voice_card.layout.addLayout(stt_row)

        self.dl_progress = QProgressBar()
        self.dl_progress.setRange(0, 100)
        self.dl_progress.setFixedHeight(4)
        self.dl_progress.setTextVisible(False)
        self.dl_progress.setVisible(False)
        voice_card.layout.addWidget(self.dl_progress)

        voice_card.layout.addWidget(divider())

        self.tts_enabled = QCheckBox("Speak assistant replies and setup help")
        self.tts_enabled.setChecked(bool(getattr(self.config, "tts_enabled", False)))
        self.tts_enabled.toggled.connect(self._auto_save)
        voice_card.layout.addWidget(self.tts_enabled)

        tts_form = QFormLayout()
        tts_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        tts_form.setVerticalSpacing(8)
        self.tts_voice = _compact_combo(QComboBox())
        self.tts_voice.addItem("Juno (Underdog Woof)", "juno")
        self.tts_voice.addItem("Mira (Underdog Woof)", "mira")
        v_idx = self.tts_voice.findData(getattr(self.config, "tts_voice", "juno"))
        self.tts_voice.setCurrentIndex(v_idx if v_idx >= 0 else 0)
        self.tts_voice.currentIndexChanged.connect(self._auto_save)
        tts_form.addRow("Assistant voice", self.tts_voice)

        tts_actions = QHBoxLayout()
        tts_actions.setSpacing(8)
        self.tts_preview_btn = QPushButton("Test voice")
        self.tts_preview_btn.clicked.connect(self._preview_tts)
        tts_actions.addWidget(self.tts_preview_btn)
        self.tts_dl_button = QPushButton("Download voice (40 MB)")
        self.tts_dl_button.clicked.connect(self._download_tts_model)
        tts_actions.addWidget(self.tts_dl_button)
        tts_actions.addStretch(1)
        tts_form.addRow("", tts_actions)
        voice_card.layout.addLayout(tts_form)

        self.tts_status = _wrap_label(QLabel("Voice by Underdog Woof · non-commercial license"))
        self.tts_status.setObjectName("Muted")
        voice_card.layout.addWidget(self.tts_status)
        layout.addWidget(voice_card)

        # ------------------------------------------------------------------ Camera & Gaze
        gaze_card = Card()
        gaze_card.layout.addWidget(eyebrow("Camera & gaze"))
        gform = QFormLayout()
        gform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        gform.setVerticalSpacing(10)

        self.cam_mode_label = QLabel("Camera setup")
        self.cam_mode = _compact_combo(QComboBox())
        self.cam_mode.addItem("Automatic", "auto")
        self.cam_mode.addItem("One camera", "mono")
        self.cam_mode.addItem("Two cameras (stereo)", "stereo")
        m_idx = self.cam_mode.findData(self.config.camera_mode)
        self.cam_mode.setCurrentIndex(m_idx if m_idx >= 0 else 0)
        self.cam_mode.currentIndexChanged.connect(self._on_camera_setting_changed)
        gform.addRow(self.cam_mode_label, self.cam_mode)

        self.cam1 = _compact_combo(QComboBox())
        self.cam1.currentIndexChanged.connect(self._auto_save)
        gform.addRow("Camera", self.cam1)

        self.cam2_label = QLabel("Second camera")
        self.cam2 = _compact_combo(QComboBox())
        self.cam2.currentIndexChanged.connect(self._auto_save)
        gform.addRow(self.cam2_label, self.cam2)

        self.gaze_model = _compact_combo(QComboBox())
        self.gaze_model.addItem("Precision RBF (recommended)", "rbf")
        self.gaze_model.addItem("3-D Geometric (experimental)", "geo")
        gm_idx = self.gaze_model.findData(getattr(self.config, "gaze_model", "rbf"))
        self.gaze_model.setCurrentIndex(gm_idx if gm_idx >= 0 else 0)
        self.gaze_model.currentIndexChanged.connect(self._auto_save)
        gform.addRow("Gaze model", self.gaze_model)

        self.eye_mouse = QCheckBox("Show eye cursor (does not move your mouse)")
        self.eye_mouse.setChecked(self.config.gaze_cursor_control)
        self.eye_mouse.toggled.connect(self._auto_save)
        gform.addRow("", self.eye_mouse)

        self.online_adapt = QCheckBox("Automatically adapt to posture and camera drift")
        self.online_adapt.setChecked(
            bool(getattr(self.config, "gaze_online_ekf", True))
        )
        self.online_adapt.toggled.connect(self._auto_save)
        gform.addRow("", self.online_adapt)

        self.share_camera = QCheckBox("Share camera with video calls and other apps")
        self.share_camera.setChecked(self.config.camera_backend == "pipewire")
        self.share_camera.toggled.connect(self._auto_save)
        gform.addRow("", self.share_camera)

        gaze_card.layout.addLayout(gform)

        calib_row = QHBoxLayout()
        calib_row.setSpacing(8)
        self.calib_status = _wrap_label(QLabel(""))
        self.calib_status.setObjectName("Muted")
        calib_row.addWidget(self.calib_status, 1)
        self.clear_calib_button = QPushButton("Clear data")
        self.clear_calib_button.setObjectName("Ghost")
        self.clear_calib_button.clicked.connect(self._clear_calibration_data)
        calib_row.addWidget(self.clear_calib_button)
        self.calibrate_button = QPushButton("Calibrate")
        self.calibrate_button.clicked.connect(self._trigger_calibrate)
        calib_row.addWidget(self.calibrate_button)
        gaze_card.layout.addLayout(calib_row)
        layout.addWidget(gaze_card)

        # ------------------------------------------------------------------ Firefox DOM Bridge
        from pathlib import Path

        ff_card = Card()
        ff_card.layout.addWidget(eyebrow("Firefox extension (DOM clicking & scrolling)"))
        ff_desc = _wrap_label(
            QLabel(
                "For instant, pixel-accurate clicking, tab switching, and smooth scrolling in Firefox, "
                "open about:debugging#/runtime/this-firefox → Load Temporary Add-on and select firefox_ext/manifest.json."
            )
        )
        ff_desc.setObjectName("Muted")
        ff_card.layout.addWidget(ff_desc)
        manifest_path = Path(__file__).resolve().parents[4] / "firefox_ext" / "manifest.json"
        self._ff_manifest_path = str(manifest_path) if manifest_path.exists() else "firefox_ext/manifest.json"
        ff_row = QHBoxLayout()
        ff_row.setSpacing(8)
        self.ff_copy_btn = QPushButton("Copy manifest path")
        self.ff_copy_btn.clicked.connect(self._copy_firefox_manifest)
        ff_row.addWidget(self.ff_copy_btn)
        ff_path_lbl = _wrap_label(QLabel(self._ff_manifest_path))
        ff_path_lbl.setObjectName("Muted")
        ff_row.addWidget(ff_path_lbl, 1)
        ff_card.layout.addLayout(ff_row)
        layout.addWidget(ff_card)

        # ------------------------------------------------------------------ Optional AI
        ai_card = Card()
        ai_card.layout.addWidget(eyebrow("AI assistant (optional)"))
        aform = QFormLayout()
        aform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        aform.setVerticalSpacing(10)

        self.provider_kind = _compact_combo(QComboBox())
        self.provider_kind.addItem("Automatic", "auto")
        self.provider_kind.addItem("Ollama (local)", "ollama")
        self.provider_kind.addItem("API provider", "openai_compat")
        self.provider_kind.addItem("Local ACP agent", "acp")
        p_idx = self.provider_kind.findData(self.config.llm_provider)
        self.provider_kind.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        self.provider_kind.currentIndexChanged.connect(self._on_ai_kind_changed)
        aform.addRow("Backend", self.provider_kind)

        self.provider_preset_label = QLabel("Provider")
        self.provider_preset = _compact_combo(QComboBox())
        for preset in load_presets():
            self.provider_preset.addItem(preset.name, preset.id)
        index = self.provider_preset.findData(self.config.llm_preset)
        if index >= 0:
            self.provider_preset.setCurrentIndex(index)
        self.provider_preset.currentIndexChanged.connect(self._auto_save)
        aform.addRow(self.provider_preset_label, self.provider_preset)

        self.acp_label = QLabel("ACP agent")
        self.acp_agent = QLineEdit(self.config.acp_agent_id)
        self.acp_agent.setPlaceholderText("e.g. opencode")
        self.acp_agent.editingFinished.connect(self._auto_save)
        aform.addRow(self.acp_label, self.acp_agent)

        self.llm_model_label = QLabel("Model")
        self.llm_model = QLineEdit(self.config.llm_model)
        self.llm_model.setPlaceholderText("Provider default")
        self.llm_model.editingFinished.connect(self._auto_save)
        aform.addRow(self.llm_model_label, self.llm_model)

        self.api_key_label = QLabel("API key")
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(
            "Stored in OS keychain" if keyring_available() else "OS keychain unavailable"
        )
        self.api_key.setEnabled(keyring_available())
        key_row.addWidget(self.api_key, 1)
        self.save_key_button = QPushButton("Store key")
        self.save_key_button.setEnabled(keyring_available())
        self.save_key_button.clicked.connect(self._store_api_key)
        key_row.addWidget(self.save_key_button)
        aform.addRow(self.api_key_label, key_row)

        self.vlm_consent = QCheckBox("Allow sending screen crops to remote AI for visual clicking")
        self.vlm_consent.setChecked(self.config.gaze_vlm_remote_consent)
        self.vlm_consent.toggled.connect(self._auto_save)
        aform.addRow("", self.vlm_consent)

        self.ai_note = _wrap_label(QLabel(""))
        self.ai_note.setObjectName("Muted")
        self.ai_note.setVisible(False)
        aform.addRow("", self.ai_note)

        ai_card.layout.addLayout(aform)
        layout.addWidget(ai_card)

        # ------------------------------------------------------------------ Collapsible Voice Commands Reference
        from keyless.ui.commands_help import CommandsHelpWidget

        self.commands_help = DisclosureSection("Voice commands reference")
        self.commands_help.body_layout.addWidget(CommandsHelpWidget(self, scrollable=False))
        layout.addWidget(self.commands_help)

        # ------------------------------------------------------------------ Collapsible Diagnostics & About
        self.diagnostics = DisclosureSection(
            "Diagnostics and about", on_expand=self.refresh_diagnostics
        )
        diag_card = Card(subtle=True)
        self._diag_grid = QGridLayout()
        self._diag_grid.setVerticalSpacing(6)
        self._diag_badges: dict[str, QLabel] = {}
        for row_i, (key, label) in enumerate(
            (
                ("input", "Input"),
                ("cameras", "Cameras"),
                ("screen", "Screen"),
                ("speech", "Speech"),
                ("ai", "AI"),
                ("overlay", "Overlay"),
            )
        ):
            self._diag_grid.addWidget(QLabel(label), row_i, 0)
            val = QLabel("—")
            val.setObjectName("Muted")
            self._diag_grid.addWidget(val, row_i, 1)
            self._diag_badges[key] = val
        self._diag_grid.setColumnStretch(1, 1)
        diag_card.layout.addLayout(self._diag_grid)

        self.error_summary_label = _wrap_label(QLabel(""))
        self.error_summary_label.setObjectName("Warning")
        self.error_summary_label.setVisible(False)
        diag_card.layout.addWidget(self.error_summary_label)

        self.diag_text = QTextEdit()
        self.diag_text.setReadOnly(True)
        self.diag_text.setFixedHeight(190)
        diag_card.layout.addWidget(self.diag_text)

        log_actions = QHBoxLayout()
        log_actions.setSpacing(8)
        self.copy_error_btn = QPushButton("Copy error")
        self.copy_error_btn.clicked.connect(self._copy_error)
        log_actions.addWidget(self.copy_error_btn)
        open_log = QPushButton("Open log file")
        open_log.clicked.connect(self._open_log_file)
        log_actions.addWidget(open_log)
        open_folder = QPushButton("Open logs folder")
        open_folder.clicked.connect(self._open_logs_folder)
        log_actions.addWidget(open_folder)
        log_actions.addStretch(1)
        diag_card.layout.addLayout(log_actions)

        diag_actions = QHBoxLayout()
        diag_actions.setSpacing(8)
        copy_diag = QPushButton("Copy full report")
        copy_diag.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.diag_text.toPlainText()))
        diag_actions.addWidget(copy_diag)
        rerun = QPushButton("Run setup again")
        rerun.clicked.connect(self._trigger_setup)
        diag_actions.addWidget(rerun)
        diag_actions.addStretch(1)
        about = QLabel(f"Keyless {VERSION}")
        about.setObjectName("Muted")
        diag_actions.addWidget(about)
        diag_card.layout.addLayout(diag_actions)

        diag_card.layout.addWidget(divider())
        upd_row = QHBoxLayout()
        upd_row.setSpacing(8)
        self.auto_updates = QCheckBox("Check for updates automatically")
        self.auto_updates.setChecked(bool(getattr(self.config, "auto_update_checks", True)))
        self.auto_updates.toggled.connect(self._auto_save)
        upd_row.addWidget(self.auto_updates, 1)
        self.check_updates_btn = QPushButton("Check for updates")
        self.check_updates_btn.clicked.connect(self._trigger_check_updates)
        upd_row.addWidget(self.check_updates_btn)
        self.update_now_btn = QPushButton("Update now")
        self.update_now_btn.setObjectName("Primary")
        self.update_now_btn.setVisible(False)
        self.update_now_btn.clicked.connect(self._trigger_apply_update)
        upd_row.addWidget(self.update_now_btn)
        diag_card.layout.addLayout(upd_row)

        self.update_status = _wrap_label(QLabel(""))
        self.update_status.setObjectName("Muted")
        self.update_status.setVisible(False)
        diag_card.layout.addWidget(self.update_status)

        self.diagnostics.body_layout.addWidget(diag_card)
        layout.addWidget(self.diagnostics)
        layout.addStretch(1)

        self._loading = False
        self.refresh()

    # ------------------------------------------------------------------ Helpers

    def _populate_microphones(self) -> None:
        self.microphone.blockSignals(True)
        self.microphone.clear()
        self.microphone.addItem("System default", None)
        for device in input_devices():
            self.microphone.addItem(device.name, device.index)
        current = self.config.audio_device
        if current is not None:
            index = self.microphone.findData(current)
            if index < 0 and isinstance(current, str):
                index = self.microphone.findText(current)
            if index >= 0:
                self.microphone.setCurrentIndex(index)
        self.microphone.blockSignals(False)

    def _refresh_cameras(self) -> None:
        devices = discover_v4l_by_id()
        has_two = len(devices) >= 2
        mode, cam1, cam2 = resolve_camera_config(
            self.config.camera_mode, self.config.cam1_by_id, self.config.cam2_by_id
        )
        show_stereo = has_two and self.cam_mode.currentData() != "mono"
        self.cam_mode_label.setVisible(has_two)
        self.cam_mode.setVisible(has_two)
        self.cam2_label.setVisible(show_stereo)
        self.cam2.setVisible(show_stereo)

        for combo, current in ((self.cam1, cam1), (self.cam2, cam2)):
            combo.blockSignals(True)
            combo.clear()
            if not devices and not current:
                combo.addItem("No camera detected", "")
            for dev in devices:
                combo.addItem(friendly_camera_name(dev), dev)
            idx = combo.findData(current)
            if idx < 0 and current:
                combo.addItem(friendly_camera_name(current), current)
                idx = combo.count() - 1
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)

        can_share = pipewire_available() and mode == "mono"
        self.share_camera.setEnabled(can_share)
        self.share_camera.setToolTip(
            "" if can_share else "Camera sharing is available in single-camera mode with PipeWire."
        )

    def refresh(self) -> None:
        self._loading = True
        self._refresh_cameras()
        self._sync_hotkey_visibility()
        self._sync_ai_visibility()

        report = stt_report(self.config, paths=self.paths)
        ready = report.get("selected") == "parakeet"
        self.stt_status.setText("Speech model ready (offline)" if ready else "Speech model not downloaded")
        self.dl_button.setVisible(not ready)
        self.speech_test.setEnabled(ready)

        calibrated = self.paths.calibration_path.exists()
        dataset_exists = (self.paths.calibration_path.parent / "calib_dataset.npz").exists()
        self.calib_status.setText("Eye tracking calibrated" if calibrated else "Not calibrated yet")
        self.calibrate_button.setText("Recalibrate" if calibrated else "Calibrate")
        self.clear_calib_button.setVisible(calibrated or dataset_exists)

        try:
            from keyless.voice.tts import tts_report

            t_rep = tts_report(self.paths)
            t_ready = bool(t_rep["ready"])
            self.tts_dl_button.setVisible(not t_ready)
            self.tts_preview_btn.setEnabled(t_ready)
            self.tts_status.setText(
                "Voice by Underdog Woof (ready offline · non-commercial license)"
                if t_ready
                else "Voice by Underdog Woof (optional 40 MB download · non-commercial license)"
            )
        except Exception:
            pass
        self._loading = False

    def _sync_hotkey_visibility(self) -> None:
        # Progressive disclosure: hide push-to-talk shortcut when always-on is enabled
        show_ptt = not self.always_on.isChecked()
        self.hotkey_label.setVisible(show_ptt)
        self.hotkey.setVisible(show_ptt)

    def _sync_ai_visibility(self) -> None:
        kind = str(self.provider_kind.currentData() or "auto")
        is_api = kind == "openai_compat"
        is_ollama = kind == "ollama"
        is_acp = kind == "acp"
        self.provider_preset_label.setVisible(is_api)
        self.provider_preset.setVisible(is_api)
        self.api_key_label.setVisible(is_api)
        self.api_key.setVisible(is_api)
        self.save_key_button.setVisible(is_api)
        self.llm_model_label.setVisible(is_api or is_ollama)
        self.llm_model.setVisible(is_api or is_ollama)
        if is_ollama and not self.llm_model.text().strip():
            self.llm_model.setPlaceholderText(self.config.gaze_vlm_model or self.config.ollama_model or "gemma4:e4b")
        else:
            self.llm_model.setPlaceholderText("Provider default")
        self.acp_label.setVisible(is_acp)
        self.acp_agent.setVisible(is_acp)
        if is_acp:
            try:
                installed = [s.id for s in fetch_registry() if s.installed]
                if installed and not self.acp_agent.text().strip():
                    self.acp_agent.setText(installed[0])
            except Exception:
                pass

    def _on_always_on_toggled(self) -> None:
        self._sync_hotkey_visibility()
        self._auto_save()

    def _on_camera_setting_changed(self) -> None:
        self._refresh_cameras()
        self._auto_save()

    def _on_ai_kind_changed(self) -> None:
        self._sync_ai_visibility()
        self._auto_save()

    def _store_api_key(self) -> None:
        key = self.api_key.text().strip()
        if not key:
            return
        preset = str(self.provider_preset.currentData() or "openai")
        set_secret(preset, key)
        self.api_key.clear()
        self.ai_note.setText("Key saved securely in the OS keychain.")
        self.ai_note.setObjectName("Success")
        self.ai_note.setVisible(True)
        self._auto_save()

    def _trigger_calibrate(self) -> None:
        if self._on_calibrate is not None:
            self._on_calibrate()

    def _clear_calibration_data(self) -> None:
        for path in (
            self.paths.calibration_path,
            self.paths.calibration_path.parent / "calib_dataset.npz",
        ):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        self.refresh()
        if self._on_config_changed is not None:
            self._on_config_changed()

    def _trigger_setup(self) -> None:
        if self._on_run_setup is not None:
            self._on_run_setup()

    # ------------------------------------------------------------------ Audio checks

    def check_mic(self) -> None:
        if self._mic_worker is not None and self._mic_worker.isRunning():
            return
        self.mic_test.setEnabled(False)
        self.mic_result.setObjectName("Muted")
        self.mic_result.setText("Listening for 1.5 seconds…")
        self.mic_result.setVisible(True)
        worker = MicCheckWorker(self.microphone.currentData())
        worker.done.connect(self._on_mic_done)
        worker.failed.connect(self._on_mic_failed)
        self._mic_worker = worker
        worker.start()

    def _on_mic_done(self, level: MicLevel) -> None:
        self.mic_test.setEnabled(True)
        self.mic_result.setObjectName("Success" if level.heard else "Warning")
        self.mic_result.setText(
            f"Microphone heard you clearly (peak {level.peak:.2f})."
            if level.heard
            else "No audio signal — try another microphone."
        )
        self.mic_result.setVisible(True)
        self.mic_result.style().unpolish(self.mic_result)
        self.mic_result.style().polish(self.mic_result)

    def _on_mic_failed(self, message: str) -> None:
        self.mic_test.setEnabled(True)
        self.speech_test.setEnabled(True)
        self.mic_result.setObjectName("Warning")
        self.mic_result.setText(message)
        self.mic_result.setVisible(True)

    def test_speech(self) -> None:
        if self._speech_worker is not None and self._speech_worker.isRunning():
            return
        self.speech_test.setEnabled(False)
        self.mic_result.setObjectName("Muted")
        self.mic_result.setText("Loading speech engine…")
        self.mic_result.setVisible(True)
        worker = _SpeechCheckWorker(self.config, self.paths)
        worker.stage.connect(
            lambda s: self.mic_result.setText(
                "Speak now (4 seconds)…" if s == "recording" else "Transcribing…" if s == "transcribing" else "Loading speech engine…"
            )
        )
        worker.done.connect(self._on_speech_done)
        worker.failed.connect(self._on_mic_failed)
        self._speech_worker = worker
        worker.start()

    def _on_speech_done(self, result: SpeechTestResult) -> None:
        self.speech_test.setEnabled(True)
        self.mic_result.setObjectName("Success" if result.has_speech else "Warning")
        self.mic_result.setText(
            f"Heard: “{result.transcript}” ({result.latency_ms:.0f} ms)"
            if result.transcript
            else "No words recognized."
        )
        self.mic_result.setVisible(True)
        self.mic_result.style().unpolish(self.mic_result)
        self.mic_result.style().polish(self.mic_result)

    def _download_speech_model(self) -> None:
        if self._dl_worker is not None and self._dl_worker.isRunning():
            return
        self.dl_button.setEnabled(False)
        self.dl_button.setText("Downloading…")
        self.dl_progress.setVisible(True)
        self.dl_progress.setValue(0)
        worker = _ModelDownloadWorker(self)
        worker.progress.connect(lambda p: self.dl_progress.setValue(int(p.percent)))
        worker.done.connect(lambda _r: self._finish_dl(""))
        worker.failed.connect(self._finish_dl)
        self._dl_worker = worker
        worker.start()

    def _finish_dl(self, err: str) -> None:
        self._dl_worker = None
        self.dl_progress.setVisible(False)
        self.dl_button.setEnabled(True)
        self.dl_button.setText("Retry download" if err else "Download speech model (630 MB)")
        if err:
            self.stt_status.setText(f"Download failed: {err}")
        else:
            self.refresh()

    # ------------------------------------------------------------------ TTS & Updates

    def _download_tts_model(self) -> None:
        if self._tts_dl_worker is not None and self._tts_dl_worker.isRunning():
            return
        self.tts_dl_button.setEnabled(False)
        self.tts_dl_button.setText("Downloading…")
        worker = _ModelDownloadWorker(self, asset_id="underdog-woof")
        worker.progress.connect(
            lambda p: self.tts_dl_button.setText(f"Downloading {int(p.percent)}%")
        )
        worker.done.connect(lambda _r: self._finish_tts_dl(""))
        worker.failed.connect(self._finish_tts_dl)
        self._tts_dl_worker = worker
        worker.start()

    def _finish_tts_dl(self, err: str) -> None:
        self._tts_dl_worker = None
        self.tts_dl_button.setEnabled(True)
        self.tts_dl_button.setText("Retry voice" if err else "Download voice (40 MB)")
        if err:
            self.tts_status.setText(f"Voice download failed: {err}")
        else:
            self.tts_enabled.setChecked(True)
            self.refresh()

    def _preview_tts(self) -> None:
        from keyless.voice.tts import WoofSpeaker

        if self._tts_speaker is None:
            self._tts_speaker = WoofSpeaker(paths=self.paths, enabled=True)
        voice = str(self.tts_voice.currentData() or "juno")
        self._tts_speaker.speak(
            f"Hi, this is {voice.capitalize()} from Keyless, powered by Underdog Woof.",
            voice=voice,
            interrupt=True,
        )

    def _trigger_check_updates(self) -> None:
        if self._on_check_updates is not None:
            self._on_check_updates()

    def _trigger_apply_update(self) -> None:
        if self._on_apply_update is not None:
            self._on_apply_update()

    def set_update_busy(self, message: str) -> None:
        self.check_updates_btn.setEnabled(False)
        self.update_status.setText(message)
        self.update_status.setVisible(True)

    def apply_update_state(self, state) -> None:
        self.check_updates_btn.setEnabled(True)
        avail = getattr(state, "available_version", None)
        notes = getattr(state, "release_notes", None) or []
        summary = getattr(state, "message", "") or ""
        if notes and avail:
            summary += "\n• " + "\n• ".join(notes[:3])
        self.update_status.setText(summary)
        self.update_status.setVisible(bool(summary))
        self.update_now_btn.setVisible(bool(avail and getattr(state, "status", "") == "available"))
        self.update_now_btn.setEnabled(True)

    def set_update_result(self, ok: bool, message: str) -> None:
        self.check_updates_btn.setEnabled(True)
        self.update_now_btn.setEnabled(not ok)
        self.update_status.setText(message)
        self.update_status.setVisible(bool(message))

    # ------------------------------------------------------------------ Diagnostics

    def _copy_firefox_manifest(self) -> None:
        QGuiApplication.clipboard().setText(self._ff_manifest_path)
        self.ff_copy_btn.setText("Copied ✓")
        QTimer.singleShot(1800, lambda: self.ff_copy_btn.setText("Copy manifest path"))

    def _copy_error(self) -> None:
        from keyless.ui.session import inspect_daemon_error

        rep = inspect_daemon_error(self.paths.logs_dir / "daemon.log", 1)
        QGuiApplication.clipboard().setText(rep.formatted_clipboard_text())

    def _open_log_file(self) -> None:
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.paths.logs_dir / "daemon.log"
        if not log_file.exists():
            log_file.touch()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file)))

    def _open_logs_folder(self) -> None:
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.logs_dir)))

    def expand_error_details(self) -> None:
        """Open the Diagnostics section, load latest daemon log, and scroll to it."""
        if not self.diagnostics.toggle_button.isChecked():
            self.diagnostics.toggle_button.setChecked(True)
        else:
            self.refresh_diagnostics()
        self.scroll.ensureWidgetVisible(self.diagnostics)
        QTimer.singleShot(
            0,
            lambda: self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().maximum()
            ),
        )

    def refresh_diagnostics(self) -> None:
        from keyless.ui.session import inspect_daemon_error

        sections: list[str] = []
        try:
            report = collect_doctor(check_providers=False)
            caps = report.capabilities.get("capabilities", {})
            mapping = {
                "input": (caps.get("pointer", {}).get("status", "unknown"), caps.get("pointer", {}).get("status") == "available"),
                "cameras": (report.cameras.get("mode", "unknown"), report.cameras.get("mode") in ("mono", "stereo")),
                "screen": (caps.get("screen_capture", {}).get("status", "unknown"), caps.get("screen_capture", {}).get("status") == "available"),
                "speech": (report.stt.get("selected", "missing"), report.stt.get("selected") == "parakeet"),
                "ai": (report.providers.get("text_llm", {}).get("status", {}).get("state", "optional"), None),
                "overlay": (caps.get("overlay", {}).get("status", "unknown"), caps.get("overlay", {}).get("status") == "available"),
            }
            for key, (val, ok) in mapping.items():
                lbl = self._diag_badges[key]
                lbl.setText(str(val).replace("_", " ").capitalize())
                lbl.setObjectName("Success" if ok is True else "Warning" if ok is False else "Muted")
                lbl.style().unpolish(lbl)
                lbl.style().polish(lbl)
            sections.append(format_text(report))
        except Exception as exc:
            sections.append(f"Diagnostics unavailable: {exc}")

        log_path = self.paths.logs_dir / "daemon.log"
        if log_path.exists():
            rep = inspect_daemon_error(log_path, 1)
            if rep.log_excerpt:
                self.error_summary_label.setText(f"{rep.summary} — {rep.hint}")
                self.error_summary_label.setVisible(True)
                sections.append(f"--- Recent Daemon Log ({log_path}) ---\n{rep.log_excerpt}")
            else:
                self.error_summary_label.setVisible(False)
        else:
            self.error_summary_label.setVisible(False)

        self.diag_text.setPlainText("\n\n".join(sections))

    # ------------------------------------------------------------------ Persistence

    def _auto_save(self, *_args: object) -> None:
        if self._loading:
            return
        self.save()

    def save(self) -> None:
        cfg = self.config
        cfg.ptt_hotkey = self.hotkey.text().strip() or "KEY_RIGHTCTRL"
        cfg.always_on_voice = self.always_on.isChecked()
        cfg.audio_device = self.microphone.currentData()
        cfg.require_command_prefix = self.require_prefix.isChecked()
        cfg.enable_pop_click = self.pop_click.isChecked()
        cfg.gaze_cursor_control = self.eye_mouse.isChecked()
        cfg.gaze_model = str(self.gaze_model.currentData() or "rbf")
        cfg.gaze_online_ekf = self.online_adapt.isChecked()
        cfg.gaze_online_chase = self.online_adapt.isChecked()
        cfg.camera_backend = "pipewire" if self.share_camera.isChecked() else "v4l2"
        if self.cam_mode.isVisible():
            cfg.camera_mode = str(self.cam_mode.currentData() or "auto")
        if self.cam1.currentData():
            cfg.cam1_by_id = str(self.cam1.currentData())
        if self.cam2.isVisible() and self.cam2.currentData():
            cfg.cam2_by_id = str(self.cam2.currentData())
        cfg.llm_provider = str(self.provider_kind.currentData() or "auto")
        cfg.llm_preset = str(self.provider_preset.currentData() or "openai")
        cfg.llm_model = self.llm_model.text().strip()
        cfg.acp_agent_id = self.acp_agent.text().strip() or "opencode"
        cfg.gaze_vlm_remote_consent = self.vlm_consent.isChecked()
        cfg.tts_enabled = self.tts_enabled.isChecked()
        cfg.tts_voice = str(self.tts_voice.currentData() or "juno")
        cfg.auto_update_checks = self.auto_updates.isChecked()
        cfg.save(self.paths.settings_path)
        if self._on_config_changed is not None:
            self._on_config_changed()

    def stop(self) -> None:
        if self._tts_speaker is not None:
            self._tts_speaker.close()
            self._tts_speaker = None
        for worker in (self._mic_worker, self._speech_worker, self._dl_worker, self._tts_dl_worker):
            if worker is not None and worker.isRunning():
                worker.wait(3000)
