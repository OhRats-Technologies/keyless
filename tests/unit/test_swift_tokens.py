"""``Tokens.swift`` must stay a faithful projection of ``design-tokens.json``.

The Swift toolchain is not available on every dev box, so parity is checked in
Python: regenerate in memory, compare against the committed file, and verify
every emitted RGB triple against the JSON hex palette.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from gen_swift_tokens import OUTPUT_PATH, TOKENS_PATH, render  # noqa: E402

_COLOR_RE = re.compile(
    r"static let (\w+) = Color\(red: (\d+) / 255\.0, green: (\d+) / 255\.0, "
    r"blue: (\d+) / 255\.0\)  // (#[0-9a-f]{6})"
)


def _tokens() -> dict:
    import json

    return json.loads(TOKENS_PATH.read_text())


def test_committed_swift_matches_generated() -> None:
    assert OUTPUT_PATH.read_text() == render(_tokens())


def test_every_palette_color_matches_json() -> None:
    tokens = _tokens()
    text = OUTPUT_PATH.read_text()
    matches = list(_COLOR_RE.finditer(text))
    by_hex = {m.group(5): m for m in matches}
    seen: set[str] = set()
    for theme in ("light", "dark"):
        for key, hex_value in tokens["themes"][theme].items():
            assert hex_value in by_hex, (theme, key)
        seen |= set(tokens["themes"][theme])
    for match in matches:
        r, g, b = (int(match.group(i)) for i in (2, 3, 4))
        hex_value = match.group(5)
        rr, gg, bb = (int(hex_value[i : i + 2], 16) for i in (1, 3, 5))
        assert (r, g, b) == (rr, gg, bb)
    assert seen == {"background", "surface", "surface_alt", "sidebar", "text", "muted",
                    "accent", "accent_text", "border", "danger", "success", "warning",
                    "selected"}


def test_spacing_radii_and_type_constants_present() -> None:
    tokens = _tokens()
    text = OUTPUT_PATH.read_text()
    for key, value in tokens["spacing"].items():
        name = "XS" if key == "xs" else key.upper()
        assert f"static let space_{name} = {value}.0" in text
    for key, value in tokens["radii"].items():
        assert f"static let radius_{key.capitalize()} = {value}.0" in text
    for key, value in tokens["typography"]["sizes"].items():
        name = "S" if key == "small" else key.capitalize()
        assert f"static let fontSize_{name} = {value}.0" in text
