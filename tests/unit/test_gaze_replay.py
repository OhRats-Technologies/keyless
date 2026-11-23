"""Deterministic replay and session-level splits."""

from __future__ import annotations

import json

import pytest

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.collect import SessionCollector
from keyless.gaze.replay import (
    SchemaError,
    iter_sessions,
    load_session,
    split_sessions,
)
from keyless.gaze.schema import make_event


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    resolved = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    resolved.ensure_directories()
    return resolved


def _session(root, session_id: str, events: list[dict]) -> None:
    directory = root / session_id
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(
        json.dumps({"schema_version": 0, "session_id": session_id}), encoding="utf-8"
    )
    body = "".join(json.dumps(event) + "\n" for event in events)
    (directory / "events.jsonl").write_text(body, encoding="utf-8")


def test_round_trip_sorts_events_by_timestamp(paths: AppPaths) -> None:
    collector = SessionCollector(paths, session_id="ordered")
    payloads = [
        make_event("ordered", 3_000_000_000, prediction={"top": None, "p": 0.0}),
        make_event("ordered", 1_000_000_000, prediction={"top": None, "p": 0.0}),
        make_event("ordered", 2_000_000_000, prediction={"top": None, "p": 0.0}),
    ]
    for payload in payloads:
        collector.record(payload)
    collector.close()

    session = load_session(collector.session_dir)
    assert session.session_id == "ordered"
    assert session.meta["schema_version"] == 0
    assert [event["ts_ns"] for event in session.events] == [1_000_000_000, 2_000_000_000, 3_000_000_000]
    assert session.events == sorted(payloads, key=lambda event: event["ts_ns"])


def test_equal_timestamps_keep_recorded_order(paths: AppPaths) -> None:
    collector = SessionCollector(paths, session_id="ties")
    for value in (1.0, 2.0, 3.0):
        collector.record(make_event("ties", 5, prediction={"top": None, "entropy": value}))
    collector.close()
    session = load_session(collector.session_dir)
    assert [event["prediction"]["entropy"] for event in session.events] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize(
    "bad_event",
    [
        {"session_id": "x", "ts_ns": "later"},
        {"ts_ns": 1, "prediction": {"top": None}},
        {"session_id": "x", "ts_ns": 1},
    ],
    ids=["bad-ts", "no-session", "no-sections"],
)
def test_corrupt_event_raises_schema_error(tmp_path, bad_event: dict) -> None:
    root = tmp_path / "sessions"
    _session(root, "corrupt", [bad_event])
    with pytest.raises(SchemaError, match="event 0"):
        load_session(root / "corrupt")


def test_invalid_json_line_raises_schema_error(tmp_path) -> None:
    root = tmp_path / "sessions"
    _session(root, "broken", [])
    (root / "broken" / "events.jsonl").write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="event 0: invalid JSON"):
        load_session(root / "broken")


def test_missing_files_are_reported(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="session directory not found"):
        load_session(tmp_path / "absent")

    directory = tmp_path / "sessions" / "half"
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="meta.json"):
        load_session(directory)

    (directory / "meta.json").write_text(json.dumps({"session_id": "half"}), encoding="utf-8")
    (directory / "events.jsonl").unlink()
    with pytest.raises(FileNotFoundError, match="events.jsonl"):
        load_session(directory)


def test_iter_sessions_finds_only_complete_sessions(paths: AppPaths) -> None:
    first = SessionCollector(paths, session_id="b-session")
    second = SessionCollector(paths, session_id="a-session")
    first.close()
    second.close()

    stray = paths.data_dir / "sessions" / "not-a-session"
    stray.mkdir()
    (stray / "meta.json").write_text("{}", encoding="utf-8")

    found = iter_sessions(paths.data_dir / "sessions")
    assert [directory.name for directory in found] == ["a-session", "b-session"]
    assert iter_sessions(paths.data_dir / "missing") == []


def test_splits_are_disjoint_complete_and_deterministic() -> None:
    ids = [f"session-{index:03d}" for index in range(100)]
    splits = [split_sessions(ids, seed=seed) for seed in range(5)]

    for split in splits:
        assert set(split) == {"train", "val", "test"}
        assert [len(split[name]) for name in ("train", "val", "test")] == [60, 20, 20]

        union = split["train"] + split["val"] + split["test"]
        assert sorted(union) == sorted(ids)
        assert len(union) == len(set(union))
        assert not set(split["train"]) & set(split["val"])
        assert not set(split["train"]) & set(split["test"])
        assert not set(split["val"]) & set(split["test"])

    assert split_sessions(ids, seed=0) == splits[0]
    assert split_sessions(list(reversed(ids)), seed=0) == splits[0]
    for earlier, later in zip(splits, splits[1:]):
        assert earlier != later


def test_split_rounding_uses_floor_and_remainder() -> None:
    ids = [f"s{index}" for index in range(7)]
    split = split_sessions(ids, train=0.6, val=0.2, test=0.2, seed=3)
    assert [len(split[name]) for name in ("train", "val", "test")] == [4, 1, 2]
    assert split_sessions([], seed=0) == {"train": [], "val": [], "test": []}


def test_split_rejects_bad_fractions_and_duplicates() -> None:
    ids = [f"s{index}" for index in range(10)]
    with pytest.raises(ValueError, match="sum to 1.0"):
        split_sessions(ids, train=0.5, val=0.5, test=0.5)
    with pytest.raises(ValueError, match="sum to 1.0"):
        split_sessions(ids, train=0.5, val=0.2, test=0.2)
    with pytest.raises(ValueError, match="non-negative"):
        split_sessions(ids, train=-0.1, val=0.6, test=0.5)
    with pytest.raises(ValueError, match="unique"):
        split_sessions(["a", "a"], seed=0)
