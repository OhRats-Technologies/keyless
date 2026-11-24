"""macOS eye cursor fed from the shared-memory gaze channel.

The macOS counterpart of ``platform/linux/gaze_overlay.py``: the gaze
publisher owns it, it reads ``keyless_gaze_v1`` and draws on cua-driver's
agent cursor (never the hardware pointer, AGENTS rule 3).
``MacOSPointerVisibility`` keeps exactly one cursor on screen (rule 4); while
the calibration stage is up or the face has been lost, the eye cursor is
disallowed and the hardware pointer is handed back synchronously.

Only one process may draw the eye cursor. Ownership is an ``flock`` on a file
in the cache dir, so it is released by the OS even if the owner crashes; the
voice daemon checks it before creating its own overlay.
"""

from __future__ import annotations

import fcntl
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from keyless.ipc.state import FLAG_FACE_DETECTED, SHM_NAME, SharedGazeReader

logger = logging.getLogger(__name__)

# A face lost for longer than this hands the screen back to the hardware
# pointer; shorter dropouts keep the eye cursor still instead of flickering.
FACE_LOST_S = 0.5
_POLL_S = 1.0 / 60.0


def eye_cursor_lock_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "eye_cursor.lock"


def claim_eye_cursor(cache_dir: Path) -> IO[str] | None:
    """Take the process-wide eye-cursor ownership lock, or None if held elsewhere."""
    path = eye_cursor_lock_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def eye_cursor_owned_elsewhere(cache_dir: Path) -> bool:
    handle = claim_eye_cursor(cache_dir)
    if handle is None:
        return True
    handle.close()  # closing releases the probe lock
    return False


class MacGazeCursor:
    """Publisher-owned eye cursor: shm frames -> cua agent cursor, one cursor on screen."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        shm_name: str = SHM_NAME,
        hold: Callable[[], bool] = lambda: False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.shm_name = shm_name
        self._hold = hold
        self._lock_handle: IO[str] | None = None
        self._overlay = None
        self._pointer_mode = None
        self._reader: SharedGazeReader | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        from keyless.platform.cua.agent_cursor import AgentCursorOverlay
        from keyless.platform.macos.pointer_mode import MacOSPointerVisibility

        available, reason = AgentCursorOverlay.available()
        if not available:
            self.error = reason
            return False
        self._lock_handle = claim_eye_cursor(self.cache_dir)
        if self._lock_handle is None:
            self.error = "another Keyless process already draws the eye cursor"
            return False
        try:
            self._reader = SharedGazeReader(self.shm_name)
        except FileNotFoundError:
            self.error = f"gaze channel {self.shm_name} is not published"
            self._release()
            return False
        self._overlay = AgentCursorOverlay()
        self._pointer_mode = MacOSPointerVisibility(self._overlay)
        self._pointer_mode.set_eye_allowed(False)  # until the first face frame
        self._pointer_mode.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="keyless-mac-eye-cursor", daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        assert self._reader is not None and self._overlay is not None and self._pointer_mode is not None
        last_face = 0.0
        last_seq = -1
        while not self._stop.wait(_POLL_S):
            state = self._reader.read()
            now = time.monotonic()
            if state is not None and state.seq != last_seq:
                last_seq = state.seq
                if state.flags & FLAG_FACE_DETECTED:
                    last_face = now
                    self._overlay.move_absolute(state.x, state.y)
            allowed = not self._hold() and (now - last_face) < FACE_LOST_S
            self._pointer_mode.set_eye_allowed(allowed)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        if self._pointer_mode is not None:
            self._pointer_mode.stop()  # leaves the hardware pointer visible
            self._pointer_mode = None
        if self._overlay is not None:
            self._overlay.close()
            self._overlay = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._release()

    def _release(self) -> None:
        if self._lock_handle is not None:
            self._lock_handle.close()
            self._lock_handle = None


__all__ = ["MacGazeCursor", "claim_eye_cursor", "eye_cursor_owned_elsewhere"]
