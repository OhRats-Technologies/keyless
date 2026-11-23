"""Pinned third-party runtime binaries (cua-driver).

Same policy as the model catalog: one pinned version, exact bytes per
platform, SHA-256 verified before use, receipt written on success. Bumping
the runtime is a version + hash change here — no code changes, no surprises.

Upstream: `cua-driver-rs-v*` GitHub releases
(https://github.com/trycua/cua), asset pattern
``cua-driver-rs-<version>-<label>-binary.{tar.gz,zip}`` where label is
``darwin-universal`` (macOS), ``linux-x86_64`` / ``linux-arm64``,
``windows-x86_64`` / ``windows-arm64``.
"""

from __future__ import annotations

import hashlib
import json
import platform as _platform
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

CUA_DRIVER_ID = "cua-driver"
CUA_DRIVER_VERSION = "0.59.2"
CUA_DRIVER_TAG = f"cua-driver-rs-v{CUA_DRIVER_VERSION}"
CUA_DRIVER_REPO = "https://github.com/trycua/cua"
CUA_DRIVER_BASE = f"{CUA_DRIVER_REPO}/releases/download/cua-driver-rs-v{CUA_DRIVER_VERSION}"
CUA_DRIVER_LICENSE = "MIT"


@dataclass(frozen=True)
class RuntimeTarget:
    label: str
    asset: str
    bytes: int
    sha256: str
    exe: str = "cua-driver"


CUA_DRIVER_TARGETS: tuple[RuntimeTarget, ...] = (
    RuntimeTarget(
        label="darwin-universal",
        asset=f"cua-driver-rs-{CUA_DRIVER_VERSION}-darwin-universal-binary.tar.gz",
        bytes=44155614,
        sha256="85ac3caebf808567f64d636ec9d1b03bd2c079fe53955eef6f21ce08a140974c",
    ),
    RuntimeTarget(
        label="linux-x86_64",
        asset=f"cua-driver-rs-{CUA_DRIVER_VERSION}-linux-x86_64-binary.tar.gz",
        bytes=30447495,
        sha256="6aa6b740c0473ce93f64bd42b370d98532b6b497c7bc15887364f8782977f10c",
    ),
    RuntimeTarget(
        label="linux-arm64",
        asset=f"cua-driver-rs-{CUA_DRIVER_VERSION}-linux-arm64-binary.tar.gz",
        bytes=28340370,
        sha256="1047cae355c4fe0197bb25417c657eaafcbf93ee6dd0b3292440541eaaf9ce67",
    ),
    RuntimeTarget(
        label="windows-x86_64",
        asset=f"cua-driver-rs-{CUA_DRIVER_VERSION}-windows-x86_64-binary.zip",
        bytes=32683167,
        sha256="7d14c63204ba7ebf8f195ab3e6cef2c261764428fa5e2ea033a446a2a4410fc",
        exe="cua-driver.exe",
    ),
    RuntimeTarget(
        label="windows-arm64",
        asset=f"cua-driver-rs-{CUA_DRIVER_VERSION}-windows-arm64-binary.zip",
        bytes=30441700,
        sha256="33cd789c38999bada127e56a40aa7514ca61023bbf892bf69522c9a05e604516",
        exe="cua-driver.exe",
    ),
)


def _current_label() -> str:
    system = _platform.system().lower()
    machine = _platform.machine().lower()
    if system == "darwin":
        return "darwin-universal"
    if system == "windows":
        return "windows-arm64" if machine in ("arm64", "aarch64") else "windows-x86_64"
    # linux and friends
    return "linux-arm64" if machine in ("arm64", "aarch64") else "linux-x86_64"


def current_target() -> RuntimeTarget:
    want = _current_label()
    for target in CUA_DRIVER_TARGETS:
        if target.label == want:
            return target
    raise NotImplementedError(f"No pinned cua-driver runtime for this host ({want}).")


def runtime_dir(data_dir: Path | None = None) -> Path:
    from keyless.config.paths import current_paths

    root = data_dir if data_dir is not None else current_paths().data_dir
    return root / "runtimes" / CUA_DRIVER_ID / CUA_DRIVER_VERSION


def binary_path(data_dir: Path | None = None) -> Path:
    return runtime_dir(data_dir) / current_target().exe


def receipt_path(data_dir: Path | None = None) -> Path:
    return runtime_dir(data_dir) / "receipt.json"


def status(data_dir: Path | None = None) -> dict[str, object]:
    """Lightweight readiness report (no process is spawned)."""
    target = current_target()
    exe = runtime_dir(data_dir) / target.exe
    receipt_file = receipt_path(data_dir)
    receipt: dict[str, object] = {}
    try:
        receipt = json.loads(receipt_file.read_text())
    except Exception:
        receipt = {}
    ready = exe.exists() and receipt.get("sha256") == target.sha256
    return {
        "id": CUA_DRIVER_ID,
        "version": CUA_DRIVER_VERSION,
        "tag": CUA_DRIVER_TAG,
        "target": target.label,
        "asset": target.asset,
        "path": str(exe),
        "ready": bool(ready),
        "receipt": receipt,
        "license": CUA_DRIVER_LICENSE,
    }


def download(
    data_dir: Path | None = None,
    progress=None,  # type: ignore[no-untyped-def]
) -> Path:
    """Download, SHA-256 verify, and extract the pinned binary; write receipt."""
    target = current_target()
    dest_dir = runtime_dir(data_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"{CUA_DRIVER_BASE}/{target.asset}"
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(target.asset).suffix) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, tmp_path.open("wb") as out:
            total = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                if progress is not None:
                    try:
                        progress(total, target.bytes)
                    except Exception:
                        pass
        if digest.hexdigest() != target.sha256:
            raise ValueError(
                f"SHA-256 mismatch for {target.asset}: "
                f"got {digest.hexdigest()}, want {target.sha256}"
            )
        if target.asset.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as zf:
                names = [n for n in zf.namelist() if Path(n).name == target.exe]
                if not names:
                    raise ValueError(f"{target.exe} not found in {target.asset}")
                with zf.open(names[0]) as src, (dest_dir / target.exe).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(tmp_path, "r:gz") as tf:
                member = next(
                    (m for m in tf.getmembers() if Path(m.name).name == target.exe and m.isfile()),
                    None,
                )
                if member is None:
                    raise ValueError(f"{target.exe} not found in {target.asset}")
                member.name = target.exe  # strip any leading directories
                tf.extract(member, dest_dir, filter="data")
        exe = dest_dir / target.exe
        exe.chmod(0o755)
        receipt_path(data_dir).write_text(json.dumps({
            "id": CUA_DRIVER_ID,
            "version": CUA_DRIVER_VERSION,
            "tag": CUA_DRIVER_TAG,
            "target": target.label,
            "asset": target.asset,
            "sha256": target.sha256,
            "bytes": target.bytes,
        }, indent=2) + "\n")
        return exe
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
