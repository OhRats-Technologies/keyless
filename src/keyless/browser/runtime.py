"""Thread-safe sync bridge around PlaywrightFirefoxSession and VoiceBrowserAgent."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from keyless.browser.agent import PlannerFn, SpeakFn, VoiceBrowserAgent
from keyless.browser.session import PlaywrightFirefoxSession

logger = logging.getLogger(__name__)


class BrowserRuntime:
    """Runs Playwright Firefox + VoiceBrowserAgent on a dedicated background asyncio thread."""

    def __init__(
        self,
        planner: PlannerFn,
        speak: SpeakFn | None = None,
        *,
        profile_dir: Path | None = None,
        auto_seed_on_first_run: bool = True,
        session: PlaywrightFirefoxSession | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="keyless-playwright",
        )
        self._thread.start()
        self.session = (
            session
            if session is not None
            else PlaywrightFirefoxSession(
                profile_dir=profile_dir,
                auto_seed_on_first_run=auto_seed_on_first_run,
            )
        )
        self.agent = VoiceBrowserAgent(self.session, planner=planner, speak=speak)
        self._active_task: Future[Any] | None = None
        self._loop_task: asyncio.Task[Any] | None = None

    def launch_browser(self) -> None:
        asyncio.run_coroutine_threadsafe(self.session.start(), self._loop).result(timeout=20.0)

    def scroll_at_gaze(
        self,
        gaze_x: float,
        gaze_y: float,
        direction: str = "down",
        amount_px: int = 520,
        continuous: str = "",
        win_origin: tuple[int, int] | None = None,
    ) -> bool:
        if not self.session.is_running:
            return False
        fut = asyncio.run_coroutine_threadsafe(
            self.session.scroll_at_gaze(
                gaze_x,
                gaze_y,
                direction=direction,
                amount_px=amount_px,
                continuous=continuous,
                win_origin=win_origin,
            ),
            self._loop,
        )
        try:
            res = fut.result(timeout=1.5)
            return bool(res and res.get("ok"))
        except Exception as exc:
            logger.debug("scroll_at_gaze failed: %s", exc)
            return False

    def run_fast_action(self, action_name: str, arg: Any = None) -> bool:
        """Execute a deterministic browser reflex (tabs, history, YouTube media keys) without LLM."""
        if not self.session.is_running:
            return False

        async def _exec() -> bool:
            if action_name == "go_back":
                await self.session.go_back()
            elif action_name == "new_tab":
                await self.session.new_tab(str(arg or ""))
            elif action_name == "close_tab":
                await self.session.close_tab()
            elif action_name == "switch_tab":
                await self.session.switch_tab(int(arg))
            elif action_name == "reload":
                p = await self.session.get_active_page()
                await p.reload(wait_until="domcontentloaded")
            elif action_name == "media_key":
                # Works on YouTube/Twitch/HTML5 video: 'k' (play/pause), 'f' (fullscreen), 'm' (mute)
                p = await self.session.get_active_page()
                await p.keyboard.press(str(arg))
            elif action_name == "split_view":
                return await self.session.split_view(str(arg or "add"))
            else:
                return False
            return True

        try:
            return bool(asyncio.run_coroutine_threadsafe(_exec(), self._loop).result(timeout=3.0))
        except Exception as exc:
            logger.debug("run_fast_action(%s) failed: %s", action_name, exc)
            return False

    def submit_goal(self, transcript: str) -> None:
        """Start an agentic browsing goal asynchronously (non-blocking so barge-in works)."""
        def _on_done(task: asyncio.Task[Any]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Browser goal %r failed: %s", transcript, exc)
                print(f"  [Browser] goal failed: {exc}")

        def _start_on_loop() -> None:
            if self._loop_task is not None and not self._loop_task.done():
                self.agent.cancel()
                self._loop_task.cancel()
            task = self._loop.create_task(self.agent.run_goal(transcript))
            task.add_done_callback(_on_done)
            self._loop_task = task

        self._loop.call_soon_threadsafe(_start_on_loop)

    def cancel(self) -> None:
        def _cancel_on_loop() -> None:
            self.agent.cancel()
            if self._loop_task is not None and not self._loop_task.done():
                self._loop_task.cancel()

        self._loop.call_soon_threadsafe(_cancel_on_loop)

    def confirm(self, approved: bool) -> None:
        self._loop.call_soon_threadsafe(self.agent.resolve_confirmation, approved)

    def shutdown(self) -> None:
        try:
            if self.agent.is_running:
                self._loop.call_soon_threadsafe(self.agent.cancel)
            asyncio.run_coroutine_threadsafe(self.session.stop(), self._loop).result(timeout=5.0)
        except Exception:
            pass
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
