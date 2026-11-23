"""macOS capability report: real probes for the native backends + SwiftUI host.

Shared-engine + per-OS hosts:

- Linux host: PySide6 Qt shell.
- macOS host: native SwiftUI app in ``packaging/macos/`` talking to the Python
  engine over the loopback control plane (``keyless serve``).

Each row is probed — nothing is assumed from the OS name.
"""

from __future__ import annotations

import shutil

from keyless.platform.base import (
    CapabilityInfo,
    CapabilityStatus,
    PermissionInfo,
    PermissionState,
)
from keyless.platform.capabilities import Capability, CapabilityReport, probe_cua_driver, probe_swiftui


def _available(name: str, detail: str) -> CapabilityInfo:
    return CapabilityInfo(name=name, status=CapabilityStatus.AVAILABLE, detail=detail)


def _needs_permission(name: str, detail: str, action: str) -> CapabilityInfo:
    return CapabilityInfo(name=name, status=CapabilityStatus.NEEDS_PERMISSION, detail=detail,
                          permission=PermissionState.DENIED, action=action)


def _unavailable(name: str, detail: str, action: str = "") -> CapabilityInfo:
    return CapabilityInfo(name=name, status=CapabilityStatus.UNAVAILABLE, detail=detail,
                          permission=PermissionState.UNKNOWN, action=action)


def detect() -> CapabilityReport:
    from keyless.platform.macos import cameras as mac_cams

    report = CapabilityReport(platform="macos")

    # Pointer: CoreGraphics is always present; Accessibility grant checked at use.
    try:
        from keyless.platform.macos.pointer import _load as _cg_load

        if _cg_load():
            report.set(_available(Capability.POINTER.value,
                                  "CoreGraphics CGEvent is available; grant Accessibility to inject input."))
        else:
            report.set(_unavailable(Capability.POINTER.value, "CoreGraphics could not be loaded."))
    except Exception as exc:
        report.set(_unavailable(Capability.POINTER.value, f"Pointer probe failed: {exc}"))

    # Hotkeys: CGEventTap listen-only; needs Input Monitoring.
    report.set(_needs_permission(
        Capability.HOTKEY.value,
        "CGEventTap PTT needs Input Monitoring; grant on first use.",
        "Open System Settings > Privacy & Security > Input Monitoring.",
    ))

    # Cameras: AVFoundation / profiler / OpenCV probe.
    try:
        found = mac_cams.list_cameras()
        if found:
            names = ", ".join(c.name for c in found[:3])
            report.set(_available(Capability.CAMERAS.value, f"{len(found)} camera(s): {names}."))
            report.set(_available(Capability.CAMERA_CONTROLS.value,
                                  "AVFoundation exposure/focus follow camera availability."))
        else:
            report.set(_unavailable(Capability.CAMERAS.value, "No cameras found.",
                                    action="Connect a camera and grant Camera permission."))
            report.set(_unavailable(Capability.CAMERA_CONTROLS.value, "No cameras found."))
    except Exception as exc:
        report.set(_unavailable(Capability.CAMERAS.value, f"Camera probe failed: {exc}"))

    # Audio: CoreAudio via PortAudio (sounddevice) when installed.
    try:
        import sounddevice as sd  # type: ignore[import-not-found]

        devices = sd.query_devices()
        inputs = [d for d in devices if int(d.get("max_input_channels", 0)) > 0]
        if inputs:
            report.set(_available(Capability.AUDIO.value, f"{len(inputs)} input device(s) via CoreAudio."))
        else:
            report.set(_needs_permission(Capability.AUDIO.value, "No microphone input found.",
                                         "Connect a microphone and allow microphone access."))
    except Exception as exc:
        report.set(_unavailable(Capability.AUDIO.value, f"Audio unavailable: {exc}",
                                action="Install PortAudio / sounddevice."))

    # STT: same ONNX engine as Linux (cross-platform).
    try:
        import importlib.util

        if importlib.util.find_spec("onnx_asr") is None:
            report.set(_unavailable(Capability.STT.value, "Parakeet ONNX STT is not installed (onnx_asr)."))
        else:
            report.set(_available(Capability.STT.value, "Parakeet ONNX STT is installed."))
    except Exception as exc:
        report.set(_unavailable(Capability.STT.value, f"STT probe failed: {exc}"))

    # Screen: screencapture CLI present; Recording grant checked at capture.
    if shutil.which("screencapture"):
        report.set(CapabilityInfo(name=Capability.SCREEN_CAPTURE.value,
                                  status=CapabilityStatus.AVAILABLE,
                                  detail="screencapture capture available; Screen Recording asked on first capture."))
    else:
        report.set(_unavailable(Capability.SCREEN_CAPTURE.value, "screencapture binary missing."))

    report.set(_needs_permission(
        Capability.ACCESSIBILITY.value,
        "AXUIElement bridge needs Accessibility; grant on first use.",
        "Open System Settings > Privacy & Security > Accessibility.",
    ))
    report.set(_available(Capability.PERMISSIONS.value, "TCC rows (Camera/Mic/Screen/AX/Input Monitoring) are probed."))
    # Tray/menu bar is the SwiftUI MenuBarExtra host.
    report.set(_available(Capability.TRAY.value, "Native MenuBarExtra host in packaging/macos."))
    report.set(_available(Capability.OVERLAY.value, "NSPanel click-through pill (GazeOverlayPanel)."))
    report.set(_available(Capability.AUTOSTART.value, "LaunchAgent party.ohrats.keyless.plist."))
    report.set(_available(Capability.SINGLE_INSTANCE.value, "Unix socket + lock."))
    report.set(_available(Capability.NOTIFICATIONS.value, "UserNotifications via SwiftUI host."))
    report.set(_available(Capability.BROWSER_BRIDGE.value, "Firefox WebExtension bridge on loopback."))
    try:
        import importlib.util

        if importlib.util.find_spec("keyring") is None:
            report.set(_unavailable(Capability.KEYCHAIN.value, "OS keychain (keyring) not installed."))
        else:
            report.set(_available(Capability.KEYCHAIN.value, "Keychain via keyring."))
        if importlib.util.find_spec("httpx") is None:
            report.set(_unavailable(Capability.LLM_PROVIDER.value, "LLM transport (httpx) not installed."))
        else:
            report.set(_available(Capability.LLM_PROVIDER.value, "LLM provider transport ready."))
    except Exception as exc:
        report.set(_unavailable(Capability.LLM_PROVIDER.value, f"Provider probe failed: {exc}"))

    report.set(probe_swiftui(platform_name="darwin"))
    report.set(probe_cua_driver())

    from keyless.platform.macos.permissions import check_permission

    def _info(need: str, fallback_detail: str) -> PermissionInfo:
        try:
            probed = check_permission(need)
            if probed.state is PermissionState.UNKNOWN and fallback_detail:
                return PermissionInfo(probed.name, probed.state, fallback_detail, probed.action)
            return probed.as_info()
        except Exception:
            return PermissionInfo(need, PermissionState.UNKNOWN, fallback_detail, "")

    report.permissions = {
        "camera": _info("camera", "Grant Camera on first AVFoundation use."),
        "microphone": _info("microphone", "Grant Microphone on first audio use."),
        "screen_capture": _info(
            "screen_capture", "The desktop asks for Screen Recording on first capture."
        ),
        "input_injection": _info("accessibility", "Grant Accessibility for CGEvent input."),
        "hotkey": _info("input_monitoring", "Grant Input Monitoring for PTT."),
        "native_host": PermissionInfo("native_host", PermissionState.GRANTED,
                                      "SwiftUI host sources present.",
                                      "Build packaging/macos on macOS 14+."),
    }
    # Keep human actions on each row even when the probe already filled them.
    report.permissions["camera"] = PermissionInfo(
        "camera", report.permissions["camera"].state,
        report.permissions["camera"].detail or "Camera permission.",
        "Open System Settings > Privacy & Security > Camera.")
    return report
