"""Main window shell: 3 calm views (Home, Activity, Settings) + inline first-run setup."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable

from PySide6.QtCore import QThread, QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.platform.router import get_display_layout, resolve_camera_config
from keyless.ui.components import Card, ContractSections, divider, eyebrow
from keyless.ui.gaze_field import GazeField
from keyless.ui.gaze_region import gaze_region
from keyless.core.history import HistoryStore
from keyless.core.lifecycle import (
    NullController,
    SessionController,
    SessionState,
    inspect_daemon_error,
)
from keyless.core.onboarding import OnboardingFlow
from keyless.ui.history.window import HistoryPanel
from keyless.ui.onboarding.steps import OnboardingWidget, OnboardingWindow
from keyless.ui.settings.window import SettingsPanel
from keyless.ui.theme.tokens import ThemeName, load_bundled_fonts, stylesheet, tokens_for
from keyless.ui.transcript_overlay import TranscriptOverlayController
from keyless.ui.tray import make_icon


class _UpdateWorker(QThread):
    checked = Signal(object)
    applied = Signal(bool, str)

    def __init__(self, updater, *, apply: bool = False, channel: str = "stable") -> None:
        super().__init__()
        self._updater = updater
        self._apply = apply
        self._channel = channel

    def run(self) -> None:
        if self._apply:
            try:
                ok, msg = self._updater.apply_update()
            except Exception as exc:
                ok, msg = False, str(exc)
            self.applied.emit(ok, msg)
            return
        state = self._updater.check_for_updates(channel=self._channel)
        self.checked.emit(state)


class HomePanel(QWidget):
    """Single calm control surface: Start/Pause, live gaze preview, quick switches, recent activity."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self._last_state: SessionState | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(28)

        # ---- Left column: hero state + primary controls + quick toggles
        left = QVBoxLayout()
        left.setSpacing(16)

        # Quiet t3code-style update pill (hidden unless a newer release is found)
        self.update_banner = Card(subtle=True)
        self.update_banner.layout.setContentsMargins(14, 10, 14, 10)
        self.update_banner.layout.setSpacing(8)
        upd_row = QHBoxLayout()
        upd_row.setSpacing(10)
        self.update_label = QLabel("")
        self.update_label.setObjectName("Heading")
        upd_row.addWidget(self.update_label, 1)
        self.update_apply_btn = QPushButton("Update")
        self.update_apply_btn.clicked.connect(window.apply_available_update)
        upd_row.addWidget(self.update_apply_btn)
        self.update_dismiss_btn = QPushButton("Dismiss")
        self.update_dismiss_btn.setObjectName("Ghost")
        self.update_dismiss_btn.clicked.connect(window.dismiss_available_update)
        upd_row.addWidget(self.update_dismiss_btn)
        self.update_banner.layout.addLayout(upd_row)
        self.update_banner.setVisible(False)
        left.addWidget(self.update_banner)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status_dot = QLabel("")
        self.status_dot.setObjectName("StatusDot")
        status_row.addWidget(self.status_dot)
        self.state_label = QLabel("Ready")
        self.state_label.setObjectName("Heading")
        status_row.addWidget(self.state_label)
        status_row.addStretch(1)
        self.setup_link = QPushButton("Finish setup")
        self.setup_link.setObjectName("Link")
        self.setup_link.clicked.connect(window.show_inline_setup)
        status_row.addWidget(self.setup_link)
        left.addLayout(status_row)

        self.headline = QLabel("Ready when you are.")
        self.headline.setObjectName("Display")
        self.headline.setWordWrap(True)
        self.headline.setMaximumWidth(520)
        left.addWidget(self.headline)

        self.detail = QLabel("Speak to click, scroll, and type.")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        self.detail.setMaximumWidth(520)
        left.addWidget(self.detail)

        # User-centric error bar (hidden unless daemon/setup hits an error)
        self.error_actions = QWidget()
        err_row = QHBoxLayout(self.error_actions)
        err_row.setContentsMargins(0, 0, 0, 0)
        err_row.setSpacing(8)
        self.view_error_btn = QPushButton("View error log")
        self.view_error_btn.clicked.connect(window.open_error_diagnostics)
        err_row.addWidget(self.view_error_btn)
        self.copy_error_btn = QPushButton("Copy error")
        self.copy_error_btn.clicked.connect(self._copy_error)
        err_row.addWidget(self.copy_error_btn)
        self.open_logs_btn = QPushButton("Open logs folder")
        self.open_logs_btn.setObjectName("Ghost")
        self.open_logs_btn.clicked.connect(window.open_logs_folder)
        err_row.addWidget(self.open_logs_btn)
        err_row.addStretch(1)
        self.error_actions.setVisible(False)
        left.addWidget(self.error_actions)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.primary = QPushButton("Start listening")
        self.primary.setObjectName("Primary")
        self.primary.clicked.connect(self.toggle)
        actions.addWidget(self.primary)

        self.commands_button = QPushButton("Commands")
        self.commands_button.setObjectName("Ghost")
        self.commands_button.clicked.connect(window.open_commands_help)
        actions.addWidget(self.commands_button)
        actions.addStretch(1)
        left.addLayout(actions)

        # Quick controls card (instant-apply)
        controls = Card()
        controls.layout.addWidget(eyebrow("Controls"))

        self.transcript_button = QPushButton("Show live captions")
        self.transcript_button.setCheckable(True)
        self.transcript_button.clicked.connect(self._toggle_transcript)

        overlay_row = QHBoxLayout()
        overlay_row.setSpacing(10)
        overlay_row.addWidget(self.transcript_button)
        overlay_row.addStretch(1)
        controls.layout.addLayout(overlay_row)

        self.transcript_note = QLabel("")
        self.transcript_note.setObjectName("Muted")
        self.transcript_note.setWordWrap(True)
        self.transcript_note.setVisible(False)
        controls.layout.addWidget(self.transcript_note)

        self.eye_mouse = QCheckBox("Show eye cursor (does not move your mouse)")
        self.eye_mouse.setChecked(window.config.gaze_cursor_control)
        self.eye_mouse.toggled.connect(self._toggle_eye_mouse)
        controls.layout.addWidget(self.eye_mouse)

        left.addWidget(controls)

        # Readiness, Devices and provider, and Recent come from the engine-owned
        # contract document; the hero above stays shell chrome.
        self.contract_sections = ContractSections(
            window.on_ui_action, lambda: window.theme_tokens
        )
        left.addWidget(self.contract_sections)

        left.addStretch(1)
        layout.addLayout(left, 3)

        # ---- Right column: compact live gaze preview card (hugs content cleanly)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        gaze_card = Card()
        gaze_card.setMaximumWidth(360)
        gaze_card.layout.addWidget(eyebrow("Gaze"))
        layout_info = window.display_layout
        self.live_region = gaze_region(layout_info)
        self.gaze_field = GazeField(
            window,
            source_size=(layout_info.total_width, layout_info.total_height),
            live_region=self.live_region,
        )
        self.gaze_field.setFixedHeight(190)
        gaze_card.layout.addWidget(self.gaze_field)

        gaze_card.layout.addWidget(divider())
        self.hardware_summary = QLabel("")
        self.hardware_summary.setObjectName("Muted")
        self.hardware_summary.setWordWrap(True)
        gaze_card.layout.addWidget(self.hardware_summary)
        right.addWidget(gaze_card)
        right.addStretch(1)
        layout.addLayout(right, 2)

        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(1000)
        self._cursor_timer.timeout.connect(self._poll_cursor)
        self._cursor_timer.start()
        self.refresh()

    def _dot_color(self, state: SessionState) -> str:
        tokens = self.window.theme_tokens
        if state is SessionState.LISTENING:
            return tokens.success
        if state is SessionState.ERROR:
            return tokens.warning
        return tokens.muted

    def refresh(self) -> None:
        status = self.window.controller.status
        self.status_dot.setStyleSheet(f"background: {self._dot_color(status.state)}; border-radius: 5px;")

        if status.state is SessionState.LISTENING:
            self.state_label.setText("Listening")
            self.headline.setText("Listening.")
            self.detail.setText("Voice control is active.")
            self.primary.setText("Pause")
        elif status.state is SessionState.PAUSED:
            self.state_label.setText("Paused")
            self.headline.setText("Paused.")
            self.detail.setText("Camera and microphone are off.")
            self.primary.setText("Resume")
        elif status.state is SessionState.ERROR:
            rep = getattr(self.window.controller, "failure_report", None)
            self.state_label.setText("Needs attention")
            self.headline.setText((rep.summary + ".") if rep else "Needs attention.")
            self.detail.setText(rep.hint if rep else (status.detail or "Open Settings → Diagnostics for details."))
            self.primary.setText("Try again")
            self.copy_error_btn.setText("Copy error")
            self.error_actions.setVisible(True)
        else:
            self.error_actions.setVisible(False)
            self.state_label.setText("Ready")
            self.headline.setText("Ready when you are.")
            self.detail.setText("Speak to click, scroll, and type.")
            self.primary.setText("Start listening")

        if status.state is not SessionState.ERROR:
            self.error_actions.setVisible(False)

        # Readiness/Devices/Recent render from the engine contract document; the
        # hero above is the shell's own chrome and covers the Status/Action rows.
        document = self.window.contract_document()
        if document is not None:
            home = document.get("screens", {}).get("home", {})
            self.contract_sections.update_sections(
                [
                    section
                    for section in home.get("sections", ())
                    if str(section.get("title", "")) not in ("Status", "Action")
                ]
            )

        mode, _cam1, cam2 = resolve_camera_config(
            self.window.config.camera_mode,
            self.window.config.cam1_by_id,
            self.window.config.cam2_by_id,
        )
        cams_txt = "2 cameras" if cam2 else "1 camera"
        mons = len(self.window.display_layout.monitors)
        mon_txt = f"{mons} displays" if mons > 1 else "1 display"
        self.hardware_summary.setText(f"{cams_txt}, {mon_txt}")

        self.setup_link.setVisible(not self.window.flow.is_complete)

        self.primary.setObjectName("Primary")
        for btn in (self.primary,):
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        self.eye_mouse.blockSignals(True)
        self.eye_mouse.setChecked(self.window.config.gaze_cursor_control)
        self.eye_mouse.blockSignals(False)

        self.window.primary_action = self.primary
        self._sync_transcript_controls()

    def _toggle_eye_mouse(self, checked: bool) -> None:
        self.window.config.gaze_cursor_control = checked
        self.window.config.save(self.window.paths.settings_path)
        self.window.apply_live_config_change()

    def _poll_cursor(self) -> None:
        if not self.window.transcript_overlay.refresh() and self.transcript_button.isChecked():
            self._sync_transcript_controls()

    def _toggle_transcript(self, checked: bool) -> None:
        if checked:
            if not self.window.transcript_overlay.start():
                self.transcript_button.blockSignals(True)
                self.transcript_button.setChecked(False)
                self.transcript_button.blockSignals(False)
                self.transcript_note.setText(
                    self.window.transcript_overlay.error or "Live captions unavailable."
                )
                self.transcript_note.setVisible(True)
                return
        else:
            self.window.transcript_overlay.stop()
        self.window.config.transcript_overlay = checked
        self.window.config.save(self.window.paths.settings_path)
        self._sync_transcript_controls()

    def _sync_transcript_controls(self) -> None:
        running = self.window.transcript_overlay.running
        self.transcript_button.blockSignals(True)
        self.transcript_button.setChecked(running)
        self.transcript_button.blockSignals(False)
        self.transcript_button.setText("Hide live captions" if running else "Show live captions")
        if self.window.transcript_overlay.error and not running:
            self.transcript_note.setText(self.window.transcript_overlay.error)
            self.transcript_note.setVisible(True)
        elif running and self.window.controller.status.state is not SessionState.LISTENING:
            paused = self.window.controller.status.state is SessionState.PAUSED
            self.transcript_note.setText(
                "Captions appear as you speak. Press Resume to start listening."
                if paused
                else "Captions appear as you speak."
            )
            self.transcript_note.setVisible(True)
        else:
            self.transcript_note.setVisible(False)

    def poll_state(self) -> None:
        state = self.window.controller.status.state
        if state is not self._last_state:
            self._last_state = state
            self.refresh()
            self.window.refresh_tray()

    def shutdown(self) -> None:
        self._cursor_timer.stop()
        self.gaze_field.shutdown()
        self.window.transcript_overlay.stop()

    def _copy_error(self) -> None:
        self.window.copy_latest_error()
        self.copy_error_btn.setText("Copied ✓")
        QTimer.singleShot(1800, lambda: self.copy_error_btn.setText("Copy error"))

    def toggle(self) -> None:
        status = self.window.controller.status
        if status.state is SessionState.LISTENING:
            self.window.controller.pause()
        else:
            self.window.controller.start()
        self._last_state = None
        self.poll_state()


class MainWindow(QMainWindow):
    """Single-window app shell: 3 calm views + inline first-run setup."""

    PAGES = ("Home", "Activity", "Settings")

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        config: AppConfig | None = None,
        controller: SessionController | None = None,
        flow: OnboardingFlow | None = None,
        theme: ThemeName | str | None = None,
        enable_previews: bool = True,
        report_provider: Callable | None = None,
        client=None,
    ) -> None:
        super().__init__()
        self.paths = paths or current_paths()
        self.config = config or AppConfig.load(self.paths.settings_path)
        self.controller = controller or NullController()
        self._control_client = client
        self.transcript_overlay = TranscriptOverlayController(paths=self.paths)
        self.flow = flow or OnboardingFlow(self.paths)
        self.enable_previews = enable_previews
        self.report_provider = report_provider
        self.history = HistoryStore(self.paths)
        self.primary_action: QPushButton | None = None
        self.tray = None
        self.overlay = None
        self._flow_window: OnboardingWindow | None = None
        self._lab_window = None
        self._commands_dialog = None
        self._inline_setup: OnboardingWidget | None = None
        self._update_worker: _UpdateWorker | None = None
        self._updater = None
        self._instance_server = None
        self._control_server = None
        self._contract_source = None
        self._display_layout = None
        self._shutdown = False

        self.setWindowTitle("Keyless")
        self.setWindowIcon(make_icon())
        self.resize(980, 640)
        self.setMinimumSize(820, 540)

        load_bundled_fonts()
        self.theme_tokens = tokens_for(theme or ThemeName.LIGHT)

        # Root stack switches cleanly between the main 3-tab shell and inline first-run setup
        self.root_stack = QStackedWidget()
        self.setCentralWidget(self.root_stack)

        # ---- Main Shell (Index 0)
        shell = QWidget()
        row = QHBoxLayout(shell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(196)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 20, 14, 16)
        side_layout.setSpacing(12)

        brand = QHBoxLayout()
        brand.setSpacing(8)
        wordmark = QLabel("Keyless")
        wordmark.setObjectName("Wordmark")
        brand.addWidget(wordmark)
        brand.addStretch(1)
        side_layout.addLayout(brand)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav_rows: dict[str, int] = {}
        for page in self.PAGES:
            item = QListWidgetItem(page)
            item.setData(Qt.ItemDataRole.UserRole, page)
            self.nav.addItem(item)
            self.nav_rows[page] = self.nav.count() - 1
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._select)
        side_layout.addWidget(self.nav, 1)

        self.lab_button = QPushButton("Keyless Lab")
        self.lab_button.setObjectName("Ghost")
        self.lab_button.clicked.connect(self.open_lab)
        side_layout.addWidget(self.lab_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.theme_button = QPushButton("Dark mode")
        self.theme_button.setObjectName("Ghost")
        self.theme_button.clicked.connect(self.toggle_theme)
        side_layout.addWidget(self.theme_button, 0, Qt.AlignmentFlag.AlignLeft)
        row.addWidget(sidebar)

        content = QWidget()
        content.setObjectName("Content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(32, 28, 32, 24)
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        row.addWidget(content, 1)

        self.home = HomePanel(self)
        self.activity = HistoryPanel(self.history)
        self.settings = SettingsPanel(
            self.config,
            self.paths,
            on_config_changed=self.apply_live_config_change,
            on_calibrate=self.launch_calibration,
            on_run_setup=self.show_inline_setup,
            on_check_updates=lambda: self.check_for_updates(force=True),
            on_apply_update=self.apply_available_update,
            client=self._control_client,
        )
        self.pages: dict[str, QWidget] = {
            "Home": self.home,
            "Activity": self.activity,
            "Settings": self.settings,
        }
        for name in self.PAGES:
            self.stack.addWidget(self.pages[name])

        self.root_stack.addWidget(shell)

        self.set_theme(self.theme_tokens.name)
        self.refresh_tray()
        if self.config.transcript_overlay:
            self.transcript_overlay.start()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self.home.poll_state)
        self._status_timer.start()

        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.setInterval(400)
        self._restart_timer.timeout.connect(self._restart_listening_daemon)

        # Background auto-update check (t3code cadence: 12s after start, then every 4 hours)
        self._update_poll_timer = QTimer(self)
        self._update_poll_timer.setInterval(4 * 60 * 60 * 1000)
        self._update_poll_timer.timeout.connect(lambda: self.check_for_updates(force=False))
        if self.enable_previews and getattr(self.config, "auto_update_checks", True):
            QTimer.singleShot(12_000, lambda: self.check_for_updates(force=False))
            self._update_poll_timer.start()

    @property
    def display_layout(self):
        if self._display_layout is None:
            self._display_layout = get_display_layout()
        return self._display_layout

    def apply_live_config_change(self) -> None:
        self.config = AppConfig.load(self.paths.settings_path)
        self.home.refresh()
        if self.controller.status.state is SessionState.LISTENING:
            self._restart_timer.start()

    def _restart_listening_daemon(self) -> None:
        if self._shutdown or self.controller.status.state is not SessionState.LISTENING:
            return
        self.controller.pause()
        self.controller.start()
        self.home.poll_state()

    def open_lab(self):
        from keyless.ui.lab.window import LabWindow

        if self._lab_window is None:
            self._lab_window = LabWindow(paths=self.paths, theme=self.theme_tokens.name)
        self._lab_window.show()
        self._lab_window.raise_()
        self._lab_window.activateWindow()
        return self._lab_window

    def open_commands_help(self):
        from keyless.ui.commands_help import CommandsHelpDialog

        if self._commands_dialog is None:
            self._commands_dialog = CommandsHelpDialog(self)
        self._commands_dialog.show()
        self._commands_dialog.raise_()
        self._commands_dialog.activateWindow()
        return self._commands_dialog

    def check_for_updates(self, *, force: bool = True) -> None:
        if self._shutdown or (self._update_worker is not None and self._update_worker.isRunning()):
            return
        from keyless.updater import Updater, normalize_channel

        if self._updater is None:
            self._updater = Updater(
                paths=self.paths,
                channel=normalize_channel(getattr(self.config, "update_channel", "stable")),
            )
        if not force and not self._updater.should_auto_check(self.config):
            self._on_update_checked(self._updater.state)
            return
        self.settings.set_update_busy("Checking for updates…")
        worker = _UpdateWorker(
            self._updater,
            apply=False,
            channel=normalize_channel(getattr(self.config, "update_channel", "stable")),
        )
        worker.checked.connect(self._on_update_checked)
        self._update_worker = worker
        worker.start()

    def _on_update_checked(self, state) -> None:
        self._update_worker = None
        skipped = (getattr(self.config, "skipped_update_version", "") or "").strip()
        avail = state.available_version
        show_banner = bool(avail and state.status == "available" and avail != skipped)
        self.home.update_banner.setVisible(show_banner)
        if show_banner and avail:
            self.home.update_label.setText(f"Keyless {avail} is available")
            self.home.update_apply_btn.setEnabled(True)
            self.home.update_apply_btn.setText("Update")
        self.settings.apply_update_state(state)
        if self.tray is not None:
            self.tray.set_update_available(avail if show_banner else None, self.apply_available_update)

    def apply_available_update(self) -> None:
        if self._updater is None or (self._update_worker is not None and self._update_worker.isRunning()):
            return
        self.home.update_apply_btn.setEnabled(False)
        self.home.update_apply_btn.setText("Updating…")
        self.settings.set_update_busy("Installing update…")
        worker = _UpdateWorker(self._updater, apply=True)
        worker.applied.connect(self._on_update_applied)
        self._update_worker = worker
        worker.start()

    def _on_update_applied(self, ok: bool, message: str) -> None:
        self._update_worker = None
        self.home.update_apply_btn.setEnabled(True)
        self.home.update_apply_btn.setText("Update" if not ok else "Updated")
        if ok:
            self.home.update_label.setText(message)
        self.settings.set_update_result(ok, message)

    def dismiss_available_update(self) -> None:
        if self._updater is not None and self._updater.state.available_version:
            self._updater.skip_version(self._updater.state.available_version, self.config)
        self.home.update_banner.setVisible(False)
        if self.tray is not None:
            self.tray.set_update_available(None)

    def launch_calibration(self) -> None:
        QMessageBox.information(
            self,
            "Align the eye cursor",
            "The legacy calibration engine has been removed.\n\n"
            "The new target-level gaze model is not installed yet. "
            "Voice control remains available.",
        )
        self.home.refresh()
        self.settings.refresh()

    def show_inline_setup(self) -> OnboardingWidget:
        """Show the distraction-free setup flow inline inside this single window."""
        if self._inline_setup is not None:
            self.root_stack.removeWidget(self._inline_setup)
            self._inline_setup.close_resources()
            self._inline_setup.deleteLater()
        if self.flow.is_complete:
            self.flow.reset()
        widget = OnboardingWidget(
            flow=self.flow,
            config=self.config,
            paths=self.paths,
            report_provider=self.report_provider,
            enable_previews=self.enable_previews,
            parent=self,
        )
        widget.completed.connect(self._on_inline_setup_complete)
        self._inline_setup = widget
        self.root_stack.addWidget(widget)
        self.root_stack.setCurrentWidget(widget)
        return widget

    def _on_inline_setup_complete(self) -> None:
        if self._lab_window is not None:
            self._lab_window.close()
            self._lab_window = None
        if self._inline_setup is not None:
            self._inline_setup.close_resources()
        self.config = AppConfig.load(self.paths.settings_path)
        self.root_stack.setCurrentIndex(0)
        self.show_page("Home")
        self.settings.refresh()
        self.home.refresh()
        if self.controller.status.state is not SessionState.LISTENING:
            self.controller.start()
            self.home.poll_state()

    def open_onboarding(self) -> OnboardingWindow:
        """Open setup in a standalone dialog (kept for external callers/tests)."""
        window = OnboardingWindow(
            flow=self.flow,
            config=self.config,
            paths=self.paths,
            report_provider=self.report_provider,
            enable_previews=self.enable_previews,
        )
        self._flow_window = window
        window.finished.connect(self._onboarding_finished)
        window.show()
        return window

    def _onboarding_finished(self, _result: int) -> None:
        self.config = AppConfig.load(self.paths.settings_path)
        self.show_page("Home")
        self.settings.refresh()
        self.home.refresh()
        self.flow = self._flow_window.flow if self._flow_window else self.flow

    def set_theme(self, theme: ThemeName | str) -> None:
        self.theme_tokens = tokens_for(theme)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet(self.theme_tokens))
        else:
            self.setStyleSheet(stylesheet(self.theme_tokens))
        if hasattr(self, "theme_button"):
            self.theme_button.setText(
                "Light mode" if self.theme_tokens.name is ThemeName.DARK else "Dark mode"
            )
        if hasattr(self, "home"):
            self.home.refresh()

    def toggle_theme(self) -> None:
        self.set_theme(
            ThemeName.LIGHT
            if self.theme_tokens.name is ThemeName.DARK
            else ThemeName.DARK
        )

    def _select(self, index: int) -> None:
        item = self.nav.item(index)
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if name in self.pages:
            page = self.pages[name]
            self.stack.setCurrentWidget(page)
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                refresh()

    def show_page(self, name: str) -> None:
        # Backwards-compatible alias for older callers referencing "History"
        target = "Activity" if name == "History" else name
        if target in self.pages:
            self.root_stack.setCurrentIndex(0)
            row = self.nav_rows.get(target, 0)
            self.nav.setCurrentRow(row)
            self._select(row)

    def refresh_tray(self) -> None:
        if self.tray is not None:
            status = self.controller.status
            self.tray.set_state(status.state, status.detail)

    def present_window(self) -> None:
        """Bring the existing window forward (used by tray & single-instance activation)."""
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def copy_latest_error(self) -> str:
        rep = getattr(self.controller, "failure_report", None)
        if rep is None:
            rep = inspect_daemon_error(self.paths.logs_dir / "daemon.log", 1)
        text = rep.formatted_clipboard_text()
        QGuiApplication.clipboard().setText(text)
        return text

    def open_logs_folder(self) -> None:
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.logs_dir)))

    def open_error_diagnostics(self) -> None:
        self.show_page("Settings")
        self.settings.expand_error_details()

    def contract_document(self) -> dict[str, object] | None:
        """The engine-owned schema-v1 document this shell renders.

        The shell never assembles contract inputs itself: it reads through the
        running control client when one exists, otherwise through an in-process
        control server built by the same engine factory ``keyless serve`` uses.
        """
        if self._control_client is not None:
            try:
                return self._control_client.ui_state()
            except Exception:
                return None
        server = self._control_server or self._contract_source
        if server is None:
            from keyless.daemon import create_control_server

            server = create_control_server(paths=self.paths)
            self._contract_source = server
        return server.ui_state()

    def on_ui_action(self, action: str, arg: object = None) -> None:
        """Dispatch a contract row action through the engine action registry."""
        if self._control_client is not None:
            try:
                self._control_client.ui_action(action, arg)
            except Exception:
                return
        else:
            if self._control_server is None and self._contract_source is None:
                self.contract_document()
            server = self._control_server or self._contract_source
            try:
                server.run_ui_action({"action": action, "arg": arg})
            except Exception:
                return
        self.home.poll_state()

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._status_timer.stop()
        self._restart_timer.stop()
        self._update_poll_timer.stop()
        if self._update_worker is not None and self._update_worker.isRunning():
            self._update_worker.wait(2000)
        if self._inline_setup is not None:
            self._inline_setup.close_resources()
        self.settings.stop()
        self.home.shutdown()
        self.transcript_overlay.stop()
        if self._instance_server is not None:
            self._instance_server.close()
            self._instance_server = None
        self.controller.quit()
        if self._control_server is not None:
            self._control_server.stop()
            self._control_server = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.tray is not None and self.tray.available():
            event.ignore()
            self.hide()
            return
        self.shutdown()
        super().closeEvent(event)


def run_ui(
    *,
    paths: AppPaths | None = None,
    config: AppConfig | None = None,
    theme: ThemeName | str | None = None,
    controller: SessionController | None = None,
    enable_previews: bool = True,
) -> int:
    from keyless.ui.instance_lock import SingleInstanceServer, try_activate_existing
    from keyless.ui.overlay.linux import create_overlay
    from keyless.ui.theme.tokens import system_theme_name
    from keyless.ui.tray import Tray

    resolved_paths = paths or current_paths()
    if controller is None and try_activate_existing(resolved_paths):
        return 0

    app = QApplication.instance() or QApplication([])
    load_bundled_fonts()
    app.setApplicationName("Keyless")
    app.setApplicationDisplayName("Keyless")
    app.setWindowIcon(make_icon())
    resolved_theme = theme or system_theme_name(app)
    control_server = None
    client = None
    if controller is None:
        from keyless.daemon import create_control_server
        from keyless.ipc.client import ControlClient, HttpSessionController

        control_server = create_control_server(paths=resolved_paths)
        bound = control_server.start(host="127.0.0.1", port=0)
        client = ControlClient(f"http://127.0.0.1:{bound}")
        session_controller: SessionController = HttpSessionController(
            client, paths=resolved_paths
        )
    else:
        session_controller = controller
    window = MainWindow(
        paths=resolved_paths,
        config=config,
        controller=session_controller,
        theme=resolved_theme,
        enable_previews=enable_previews,
        client=client if controller is None else None,
    )
    window._control_server = control_server
    if controller is None:
        server = SingleInstanceServer(resolved_paths, parent=window)
        try:
            if server.start():
                server.activate_requested.connect(window.present_window)
                window._instance_server = server
        except OSError:
            pass
    app.aboutToQuit.connect(window.shutdown)
    tray = Tray(
        window=window,
        on_open=window.present_window,
        on_pause=window.home.toggle,
        on_quit=app.quit,
        on_calibrate=window.launch_calibration,
        on_open_lab=window.open_lab,
    )
    if tray.start():
        window.tray = tray
    window.overlay = create_overlay(tray=tray.icon, env=None)
    # First launch embeds setup directly in the same window — never stacks two windows!
    if not window.flow.is_complete:
        window.show_inline_setup()
    window.show()
    return app.exec()
