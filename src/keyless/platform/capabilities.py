"""One capability report for onboarding, the tray, settings, and `doctor`.

No capability is assumed from the OS name alone: each row comes from an explicit
probe, and the same values feed the CLI and the UI.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from keyless.platform.base import (
    CapabilityInfo,
    CapabilityStatus,
    PermissionInfo,
    PermissionState,
)


class Capability(str, Enum):
    POINTER = "pointer"
    HOTKEY = "hotkey"
    CAMERAS = "cameras"
    CAMERA_CONTROLS = "camera_controls"
    AUDIO = "audio"
    STT = "stt"
    SCREEN_CAPTURE = "screen_capture"
    ACCESSIBILITY = "accessibility"
    PERMISSIONS = "permissions"
    TRAY = "tray"
    AUTOSTART = "autostart"
    OVERLAY = "overlay"
    SINGLE_INSTANCE = "single_instance"
    NOTIFICATIONS = "notifications"
    BROWSER_BRIDGE = "browser_bridge"
    KEYCHAIN = "keychain"
    LLM_PROVIDER = "llm_provider"
    SWIFTUI = "swiftui"
    CUA_DRIVER = "cua_driver"


@dataclass
class CapabilityReport:
    """Named capability rows plus the permission rows that explain them."""

    platform: str
    items: dict[str, CapabilityInfo] = field(default_factory=dict)
    permissions: dict[str, PermissionInfo] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)

    def get(self, capability: Capability | str) -> CapabilityInfo | None:
        key = capability.value if isinstance(capability, Capability) else capability
        return self.items.get(key)

    def status(self, capability: Capability | str) -> CapabilityStatus:
        info = self.get(capability)
        return info.status if info is not None else CapabilityStatus.UNAVAILABLE

    def set(self, info: CapabilityInfo) -> None:
        self.items[info.name] = info

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "paths": dict(self.paths),
            "capabilities": {
                name: {
                    "status": info.status.value,
                    "detail": info.detail,
                    "permission": info.permission.value if info.permission else None,
                    "action": info.action,
                }
                for name, info in self.items.items()
            },
            "permissions": {
                name: {
                    "state": info.state.value,
                    "detail": info.detail,
                    "action": info.action,
                }
                for name, info in self.permissions.items()
            },
        }


Probe = Callable[[], CapabilityInfo]


def _ok(name: str, detail: str = "") -> CapabilityInfo:
    return CapabilityInfo(name=name, status=CapabilityStatus.AVAILABLE, detail=detail)


def _bad(
    name: str,
    detail: str,
    *,
    permission: PermissionState | None = None,
    action: str = "",
) -> CapabilityInfo:
    status = (
        CapabilityStatus.NEEDS_PERMISSION
        if permission in (PermissionState.DENIED, PermissionState.NEEDS_RESTART)
        else CapabilityStatus.UNAVAILABLE
    )
    return CapabilityInfo(
        name=name,
        status=status,
        detail=detail,
        permission=permission,
        action=action,
    )


def _readable_input_devices() -> list[str]:
    return [p for p in sorted(Path("/dev/input").glob("event*")) if os.access(p, os.R_OK)]


def probe_pointer(device: Path = Path("/dev/uinput")) -> CapabilityInfo:
    if not device.exists():
        return _bad(
            Capability.POINTER.value,
            "/dev/uinput is missing; synthetic pointer input is unavailable.",
            permission=PermissionState.DENIED,
            action="Install the udev rule or load the uinput module.",
        )
    if not os.access(device, os.W_OK):
        return _bad(
            Capability.POINTER.value,
            "/dev/uinput exists but the current user cannot write to it.",
            permission=PermissionState.DENIED,
            action="Run the uinput setup script or install the packaged udev rule.",
        )
    return _ok(Capability.POINTER.value, "/dev/uinput is writable.")


def probe_hotkeys() -> CapabilityInfo:
    readable = _readable_input_devices()
    if not readable:
        return _bad(
            Capability.HOTKEY.value,
            "No /dev/input/event* device is readable; push-to-talk cannot see keys.",
            permission=PermissionState.DENIED,
            action="Add the user to the input group, then sign in again.",
        )
    return _ok(Capability.HOTKEY.value, f"{len(readable)} readable input device(s).")


def probe_cameras(root: Path = Path("/dev/v4l/by-id")) -> CapabilityInfo:
    by_id = sorted(root.glob("*-video-index0")) if root.exists() else []
    if not by_id:
        videos = sorted(Path("/dev").glob("video*"))
        if videos:
            return CapabilityInfo(
                name=Capability.CAMERAS.value,
                status=CapabilityStatus.DEGRADED,
                detail=(
                    f"{len(videos)} camera node(s), but no stable /dev/v4l/by-id identity; "
                    "choose cameras explicitly."
                ),
            )
        return _bad(Capability.CAMERAS.value, "No camera devices found.")
    if len(by_id) < 2:
        return CapabilityInfo(
            name=Capability.CAMERAS.value,
            status=CapabilityStatus.AVAILABLE,
            detail=f"One stable camera found ({by_id[0].name}); mono gaze is available, stereo needs two.",
        )
    return _ok(Capability.CAMERAS.value, f"{len(by_id)} stable cameras found.")


def probe_audio(sounddevice_module: object | None = None) -> CapabilityInfo:
    try:
        sd = sounddevice_module
        if sd is None:
            import sounddevice as sd  # type: ignore[no-redef]
        devices = sd.query_devices()  # type: ignore[attr-defined]
    except Exception as exc:  # sounddevice raises PortAudioError et al.
        return _bad(
            Capability.AUDIO.value,
            f"Audio devices are unavailable: {exc}",
            action="Install PortAudio/PipeWire support.",
        )
    inputs = [d for d in devices if int(d.get("max_input_channels", 0)) > 0]
    if not inputs:
        return _bad(
            Capability.AUDIO.value,
            "No microphone input was found.",
            permission=PermissionState.DENIED,
            action="Connect a microphone and allow microphone access.",
        )
    names = ", ".join(str(d.get("name", "capture")) for d in inputs[:3])
    return _ok(Capability.AUDIO.value, f"{len(inputs)} input device(s): {names}.")


def probe_screen(env: Mapping[str, str] | None = None, *, has_gdbus: bool | None = None) -> CapabilityInfo:
    environ = os.environ if env is None else env
    session = environ.get("XDG_SESSION_TYPE", "unknown")
    gdbus = shutil.which("gdbus") is not None if has_gdbus is None else has_gdbus
    if not gdbus:
        return _bad(
            Capability.SCREEN_CAPTURE.value,
            "gdbus is missing; GNOME Shell and portal screen capture are unavailable.",
            action="Install glib2/gdbus.",
        )
    detail = f"{session} session; permission is checked on the first capture."
    return _ok(Capability.SCREEN_CAPTURE.value, detail)


def probe_accessibility(python_bin: str = "/usr/bin/python3") -> CapabilityInfo:
    if not Path(python_bin).exists():
        return _bad(
            Capability.ACCESSIBILITY.value,
            f"{python_bin} is missing; the AT-SPI bridge cannot start.",
            action="Install python3-gi and gir1.2-atspi-2.0.",
        )
    # Importing gi in the app interpreter is not required: the bridge runs in
    # /usr/bin/python3. Only report that the bridge runtime is present.
    return _ok(Capability.ACCESSIBILITY.value, f"AT-SPI bridge runtime: {python_bin}.")


def probe_module(module: str, capability: Capability, label: str) -> CapabilityInfo:
    if importlib.util.find_spec(module) is None:
        return _bad(capability.value, f"{label} is not installed ({module}).")
    return _ok(capability.value, f"{label} is installed.")


def probe_swiftui(
    *,
    platform_name: str | None = None,
    swiftc: str | None = None,
    host_sources: Path | None = None,
) -> CapabilityInfo:
    """Probe the native macOS SwiftUI host seam (Capability.SWIFTUI).

    The shared Python engine stays cross-platform; on macOS the preferred UI
    is the native SwiftUI host in ``packaging/macos/`` talking to the engine
    over the loopback control plane (``keyless serve``). This probe reports
    whether that host can be built here — it never pretends the engine's
    macOS input/camera/screen backends exist (those stay honest stubs until
    CGEvent/AVFoundation/ScreenCaptureKit land).
    """
    name = platform_name if platform_name is not None else sys.platform
    host = "macos" if name == "darwin" else name
    sources = host_sources or (Path(__file__).resolve().parents[3] / "packaging" / "macos")
    has_sources = (sources / "Keyless" / "KeylessApp.swift").exists() or (
        sources / "KeylessApp.swift"
    ).exists()
    if host not in ("macos", "darwin"):
        return CapabilityInfo(
            name=Capability.SWIFTUI.value,
            status=CapabilityStatus.UNAVAILABLE,
            detail=(
                "SwiftUI host runs on macOS only; "
                + (
                    "native host sources are present, build them on a Mac."
                    if has_sources
                    else "native host sources are not checked out here."
                )
            ),
            action="Build packaging/macos on macOS with Xcode 15+.",
        )
    swift_bin = swiftc or shutil.which("swiftc") or shutil.which("swift")
    if swift_bin is None:
        return _bad(
            Capability.SWIFTUI.value,
            "Swift toolchain not found; install Xcode 15+ command line tools to build the native host.",
            action="Install Xcode, then build packaging/macos.",
        )
    if not has_sources:
        return _bad(
            Capability.SWIFTUI.value,
            f"Swift {swift_bin} found but packaging/macos sources are missing.",
            action="Check out packaging/macos/KeylessApp sources.",
        )
    return _ok(
        Capability.SWIFTUI.value,
        f"Native SwiftUI host sources present; Swift toolchain at {swift_bin}.",
    )


def probe_cua_driver() -> CapabilityInfo:
    """Probe the pinned cua-driver runtime (no process is spawned).

    Available when ``keyless models download cua-driver`` has fetched and
    verified the pinned binary; otherwise unavailable with the download as
    the one action. Upgrading the driver is a version + hash bump in
    ``keyless.models.runtimes``, never platform-code maintenance.
    """
    try:
        from keyless.models import runtimes

        info = runtimes.status()
    except Exception as exc:
        return _bad(Capability.CUA_DRIVER.value, f"cua-driver probe failed: {exc}")
    if info.get("ready"):
        return _ok(
            Capability.CUA_DRIVER.value,
            f"cua-driver {info.get('version')} verified ({info.get('target')}).",
        )
    return CapabilityInfo(
        name=Capability.CUA_DRIVER.value,
        status=CapabilityStatus.UNAVAILABLE,
        detail="Pinned cua-driver binary not downloaded; discrete actions stay native.",
        action="Run `keyless models download cua-driver`.",
    )


def probe_permissions(
    pointer: CapabilityInfo,
    hotkeys: CapabilityInfo,
    audio: CapabilityInfo,
    screen: CapabilityInfo,
) -> dict[str, PermissionInfo]:
    def state(info: CapabilityInfo) -> PermissionState:
        if info.status is CapabilityStatus.AVAILABLE:
            return PermissionState.GRANTED
        if info.status is CapabilityStatus.DEGRADED:
            return PermissionState.UNKNOWN
        return info.permission or PermissionState.UNKNOWN

    return {
        "input_injection": PermissionInfo(
            name="input_injection",
            state=state(pointer),
            detail=pointer.detail,
            action=pointer.action,
        ),
        "hotkey": PermissionInfo(
            name="hotkey",
            state=state(hotkeys),
            detail=hotkeys.detail,
            action=hotkeys.action,
        ),
        "microphone": PermissionInfo(
            name="microphone",
            state=state(audio),
            detail=audio.detail,
            action=audio.action,
        ),
        "screen_capture": PermissionInfo(
            name="screen_capture",
            state=PermissionState.UNKNOWN,
            detail=screen.detail,
            action="The desktop asks for screen access on the first capture.",
        ),
    }


def detect_capabilities(
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    sounddevice_module: object | None = None,
    paths: object | None = None,
) -> CapabilityReport:
    """Probe the host and return the shared capability report."""
    name = platform_name or sys.platform
    host = (
        "linux"
        if name.startswith("linux")
        else "macos"
        if name == "darwin"
        else "windows"
        if name.startswith("win")
        else name
    )
    report = CapabilityReport(platform=host)
    if paths is not None and hasattr(paths, "as_dict"):
        report.paths = paths.as_dict()  # type: ignore[union-attr]

    if host in ("macos", "windows"):
        module = __import__(
            f"keyless.platform.{host}.capabilities",
            fromlist=["detect"],
        )
        delegated = module.detect()
        if paths is not None and hasattr(paths, "as_dict"):
            delegated.paths = paths.as_dict()  # type: ignore[union-attr]
        return delegated

    if host != "linux":
        for capability in Capability:
            report.set(
                CapabilityInfo(
                    name=capability.value,
                    status=CapabilityStatus.UNSUPPORTED_PLATFORM,
                    detail=f"{host} support is a capability stub in this build.",
                )
            )
        return report

    pointer = probe_pointer()
    hotkeys = probe_hotkeys()
    cameras = probe_cameras()
    audio = probe_audio(sounddevice_module)
    screen = probe_screen(env)

    report.set(pointer)
    report.set(hotkeys)
    report.set(cameras)
    report.set(
        CapabilityInfo(
            name=Capability.CAMERA_CONTROLS.value,
            status=cameras.status,
            detail="V4L2 exposure/gain controls follow camera availability.",
        )
    )
    report.set(audio)
    report.set(
        probe_module("onnx_asr", Capability.STT, "Parakeet ONNX STT")
    )
    report.set(screen)
    report.set(probe_accessibility())
    report.set(
        CapabilityInfo(
            name=Capability.PERMISSIONS.value,
            status=CapabilityStatus.AVAILABLE,
            detail="udev, input groups, microphone, and screen capture are probed.",
        )
    )
    report.set(probe_module("PySide6", Capability.TRAY, "PySide6 tray"))
    report.set(probe_module("PySide6", Capability.OVERLAY, "PySide6 overlay"))
    report.set(
        CapabilityInfo(
            name=Capability.AUTOSTART.value,
            status=CapabilityStatus.AVAILABLE,
            detail="XDG autostart and a systemd user unit are packaged on Linux.",
        )
    )
    report.set(
        CapabilityInfo(
            name=Capability.SINGLE_INSTANCE.value,
            status=CapabilityStatus.AVAILABLE,
            detail="Unix socket + lock.",
        )
    )
    report.set(
        CapabilityInfo(
            name=Capability.NOTIFICATIONS.value,
            status=CapabilityStatus.AVAILABLE,
            detail="Desktop notifications through the session bus.",
        )
    )
    report.set(
        CapabilityInfo(
            name=Capability.BROWSER_BRIDGE.value,
            status=CapabilityStatus.AVAILABLE,
            detail="Optional Firefox WebExtension bridge on loopback.",
        )
    )
    report.set(probe_module("keyring", Capability.KEYCHAIN, "OS keychain"))
    report.set(probe_module("httpx", Capability.LLM_PROVIDER, "LLM provider transport"))
    report.set(probe_swiftui(platform_name="linux"))
    report.set(probe_cua_driver())
    report.permissions = probe_permissions(pointer, hotkeys, audio, screen)
    return report
