"""Passive left-click listener for macOS (the mouse is a teacher, AGENTS rule 5).

A listen-only ``CGEventTap`` on left-mouse-down. Listen-only taps need Input
Monitoring (``kTCCServiceListenEvent``), not Accessibility, and can never
block, modify, or inject events. The observer never prompts by itself:
``request_access()`` is called only from an explicit user action.

Event locations are CG global coordinates (top-left of the primary display,
y down, logical points) - the same space as ``platform/macos/display.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

NEEDS_PERMISSION = "input_monitoring"


def _quartz() -> Any | None:
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError:
        logger.info("pyobjc Quartz is not installed; macOS click capture is off")
        return None
    return Quartz


def listen_access_granted() -> bool | None:
    """Fresh Input Monitoring preflight; None when Quartz is unavailable."""
    quartz = _quartz()
    return None if quartz is None else bool(quartz.CGPreflightListenEventAccess())


def request_access() -> bool | None:
    """Add Keyless to the Input Monitoring list and show the OS prompt once."""
    quartz = _quartz()
    return None if quartz is None else bool(quartz.CGRequestListenEventAccess())


class MacClickObserver:
    """Calls ``on_click(x, y, t_click=monotonic)`` for each physical left-click."""

    def __init__(self, on_click: Callable[..., Any]) -> None:
        self._on_click = on_click
        self._thread: threading.Thread | None = None
        self._loop: Any = None
        self._tap: Any = None
        self._ready = threading.Event()
        self.unavailable_reason = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._tap is not None

    def start(self) -> bool:
        if self.running:
            return True
        granted = listen_access_granted()
        if granted is None:
            self.unavailable_reason = "Quartz (pyobjc) is not installed"
            return False
        if not granted:
            self.unavailable_reason = NEEDS_PERMISSION
            return False
        self._ready.clear()
        self.unavailable_reason = ""
        self._thread = threading.Thread(target=self._run, name="keyless-mac-clicks", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        if self._tap is None:
            self.unavailable_reason = self.unavailable_reason or "event tap could not be created"
            return False
        return True

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        quartz = _quartz() if loop is not None else None
        if quartz is not None:
            quartz.CFRunLoopStop(loop)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._tap = None

    def _callback(self, _proxy: Any, etype: int, event: Any, _refcon: Any) -> Any:
        import Quartz  # type: ignore[import-not-found]

        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            # macOS disables slow taps; ours does no work inline, so just re-arm.
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            return event
        if etype == Quartz.kCGEventLeftMouseDown:
            t_click = time.monotonic()
            loc = Quartz.CGEventGetLocation(event)
            try:
                self._on_click(float(loc.x), float(loc.y), t_click=t_click)
            except Exception:
                logger.debug("click handler failed", exc_info=True)
        return event

    def _run(self) -> None:
        try:
            import Quartz  # type: ignore[import-not-found]

            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown),
                self._callback,
                None,
            )
            if tap is None:
                self.unavailable_reason = NEEDS_PERMISSION
                return
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            self._tap, self._loop = tap, loop
        except Exception as exc:
            self.unavailable_reason = f"event tap failed: {exc}"
            return
        finally:
            self._ready.set()
        Quartz.CFRunLoopRun()
        Quartz.CGEventTapEnable(tap, False)


__all__ = ["MacClickObserver", "NEEDS_PERMISSION", "listen_access_granted", "request_access"]
