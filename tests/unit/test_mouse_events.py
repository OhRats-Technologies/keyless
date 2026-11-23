"""macOS stationary-click harvesting: labels come from the pre-click fixation."""

from __future__ import annotations

from keyless.platform.macos import mouse_events


class _FakeLearner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_verified_interaction(self, **kwargs) -> bool:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return True


class _Pointer:
    def __init__(self) -> None:
        self.pressed = False
        self.x = 100.0
        self.y = 200.0

    def button(self) -> bool:
        return self.pressed

    def position(self) -> tuple[float, float]:
        return self.x, self.y


class _LinearModel:
    bounds_min_x = 0.0
    bounds_min_y = 0.0
    bounds_max_x = 1920.0
    bounds_max_y = 1080.0
    cam1_by_id = "cam1"
    cam2_by_id = "cam2"
    rmse_x = 0.0
    rmse_y = 0.0

    def predict(self, feat):  # type: ignore[no-untyped-def]
        return float(feat[0]), float(feat[1])


def test_stationary_click_updates_the_online_learner_bias(monkeypatch) -> None:
    from keyless.config import AppConfig
    from keyless.platform.base import DisplayLayout, MonitorInfo
    from keyless.vision.online_learner import OnlineGazeLearner

    monkeypatch.setattr(mouse_events, "synthetic_click_recent", lambda: False)
    monitor = MonitorInfo("DP-1", 0, 0, 1920, 1080, 1920, 1080, 1.0, True)
    layout = DisplayLayout(monitors=(monitor,), total_width=1920, total_height=1080)
    learner = OnlineGazeLearner(
        _LinearModel(),  # type: ignore[arg-type]
        AppConfig(),
        monitor,
        layout,
        allow_disk_overwrite=False,
    )
    feat = [500.0, 500.0] + [0.0] * 18
    for index in range(70):
        learner.predict(feat, 10.0 + index / 30.0)
    assert learner.bias_xy == (0.0, 0.0)

    pointer = _Pointer()
    pointer.x, pointer.y = 520.0, 520.0
    harvester = _harvester(learner, pointer)  # type: ignore[arg-type]
    now = _settle(harvester, pointer, 12.4)
    pointer.pressed = True
    harvester.poll_once(now)
    now += 0.08
    pointer.pressed = False
    assert harvester.poll_once(now) is True
    bx, by = learner.bias_xy
    assert bx > 0.0 and by > 0.0


def _harvester(learner: _FakeLearner, pointer: _Pointer) -> mouse_events.MacOSMouseClickHarvester:
    return mouse_events.MacOSMouseClickHarvester(
        learner,
        read_button=pointer.button,
        read_position=pointer.position,
        max_stationary_px=12.0,
        max_path_px=30.0,
    )


def _settle(harvester, pointer, start: float, count: int = 20) -> float:
    now = start
    for _ in range(count):
        harvester.poll_once(now)
        now += 1 / 120
    return now


def test_stationary_click_feeds_the_pre_click_target(monkeypatch) -> None:
    monkeypatch.setattr(mouse_events, "synthetic_click_recent", lambda: False)
    learner = _FakeLearner()
    pointer = _Pointer()
    harvester = _harvester(learner, pointer)
    now = _settle(harvester, pointer, 10.0)

    pointer.pressed = True
    down_ts = now
    harvester.poll_once(down_ts)
    now += 0.08
    pointer.pressed = False
    assert harvester.poll_once(now) is True

    assert len(learner.calls) == 1
    call = learner.calls[0]
    assert (call["true_x"], call["true_y"]) == (100.0, 200.0)
    assert call["anchor_ts"] == down_ts
    assert call["source"] == "mouse"
    assert harvester.accepted == 1


def test_quick_moving_click_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(mouse_events, "synthetic_click_recent", lambda: False)
    learner = _FakeLearner()
    pointer = _Pointer()
    harvester = _harvester(learner, pointer)
    now = 20.0
    for i in range(20):
        pointer.x = 100.0 + i * 6.0
        harvester.poll_once(now)
        now += 1 / 120

    pointer.pressed = True
    harvester.poll_once(now)
    now += 0.08
    pointer.pressed = False
    assert harvester.poll_once(now) is False
    assert learner.calls == []
    assert harvester.rejected == 1


def test_drag_and_synthetic_clicks_are_not_labels(monkeypatch) -> None:
    monkeypatch.setattr(mouse_events, "synthetic_click_recent", lambda: False)
    learner = _FakeLearner()
    pointer = _Pointer()
    harvester = _harvester(learner, pointer)
    now = _settle(harvester, pointer, 30.0)
    pointer.pressed = True
    harvester.poll_once(now)
    now += 0.06
    pointer.x = 140.0
    pointer.pressed = False
    assert harvester.poll_once(now) is False

    monkeypatch.setattr(mouse_events, "synthetic_click_recent", lambda: True)
    now = _settle(harvester, pointer, now + 0.1)
    pointer.pressed = True
    harvester.poll_once(now)
    now += 0.06
    pointer.pressed = False
    assert harvester.poll_once(now) is False
    assert learner.calls == []


def test_click_learning_ignores_fast_double_click_second_press(monkeypatch) -> None:
    monkeypatch.setattr(mouse_events, "synthetic_click_recent", lambda: False)
    learner = _FakeLearner()
    pointer = _Pointer()
    harvester = _harvester(learner, pointer)
    now = _settle(harvester, pointer, 40.0)
    for index in range(2):
        pointer.pressed = True
        harvester.poll_once(now)
        now += 0.06
        pointer.pressed = False
        harvester.poll_once(now)
        now += 0.04 + (0 if index == 0 else 0.5)
    assert len(learner.calls) == 1
    assert harvester.accepted == 1
    assert harvester.rejected == 1


class _EyeCursor:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.visible = True
        self.changes: list[bool] = []

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)
        self.changes.append(self.visible)


class _VisibilityHarness:
    def __init__(self, *, ready: bool = True, idle_switch: float = 1.0) -> None:
        self.eye = _EyeCursor(ready)
        self.x = 100.0
        self.y = 200.0
        self.pressed = False
        self.hides = 0
        self.shows = 0
        self.mode = mouse_events.MacOSPointerVisibility(
            self.eye,
            idle_switch_sec=idle_switch,
            move_threshold_px=2.0,
            read_button=lambda: self.pressed,
            read_position=lambda: (self.x, self.y),
            hide_cursor=self._hide,
            show_cursor=self._show,
        )
        self.mode._last_activity = 0.0

    def _hide(self) -> None:
        self.hides += 1

    def _show(self) -> None:
        self.shows += 1


def test_pointer_mode_shows_the_eye_cursor_while_the_mouse_rests() -> None:
    harness = _VisibilityHarness()
    harness.mode.poll_once(10.0)
    assert harness.eye.visible is True
    assert (harness.hides, harness.shows) == (1, 0)

    harness.mode.poll_once(10.2)
    assert harness.eye.visible is True
    assert (harness.hides, harness.shows) == (1, 0)


def test_a_click_hands_off_to_exactly_one_system_cursor() -> None:
    harness = _VisibilityHarness()
    harness.mode.poll_once(20.0)
    assert harness.eye.visible is True

    harness.pressed = True
    harness.mode.poll_once(20.05)
    assert harness.eye.visible is False
    assert (harness.hides, harness.shows) == (1, 1)

    for now in (20.08, 20.12, 20.20):
        harness.mode.poll_once(now)
    assert harness.shows == 1
    assert harness.eye.visible is False


def test_mouse_movement_switches_back_and_idle_restores_the_eye() -> None:
    harness = _VisibilityHarness()
    harness.mode.poll_once(30.0)
    assert harness.eye.visible is True

    harness.x += 8.0
    harness.mode.poll_once(30.1)
    assert harness.eye.visible is False
    assert harness.shows == 1

    harness.mode.poll_once(30.9)
    assert harness.eye.visible is False

    harness.mode.poll_once(31.15)
    assert harness.eye.visible is True
    assert harness.hides == 2


def test_eye_cursor_waits_for_the_overlay_before_hiding_the_pointer() -> None:
    harness = _VisibilityHarness(ready=False)
    harness.mode.poll_once(40.0)
    assert harness.eye.changes == []
    assert harness.hides == 0

    harness.eye.ready = True
    harness.mode.poll_once(40.05)
    assert harness.eye.visible is True
    assert harness.eye.changes == [True]
    assert harness.hides == 1


def test_force_eye_mode_skips_the_idle_delay() -> None:
    harness = _VisibilityHarness()
    harness.mode.poll_once(50.0)
    harness.pressed = True
    harness.mode.poll_once(50.05)
    assert harness.eye.visible is False
    harness.pressed = False

    harness.mode.force_eye_mode()
    assert harness.eye.visible is True
    assert harness.shows == 1


def test_start_begins_in_eye_mode_and_stop_restores_the_pointer() -> None:
    harness = _VisibilityHarness()
    harness.mode.start()
    try:
        assert harness.eye.visible is True
        assert harness.hides == 1
    finally:
        harness.mode.stop()
    assert harness.eye.visible is False
    assert harness.shows == 1


def test_seeded_pointer_position_does_not_count_as_movement() -> None:
    harness = _VisibilityHarness()
    harness.mode._last_x, harness.mode._last_y = harness.x, harness.y
    harness.mode.poll_once(60.0)
    assert harness.eye.visible is True
    assert harness.shows == 0
