"""Renderer-level parity: every contract row renders in the Qt shell as-is.

The source-level half of this check lives in ``tests/unit/test_ui_parity.py``.
Here the real Qt row dispatcher is handed the shared fixtures plus one row the
engine does not emit yet, proving a contract-only row reaches the shell with no
shell edit (task 7.3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QWidget

from keyless.ui.components import ContractSections, build_row
from keyless.ui.contract import ROW_KINDS
from keyless.ui.theme.tokens import tokens_for

pytestmark = pytest.mark.ui

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ui"


def _home_sections(name: str) -> list[dict]:
    document = json.loads((FIXTURES / name).read_text())
    return document["screens"]["home"]["sections"]


@pytest.mark.parametrize("fixture_name", ["contract_full.json", "contract_cold.json"])
def test_home_sections_render_every_row(qapp, fixture_name: str) -> None:
    sections = _home_sections(fixture_name)
    rows = [row for section in sections for row in section["rows"]]
    tokens = tokens_for("dark")
    built = [build_row(row, lambda action, payload: None, tokens) for row in rows]
    assert all(widget is not None for widget in built), fixture_name
    assert {row["kind"] for row in rows} <= set(ROW_KINDS)

    view = ContractSections(lambda action, payload: None, lambda: tokens)
    view.update_sections(sections)
    assert len(view.findChildren(QWidget)) > len(rows)


def test_row_added_only_in_the_contract_renders_without_shell_edits(qapp) -> None:
    """Task 7.3: the engine adds a row; the renderer already draws it."""
    injected = {
        "kind": "progress",
        "title": "Syncing vocabulary",
        "fraction": 0.25,
        "indeterminate": False,
    }
    tokens = tokens_for("dark")

    row_widget = build_row(injected, lambda action, payload: None, tokens)
    assert row_widget is not None
    assert "Syncing vocabulary" in [
        label.text() for label in row_widget.findChildren(QLabel)
    ]

    view = ContractSections(lambda action, payload: None, lambda: tokens)
    view.update_sections([{"title": "Progress", "rows": [injected]}])
    assert "Syncing vocabulary" in [label.text() for label in view.findChildren(QLabel)]
