"""Click-through Parakeet transcript overlay for X11 and XWayland."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from keyless.config.paths import AppPaths, current_paths
from keyless.ipc.transcript import TranscriptState, read_transcript
from keyless.ui.theme.tokens import ThemeName, tokens_for

TRANSCRIPT_TTL_S = 6.0
OVERLAY_WIDTH = 760
OVERLAY_MARGIN = 46


def _alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TranscriptOverlay(QWidget):
    """A bottom-center card that appears only when a new transcript arrives."""

    def __init__(self, path, *, parent_pid: int | None = None) -> None:
        super().__init__()
        self.path = Path(path)
        self.parent_pid = parent_pid
        initial = read_transcript(self.path)
        self._last_seq = initial.seq if initial is not None else 0
        self._shown_at = 0.0
        self._tokens = tokens_for(ThemeName.DARK)

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
            flags |= Qt.WindowType.BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Keyless Transcript")
        self.setFixedWidth(OVERLAY_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(6)
        self.eyebrow = QLabel("Live captions")
        self.eyebrow.setFont(QFont("Inter", 9, QFont.Weight.DemiBold))
        self.eyebrow.setStyleSheet("color: #928f87; background: transparent;")
        layout.addWidget(self.eyebrow)
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setFont(QFont("Inter", 13))
        self.text.setStyleSheet("color: #efede5; background: transparent;")
        layout.addWidget(self.text)
        self.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_parent)
        self._watchdog.start()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(99, 73, 54, 230), 1))
        painter.setBrush(QColor(20, 20, 20, 224))
        painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    def _poll(self) -> None:
        state = read_transcript(self.path)
        now = time.monotonic()
        if state is not None and state.seq > self._last_seq:
            if state.age_s <= 8.0:
                self._show_text(state, now)
            self._last_seq = state.seq
        if self.isVisible() and now - self._shown_at > TRANSCRIPT_TTL_S:
            self.hide()

    def _show_text(self, state: TranscriptState, now: float) -> None:
        self.text.setText(state.text)
        color = "#efede5" if state.final else "#b9b5ab"
        self.text.setStyleSheet(f"color: {color}; background: transparent;")
        self.text.adjustSize()
        metrics = self.text.fontMetrics()
        text_height = metrics.boundingRect(
            QRect(0, 0, OVERLAY_WIDTH - 40, 600),
            Qt.TextFlag.TextWordWrap,
            state.text,
        ).height()
        height = max(92, min(230, 56 + text_height))
        screen = self.screen() or QGuiApplication.primaryScreen()
        geometry: QRect = (
            screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        )
        self.setGeometry(
            geometry.x() + (geometry.width() - OVERLAY_WIDTH) // 2,
            geometry.y() + geometry.height() - height - OVERLAY_MARGIN,
            OVERLAY_WIDTH,
            height,
        )
        self._shown_at = now
        self.show()
        self.raise_()

    def _check_parent(self) -> None:
        if self.parent_pid is not None and not _alive(self.parent_pid):
            QApplication.instance().quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self._watchdog.stop()
        super().closeEvent(event)


class TranscriptOverlayController:
    """Own the overlay child process; the daemon writes the transcript file."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        python: str | None = None,
    ) -> None:
        self.paths = paths or current_paths()
        self.python = python or sys.executable
        self._proc: subprocess.Popen[bytes] | None = None
        self._log: object | None = None
        self._error = ""

    @staticmethod
    def availability(env: dict[str, str] | None = None) -> tuple[bool, str]:
        environ = os.environ if env is None else env
        if sys.platform != "linux":
            return False, "The transcript overlay is currently Linux-only."
        if not environ.get("DISPLAY"):
            return False, "No X11/XWayland display; use the in-app transcript."
        return True, ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> bool:
        if self.running:
            return True
        ok, reason = self.availability()
        if not ok:
            self._error = reason
            return False
        self.paths.ensure_directories()
        log_path = self.paths.logs_dir / "transcript-overlay.log"
        try:
            log = log_path.open("ab")
            self._log = log
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "xcb"
            command = [
                self.python,
                "-m",
                "keyless.ui.transcript_overlay",
                "--parent-pid",
                str(os.getpid()),
                "--path",
                str(self.paths.transcript_path),
            ]
            self._proc = subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._error = f"Could not start the transcript overlay: {exc}"
            self._close_log()
            return False
        self._error = ""
        return True

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._close_log()

    def refresh(self) -> bool:
        if self._proc is not None:
            code = self._proc.poll()
            if code is not None:
                self._proc = None
                self._close_log()
                self._error = (
                    f"Transcript overlay exited with code {code}. "
                    f"See {self.paths.logs_dir / 'transcript-overlay.log'}."
                )
        return self.running

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._log = None

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keyless transcript overlay")
    parser.add_argument("--path", default=None)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--duration", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    QApplication.setApplicationName("Keyless Transcript")
    app = QApplication([sys.argv[0]])
    overlay = TranscriptOverlay(
        args.path or current_paths().transcript_path,
        parent_pid=args.parent_pid,
    )
    if args.duration is not None:
        QTimer.singleShot(max(0, int(args.duration * 1000)), app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
