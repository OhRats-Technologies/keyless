"""Offscreen Qt tests for Keyless Lab (curate, recall, Dream-RSI, visual rendering)."""

from __future__ import annotations

from pathlib import Path

import pytest

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.gaze.collect import SessionCollector
from keyless.gaze.replay import load_session
from keyless.gaze.schema import make_event
from keyless.ui.lab.window import LabWindow
from keyless.ui.theme.tokens import ThemeName

pytestmark = pytest.mark.ui


@pytest.fixture
def populated_paths(tmp_path: Path) -> AppPaths:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    collector = SessionCollector(paths, session_id="lab-demo-01")
    cands = [
        {"id": "btn-start", "bbox": [120, 140, 220, 56], "role": "button", "text": "Start listening", "score": 0.96},
        {"id": "btn-settings", "bbox": [380, 140, 180, 56], "role": "button", "text": "Settings", "score": 0.88},
        {"id": "tab-activity", "bbox": [120, 240, 300, 44], "role": "tab", "text": "Recent activity", "score": 0.81},
    ]
    collector.record(
        make_event(
            "lab-demo-01",
            1_000_000_000,
            screen={"display_id": 0, "app": "Firefox", "window_id": 11, "candidates": cands},
            prediction={"top": "btn-start", "p": 0.91, "entropy": 0.25, "pixel": [240.0, 165.0]},
            interaction={"kind": "click", "mouse": [230.0, 168.0], "target": "btn-start", "accepted": True},
        )
    )
    collector.record(
        make_event(
            "lab-demo-01",
            2_000_000_000,
            screen={"display_id": 0, "app": "Code", "window_id": 12, "candidates": cands},
            prediction={"top": "btn-settings", "p": 0.42, "entropy": 0.88, "pixel": [500.0, 210.0]},
            interaction={"kind": "click", "mouse": [820.0, 480.0], "target": None, "accepted": None},
        )
    )
    collector.close()
    return paths


def test_lab_window_curates_events_and_renders_both_themes(qapp, populated_paths: AppPaths) -> None:
    win = LabWindow(paths=populated_paths, theme=ThemeName.DARK)
    win.show()
    qapp.processEvents()

    assert win.session_list.count() == 1
    assert win.event_list.count() == 2
    assert "RECALL 50.0%" in win.recall_metric.text()

    # Reject event 0 -> persists to disk
    win.reject_btn.click()
    qapp.processEvents()
    reloaded = load_session(populated_paths.data_dir / "sessions" / "lab-demo-01")
    assert reloaded.events[0]["interaction"]["accepted"] is False

    # Re-assign target via canvas signal -> sets target and accepted=True
    win.canvas.candidate_clicked.emit("btn-settings")
    qapp.processEvents()
    reloaded = load_session(populated_paths.data_dir / "sessions" / "lab-demo-01")
    assert reloaded.events[0]["interaction"]["target"] == "btn-settings"
    assert reloaded.events[0]["interaction"]["accepted"] is True

    # Delete event 1 -> only 1 event remains
    win.event_list.setCurrentRow(1)
    qapp.processEvents()
    win.delete_event_btn.click()
    qapp.processEvents()
    assert win.event_list.count() == 1
    assert "GATE  MET" in win.gate_badge.text()

    # Switch through Recall & Dream-RSI tabs cleanly
    win._on_nav_changed(1)
    qapp.processEvents()
    assert win.app_recall_table.rowCount() == 1
    win._on_nav_changed(2)
    qapp.processEvents()
    assert "Promoted policies:" in win.dream_summary_label.text()

    win.close()
