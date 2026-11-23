"""Packaging structure and cross-platform capability-stub tests."""

from __future__ import annotations

import configparser
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from keyless.config.paths import HOME_ENV, default_paths
from keyless.platform.base import CapabilityStatus, NotSupportedError, PermissionState
from keyless.platform.capabilities import detect_capabilities
from keyless.platform.macos.pointer import macOSPointerBackend
from keyless.platform.windows.pointer import WindowsPointerBackend

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "packaging" / "linux"


def test_udev_rule_matches_the_developer_helper() -> None:
    rule = (LINUX / "udev" / "99-keyless-uinput.rules").read_text()
    script = (ROOT / "scripts" / "setup_uinput_udev.sh").read_text()
    rule_line = next(line for line in rule.splitlines() if line.startswith("KERNEL"))
    assert 'KERNEL=="uinput"' in rule_line
    assert 'TAG+="uaccess"' in rule_line
    assert rule_line in script


def test_desktop_entry_launches_the_ui() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(LINUX / "keyless.desktop")
    entry = parser["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Exec"] == "/usr/bin/keyless ui"
    assert entry["Terminal"] == "false"
    assert "Accessibility" in entry["Categories"]


def test_systemd_unit_is_optional_and_user_scoped() -> None:
    text = (LINUX / "systemd" / "keyless.service").read_text()
    assert "ExecStart=/usr/bin/keyless run" in text
    assert "WantedBy=graphical-session.target" in text
    assert "system" not in text.lower().replace("systemd", "")


def test_structure_only_deb_contains_rules_desktop_and_unit(tmp_path: Path) -> None:
    output = tmp_path / "keyless.deb"
    result = subprocess.run(
        [
            sys.executable,
            str(LINUX / "build_deb.py"),
            "--no-venv",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.exists()

    members = subprocess.check_output(["ar", "t", str(output)], text=True).split()
    assert members == ["debian-binary", "control.tar.gz", "data.tar.gz"]

    extract = tmp_path / "extract"
    extract.mkdir()
    subprocess.run(["ar", "x", str(output)], cwd=extract, check=True)
    assert (extract / "debian-binary").read_text() == "2.0\n"

    with tarfile.open(extract / "control.tar.gz") as control:
        names = control.getnames()
        assert "./control" in names
        control_text = control.extractfile("./control").read().decode()
    assert "Package: keyless" in control_text
    assert "Architecture: amd64" in control_text

    with tarfile.open(extract / "data.tar.gz") as data:
        names = set(data.getnames())
    assert "./usr/bin/keyless" in names
    assert "./usr/share/applications/keyless.desktop" in names
    assert "./usr/lib/udev/rules.d/99-keyless-uinput.rules" in names
    assert "./usr/lib/systemd/user/keyless.service" in names
    assert "./usr/share/doc/keyless/README.Debian" in names


def test_windows_is_an_honest_capability_stub(tmp_path: Path) -> None:
    report = detect_capabilities(platform_name="win32")
    assert report.status("pointer") is CapabilityStatus.UNSUPPORTED_PLATFORM
    assert report.status("cameras") is CapabilityStatus.UNSUPPORTED_PLATFORM
    assert report.permissions["camera"].state is PermissionState.UNKNOWN
    for backend in (WindowsPointerBackend(),):
        with pytest.raises(NotSupportedError):
            backend.move_absolute(1.0, 2.0)
        with pytest.raises(NotSupportedError):
            backend.click()


def test_macos_probes_real_backends_no_click(tmp_path: Path) -> None:
    report = detect_capabilities(platform_name="darwin")
    assert report.status("pointer") in (
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.NEEDS_PERMISSION,
        CapabilityStatus.UNAVAILABLE,
    )
    assert report.status("swiftui") in (
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.UNAVAILABLE,
    )
    # Never synthesize input in tests: construction is the contract.
    backend = macOSPointerBackend()
    assert hasattr(backend, "move_absolute")
    assert hasattr(backend, "type_text")


def test_paths_for_windows_and_macos_are_already_unit_testable(tmp_path: Path) -> None:
    windows = default_paths(env={"APPDATA": str(tmp_path / "R"), "LOCALAPPDATA": str(tmp_path / "L")}, platform_name="win32")
    macos = default_paths(env={}, platform_name="darwin", home=tmp_path)
    assert windows.config_dir.name == "Keyless"
    assert windows.data_dir != windows.config_dir
    assert macos.config_dir == macos.data_dir
    assert macos.logs_dir.name == "Keyless"
