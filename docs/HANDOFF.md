# Handoff - Gaze v2 Greenfield & Unified Local Web UI (`alt`)

**Date:** 2026-11-24
**Branch:** `alt` (single active development branch; synced with `origin/main` through `70db92f`)
**Base:** `main` / `origin/main` = `70db92f` (n123xyz's browser agent + Split View; do not merge `alt` into `main` without explicit approval)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055` (audit only; never a code source)

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 000. macOS parity session (2026-11-24, MacBook Air M5 13.6in, macOS 26, one display)

Commits on `alt`: c0e07ab, f945bc4, 5216165, 2f85b43, 8a0a84c, f1fbe0f, cde29e8, bd52ad9.
Tests: 407 pass + 2 skip (both evdev-only) excluding packaging/release/instance-lock.

**Tested on real hardware**
- Camera: OpenCV AVFoundation index = devices sorted by `uniqueID` (verified by grabbing a
  frame per index). Old code picked the OBS virtual camera and paired it into "binocular".
  Auto is now mono on the built-in (`transport == bltn`); 208/208 and 230/230 face frames at 30 fps,
  MediaPipe on Metal (Apple M5).
- Display: CG global coords (top-left, y down); `CGDisplayScreenSize` = 290.6 x 189.0 mm.
- Step 1, onboarding warm-up, **browser fallback** full screen, 1 display:
  grade **great**, accuracy **1.85 deg / 81 px**, precision 0.36 deg, distance 488 mm,
  253/424 labels, 14.2 s. Latency **320 ms = the old search-window edge** (conf 0.53), i.e. not
  measured; window is now 600 ms and an edge minimum reports confidence 0.
- Step 2, click capture (listen-only CGEventTap, Input Monitoring): 4 real clicks captured,
  3 paired to a fixation, 1 rejected by the look gate, 2 labels. `stats.clicks_captured` is new.
- Step 3, eye cursor: synthetic shm path drove the cua agent cursor (screenshot); calibration
  hold hid it and returned the hardware pointer; stop exits cua-driver. Live overlay active
  on real gaze.
- Step 4, native stage: synthetic sweep drew dim + stimulus on the display (screenshots),
  start 0.37 s / stop 0.02 s.

- Menu-bar item (`keyless ui` on macOS; `--no-tray` to skip): Open Keyless, Eye tracking on/off,
  Quit Keyless. Icon follows gaze state (eye / eye.slash, refreshed every 1 s). Quit, Ctrl+C and
  SIGTERM all stop gaze, the eye cursor, cua-driver and the server (verified: no process, port free).
  Menu clicks themselves were not exercised by the agent (that would drive the real pointer).

**Not tested yet (needs a person at the Mac)**
- A full warm-up through the **native stage** (grade/latency vs the browser run above).
- Live eye-cursor quality. While typing (looking at the keyboard) predictions pinned to
  screen edges, with looking *down* mapping to the *top* (y=0): check with eyes on screen
  whether vertical sign is right or this is just off-screen extrapolation.
- Multi-display (only one display here): stage windows per screen, rig solve.
- Allow screens in onboarding (all grants already present, so they never showed).
- Browser full-screen guard (`not_fullscreen` pause) in a real browser.
- Fresh-user TCC: grants attach to the *responsible app* (Terminal/IDE when run from a
  shell), so "Turn on Keyless" copy is only accurate for a packaged app.

**Known issues**
- Opening the iPhone Continuity Camera rings the phone: never probe all camera indexes.
  `_opencv_cameras()` (last-resort fallback when AVFoundation is missing) still probes 0..3.
- A suspended built-in camera (lid closed) gives 0 frames silently; onboarding should say so.

## 00. NEXT SESSION: macOS parity (written 2026-11-24, on Linux, before moving to the Mac)

**Branch:** `alt` (pushed). Last Linux commit: onboarding + pursuit warm-up + learner + rig solver (D12).
Everything in section 0 was built and verified **on Linux (KDE Wayland) only**. On macOS it is
not good yet. Known gaps, verified by reading the code:

| Piece | Linux | macOS today | What to build |
|---|---|---|---|
| Gaze publisher + camera | works (v4l2/pipewire) | `opencv` backend selected; untested since D12 | run `keyless ui`, confirm frames/fps, MediaPipe on Apple Silicon |
| Eye cursor (reticle) | `overlay_wayland.c` | **not started**: `monocular.py` starts the overlay only on linux | drive `platform/cua/agent_cursor.py` from the shm channel; keep AGENTS rule 3/4 (single cursor, `pointer_mode.py`) |
| Click learning | `LinuxClickObserver` (evdev) | **none**: `monocular.py` wires clicks only on linux | `CGEventTap` (Quartz) listener for left-mouse-down -> `publisher.on_click(x, y, t_click=monotonic)`; needs Accessibility/Input Monitoring permission UX in onboarding |
| Calibration stage | native layer-shell stage, all displays dimmed | **falls back to browser full-screen on ONE display** | native stage for macOS: borderless click-through `NSWindow` per `NSScreen` at `.screenSaver` level, reads `keyless_stage_v1` (layout `=QddffffII`, see `gaze/stage.py`); port `draw_stimulus` (or use a Metal/CoreImage layer). Python side (`CalibrationSession`) needs only a `MacStageOverlay` with the same `start/stop/running/error` as `LinuxStageOverlay` |
| Display layout | xdg-output | `platform/macos/display.py` | verify `width_mm/height_mm` (CGDisplayScreenSize) and Retina scale: stage/shm coords are **logical points**; check `DisplayPlane.scale` handling |
| Browser fallback monitor | n/a | browser cannot know which screen it is on | pass `connector` from the page if `window.getScreenDetails()` is available (Chromium), else primary |

### Order of work on the Mac
1. `git checkout alt && git pull`; `uv sync`/venv; run the test command in `AGENTS.md` (expect 398 pass + 1 skip; packaging/release tests are known-failing).
2. `keyless ui` -> onboarding opens. Confirm camera step reaches "Perfect." and that the browser-fallback warm-up completes with a grade. Record grade/latency/accuracy here.
3. Click capture (CGEventTap) -> verify `stats.clicks_seen` rises in `/v1/gaze/status` and ignored clicks increment `clicks_ignored`.
4. Eye cursor via cua `agent_cursor` reading the shm channel; hide while `stats.calibrating` (publisher writes flags=0 then).
5. Native macOS stage so the warm-up spans all displays and fixes multi-monitor layout (the main D12 win).
6. Onboarding copy: add a permissions step on macOS (Camera, Accessibility/Input Monitoring) before "Follow the light"; keep it one action per screen, skippable.
7. Then run the D12 falsifier (decisions.md) on real sessions.

### Do not
- reintroduce legacy calibration code or a startup gate (AGENTS rules 1, 7);
- move the hardware pointer implicitly (rule 3);
- merge to `main`.

## 0. Latest: onboarding + pursuit warm-up (2026-11-24, D12)

- Web onboarding (`web/static/onboarding.{js,css}`): hello -> camera (live face guide) ->
  "Follow the light" -> result (accuracy, eye-to-cursor latency, solved screen map). Opens
  once when `gaze_profile.json` is absent; skippable; Home "Set up eye tracking", Gaze
  "Follow the light".
- Warm-up: `gaze/pursuit.py` (plan, latency, joint fit, thresholds), `gaze/calibration.py`
  (session, face-loss pause), `gaze/stage.py` + `overlay_wayland.c` stage mode (dim + spiral).
- Learner: `gaze/learner.py` replaces ridge + min_clicks blend; click pairing is fixation-aware.
- Rig: `gaze/layout_solver.py` + `displays.place_displays` (fixes overlapping displays and a
  crash when a display had zero clicks).
- Proofs: `cd proofs/lean && lake build` (core Lean 4.25.1, no Mathlib).
- Verified: synthetic sims (latency within 10 ms, 0.4 deg held-out, swap detected), live
  stage launch on KDE Wayland. **Not yet done:** a full warm-up on real camera data; the
  D12 falsifier comparison on real sessions.

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
