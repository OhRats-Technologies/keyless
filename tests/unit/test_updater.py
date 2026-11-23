"""Unit tests for t3code-style Keyless Updater."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx

from keyless.config.paths import HOME_ENV, default_paths
from keyless.config.schema import AppConfig
from keyless.updater import (
    Updater,
    clean_release_notes,
    compare_semver,
    detect_install_mode,
    is_prerelease_version,
)


def test_semver_comparison_and_prerelease_detection() -> None:
    assert compare_semver("v0.1.1", "0.1.0") > 0
    assert compare_semver("0.2.0-nightly.20261120.1", "0.2.0") < 0
    assert compare_semver("0.2.0", "0.2.0") == 0
    assert is_prerelease_version("0.2.0-rc.1") is True
    assert is_prerelease_version("0.1.1") is False


def test_clean_release_notes_filters_changelog_noise() -> None:
    notes = """
## What's Changed
* **Translucent calibration** overlay over desktop (#12)
* Local [Underdog Woof](https://hf.co/...) TTS answers (#13)
* Full Changelog: https://github.com/OhRats-Technologies/keyless/compare/v0.1.0...v0.1.1
"""
    cleaned = clean_release_notes(notes)
    assert len(cleaned) == 2
    assert cleaned[0] == "Translucent calibration overlay over desktop"
    assert cleaned[1] == "Local Underdog Woof TTS answers"


def test_updater_checks_channels_and_verifies_download(tmp_path) -> None:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    payload_bytes = b"fake-keyless-appimage-binary"
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()

    releases = [
        {
            "tag_name": "v9.9.0-rc.1",
            "prerelease": True,
            "draft": False,
            "html_url": "https://github.com/OhRats-Technologies/keyless/releases/tag/v9.9.0-rc.1",
            "body": "* Preview feature",
            "assets": [
                {
                    "name": "keyless_amd64.deb",
                    "browser_download_url": "https://dl.example/pre.deb",
                }
            ],
        },
        {
            "tag_name": "v9.8.0",
            "prerelease": False,
            "draft": False,
            "html_url": "https://github.com/OhRats-Technologies/keyless/releases/tag/v9.8.0",
            "body": "* Stable release item",
            "assets": [
                {
                    "name": "keyless_amd64.deb",
                    "browser_download_url": "https://dl.example/keyless_amd64.deb",
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": "https://dl.example/SHA256SUMS",
                },
            ],
        },
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/releases" in url:
            return httpx.Response(200, json=releases)
        if url.endswith("SHA256SUMS"):
            return httpx.Response(200, text=f"{payload_sha}  keyless_amd64.deb\n")
        if url.endswith("keyless_amd64.deb"):
            return httpx.Response(
                200,
                content=payload_bytes,
                headers={"Content-Length": str(len(payload_bytes))},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    updater = Updater(
        paths=paths,
        channel="stable",
        repo_root=tmp_path,
        client_factory=lambda: httpx.Client(transport=transport),
    )

    state = updater.check_for_updates(channel="stable")
    assert state.status == "available"
    assert state.available_version == "9.8.0"
    assert state.asset_name == "keyless_amd64.deb"
    assert state.asset_sha256 == payload_sha
    assert state.release_notes == ["Stable release item"]

    # Prerelease channel picks 9.9.0-rc.1
    pre_state = updater.check_for_updates(channel="prerelease")
    assert pre_state.available_version == "9.9.0-rc.1"

    # Verify verified download works on stable
    updater.check_for_updates(channel="stable")
    downloaded = updater.download_update()
    assert downloaded.exists()
    assert downloaded.read_bytes() == payload_bytes
    assert updater.state.status == "downloaded"

    # Poll interval gating
    cfg = AppConfig(auto_update_checks=True)
    assert updater.should_auto_check(cfg, now=datetime.now(timezone.utc)) is False
    assert updater.should_auto_check(cfg, now=datetime.now(timezone.utc) + timedelta(hours=5)) is True


def test_detect_install_mode_identifies_source_git(tmp_path, monkeypatch) -> None:
    # Detection is environment-sensitive (APPIMAGE wins); keep the test hermetic
    # so a desktop session that exports APPIMAGE cannot flip the result.
    monkeypatch.delenv("APPIMAGE", raising=False)
    (tmp_path / ".git").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    mode, root = detect_install_mode(tmp_path)
    assert mode == "source_git"
    assert root == tmp_path
