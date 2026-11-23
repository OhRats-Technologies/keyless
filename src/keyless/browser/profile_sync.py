"""One-shot safe cloning of Firefox cookies, storage, bookmarks, and logins for Playwright."""

from __future__ import annotations

import configparser
import shutil
import sqlite3
from pathlib import Path
from typing import Sequence

from keyless.config.paths import current_paths

KEYLESS_FIREFOX_PROFILE = current_paths().data_dir / "playwright-firefox"

_FIREFOX_ROOTS: tuple[Path, ...] = (
    Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox",  # Ubuntu Snap
    Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",  # Flatpak
    Path.home() / ".mozilla" / "firefox",  # Native .deb / APT
)

_SQLITE_STORES: tuple[str, ...] = (
    "cookies.sqlite",
    "places.sqlite",
    "favicons.sqlite",
    "permissions.sqlite",
    "formhistory.sqlite",
    "key4.db",
    "cert9.db",
)


def default_playwright_firefox_profile() -> Path:
    """Resolve the persistent Playwright Firefox profile path (respects KEYLESS_HOME)."""
    return current_paths().data_dir / "playwright-firefox"


def _sqlite_safe_copy(src: Path, dst: Path) -> None:
    """Copy a live SQLite DB via online backup API so WAL transactions flush cleanly."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    try:
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=2.0) as src_conn:
            with sqlite3.connect(str(dst)) as dst_conn:
                src_conn.backup(dst_conn)
    except Exception:
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(src) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(dst) + suffix))


def find_default_firefox_profile(roots: Sequence[Path] = _FIREFOX_ROOTS) -> Path | None:
    """Return the active default Firefox profile path on Linux."""
    for root in roots:
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        cfg = configparser.RawConfigParser()
        cfg.read(ini)
        for sec in cfg.sections():
            if sec.startswith("Install") and cfg.has_option(sec, "Default"):
                cand = root / cfg.get(sec, "Default")
                if cand.is_dir():
                    return cand
        for sec in cfg.sections():
            if cfg.has_option(sec, "Path") and cfg.get(sec, "Default", fallback="0") == "1":
                raw = cfg.get(sec, "Path")
                cand = (
                    (root / raw)
                    if cfg.get(sec, "IsRelative", fallback="1") == "1"
                    else Path(raw)
                )
                if cand.is_dir():
                    return cand
    return None


def sync_firefox_profile(
    src_profile: Path | None = None,
    dst_profile: Path | None = None,
) -> tuple[Path, list[str]]:
    """Copy cookies, IndexedDB/LocalStorage, bookmarks, and saved logins into Keyless's Firefox."""
    src = src_profile or find_default_firefox_profile()
    if src is None or not src.is_dir():
        raise FileNotFoundError(
            "No Firefox profile found under Snap, Flatpak, or ~/.mozilla/firefox. "
            "Pass --profile /path/to/profile explicitly."
        )

    target = dst_profile if dst_profile is not None else default_playwright_firefox_profile()
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    for db_name in _SQLITE_STORES:
        src_db = src / db_name
        if src_db.exists():
            _sqlite_safe_copy(src_db, target / db_name)
            copied.append(db_name)

    for plain_file in ("logins.json", "containers.json"):
        src_file = src / plain_file
        if src_file.exists():
            shutil.copy2(src_file, target / plain_file)
            copied.append(plain_file)

    src_storage = src / "storage"
    dst_storage = target / "storage"
    if src_storage.is_dir():
        if dst_storage.exists():
            shutil.rmtree(dst_storage)
        shutil.copytree(
            src_storage,
            dst_storage,
            ignore=shutil.ignore_patterns("*.lock", "lock", "parent.lock", ".parentlock"),
            dirs_exist_ok=True,
        )
        copied.append("storage/")

    # Remove stale compatibility cache so Playwright's Firefox rebuilds extensions/cache cleanly
    compat = target / "compatibility.ini"
    if compat.exists():
        compat.unlink()

    return target, copied
