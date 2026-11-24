"""Settings surface generated from the config schema, with special capability panels.

The form itself is data: one widget per ``SettingSpec`` (toggle, select, number,
text, path), grouped by ``spec["group"]``. Schema source order is the explicit
``schema`` argument, then ``client.config_schema()``, then the local
``SETTING_SPECS`` registry (offline/test fallback with identical data). Live
specs apply on change; restart specs save and raise a restart hint. Invalid
values are shown inline and never persisted.

Panels that are not ``AppConfig`` fields stay hand-built: live microphone
enumeration with Check mic/Test speech, the speech-model download, TTS preview
and download, the Firefox bridge hint, the keychain API key, voice-command help,
and diagnostics/updates.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import fields as dataclass_fields
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
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
from keyless.config.specs import SETTING_SPECS, spec_for, validate_patch
from keyless.diagnostics.doctor import collect_doctor, format_text
from keyless.models.catalog import get_asset
from keyless.models.downloader import download_asset
from keyless.models.providers.secrets import keyring_available, set_secret
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


def _current_platform() -> str:
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


class SettingsPanel(QWidget):
    """Instant-apply Settings generated from ``SettingSpec``, plus capability panels."""

    def __init__(
        self,
        config: AppConfig | None = None,
        paths: AppPaths | None = None,
        *,
        on_config_changed: Callable[[], None] | None = None,
        on_run_setup: Callable[[], None] | None = None,
        on_check_updates: Callable[[], None] | None = None,
        on_apply_update: Callable[[], None] | None = None,
        client=None,
        schema: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths or current_paths()
        self.config = config or AppConfig.load(self.paths.settings_path)
        self.client = client
        self._on_config_changed = on_config_changed
        self._on_run_setup = on_run_setup
        self._on_check_updates = on_check_updates
        self._on_apply_update = on_apply_update
        self._loading = True
        self._mic_worker: MicCheckWorker | None = None
        self._speech_worker: _SpeechCheckWorker | None = None
        self._dl_worker: _ModelDownloadWorker | None = None
        self._tts_dl_worker: _ModelDownloadWorker | None = None
        self._tts_speaker = None

        self._schema = self._resolve_schema(schema)
        self._specs: dict[str, dict] = {
            str(spec.get("key")): spec for spec in self._schema if spec.get("key")
        }
        self._field_widgets: dict[str, QWidget] = {}
        self._error_labels: dict[str, QLabel] = {}
        self._field_labels: dict[str, QLabel] = {}
        self.form_groups: dict[str, QWidget] = {}

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

        self.restart_hint = QLabel("Restart Keyless to apply the change.")
        self.restart_hint.setObjectName("Muted")
        self.restart_hint.setWordWrap(True)
        self.restart_hint.setVisible(False)
        layout.addWidget(self.restart_hint)

        self._build_schema_form(layout)
        self._build_audio_card(layout)
        self._build_firefox_card(layout)
        self._build_api_key_card(layout)

        # Collapsible Voice Commands Reference
        from keyless.ui.commands_help import CommandsHelpWidget

        self.commands_help = DisclosureSection("Voice commands reference")
        self.commands_help.body_layout.addWidget(CommandsHelpWidget(self, scrollable=False))
        layout.addWidget(self.commands_help)

        self._build_diagnostics(layout)
        layout.addStretch(1)

        # The microphone picker is the editable widget for `audio_device`; it
        # enumerates live devices, so it lives in the special audio panel.
        self._field_widgets.setdefault("audio_device", self.microphone)

        self._loading = False
        self.refresh()

    # ------------------------------------------------------------------ Schema form

    def _resolve_schema(self, schema: list[dict] | None) -> list[dict]:
        if schema is not None:
            return list(schema)
        if self.client is not None:
            try:
                remote = self.client.config_schema()
            except Exception:
                remote = []
            if remote:
                return remote
        return [spec.as_dict() for spec in SETTING_SPECS]

    def _applicable_specs(self) -> list[dict]:
        platform = _current_platform()
        selected: list[dict] = []
        for spec in self._schema:
            if not isinstance(spec, dict) or not spec.get("key"):
                continue
            platforms = spec.get("platforms") or ["all"]
            if "all" not in platforms and platform not in platforms:
                continue
            if spec.get("key") == "audio_device":
                continue  # rendered by the live microphone panel
            selected.append(spec)
        return selected

    def _build_schema_form(self, layout: QVBoxLayout) -> None:
        grouped: dict[str, list[dict]] = {}
        for spec in self._applicable_specs():
            grouped.setdefault(str(spec.get("group") or "Settings"), []).append(spec)

        for group, specs in grouped.items():
            card = Card()
            header = QHBoxLayout()
            header.setSpacing(8)
            header.addWidget(eyebrow(group))
            header.addStretch(1)
            if not any(spec.get("apply") == "restart" for spec in specs):
                badge = QLabel("Applies immediately")
                badge.setObjectName("Muted")
                header.addWidget(badge)
            card.layout.addLayout(header)

            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setVerticalSpacing(10)
            for spec in specs:
                self._add_spec_row(form, spec)
            card.layout.addLayout(form)
            self.form_groups[group] = card
            layout.addWidget(card)

    def _add_spec_row(self, form: QFormLayout, spec: dict) -> None:
        key = str(spec["key"])
        label = str(spec.get("label") or key)
        widget = self._make_widget(spec)
        self._field_widgets[key] = widget
        if key == "ptt_hotkey":
            self.hotkey = widget
        elif key == "always_on_voice":
            self.always_on = widget

        holder = QWidget()
        holder.setObjectName("FieldHolder")
        holder.setMinimumWidth(0)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(widget)

        if spec.get("apply") == "restart":
            badge = QLabel("Restart required")
            badge.setObjectName("Muted")
            column.addWidget(badge)

        error = QLabel("")
        error.setObjectName("Warning")
        error.setWordWrap(True)
        error.setVisible(False)
        column.addWidget(error)
        self._error_labels[key] = error

        if str(spec.get("control")) == "toggle":
            form.addRow("", holder)
        else:
            field_label = QLabel(label)
            field_label.setWordWrap(True)
            field_label.setMinimumWidth(0)
            form.addRow(field_label, holder)
            self._field_labels[key] = field_label

    def _make_widget(self, spec: dict) -> QWidget:
        key = str(spec["key"])
        control = str(spec.get("control", "text"))
        value = getattr(self.config, key, spec.get("default"))
        if key == "ptt_hotkey":
            widget = KeyCaptureButton(str(value or "KEY_RIGHTCTRL"))
            widget.key_changed.connect(lambda _code, k=key: self._on_field_changed(k))
            return widget
        if control == "toggle":
            widget = QCheckBox(str(spec.get("label") or key))
            widget.setChecked(bool(value))
            widget.toggled.connect(lambda _on, k=key: self._on_field_changed(k))
            return widget
        if control == "select":
            combo = _compact_combo(QComboBox())
            for option in spec.get("options") or ():
                combo.addItem(str(option), str(option))
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(lambda _i, k=key: self._on_field_changed(k))
            return combo
        if control == "number":
            if str(spec.get("value_type")) == "int":
                spin = QSpinBox()
                spin.setRange(
                    int(spec.get("minimum") if spec.get("minimum") is not None else -1000000),
                    int(spec.get("maximum") if spec.get("maximum") is not None else 1000000),
                )
                spin.setValue(int(value or 0))
                spin.valueChanged.connect(lambda _v, k=key: self._on_field_changed(k))
                return spin
            dspin = QDoubleSpinBox()
            dspin.setDecimals(3)
            dspin.setSingleStep(0.01)
            dspin.setRange(
                float(spec.get("minimum") if spec.get("minimum") is not None else -1e6),
                float(spec.get("maximum") if spec.get("maximum") is not None else 1e6),
            )
            dspin.setValue(float(value or 0.0))
            dspin.valueChanged.connect(lambda _v, k=key: self._on_field_changed(k))
            return dspin
        line = QLineEdit(str(value if value is not None else ""))
        line.setMinimumWidth(0)
        line.editingFinished.connect(lambda k=key: self._on_field_changed(k))
        return line

    def _field_value(self, key: str):
        widget = self._field_widgets.get(key)
        if widget is None:
            return None
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QSpinBox):
            return int(widget.value())
        if isinstance(widget, QDoubleSpinBox):
            return float(widget.value())
        if isinstance(widget, KeyCaptureButton):
            return widget.text_code() or "KEY_RIGHTCTRL"
        if isinstance(widget, QLineEdit):
            text = widget.text().strip()
            return text
        return None

    def _sync_fields(self) -> None:
        for key, widget in self._field_widgets.items():
            if key == "audio_device":
                continue
            value = getattr(self.config, key, None)
            widget.blockSignals(True)
            try:
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(value or 0))
                elif isinstance(widget, QDoubleSpinBox):
                    widget.setValue(float(value or 0.0))
                elif isinstance(widget, KeyCaptureButton):
                    widget.setText(str(value or "KEY_RIGHTCTRL"))
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value if value is not None else ""))
            finally:
                widget.blockSignals(False)
        self._sync_hotkey_visibility()

    def _sync_hotkey_visibility(self) -> None:
        hotkey = getattr(self, "hotkey", None)
        if hotkey is None:
            return
        show_ptt = not self._field_value("always_on_voice")
        hotkey.setVisible(show_ptt)
        label = self._field_labels.get("ptt_hotkey")
        if label is not None:
            label.setVisible(show_ptt)

    def _on_field_changed(self, key: str) -> None:
        if self._loading:
            return
        if key == "always_on_voice":
            self._sync_hotkey_visibility()
        self._apply_patch({key: self._field_value(key)})

    def _on_microphone_changed(self, _index: int) -> None:
        if self._loading:
            return
        device = self.microphone.currentData()
        self._apply_patch({"audio_device": device})

    def _show_errors(self, errors: dict[str, str]) -> None:
        for key, label in self._error_labels.items():
            message = str(errors.get(key, ""))
            label.setText(message)
            label.setVisible(bool(message))

    def _apply_patch(self, patch: dict) -> bool:
        """Validate, persist, and surface one field change. Returns success."""
        errors = validate_patch(patch)
        if errors:
            self._show_errors(errors)
            return False

        if self.client is not None:
            from keyless.ipc.client import ControlError

            try:
                # The contract action is the UI path; PATCH /v1/config stays
                # reserved for CLI/automation.
                body = self.client.ui_action("set_config", patch)
            except ControlError as exc:
                payload_errors = exc.payload.get("errors")
                self._show_errors(payload_errors if isinstance(payload_errors, dict) else {})
                return False
            fields = {field.name for field in dataclass_fields(AppConfig)}
            raw_config = body.get("config")
            self.config = AppConfig(
                **{
                    key: value
                    for key, value in (raw_config if isinstance(raw_config, dict) else {}).items()
                    if key in fields
                }
            )
            changed = [str(key) for key in body.get("changed", [])]
            restart_required = bool(body.get("restart_required", False))
        else:
            changed = [key for key, value in patch.items() if getattr(self.config, key) != value]
            for key in changed:
                setattr(self.config, key, patch[key])
            if changed:
                self.config.save(self.paths.settings_path)
            restart_required = any(
                (spec := spec_for(key)) is not None and spec.apply == "restart"
                for key in changed
            )

        self._show_errors({})
        if restart_required:
            self.restart_hint.setVisible(True)
        if changed and self._on_config_changed is not None:
            self._on_config_changed()
        return True

    def save(self) -> None:
        """Persist every generated field (used by callers that batch a refresh)."""
        patch: dict[str, object] = {}
        for key in self._field_widgets:
            if key in self._specs:
                patch[key] = self._field_value(key)
        if patch:
            self._apply_patch(patch)

    # ------------------------------------------------------------------ Audio panel

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

    def _build_audio_card(self, layout: QVBoxLayout) -> None:
        voice_card = Card()
        voice_card.layout.addWidget(eyebrow("Microphone & speech"))

        voice_form = QFormLayout()
        voice_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        voice_form.setVerticalSpacing(10)

        self.microphone = _compact_combo(QComboBox())
        self._populate_microphones()
        self.microphone.currentIndexChanged.connect(self._on_microphone_changed)
        voice_form.addRow("Microphone", self.microphone)

        mic_actions = QHBoxLayout()
        mic_actions.setSpacing(8)
        self.mic_test = QPushButton("Check mic")
        self.mic_test.clicked.connect(self.check_mic)
        mic_actions.addWidget(self.mic_test)
        self.speech_test = QPushButton("Test speech")
        self.speech_test.clicked.connect(self.test_speech)
        mic_actions.addWidget(self.speech_test)
        mic_actions.addStretch(1)
        voice_form.addRow("", mic_actions)

        self.mic_result = _wrap_label(QLabel(""))
        self.mic_result.setObjectName("Muted")
        self.mic_result.setVisible(False)
        voice_form.addRow("", self.mic_result)
        voice_card.layout.addLayout(voice_form)

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

        tts_row = QHBoxLayout()
        tts_row.setSpacing(8)
        self.tts_preview_btn = QPushButton("Test voice")
        self.tts_preview_btn.clicked.connect(self._preview_tts)
        tts_row.addWidget(self.tts_preview_btn)
        self.tts_dl_button = QPushButton("Download voice (40 MB)")
        self.tts_dl_button.clicked.connect(self._download_tts_model)
        tts_row.addWidget(self.tts_dl_button)
        tts_row.addStretch(1)
        voice_card.layout.addLayout(tts_row)

        self.tts_status = _wrap_label(QLabel("Voice by Underdog Woof (non-commercial license)"))
        self.tts_status.setObjectName("Muted")
        voice_card.layout.addWidget(self.tts_status)
        layout.addWidget(voice_card)

    # ------------------------------------------------------------------ Firefox panel

    def _build_firefox_card(self, layout: QVBoxLayout) -> None:
        ff_card = Card()
        ff_card.layout.addWidget(eyebrow("Firefox extension (DOM clicking & scrolling)"))
        ff_desc = _wrap_label(
            QLabel(
                "For instant, pixel-accurate clicking, tab switching, and smooth scrolling in "
                "Firefox, open about:debugging#/runtime/this-firefox → Load Temporary Add-on "
                "and select firefox_ext/manifest.json."
            )
        )
        ff_desc.setObjectName("Muted")
        ff_card.layout.addWidget(ff_desc)
        manifest_path = Path(__file__).resolve().parents[4] / "firefox_ext" / "manifest.json"
        self._ff_manifest_path = (
            str(manifest_path) if manifest_path.exists() else "firefox_ext/manifest.json"
        )
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

    # ------------------------------------------------------------------ API key panel

    def _build_api_key_card(self, layout: QVBoxLayout) -> None:
        ai_card = Card()
        ai_card.layout.addWidget(eyebrow("API key (optional)"))
        key_form = QFormLayout()
        key_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        key_form.setVerticalSpacing(8)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(
            "Stored in OS keychain" if keyring_available() else "OS keychain unavailable"
        )
        self.api_key.setEnabled(keyring_available())
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_row.addWidget(self.api_key, 1)
        self.save_key_button = QPushButton("Store key")
        self.save_key_button.setEnabled(keyring_available())
        self.save_key_button.clicked.connect(self._store_api_key)
        key_row.addWidget(self.save_key_button)
        key_form.addRow("API key", key_row)
        self.ai_note = _wrap_label(QLabel(""))
        self.ai_note.setObjectName("Muted")
        self.ai_note.setVisible(False)
        key_form.addRow("", self.ai_note)
        ai_card.layout.addLayout(key_form)
        layout.addWidget(ai_card)

    def _store_api_key(self) -> None:
        key = self.api_key.text().strip()
        if not key:
            return
        preset = str(self.config.llm_preset or "openai")
        set_secret(preset, key)
        self.api_key.clear()
        self.ai_note.setText("Key saved securely in the OS keychain.")
        self.ai_note.setObjectName("Success")
        self.ai_note.setVisible(True)
        if self._on_config_changed is not None:
            self._on_config_changed()

    # ------------------------------------------------------------------ Diagnostics

    def _build_diagnostics(self, layout: QVBoxLayout) -> None:
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
        copy_diag.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.diag_text.toPlainText())
        )
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
        upd_row.addStretch(1)
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
                "Speak now (4 seconds)…"
                if s == "recording"
                else "Transcribing…"
                if s == "transcribing"
                else "Loading speech engine…"
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
            self._apply_patch({"tts_enabled": True})
            self.refresh()

    def _preview_tts(self) -> None:
        from keyless.voice.tts import WoofSpeaker

        if self._tts_speaker is None:
            self._tts_speaker = WoofSpeaker(paths=self.paths, enabled=True)
        voice = str(self.config.tts_voice or "juno")
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
        self.update_now_btn.setVisible(
            bool(avail and getattr(state, "status", "") == "available")
        )
        self.update_now_btn.setEnabled(True)

    def set_update_result(self, ok: bool, message: str) -> None:
        self.check_updates_btn.setEnabled(True)
        self.update_now_btn.setEnabled(not ok)
        self.update_status.setText(message)
        self.update_status.setVisible(bool(message))

    # ------------------------------------------------------------------ Helpers

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
                "input": (
                    caps.get("pointer", {}).get("status", "unknown"),
                    caps.get("pointer", {}).get("status") == "available",
                ),
                "cameras": (
                    report.cameras.get("mode", "unknown"),
                    report.cameras.get("mode") in ("mono", "stereo"),
                ),
                "screen": (
                    caps.get("screen_capture", {}).get("status", "unknown"),
                    caps.get("screen_capture", {}).get("status") == "available",
                ),
                "speech": (
                    report.stt.get("selected", "missing"),
                    report.stt.get("selected") == "parakeet",
                ),
                "ai": (
                    report.providers.get("text_llm", {}).get("status", {}).get("state", "optional"),
                    None,
                ),
                "overlay": (
                    caps.get("overlay", {}).get("status", "unknown"),
                    caps.get("overlay", {}).get("status") == "available",
                ),
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

    def refresh(self) -> None:
        self._loading = True
        try:
            self._sync_fields()
            report = stt_report(self.config, paths=self.paths)
            ready = report.get("selected") == "parakeet"
            self.stt_status.setText(
                "Speech model ready (offline)" if ready else "Speech model not downloaded"
            )
            self.dl_button.setVisible(not ready)
            self.speech_test.setEnabled(ready)

            try:
                from keyless.voice.tts import tts_report

                t_rep = tts_report(self.paths)
                t_ready = bool(t_rep["ready"])
                self.tts_dl_button.setVisible(not t_ready)
                self.tts_preview_btn.setEnabled(t_ready)
                self.tts_status.setText(
                    "Voice by Underdog Woof (ready offline, non-commercial license)"
                    if t_ready
                    else "Voice by Underdog Woof (optional 40 MB download, non-commercial license)"
                )
            except Exception:
                pass
        finally:
            self._loading = False

    def stop(self) -> None:
        if self._tts_speaker is not None:
            self._tts_speaker.close()
            self._tts_speaker = None
        for worker in (self._mic_worker, self._speech_worker, self._dl_worker, self._tts_dl_worker):
            if worker is not None and worker.isRunning():
                worker.wait(3000)
