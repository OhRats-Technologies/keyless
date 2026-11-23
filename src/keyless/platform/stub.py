"""Honest unsupported-platform stubs shared by the Windows and macOS seams.

These classes exist so capability checks, onboarding, and settings can report a
real status without pretending the OS integration exists.
"""

from __future__ import annotations

from typing import Any

from keyless.platform.base import (
    CapabilityInfo,
    CapabilityStatus,
    NotSupportedError,
    PermissionInfo,
    PermissionState,
)
from keyless.platform.capabilities import Capability, CapabilityReport


def _fail(os_name: str, feature: str) -> NotSupportedError:
    return NotSupportedError(f"{feature} is not implemented for {os_name} in this build.")


class UnsupportedPointer:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def move_absolute(self, x: float, y: float) -> None:
        raise _fail(self.os_name, "Pointer movement")

    def move_relative(self, dx: float, dy: float) -> None:
        raise _fail(self.os_name, "Pointer movement")

    def click(self, button: int = 1) -> None:
        raise _fail(self.os_name, "Pointer click")

    def double_click(self, button: int = 1) -> None:
        raise _fail(self.os_name, "Pointer click")

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        raise _fail(self.os_name, "Pointer scroll")

    def key_combo(self, *keys: str) -> None:
        raise _fail(self.os_name, "Keyboard injection")

    def type_text(self, text: str) -> None:
        raise _fail(self.os_name, "Text injection")

    def close(self) -> None:
        return None


class UnsupportedHotkeySource:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def start(self, on_event: Any = None) -> int:
        raise _fail(self.os_name, "Global hotkeys")

    def stop(self) -> None:
        return None


class UnsupportedCameraSource:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def list_devices(self) -> list[Any]:
        raise _fail(self.os_name, "Camera discovery")


class UnsupportedScreenSource:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def permission_state(self) -> PermissionState:
        return PermissionState.UNKNOWN

    def capture_rect(self, x: int, y: int, width: int, height: int) -> Any:
        raise _fail(self.os_name, "Screen capture")

    def capture_full_desktop(self) -> Any:
        raise _fail(self.os_name, "Screen capture")


class UnsupportedAccessibilityBridge:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def available(self) -> bool:
        return False

    def scan_active_window(self, max_nodes: int = 160) -> list[Any]:
        raise _fail(self.os_name, "Accessibility")

    def invoke(self, node_id: str, action: str = "click") -> bool:
        raise _fail(self.os_name, "Accessibility")


class UnsupportedPermissionService:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def check(self, need: str) -> PermissionInfo:
        return PermissionInfo(
            name=need,
            state=PermissionState.UNKNOWN,
            detail=f"{need} permission is not implemented for {self.os_name} in this build.",
            action="Use the Linux build for a supported host.",
        )


class UnsupportedTray:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def start(self) -> None:
        raise _fail(self.os_name, "Tray")

    def set_state(self, state: str, detail: str = "") -> None:
        raise _fail(self.os_name, "Tray")

    def stop(self) -> None:
        return None


class UnsupportedAutostart:
    def __init__(self, os_name: str) -> None:
        self.os_name = os_name

    def enabled(self) -> bool:
        return False

    def enable(self) -> None:
        raise _fail(self.os_name, "Autostart")

    def disable(self) -> None:
        raise _fail(self.os_name, "Autostart")


def unsupported_capabilities(os_name: str) -> CapabilityReport:
    report = CapabilityReport(platform=os_name)
    detail = f"{os_name} support is a capability stub in this Linux-first build."
    for capability in Capability:
        report.set(
            CapabilityInfo(
                name=capability.value,
                status=CapabilityStatus.UNSUPPORTED_PLATFORM,
                detail=detail,
            )
        )
    report.permissions = {
        "camera": PermissionInfo("camera", PermissionState.UNKNOWN, detail),
        "microphone": PermissionInfo("microphone", PermissionState.UNKNOWN, detail),
        "screen_capture": PermissionInfo("screen_capture", PermissionState.UNKNOWN, detail),
        "input_injection": PermissionInfo("input_injection", PermissionState.UNKNOWN, detail),
    }
    return report
