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
