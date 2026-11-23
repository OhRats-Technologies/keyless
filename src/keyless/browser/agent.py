"""Voice-driven multi-step Playwright browser agent with confirmation and barge-in."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from keyless.browser.session import PageObservation, PlaywrightFirefoxSession

logger = logging.getLogger(__name__)


class AgentAction(BaseModel):
    """Structured single-step action returned by the browser planner LLM."""

    thought: str = Field(
        default="",
        description="Brief reasoning for the next browser action.",
    )
    tool: Literal[
        "navigate",
        "search_youtube",
        "search_web",
        "click",
        "type",
        "press_key",
        "scroll",
        "go_back",
        "new_tab",
        "close_tab",
        "switch_tab",
        "inspect_visual",
        "ask_confirm",
        "done",
    ] = Field(description="Browser tool to invoke.")
    ref: str = Field(default="", description="Element ref like 'e12' for click/type.")
    url: str = Field(default="", description="Target URL for navigate or new_tab.")
    text: str = Field(
        default="",
        description="Text to type, confirmation prompt, or final spoken summary for done.",
    )
    press_enter: bool = Field(default=False, description="Press Enter after typing.")
    direction: str = Field(default="down", description="Scroll direction: up or down.")
    amount: int = Field(default=600, description="Scroll distance in pixels.")
    tab_index: int = Field(default=0, description="0-based tab index for switch_tab.")
    key: str = Field(default="", description="Playwright keyboard key for press_key.")


PlannerFn = Callable[[str, str, bytes | None], Awaitable[AgentAction]]
SpeakFn = Callable[[str], Any]

_SYSTEM_PROMPT = """You are Keyless, an agentic hands-free voice browser assistant controlling a visible Firefox window.
At each step you receive the user's goal, recent step history, and a ref-tagged DOM snapshot ([e1], [e2], ...) of the active tab.
Return ONLY valid JSON matching the AgentAction schema.

Available tools and examples:
- Search YouTube directly (BEST for finding/playing videos):
  {"thought": "Search YouTube for GTA 6", "tool": "search_youtube", "text": "GTA 6"}
- Search Google directly (BEST for web lookups):
  {"thought": "Search web for weather", "tool": "search_web", "text": "Boston weather"}
- Open a website directly:
  {"thought": "Go to GitHub", "tool": "navigate", "url": "https://github.com"}
- Click an element using its EXACT id from Interactive Elements (NEVER leave ref empty!):
  {"thought": "Open the GTA 6 trailer", "tool": "click", "ref": "e12"}
- Type into an input using its EXACT id from Interactive Elements:
  {"thought": "Type query in search box", "tool": "type", "ref": "e1", "text": "GTA 6", "press_enter": true}
- Finish once the user's goal is achieved:
  {"thought": "Video is now playing", "tool": "done", "text": "Playing the GTA 6 video."}

Rules:
- For "find a video about X" or "play a video about X": first call `search_youtube` with `text: "X"`, then on the results page call `click` on a matching video link's `ref`, then call `done`!
- For `click` and `type`, `ref` MUST be a real id like `"e5"` from Interactive Elements (without brackets). Never pass `ref: ""`.
- Call `inspect_visual` ONLY when the DOM manifest lacks visual cues needed to pick the right element.
- Before irreversible actions (payments, deleting data, sending messages/emails, posting publicly), call `ask_confirm` with a brief question in `text`.
- Keep `thought` to 1 short sentence."""


class VoiceBrowserAgent:
    """Executes multi-step voice goals against a live PlaywrightFirefoxSession."""

    def __init__(
        self,
        session: PlaywrightFirefoxSession,
        planner: PlannerFn,
        speak: SpeakFn | None = None,
        max_steps: int = 10,
        confirm_timeout_sec: float = 30.0,
    ) -> None:
        self.session = session
        self.planner = planner
        self.speak = speak
        self.max_steps = max_steps
        self.confirm_timeout_sec = confirm_timeout_sec

        self._running = False
        self._cancelled = False
        self._awaiting_confirmation = False
        self._confirm_event: asyncio.Event | None = None
        self._confirm_result = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def awaiting_confirmation(self) -> bool:
        return self._awaiting_confirmation

    def _say(self, message: str) -> None:
        text = (message or "").strip()
        if not text or self.speak is None:
            return
        try:
            self.speak(text)
        except Exception as exc:
            logger.debug("Browser agent speak() failed: %s", exc)

    def cancel(self) -> None:
        """Request immediate cancellation of the active browsing goal."""
        self._cancelled = True
        if self._confirm_event is not None:
            self._confirm_result = False
            self._confirm_event.set()

    def resolve_confirmation(self, approved: bool) -> None:
        """Resolve a pending safety confirmation from voice input."""
        if not self._awaiting_confirmation or self._confirm_event is None:
            return
        self._confirm_result = bool(approved)
        self._confirm_event.set()

    async def _wait_for_confirmation(self, prompt: str) -> bool:
        self._awaiting_confirmation = True
        self._confirm_result = False
        self._confirm_event = asyncio.Event()
        self._say(f"{prompt} Say confirm or cancel.")
        try:
            await asyncio.wait_for(self._confirm_event.wait(), timeout=self.confirm_timeout_sec)
            return self._confirm_result and not self._cancelled
        except asyncio.TimeoutError:
            self._say("Confirmation timed out. Cancelling.")
            return False
        finally:
            self._awaiting_confirmation = False
            self._confirm_event = None

    @staticmethod
    def _build_user_prompt(
        goal: str,
        obs: PageObservation,
        history: list[str],
        step: int,
        max_steps: int,
    ) -> str:
        hist_block = "\n".join(f"  {h}" for h in history[-6:]) if history else "  (none yet)"
        return (
            f"User Goal: {goal}\n"
            f"Step: {step}/{max_steps}\n"
            f"Recent History:\n{hist_block}\n\n"
            f"Active Tab Snapshot:\n{obs.manifest}\n\n"
            f"Visible Text Excerpt:\n{obs.text_excerpt[:1000]}"
        )

    async def run_goal(self, goal: str) -> str:
        """Run the observe-plan-act loop until done, cancelled, or max_steps reached."""
        self._running = True
        self._cancelled = False
        history: list[str] = []
        pending_image: bytes | None = None

        try:
            await self.session.start()
            for step in range(1, self.max_steps + 1):
                if self._cancelled:
                    self._say("Cancelled.")
                    return "Cancelled."

                obs = await self.session.observe()
                user_prompt = self._build_user_prompt(goal, obs, history, step, self.max_steps)

                action = await self.planner(_SYSTEM_PROMPT, user_prompt, pending_image)
                pending_image = None

                if self._cancelled:
                    self._say("Cancelled.")
                    return "Cancelled."

                if action.tool == "done":
                    final_msg = action.text or "Done."
                    self._say(final_msg)
                    return final_msg

                if action.tool == "inspect_visual":
                    pending_image = await self.session.screenshot_jpeg()
                    history.append(f"step {step}: inspect_visual -> captured screenshot")
                    continue

                if action.tool == "ask_confirm":
                    question = action.text or "Should I proceed with this action?"
                    approved = await self._wait_for_confirmation(question)
                    if not approved:
                        self._say("Okay, cancelled.")
                        return "Cancelled by user."
                    history.append(f"step {step}: user confirmed '{question}'")
                    continue

                try:
                    outcome = await self._execute_tool(action)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    outcome = f"ERROR executing {action.tool}: {exc}"
                    logger.warning("Browser tool %s failed: %s", action.tool, exc)

                print(f"  [Browser] step {step}: {action.tool}({action.ref or action.url or action.text}) -> {outcome}")
                history.append(f"step {step}: {action.tool} -> {outcome}")

            msg = "Reached the maximum number of browser steps."
            self._say(msg)
            return msg
        except asyncio.CancelledError:
            self._say("Cancelled.")
            return "Cancelled."
        finally:
            self._running = False
            self._awaiting_confirmation = False
            self._confirm_event = None

    async def _execute_tool(self, action: AgentAction) -> str:
        if action.tool == "navigate":
            return await self.session.navigate(action.url)
        if action.tool == "search_youtube":
            return await self.session.search_youtube(action.text or action.url)
        if action.tool == "search_web":
            return await self.session.search_web(action.text or action.url)
        if action.tool == "click":
            if hasattr(self.session, "click_ref"):
                try:
                    return await self.session.click_ref(action.ref, fallback_text=action.text)
                except TypeError:
                    return await self.session.click_ref(action.ref)
        if action.tool == "type":
            return await self.session.type_ref(
                action.ref, action.text, press_enter=action.press_enter
            )
        if action.tool == "press_key":
            return await self.session.press_key(action.key or "Enter")
        if action.tool == "scroll":
            return await self.session.scroll(direction=action.direction, amount=action.amount)
        if action.tool == "go_back":
            return await self.session.go_back()
        if action.tool == "new_tab":
            return await self.session.new_tab(action.url)
        if action.tool == "close_tab":
            return await self.session.close_tab()
        if action.tool == "switch_tab":
            return await self.session.switch_tab(action.tab_index)
        return f"Unknown tool: {action.tool}"
