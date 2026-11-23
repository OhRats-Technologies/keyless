"""Harvest deliberate macOS mouse-downs as verified gaze targets.

A physical click is only useful as gaze ground truth when the pointer was
already resting on the target: quick drag-clicks and clicks Keyless synthesized
itself are ignored. The looked-at position is taken from the gaze sample
shortly *before* the button went down, so the post-click saccade does not
poison the label.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path

from keyless.input.synthetic_clicks import synthetic_click_recent

logger = logging.getLogger(__name__)

# CGEventSourceStateCombinedSessionState / kCGMouseButtonLeft
_COMBINED_SESSION_STATE = 0
_LEFT_BUTTON = 0


class MacOSMouseClickHarvester:
    """Poll the global pointer and feed stationary clicks to OnlineGazeLearner."""

    def __init__(
        self,
        online_learner: object,
        *,
        poll_hz: float = 120.0,
        min_click_interval_sec: float = 0.45,
        stationary_window_sec: float = 0.12,
        max_stationary_px: float = 12.0,
        max_path_px: float = 30.0,
        max_hold_sec: float = 0.35,
        log_path: Path | None = None,
        read_button=None,  # type: ignore[no-untyped-def]
        read_position=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._learner = online_learner
        self._log_path = log_path
        self._poll_period = 1.0 / max(20.0, float(poll_hz))
        self._min_interval = float(min_click_interval_sec)
        self._stationary_window = float(stationary_window_sec)
        self._max_stationary = float(max_stationary_px)
        self._max_path = float(max_path_px)
        self._max_hold = float(max_hold_sec)
        self._read_button = read_button or _left_button_down
        self._read_position = read_position or _cursor_point

        self._history: deque[tuple[float, float, float]] = deque(maxlen=96)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._down = False
        self._down_ts = 0.0
        self._down_xy: tuple[float, float] | None = None
        self._down_was_stationary = False
        self._down_was_synthetic = False
        self._last_accepted_ts = 0.0
        self.accepted = 0
        self.rejected = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="macos-click-harvest", daemon=True)
        self._thread.start()
        logger.info("Learning gaze from stationary mouse clicks.")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once(time.monotonic())
            except Exception as exc:  # pragma: no cover - OS polling guard
                logger.debug("Mouse click poll failed: %s", exc)
                time.sleep(0.1)
                continue
            time.sleep(self._poll_period)

    def poll_once(self, now: float) -> bool:
        """Advance one poll; returns True when a click label was submitted."""
        pressed = bool(self._read_button())
        x, y = self._read_position()
        self._history.append((now, float(x), float(y)))

        if pressed and not self._down:
            self._down = True
            self._down_ts = now
            self._down_xy = (float(x), float(y))
            self._down_was_stationary = self._stationary_before(now)
            self._down_was_synthetic = synthetic_click_recent()

        if pressed or not self._down:
            return False

        self._down = False
        down_xy = self._down_xy
        down_ts = self._down_ts
        release_xy = (float(x), float(y))
        hold = now - down_ts
        drag = 0.0 if down_xy is None else math.hypot(
            release_xy[0] - down_xy[0], release_xy[1] - down_xy[1]
        )

        reason = ""
        if self._down_was_synthetic:
            reason = "synthetic"
        elif not self._down_was_stationary:
            reason = "moving"
        elif hold > self._max_hold:
            reason = "hold"
        elif drag > 6.0:
            reason = "drag"
        elif down_ts - self._last_accepted_ts < self._min_interval:
            reason = "rate"
        if reason:
            self.rejected += 1
            self._write_log(down_xy, down_ts, hold, drag, False, reason, None)
            logger.debug("Click label skipped (%s).", reason)
            return False

        self._last_accepted_ts = down_ts
        accepted = self._learner.record_verified_interaction(  # type: ignore[attr-defined]
            true_x=down_xy[0],
            true_y=down_xy[1],
            anchor_ts=down_ts,
            source="mouse",
            confidence_weight=1.8,
        )
        outcome = getattr(self._learner, "last_outcome", None) or {}
        if accepted:
            self.accepted += 1
            self._write_log(down_xy, down_ts, hold, drag, True, "accepted", outcome)
        else:
            self.rejected += 1
            self._write_log(
                down_xy,
                down_ts,
                hold,
                drag,
                False,
                str(outcome.get("reason") or "gate"),
                outcome,
            )
        return bool(accepted)

    def _write_log(
        self,
        point: tuple[float, float] | None,
        down_ts: float,
        hold: float,
        drag: float,
        accepted: bool,
        reason: str,
        outcome: dict | None,
    ) -> None:
        if self._log_path is None or point is None:
            return
        record = {
            "ts": time.time(),
            "down_monotonic": down_ts,
            "x": point[0],
            "y": point[1],
            "hold_ms": round(hold * 1000.0, 1),
            "drag_px": round(drag, 1),
            "accepted": accepted,
            "reason": reason,
        }
        if outcome:
            for key in ("error_px", "bias_x", "bias_y"):
                if key in outcome:
                    record[key] = outcome[key]
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def _stationary_before(self, now: float) -> bool:
        points = [p for p in self._history if now - p[0] <= self._stationary_window]
        if len(points) < 4:
            return False
        travel = sum(
            math.hypot(b[1] - a[1], b[2] - a[2])
            for a, b in zip(points, points[1:])
        )
        displacement = math.hypot(
            points[-1][1] - points[0][1],
            points[-1][2] - points[0][2],
        )
        return displacement <= self._max_stationary and travel <= self._max_path


class MacOSPointerVisibility:
    """Keep exactly one pointer visible: the real one while the mouse moves,
    the eye cursor while the mouse rests.

    At rest the hardware cursor is hidden and the cua agent cursor is shown.
    Any physical movement or button press brings the hardware cursor back and
    hides the eye cursor, so a click never shows two arrows.
    """

    def __init__(
        self,
        eye_cursor: object,
        *,
        idle_switch_sec: float = 1.2,
        move_threshold_px: float = 2.0,
        poll_hz: float = 60.0,
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

    def force_eye_mode(self) -> None:
        """Voice steering just used the mouse; switch to the eye cursor now."""
        with self._lock:
            self._last_activity = time.monotonic()
            self._set_eye(True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once(time.monotonic())
            except Exception as exc:  # pragma: no cover - OS polling guard
                logger.debug("Pointer mode poll failed: %s", exc)
                time.sleep(0.1)
                continue
            time.sleep(self._period)

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
        return bool(getattr(self._eye, "ready", True))

    def _set_eye(self, visible: bool) -> None:
        if visible == self._eye_visible:
            return
        if visible:
            self._eye.set_visible(True)  # type: ignore[attr-defined]
            self._hide_system_cursor()
        else:
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


def _load_coregraphics():  # type: ignore[no-untyped-def]
    from keyless.platform.macos.pointer import _load

    if not _load():
        raise RuntimeError("CoreGraphics unavailable; cannot monitor mouse clicks.")
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
    return bool(state(_COMBINED_SESSION_STATE, _LEFT_BUTTON))


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


__all__ = ["MacOSMouseClickHarvester"]
