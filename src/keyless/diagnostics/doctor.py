"""One diagnostics report for `doctor`, support bundles, and the UI."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from keyless.config.migrations import ensure_migrated
from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.models.store import ModelStore
from keyless.models.providers.base import ProviderStatus
from keyless.models.providers.roles import resolve_gaze_vlm_role, resolve_text_role
from keyless.models.providers.secrets import keyring_available
from keyless.platform.base import PermissionState
from keyless.platform.capabilities import CapabilityReport, detect_capabilities
from keyless.platform.router import get_display_layout, list_cameras, resolve_camera_config
from keyless.voice.stt.factory import stt_report


def _safe_str(value: object) -> str:
    return str(value) if value is not None else ""


@dataclass
class DoctorReport:
    generated_at: str
    app_version: str
    host: dict[str, Any]
    paths: dict[str, str]
    path_checks: dict[str, dict[str, Any]]
    capabilities: dict[str, Any]
    permissions: dict[str, Any]
    cameras: dict[str, Any]
    audio: dict[str, Any]
    display: dict[str, Any]
    stt: dict[str, Any]
    providers: dict[str, Any]
    keychain: dict[str, Any]
    cua_driver: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "app_version": self.app_version,
            "host": self.host,
            "paths": self.paths,
            "path_checks": self.path_checks,
            "capabilities": self.capabilities,
            "permissions": self.permissions,
            "cameras": self.cameras,
            "audio": self.audio,
            "display": self.display,
            "stt": self.stt,
            "providers": self.providers,
            "keychain": self.keychain,
            "cua_driver": self.cua_driver,
            "errors": list(self.errors),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=False, default=str)

    @property
    def ok(self) -> bool:
        """Core pointer/voice requirements; optional AI never fails the report."""
        capability = self.capabilities.get("capabilities", {})
        for name in ("pointer", "cameras", "audio", "stt"):
            status = capability.get(name, {}).get("status")
            if status not in ("available", "degraded"):
                return False
        return True


def _path_checks(paths: AppPaths) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for name, path in paths.as_dict().items():
        target = Path(path)
        checks[name] = {
            "path": path,
            "exists": target.exists(),
            "writable": target.exists() and os.access(target, os.W_OK),
        }
    return checks


def _camera_section(
    config: AppConfig,
    camera_lister: Callable[[], list[Any]] | None = None,
) -> dict[str, Any]:
    lister = camera_lister or list_cameras
    try:
        discovered = [camera.as_dict() if hasattr(camera, "as_dict") else {
            "id": camera.id,
            "name": camera.name,
            "path": camera.path,
            "capabilities": camera.capabilities,
        } for camera in lister()]
    except Exception as exc:
        return {"discovered": [], "configured": {}, "error": str(exc)}
    mode, resolved1, resolved2 = resolve_camera_config(
        config.camera_mode, config.cam1_by_id, config.cam2_by_id
    )
    configured = {}
    for slot, path in (("cam1", resolved1), ("cam2", resolved2)):
        # Linux paths are /dev nodes; macOS paths are AVFoundation ids/indexes.
        looks_fs = bool(path) and (path.startswith("/") or path.startswith("avf:"))
        fs_exists = bool(path) and Path(path).exists() if path.startswith("/") else bool(path)
        configured[slot] = {
            "path": path,
            "exists": bool(path) and (fs_exists if path.startswith("/") else True),
            "readable": bool(path)
            and (
                (Path(path).exists() and os.access(path, os.R_OK))
                if path.startswith("/")
                else True
            ),
            "matches_discovered": any(item["path"] == path for item in discovered),
        }
    return {
        "discovered": discovered,
        "requested_mode": config.camera_mode,
        "mode": mode,
        "configured": configured,
        "error": "",
    }


def _audio_section(sounddevice_module: object | None = None) -> dict[str, Any]:
    try:
        sd = sounddevice_module
        if sd is None:
            import sounddevice as sd  # type: ignore[no-redef]
        devices = sd.query_devices()  # type: ignore[attr-defined]
        try:
            default = list(sd.default.device)  # type: ignore[attr-defined]
        except Exception:
            default = None
    except Exception as exc:
        return {"inputs": [], "default": None, "error": str(exc)}
    inputs = [
        {
            "name": _safe_str(device.get("name")),
            "channels": int(device.get("max_input_channels", 0)),
            "samplerate": float(device.get("default_samplerate", 0.0)),
        }
        for device in devices
        if int(device.get("max_input_channels", 0)) > 0
    ]
    return {"inputs": inputs, "default": default, "error": ""}


def _display_section() -> dict[str, Any]:
    try:
        layout = get_display_layout()
        return {
            "total_width": layout.total_width,
            "total_height": layout.total_height,
            "primary": layout.primary.connector,
            "monitors": [
                {
                    "connector": monitor.connector,
                    "x": monitor.x,
                    "y": monitor.y,
                    "width": monitor.width,
                    "height": monitor.height,
                    "scale": monitor.scale,
                    "primary": monitor.is_primary,
                }
                for monitor in layout.monitors
            ],
            "error": "",
        }
    except Exception as exc:
        return {"monitors": [], "error": str(exc)}


def _provider_section(config: AppConfig, check: bool) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for label, resolver in (
        ("text_llm", resolve_text_role),
        ("gaze_vlm", resolve_gaze_vlm_role),
    ):
        try:
            resolution = resolver(config)
            status: ProviderStatus | None = resolution.provider.status() if check else None
            output[label] = {
                "provider": resolution.provider.id,
                "display_name": resolution.provider.display_name,
                "model": resolution.provider.model,
                "local": resolution.provider.capabilities.local,
                "capabilities": {
                    "image_input": resolution.provider.capabilities.image_input,
                    "json_mode": resolution.provider.capabilities.json_mode,
                    "latency_class": resolution.provider.capabilities.latency_class,
                },
                "eligible": resolution.eligible,
                "reasons": resolution.reasons,
                "status": status.as_dict() if status is not None else {"state": "not_checked"},
            }
        except Exception as exc:
            output[label] = {"error": str(exc), "status": {"state": "error"}}
    return output


def _capability_section(
    paths: AppPaths,
    platform_name: str | None,
    sounddevice_module: object | None,
    provided: CapabilityReport | None,
) -> CapabilityReport:
    if provided is not None:
        return provided
    return detect_capabilities(
        platform_name=platform_name,
        sounddevice_module=sounddevice_module,
        paths=paths,
    )


def _cua_section() -> dict[str, Any]:
    try:
        from keyless.models import runtimes

        return runtimes.status()
    except Exception as exc:
        return {"ready": False, "error": str(exc)}


def collect_doctor(
    *,
    paths: AppPaths | None = None,
    config: AppConfig | None = None,
    platform_name: str | None = None,
    capabilities: CapabilityReport | None = None,
    sounddevice_module: object | None = None,
    camera_lister: Callable[[], list[Any]] | None = None,
    check_providers: bool = True,
) -> DoctorReport:
    resolved_paths = paths or current_paths()
    resolved_config = config or AppConfig.load()
    errors: list[str] = []

    try:
        resolved_paths.ensure_directories()
    except OSError as exc:
        errors.append(f"Could not create application directories: {exc}")
    try:
        ensure_migrated(resolved_paths)
    except OSError as exc:
        errors.append(f"Could not migrate legacy files: {exc}")

    report = _capability_section(
        resolved_paths,
        platform_name,
        sounddevice_module,
        capabilities,
    )
    permissions = {
        name: {
            "state": info.state.value,
            "detail": info.detail,
            "action": info.action,
        }
        for name, info in report.permissions.items()
    }
    permissions["keychain"] = {
        "state": PermissionState.GRANTED.value if keyring_available() else PermissionState.UNKNOWN.value,
        "detail": "OS keychain available." if keyring_available() else "keyring backend not installed.",
        "action": "" if keyring_available() else "Install the keyring backend.",
    }

    try:
        stt = stt_report(resolved_config, paths=resolved_paths)
    except Exception as exc:
        stt = {"error": str(exc), "selected": "unavailable"}
        errors.append(f"STT status failed: {exc}")

    app_version = "0.1.0"
    try:
        app_version = importlib.metadata.version("keyless")
    except importlib.metadata.PackageNotFoundError:
        pass

    return DoctorReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        app_version=app_version,
        host={
            "platform": sys.platform,
            "platform_release": platform.release(),
            "python": platform.python_version(),
            "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
            "display": os.environ.get("DISPLAY", ""),
            "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        },
        paths=resolved_paths.as_dict(),
        path_checks=_path_checks(resolved_paths),
        capabilities=report.as_dict(),
        permissions=permissions,
        cameras=_camera_section(resolved_config, camera_lister),
        audio=_audio_section(sounddevice_module),
        display=_display_section(),
        stt=stt,
        providers=_provider_section(resolved_config, check_providers),
        keychain={
            "available": keyring_available(),
            "service": "keyless",
        },
        cua_driver=_cua_section(),
        errors=errors,
    )


def format_text(report: DoctorReport) -> str:
    """Plain-text summary with one action per failing row."""
    lines: list[str] = []
    lines.append(f"Keyless {report.app_version} on {report.host['platform']} ({report.host['session']})")
    lines.append("")
    lines.append("Paths:")
    for name, check in report.path_checks.items():
        state = "ok" if check["writable"] else ("missing" if not check["exists"] else "read-only")
        lines.append(f"  {name:6s} {state:9s} {check['path']}")

    lines.append("")
    lines.append("Capabilities:")
    for name, info in report.capabilities.get("capabilities", {}).items():
        lines.append(f"  {name:18s} {info['status']:20s} {info.get('detail', '')}")
        if info.get("action"):
            lines.append(f"  {'':18s} -> {info['action']}")

    lines.append("")
    lines.append("Cameras:")
    discovered = report.cameras.get("discovered", [])
    if discovered:
        for camera in discovered:
            lines.append(f"  {camera['id']} -> {camera['path']}")
    else:
        lines.append("  none found")
    for slot, state in report.cameras.get("configured", {}).items():
        lines.append(
            f"  {slot}: {'ok' if state['readable'] else 'MISSING'} {state['path']}"
        )

    lines.append("")
    lines.append("Audio:")
    if report.audio.get("inputs"):
        for device in report.audio["inputs"]:
            lines.append(f"  {device['name']} ({device['channels']} ch @ {device['samplerate']:.0f} Hz)")
    else:
        lines.append(f"  none ({report.audio.get('error', 'no input devices')})")

    lines.append("")
    lines.append("Speech-to-text:")
    parakeet = report.stt.get("parakeet", {})
    if parakeet:
        lines.append(
            f"  parakeet: {parakeet.get('state')} ({parakeet.get('path')})"
        )
    lines.append(f"  selected: {report.stt.get('selected', 'missing')}")

    lines.append("")
    lines.append("Providers:")
    for role, info in report.providers.items():
        status = info.get("status", {})
        lines.append(
            f"  {role:9s} {info.get('display_name', info.get('error', 'unavailable'))} "
            f"[{status.get('state', 'unknown')}]"
        )
        for reason in info.get("reasons", []):
            lines.append(f"            {reason}")

    lines.append("")
    lines.append(f"Keychain: {'available' if report.keychain['available'] else 'unavailable'}")
    cua = report.cua_driver or {}
    cua_state = "ready" if cua.get("ready") else "missing"
    lines.append(
        f"cua-driver: {cua_state} ({cua.get('version', '?')}, {cua.get('target', '?')})"
        + ("" if cua.get("ready") else " — run `keyless models download cua-driver`")
    )
    if report.errors:
        lines.append("")
        lines.append("Errors:")
        for error in report.errors:
            lines.append(f"  {error}")
    lines.append("")
    lines.append("Hardware checks (cameras, mic use, uinput injection) are only proven by running them.")
    return "\n".join(lines)
