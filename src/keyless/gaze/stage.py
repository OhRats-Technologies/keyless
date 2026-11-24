"""Calibration stage channel: Python owns timing, the native overlay draws.

Layout ``=QddffffII`` (48 bytes) mirrors ``stage_shm_layout`` in
``platform/linux/overlay_wayland.c``. Even/odd seqlock like the gaze channel.
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
from multiprocessing import shared_memory

from keyless.config.paths import AppPaths, current_paths

logger = logging.getLogger(__name__)

STAGE_SHM_NAME = "keyless_stage_v1"
_STRUCT = struct.Struct("=QddffffII")
STAGE_SHM_SIZE = _STRUCT.size

STAGE_ACTIVE = 1 << 0
STAGE_TARGET = 1 << 1
STAGE_PAUSED = 1 << 2
STAGE_DONE = 1 << 3


class StageWriter:
    def __init__(self, shm_name: str = STAGE_SHM_NAME) -> None:
        self.shm_name = shm_name
        self._seq = 0
        try:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=STAGE_SHM_SIZE)
        except FileExistsError:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=False, track=False)
        self._shm.buf[:STAGE_SHM_SIZE] = b"\x00" * STAGE_SHM_SIZE

    def write(
        self,
        x: float,
        y: float,
        *,
        size: float,
        phase: float,
        dim: float,
        burst: float = -1.0,
        flags: int = STAGE_ACTIVE,
    ) -> None:
        odd, even = self._seq + 1, self._seq + 2
        struct.pack_into("=Q", self._shm.buf, 0, odd)
        _STRUCT.pack_into(
            self._shm.buf, 0, odd, float(x), float(y), float(size), float(phase),
            float(dim), float(burst), int(flags), 0,
        )
        struct.pack_into("=Q", self._shm.buf, 0, even)
        self._seq = even

    def close(self) -> None:
        try:
            self.write(0.0, 0.0, size=0.0, phase=0.0, dim=0.0, flags=STAGE_DONE)
        except Exception:
            pass
        try:
            self._shm.close()
            self._shm.unlink()
        except Exception:
            pass


class LinuxStageOverlay:
    """Runs the native overlay binary in stage mode for the duration of a run."""

    def __init__(self, paths: AppPaths | None = None, *, stage_name: str = STAGE_SHM_NAME) -> None:
        self.paths = paths or current_paths()
        self.stage_name = stage_name
        self._proc: subprocess.Popen[bytes] | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        from keyless.platform.linux.gaze_overlay import _discover_wayland_env, ensure_overlay_binary

        env = _discover_wayland_env()
        if env is None:
            self.error = "no Wayland session"
            return False
        binary, err = ensure_overlay_binary(self.paths)
        if binary is None:
            self.error = err
            return False
        try:
            self._proc = subprocess.Popen(
                [str(binary), "keyless_gaze_v1", str(os.getpid()), self.stage_name],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        try:
            # A missing layer-shell makes the binary exit immediately (rc 2).
            self._proc.wait(timeout=0.35)
            err_txt = (self._proc.stderr.read() if self._proc.stderr else b"").decode(errors="replace")
            self.error = err_txt.strip() or f"overlay exited ({self._proc.returncode})"
            self._proc = None
            return False
        except subprocess.TimeoutExpired:
            return True

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        _stop_stage_process(proc)


def _stop_stage_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.wait(timeout=1.0)  # STAGE_DONE makes it exit on its own
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()


class MacStageOverlay:
    """Runs ``platform/macos/stage_window.py`` (one window per display) for a run.

    Same ``start/stop/running/error`` contract as ``LinuxStageOverlay``. The
    child prints ``ready`` once its windows are up, so ``start`` waits for
    that line instead of guessing with a timeout.
    """

    READY_TIMEOUT_S = 5.0

    def __init__(self, paths: AppPaths | None = None, *, stage_name: str = STAGE_SHM_NAME) -> None:
        self.paths = paths or current_paths()
        self.stage_name = stage_name
        self._proc: subprocess.Popen[bytes] | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        import select
        import sys

        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "keyless.platform.macos.stage_window",
                 self.stage_name, str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        assert self._proc.stdout is not None
        ready, _, _ = select.select([self._proc.stdout], [], [], self.READY_TIMEOUT_S)
        line = self._proc.stdout.readline() if ready else b""
        if line.strip() == b"ready":
            return True
        self._proc.kill()
        err = (self._proc.stderr.read() if self._proc.stderr else b"").decode(errors="replace")
        self.error = err.strip().splitlines()[-1] if err.strip() else "stage window did not start"
        self._proc = None
        return False

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        _stop_stage_process(proc)


__all__ = [
    "LinuxStageOverlay",
    "MacStageOverlay",
    "STAGE_ACTIVE",
    "STAGE_DONE",
    "STAGE_PAUSED",
    "STAGE_SHM_NAME",
    "STAGE_TARGET",
    "StageWriter",
]
