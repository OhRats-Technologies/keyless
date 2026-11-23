"""Linux evdev hotkey and push-to-talk input."""

from __future__ import annotations

import glob
import math
import os
import selectors
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import evdev
from evdev import InputDevice, ecodes

try:
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib  # type: ignore[import-not-found]

    _HAS_GIO = True
except Exception:
    _HAS_GIO = False


def resolve_hotkey_code(key_name: str) -> int:
    """Resolve an evdev key name (e.g. `KEY_RIGHTCTRL`, `BTN_SIDE`) to its numeric code."""
    normalized = key_name.strip().upper()
    if not normalized.startswith(("KEY_", "BTN_")):
        normalized = f"KEY_{normalized}"
    code = getattr(ecodes, normalized, None)
    if code is None:
        raise ValueError(f"Unknown evdev hotkey name: {key_name!r}")
    return int(code)


class EvdevHotkeyMonitor:
    """Monitors physical `/dev/input/event*` devices for a Wayland-agnostic PTT key."""

    def __init__(
        self,
        hotkey_code: int,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self.hotkey_code = int(hotkey_code)
        self._on_press = on_press
        self._on_release = on_release
        self._devices: list[InputDevice] = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="evdev-ptt-monitor", daemon=True)
        self._pressed = False

    def start(self) -> int:
        """Open matching readable input devices and start listening; returns device count."""
        for path in sorted(glob.glob("/dev/input/event*")):
            if not os.access(path, os.R_OK):
                continue
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if dev.name.startswith("keyless"):
                dev.close()
                continue
            caps = dev.capabilities()
            keys = caps.get(ecodes.EV_KEY, [])
            if self.hotkey_code in keys:
                self._devices.append(dev)
            else:
                dev.close()

        if self._devices:
            self._thread.start()
        return len(self._devices)

    def _run(self) -> None:
        sel = selectors.DefaultSelector()
        for dev in self._devices:
            sel.register(dev, selectors.EVENT_READ)
        try:
            while not self._stop_event.is_set():
                events = sel.select(timeout=0.1)
                for key, _ in events:
                    dev: InputDevice = key.fileobj  # type: ignore[assignment]
                    try:
                        for ev in dev.read():
                            if ev.type == ecodes.EV_KEY and ev.code == self.hotkey_code:
                                if ev.value == 1 and not self._pressed:
                                    self._pressed = True
                                    self._on_press()
                                elif ev.value == 0 and self._pressed:
                                    self._pressed = False
                                    self._on_release()
                    except OSError:
                        continue
        finally:
            sel.close()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        for dev in self._devices:
            try:
                dev.close()
            except OSError:
                pass
        self._devices.clear()
