"""Headless Qt fixtures. Set the offscreen platform before any PySide6 import."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


def pytest_collection_modifyitems(items) -> None:
    """Mark every test under tests/ui so the default suite skips Qt rendering."""
    for item in items:
        parts = Path(str(item.path)).parts
        if "tests" in parts and "ui" in parts:
            item.add_marker(pytest.mark.ui)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from keyless.ui.theme.tokens import load_bundled_fonts

    load_bundled_fonts()
    app.setApplicationName("Keyless-Tests")
    yield app
    app.processEvents()
