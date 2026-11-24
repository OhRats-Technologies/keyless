"""macOS display layout via NSScreen (pyobjc) with system_profiler fallback."""

from __future__ import annotations

import json
import subprocess

from keyless.platform.base import DisplayLayout, MonitorInfo


def _layout_from_profiler() -> DisplayLayout | None:
    try:
        res = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=5.0, check=True,
        )
        data = json.loads(res.stdout or "{}")
        monitors: list[MonitorInfo] = []
        x = 0
        for idx, gpu in enumerate(data.get("SPDisplaysDataType", [])):
            for disp in gpu.get("spdisplays_ndrvs", []):
                res_str: str = disp.get("_spdisplays_resolution", "1920 x 1080")
                parts = res_str.replace("x", " ").split()
                nums = [int(p) for p in parts if p.isdigit()]
                w, h = (nums[0], nums[1]) if len(nums) >= 2 else (1920, 1080)
                name = str(disp.get("_name", f"Display {idx}"))
                monitors.append(MonitorInfo(
                    connector=name, x=x, y=0, width=w, height=h,
                    physical_width=w, physical_height=h,
                    scale=2.0 if "retina" in res_str.lower() else 1.0,
                    is_primary=(len(monitors) == 0),
                ))
                x += w
        if monitors:
            return DisplayLayout(monitors=tuple(monitors), total_width=x,
                                 total_height=max(m.height for m in monitors))
    except Exception:
        pass
    return None


def _screen_monitors() -> list[MonitorInfo]:
    """NSScreen geometry converted to CG global coordinates.

    Cocoa frames are bottom-left origin with y up; CGEvent locations, cua and
    everything downstream use top-left of the primary display with y down, in
    logical points. Panel size comes from CGDisplayScreenSize (EDID, mm).
    """
    from AppKit import NSScreen  # type: ignore[import-not-found]

    try:
        from Quartz import CGDisplayScreenSize  # type: ignore[import-not-found]
    except Exception:
        CGDisplayScreenSize = None  # noqa: N806

    screens = list(NSScreen.screens() or [])
    if not screens:
        return []
    primary_h = float(screens[0].frame().size.height)
    monitors: list[MonitorInfo] = []
    seen: dict[str, int] = {}
    for i, screen in enumerate(screens):
        frame = screen.frame()
        scale = float(screen.backingScaleFactor() or 1.0)
        w = int(round(float(frame.size.width)))
        h = int(round(float(frame.size.height)))
        x = int(round(float(frame.origin.x)))
        y = int(round(primary_h - float(frame.origin.y) - float(frame.size.height)))
        width_mm = height_mm = 0.0
        try:
            display_id = int(screen.deviceDescription()["NSScreenNumber"])
            if CGDisplayScreenSize is not None:
                size = CGDisplayScreenSize(display_id)
                width_mm, height_mm = float(size.width), float(size.height)
        except Exception:
            pass
        try:
            name = str(screen.localizedName()) or f"Display-{i}"
        except Exception:
            name = f"Display-{i}"
        seen[name] = seen.get(name, 0) + 1
        connector = name if seen[name] == 1 else f"{name} {seen[name]}"
        monitors.append(MonitorInfo(
            connector=connector, x=x, y=y, width=w, height=h,
            physical_width=int(round(w * scale)), physical_height=int(round(h * scale)),
            scale=scale, is_primary=(i == 0), width_mm=width_mm, height_mm=height_mm,
        ))
    return monitors


def get_display_layout() -> DisplayLayout:
    try:
        monitors = _screen_monitors()
        if monitors:
            return DisplayLayout(
                monitors=tuple(monitors),
                total_width=max(m.x + m.width for m in monitors),
                total_height=max(m.y + m.height for m in monitors),
            )
    except Exception:
        pass
    fallback = _layout_from_profiler()
    if fallback is not None:
        return fallback
    single = MonitorInfo(connector="Display-0", x=0, y=0, width=1920, height=1080,
                         physical_width=1920, physical_height=1080, scale=1.0, is_primary=True)
    return DisplayLayout(monitors=(single,), total_width=1920, total_height=1080)
