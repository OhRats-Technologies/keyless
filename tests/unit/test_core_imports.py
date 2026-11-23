"""Import-boundary guard: the IPC domain and core modules stay UI-free."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "keyless"


def _imports_ui(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "keyless.ui" or alias.name.startswith("keyless.ui."):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "keyless.ui" or module.startswith("keyless.ui."):
                offenders.append(module)
    return offenders


def test_control_plane_has_no_ui_imports() -> None:
    offenders = _imports_ui(SRC / "ipc" / "control.py")
    assert offenders == []


def test_core_modules_have_no_ui_imports() -> None:
    for name in ("lifecycle", "onboarding", "history"):
        offenders = _imports_ui(SRC / "core" / f"{name}.py")
        assert offenders == [], f"keyless.core.{name} imports {offenders}"


def test_shims_reexport_canonical_objects() -> None:
    from keyless.core import history, lifecycle, onboarding
    from keyless.ui import session
    from keyless.ui.history import db
    from keyless.ui.onboarding import flow

    assert flow.OnboardingFlow is onboarding.OnboardingFlow
    assert flow.OnboardingStep is onboarding.OnboardingStep
    assert flow.STEPS is onboarding.STEPS
    assert flow.OnboardingState is onboarding.OnboardingState

    assert db.HistoryStore is history.HistoryStore
    assert db.ActionRecord is history.ActionRecord

    assert session.DaemonController is lifecycle.DaemonController
    assert session.NullController is lifecycle.NullController
    assert session.SessionController is lifecycle.SessionController
    assert session.SessionState is lifecycle.SessionState
    assert session.SessionStatus is lifecycle.SessionStatus
    assert session.DaemonFailureReport is lifecycle.DaemonFailureReport
    assert session.inspect_daemon_error is lifecycle.inspect_daemon_error
    assert session._extract_daemon_error is lifecycle._extract_daemon_error
