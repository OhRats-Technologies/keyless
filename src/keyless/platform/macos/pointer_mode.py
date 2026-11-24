"""Keep exactly one pointer visible: the real one while the mouse is in use,
the eye cursor while the mouse rests.

This module is deliberately independent of any gaze model. It only coordinates
two already-rendered cursors:

* the hardware pointer, hidden with ``CGDisplayHideCursor`` while the eye
  cursor owns the screen;
* the cua-driver agent cursor, shown through its session API.

Any physical movement or button press brings the hardware cursor back and
hides the eye cursor. The handoff is synchronous, so a click never shows two
arrows.
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
import time

logger = logging.getLogger(__name__)


class MacOSPointerVisibility:
    """Arbitrate between the hardware pointer and the cua eye cursor."""

    def __init__(
        self,
        eye_cursor: object,
        *,
        idle_switch_sec: float = 1.2,
        move_threshold_px: float = 2.0,
        poll_hz: float = 120.0,
        read_button=None,  # type: ignore[no-untyped-def]
        read_position=None,  # type: ignore[no-untyped-def]
        hide_cursor=None,  # type: ignore[no-untyped-def]
        show_cursor=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._eye = eye_cursor
        self._idle_switch = float(idle_switch_sec)
        self._threshold = float(move_threshold_px)
        self._period = 1.0 / max(20.0, float(poll_hz))
        self._read_button = read_button or _left_button_down
        self._read_position = read_position or _cursor_point
        self._hide_cursor_fn = hide_cursor or _hide_cursor
        self._show_cursor_fn = show_cursor or _show_cursor
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_activity = time.monotonic()
        self._eye_visible = False
        self._system_visible = True
        self._eye_allowed = True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self._eye_visible = False
            self._system_visible = True
            # Treat startup as already idle: the eye cursor takes over as soon
            # as the overlay reports ready, without waiting out a fresh timer.
            self._last_activity = time.monotonic() - self._idle_switch
            try:
                self._last_x, self._last_y = self._read_position()
            except Exception as exc:  # pragma: no cover - OS read guard
                logger.debug("Pointer position unavailable at start: %s", exc)
                self._last_x = self._last_y = None
            if self._eye_ready():
                self._set_eye(True)
        self._thread = threading.Thread(
            target=self._loop, name="macos-pointer-mode", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        with self._lock:
            self._set_eye(False)

    def set_eye_allowed(self, allowed: bool) -> None:
        """Gate the eye cursor (calibration stage up, face lost).

        Disallowing performs the same synchronous handoff as a click: the eye
        cursor is confirmed off, then the hardware pointer returns, so there is
        always exactly one cursor and never zero. Allowing lets the normal idle
        rule bring the eye cursor back.
        """
        with self._lock:
            if self._eye_allowed == bool(allowed):
                return
            self._eye_allowed = bool(allowed)
            if not allowed:
                self._set_eye(False)

    def force_eye_mode(self) -> None:
        """A voice command just finished with the mouse: switch to the eye now."""
        with self._lock:
            self._last_activity = time.monotonic()
            if self._eye_allowed:
                self._set_eye(True)

    def poll_once(self, now: float) -> None:
        x, y = self._read_position()
        pressed = bool(self._read_button())
        moved = False
        if self._last_x is not None and self._last_y is not None:
            moved = math.hypot(x - self._last_x, y - self._last_y) > self._threshold
        self._last_x, self._last_y = float(x), float(y)

        with self._lock:
            if pressed or moved:
                self._last_activity = now
                if self._eye_visible:
                    self._set_eye(False)
                return
            if self._eye_visible or now - self._last_activity < self._idle_switch:
                return
            if self._eye_ready():
                self._set_eye(True)

    def _eye_ready(self) -> bool:
        return self._eye_allowed and bool(getattr(self._eye, "ready", True))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once(time.monotonic())
            except Exception as exc:  # pragma: no cover - OS polling guard
                logger.debug("Pointer mode poll failed: %s", exc)
                time.sleep(0.1)
                continue
            time.sleep(self._period)

    def _set_eye(self, visible: bool) -> None:
        if visible == self._eye_visible:
            return
        if visible:
            # The overlay is enabled and re-seeded before the hardware cursor
            # disappears, so there is never a frame with no cursor.
            self._eye.set_visible(True)  # type: ignore[attr-defined]
            self._hide_system_cursor()
        else:
            # The overlay is confirmed off before the hardware cursor returns,
            # so there is never a frame with two cursors.
            self._eye.set_visible(False)  # type: ignore[attr-defined]
            self._show_system_cursor()
        self._eye_visible = visible

    def _hide_system_cursor(self) -> None:
        if not self._system_visible:
            return
        try:
            self._hide_cursor_fn()
            self._system_visible = False
        except Exception as exc:  # pragma: no cover - display guard
            logger.debug("Could not hide the system cursor: %s", exc)

    def _show_system_cursor(self) -> None:
        if self._system_visible:
            return
        try:
            self._show_cursor_fn()
            self._system_visible = True
        except Exception as exc:  # pragma: no cover - display guard
            logger.debug("Could not show the system cursor: %s", exc)


def _display_id() -> int:
    _load_coregraphics()
    from keyless.platform.macos.pointer import _fn

    main = _fn("CGMainDisplayID", ctypes.c_uint32, [])
    return int(main())


def _hide_cursor() -> None:
    from keyless.platform.macos.pointer import _fn

    hide = _fn("CGDisplayHideCursor", ctypes.c_int32, [ctypes.c_uint32])
    hide(_display_id())


def _show_cursor() -> None:
    from keyless.platform.macos.pointer import _fn

    show = _fn("CGDisplayShowCursor", ctypes.c_int32, [ctypes.c_uint32])
    show(_display_id())


def _load_coregraphics() -> bool:
    from keyless.platform.macos.pointer import _load

    if not _load():
        raise RuntimeError("CoreGraphics unavailable; cannot control the pointer.")
    return True


def _left_button_down() -> bool:
    # NSEvent's global state is the most direct read and does not need an
    # event tap; fall back to the Quartz session state if AppKit is unavailable.
    try:
        from AppKit import NSEvent

        if int(NSEvent.pressedMouseButtons()) & 1:
            return True
    except Exception:
        pass

    _load_coregraphics()
    from keyless.platform.macos.pointer import _fn

    state = _fn("CGEventSourceButtonState", ctypes.c_bool, [ctypes.c_int, ctypes.c_uint])
    return bool(state(0, 0))


def _cursor_point() -> tuple[float, float]:
    _load_coregraphics()
    from keyless.platform.macos.pointer import _CGPoint, _fn

    create = _fn("CGEventCreate", ctypes.c_void_p, [ctypes.c_void_p])
    get_loc = _fn("CGEventGetLocation", _CGPoint, [ctypes.c_void_p])
    release = _fn("CFRelease", None, [ctypes.c_void_p])
    event = create(None)
    if not event:
        raise RuntimeError("Could not read the global pointer position.")
    try:
        point = get_loc(event)
        return float(point.x), float(point.y)
    finally:
        release(event)


__all__ = ["MacOSPointerVisibility"]
