"""Hybrid Keyword + Ollama gemma4:e4b Set-of-Marks UI Grounding & Click Executor."""

from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import ollama
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from keyless.config import DisplayLayout
from keyless.ipc.firefox_bridge import DOMCandidate, FirefoxDOMBridge
from keyless.platform.linux.accessibility import LinuxDesktopAccessibilityBridge
from keyless.models.providers.base import Provider
from keyless.platform.linux.screen import monitor_at_point
from keyless.vision.ui_detector import (
    GazeROISnapshot,
    GazeUIDetector,
    UIBoundingBox,
)

logger = logging.getLogger(__name__)


class GroundingSelection(BaseModel):
    """Structured JSON output schema enforced on Ollama gemma4:e4b."""

    target_id: int = Field(
        description="Integer ID [1..N] of the bounding box matching the user's command, or 0 if none match."
    )
    offset_dx: int = Field(
        default=0,
        description="Optional pixel offset (-60..+60) from box center (e.g., if clicking an input field to the right of a label).",
    )
    offset_dy: int = Field(
        default=0,
        description="Optional vertical pixel offset (-40..+40) from box center.",
    )


@dataclass(frozen=True)
class ParsedVoiceIntent:
    """Parsed action verb and target description from a speech transcript."""

    action: Literal["click", "double_click", "right_click", "hover", "type_into"]
    target_query: str
    text_to_type: str = ""


_STOP_WORDS = {
    "on",
    "the",
    "a",
    "an",
    "button",
    "link",
    "icon",
    "box",
    "field",
    "tab",
    "area",
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


def parse_voice_click_command(transcript: str) -> ParsedVoiceIntent | None:
    """Parse click, hover, 'type X in Y', and single-breath 'click Y and type X' commands."""
    raw_clean = transcript.strip().rstrip(".,!?").strip()
    low = raw_clean.lower()

    # Pattern 0: Single-breath "click <target> and type/enter/write <text>"
    m_click_type = re.match(
        r"^(?:click|press|select|tap)\s+(?:(?:on|at|the|a|an)\s+)*(.+?)\s*,?\s+and\s+(?:type|enter|write)\s+(.+)$",
        raw_clean,
        flags=re.IGNORECASE,
    )
    if m_click_type:
        target = re.sub(r"^(?:(?:on|at|the|a|an)\s+)+", "", m_click_type.group(1).strip().lower()).strip()
        text_body = m_click_type.group(2).strip()
        if target and text_body:
            return ParsedVoiceIntent(
                action="type_into",
                target_query=target,
                text_to_type=text_body,
            )

    # Pattern 1: "type <text> in/into <target>"
    m_type = re.match(
        r"^(?:type|enter|write)\s+(.+?)\s+(?:in|into|on)\s+(.+)$",
        raw_clean,
        flags=re.IGNORECASE,
    )
    if m_type:
        target = re.sub(r"^(?:the|a|an)\s+", "", m_type.group(2).strip().lower()).strip()
        text_body = m_type.group(1).strip()
        if target and text_body:
            return ParsedVoiceIntent(
                action="type_into",
                target_query=target,
                text_to_type=text_body,
            )

    # Pattern 2: "double click / right click / click / hover <target>"
    for prefix, act in (
        ("double click", "double_click"),
        ("right click", "right_click"),
        ("middle click", "click"),
        ("click", "click"),
        ("press", "click"),
        ("select", "click"),
        ("tap", "click"),
        ("hover over", "hover"),
        ("hover", "hover"),
        ("move to", "hover"),
    ):
        if low.startswith(prefix):
            rem = low[len(prefix) :].strip()
            rem = re.sub(r"^(?:(?:on|at|the|a|an)\s+)+", "", rem).strip()
            if rem:
                return ParsedVoiceIntent(action=act, target_query=rem)  # type: ignore[arg-type]

    return None


def _clean_target_keywords(query: str) -> str:
    tokens = [t for t in re.split(r"\W+", query.lower()) if t and t not in _STOP_WORDS]
    return " ".join(tokens) if tokens else query.lower().strip()


class GazeVLMClicker:
    """Orchestrates parallel gaze ROI capture, Tier-1 keyword match, and Tier-2 gemma4:e4b SoM."""

    def __init__(
        self,
        detector: GazeUIDetector,
        model_name: str = "gemma4:e4b",
        screen_w: int = 1920,
        screen_h: int = 1080,
        layout: DisplayLayout | None = None,
        crop_w: int = 680,
        crop_h: int = 480,
        keyword_threshold: float = 82.0,
        ollama_client: Any | None = None,
        vlm_provider: Provider | None = None,
        enable_vlm: bool = True,
        firefox_bridge: FirefoxDOMBridge | None = None,
        desktop_atspi: LinuxDesktopAccessibilityBridge | None = None,
    ) -> None:
        self.detector = detector
        self.model_name = model_name
        self.screen_w = int(layout.total_width if layout is not None else screen_w)
        self.screen_h = int(layout.total_height if layout is not None else screen_h)
        self.layout = layout
        self.crop_w = int(crop_w)
        self.crop_h = int(crop_h)
        self.keyword_threshold = float(keyword_threshold)
        self._ollama = ollama_client if ollama_client is not None else ollama
        self._vlm_provider = vlm_provider
        self.enable_vlm = bool(enable_vlm)
        self.firefox_bridge = firefox_bridge
        self.desktop_atspi = desktop_atspi

        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="gaze-vlm")
        self._lock = threading.Lock()
        self._pending_snapshot: Future[GazeROISnapshot] | None = None
        self._pending_dom_future: Future[list[DOMCandidate]] | None = None
        self._pending_onset: tuple[float, float, np.ndarray | None] | None = None
        self._cached_dom_candidates: list[DOMCandidate] = []

    def warm_model_in_vram(self) -> bool:
        """Lock gemma4:e4b in GPU VRAM on startup with keep_alive=-1."""
        try:
            if self._vlm_provider is not None:
                self._vlm_provider.warm()
            else:
                self._ollama.chat(
                    model=self.model_name,
                    messages=[{"role": "user", "content": "ready"}],
                    keep_alive=-1,
                    think=False,
                    options={"num_predict": 1, "draft_num_predict": 0},
                )
            return True
        except Exception as exc:
            logger.warning("Failed to warm VLM '%s': %s", self.model_name, exc)
            return False

    def _monitor_rect(self, gaze_x: float, gaze_y: float) -> tuple[int, int, int, int]:
        if self.layout is None:
            return 0, 0, self.screen_w, self.screen_h
        mon = monitor_at_point(self.layout, gaze_x, gaze_y)
        return mon.x, mon.y, mon.width, mon.height

    def on_speech_start_prewarm(
        self,
        gaze_x: float,
        gaze_y: float,
        stereo_feat: np.ndarray | None = None,
    ) -> None:
        """Fetch Firefox DOM candidates (and pre-warm synthetic/silent ROI capture) at speech onset.

        On real GNOME Wayland, speculative portal screenshots are intentionally skipped here so
        speaking non-click commands or Firefox DOM commands never triggers screen flash effects.
        """
        feat_copy = stereo_feat.copy() if stereo_feat is not None else None
        mon_x, mon_y, mon_w, mon_h = self._monitor_rect(gaze_x, gaze_y)
        with self._lock:
            self._pending_onset = (gaze_x, gaze_y, feat_copy)
            self._cached_dom_candidates = []
            if self.firefox_bridge is not None and self.firefox_bridge.is_connected:
                self._pending_dom_future = self._pool.submit(
                    self.firefox_bridge.get_dom_candidates,
                    gaze_x,
                    gaze_y,
                    mon_x,
                    mon_y,
                    mon_w,
                    mon_h,
                )
            else:
                self._pending_dom_future = None
            if self.detector._capture_fn is not None:
                self._pending_snapshot = self._pool.submit(
                    self.detector.capture_and_annotate,
                    gaze_x,
                    gaze_y,
                    self.screen_w,
                    self.screen_h,
                    self.crop_w,
                    self.crop_h,
                    feat_copy,
                )
            else:
                self._pending_snapshot = None

    def clear_pending_snapshot(self) -> None:
        """Discard any unused pre-warmed snapshot or DOM candidate future."""
        with self._lock:
            self._pending_snapshot = None
            self._pending_dom_future = None
            self._pending_onset = None
            self._cached_dom_candidates = []

    def _pop_pending_state(
        self,
    ) -> tuple[
        Future[GazeROISnapshot] | None,
        Future[list[DOMCandidate]] | None,
        tuple[float, float, np.ndarray | None] | None,
    ]:
        with self._lock:
            snap_fut = self._pending_snapshot
            dom_fut = self._pending_dom_future
            onset = self._pending_onset
            self._pending_snapshot = None
            self._pending_dom_future = None
            self._pending_onset = None
            return snap_fut, dom_fut, onset

    def _tier1_fast_keyword_match(
        self,
        query: str,
        snapshot: GazeROISnapshot,
        search_all_text: bool = False,
    ) -> UIBoundingBox | None:
        """Return matching OCR box in <2ms, prioritizing exact button labels over long headings."""
        clean_q = _clean_target_keywords(query)
        if not clean_q:
            return None
        q_tokens = clean_q.split()
        clean_q_ns = clean_q.replace(" ", "")

        candidates = (
            snapshot.all_text_boxes
            if (search_all_text and snapshot.all_text_boxes)
            else snapshot.boxes
        )
        best_box: UIBoundingBox | None = None
        best_score = 0.0

        for box in candidates:
            if not box.label:
                continue
            raw_lbl = box.label.strip()
            clean_lbl = _clean_target_keywords(raw_lbl)
            if not clean_lbl:
                continue
            lbl_tokens = clean_lbl.split()
            clean_lbl_ns = clean_lbl.replace(" ", "")

            exact_sim = fuzz.ratio(clean_q, clean_lbl)
            short_len = min(len(clean_q), len(clean_lbl))
            long_len = max(1, max(len(clean_q), len(clean_lbl)))
            len_ratio = min(1.0, (short_len + 1) / (long_len + 1))

            if all(qt in lbl_tokens for qt in q_tokens):
                # Whole-word match: exact short button beats multi-word sentence
                extra_words = max(0, len(lbl_tokens) - len(q_tokens))
                sim = 96.0 - min(8.0, extra_words * 1.2)
            elif len(clean_q_ns) >= 5 and clean_q_ns in clean_lbl_ns and not raw_lbl.endswith("?"):
                # Handles concatenated OCR strings like "LiveStreamerCAM313Mono"
                ratio_ns = len(clean_q_ns) / max(1, len(clean_lbl_ns))
                sim = 86.0 if ratio_ns >= 0.35 else 74.0
            else:
                token_sim = fuzz.token_set_ratio(clean_q, clean_lbl) * (0.55 + 0.45 * len_ratio)
                partial_sim = (
                    fuzz.partial_ratio(clean_q, raw_lbl.lower()) * (0.40 + 0.60 * len_ratio)
                    if len(clean_lbl) >= 3 or len(clean_q) <= 3
                    else 0.0
                )
                sim = max(exact_sim, token_sim, partial_sim)

            if exact_sim >= 88.0:
                sim += 12.0
            if raw_lbl.endswith("?"):
                sim -= 10.0

            # Fullscreen/monitor-wide fallback should prefer true keyword matches over gaze distance.
            gaze_penalty = (
                min(4.0, box.gaze_dist_px * 0.005)
                if search_all_text
                else min(14.0, box.gaze_dist_px * 0.035)
            )
            eff_score = sim - gaze_penalty

            if eff_score > best_score:
                best_score = eff_score
                best_box = box

        if best_box is not None and best_score >= self.keyword_threshold:
            return best_box
        return None

    def _tier2_gemma4_som_grounding(
        self,
        intent: ParsedVoiceIntent,
        snapshot: GazeROISnapshot,
        app_id: str = "",
    ) -> tuple[int, int] | None:
        """Query local Ollama gemma4:e4b with the Set-of-Marks crop and box directory."""
        if not self.enable_vlm or not snapshot.boxes or not snapshot.som_png_bytes:
            return None

        box_lines: list[str] = []
        for b in snapshot.boxes:
            desc = f'"{b.label}"' if b.label else f"<{b.kind}>"
            box_lines.append(f"[{b.mark_id}] {desc} (dist={int(b.gaze_dist_px)}px)")
        box_directory = "\n".join(box_lines)

        # Leave <|think|> out of the system prompt so Gemma 4 stays in fast non-thinking mode.
        sys_prompt = (
            "You are a low-latency Linux UI grounding agent. "
            "The user is looking near the cyan circle in this screenshot crop. "
            "Green and cyan numbered boxes [1..N] mark clickable text, buttons, and icons. "
            "Select the exact target_id that matches the user's command. "
            "Return JSON matching the schema."
        )
        user_prompt = (
            f"User command: '{intent.action} {intent.target_query}'\n"
            f"Detected candidate boxes (sorted by closeness to eye gaze):\n{box_directory}\n"
            "Return JSON with target_id (or 0 if not visible)."
        )

        try:
            if self._vlm_provider is not None:
                raw_content = self._vlm_provider.complete(
                    sys_prompt,
                    user_prompt,
                    json=True,
                    images=[snapshot.som_png_bytes],
                ).strip()
            else:
                resp = self._ollama.chat(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {
                            "role": "user",
                            "images": [snapshot.som_png_bytes],
                            "content": user_prompt,
                        },
                    ],
                    format=GroundingSelection.model_json_schema(),
                    keep_alive=-1,
                    think=False,
                    options={
                        "temperature": 0.0,
                        "num_ctx": 4096,
                        "num_predict": 48,
                        "draft_num_predict": 0,
                    },
                )
                raw_content = (resp.message.content or "").strip()
        except Exception as exc:
            logger.warning("VLM grounding call failed (%s): %s", self.model_name, exc)
            return None
        if not raw_content:
            return None
        selection = GroundingSelection.model_validate_json(raw_content)
        if selection.target_id <= 0:
            return None

        for b in snapshot.boxes:
            if b.mark_id == selection.target_id:
                # If gemma4:e4b identified an unlabeled visual icon/image (e.g. "gear icon",
                # "play button", "folder"), auto-cache its visual template so future clicks take <3ms!
                if not b.label and b.width <= 80 and b.height <= 80 and hasattr(self.detector, "icon_bank"):
                    lx1 = max(0, b.global_x1 - snapshot.crop_x)
                    ly1 = max(0, b.global_y1 - snapshot.crop_y)
                    lx2 = min(snapshot.crop_w, b.global_x2 - snapshot.crop_x)
                    ly2 = min(snapshot.crop_h, b.global_y2 - snapshot.crop_y)
                    icon_patch = snapshot.raw_bgr[ly1:ly2, lx1:lx2]
                    self.detector.icon_bank.save_icon_patch(
                        intent.target_query, icon_patch, app_id=app_id
                    )

                cx, cy = b.center_xy
                tx = max(0, min(self.screen_w - 1, cx + int(selection.offset_dx)))
                ty = max(0, min(self.screen_h - 1, cy + int(selection.offset_dy)))
                return tx, ty

        return None

    def _is_firefox_active(self) -> bool:
        """Check whether Firefox is the focused desktop window (fail-open if unknown)."""
        if self.desktop_atspi is None or not self.desktop_atspi.available:
            return True
        try:
            app_name, win_title = self.desktop_atspi.get_active_window_info()
        except Exception:
            return True
        if not app_name and not win_title:
            return True
        return "firefox" in app_name.lower() or "firefox" in win_title.lower()

    def resolve_voice_target(
        self,
        transcript: str,
        current_gaze_x: float,
        current_gaze_y: float,
        current_stereo_feat: np.ndarray | None = None,
    ) -> tuple[ParsedVoiceIntent, tuple[int, int], str] | None:
        """Resolve a voice transcript into `(intent, (click_x, click_y), tier_used)`."""
        intent = parse_voice_click_command(transcript)
        if intent is None:
            self.clear_pending_snapshot()
            return None

        snap_fut, dom_fut, onset = self._pop_pending_state()
        win_ctx = self.detector.capturer.get_active_window_context(
            self.screen_w, self.screen_h, desktop_atspi=self.desktop_atspi
        )
        app_id = win_ctx.app_id
        firefox_active = self._is_firefox_active()

        # Tier 0: Fast Deterministic Firefox DOM Match (only when Firefox is focused)
        dom_cands: list[DOMCandidate] = []
        if firefox_active and self.firefox_bridge is not None and self.firefox_bridge.is_connected:
            if dom_fut is not None:
                try:
                    dom_cands = dom_fut.result(timeout=0.45)
                except Exception:
                    dom_cands = []
            if not dom_cands:
                mon_x, mon_y, mon_w, mon_h = self._monitor_rect(current_gaze_x, current_gaze_y)
                dom_cands = self.firefox_bridge.get_dom_candidates(
                    current_gaze_x, current_gaze_y, mon_x, mon_y, mon_w, mon_h
                )
            self._cached_dom_candidates = dom_cands
            dom_match = self.firefox_bridge.match_voice_target_in_dom(
                intent.target_query,
                dom_cands,
                use_llm_fallback=False,
                llm_model=self.model_name,
            )
            if dom_match is not None:
                return self._finish_dom_match(intent, dom_match, onset, current_gaze_x, current_gaze_y, current_stereo_feat)

        # Tier 0.5: Native Linux Desktop AT-SPI2 Accessibility Tree
        if self.desktop_atspi is not None and self.desktop_atspi.available:
            atspi_action = "type" if intent.action == "type_into" else "click"
            atspi_res = self.desktop_atspi.match_and_execute(
                intent.target_query,
                gaze_x=current_gaze_x,
                gaze_y=current_gaze_y,
                action=atspi_action,
            )
            if atspi_res is not None:
                node, invoked = atspi_res
                if intent.action in ("right_click", "double_click"):
                    invoked = False
                gx_ref = onset[0] if onset is not None else current_gaze_x
                gy_ref = onset[1] if onset is not None else current_gaze_y
                label = node.name or node.description or node.role
                return (
                    intent,
                    (node.cx, node.cy),
                    f"Tier-0.5 Desktop AT-SPI ('{label}', invoked={invoked})",
                )

        # Window-scoped or Single-Pass Monitor-Scoped OCR for non-browser / Electron apps
        # (eliminates redundant 680x480 + monitor two-pass OCR when gaze is a coarse region guide)
        window_rect = win_ctx.rect
        use_window_scope = (
            not firefox_active
            and window_rect is not None
            and window_rect[0] <= current_gaze_x <= window_rect[0] + window_rect[2]
            and window_rect[1] <= current_gaze_y <= window_rect[1] + window_rect[3]
        )
        single_pass_monitor = (
            not firefox_active
            and not use_window_scope
            and snap_fut is None
            and self.detector._capture_fn is None
        )

        if self.layout is not None:
            mon = monitor_at_point(self.layout, current_gaze_x, current_gaze_y)
            t3_x, t3_y, t3_w, t3_h = mon.x, mon.y, mon.width, mon.height
        else:
            t3_x, t3_y, t3_w, t3_h = 0, 0, self.screen_w, self.screen_h

        if snap_fut is not None:
            snapshot = snap_fut.result(timeout=4.0)
        elif use_window_scope and window_rect is not None:
            wx, wy, ww, wh = window_rect
            snapshot = self.detector.capture_and_annotate(
                current_gaze_x,
                current_gaze_y,
                self.screen_w,
                self.screen_h,
                crop_w=ww,
                crop_h=wh,
                stereo_feat=current_stereo_feat,
                crop_origin=(wx, wy),
                max_som_boxes=48,
            )
        elif single_pass_monitor:
            snapshot = self.detector.capture_and_annotate(
                current_gaze_x,
                current_gaze_y,
                self.screen_w,
                self.screen_h,
                crop_w=t3_w,
                crop_h=t3_h,
                stereo_feat=current_stereo_feat,
                crop_origin=(t3_x, t3_y),
                max_som_boxes=48,
            )
        else:
            snapshot = self.detector.capture_and_annotate(
                current_gaze_x,
                current_gaze_y,
                self.screen_w,
                self.screen_h,
                self.crop_w,
                self.crop_h,
                current_stereo_feat,
            )

        # Tier 1: Sub-50ms Visible-Screen Keyword + Gaze Match (boxes first, then all OCR text in crop)
        fast_box = self._tier1_fast_keyword_match(intent.target_query, snapshot)
        if fast_box is None:
            fast_box = self._tier1_fast_keyword_match(intent.target_query, snapshot, search_all_text=True)
        if fast_box is not None:
            target_xy = fast_box.center_xy
            if use_window_scope and app_id:
                return intent, target_xy, f"Tier-1 Window OCR ({app_id}: '{fast_box.label}')"
            if single_pass_monitor and app_id:
                return intent, target_xy, f"Tier-1 Monitor OCR ({app_id}: '{fast_box.label}')"
            return intent, target_xy, f"Tier-1 Keyword ('{fast_box.label}')"

        # Tier 0.5b: Firefox DOM LLM Fallback (only after Tier-1 visible-screen OCR missed)
        if self.firefox_bridge is not None and self.firefox_bridge.is_connected and dom_cands:
            dom_llm_match = self.firefox_bridge._llm_select_dom_candidate(
                intent.target_query, dom_cands[:25], llm_model=self.model_name
            )
            if dom_llm_match is not None:
                return self._finish_dom_match(intent, dom_llm_match, onset, current_gaze_x, current_gaze_y, current_stereo_feat)

        # Tier 3a: Fast Monitor-Wide Keyword OCR (reuses portal full_desktop_bgr in 0ms) before VLM
        if single_pass_monitor:
            full_snap = snapshot
        else:
            full_snap = self.detector.capture_and_annotate(
                gaze_x=current_gaze_x,
                gaze_y=current_gaze_y,
                screen_w=self.screen_w,
                screen_h=self.screen_h,
                crop_w=t3_w,
                crop_h=t3_h,
                stereo_feat=None,
                crop_origin=(t3_x, t3_y),
                cached_full_bgr=snapshot.full_desktop_bgr,
                max_som_boxes=48,
            )
            fast_full = self._tier1_fast_keyword_match(
                intent.target_query, full_snap, search_all_text=True
            )
            if fast_full is not None:
                return intent, fast_full.center_xy, f"Tier-3 Fullscreen Keyword ('{fast_full.label}')"

        # If multi-monitor and still not found on gaze monitor, try full virtual desktop OCR before VLM
        all_snap: GazeROISnapshot | None = None
        if (t3_w, t3_h) != (self.screen_w, self.screen_h):
            all_snap = self.detector.capture_and_annotate(
                gaze_x=current_gaze_x,
                gaze_y=current_gaze_y,
                screen_w=self.screen_w,
                screen_h=self.screen_h,
                crop_w=self.screen_w,
                crop_h=self.screen_h,
                stereo_feat=None,
                crop_origin=(0, 0),
                cached_full_bgr=full_snap.full_desktop_bgr,
                max_som_boxes=48,
            )
            fast_all = self._tier1_fast_keyword_match(
                intent.target_query, all_snap, search_all_text=True
            )
            if fast_all is not None:
                return intent, fast_all.center_xy, f"Tier-3 Desktop Keyword ('{fast_all.label}')"

        # Tier 2: Ollama gemma4:e4b Set-of-Marks Grounding on Gaze Crop (for unlabeled icons)
        vlm_xy = self._tier2_gemma4_som_grounding(intent, snapshot, app_id=app_id)
        if vlm_xy is not None:
            return intent, vlm_xy, f"Tier-2 {self.model_name} Gaze-SoM"

        vlm_full = self._tier2_gemma4_som_grounding(intent, full_snap, app_id=app_id)
        if vlm_full is not None:
            return intent, vlm_full, f"Tier-3 Fullscreen {self.model_name} SoM"

        if all_snap is not None:
            vlm_all = self._tier2_gemma4_som_grounding(intent, all_snap, app_id=app_id)
            if vlm_all is not None:
                return intent, vlm_all, f"Tier-3 Desktop {self.model_name} SoM"

        return None

    def _finish_dom_match(
        self,
        intent: ParsedVoiceIntent,
        dom_match: DOMCandidate,
        onset: tuple[float, float, np.ndarray | None] | None,
        current_gaze_x: float,
        current_gaze_y: float,
        current_stereo_feat: np.ndarray | None,
    ) -> tuple[ParsedVoiceIntent, tuple[int, int], str] | None:
        assert self.firefox_bridge is not None
        if intent.action == "right_click":
            clicked_xy: tuple[int, int] | None = (dom_match.screen_x, dom_match.screen_y)
            tier_label = (
                f"Tier-0 Firefox DOM Locate ('{dom_match.primary_label}' / "
                f"aliases={dom_match.aliases[:3]})"
            )
        else:
            clicked_xy = self.firefox_bridge.execute_dom_action(
                dom_id=dom_match.dom_id,
                action=intent.action,
                text=intent.text_to_type,
            )
            # For editable rich-text editors (CKEditor/Teams), also emit a trusted OS uinput click
            is_editable_target = dom_match.is_editable or dom_match.role in (
                "textbox",
                "searchbox",
                "combobox",
            ) or dom_match.tag in ("textarea", "input")
            prefix = (
                "Tier-0 Firefox DOM Focus"
                if (intent.action == "click" and is_editable_target)
                else "Tier-0 Firefox DOM"
            )
            tier_label = (
                f"{prefix} ('{dom_match.primary_label}' / "
                f"aliases={dom_match.aliases[:3]})"
            )
        if clicked_xy is None:
            return None
        gx_ref = onset[0] if onset is not None else current_gaze_x
        gy_ref = onset[1] if onset is not None else current_gaze_y
        return intent, clicked_xy, tier_label

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
        self.detector.close()
        if self.desktop_atspi is not None:
            self.desktop_atspi.close()
