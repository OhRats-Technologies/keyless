"""Linux pointer backend built on /dev/uinput virtual devices."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time

from evdev import AbsInfo, UInput, ecodes

logger = logging.getLogger(__name__)

# Full US-QWERTY ASCII map -> (keycode, needs_shift)
_CHAR_KEYMAP: dict[str, tuple[int, bool]] = {
    "a": (ecodes.KEY_A, False),
    "b": (ecodes.KEY_B, False),
    "c": (ecodes.KEY_C, False),
    "d": (ecodes.KEY_D, False),
    "e": (ecodes.KEY_E, False),
    "f": (ecodes.KEY_F, False),
    "g": (ecodes.KEY_G, False),
    "h": (ecodes.KEY_H, False),
    "i": (ecodes.KEY_I, False),
    "j": (ecodes.KEY_J, False),
    "k": (ecodes.KEY_K, False),
    "l": (ecodes.KEY_L, False),
    "m": (ecodes.KEY_M, False),
    "n": (ecodes.KEY_N, False),
    "o": (ecodes.KEY_O, False),
    "p": (ecodes.KEY_P, False),
    "q": (ecodes.KEY_Q, False),
    "r": (ecodes.KEY_R, False),
    "s": (ecodes.KEY_S, False),
    "t": (ecodes.KEY_T, False),
    "u": (ecodes.KEY_U, False),
    "v": (ecodes.KEY_V, False),
    "w": (ecodes.KEY_W, False),
    "x": (ecodes.KEY_X, False),
    "y": (ecodes.KEY_Y, False),
    "z": (ecodes.KEY_Z, False),
    "0": (ecodes.KEY_0, False),
    "1": (ecodes.KEY_1, False),
    "2": (ecodes.KEY_2, False),
    "3": (ecodes.KEY_3, False),
    "4": (ecodes.KEY_4, False),
    "5": (ecodes.KEY_5, False),
    "6": (ecodes.KEY_6, False),
    "7": (ecodes.KEY_7, False),
    "8": (ecodes.KEY_8, False),
    "9": (ecodes.KEY_9, False),
    " ": (ecodes.KEY_SPACE, False),
    "\n": (ecodes.KEY_ENTER, False),
    "\t": (ecodes.KEY_TAB, False),
    "-": (ecodes.KEY_MINUS, False),
    "=": (ecodes.KEY_EQUAL, False),
    "[": (ecodes.KEY_LEFTBRACE, False),
    "]": (ecodes.KEY_RIGHTBRACE, False),
    "\\": (ecodes.KEY_BACKSLASH, False),
    ";": (ecodes.KEY_SEMICOLON, False),
    "'": (ecodes.KEY_APOSTROPHE, False),
    "`": (ecodes.KEY_GRAVE, False),
    ",": (ecodes.KEY_COMMA, False),
    ".": (ecodes.KEY_DOT, False),
    "/": (ecodes.KEY_SLASH, False),
    "!": (ecodes.KEY_1, True),
    "@": (ecodes.KEY_2, True),
    "#": (ecodes.KEY_3, True),
    "$": (ecodes.KEY_4, True),
    "%": (ecodes.KEY_5, True),
    "^": (ecodes.KEY_6, True),
    "&": (ecodes.KEY_7, True),
    "*": (ecodes.KEY_8, True),
    "(": (ecodes.KEY_9, True),
    ")": (ecodes.KEY_0, True),
    "_": (ecodes.KEY_MINUS, True),
    "+": (ecodes.KEY_EQUAL, True),
    "{": (ecodes.KEY_LEFTBRACE, True),
    "}": (ecodes.KEY_RIGHTBRACE, True),
    "|": (ecodes.KEY_BACKSLASH, True),
    ":": (ecodes.KEY_SEMICOLON, True),
    '"': (ecodes.KEY_APOSTROPHE, True),
    "~": (ecodes.KEY_GRAVE, True),
    "<": (ecodes.KEY_COMMA, True),
    ">": (ecodes.KEY_DOT, True),
    "?": (ecodes.KEY_SLASH, True),
}

for _ch in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_KEYMAP[_ch.upper()] = (_CHAR_KEYMAP[_ch][0], True)

_COMMON_KEYS: list[int] = sorted(
    {
        ecodes.BTN_LEFT,
        ecodes.BTN_RIGHT,
        ecodes.BTN_MIDDLE,
        ecodes.KEY_ESC,
        ecodes.KEY_ENTER,
        ecodes.KEY_SPACE,
        ecodes.KEY_TAB,
        ecodes.KEY_BACKSPACE,
        ecodes.KEY_DELETE,
        ecodes.KEY_UP,
        ecodes.KEY_DOWN,
        ecodes.KEY_LEFT,
        ecodes.KEY_RIGHT,
        ecodes.KEY_HOME,
        ecodes.KEY_END,
        ecodes.KEY_PAGEUP,
        ecodes.KEY_PAGEDOWN,
        ecodes.KEY_LEFTCTRL,
        ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTSHIFT,
        ecodes.KEY_RIGHTSHIFT,
        ecodes.KEY_LEFTALT,
        ecodes.KEY_LEFTMETA,
        ecodes.KEY_F4,
        ecodes.KEY_PLAYPAUSE,
        ecodes.KEY_MUTE,
        *(code for code, _ in _CHAR_KEYMAP.values()),
    }
)


class UInputManager:
    """Manages two separate virtual devices so libinput handles ABS and REL cleanly.

    1. `keyless-abs`: EV_ABS (ABS_X, ABS_Y) + INPUT_PROP_DIRECT for saccade warps.
    2. `keyless-rel-kbd`: EV_REL + EV_KEY for head-gimbal nudges, clicks, and typing.
    """

    def __init__(
        self,
        screen_width: int,
        screen_height: int,
        abs_input_prop: int = ecodes.INPUT_PROP_DIRECT,
    ) -> None:
        if screen_width <= 1 or screen_height <= 1:
            raise ValueError(f"Invalid screen dimensions: {screen_width}x{screen_height}")
        self.screen_width = int(screen_width)
        self.screen_height = int(screen_height)
        self._rem_x = 0.0
        self._rem_y = 0.0
        self._io_lock = threading.RLock()

        if not os.access("/dev/uinput", os.W_OK):
            raise PermissionError(
                "Cannot write to /dev/uinput. Run `sudo ./scripts/setup_uinput_udev.sh` "
                "to grant user access."
            )

        abs_events = {
            ecodes.EV_ABS: [
                (
                    ecodes.ABS_X,
                    AbsInfo(
                        value=0,
                        min=0,
                        max=self.screen_width - 1,
                        fuzz=0,
                        flat=0,
                        resolution=1,
                    ),
                ),
                (
                    ecodes.ABS_Y,
                    AbsInfo(
                        value=0,
                        min=0,
                        max=self.screen_height - 1,
                        fuzz=0,
                        flat=0,
                        resolution=1,
                    ),
                ),
            ],
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
        }
        self._abs_dev = UInput(
            events=abs_events,
            name="keyless-abs",
            vendor=0x1209,
            product=0x4B4B,
            phys="keyless/abs",
            input_props=[abs_input_prop],
        )

        rel_events = {
            ecodes.EV_REL: [
                ecodes.REL_X,
                ecodes.REL_Y,
                ecodes.REL_WHEEL,
                ecodes.REL_HWHEEL,
            ],
            ecodes.EV_KEY: _COMMON_KEYS,
        }
        self._rel_dev = UInput(
            events=rel_events,
            name="keyless-rel-kbd",
            vendor=0x1209,
            product=0x4B4C,
            phys="keyless/rel-kbd",
        )
        time.sleep(0.15)

    def move_absolute(self, x: float, y: float) -> None:
        """Warp pointer instantaneously to global desktop logical coordinates `(x, y)`."""
        ix = int(round(max(0.0, min(float(self.screen_width - 1), x))))
        iy = int(round(max(0.0, min(float(self.screen_height - 1), y))))
        with self._io_lock:
            self._rem_x = 0.0
            self._rem_y = 0.0
            self._abs_dev.write(ecodes.EV_ABS, ecodes.ABS_X, ix)
            self._abs_dev.write(ecodes.EV_ABS, ecodes.ABS_Y, iy)
            self._abs_dev.syn()

    def move_relative(self, dx: float, dy: float) -> None:
        """Apply smooth sub-pixel relative motion using fractional residual accumulation."""
        with self._io_lock:
            self._rem_x += float(dx)
            self._rem_y += float(dy)
            ix = int(round(self._rem_x))
            iy = int(round(self._rem_y))
            if ix == 0 and iy == 0:
                return
            self._rem_x -= ix
            self._rem_y -= iy
            if ix != 0:
                self._rel_dev.write(ecodes.EV_REL, ecodes.REL_X, ix)
            if iy != 0:
                self._rel_dev.write(ecodes.EV_REL, ecodes.REL_Y, iy)
            self._rel_dev.syn()

    def click(self, button: int = 1) -> None:
        """Press and release a mouse button via the relative/button device."""
        from keyless.input.keys import linux_button

        code = linux_button(button)
        with self._io_lock:
            self._rel_dev.write(ecodes.EV_KEY, code, 1)
            self._rel_dev.syn()
            time.sleep(0.01)
            self._rel_dev.write(ecodes.EV_KEY, code, 0)
            self._rel_dev.syn()

    def double_click(self, button: int = 1) -> None:
        with self._io_lock:
            self.click(button)
            time.sleep(0.04)
            self.click(button)

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        """Emit vertical/horizontal wheel ticks."""
        with self._io_lock:
            if dy != 0:
                self._rel_dev.write(ecodes.EV_REL, ecodes.REL_WHEEL, int(dy))
            if dx != 0:
                self._rel_dev.write(ecodes.EV_REL, ecodes.REL_HWHEEL, int(dx))
            if dy != 0 or dx != 0:
                self._rel_dev.syn()

    def key_combo(self, *keys: str) -> None:
        """Press modifier keys first, tap the final key, then release in reverse order."""
        from keyless.input.keys import linux_keycode

        codes = [linux_keycode(k) for k in keys]
        if not codes:
            return
        with self._io_lock:
            if len(codes) > 1:
                for code in codes[:-1]:
                    self._rel_dev.write(ecodes.EV_KEY, code, 1)
                self._rel_dev.syn()
                time.sleep(0.015)
            self._rel_dev.write(ecodes.EV_KEY, codes[-1], 1)
            self._rel_dev.syn()
            time.sleep(0.02)
            self._rel_dev.write(ecodes.EV_KEY, codes[-1], 0)
            self._rel_dev.syn()
            if len(codes) > 1:
                time.sleep(0.01)
                for code in reversed(codes[:-1]):
                    self._rel_dev.write(ecodes.EV_KEY, code, 0)
                self._rel_dev.syn()

    def hold_and_tap(
        self,
        hold_key: str,
        tap_key: str,
        count: int = 1,
        tap_delay: float = 0.035,
    ) -> None:
        """Hold `hold_key` while tapping `tap_key` `count` times (for GNOME Alt+Tab MRU cycling)."""
        from keyless.input.keys import linux_keycode

        hold, tap = linux_keycode(hold_key), linux_keycode(tap_key)
        taps = max(1, int(count))
        with self._io_lock:
            self._rel_dev.write(ecodes.EV_KEY, hold, 1)
            self._rel_dev.syn()
            time.sleep(tap_delay)
            try:
                for _ in range(taps):
                    self._rel_dev.write(ecodes.EV_KEY, tap, 1)
                    self._rel_dev.syn()
                    time.sleep(0.02)
                    self._rel_dev.write(ecodes.EV_KEY, tap, 0)
                    self._rel_dev.syn()
                    time.sleep(tap_delay)
            finally:
                self._rel_dev.write(ecodes.EV_KEY, hold, 0)
                self._rel_dev.syn()

    def type_text(self, text: str, inter_key_delay: float = 0.003) -> None:
        """Type text directly via `/dev/uinput` (or `wl-copy` + Ctrl+V for non-ASCII if available)."""
        if not text:
            return
        if not text.isascii() and shutil.which("wl-copy"):
            subprocess.run(["wl-copy", "--", text], check=True)
            time.sleep(0.02)
            self.key_combo("LEFTCTRL", "V")
            return

        with self._io_lock:
            for ch in text:
                spec = _CHAR_KEYMAP.get(ch)
                if spec is None:
                    continue
                keycode, needs_shift = spec
                if needs_shift:
                    self._rel_dev.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                    self._rel_dev.syn()
                self._rel_dev.write(ecodes.EV_KEY, keycode, 1)
                self._rel_dev.syn()
                self._rel_dev.write(ecodes.EV_KEY, keycode, 0)
                self._rel_dev.syn()
                if needs_shift:
                    self._rel_dev.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                    self._rel_dev.syn()
                if inter_key_delay > 0:
                    time.sleep(inter_key_delay)

    def close(self) -> None:
        with self._io_lock:
            self._abs_dev.close()
            self._rel_dev.close()
