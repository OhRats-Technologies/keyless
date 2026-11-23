"""`keyless gaze replay` reads a session through the real CLI entrypoint."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.collect import SessionCollector
from keyless.gaze.schema import make_event
from keyless.main import app


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    resolved = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    resolved.ensure_directories()
    return resolved


@pytest.fixture
def session_dir(paths: AppPaths):
    collector = SessionCollector(paths, session_id="cli-session")
    collector.record(
        make_event(
            "cli-session",
            1_000_000_000,
            screen={
                "display_id": 1,
                "app": "Chrome",
                "window_id": 7,
                "screenshot": "a" * 64,
                "candidates": [
                    {"id": "c1", "bbox": [0.0, 0.0, 20.0, 20.0], "role": "button", "score": 0.9},
                    {"id": "c2", "bbox": [30.0, 0.0, 20.0, 20.0], "role": "button", "score": 0.2},
                ],
            },
            prediction={"top": "c1", "p": 0.9, "entropy": 0.3, "pixel": [10.0, 10.0]},
            interaction={
                "kind": "click",
                "mouse": [11.0, 10.0],
                "target": "c1",
                "accepted": True,
                "latency_ms": 90,
            },
        )
    )
    collector.record(
        make_event(
            "cli-session",
            2_000_000_000,
            interaction={"kind": "click", "mouse": [40.0, 40.0], "target": None, "accepted": False},
        )
    )
    collector.close()
    return collector.session_dir


def _run(*args: str):
    return CliRunner().invoke(app, list(args))


def test_replay_prints_metrics(session_dir) -> None:
    result = _run("gaze", "replay", str(session_dir))
    assert result.exit_code == 0, result.output

    output = result.output
    assert "cli-session" in output
    assert "2 events" in output
    for label in (
        "candidate_recall@3",
        "top1",
        "top3",
        "hit@25",
        "hit@50",
        "hit@100",
        "misclicks/h",
        "abstention",
        "actions/correction",
        "baseline_top1",
    ):
        assert label in output


def test_replay_reports_corrupt_session(session_dir) -> None:
    (session_dir / "events.jsonl").write_text(
        json.dumps({"session_id": "cli-session", "ts_ns": "yesterday"}) + "\n",
        encoding="utf-8",
    )
    result = _run("gaze", "replay", str(session_dir))
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "ts_ns" in result.output


def test_replay_rejects_missing_directory(tmp_path) -> None:
    result = _run("gaze", "replay", str(tmp_path / "absent"))
    assert result.exit_code != 0


def test_gaze_group_is_registered() -> None:
    result = _run("gaze", "--help")
    assert result.exit_code == 0
    assert "replay" in result.output
