"""design-tokens.json is the single source of truth for theme values."""

from __future__ import annotations

import json
from dataclasses import fields

from keyless.ui.theme.tokens import (
    DARK,
    DESIGN_TOKENS,
    LIGHT,
    TOKENS_PATH,
    ThemeName,
    ThemeTokens,
    stylesheet,
    tokens_for,
)

COLOR_FIELDS = (
    "background",
    "surface",
    "surface_alt",
    "sidebar",
    "text",
    "muted",
    "accent",
    "accent_text",
    "border",
    "danger",
    "success",
    "warning",
    "selected",
)


def _raw() -> dict:
    return json.loads(TOKENS_PATH.read_text())


def test_design_tokens_parse_and_are_versioned() -> None:
    raw = _raw()
    assert raw["schema_version"] == 1
    assert raw is not None
    assert DESIGN_TOKENS["schema_version"] == 1
    assert set(raw["themes"]) == {"light", "dark"}


def test_every_color_role_exists_in_both_themes() -> None:
    raw = _raw()
    for theme in ("light", "dark"):
        palette = raw["themes"][theme]
        for role in COLOR_FIELDS:
            assert role in palette, f"{theme} is missing {role}"
            assert isinstance(palette[role], str) and palette[role].startswith("#")


def test_tokens_match_json_values() -> None:
    raw = _raw()
    for name, tokens in (("light", LIGHT), ("dark", DARK)):
        palette = raw["themes"][name]
        for role in COLOR_FIELDS:
            assert getattr(tokens, role) == palette[role], f"{name}.{role} drifted"
    typography = raw["typography"]
    assert LIGHT.font_family == typography["font_family"]
    assert LIGHT.mono_family == typography["mono_family"]
    assert LIGHT.font_size == typography["sizes"]["body"]
    assert LIGHT.title_size == typography["sizes"]["title"]
    assert LIGHT.heading_size == typography["sizes"]["heading"]
    assert DARK.font_size == typography["sizes"]["body"]
    spacing = raw["spacing"]
    assert (LIGHT.spacing_xs, LIGHT.spacing_sm, LIGHT.spacing_md, LIGHT.spacing_lg) == (
        spacing["xs"],
        spacing["sm"],
        spacing["md"],
        spacing["lg"],
    )
    radii = raw["radii"]
    assert LIGHT.radius == radii["default"]
    assert LIGHT.radius_sm == radii["small"]
    assert LIGHT.pill_height == radii["pill"] * 2


def test_theme_tokens_api_is_unchanged() -> None:
    names = {field.name for field in fields(ThemeTokens)}
    assert {
        "name",
        *COLOR_FIELDS,
        "spacing_xs",
        "spacing_sm",
        "spacing_md",
        "spacing_lg",
        "radius",
        "radius_sm",
        "font_family",
        "mono_family",
        "font_size",
        "title_size",
        "heading_size",
        "pill_height",
    } == names
    assert tokens_for("light") is LIGHT
    assert tokens_for(ThemeName.DARK) is DARK


def test_stylesheet_renders_for_both_themes_with_telemetry_rule() -> None:
    for tokens in (LIGHT, DARK):
        qss = stylesheet(tokens)
        assert "$" not in qss, "unsubstituted template variable"
        assert tokens.background in qss
        assert "QLabel#Telemetry" in qss
        assert tokens.mono_family in qss
