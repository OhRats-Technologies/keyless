"""Per-OS dispatch: shared code imports from here, never from linux/macos directly.

A capability with no implementation on the current host raises
``NotSupportedError`` instead of silently running another OS's code.
"""

from __future__ import annotations

import sys

from keyless.platform.base import NotSupportedError

_SUPPORTED_HOSTS = ("linux", "macos")


def current_host(platform_name: str | None = None) -> str:
    name = platform_name or sys.platform
    if name.startswith("linux"):
        return "linux"
    if name == "darwin":
        return "macos"
    if name.startswith("win"):
        return "windows"
    return name


def _require_host(host: str, capability: str) -> None:
    if host not in _SUPPORTED_HOSTS:
        raise NotSupportedError(f"{capability} is not implemented on {host}")


def get_display_layout(platform_name: str | None = None):  # type: ignore[no-untyped-def]
    host = current_host(platform_name)
    _require_host(host, "display layout")
    if host == "macos":
        from keyless.platform.macos.display import get_display_layout as get
    else:
        from keyless.platform.linux.display import get_display_layout as get
    return get()


def list_cameras(platform_name: str | None = None):  # type: ignore[no-untyped-def]
    host = current_host(platform_name)
    _require_host(host, "camera enumeration")
    if host == "macos":
        from keyless.platform.macos.cameras import list_cameras as list_c
    else:
        from keyless.platform.linux.cameras import list_cameras as list_c
    return list_c()


def resolve_camera_config(
    requested_mode: str,
    cam1: str,
    cam2: str,
    platform_name: str | None = None,
    **kwargs,  # type: ignore[no-untyped-def]
):
    host = current_host(platform_name)
    _require_host(host, "camera configuration")
    if host == "macos":
        from keyless.platform.macos.cameras import resolve_camera_config as resolve
    else:
        from keyless.platform.linux.cameras import resolve_camera_config as resolve
    return resolve(requested_mode, cam1, cam2, **kwargs)


def monitor_at_point(layout, x: float, y: float, platform_name: str | None = None):  # type: ignore[no-untyped-def]
    host = current_host(platform_name)
    _require_host(host, "monitor hit testing")
    if host == "macos":
        from keyless.platform.macos.screen import monitor_at_point as at_point
    else:
        from keyless.platform.linux.screen import monitor_at_point as at_point
    return at_point(layout, x, y)


def pointer_position() -> tuple[float, float] | None:
    """Read the real OS pointer without moving it, where the host supports it."""
    if current_host() != "macos":
        return None
    try:
        from keyless.platform.macos.pointer import macOSPointerBackend

        x, y = macOSPointerBackend()._current_position()
        return float(x), float(y)
    except Exception:
        return None


def create_pointer_backend(
    layout_width: int,
    layout_height: int,
    action_backend: str = "native",
    platform_name: str | None = None,
):  # type: ignore[no-untyped-def]
    host = current_host(platform_name)
    if action_backend == "cua":
        from keyless.platform.cua.driver import CuaDriverManager
        from keyless.platform.cua.pointer import CuaDriverPointerBackend

        native = _native_pointer_backend(host, layout_width, layout_height)
        try:
            from keyless.models import runtimes

            info = runtimes.status()
            if not info.get("ready"):
                raise NotImplementedError(
                    f"cua-driver {runtimes.CUA_DRIVER_VERSION} not downloaded "
                    f"(run `keyless models download cua-driver`)."
                )
            return CuaDriverPointerBackend(native, CuaDriverManager())
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "cua action backend unavailable, staying native: %s", exc
            )
            return native
    return _native_pointer_backend(host, layout_width, layout_height)


def _native_pointer_backend(host: str, layout_width: int, layout_height: int):  # type: ignore[no-untyped-def]
    _require_host(host, "pointer control")
    if host == "macos":
        from keyless.platform.macos.pointer import macOSPointerBackend

        return macOSPointerBackend()
    from keyless.platform.linux.pointer import UInputManager

    return UInputManager(layout_width, layout_height)
