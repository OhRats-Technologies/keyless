"""Pointer sink contract and the dry-run backend used by tests and `--dry-run`."""

from __future__ import annotations

import logging

from keyless.input.keys import BUTTON_LEFT
from keyless.platform.base import PointerBackend

logger = logging.getLogger(__name__)

__all__ = ["PointerBackend", "DryRunPointerBackend"]


class DryRunPointerBackend:
    """No-op pointer backend when running diagnostics without /dev/uinput."""

    def __init__(self, screen_width: int, screen_height: int) -> None:
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.last_abs: tuple[float, float] | None = None
        self.rel_accum: tuple[float, float] = (0.0, 0.0)
        self.clicks: list[int] = []
        self.typed_texts: list[str] = []
        self.combos: list[tuple[str, ...]] = []
        self.hold_taps: list[tuple[str, str, int]] = []
        self.scrolls: list[tuple[int, int]] = []

    def move_absolute(self, x: float, y: float) -> None:
        self.last_abs = (
            max(0.0, min(float(self.screen_width - 1), x)),
            max(0.0, min(float(self.screen_height - 1), y)),
        )

    def move_relative(self, dx: float, dy: float) -> None:
        self.rel_accum = (self.rel_accum[0] + dx, self.rel_accum[1] + dy)

    def click(self, button: int = BUTTON_LEFT) -> None:
        self.clicks.append(button)
        logger.debug("Dry-run click: %s", button)

    def double_click(self, button: int = BUTTON_LEFT) -> None:
        self.click(button)
        self.click(button)

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        self.scrolls.append((dx, dy))
        logger.debug("Dry-run scroll: dx=%s dy=%s", dx, dy)

    def key_combo(self, *keys: str) -> None:
        self.combos.append(keys)
        logger.debug("Dry-run key_combo: %s", keys)

    def hold_and_tap(
        self,
        hold_key: str,
        tap_key: str,
        count: int = 1,
        tap_delay: float = 0.035,
    ) -> None:
        self.hold_taps.append((hold_key, tap_key, count))
        self.combos.append((hold_key, *(tap_key,) * max(1, count)))
        logger.debug("Dry-run hold_and_tap: hold=%s tap=%s count=%s", hold_key, tap_key, count)

    def type_text(self, text: str) -> None:
        self.typed_texts.append(text)
        logger.debug("Dry-run type_text: %r", text)

    def close(self) -> None:
        pass
