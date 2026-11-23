"""`keyless gaze eval-candidates` recall harness against logged clicks."""

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


def _click_event(session_id: str, ts_ns: int, app: str, mouse, candidates, target=None):
    return make_event(
        session_id,
        ts_ns,
        screen={
            "display_id": 1,
            "app": app,
            "window_id": 3,
            "candidates": candidates,
        },
        interaction={"kind": "click", "mouse": list(mouse), "target": target, "accepted": True},
    )


def _write_session(paths: AppPaths, session_id: str, events) -> None:
    collector = SessionCollector(paths, session_id=session_id)
    for event in events:
        collector.record(event)
    collector.close()


def _run(*args: str):
    return CliRunner().invoke(app, list(args))


def test_per_app_and_overall_recall_exact(paths) -> None:
    # Firefox: 2 clicks, 1 covered. Chrome: 2 clicks, 2 covered. Overall 3/4.
    _write_session(
        paths,
        "s1",
        [
            _click_event("s1", 1_000_000_000, "Firefox", (10, 10), [{"id": "a", "bbox": [0, 0, 20, 20]}], "a"),
            _click_event("s1", 2_000_000_000, "Firefox", (500, 500), [{"id": "a", "bbox": [0, 0, 20, 20]}]),
            _click_event("s1", 3_000_000_000, "Chrome", (5, 5), [{"id": "b", "bbox": [0, 0, 10, 10]}], "b"),
            _click_event("s1", 4_000_000_000, "Chrome", (35, 5), [{"id": "c", "bbox": [30, 0, 10, 10]}], "c"),
        ],
    )
    result = _run("gaze", "eval-candidates", str(paths.data_dir / "sessions"))
    assert result.exit_code == 0, result.output
    assert "Firefox" in result.output and "Chrome" in result.output
    assert "50.0%" in result.output  # Firefox 1/2
    assert "100.0%" in result.output  # Chrome 2/2
    assert "75.0%" in result.output  # overall 3/4
    assert "PHASE 1 GATE NOT MET" in result.output
    assert "Target-id recall: 3/3" in result.output


def test_gate_met_above_95_percent(paths) -> None:
    events = [
        _click_event("s2", i * 1_000_000_000, "Terminal", (5, 5), [{"id": "t", "bbox": [0, 0, 10, 10]}])
        for i in range(1, 21)
    ]
    _write_session(paths, "s2", events)
    result = _run("gaze", "eval-candidates", str(paths.data_dir / "sessions"))
    assert result.exit_code == 0, result.output
    assert "100.0%" in result.output
    assert "PHASE 1 GATE MET" in result.output


def test_no_clicks_exits_nonzero(paths) -> None:
    _write_session(
        paths,
        "s3",
        [make_event("s3", 1_000_000_000, interaction={"kind": "hover", "mouse": [1, 2]})],
    )
    result = _run("gaze", "eval-candidates", str(paths.data_dir / "sessions"))
    assert result.exit_code == 1
    assert "no labeled clicks" in result.output


def test_empty_sessions_dir_exits_nonzero(paths) -> None:
    (paths.data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    result = _run("gaze", "eval-candidates", str(paths.data_dir / "sessions"))
    assert result.exit_code == 1
    assert "no sessions" in result.output


def test_corrupt_session_is_skipped_with_warning(paths) -> None:
    _write_session(
        paths,
        "good",
        [_click_event("good", 1_000_000_000, "Files", (5, 5), [{"id": "f", "bbox": [0, 0, 10, 10]}])],
    )
    bad = paths.data_dir / "sessions" / "bad"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text(json.dumps({"session_id": "bad", "schema_version": 0}))
    (bad / "events.jsonl").write_text(json.dumps({"session_id": "bad", "ts_ns": "nope"}) + "\n")
    result = _run("gaze", "eval-candidates", str(paths.data_dir / "sessions"))
    assert result.exit_code == 0, result.output
    assert "warning" in result.output and "bad" in result.output
    assert "Skipped sessions: 1" in result.output
    assert "100.0%" in result.output


def test_latency_na_without_regenerate(paths) -> None:
    _write_session(
        paths,
        "s4",
        [_click_event("s4", 1_000_000_000, "Files", (5, 5), [{"id": "f", "bbox": [0, 0, 10, 10]}])],
    )
    result = _run("gaze", "eval-candidates", str(paths.data_dir / "sessions"))
    assert result.exit_code == 0, result.output
    assert "Generator latency: n/a" in result.output


def test_regenerate_without_screenshots_reports_na(paths) -> None:
    _write_session(
        paths,
        "s5",
        [_click_event("s5", 1_000_000_000, "Files", (5, 5), [{"id": "f", "bbox": [0, 0, 10, 10]}])],
    )
    result = _run("gaze", "eval-candidates", "--regenerate", str(paths.data_dir / "sessions"))
    assert result.exit_code == 0, result.output
    assert "Generator latency: n/a" in result.output


def test_eval_candidates_group_registered() -> None:
    result = _run("gaze", "--help")
    assert result.exit_code == 0
    assert "eval-candidates" in result.output
