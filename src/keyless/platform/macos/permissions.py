"""macOS TCC permission probes, adapted from Hex's approach (anomalyco/hex).

Hex (native/permission_guide.m + src/desktop/) does four things our old stub
did not:

1. Deep-link to the exact Privacy pane
   (``x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_*``)
   and float a small non-activating guide beside System Settings instead of a
   bare "Open Settings" button.
2. Trigger the OS prompt directly where one exists
   (``AXIsProcessTrustedWithOptions`` for Accessibility, ``AVCaptureDevice``
   ``requestAccess`` for Camera/Microphone) rather than sending the user away first.
3. Read TCC fresh via the private ``TCCAccessPreflight`` (dlopen, not linked)
   because ``CGPreflightScreenCaptureAccess`` and AVFoundation cache the answer
   they read at launch — a grant made while Keyless runs would otherwise never
   be seen until restart.
4. Detect stale rows (back from Settings, still untrusted) and offer
   ``tccutil reset <service> <bundle-id>`` + re-prompt, plus one-time cleanup
   of retired bundle IDs so Settings never lists duplicate Keyless rows.

This module ports the probe/reset half to Python (ctypes only, no pyobjc
required). The floating guide + drag tile live in the SwiftUI host
(``packaging/macos/Keyless/PermissionGuide.swift``), which calls the same
pane URLs and preflight semantics.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
from dataclasses import dataclass

from keyless.platform.base import PermissionInfo, PermissionState

# Exact Privacy panes (Hex's mapping). The old
# ``com.apple.preference.security?Privacy_*`` URLs still resolve on some
# releases but land on the top-level pane; these land on the list itself.
PANE_URLS = {
    "camera": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Camera",
    "microphone": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone",
    "screen_capture": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
    "input_monitoring": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent",
    "system_audio": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AudioCapture",
}

LEGACY_MAC_BUNDLE_IDS = (
    "com.ohrats.keyless.debug",
    "party.ohrats.keyless.debug",
)


def _ax_trusted(prompt: bool = False) -> bool | None:
    """AXIsProcessTrusted[_WithOptions]; None when ApplicationServices is missing."""
    try:
        path = ctypes.util.find_library("ApplicationServices") or (
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib = ctypes.CDLL(path)
    except OSError:
        return None
    try:
        if prompt:
            lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
            lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
            cfstr = ctypes.c_void_p(0x1)  # placeholder replaced below
            # Build {kAXTrustedCheckOptionPrompt: True} via CoreFoundation.
            try:
                cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation")
                                 or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
                cf.CFStringCreateWithCString.restype = ctypes.c_void_p
                cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
                cf.CFNumberCreate.restype = ctypes.c_void_p
                cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                cf.CFDictionaryCreate.restype = ctypes.c_void_p
                cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p] * 6
                kAX = cf.CFStringCreateWithCString(None, b"AXTrustedCheckOptionPrompt", 0x08000100)
                true_num = ctypes.c_int(1)
                kCFBooleanTrue = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
                keys = (ctypes.c_void_p * 1)(kAX)
                vals = (ctypes.c_void_p * 1)(kCFBooleanTrue.value)
                opts = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
                result = lib.AXIsProcessTrustedWithOptions(opts)
                return bool(result)
            except Exception:
                # Fall back to the non-prompt check rather than failing.
                cfstr = None
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def _cg_preflight_screen_capture() -> bool | None:
    try:
        path = ctypes.util.find_library("CoreGraphics") or (
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        lib = ctypes.CDLL(path)
        lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(lib.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def _tcc_preflight(service: bytes) -> int | None:
    """Private TCCAccessPreflight via dlopen; 0 granted, 1 denied, 2 not-asked, None unavailable."""
    try:
        tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
        try:
            fn = tcc.TCCAccessPreflight
        except AttributeError:
            return None
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation")
                         or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cfstr = cf.CFStringCreateWithCString(None, service, 0x08000100)
        if not cfstr:
            return None
        return int(fn(cfstr, None))
    except Exception:
        return None


def classify_tccutil_reset(code: int | None, stderr: str) -> str:
    """Mirror Hex's TccResetOutcome as plain strings: cleared|nothing|failed."""
    if code == 0:
        return "cleared"
    if code == 64 or "-10814" in stderr or "No such bundle identifier" in stderr:
        return "nothing"
    return "failed"


def reset_tcc_service(service: str, bundle_id: str) -> str:
    """Run `/usr/bin/tccutil reset <service> <bundle-id>`; returns outcome string."""
    try:
        out = subprocess.run(["/usr/bin/tccutil", "reset", service, bundle_id],
                             capture_output=True, text=True, timeout=15.0, check=False)
        err = out.stderr or ""
        return classify_tccutil_reset(out.returncode, err)
    except Exception as exc:
        return f"failed: {exc}"


@dataclass(frozen=True)
class MacPermissionStatus:
    name: str
    state: PermissionState
    detail: str
    action: str
    pane_url: str = ""

    def as_info(self) -> PermissionInfo:
        return PermissionInfo(name=self.name, state=self.state, detail=self.detail, action=self.action)


def check_permission(need: str) -> MacPermissionStatus:
    """Probe one TCC need without prompting (except Accessibility prompt is opt-in via prompt())."""
    key = need.lower()
    if key in ("accessibility", "input_injection"):
        trusted = _ax_trusted(prompt=False)
        if trusted is True:
            return MacPermissionStatus(key, PermissionState.GRANTED,
                                       "Accessibility is trusted for this process.",
                                       "", PANE_URLS["accessibility"])
        if trusted is None:
            return MacPermissionStatus(key, PermissionState.UNKNOWN,
                                       "Accessibility trust could not be read on this host.",
                                       "Open System Settings > Privacy & Security > Accessibility.",
                                       PANE_URLS["accessibility"])
        return MacPermissionStatus(key, PermissionState.DENIED,
                                   "Not trusted: drag Keyless into the Accessibility list (guide shows how).",
                                   "Allow in System Settings, or use the floating guide.",
                                   PANE_URLS["accessibility"])
    if key in ("microphone", "mic", "audio"):
        pre = _tcc_preflight(b"kTCCServiceMicrophone")
        if pre == 0:
            return MacPermissionStatus(key, PermissionState.GRANTED, "Microphone granted (TCC).",
                                       "", PANE_URLS["microphone"])
        if pre == 1:
            return MacPermissionStatus(key, PermissionState.DENIED,
                                       "Microphone was refused: switch Keyless on in the Microphone list.",
                                       "Switch on in System Settings > Privacy & Security > Microphone.",
                                       PANE_URLS["microphone"])
        # Try AVFoundation's cached answer as a second opinion when pyobjc exists.
        try:
            from AVFoundation import AVCaptureDevice  # type: ignore[import-not-found]

            status = int(AVCaptureDevice.authorizationStatusForMediaType_("audio"))
            if status == 3:  # AVAuthorizationStatusAuthorized
                return MacPermissionStatus(key, PermissionState.GRANTED,
                                           "Microphone authorized (AVFoundation).", "", PANE_URLS["microphone"])
            if status in (1, 2):
                return MacPermissionStatus(key, PermissionState.DENIED,
                                           "Microphone denied/restricted.",
                                           "Switch on in System Settings > Privacy & Security > Microphone.",
                                           PANE_URLS["microphone"])
        except Exception:
            pass
        return MacPermissionStatus(key, PermissionState.UNKNOWN,
                                   "Microphone not asked yet; the OS prompt appears on first use.",
                                   "Allow when prompted, or turn on in System Settings.",
                                   PANE_URLS["microphone"])
    if key in ("camera",):
        pre = _tcc_preflight(b"kTCCServiceCamera")
        if pre == 0:
            return MacPermissionStatus(key, PermissionState.GRANTED, "Camera granted (TCC).",
                                       "", PANE_URLS["camera"])
        if pre == 1:
            return MacPermissionStatus(key, PermissionState.DENIED,
                                       "Camera was refused for this app.",
                                       "Switch on in System Settings > Privacy & Security > Camera.",
                                       PANE_URLS["camera"])
        return MacPermissionStatus(key, PermissionState.UNKNOWN,
                                   "Camera not asked yet; the OS prompt appears on first AVFoundation use.",
                                   "Allow when prompted, or turn on in System Settings.",
                                   PANE_URLS["camera"])
    if key in ("screen_capture", "screen", "screen_recording"):
        pre = _tcc_preflight(b"kTCCServiceScreenCapture")
        cg = _cg_preflight_screen_capture()
        granted = (pre == 0) or (pre is None and cg is True)
        if granted:
            # Fresh helper semantics: if a capture probe was requested and failed
            # despite TCC saying granted, the grant needs a restart (Hex's
            # ScreenAccess::NeedsRestart). Without a probe we report granted.
            return MacPermissionStatus(key, PermissionState.GRANTED,
                                       "Screen Recording granted (TCC). A fresh grant may need a relaunch.",
                                       "", PANE_URLS["screen_capture"])
        if pre == 1 or cg is False:
            return MacPermissionStatus(key, PermissionState.DENIED,
                                       "Screen Recording is off: drag Keyless into the list (guide shows how).",
                                       "Allow in System Settings, then relaunch Keyless.",
                                       PANE_URLS["screen_capture"])
        return MacPermissionStatus(key, PermissionState.UNKNOWN,
                                   "Screen Recording not asked yet; the OS prompt appears on first capture.",
                                   "Allow when prompted, then relaunch if the guide asks.",
                                   PANE_URLS["screen_capture"])
    if key in ("hotkey", "input_monitoring", "listen_event"):
        pre = _tcc_preflight(b"kTCCServiceListenEvent")
        if pre == 0:
            return MacPermissionStatus(key, PermissionState.GRANTED, "Input Monitoring granted (TCC).",
                                       "", PANE_URLS["input_monitoring"])
        if pre == 1:
            return MacPermissionStatus(key, PermissionState.DENIED,
                                       "Input Monitoring is off: switch Keyless on for the PTT hotkey.",
                                       "Switch on in System Settings > Privacy & Security > Input Monitoring.",
                                       PANE_URLS["input_monitoring"])
        return MacPermissionStatus(key, PermissionState.UNKNOWN,
                                   "Input Monitoring not asked yet.",
                                   "Allow when prompted in System Settings.",
                                   PANE_URLS["input_monitoring"])
    return MacPermissionStatus(key, PermissionState.UNKNOWN, f"Unknown permission need: {need}.",
                               "Open System Settings > Privacy & Security.", "")


def prompt_accessibility_trust() -> bool | None:
    """Register in TCC's Accessibility list and trigger the OS prompt. Main thread only."""
    return _ax_trusted(prompt=True)


class macOSPermissionService:
    """PermissionService backed by the ctypes TCC probes above."""

    def check(self, need: str) -> PermissionInfo:
        return check_permission(need).as_info()
