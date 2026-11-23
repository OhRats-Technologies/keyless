"""macOS pointer backend via CoreGraphics CGEvent (ctypes, no pyobjc required).

Requires Accessibility permission; without it CGEventPost silently drops
synthesized input and the capability probe reports NEEDS_PERMISSION.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading

from keyless.platform.base import NotSupportedError
from keyless.input.synthetic_clicks import mark_synthetic_click

_coregraphics = None
_funcs: dict[str, object] = {}


def _load() -> bool:
    global _coregraphics
    if _coregraphics is not None:
        return True
    path = ctypes.util.find_library("CoreGraphics")
    candidates = [path, "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"] if path else [
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"]
    for cand in candidates:
        if not cand:
            continue
        try:
            _coregraphics = ctypes.CDLL(cand, use_errno=True)
            return True
        except OSError:
            continue
    return False


def _fn(name: str, restype=None, argtypes=None):  # type: ignore[no-untyped-def]
    if name in _funcs:
        return _funcs[name]
    assert _coregraphics is not None
    fn = getattr(_coregraphics, name)
    if restype is not None:
        fn.restype = restype
    if argtypes is not None:
        fn.argtypes = argtypes
    _funcs[name] = fn
    return fn


# CGEvent constants (CGEventTypes.h / CGEventSource.h)
_kCGEventMouseMoved = 5
_kCGEventLeftMouseDown = 1
_kCGEventLeftMouseUp = 2
_kCGEventRightMouseDown = 3
_kCGEventRightMouseUp = 4
_kCGEventScrollWheel = 22
_kCGMouseButtonLeft = 0
_kCGMouseButtonRight = 1
_kCGMouseButtonCenter = 2
_kCGHIDEventTap = 0


# CGPoint must be a real struct: it is passed and returned by value as a
# two-double aggregate. Declaring it as two scalars (or an array return) reads
# uninitialized registers instead of the coordinates.
class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class macOSPointerBackend:
    """CGEvent pointer: absolute warps, relative nudges, clicks, scroll, typing."""

    def __init__(self) -> None:
        if not _load():
            raise NotSupportedError("CoreGraphics unavailable; pointer input unsupported.")
        self._lock = threading.RLock()
        self._rem_x = 0.0
        self._rem_y = 0.0
        # Cache current position for relative moves.
        self._pos_x = 0.0
        self._pos_y = 0.0

    def _post(self, event) -> None:  # type: ignore[no-untyped-def]
        _fn("CGEventPost", None, [ctypes.c_int, ctypes.c_void_p])(_kCGHIDEventTap, event)
        _fn("CFRelease", None, [ctypes.c_void_p])(event)

    def _current_position(self) -> tuple[float, float]:
        create = _fn("CGEventCreate", ctypes.c_void_p, [ctypes.c_void_p])
        get_loc = _fn("CGEventGetLocation", _CGPoint, [ctypes.c_void_p])
        event = create(None)
        if not event:
            return self._pos_x, self._pos_y
        try:
            pt = get_loc(event)
            return float(pt.x), float(pt.y)
        finally:
            _fn("CFRelease", None, [ctypes.c_void_p])(event)

    def _mouse_event(self, kind: int, x: float, y: float, button: int) -> None:
        create = _fn("CGEventCreateMouseEvent", ctypes.c_void_p,
                     [ctypes.c_void_p, ctypes.c_int, _CGPoint, ctypes.c_int])
        # kCGEventSourceStateHIDSystemState = 1
        src = _fn("CGEventSourceCreate", ctypes.c_void_p, [ctypes.c_int])(1)
        try:
            ev = create(src, kind, _CGPoint(float(x), float(y)), button)
            if not ev:
                raise NotSupportedError("CGEvent creation failed (Accessibility?).")
            self._post(ev)
        finally:
            if src:
                _fn("CFRelease", None, [ctypes.c_void_p])(src)

    def move_absolute(self, x: float, y: float) -> None:
        if not _load():
            raise NotSupportedError("CoreGraphics unavailable.")
        with self._lock:
            self._mouse_event(_kCGEventMouseMoved, x, y, _kCGMouseButtonLeft)
            self._pos_x, self._pos_y = float(x), float(y)

    def move_relative(self, dx: float, dy: float) -> None:
        with self._lock:
            self._rem_x += float(dx)
            self._rem_y += float(dy)
            ix = int(round(self._rem_x))
            iy = int(round(self._rem_y))
            if ix == 0 and iy == 0:
                return
            self._rem_x -= ix
            self._rem_y -= iy
            cx, cy = self._current_position()
            # If the OS position query failed, fall back to cached warp target.
            if cx == self._pos_x and cy == self._pos_y and (self._pos_x, self._pos_y) == (0.0, 0.0):
                pass
            self.move_absolute(cx + ix, cy + iy)

    def click(self, button: int = 1) -> None:
        mark_synthetic_click()
        btn = _kCGMouseButtonLeft if button == 1 else (_kCGMouseButtonRight if button == 2 else _kCGMouseButtonCenter)
        down = _kCGEventLeftMouseDown if btn == _kCGMouseButtonLeft else _kCGEventRightMouseDown
        up = _kCGEventLeftMouseUp if btn == _kCGMouseButtonLeft else _kCGEventRightMouseUp
        if btn == _kCGMouseButtonCenter:
            down, up = _kCGEventRightMouseDown, _kCGEventRightMouseUp
        with self._lock:
            x, y = self._current_position()
            self._mouse_event(down, x, y, btn)
            self._mouse_event(up, x, y, btn)

    def double_click(self, button: int = 1) -> None:
        import time

        self.click(button)
        time.sleep(0.04)
        self.click(button)

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        if dy == 0 and dx == 0:
            return
        with self._lock:
            create = _fn("CGEventCreateScrollWheelEvent", ctypes.c_void_p,
                         [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int32, ctypes.c_int32])
            src = _fn("CGEventSourceCreate", ctypes.c_void_p, [ctypes.c_int])(1)
            # CGScrollEventUnitLine = 1? use 1 unit, 2 wheels (y then x)
            try:
                ev = create(src, 1, 2, int(dy), int(dx))
                if ev:
                    self._post(ev)
            finally:
                if src:
                    _fn("CFRelease", None, [ctypes.c_void_p])(src)

    def key_combo(self, *keys: str) -> None:
        """Press a physical chord: modifiers down in order, base down/up, modifiers up reversed.

        Same transition model as cua-driver / Windows SendInput / Linux XTest.
        Media keys (PLAYPAUSE/MUTE) have no CG keycode and raise honestly.
        """
        import time

        from keyless.input.keys import mac_keycode

        codes = [mac_keycode(k) for k in keys]
        if not codes:
            return
        with self._lock:
            create = _fn("CGEventCreateKeyboardEvent", ctypes.c_void_p,
                         [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool])
            src = _fn("CGEventSourceCreate", ctypes.c_void_p, [ctypes.c_int])(1)
            try:
                for code in codes[:-1]:
                    ev = create(src, code, True)
                    if not ev:
                        raise NotSupportedError("CGEvent chord failed (Accessibility?).")
                    self._post(ev)
                if len(codes) > 1:
                    time.sleep(0.015)
                down = create(src, codes[-1], True)
                if not down:
                    raise NotSupportedError("CGEvent chord failed (Accessibility?).")
                self._post(down)
                time.sleep(0.02)
                up = create(src, codes[-1], False)
                if up:
                    self._post(up)
                if len(codes) > 1:
                    time.sleep(0.01)
                    for code in reversed(codes[:-1]):
                        release = create(src, code, False)
                        if release:
                            self._post(release)
            finally:
                if src:
                    _fn("CFRelease", None, [ctypes.c_void_p])(src)

    def type_text(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            create_unicode = _fn("CGEventCreateKeyboardEvent", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool])
            set_unicode = _fn("CGEventKeyboardSetUnicodeString", None, [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p])
            src = _fn("CGEventSourceCreate", ctypes.c_void_p, [ctypes.c_int])(1)
            try:
                for ch in text:
                    down = create_unicode(src, 0, True)
                    if not down:
                        raise NotSupportedError("CGEvent typing failed (Accessibility?).")
                    set_unicode(down, 1, ch)
                    self._post(down)
                    up = create_unicode(src, 0, False)
                    if up:
                        set_unicode(up, 1, ch)
                        self._post(up)
            finally:
                if src:
                    _fn("CFRelease", None, [ctypes.c_void_p])(src)

    def close(self) -> None:
        return None
