"""OhRats-styled Keyless Lab window for Gaze v2 curation and Dream-RSI review.

Follows .agents/skills/ohrats-ui/SKILL.md:
- All colors, spacing, and typography come from ``keyless.ui.theme.tokens``.
- Exactly one ``Primary`` accent-filled action per surface (Record / Stop).
- Tabular monospace (``Telemetry``) for metrics, coordinates, and hashes.
- Domain curation lives in ``keyless.gaze.curate``; this window is a thin view.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyless.config.paths import AppPaths, current_paths
from keyless.gaze.curate import (
    SessionSummary,
    delete_event,
    delete_session,
    evaluate_recall_summary,
    event_click_covered,
    export_curated_split,
    grant_linux_input_access,
    list_session_summaries,
    probe_lab_environment,
    seed_demo_session,
    update_event,
)
from keyless.gaze.metrics import bbox_contains, metric_table
from keyless.gaze.replay import Session, load_session
from keyless.ui.components import Card, divider, eyebrow
from keyless.ui.theme.tokens import (
    ThemeName,
    load_bundled_fonts,
    stylesheet,
    system_theme_name,
    tokens_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _telemetry(text: str, *, role: str = "Telemetry") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(role)
    return lbl


class _CommandWorker(QThread):
    finished_output = Signal(int, str)

    def __init__(
        self,
        cmd: list[str],
        cwd: Path,
        *,
        extra_env: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cmd = cmd
        self._cwd = cwd
        self._extra_env = extra_env or {}

    def run(self) -> None:
        try:
            env = dict(os.environ)
            home = Path.home()
            env["PATH"] = f"{home / '.local' / 'bin'}:{home / '.bun' / 'bin'}:{env.get('PATH', '')}"
            env.update(self._extra_env)
            proc = subprocess.run(
                self._cmd,
                cwd=self._cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            self.finished_output.emit(proc.returncode, out.strip())
        except Exception as exc:
            self.finished_output.emit(1, str(exc))


class _CallableWorker(QThread):
    finished_result = Signal(bool, str)

    def __init__(self, fn, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            ok, msg = self._fn()
            self.finished_result.emit(bool(ok), str(msg))
        except Exception as exc:
            self.finished_result.emit(False, str(exc))


class EventCanvas(QWidget):
    """Draws an event's screenshot (or schematic frame) with candidate/click overlays.

    Clicking inside a candidate bbox emits ``candidate_clicked(candidate_id)`` so
    the user can reassign ``interaction.target`` directly on the visual canvas.
    """

    candidate_clicked = Signal(str)

    def __init__(self, tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._pixmap: QPixmap | None = None
        self._event: dict[str, Any] | None = None
        self._selected_candidate_id: str | None = None
        self._canvas_rect = QRectF()
        self._coord_size = (1920.0, 1080.0)
        self.setMinimumSize(360, 260)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_tokens(self, tokens) -> None:
        self._tokens = tokens
        self.update()

    def set_event(self, session_dir: Path | None, event: dict[str, Any] | None) -> None:
        self._event = event
        self._pixmap = None
        self._selected_candidate_id = None

        if event is not None:
            interaction = event.get("interaction") or {}
            self._selected_candidate_id = interaction.get("target")
            screen = event.get("screen") or {}
            digest = screen.get("screenshot")
            if session_dir is not None and digest:
                blob = session_dir / "blobs" / f"{digest}.png"
                if blob.exists():
                    pix = QPixmap(str(blob))
                    if not pix.isNull():
                        self._pixmap = pix

            self._coord_size = self._infer_coord_size(event, self._pixmap)
        self.update()

    @staticmethod
    def _infer_coord_size(
        event: dict[str, Any], pixmap: QPixmap | None
    ) -> tuple[float, float]:
        if pixmap is not None and not pixmap.isNull():
            return float(pixmap.width()), float(pixmap.height())
        max_x, max_y = 1280.0, 720.0
        screen = event.get("screen") or {}
        for cand in screen.get("candidates") or []:
            bbox = cand.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                max_x = max(max_x, float(bbox[0]) + float(bbox[2]) + 40.0)
                max_y = max(max_y, float(bbox[1]) + float(bbox[3]) + 40.0)
        for pt in (
            (event.get("interaction") or {}).get("mouse"),
            (event.get("prediction") or {}).get("pixel"),
        ):
            if isinstance(pt, (list, tuple)) and len(pt) == 2:
                max_x = max(max_x, float(pt[0]) + 60.0)
                max_y = max(max_y, float(pt[1]) + 60.0)
        return max_x, max_y

    def _compute_draw_rect(self) -> QRectF:
        pad = 12.0
        avail_w = max(10.0, self.width() - 2.0 * pad)
        avail_h = max(10.0, self.height() - 2.0 * pad)
        cw, ch = self._coord_size
        scale = min(avail_w / max(1.0, cw), avail_h / max(1.0, ch))
        draw_w = cw * scale
        draw_h = ch * scale
        ox = pad + (avail_w - draw_w) / 2.0
        oy = pad + (avail_h - draw_h) / 2.0
        return QRectF(ox, oy, draw_w, draw_h)

    def _to_widget(self, x: float, y: float, rect: QRectF) -> QPointF:
        cw, ch = self._coord_size
        sx = rect.width() / max(1.0, cw)
        sy = rect.height() / max(1.0, ch)
        return QPointF(rect.left() + x * sx, rect.top() + y * sy)

    def _to_screen_coords(self, pos: QPointF) -> tuple[float, float] | None:
        rect = self._canvas_rect
        if rect.isEmpty() or not rect.contains(pos):
            return None
        cw, ch = self._coord_size
        x = (pos.x() - rect.left()) * (cw / max(1.0, rect.width()))
        y = (pos.y() - rect.top()) * (ch / max(1.0, rect.height()))
        return x, y

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._event is None:
            super().mousePressEvent(event)
            return
        coords = self._to_screen_coords(event.position())
        if coords is None:
            return
        sx, sy = coords
        candidates = (self._event.get("screen") or {}).get("candidates") or []
        hits = [
            c
            for c in candidates
            if isinstance(c, dict) and bbox_contains(c.get("bbox", (0, 0, 0, 0)), sx, sy)
        ]
        if hits:
            hits.sort(
                key=lambda c: max(1.0, float(c["bbox"][2]) * float(c["bbox"][3]))
            )
            cid = str(hits[0].get("id") or "")
            if cid:
                self._selected_candidate_id = cid
                self.update()
                self.candidate_clicked.emit(cid)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self._tokens

        painter.fillRect(self.rect(), QColor(t.surface_alt))
        rect = self._compute_draw_rect()
        self._canvas_rect = rect

        if self._event is None:
            painter.setPen(QColor(t.muted))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Select a recorded event to inspect candidates and clicks.",
            )
            return

        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(rect.toRect(), self._pixmap)
        else:
            painter.fillRect(rect, QColor(t.surface))
            grid_pen = QPen(QColor(t.border))
            grid_pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(grid_pen)
            for frac in (0.25, 0.5, 0.75):
                gx = rect.left() + rect.width() * frac
                gy = rect.top() + rect.height() * frac
                painter.drawLine(QPointF(gx, rect.top()), QPointF(gx, rect.bottom()))
                painter.drawLine(QPointF(rect.left(), gy), QPointF(rect.right(), gy))

        painter.setPen(QPen(QColor(t.border), 1.0))
        painter.drawRect(rect)

        cw, ch = self._coord_size
        sx = rect.width() / max(1.0, cw)
        sy = rect.height() / max(1.0, ch)

        candidates = (self._event.get("screen") or {}).get("candidates") or []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            bbox = cand.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            bx, by, bw, bh = [float(v) for v in bbox]
            box_rect = QRectF(rect.left() + bx * sx, rect.top() + by * sy, bw * sx, bh * sy)
            is_target = str(cand.get("id") or "") == str(self._selected_candidate_id or "")
            color = QColor(t.success if is_target else t.muted)
            fill = QColor(color)
            fill.setAlpha(46 if is_target else 18)
            painter.fillRect(box_rect, fill)
            painter.setPen(QPen(color, 2.0 if is_target else 1.0))
            painter.drawRect(box_rect)

            raw_label = str(cand.get("text") or cand.get("id") or "")
            if raw_label and box_rect.width() >= 28 and box_rect.height() >= 14:
                font = painter.font()
                font.setPixelSize(11)
                painter.setFont(font)
                inner = box_rect.adjusted(4, 2, -4, -2)
                elided = painter.fontMetrics().elidedText(
                    raw_label, Qt.TextElideMode.ElideRight, int(max(1.0, inner.width()))
                )
                painter.setPen(QColor(t.text if is_target else t.muted))
                painter.drawText(
                    inner,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    elided,
                )

        pred_px = (self._event.get("prediction") or {}).get("pixel")
        if isinstance(pred_px, (list, tuple)) and len(pred_px) == 2:
            gp = self._to_widget(float(pred_px[0]), float(pred_px[1]), rect)
            painter.setPen(QPen(QColor(t.warning), 2.0))
            painter.drawEllipse(gp, 7.0, 7.0)

        mouse = (self._event.get("interaction") or {}).get("mouse")
        if isinstance(mouse, (list, tuple)) and len(mouse) == 2:
            mp = self._to_widget(float(mouse[0]), float(mouse[1]), rect)
            covered = event_click_covered(self._event)
            click_color = QColor(t.success if covered else t.danger)
            painter.setPen(QPen(click_color, 2.0))
            painter.drawLine(QPointF(mp.x() - 9, mp.y()), QPointF(mp.x() + 9, mp.y()))
            painter.drawLine(QPointF(mp.x(), mp.y() - 9), QPointF(mp.x(), mp.y() + 9))
            painter.drawEllipse(mp, 4.0, 4.0)


class LabWindow(QMainWindow):
    """Unified Gaze v2 Guide, Data Curator, Phase 1 Recall Dashboard, and Dream-RSI Console."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        sessions_dir: Path | None = None,
        repo_root: Path | None = None,
        theme: ThemeName | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths = paths or current_paths()
        self._sessions_dir = sessions_dir or (self._paths.data_dir / "sessions")
        self._repo_root = repo_root or _REPO_ROOT
        self._theme = theme or system_theme_name()
        self._tokens = tokens_for(self._theme)

        self._summaries: list[SessionSummary] = []
        self._current_session: Session | None = None
        self._current_session_dir: Path | None = None
        self._filtered_indices: list[int] = []
        self._current_event_index: int | None = None
        self._recorder: Any = None
        self._click_observer: Any = None
        self._worker: _CommandWorker | None = None
        self._perm_worker: _CallableWorker | None = None

        self.setWindowTitle("Keyless Lab")
        self.resize(1180, 720)
        self.setMinimumSize(900, 580)

        self._build_ui()
        self._install_shortcuts()
        self._apply_theme(self._theme)
        self.refresh_all()

    def _apply_theme(self, theme: ThemeName) -> None:
        self._theme = theme
        self._tokens = tokens_for(theme)
        t = self._tokens
        extra = (
            f"\nQPushButton#NavTab {{ background: transparent; color: {t.muted}; "
            f"border: 1px solid transparent; border-radius: {t.radius_sm}px; padding: 6px 12px; }}"
            f"\nQPushButton#NavTab:hover {{ color: {t.text}; background: {t.surface_alt}; }}"
            f"\nQPushButton#NavTab:checked {{ color: {t.text}; background: {t.selected}; "
            f"border: 1px solid {t.border}; font-weight: 600; }}\n"
        )
        self.setStyleSheet(stylesheet(t) + extra)
        self.canvas.set_tokens(t)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(14)

        # --- Top bar: title + nav + Phase 1 telemetry + single Primary CTA ---
        top = QHBoxLayout()
        top.setSpacing(12)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Keyless Lab")
        title.setObjectName("Title")
        self.subtitle = QLabel("Gaze v2 workbench")
        self.subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        top.addLayout(title_box)

        top.addSpacing(12)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        nav_labels = ("Guide", "Curator", "Recall & Replay", "Dream-RSI")
        self._nav_buttons: list[QPushButton] = []
        for idx, label in enumerate(nav_labels):
            btn = QPushButton(label)
            btn.setObjectName("NavTab")
            btn.setCheckable(True)
            if idx == 0:
                btn.setChecked(True)
            self._nav_group.addButton(btn, idx)
            self._nav_buttons.append(btn)
            top.addWidget(btn)
        self._nav_group.idClicked.connect(self._on_nav_changed)

        top.addStretch(1)

        self.recall_metric = _telemetry("RECALL  --")
        self.gate_badge = _telemetry("GATE  NO DATA")
        top.addWidget(self.recall_metric)
        top.addWidget(self.gate_badge)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("Ghost")
        self.refresh_btn.clicked.connect(self.refresh_all)
        top.addWidget(self.refresh_btn)

        # Single Primary action on the surface
        self.record_btn = QPushButton("Record session")
        self.record_btn.setObjectName("Primary")
        self.record_btn.clicked.connect(self._toggle_recording)
        top.addWidget(self.record_btn)

        root.addLayout(top)
        root.addWidget(divider())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_guide_page())
        self.pages.addWidget(self._build_curator_page())
        self.pages.addWidget(self._build_recall_page())
        self.pages.addWidget(self._build_dream_page())
        root.addWidget(self.pages, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.shortcuts_hint = _telemetry("A accept · R reject · Del remove · Click box to assign target")
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.shortcuts_hint)
        root.addLayout(footer)

    # --- Page 0: Newcomer Guide & 1-Click Readiness ---------------------

    def _build_guide_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # Readiness card
        ready_card = Card()
        ready_card.layout.addWidget(eyebrow("ENVIRONMENT & READINESS"))

        # Row 1: Input capture permission
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        self.guide_input_title = QLabel("Global click capture")
        self.guide_input_title.setObjectName("Heading")
        self.guide_input_detail = QLabel("")
        self.guide_input_detail.setObjectName("Muted")
        self.guide_grant_input_btn = QPushButton("Grant input access")
        self.guide_grant_input_btn.setObjectName("Ghost")
        self.guide_grant_input_btn.clicked.connect(self._on_grant_input_access)
        r1.addWidget(self.guide_input_title)
        r1.addWidget(self.guide_input_detail, 1)
        r1.addWidget(self.guide_grant_input_btn)
        ready_card.layout.addLayout(r1)

        ready_card.layout.addWidget(divider())

        # Row 2: Local Strata server
        r2 = QHBoxLayout()
        r2.setSpacing(10)
        self.guide_strata_title = QLabel("Local model server (Strata)")
        self.guide_strata_title.setObjectName("Heading")
        self.guide_strata_detail = QLabel("")
        self.guide_strata_detail.setObjectName("Muted")
        self.guide_strata_btn = QPushButton("Start Strata")
        self.guide_strata_btn.setObjectName("Ghost")
        self.guide_strata_btn.clicked.connect(self._toggle_strata_server)
        r2.addWidget(self.guide_strata_title)
        r2.addWidget(self.guide_strata_detail, 1)
        r2.addWidget(self.guide_strata_btn)
        ready_card.layout.addLayout(r2)

        ready_card.layout.addWidget(divider())

        # Row 3: Phase 1 Gate progress
        r3 = QHBoxLayout()
        r3.setSpacing(10)
        self.guide_gate_title = QLabel("Phase 1 candidate recall gate (>95%)")
        self.guide_gate_title.setObjectName("Heading")
        self.guide_gate_detail = QLabel("")
        self.guide_gate_detail.setObjectName("Muted")
        self.guide_sample_btn = QPushButton("Create sample session")
        self.guide_sample_btn.setObjectName("Ghost")
        self.guide_sample_btn.clicked.connect(self._on_seed_sample_session)
        r3.addWidget(self.guide_gate_title)
        r3.addWidget(self.guide_gate_detail, 1)
        r3.addWidget(self.guide_sample_btn)
        ready_card.layout.addLayout(r3)

        layout.addWidget(ready_card)

        # 3-step pipeline cards
        steps_row = QHBoxLayout()
        steps_row.setSpacing(14)

        s1 = Card(subtle=True)
        s1.layout.addWidget(eyebrow("STEP 1 · COLLECT"))
        t1 = QLabel("Record natural clicks")
        t1.setObjectName("Heading")
        d1 = QLabel(
            "Gaze v2 has no calibration grid. Your physical clicks are the labels. "
            "Press Record session above, use your desktop normally, then press Stop recording."
        )
        d1.setWordWrap(True)
        d1.setObjectName("Muted")
        b1 = QPushButton("Open Curator")
        b1.setObjectName("Ghost")
        b1.clicked.connect(lambda: self._select_tab(1))
        s1.layout.addWidget(t1)
        s1.layout.addWidget(d1, 1)
        s1.layout.addWidget(b1, 0, Qt.AlignmentFlag.AlignLeft)

        s2 = Card(subtle=True)
        s2.layout.addWidget(eyebrow("STEP 2 · VERIFY GATE"))
        t2 = QLabel("Curate & reach >95% recall")
        t2.setObjectName("Heading")
        d2 = QLabel(
            "Inspect clicks against detected UI targets. Click any box on the canvas to reassign "
            "a target, reject noisy events, and verify recall exceeds 95% before model training."
        )
        d2.setWordWrap(True)
        d2.setObjectName("Muted")
        b2 = QPushButton("View recall & metrics")
        b2.setObjectName("Ghost")
        b2.clicked.connect(lambda: self._select_tab(2))
        s2.layout.addWidget(t2)
        s2.layout.addWidget(d2, 1)
        s2.layout.addWidget(b2, 0, Qt.AlignmentFlag.AlignLeft)

        s3 = Card(subtle=True)
        s3.layout.addWidget(eyebrow("STEP 3 · ITERATE"))
        t3 = QLabel("Export splits & Dream-RSI")
        t3.setObjectName("Heading")
        d3 = QLabel(
            "Export session-held-out train/val/test splits with zero frame leakage, or run local "
            "Pi + Strata tasks and monotonically promote verified skill updates."
        )
        d3.setWordWrap(True)
        d3.setObjectName("Muted")
        b3 = QPushButton("Open Dream-RSI")
        b3.setObjectName("Ghost")
        b3.clicked.connect(lambda: self._select_tab(3))
        s3.layout.addWidget(t3)
        s3.layout.addWidget(d3, 1)
        s3.layout.addWidget(b3, 0, Qt.AlignmentFlag.AlignLeft)

        steps_row.addWidget(s1, 1)
        steps_row.addWidget(s2, 1)
        steps_row.addWidget(s3, 1)
        layout.addLayout(steps_row, 1)
        return page

    # --- Page 1: Visual Curator -----------------------------------------

    def _build_curator_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # Inline permission banner (hidden once /dev/input/event* is readable)
        self.curator_perm_bar = Card(subtle=True)
        self.curator_perm_bar.layout.setContentsMargins(14, 10, 14, 10)
        p_row = QHBoxLayout()
        p_row.setSpacing(10)
        self.curator_perm_label = QLabel(
            "Global click recording needs read access to Linux input devices."
        )
        self.curator_perm_label.setObjectName("Muted")
        self.curator_grant_btn = QPushButton("Grant input access")
        self.curator_grant_btn.setObjectName("Ghost")
        self.curator_grant_btn.clicked.connect(self._on_grant_input_access)
        self.curator_sample_btn = QPushButton("Create sample session")
        self.curator_sample_btn.setObjectName("Ghost")
        self.curator_sample_btn.clicked.connect(self._on_seed_sample_session)
        p_row.addWidget(self.curator_perm_label, 1)
        p_row.addWidget(self.curator_sample_btn)
        p_row.addWidget(self.curator_grant_btn)
        self.curator_perm_bar.layout.addLayout(p_row)
        self.curator_perm_bar.setVisible(False)
        outer.addWidget(self.curator_perm_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Left pane: sessions
        left_card = Card()
        left_card.layout.setContentsMargins(14, 14, 14, 14)
        left_card.layout.setSpacing(10)
        left_card.layout.addWidget(eyebrow("SESSIONS"))
        self.session_list = QListWidget()
        self.session_list.currentRowChanged.connect(self._on_session_selected)
        left_card.layout.addWidget(self.session_list, 1)

        sess_btns = QHBoxLayout()
        sess_btns.setSpacing(8)
        self.export_btn = QPushButton("Export split")
        self.export_btn.setObjectName("Ghost")
        self.export_btn.clicked.connect(self._on_export_split)
        self.delete_session_btn = QPushButton("Delete")
        self.delete_session_btn.setObjectName("Ghost")
        self.delete_session_btn.clicked.connect(self._on_delete_session)
        sess_btns.addWidget(self.export_btn)
        sess_btns.addWidget(self.delete_session_btn)
        left_card.layout.addLayout(sess_btns)

        # Center pane: visual canvas
        center_card = Card()
        center_card.layout.setContentsMargins(14, 14, 14, 14)
        center_card.layout.setSpacing(10)
        canvas_hdr = QHBoxLayout()
        canvas_hdr.addWidget(eyebrow("VISUAL INSPECTOR"))
        canvas_hdr.addStretch(1)
        self.canvas_meta = _telemetry("No event selected")
        canvas_hdr.addWidget(self.canvas_meta)
        center_card.layout.addLayout(canvas_hdr)

        self.canvas = EventCanvas(self._tokens)
        self.canvas.candidate_clicked.connect(self._on_canvas_target_assigned)
        center_card.layout.addWidget(self.canvas, 1)

        # Right pane: filtered events + curation controls
        right_card = Card()
        right_card.layout.setContentsMargins(14, 14, 14, 14)
        right_card.layout.setSpacing(10)

        ev_hdr = QHBoxLayout()
        ev_hdr.addWidget(eyebrow("EVENTS"))
        ev_hdr.addStretch(1)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All events", "Clicks only", "Missed clicks", "Rejected"])
        self.filter_combo.currentIndexChanged.connect(self._rebuild_event_list)
        ev_hdr.addWidget(self.filter_combo)
        right_card.layout.addLayout(ev_hdr)

        self.event_list = QListWidget()
        self.event_list.currentRowChanged.connect(self._on_event_selected)
        right_card.layout.addWidget(self.event_list, 1)

        right_card.layout.addWidget(divider())
        right_card.layout.addWidget(eyebrow("CURATION"))
        self.inspector_detail = QLabel("Select an event to review.")
        self.inspector_detail.setWordWrap(True)
        self.inspector_detail.setObjectName("Muted")
        right_card.layout.addWidget(self.inspector_detail)

        cur_row1 = QHBoxLayout()
        cur_row1.setSpacing(8)
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setObjectName("Ghost")
        self.accept_btn.clicked.connect(lambda: self._set_current_accepted(True))
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setObjectName("Ghost")
        self.reject_btn.clicked.connect(lambda: self._set_current_accepted(False))
        cur_row1.addWidget(self.accept_btn)
        cur_row1.addWidget(self.reject_btn)
        right_card.layout.addLayout(cur_row1)

        cur_row2 = QHBoxLayout()
        cur_row2.setSpacing(8)
        self.clear_target_btn = QPushButton("Clear target")
        self.clear_target_btn.setObjectName("Ghost")
        self.clear_target_btn.clicked.connect(self._clear_current_target)
        self.delete_event_btn = QPushButton("Delete event")
        self.delete_event_btn.setObjectName("Ghost")
        self.delete_event_btn.clicked.connect(self._delete_current_event)
        cur_row2.addWidget(self.clear_target_btn)
        cur_row2.addWidget(self.delete_event_btn)
        right_card.layout.addLayout(cur_row2)

        splitter.addWidget(left_card)
        splitter.addWidget(center_card)
        splitter.addWidget(right_card)
        splitter.setSizes([270, 560, 310])
        outer.addWidget(splitter, 1)
        return page

    # --- Page 2: Recall & Replay ----------------------------------------

    def _build_recall_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        left = Card()
        left.layout.addWidget(eyebrow("PHASE 1 CANDIDATE RECALL (>95% GATE)"))
        self.recall_summary_label = QLabel("No labeled clicks recorded yet.")
        self.recall_summary_label.setObjectName("Muted")
        left.layout.addWidget(self.recall_summary_label)

        self.app_recall_table = QTableWidget(0, 4)
        self.app_recall_table.setHorizontalHeaderLabels(["App", "Clicks", "Hits", "Recall"])
        self.app_recall_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.app_recall_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        left.layout.addWidget(self.app_recall_table, 1)

        right = Card()
        right.layout.addWidget(eyebrow("SELECTED SESSION BASELINE 0 METRICS"))
        self.replay_session_label = QLabel("Select a session in Curator to inspect replay metrics.")
        self.replay_session_label.setObjectName("Muted")
        right.layout.addWidget(self.replay_session_label)

        self.metrics_table = QTableWidget(0, 2)
        self.metrics_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.metrics_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.metrics_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        right.layout.addWidget(self.metrics_table, 1)

        layout.addWidget(left, 1)
        layout.addWidget(right, 1)
        return page

    # --- Page 3: Dream-RSI Harness Console ------------------------------

    def _build_dream_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top_card = Card()
        hdr = QHBoxLayout()
        hdr.addWidget(eyebrow("DREAM-RSI LOCAL + FRONTIER HARNESS"))
        hdr.addStretch(1)
        self.dream_strata_btn = QPushButton("Start Strata")
        self.dream_strata_btn.setObjectName("Ghost")
        self.dream_strata_btn.clicked.connect(self._toggle_strata_server)
        self.dream_eval_btn = QPushButton("Run invariant eval")
        self.dream_eval_btn.setObjectName("Ghost")
        self.dream_eval_btn.clicked.connect(self._run_dream_eval)
        self.dream_digest_btn = QPushButton("Compile digest")
        self.dream_digest_btn.setObjectName("Ghost")
        self.dream_digest_btn.clicked.connect(self._run_dream_digest)
        self.dream_copy_btn = QPushButton("Copy frontier prompt")
        self.dream_copy_btn.setObjectName("Ghost")
        self.dream_copy_btn.clicked.connect(self._copy_frontier_prompt)
        hdr.addWidget(self.dream_strata_btn)
        hdr.addWidget(self.dream_eval_btn)
        hdr.addWidget(self.dream_digest_btn)
        hdr.addWidget(self.dream_copy_btn)
        top_card.layout.addLayout(hdr)

        self.dream_summary_label = QLabel("No policy ledger or traces loaded.")
        self.dream_summary_label.setWordWrap(True)
        self.dream_summary_label.setObjectName("Muted")
        top_card.layout.addWidget(self.dream_summary_label)

        # Local Pi + Strata task runner row
        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self.dream_task_input = QLineEdit()
        self.dream_task_input.setPlaceholderText(
            "Describe a scoped coding task for the local Pi + Strata agent…"
        )
        self.dream_run_btn = QPushButton("Run local task")
        self.dream_run_btn.setObjectName("Ghost")
        self.dream_run_btn.clicked.connect(self._run_dream_task)
        run_row.addWidget(self.dream_task_input, 1)
        run_row.addWidget(self.dream_run_btn)
        top_card.layout.addLayout(run_row)

        # Monotonic policy promotion row
        prom_row = QHBoxLayout()
        prom_row.setSpacing(8)
        self.dream_note_input = QLineEdit()
        self.dream_note_input.setPlaceholderText(
            "Policy revision note (promotes AGENTS.md + skills only if all 6 invariants pass)…"
        )
        self.dream_promote_btn = QPushButton("Promote policy")
        self.dream_promote_btn.setObjectName("Ghost")
        self.dream_promote_btn.clicked.connect(self._run_dream_promote)
        prom_row.addWidget(self.dream_note_input, 1)
        prom_row.addWidget(self.dream_promote_btn)
        top_card.layout.addLayout(prom_row)

        self.dream_output = QPlainTextEdit()
        self.dream_output.setReadOnly(True)
        self.dream_output.setObjectName("Mono")
        top_card.layout.addWidget(self.dream_output, 1)

        layout.addWidget(top_card, 1)
        return page

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("A"), self, activated=lambda: self._set_current_accepted(True))
        QShortcut(QKeySequence("R"), self, activated=lambda: self._set_current_accepted(False))
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self._delete_current_event)

    def _select_tab(self, index: int) -> None:
        if 0 <= index < len(self._nav_buttons):
            self._nav_buttons[index].setChecked(True)
        self._on_nav_changed(index)

    def _on_nav_changed(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == 0:
            self._refresh_environment_view()
        elif index == 3:
            self._refresh_dream_view()

    # --- Data & Environment refresh -------------------------------------

    def refresh_all(self) -> None:
        selected_id = self._current_session.session_id if self._current_session else None
        self._summaries = list_session_summaries(self._sessions_dir)
        self.session_list.blockSignals(True)
        self.session_list.clear()
        restore_row = 0
        for idx, summary in enumerate(self._summaries):
            recall_str = "--" if summary.recall is None else f"{summary.recall:.0%}"
            apps_str = ", ".join(summary.apps[:2]) if summary.apps else "no apps"
            item = QListWidgetItem(
                f"{summary.session_id[:10]}  ·  {summary.click_count} clicks ({recall_str})\n{apps_str}"
            )
            self.session_list.addItem(item)
            if selected_id and summary.session_id == selected_id:
                restore_row = idx
        self.session_list.blockSignals(False)

        self._refresh_recall_tables()
        self._refresh_environment_view()
        self._refresh_dream_view()

        if self._summaries:
            self.session_list.setCurrentRow(restore_row)
            self._on_session_selected(restore_row)
        else:
            self._current_session = None
            self._current_session_dir = None
            self.event_list.clear()
            self.canvas.set_event(None, None)
            self.canvas_meta.setText("No sessions recorded")
            self.inspector_detail.setText(
                "No sessions recorded yet. Click Record session above or Create sample session to explore."
            )
            self._set_curation_enabled(False)

    def _refresh_environment_view(self) -> None:
        env = probe_lab_environment()
        if env.input_group_active:
            self.guide_input_detail.setText("Active · physical mouse clicks can be recorded.")
            self.guide_grant_input_btn.setVisible(False)
            self.curator_perm_bar.setVisible(False)
        else:
            if env.input_group_configured:
                msg = "Group configured · click Grant input access to apply ACL now (or sign out and back in)."
            else:
                msg = "Permission needed · click Grant input access to authorize global click capture."
            self.guide_input_detail.setText(msg)
            self.guide_grant_input_btn.setVisible(env.pkexec_available)
            self.curator_perm_label.setText(msg)
            self.curator_grant_btn.setVisible(env.pkexec_available)
            self.curator_perm_bar.setVisible(True)

        if env.strata_serving:
            self.guide_strata_detail.setText("Ready on 127.0.0.1:8080 (Qwen3.8-Flash-Next).")
            self.guide_strata_btn.setText("Stop Strata")
            self.dream_strata_btn.setText("Stop Strata")
            self.guide_strata_btn.setVisible(env.strata_cli_available)
            self.dream_strata_btn.setVisible(env.strata_cli_available)
        elif env.strata_cli_available:
            self.guide_strata_detail.setText("Stopped · click Start Strata to launch the local model.")
            self.guide_strata_btn.setText("Start Strata")
            self.dream_strata_btn.setText("Start Strata")
            self.guide_strata_btn.setVisible(True)
            self.dream_strata_btn.setVisible(True)
        else:
            self.guide_strata_detail.setText("Not installed on this machine.")
            self.guide_strata_btn.setVisible(False)
            self.dream_strata_btn.setVisible(False)

        self.guide_sample_btn.setVisible(len(self._summaries) == 0)

    def _refresh_recall_tables(self) -> None:
        summary = evaluate_recall_summary(self._sessions_dir)
        if summary.overall_recall is None:
            self.recall_metric.setText(f"SESSIONS {summary.sessions}  ·  CLICKS 0")
            self.gate_badge.setText("GATE  NO DATA")
            self.gate_badge.setObjectName("Telemetry")
            self.recall_summary_label.setText(
                "No labeled clicks with candidates yet. Record a session to measure Phase 1 recall."
            )
            self.guide_gate_detail.setText(
                f"{summary.sessions} session(s) · 0 labeled clicks recorded yet."
            )
        else:
            self.recall_metric.setText(
                f"RECALL {summary.overall_recall:.1%} ({summary.hits}/{summary.clicks})"
            )
            if summary.gate_met:
                self.gate_badge.setText("GATE  MET (>95%)")
                self.gate_badge.setObjectName("Success")
                gate_txt = "Gate met (>95%) · ready for Phase 2 target scorer."
            else:
                self.gate_badge.setText("GATE  BELOW 95%")
                self.gate_badge.setObjectName("Warning")
                gate_txt = "Below 95% · inspect missed clicks in Curator before training."
            self.recall_summary_label.setText(
                f"{summary.sessions} session(s), {summary.clicks} labeled click(s), "
                f"overall geometric recall {summary.overall_recall:.1%}."
            )
            self.guide_gate_detail.setText(
                f"{summary.overall_recall:.1%} ({summary.hits}/{summary.clicks} clicks) · {gate_txt}"
            )
        self.gate_badge.style().unpolish(self.gate_badge)
        self.gate_badge.style().polish(self.gate_badge)

        self.app_recall_table.setRowCount(len(summary.per_app))
        for row_idx, row in enumerate(summary.per_app):
            self.app_recall_table.setItem(row_idx, 0, QTableWidgetItem(row.app))
            self.app_recall_table.setItem(row_idx, 1, QTableWidgetItem(str(row.clicks)))
            self.app_recall_table.setItem(row_idx, 2, QTableWidgetItem(str(row.hits)))
            self.app_recall_table.setItem(row_idx, 3, QTableWidgetItem(f"{row.recall:.1%}"))

    def _on_session_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._summaries):
            self._current_session = None
            self._current_session_dir = None
            return
        summary = self._summaries[row]
        try:
            self._current_session = load_session(summary.session_dir)
            self._current_session_dir = summary.session_dir
        except Exception as exc:
            self.status_label.setText(f"Failed to load session: {exc}")
            self._current_session = None
            self._current_session_dir = None
            return

        self._rebuild_event_list()
        self._refresh_session_metrics()

    def _refresh_session_metrics(self) -> None:
        if self._current_session is None:
            self.metrics_table.setRowCount(0)
            return
        self.replay_session_label.setText(
            f"Session {self._current_session.session_id} ({len(self._current_session.events)} events)"
        )
        metrics = metric_table(self._current_session.events)
        rows = [
            ("candidate_recall@3", metrics.get("candidate_recall@3")),
            ("top1", metrics.get("top1")),
            ("top3", metrics.get("top3")),
            ("hit@25px", metrics.get("hit@25px")),
            ("hit@50px", metrics.get("hit@50px")),
            ("hit@100px", metrics.get("hit@100px")),
            ("misclicks/h", metrics.get("misclicks_per_hour")),
            ("abstention", metrics.get("abstention_rate")),
            ("actions/correction", metrics.get("actions_per_correction")),
            ("baseline_top1", metrics.get("baseline_top1")),
        ]
        self.metrics_table.setRowCount(len(rows))
        for i, (label, val) in enumerate(rows):
            self.metrics_table.setItem(i, 0, QTableWidgetItem(label))
            self.metrics_table.setItem(
                i, 1, QTableWidgetItem("n/a" if val is None else f"{val:.3f}")
            )

    def _rebuild_event_list(self) -> None:
        self.event_list.blockSignals(True)
        self.event_list.clear()
        self._filtered_indices = []
        mode = self.filter_combo.currentText()

        if self._current_session is not None:
            for idx, ev in enumerate(self._current_session.events):
                interaction = ev.get("interaction") or {}
                kind = str(interaction.get("kind") or "frame")
                covered = event_click_covered(ev)
                accepted = interaction.get("accepted")

                if mode == "Clicks only" and kind != "click":
                    continue
                if mode == "Missed clicks" and (kind != "click" or covered is not False):
                    continue
                if mode == "Rejected" and accepted is not False:
                    continue

                self._filtered_indices.append(idx)
                app_name = str((ev.get("screen") or {}).get("app") or "-")
                tag = "OK" if accepted is True else ("REJ" if accepted is False else "NEW")
                cov = ""
                if kind == "click":
                    cov = " · HIT" if covered else " · MISS"
                self.event_list.addItem(f"#{idx:03d}  {kind:<5}  {tag}{cov}  ({app_name})")

        self.event_list.blockSignals(False)
        if self._filtered_indices:
            self.event_list.setCurrentRow(0)
            self._on_event_selected(0)
        else:
            self._current_event_index = None
            self.canvas.set_event(None, None)
            self.canvas_meta.setText("No matching events")
            self.inspector_detail.setText("No events match the active filter.")
            self._set_curation_enabled(False)

    def _on_event_selected(self, list_row: int) -> None:
        if (
            self._current_session is None
            or list_row < 0
            or list_row >= len(self._filtered_indices)
        ):
            self._current_event_index = None
            self._set_curation_enabled(False)
            return

        ev_idx = self._filtered_indices[list_row]
        self._current_event_index = ev_idx
        ev = self._current_session.events[ev_idx]
        self.canvas.set_event(self._current_session_dir, ev)

        interaction = ev.get("interaction") or {}
        screen = ev.get("screen") or {}
        cands = screen.get("candidates") or []
        mouse = interaction.get("mouse")
        target = interaction.get("target")
        accepted = interaction.get("accepted")

        pos_str = f"({mouse[0]:.0f}, {mouse[1]:.0f})" if mouse else "n/a"
        self.canvas_meta.setText(
            f"#{ev_idx} · app={screen.get('app') or 'unknown'} · mouse={pos_str} · {len(cands)} candidates"
        )
        self.inspector_detail.setText(
            f"Kind: {interaction.get('kind', 'frame')}\n"
            f"Accepted: {accepted}\n"
            f"Target: {target or 'none'}\n"
            f"Voice: {interaction.get('voice') or 'none'}"
        )
        self._set_curation_enabled(True, can_label=bool(interaction))

    def _set_curation_enabled(self, has_event: bool, *, can_label: bool = False) -> None:
        self.accept_btn.setEnabled(has_event and can_label)
        self.reject_btn.setEnabled(has_event and can_label)
        self.clear_target_btn.setEnabled(has_event and can_label)
        self.delete_event_btn.setEnabled(has_event)

    # --- Curation & Onboarding actions ----------------------------------

    def _on_grant_input_access(self) -> None:
        if self._perm_worker is not None and self._perm_worker.isRunning():
            return
        self.guide_grant_input_btn.setEnabled(False)
        self.curator_grant_btn.setEnabled(False)
        self.status_label.setText("Requesting input-device authorization…")
        self._perm_worker = _CallableWorker(grant_linux_input_access, self)
        self._perm_worker.finished_result.connect(self._on_grant_input_finished)
        self._perm_worker.start()

    def _on_grant_input_finished(self, ok: bool, message: str) -> None:
        self.guide_grant_input_btn.setEnabled(True)
        self.curator_grant_btn.setEnabled(True)
        self.status_label.setText(message)
        self._refresh_environment_view()

    def _on_seed_sample_session(self) -> None:
        sdir = seed_demo_session(self._paths)
        self.status_label.setText(f"Created sample session {sdir.name} for exploration.")
        self.refresh_all()
        self._select_tab(1)

    def _set_current_accepted(self, value: bool) -> None:
        if self._current_session_dir is None or self._current_event_index is None:
            return
        row = self.event_list.currentRow()
        try:
            update_event(self._current_session_dir, self._current_event_index, accepted=value)
        except Exception as exc:
            self.status_label.setText(f"Update failed: {exc}")
            return
        self.status_label.setText(
            f"Event #{self._current_event_index} marked {'accepted' if value else 'rejected'}."
        )
        self._reload_current_session(restore_row=row)

    def _on_canvas_target_assigned(self, candidate_id: str) -> None:
        if self._current_session_dir is None or self._current_event_index is None:
            return
        row = self.event_list.currentRow()
        try:
            update_event(
                self._current_session_dir,
                self._current_event_index,
                target=candidate_id,
                accepted=True,
            )
        except Exception as exc:
            self.status_label.setText(f"Target assign failed: {exc}")
            return
        self.status_label.setText(
            f"Event #{self._current_event_index} target set to {candidate_id}."
        )
        self._reload_current_session(restore_row=row)

    def _clear_current_target(self) -> None:
        if self._current_session_dir is None or self._current_event_index is None:
            return
        row = self.event_list.currentRow()
        try:
            update_event(self._current_session_dir, self._current_event_index, target=None)
        except Exception as exc:
            self.status_label.setText(f"Clear target failed: {exc}")
            return
        self.status_label.setText(f"Cleared target on event #{self._current_event_index}.")
        self._reload_current_session(restore_row=row)

    def _delete_current_event(self) -> None:
        if self._current_session_dir is None or self._current_event_index is None:
            return
        row = self.event_list.currentRow()
        try:
            remaining = delete_event(self._current_session_dir, self._current_event_index)
        except Exception as exc:
            self.status_label.setText(f"Delete event failed: {exc}")
            return
        self.status_label.setText(
            f"Deleted event #{self._current_event_index} ({remaining} remaining)."
        )
        self._reload_current_session(restore_row=max(0, min(row, remaining - 1)))

    def _reload_current_session(self, *, restore_row: int = 0) -> None:
        if self._current_session_dir is None:
            return
        self._current_session = load_session(self._current_session_dir)
        self._refresh_recall_tables()
        self._refresh_environment_view()
        self._refresh_session_metrics()
        self._rebuild_event_list()
        if 0 <= restore_row < self.event_list.count():
            self.event_list.setCurrentRow(restore_row)

    def _on_delete_session(self) -> None:
        if self._current_session is None or self._current_session_dir is None:
            return
        sid = self._current_session.session_id
        sdir = self._current_session_dir
        reply = QMessageBox.question(
            self,
            "Delete session",
            f"Delete recorded session {sid} and its screenshots?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        delete_session(self._sessions_dir, sdir)
        self.status_label.setText(f"Deleted session {sid}.")
        self.refresh_all()

    def _on_export_split(self) -> None:
        out_dir_str = QFileDialog.getExistingDirectory(
            self, "Export session-level train/val/test split", str(self._paths.data_dir)
        )
        if not out_dir_str:
            return
        try:
            manifest = export_curated_split(self._sessions_dir, Path(out_dir_str))
        except Exception as exc:
            self.status_label.setText(f"Export failed: {exc}")
            return
        counts = ", ".join(
            f"{k}:{v['events']}" for k, v in (manifest.get("splits") or {}).items()
        )
        self.status_label.setText(f"Exported curated split ({counts}) -> {out_dir_str}")

    # --- Live recording toggle ------------------------------------------

    def _toggle_recording(self) -> None:
        if self._recorder is not None:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        from keyless.gaze.collect import SessionCollector
        from keyless.gaze.recorder import (
            LinuxClickObserver,
            SessionRecorder,
            build_live_generator,
            build_live_screen_provider,
        )
        from keyless.platform.router import current_host

        collector = SessionCollector(self._paths)
        recorder = SessionRecorder(
            collector,
            build_live_generator(use_ocr=True),
            screen_provider=build_live_screen_provider(capture_pixels=True),
            save_screenshots=True,
        )
        observer = None
        if current_host() == "linux":
            observer = LinuxClickObserver(recorder.record_click)
            if not observer.start():
                reason = observer.unavailable_reason
                recorder.close()
                self.status_label.setText(
                    f"Cannot record clicks: {reason}. Click Grant input access in Guide or Curator."
                )
                self._refresh_environment_view()
                return
        self._recorder = recorder
        self._click_observer = observer
        self.record_btn.setText("Stop recording")
        self.status_label.setText(
            f"Recording live clicks -> {recorder.session_id[:10]} (click normally in other apps)"
        )

    def _stop_recording(self) -> None:
        if self._click_observer is not None:
            self._click_observer.stop()
            self._click_observer = None
        if self._recorder is not None:
            self._recorder.close()
            stats = self._recorder.stats()
            sid = self._recorder.session_id[:10]
            self._recorder = None
            self.status_label.setText(
                f"Saved session {sid}: {stats['clicks']} click(s), {stats['written']} event(s)."
            )
        self.record_btn.setText("Record session")
        self.refresh_all()
        if self._summaries:
            self._select_tab(1)

    # --- Dream-RSI & Strata controls ------------------------------------

    def _refresh_dream_view(self) -> None:
        ledger_path = self._repo_root / ".agents" / "harness" / "policy-ledger.jsonl"
        digest_md = self._repo_root / ".agents" / "traces" / "dream-digest.md"

        ledger_count = 0
        latest_hash = "none"
        latest_score = "n/a"
        if ledger_path.exists():
            lines = [ln for ln in ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            ledger_count = len(lines)
            if lines:
                last = json.loads(lines[-1])
                latest_hash = str(last.get("skills_hash", ""))[:12]
                latest_score = f"{float(last.get('score', 0.0)):.0%}"

        self.dream_summary_label.setText(
            f"Promoted policies: {ledger_count}  ·  Active skills_hash: {latest_hash}  ·  Last eval score: {latest_score}"
        )
        if digest_md.exists() and not self.dream_output.toPlainText().strip():
            self.dream_output.setPlainText(digest_md.read_text(encoding="utf-8"))

    def _toggle_strata_server(self) -> None:
        env = probe_lab_environment()
        if env.strata_serving:
            self._launch_dream_cmd(
                ["strata", "launch", "--stop"],
                "Stopping local Strata server…",
            )
        else:
            preset = os.environ.get("STRATA_PRESET", "qwen38-iq3s-128k")
            self._launch_dream_cmd(
                ["strata", "launch", "--preset", preset, "--no-tui", "--yes"],
                f"Starting local Strata server ({preset})…",
            )

    def _run_dream_task(self) -> None:
        task = self.dream_task_input.text().strip()
        if not task:
            self.status_label.setText("Enter a scoped task for the local Pi + Strata agent first.")
            return
        env = probe_lab_environment()
        if not env.strata_serving:
            self.status_label.setText("Start Strata first so the local model server is ready on :8080.")
            return
        self._launch_dream_cmd(
            [sys.executable, "scripts/agent_dream.py", "run", task],
            "Running local Pi + Strata agent task…",
        )

    def _run_dream_eval(self) -> None:
        self._launch_dream_cmd(
            [sys.executable, "scripts/agent_dream.py", "eval"],
            "Running all 6 deterministic repo invariants…",
        )

    def _run_dream_digest(self) -> None:
        self._launch_dream_cmd(
            [sys.executable, "scripts/agent_dream.py", "digest"],
            "Compiling local traces into dream-digest.md…",
        )

    def _run_dream_promote(self) -> None:
        note = self.dream_note_input.text().strip() or "ui-policy-update"
        self._launch_dream_cmd(
            [sys.executable, "scripts/agent_dream.py", "promote", "--note", note],
            "Evaluating invariants for monotonic policy promotion…",
        )

    def _copy_frontier_prompt(self) -> None:
        digest_md = self._repo_root / ".agents" / "traces" / "dream-digest.md"
        body = (
            digest_md.read_text(encoding="utf-8")
            if digest_md.exists()
            else (
                "Read .agents/skills/dream-harness/SKILL.md, AGENTS.md, and .agents/skills/**/SKILL.md.\n"
                "Propose minimal, surgical edits to improve local Pi+Strata execution without bloating the prompt."
            )
        )
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(body)
        self.status_label.setText("Copied Dream-RSI frontier prompt and digest to clipboard.")

    def _set_dream_buttons_enabled(self, enabled: bool) -> None:
        for btn in (
            self.guide_strata_btn,
            self.dream_strata_btn,
            self.dream_eval_btn,
            self.dream_digest_btn,
            self.dream_run_btn,
            self.dream_promote_btn,
        ):
            btn.setEnabled(enabled)

    def _launch_dream_cmd(self, cmd: list[str], message: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._set_dream_buttons_enabled(False)
        self.status_label.setText(message)
        self._worker = _CommandWorker(cmd, self._repo_root, parent=self)
        self._worker.finished_output.connect(self._on_dream_finished)
        self._worker.start()

    def _on_dream_finished(self, rc: int, output: str) -> None:
        self._set_dream_buttons_enabled(True)
        self.dream_output.setPlainText(output)
        self.status_label.setText("Completed." if rc == 0 else f"Command exited with {rc}.")
        self._refresh_environment_view()
        self._refresh_dream_view()

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self._recorder is not None:
            self._stop_recording()
        super().closeEvent(event)


def run_lab_ui(
    *,
    theme: str | None = None,
    sessions_dir: Path | None = None,
) -> int:
    """Launch the Keyless Lab desktop window."""
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    load_bundled_fonts()
    resolved = ThemeName(theme) if theme in ("light", "dark") else system_theme_name()
    window = LabWindow(sessions_dir=sessions_dir, theme=resolved)
    window.show()
    if owns_app:
        return int(app.exec())
    return 0
