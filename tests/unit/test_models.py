"""Unit tests for the model catalog, resumable downloader, store, and STT engines."""

from __future__ import annotations

import hashlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from keyless.config.paths import HOME_ENV, default_paths
from keyless.models.catalog import (
    PARAKEET_TDT_0_6B_V2,
    ModelAsset,
    ModelFile,
    get_asset,
    stt_assets,
)
from keyless.models.downloader import ChecksumMismatch, DownloadError, download_asset
from keyless.models.receipts import ModelReceipt
from keyless.models.store import ModelState, ModelStore
from keyless.voice.stt.base import SttEngine, SttResult, SttStatus
from keyless.voice.stt.factory import SttModelMissing, create_stt_engine, stt_report


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _paths(tmp_path: Path):
    return default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")


class _RangeHandler(BaseHTTPRequestHandler):
    root: Path
    seen_ranges: list[str] = []
    payloads: dict[str, bytes] = {}

    def do_GET(self) -> None:  # noqa: N802
        name = self.path.lstrip("/")
        data = self.payloads.get(name)
        if data is None:
            self.send_error(404)
            return
        range_header = self.headers.get("Range")
        start = 0
        if range_header:
            self.seen_ranges.append(range_header)
            start = int(range_header.split("=")[1].split("-")[0])
        body = data[start:]
        self.send_response(206 if range_header else 200)
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def http_asset(tmp_path: Path):
    files = {
        "alpha.bin": b"alpha-" * 1000,
        "beta.bin": b"beta-" * 2000,
    }
    asset = ModelAsset(
        id="test-asset",
        display_name="Test asset",
        engine="test",
        quantization="none",
        revision="deadbeef",
        repo="example/test",
        license="CC0",
        attribution="test",
        files=tuple(
            ModelFile(name=name, bytes=len(data), sha256=_sha(data)) for name, data in files.items()
        ),
    )
    handler = type(
        "Handler",
        (_RangeHandler,),
        {"root": tmp_path, "seen_ranges": [], "payloads": files},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield asset, files, base, handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_catalog_pins_revision_and_checksums() -> None:
    asset = get_asset("parakeet-tdt-0.6b-v2")
    assert asset.revision != "main"
    assert len(asset.revision) == 40
    assert asset.license == "CC-BY-4.0"
    names = {f.name for f in asset.files}
    assert {"config.json", "vocab.txt", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"} <= names
    for item in asset.files:
        assert len(item.sha256) == 64
        assert item.bytes > 0
        assert item.sha256 == item.sha256.lower()
    assert stt_assets() == [PARAKEET_TDT_0_6B_V2]


def test_download_verifies_promotes_and_writes_receipt(http_asset, tmp_path: Path) -> None:
    asset, files, base, handler = http_asset
    progress = []
    receipt = download_asset(
        asset,
        paths=_paths(tmp_path),
        progress=progress.append,
        url_for=lambda item: f"{base}/{item.name}",
    )

    store = ModelStore(_paths(tmp_path))
    status = store.status(asset, verify_hashes=True)
    assert status.state is ModelState.READY
    assert (store.asset_dir(asset) / "alpha.bin").read_bytes() == files["alpha.bin"]
    assert receipt.revision == asset.revision
    assert receipt.files["alpha.bin"].sha256 == _sha(files["alpha.bin"])
    assert progress[-1].percent == 100.0
    assert store.receipt_path(asset).exists()
    assert not list(store.asset_dir(asset).glob("*.partial"))


def test_download_resumes_from_partial_with_range(http_asset, tmp_path: Path) -> None:
    asset, files, base, handler = http_asset
    store = ModelStore(_paths(tmp_path))
    store.asset_dir(asset).mkdir(parents=True, exist_ok=True)
    partial = store.partial_path(asset, "alpha.bin")
    partial.write_bytes(files["alpha.bin"][:100])

    download_asset(
        asset,
        store=store,
        url_for=lambda item: f"{base}/{item.name}",
    )

    assert any(value.startswith("bytes=100-") for value in handler.seen_ranges)
    assert (store.asset_dir(asset) / "alpha.bin").read_bytes() == files["alpha.bin"]
    status = store.status(asset, verify_hashes=True)
    assert status.state is ModelState.READY


def test_download_rejects_bad_checksum_and_does_not_promote(tmp_path: Path) -> None:
    payload = b"not-the-pinned-bytes"
    bad = ModelAsset(
        id="bad-asset",
        display_name="Bad",
        engine="test",
        quantization="none",
        revision="deadbeef",
        repo="example/test",
        license="CC0",
        attribution="test",
        files=(ModelFile("bad.bin", len(payload), "0" * 64),),
    )
    handler = type("Handler", (_RangeHandler,), {"payloads": {"bad.bin": payload}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(ChecksumMismatch):
            download_asset(bad, paths=_paths(tmp_path), url_for=lambda item: f"{base}/{item.name}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    store = ModelStore(_paths(tmp_path))
    assert not (store.asset_dir(bad) / "bad.bin").exists()
    assert not store.receipt_path(bad).exists()


def test_store_reports_partial_and_detects_corruption(tmp_path: Path) -> None:
    payload = b"0123456789"
    asset = ModelAsset(
        id="tiny",
        display_name="Tiny",
        engine="test",
        quantization="none",
        revision="rev",
        repo="example/test",
        license="CC0",
        attribution="test",
        files=(ModelFile("tiny.bin", len(payload), _sha(payload)),),
    )
    paths = _paths(tmp_path)
    store = ModelStore(paths)
    store.asset_dir(asset).mkdir(parents=True, exist_ok=True)
    store.partial_path(asset, "tiny.bin").write_bytes(b"01")
    assert store.status(asset).state is ModelState.PARTIAL

    store.partial_path(asset, "tiny.bin").unlink()
    (store.asset_dir(asset) / "tiny.bin").write_bytes(payload)
    receipt = ModelReceipt.for_asset(
        asset, [type("R", (), {"name": "tiny.bin", "bytes": len(payload), "sha256": _sha(payload)})()]
    )
    receipt.write_atomic(store.receipt_path(asset))
    assert store.status(asset, verify_hashes=True).state is ModelState.READY

    (store.asset_dir(asset) / "tiny.bin").write_bytes(b"X" * len(payload))
    assert store.status(asset, verify_hashes=False).state is ModelState.READY
    assert store.status(asset, verify_hashes=True).state is ModelState.CORRUPT


def test_parakeet_uses_local_int8_files_and_cpu_provider(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, Any] = {}

    class FakeAdapter:
        def recognize(self, audio: np.ndarray, *, sample_rate: int = 16000) -> str:
            calls["audio_len"] = len(audio)
            calls["sample_rate"] = sample_rate
            return "scroll down"

    def fake_load_model(name: str, path: str, quantization: str, providers: list[str]):
        calls.update(name=name, path=path, quantization=quantization, providers=providers)
        return FakeAdapter()

    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=fake_load_model))
    from keyless.voice.stt.parakeet import ParakeetSttEngine

    engine = ParakeetSttEngine(tmp_path, quantization="int8")
    result = engine.transcribe_detailed(np.ones(16000, dtype=np.float32) * 0.1)

    assert calls["name"] == "nemo-parakeet-tdt-0.6b-v2"
    assert calls["path"] == str(tmp_path)
    assert calls["quantization"] == "int8"
    assert calls["providers"] == ["CPUExecutionProvider"]
    assert calls["sample_rate"] == 16000
    assert result.text == "scroll down"
    assert engine.device == "cpu"


def test_factory_requires_the_downloaded_parakeet_model(tmp_path: Path) -> None:
    from keyless.config import AppConfig

    cfg = AppConfig()
    with pytest.raises(SttModelMissing, match="models download stt"):
        create_stt_engine(cfg, paths=_paths(tmp_path))

    report = stt_report(cfg, paths=_paths(tmp_path))
    assert report["requested"] == "parakeet"
    assert report["selected"] == "missing"
    assert report["parakeet"]["state"] == "missing"


def test_stt_engine_protocol_accepts_the_flat_engine() -> None:
    class Flat:
        name = "flat"

        def transcribe(self, audio: np.ndarray) -> str:
            return ""

        def transcribe_detailed(self, audio: np.ndarray) -> SttResult:
            return SttResult("")

        def status(self) -> SttStatus:
            return SttStatus("flat", True, "cpu", "int8", "flat")

        def close(self) -> None:
            pass

    assert isinstance(Flat(), SttEngine)


def test_active_listener_uses_the_stt_engine_contract() -> None:
    from keyless.audio.active_listener import ActiveVoiceListener

    class FakeEngine:
        name = "fake"
        device = "cpu"
        compute_type = "int8"
        closed = False

        def transcribe(self, audio: np.ndarray) -> str:
            return "Click send."

        def transcribe_detailed(self, audio: np.ndarray) -> SttResult:
            return SttResult("Click send.")

        def status(self) -> SttStatus:
            return SttStatus("fake", True, "cpu", "int8", "fake")

        def close(self) -> None:
            self.closed = True

    engine = FakeEngine()
    listener = ActiveVoiceListener(
        is_user_attending=lambda: True,
        require_command_prefix=True,
        stt_engine=engine,
    )
    listener._transcribe_and_filter(np.ones(16000, dtype=np.float32) * 0.1, 1.0)
    event = listener.events.get_nowait()
    assert event.transcript == "Click send."
    assert event.confidence == 1.0
    listener.stop()
    assert engine.closed
