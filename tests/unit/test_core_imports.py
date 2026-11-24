"""Boundary guard: engine/core/ipc modules never import GUI toolkits or web assets."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("PySide6", "keyless.ui", "keyless.web")


def _imports_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_core_and_gaze_never_import_gui_or_web() -> None:
    for folder in (ROOT / "src" / "keyless" / "core", ROOT / "src" / "keyless" / "gaze"):
        for py in sorted(folder.rglob("*.py")):
            for mod in _imports_in(py):
                for bad in FORBIDDEN:
                    assert mod != bad and not mod.startswith(bad + "."), (
                        f"{py.relative_to(ROOT)} imports forbidden module {mod!r}"
                    )


def test_all_internal_keyless_imports_resolve() -> None:
    import importlib

    errors: list[str] = []
    for py in sorted((ROOT / "src" / "keyless").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("keyless"):
                if node.module.startswith(("keyless.platform.macos", "keyless.platform.windows")):
                    continue
                mod = importlib.import_module(node.module)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if not hasattr(mod, alias.name):
                        try:
                            importlib.import_module(f"{node.module}.{alias.name}")
                        except Exception:
                            errors.append(
                                f"{py.relative_to(ROOT)}:{node.lineno}: missing {alias.name!r} in {node.module!r}"
                            )
    assert errors == [], "\n".join(errors)
