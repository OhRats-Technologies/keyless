"""Resumable, verified model downloader with atomic promote and receipts."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from keyless.config.paths import AppPaths, current_paths
from keyless.models.catalog import ModelAsset, ModelFile
from keyless.models.receipts import FileReceipt, ModelReceipt
from keyless.models.store import ModelStore, sha256_file


class DownloadError(RuntimeError):
    """A model download failed; partial state is kept when resume is possible."""


class ChecksumMismatch(DownloadError):
    """Downloaded bytes do not match the pinned SHA-256."""


class DownloadCancelled(DownloadError):
    """The caller stopped the job; existing .partial files stay resumable."""


@dataclass(frozen=True)
class DownloadProgress:
    asset_id: str
    file_name: str
    file_index: int
    file_count: int
    bytes_done: int
    bytes_total: int
    overall_done: int
    overall_total: int

    @property
    def percent(self) -> float:
        if self.overall_total <= 0:
            return 100.0
        return min(100.0, 100.0 * self.overall_done / self.overall_total)


ProgressCallback = Callable[[DownloadProgress], None]
UrlResolver = Callable[[ModelFile], str]


def _download_one(
    asset: ModelAsset,
    item: ModelFile,
    store: ModelStore,
    *,
    client: httpx.Client,
    url: str,
    progress: ProgressCallback | None,
    file_index: int,
    overall_done_before: int,
    cancel: threading.Event | None = None,
) -> FileReceipt:
    target = store.asset_dir(asset) / item.name
    partial = store.partial_path(asset, item.name)

    if target.exists() and target.stat().st_size == item.bytes:
        if sha256_file(target) == item.sha256:
            return FileReceipt(item.name, item.bytes, item.sha256)
        # Keep the bad file instead of deleting user data.
        target.rename(target.with_name(target.name + f".corrupt-{int(time.time())}"))

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = partial.stat().st_size if partial.exists() else 0
    if existing > item.bytes:
        partial.unlink()
        existing = 0

    headers = {"Range": f"bytes={existing}-"} if existing else {}
    mode = "ab" if existing else "wb"
    try:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416:
                partial.unlink(missing_ok=True)
                return _download_one(
                    asset,
                    item,
                    store,
                    client=client,
                    url=url,
                    progress=progress,
                    file_index=file_index,
                    overall_done_before=overall_done_before,
                    cancel=cancel,
                )
            if response.status_code == 200 and existing:
                # Server ignored Range; restart this file.
                existing = 0
                mode = "wb"
            elif response.status_code not in (200, 206):
                raise DownloadError(f"HTTP {response.status_code} for {url}")

            written = existing
            last_report = 0.0
            with partial.open(mode) as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled("download stopped by user")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if progress is not None and (now - last_report >= 0.2 or written >= item.bytes):
                        progress(
                            DownloadProgress(
                                asset_id=asset.id,
                                file_name=item.name,
                                file_index=file_index,
                                file_count=len(asset.files),
                                bytes_done=written,
                                bytes_total=item.bytes,
                                overall_done=overall_done_before + written,
                                overall_total=asset.total_bytes,
                            )
                        )
                        last_report = now
    except httpx.HTTPError as exc:
        raise DownloadError(f"Could not download {url}: {exc}") from exc

    if partial.stat().st_size != item.bytes:
        raise DownloadError(
            f"{item.name}: expected {item.bytes} bytes, got {partial.stat().st_size}; re-run to resume."
        )
    actual = sha256_file(partial)
    if actual != item.sha256:
        # A corrupt partial cannot be resumed safely; remove only this partial.
        partial.unlink(missing_ok=True)
        raise ChecksumMismatch(f"{item.name}: SHA-256 {actual} != pinned {item.sha256}")

    os.replace(partial, target)
    if progress is not None:
        progress(
            DownloadProgress(
                asset_id=asset.id,
                file_name=item.name,
                file_index=file_index,
                file_count=len(asset.files),
                bytes_done=item.bytes,
                bytes_total=item.bytes,
                overall_done=overall_done_before + item.bytes,
                overall_total=asset.total_bytes,
            )
        )
    return FileReceipt(item.name, item.bytes, item.sha256)


def download_asset(
    asset: ModelAsset,
    *,
    paths: AppPaths | None = None,
    store: ModelStore | None = None,
    progress: ProgressCallback | None = None,
    client: httpx.Client | None = None,
    url_for: UrlResolver | None = None,
    cancel: threading.Event | None = None,
) -> ModelReceipt:
    """Download, verify, and atomically promote every file in ``asset``.

    Existing complete files are verified and kept.  ``.partial`` files resume by
    byte offset.  The receipt is written only after every file verifies.
    """
    resolved_store = store or ModelStore(paths or current_paths())
    resolved_store.asset_dir(asset).mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True)
    receipts: list[FileReceipt] = []
    done = 0
    try:
        for index, item in enumerate(asset.files, start=1):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("download stopped by user")
            url = url_for(item) if url_for is not None else asset.url(item)
            receipt = _download_one(
                asset,
                item,
                resolved_store,
                client=http,
                url=url,
                progress=progress,
                file_index=index,
                overall_done_before=done,
                cancel=cancel,
            )
            done += item.bytes
            receipts.append(receipt)
    finally:
        if owns_client:
            http.close()

    receipt = ModelReceipt.for_asset(asset, receipts)
    receipt.write_atomic(resolved_store.receipt_path(asset))
    return receipt
