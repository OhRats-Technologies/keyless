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
