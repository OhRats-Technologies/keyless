"""On-disk model store: paths, receipts, and status (no network)."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from keyless.config.paths import AppPaths, current_paths
from keyless.models.catalog import ModelAsset
from keyless.models.receipts import ModelReceipt


class ModelState(str, Enum):
    READY = "ready"
    MISSING = "missing"
    PARTIAL = "partial"
    CORRUPT = "corrupt"


@dataclass
class ModelStatus:
    asset_id: str
    state: ModelState
    path: Path
    detail: str = ""
    missing_files: list[str] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def ready(self) -> bool:
        return self.state is ModelState.READY

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset_id,
            "state": self.state.value,
            "path": str(self.path),
            "detail": self.detail,
            "missing_files": list(self.missing_files),
            "total_bytes": self.total_bytes,
        }


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def prune_legacy_stt_assets(paths: AppPaths | None = None) -> list[str]:
    """Delete model directories for retired STT assets; return removed ids.

    The config layer already rewrites stored ids to the current default, so
    these directories are dead weight: remove them once on daemon start.
    """
    from keyless.models.catalog import LEGACY_STT_ASSET_IDS

    models_dir = (paths or current_paths()).models_dir
    removed: list[str] = []
    for asset_id in LEGACY_STT_ASSET_IDS:
        directory = models_dir / asset_id
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                removed.append(asset_id)
    return removed


class ModelStore:
    """App-owned model files under ``<data>/models/<asset-id>``."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or current_paths()

    def asset_dir(self, asset: ModelAsset) -> Path:
        return self.paths.models_dir / asset.id

    def receipt_path(self, asset: ModelAsset) -> Path:
        return self.asset_dir(asset) / "receipt.json"

    def partial_path(self, asset: ModelAsset, file_name: str) -> Path:
        return self.asset_dir(asset) / f"{file_name}.partial"

    def status(self, asset: ModelAsset, *, verify_hashes: bool = False) -> ModelStatus:
        directory = self.asset_dir(asset)
        total = asset.total_bytes
        if not directory.exists():
            return ModelStatus(asset.id, ModelState.MISSING, directory, "Model is not downloaded.", [], total)

        missing: list[str] = []
        partial = False
        for item in asset.files:
            target = directory / item.name
            if not target.exists():
                missing.append(item.name)
                if self.partial_path(asset, item.name).exists():
                    partial = True
        receipt = ModelReceipt.read(self.receipt_path(asset))
        if missing:
            state = ModelState.PARTIAL if partial else ModelState.MISSING
            detail = "Download did not finish; it can resume." if partial else "Model files are missing."
            return ModelStatus(asset.id, state, directory, detail, missing, total)

        if receipt is None or receipt.revision != asset.revision:
            return ModelStatus(
                asset.id,
                ModelState.CORRUPT,
                directory,
                "No matching verified receipt; run the downloader to verify.",
                [],
                total,
            )

        for item in asset.files:
            recorded = receipt.files.get(item.name)
            target = directory / item.name
            if recorded is None or recorded.bytes != item.bytes or target.stat().st_size != item.bytes:
                return ModelStatus(
                    asset.id,
                    ModelState.CORRUPT,
                    directory,
                    f"Size mismatch for {item.name}.",
                    [],
                    total,
                )
            if verify_hashes and sha256_file(target) != item.sha256:
                return ModelStatus(
                    asset.id,
                    ModelState.CORRUPT,
                    directory,
                    f"Checksum mismatch for {item.name}.",
                    [],
                    total,
                )
        return ModelStatus(asset.id, ModelState.READY, directory, "Verified.", [], total)
