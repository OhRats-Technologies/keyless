"""Canonical input keys: the cross-platform contract between engine and OS backends.

Modeled on cua-driver (`press_key(pid, key: &str, modifiers: &[&str])`) and
SDL scancodes: engine code speaks names, each ``PointerBackend`` maps names to
native codes. No engine module may import Linux ``evdev`` (or any OS input
API) directly — that was the blocker that kept ``keyless doctor`` from even
importing on macOS.

Names are **positional** (like SDL scancodes / USB HID usages): ``"C"`` is the
physical key in the C position, not the C symbol. Both evdev codes and macOS
CG keycodes are positional, so the mapping preserves position on each OS.
Full layout-aware resolution (read the live layout via UCKeyTranslate, as
cua-driver does for AZERTY `cmd+a`) is a documented follow-up; until then a
static US table applies and shortcuts follow positions.

Buttons use the universal 1/2/3 = left/right/middle numbering; backends map
them (Linux: BTN_LEFT/RIGHT/MIDDLE, macOS: kCGMouseButtonLeft/...).
"""

from __future__ import annotations

BUTTON_LEFT = 1
BUTTON_RIGHT = 2
BUTTON_MIDDLE = 3

# Modifier aliases folded by canonical(): cua-driver accepts cmd/command/
# super/meta/win for Command; we fold to the LEFT* canonical form.
_MODIFIER_ALIASES = {
    "cmd": "LEFTMETA",
    "command": "LEFTMETA",
    "super": "LEFTMETA",
    "meta": "LEFTMETA",
    "win": "LEFTMETA",
    "ctrl": "LEFTCTRL",
    "control": "LEFTCTRL",
    "alt": "LEFTALT",
    "option": "LEFTALT",
    "shift": "LEFTSHIFT",
}


def canonical(name: str) -> str:
    """Fold one key spelling to its canonical name (case/space/dash-insensitive).

    Mirrors cua-driver's ``canonical_key_name``: "Page_Down", "page-down" and
    "PgDn" are one key; "ArrowLeft" is LEFT; single characters pass through
    uppercased; modifier aliases (cmd/super/win/ctrl/alt/...) fold to LEFT*.
    Raises ValueError for empty names.
    """
    raw = name.strip()
    if not raw:
        raise ValueError("Empty key name.")
    if len(raw) == 1:
        return raw.upper()
    compact = raw.lower().replace(" ", "").replace("_", "").replace("-", "")
    if compact in _MODIFIER_ALIASES:
        return _MODIFIER_ALIASES[compact]
    folded = {
        "pgdn": "PAGEDOWN",
        "pgdown": "PAGEDOWN",
        "pgup": "PAGEUP",
        "arrowleft": "LEFT",
        "leftarrow": "LEFT",
        "arrowright": "RIGHT",
        "rightarrow": "RIGHT",
        "arrowup": "UP",
        "uparrow": "UP",
        "arrowdown": "DOWN",
        "downarrow": "DOWN",
        "forwarddelete": "DELETE",
        "fwddelete": "DELETE",
        "deleteforward": "DELETE",
        "del": "DELETE",
        "bksp": "BACKSPACE",
        "escape": "ESC",
        "esc": "ESC",
        "return": "ENTER",
        "enter": "ENTER",
        "space": "SPACE",
        "backspace": "BACKSPACE",
        "delete": "DELETE",
        "tab": "TAB",
        "home": "HOME",
        "end": "END",
        "pageup": "PAGEUP",
        "pagedown": "PAGEDOWN",
        "left": "LEFT",
        "right": "RIGHT",
        "up": "UP",
        "down": "DOWN",
        "capslock": "CAPSLOCK",
    }.get(compact)
    if folded is not None:
        return folded
    upper = raw.upper()
    if upper.startswith("F") and upper[1:].isdigit() and 1 <= int(upper[1:]) <= 24:
        return upper
    if len(upper) == 1 or upper.isalnum():
        return upper
    raise ValueError(
        f"Unknown key name: {name!r}. Send one key plus modifiers "
        f"(e.g. key \"RIGHT\" with modifiers [\"SHIFT\"]), or use type_text "
        f"for printable symbols such as * or $."
    )


def linux_keycode(name: str) -> int:
    """Map a canonical key name to a Linux evdev code (Linux only)."""
    key = canonical(name)
    try:
        from evdev import ecodes
    except ImportError as exc:
        raise NotImplementedError("Linux evdev keycodes need the Linux-only evdev package.") from exc
    try:
        return int(getattr(ecodes, f"KEY_{key}"))
    except AttributeError as exc:
        raise ValueError(f"Unknown canonical key: {name!r}.") from exc


def linux_button(button: int) -> int:
    """Map a universal button number to a Linux evdev button code."""
    from evdev import ecodes

    return {BUTTON_LEFT: ecodes.BTN_LEFT, BUTTON_RIGHT: ecodes.BTN_RIGHT,
            BUTTON_MIDDLE: ecodes.BTN_MIDDLE}.get(button, button)


# macOS CG virtual keycodes (kVK_ANSI_* / kVK_*). No dependency required.
# Media keys (PLAYPAUSE/MUTE) have no CG keycode — macOS delivers them as NX
# system events, so the backend reports them honestly instead of faking a code.
_MAC_KEYCODES: dict[str, int] = {
    "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7,
    "C": 8, "V": 9, "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15,
    "Y": 16, "T": 17, "O": 31, "U": 32, "I": 34, "P": 35, "L": 37,
    "J": 38, "K": 40, "M": 46, "N": 45,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "0": 29,
    "MINUS": 27, "EQUAL": 24, "LEFTBRACE": 33, "RIGHTBRACE": 30,
    "BACKSLASH": 42, "SEMICOLON": 41, "APOSTROPHE": 39, "GRAVE": 50,
    "COMMA": 43, "DOT": 47, "SLASH": 44,
    "ENTER": 36, "TAB": 48, "SPACE": 49, "BACKSPACE": 51, "ESC": 53,
    "LEFT": 123, "RIGHT": 124, "DOWN": 125, "UP": 126,
    "HOME": 115, "END": 119, "PAGEUP": 116, "PAGEDOWN": 121,
    "DELETE": 117,
    "LEFTSHIFT": 56, "RIGHTSHIFT": 60,
    "LEFTCTRL": 59, "RIGHTCTRL": 62,
    "LEFTALT": 58, "RIGHTALT": 61,
    "LEFTMETA": 55, "RIGHTMETA": 54,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}

_MAC_UNMAPPED = frozenset({"PLAYPAUSE", "MUTE", "CAPSLOCK"})


def mac_keycode(name: str) -> int:
    """Map a canonical key name to a macOS CG virtual keycode."""
    key = canonical(name)
    if key in _MAC_UNMAPPED:
        raise NotImplementedError(
            f"{key} has no CG keycode on macOS (NX system event, not implemented)."
        )
    try:
        return _MAC_KEYCODES[key]
    except KeyError as exc:
        raise ValueError(f"Unknown canonical key: {name!r}.") from exc
