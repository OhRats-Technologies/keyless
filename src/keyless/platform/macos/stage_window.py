"""Native macOS calibration stage: one click-through window per display.

The macOS counterpart of the stage mode in ``platform/linux/overlay_wayland.c``.
Python (``gaze/calibration.py``) owns timing and writes ``keyless_stage_v1``
(``gaze/stage.py``); this process only draws it:

* every display is dimmed to ``dim`` (window background, black with alpha);
* the stimulus is drawn on the display that contains ``(x, y)``, radius
  ``120 * size`` points: amber halo, spinning spiral disc, burst ring, and a
  pip that is always full strength;
* windows are borderless, non-activating, ignore the mouse, sit at
  screen-saver level and join every Space, so focus and clicks are untouched.

Run as ``python -m keyless.platform.macos.stage_window <stage_shm> <parent_pid>``.
Prints ``ready`` once windows are up; exits on ``STAGE_DONE`` or parent death.
Coordinates are CG global points (top-left of the primary display, y down),
the same space as ``platform/macos/display.py``.
"""

from __future__ import annotations

import math
import os
import struct
import sys
from multiprocessing import shared_memory

import objc  # type: ignore[import-not-found]
from AppKit import (  # type: ignore[import-not-found]
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSColorSpace,
    NSGradient,
    NSGraphicsContext,
    NSPanel,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSObject, NSTimer  # type: ignore[import-not-found]

from keyless.gaze.stage import STAGE_ACTIVE, STAGE_DONE, STAGE_PAUSED, STAGE_SHM_SIZE, STAGE_TARGET

_STRUCT = struct.Struct("=QddffffII")
_BASE_RADIUS_PT = 120.0
_REACH = 2.4  # halo extent in radii, as in the Wayland stage

# OhRats palette (same constants as overlay_wayland.c).
_AMBER = (0.886, 0.478, 0.231)
_IVORY = (0.957, 0.953, 0.875)
_TERRACOTTA = (0.42, 0.16, 0.07)


def _rgba(rgb: tuple[float, float, float], alpha: float):  # type: ignore[no-untyped-def]
    return NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], max(0.0, min(1.0, alpha)))


def read_stage(buf) -> tuple | None:  # type: ignore[no-untyped-def]
    """Seqlock read of the stage block; None while the writer is mid-update."""
    for _ in range(8):
        seq1 = struct.unpack_from("=Q", buf, 0)[0]
        if seq1 & 1:
            continue
        values = _STRUCT.unpack_from(buf, 0)
        if values[0] == seq1 == struct.unpack_from("=Q", buf, 0)[0]:
            return values
    return None


class StimulusView(NSView):
    """Square view centred on the target; draws the stimulus around its centre."""

    def initWithFrame_(self, frame):  # type: ignore[no-untyped-def]
        self = objc.super(StimulusView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.radius = 0.0
        self.phase = 0.0
        self.burst = -1.0
        self.paused = False
        return self

    def isFlipped(self) -> bool:
        return True

    def drawRect_(self, _rect) -> None:  # type: ignore[no-untyped-def]
        R = float(self.radius)
        if R <= 0.5:
            return
        bounds = self.bounds()
        cx, cy = bounds.size.width / 2.0, bounds.size.height / 2.0
        master = 0.38 if self.paused else 1.0
        spin = 0.0 if self.paused else float(self.phase)
        Rb = R * (1.0 + 0.04 * math.sin(self.phase * 2.1))

        def circle(r: float):  # type: ignore[no-untyped-def]
            return NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, 2 * r, 2 * r))

        # 1. halo
        # Gaussian falloff exp(-1.6 rho^2) like the Wayland stage, sampled as gradient stops.
        stops = [i / 8.0 for i in range(9)]
        colors = [_rgba(_AMBER, 0.42 * master * math.exp(-1.6 * (u * _REACH) ** 2)) for u in stops]
        halo = NSGradient.alloc().initWithColors_atLocations_colorSpace_(colors, stops, NSColorSpace.sRGBColorSpace())
        halo.drawInBezierPath_relativeCenterPosition_(circle(R * _REACH), (0.0, 0.0))
        # 2. disc: terracotta base, warm core, three spinning spiral arms
        _rgba(_TERRACOTTA, 0.55 * master).setFill()
        circle(Rb).fill()
        core = NSGradient.alloc().initWithColors_([_rgba(_IVORY, 0.92 * master), _rgba(_AMBER, 0.6 * master)])
        core.drawInBezierPath_relativeCenterPosition_(circle(Rb * 0.55), (0.0, 0.0))
        ctx = NSGraphicsContext.currentContext()
        ctx.saveGraphicsState()
        circle(Rb).addClip()
        _rgba(_IVORY, 0.55 * master).setStroke()
        for arm in range(3):
            path = NSBezierPath.bezierPath()
            path.setLineWidth_(max(1.5, Rb * 0.16))
            for i in range(25):
                rho = i / 24.0
                # Constant phase of sin(3*th + 17*sqrt(rho) - 5.5*spin), as in the C shader.
                th = (2.0 * math.pi * arm + 5.5 * spin - 17.0 * math.sqrt(rho)) / 3.0
                pt = (cx + rho * Rb * math.cos(th), cy + rho * Rb * math.sin(th))
                if i == 0:
                    path.moveToPoint_(pt)
                else:
                    path.lineToPoint_(pt)
            path.stroke()
        ctx.restoreGraphicsState()
        # 3. burst ring
        b = float(self.burst)
        if 0.0 < b < 1.0:
            ring = circle(Rb * (1.0 + 1.3 * b))
            ring.setLineWidth_(2.0 + 3.0 * (1.0 - b))
            _rgba(_IVORY, 0.9 * (1.0 - b) ** 2).setStroke()
            ring.stroke()
        # 4. pip: dark ring, then an ivory point at full strength
        pip = max(2.4, R * 0.045)
        _rgba((0.05, 0.03, 0.02), 0.85).setFill()
        circle(pip + 2.0).fill()
        _rgba(_IVORY, 1.0).setFill()
        circle(pip).fill()


class Stage(NSObject):
    def initWithShm_parent_(self, shm_name: str, parent_pid: int):  # type: ignore[no-untyped-def]
        self = objc.super(Stage, self).init()
        if self is None:
            return None
        self.shm = shared_memory.SharedMemory(name=shm_name, create=False, track=False)
        self.parent_pid = int(parent_pid)
        self.last_seq = -1
        self.dim = -1.0
        self.displays = []  # (window, stimulus view, cg_x, cg_y, width, height)
        screens = list(NSScreen.screens() or [])
        primary_h = float(screens[0].frame().size.height)
        for screen in screens:
            frame = screen.frame()
            win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
                NSBackingStoreBuffered, False,
            )
            win.setLevel_(NSScreenSaverWindowLevel)
            win.setOpaque_(False)
            win.setHasShadow_(False)
            win.setIgnoresMouseEvents_(True)
            win.setBackgroundColor_(NSColor.clearColor())
            win.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorIgnoresCycle
            )
            win.setFrame_display_(frame, False)
            stim = StimulusView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
            stim.setHidden_(True)
            win.contentView().addSubview_(stim)
            win.orderFrontRegardless()
            cg_y = primary_h - float(frame.origin.y) - float(frame.size.height)
            self.displays.append((win, stim, float(frame.origin.x), cg_y,
                                  float(frame.size.width), float(frame.size.height)))
        return self

    def tick_(self, _timer) -> None:  # type: ignore[no-untyped-def]
        if os.getppid() != self.parent_pid:
            NSApplication.sharedApplication().terminate_(None)
            return
        st = read_stage(self.shm.buf)
        if st is None:
            return
        seq, x, y, size, phase, dim, burst, flags, _ = st
        if flags & STAGE_DONE:
            NSApplication.sharedApplication().terminate_(None)
            return
        if seq == self.last_seq:
            return
        self.last_seq = seq
        dim = max(0.0, min(1.0, dim)) if flags & STAGE_ACTIVE else 0.0
        if abs(dim - self.dim) > 1.0 / 128.0:
            self.dim = dim
            for win, *_ in self.displays:
                win.setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.0, dim))
        R = _BASE_RADIUS_PT * float(size)
        for _win, stim, dx, dy, w, h in self.displays:
            on_this = bool(flags & STAGE_TARGET) and dx <= x < dx + w and dy <= y < dy + h and R > 1.2
            if not on_this:
                if not stim.isHidden():
                    stim.setHidden_(True)
                continue
            side = 2.0 * (R * _REACH + 6.0)
            lx, ly = x - dx, y - dy
            # Content views are not flipped: Cocoa y grows upward from the bottom.
            stim.setFrame_(NSMakeRect(lx - side / 2.0, (h - ly) - side / 2.0, side, side))
            stim.radius, stim.phase, stim.burst = R, float(phase), float(burst)
            stim.paused = bool(flags & STAGE_PAUSED)
            stim.setHidden_(False)
            stim.setNeedsDisplay_(True)


def main(argv: list[str]) -> int:
    shm_name = argv[1] if len(argv) > 1 else "keyless_stage_v1"
    parent = int(argv[2]) if len(argv) > 2 else os.getppid()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    try:
        stage = Stage.alloc().initWithShm_parent_(shm_name, parent)
    except FileNotFoundError:
        print(f"no stage shm {shm_name}", file=sys.stderr, flush=True)
        return 3
    if stage.shm.size < STAGE_SHM_SIZE:
        print("stage shm too small", file=sys.stderr, flush=True)
        return 3
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0 / 120.0, stage, "tick:", None, True
    )
    print("ready", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
