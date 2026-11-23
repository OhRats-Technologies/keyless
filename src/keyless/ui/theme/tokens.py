"""OhRats UI-kit tokens for the desktop app.

Warm neutrals, subtle hairline borders, consistent radii, Inter for UI and prose,
and Space Mono strictly for small secondary telemetry. No widget owns a raw color.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from string import Template

FONT_DIR = Path(__file__).with_name("fonts")
CHEVRON = Path(__file__).with_name("chevron-down.svg")
CHECK_LIGHT = Path(__file__).with_name("check-light.svg")
CHECK_DARK = Path(__file__).with_name("check-dark.svg")
TOKENS_PATH = Path(__file__).with_name("design-tokens.json")
_LOADED_FONTS = False

#: The parsed design-tokens document. ``design-tokens.json`` is the single
#: source of truth for colors, type, spacing, and radii; this module only maps
#: its flat records onto the ``ThemeTokens`` API the widgets already use.
DESIGN_TOKENS: dict[str, object] = json.loads(TOKENS_PATH.read_text())


class ThemeName(str, Enum):
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class ThemeTokens:
    name: ThemeName
    background: str
    surface: str
    surface_alt: str
    sidebar: str
    text: str
    muted: str
    accent: str
    accent_text: str
    border: str
    danger: str
    success: str
    warning: str
    selected: str
    spacing_xs: int = 4
    spacing_sm: int = 8
    spacing_md: int = 16
    spacing_lg: int = 24
    radius: int = 10
    radius_sm: int = 7
    font_family: str = 'Inter, "Noto Sans", "DejaVu Sans", sans-serif'
    mono_family: str = '"Space Mono", "MesloLGS Nerd Font Mono", monospace'
    font_size: int = 14
    title_size: int = 26
    heading_size: int = 16
    pill_height: int = 28

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _build_theme(name: ThemeName) -> ThemeTokens:
    themes = DESIGN_TOKENS["themes"]
    assert isinstance(themes, dict)
    palette = themes[name.value]
    assert isinstance(palette, dict)
    typography = DESIGN_TOKENS["typography"]
    assert isinstance(typography, dict)
    sizes = typography["sizes"]
    spacing = DESIGN_TOKENS["spacing"]
    radii = DESIGN_TOKENS["radii"]
    assert isinstance(sizes, dict) and isinstance(spacing, dict) and isinstance(radii, dict)
    return ThemeTokens(
        name=name,
        **palette,
        spacing_xs=int(spacing["xs"]),
        spacing_sm=int(spacing["sm"]),
        spacing_md=int(spacing["md"]),
        spacing_lg=int(spacing["lg"]),
        radius=int(radii["default"]),
        radius_sm=int(radii["small"]),
        font_family=str(typography["font_family"]),
        mono_family=str(typography["mono_family"]),
        font_size=int(sizes["body"]),
        title_size=int(sizes["title"]),
        heading_size=int(sizes["heading"]),
        pill_height=int(radii["pill"]) * 2,
    )


LIGHT = _build_theme(ThemeName.LIGHT)
DARK = _build_theme(ThemeName.DARK)


def tokens_for(name: ThemeName | str) -> ThemeTokens:
    resolved = ThemeName(name) if isinstance(name, str) else name
    return DARK if resolved is ThemeName.DARK else LIGHT


def load_bundled_fonts() -> bool:
    """Register Inter and Space Mono with Qt; safe to call repeatedly."""
    global _LOADED_FONTS
    if _LOADED_FONTS:
        return True
    try:
        from PySide6.QtGui import QFontDatabase

        loaded = False
        for path in FONT_DIR.glob("*.ttf"):
            if QFontDatabase.addApplicationFont(str(path)) >= 0:
                loaded = True
        _LOADED_FONTS = loaded
        return loaded
    except Exception:
        return False


def system_theme_name(app: object | None = None) -> ThemeName:
    """Follow the system preference when Qt exposes one; default to light."""
    try:
        from PySide6.QtCore import Qt

        hints = app.styleHints() if app is not None else None
        if hints is not None:
            scheme = hints.colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return ThemeName.DARK
    except Exception:
        pass
    return ThemeName.LIGHT


def stylesheet(tokens: ThemeTokens) -> str:
    values = {
        "background": tokens.background,
        "surface": tokens.surface,
        "surface_alt": tokens.surface_alt,
        "sidebar": tokens.sidebar,
        "text": tokens.text,
        "muted": tokens.muted,
        "accent": tokens.accent,
        "accent_text": tokens.accent_text,
        "border": tokens.border,
        "danger": tokens.danger,
        "success": tokens.success,
        "warning": tokens.warning,
        "selected": tokens.selected,
        "spacing_xs": f"{tokens.spacing_xs}px",
        "spacing_sm": f"{tokens.spacing_sm}px",
        "spacing_md": f"{tokens.spacing_md}px",
        "spacing_lg": f"{tokens.spacing_lg}px",
        "radius": f"{tokens.radius}px",
        "radius_sm": f"{tokens.radius_sm}px",
        "font_family": tokens.font_family,
        "mono_family": tokens.mono_family,
        "font_size": f"{tokens.font_size}px",
        "title_size": f"{tokens.title_size}px",
        "heading_size": f"{tokens.heading_size}px",
        "pill_height": f"{tokens.pill_height}px",
        "pill_radius": f"{tokens.pill_height // 2}px",
        "chevron": str(CHEVRON),
        "checkmark": str(CHECK_DARK if tokens.name is ThemeName.DARK else CHECK_LIGHT),
    }
    return Template(_QSS).substitute(values)


_QSS = """
    QWidget {
        background: $background;
        color: $text;
        font-family: $font_family;
        font-size: $font_size;
    }
    QMainWindow, QDialog {
        background: $background;
    }
    QLabel, QCheckBox {
        background: transparent;
    }
    QWidget#Content {
        background: $background;
    }
    QFrame#Sidebar {
        background: $sidebar;
        border-right: 1px solid $border;
    }
    QLabel#Wordmark {
        color: $text;
        font-family: $font_family;
        font-size: 14px;
        font-weight: 600;
    }
    QLabel#Title {
        color: $text;
        font-size: $title_size;
        font-weight: 600;
    }
    QLabel#Display {
        color: $text;
        font-size: 36px;
        font-weight: 600;
    }
    QLabel#Heading {
        color: $text;
        font-size: $heading_size;
        font-weight: 600;
    }
    QLabel#SectionTitle {
        color: $muted;
        font-family: $font_family;
        font-size: 12px;
        font-weight: 600;
        padding-top: 4px;
    }
    QLabel#Mono, QLabel#Eyebrow {
        color: $muted;
        font-family: $mono_family;
        font-size: 11px;
    }
    /* Telemetry numerals stay tabular. Qt QSS has no font-feature-settings
       property, so the mono family (Space Mono) is the guarantee: every digit
       advances by the same width in live counters and timings. */
    QLabel#Telemetry {
        color: $muted;
        font-family: $mono_family;
        font-size: 11px;
    }
    QLabel#Muted {
        color: $muted;
        font-size: 13px;
    }
    QLabel#Success {
        color: $success;
        font-weight: 500;
    }
    QLabel#Danger {
        color: $danger;
        font-weight: 500;
    }
    QLabel#Warning {
        color: $warning;
        font-weight: 500;
    }
    QFrame#Card {
        background: $surface;
        border: 1px solid $border;
        border-radius: $radius;
    }
    QFrame#SubtleCard {
        background: $surface_alt;
        border: 1px solid $border;
        border-radius: $radius;
    }
    QFrame#Divider {
        background: $border;
        max-height: 1px;
        min-height: 1px;
    }
    QLabel#StatusDot {
        border-radius: 5px;
        min-width: 10px;
        max-width: 10px;
        min-height: 10px;
        max-height: 10px;
    }
    QLabel#Pill {
        background: $surface_alt;
        border: 1px solid $border;
        border-radius: $pill_radius;
        color: $muted;
        font-family: $font_family;
        font-size: 12px;
        font-weight: 500;
        padding: 3px 10px;
    }
    QPushButton {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        border-radius: $radius_sm;
        padding: 7px 14px;
        min-height: 32px;
        font-family: $font_family;
        font-size: 13px;
        font-weight: 500;
    }
    QPushButton:hover {
        background: $surface_alt;
    }
    QPushButton:checked {
        background: $accent;
        color: $accent_text;
        border-color: $accent;
    }
    QPushButton:disabled {
        color: $muted;
        background: $surface_alt;
        border-color: $border;
    }
    QPushButton#Primary {
        background: $accent;
        color: $accent_text;
        border: 1px solid $accent;
        padding: 8px 18px;
        font-weight: 600;
    }
    QPushButton#Primary:hover {
        opacity: 0.92;
    }
    QPushButton#Primary:disabled {
        background: $surface_alt;
        color: $muted;
        border-color: $border;
    }
    QPushButton#Ghost {
        background: transparent;
        color: $muted;
        border: none;
        padding: 6px 10px;
        min-height: 28px;
        font-weight: 500;
    }
    QPushButton#Ghost:hover {
        color: $text;
        background: $surface_alt;
    }
    QPushButton#Link {
        background: transparent;
        color: $muted;
        border: none;
        padding: 2px 4px;
        min-height: 20px;
        font-size: 12px;
        font-weight: 500;
        text-align: left;
    }
    QPushButton#Link:hover {
        color: $text;
    }
    QPushButton#Destructive {
        color: $danger;
        border: 1px solid $border;
        background: transparent;
    }
    QPushButton#Destructive:hover {
        border-color: $danger;
        background: $surface_alt;
    }
    QPushButton#KeyCap {
        background: $surface_alt;
        color: $text;
        border: 1px solid $border;
        border-radius: $radius_sm;
        font-family: $mono_family;
        font-size: 12px;
        padding: 5px 12px;
        min-height: 28px;
    }
    QListWidget#Nav {
        background: $sidebar;
        border: none;
        outline: none;
        padding: 4px 0;
        font-family: $font_family;
        font-size: 13px;
        font-weight: 500;
    }
    QListWidget#Nav::item {
        padding: 9px 12px;
        border-radius: $radius_sm;
        margin: 2px 0;
        color: $muted;
    }
    QListWidget#Nav::item:hover {
        background: $surface_alt;
        color: $text;
    }
    QListWidget#Nav::item:selected {
        background: $selected;
        color: $text;
        font-weight: 600;
    }
    QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        border-radius: $radius_sm;
        padding: 6px 10px;
        min-height: 24px;
        selection-background-color: $accent;
        selection-color: $accent_text;
    }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {
        border-color: $text;
    }
    QComboBox::drop-down {
        border: none;
        width: 24px;
    }
    QComboBox::down-arrow {
        image: url("$chevron");
        width: 10px;
        height: 6px;
    }
    QComboBox QAbstractItemView {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        border-radius: $radius_sm;
        padding: 4px;
        selection-background-color: $selected;
        selection-color: $text;
    }
    QCheckBox {
        spacing: 10px;
        color: $text;
        font-size: 13px;
    }
    QCheckBox::indicator {
        width: 18px;
        height: 18px;
        border-radius: 5px;
        border: 1px solid $border;
        background: $surface;
    }
    QCheckBox::indicator:checked {
        background: $accent;
        border-color: $accent;
        image: url("$checkmark");
    }
    QProgressBar {
        background: $surface_alt;
        border: none;
        border-radius: 3px;
        color: $muted;
        font-size: 10px;
        text-align: center;
    }
    QProgressBar::chunk {
        background: $accent;
        border-radius: 3px;
    }
    QTableWidget {
        background: $surface;
        color: $text;
        gridline-color: transparent;
        border: 1px solid $border;
        border-radius: $radius;
        selection-background-color: $selected;
        selection-color: $text;
    }
    QTableWidget::item {
        padding: 8px 10px;
        border-bottom: 1px solid $border;
    }
    QHeaderView::section {
        background: $surface;
        color: $muted;
        border: none;
        border-bottom: 1px solid $border;
        padding: 8px 10px;
        font-family: $font_family;
        font-size: 12px;
        font-weight: 600;
    }
    QScrollArea {
        border: none;
        background: transparent;
    }
    QScrollArea > QWidget > QWidget {
        background: transparent;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 8px;
        margin: 2px 0;
    }
    QScrollBar::handle:vertical {
        background: $border;
        border-radius: 4px;
        min-height: 24px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
    }
    QMenu {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        border-radius: $radius_sm;
        padding: 4px;
    }
    QMenu::item {
        padding: 6px 14px;
        border-radius: 4px;
    }
    QMenu::item:selected {
        background: $selected;
        color: $text;
    }
    QToolTip {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        border-radius: 6px;
        padding: 4px 8px;
    }
"""
