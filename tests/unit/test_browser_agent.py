"""Unit tests for Playwright Firefox profile sync, planner adapter, agent loop, and voice routing."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from keyless.browser.agent import AgentAction, VoiceBrowserAgent
from keyless.browser.planner import (
    ProviderEndpoint,
    _extract_json_payload,
    build_browser_planner,
    endpoint_from_provider,
)
from keyless.browser.profile_sync import find_default_firefox_profile, sync_firefox_profile
from keyless.browser.session import PageObservation
from keyless.input.pointer import DryRunPointerBackend
from keyless.main import app
from keyless.models.providers.ollama import OllamaProvider
from keyless.models.providers.openai_compat import OpenAICompatProvider
from keyless.router.dispatcher import VoiceCommandRouter


def test_find_and_sync_firefox_profile(tmp_path: Path) -> None:
    fx_root = tmp_path / "firefox_root"
    prof = fx_root / "abcd1234.default-release"
    prof.mkdir(parents=True)

    (fx_root / "profiles.ini").write_text(
        "[Install4F96D1932A9F858E]\nDefault=abcd1234.default-release\nLocked=1\n"
    )

    # Create a real SQLite cookies DB in WAL mode
    cookies_db = prof / "cookies.sqlite"
    with sqlite3.connect(cookies_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        conn.execute("INSERT INTO moz_cookies VALUES ('youtube.com', 'SID', 'abc123')")
        conn.commit()

    (prof / "logins.json").write_text('{"logins": []}')
    (prof / "prefs.js").write_text('user_pref("test.pref", true);')
    (prof / "parent.lock").write_text("locked")
    storage_dir = prof / "storage" / "default" / "https+++youtube.com"
    storage_dir.mkdir(parents=True)
    (storage_dir / "ls.sqlite").write_bytes(b"data")
    (storage_dir / "session.lock").write_text("skip me")

    found = find_default_firefox_profile(roots=(fx_root,))
    assert found == prof

    dst = tmp_path / "playwright-firefox"
    dst.mkdir()
    (dst / "compatibility.ini").write_text("stale")

    target, copied = sync_firefox_profile(src_profile=found, dst_profile=dst)
    assert target == dst
    assert "cookies.sqlite" in copied
    assert "logins.json" in copied
    assert "storage/" in copied
    assert not (dst / "prefs.js").exists()
    assert not (dst / "parent.lock").exists()
    assert not (dst / "compatibility.ini").exists()
    assert (dst / "storage" / "default" / "https+++youtube.com" / "ls.sqlite").exists()
    assert not (dst / "storage" / "default" / "https+++youtube.com" / "session.lock").exists()

    with sqlite3.connect(dst / "cookies.sqlite") as conn:
        rows = conn.execute("SELECT host, name, value FROM moz_cookies").fetchall()
    assert rows == [("youtube.com", "SID", "abc123")]


def test_endpoint_from_provider_adapts_ollama_and_openai_compat() -> None:
    ol = OllamaProvider("qwen2.5:14b", base_url="http://127.0.0.1:11434/")
    ep_ol = endpoint_from_provider(ol)
    assert ep_ol.kind == "ollama"
    assert ep_ol.model == "qwen2.5:14b"
    assert ep_ol.base_url == "http://127.0.0.1:11434"

    oa = OpenAICompatProvider("openai", "gpt-4o-mini", api_key="sk-test")
    ep_oa = endpoint_from_provider(oa)
    assert ep_oa.kind == "openai_compat"
    assert ep_oa.model == "gpt-4o-mini"
    assert ep_oa.api_key == "sk-test"

    direct = ProviderEndpoint(kind="ollama", model="gemma4:e4b", base_url="http://127.0.0.1:11434")
    assert endpoint_from_provider(direct) is direct


def test_extract_json_payload_strips_think_and_fences() -> None:
    raw = '<think>need to click e2</think>\n```json\n{"thought": "ok", "tool": "click", "ref": "e2"}\n```'
    parsed = json.loads(_extract_json_payload(raw))
    assert parsed["tool"] == "click"
    assert parsed["ref"] == "e2"


def test_planner_dual_routing_and_self_repair() -> None:
    calls: list[tuple[str, int]] = []

    class _FakeTextProvider:
        def __init__(self) -> None:
            self.n = 0

        def complete(self, system: str, user: str, *, json: bool = False, images: Any = ()) -> str:
            self.n += 1
            calls.append(("text", len(images)))
            if self.n == 1:
                return "not valid json at all"
            return '<think>fixed</think>{"thought": "repaired", "tool": "navigate", "url": "https://youtube.com"}'

    class _FakeVisionProvider:
        def complete(self, system: str, user: str, *, json: bool = False, images: Any = ()) -> str:
            calls.append(("vision", len(images)))
            return '{"thought": "saw play button", "tool": "click", "ref": "e7"}'

    planner = build_browser_planner(_FakeTextProvider(), _FakeVisionProvider())

    act1 = asyncio.run(planner("sys", "usr", None))
    assert act1.tool == "navigate"
    assert act1.url == "https://youtube.com"
    assert calls == [("text", 0), ("text", 0)]

    act2 = asyncio.run(planner("sys", "usr", b"\xff\xd8jpeg"))
    assert act2.tool == "click"
    assert act2.ref == "e7"
    assert calls[-1] == ("vision", 1)


class _FakeSession:
    def __init__(self) -> None:
        self.is_running = False
        self.actions: list[tuple[str, Any]] = []

    async def start(self) -> None:
        self.is_running = True

    async def observe(self, max_items: int = 65) -> PageObservation:
        return PageObservation(
            url="https://www.youtube.com/",
            title="YouTube",
            tabs=["0: YouTube"],
            manifest='URL: https://www.youtube.com/\nInteractive Elements:\n  [e1] link "Veritasium"',
            text_excerpt="YouTube Home",
        )

    async def navigate(self, url: str) -> str:
        self.actions.append(("navigate", url))
        return f"Navigated to {url}"

    async def click_ref(self, ref: str) -> str:
        self.actions.append(("click", ref))
        return f"Clicked [{ref}]"

    async def screenshot_jpeg(self) -> bytes:
        self.actions.append(("screenshot", None))
        return b"\xff\xd8fakejpeg"


def test_voice_browser_agent_multi_step_and_confirm() -> None:
    session = _FakeSession()
    spoken: list[str] = []
    seen_images: list[bool] = []
    plan_seq = [
        AgentAction(thought="need visual", tool="inspect_visual"),
        AgentAction(thought="confirm first", tool="ask_confirm", text="Play this video?"),
        AgentAction(thought="click video", tool="click", ref="e1"),
        AgentAction(thought="finished", tool="done", text="Playing Veritasium."),
    ]
    idx = {"i": 0}

    async def _planner(sys_p: str, usr_p: str, img: bytes | None) -> AgentAction:
        seen_images.append(img is not None)
        act = plan_seq[idx["i"]]
        idx["i"] += 1
        return act

    agent = VoiceBrowserAgent(session, planner=_planner, speak=spoken.append)  # type: ignore[arg-type]

    async def _run() -> str:
        task = asyncio.create_task(agent.run_goal("play veritasium"))
        while not agent.awaiting_confirmation:
            await asyncio.sleep(0.01)
        agent.resolve_confirmation(True)
        return await task

    result = asyncio.run(_run())
    assert result == "Playing Veritasium."
    assert seen_images == [False, True, False, False]
    assert ("screenshot", None) in session.actions
    assert ("click", "e1") in session.actions
    assert any("Play this video?" in s for s in spoken)
    assert spoken[-1] == "Playing Veritasium."


def test_dispatcher_routes_browser_confirm_gaze_scroll_fast_and_goals() -> None:
    class _FakeAgent:
        is_running = False
        awaiting_confirmation = False

    class _FakeBrowserSession:
        is_running = True

    class _FakeRuntime:
        def __init__(self) -> None:
            self.session = _FakeBrowserSession()
            self.agent = _FakeAgent()
            self.scroll_calls: list[dict[str, Any]] = []
            self.fast_calls: list[tuple[str, Any]] = []
            self.goals: list[str] = []
            self.confirms: list[bool] = []
            self.cancelled = False

        def scroll_at_gaze(self, **kwargs: Any) -> bool:
            self.scroll_calls.append(kwargs)
            return True

        def run_fast_action(self, action_name: str, arg: Any = None) -> bool:
            self.fast_calls.append((action_name, arg))
            return True

        def submit_goal(self, transcript: str) -> None:
            self.goals.append(transcript)

        def confirm(self, approved: bool) -> None:
            self.confirms.append(approved)

        def cancel(self) -> None:
            self.cancelled = True

    ptr = DryRunPointerBackend(1920, 1080)
    rt = _FakeRuntime()
    router = VoiceCommandRouter(
        pointer=ptr,
        gaze_provider=lambda: (420.0, 610.0),
        browser_runtime=rt,  # type: ignore[arg-type]
    )

    # 1. Passive gaze scroll never moves OS pointer
    res_scroll = router.dispatch("scroll down")
    assert res_scroll.route == "browser_scroll"
    assert rt.scroll_calls[-1]["gaze_x"] == 420.0
    assert rt.scroll_calls[-1]["gaze_y"] == 610.0
    assert ptr.last_abs is None

    # 2. Zero-LLM fast reflexes
    assert router.dispatch("pause video").route == "browser_fast"
    assert rt.fast_calls[-1] == ("media_key", "k")
    assert router.dispatch("tab 2").route == "browser_fast"
    assert rt.fast_calls[-1] == ("switch_tab", 1)
    assert router.dispatch("reload page").route == "browser_fast"
    assert rt.fast_calls[-1] == ("reload", None)

    # 3. Explicit browse goal & in-browser click goal
    assert router.dispatch("open youtube and play veritasium").route == "browser_agent"
    assert rt.goals[-1] == "open youtube and play veritasium"
    assert router.dispatch("click the quantum computing video").route == "browser_agent"
    assert rt.goals[-1] == "click the quantum computing video"

    # 4. Non-browser OS command still falls through cleanly
    res_os = router.dispatch("open app terminal")
    assert res_os.route == "os_window_switch"

    # 5. Confirmation & barge-in
    rt.agent.awaiting_confirmation = True
    assert router.dispatch("confirm").route == "browser_confirm"
    assert rt.confirms == [True]
    rt.agent.awaiting_confirmation = False

    rt.agent.is_running = True
    assert router.dispatch("stop").route == "browser_cancel"
    assert rt.cancelled is True


def test_cli_browser_sync_command(tmp_path: Path) -> None:
    src = tmp_path / "src_fx"
    src.mkdir()
    with sqlite3.connect(src / "cookies.sqlite") as conn:
        conn.execute("CREATE TABLE t (k TEXT)")
        conn.commit()
    dst = tmp_path / "dst_fx"

    runner = CliRunner()
    res = runner.invoke(app, ["browser-sync", "--profile", str(src), "--dest", str(dst)])
    assert res.exit_code == 0, res.output
    assert (dst / "cookies.sqlite").exists()


def test_bare_click_with_dom_candidate_and_filler_prefixed_browser_commands() -> None:
    from evdev import ecodes
    from keyless.ipc.firefox_bridge import DOMCandidate

    class _FakeBridge:
        is_connected = True

        def __init__(self) -> None:
            self.clicked: list[int] = []

        def get_dom_candidates(self, *_args: Any) -> list[DOMCandidate]:
            return [
                DOMCandidate(
                    dom_id=42,
                    tag="button",
                    role="button",
                    primary_label="Play",
                    visible_text="Play",
                    aria_label="Play",
                    icon_hints="",
                    aliases=[],
                    gaze_dist_px=12.0,
                    screen_x=500,
                    screen_y=300,
                )
            ]

        def click_candidate(self, dom_id: int, action: str = "click") -> bool:
            self.clicked.append(dom_id)
            return True

    ptr = DryRunPointerBackend(1920, 1080)
    fb = _FakeBridge()
    router = VoiceCommandRouter(
        pointer=ptr,
        gaze_provider=lambda: (505.0, 305.0),
        firefox_bridge=fb,  # type: ignore[arg-type]
    )
    res = router.dispatch("Click.")
    assert res.route == "deterministic"
    assert fb.clicked == [42]

    # Bare "Click." without Firefox bridge falls back to standard left click
    ptr2 = DryRunPointerBackend(1920, 1080)
    router2 = VoiceCommandRouter(pointer=ptr2)
    res2 = router2.dispatch("Click.")
    assert res2.route == "deterministic"
    assert ptr2.clicks[-1] == ecodes.BTN_LEFT

    # Filler words ("Uh scroll down.", "Can you find me a video about GTA 6") route to browser
    class _FakeAgent:
        is_running = False
        awaiting_confirmation = False

    class _FakeBrowserSession:
        is_running = True

    class _FakeRuntime:
        def __init__(self) -> None:
            self.session = _FakeBrowserSession()
            self.agent = _FakeAgent()
            self.scroll_calls: list[dict[str, Any]] = []
            self.goals: list[str] = []

        def scroll_at_gaze(self, **kwargs: Any) -> bool:
            self.scroll_calls.append(kwargs)
            return True

        def submit_goal(self, transcript: str) -> None:
            self.goals.append(transcript)

    rt = _FakeRuntime()
    ptr3 = DryRunPointerBackend(1920, 1080)
    router3 = VoiceCommandRouter(
        pointer=ptr3,
        gaze_provider=lambda: (300.0, 400.0),
        browser_runtime=rt,  # type: ignore[arg-type]
    )
    assert router3.dispatch("Uh scroll down.").route == "browser_scroll"
    assert router3.dispatch("Can you find me a video about GTA 6").route == "browser_agent"
    assert router3.dispatch("Uh open Gemini.").route == "browser_agent"
    assert router3.dispatch("Show me GTA 6 trailer").route == "browser_agent"
    assert router3.dispatch("Check the weather in Toronto").route == "browser_agent"
    assert router3.dispatch("Playwright write hello world").route == "browser_agent"
    assert router3.dispatch("Type GTA 6 in the search box").route == "browser_agent"

    # Plain "Type ..." MUST fall through to system-wide OS typing even when Playwright is running
    res_type = router3.dispatch("Type that didn't work.")
    assert res_type.route == "type"
    assert ptr3.typed_texts[-1] == "that didn't work."

    res_uh_type = router3.dispatch("Uh, type hello world")
    assert res_uh_type.route == "type"
    assert ptr3.typed_texts[-1] == "hello world"

