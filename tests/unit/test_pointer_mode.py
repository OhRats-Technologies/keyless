"""Single-cursor arbitration: the eye cursor at rest, the hardware cursor in use."""

from __future__ import annotations

from keyless.platform.macos import pointer_mode


class _EyeCursor:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.visible = True
        self.changes: list[bool] = []

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)
        self.changes.append(self.visible)


class _Harness:
    def __init__(self, *, ready: bool = True, idle_switch: float = 1.0) -> None:
        self.eye = _EyeCursor(ready)
        self.x = 100.0
        self.y = 200.0
        self.pressed = False
        self.hides = 0
        self.shows = 0
        self.mode = pointer_mode.MacOSPointerVisibility(
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
    harness = _Harness()
    harness.mode.poll_once(10.0)
    assert harness.eye.visible is True
    assert (harness.hides, harness.shows) == (1, 0)

    harness.mode.poll_once(10.2)
    assert harness.eye.visible is True
    assert (harness.hides, harness.shows) == (1, 0)


def test_a_click_hands_off_to_exactly_one_system_cursor() -> None:
    harness = _Harness()
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
    harness = _Harness()
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
    harness = _Harness(ready=False)
    harness.mode.poll_once(40.0)
    assert harness.eye.changes == []
    assert harness.hides == 0

    harness.eye.ready = True
    harness.mode.poll_once(40.05)
    assert harness.eye.visible is True
    assert harness.eye.changes == [True]
    assert harness.hides == 1


def test_force_eye_mode_skips_the_idle_delay() -> None:
    harness = _Harness()
    harness.mode.poll_once(50.0)
    harness.pressed = True
    harness.mode.poll_once(50.05)
    assert harness.eye.visible is False
    harness.pressed = False

    harness.mode.force_eye_mode()
    assert harness.eye.visible is True
    assert harness.shows == 1


def test_start_begins_in_eye_mode_and_stop_restores_the_pointer() -> None:
    harness = _Harness()
    harness.mode.start()
    try:
        assert harness.eye.visible is True
        assert harness.hides == 1
    finally:
        harness.mode.stop()
    assert harness.eye.visible is False
    assert harness.shows == 1


def test_seeded_pointer_position_does_not_count_as_movement() -> None:
    harness = _Harness()
    harness.mode._last_x, harness.mode._last_y = harness.x, harness.y
    harness.mode.poll_once(60.0)
    assert harness.eye.visible is True
    assert harness.shows == 0


def test_disallowing_the_eye_hands_back_to_the_pointer_and_blocks_idle_return() -> None:
    # Calibration (or a lost face) must leave exactly the hardware pointer on screen.
    harness = _Harness()
    harness.mode.poll_once(30.0)
    assert harness.eye.visible is True

    harness.mode.set_eye_allowed(False)
    assert harness.eye.visible is False
    assert (harness.hides, harness.shows) == (1, 1)
    for now in (31.0, 35.0, 60.0):
        harness.mode.poll_once(now)
    assert harness.eye.visible is False and harness.shows == 1

    harness.mode.set_eye_allowed(True)
    harness.mode.poll_once(61.0)
    assert harness.eye.visible is True
    assert (harness.hides, harness.shows) == (2, 1)
