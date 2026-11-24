"""Exactly one process may draw the eye cursor (AGENTS rule 4)."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")


def test_second_claim_fails_until_the_owner_releases(tmp_path) -> None:
    from keyless.platform.macos.gaze_cursor import claim_eye_cursor, eye_cursor_owned_elsewhere

    owner = claim_eye_cursor(tmp_path)
    assert owner is not None
    assert claim_eye_cursor(tmp_path) is None
    assert eye_cursor_owned_elsewhere(tmp_path) is True
    owner.close()
    assert eye_cursor_owned_elsewhere(tmp_path) is False
