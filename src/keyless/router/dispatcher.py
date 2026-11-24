"""Sub-10ms deterministic voice command router and prefix-based AI dispatcher."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from evdev import ecodes

from keyless.input.keys import BUTTON_LEFT, BUTTON_MIDDLE, BUTTON_RIGHT

from keyless.input.pointer import DryRunPointerBackend, PointerBackend
from keyless.ipc.firefox_bridge import FirefoxDOMBridge
from keyless.ipc.state import SharedGazeReader
from keyless.router.command_splitter import execute_compound_voice_commands
from keyless.router.gaze_vlm_clicker import GazeVLMClicker, parse_voice_click_command
from keyless.routing.llm import LLMAgent
from keyless.router.scroll_handler import parse_voice_scroll_command

if TYPE_CHECKING:
    from keyless.browser.runtime import BrowserRuntime

logger = logging.getLogger(__name__)

_CONFIRM_YES = {"confirm", "yes", "approve", "do it", "go ahead", "proceed", "send it"}
_CONFIRM_NO = {"cancel", "no", "stop", "abort", "never mind", "dont", "don't"}
_BARGE_IN_STOP = {"stop", "cancel", "abort", "stop browsing", "never mind", "halt"}

_EXPLICIT_BROWSE_PREFIXES = (
    "browse ",
    "browser ",
    "playwright ",
    "search ",
    "google ",
    "look up ",
    "look for ",
    "open ",
    "go to ",
    "navigate to ",
    "watch ",
    "play ",
    "find ",
    "show me ",
    "pull up ",
    "youtube ",
    "check ",
    "weather ",
    "what is the weather ",
    "whats the weather ",
    "what's the weather ",
    "summarize ",
    "read this ",
    "what does this page ",
)

_NON_BROWSE_OPEN_PREFIXES = (
    "open app ",
    "open tab ",
    "open new tab",
    "open a new tab",
    "go to tab ",
    "go to top",
    "go to bottom",
    "go to sleep",
    "play video",
    "play the video",
)

_IN_BROWSER_DETERMINISTIC_EXCLUSIONS = {
    "click here",
    "click now",
    "click it",
    "click that",
    "select all",
    "press enter",
    "press escape",
}

_TAB_NUM_MAP = {
    "1": 0, "one": 0, "first": 0,
    "2": 1, "two": 1, "second": 1,
    "3": 2, "three": 2, "third": 2,
    "4": 3, "four": 3, "fourth": 3,
    "5": 4, "five": 4, "fifth": 4,
    "6": 5, "six": 5, "7": 6, "seven": 6, "8": 7, "eight": 7, "9": 8, "nine": 8,
}
_TAB_SWITCH_RE = re.compile(r"^(?:switch to\s+)?tab\s+(\w+)$", re.IGNORECASE)


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of routing a spoken transcript."""

    route: str
    transcript: str
    detail: str
    payload: dict[str, Any] | None = None


def _normalize_command(text: str) -> str:
    lowered = text.lower().replace("-", " ")
    cleaned = re.sub(r"[^\w\s]", "", lowered).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\bre center\b", "recenter", cleaned)
    cleaned = re.sub(
        r"^(?:(?:uh|um|mm|hmm|yeah|okay|ok|hey|so|alright|please|can you|could you)\s+)+",
        "",
        cleaned,
    )
    return cleaned.strip()


class VoiceCommandRouter:
    """Routes transcripts between <10ms deterministic actions and prefix-routed AI workflows."""

    def __init__(
        self,
        pointer: PointerBackend,
        llm_agent: LLMAgent | None = None,
        gaze_reader: SharedGazeReader | None = None,
        gaze_provider: Callable[[], tuple[float, float] | None] | None = None,
        browser_callback: Callable[[dict[str, Any]], None] | None = None,
        dictate_unprefixed: bool = False,
        gaze_vlm_clicker: GazeVLMClicker | None = None,
        gaze_state_provider: Callable[[], tuple[float, float, np.ndarray | None] | None]
        | None = None,
        on_before_click: Callable[[int, int], None] | None = None,
        firefox_bridge: FirefoxDOMBridge | None = None,
        browser_runtime: BrowserRuntime | None = None,
    ) -> None:
        self.pointer = pointer
        self.llm_agent = llm_agent
        self.gaze_reader = gaze_reader
        self.gaze_provider = gaze_provider
        self.browser_callback = browser_callback
        self.dictate_unprefixed = dictate_unprefixed
        self.gaze_vlm_clicker = gaze_vlm_clicker
        self.gaze_state_provider = gaze_state_provider
        self.on_before_click = on_before_click
        self.browser_runtime = browser_runtime
        self.firefox_bridge = (
            firefox_bridge
            if firefox_bridge is not None
            else (gaze_vlm_clicker.firefox_bridge if gaze_vlm_clicker is not None else None)
        )
        self._window_cycle_step = 0
        self._window_cycle_last_t = 0.0

        self._media_map: dict[str, tuple[str, str]] = {
            "pause": ("pause", "PLAYPAUSE"),
            "pause video": ("pause", "PLAYPAUSE"),
            "pause the video": ("pause", "PLAYPAUSE"),
            "play": ("play", "PLAYPAUSE"),
            "play video": ("play", "PLAYPAUSE"),
            "play the video": ("play", "PLAYPAUSE"),
            "resume": ("resume", "PLAYPAUSE"),
            "resume video": ("resume", "PLAYPAUSE"),
            "mute": ("mute", "MUTE"),
            "mute video": ("mute", "MUTE"),
            "mute audio": ("mute", "MUTE"),
            "unmute": ("unmute", "MUTE"),
            "unmute video": ("unmute", "MUTE"),
            "unmute audio": ("unmute", "MUTE"),
            "fullscreen": ("fullscreen", "F"),
            "full screen": ("fullscreen", "F"),
            "exit fullscreen": ("exit_fullscreen", "ESC"),
            "exit full screen": ("exit_fullscreen", "ESC"),
        }

        # Require explicit "click here" / "click now" / "left click" / "tap" for in-place clicks.
        # Bare "click" is ignored so truncated utterances never blind-click random spots.
        self._deterministic_map: dict[str, tuple[str, Callable[[], None]]] = {
            "click here": ("click_left", lambda: self.pointer.click(BUTTON_LEFT)),
            "click now": ("click_left", lambda: self.pointer.click(BUTTON_LEFT)),
            "tap": ("click_left", lambda: self.pointer.click(BUTTON_LEFT)),
            "tap here": ("click_left", lambda: self.pointer.click(BUTTON_LEFT)),
            "left click": ("click_left", lambda: self.pointer.click(BUTTON_LEFT)),
            "right click": ("click_right", lambda: self.pointer.click(BUTTON_RIGHT)),
            "middle click": ("click_middle", lambda: self.pointer.click(BUTTON_MIDDLE)),
            "double click": ("double_click", lambda: self.pointer.double_click(BUTTON_LEFT)),
            "scroll down": ("scroll_down", lambda: self.pointer.scroll(dy=-4)),
            "scroll up": ("scroll_up", lambda: self.pointer.scroll(dy=4)),
            "page down": ("page_down", lambda: self.pointer.key_combo("PAGEDOWN")),
            "page up": ("page_up", lambda: self.pointer.key_combo("PAGEUP")),
            "go back": ("nav_back", lambda: self.pointer.key_combo("LEFTALT", "LEFT")),
            "back": ("nav_back", lambda: self.pointer.key_combo("LEFTALT", "LEFT")),
            "go forward": (
                "nav_forward",
                lambda: self.pointer.key_combo("LEFTALT", "RIGHT"),
            ),
            "forward": (
                "nav_forward",
                lambda: self.pointer.key_combo("LEFTALT", "RIGHT"),
            ),
            "copy": ("copy", lambda: self.pointer.key_combo("LEFTCTRL", "C")),
            "paste": ("paste", lambda: self.pointer.key_combo("LEFTCTRL", "V")),
            "cut": ("cut", lambda: self.pointer.key_combo("LEFTCTRL", "X")),
            "undo": ("undo", lambda: self.pointer.key_combo("LEFTCTRL", "Z")),
            "redo": (
                "redo",
                lambda: self.pointer.key_combo("LEFTCTRL", "LEFTSHIFT", "Z"),
            ),
            "select all": ("select_all", lambda: self.pointer.key_combo("LEFTCTRL", "A")),
            "close tab": ("close_tab", lambda: self.pointer.key_combo("LEFTCTRL", "W")),
            "new tab": ("new_tab", lambda: self.pointer.key_combo("LEFTCTRL", "T")),
            "next tab": ("next_tab", lambda: self.pointer.key_combo("LEFTCTRL", "TAB")),
            "previous tab": (
                "prev_tab",
                lambda: self.pointer.key_combo("LEFTCTRL", "LEFTSHIFT", "TAB"),
            ),
            "prev tab": (
                "prev_tab",
                lambda: self.pointer.key_combo("LEFTCTRL", "LEFTSHIFT", "TAB"),
            ),
            "switch tab": ("os_window_next", self._cycle_next_window),
            "switch window": ("os_window_next", self._cycle_next_window),
            "switch app": ("os_window_next", self._cycle_next_window),
            "cycle window": ("os_window_next", self._cycle_next_window),
            "alt tab": (
                "os_alt_tab",
                lambda: self.pointer.key_combo("LEFTALT", "TAB"),
            ),
            "next window": ("os_window_next", self._cycle_next_window),
            "previous window": ("os_window_prev", self._cycle_prev_window),
            "prev window": ("os_window_prev", self._cycle_prev_window),
            "last window": ("os_window_prev", self._cycle_prev_window),
            "switch back": (
                "os_alt_shift_tab",
                lambda: self.pointer.key_combo(
                    "LEFTALT", "LEFTSHIFT", "TAB"
                ),
            ),
            "switch same app": (
                "os_alt_grave",
                lambda: self.pointer.key_combo("LEFTALT", "GRAVE"),
            ),
            "next app window": (
                "os_alt_grave",
                lambda: self.pointer.key_combo("LEFTALT", "GRAVE"),
            ),
            "alt grave": (
                "os_alt_grave",
                lambda: self.pointer.key_combo("LEFTALT", "GRAVE"),
            ),
            "overview": ("os_overview", lambda: self.pointer.key_combo("LEFTMETA")),
            "show windows": ("os_overview", lambda: self.pointer.key_combo("LEFTMETA")),
            "activities": ("os_overview", lambda: self.pointer.key_combo("LEFTMETA")),
            "next workspace": (
                "os_workspace_next",
                lambda: self.pointer.key_combo(
                    "LEFTCTRL", "LEFTALT", "RIGHT"
                ),
            ),
            "workspace right": (
                "os_workspace_next",
                lambda: self.pointer.key_combo(
                    "LEFTCTRL", "LEFTALT", "RIGHT"
                ),
            ),
            "previous workspace": (
                "os_workspace_prev",
                lambda: self.pointer.key_combo(
                    "LEFTCTRL", "LEFTALT", "LEFT"
                ),
            ),
            "prev workspace": (
                "os_workspace_prev",
                lambda: self.pointer.key_combo(
                    "LEFTCTRL", "LEFTALT", "LEFT"
                ),
            ),
            "workspace left": (
                "os_workspace_prev",
                lambda: self.pointer.key_combo(
                    "LEFTCTRL", "LEFTALT", "LEFT"
                ),
            ),
            "enter": ("enter", lambda: self.pointer.key_combo("ENTER")),
            "press enter": ("enter", lambda: self.pointer.key_combo("ENTER")),
            "escape": ("escape", lambda: self.pointer.key_combo("ESC")),
            "press escape": ("escape", lambda: self.pointer.key_combo("ESC")),
            "tab": ("tab", lambda: self.pointer.key_combo("TAB")),
            "space": ("space", lambda: self.pointer.key_combo("SPACE")),
            "backspace": ("backspace", lambda: self.pointer.key_combo("BACKSPACE")),
            "delete line": ("delete_line", self._delete_line),
        }

    def _delete_line(self) -> None:
        self.pointer.key_combo("HOME")
        self.pointer.key_combo("LEFTSHIFT", "END")
        self.pointer.key_combo("BACKSPACE")

    def current_gaze_xy(self) -> tuple[float, float] | None:
        if self.gaze_provider is not None:
            pos = self.gaze_provider()
            if pos is not None:
                return pos
        if self.gaze_reader is None:
            return None
        snap = self.gaze_reader.read()
        if snap is None:
            return None
        return (snap.x, snap.y)

    def _current_gaze_and_feat(self) -> tuple[float, float, np.ndarray | None]:
        if self.gaze_state_provider is not None:
            state = self.gaze_state_provider()
            if state is not None:
                return state
        gaze_xy = self.current_gaze_xy()
        if gaze_xy is not None:
            return (gaze_xy[0], gaze_xy[1], None)
        if self.gaze_vlm_clicker is not None:
            return (
                self.gaze_vlm_clicker.screen_w * 0.5,
                self.gaze_vlm_clicker.screen_h * 0.5,
                None,
            )
        return (960.0, 540.0, None)

    def _execute_vlm_intent(self, raw: str, norm: str) -> DispatchResult | None:
        if self.gaze_vlm_clicker is None:
            return None
        intent = parse_voice_click_command(raw)
        if intent is None:
            return None
        if intent.action == "type_into" and re.match(r"^(?:type|dictate)\s*:", raw, flags=re.IGNORECASE):
            return None
        if norm == "click this" and self.browser_callback is not None:
            return None

        gx, gy, feat = self._current_gaze_and_feat()
        res = self.gaze_vlm_clicker.resolve_voice_target(raw, gx, gy, feat)
        if res is None:
            return DispatchResult(
                "gaze_vlm_miss",
                raw,
                f"No UI target found for {intent.target_query!r}",
            )

        resolved_intent, (tx, ty), tier_info = res
        if self.on_before_click is not None:
            self.on_before_click(tx, ty)
        self.pointer.move_absolute(tx, ty)
        if (
            self.firefox_bridge is not None
            and self.firefox_bridge.is_connected
            and hasattr(self.firefox_bridge, "send_pointer_anchor")
        ):
            self.firefox_bridge.send_pointer_anchor(tx, ty)
        if not isinstance(self.pointer, DryRunPointerBackend):
            time.sleep(0.025)

        # Tier-0 Firefox DOM or Tier-0.5 Desktop AT-SPI already executed the click when invoked=True.
        already_executed = tier_info.startswith("Tier-0 Firefox DOM (") or (
            resolved_intent.action == "click"
            and tier_info.startswith("Tier-0.5 Desktop AT-SPI (")
            and "invoked=True)" in tier_info
        )
        if not already_executed:
            if resolved_intent.action == "click":
                self.pointer.click(BUTTON_LEFT)
            elif resolved_intent.action == "double_click":
                self.pointer.double_click(BUTTON_LEFT)
            elif resolved_intent.action == "right_click":
                self.pointer.click(BUTTON_RIGHT)
            elif resolved_intent.action == "type_into":
                self.pointer.click(BUTTON_LEFT)
                if not isinstance(self.pointer, DryRunPointerBackend):
                    time.sleep(0.025)
                self.pointer.type_text(resolved_intent.text_to_type)

        return DispatchResult(
            "gaze_vlm_click",
            raw,
            f"{tier_info} -> ({tx}, {ty})",
            payload={
                "action": resolved_intent.action,
                "x": tx,
                "y": ty,
                "tier": tier_info,
                "text": resolved_intent.text_to_type,
            },
        )

    def _cycle_next_window(self) -> None:
        """Step through individual open windows via Alt+Tab (hold Alt, tap Tab N times).

        Incrementing the tap count on consecutive 'next window' commands within 8s visits every
        individual window in GNOME's `switch-windows` list without treating tiled/paired windows
        as one and without A<->B MRU ping-pong.
        """
        now = time.monotonic()
        if now - self._window_cycle_last_t <= 8.0 and self._window_cycle_step > 0:
            self._window_cycle_step = (self._window_cycle_step % 8) + 1
        else:
            self._window_cycle_step = 1
        self._window_cycle_last_t = now
        self.pointer.hold_and_tap("LEFTALT", "TAB", count=self._window_cycle_step)

    def _cycle_prev_window(self) -> None:
        """Jump back to the immediately preceding window in MRU order (Alt+Shift+Tab)."""
        self._window_cycle_step = 0
        self.pointer.key_combo("LEFTALT", "LEFTSHIFT", "TAB")

    _APP_SEARCH_ALIASES: dict[str, str] = {
        "vscode": "visual studio code",
        "vs code": "visual studio code",
        "code": "visual studio code",
        "chrome": "google chrome",
    }

    def _focus_os_app_via_overview(self, app_name: str) -> None:
        """Focus or launch an OS window/app in GNOME Shell via Super + search + Enter."""
        query = self._APP_SEARCH_ALIASES.get(app_name.strip().lower(), app_name.strip())
        self.pointer.key_combo("LEFTMETA")
        if not isinstance(self.pointer, DryRunPointerBackend):
            time.sleep(0.18)
        self.pointer.type_text(query)
        if not isinstance(self.pointer, DryRunPointerBackend):
            time.sleep(0.15)
        self.pointer.key_combo("ENTER")

    def _handle_tab_or_window_command(self, norm: str, raw: str) -> DispatchResult | None:
        ordinal_keys = {
            "1": "1",
            "first": "1",
            "one": "1",
            "2": "2",
            "second": "2",
            "two": "2",
            "3": "3",
            "third": "3",
            "three": "3",
            "4": "4",
            "fourth": "4",
            "four": "4",
            "5": "5",
            "fifth": "5",
            "five": "5",
            "6": "6",
            "sixth": "6",
            "six": "6",
            "7": "7",
            "seventh": "7",
            "seven": "7",
            "8": "8",
            "eighth": "8",
            "eight": "8",
        }

        # 0. New browser tab: "new tab", "open a new tab", "new tab <url/query>"
        m_new_tab = re.match(
            r"^(?:(?:open|create)\s+(?:a\s+)?)?new\s+tab(?:\s+(?:(?:to|for|with|on)\s+)?(.+))?$",
            raw.strip().rstrip(".,!?"),
            flags=re.IGNORECASE,
        )
        if m_new_tab:
            target_q = (m_new_tab.group(1) or "").strip() or None
            if self.firefox_bridge is not None and self.firefox_bridge.is_connected:
                if self.firefox_bridge.new_tab(target_q):
                    detail = f"Firefox new tab ({target_q})" if target_q else "Firefox new tab"
                    return DispatchResult("tab_new", raw, detail)
            if target_q is None:
                self.pointer.key_combo("LEFTCTRL", "T")
                return DispatchResult("deterministic", raw, "new_tab")

        # 0.5. Close / minimize / maximize active OS window
        if norm in (
            "close window",
            "close this window",
            "close the window",
            "close app",
            "close this app",
            "close the app",
            "close application",
            "alt f4",
        ):
            self.pointer.key_combo("LEFTALT", "F4")
            return DispatchResult("deterministic", raw, "close_window")

        if norm in ("minimize window", "minimize this window", "minimize the window", "minimize"):
            self.pointer.key_combo("LEFTMETA", "H")
            return DispatchResult("deterministic", raw, "minimize_window")

        if norm in ("maximize window", "maximize this window", "maximize the window", "maximize"):
            self.pointer.key_combo("LEFTALT", "F10")
            return DispatchResult("deterministic", raw, "maximize_window")

        # 0.55. Split / snap / tile active OS window (Super + Arrow keys)
        m_snap = re.match(
            r"^(?:(?:move|snap|tile|split)\s+(?:(?:this|the)\s+)?(?:window\s+)?(?:to\s+)?(?:the\s+)?|window\s+)"
            r"(left|right|up|down)$",
            norm,
        )
        if m_snap:
            direction = m_snap.group(1)
            key_map = {
                "left": ecodes.KEY_LEFT,
                "right": ecodes.KEY_RIGHT,
                "up": ecodes.KEY_UP,
                "down": ecodes.KEY_DOWN,
            }
            self.pointer.key_combo(ecodes.KEY_LEFTMETA, key_map[direction])
            return DispatchResult("deterministic", raw, f"window_snap_{direction}")

        if norm in ("unmaximize", "unmaximize window", "restore window", "restore this window"):
            self.pointer.key_combo(ecodes.KEY_LEFTMETA, ecodes.KEY_DOWN)
            return DispatchResult("deterministic", raw, "window_snap_down")

        # 0.6. Explicit next/previous window cycling: strip leading fillers ("a", "the", "and",
        # "uh", "go to", "switch to", "move to") AND catch common STT near-homophones
        # ("text window", "net window", "nest window", "ex window") so they NEVER open OS search!
        stripped_win = re.sub(
            r"^(?:(?:uh|um|and|a|an|the|to|go\s+to|switch\s+to|move\s+to)\s+)+",
            "",
            norm,
        )
        if stripped_win in (
            "next window",
            "other window",
            "another window",
            "text window",
            "net window",
            "nest window",
            "neck window",
            "necks window",
            "ex window",
        ):
            self._cycle_next_window()
            return DispatchResult("deterministic", raw, "os_window_next")

        if stripped_win in (
            "previous window",
            "prev window",
            "last window",
        ):
            self._cycle_prev_window()
            return DispatchResult("deterministic", raw, "os_window_prev")

        # 1. Explicit OS app/window focus: "switch window to X", "focus X", "open app X", or "antigravity window"
        _forbidden_app_tokens = {
            "close",
            "open",
            "new",
            "show",
            "this",
            "that",
            "next",
            "previous",
            "prev",
            "last",
            "other",
            "another",
            "cycle",
            "switch",
            "sync",
            "align",
            "minimize",
            "maximize",
        }
        m_os = re.match(
            r"^(?:switch\s+(?:window|app)\s+(?:to\s+)?|focus\s+(?:window\s+|app\s+|on\s+)?|open\s+app\s+)(.+)$",
            norm,
        )
        if m_os:
            target = re.sub(r"\s+(?:window|app)$", "", m_os.group(1).strip()).strip()
            target_tokens = set(target.split())
            if target and not (target_tokens & _forbidden_app_tokens):
                self._focus_os_app_via_overview(target)
                return DispatchResult("os_window_switch", raw, f"OS switch/focus: {target}")

        m_app_suffix = re.match(r"^(?:the\s+)?(.+?)\s+(?:window|app)$", norm)
        if m_app_suffix and norm not in self._deterministic_map:
            target = m_app_suffix.group(1).strip()
            target_tokens = set(target.split())
            if target and not (target_tokens & _forbidden_app_tokens) and len(target.split()) <= 2:
                self._focus_os_app_via_overview(target)
                return DispatchResult("os_window_switch", raw, f"OS switch/focus: {target}")

        # 2. Explicit browser tab switch: "switch browser tab to X", "go to tab X", "tab 2", "second tab"
        browser_target: str | None = None
        m_br = re.match(
            r"^(?:switch\s+browser\s+tab\s+(?:to\s+)?|go\s+to\s+tab\s+|open\s+tab\s+|tab\s+)(.+)$",
            norm,
        )
        if m_br:
            browser_target = m_br.group(1).strip()
        elif re.match(r"^(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|last)\s+tab$", norm):
            browser_target = norm.rsplit(" ", 1)[0].strip()

        if browser_target:
            if self.firefox_bridge is not None and self.firefox_bridge.is_connected:
                title = self.firefox_bridge.switch_tab(browser_target)
                if title is not None:
                    return DispatchResult("tab_switch", raw, f"Firefox tab: {title}")
            if browser_target in ordinal_keys:
                self.pointer.key_combo("LEFTALT", ordinal_keys[browser_target])
                return DispatchResult("deterministic", raw, f"tab_ordinal:{browser_target}")
            if self.gaze_vlm_clicker is not None:
                vlm_tab = self._execute_vlm_intent(f"click {browser_target}", f"click {browser_target}")
                if vlm_tab is not None and vlm_tab.route == "gaze_vlm_click":
                    return vlm_tab
            return DispatchResult("tab_miss", raw, f"No matching browser tab: {browser_target}")

        # 3. Ambiguous "switch to X" or "switch tab to X": prefer matching Firefox tab if connected, else click tab / OS app!
        m_ambig = re.match(
            r"^(?:switch\s+(?:tab\s+)?to\s+|switch\s+tab\s+|go\s+to\s+)(?:the\s+)?(.+)$",
            norm,
        )
        if m_ambig:
            raw_target = m_ambig.group(1).strip()
            wants_tab = raw_target.endswith(" tab")
            target = re.sub(r"\s+tab$", "", raw_target).strip()
            if target in ("next", "next tab"):
                self.pointer.key_combo("LEFTCTRL", "TAB")
                return DispatchResult("deterministic", raw, "next_tab")
            if target in ("prev", "previous", "previous tab"):
                self.pointer.key_combo("LEFTCTRL", "LEFTSHIFT", "TAB")
                return DispatchResult("deterministic", raw, "prev_tab")
            if target and self.firefox_bridge is not None and self.firefox_bridge.is_connected:
                title = self.firefox_bridge.switch_tab(target)
                if title is not None:
                    return DispatchResult("tab_switch", raw, f"Firefox tab: {title}")
            if target and wants_tab and self.gaze_vlm_clicker is not None:
                vlm_tab = self._execute_vlm_intent(f"click {target}", f"click {target}")
                if vlm_tab is not None and vlm_tab.route == "gaze_vlm_click":
                    return vlm_tab
            if target:
                self._focus_os_app_via_overview(target)
                return DispatchResult("os_window_switch", raw, f"OS switch/focus: {target}")

        return None

    def dispatch(self, transcript: str) -> DispatchResult:
        """Route a transcribed utterance, supporting one-breath compound trailing commands."""
        pause_s = 0.0 if isinstance(self.pointer, DryRunPointerBackend) else 0.12
        _, results = execute_compound_voice_commands(
            transcript,
            self._dispatch_single,
            pause_between_s=pause_s,
        )
        if not results:
            return self._dispatch_single(transcript)
        if len(results) == 1:
            return results[0]
        return DispatchResult(
            results[-1].route,
            transcript.strip(),
            " -> ".join(f"{r.route}:{r.detail}" for r in results),
            payload={"steps": [r.__dict__ for r in results]},
        )

    def _is_playwright_firefox_focused(self) -> bool:
        """Check whether Playwright Firefox is running and is the focused desktop window."""
        if self.browser_runtime is None or not self.browser_runtime.session.is_running:
            return False
        if self.gaze_vlm_clicker is not None:
            return self.gaze_vlm_clicker._is_firefox_active()
        return True

    def _active_window_origin(self) -> tuple[int, int] | None:
        if self.gaze_vlm_clicker is None:
            return None
        try:
            ctx = self.gaze_vlm_clicker.detector.capturer.get_active_window_context(
                self.gaze_vlm_clicker.screen_w,
                self.gaze_vlm_clicker.screen_h,
                desktop_atspi=self.gaze_vlm_clicker.desktop_atspi,
            )
            if ctx.rect is not None:
                return (int(ctx.rect[0]), int(ctx.rect[1]))
        except Exception:
            pass
        return None

    def _try_handle_browser_voice(self, raw: str, norm: str) -> DispatchResult | None:
        """Handle confirmation, barge-in, passive gaze scrolling, fast reflexes, and agentic goals."""
        br = self.browser_runtime
        if br is None:
            return None

        cmd = raw.lower().strip(" .,!?\"'")
        if not cmd:
            return None

        # 1. Pending safety confirmation ("Say confirm or cancel")
        if br.agent.awaiting_confirmation:
            if cmd in _CONFIRM_YES or norm in _CONFIRM_YES:
                br.confirm(True)
                return DispatchResult("browser_confirm", raw, "Confirmed browser action")
            if cmd in _CONFIRM_NO or norm in _CONFIRM_NO:
                br.confirm(False)
                return DispatchResult("browser_confirm", raw, "Cancelled browser action")

        # 2. Immediate barge-in cancellation while the agent is mid-task
        if br.agent.is_running and (
            cmd in _BARGE_IN_STOP
            or norm in _BARGE_IN_STOP
            or norm.startswith(("stop ", "cancel ", "abort "))
        ):
            br.cancel()
            return DispatchResult("browser_cancel", raw, "Cancelled active browser task")

        in_browser = self._is_playwright_firefox_focused()

        # 3. Passive Gaze-Region Scrolling (<20ms, no LLM, never moves OS cursor)
        scroll_cmd = parse_voice_scroll_command(norm) or parse_voice_scroll_command(raw)
        if scroll_cmd is not None and in_browser:
            gx, gy, _ = self._current_gaze_and_feat()
            win_origin = self._active_window_origin()
            px_step = (
                240
                if scroll_cmd.amount == "small"
                else (760 if scroll_cmd.amount == "large" else 500)
            )
            if br.scroll_at_gaze(
                gaze_x=gx,
                gaze_y=gy,
                direction=scroll_cmd.direction,
                amount_px=px_step,
                continuous=scroll_cmd.continuous,
                win_origin=win_origin,
            ):
                return DispatchResult(
                    "browser_scroll",
                    raw,
                    f"Playwright gaze scroll ({scroll_cmd.direction}, amount={scroll_cmd.amount}, continuous='{scroll_cmd.continuous}')",
                )

        # 4. Zero-LLM deterministic browser reflexes (tabs, history, YouTube media controls)
        if in_browser:
            if norm in ("go back", "back", "previous page"):
                if br.run_fast_action("go_back"):
                    return DispatchResult("browser_fast", raw, "go_back")
            if norm in ("new tab", "open new tab", "open a new tab"):
                if br.run_fast_action("new_tab"):
                    return DispatchResult("browser_fast", raw, "new_tab")
            if norm in ("close tab", "close this tab"):
                if br.run_fast_action("close_tab"):
                    return DispatchResult("browser_fast", raw, "close_tab")
            if norm in ("reload", "refresh", "reload page", "refresh page"):
                if br.run_fast_action("reload"):
                    return DispatchResult("browser_fast", raw, "reload")
            if norm in ("pause video", "play video", "resume video", "toggle play"):
                if br.run_fast_action("media_key", "k"):
                    return DispatchResult("browser_fast", raw, "media_key:k")
            if norm in ("fullscreen", "full screen", "exit fullscreen", "toggle fullscreen"):
                if br.run_fast_action("media_key", "f"):
                    return DispatchResult("browser_fast", raw, "media_key:f")
            if norm in ("mute video", "unmute video", "toggle mute"):
                if br.run_fast_action("media_key", "m"):
                    return DispatchResult("browser_fast", raw, "media_key:m")
            if norm in (
                "theater mode",
                "theatre mode",
                "press theater mode",
                "press theatre mode",
                "enter theater mode",
                "enter theatre mode",
                "exit theater mode",
                "exit theatre mode",
                "toggle theater mode",
                "toggle theatre mode",
            ):
                if br.run_fast_action("media_key", "t"):
                    return DispatchResult("browser_fast", raw, "media_key:t")
            if norm in (
                "captions",
                "subtitles",
                "toggle captions",
                "toggle subtitles",
                "turn on captions",
                "turn off captions",
            ):
                if br.run_fast_action("media_key", "c"):
                    return DispatchResult("browser_fast", raw, "media_key:c")
            if norm in ("miniplayer", "mini player", "toggle miniplayer"):
                if br.run_fast_action("media_key", "i"):
                    return DispatchResult("browser_fast", raw, "media_key:i")

            m_tab = _TAB_SWITCH_RE.match(norm)
            if m_tab and m_tab.group(1) in _TAB_NUM_MAP:
                tab_idx = _TAB_NUM_MAP[m_tab.group(1)]
                if br.run_fast_action("switch_tab", tab_idx):
                    return DispatchResult("browser_fast", raw, f"switch_tab:{tab_idx}")

        # 5. Agentic Browser Loop:
        #    - Fires anywhere on explicit browse/search/watch/weather/playwright commands
        #    - Fires on click/select/fill/ask/press or targeted "type/write X in Y" when Playwright Firefox is focused
        #    - Plain "type <text>" / "dictate <text>" (without "in/into <target>" or "playwright/browser" prefix)
        #      ALWAYS falls through to system-wide OS typing so it works in any focused app!
        is_explicit_browse = (
            (norm.startswith(_EXPLICIT_BROWSE_PREFIXES) or cmd.startswith(_EXPLICIT_BROWSE_PREFIXES))
            and not cmd.startswith(_NON_BROWSE_OPEN_PREFIXES)
            and not norm.startswith(_NON_BROWSE_OPEN_PREFIXES)
        )
        is_targeted_browser_type = bool(
            re.match(r"^(?:type|write|enter)\s+.+\s+(?:in|into|on)\s+.+$", norm)
        )
        is_in_browser_goal = (
            in_browser
            and norm not in _IN_BROWSER_DETERMINISTIC_EXCLUSIONS
            and (
                is_targeted_browser_type
                or norm.startswith(("click ", "select ", "fill ", "ask ", "press "))
                or cmd.startswith(("click ", "select ", "fill ", "ask ", "press "))
            )
        )

        if is_explicit_browse or is_in_browser_goal:
            br.submit_goal(raw)
            return DispatchResult("browser_agent", raw, f"Submitted browser goal: {raw}")

        return None

    def _dispatch_single(self, transcript: str, *, _from_llm: bool = False) -> DispatchResult:
        """Route a single transcribed utterance into deterministic, Gaze-VLM, Type, Terminal, or Browser actions."""
        raw = transcript.strip()
        if not raw:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            return DispatchResult("empty", raw, "Ignored empty transcript.")

        norm = _normalize_command(raw)

        # 0. Playwright Firefox browser agent, barge-in, confirmations, passive gaze scrolling, and fast reflexes
        browser_res = self._try_handle_browser_voice(raw, norm)
        if browser_res is not None:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            return browser_res

        # 1. Zero-latency deterministic control layer.
        # In-place click words click at the live gaze point (not wherever the physical mouse is).
        _gaze_click_words = {
            "click": ("click_left", BUTTON_LEFT, False),
            "click here": ("click_left", BUTTON_LEFT, False),
            "click now": ("click_left", BUTTON_LEFT, False),
            "click it": ("click_left", BUTTON_LEFT, False),
            "click that": ("click_left", BUTTON_LEFT, False),
            "tap": ("click_left", BUTTON_LEFT, False),
            "tap here": ("click_left", BUTTON_LEFT, False),
            "left click": ("click_left", BUTTON_LEFT, False),
            "right click": ("click_right", BUTTON_RIGHT, False),
            "middle click": ("click_middle", BUTTON_MIDDLE, False),
            "double click": ("double_click", BUTTON_LEFT, True),
        }
        if norm in _gaze_click_words:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            action_name, button, is_double = _gaze_click_words[norm]
            has_live_gaze = self.gaze_state_provider is not None or self.gaze_provider is not None
            where = "at pointer"
            if has_live_gaze:
                gx, gy, _ = self._current_gaze_and_feat()
                tx, ty = int(gx), int(gy)
                # If Firefox DOM bridge is active and a green-highlighted DOM element is under gaze,
                # click that exact DOM element directly so slight gaze jitter never misses the button.
                if (
                    button == BUTTON_LEFT
                    and not is_double
                    and self.firefox_bridge is not None
                    and self.firefox_bridge.is_connected
                    and (
                        self.gaze_vlm_clicker is None
                        or self.gaze_vlm_clicker._is_firefox_active()
                    )
                ):
                    mon_x, mon_y, mon_w, mon_h = (
                        self.gaze_vlm_clicker._monitor_rect(gx, gy)
                        if self.gaze_vlm_clicker is not None
                        else (0, 0, 0, 0)
                    )
                    cands = self.firefox_bridge.get_dom_candidates(gx, gy, mon_x, mon_y, mon_w, mon_h)
                    if cands:
                        best = min(cands, key=lambda c: float(getattr(c, "gaze_dist_px", 9999.0)))
                        if float(getattr(best, "gaze_dist_px", 9999.0)) <= 165.0:
                            cx = int(getattr(best, "screen_x", getattr(best, "center_x", tx)))
                            cy = int(getattr(best, "screen_y", getattr(best, "center_y", ty)))
                            label = (
                                getattr(best, "primary_label", "")
                                or getattr(best, "label", "")
                                or getattr(best, "tag", "element")
                            )
                            if self.on_before_click is not None:
                                self.on_before_click(cx, cy)
                            self.pointer.move_absolute(cx, cy)
                            if self.firefox_bridge.click_candidate(best.dom_id, action="click"):
                                return DispatchResult(
                                    "deterministic",
                                    raw,
                                    f"click_dom ({label}) at ({cx}, {cy})",
                                )
                if self.on_before_click is not None:
                    self.on_before_click(tx, ty)
                self.pointer.move_absolute(tx, ty)
                if not isinstance(self.pointer, DryRunPointerBackend):
                    time.sleep(0.025)
                where = f"at gaze ({tx}, {ty})"
            if is_double:
                self.pointer.double_click(button)
            else:
                self.pointer.click(button)
            return DispatchResult("deterministic", raw, f"{action_name} {where}")

        if norm in self._media_map:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            dom_action, fallback_key = self._media_map[norm]
            if self.firefox_bridge is not None and self.firefox_bridge.is_connected:
                if self.firefox_bridge.execute_media_action(dom_action):
                    return DispatchResult("media_dom", raw, f"Firefox DOM media: {dom_action}")
            self.pointer.key_combo(fallback_key)
            return DispatchResult("deterministic", raw, f"media_key:{dom_action}")

        if not norm.endswith("window") and norm not in ("switch tab", "switch app"):
            self._window_cycle_step = 0

        # Explicit manual window-origin sync ("anchor window" / "align window" / "bind window" / "sync window")
        # Also accept "sink window" as an STT homophone for "sync window".
        # Must run BEFORE _handle_tab_or_window_command so "<app> window" doesn't treat it as an app name!
        if norm in ("anchor window", "bind window", "align window", "sync window", "sink window"):
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            gx, gy, _ = self._current_gaze_and_feat()
            tx, ty = int(gx), int(gy)
            if (
                self.firefox_bridge is not None
                and self.firefox_bridge.is_connected
                and hasattr(self.firefox_bridge, "send_pointer_anchor")
            ):
                self.firefox_bridge.send_pointer_anchor(tx, ty)
            self.pointer.move_absolute(tx, ty)
            if not isinstance(self.pointer, DryRunPointerBackend):
                time.sleep(0.02)
                self.pointer.move_relative(1.0, 0.0)
            return DispatchResult("sync_window", raw, f"Anchored Firefox window origin at ({tx}, {ty})")

        tab_res = self._handle_tab_or_window_command(norm, raw)
        if tab_res is not None:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            return tab_res

        # Voice command: "save icon as <name>" or "remember icon as <name>"
        if norm.startswith(("save icon as ", "remember icon as ")) and self.gaze_vlm_clicker is not None:
            self.gaze_vlm_clicker.clear_pending_snapshot()
            icon_name = re.split(r"\bas\b", raw, maxsplit=1, flags=re.IGNORECASE)[1].strip(" .,!?\"'")
            gx, gy, _ = self._current_gaze_and_feat()
            cx, cy = int(gx), int(gy)
            det = self.gaze_vlm_clicker.detector
            x0, y0 = max(0, cx - 22), max(0, cy - 22)
            if det._capture_fn is not None:
                patch = det._capture_fn(x0, y0, 44, 44)
            else:
                patch = det.capturer.capture_rect(x0, y0, 44, 44)
            det.icon_bank.save_icon_patch(icon_name, patch)
            return DispatchResult("save_icon", raw, f"Learned visual icon template for '{icon_name}'")

        # Gaze-aware Firefox DOM scrolling & gaze-warped native app scrolling
        scroll_cmd = parse_voice_scroll_command(raw)
        if scroll_cmd is not None:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            gx, gy, _ = self._current_gaze_and_feat()
            firefox_active = (
                self.gaze_vlm_clicker._is_firefox_active()
                if self.gaze_vlm_clicker is not None
                else True
            )
            if (
                firefox_active
                and self.firefox_bridge is not None
                and self.firefox_bridge.is_connected
                and hasattr(self.firefox_bridge, "scroll_page")
            ):
                if self.firefox_bridge.scroll_page(
                    gaze_x=gx,
                    gaze_y=gy,
                    direction=scroll_cmd.direction,
                    amount=scroll_cmd.amount,
                    continuous=scroll_cmd.continuous,
                ):
                    return DispatchResult(
                        "scroll_dom",
                        raw,
                        f"Firefox DOM scroll ({scroll_cmd.direction}, amount={scroll_cmd.amount}, continuous='{scroll_cmd.continuous}')",
                    )
            has_live_gaze = (
                self.gaze_state_provider is not None or self.gaze_provider is not None
            )
            if norm not in self._deterministic_map or has_live_gaze:
                if scroll_cmd.wheel_ticks != 0:
                    if has_live_gaze:
                        self.pointer.move_absolute(int(gx), int(gy))
                    if scroll_cmd.direction in ("left", "right"):
                        self.pointer.scroll(dx=scroll_cmd.wheel_ticks, dy=0)
                    else:
                        self.pointer.scroll(dx=0, dy=scroll_cmd.wheel_ticks)
                return DispatchResult(
                    "scroll",
                    raw,
                    f"scroll:{scroll_cmd.direction}:{scroll_cmd.amount}:{scroll_cmd.continuous}",
                )

        if norm in self._deterministic_map:
            if self.gaze_vlm_clicker is not None:
                self.gaze_vlm_clicker.clear_pending_snapshot()
            action_name, fn = self._deterministic_map[norm]
            fn()
            return DispatchResult("deterministic", raw, action_name)

        # 1.5. Gaze-Foveated Voice + Vision Grounding (Tier-1 OCR -> Tier-2 gemma4:e4b -> Tier-3 Fullscreen)
        vlm_res = self._execute_vlm_intent(raw, norm)
        if vlm_res is not None:
            return vlm_res

        if self.gaze_vlm_clicker is not None:
            self.gaze_vlm_clicker.clear_pending_snapshot()

        # 2. Prefix: "Type ..." -> Direct raw text injection via uinput
        raw_no_filler = re.sub(
            r"^(?:(?:uh|um|mm|hmm|yeah|okay|ok|hey|so|alright|please|can you|could you)[\s,]+)+",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        m_type = re.match(r"^(?:type|dictate)\b[\s,.:;-]*(.+)$", raw_no_filler, flags=re.IGNORECASE)
        if m_type:
            text_body = m_type.group(1).strip()
            self.pointer.type_text(text_body)
            return DispatchResult("type", raw, f"Typed {len(text_body)} chars")

        # 3. Prefix: "Terminal ..." -> Generate bash command and place onto prompt (no Enter)
        m_term = re.match(r"^(?:terminal|shell|bash)\b[\s,.:;-]*(.+)$", raw, flags=re.IGNORECASE)
        if m_term:
            instruction = m_term.group(1).strip()
            if self.llm_agent is None:
                return DispatchResult("error", raw, "No LLMAgent configured for Terminal route.")
            cmd = self.llm_agent.generate_shell_command(instruction)
            self.pointer.type_text(cmd)
            return DispatchResult("terminal", raw, cmd, payload={"command": cmd})

        # 3.5. Prefix: "Ask ..." / "Assistant ..." / "Explain ..." -> Spoken AI response
        m_ask = re.match(
            r"^(?:ask|assistant|hey assistant|hey keyless|answer|explain)\b[\s,.:;-]*(.+)$",
            raw,
            flags=re.IGNORECASE,
        )
        if m_ask:
            question = m_ask.group(1).strip()
            if self.llm_agent is None:
                return DispatchResult(
                    "assistant_unavailable",
                    raw,
                    "No AI assistant provider is configured.",
                    payload={"spoken": True},
                )
            reply = self.llm_agent.answer_question(question)
            return DispatchResult(
                "assistant",
                raw,
                reply,
                payload={"reply": reply, "spoken": True},
            )

        # 4. Prefix: "Browser ..." (or gaze-contextual queries like "What's this" / "Summarize this")
        m_browser = re.match(
            r"^(?:browser|firefox|web)\b[\s,.:;-]*(.+)$",
            raw,
            flags=re.IGNORECASE,
        )
        is_gaze_prompt = bool(
            re.match(
                r"^(?:what(?:'?s| is) this|summarize this|fill this|click this)\b",
                norm,
            )
        )
        if m_browser or is_gaze_prompt:
            instruction = m_browser.group(1).strip() if m_browser else raw
            gaze_xy = self.current_gaze_xy()
            payload: dict[str, Any] = {
                "type": "browser_command",
                "instruction": instruction,
                "gaze": {"x": gaze_xy[0], "y": gaze_xy[1]} if gaze_xy else None,
            }
            if self.browser_callback is not None:
                self.browser_callback(payload)
            return DispatchResult("browser", raw, instruction, payload=payload)

        # 5. LLM intelligent intent router fallback (before giving up as unmatched)
        if not _from_llm and self.llm_agent is not None:
            llm_res = self._dispatch_via_llm(raw)
            if llm_res is not None:
                return llm_res

        # 6. Unprefixed dictation fallback
        if self.dictate_unprefixed:
            self.pointer.type_text(raw + " ")
            return DispatchResult("type_fallback", raw, f"Dictated {len(raw)} chars")

        return DispatchResult(
            "unmatched",
            raw,
            "No prefix matched (say 'Type ...', 'Terminal ...', 'Browser ...', or a command).",
        )

    def _dispatch_via_llm(self, raw: str) -> DispatchResult | None:
        """Use the configured LLM backend to interpret natural-language voice intent."""
        assert self.llm_agent is not None
        active_app = ""
        if self.gaze_vlm_clicker is not None and self.gaze_vlm_clicker.desktop_atspi is not None:
            try:
                app_name, _ = self.gaze_vlm_clicker.desktop_atspi.get_active_window_info()
                active_app = str(app_name or "")
            except Exception:
                pass
        try:
            plan = self.llm_agent.route_voice_intent(raw, active_app=active_app)
        except Exception as exc:
            logger.debug("LLM intent routing failed (%s); falling back.", exc)
            return None

        intent = str(plan.get("intent") or "").strip().lower()
        target = str(plan.get("target") or "").strip()
        text = str(plan.get("text") or "").strip()

        if not intent or intent == "ignore":
            return None
        if intent == "click_target" and target:
            btn = str(plan.get("button") or "left").lower()
            prefix = "right click" if btn == "right" else "double click" if btn == "double" else "click"
            return self._dispatch_single(f"{prefix} {target}", _from_llm=True)
        if intent == "click_here":
            btn = str(plan.get("button") or "left").lower()
            phrase = "right click" if btn == "right" else "double click" if btn == "double" else "click"
            return self._dispatch_single(phrase, _from_llm=True)
        if intent == "switch_tab" and target:
            return self._dispatch_single(f"go to tab {target}", _from_llm=True)
        if intent == "focus_app" and target:
            return self._dispatch_single(f"focus {target}", _from_llm=True)
        if intent in ("next_window", "prev_window", "close_window", "minimize_window", "maximize_window"):
            phrase_map = {
                "next_window": "next window",
                "prev_window": "previous window",
                "close_window": "close window",
                "minimize_window": "minimize window",
                "maximize_window": "maximize window",
            }
            return self._dispatch_single(phrase_map[intent], _from_llm=True)
        if intent == "scroll":
            direction = str(plan.get("direction") or "down").lower()
            amount = str(plan.get("amount") or "normal").lower()
            mod = " a little" if amount == "small" else " a lot" if amount == "large" else ""
            if direction in ("top", "bottom"):
                return self._dispatch_single(f"scroll to {direction}", _from_llm=True)
            return self._dispatch_single(f"scroll {direction}{mod}", _from_llm=True)
        if intent == "type_into" and target and text:
            return self._dispatch_single(f"type {text} in {target}", _from_llm=True)
        if intent == "type_text" and text:
            return self._dispatch_single(f"type {text}", _from_llm=True)
        if intent == "key_press":
            key = str(plan.get("key") or "").lower()
            if key:
                return self._dispatch_single(key, _from_llm=True)
        if intent == "media":
            action = str(plan.get("action") or "").lower().replace("_", " ")
            if action:
                return self._dispatch_single(action, _from_llm=True)
        if intent == "terminal":
            instr = str(plan.get("instruction") or target or text or raw).strip()
            return self._dispatch_single(f"terminal {instr}", _from_llm=True)
        if intent == "ask":
            q = str(plan.get("question") or text or raw).strip()
            return self._dispatch_single(f"ask {q}", _from_llm=True)
        return None
