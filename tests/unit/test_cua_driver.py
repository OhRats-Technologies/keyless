"""Pinned cua-driver runtime + hybrid action backend contract."""

from __future__ import annotations

import json
import os

import pytest


def test_canonical_key_names_fold_aliases() -> None:
    from keyless.input import keys

    assert keys.canonical("PgDn") == "PAGEDOWN"
    assert keys.canonical("page-down") == "PAGEDOWN"
    assert keys.canonical("ArrowLeft") == "LEFT"
    assert keys.canonical("cmd") == "LEFTMETA"
    assert keys.canonical("Super") == "LEFTMETA"
    assert keys.canonical("ctrl") == "LEFTCTRL"
    assert keys.canonical("c") == "C"
    assert keys.canonical("RETURN") == "ENTER"
    assert keys.canonical("esc") == "ESC"
    assert keys.canonical("F4") == "F4"
    with pytest.raises(ValueError):
        keys.canonical("not a key *")
    with pytest.raises(ValueError):
        keys.canonical("")


def test_mac_keycodes_cover_router_keys() -> None:
    from keyless.input import keys

    for name in (
        "LEFTCTRL", "C", "V", "X", "Z", "A", "W", "T", "TAB", "ENTER",
        "ESC", "SPACE", "BACKSPACE", "LEFT", "RIGHT", "UP", "DOWN",
        "HOME", "END", "PAGEUP", "PAGEDOWN", "LEFTALT", "GRAVE",
        "LEFTMETA", "H", "F4", "F10", "1", "2", "8",
    ):
        assert isinstance(keys.mac_keycode(name), int)
    with pytest.raises(NotImplementedError):
        keys.mac_keycode("PLAYPAUSE")


def test_linux_keycodes_need_evdev() -> None:
    from keyless.input import keys

    evdev = pytest.importorskip("evdev")
    assert keys.linux_keycode("LEFTCTRL") == evdev.ecodes.KEY_LEFTCTRL
    assert keys.linux_button(1) == evdev.ecodes.BTN_LEFT
    with pytest.raises(ValueError):
        keys.linux_keycode("NOT_A_KEY")


def test_runtimes_pin_current_platform(tmp_path) -> None:
    from keyless.models import runtimes

    target = runtimes.current_target()
    assert target.asset.startswith(f"cua-driver-rs-{runtimes.CUA_DRIVER_VERSION}-")
    assert target.asset.endswith((".tar.gz", ".zip"))
    # Force an empty data dir so readiness is deterministic on any host.
    info = runtimes.status(data_dir=tmp_path)
    assert info["id"] == "cua-driver"
    assert info["version"] == runtimes.CUA_DRIVER_VERSION
    assert info["ready"] is False
    assert target.asset in info["receipt"] or info["receipt"] == {}


def test_capability_probe_reports_missing_binary(monkeypatch) -> None:
    from keyless.models import runtimes
    from keyless.platform.base import CapabilityStatus
    from keyless.platform.capabilities import probe_cua_driver

    monkeypatch.setattr(
        runtimes,
        "status",
        lambda: {"ready": False, "version": runtimes.CUA_DRIVER_VERSION},
    )
    info = probe_cua_driver()
    assert info.name == "cua_driver"
    assert info.status is CapabilityStatus.UNAVAILABLE
    assert "download cua-driver" in info.action


def test_cua_key_mapping() -> None:
    from keyless.platform.cua.pointer import _cua_key

    assert _cua_key("LEFTCTRL") == "ctrl"
    assert _cua_key("cmd") == "cmd"
    assert _cua_key("C") == "c"
    assert _cua_key("ENTER") == "enter"
    assert _cua_key("F4") == "f4"
    with pytest.raises(NotImplementedError):
        _cua_key("PLAYPAUSE")


class _FakeNative:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def move_absolute(self, x: float, y: float) -> None:
        self.calls.append(("move_absolute", x, y))

    def move_relative(self, dx: float, dy: float) -> None:
        self.calls.append(("move_relative", dx, dy))

    def click(self, button: int = 1) -> None:
        self.calls.append(("click", button))

    def double_click(self, button: int = 1) -> None:
        self.calls.append(("double_click", button))

    def scroll(self, dy: int = 0, dx: int = 0) -> None:
        self.calls.append(("scroll", dy, dx))

    def key_combo(self, *keys: str) -> None:
        self.calls.append(("key_combo", keys))

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    def close(self) -> None:
        self.calls.append(("close",))


class _FakeDriver:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        self.running = True

    def call(self, tool: str, args: dict | None = None, timeout_s: float = 60.0) -> dict:
        self.calls.append((tool, args))
        if self.fail:
            from keyless.platform.cua.driver import CuaDriverError

            raise CuaDriverError("boom")
        return {"content": [], "structured": {}, "isError": False}

    def stop(self) -> None:
        self.calls.append(("stop",))


def test_hybrid_moves_are_native_clicks_go_cua() -> None:
    from keyless.platform.cua.pointer import CuaDriverPointerBackend

    native = _FakeNative()
    driver = _FakeDriver()
    ptr = CuaDriverPointerBackend(native, driver)  # type: ignore[arg-type]
    ptr._cua_disabled = False
    driver.running = True  # type: ignore[attr-defined]
    # Pretend the driver is already started.
    ptr._driver = driver  # type: ignore[assignment]
    ptr.move_absolute(100.0, 200.0)
    assert native.calls == [("move_absolute", 100.0, 200.0)]
    assert driver.calls == []
    ptr.click(1)
    assert driver.calls[-1][0] == "click"
    assert driver.calls[-1][1]["x"] == 100.0
    assert driver.calls[-1][1]["button"] == "left"
    ptr.key_combo("LEFTCTRL", "C")
    assert driver.calls[-1] == ("hotkey", {"keys": ["ctrl", "c"]})
    ptr.type_text("hi")
    assert driver.calls[-1] == ("type_text", {"text": "hi"})


def test_hybrid_falls_back_to_native_on_driver_failure() -> None:
    from keyless.platform.cua.pointer import CuaDriverPointerBackend

    native = _FakeNative()
    driver = _FakeDriver(fail=True)
    ptr = CuaDriverPointerBackend(native, driver)  # type: ignore[arg-type]
    ptr._driver = driver  # type: ignore[assignment]
    ptr.move_absolute(10.0, 20.0)
    ptr.click(2)
    assert ("click", 2) in native.calls
    ptr.type_text("hello")
    assert ("type_text", "hello") in native.calls


def test_driver_jsonrpc_roundtrip_over_pipes() -> None:
    import threading
    import time

    from keyless.platform.cua.driver import CuaDriverManager

    mgr = CuaDriverManager(binary="/nonexistent/cua-driver")
    # Two pipes: manager->daemon (requests) and daemon->manager (replies).
    req_r, req_w = os.pipe()
    rep_r, rep_w = os.pipe()

    class _Stub:
        def __init__(self) -> None:
            self.stdin = os.fdopen(req_w, "wb", buffering=0)
            self.stdout = os.fdopen(rep_r, "rb", buffering=0)
            self.stderr = None

        def poll(self) -> None:
            return None

    mgr._proxy = _Stub()  # type: ignore[assignment]
    mgr._running = True
    reader = threading.Thread(target=mgr._read_loop, daemon=True)
    reader.start()
    req_reader = os.fdopen(req_r, "rb", buffering=0)
    rep_writer = os.fdopen(rep_w, "wb", buffering=0)

    box: dict = {}

    def _ask() -> None:
        box["reply"] = mgr._request("tools/call", {"name": "check_permissions"})

    asker = threading.Thread(target=_ask, daemon=True)
    asker.start()

    # Read the request the manager wrote, answer it with the same id.
    line = b""
    deadline = time.monotonic() + 5.0
    while not line.endswith(b"\n") and time.monotonic() < deadline:
        chunk = req_reader.read(1)
        if not chunk:
            break
        line += chunk
    request = json.loads(line.decode())
    reply = {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"structuredContent": {"accessibility": True}},
    }
    rep_writer.write((json.dumps(reply) + "\n").encode())
    rep_writer.flush()
    asker.join(timeout=5.0)
    mgr._running = False
    assert box["reply"]["structuredContent"] == {"accessibility": True}


class _RetinaDriver(_FakeDriver):
    def call(self, tool: str, args: dict | None = None, timeout_s: float = 60.0) -> dict:
        result = super().call(tool, args, timeout_s)
        if tool == "get_screen_size":
            result["structured"] = {"width": 1470, "height": 956, "scale_factor": 2.0}
        return result


def test_hybrid_converts_gaze_points_to_screen_pixels() -> None:
    from keyless.platform.cua.pointer import CuaDriverPointerBackend

    native = _FakeNative()
    driver = _RetinaDriver()
    ptr = CuaDriverPointerBackend(native, driver)  # type: ignore[arg-type]
    ptr.move_absolute(352.25, 183.0)
    ptr.click(1)
    args = driver.calls[-1][1]
    assert (args["x"], args["y"]) == (704.5, 366.0)
    assert args["target"] == {"kind": "desktop", "display_id": "primary"}
    ptr.scroll(dy=-3)
    assert driver.calls[-1][1]["y"] == 366.0
    assert [t for t, _ in driver.calls].count("get_screen_size") == 1
    assert native.calls == [("move_absolute", 352.25, 183.0)]


def test_macos_cursor_query_uses_cgevent_struct_abi() -> None:
    import ctypes
    import sys

    if sys.platform != "darwin":
        pytest.skip("CoreGraphics pointer is macOS-only")
    from keyless.platform.macos import pointer as mp

    if not mp._load():
        pytest.skip("CoreGraphics unavailable")
    mp.macOSPointerBackend()._current_position()
    create = mp._fn(
        "CGEventCreateMouseEvent", ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, mp._CGPoint, ctypes.c_int],
    )
    # Created but never posted, so the real cursor does not move.
    event = create(None, mp._kCGEventMouseMoved, mp._CGPoint(321.5, 654.25), 0)
    assert event
    try:
        pt = mp._fn("CGEventGetLocation")(event)
        assert (pt.x, pt.y) == (321.5, 654.25)
    finally:
        mp._fn("CFRelease", None, [ctypes.c_void_p])(event)
