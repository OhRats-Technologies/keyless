"""Small shared surfaces and calm controls for the OhRats desktop kit."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    """A clean bordered surface with generous padding and a vertical layout."""

    def __init__(self, parent: QWidget | None = None, *, subtle: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("SubtleCard" if subtle else "Card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 18, 20, 18)
        self.layout.setSpacing(12)


def eyebrow(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def status(text: str, ok: bool | None = None) -> QLabel:
    label = QLabel(text)
    if ok is True:
        label.setObjectName("Success")
    elif ok is False:
        label.setObjectName("Warning")
    else:
        label.setObjectName("Muted")
    return label


def mono(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Mono")
    return label


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    return line


_FRIENDLY_KEYS: dict[str, str] = {
    "KEY_RIGHTCTRL": "Right Control",
    "KEY_LEFTCTRL": "Left Control",
    "KEY_RIGHTALT": "Right Alt",
    "KEY_LEFTALT": "Left Alt",
    "KEY_RIGHTSHIFT": "Right Shift",
    "KEY_LEFTSHIFT": "Left Shift",
    "KEY_CAPSLOCK": "Caps Lock",
    "KEY_SPACE": "Space",
    "KEY_SCROLLLOCK": "Scroll Lock",
    "KEY_PAUSE": "Pause",
}

_QT_TO_EVDEV: dict[int, str] = {
    int(Qt.Key.Key_Control): "KEY_RIGHTCTRL",
    int(Qt.Key.Key_Alt): "KEY_RIGHTALT",
    int(Qt.Key.Key_Shift): "KEY_RIGHTSHIFT",
    int(Qt.Key.Key_CapsLock): "KEY_CAPSLOCK",
    int(Qt.Key.Key_Space): "KEY_SPACE",
    int(Qt.Key.Key_ScrollLock): "KEY_SCROLLLOCK",
    int(Qt.Key.Key_Pause): "KEY_PAUSE",
    **{int(getattr(Qt.Key, f"Key_F{n}")): f"KEY_F{n}" for n in range(1, 13)},
}


def friendly_key_name(code: str) -> str:
    """Display a Linux evdev keycode (e.g. KEY_RIGHTCTRL) as a human shortcut label."""
    norm = (code or "").strip().upper()
    if norm in _FRIENDLY_KEYS:
        return _FRIENDLY_KEYS[norm]
    if norm.startswith("KEY_"):
        return norm[4:].replace("_", " ").title()
    return norm or "Right Control"


class KeyCaptureButton(QPushButton):
    """Click to record a push-to-talk key; displays a friendly name while storing evdev code."""

    key_changed = Signal(str)

    def __init__(self, key_code: str = "KEY_RIGHTCTRL", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("KeyCap")
        self._code = key_code or "KEY_RIGHTCTRL"
        self._listening = False
        self.clicked.connect(self._begin_capture)
        self._sync_label()

    def text_code(self) -> str:
        return self._code

    def text(self) -> str:  # Compatibility for tests calling .hotkey.text()
        return self._code

    def setText(self, code: str) -> None:  # noqa: N802 - compatibility for .hotkey.setText("KEY_F9")
        self._code = (code or "KEY_RIGHTCTRL").strip()
        self._listening = False
        self._sync_label()
        self.key_changed.emit(self._code)

    def _sync_label(self) -> None:
        super().setText("Press any key…" if self._listening else friendly_key_name(self._code))

    def _begin_capture(self) -> None:
        self._listening = True
        self._sync_label()
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if not self._listening:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Escape:
            self._listening = False
            self._sync_label()
            return
        mapped = _QT_TO_EVDEV.get(int(event.key()))
        if mapped is None:
            txt = event.text().strip().upper()
            if len(txt) == 1 and txt.isalnum():
                mapped = f"KEY_{txt}"
        if mapped:
            self.setText(mapped)
        else:
            self._listening = False
            self._sync_label()


_STATE_STYLE = {"ok": "Success", "warn": "Warning", "bad": "Warning", "idle": "Muted"}


def _status_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget:
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(8)
    dot = QLabel("")
    dot.setObjectName("StatusDot")
    color = {
        "ok": tokens.success,
        "warn": tokens.warning,
        "bad": tokens.danger,
    }.get(str(row.get("state", "idle")), tokens.muted)
    dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
    line.addWidget(dot)
    title = QLabel(str(row.get("title", "")))
    title.setObjectName("BodyStrong")
    line.addWidget(title)
    detail = str(row.get("detail", ""))
    if detail:
        note = QLabel(detail)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        line.addWidget(note, 1)
    else:
        line.addStretch(1)
    return holder


def _action_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget:
    button = QPushButton(str(row.get("title", "")))
    button.setObjectName("Primary" if row.get("style") == "primary" else "Ghost")
    button.setEnabled(bool(row.get("enabled", True)))
    action = str(row.get("action", ""))
    button.clicked.connect(lambda: on_action(action, None))
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.addWidget(button)
    line.addStretch(1)
    return holder


def _permission_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget:
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(8)
    title = QLabel(str(row.get("title", "")))
    title.setObjectName("BodyStrong")
    line.addWidget(title)
    state = str(row.get("state", "unknown"))
    note = QLabel(state.replace("_", " "))
    note.setObjectName(_STATE_STYLE.get("ok" if state == "granted" else "warn"))
    line.addWidget(note)
    if state != "granted":
        button = QPushButton("Allow")
        button.setObjectName("Ghost")
        permission_id = str(row.get("id", ""))
        button.clicked.connect(lambda: on_action("allow", {"permission": permission_id}))
        line.addWidget(button)
    line.addStretch(1)
    return holder


def _progress_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget:
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(8)
    title = QLabel(str(row.get("title", "")))
    title.setObjectName("BodyStrong")
    line.addWidget(title)
    bar = QProgressBar()
    bar.setRange(0, 0) if row.get("indeterminate") else bar.setRange(0, 100)
    fraction = row.get("fraction")
    if not row.get("indeterminate") and isinstance(fraction, (int, float)):
        bar.setValue(round(float(fraction) * 100))
    line.addWidget(bar, 1)
    return holder


def _picker_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget:
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(8)
    title = QLabel(str(row.get("title", "")))
    title.setObjectName("BodyStrong")
    line.addWidget(title)
    combo = QComboBox()
    selected = row.get("selected")
    for option in row.get("options") or ():
        value = option.get("value")
        combo.addItem(str(option.get("label", "")), value)
        if value == selected:
            combo.setCurrentIndex(combo.count() - 1)
    action = str(row.get("action", ""))
    key = str(row.get("key", ""))

    def _chosen(index: int) -> None:
        if action and key:
            on_action(action, {key: combo.itemData(index)})

    combo.currentIndexChanged.connect(_chosen)
    line.addWidget(combo, 1)
    return holder


def _text_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget:
    title = QLabel(str(row.get("title", "")))
    title.setObjectName("BodyStrong")
    title.setWordWrap(True)
    detail = str(row.get("detail", ""))
    if not detail:
        return title
    note = QLabel(detail)
    note.setObjectName("Muted")
    note.setWordWrap(True)
    holder = QWidget()
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(8)
    line.addWidget(title)
    line.addWidget(note, 1)
    return holder


_BUILDERS: dict[str, Callable[[dict, Callable[[str, object], None], object], QWidget]] = {
    "status": _status_row,
    "action": _action_row,
    "permission": _permission_row,
    "progress": _progress_row,
    "picker": _picker_row,
    "text": _text_row,
}


def build_row(row: dict, on_action: Callable[[str, object], None], tokens) -> QWidget | None:
    """Build the widget for one schema-v1 row; ``None`` for unknown kinds."""
    builder = _BUILDERS.get(str(row.get("kind", "")))
    return builder(row, on_action, tokens) if builder is not None else None


class ContractSections(QWidget):
    """Generic renderer for schema-v1 sections: the only Home/Settings/Activity body."""

    def __init__(
        self,
        on_action: Callable[[str, object], None],
        tokens_provider: Callable[[], object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_action = on_action
        self._tokens_provider = tokens_provider
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        self._root = root
        self._cards: list[Card] = []

    def update_sections(self, sections: list[dict]) -> None:
        for card in self._cards:
            self._root.removeWidget(card)
            card.deleteLater()
        self._cards = []
        tokens = self._tokens_provider()
        for section in sections:
            card = Card(subtle=True)
            card.layout.addWidget(eyebrow(str(section.get("title", ""))))
            for row in section.get("rows") or ():
                widget = build_row(row, self._on_action, tokens)
                if widget is not None:
                    card.layout.addWidget(widget)
            self._root.addWidget(card)
            self._cards.append(card)

    def clear_sections(self) -> None:
        self.update_sections([])


class DisclosureSection(QWidget):
    """Collapsible advanced section hidden by default to keep primary screens calm."""

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        on_expand: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._on_expand = on_expand
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(8)
        self.toggle_button = QPushButton(f"▸  {title}")
        self.toggle_button.setObjectName("Ghost")
        self.toggle_button.setCheckable(True)
        self.toggle_button.toggled.connect(self._on_toggled)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.toggle_button)
        header.addStretch(1)
        root.addLayout(header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(12)
        self.body.setVisible(False)
        root.addWidget(self.body)

    def _on_toggled(self, expanded: bool) -> None:
        self.toggle_button.setText(f"{'▾' if expanded else '▸'}  {self._title}")
        self.body.setVisible(expanded)
        if expanded and self._on_expand is not None:
            self._on_expand()
