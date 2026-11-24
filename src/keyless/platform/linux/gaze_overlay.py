"""Native click-through Wayland multi-monitor gaze reticle overlay manager."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from keyless.config.paths import AppPaths, current_paths
from keyless.ipc.state import SHM_NAME

logger = logging.getLogger(__name__)

_C_SOURCE = Path(__file__).with_name("overlay_wayland.c")


def _discover_wayland_env() -> dict[str, str] | None:
    """Return environment with WAYLAND_DISPLAY + XDG_RUNTIME_DIR if reachable."""
    env = dict(os.environ)
    uid = os.getuid()
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    if not Path(runtime_dir).is_dir():
        return None
    env["XDG_RUNTIME_DIR"] = runtime_dir
    if env.get("WAYLAND_DISPLAY"):
        return env
    for sock in sorted(Path(runtime_dir).glob("wayland-*")):
        if not sock.name.endswith(".lock") and sock.is_socket():
            env["WAYLAND_DISPLAY"] = sock.name
            return env
    return None


def ensure_overlay_binary(paths: AppPaths | None = None) -> tuple[Path | None, str]:
    """Compile ``overlay_wayland.c`` once (cached by SHA256) and return its path."""
    if not _C_SOURCE.exists():
        return None, "overlay_wayland.c source not found"
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        return None, "gcc/cc compiler not installed"

    resolved = paths or current_paths()
    bin_dir = resolved.data_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(_C_SOURCE.read_bytes()).hexdigest()[:12]
    out_bin = bin_dir / f"keyless-overlay-{digest}"
    if out_bin.exists() and os.access(out_bin, os.X_OK):
        return out_bin, ""

    tmp_bin = bin_dir / f"keyless-overlay-{digest}.tmp"
    proc = subprocess.run(
        [cc, "-O2", str(_C_SOURCE), "-o", str(tmp_bin), "-lwayland-client", "-lrt", "-lm"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        try:
            tmp_bin.unlink(missing_ok=True)
        except OSError:
            pass
        return None, (proc.stderr or proc.stdout or "compilation failed").strip()

    tmp_bin.replace(out_bin)
    # Clean older cached builds
    for old in bin_dir.glob("keyless-overlay-*"):
        if old != out_bin:
            try:
                old.unlink()
            except OSError:
                pass
    return out_bin, ""


class LinuxGazeOverlay:
    """Runs the click-through Wayland gaze reticle as a managed child process."""

    def __init__(self, paths: AppPaths | None = None, *, shm_name: str = SHM_NAME) -> None:
        self.paths = paths or current_paths()
        self.shm_name = shm_name
        self._proc: subprocess.Popen[bytes] | None = None
        self.error: str = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        if self.running:
            return True
        env = _discover_wayland_env()
        if env is None:
            self.error = "No active Wayland session socket found."
            return False
        binary, err = ensure_overlay_binary(self.paths)
        if binary is None:
            self.error = err
            return False
        try:
            self._proc = subprocess.Popen(
                [str(binary), self.shm_name],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.error = ""
            return True
        except OSError as exc:
            self.error = str(exc)
            return False

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)


__all__ = ["LinuxGazeOverlay", "ensure_overlay_binary"]
