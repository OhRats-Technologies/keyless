# Handoff - Gaze v2 Greenfield & Unified Local Web UI (`alt`)

**Date:** 2026-11-24
**Branch:** `alt` (single active development branch; synced with `origin/main` through `70db92f`)
**Base:** `main` / `origin/main` = `70db92f` (n123xyz's browser agent + Split View; do not merge `alt` into `main` without explicit approval)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055` (audit only; never a code source)

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 1. Current Architecture & Priority (2026-11-24)

Both native UI toolkits (Qt / `PySide6` in `src/keyless/ui/` and macOS SwiftUI in `packaging/macos/`) and the separate Lab window have been removed and replaced by **one unified local HTTP web app** served directly by `ControlServer` (`src/keyless/ipc/control.py` + `src/keyless/web/`).

The #1 priority is **shipping working monocular eye tracking** end-to-end:

1. **Unified Local HTTP Web App (`src/keyless/web/` + `src/keyless/ipc/control.py`) - SHIPPED:**
   - Single zero-build HTML/CSS/ES module UI at `http://127.0.0.1:8766` styled with canonical OhRats tokens (`#efede5`/`#1a1a1b` canvas, `#ffffff`/`#202021` surface, `#634936` structural hairlines, `#c95b2b`/`#e27a3b` terracotta accent, collapsible left sidebar, inline copyable error callout).
   - Five views in one app: **Home** (listening control, summary metrics, recent activity, searchable voice commands), **Gaze** (live monocular webcam SSE canvas, cold-start prior vs. ridge adaptation, `Undo` / `Reset`, 1-click session recording), **Sessions** (Phase 1 >95% recall gate, clickable candidate boxes, `Accept (A)` / `Reject (R)` / `Delete (Del)`, Baseline 0 metrics, session-level split export), **Activity**, and **Settings** (schema-driven from `/v1/config/schema`, Linux input permission grant, offline STT model download).
   - Launcher: `keyless ui` starts `ControlServer` (or reuses an existing instance) and opens the URL via `xdg-open` on Linux (`open` on macOS, `webbrowser.open` fallback).
   - `pyside6` removed from `pyproject.toml`; `core/ui_contract.py` and `core/commands.py` remain pure Python.
2. **Live Monocular Gaze Publisher (`src/keyless/gaze/monocular.py`) - SHIPPED:**
   - Captures one webcam cross-platform (`resolve_camera_config` + V4L2/OpenCV) and extracts 10D face/iris features via MediaPipe `FaceFeatureExtractor` (GPU EGL verified at ~30 FPS on RTX 3090).
   - Cold-start geometric prior (frame 1, zero calibration clicks) + reversible passive online ridge regression once `min_clicks` (default 5) physical clicks are collected.
   - Publishes `(x, y, flags, timestamp_ns)` to `SharedGazeWriter` (`keyless_gaze_v1`), and streams live coordinates over `GET /v1/gaze/stream` (SSE) to the web UI.
   - CLI: `keyless gaze run` and `keyless gaze record --camera` (default on).
3. **Real-World Phase 1 Recall & Baseline Gate (NEXT ACTION):**
   - Record 2 to 3 natural desktop sessions from the web UI (**Record session**) or CLI (`keyless gaze record --save-screenshots`).
   - Check the Phase 1 recall meter in **Sessions** or run `keyless gaze eval-candidates --regenerate` and verify overall candidate recall **>95%** (GREENFIELD §9 kill criterion). Below 95%: fix perception (AX/OCR/DOM/visual coverage), not the model.
   - Check Baseline 0 metrics (`hit@25/50/100`, `baseline_top1`) in **Sessions** or via `keyless gaze replay <session_dir>`.
4. **Phase 2 Offline Target Scorer (ONLY AFTER STEP 3 PASSES):**
   - Plug a real feature/embedding extractor into `keyless gaze probe <sessions_dir> --extractor module:callable` and verify it beats `baseline_top1` on session-held-out splits (`split_sessions`).

## 2. Verification (current)

```bash
.venv/bin/python scripts/agent_dream.py eval
# 6/6 checks passed (score 1.0):
#   no_legacy_leakage, control_no_ui_import, no_undefined_names,
#   compileall, pytest_core, pytest_web
```

## 3. Do Not

- Do not create extra side branches; keep active work on `alt`.
- Do not reintroduce PySide6/Qt or SwiftUI shells; the local HTTP web app is the single UI.
- Do not import or copy `legacy-gaze-v1`.
- Do not restore a calibration file, dot grid, scalar bias, or startup gate.
- Do not reintroduce a global recenter or "steer to mouse" anchor.
- Do not move the real pointer implicitly.
- Do not start Phase 2 neural/scorer training before real-session candidate recall > 95%.
- Do not merge `alt` into `main` without explicit user approval.
