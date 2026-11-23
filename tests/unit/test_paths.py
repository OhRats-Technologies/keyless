"""Unit tests for the D4 path service and the non-destructive migration."""

from __future__ import annotations

import json
from pathlib import Path

from keyless.config.migrations import ensure_migrated
from keyless.config.paths import HOME_ENV, default_paths
from keyless.config.schema import AppConfig


def test_linux_roots_follow_xdg_and_split_config_data_cache_logs() -> None:
    paths = default_paths(
        env={"XDG_CONFIG_HOME": "/tmp/cfg", "XDG_CACHE_HOME": "/tmp/cache"},
        platform_name="linux",
        home=Path("/home/alice"),
    )
    assert paths.config_dir == Path("/tmp/cfg/keyless")
    assert paths.data_dir == Path("/home/alice/.local/share/keyless")
    assert paths.cache_dir == Path("/tmp/cache/keyless")
    assert paths.logs_dir == Path("/home/alice/.local/state/keyless/logs")
    assert paths.settings_path == paths.config_dir / "settings.json"
    assert paths.models_dir == paths.data_dir / "models"
    assert paths.history_db_path == paths.data_dir / "history" / "actions.db"
    assert paths.landmarker_path == paths.cache_dir / "face_landmarker.task"


def test_keyless_home_overrides_every_root(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    paths = default_paths(env={HOME_ENV: str(root)}, platform_name="linux", home=Path("/home/alice"))
    assert paths.config_dir == root / "config"
    assert paths.data_dir == root / "data"
    assert paths.cache_dir == root / "cache"
    assert paths.logs_dir == root / "logs"
    assert paths.home == root


def test_macos_uses_one_support_root_and_separate_logs() -> None:
    paths = default_paths(env={}, platform_name="darwin", home=Path("/Users/alice"))
    assert paths.config_dir == Path("/Users/alice/Library/Application Support/Keyless")
    assert paths.data_dir == paths.config_dir
    assert paths.cache_dir == Path("/Users/alice/Library/Caches/Keyless")
    assert paths.logs_dir == Path("/Users/alice/Library/Logs/Keyless")


def test_windows_keeps_large_assets_out_of_roaming(tmp_path: Path) -> None:
    env = {
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }
    paths = default_paths(env=env, platform_name="win32", home=tmp_path)
    assert paths.config_dir == tmp_path / "Roaming" / "Keyless"
    assert paths.data_dir == tmp_path / "Local" / "Keyless"
    assert paths.models_dir == tmp_path / "Local" / "Keyless" / "models"
    assert paths.logs_dir == tmp_path / "Local" / "Keyless" / "logs"


def test_migration_copies_legacy_files_and_keeps_the_originals(tmp_path: Path) -> None:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    paths.legacy_settings_path.write_text('{"ptt_hotkey": "KEY_F9"}\n')

    report = ensure_migrated(paths)

    assert report.changed
    assert paths.settings_path.read_text() == '{"ptt_hotkey": "KEY_F9"}\n'
    # Old files are kept in place.
    assert paths.legacy_settings_path.exists()
    assert json.loads(paths.migration_marker_path.read_text())["version"] == 1

    second = ensure_migrated(paths)
    assert not second.changed
    assert not second.marker_written


def test_migration_never_overwrites_newer_files(tmp_path: Path) -> None:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    paths.legacy_settings_path.write_text('{"ptt_hotkey": "KEY_OLD"}\n')
    paths.settings_path.write_text('{"ptt_hotkey": "KEY_NEW"}\n')

    ensure_migrated(paths)

    assert paths.settings_path.read_text() == '{"ptt_hotkey": "KEY_NEW"}\n'
    assert paths.legacy_settings_path.read_text() == '{"ptt_hotkey": "KEY_OLD"}\n'


def test_appconfig_loads_migrated_settings_and_also_reads_legacy(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    paths = default_paths()
    paths.ensure_directories()
    paths.legacy_settings_path.write_text('{"ptt_hotkey": "KEY_F8", "fps": 24}\n')

    cfg = AppConfig.load()

    assert cfg.ptt_hotkey == "KEY_F8"
    assert cfg.fps == 24
    assert paths.settings_path.exists()
    assert paths.legacy_settings_path.exists()


def test_appconfig_save_uses_settings_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    cfg = AppConfig(fps=12)
    cfg.save()

    paths = default_paths()
    assert paths.settings_path.exists()
    assert json.loads(paths.settings_path.read_text())["fps"] == 12
