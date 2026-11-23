"""Live captions overlay: voice meter while listening, subtitles as speech arrives."""

from __future__ import annotations

import json
import time

import pytest

from keyless.ipc.transcript import level_path_for, write_level, write_transcript
from keyless.ui.transcript_overlay import (
    LEVEL_STALE_S,
    TRANSCRIPT_TTL_S,
    TranscriptOverlay,
    meter_position,
)


def test_meter_position_maps_dbfs_range() -> None:
    assert meter_position(0.0) == 0.0
    assert meter_position(1.0) == pytest.approx(1.0)
    assert 0.0 < meter_position(0.02) < 1.0
    assert meter_position(0.02) > meter_position(0.002)


def test_overlay_shows_meter_then_subtitles_and_hides_when_idle(qapp, tmp_path) -> None:
    transcript = tmp_path / "transcript.json"
    overlay = TranscriptOverlay(transcript)
    try:
        assert not overlay.isVisible()

        write_level(level_path_for(transcript), 0.05)
        overlay._poll()
        assert overlay.isVisible()
        assert overlay.meter._value > 0.0
        assert not overlay.text.isVisible()

        write_transcript(transcript, "open the settings", final=False)
        overlay._poll()
        assert overlay.text.isVisible()
        assert overlay.text.text() == "open the settings"
        assert overlay.isVisible()

        overlay._shown_at -= TRANSCRIPT_TTL_S + 1.0
        stale = {"rms": 0.0, "timestamp_s": time.time() - (LEVEL_STALE_S + 5.0)}
        level_path_for(transcript).write_text(json.dumps(stale))
        overlay._poll()
        assert not overlay.isVisible()
    finally:
        overlay.close()
