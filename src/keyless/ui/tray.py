"""Tray/menu-bar surface shared by the window and the D3 fallback."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from keyless.ui.session import SessionState


def make_icon(background: str = "#19191a", foreground: str = "#f4f2ec") -> QIcon:
    """Crisp Keyless optical gaze-lock emblem shared by the window and system tray."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(background))
    painter.setPen(QPen(QColor("#2e2e32"), 2))
    painter.drawRoundedRect(QRect(4, 4, 56, 56), 14, 14)

    ring_pen = QPen(QColor(foreground), 3.0)
    ring_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(ring_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(16, 16, 32, 32)
    painter.drawLine(32, 11, 32, 19)
    painter.drawLine(32, 45, 32, 53)
    painter.drawLine(11, 32, 19, 32)
    painter.drawLine(45, 32, 53, 32)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#c95b2b"))
    painter.drawEllipse(25, 25, 14, 14)
    painter.setBrush(QColor(foreground))
    painter.drawEllipse(29, 29, 6, 6)
    painter.end()
    return QIcon(pixmap)


class Tray:
    """One tray icon; the menu mirrors Listening/Paused, Open, Pause, Quit."""

    def __init__(
        self,
        *,
        window: object | None = None,
        on_open: Callable[[], None] | None = None,
        on_pause: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_open_lab: Callable[[], None] | None = None,
    ) -> None:
        self.icon = QSystemTrayIcon(make_icon())
        self.icon.setToolTip("Keyless: stopped")
        self.menu = QMenu()
        self.open_action = QAction("Open Keyless", self.menu)
        self.lab_action = QAction("Open Keyless Lab", self.menu)
        self.pause_action = QAction("Pause", self.menu)
        self.update_action = QAction("Update available…", self.menu)
        self.update_action.setVisible(False)
        self.quit_action = QAction("Quit", self.menu)
        self.open_action.triggered.connect(on_open or (lambda: None))
        self.lab_action.triggered.connect(on_open_lab or (lambda: None))
        self.pause_action.triggered.connect(on_pause or (lambda: None))
        self.quit_action.triggered.connect(on_quit or (lambda: None))
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.lab_action)
        self.menu.addAction(self.pause_action)
        self.menu.addSeparator()
        self.menu.addAction(self.update_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.icon.setContextMenu(self.menu)
        self._window = window

    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def start(self) -> bool:
        if not self.available():
            return False
        self.icon.show()
        self.icon.activated.connect(self._activated)
        return True

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ) and self._window is not None:
            show = getattr(self._window, "show", None)
            if callable(show):
                show()

    def set_state(self, state: SessionState, detail: str = "") -> None:
        labels = {
            SessionState.STOPPED: "stopped",
            SessionState.LISTENING: "listening",
            SessionState.PAUSED: "paused",
            SessionState.ERROR: "error",
        }
        self.icon.setToolTip(f"Keyless: {labels.get(state, state.value)}")
        self.pause_action.setText("Resume" if state is SessionState.PAUSED else "Pause")

    def set_update_available(
        self,
        version: str | None,
        on_trigger: Callable[[], None] | None = None,
    ) -> None:
        if not version:
            self.update_action.setVisible(False)
            return
        self.update_action.setText(f"Update to Keyless {version}…")
        if on_trigger is not None:
            try:
                self.update_action.triggered.disconnect()
            except Exception:
                pass
            self.update_action.triggered.connect(on_trigger)
        self.update_action.setVisible(True)

    def stop(self) -> None:
        self.icon.hide()


def tray_supported() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    return QSystemTrayIcon.isSystemTrayAvailable()
