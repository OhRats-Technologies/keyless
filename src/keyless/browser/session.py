"""Visible persistent Playwright Firefox session with ref-tagged DOM observations and gaze scrolling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from keyless.browser.profile_sync import (
    default_playwright_firefox_profile,
    sync_firefox_profile,
)
from keyless.config.paths import current_paths

logger = logging.getLogger(__name__)

_SNAPSHOT_JS = r"""
(maxItems) => {
  const SELECTOR = [
    "a[href]", "button", "input", "textarea", "select", "summary",
    "[role='button']", "[role='link']", "[role='textbox']", "[role='searchbox']",
    "[role='combobox']", "[role='tab']", "[role='menuitem']", "[role='option']",
    "[role='checkbox']", "[role='switch']", "[contenteditable='true']"
  ].join(",");

  document.querySelectorAll("[data-keyless-ref]").forEach(el => el.removeAttribute("data-keyless-ref"));

  function collect(root, out) {
    root.querySelectorAll(SELECTOR).forEach(n => out.push(n));
    root.querySelectorAll("*").forEach(el => {
      if (el.shadowRoot) collect(el.shadowRoot, out);
    });
  }

  const raw = [];
  collect(document, raw);
  const candidates = [];

  for (const el of raw) {
    if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;

    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    if (r.bottom < -100 || r.top > window.innerHeight + 300) continue;

    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) < 0.05) continue;

    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || (tag === "a" ? "link" : tag)).toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const aria = (
      el.getAttribute("aria-label") ||
      el.getAttribute("placeholder") ||
      el.getAttribute("title") ||
      el.getAttribute("name") ||
      ""
    ).replace(/\s+/g, " ").trim();
    const text = (el.innerText || el.value || "").replace(/\s+/g, " ").trim().slice(0, 80);
    const label = (aria && text && aria.toLowerCase() !== text.toLowerCase())
      ? `${aria} (${text})`
      : (aria || text);

    if (!label && !["input", "textarea"].includes(tag) && role !== "textbox") continue;

    const inView = r.top >= 0 && r.bottom <= window.innerHeight;
    const isInput = ["input", "textarea"].includes(tag) || role === "textbox" || role === "searchbox" || role === "combobox";
    const isVideoTitle = el.id === "video-title" || el.id === "video-title-link";
    const prio = (isInput ? 0 : isVideoTitle ? 1 : inView ? 2 : 3);
    candidates.push({ el, prio, top: r.top, role: type ? `${role}:${type}` : role, label: label.slice(0, 95), in_view: inView });
  }

  candidates.sort((a, b) => (a.prio - b.prio) || (a.top - b.top));
  const items = [];
  let idx = 1;
  for (const c of candidates) {
    if (items.length >= maxItems) break;
    const ref = `e${idx++}`;
    c.el.setAttribute("data-keyless-ref", ref);
    items.push({
      ref,
      role: c.role,
      label: c.label,
      in_view: c.in_view
    });
  }

  const bodyText = (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1800);
  return {
    url: window.location.href,
    title: document.title,
    scroll_y: Math.round(window.scrollY),
    max_scroll: Math.max(0, Math.round(document.documentElement.scrollHeight - window.innerHeight)),
    items,
    text_excerpt: bodyText
  };
}
"""

_GAZE_SCROLL_JS = r"""
({ gazeX, gazeY, winX, winY, direction, amountPx, continuous }) => {
  if (continuous === "faster" || continuous === "slower") {
    const curSpeed = window.__kkAutoScrollSpeed || 135;
    window.__kkAutoScrollSpeed = continuous === "faster"
      ? Math.min(600, Math.round(curSpeed * 1.45))
      : Math.max(40, Math.round(curSpeed * 0.65));
    return { ok: true, mode: continuous, speed: window.__kkAutoScrollSpeed };
  }

  if (window.__kkAutoScrollRAF) {
    cancelAnimationFrame(window.__kkAutoScrollRAF);
    window.__kkAutoScrollRAF = null;
  }
  if (continuous === "stop") return { ok: true, mode: "stopped" };

  const dpr = window.devicePixelRatio || 1;
  const topChrome = Math.max(0, window.outerHeight - window.innerHeight);
  const leftChrome = Math.max(0, Math.round((window.outerWidth - window.innerWidth) / 2));
  const baseWinX = (typeof winX === "number") ? winX : (window.screenX || 0);
  const baseWinY = (typeof winY === "number") ? winY : (window.screenY || 0);

  let vx = gazeX - (baseWinX + leftChrome);
  let vy = gazeY - (baseWinY + topChrome);
  if ((vx < -150 || vx > window.innerWidth + 150) && dpr > 1.05) {
    vx = (gazeX / dpr) - (baseWinX / dpr + leftChrome);
    vy = (gazeY / dpr) - (baseWinY / dpr + topChrome);
  }
  vx = Math.max(8, Math.min(window.innerWidth - 8, vx));
  vy = Math.max(8, Math.min(window.innerHeight - 8, vy));

  const isHoriz = direction === "left" || direction === "right";
  const dirSign = (direction === "up" || direction === "left" || direction === "top") ? -1 : 1;

  function canScroll(el, checkBounds) {
    if (!el || el === document.body || el === document.documentElement) return false;
    const tag = (el.tagName || "").toLowerCase();
    if ((tag === "input" || tag === "textarea" || el.isContentEditable) && el.clientHeight < 220) {
      return false;
    }
    const st = window.getComputedStyle(el);
    const ov = isHoriz ? st.overflowX : st.overflowY;
    if (ov !== "auto" && ov !== "scroll" && ov !== "overlay") return false;
    if (isHoriz) {
      if (el.scrollWidth <= el.clientWidth + 12) return false;
      if (checkBounds && dirSign < 0 && el.scrollLeft <= 1) return false;
      if (checkBounds && dirSign > 0 && el.scrollLeft + el.clientWidth >= el.scrollWidth - 2) return false;
      return true;
    }
    if (el.scrollHeight <= el.clientHeight + 12) return false;
    if (checkBounds && dirSign < 0 && el.scrollTop <= 1) return false;
    if (checkBounds && dirSign > 0 && el.scrollTop + el.clientHeight >= el.scrollHeight - 2) return false;
    return true;
  }

  let target = null;
  const hitStack = document.elementsFromPoint(vx, vy);
  for (const checkBounds of [true, false]) {
    for (const node of hitStack) {
      let cur = node;
      while (cur && cur !== document.body && cur !== document.documentElement) {
        if (canScroll(cur, checkBounds)) { target = cur; break; }
        cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host) || null;
      }
      if (target) break;
    }
    if (target) break;
  }

  if (!target) {
    const root = document.scrollingElement || document.documentElement;
    if (!isHoriz && root && root.scrollHeight > window.innerHeight + 20) {
      target = root;
    } else {
      let bestArea = 0;
      for (const el of document.querySelectorAll("div, main, section, aside, ytd-app, pre")) {
        if (!canScroll(el, false)) continue;
        const r = el.getBoundingClientRect();
        const area = r.width * r.height;
        if (area > bestArea) { bestArea = area; target = el; }
      }
      target = target || root;
    }
  }

  // Subtle 250ms cyan border flash showing which pane your gaze selected
  const rect = (target === document.documentElement || target === document.scrollingElement)
    ? { left: 2, top: 2, width: window.innerWidth - 4, height: window.innerHeight - 4 }
    : target.getBoundingClientRect();
  const flash = document.createElement("div");
  Object.assign(flash.style, {
    position: "fixed",
    left: `${rect.left}px`, top: `${rect.top}px`,
    width: `${rect.width}px`, height: `${rect.height}px`,
    border: "2px solid rgba(0, 220, 255, 0.55)",
    borderRadius: "6px", pointerEvents: "none", zIndex: "2147483647",
    transition: "opacity 260ms ease-out"
  });
  document.documentElement.appendChild(flash);
  setTimeout(() => { flash.style.opacity = "0"; setTimeout(() => flash.remove(), 280); }, 90);

  if (continuous === "start") {
    let last = performance.now();
    window.__kkAutoScrollSpeed = window.__kkAutoScrollSpeed || 135;
    const step = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const speed = window.__kkAutoScrollSpeed || 135;
      if (target === document.documentElement || target === document.scrollingElement) {
        window.scrollBy(0, dirSign * speed * dt);
      } else {
        target.scrollTop += dirSign * speed * dt;
      }
      window.__kkAutoScrollRAF = requestAnimationFrame(step);
    };
    window.__kkAutoScrollRAF = requestAnimationFrame(step);
    return { ok: true, mode: "auto_scroll_started", tag: target.tagName };
  }

  if (direction === "top") {
    target.scrollTo({ top: 0, behavior: "smooth" });
    return { ok: true, tag: target.tagName, direction };
  }
  if (direction === "bottom") {
    target.scrollTo({ top: target.scrollHeight, behavior: "smooth" });
    return { ok: true, tag: target.tagName, direction };
  }

  const delta = Math.abs(amountPx || 520) * dirSign;
  if (isHoriz) target.scrollBy({ left: delta, behavior: "smooth" });
  else target.scrollBy({ top: delta, behavior: "smooth" });
  return { ok: true, tag: target.tagName, vx: Math.round(vx), vy: Math.round(vy), delta };
}
"""


def _resolve_firefox_extension_dir(explicit_dir: Path | None = None) -> Path | None:
    """Locate the Keyless Firefox WebExtension directory (`firefox_ext/manifest.json`)."""
    candidates: list[Path] = []
    if explicit_dir is not None:
        candidates.append(explicit_dir)
    candidates.append(Path(__file__).resolve().parents[3] / "firefox_ext")
    candidates.append(Path.cwd() / "firefox_ext")
    candidates.append(current_paths().data_dir / "firefox_ext")
    for cand in candidates:
        if (cand / "manifest.json").is_file():
            return cand.resolve()
    return None


def _pick_free_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rdp_send(sock: socket.socket, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload).encode("utf-8")
    sock.sendall(f"{len(raw)}:".encode("ascii") + raw)


def _rdp_recv(sock: socket.socket) -> dict[str, Any]:
    header = b""
    while b":" not in header:
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError("Firefox RDP closed before packet header")
        header += chunk
    length = int(header.split(b":", 1)[0])
    body = bytearray()
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise EOFError("Firefox RDP closed mid-packet")
        body.extend(chunk)
    return json.loads(body.decode("utf-8"))


def _install_firefox_temp_addon(
    port: int,
    extension_dir: Path,
    timeout_sec: float = 6.0,
) -> str | None:
    """Install an unpacked Firefox WebExtension via Firefox's local RDP debugger server."""
    deadline = time.monotonic() + timeout_sec
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
                sock.settimeout(3.0)
                _rdp_recv(sock)  # initial root greeting
                _rdp_send(sock, {"to": "root", "type": "getRoot"})
                root = _rdp_recv(sock)
                addons_actor = root.get("addonsActor")
                if not addons_actor:
                    raise RuntimeError(f"Firefox RDP missing addonsActor: {root}")
                _rdp_send(
                    sock,
                    {
                        "to": addons_actor,
                        "type": "installTemporaryAddon",
                        "addonPath": str(extension_dir.resolve()),
                    },
                )
                resp = _rdp_recv(sock)
                addon = resp.get("addon") or {}
                if resp.get("error"):
                    raise RuntimeError(f"installTemporaryAddon error: {resp}")
                return str(addon.get("id") or extension_dir.name)
        except Exception as exc:
            last_err = exc
            time.sleep(0.15)
    logger.warning("Could not auto-install Firefox extension from %s: %s", extension_dir, last_err)
    return None


def _eval_firefox_chrome_js(port: int, js_expr: str, timeout_sec: float = 3.0) -> Any:
    """Evaluate a privileged browser-chrome JS expression via Firefox's local RDP server."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout_sec) as sock:
        sock.settimeout(timeout_sec)
        _rdp_recv(sock)  # initial root greeting
        _rdp_send(sock, {"to": "root", "type": "getProcess", "id": 0})
        proc = _rdp_recv(sock)
        desc = (proc.get("processDescriptor") or {}).get("actor")
        if not desc:
            raise RuntimeError(f"Firefox RDP missing processDescriptor: {proc}")
        _rdp_send(sock, {"to": desc, "type": "getTarget"})
        tgt = _rdp_recv(sock)
        console_actor = (tgt.get("process") or {}).get("consoleActor")
        if not console_actor:
            raise RuntimeError(f"Firefox RDP missing consoleActor: {tgt}")
        _rdp_send(sock, {"to": console_actor, "type": "evaluateJSAsync", "text": js_expr})
        for _ in range(15):
            msg = _rdp_recv(sock)
            if msg.get("type") == "evaluationResult":
                return msg.get("result")
    return None


@dataclass
class PageObservation:
    """Structured snapshot of the currently visible browser tab."""

    url: str
    title: str
    tabs: list[str]
    manifest: str
    text_excerpt: str


class PlaywrightFirefoxSession:
    """Persistent, visible Playwright Firefox instance backed by cloned local Firefox stores."""

    def __init__(
        self,
        profile_dir: Path | None = None,
        auto_seed_on_first_run: bool = True,
        headless: bool = False,
        extension_dir: Path | None = None,
        install_extension: bool = True,
    ) -> None:
        self.profile_dir = (
            profile_dir if profile_dir is not None else default_playwright_firefox_profile()
        )
        self.auto_seed_on_first_run = auto_seed_on_first_run
        self.headless = headless
        self.extension_dir = extension_dir
        self.install_extension = install_extension
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._last_page: Page | None = None
        self._installed_addon_id: str | None = None
        self._rdp_port: int | None = None
        self.active_window_title: str = ""
        self.active_tab_url: str = ""
        self.active_tab_title: str = ""

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def start(self) -> None:
        if self._context is not None:
            return

        # Seed once from the user's normal Firefox profile if this directory has no cookies yet
        if self.auto_seed_on_first_run and not (self.profile_dir / "cookies.sqlite").exists():
            try:
                _, copied = sync_firefox_profile(dst_profile=self.profile_dir)
                logger.info(
                    "Seeded Playwright Firefox profile (%s) with: %s",
                    self.profile_dir,
                    ", ".join(copied),
                )
            except FileNotFoundError as exc:
                logger.info("Starting fresh Playwright Firefox profile (%s)", exc)
                self.profile_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.profile_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env.setdefault("MOZ_ENABLE_WAYLAND", "1")

        ext_dir = _resolve_firefox_extension_dir(self.extension_dir) if self.install_extension else None
        rdp_port: int = _pick_free_localhost_port()
        self._rdp_port = rdp_port
        launch_args = ["--start-debugger-server", str(rdp_port)]
        user_prefs: dict[str, Any] = {
            # Restore open tabs and session cookies across browser restarts
            "browser.startup.page": 3,
            "browser.sessionstore.resume_from_crash": True,
            # Keep audio/video natural for YouTube and media playback
            "media.autoplay.default": 0,
            "media.hardware-video-decoding.enabled": True,
            "gfx.webrender.all": True,
            # Avoid first-run nags and default-browser prompts
            "browser.shell.checkDefaultBrowser": False,
            "browser.aboutwelcome.enabled": False,
            # Enable local RDP debugger for extension auto-install and native Split View commands
            "devtools.debugger.remote-enabled": True,
            "devtools.debugger.prompt-connection": False,
            "devtools.chrome.enabled": True,
            "xpinstall.signatures.required": False,
        }

        self._pw = await async_playwright().start()
        self._context = await self._pw.firefox.launch_persistent_context(
            user_data_dir=str(self.profile_dir.resolve()),
            headless=self.headless,
            no_viewport=True,  # Normal resizable window; never locked to 1280x720
            env=env,
            args=launch_args,
            firefox_user_prefs=user_prefs,
        )
        await self._context.add_init_script(
            """
            (() => {
              window.__kkLastFocusAt = document.hasFocus() ? Date.now() : 0;
              window.addEventListener("focus", () => { window.__kkLastFocusAt = Date.now(); }, true);
              window.addEventListener("pointerdown", () => { window.__kkLastFocusAt = Date.now(); }, true);
            })();
            """
        )
        self._context.on("page", self._on_new_page)
        self._context.on("close", self._on_context_close)
        if ext_dir is not None and rdp_port is not None:
            self._installed_addon_id = await asyncio.to_thread(
                _install_firefox_temp_addon, rdp_port, ext_dir
            )
            if self._installed_addon_id:
                logger.info(
                    "Installed Keyless Firefox extension %s from %s",
                    self._installed_addon_id,
                    ext_dir,
                )
                print(f"[Browser] Auto-installed Firefox extension: {self._installed_addon_id}")
        self._last_page = (
            self._context.pages[-1] if self._context.pages else await self._context.new_page()
        )

    def _on_new_page(self, page: Page) -> None:
        self._last_page = page

    def _on_context_close(self, _ctx: BrowserContext) -> None:
        self._context = None
        self._last_page = None

    async def stop(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._pw = None
        self._last_page = None
        self._rdp_port = None

    async def get_active_page(self, active_window_title: str = "") -> Page:
        await self.start()
        assert self._context is not None
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            try:
                self._last_page = await self._context.new_page()
                return self._last_page
            except Exception as exc:
                # Firefox destroys its browser window when the last tab closes, leaving a zombie
                # BrowserContext ("delayedStartupPromise, window is null"). Cleanly relaunch it.
                logger.info("Playwright Firefox window closed (%s); relaunching persistent context.", exc)
                await self.stop()
                await self.start()
                assert self._context is not None
                pages = [p for p in self._context.pages if not p.is_closed()]
                self._last_page = pages[-1] if pages else await self._context.new_page()
                return self._last_page

        # 0. Ground-truth active tab from Firefox WebExtension (Split-View & multi-window exact)
        target_url = (self.active_tab_url or "").strip()
        target_title = (self.active_tab_title or "").strip().lower()
        if target_url:
            exact = [p for p in pages if (p.url or "").strip() == target_url]
            if exact:
                self._last_page = exact[-1]
                return exact[-1]
            norm_target = target_url.rstrip("/")
            near = [p for p in pages if (p.url or "").strip().rstrip("/") == norm_target]
            if near:
                self._last_page = near[-1]
                return near[-1]

        # Inspect every open page so Split View & multi-window pick ONLY the truly focused tab
        # instead of whichever visible tab happens to appear last in context.pages.
        visible_entries: list[tuple[Page, bool, float, str]] = []
        for p in pages:
            try:
                info = await p.evaluate(
                    """() => ({
                        vis: document.visibilityState === 'visible',
                        focused: document.hasFocus(),
                        lastFocus: Number(window.__kkLastFocusAt || 0),
                        title: String(document.title || '')
                    })"""
                )
                if info.get("vis"):
                    visible_entries.append(
                        (
                            p,
                            bool(info.get("focused")),
                            float(info.get("lastFocus") or 0.0),
                            str(info.get("title") or ""),
                        )
                    )
            except Exception:
                continue

        if visible_entries:
            if target_title:
                ext_title_matches = [
                    e for e in visible_entries if e[3] and e[3].strip().lower() == target_title
                ]
                if ext_title_matches:
                    self._last_page = ext_title_matches[-1][0]
                    return ext_title_matches[-1][0]

            # 1. Exact focused tab (handles Firefox Split View & side-by-side windows)
            focused_entries = [e for e in visible_entries if e[1]]
            if focused_entries:
                best = max(focused_entries, key=lambda e: e[2])
                self._last_page = best[0]
                return best[0]

            # 2. Active OS window title match (e.g. when focus is in the address bar/chrome)
            win_low = (active_window_title or self.active_window_title or "").strip().lower()
            if win_low:
                title_matches = [
                    e for e in visible_entries if e[3] and e[3].strip().lower() in win_low
                ]
                if title_matches:
                    best = max(title_matches, key=lambda e: (len(e[3]), e[2]))
                    self._last_page = best[0]
                    return best[0]

            # 3. Most recently focused/clicked visible pane
            most_recent = max(visible_entries, key=lambda e: e[2])
            if most_recent[2] > 0:
                self._last_page = most_recent[0]
                return most_recent[0]

            # 4. Keep _last_page if it is still among the visible panes
            if self._last_page is not None and any(e[0] is self._last_page for e in visible_entries):
                return self._last_page

            self._last_page = visible_entries[-1][0]
            return self._last_page

        if self._last_page is not None and not self._last_page.is_closed():
            return self._last_page
        self._last_page = pages[-1]
        return self._last_page

    async def observe(self, max_items: int = 90) -> PageObservation:
        p = await self.get_active_page()
        data: dict[str, Any] = await p.evaluate(_SNAPSHOT_JS, max_items)
        pages = [pg for pg in self._context.pages if not pg.is_closed()] if self._context else [p]
        tabs = [f"{i}: {(await pg.title()) or pg.url}" for i, pg in enumerate(pages)]

        lines = [
            f"URL: {data['url']}",
            f"Title: {data['title']}",
            f"Scroll: {data['scroll_y']}/{data['max_scroll']}px",
            "Tabs: " + " | ".join(tabs),
            "Interactive Elements:",
        ]
        for it in data["items"]:
            marker = "" if it["in_view"] else " (offscreen)"
            lines.append(f"  [{it['ref']}] {it['role']} \"{it['label']}\"{marker}")

        return PageObservation(
            url=data["url"],
            title=data["title"],
            tabs=tabs,
            manifest="\n".join(lines),
            text_excerpt=data["text_excerpt"],
        )

    async def navigate(self, url: str) -> str:
        p = await self.get_active_page()
        url = (url or "").strip()
        if not url:
            raise ValueError("navigate requires a non-empty url")
        if not url.startswith(("http://", "https://", "about:", "file:")):
            url = "https://" + url
        await p.goto(url, wait_until="domcontentloaded", timeout=15000)
        await p.wait_for_timeout(400)
        return f"Navigated to {p.url}"

    async def search_youtube(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            raise ValueError("search_youtube requires a non-empty query")
        url = f"https://www.youtube.com/results?search_query={quote_plus(q)}"
        return await self.navigate(url)

    async def search_web(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            raise ValueError("search_web requires a non-empty query")
        url = f"https://www.google.com/search?q={quote_plus(q)}"
        return await self.navigate(url)

    async def click_ref(self, ref: str, fallback_text: str = "") -> str:
        p = await self.get_active_page()
        clean_ref = (ref or "").strip().strip("[]")
        if clean_ref:
            loc = p.locator(f'[data-keyless-ref="{clean_ref}"]').first
            if await loc.count() > 0:
                await loc.scroll_into_view_if_needed(timeout=2500)
                await loc.click(timeout=4000)
                await p.wait_for_timeout(350)
                return f"Clicked [{clean_ref}]"
        if fallback_text:
            loc = p.get_by_text(fallback_text.strip(), exact=False).first
            if await loc.count() > 0:
                await loc.scroll_into_view_if_needed(timeout=2500)
                await loc.click(timeout=4000)
                await p.wait_for_timeout(350)
                return f"Clicked text {fallback_text!r}"
        raise ValueError(
            f"Element ref {ref!r} not found. Pick an exact [eN] id from Interactive Elements (e.g. 'e5')."
        )

    async def type_ref(self, ref: str, text: str, press_enter: bool = False) -> str:
        p = await self.get_active_page()
        clean_ref = (ref or "").strip().strip("[]")
        loc = None
        if clean_ref:
            cand = p.locator(f'[data-keyless-ref="{clean_ref}"]').first
            if await cand.count() > 0:
                loc = cand
        if loc is None:
            # Smart fallback: find a visible search or text input on the current page
            for sel in (
                'input[name="search_query"]',
                'input[type="search"]',
                '[role="searchbox"]',
                'input[type="text"]',
                'textarea',
                '[contenteditable="true"]',
            ):
                cand = p.locator(sel).first
                try:
                    if await cand.count() > 0 and await cand.is_visible():
                        loc = cand
                        clean_ref = sel
                        break
                except Exception:
                    continue
        if loc is None:
            raise ValueError(
                "No input element found for ref "
                f"{ref!r}. Use tool='search_youtube', tool='search_web', or tool='navigate' first."
            )
        await loc.scroll_into_view_if_needed(timeout=2500)
        await loc.click(timeout=3000)
        try:
            await loc.fill(text, timeout=2500)
        except Exception:
            await p.keyboard.press("Control+a")
            await p.keyboard.type(text, delay=8)
        if press_enter:
            await p.keyboard.press("Enter")
            await p.wait_for_timeout(500)
        return f"Typed into [{clean_ref}]" + (" and pressed Enter" if press_enter else "")

    async def press_key(self, key: str) -> str:
        p = await self.get_active_page()
        await p.keyboard.press(key)
        await p.wait_for_timeout(150)
        return f"Pressed {key}"

    async def scroll_at_gaze(
        self,
        gaze_x: float,
        gaze_y: float,
        direction: str = "down",
        amount_px: int = 520,
        continuous: str = "",
        win_origin: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        p = await self.get_active_page()
        return await p.evaluate(
            _GAZE_SCROLL_JS,
            {
                "gazeX": float(gaze_x),
                "gazeY": float(gaze_y),
                "winX": win_origin[0] if win_origin is not None else None,
                "winY": win_origin[1] if win_origin is not None else None,
                "direction": direction,
                "amountPx": int(amount_px),
                "continuous": continuous,
            },
        )

    async def scroll(self, direction: str = "down", amount: int = 600) -> str:
        p = await self.get_active_page()
        delta = -abs(amount) if direction == "up" else abs(amount)
        await p.mouse.wheel(0, delta)
        await p.wait_for_timeout(250)
        return f"Scrolled {direction} by {abs(delta)}px"

    async def go_back(self) -> str:
        p = await self.get_active_page()
        await p.go_back(wait_until="domcontentloaded", timeout=8000)
        return f"Went back to {p.url}"

    async def new_tab(self, url: str = "") -> str:
        await self.start()
        assert self._context is not None
        try:
            page = await self._context.new_page()
        except Exception as exc:
            logger.info("new_tab hit closed Firefox window (%s); relaunching.", exc)
            await self.stop()
            await self.start()
            assert self._context is not None
            pages = [p for p in self._context.pages if not p.is_closed()]
            page = pages[-1] if pages else await self._context.new_page()
        self._last_page = page
        if url:
            if not url.startswith(("http://", "https://", "about:", "file:")):
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return f"Opened new tab {page.url}"

    async def close_tab(self) -> str:
        p = await self.get_active_page()
        remaining = [
            pg for pg in (self._context.pages if self._context else []) if not pg.is_closed()
        ]
        await p.close()
        if len(remaining) <= 1:
            # Closing the last tab destroys Firefox's XUL window; reset context so next command relaunches cleanly.
            await self.stop()
        return "Closed current tab"

    async def switch_tab(self, index: int) -> str:
        await self.start()
        assert self._context is not None
        pages = [pg for pg in self._context.pages if not pg.is_closed()]
        if 0 <= index < len(pages):
            self._last_page = pages[index]
            await self._last_page.bring_to_front()
            return f"Switched to tab {index}: {self._last_page.url}"
        return f"Invalid tab index {index}"

    async def split_view(self, action: str = "add") -> bool:
        """Control native Firefox Split View ('add', 'separate', 'reverse', 'left', 'right', 'switch')."""
        await self.start()
        if self._rdp_port is None:
            return False
        act = (action or "add").strip().lower()
        js = f"""(() => {{
          const win = Services.wm.getMostRecentBrowserWindow();
          if (!win || !win.gBrowser) return false;
          const gb = win.gBrowser;
          const act = {json.dumps(act)};
          if (act === "add") {{
            const cmd = win.document.getElementById("Browser:AddTabSplitView");
            if (!cmd) return false;
            cmd.doCommand();
            return Boolean(gb.activeSplitView);
          }}
          if (act === "separate") {{
            const cmd = win.document.getElementById("Browser:SeparateTabSplitView")
              || win.document.getElementById("splitViewCmd_separateTabs");
            if (!cmd) return false;
            cmd.doCommand();
            return true;
          }}
          if (act === "reverse") {{
            const cmd = win.document.getElementById("splitViewCmd_reverseTabs");
            if (!cmd) return false;
            cmd.doCommand();
            return true;
          }}
          const sv = gb.activeSplitView;
          if (!sv || !sv.tabs || sv.tabs.length < 2) return false;
          let targetTab = null;
          if (act === "left") targetTab = sv.tabs[0];
          else if (act === "right") targetTab = sv.tabs[1];
          else if (act === "switch") {{
            const idx = sv.tabs.indexOf(gb.selectedTab);
            targetTab = sv.tabs[idx === 0 ? 1 : 0];
          }}
          if (!targetTab) return false;
          gb.selectedTab = targetTab;
          if (gb.selectedBrowser) gb.selectedBrowser.focus();
          return true;
        }})()"""
        try:
            res = await asyncio.to_thread(_eval_firefox_chrome_js, self._rdp_port, js)
            return bool(res)
        except Exception as exc:
            logger.warning("split_view(%s) failed: %s", act, exc)
            return False

    async def screenshot_jpeg(self) -> bytes:
        p = await self.get_active_page()
        return await p.screenshot(type="jpeg", quality=72)
