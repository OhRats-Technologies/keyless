"""Tests for the canonical display topology and honest per-OS router."""

from __future__ import annotations

import pytest

from keyless.platform import router
from keyless.platform.base import (
    DisplayLayout,
    DisplayTopology,
    MonitorInfo,
    NotSupportedError,
    normalize_display_topology,
    topologies_from_layout,
)


def _monitor(
    connector: str = "HDMI-1",
    *,
    x: int = 0,
    y: int = 0,
    width: int = 1920,
    height: int = 1080,
    scale: float = 1.0,
    primary: bool = True,
) -> MonitorInfo:
    return MonitorInfo(
        connector=connector,
        x=x,
        y=y,
        width=width,
        height=height,
        physical_width=width,
        physical_height=height,
        scale=scale,
        is_primary=primary,
    )


def test_normalization_divides_by_scale() -> None:
    topology = normalize_display_topology(_monitor(x=100, y=50, scale=2.0))
    assert isinstance(topology, DisplayTopology)
    assert topology.display_id == "HDMI-1"
    assert topology.global_rect == (100, 50, 1920, 1080)
    assert topology.native_rect == (50, 25, 960, 540)
    assert topology.work_area == topology.global_rect
    assert topology.scale == 2.0
    assert topology.rotation == 0.0
    assert topology.primary is True


def test_work_area_clamps_to_global_rect() -> None:
    topology = normalize_display_topology(
        _monitor(x=100, y=50),
        work_area=(-50, 0, 500, 2000),
    )
    assert topology.work_area == (100, 50, 350, 1080)


def test_work_area_inside_is_kept() -> None:
    topology = normalize_display_topology(
        _monitor(x=100, y=50),
        work_area=(120, 80, 800, 600),
    )
    assert topology.work_area == (120, 80, 800, 600)


def test_display_id_and_primary_can_be_overridden() -> None:
    topology = normalize_display_topology(
        _monitor(primary=False), display_id="builtin", rotation=90.0
    )
    assert topology.display_id == "builtin"
    assert topology.primary is False
    assert topology.rotation == 90.0


def test_invalid_scale_or_size_raises() -> None:
    with pytest.raises(ValueError):
        normalize_display_topology(_monitor(scale=0))
    with pytest.raises(ValueError):
        normalize_display_topology(_monitor(scale=-2.0))
    with pytest.raises(ValueError):
        normalize_display_topology(_monitor(width=0))
    with pytest.raises(ValueError):
        normalize_display_topology(_monitor(height=0))


def test_topologies_from_layout_maps_every_monitor() -> None:
    layout = DisplayLayout(
        monitors=(
            _monitor("eDP-1", width=1440, height=900, scale=2.0),
            _monitor("DP-1", x=1440, width=2560, height=1440, primary=False),
        ),
        total_width=4000,
        total_height=1440,
    )
    topologies = topologies_from_layout(layout)
    assert [item.display_id for item in topologies] == ["eDP-1", "DP-1"]
    assert topologies[0].native_rect == (0, 0, 720, 450)
    assert topologies[1].global_rect == (1440, 0, 2560, 1440)
    assert [item.primary for item in topologies] == [True, False]


# --- router honesty -------------------------------------------------------


def test_windows_raises_for_display_and_cameras() -> None:
    with pytest.raises(NotSupportedError, match="display layout"):
        router.get_display_layout(platform_name="win32")
    with pytest.raises(NotSupportedError, match="camera enumeration"):
        router.list_cameras(platform_name="win32")


def test_windows_raises_for_resolve_and_hit_test() -> None:
    with pytest.raises(NotSupportedError, match="camera configuration"):
        router.resolve_camera_config("auto", "a", "b", platform_name="win32")
    with pytest.raises(NotSupportedError, match="monitor hit testing"):
        router.monitor_at_point(None, 0, 0, platform_name="win32")


def test_windows_raises_for_pointer_backend() -> None:
    with pytest.raises(NotSupportedError, match="pointer control"):
        router.create_pointer_backend(1920, 1080, platform_name="win32")
    with pytest.raises(NotSupportedError, match="pointer control"):
        router._native_pointer_backend("windows", 1920, 1080)


def test_unknown_host_raises() -> None:
    with pytest.raises(NotSupportedError):
        router.get_display_layout(platform_name="sunos")


def test_linux_dispatch_still_resolves(monkeypatch) -> None:
    import keyless.platform.linux.cameras as linux_cameras
    import keyless.platform.linux.display as linux_display
    import keyless.platform.linux.screen as linux_screen

    sentinel = object()
    monkeypatch.setattr(linux_display, "get_display_layout", lambda: sentinel)
    monkeypatch.setattr(linux_cameras, "list_cameras", lambda: ["cam"])
    monkeypatch.setattr(linux_cameras, "resolve_camera_config", lambda *a, **k: ("mono", "a", ""))
    monkeypatch.setattr(linux_screen, "monitor_at_point", lambda layout, x, y: "monitor")

    assert router.get_display_layout(platform_name="linux") is sentinel
    assert router.list_cameras(platform_name="linux") == ["cam"]
    assert router.resolve_camera_config("auto", "a", "b", platform_name="linux") == ("mono", "a", "")
    assert router.monitor_at_point(None, 1, 2, platform_name="linux") == "monitor"


def test_macos_dispatch_still_resolves(monkeypatch) -> None:
    import keyless.platform.macos.cameras as macos_cameras
    import keyless.platform.macos.display as macos_display
    import keyless.platform.macos.screen as macos_screen

    monkeypatch.setattr(macos_display, "get_display_layout", lambda: "mac-layout")
    monkeypatch.setattr(macos_cameras, "list_cameras", lambda: ["mac-cam"])
    monkeypatch.setattr(macos_cameras, "resolve_camera_config", lambda *a, **k: ("stereo", "a", "b"))
    monkeypatch.setattr(macos_screen, "monitor_at_point", lambda layout, x, y: "mac-monitor")

    assert router.get_display_layout(platform_name="darwin") == "mac-layout"
    assert router.list_cameras(platform_name="darwin") == ["mac-cam"]
    assert router.resolve_camera_config("auto", "a", "b", platform_name="darwin") == ("stereo", "a", "b")
    assert router.monitor_at_point(None, 1, 2, platform_name="darwin") == "mac-monitor"
