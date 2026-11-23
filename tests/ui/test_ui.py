"""Headless UI tests for streamlined onboarding, settings, activity, overlay, and goldens."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage

from keyless.config.paths import HOME_ENV, default_paths
from keyless.config.schema import AppConfig
from keyless.platform.linux.cameras import friendly_camera_name
from keyless.ui.components import friendly_key_name
from keyless.ui.history.db import ActionRecord, HistoryStore
from keyless.ui.history.window import HistoryPanel
from keyless.ui.onboarding.flow import OnboardingFlow, OnboardingStep
from keyless.ui.onboarding.steps import DevicesPage, OnboardingWindow
from keyless.ui.overlay.base import OverlayMode, OverlayState, OverlayUpdate
from keyless.ui.overlay.linux import TrayToastOverlay, overlay_mode
from keyless.ui.session import NullController
from keyless.ui.settings.window import SettingsPanel
from keyless.ui.theme.tokens import ThemeName

GOLDENS = Path(__file__).with_name("goldens")


def _paths(tmp_path: Path):
    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    return paths


def _report(denied: bool = False):
    return SimpleNamespace(
        permissions={
            "input_injection": {
                "state": "denied" if denied else "granted",
                "detail": "uinput writable",
                "action": "Add user to input group" if denied else "",
            },
            "microphone": {"state": "granted", "detail": "mic found", "action": ""},
        }
    )


def _compare_golden(pixmap, name: str, tolerance: float = 6.0) -> None:
    golden_path = GOLDENS / name
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    assert image.width() > 400 and image.height() > 300
    if not golden_path.exists():
        pixmap.save(str(golden_path))
        pytest.fail(f"Golden did not exist; wrote {golden_path}. Re-run to compare.")
    golden = QImage(str(golden_path)).convertToFormat(QImage.Format.Format_RGBA8888)
    assert (golden.width(), golden.height()) == (image.width(), image.height())
    total = 0
    different = 0
    for y in range(0, image.height(), 3):
        for x in range(0, image.width(), 3):
            left = image.pixelColor(x, y)
            right = golden.pixelColor(x, y)
            delta = (
                abs(left.red() - right.red())
                + abs(left.green() - right.green())
                + abs(left.blue() - right.blue())
            ) / 3
            total += 1
            if delta > 12:
                different += 1
    ratio = different / max(1, total)
    assert ratio < tolerance / 100, f"{name}: {ratio:.2%} pixels differ"


def test_friendly_camera_and_key_names() -> None:
    assert (
        friendly_camera_name("usb-Sonix_Technology_Co.__Ltd._GENERAL_WEBCAM_GENERAL_WEBCAM-video-index0")
        == "Sonix Technology General Webcam"
    )
    assert friendly_key_name("KEY_RIGHTCTRL") == "Right Control"
    assert friendly_key_name("KEY_F9") == "F9"


def test_onboarding_flow_persists_and_resumes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    flow = OnboardingFlow(paths)
    assert flow.step is OnboardingStep.WELCOME
    flow.advance()
    assert flow.step is OnboardingStep.DEVICES
    flow.advance(unresolved=True)
    assert flow.step is OnboardingStep.CALIBRATION
    assert "welcome" in flow.state.completed
    assert "devices" in flow.state.unresolved

    resumed = OnboardingFlow(paths)
    assert resumed.step is OnboardingStep.CALIBRATION
    assert "devices" in resumed.state.unresolved
    resumed.back()
    assert resumed.step is OnboardingStep.DEVICES
    resumed.reset()
    assert resumed.step is OnboardingStep.WELCOME
    assert json.loads(paths.onboarding_path.read_text())["version"] == 1


def test_onboarding_window_resumes_and_reaches_ready(qapp, tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "keyless.ui.onboarding.steps.stt_report",
        lambda *_a, **_k: {
            "selected": "parakeet",
            "parakeet": {"state": "ready", "path": str(paths.models_dir)},
        },
    )
    monkeypatch.setattr(
        "keyless.ui.onboarding.steps.discover_v4l_by_id",
        lambda: ["/dev/v4l/by-id/usb-Logitech_Webcam-video-index0"],
    )
    flow = OnboardingFlow(paths)
    flow.go_to(OnboardingStep.WELCOME)
    window = OnboardingWindow(
        flow=flow,
        config=AppConfig(cam1_by_id="/dev/v4l/by-id/usb-Logitech_Webcam-video-index0"),
        paths=paths,
        report_provider=_report,
        enable_previews=False,
    )
    try:
        assert window.stack.currentIndex() == 0
        window.next()  # welcome -> devices
        assert flow.step is OnboardingStep.DEVICES
        window.next()  # devices -> calibration
        assert flow.step is OnboardingStep.CALIBRATION
        window.next()  # calibration -> ready
        assert flow.step is OnboardingStep.READY
        window.next()  # finish
        assert flow.is_complete
        assert json.loads(paths.onboarding_path.read_text())["step"] == "ready"
    finally:
        window.close()


def test_devices_page_progressive_enhancement_single_vs_dual_camera(qapp, tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    # 1 camera: stereo controls, camera 2, and swap must stay hidden
    monkeypatch.setattr(
        "keyless.ui.onboarding.steps.discover_v4l_by_id",
        lambda: ["/dev/v4l/by-id/usb-Cam_One-video-index0"],
    )
    page1 = DevicesPage(AppConfig(), paths, report_provider=_report, enable_previews=False)
    try:
        assert page1.mode.isHidden()
        assert page1.cam2.isHidden()
        assert page1.swap_button.isHidden()
        assert page1.perm_card.isHidden()  # all permissions granted -> zero noise
    finally:
        page1.close_resources()

    # 2 cameras + denied permission: stereo controls and permission card appear
    monkeypatch.setattr(
        "keyless.ui.onboarding.steps.discover_v4l_by_id",
        lambda: ["/dev/v4l/by-id/usb-Cam_One-video-index0", "/dev/v4l/by-id/usb-Cam_Two-video-index0"],
    )
    page2 = DevicesPage(AppConfig(), paths, report_provider=lambda: _report(denied=True), enable_previews=False)
    try:
        page2.on_enter()
        assert not page2.mode.isHidden()
        assert not page2.cam2.isHidden()
        assert not page2.swap_button.isHidden()
        assert not page2.perm_card.isHidden()
        # Switching to one camera hides cam2 and swap cleanly
        page2.mode.setCurrentIndex(page2.mode.findData("mono"))
        assert page2.cam2.isHidden()
        assert page2.swap_button.isHidden()
    finally:
        page2.close_resources()


def test_settings_panel_instant_saves_and_preserves_provider(qapp, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config = AppConfig(llm_provider="auto")
    panel = SettingsPanel(config, paths)
    try:
        panel.hotkey.setText("KEY_F9")
        panel.always_on.setChecked(False)
        assert not hasattr(panel, "whisper_model")
        assert not hasattr(panel, "stt_engine")
        loaded = AppConfig.load(paths.settings_path)
        assert loaded.ptt_hotkey == "KEY_F9"
        assert loaded.always_on_voice is False
        # Must NOT silently clobber llm_provider to openai_compat!
        assert loaded.llm_provider == "auto"
    finally:
        panel.stop()


def test_history_store_and_panel(qapp, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = HistoryStore(paths)
    store.add(ActionRecord(transcript="click send", route="gaze", target="Send", result="clicked", latency_ms=42))
    rows = store.recent()
    assert rows[0]["transcript"] == "click send"
    panel = HistoryPanel(store)
    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 1).text() == "click send"
    panel.clear()
    assert panel.table.rowCount() == 0
    assert panel.table.isHidden()


def test_overlay_fallback_is_honest_on_gnome_wayland() -> None:
    gnome = overlay_mode({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"})
    assert gnome.mode is OverlayMode.TRAY_TOAST
    assert gnome.available
    assert "GNOME Wayland" in gnome.reason
    assert not gnome.persistent_indicator

    kde = overlay_mode({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"})
    assert kde.mode is OverlayMode.TRAY_TOAST
    assert "portable click-through" in kde.reason

    x11 = overlay_mode({"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "GNOME"})
    assert x11.mode is OverlayMode.TRANSPARENT_WINDOW
    overlay = TrayToastOverlay(tray=None)
    overlay.show_state(OverlayUpdate(OverlayState.LISTENING, "Listening"))
    assert overlay.capability().mode is OverlayMode.TRAY_TOAST


def test_main_window_three_view_navigation_and_inline_setup(qapp, tmp_path: Path) -> None:
    from keyless.ui.app import MainWindow

    paths = _paths(tmp_path)
    window = MainWindow(
        paths=paths,
        config=AppConfig(),
        controller=NullController(),
        theme=ThemeName.LIGHT,
        enable_previews=False,
    )
    try:
        assert window.stack.count() == 3
        window.show_page("Activity")
        assert window.stack.currentWidget() is window.pages["Activity"]
        # Backwards-compatible alias
        window.show_page("History")
        assert window.stack.currentWidget() is window.pages["Activity"]
        window.set_theme(ThemeName.DARK)
        assert window.theme_tokens.name is ThemeName.DARK
        window.home.toggle()
        assert window.controller.status.state.value == "listening"
        window.home.toggle()
        assert window.controller.status.state.value == "paused"
        # Inline setup embeds in the same window (no second dialog!)
        setup = window.show_inline_setup()
        assert window.root_stack.currentWidget() is setup
    finally:
        window.shutdown()
        window.close()


def test_main_window_light_and_dark_goldens(qapp, tmp_path: Path) -> None:
    from keyless.ui.app import MainWindow

    paths = _paths(tmp_path)
    for theme in (ThemeName.LIGHT, ThemeName.DARK):
        window = MainWindow(
            paths=paths,
            config=AppConfig(),
            controller=NullController(),
            theme=theme,
            enable_previews=False,
        )
        try:
            window.resize(980, 640)
            window.show()
            qapp.processEvents()
            _compare_golden(window.grab(), f"main-{theme.value}.png")
        finally:
            window.shutdown()
            window.close()


def test_onboarding_welcome_light_and_dark_goldens(qapp, tmp_path: Path) -> None:
    from keyless.ui.theme.tokens import stylesheet, tokens_for

    paths = _paths(tmp_path)
    flow = OnboardingFlow(paths)
    for theme in (ThemeName.LIGHT, ThemeName.DARK):
        qapp.setStyleSheet(stylesheet(tokens_for(theme)))
        window = OnboardingWindow(
            flow=flow,
            config=AppConfig(),
            paths=paths,
            report_provider=_report,
            enable_previews=False,
        )
        try:
            window.setFixedSize(740, 580)
            window.show()
            qapp.processEvents()
            _compare_golden(window.grab(), f"onboarding-{theme.value}.png")
        finally:
            window.close()


def test_settings_panel_lists_and_saves_a_microphone(qapp, tmp_path: Path, monkeypatch) -> None:
    from keyless.audio.quick_test import InputDevice

    monkeypatch.setattr(
        "keyless.ui.settings.window.input_devices",
        lambda: [InputDevice(8, "Razer Seiren Mini", 1), InputDevice(7, "Webcam", 1)],
    )
    paths = _paths(tmp_path)
    panel = SettingsPanel(AppConfig(), paths)
    try:
        assert panel.microphone.count() == 3
        panel.microphone.setCurrentIndex(1)
        assert AppConfig.load(paths.settings_path).audio_device == 8
    finally:
        panel.stop()


def test_settings_mic_check_reports_signal(qapp, tmp_path: Path, monkeypatch) -> None:
    from keyless.audio.quick_test import MicLevel

    monkeypatch.setattr(
        "keyless.ui.settings.window.check_microphone",
        lambda device=None: MicLevel(peak=0.21, rms=0.05, seconds=1.5),
    )
    paths = _paths(tmp_path)
    panel = SettingsPanel(AppConfig(), paths)
    try:
        panel.check_mic()
        assert panel._mic_worker is not None
        panel._mic_worker.wait(3000)
        qapp.processEvents()
        assert "clearly" in panel.mic_result.text().lower()
    finally:
        panel.stop()


def test_devices_page_reports_mic_and_transcript(qapp, tmp_path: Path) -> None:
    from keyless.audio.quick_test import MicLevel, SpeechTestResult

    paths = _paths(tmp_path)
    page = DevicesPage(AppConfig(camera_mode="mono"), paths, report_provider=_report, enable_previews=False)
    try:
        page._on_mic_done(MicLevel(peak=0.21, rms=0.05, seconds=1.5))
        assert "clearly" in page.voice_note.text().lower()
        page._on_speech_done(
            SpeechTestResult(
                transcript="click send",
                latency_ms=41.0,
                recorded_s=4.0,
                sample_rate=16000,
                peak=0.2,
                engine="parakeet-tdt-0.6b-v2",
            )
        )
        assert "click send" in page.voice_note.text()
        page.mic.addItem("Fake microphone", 42)
        page.mic.setCurrentIndex(page.mic.count() - 1)
        assert AppConfig.load(paths.settings_path).audio_device == 42
    finally:
        page.close_resources()


def test_settings_panel_never_overflows_viewport_at_minimum_width(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtCore import QPoint
    from keyless.audio.quick_test import InputDevice
    from keyless.ui.app import MainWindow

    monkeypatch.setattr(
        "keyless.ui.settings.window.input_devices",
        lambda: [InputDevice(1, "Very Long USB Studio Condenser Microphone With Hardware Monitoring Interface", 1)],
    )
    paths = _paths(tmp_path)
    window = MainWindow(
        paths=paths,
        config=AppConfig(),
        controller=NullController(),
        theme=ThemeName.DARK,
        enable_previews=False,
    )
    try:
        window.resize(820, 540)
        window.show_page("Settings")
        window.settings.tts_status.setText(
            "Diagnostic status with a very long SHA-256 hex digest 784c4a353d15f370ff387f74bde64f50a2041df9c6f2338c3d8a2f781b944de5 that must wrap cleanly."
        )
        window.show()
        qapp.processEvents()
        content = window.settings.scroll.widget()
        viewport = window.settings.scroll.viewport()
        assert content is not None
        assert content.width() <= viewport.width()
        for btn in (
            window.settings.mic_test,
            window.settings.speech_test,
            window.settings.dl_button,
            window.settings.tts_preview_btn,
        ):
            right_edge = btn.mapTo(viewport, QPoint(btn.width(), 0)).x()
            assert right_edge <= viewport.width()
    finally:
        window.shutdown()
        window.close()


def test_settings_form_is_generated_from_schema(qapp, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    panel = SettingsPanel(AppConfig(), paths)
    try:
        # Every AppConfig setting applicable on this platform has an editable
        # widget (audio_device is the live microphone picker, mapped into the
        # same field registry).
        expected = {str(spec["key"]) for spec in panel._applicable_specs()} | {"audio_device"}
        assert expected <= set(panel._field_widgets)
        assert panel.hotkey is panel._field_widgets["ptt_hotkey"]
        assert panel.always_on is panel._field_widgets["always_on_voice"]
        assert panel.microphone is panel._field_widgets["audio_device"]
    finally:
        panel.stop()

    synthetic = [
        {
            "key": "fps",
            "value_type": "int",
            "default": 30,
            "label": "Synthetic rate",
            "help_text": "",
            "group": "Synthetic group",
            "control": "number",
            "options": None,
            "minimum": 1,
            "maximum": 240,
            "apply": "live",
            "platforms": ["all"],
        }
    ]
    panel = SettingsPanel(AppConfig(), paths, schema=synthetic)
    try:
        assert set(panel.form_groups) == {"Synthetic group"}
        assert "fps" in panel._field_widgets
        assert panel._field_widgets["fps"].value() == 30
        # Invalid patches are rejected before they reach disk.
        assert panel._apply_patch({"fps": "fast"}) is False
        assert AppConfig.load(paths.settings_path).fps == 30
    finally:
        panel.stop()


def test_daemon_error_is_readable(tmp_path: Path) -> None:
    from keyless.ui.session import _extract_daemon_error

    log = tmp_path / "daemon.log"
    log.write_text("Traceback...\nRuntimeError: Failed to open V4L2 capture on /dev/video0\n")
    msg = _extract_daemon_error(log, 1)
    assert "Camera is currently in use" in msg
    assert "/home/" not in msg


def test_commands_help_dialog_and_search(qapp, tmp_path: Path) -> None:
    from keyless.ui.app import MainWindow
    from keyless.ui.commands_help import COMMAND_CATEGORIES, filter_command_categories

    assert len(COMMAND_CATEGORIES) >= 5
    scroll_cats = filter_command_categories("auto scroll")
    assert len(scroll_cats) == 1
    assert scroll_cats[0].title == "Scrolling & Reading"
    assert filter_command_categories("nonexistent_command_xyz_123") == []

    paths = _paths(tmp_path)
    window = MainWindow(
        paths=paths,
        config=AppConfig(),
        controller=NullController(),
        theme=ThemeName.DARK,
        enable_previews=False,
    )
    try:
        dlg = window.open_commands_help()
        assert dlg.isVisible()
        dlg.help_widget.search_input.setText("terminal")
        qapp.processEvents()
        dlg.close()
    finally:
        window.shutdown()
        window.close()
