"""Eye-cursor overlay: independent motion, never the system pointer."""

from __future__ import annotations

import time
from types import SimpleNamespace

from keyless.platform.cua import agent_cursor


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.soon: list[tuple[str, dict]] = []
        self.running = True

    def start(self) -> _FakeDriver:
        return self

    def call(self, tool: str, args: dict | None = None, timeout_s: float = 60.0) -> dict:
        self.calls.append((tool, args))
        return {"content": [], "structured": {}, "isError": False}

    def call_soon(self, tool: str, args: dict | None = None) -> bool:
        self.soon.append((tool, args or {}))
        return True

    def stop(self) -> None:
        self.running = False


class _FakeOverlay:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float, str, int]] = []
        self.closed = False

    @staticmethod
    def available() -> tuple[bool, str]:
        return True, ""

    def move_absolute(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    def move_relative(self, dx: float, dy: float) -> None:
        pass

    def click_at(
        self, x: float, y: float, *, button: str = "left", count: int = 1
    ) -> bool:
        self.clicks.append((x, y, button, count))
        return True

    def scroll_at(self, x: float, y: float, *, direction: str, amount: int) -> bool:
        self.moves.append((x, y))
        return True

    def close(self) -> None:
        self.closed = True


class _FakeAction:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def note_gaze_position(self, x: float, y: float) -> None:
        self.calls.append(("note", x, y))

    def click(self, button: int = 1) -> None:
        self.calls.append(("click", button))

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    def close(self) -> None:
        self.calls.append(("close",))


class _WindowDriver(_FakeDriver):
    def call(self, tool: str, args: dict | None = None, timeout_s: float = 60.0) -> dict:
        if tool == "list_windows":
            return {
                "content": [],
                "isError": False,
                "structured": {
                    "windows": [
                        {
                            "app_name": "Example",
                            "pid": 42,
                            "window_id": 7,
                            "layer": 0,
                            "is_on_screen": True,
                            "z_index": 3,
                            "bounds": {"x": 100.0, "y": 50.0, "width": 500.0, "height": 400.0},
                        }
                    ]
                },
            }
        if tool == "get_window_state":
            return {
                "content": [],
                "isError": False,
                "structured": {
                    "window_bounds": {"x": 100.0, "y": 50.0, "width": 500.0, "height": 400.0},
                    "screenshot_width": 1000,
                    "screenshot_height": 800,
                },
            }
        return super().call(tool, args, timeout_s)


def _wait_ready(overlay: agent_cursor.AgentCursorOverlay, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not overlay.ready and time.monotonic() < deadline:
        time.sleep(0.01)
    assert overlay.ready


def test_overlay_configures_a_named_session_and_sends_overlay_only_moves() -> None:
    driver = _FakeDriver()
    overlay = agent_cursor.AgentCursorOverlay(driver=driver)  # type: ignore[arg-type]
    _wait_ready(overlay)
    overlay.move_absolute(120.0, 340.0)
    time.sleep(0.05)
    overlay.close()

    tools = [tool for tool, _ in driver.calls]
    assert tools[:3] == [
        "start_session",
        "set_agent_cursor_motion",
        "set_agent_cursor_enabled",
    ]
    assert driver.calls[0][1]["session"] == agent_cursor.SESSION_LABEL
    assert driver.calls[0][1]["cursor_motion"]["timing"] == "fixed"
    assert driver.calls[0][1]["cursor_motion"]["idle_hide_ms"] == 0
    moves = [args for tool, args in driver.soon if tool == "move_cursor"]
    assert moves[-1] == {"session": agent_cursor.SESSION_LABEL, "x": 120.0, "y": 340.0}
    assert all("target" not in args and "scope" not in args for args in moves)
    assert tools[-2:] == ["set_agent_cursor_enabled", "end_session"]
    assert driver.calls[-2][1]["enabled"] is False


def test_acknowledge_snaps_to_target_then_settles() -> None:
    driver = _FakeDriver()
    overlay = agent_cursor.AgentCursorOverlay(driver=driver)  # type: ignore[arg-type]
    _wait_ready(overlay)
    overlay.move_absolute(0.0, 0.0)
    time.sleep(0.04)
    overlay.acknowledge_at(100.0, 200.0)
    time.sleep(0.35)
    overlay.close()
    moves = [args for tool, args in driver.soon if tool == "move_cursor"]
    assert any(args["x"] > 100.0 and args["y"] < 200.0 for args in moves)
    assert moves[-1] == {"session": agent_cursor.SESSION_LABEL, "x": 100.0, "y": 200.0}


def test_visibility_handoff_waits_until_the_daemon_applies_it() -> None:
    driver = _FakeDriver()
    overlay = agent_cursor.AgentCursorOverlay(driver=driver)  # type: ignore[arg-type]
    _wait_ready(overlay)

    overlay.set_visible(False)
    toggles = [
        args["enabled"]
        for tool, args in driver.calls
        if tool == "set_agent_cursor_enabled"
    ]
    assert toggles[-1] is False
    assert overlay._visible is False

    overlay.set_visible(True)
    toggles = [
        args["enabled"]
        for tool, args in driver.calls
        if tool == "set_agent_cursor_enabled"
    ]
    assert toggles[-1] is True
    assert overlay._visible is True
    overlay.close()


def test_overlay_clicks_the_window_under_the_eye_not_the_real_pointer() -> None:
    driver = _WindowDriver()
    overlay = agent_cursor.AgentCursorOverlay(driver=driver)  # type: ignore[arg-type]
    _wait_ready(overlay)
    assert overlay.click_at(350.0, 250.0, button="right", count=2) is True
    args = [args for tool, args in driver.calls if tool == "click"][-1]
    assert args["target"] == {"kind": "window", "pid": 42, "window_id": 7}
    assert (args["x"], args["y"]) == (500.0, 400.0)
    assert args["button"] == "right"
    assert args["count"] == 2
    assert overlay.click_at(900.0, 900.0) is False
    overlay.close()


def test_create_gaze_sink_keeps_system_pointer_when_overlay_is_off(monkeypatch) -> None:
    action = _FakeAction()
    config = SimpleNamespace(gaze_cursor_control=True, gaze_cursor_overlay=False)
    pointer, overlay, move = agent_cursor.create_gaze_sink(config, action, dry_run=False)
    assert pointer is action
    assert overlay is None
    assert move is True


def test_create_gaze_sink_disables_gaze_when_overlay_cannot_start(monkeypatch) -> None:
    action = _FakeAction()
    config = SimpleNamespace(gaze_cursor_control=True, gaze_cursor_overlay=True)
    monkeypatch.setattr(agent_cursor.sys, "platform", "darwin")
    monkeypatch.setattr(
        agent_cursor.AgentCursorOverlay,
        "available",
        staticmethod(lambda: (False, "missing")),
    )
    pointer, overlay, move = agent_cursor.create_gaze_sink(config, action, dry_run=False)
    assert pointer is action
    assert overlay is None
    assert move is False


def test_create_gaze_sink_routes_motion_to_overlay_and_actions_to_pointer(monkeypatch) -> None:
    action = _FakeAction()
    config = SimpleNamespace(gaze_cursor_control=True, gaze_cursor_overlay=True)
    monkeypatch.setattr(agent_cursor.sys, "platform", "darwin")
    monkeypatch.setattr(agent_cursor, "AgentCursorOverlay", _FakeOverlay)

    pointer, overlay, move = agent_cursor.create_gaze_sink(config, action, dry_run=False)
    assert move is True
    assert isinstance(overlay, _FakeOverlay)
    fake = overlay
    pointer.move_absolute(10.0, 20.0)
    assert fake.moves == [(10.0, 20.0)]
    assert action.calls == [("note", 10.0, 20.0)]
    pointer.click(2)
    assert fake.clicks[-1] == (10.0, 20.0, "right", 1)
    assert ("click", 2) not in action.calls
    pointer.type_text("hello")
    assert action.calls[-1] == ("type_text", "hello")
    pointer.close()
    assert fake.closed is True
    assert action.calls[-1] == ("close",)


def test_an_existing_sink_is_not_wrapped_twice(monkeypatch) -> None:
    fake = _FakeOverlay()
    action = _FakeAction()
    sink = agent_cursor.AgentCursorSink(fake, action)  # type: ignore[arg-type]
    config = SimpleNamespace(gaze_cursor_control=True, gaze_cursor_overlay=True)
    pointer, overlay, move = agent_cursor.create_gaze_sink(config, sink, dry_run=False)
    assert pointer is sink
    assert overlay is None
    assert move is True
