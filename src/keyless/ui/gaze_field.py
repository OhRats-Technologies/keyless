"""Live gaze preview driven by the model-agnostic shared-memory channel.

No calibration, regression, or model code lives here: the widget reads the
same ``(x, y, flags)`` snapshot the daemon consumes and paints one dot.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from keyless.ipc.state import SharedGazeReader


class GazeField(QWidget):
    mode_changed = Signal(str)

    def __init__(
        self,
        window=None,  # type: ignore[no-untyped-def]
        *,
        source_size: tuple[int, int] = (1920, 1080),
        live_region=None,  # type: ignore[no-untyped-def]
    ) -> None:
        super().__init__()
        self._source_w, self._source_h = source_size
        self._reader: SharedGazeReader | None = None
        try:
            self._reader = SharedGazeReader()
        except FileNotFoundError:
            self._reader = None
        self._dot: tuple[float, float] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self) -> None:
        if self._reader is None:
            return
        try:
            state = self._reader.read()
        except Exception:
            return
        if state is not None:
            self._dot = (state.x, state.y)
            self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(12, 14, 20))
        if self._dot is None:
            return
        x = self._dot[0] / max(1, self._source_w) * self.width()
        y = self._dot[1] / max(1, self._source_h) * self.height()
        painter.setPen(QPen(QColor(40, 40, 48), 1))
        painter.drawLine(0, int(y), self.width(), int(y))
        painter.drawLine(int(x), 0, int(x), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(120, 190, 255))
        painter.drawEllipse(int(x) - 5, int(y) - 5, 10, 10)

    def shutdown(self) -> None:
        self._timer.stop()
        if self._reader is not None:
            self._reader.close()
            self._reader = None
