"""Local WebSocket bridge to the Firefox DOM Extension (ws://127.0.0.1:8765)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

import ollama
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from websockets.asyncio.server import ServerConnection, serve

logger = logging.getLogger(__name__)

_TEXTBOX_INTENT_WORDS = {
    "box",
    "textbox",
    "input",
    "field",
    "prompt",
    "question",
    "ask",
    "chat",
    "write",
    "message",
    "search",
    "bar",
}
_BUTTON_INTENT_WORDS = {
    "button",
    "submit",
    "send",
    "icon",
    "run",
    "play",
    "stop",
    "copy",
    "close",
    "menu",
    "gear",
    "mic",
    "microphone",
}
_DOM_STOP_WORDS = {
    "on",
    "the",
    "a",
    "an",
    "to",
    "in",
    "into",
    "at",
    "for",
    "of",
    "my",
    "this",
    "that",
    "says",
    "say",
    "reading",
    "reads",
    "labeled",
    "labelled",
    "called",
    "named",
    "with",
    "text",
    "where",
    "it",
    "place",
}

_NUMBER_WORD_MAP = {
    "one": "1",
    "first": "1",
    "1st": "1",
    "two": "2",
    "second": "2",
    "2nd": "2",
    "three": "3",
    "third": "3",
    "3rd": "3",
    "four": "4",
    "fourth": "4",
    "4th": "4",
    "five": "5",
    "fifth": "5",
    "5th": "5",
    "six": "6",
    "sixth": "6",
    "6th": "6",
    "seven": "7",
    "seventh": "7",
    "7th": "7",
    "eight": "8",
    "eighth": "8",
    "8th": "8",
    "nine": "9",
    "ninth": "9",
    "9th": "9",
    "ten": "10",
    "tenth": "10",
    "10th": "10",
    "eleven": "11",
    "eleventh": "11",
    "11th": "11",
    "twelve": "12",
    "twelfth": "12",
    "12th": "12",
}


class _DOMSelectionSchema(BaseModel):
    """Structured output for Tier-0.5 gemma4:e4b text-only DOM element selection."""

    dom_id: int = Field(
        description="The integer dom_id of the matching HTML element, or 0 if none match."
    )


@dataclass(frozen=True)
class TabInfo:
    """Open Firefox browser tab metadata."""

    tab_id: int
    window_id: int
    index: int
    active: bool
    title: str
    url: str


@dataclass(frozen=True)
class DOMCandidate:
    """Interactive DOM element returned by the Firefox extension."""

    dom_id: int
    tag: str
    role: str
    primary_label: str
    visible_text: str
    aria_label: str
    icon_hints: str
    aliases: list[str]
    gaze_dist_px: float
    screen_x: int
    screen_y: int
    is_editable: bool = False


class FirefoxDOMBridge:
    """Thread-safe WebSocket server communicating with firefox_ext/background.js."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        ollama_client: Any | None = None,
        on_passive_click: Any | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.bound_port = self.port
        self._ollama = ollama_client if ollama_client is not None else ollama
        self.on_passive_click = on_passive_click
        self._loop = asyncio.new_event_loop()
        self._clients: list[ServerConnection] = []
        self._pending_reqs: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._last_gaze_broadcast = 0.0
        self._started = threading.Event()
        self._stop_future: asyncio.Future[None] | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="firefox-dom-bridge",
            daemon=True,
        )

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._started.clear()
        self._thread.start()
        self._started.wait(timeout=2.0)

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        if self._loop.is_running() and self._stop_future is not None:
            self._loop.call_soon_threadsafe(
                lambda: None if self._stop_future.done() else self._stop_future.set_result(None)
            )
        self._thread.join(timeout=1.5)

    @property
    def is_connected(self) -> bool:
        return len(self._clients) > 0

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            logger.warning("FirefoxDOMBridge server stopped: %s", exc)
            self._started.set()
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        self._stop_future = self._loop.create_future()
        async with serve(self._handler, self.host, self.port) as server:
            if server.sockets:
                self.bound_port = int(server.sockets[0].getsockname()[1])
            self._started.set()
            await self._stop_future

    async def _handler(self, websocket: ServerConnection) -> None:
        self._clients.append(websocket)
        logger.info("Firefox extension connected (%d active)", len(self._clients))
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "passive_mouse_click" and self.on_passive_click is not None:
                    try:
                        self.on_passive_click(int(msg["screen_x"]), int(msg["screen_y"]))
                    except Exception:
                        pass
                    continue
                req_id = msg.get("req_id")
                if req_id and req_id in self._pending_reqs:
                    fut = self._pending_reqs.pop(req_id)
                    if not fut.done():
                        fut.set_result(msg)
        except Exception as exc:
            logger.debug("Firefox extension socket closed: %s", exc)
        finally:
            if websocket in self._clients:
                self._clients.remove(websocket)

    async def _send_cmd(
        self, payload: dict[str, Any], timeout: float = 0.45
    ) -> dict[str, Any] | None:
        if not self._clients:
            return None
        ws = self._clients[-1]
        if "req_id" not in payload:
            try:
                await ws.send(json.dumps(payload))
                return {"ok": True}
            except Exception as exc:
                logger.debug("WebSocket send failed: %s", exc)
                if ws in self._clients:
                    self._clients.remove(ws)
                return None

        req_id = payload["req_id"]
        fut: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending_reqs[req_id] = fut
        try:
            await ws.send(json.dumps(payload))
            resp = await asyncio.wait_for(fut, timeout=timeout)
            if not resp.get("ok"):
                logger.debug("Firefox extension command %s error: %s", payload.get("type"), resp.get("error"))
            return resp
        except Exception as exc:
            self._pending_reqs.pop(req_id, None)
            logger.debug("WebSocket command %s failed/timed out: %s", payload.get("type"), exc)
            return None

    def broadcast_gaze(
        self,
        gaze_x: float,
        gaze_y: float,
        mon_x: int = 0,
        mon_y: int = 0,
        mon_w: int = 0,
        mon_h: int = 0,
    ) -> None:
        """Stream gaze coordinates at ~20Hz so Firefox updates the magnetic highlight HUD."""
        now = time.monotonic()
        if not self.is_connected or (now - self._last_gaze_broadcast) < 0.048:
            return
        if not self._loop.is_running():
            return
        self._last_gaze_broadcast = now
        asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "type": "gaze_update",
                    "gaze_x": round(gaze_x, 1),
                    "gaze_y": round(gaze_y, 1),
                    "mon_x": int(mon_x),
                    "mon_y": int(mon_y),
                    "mon_w": int(mon_w),
                    "mon_h": int(mon_h),
                }
            ),
            self._loop,
        )

    def send_pointer_anchor(self, global_x: int, global_y: int) -> None:
        """Send a known global pointer position so Firefox locks its exact Wayland window origin."""
        if not self.is_connected or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "type": "pointer_anchor",
                    "global_x": int(global_x),
                    "global_y": int(global_y),
                }
            ),
            self._loop,
        )

    def get_dom_candidates(
        self,
        gaze_x: float,
        gaze_y: float,
        mon_x: int = 0,
        mon_y: int = 0,
        mon_w: int = 0,
        mon_h: int = 0,
    ) -> list[DOMCandidate]:
        """Fetch top 50 interactive DOM elements sorted by proximity to user's gaze."""
        if not self.is_connected or not self._loop.is_running():
            return []
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "req_id": str(uuid.uuid4()),
                    "type": "get_dom_candidates",
                    "gaze_x": round(gaze_x, 1),
                    "gaze_y": round(gaze_y, 1),
                    "mon_x": int(mon_x),
                    "mon_y": int(mon_y),
                    "mon_w": int(mon_w),
                    "mon_h": int(mon_h),
                },
                timeout=0.35,
            ),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.4)
        except Exception:
            return []

        if not resp or not resp.get("ok") or not resp.get("data"):
            return []

        raw_list = resp["data"].get("candidates", [])
        out: list[DOMCandidate] = []
        for c in raw_list:
            out.append(
                DOMCandidate(
                    dom_id=int(c["dom_id"]),
                    tag=str(c["tag"]),
                    role=str(c["role"]),
                    primary_label=str(c["primary_label"]),
                    visible_text=str(c["visible_text"]),
                    aria_label=str(c["aria_label"]),
                    icon_hints=str(c["icon_hints"]),
                    aliases=[str(a) for a in c.get("aliases", [])],
                    gaze_dist_px=float(c["gaze_dist_px"]),
                    screen_x=int(c["screen_x"]),
                    screen_y=int(c["screen_y"]),
                    is_editable=bool(c.get("is_editable", False)),
                )
            )
        return out

    def execute_dom_action(
        self,
        dom_id: int,
        action: str = "click",
        text: str = "",
        press_enter: bool = False,
    ) -> tuple[int, int] | None:
        """Trigger a direct DOM click or text entry inside Firefox and return its screen (x, y)."""
        if not self.is_connected or not self._loop.is_running():
            return None
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "req_id": str(uuid.uuid4()),
                    "type": "execute_dom_action",
                    "dom_id": int(dom_id),
                    "action": action,
                    "text": text,
                    "press_enter": press_enter,
                },
                timeout=0.45,
            ),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.5)
        except Exception:
            return None

        if not resp or not resp.get("ok") or not resp.get("data", {}).get("ok"):
            return None
        data = resp["data"]
        return int(data["screen_x"]), int(data["screen_y"])

    def execute_media_action(self, media_action: str) -> bool:
        """Control HTML5 `<video>` / `<audio>` playback in the active Firefox tab."""
        if not self.is_connected or not self._loop.is_running():
            return False
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "req_id": str(uuid.uuid4()),
                    "type": "execute_media_action",
                    "media_action": media_action,
                },
                timeout=0.35,
            ),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.4)
        except Exception:
            return False
        return bool(resp and resp.get("ok") and resp.get("data", {}).get("handled"))

    def scroll_page(
        self,
        gaze_x: float,
        gaze_y: float,
        direction: str = "down",
        amount: str = "normal",
        continuous: str = "",
    ) -> bool:
        """Scroll the DOM container under `(gaze_x, gaze_y)` inside Firefox."""
        if not self.is_connected or not self._loop.is_running():
            return False
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "req_id": str(uuid.uuid4()),
                    "type": "scroll_dom",
                    "gaze_x": round(gaze_x, 1),
                    "gaze_y": round(gaze_y, 1),
                    "direction": direction,
                    "amount": amount,
                    "continuous": continuous,
                },
                timeout=0.35,
            ),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.4)
            if not resp or not resp.get("ok"):
                return False
            data = resp.get("data")
            if isinstance(data, dict) and "ok" in data:
                return bool(data.get("ok"))
            return True
        except Exception:
            return False

    def get_active_tab(self) -> TabInfo | None:
        """Return the single active tab in Firefox's focused window (Split-View accurate)."""
        if not self.is_connected or not self._loop.is_running():
            return None
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd({"req_id": str(uuid.uuid4()), "type": "get_active_tab"}, timeout=0.25),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.3)
        except Exception:
            resp = None
        if resp and resp.get("ok") and resp.get("data"):
            t = resp["data"]
            return TabInfo(
                tab_id=int(t["id"]),
                window_id=int(t.get("windowId", 0)),
                index=int(t.get("index", 0)),
                active=True,
                title=str(t.get("title", "")),
                url=str(t.get("url", "")),
            )
        active = [t for t in self.list_tabs() if t.active]
        return active[-1] if active else None

    def list_tabs(self) -> list[TabInfo]:
        """Fetch all open tabs across Firefox windows."""
        if not self.is_connected or not self._loop.is_running():
            return []
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd({"req_id": str(uuid.uuid4()), "type": "list_tabs"}, timeout=0.35),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.4)
        except Exception:
            return []
        if not resp or not resp.get("ok") or not resp.get("data"):
            return []
        out: list[TabInfo] = []
        for t in resp["data"].get("tabs", []):
            out.append(
                TabInfo(
                    tab_id=int(t["id"]),
                    window_id=int(t.get("windowId", 0)),
                    index=int(t.get("index", 0)),
                    active=bool(t.get("active", False)),
                    title=str(t.get("title", "")),
                    url=str(t.get("url", "")),
                )
            )
        return out

    def switch_tab(self, target: str) -> str | None:
        """Switch Firefox tab by direction ('next'/'prev'), ordinal ('1'..'9'), or title/URL query."""
        if not self.is_connected or not self._loop.is_running():
            return None

        clean = target.strip().lower()
        if clean in ("next", "prev", "previous"):
            direction = "prev" if clean in ("prev", "previous") else "next"
            fut = asyncio.run_coroutine_threadsafe(
                self._send_cmd(
                    {
                        "req_id": str(uuid.uuid4()),
                        "type": "switch_tab",
                        "direction": direction,
                    },
                    timeout=0.35,
                ),
                self._loop,
            )
            try:
                resp = fut.result(timeout=0.4)
            except Exception:
                return None
            if resp and resp.get("ok") and resp.get("data"):
                return str(resp["data"].get("title") or direction)
            return None

        tabs = self.list_tabs()
        if not tabs:
            return None

        ordinals = {
            "first": 1,
            "1": 1,
            "one": 1,
            "second": 2,
            "2": 2,
            "two": 2,
            "third": 3,
            "3": 3,
            "three": 3,
            "fourth": 4,
            "4": 4,
            "four": 4,
            "fifth": 5,
            "5": 5,
            "five": 5,
            "sixth": 6,
            "6": 6,
            "six": 6,
            "seventh": 7,
            "7": 7,
            "seven": 7,
            "eighth": 8,
            "8": 8,
            "eight": 8,
            "ninth": 9,
            "9": 9,
            "nine": 9,
        }

        chosen: TabInfo | None = None
        if clean == "last":
            chosen = tabs[-1]
        elif clean in ordinals:
            idx = ordinals[clean] - 1
            active_win = next((t.window_id for t in tabs if t.active), tabs[0].window_id)
            win_tabs = sorted([t for t in tabs if t.window_id == active_win], key=lambda t: t.index)
            if 0 <= idx < len(win_tabs):
                chosen = win_tabs[idx]
        else:
            best_score = 0.0
            for t in tabs:
                hay = f"{t.title} {t.url}".lower()
                sim = max(
                    fuzz.token_set_ratio(clean, hay),
                    fuzz.partial_ratio(clean, t.title.lower()) if t.title else 0.0,
                    fuzz.partial_ratio(clean, t.url.lower()) if t.url else 0.0,
                )
                # Slight preference for inactive tabs when switching
                if not t.active:
                    sim += 4.0
                if sim > best_score:
                    best_score = sim
                    chosen = t
            if best_score < 58.0:
                chosen = None

        if chosen is None:
            return None

        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd(
                {
                    "req_id": str(uuid.uuid4()),
                    "type": "switch_tab",
                    "tab_id": chosen.tab_id,
                },
                timeout=0.35,
            ),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.4)
        except Exception:
            return None
        if resp and resp.get("ok"):
            return chosen.title or chosen.url or f"tab {chosen.index + 1}"
        return None

    @staticmethod
    def _resolve_tab_url(query_or_url: str | None) -> str | None:
        if not query_or_url:
            return None
        q = query_or_url.strip()
        if not q:
            return None
        if re.match(r"^https?://", q, flags=re.IGNORECASE):
            return q
        if " " not in q and "." in q:
            return f"https://{q}"
        return f"https://www.google.com/search?q={urllib.parse.quote_plus(q)}"

    def new_tab(self, query_or_url: str | None = None) -> bool:
        """Open a new Firefox tab (blank, URL, or search query) and focus its window."""
        if not self.is_connected or not self._loop.is_running():
            return False
        payload: dict[str, Any] = {"req_id": str(uuid.uuid4()), "type": "new_tab"}
        resolved = self._resolve_tab_url(query_or_url)
        if resolved is not None:
            payload["url"] = resolved
        fut = asyncio.run_coroutine_threadsafe(
            self._send_cmd(payload, timeout=0.35),
            self._loop,
        )
        try:
            resp = fut.result(timeout=0.4)
        except Exception:
            return False
        return bool(resp and resp.get("ok"))

    def match_voice_target_in_dom(
        self,
        target_query: str,
        candidates: list[DOMCandidate],
        use_llm_fallback: bool = True,
        llm_model: str = "gemma4:e4b",
    ) -> DOMCandidate | None:
        """Match natural phrases like 'gemini ask question box' against DOM elements."""
        if not candidates:
            return None

        raw_tokens = [
            _NUMBER_WORD_MAP.get(t, t)
            for t in re.split(r"\W+", target_query.lower())
            if t and t not in _DOM_STOP_WORDS
        ]
        if not raw_tokens:
            return None

        wants_textbox = any(t in _TEXTBOX_INTENT_WORDS for t in raw_tokens)
        wants_button = any(t in _BUTTON_INTENT_WORDS for t in raw_tokens) and not (
            "box" in raw_tokens or "field" in raw_tokens or "input" in raw_tokens
        )
        clean_q = " ".join(raw_tokens)

        best_cand: DOMCandidate | None = None
        best_score = -100.0

        for c in candidates:
            is_textbox_el = (
                c.role in ("textbox", "searchbox", "combobox")
                or c.tag in ("textarea", "input")
                or "textbox" in c.aliases
                or "prompt" in c.aliases
            )
            is_button_el = c.role in ("button", "link", "tab", "menuitem") or c.tag in (
                "button",
                "a",
            )

            combined_blob = " ".join(
                [
                    c.primary_label.lower(),
                    c.aria_label.lower(),
                    c.visible_text.lower(),
                    c.icon_hints.lower(),
                    c.role.lower(),
                    c.tag.lower(),
                    *c.aliases,
                ]
            )
            el_tokens = set(re.split(r"\W+", combined_blob)) - {""}

            matched_tokens = 0.0
            for qt in raw_tokens:
                if qt in el_tokens:
                    matched_tokens += 1.0
                else:
                    best_tok_sim = max(
                        (fuzz.ratio(qt, et) for et in el_tokens if len(et) >= 3),
                        default=0.0,
                    )
                    if best_tok_sim >= 80.0:
                        matched_tokens += best_tok_sim / 100.0

            token_recall_score = 100.0 * (matched_tokens / len(raw_tokens))

            individual_fields = [
                c.primary_label.lower(),
                c.aria_label.lower(),
                c.visible_text.lower(),
                *c.aliases,
            ]
            best_field_sim = max(
                (fuzz.token_set_ratio(clean_q, f) for f in individual_fields if f),
                default=0.0,
            )

            base_sim = max(token_recall_score, best_field_sim)

            role_bonus = 0.0
            if wants_textbox:
                role_bonus = 24.0 if is_textbox_el else -18.0
            elif wants_button:
                role_bonus = 15.0 if is_button_el else -10.0

            gaze_penalty = min(15.0, c.gaze_dist_px * 0.02)
            total_score = base_sim + role_bonus - gaze_penalty
            if total_score > best_score:
                best_score = total_score
                best_cand = c

        if best_cand is not None and best_score >= 74.0:
            logger.debug(
                "DOM match %r -> [%d] %r (role=%s, score=%.1f)",
                target_query,
                best_cand.dom_id,
                best_cand.primary_label,
                best_cand.role,
                best_score,
            )
            return best_cand

        if use_llm_fallback and candidates:
            return self._llm_select_dom_candidate(target_query, candidates[:25], llm_model)

        return None

    def _llm_select_dom_candidate(
        self,
        target_query: str,
        candidates: list[DOMCandidate],
        llm_model: str = "gemma4:e4b",
    ) -> DOMCandidate | None:
        """Ask gemma4:e4b (text-only) to pick the best DOM element for unusual descriptions."""
        lines = []
        for c in candidates:
            alias_str = ",".join(c.aliases[:6])
            lines.append(
                f"id={c.dom_id} | role={c.role} | label='{c.primary_label}' | "
                f"text='{c.visible_text[:40]}' | aliases=[{alias_str}] | gaze_dist={int(c.gaze_dist_px)}px"
            )
        dom_table = "\n".join(lines)

        try:
            resp = self._ollama.chat(
                model=llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You map a user's spoken UI target description to the matching browser DOM element. "
                            "Return JSON with dom_id (or 0 if none match)."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"User wants to interact with: '{target_query}'\n"
                            f"Interactive DOM elements on active webpage:\n{dom_table}\n"
                            "Select the matching dom_id."
                        ),
                    },
                ],
                format=_DOMSelectionSchema.model_json_schema(),
                keep_alive=-1,
                think=False,
                options={"temperature": 0.0, "num_predict": 32},
            )
            raw_content = (resp.message.content or "").strip()
            if not raw_content:
                return None
            sel = _DOMSelectionSchema.model_validate_json(raw_content)
            for c in candidates:
                if c.dom_id == sel.dom_id:
                    return c
        except Exception as exc:
            logger.debug("DOM LLM fallback error: %s", exc)

        return None
