"""End-to-end tests for SessionRecorder -> SessionCollector -> replay -> eval-candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.candidate_generator import CandidateGenerator
from keyless.gaze.collect import REDACTED, SessionCollector
from keyless.gaze.recorder import ScreenSnapshot, SessionRecorder
from keyless.gaze.replay import load_session
from keyless.main import app


@dataclass
class _FakeNode:
    node_id: str
    role: str
    name: str
    cx: int
    cy: int
    w: int
    h: int


class _FakeAXBridge:
    def __init__(self, nodes: list[_FakeNode], *, is_available: bool = True) -> None:
        self._nodes = nodes
        self._available = is_available

    def available(self) -> bool:
        return self._available

    def scan_active_window(self) -> list[_FakeNode]:
        return list(self._nodes)


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    resolved = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    resolved.ensure_directories()
    return resolved


def test_recorder_roundtrip_replay_and_eval_candidates_gate_met(paths: AppPaths) -> None:
    nodes = [
        _FakeNode("btn-save", "button", "Save", 100, 100, 80, 40),
        _FakeNode("btn-cancel", "button", "Cancel", 240, 100, 80, 40),
    ]
    gen = CandidateGenerator(ax_bridge=_FakeAXBridge(nodes))
    collector = SessionCollector(paths, session_id="rec-pass")
    snap = ScreenSnapshot(display_id=1, app="Firefox", window_id=42, screenshot_hash="a" * 64)
    recorder = SessionRecorder(collector, gen, screen_provider=lambda: snap)

    # 20 clicks inside known candidates -> 100% recall (>95% Phase 1 gate)
    for i in range(20):
        x, y = (100.0, 100.0) if i % 2 == 0 else (240.0, 100.0)
        assert recorder.record_click(x, y, gaze_pixel=(x + 3.0, y - 2.0))
    assert recorder.record_voice("click save", target="btn-save", accepted=True)
    recorder.close()

    session = load_session(collector.session_dir)
    assert len(session.events) == 21
    first_click = session.events[0]
    assert first_click["interaction"]["target"] == "btn-save"
    assert first_click["screen"]["app"] == "Firefox"
    assert len(first_click["screen"]["candidates"]) == 2
    assert recorder.stats()["sources"]["ax"] is True

    runner = CliRunner()
    res_replay = runner.invoke(app, ["gaze", "replay", str(collector.session_dir)])
    assert res_replay.exit_code == 0, res_replay.output
    assert "baseline_top1" in res_replay.output

    res_eval = runner.invoke(app, ["gaze", "eval-candidates", str(paths.data_dir / "sessions")])
    assert res_eval.exit_code == 0, res_eval.output
    assert "PHASE 1 GATE MET" in res_eval.output
    assert "100.0%" in res_eval.output


def test_recorder_eval_candidates_gate_fails_on_uncovered_clicks(paths: AppPaths) -> None:
    nodes = [_FakeNode("btn-save", "button", "Save", 100, 100, 40, 20)]
    gen = CandidateGenerator(ax_bridge=_FakeAXBridge(nodes))
    collector = SessionCollector(paths, session_id="rec-fail")
    snap = ScreenSnapshot(display_id=1, app="Code", window_id=9, screenshot_hash="b" * 64)
    recorder = SessionRecorder(collector, gen, screen_provider=lambda: snap)

    # 1 hit, 1 miss -> 50% recall (<95%)
    recorder.record_click(100.0, 100.0)
    recorder.record_click(800.0, 600.0)
    recorder.close()

    session = load_session(collector.session_dir)
    assert session.events[0]["interaction"]["target"] == "btn-save"
    assert session.events[1]["interaction"]["target"] is None

    runner = CliRunner()
    res_eval = runner.invoke(app, ["gaze", "eval-candidates", str(paths.data_dir / "sessions")])
    assert res_eval.exit_code == 0, res_eval.output
    assert "PHASE 1 GATE NOT MET" in res_eval.output


def test_recorder_respects_explicit_redaction_and_screenshot_blobs(paths: AppPaths) -> None:
    nodes = [_FakeNode("btn-secret", "button", "Secret Password", 50, 50, 100, 40)]
    gen = CandidateGenerator(ax_bridge=_FakeAXBridge(nodes))
    collector = SessionCollector(paths, session_id="rec-redact", redact_text=True)
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    snap = ScreenSnapshot(display_id=0, app="Terminal", window_id=3, image_bgr=img)
    recorder = SessionRecorder(
        collector, gen, screen_provider=lambda: snap, save_screenshots=True
    )

    assert recorder.record_click(50.0, 50.0)
    assert recorder.record_voice("secret transcript")
    recorder.close()

    session = load_session(collector.session_dir)
    assert session.meta["redact_text"] is True
    click_ev = session.events[0]
    assert click_ev["screen"]["candidates"][0]["text"] == REDACTED
    digest = click_ev["screen"]["screenshot"]
    assert digest and (collector.session_dir / "blobs" / f"{digest}.png").exists()
    assert session.events[1]["interaction"]["voice"] == REDACTED


def test_recorder_degrades_honestly_when_ax_and_screen_unavailable(paths: AppPaths) -> None:
    gen = CandidateGenerator(ax_bridge=_FakeAXBridge([], is_available=False))
    collector = SessionCollector(paths, session_id="rec-honest")
    recorder = SessionRecorder(collector, gen, screen_provider=lambda: None)

    assert recorder.record_click(300.0, 200.0)
    assert recorder.record_gaze_frame(305.0, 202.0, confidence=0.8, features=[0.1, 0.2])
    recorder.close()

    stats = recorder.stats()
    assert stats["clicks"] == 1
    assert stats["frames"] == 1
    assert stats["sources"]["ax"] is False
    session = load_session(collector.session_dir)
    assert len(session.events) == 2


def test_candidate_generator_accepts_bool_property_ax_bridge() -> None:
    class _PropertyAXBridge:
        @property
        def available(self) -> bool:
            return True

        def scan_active_window(self) -> list[_FakeNode]:
            return [_FakeNode("prop-1", "button", "Apply", 50, 50, 80, 30)]

    gen = CandidateGenerator(ax_bridge=_PropertyAXBridge())
    res = gen.generate(0, 1, "hash-prop", include_null=False)
    assert res.sources["ax"] is True
    assert len(res.candidates) == 1
    assert res.candidates[0].id == "prop-1"


def test_gaze_record_cli_smoke_runs_and_creates_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["gaze", "record", "--duration", "0.05", "--no-ocr", "--session-id", "cli-rec-1"],
    )
    assert res.exit_code == 0, res.output
    assert "cli-rec-1" in res.output
    session_dir = default_paths(env={HOME_ENV: str(tmp_path)}).data_dir / "sessions" / "cli-rec-1"
    assert (session_dir / "meta.json").exists()


def test_eval_candidates_regenerate_measures_latency_on_real_png(paths: AppPaths, monkeypatch) -> None:
    nodes = [_FakeNode("btn-ok", "button", "OK", 20, 20, 30, 20)]
    gen = CandidateGenerator(ax_bridge=_FakeAXBridge(nodes))
    collector = SessionCollector(paths, session_id="rec-regen")
    img = np.full((64, 64, 3), 240, dtype=np.uint8)
    snap = ScreenSnapshot(display_id=0, app="Demo", window_id=5, image_bgr=img)
    recorder = SessionRecorder(
        collector, gen, screen_provider=lambda: snap, save_screenshots=True
    )
    assert recorder.record_click(20.0, 20.0)
    recorder.close()

    # Use a fast deterministic OCR stub so the test does not depend on model downloads
    fake_gen = CandidateGenerator(ocr_fn=lambda im: [((5, 10, 30, 20), "OK", 0.99)])
    monkeypatch.setattr("keyless.main._candidate_generator_for_eval", lambda: fake_gen)

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["gaze", "eval-candidates", str(paths.data_dir / "sessions"), "--regenerate"],
    )
    assert res.exit_code == 0, res.output
    assert "Generator latency: p50" in res.output
    assert "1 samples" in res.output


def test_collector_survives_corrupt_settings_json(paths: AppPaths) -> None:
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text("{corrupt json", encoding="utf-8")
    collector = SessionCollector(paths, session_id="rec-corrupt")
    collector.close()
    session = load_session(collector.session_dir)
    assert len(session.meta["config_hash"]) == 64


def test_eval_candidates_defaults_to_data_dir_sessions_and_reports_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    runner = CliRunner()
    res = runner.invoke(app, ["gaze", "eval-candidates"])
    assert res.exit_code == 1
    assert "no sessions directory" in res.output
