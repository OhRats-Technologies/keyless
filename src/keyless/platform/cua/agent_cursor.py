"""Eye-driven cursor on cua-driver's independent agent overlay.

cua-driver draws a per-session agent cursor in its own click-through window.
``move_cursor`` without a target moves only that overlay in screen points; it
never touches the OS pointer. The overlay render loop runs at 60 fps, while a
single ``move_cursor`` reply can trail by a quarter second because the tool
waits for the glide to arrive. Gaze therefore sends fire-and-forget targets at
camera rate and lets the overlay interpolate the newest one.

The daemon processes overlapping ``move_cursor`` calls concurrently, so the
pump keeps one update in flight per display frame instead of serially waiting
for each arrival. A dropped frame is preferable to blocking the gaze loop.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from keyless.input.synthetic_clicks import mark_synthetic_click
from keyless.platform.cua.driver import CuaDriverManager

logger = logging.getLogger(__name__)

SESSION_LABEL = "keyless-gaze"

# One target per display frame. The overlay itself animates at 60 fps; sending
# faster only replaces the target before it is drawn.
_MOVE_INTERVAL_S = 1.0 / 60.0

# Fixed timing with a one-frame glide keeps the drawn cursor close to the
# latest gaze sample. Larger values add visible lag; native timing adds its
# own 600 ms floor.
_CURSOR_MOTION = {
    "style": "classic",
    "timing": "fixed",
    "glide_duration_ms": 16,
    "turn_radius": 1,
    "arc_size": 0,
    "spring": 1,
    "effects": {
        "glow": "off",
        "trail": "off",
        "ripple": "off",
        "squish": "off",
        "magnet": "off",
    },
    "idle_hide_ms": 0,
}


class AgentCursorOverlay:
    """Pump gaze positions into one named cua-driver agent cursor."""

    def __init__(
        self,
        driver: CuaDriverManager | None = None,
        session: str = SESSION_LABEL,
    ) -> None:
        self.session = session
        self._driver = driver or CuaDriverManager()
        self._condition = threading.Condition()
        self._target: tuple[float, float] | None = None
        self._position: tuple[float, float] | None = None
        self._ack: tuple[tuple[float, float], tuple[float, float], float] | None = None
        self._visible = True
        self._visibility_gen = 0
        self._visibility_done = 0
        self._closed = False
        self._ready = False
        self._error = ""
        self.ownership: object | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="keyless-agent-cursor",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def available() -> tuple[bool, str]:
        """Whether this host can draw the agent cursor at all."""
        if sys.platform != "darwin":
            return False, "The cua-driver agent cursor is macOS-only."
        try:
            from keyless.models import runtimes

            status = runtimes.status()
        except Exception as exc:  # pragma: no cover - import/runtime guard
            return False, f"Could not inspect cua-driver: {exc}"
        if not status.get("ready"):
            return False, (
                f"cua-driver {runtimes.CUA_DRIVER_VERSION} is not downloaded "
                "(run `keyless models download cua-driver`)."
            )
        return True, ""

    @property
    def error(self) -> str:
        return self._error

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    def move_absolute(self, x: float, y: float) -> None:
        with self._condition:
            if self._closed:
                return
            self._position = (float(x), float(y))
            self._target = self._position
            self._condition.notify()

    def move_relative(self, dx: float, dy: float) -> None:
        with self._condition:
            if self._closed or self._position is None:
                return
            self._position = (
                self._position[0] + float(dx),
                self._position[1] + float(dy),
            )
            self._target = self._position
            self._condition.notify()

    def acknowledge_at(self, x: float, y: float) -> None:
        """Snap the cursor to ``(x, y)`` with a short overshoot, for feedback."""
        with self._condition:
            if self._closed:
                return
            start = self._position or (float(x), float(y))
            self._ack = (start, (float(x), float(y)), time.monotonic())
            self._target = None
            self._condition.notify()

    def set_visible(self, visible: bool) -> None:
        """Show or hide the eye cursor (the pointer-mode coordinator calls this).

        Waits for the pump to apply the change so the caller can show the
        system cursor only after the eye cursor is really gone (and vice
        versa), which keeps exactly one cursor on screen during the handoff.
        """
        with self._condition:
            if self._closed or self._visible == bool(visible):
                return
            self._visible = bool(visible)
            self._visibility_gen += 1
            generation = self._visibility_gen
            if not visible:
                self._target = None
                self._ack = None
            self._condition.notify_all()
            if not self._ready:
                return
            deadline = time.monotonic() + 0.5
            while not self._closed and self._visibility_done < generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.debug("Agent cursor visibility handoff timed out.")
                    break
                self._condition.wait(timeout=remaining)

    def click_at(
        self,
        x: float,
        y: float,
        *,
        button: str = "left",
        count: int = 1,
    ) -> bool:
        """Click the window under ``(x, y)`` without touching the real pointer.

        cua-driver's window-target click posts events to that window in the
        background. If no ordinary window is under the eye cursor, nothing is
        sent: a desktop-scope click would warp the user's pointer.
        """
        target = self._window_under_point(x, y)
        if target is None:
            return False
        pid, window_id, bounds, screenshot_w, screenshot_h = target
        local_x = (x - bounds["x"]) * screenshot_w / bounds["width"]
        local_y = (y - bounds["y"]) * screenshot_h / bounds["height"]
        result = self._driver.call(
            "click",
            {
                "target": {"kind": "window", "pid": pid, "window_id": window_id},
                "x": local_x,
                "y": local_y,
                "button": button,
                "count": max(1, int(count)),
                "post_action_wait": "none",
            },
            timeout_s=30.0,
        )
        if result.get("isError"):
            logger.warning("Agent cursor click refused: %s", result.get("content"))
            return False
        return True

    def scroll_at(self, x: float, y: float, *, direction: str, amount: int) -> bool:
        """Wheel-scroll the window under the eye cursor, never the real pointer."""
        target = self._window_under_point(x, y)
        if target is None:
            return False
        pid, window_id, bounds, screenshot_w, screenshot_h = target
        result = self._driver.call(
            "scroll",
            {
                "pid": pid,
                "window_id": window_id,
                "x": (x - bounds["x"]) * screenshot_w / bounds["width"],
                "y": (y - bounds["y"]) * screenshot_h / bounds["height"],
                "direction": direction,
                "amount": max(1, int(amount)),
            },
            timeout_s=30.0,
        )
        if result.get("isError"):
            logger.warning("Agent cursor scroll refused: %s", result.get("content"))
            return False
        return True

    def _window_under_point(
        self, x: float, y: float
    ) -> tuple[int, int, dict, int, int] | None:
        listing = self._driver.call("list_windows", {}, timeout_s=10.0).get("structured", {})
        own_pid = getattr(self._driver, "daemon_pid", None)
        candidates = []
        for window in listing.get("windows", []):
            if not window.get("is_on_screen") or window.get("layer") != 0:
                continue
            if own_pid is not None and window.get("pid") == own_pid:
                continue
            bounds = window.get("bounds") or {}
            if not _contains(bounds, x, y):
                continue
            candidates.append(window)
        if not candidates:
            return None
        window = max(candidates, key=lambda w: w.get("z_index") or 0)
        state = self._driver.call(
            "get_window_state",
            {
                "pid": window["pid"],
                "window_id": window["window_id"],
                "tree_format": "elements",
            },
            timeout_s=30.0,
        ).get("structured", {})
        bounds = state.get("window_bounds") or window["bounds"]
        width = state.get("screenshot_width")
        height = state.get("screenshot_height")
        if not width or not height or not _contains(bounds, x, y):
            return None
        return (
            int(window["pid"]),
            int(window["window_id"]),
            bounds,
            int(width),
            int(height),
        )

    def _run(self) -> None:
        if not self._start():
            return
        while not self._closed:
            target = self._wait_for_work()
            if self._closed:
                break
            with self._condition:
                visible = self._visible
                generation = (
                    self._visibility_gen
                    if self._visibility_done < self._visibility_gen
                    else 0
                )
            if generation:
                applied = self._apply_visibility(visible)
                with self._condition:
                    if self._visibility_done < generation:
                        self._visibility_done = generation
                    self._condition.notify_all()
                if not applied:
                    break
            if not visible:
                continue
            ack = self._ack_point()
            if ack is not None:
                target = ack
            if target is not None and not self._send(target):
                break
        self._shutdown()

    def _apply_visibility(self, visible: bool) -> bool:
        try:
            result = self._driver.call(
                "set_agent_cursor_enabled",
                {"session": self.session, "enabled": visible},
                timeout_s=2.0,
            )
        except Exception as exc:
            self._error = f"cua-driver did not update the agent cursor: {exc}"
            logger.warning("%s", self._error)
            return False
        if result.get("isError"):
            self._error = f"cua-driver refused the agent cursor toggle: {result.get('content')}"
            logger.warning("%s", self._error)
            return False
        if visible and self._position is not None:
            # Re-enabling may reset the overlay to its off-screen sentinel, so
            # put it back where the gaze already was.
            x, y = self._position
            self._driver.call(
                "move_cursor",
                {"session": self.session, "x": x, "y": y},
                timeout_s=2.0,
            )
        return True

    def _ack_point(self) -> tuple[float, float] | None:
        """Current point of the short snap acknowledgement, if one is playing."""
        with self._condition:
            ack = self._ack
            if ack is None:
                return None
            start, target, started = ack
            elapsed = time.monotonic() - started
            if elapsed >= 0.28:
                self._ack = None
                self._position = target
                return target
            if elapsed < 0.10:
                point = _lerp(start, (target[0] + 10.0, target[1] - 8.0), elapsed / 0.10)
            elif elapsed < 0.20:
                point = _lerp(
                    (target[0] + 10.0, target[1] - 8.0),
                    target,
                    (elapsed - 0.10) / 0.10,
                )
            else:
                point = target
            self._position = point
            return point

    def _send(self, target: tuple[float, float]) -> bool:
        if self._driver.call_soon(
            "move_cursor",
            {"session": self.session, "x": target[0], "y": target[1]},
        ):
            return True
        self._error = "cua-driver stopped while moving the agent cursor."
        logger.warning("%s", self._error)
        return False

    def _start(self) -> bool:
        try:
            self._driver.start()
            self._configure()
        except Exception as exc:
            self._error = str(exc)
            logger.warning("Agent cursor overlay unavailable: %s", exc)
            self._shutdown()
            return False
        with self._condition:
            self._ready = True
            self._condition.notify_all()
        logger.info("Agent cursor overlay ready (session %s).", self.session)
        return True

    def _wait_for_work(self) -> tuple[float, float] | None:
        """Block one frame, then hand back the newest target, if any."""
        with self._condition:
            if self._closed:
                return None
            self._condition.wait(timeout=_MOVE_INTERVAL_S)
            while (
                not self._closed
                and self._target is None
                and self._ack is None
                and self._visibility_done >= self._visibility_gen
            ):
                self._condition.wait(timeout=_MOVE_INTERVAL_S)
            if self._closed:
                return None
            target = self._target
            self._target = None
            return target

    def _configure(self) -> None:
        driver = self._driver
        started = driver.call(
            "start_session",
            {"session": self.session, "cursor_motion": _CURSOR_MOTION},
            timeout_s=15.0,
        )
        if started.get("isError"):
            raise RuntimeError(f"cua-driver refused the cursor session: {started.get('content')}")
        motion = driver.call(
            "set_agent_cursor_motion",
            {"session": self.session, **_CURSOR_MOTION},
            timeout_s=10.0,
        )
        if motion.get("isError"):
            raise RuntimeError(f"cua-driver refused the cursor motion: {motion.get('content')}")
        result = driver.call(
            "set_agent_cursor_enabled",
            {"session": self.session, "enabled": True},
            timeout_s=10.0,
        )
        if result.get("isError"):
            raise RuntimeError(f"cua-driver refused the agent cursor: {result.get('content')}")
        self._seed_visible()

    def _seed_visible(self) -> None:
        """Place the cursor on screen immediately, before the first gaze sample.

        ``set_agent_cursor_enabled`` alone creates a cursor at the off-screen
        sentinel; without a move it stays invisible until the first click or
        gaze update. The eye cursor is always-on, so seed it at screen center.
        """
        try:
            screen = self._driver.call("get_screen_size", {}, timeout_s=10.0)
        except Exception as exc:  # pragma: no cover - best-effort placement
            logger.debug("Agent cursor screen size unavailable: %s", exc)
            return
        info = screen.get("structured", {})
        width = float(info.get("width") or 0.0)
        height = float(info.get("height") or 0.0)
        if width <= 0 or height <= 0:
            return
        self._driver.call(
            "move_cursor",
            {"session": self.session, "x": width / 2.0, "y": height / 2.0},
            timeout_s=10.0,
        )
        with self._condition:
            self._position = (width / 2.0, height / 2.0)

    def _shutdown(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        try:
            self._driver.call(
                "set_agent_cursor_enabled",
                {"session": self.session, "enabled": False},
                timeout_s=3.0,
            )
            self._driver.call("end_session", {"session": self.session}, timeout_s=3.0)
        except Exception as exc:
            # Cleanup is best effort: a dead transport or an already-ended
            # session must not mask the reason the pump stopped.
            logger.debug("Agent cursor cleanup skipped: %s", exc)
        finally:
            self._stop_driver()

    def _stop_driver(self) -> None:
        try:
            self._driver.stop()
        except Exception as exc:  # pragma: no cover - best effort teardown
            # The pump is already stopping; report and let the process reclaim
            # the child instead of raising out of a background thread.
            logger.debug("Agent cursor driver stop failed: %s", exc)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        ownership, self.ownership = self.ownership, None
        closer = getattr(ownership, "close", None)
        if callable(closer):
            closer()


class AgentCursorSink:
    """Gaze movement sink: overlay for motion, action pointer for everything else."""

    is_agent_cursor_sink = True

    def __init__(self, overlay: AgentCursorOverlay, action_pointer: object | None = None) -> None:
        self._overlay = overlay
        self._action = action_pointer
        self._pos: tuple[float, float] | None = None

    @property
    def overlay(self) -> AgentCursorOverlay:
        return self._overlay

    def move_absolute(self, x: float, y: float) -> None:
        self._pos = (float(x), float(y))
        self._overlay.move_absolute(x, y)
        note = getattr(self._action, "note_gaze_position", None)
        if callable(note):
            note(x, y)

    def move_relative(self, dx: float, dy: float) -> None:
        if self._pos is not None:
            self._pos = (self._pos[0] + float(dx), self._pos[1] + float(dy))
        self._overlay.move_relative(dx, dy)

    def acknowledge(self, x: float, y: float) -> None:
        self._overlay.acknowledge_at(x, y)

    def click(self, button: int = 1) -> None:
        self._click_at(button, count=1)

    def double_click(self, button: int = 1) -> None:
        self._click_at(button, count=2)

    def _click_at(self, button: int, *, count: int) -> None:
        if self._pos is None:
            logger.warning("Eye cursor has no position yet; click not sent.")
            return
        name = {1: "left", 2: "right", 3: "middle"}.get(button, "left")
        mark_synthetic_click()
        if not self._overlay.click_at(self._pos[0], self._pos[1], button=name, count=count):
            logger.warning(
                "Eye cursor found no clickable window at (%.0f, %.0f); "
                "the real pointer was left untouched.",
                self._pos[0],
                self._pos[1],
            )

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        if self._pos is None:
            return
        if dy:
            direction = "down" if dy < 0 else "up"
            self._overlay.scroll_at(self._pos[0], self._pos[1], direction=direction, amount=abs(int(dy)))
        if dx:
            direction = "right" if dx > 0 else "left"
            self._overlay.scroll_at(self._pos[0], self._pos[1], direction=direction, amount=abs(int(dx)))

    def __getattr__(self, name: str):  # noqa: ANN001 - transparent action-pointer delegate
        if self._action is None:
            raise AttributeError(name)
        return getattr(self._action, name)

    def close(self) -> None:
        self._overlay.close()
        closer = getattr(self._action, "close", None)
        if callable(closer):
            closer()


def _lerp(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    t = max(0.0, min(1.0, t))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _contains(bounds: dict, x: float, y: float) -> bool:
    try:
        left = float(bounds["x"])
        top = float(bounds["y"])
        width = float(bounds["width"])
        height = float(bounds["height"])
    except (KeyError, TypeError, ValueError):
        return False
    return left <= x < left + width and top <= y < top + height


def create_gaze_sink(
    config: object,
    action_pointer: object,
    dry_run: bool,
) -> tuple[object, AgentCursorOverlay | None, bool]:
    """Build the eye-cursor sink, or explain why gaze stays internal.

    Returns ``(pointer, overlay, move_enabled)``. On macOS with the overlay
    enabled, the returned pointer draws motion on cua-driver's own cursor and
    delegates every other action to ``action_pointer``. If the overlay cannot
    start, ``move_enabled`` is False so the gaze loop never falls back to
    warping the real pointer.
    """
    if getattr(action_pointer, "is_agent_cursor_sink", False):
        return action_pointer, None, True

    control = bool(getattr(config, "gaze_cursor_control", False))
    overlay_requested = (
        control
        and not dry_run
        and bool(getattr(config, "gaze_cursor_overlay", True))
        and sys.platform == "darwin"
    )
    if not overlay_requested:
        return action_pointer, None, control

    available, reason = AgentCursorOverlay.available()
    if not available:
        logger.warning("Eye cursor stays internal: %s", reason)
        return action_pointer, None, False
    from keyless.config.paths import current_paths
    from keyless.platform.macos.gaze_cursor import claim_eye_cursor

    # Exactly one eye cursor (AGENTS rule 4): the gaze publisher usually owns it.
    ownership = claim_eye_cursor(current_paths().cache_dir)
    if ownership is None:
        logger.info("Eye cursor is drawn by the gaze publisher; the daemon stays internal.")
        return action_pointer, None, False
    overlay = AgentCursorOverlay()
    overlay.ownership = ownership  # held for the overlay's lifetime; the OS frees it on exit
    return AgentCursorSink(overlay, action_pointer), overlay, True


__all__ = ["AgentCursorOverlay", "AgentCursorSink", "SESSION_LABEL", "create_gaze_sink"]
