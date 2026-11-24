"""macOS menu-bar item for ``keyless ui``: open the web UI, toggle eye tracking, quit.

AppKit must own the main thread, so ``run()`` blocks there while the control
server keeps running on its own background threads. The app is an accessory
(menu bar only, no Dock icon). Ctrl+C in the launching terminal still quits.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable

import objc  # type: ignore[import-not-found]
from AppKit import (  # type: ignore[import-not-found]
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer  # type: ignore[import-not-found]
from PyObjCTools import AppHelper  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

_ON = 1  # NSControlStateValueOn
_OFF = 0


class _MenuTarget(NSObject):
    """Objective-C target for the menu actions; forwards to the Python tray."""

    def initWithTray_(self, tray):  # type: ignore[no-untyped-def]
        self = objc.super(_MenuTarget, self).init()
        if self is None:
            return None
        self.tray = tray
        return self

    def openUI_(self, _sender) -> None:  # type: ignore[no-untyped-def]
        self.tray._open_ui()

    def toggleGaze_(self, _sender) -> None:  # type: ignore[no-untyped-def]
        self.tray._toggle_gaze()

    def quit_(self, _sender) -> None:  # type: ignore[no-untyped-def]
        self.tray.stop()

    def menuWillOpen_(self, _menu) -> None:  # type: ignore[no-untyped-def]
        self.tray.refresh()

    def tick_(self, _timer) -> None:  # type: ignore[no-untyped-def]
        self.tray._tick()


class macOSTray:
    """Menu-bar item. ``run()`` blocks on the main thread until Quit or Ctrl+C."""

    def __init__(
        self,
        *,
        open_ui: Callable[[], None] | None = None,
        gaze_running: Callable[[], bool] | None = None,
        set_gaze: Callable[[bool], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._open_ui_cb = open_ui
        self._gaze_running = gaze_running or (lambda: False)
        self._set_gaze = set_gaze
        self._on_quit = on_quit
        self._item = None
        self._gaze_item = None
        self._target = None
        self._state = "idle"
        self._signalled = False

    # --- Tray protocol ------------------------------------------------------

    def start(self) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._target = _MenuTarget.alloc().initWithTray_(self)
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._item.button().setToolTip_("Keyless")
        menu = NSMenu.alloc().init()
        menu.setDelegate_(self._target)
        menu.setAutoenablesItems_(False)
        menu.addItem_(self._menu_item("Open Keyless", "openUI:", "o"))
        self._gaze_item = self._menu_item("Eye tracking", "toggleGaze:", "")
        self._gaze_item.setEnabled_(self._set_gaze is not None)
        menu.addItem_(self._gaze_item)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._menu_item("Quit Keyless", "quit:", "q"))
        self._item.setMenu_(menu)
        self.refresh()
        # Python signal handlers only run when Python code runs; this timer gives them
        # (and the icon, which the web UI can change) a turn every second.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self._target, "tick:", None, True
        )

    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        if self._item is not None:
            self._item.button().setToolTip_(f"Keyless — {detail or state}")

    def stop(self) -> None:
        """Quit: run the shutdown hook (gaze off, pointer back, server down), then exit."""
        try:
            if self._on_quit is not None:
                self._on_quit()
        finally:
            AppHelper.stopEventLoop()

    # --- loop -----------------------------------------------------------------

    def run(self) -> None:
        if self._item is None:
            self.start()
        AppHelper.runEventLoop()

    def _on_signal(self, _signum, _frame) -> None:  # type: ignore[no-untyped-def]
        self._signalled = True

    def _tick(self) -> None:
        if self._signalled:
            self.stop()
            return
        self.refresh()

    def refresh(self) -> None:
        on = bool(self._gaze_running())
        if self._gaze_item is not None:
            self._gaze_item.setState_(_ON if on else _OFF)
        if self._item is not None:
            symbol = "eye" if on else "eye.slash"
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "Keyless")
            if image is not None:
                image.setTemplate_(True)  # follows light/dark menu bar
                self._item.button().setImage_(image)
                self._item.button().setTitle_("")
            else:
                self._item.button().setTitle_("Keyless")

    def _menu_item(self, title: str, action: str, key: str):  # type: ignore[no-untyped-def]
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        item.setTarget_(self._target)
        return item

    def _open_ui(self) -> None:
        if self._open_ui_cb is not None:
            self._open_ui_cb()

    def _toggle_gaze(self) -> None:
        if self._set_gaze is None:
            return
        try:
            self._set_gaze(not self._gaze_running())
        except Exception as exc:  # camera missing, permission denied: keep the app alive
            logger.warning("Eye tracking toggle failed: %s", exc)
            self.set_state("error", str(exc))
        self.refresh()


__all__ = ["macOSTray"]
