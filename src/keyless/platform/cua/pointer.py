"""Hybrid pointer: native moves for callers without the eye overlay.

Discrete actions route through cua-driver. The macOS eye cursor uses
``keyless.platform.cua.agent_cursor`` instead: its independent overlay owns
motion, so the system pointer is never warped. This class keeps native motion
for other callers and for hosts where the overlay is unavailable.

Rationale (measured by cua-driver): a background AX click costs 250–750 ms,
batched typing ~185 chars in ~1 s. That is fine for voice-triggered clicks
but fatal for the 60 Hz gaze cursor loop — so ``move_absolute`` /
``move_relative`` always use the inner native backend (uinput/CGEvent) while
click/scroll/key/type route through the embedded driver when it is up.

Any cua failure falls back to the native backend for that call and (on
transport failure) disables the cua path for the session. Nothing here can
take down the voice loop.
"""

from __future__ import annotations

import logging
import threading

from keyless.input.keys import BUTTON_LEFT, BUTTON_MIDDLE, BUTTON_RIGHT, canonical
from keyless.input.synthetic_clicks import mark_synthetic_click
from keyless.platform.cua.driver import CuaDriverError, CuaDriverManager

logger = logging.getLogger(__name__)

_BUTTONS = {BUTTON_LEFT: "left", BUTTON_RIGHT: "right", BUTTON_MIDDLE: "middle"}

_MODIFIERS = {
    "LEFTCTRL": "ctrl", "RIGHTCTRL": "ctrl",
    "LEFTSHIFT": "shift", "RIGHTSHIFT": "shift",
    "LEFTALT": "alt", "RIGHTALT": "alt",
    "LEFTMETA": "cmd", "RIGHTMETA": "cmd",
    "FN": "fn",
}

_NO_CUA_KEY = frozenset({"PLAYPAUSE", "MUTE", "CAPSLOCK"})


def _cua_key(name: str) -> str:
    """Canonical key name -> cua-driver key string (lowercase names)."""
    key = canonical(name)
    if key in _NO_CUA_KEY:
        raise NotImplementedError(f"{key} has no cua-driver key mapping.")
    if key in _MODIFIERS:
        return _MODIFIERS[key]
    mapping = {
        "ENTER": "enter", "TAB": "tab", "SPACE": "space",
        "BACKSPACE": "backspace", "ESC": "escape", "DELETE": "delete",
        "LEFT": "left", "RIGHT": "right", "UP": "up", "DOWN": "down",
        "HOME": "home", "END": "end", "PAGEUP": "pageup", "PAGEDOWN": "pagedown",
        "GRAVE": "`", "MINUS": "-", "EQUAL": "=",
        "LEFTBRACE": "[", "RIGHTBRACE": "]", "BACKSLASH": "\\",
        "SEMICOLON": ";", "APOSTROPHE": "'", "COMMA": ",",
        "DOT": ".", "SLASH": "/",
    }
    if key in mapping:
        return mapping[key]
    if len(key) == 1 or (key.startswith("F") and key[1:].isdigit()):
        return key.lower()
    raise NotImplementedError(f"{key} has no cua-driver key mapping.")


def _desktop_target() -> dict:
    return {"kind": "desktop", "display_id": "primary"}


class CuaDriverPointerBackend:
    """PointerBackend with native cursor motion and cua-driver discrete actions."""

    def __init__(self, native_pointer, driver: CuaDriverManager | None = None) -> None:  # type: ignore[no-untyped-def]
        self._native = native_pointer
        self._driver = driver
        self._pos: tuple[float, float] | None = None
        self._start_lock = threading.Lock()
        self._cua_disabled = False
        self._scale: float | None = None

    # -- driver handling -------------------------------------------------

    def _ensure_driver(self):  # type: ignore[no-untyped-def]
        if self._cua_disabled or self._driver is None:
            return None
        if self._driver.running:
            return self._driver
        with self._start_lock:
            if self._driver.running:
                return self._driver
            try:
                self._driver.start()
                return self._driver
            except CuaDriverError as exc:
                logger.warning("cua-driver unavailable, staying native: %s", exc)
                self._cua_disabled = True
                return None

    def _call(self, tool: str, args: dict):  # type: ignore[no-untyped-def]
        driver = self._ensure_driver()
        if driver is None:
            raise CuaDriverError("cua-driver unavailable.")
        try:
            result = driver.call(tool, args, timeout_s=30.0)
        except CuaDriverError:
            self._cua_disabled = True
            raise
        if result.get("isError"):
            raise CuaDriverError(f"cua-driver {tool} refused: {result.get('content')}")
        return result

    def _screen_px(self) -> tuple[float, float]:
        """Gaze coordinates are display points; cua desktop targets take pixels."""
        driver = self._ensure_driver()
        if driver is None:
            raise CuaDriverError("cua-driver unavailable.")
        if self._scale is None:
            info = driver.call("get_screen_size", {}, timeout_s=10.0).get("structured", {})
            self._scale = float(info.get("scale_factor") or 1.0)
        assert self._pos is not None
        return self._pos[0] * self._scale, self._pos[1] * self._scale

    # -- PointerBackend ---------------------------------------------------

    def move_absolute(self, x: float, y: float) -> None:
        self._pos = (float(x), float(y))
        self._native.move_absolute(x, y)

    def note_gaze_position(self, x: float, y: float) -> None:
        """Track where the eye cursor is without moving the OS pointer.

        The cua-driver agent overlay owns motion; a later click still needs the
        latest gaze target, so the hybrid backend keeps its own position copy.
        """
        self._pos = (float(x), float(y))

    def move_relative(self, dx: float, dy: float) -> None:
        if self._pos is not None:
            self._pos = (self._pos[0] + float(dx), self._pos[1] + float(dy))
        self._native.move_relative(dx, dy)

    def click(self, button: int = BUTTON_LEFT) -> None:
        mark_synthetic_click()
        if self._pos is not None:
            try:
                x, y = self._screen_px()
                self._call("click", {
                    "target": _desktop_target(),
                    "x": x, "y": y,
                    "button": _BUTTONS.get(button, "left"),
                    "post_action_wait": "none",
                })
                return
            except (CuaDriverError, NotImplementedError) as exc:
                logger.debug("cua click failed, native fallback: %s", exc)
        self._native.click(button)

    def double_click(self, button: int = BUTTON_LEFT) -> None:
        if self._pos is not None:
            try:
                x, y = self._screen_px()
                self._call("click", {
                    "target": _desktop_target(),
                    "x": x, "y": y,
                    "button": _BUTTONS.get(button, "left"),
                    "count": 2,
                    "post_action_wait": "none",
                })
                return
            except (CuaDriverError, NotImplementedError) as exc:
                logger.debug("cua double-click failed, native fallback: %s", exc)
        self._native.double_click(button)

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        if self._pos is not None and (dy or dx):
            try:
                x, y = self._screen_px()
                if dy:
                    self._call("scroll", {
                        "x": x, "y": y,
                        "target": _desktop_target(),
                        "direction": "down" if dy < 0 else "up",
                        "amount": abs(int(dy)),
                    })
                if dx:
                    self._call("scroll", {
                        "x": x, "y": y,
                        "target": _desktop_target(),
                        "direction": "right" if dx > 0 else "left",
                        "amount": abs(int(dx)),
                    })
                return
            except (CuaDriverError, NotImplementedError) as exc:
                logger.debug("cua scroll failed, native fallback: %s", exc)
        self._native.scroll(dy=dy, dx=dx)

    def key_combo(self, *keys: str) -> None:
        if keys:
            try:
                if len(keys) == 1:
                    self._call("press_key", {
                        "key": _cua_key(keys[0]),
                        "target": _desktop_target(),
                    })
                else:
                    self._call("hotkey", {"keys": [_cua_key(k) for k in keys]})
                return
            except (CuaDriverError, NotImplementedError) as exc:
                logger.debug("cua key_combo failed, native fallback: %s", exc)
        self._native.key_combo(*keys)

    def type_text(self, text: str) -> None:
        if text:
            try:
                self._call("type_text", {"text": text})
                return
            except (CuaDriverError, NotImplementedError) as exc:
                logger.debug("cua type_text failed, native fallback: %s", exc)
        self._native.type_text(text)

    def close(self) -> None:
        try:
            if self._driver is not None:
                self._driver.stop()
        finally:
            self._native.close()
