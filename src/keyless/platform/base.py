"""Shared platform contracts.

OS packages under ``keyless.platform.<os>`` implement these protocols.
The engine imports only from this module; no OS module is imported by shared code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from keyless.config.paths import AppPaths


class NotSupportedError(RuntimeError):
    """Raised by a platform stub whose OS capability is not implemented yet."""


class PermissionState(str, Enum):
    GRANTED = "granted"
    DENIED = "denied"
    UNKNOWN = "unknown"
    NEEDS_RESTART = "needs_restart"
    NOT_APPLICABLE = "not_applicable"


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    NEEDS_PERMISSION = "needs_permission"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED_PLATFORM = "unsupported_platform"


@dataclass(frozen=True)
class PermissionInfo:
    """One user-facing permission state with a single next action."""

    name: str
    state: PermissionState
    detail: str = ""
    action: str = ""

    @property
    def granted(self) -> bool:
        return self.state is PermissionState.GRANTED


@dataclass(frozen=True)
class CapabilityInfo:
    """One capability row used by onboarding, settings, `doctor`, and the UI."""

    name: str
    status: CapabilityStatus
    detail: str = ""
    permission: PermissionState | None = None
    action: str = ""

    @property
    def available(self) -> bool:
        return self.status is CapabilityStatus.AVAILABLE


@dataclass(frozen=True)
class MonitorInfo:
    """Single logical monitor in the desktop layout."""

    connector: str
    x: int
    y: int
    width: int
    height: int
    physical_width: int
    physical_height: int
    scale: float
    is_primary: bool
    width_mm: float = 0.0
    height_mm: float = 0.0


@dataclass(frozen=True)
class DisplayLayout:
    """Combined multi-monitor desktop bounding box and active monitors."""

    monitors: tuple[MonitorInfo, ...]
    total_width: int
    total_height: int
    layout_mode: int = 1

    @property
    def primary(self) -> MonitorInfo:
        for mon in self.monitors:
            if mon.is_primary:
                return mon
        return self.monitors[0]


@dataclass(frozen=True)
class DisplayTopology:
    """One display in global top-left-origin pixels plus its OS-native geometry.

    ``global_rect`` is what pointer and overlay math uses. ``native_rect`` keeps
    the OS-native units (for example macOS points) so a shell can draw a window
    on the right screen without re-deriving scale. ``work_area`` is the part of
    the display not covered by menu bars or docks, clamped to ``global_rect``.
    """

    display_id: str
    global_rect: tuple[int, int, int, int]
    native_rect: tuple[int, int, int, int]
    work_area: tuple[int, int, int, int]
    scale: float
    rotation: float = 0.0
    primary: bool = False


def normalize_display_topology(
    monitor: MonitorInfo,
    display_id: str | None = None,
    work_area: tuple[int, int, int, int] | None = None,
    rotation: float = 0.0,
) -> DisplayTopology:
    """Validate one monitor and derive its canonical pixel/native geometry."""
    if monitor.scale <= 0:
        raise ValueError(f"display {monitor.connector}: scale must be > 0")
    if monitor.width <= 0 or monitor.height <= 0:
        raise ValueError(f"display {monitor.connector}: width and height must be > 0")

    global_rect = (monitor.x, monitor.y, monitor.width, monitor.height)
    native_rect = (
        round(monitor.x / monitor.scale),
        round(monitor.y / monitor.scale),
        round(monitor.width / monitor.scale),
        round(monitor.height / monitor.scale),
    )
    clamped_area = _clamp_work_area(global_rect, work_area) if work_area is not None else global_rect
    return DisplayTopology(
        display_id=display_id or monitor.connector,
        global_rect=global_rect,
        native_rect=native_rect,
        work_area=clamped_area,
        scale=monitor.scale,
        rotation=rotation,
        primary=monitor.is_primary,
    )


def _clamp_work_area(
    global_rect: tuple[int, int, int, int],
    work_area: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    gx, gy, gw, gh = global_rect
    ax, ay, aw, ah = work_area
    left = max(gx, ax)
    top = max(gy, ay)
    right = min(gx + gw, ax + aw)
    bottom = min(gy + gh, ay + ah)
    if right <= left or bottom <= top:
        raise ValueError("work area does not intersect the display")
    return (left, top, right - left, bottom - top)


def topologies_from_layout(layout: DisplayLayout) -> tuple[DisplayTopology, ...]:
    """Map every monitor in a layout to its canonical topology."""
    return tuple(normalize_display_topology(monitor) for monitor in layout.monitors)


@dataclass(frozen=True)
class CameraInfo:
    """Stable camera identity (D5); never an enumeration index or /dev/videoN."""

    id: str
    name: str
    path: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HotkeyEvent:
    key: str
    pressed: bool


@runtime_checkable
class PointerBackend(Protocol):
    def move_absolute(self, x: float, y: float) -> None: ...
    def move_relative(self, dx: float, dy: float) -> None: ...
    def click(self, button: int = 1) -> None: ...
    def double_click(self, button: int = 1) -> None: ...
    def scroll(self, dy: int = 0, dx: int = 0) -> None: ...
    def key_combo(self, *keys: str) -> None: ...
    def type_text(self, text: str) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class HotkeySource(Protocol):
    def start(self, on_event: Any) -> int: ...
    def stop(self) -> None: ...


@runtime_checkable
class CameraSource(Protocol):
    def list_devices(self) -> list[CameraInfo]: ...


@runtime_checkable
class CameraControls(Protocol):
    def set_exposure(self, device: CameraInfo, value: int) -> bool: ...
    def set_gain(self, device: CameraInfo, value: int) -> bool: ...


@runtime_checkable
class AudioSource(Protocol):
    def list_inputs(self) -> list[dict[str, Any]]: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class ScreenSource(Protocol):
    def permission_state(self) -> PermissionState: ...
    def capture_rect(self, x: int, y: int, width: int, height: int) -> Any: ...
    def capture_full_desktop(self) -> Any: ...


@runtime_checkable
class AccessibilityBridge(Protocol):
    def available(self) -> bool: ...
    def scan_active_window(self, max_nodes: int = 160) -> list[Any]: ...
    def invoke(self, node_id: str, action: str = "click") -> bool: ...


@runtime_checkable
class PermissionService(Protocol):
    def check(self, need: str) -> PermissionInfo: ...


@runtime_checkable
class Tray(Protocol):
    def start(self) -> None: ...
    def set_state(self, state: str, detail: str = "") -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class Autostart(Protocol):
    def enabled(self) -> bool: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...


@runtime_checkable
class SingleInstance(Protocol):
    def acquire(self) -> bool: ...
    def release(self) -> None: ...


@dataclass(frozen=True)
class PlatformContext:
    """Everything an OS package needs from the shared app."""

    paths: AppPaths
    env: dict[str, str]
