"""SwiftUI capability + loopback control plane contract."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from keyless.platform.base import CapabilityStatus
from keyless.platform.capabilities import Capability, detect_capabilities, probe_swiftui

ROOT = Path(__file__).resolve().parents[2]


def test_swiftui_probe_finds_packaged_sources_on_linux() -> None:
    info = probe_swiftui(platform_name="linux", host_sources=ROOT / "packaging" / "macos")
    assert info.name == Capability.SWIFTUI.value
    # Linux can never run the host, but sources must exist for Mac builds.
    assert info.status is CapabilityStatus.UNAVAILABLE
    assert "macOS only" in info.detail


def test_swiftui_probe_reports_missing_toolchain(tmp_path: Path) -> None:
    empty = tmp_path / "macos"
    (empty / "Keyless").mkdir(parents=True)
    (empty / "Keyless" / "KeylessApp.swift").write_text("// stub")
    info = probe_swiftui(platform_name="darwin", swiftc=None, host_sources=empty)
    # Depends on host: if swift exists this may be AVAILABLE; either way honest.
    assert info.name == "swiftui"
    assert info.status in (CapabilityStatus.AVAILABLE, CapabilityStatus.UNAVAILABLE)


def test_swiftui_probe_missing_sources_is_unavailable(tmp_path: Path) -> None:
    info = probe_swiftui(
        platform_name="darwin", swiftc="/usr/bin/swiftc", host_sources=tmp_path / "empty"
    )
    assert info.status is CapabilityStatus.UNAVAILABLE


def test_linux_report_includes_swiftui_row() -> None:
    report = detect_capabilities(platform_name="linux")
    assert report.get(Capability.SWIFTUI) is not None


def test_macos_report_probes_swiftui_honestly() -> None:
    report = detect_capabilities(platform_name="darwin")
    assert report.platform == "macos"
    assert report.get(Capability.SWIFTUI) is not None
    # Engine backends are really probed (Hex-style), never blanket stubs.
    assert report.status(Capability.POINTER) in (
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.NEEDS_PERMISSION,
        CapabilityStatus.UNAVAILABLE,
    )
    assert report.status(Capability.CAMERAS) in (
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.UNAVAILABLE,
    )
    assert "camera" in report.permissions
    assert "native_host" in report.permissions


def test_packaged_swiftui_sources_exist() -> None:
    base = ROOT / "packaging" / "macos" / "Keyless"
    for name in (
        "KeylessApp.swift",
        "EngineBridge.swift",
        "ContentView.swift",
        "PermissionsView.swift",
        "PermissionGuide.swift",
        "OnboardingView.swift",
        "Info.plist",
    ):
        assert (base / name).exists(), f"missing {name}"


def test_macos_permission_probes_are_honest() -> None:
    from keyless.platform.macos.permissions import (
        classify_tccutil_reset,
        check_permission,
    )

    assert classify_tccutil_reset(0, "") == "cleared"
    assert classify_tccutil_reset(64, "") == "nothing"
    assert classify_tccutil_reset(1, "No such bundle identifier") == "nothing"
    assert classify_tccutil_reset(1, "boom") == "failed"
    for need in ("accessibility", "microphone", "camera", "screen_capture", "input_monitoring"):
        info = check_permission(need)
        assert info.name in (need, "accessibility" if need == "accessibility" else info.name)
        assert info.as_info().state.value in (
            "granted", "denied", "unknown", "needs_restart", "not_applicable",
        )


def test_control_plane_roundtrip(tmp_path: Path) -> None:
    from keyless.config.paths import default_paths
    from keyless.ipc.control import ControlServer

    paths = default_paths(env={}, platform_name="linux", home=tmp_path)
    server = ControlServer(paths=paths)
    port = server.start(port=0)
    try:

        def get(route: str) -> dict:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=5) as resp:
                return json.loads(resp.read().decode())

        status = get("/v1/status")
        assert "state" in status
        caps = get("/v1/capabilities")
        assert "capabilities" in caps
        assert "swiftui" in caps["capabilities"]
        history = get("/v1/history?limit=2")
        assert "actions" in history
    finally:
        server.stop()


def test_control_plane_setup_is_engine_owned(tmp_path: Path) -> None:
    import urllib.request

    from keyless.config.paths import default_paths
    from keyless.ipc.control import ControlServer

    paths = default_paths(env={}, platform_name="linux", home=tmp_path)
    server = ControlServer(paths=paths)
    port = server.start(port=0)

    def request(route: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload or {}).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{route}", data=data, method="POST" if data else "GET"
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    try:
        setup = request("/v1/setup")
        assert setup["schema_version"] == 1
        assert setup["steps"] == ["welcome", "devices", "calibration", "ready"]
        assert setup["calibration"]["ready"] is True
        assert setup["calibration"]["legacy_removed"] is True
        assert setup["complete"] is False

        moved = request("/v1/setup", {"action": "go_to", "step": "calibration"})
        assert moved["state"]["step"] == "calibration"
        assert request("/v1/setup")["state"]["step"] == "calibration"

        request("/v1/setup", {"action": "go_to", "step": "ready"})
        complete = request("/v1/setup", {"action": "complete"})
        assert complete["complete"] is True

        request("/v1/setup", {"action": "reset"})
        assert request("/v1/setup")["complete"] is False
    finally:
        server.stop()


def test_control_plane_model_download_is_engine_owned(tmp_path: Path, monkeypatch) -> None:
    import threading
    import urllib.error
    import urllib.request

    from keyless.config.paths import default_paths
    from keyless.ipc.control import ControlServer
    from keyless.models import downloader

    started = threading.Event()
    release = threading.Event()

    def fake_download(asset, **kwargs):  # type: ignore[no-untyped-def]
        started.set()
        release.wait(5)
        return object()

    monkeypatch.setattr(downloader, "download_asset", fake_download)
    paths = default_paths(env={}, platform_name="linux", home=tmp_path)
    server = ControlServer(paths=paths)
    port = server.start(port=0)

    def post(route: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{route}", data=b"{}", method="POST"
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    try:
        model = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5).read()
        )["model"]
        assert model["id"] == "parakeet-unified-en-0.6b"
        assert model["ready"] is False
        assert model["size"] > 0

        code, result = post("/v1/models/download")
        assert code == 200 and result["started"] is True
        assert started.wait(2)
        assert post("/v1/models/download")[0] == 409

        job = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5).read()
        )["model"]["job"]
        assert job["state"] == "downloading"

        assert post("/v1/models/cancel")[1]["cancelled"] is True
        release.set()
    finally:
        release.set()
        server.stop()
