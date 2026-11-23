"""Session-keyed recording of gaze events (GREENFIELD.md §7.1, §7.3).

The collector is instrumentation, not a model.  It writes one JSONL file per
session plus a content-addressed blob store so a recorded session can be
replayed offline after the fact.

Two invariants matter more than throughput:

* the writer never blocks a capture loop and never grows without bound, so a
  slow disk drops events (counted) instead of stalling the daemon;
* redaction is explicit.  Text is kept verbatim unless ``redact_text=True``,
  and the choice is recorded in ``meta.json`` — never applied silently.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from keyless.config.paths import AppPaths, current_paths
from keyless.config.schema import AppConfig
from keyless.gaze.schema import SCHEMA_VERSION, validate_event

REDACTED = "[redacted]"

# src/keyless/gaze/collect.py -> repo root (best effort; git may be absent).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SUFFIX = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_hash(payload: Any) -> str:
    """SHA-256 of ``payload`` serialized as canonical JSON (sorted keys)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    """Current HEAD, or ``"unknown"`` when git or the repository is missing."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - instrumentation must never raise
        return "unknown"
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        return "unknown"
    return commit


def _config_hash(settings_path: Path) -> str:
    """Hash the effective :class:`AppConfig` so metrics can be attributed."""
    return canonical_hash(asdict(AppConfig.load(settings_path)))


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    """Replace user text with a marker, returning a copy of ``event``."""
    redacted = dict(event)

    screen = redacted.get("screen")
    if isinstance(screen, Mapping):
        screen = dict(screen)
        candidates = screen.get("candidates")
        if isinstance(candidates, list):
            screen["candidates"] = [
                {**candidate, "text": REDACTED} if isinstance(candidate, Mapping) else candidate
                for candidate in candidates
            ]
        redacted["screen"] = screen

    interaction = redacted.get("interaction")
    if isinstance(interaction, Mapping):
        interaction = dict(interaction)
        if interaction.get("voice") is not None:
            interaction["voice"] = REDACTED
        redacted["interaction"] = interaction

    return redacted


class SessionCollector:
    """Record events into ``<data_dir>/sessions/<session_id>/``.

    The writer thread appends and flushes one JSON line per event.  ``record``
    only enqueues; when the bounded queue is full the event is dropped and
    counted so callers can see the loss in :meth:`stats`.
    """

    def __init__(
        self,
        paths: AppPaths | None = None,
        session_id: str | None = None,
        *,
        redact_text: bool = False,
        queue_size: int = 1024,
        model_version: str = "none",
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")

        self._paths = paths or current_paths()
        self.session_id = session_id or uuid.uuid4().hex
        self.redact_text = bool(redact_text)

        self.session_dir = Path(self._paths.data_dir) / "sessions" / self.session_id
        self.blobs_dir = self.session_dir / "blobs"
        self.events_path = self.session_dir / "events.jsonl"
        self.meta_path = self.session_dir / "meta.json"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._blob_lock = threading.Lock()
        self._written = 0
        self._dropped = 0
        self._closed = False
        self._stop = threading.Event()

        self._meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_ts_ns": time.time_ns(),
            "model_version": str(model_version),
            "config_hash": _config_hash(self._paths.settings_path),
            "git_commit": _git_commit(),
            "redact_text": self.redact_text,
        }
        self._write_meta()

        self._handle = self.events_path.open("a", encoding="utf-8")
        self._thread = threading.Thread(
            target=self._write_loop,
            name=f"gaze-collect-{self.session_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    # --- recording ------------------------------------------------------

    def record(self, event: Mapping[str, Any]) -> bool:
        """Validate, stamp, and enqueue one event.

        Returns ``False`` when the queue was full and the event was dropped.
        Raises ``ValueError`` for a schema violation or for an event that
        belongs to a different session — garbage must not reach disk and a
        mislabelled event would break session-level splits.
        """
        if self._closed:
            raise RuntimeError("collector is closed")

        prepared = dict(event)
        if not prepared.get("session_id"):
            prepared["session_id"] = self.session_id
        elif prepared["session_id"] != self.session_id:
            raise ValueError(
                f"event session_id {prepared['session_id']!r} does not match "
                f"collector session {self.session_id!r}"
            )
        if prepared.get("ts_ns") is None:
            prepared["ts_ns"] = time.time_ns()

        if self.redact_text:
            prepared = _redact(prepared)

        errors = validate_event(prepared)
        if errors:
            raise ValueError("; ".join(errors))

        try:
            self._queue.put_nowait(prepared)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        return True

    def stats(self) -> dict[str, int]:
        """``queued`` is a snapshot; ``written``/``dropped_events`` are totals."""
        with self._lock:
            written = self._written
            dropped = self._dropped
        return {"queued": self._queue.qsize(), "written": written, "dropped_events": dropped}

    @property
    def dropped_events(self) -> int:
        with self._lock:
            return self._dropped

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until the writer has consumed every queued event.

        Returns ``True`` when drained.  ``timeout`` is in seconds; ``None``
        waits indefinitely.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.001)
        return True

    def close(self) -> None:
        """Flush, stop the writer, and finalize ``meta.json``.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.flush(timeout=10.0)
        self._stop.set()
        self._thread.join(timeout=10.0)
        self._handle.close()

        counts = self.stats()
        self._meta["ended_ts_ns"] = time.time_ns()
        self._meta["counts"] = {
            "events": counts["written"],
            "dropped": counts["dropped_events"],
        }
        self._write_meta()

    def __enter__(self) -> SessionCollector:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # --- blob store -----------------------------------------------------

    def put_blob(self, data: bytes, suffix: str) -> str:
        """Write ``data`` to ``blobs/<sha256>.<suffix>`` and return the digest.

        Identical bytes dedup by hash.  The write is atomic (temp + replace) so
        a concurrent reader never observes a partial blob.
        """
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        target = self.blob_path(digest, suffix)
        with self._blob_lock:
            if not target.exists():
                temp = target.with_name(f".{target.name}.tmp")
                temp.write_bytes(payload)
                temp.replace(target)
        return digest

    def blob_path(self, digest: str, suffix: str) -> Path:
        """Path of a stored blob.  Rejects traversal and malformed digests."""
        if not _HEX64.fullmatch(digest):
            raise ValueError("digest must be a lowercase sha256 hex string")
        clean = suffix.lstrip(".").strip()
        if not _SUFFIX.fullmatch(clean):
            raise ValueError("suffix must be a short alphanumeric token such as 'png'")
        return self.blobs_dir / f"{digest}.{clean}"

    def get_blob(self, digest: str, suffix: str) -> bytes:
        return self.blob_path(digest, suffix).read_bytes()

    # --- internals ------------------------------------------------------

    def _write_meta(self) -> None:
        payload = json.dumps(self._meta, indent=2, sort_keys=True) + "\n"
        self.meta_path.write_text(payload, encoding="utf-8")

    def _write_loop(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
                self._handle.write(line + "\n")
                self._handle.flush()
                with self._lock:
                    self._written += 1
            finally:
                self._queue.task_done()
