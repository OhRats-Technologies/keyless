"""Configuration schema, OS paths, and migrations."""

from keyless.config.migrations import MigrationReport, ensure_migrated
from keyless.config.paths import AppPaths, current_paths, default_paths
from keyless.config.schema import (
    CACHE_DIR,
    CONFIG_DIR,
    DEFAULT_CAM1_ID,
    DEFAULT_CAM2_ID,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LANDMARKER_MODEL_PATH,
    FACE_LANDMARKER_URL,
    AppConfig,
)
from keyless.platform.base import DisplayLayout, MonitorInfo
from keyless.platform.router import get_display_layout, list_cameras as _list_cameras
from keyless.platform.linux.cameras import discover_v4l_by_id as _discover_v4l_by_id
from keyless.platform.linux.display import (
    parse_kscreen_layout,
    parse_mutter_display_state,
)


def discover_v4l_by_id(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Linux V4L2 discovery (kept for backward compat; macOS uses AVFoundation)."""
    import sys

    if sys.platform == "darwin":
        return [c.id for c in _list_cameras("darwin")]
    return _discover_v4l_by_id(*args, **kwargs)

__all__ = [
    "CACHE_DIR",
    "CONFIG_DIR",
    "DEFAULT_CAM1_ID",
    "DEFAULT_CAM2_ID",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_LANDMARKER_MODEL_PATH",
    "FACE_LANDMARKER_URL",
    "AppConfig",
    "AppPaths",
    "DisplayLayout",
    "MigrationReport",
    "MonitorInfo",
    "current_paths",
    "default_paths",
    "discover_v4l_by_id",
    "ensure_migrated",
    "get_display_layout",
    "parse_kscreen_layout",
    "parse_mutter_display_state",
]
