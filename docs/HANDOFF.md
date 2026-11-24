# Handoff — Gaze v2 Greenfield & Monocular Tracking (`alt`)

**Date:** 2026-11-24
**Branch:** `alt` (single active development branch; synced with `origin/main` through `70db92f`)
**Base:** `main` / `origin/main` = `70db92f` (n123xyz's browser agent + Split View; do not merge `alt` into `main` without explicit approval)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055` (audit only; never a code source)

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 1. Current Vision & Priority Shift (2026-11-24)

UI unification (`openspec/changes/unify-ui-contract-renderers`) and `origin/main` browser-agent integration are **complete on Linux** (only the macOS Swift build gate `5.5` / `7.2` remains for a Mac host). **Do not spend more cycles on UI scaffolding.**

The #1 priority now is **shipping working monocular eye tracking** on top of the Gaze v2 flywheel:

1. **Live Monocular Gaze Publisher (`src/keyless/gaze/monocular.py`) — SHIPPED:**
   - Captures one webcam cross-platform (`resolve_camera_config` + V4L2/OpenCV) and extracts 10D face/iris features via MediaPipe `FaceFeatureExtractor` (GPU EGL verified at ~30 FPS on RTX 3090).
   - **Cold-start geometric prior (frame 1):** projects iris offsets + head yaw/pitch onto `DisplayTopology` bounds so live display-only preview works immediately with zero dot-grid calibration (GREENFIELD §6.3).
   - **Reversible passive click adaptation:** pairs natural physical clicks with the freshest 10D observation; once `min_clicks` (default 5) are collected, fits regularized linear ridge regression (`(X^T X + alpha I)^-1 X^T Y`) and blends smoothly from the prior. Fully reversible via `undo_last()` / `reset()`.
   - **Seam:** publishes `(x, y, flags, timestamp_ns)` to `SharedGazeWriter` (`keyless_gaze_v1`), consumed by `SharedMemoryGazeSource` in `daemon.py`, Firefox DOM bridge, and `HomePanel`.
   - **CLI:** `keyless gaze run` (standalone publisher) and `keyless gaze record --camera` (default on; records clicks + candidates + monocular features + predicted pixel in one command). Keyless Lab's **Record session** button also attaches the live monocular stream automatically.
2. **Real-World Phase 1 Recall & Baseline Gate (NEXT ACTION):**
   - Record 2 to 3 natural desktop sessions with `keyless gaze record --save-screenshots` (or Keyless Lab **Record session**).
   - Run `keyless gaze eval-candidates --regenerate` and verify overall candidate recall **>95%** (GREENFIELD §9 kill criterion). Below 95%: fix perception (AX/OCR/DOM/visual coverage), not the model.
   - Run `keyless gaze replay <session_dir>` to record real webcam `hit@25/50/100` and `baseline_top1`.
3. **Phase 2 Offline Target Scorer (ONLY AFTER STEP 2 PASSES):**
   - Plug a real feature/embedding extractor (e.g. EG2 or lightweight monocular target scorer) into `keyless gaze probe <sessions_dir> --extractor module:callable` and verify it beats `baseline_top1` on session-held-out splits (`split_sessions`).
4. **Deferred / Parked Work:**
   - `openspec/changes/unify-ui-contract-renderers` tasks `5.5` and `7.2` require a macOS host (`cd packaging/macos && swift build -c release && xcrun swift test`). Everything verifiable on Linux is already green.

## 2. What Is Already Shipped on `alt`

- **Browser agent & voice parity (`origin/main` merged at `bc9c9bc` / `3bb36b5`):** Playwright Firefox browser agent, YouTube fast reflexes, Split View voice commands, Parakeet Unified default STT, provisional captions + live level meter.
- **Engine-owned UI contract & OhRats brand system:** `design-tokens.json` (canonical OhRats palette `#efede5`/`#1a1a1b`, `#634936` hairlines, `#c95b2b`/`#e27a3b` accent), generated `Tokens.swift`, `GET /v1/ui/state`, `POST /v1/ui/action`, `GET /v1/events` (SSE), Qt + SwiftUI contract parity tests, and `Keyless Lab` (`keyless gaze lab`).
- **Gaze v2 data flywheel & probe harness:** `gaze/schema.py` (v0), `gaze/collect.py`, `gaze/replay.py`, `gaze/metrics.py`, `gaze/candidates.py`, `gaze/candidate_generator.py`, `gaze/recorder.py`, `gaze/curate.py`, `gaze/experiment.py` (`keyless gaze probe`), `gaze/monocular.py` (`keyless gaze run`), and synthetic Blender 3D lab (`experiments/synth_lab/`).
- **Dream-RSI local + frontier harness:** `scripts/agent_dream.py` (6 deterministic invariants), `scripts/dream-loop.sh`, `scripts/gaze-flywheel.sh`, `.agents/skills/dream-harness/SKILL.md`.

## 3. Verification (current)

```bash
.venv/bin/python scripts/agent_dream.py eval
# 6/6 checks passed (score 1.0):
#   no_legacy_leakage, control_no_ui_import, no_undefined_names,
#   compileall, pytest_core, pytest_ui
```

Live hardware verification on `fernix` (CachyOS / KDE Plasma 6 Wayland, RTX 3090, Ruision UVC webcam):
```bash
.venv/bin/keyless gaze run --duration 2.5 --no-clicks
# 76 frames, 76 faces detected (~30.4 FPS on GPU EGL), published to keyless_gaze_v1
```

## 4. Do Not

- Do not create extra side branches; keep active work on `alt`.
- Do not import or copy `legacy-gaze-v1`.
- Do not restore a calibration file, dot grid, scalar bias, or startup gate.
- Do not reintroduce a global recenter or "steer to mouse" anchor.
- Do not show two cursors; do not move the real pointer when the overlay is unavailable.
- Do not start Phase 2 neural/scorer training before real-session candidate recall > 95%.
- Do not merge `alt` into `main` without explicit user approval.
