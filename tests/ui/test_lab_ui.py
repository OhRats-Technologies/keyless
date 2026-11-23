"""Offscreen Qt tests for Keyless Lab (Setup, Curator, Recall, Dream-RSI, inline error callout)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QGuiApplication

from keyless.config.paths import HOME_ENV, AppPaths, default_paths
from keyless.config.schema import AppConfig
from keyless.core.lifecycle import NullController
from keyless.gaze.collect import SessionCollector
from keyless.gaze.replay import load_session
from keyless.gaze.schema import make_event
from keyless.ui.app import MainWindow
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


def test_lab_window_setup_curates_events_and_navigates_tabs(qapp, populated_paths: AppPaths) -> None:
    win = LabWindow(paths=populated_paths, theme=ThemeName.DARK)
    win.show()
    qapp.processEvents()

    # Starts on Setup (0), then switches to Curator (1)
    assert win.pages.currentIndex() == 0
    assert win.page_title.text() == "Setup"
    win._select_tab(1)
    qapp.processEvents()
    assert win.page_title.text() == "Curator"

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
    assert "GATE MET" in win.gate_badge.text()

    # Switch through Recall & Replay (2) and Dream-RSI (3) tabs cleanly
    win._select_tab(2)
    qapp.processEvents()
    assert win.app_recall_table.rowCount() == 1
    win._select_tab(3)
    qapp.processEvents()
    assert "Promoted policies:" in win.dream_summary_label.text()

    win.close()


def test_lab_window_seeds_sample_session_and_copies_error_callout(qapp, tmp_path: Path) -> None:
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()

    main = MainWindow(
        paths=paths,
        config=AppConfig(),
        controller=NullController(),
        theme=ThemeName.LIGHT,
        enable_previews=False,
    )
    try:
        lab = main.open_lab()
        qapp.processEvents()
        assert lab.isVisible()
        assert lab.session_list.count() == 0

        # Inline copyable error callout shows full message + command without truncation
        lab._show_error(
            "Global click capture is locked",
            "no readable /dev/input/event* pointer devices",
            show_input_fix=True,
            command='sudo usermod -aG input "$USER"',
        )
        qapp.processEvents()
        assert not lab.error_banner.isHidden()
        assert not lab.error_cmd_row.isHidden()
        lab.error_copy_cmd_btn.click()
        cb = QGuiApplication.clipboard()
        if cb is not None:
            assert cb.text() == 'sudo usermod -aG input "$USER"'
        lab.error_dismiss_btn.click()
        assert lab.error_banner.isHidden()

        # Newcomer clicks Create sample session on Setup tab -> populates and switches to Curator
        lab.guide_sample_btn.click()
        qapp.processEvents()
        assert lab.session_list.count() == 1
        assert lab.pages.currentIndex() == 1
        assert lab.event_list.count() == 4
    finally:
        main.shutdown()
        main.close()
