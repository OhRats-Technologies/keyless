"""Structured voice command reference and searchable Qt help surface."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from keyless.ui.components import Card, eyebrow
from keyless.ui.theme.tokens import load_bundled_fonts
from keyless.ui.tray import make_icon


@dataclass(frozen=True)
class CommandEntry:
    phrase: str
    description: str
    example: str = ""


@dataclass(frozen=True)
class CommandCategory:
    title: str
    summary: str
    commands: tuple[CommandEntry, ...]


COMMAND_CATEGORIES: tuple[CommandCategory, ...] = (
    CommandCategory(
        title="Gaze & Clicking",
        summary="Look at a UI element on any monitor and speak the action.",
        commands=(
            CommandEntry(
                "click <target>",
                "Left-click the button, link, or icon near your gaze.",
                "“click Submit”, “click Settings”",
            ),
            CommandEntry(
                "double click <target> / right click <target>",
                "Double-click or right-click the named target near your gaze.",
                "“right click file”, “double click folder”",
            ),
            CommandEntry(
                "hover <target> / move to <target>",
                "Move the pointer onto the named UI element without clicking.",
                "“hover menu”",
            ),
            CommandEntry(
                "click here / tap / left click / right click / double click",
                "Click immediately at the current pointer location.",
                "“click here”, “right click”",
            ),
            CommandEntry(
                "click <field> and type <text>",
                "Focus an input field near your gaze and type into it in one breath.",
                "“click search and type mechanical keyboards and press enter”",
            ),
            CommandEntry(
                "type <text> in <field>",
                "Click the target field and type the requested text.",
                "“type hello in message box”",
            ),
            CommandEntry(
                "recenter gaze / recenter",
                "Look at the center of your current screen to snap calibration offset.",
                "“recenter gaze”",
            ),
            CommandEntry(
                "steer to mouse / adjust",
                "Look at your mouse and snap the eye cursor to it; the correction applies immediately.",
                "“steer to mouse”",
            ),
            CommandEntry(
                "save icon as <name>",
                "Remember the icon under your gaze for instant future clicks.",
                "“save icon as compose”",
            ),
        ),
    ),
    CommandCategory(
        title="Scrolling & Reading",
        summary="Scroll the window or webpage under your gaze.",
        commands=(
            CommandEntry(
                "scroll down / scroll up / scroll left / scroll right",
                "Scroll in any direction (add “a little” or “a lot” for step size).",
                "“scroll down”, “scroll down a lot”, “scroll up a little”",
            ),
            CommandEntry(
                "page down / page up",
                "Jump by a full page down or up.",
                "“page down”, “next page”",
            ),
            CommandEntry(
                "scroll to top / scroll to bottom",
                "Jump to the top or bottom of the page.",
                "“scroll to top”, “bottom of page”",
            ),
            CommandEntry(
                "auto scroll / keep scrolling / stop scrolling",
                "Start or stop continuous hands-free reading scroll in Firefox.",
                "“auto scroll”, “scroll faster”, “scroll slower”, “stop scrolling”",
            ),
        ),
    ),
    CommandCategory(
        title="Typing, Editing & Keys",
        summary="Dictate text, trigger shortcuts, or chain trailing actions in one breath.",
        commands=(
            CommandEntry(
                "type <text> / dictate <text>",
                "Type literal text at the active cursor.",
                "“type hello world”, “type git status and press enter”",
            ),
            CommandEntry(
                "press enter / press escape / tab / space / backspace",
                "Send a single key press.",
                "“press enter”, “press escape”",
            ),
            CommandEntry(
                "delete line",
                "Select and delete the current line.",
                "“delete line”",
            ),
            CommandEntry(
                "copy / paste / cut / select all",
                "Standard clipboard and selection shortcuts.",
                "“select all”, “copy”, “paste”",
            ),
            CommandEntry(
                "undo / redo",
                "Undo or redo the last edit.",
                "“undo”, “redo”",
            ),
            CommandEntry(
                "… and press enter / … and submit / … and click <target>",
                "Chain a trailing Enter or click onto the end of a typing command.",
                "“type pytest and press enter”",
            ),
        ),
    ),
    CommandCategory(
        title="Windows, Apps & Workspaces",
        summary="Switch apps, cycle windows, or navigate virtual desktops.",
        commands=(
            CommandEntry(
                "next window / previous window / switch window / alt tab",
                "Cycle forward or backward through open windows.",
                "“next window”, “previous window”",
            ),
            CommandEntry(
                "focus <app> / switch window to <app> / <app> window",
                "Focus or launch an application by name.",
                "“focus terminal”, “firefox window”, “switch app to code”",
            ),
            CommandEntry(
                "switch same app / next app window",
                "Cycle windows belonging to the current application (Alt+`).",
                "“switch same app”",
            ),
            CommandEntry(
                "close window / close app",
                "Close the active window (Alt+F4).",
                "“close window”",
            ),
            CommandEntry(
                "overview / show windows / activities",
                "Toggle the desktop window overview.",
                "“show windows”",
            ),
            CommandEntry(
                "next workspace / previous workspace",
                "Move right or left between workspaces.",
                "“next workspace”, “workspace left”",
            ),
        ),
    ),
    CommandCategory(
        title="Browser & Media",
        summary="Control tabs, history, and video playback.",
        commands=(
            CommandEntry(
                "new tab / new tab <url or query> / close tab",
                "Open a blank or searched browser tab, or close the current tab.",
                "“new tab”, “new tab github.com”, “close tab”",
            ),
            CommandEntry(
                "next tab / previous tab / go to tab <name or 1..8>",
                "Switch browser tabs sequentially, by title keyword, or by number.",
                "“next tab”, “second tab”, “go to tab docs”",
            ),
            CommandEntry(
                "go back / go forward",
                "Navigate backward or forward in history.",
                "“go back”, “go forward”",
            ),
            CommandEntry(
                "play / pause / resume / mute / unmute",
                "Control media or video playback.",
                "“pause video”, “mute”",
            ),
            CommandEntry(
                "fullscreen / exit fullscreen",
                "Enter or exit full-screen video mode.",
                "“fullscreen”, “exit fullscreen”",
            ),
        ),
    ),
    CommandCategory(
        title="AI Assistant & Terminal (Ollama / API / ACP)",
        summary="Use your local Ollama model or configured AI provider for shell and Q&A.",
        commands=(
            CommandEntry(
                "ask <question> / assistant <question> / explain <topic>",
                "Ask a question and receive a concise answer (spoken aloud when voice is enabled).",
                "“ask what port is postgres using”, “explain git rebase”",
            ),
            CommandEntry(
                "terminal <request> / shell <request> / bash <request>",
                "Generate a Linux bash command from natural language and type it onto your prompt.",
                "“terminal find all python files modified today”",
            ),
            CommandEntry(
                "what's this / summarize this / browser <instruction>",
                "Run a gaze-contextual browser action via the Firefox bridge.",
                "“summarize this”, “browser fill search with keyless”",
            ),
        ),
    ),
)


def filter_command_categories(query: str = "") -> list[CommandCategory]:
    """Filter command categories by a case-insensitive substring query."""
    q = (query or "").strip().lower()
    if not q:
        return list(COMMAND_CATEGORIES)
    tokens = [t for t in q.split() if t]
    filtered: list[CommandCategory] = []
    for cat in COMMAND_CATEGORIES:
        cat_hay = f"{cat.title} {cat.summary}".lower()
        if all(t in cat_hay for t in tokens):
            filtered.append(cat)
            continue
        matched = tuple(
            cmd
            for cmd in cat.commands
            if all(t in f"{cmd.phrase} {cmd.description} {cmd.example}".lower() for t in tokens)
        )
        if matched:
            filtered.append(CommandCategory(title=cat.title, summary=cat.summary, commands=matched))
    return filtered


class CommandsHelpWidget(QWidget):
    """Searchable voice commands reference widget (embeddable or used inside CommandsHelpDialog)."""

    def __init__(self, parent: QWidget | None = None, *, scrollable: bool = True) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search voice commands (e.g. click, scroll, terminal, tab, ask)…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._rebuild_cards)
        search_row.addWidget(self.search_input, 1)
        root.addLayout(search_row)

        if scrollable:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            container = QWidget()
            self._cards_layout = QVBoxLayout(container)
            self._cards_layout.setContentsMargins(0, 0, 8, 8)
            self._cards_layout.setSpacing(12)
            scroll.setWidget(container)
            root.addWidget(scroll, 1)
        else:
            self._cards_layout = QVBoxLayout()
            self._cards_layout.setContentsMargins(0, 0, 0, 0)
            self._cards_layout.setSpacing(12)
            root.addLayout(self._cards_layout)

        self._rebuild_cards("")

    def _rebuild_cards(self, query: str) -> None:
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

        categories = filter_command_categories(query)
        if not categories:
            empty = Card(subtle=True)
            lbl = QLabel(f"No voice commands matching “{query.strip()}”.")
            lbl.setObjectName("Muted")
            empty.layout.addWidget(lbl)
            self._cards_layout.addWidget(empty)
            self._cards_layout.addStretch(1)
            return

        for cat in categories:
            card = Card()
            card.layout.addWidget(eyebrow(cat.title))
            summary = QLabel(cat.summary)
            summary.setObjectName("Muted")
            summary.setWordWrap(True)
            card.layout.addWidget(summary)

            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(8)
            grid.setColumnStretch(0, 2)
            grid.setColumnStretch(1, 3)

            for row_idx, cmd in enumerate(cat.commands):
                phrase_lbl = QLabel(f"“{cmd.phrase}”")
                phrase_lbl.setObjectName("Heading")
                phrase_lbl.setWordWrap(True)
                phrase_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

                desc_text = cmd.description
                if cmd.example:
                    desc_text = f"{desc_text}  ({cmd.example})"
                desc_lbl = QLabel(desc_text)
                desc_lbl.setObjectName("Muted")
                desc_lbl.setWordWrap(True)
                desc_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

                grid.addWidget(phrase_lbl, row_idx, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(desc_lbl, row_idx, 1, Qt.AlignmentFlag.AlignTop)

            card.layout.addLayout(grid)
            self._cards_layout.addWidget(card)

        self._cards_layout.addStretch(1)


class CommandsHelpDialog(QDialog):
    """Standalone searchable Voice Commands Help dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        load_bundled_fonts()
        self.setWindowTitle("Keyless Voice Commands")
        self.setWindowIcon(make_icon())
        self.resize(760, 620)
        self.setMinimumSize(580, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Voice commands")
        title.setObjectName("Title")
        subtitle = QLabel(
            "Look at a target and speak naturally. Chain trailing actions like “… and press enter” in one breath."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        layout.addLayout(header)

        self.help_widget = CommandsHelpWidget(self, scrollable=True)
        layout.addWidget(self.help_widget, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
