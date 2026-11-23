"""Transcript file shared with the screen overlay."""

from __future__ import annotations

from keyless.ipc.transcript import read_transcript, write_transcript


def test_write_transcript_is_atomic_and_increments_sequence(tmp_path) -> None:
    path = tmp_path / "transcript.json"
    assert read_transcript(path) is None
    first = write_transcript(path, "  click   send  ")
    assert first is not None and first.text == "click send" and first.seq == 1
    second = write_transcript(path, "scroll down")
    assert second is not None and second.seq == 2
    loaded = read_transcript(path)
    assert loaded is not None
    assert loaded.text == "scroll down"
    assert loaded.seq == 2
    assert loaded.source == "parakeet"
    assert loaded.final is True


def test_provisional_transcript_is_marked_not_final(tmp_path) -> None:
    path = tmp_path / "transcript.json"
    state = write_transcript(path, "so the plan", final=False)
    assert state is not None and state.final is False
    loaded = read_transcript(path)
    assert loaded is not None and loaded.final is False and loaded.text == "so the plan"


def test_write_transcript_ignores_empty_text(tmp_path) -> None:
    assert write_transcript(tmp_path / "transcript.json", "   ") is None


def test_level_file_round_trips_and_goes_stale(tmp_path) -> None:
    from keyless.ipc.transcript import level_path_for, read_level, write_level

    transcript = tmp_path / "transcript.json"
    level = level_path_for(transcript)
    assert level == tmp_path / "level.json"
    assert read_level(level) is None

    write_level(level, 0.25)
    fresh = read_level(level)
    assert fresh is not None and fresh.rms == 0.25
    assert fresh.age_s < 1.0

    stale = type(fresh)(rms=0.25, timestamp_s=fresh.timestamp_s - 5.0)
    assert stale.age_s >= 5.0


def test_level_write_survives_missing_directory(tmp_path) -> None:
    from keyless.ipc.transcript import write_level

    write_level(tmp_path / "missing" / "level.json", 0.1)
    assert (tmp_path / "missing" / "level.json").exists()
