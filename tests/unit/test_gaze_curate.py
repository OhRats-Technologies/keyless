"""Unit tests for pure-Python Gaze v2 session curation and split export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.collect import SessionCollector
from keyless.gaze.curate import (
    delete_event,
    delete_session,
    evaluate_recall_summary,
    export_curated_split,
    list_session_summaries,
    update_event,
)
from keyless.gaze.replay import load_session
from keyless.gaze.schema import make_event


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    resolved = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    resolved.ensure_directories()
    return resolved


def _write_demo_session(paths: AppPaths, sid: str, *, hits: int = 2, misses: int = 1) -> Path:
    collector = SessionCollector(paths, session_id=sid)
    cands = [
        {"id": "btn-save", "bbox": [10, 10, 80, 30], "role": "button", "text": "Save", "score": 0.9},
        {"id": "btn-cancel", "bbox": [120, 10, 80, 30], "role": "button", "text": "Cancel", "score": 0.8},
    ]
    ts = 1_000_000_000
    for _ in range(hits):
        collector.record(
            make_event(
                sid,
                ts,
                screen={"display_id": 0, "app": "Firefox", "window_id": 1, "candidates": cands},
                prediction={"top": "btn-save", "p": 0.85, "pixel": [45.0, 22.0]},
                interaction={"kind": "click", "mouse": [40.0, 20.0], "target": "btn-save", "accepted": True},
            )
        )
        ts += 1_000_000
    for _ in range(misses):
        collector.record(
            make_event(
                sid,
                ts,
                screen={"display_id": 0, "app": "Code", "window_id": 2, "candidates": cands},
                interaction={"kind": "click", "mouse": [500.0, 400.0], "target": None, "accepted": None},
            )
        )
        ts += 1_000_000
    collector.close()
    return collector.session_dir


def test_summaries_and_recall_gate(paths: AppPaths) -> None:
    _write_demo_session(paths, "sess-a", hits=3, misses=1)
    sessions_dir = paths.data_dir / "sessions"

    summaries = list_session_summaries(sessions_dir)
    assert len(summaries) == 1
    assert summaries[0].click_count == 4
    assert summaries[0].covered_clicks == 3
    assert summaries[0].recall == pytest.approx(0.75)

    recall = evaluate_recall_summary(sessions_dir)
    assert recall.clicks == 4
    assert recall.hits == 3
    assert recall.gate_met is False
    assert [row.app for row in recall.per_app] == ["Code", "Firefox"]


def test_update_and_delete_event_are_atomic_and_validated(paths: AppPaths) -> None:
    sdir = _write_demo_session(paths, "sess-edit", hits=1, misses=1)

    updated = update_event(sdir, 1, accepted=False, target="btn-cancel")
    assert updated["interaction"]["accepted"] is False
    assert updated["interaction"]["target"] == "btn-cancel"

    reloaded = load_session(sdir)
    assert reloaded.events[1]["interaction"]["accepted"] is False
    assert reloaded.events[1]["interaction"]["target"] == "btn-cancel"
    assert "curated_ts_ns" in reloaded.meta

    remaining = delete_event(sdir, 1)
    assert remaining == 1
    assert len(load_session(sdir).events) == 1


def test_delete_session_refuses_path_outside_root(paths: AppPaths, tmp_path: Path) -> None:
    sessions_dir = paths.data_dir / "sessions"
    sdir = _write_demo_session(paths, "sess-del", hits=1, misses=0)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="not inside"):
        delete_session(sessions_dir, outside)

    delete_session(sessions_dir, sdir)
    assert not sdir.exists()


def test_export_curated_split_preserves_session_isolation_and_filters_rejected(
    paths: AppPaths, tmp_path: Path
) -> None:
    s1 = _write_demo_session(paths, "sess-1", hits=2, misses=1)
    _write_demo_session(paths, "sess-2", hits=2, misses=0)
    _write_demo_session(paths, "sess-3", hits=2, misses=0)
    update_event(s1, 2, accepted=False)

    out_dir = tmp_path / "export"
    manifest = export_curated_split(paths.data_dir / "sessions", out_dir, seed=7, accepted_only=True)
    seen: set[str] = set()
    total_exported = 0
    for split_info in manifest["splits"].values():
        sids = set(split_info["sessions"])
        assert seen.isdisjoint(sids)
        seen.update(sids)
        total_exported += split_info["events"]

    assert seen == {"sess-1", "sess-2", "sess-3"}
    # 7 raw events across 3 sessions minus 1 rejected event = 6
    assert total_exported == 6
    assert (out_dir / "manifest.json").exists()
    loaded = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["accepted_only"] is True
