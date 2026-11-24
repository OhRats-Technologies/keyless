"""Structural parity between the two renderers (tasks 6.3 and 7.3).

Both shells consume the same schema-v1 fixture. The Swift toolchain is not
available on every dev box, so Swift-side parity is checked against the
committed sources: the row-kind enum must equal the engine's closed set, and
``Rows.swift`` must dispatch on ``row.kind`` only. That kind-only dispatch is
what lets a row added in the engine contract reach both shells with zero shell
edits (task 7.3); the widget-level half of that check lives in
``tests/ui/test_ui_contract_render.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from keyless.ui.contract import ROW_KINDS

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "ui"
SWIFT_DIR = REPO / "packaging" / "macos" / "Keyless"

ROWS_SOURCE = (SWIFT_DIR / "Rows.swift").read_text()
MODELS_SOURCE = (SWIFT_DIR / "ContractModels.swift").read_text()


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _swift_enum_cases(source: str, enum_name: str) -> list[str]:
    body = re.search(rf"enum {enum_name}[^{{]*\{{(.*?)\n\}}", source, re.S)
    assert body is not None, enum_name
    cases: list[str] = []
    for line in body.group(1).splitlines():
        line = line.strip()
        if line.startswith("case "):
            cases.extend(part.strip() for part in line[5:].split(","))
    return cases


def test_swift_decodes_exactly_the_engine_row_kinds() -> None:
    assert set(_swift_enum_cases(MODELS_SOURCE, "ContractRowKind")) == set(ROW_KINDS)


def test_swift_rows_dispatch_on_kind_only() -> None:
    """Row widgets come from the kind switch, never from titles or screens."""
    blocks = re.findall(r"switch row\.kind \{(.*?)\n    \}", ROWS_SOURCE, re.S)
    assert blocks, "Rows.swift must switch on row.kind"
    handled: set[str] = set()
    for block in blocks:
        handled |= {case for case in re.findall(r"case \.(\w+)", block)}
    assert handled == set(ROW_KINDS)
    for screen in ("home", "settings", "activity", "setup"):
        assert f'"{screen}"' not in ROWS_SOURCE, screen


def test_both_renderers_cover_every_row_in_the_shared_fixtures() -> None:
    swift_kinds = set(_swift_enum_cases(MODELS_SOURCE, "ContractRowKind"))
    swift_widgets = set(re.findall(r"case \.(\w+)", ROWS_SOURCE))
    for name in ("contract_full.json", "contract_cold.json"):
        for screen in _fixture(name)["screens"].values():
            for section in screen["sections"]:
                for row in section["rows"]:
                    kind = row["kind"]
                    assert kind in ROW_KINDS, (name, kind)
                    assert kind in swift_kinds, (name, kind)
                    assert kind in swift_widgets, (name, kind)


def test_screens_and_rows_are_not_hardcoded_in_either_shell() -> None:
    """A contract change needs no shell edit: screens render generic sections."""
    screens_source = (SWIFT_DIR / "ContentView.swift").read_text()
    for view in ("HomeView", "ActivityView", "SettingsView"):
        body = re.search(rf"struct {view}: View \{{(.*?)\n\}}", screens_source, re.S)
        assert body is not None, view
        assert "SectionListView" in body.group(1), view
    for title in ("Start listening", "Camera setup", "No activity yet"):
        assert f'Text("{title}")' not in screens_source, title
