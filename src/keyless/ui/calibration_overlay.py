"""Interactive translucent Qt desktop overlay for gaze calibration & live refinement."""

from __future__ import annotations

import math
import os
import sys
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from keyless.config import AppConfig, DisplayLayout, MonitorInfo
from keyless.vision.one_euro import OneEuroFilter2D
from keyless.vision.landmarks import STEREO_FEATURE_DIM, build_stereo_feature_vector


@dataclass(frozen=True)
class _CalibrationStageText:
    title: str
    detail: str
    spoken_cue: str = ""


_HUD_STAGES = {
    "intro": _CalibrationStageText(
        "Eye calibration",
        "Look at each dot as it appears, then follow the gliding target over your screen.",
        "Eye calibration ready. Look straight at the screen and press Start when you see the green dot.",
    ),
    "anchors": _CalibrationStageText(
        "Look at the dot",
        "A few dots will appear. Keep your eyes on each one until it moves.",
        "Look at each dot as it appears on your screen.",
    ),
    "pursuit1": _CalibrationStageText(
        "Follow the dot",
        "Keep your head still and follow the moving dot with your eyes.",
        "Follow the moving dot smoothly with your eyes.",
    ),
    "pursuit2": _CalibrationStageText(
        "Follow with head motion",
        "Keep looking at the dot while gently turning your head.",
        "Keep your eyes on the dot while gently turning and tilting your head.",
    ),
    "fitting": _CalibrationStageText(
        "Fitting gaze model…",
        "Computing your 3-D gaze model. This only takes a moment.",
    ),
    "refine": _CalibrationStageText(
        "Interactive check & refine",
        "Left-drag while looking to teach · Right-click to spot-fix.",
        "Calibration ready. Look around your desktop to check accuracy, drag to refine, or press Save.",
    ),
}


def _hud_stylesheet() -> str:
    return """
    QFrame#CalibHud {
        background: rgba(20, 21, 26, 232);
        border: 1px solid rgba(255, 255, 255, 38);
        border-radius: 16px;
    }
    QFrame#IntroCard {
        background: rgba(20, 21, 26, 236);
        border: 1px solid rgba(255, 255, 255, 42);
        border-radius: 18px;
    }
    QLabel {
        color: #f3f1eb;
        font-family: "Inter", "Cantarell", "Noto Sans", system-ui, sans-serif;
        background: transparent;
    }
    QLabel#HudSub {
        color: #a9a6a0;
        font-size: 12px;
    }
    QLabel#HudTitle {
        color: #f3f1eb;
        font-size: 13px;
        font-weight: 600;
    }
    QLabel#FaceBadge {
        font-size: 12px;
        font-weight: 600;
        padding: 3px 10px;
        border-radius: 10px;
    }
    QPushButton {
        background: rgba(255, 255, 255, 20);
        color: #f3f1eb;
        border: 1px solid rgba(255, 255, 255, 36);
        border-radius: 10px;
        padding: 6px 12px;
        font-size: 12px;
        font-weight: 600;
    }
    QPushButton:hover {
        background: rgba(255, 255, 255, 36);
    }
    QPushButton#PrimaryBtn {
        background: #f3f1eb;
        color: #16171b;
        border: 1px solid #f3f1eb;
    }
    QPushButton#PrimaryBtn:hover {
        background: #ffffff;
    }
    QPushButton#CancelBtn {
        color: #e69a8d;
        border-color: rgba(230, 154, 141, 70);
    }
    """


class CalibrationOverlayWindow(QWidget):
    """Frameless per-pixel translucent overlay for non-disruptive desktop calibration."""

    closed = Signal()

    def __init__(
        self,
        *,
        stereo: Any,
        config: AppConfig,
        target_mon: MonitorInfo,
        layout: DisplayLayout,
        calib_path: Path,
        samples_per_point: int = 22,
        settle_sec: float = 2.0,
        pose_settle_sec: float = 3.8,
        sweep1_duration_sec: float = 52.0,
        sweep2_duration_sec: float = 44.0,
        sweep_lead_in_sec: float = 2.5,
        speaker: Any = None,
        on_finished: Callable[[Any], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(None)
        self.stereo = stereo
        self.config = config
        self.target_mon = target_mon
        self.layout_info = layout
        self.calib_path = calib_path
        self.samples_per_point = int(samples_per_point)
        self.settle_sec = float(settle_sec)
        self.pose_settle_sec = float(pose_settle_sec)
        self.sweep1_duration_sec = float(sweep1_duration_sec)
        self.sweep2_duration_sec = float(sweep2_duration_sec)
        self.speaker = speaker
        self._on_finished = on_finished
        self._on_cancelled = on_cancelled

        from keyless.vision.calibrator import _default_dataset_path, _load_replay_dataset

        self.dataset_path = _default_dataset_path(calib_path)
        self.saved_replay = _load_replay_dataset(
            self.dataset_path, config.cam1_by_id, config.cam2_by_id
        )

        # Transparent, stays on top of the active monitor without replacing it.
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(target_mon.x, target_mon.y, target_mon.width, target_mon.height)
        self.setWindowTitle("Keyless Calibration")

        self._veil_mode: str = "light"  # "light" (~20%) | "medium" (~45%)
        self._voice_guide: bool = bool(getattr(config, "tts_onboarding_help", True))
        self._phase: str = "intro"
        self._cancelled = False
        self._accepted_model: Any = None

        # Face / camera observation tracking
        self._cam1_ok = False
        self._cam2_ok = False
        self._last_seq1 = -1
        self._last_seq2 = -1
        self._neutral_yaw = 0.0
        self._neutral_pitch = 0.0
        self._live_yaw = 0.0
        self._live_pitch = 0.0

        # Active collection state
        self._all_features: list[np.ndarray] = []
        self._all_targets: list[tuple[float, float]] = []
        self._all_groups: list[int] = []
        self._all_weights: list[float] = []

        self._calib_points: list[Any] = []
        self._pt_idx = 0
        self._pt_features: list[np.ndarray] = []
        self._pt_start_t = 0.0
        self._pt_manual_trigger = False
        self._prev_feat_vec: np.ndarray | None = None

        self._sweep_start_t = 0.0
        self._sweep_feats: list[np.ndarray] = []
        self._sweep_times: list[float] = []
        self._sweep_cursor_hist: list[tuple[float, int, int, float]] = []
        self._sweep_trail: deque[tuple[int, int]] = deque(maxlen=42)
        self._sweep_lead_in = float(sweep_lead_in_sec)

        # Refinement state
        self._model: Any = None
        self._flt = OneEuroFilter2D(min_cutoff=0.85, beta=0.003)
        self._gaze_x = target_mon.x + target_mon.width * 0.5
        self._gaze_y = target_mon.y + target_mon.height * 0.5
        self._raw_gaze_x = self._gaze_x
        self._raw_gaze_y = self._gaze_y
        self._mouse_x = target_mon.width // 2
        self._mouse_y = target_mon.height // 2
        self._left_down = False
        self._spot_burst_remaining = 0
        self._cursor_history: list[tuple[float, int, int, float]] = []
        self._refinement_batches: list[Any] = []
        self._active_stroke_feats: list[np.ndarray] = []
        self._active_stroke_times: list[float] = []
        self._active_stroke_trail: list[tuple[int, int]] = []
        self._prev_refine_feat: np.ndarray | None = None
        self._next_group_id = 100
        self._status_msg = ""
        self._status_until = 0.0

        self._fit_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="keyless-calib-fit")
        self._pending_fit: Future[Any] | None = None
        self._pending_fit_action = ""
        self._pending_fit_msg = ""
        self._save_when_fit_done = False

        # Current dot display
        self._dot_local_x = target_mon.width * 0.5
        self._dot_local_y = target_mon.height * 0.5
        self._dot_progress = 0.0
        self._dot_capturing = False
        self._dot_pose_delta: tuple[float, float] | None = None
        self._dot_caption = ""

        self._build_hud()
        self._set_phase("intro")

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _build_hud(self) -> None:
        self.setStyleSheet(_hud_stylesheet())
        self.hud = QFrame(self)
        self.hud.setObjectName("CalibHud")
        hud_row = QHBoxLayout(self.hud)
        hud_row.setContentsMargins(16, 10, 14, 10)
        hud_row.setSpacing(12)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        top_meta = QHBoxLayout()
        top_meta.setSpacing(8)
        self.title_lbl = QLabel("Eye calibration")
        self.title_lbl.setObjectName("HudTitle")
        self.face_badge = QLabel("Position face")
        self.face_badge.setObjectName("FaceBadge")
        top_meta.addWidget(self.title_lbl)
        top_meta.addWidget(self.face_badge)
        top_meta.addStretch(1)
        text_col.addLayout(top_meta)

        self.sub_lbl = QLabel("")
        self.sub_lbl.setObjectName("HudSub")
        self.sub_lbl.setWordWrap(False)
        text_col.addWidget(self.sub_lbl)
        hud_row.addLayout(text_col, 1)

        self.primary_btn = QPushButton("Start")
        self.primary_btn.setObjectName("PrimaryBtn")
        self.primary_btn.clicked.connect(self._on_primary_clicked)

        self.quick_btn = QPushButton("Quick refine")
        self.quick_btn.clicked.connect(self._on_quick_refine)

        self.clear_data_btn = QPushButton("Clear saved data")
        self.clear_data_btn.clicked.connect(self._on_clear_saved_data)

        self.spot_btn = QPushButton("Spot-fix")
        self.spot_btn.clicked.connect(self._trigger_spot_fix)

        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self._on_undo)

        self.redo_btn = QPushButton("Redo")
        self.redo_btn.clicked.connect(self._on_redo)

        self.veil_btn = QPushButton("Veil: Light")
        self.veil_btn.clicked.connect(self._toggle_veil)

        self.voice_btn = QPushButton("Voice: On" if self._voice_guide else "Voice: Off")
        self.voice_btn.clicked.connect(self._toggle_voice)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("CancelBtn")
        self.cancel_btn.clicked.connect(self._cancel)

        for btn in (
            self.primary_btn,
            self.quick_btn,
            self.clear_data_btn,
            self.spot_btn,
            self.undo_btn,
            self.redo_btn,
            self.veil_btn,
            self.voice_btn,
            self.cancel_btn,
        ):
            hud_row.addWidget(btn)

        # Keep the surface calm: only Start/Cancel are visible. The advanced
        # refinement controls remain connected for keyboard/experimental use.
        for btn in (
            self.quick_btn, self.clear_data_btn, self.spot_btn,
            self.undo_btn, self.redo_btn, self.veil_btn, self.voice_btn,
        ):
            btn.setVisible(False)

        # Compact centered welcome card only during intro (leaves 80%+ desktop visible).
        self.intro_card = QFrame(self)
        self.intro_card.setObjectName("IntroCard")
        intro_layout = QVBoxLayout(self.intro_card)
        intro_layout.setContentsMargins(24, 20, 24, 20)
        intro_layout.setSpacing(10)
        intro_title = QLabel(f"Calibrate gaze · {self.target_mon.connector}")
        intro_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        intro_body = QLabel(
            "A small dot moves around your screen. Just look at it.\n"
            "Press Space to start · Esc to cancel."
        )
        intro_body.setObjectName("HudSub")
        intro_body.setStyleSheet("font-size: 13px; color: #c7c4bd;")
        intro_body.setWordWrap(True)
        self.intro_hint = QLabel("Press Space to start · Esc to cancel")
        self.intro_hint.setStyleSheet("font-size: 12px; color: #9c9992;")
        intro_layout.addWidget(intro_title)
        intro_layout.addWidget(intro_body)
        intro_layout.addWidget(self.intro_hint)

        self._layout_hud()
        self._sync_hud_buttons()

    def _layout_hud(self) -> None:
        w = max(640, self.width())
        h = max(480, self.height())
        hud_w = min(1040, w - 36)
        self.hud.setGeometry((w - hud_w) // 2, 18, hud_w, 64)
        card_w = min(490, w - 60)
        self.intro_card.setGeometry((w - card_w) // 2, max(108, h // 2 - 96), card_w, 178)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_hud()

    def _speak_cue(self, text: str) -> None:
        if not text or not self._voice_guide or self.speaker is None:
            return
        try:
            self.speaker.speak(text, interrupt=True)
        except Exception:
            pass

    def _toggle_veil(self) -> None:
        self._veil_mode = "medium" if self._veil_mode == "light" else "light"
        self.veil_btn.setText("Veil: Soft dark" if self._veil_mode == "medium" else "Veil: Light")
        self.update()

    def _toggle_voice(self) -> None:
        self._voice_guide = not self._voice_guide
        self.voice_btn.setText("Voice: On" if self._voice_guide else "Voice: Off")
        if not self._voice_guide and self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception:
                pass

    def _sync_face_badge(self) -> None:
        ready = self._cam1_ok and self._cam2_ok
        self.face_badge.setText("Face ready" if ready else "Finding face…")
        if ready:
            self.face_badge.setStyleSheet(
                "background: rgba(95, 209, 141, 34); color: #7be3a4; border: 1px solid rgba(95, 209, 141, 80);"
            )
        else:
            self.face_badge.setStyleSheet(
                "background: rgba(227, 168, 74, 34); color: #f0be6b; border: 1px solid rgba(227, 168, 74, 80);"
            )
        if self._phase == "intro":
            self.primary_btn.setEnabled(ready)
            has_replay = self.saved_replay is not None and len(self.saved_replay[0]) > 0
            self.quick_btn.setEnabled(ready and has_replay)

    def _sync_hud_buttons(self) -> None:
        p = self._phase
        has_replay = self.saved_replay is not None and len(self.saved_replay[0]) > 0
        self.intro_card.setVisible(p == "intro")
        self.voice_btn.setVisible(self.speaker is not None and self.speaker.available())

        self.primary_btn.setVisible(p in ("intro", "refine"))
        self.primary_btn.setText("Save" if p == "refine" else "Start")
        self.quick_btn.setVisible(p == "intro" and has_replay)
        has_saved_files = has_replay or self.dataset_path.exists() or self.calib_path.exists()
        self.clear_data_btn.setVisible(p == "intro" and has_saved_files)
        self.spot_btn.setVisible(p == "refine")
        self.undo_btn.setVisible(p == "refine")
        self.undo_btn.setEnabled(bool(self._refinement_batches))
        self.redo_btn.setVisible(p in ("anchors", "pursuit1", "pursuit2", "refine"))
        self.redo_btn.setText("Restart" if p != "refine" else "Redo")
        self.veil_btn.setVisible(True)
        self.cancel_btn.setVisible(True)
        self._sync_face_badge()

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        info = _HUD_STAGES.get(phase, _HUD_STAGES["intro"])
        self.title_lbl.setText(info.title)
        self.sub_lbl.setText(info.detail)
        self._sync_hud_buttons()
        self._speak_cue(info.spoken_cue)
        self.update()

    def _reset_session_buffers(self, *, keep_replay: bool) -> None:
        self._all_features.clear()
        self._all_targets.clear()
        self._all_groups.clear()
        self._all_weights.clear()
        self._refinement_batches.clear()
        if keep_replay and self.saved_replay is not None:
            self._all_features.extend(self.saved_replay[0])
            self._all_targets.extend(self.saved_replay[1])
            self._all_groups.extend(self.saved_replay[2])
            self._all_weights.extend(self.saved_replay[3])
        self._next_group_id = (max(self._all_groups) + 1) if self._all_groups else 100

    def _begin_anchors(self) -> None:
        from keyless.vision.calibrator import ANCHOR_CALIBRATION_POINTS

        # A fresh full calibration must start clean so stale/bumped/mono sessions never poison it.
        # (Quick Refine still passes keep_replay=True.)
        self._reset_session_buffers(keep_replay=False)
        self._calib_points = list(ANCHOR_CALIBRATION_POINTS)
        self._pt_idx = 0
        self._start_current_anchor()
        self._set_phase("anchors")

    def _start_current_anchor(self) -> None:
        self._pt_features = []
        self._prev_feat_vec = None
        self._pt_manual_trigger = False
        self._pt_start_t = time.monotonic()
        self._dot_progress = 0.0
        self._dot_capturing = False
        self._dot_pose_delta = None
        if self._pt_idx < len(self._calib_points):
            pt = self._calib_points[self._pt_idx]
            self._dot_local_x = pt.rel_x * self.target_mon.width
            self._dot_local_y = pt.rel_y * self.target_mon.height
            self._dot_caption = f"Dot {self._pt_idx + 1}/{len(self._calib_points)} · {pt.prompt}"

    def _begin_pursuit(self, idx: int) -> None:
        self._sweep_start_t = time.monotonic()
        self._sweep_feats = []
        self._sweep_times = []
        self._sweep_cursor_hist = []
        self._sweep_trail.clear()
        self._dot_progress = 0.0
        self._dot_capturing = False
        self._dot_pose_delta = None
        self._dot_caption = ""
        self._set_phase("pursuit1" if idx == 1 else "pursuit2")

    def _finish_pursuit(self, idx: int) -> None:
        from keyless.vision.calibrator import _align_and_filter_pursuit_stroke

        feats, targs = _align_and_filter_pursuit_stroke(
            stroke_times=self._sweep_times,
            stroke_feats=self._sweep_feats,
            cursor_history=self._sweep_cursor_hist,
            mon_x=self.target_mon.x,
            mon_y=self.target_mon.y,
        )
        self._sweep_feats = []
        self._sweep_times = []
        self._sweep_cursor_hist = []
        grp_id = 50 if idx == 1 else 51
        self._all_features.extend(feats)
        self._all_targets.extend(targs)
        self._all_groups.extend([grp_id] * len(feats))
        self._all_weights.extend([1.8] * len(feats))
        if idx == 1:
            self._begin_pursuit(2)
        else:
            self._set_phase("fitting")
            self._begin_fitting_model(action="enter_refine", message="Fitting 3-D gaze model…")

    def _gather_all_data(self) -> tuple[list[np.ndarray], list[tuple[float, float]], list[int], list[float]]:
        feats = list(self._all_features)
        targs = list(self._all_targets)
        grps = list(self._all_groups)
        wgts = list(self._all_weights)
        for batch in self._refinement_batches:
            feats.extend(batch.features)
            targs.extend(batch.targets)
            grps.extend(batch.groups)
            wgts.extend(batch.weights)
        return feats, targs, grps, wgts

    def _begin_fitting_model(self, *, action: str, message: str) -> None:
        from keyless.vision.calibrator import _fit_gaze_model

        feats, targs, grps, wgts = self._gather_all_data()
        if not feats:
            self._set_phase("intro")
            return
        if self._pending_fit is not None and not self._pending_fit.done():
            self._pending_fit.cancel()
        self._pending_fit_action = action
        self._pending_fit_msg = message
        self.title_lbl.setText("Fitting gaze model…")
        self.sub_lbl.setText(message)
        X = np.vstack(feats)
        Y = np.asarray(targs, dtype=np.float64)
        groups = np.asarray(grps, dtype=np.intp)
        weights = np.asarray(wgts, dtype=np.float64)
        self._pending_fit = self._fit_executor.submit(
            _fit_gaze_model,
            X,
            Y,
            self.config,
            self.target_mon,
            self.layout_info,
            groups,
            weights,
        )

    def _on_primary_clicked(self) -> None:
        if self._phase == "intro":
            if self._cam1_ok and self._cam2_ok:
                self._begin_anchors()
        elif self._phase == "refine":
            self._save_and_finish()

    def _on_quick_refine(self) -> None:
        if self._phase == "intro" and self.saved_replay is not None and self._cam1_ok and self._cam2_ok:
            self._reset_session_buffers(keep_replay=True)
            self._set_phase("fitting")
            self._begin_fitting_model(action="enter_refine", message="Loading previous calibration for refinement…")

    def _on_clear_saved_data(self) -> None:
        self.saved_replay = None
        self._model = None
        self._reset_session_buffers(keep_replay=False)
        for path in (self.dataset_path, self.calib_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        self._sync_hud_buttons()
        self.sub_lbl.setText("Cleared all previous calibration data. Press Start for a fresh calibration.")

    def _trigger_spot_fix(self) -> None:
        if self._phase != "refine":
            return
        self._spot_burst_remaining = 20
        self._prev_refine_feat = None
        self._active_stroke_feats.clear()
        self._active_stroke_times.clear()
        self._status_msg = "Hold eyes on your cursor for a moment (spot-fix)…"
        self._status_until = time.monotonic() + 2.5

    def _on_undo(self) -> None:
        if self._phase != "refine" or not self._refinement_batches:
            return
        self._refinement_batches.pop()
        self._sync_hud_buttons()
        self._begin_fitting_model(
            action="update_refine",
            message=f"Undid last stroke ({len(self._refinement_batches)} left)",
        )

    def _on_redo(self) -> None:
        self._pending_fit = None
        self._set_phase("intro")

    def _save_and_finish(self) -> None:
        if self._pending_fit is not None:
            if not self._pending_fit.done():
                # Never block the Qt GUI main thread waiting for a background fit.
                self._save_when_fit_done = True
                self.primary_btn.setEnabled(False)
                self.primary_btn.setText("Saving…")
                self.sub_lbl.setText("Finishing model fit before saving…")
                return
            self._model = self._pending_fit.result()
            self._pending_fit = None
        self._save_when_fit_done = False
        if self._model is None:
            return
        from keyless.vision.calibrator import _save_replay_dataset

        feats, targs, grps, wgts = self._gather_all_data()
        _save_replay_dataset(
            dataset_path=self.dataset_path,
            X=np.vstack(feats),
            Y=np.asarray(targs, dtype=np.float64),
            groups=np.asarray(grps, dtype=np.intp),
            weights=np.asarray(wgts, dtype=np.float64),
            cam1_by_id=self.config.cam1_by_id,
            cam2_by_id=self.config.cam2_by_id,
        )
        self._model.save(self.calib_path)
        self._accepted_model = self._model
        self._timer.stop()
        self._fit_executor.shutdown(wait=False, cancel_futures=True)
        if self._on_finished is not None:
            self._on_finished(self._accepted_model)
        self.close()

    def _cancel(self) -> None:
        self._cancelled = True
        self._timer.stop()
        self._fit_executor.shutdown(wait=False, cancel_futures=True)
        if self._on_cancelled is not None:
            self._on_cancelled()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self._fit_executor.shutdown(wait=False, cancel_futures=True)
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._cancel()
            return
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._phase == "intro":
                if self._cam1_ok and self._cam2_ok:
                    self._begin_anchors()
            elif self._phase == "anchors":
                self._pt_manual_trigger = True
            elif self._phase == "refine":
                self._save_and_finish()
            return
        if key == Qt.Key.Key_P and self._phase == "intro":
            self._on_quick_refine()
            return
        if key == Qt.Key.Key_R:
            if self._phase == "anchors":
                self._start_current_anchor()
            elif self._phase in ("pursuit1", "pursuit2", "refine"):
                self._on_redo()
            return
        if key == Qt.Key.Key_F and self._phase == "refine":
            self._trigger_spot_fix()
            return
        if key == Qt.Key.Key_U and self._phase == "refine":
            self._on_undo()
            return
        if key == Qt.Key.Key_X and self._phase == "intro" and self.saved_replay is not None:
            self.saved_replay = None
            if self.dataset_path.exists():
                self.dataset_path.unlink()
            self._sync_hud_buttons()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position().toPoint()
        self._record_cursor(pos)
        if self.hud.geometry().contains(pos) or (
            self.intro_card.isVisible() and self.intro_card.geometry().contains(pos)
        ):
            super().mousePressEvent(event)
            return
        if self._phase == "refine":
            if event.button() == Qt.MouseButton.LeftButton:
                self._left_down = True
            elif event.button() == Qt.MouseButton.RightButton:
                self._trigger_spot_fix()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._record_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._record_cursor(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and self._left_down:
            self._left_down = False
            self._commit_pursuit_stroke()
        super().mouseReleaseEvent(event)

    def _record_cursor(self, pos: QPoint) -> None:
        now_t = time.monotonic()
        px, py = int(pos.x()), int(pos.y())
        prev_t, prev_x, prev_y, _ = (
            self._cursor_history[-1] if self._cursor_history else (now_t - 0.016, px, py, 0.0)
        )
        dt = max(1e-3, now_t - prev_t)
        speed = math.hypot(px - prev_x, py - prev_y) / dt
        self._cursor_history.append((now_t, px, py, speed))
        if len(self._cursor_history) > 1200:
            del self._cursor_history[:600]
        self._mouse_x = px
        self._mouse_y = py

    def _commit_pursuit_stroke(self) -> None:
        from keyless.vision.calibrator import _RefinementBatch, _align_and_filter_pursuit_stroke

        aligned_f, aligned_t = _align_and_filter_pursuit_stroke(
            stroke_times=self._active_stroke_times,
            stroke_feats=self._active_stroke_feats,
            cursor_history=self._cursor_history,
            mon_x=self.target_mon.x,
            mon_y=self.target_mon.y,
        )
        self._active_stroke_feats.clear()
        self._active_stroke_times.clear()
        self._active_stroke_trail.clear()
        if len(aligned_f) >= 10:
            self._refinement_batches.append(
                _RefinementBatch(
                    features=aligned_f,
                    targets=aligned_t,
                    groups=[self._next_group_id] * len(aligned_f),
                    weights=[2.6] * len(aligned_f),
                )
            )
            self._next_group_id += 1
            self._sync_hud_buttons()
            self._begin_fitting_model(
                action="update_refine",
                message=f"Added {len(aligned_f)} pursuit samples…",
            )
        else:
            self._status_msg = "Stroke too short — hold Left-Click and move smoothly while looking at cursor."
            self._status_until = time.monotonic() + 2.8

    def _tick(self) -> None:
        if self._pending_fit is not None and self._pending_fit.done():
            fut = self._pending_fit
            self._pending_fit = None
            try:
                self._model = fut.result()
            except Exception as exc:
                self._save_when_fit_done = False
                self._status_msg = f"Model fit failed: {exc}"
                self._status_until = time.monotonic() + 5.0
                if self._phase == "fitting":
                    self._set_phase("intro")
                self.sub_lbl.setText(self._status_msg)
                self._sync_hud_buttons()
            else:
                rmse_x = getattr(self._model, "rmse_x", 0.0)
                rmse_y = getattr(self._model, "rmse_y", 0.0)
                samples = getattr(self._model, "n_train_samples", 0)
                self._status_msg = f"Updated · {rmse_x:.0f} × {rmse_y:.0f} px error · {samples} samples"
                self._status_until = time.monotonic() + 4.0
                if self._pending_fit_action == "enter_refine":
                    self._set_phase("refine")
                self.title_lbl.setText(
                    f"Interactive refine · {rmse_x:.0f} × {rmse_y:.0f} px ({samples} samples)"
                )
                self.sub_lbl.setText(self._status_msg)
                self._sync_hud_buttons()
                if self._save_when_fit_done:
                    self._save_and_finish()
                    return

        pair = self.stereo.read_pair() if self.stereo is not None else None
        now = time.monotonic()
        new_obs = False
        o1 = o2 = None
        if pair is not None:
            o1 = pair.cam1.observation
            o2 = pair.cam2.observation
            self._cam1_ok = o1 is not None
            self._cam2_ok = o2 is not None
            new_obs = pair.cam1.seq != self._last_seq1 or pair.cam2.seq != self._last_seq2
            if new_obs:
                self._last_seq1 = pair.cam1.seq
                self._last_seq2 = pair.cam2.seq
                if o1 is not None and o2 is not None:
                    self._live_yaw = 0.5 * (o1.yaw + o2.yaw)
                    self._live_pitch = 0.5 * (o1.pitch + o2.pitch)
                    if self._phase == "intro":
                        self._neutral_yaw = self._live_yaw
                        self._neutral_pitch = self._live_pitch
        self._sync_face_badge()

        if self._phase == "anchors":
            self._step_anchors(now, new_obs, o1, o2)
        elif self._phase in ("pursuit1", "pursuit2"):
            self._step_pursuit(now, new_obs, o1, o2)
        elif self._phase == "refine":
            self._step_refine(now, new_obs, o1, o2)
        self.update()

    def _step_anchors(self, now: float, new_obs: bool, o1: Any, o2: Any) -> None:
        from keyless.vision.calibrator import _iris_step_distance, _trim_dot_outliers

        if self._pt_idx >= len(self._calib_points):
            self._begin_pursuit(1)
            return
        pt = self._calib_points[self._pt_idx]
        elapsed = now - self._pt_start_t
        cur_settle = self.pose_settle_sec if pt.is_pose_variation else self.settle_sec
        is_settling = (elapsed < cur_settle) and (not self._pt_manual_trigger)
        self._dot_capturing = not is_settling
        self._dot_progress = min(1.0, len(self._pt_features) / max(1, self.samples_per_point))

        if pt.is_pose_variation and is_settling:
            self._dot_pose_delta = (
                math.degrees(self._live_yaw - self._neutral_yaw),
                math.degrees(self._live_pitch - self._neutral_pitch),
            )
        else:
            self._dot_pose_delta = None

        if (
            new_obs
            and not is_settling
            and o1 is not None
            and o2 is not None
            and o1.max_blink < self.config.blink_threshold
            and o2.max_blink < self.config.blink_threshold
        ):
            feat, both_ok = build_stereo_feature_vector(o1, o2)
            if both_ok and feat.shape == (STEREO_FEATURE_DIM,):
                step = 0.0 if self._prev_feat_vec is None else _iris_step_distance(self._prev_feat_vec, feat)
                self._prev_feat_vec = feat
                if step < 0.095:
                    self._pt_features.append(feat)

        if len(self._pt_features) >= self.samples_per_point:
            cleaned = _trim_dot_outliers(self._pt_features, keep_ratio=0.82)
            gx = self.target_mon.x + pt.rel_x * self.target_mon.width
            gy = self.target_mon.y + pt.rel_y * self.target_mon.height
            self._all_features.extend(cleaned)
            self._all_targets.extend([(gx, gy)] * len(cleaned))
            self._all_groups.extend([self._pt_idx] * len(cleaned))
            self._all_weights.extend([1.5] * len(cleaned))
            self._pt_idx += 1
            if self._pt_idx >= len(self._calib_points):
                self._begin_pursuit(1)
            else:
                self._start_current_anchor()

    def _step_pursuit(self, now: float, new_obs: bool, o1: Any, o2: Any) -> None:
        from keyless.vision.calibrator import _eval_continuous_sweep_path

        sweep_idx = 1 if self._phase == "pursuit1" else 2
        dur = self.sweep1_duration_sec if sweep_idx == 1 else self.sweep2_duration_sec
        elapsed = now - self._sweep_start_t
        if elapsed >= self._sweep_lead_in + dur:
            self._finish_pursuit(sweep_idx)
            return

        u = 0.0 if elapsed < self._sweep_lead_in else (elapsed - self._sweep_lead_in) / max(0.1, dur)
        rx, ry = _eval_continuous_sweep_path(sweep_idx, u)
        lx = int(round(rx * self.target_mon.width))
        ly = int(round(ry * self.target_mon.height))
        self._dot_local_x = float(lx)
        self._dot_local_y = float(ly)
        self._dot_progress = u
        self._dot_capturing = elapsed >= self._sweep_lead_in
        self._sweep_cursor_hist.append((now, lx, ly, 200.0))
        self._sweep_trail.append((lx, ly))

        if (
            new_obs
            and self._dot_capturing
            and o1 is not None
            and o2 is not None
            and o1.max_blink < self.config.blink_threshold
            and o2.max_blink < self.config.blink_threshold
        ):
            feat, both_ok = build_stereo_feature_vector(o1, o2)
            if both_ok and feat.shape == (STEREO_FEATURE_DIM,):
                self._sweep_feats.append(feat)
                self._sweep_times.append(now)

    def _step_refine(self, now: float, new_obs: bool, o1: Any, o2: Any) -> None:
        from keyless.vision.calibrator import _RefinementBatch, _iris_step_distance, _trim_dot_outliers

        if self._model is None or not new_obs:
            return
        if (
            o1 is not None
            and o2 is not None
            and o1.max_blink < self.config.blink_threshold
            and o2.max_blink < self.config.blink_threshold
        ):
            feat, both_ok = build_stereo_feature_vector(o1, o2)
            if both_ok and feat.shape == (STEREO_FEATURE_DIM,):
                rx, ry = self._model.predict(feat)
                self._raw_gaze_x, self._raw_gaze_y = float(rx), float(ry)
                sx, sy = self._flt(self._raw_gaze_x, self._raw_gaze_y, now)
                self._gaze_x, self._gaze_y = float(sx), float(sy)

                eye_step = (
                    0.0
                    if self._prev_refine_feat is None
                    else _iris_step_distance(self._prev_refine_feat, feat)
                )
                self._prev_refine_feat = feat

                if self._left_down and self._spot_burst_remaining == 0:
                    cur_speed = self._cursor_history[-1][3] if self._cursor_history else 0.0
                    if cur_speed < 950.0 and eye_step < 0.120:
                        self._active_stroke_feats.append(feat)
                        self._active_stroke_times.append(now)
                        self._active_stroke_trail.append((self._mouse_x, self._mouse_y))
                elif self._spot_burst_remaining > 0 and eye_step < 0.095:
                    self._active_stroke_feats.append(feat)
                    self._spot_burst_remaining -= 1
                    if self._spot_burst_remaining == 0 and len(self._active_stroke_feats) >= 6:
                        cleaned = _trim_dot_outliers(self._active_stroke_feats, keep_ratio=0.85)
                        t_xy = (
                            float(self.target_mon.x + self._mouse_x),
                            float(self.target_mon.y + self._mouse_y),
                        )
                        self._refinement_batches.append(
                            _RefinementBatch(
                                features=cleaned,
                                targets=[t_xy] * len(cleaned),
                                groups=[self._next_group_id] * len(cleaned),
                                weights=[3.5] * len(cleaned),
                            )
                        )
                        self._next_group_id += 1
                        self._active_stroke_feats.clear()
                        self._active_stroke_times.clear()
                        self._sync_hud_buttons()
                        self._begin_fitting_model(
                            action="update_refine",
                            message=f"Spot-fix applied ({len(cleaned)} samples)",
                        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        if self._phase == "refine":
            veil_alpha = 20 if self._veil_mode == "light" else 76
        else:
            veil_alpha = 52 if self._veil_mode == "light" else 132
        painter.fillRect(self.rect(), QColor(12, 13, 18, veil_alpha))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        if self._phase in ("anchors", "pursuit1", "pursuit2"):
            self._paint_target_dot(painter)
        elif self._phase == "refine":
            self._paint_refinement(painter)
        painter.end()

    def _paint_target_dot(self, painter: QPainter) -> None:
        if self._phase in ("pursuit1", "pursuit2") and len(self._sweep_trail) >= 2:
            path = QPainterPath()
            pts = list(self._sweep_trail)
            path.moveTo(float(pts[0][0]), float(pts[0][1]))
            for x, y in pts[1:]:
                path.lineTo(float(x), float(y))
            painter.setPen(QPen(QColor(255, 255, 255, 75), 2.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        center = QPointF(self._dot_local_x, self._dot_local_y)

        if self._dot_pose_delta is not None:
            dyaw_deg, dpitch_deg = self._dot_pose_delta
            head_pt = QPointF(center.x() - dyaw_deg * 5.0, center.y() + dpitch_deg * 5.0)
            painter.setPen(QPen(QColor(240, 190, 107, 190), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(center, head_pt)

        # Contrast dark backdrop halo keeps dot crisp over light or dark desktop apps.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 16, 20, 210))
        painter.drawEllipse(center, 22.0, 22.0)

        accent = QColor("#5fd18d") if self._dot_capturing else QColor("#c95b2b")
        painter.setPen(QPen(QColor(255, 255, 255, 65), 2.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, 18.0, 18.0)

        if self._dot_progress > 0.0:
            arc_pen = QPen(accent, 3.0)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc_pen)
            rect = QRectF(center.x() - 18.0, center.y() - 18.0, 36.0, 36.0)
            painter.drawArc(rect, 90 * 16, -int(round(self._dot_progress * 360 * 16)))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(center, 7.5, 7.5)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(center, 2.2, 2.2)

        if self._phase == "anchors" and self._dot_caption:
            y_off = 38.0 if center.y() < 220 else -38.0
            pill_w = min(380.0, float(self.width() - 40))
            pill_rect = QRectF(
                max(20.0, min(self.width() - pill_w - 20.0, center.x() - pill_w / 2.0)),
                center.y() + y_off - 14.0,
                pill_w,
                28.0,
            )
            painter.setBrush(QColor(20, 21, 26, 210))
            painter.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
            painter.drawRoundedRect(pill_rect, 10.0, 10.0)
            painter.setFont(QFont("Inter", 11))
            painter.setPen(QColor(243, 241, 235, 210 if not self._dot_capturing else 110))
            painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self._dot_caption)

    def _paint_refinement(self, painter: QPainter) -> None:
        if len(self._active_stroke_trail) >= 2:
            path = QPainterPath()
            path.moveTo(float(self._active_stroke_trail[0][0]), float(self._active_stroke_trail[0][1]))
            for x, y in self._active_stroke_trail[1:]:
                path.lineTo(float(x), float(y))
            painter.setPen(QPen(QColor(240, 190, 107, 195), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        # Mouse target reticle (contrast backdrop + crisp ring)
        m_pt = QPointF(float(self._mouse_x), float(self._mouse_y))
        active = self._left_down or self._spot_burst_remaining > 0
        r_m = 14.0 if active else 10.0
        painter.setPen(QPen(QColor(15, 16, 20, 170), 4.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(m_pt, r_m, r_m)
        m_col = QColor("#e3a84a") if active else QColor("#f3f1eb")
        painter.setPen(QPen(m_col, 2.0))
        painter.drawEllipse(m_pt, r_m, r_m)

        # Filtered live gaze ring directly over desktop
        lx = self._gaze_x - self.target_mon.x
        ly = self._gaze_y - self.target_mon.y
        if 0 <= lx <= self.target_mon.width and 0 <= ly <= self.target_mon.height:
            g_pt = QPointF(lx, ly)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(15, 16, 20, 155))
            painter.drawEllipse(g_pt, 22.0, 22.0)
            painter.setPen(QPen(QColor("#5fd18d"), 2.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(g_pt, 17.0, 17.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#5fd18d"))
            painter.drawEllipse(g_pt, 3.0, 3.0)


def run_translucent_calibration_overlay(
    *,
    config: AppConfig,
    calib_path: Path,
    target_mon: MonitorInfo,
    layout: DisplayLayout,
    samples_per_point: int = 12,
    settle_sec: float = 1.2,
    pose_settle_sec: float = 2.0,
    sweep1_duration_sec: float = 12.0,
    sweep2_duration_sec: float = 10.0,
) -> Any:
    """Run the translucent Qt calibration overlay on the target monitor."""
    # Ensure XWayland positioning works when running interactively under Wayland.
    if (
        sys.platform == "linux"
        and os.environ.get("DISPLAY")
        and not os.environ.get("QT_QPA_PLATFORM")
    ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    from keyless.platform.router import resolve_camera_config
    from keyless.vision.stereo_capture import StereoCapture

    # On macOS the built-in camera is enough and virtual cameras (OBS etc.)
    # poison the stereo model. Respect an explicit stereo choice, otherwise
    # use mono with the first discovered physical camera.
    if sys.platform == "darwin" and config.camera_mode != "stereo":
        config.camera_mode = "mono"
    config.camera_mode, config.cam1_by_id, config.cam2_by_id = resolve_camera_config(
        config.camera_mode, config.cam1_by_id, config.cam2_by_id
    )

    speaker = None
    if getattr(config, "tts_onboarding_help", True):
        try:
            from keyless.voice.tts import WoofSpeaker, tts_report

            if tts_report()["ready"]:
                speaker = WoofSpeaker(voice=getattr(config, "tts_voice", "juno"), enabled=True)
        except Exception:
            speaker = None

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        QApplication.setApplicationName("Keyless Calibration")
        app = QApplication([sys.argv[0]])

    result_box: dict[str, Any] = {"model": None, "cancelled": False}

    with StereoCapture(
        cam1_by_id=config.cam1_by_id,
        cam2_by_id=config.cam2_by_id or None,
        mono=config.camera_mode == "mono",
        width=config.frame_width,
        height=config.frame_height,
        fps=config.fps,
        cam1_exposure=config.cam1_exposure,
        cam1_gain=config.cam1_gain,
        cam2_exposure=config.cam2_exposure,
        cam2_gain=config.cam2_gain,
        extract_landmarks=True,
        use_gpu=config.use_gpu,
        keep_images=False,
        capture_backend=getattr(config, "camera_backend", "v4l2"),
    ) as stereo:
        win = CalibrationOverlayWindow(
            stereo=stereo,
            config=config,
            target_mon=target_mon,
            layout=layout,
            calib_path=calib_path,
            samples_per_point=samples_per_point,
            settle_sec=settle_sec,
            pose_settle_sec=pose_settle_sec,
            sweep1_duration_sec=sweep1_duration_sec,
            sweep2_duration_sec=sweep2_duration_sec,
            speaker=speaker,
            on_finished=lambda m: result_box.__setitem__("model", m),
            on_cancelled=lambda: result_box.__setitem__("cancelled", True),
        )
        win.show()
        win.raise_()
        win.activateWindow()
        if owns_app:
            app.exec()
        else:
            from PySide6.QtCore import QEventLoop

            loop = QEventLoop()
            win.closed.connect(loop.quit)
            loop.exec()

    if speaker is not None:
        speaker.close()

    if result_box["cancelled"] or result_box["model"] is None:
        raise KeyboardInterrupt("Calibration cancelled by user.")
    return result_box["model"]
