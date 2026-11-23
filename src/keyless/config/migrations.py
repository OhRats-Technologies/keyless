"""One-time, non-destructive migration of legacy config files into the D4 layout."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from keyless.config.paths import AppPaths, current_paths

MIGRATION_VERSION = 1


@dataclass
class MigrationReport:
    """Outcome of one migration pass; sources are never removed."""

    copied: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    marker_written: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.copied)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": MIGRATION_VERSION,
            "copied": [{"from": s, "to": d} for s, d in self.copied],
            "skipped": [{"from": s, "to": d} for s, d in self.skipped],
            "marker_written": self.marker_written,
        }


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        shutil.copy2(source, partial)
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)


def legacy_file_map(paths: AppPaths) -> list[tuple[Path, Path]]:
    """Legacy file -> new location pairs, in migration order."""
    return [
        (paths.legacy_settings_path, paths.settings_path),
    ]


def ensure_migrated(paths: AppPaths | None = None, *, force: bool = False) -> MigrationReport:
    """Copy legacy files into the new layout once.

    Never deletes or rewrites a legacy file, never overwrites an existing new
    file, and records a marker so later starts skip the pass.
    """
    resolved = paths or current_paths()
    report = MigrationReport()
    marker = resolved.migration_marker_path

    if marker.exists() and not force:
        for source, target in legacy_file_map(resolved):
            report.skipped.append((str(source), str(target)))
        return report

    for source, target in legacy_file_map(resolved):
        if source.resolve() == target.resolve():
            report.skipped.append((str(source), str(target)))
            continue
        if not source.exists() or target.exists():
            report.skipped.append((str(source), str(target)))
            continue
        _copy_atomic(source, target)
        report.copied.append((str(source), str(target)))

    resolved.config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MIGRATION_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "copied": [{"from": s, "to": d} for s, d in report.copied],
    }
    partial = marker.with_name(marker.name + ".partial")
    partial.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(partial, marker)
    report.marker_written = True
    return report
