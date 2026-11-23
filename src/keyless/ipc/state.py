"""Tear-free binary shared-memory IPC and atomic JSON snapshot for gaze state."""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path

SHM_NAME = "keyless_gaze_v1"
# Layout: uint64 seq, double x, double y, uint32 flags, uint32 reserved, uint64 timestamp_ns
_STRUCT = struct.Struct("=QddIIQ")
SHM_SIZE = _STRUCT.size  # 32 bytes

FLAG_FACE_DETECTED = 1 << 0
FLAG_BLINKING = 1 << 1
FLAG_SACCADE_JUMP = 1 << 2
FLAG_PTT_ACTIVE = 1 << 3


@dataclass(frozen=True)
class GazeState:
    """Snapshot of the current gaze and trigger state."""

    x: float
    y: float
    flags: int
    timestamp_ns: int
    seq: int = 0


class SharedGazeWriter:
    """Writes gaze state to POSIX shared memory using an even/odd seqlock."""

    def __init__(
        self,
        shm_name: str = SHM_NAME,
        json_snapshot_path: Path | None = None,
    ) -> None:
        self.shm_name = shm_name
        self.json_snapshot_path = json_snapshot_path
        self._seq = 0
        self._last_json_ns = 0

        try:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=SHM_SIZE)
        except FileExistsError:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=False, track=False)
        self._buf = self._shm.buf
        self._buf[:SHM_SIZE] = b"\x00" * SHM_SIZE

    def write(self, x: float, y: float, flags: int, timestamp_ns: int | None = None) -> None:
        """Atomically publish `(x, y, flags, timestamp_ns)` without tearing."""
        ts = time.monotonic_ns() if timestamp_ns is None else int(timestamp_ns)
        # Odd sequence number marks write-in-progress; even marks committed state.
        odd_seq = self._seq + 1
        even_seq = self._seq + 2
        struct.pack_into("=Q", self._buf, 0, odd_seq)
        _STRUCT.pack_into(self._buf, 0, odd_seq, float(x), float(y), int(flags), 0, ts)
        struct.pack_into("=Q", self._buf, 0, even_seq)
        self._seq = even_seq

        if self.json_snapshot_path is not None and (ts - self._last_json_ns) >= 33_000_000:
            self._last_json_ns = ts
            self._write_atomic_json(x, y, flags, ts, even_seq)

    def _write_atomic_json(
        self, x: float, y: float, flags: int, timestamp_ns: int, seq: int
    ) -> None:
        assert self.json_snapshot_path is not None
        tmp_path = self.json_snapshot_path.with_suffix(".tmp")
        payload = json.dumps(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "flags": flags,
                "timestamp_ns": timestamp_ns,
                "seq": seq,
            }
        )
        tmp_path.write_text(payload)
        os.replace(tmp_path, self.json_snapshot_path)

    def close(self, unlink: bool = True) -> None:
        self._buf.release()
        self._shm.close()
        if unlink:
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass


class SharedGazeReader:
    """Lock-free reader for POSIX shared memory gaze state."""

    def __init__(self, shm_name: str = SHM_NAME) -> None:
        self.shm_name = shm_name
        self._shm = shared_memory.SharedMemory(name=shm_name, create=False, track=False)
        self._buf = self._shm.buf

    def read(self, max_retries: int = 8) -> GazeState | None:
        """Read a consistent snapshot; returns None if writer is mid-update beyond retries."""
        for _ in range(max_retries):
            seq1 = struct.unpack_from("=Q", self._buf, 0)[0]
            if seq1 & 1:
                continue
            seq_a, x, y, flags, _, ts = _STRUCT.unpack_from(self._buf, 0)
            seq2 = struct.unpack_from("=Q", self._buf, 0)[0]
            if seq_a == seq1 == seq2:
                return GazeState(x=x, y=y, flags=flags, timestamp_ns=ts, seq=seq2)
        return None

    def close(self) -> None:
        self._buf.release()
        self._shm.close()
