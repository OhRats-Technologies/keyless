# LIVE STATE — Active Execution Plan (`alt`)

**Updated:** 2026-11-24
**Active Goal:** Ship working monocular eye tracking end-to-end (capture -> cold-start + passive click adaptation -> shared memory -> real-session Phase 1 recall gate >95% -> Phase 2 target scorer).
**Superseded / Closed:** `unify-ui-contract-renderers` Linux work is complete (only macOS `swift build -c release` / TCC walk-through on a Mac remains). Do not re-open UI contract refactors.

## Completed Milestones

- [x] `origin/main` (`n123xyz` browser agent + Split View) merged into `alt`.
- [x] Engine-owned UI contract + SSE (`/v1/events`) + Qt/SwiftUI contract parity tests green.
- [x] Canonical OhRats design tokens + `Keyless Lab` curator + inline copyable error callout.
- [x] Gaze v2 flywheel (`collect`, `replay`, `metrics`, `candidates`, `candidate_generator`, `recorder`, `curate`, `experiment` / `gaze probe`).
- [x] Synthetic 3D Blender gaze lab (`experiments/synth_lab/`) verified at 100% recall.
- [x] **Monocular gaze publisher (`src/keyless/gaze/monocular.py`)**:
  - Cold-start geometric prior (frame 1) + reversible passive online ridge regression (5+ natural clicks, no dot grid).
  - CLI `keyless gaze run` verified live on `fernix` UVC webcam (76/76 faces at ~30.4 FPS on RTX 3090 EGL, publishing to `keyless_gaze_v1`).
  - Wired into `keyless gaze record --camera` (default on) and Keyless Lab **Record session** so real clicks automatically pair with 10D monocular features and predicted gaze pixels.

## Immediate Next Steps (In Order)

1. [ ] **Record 2 to 3 real desktop sessions** on `fernix` with `keyless gaze record --save-screenshots` (or Keyless Lab **Record session**) while clicking normally across Firefox, terminal, and IDE.
2. [ ] **Run Phase 1 recall gate:** `keyless gaze eval-candidates --regenerate`. Verify overall candidate recall > 95%. If < 95%, inspect missed clicks in Keyless Lab and fix perception (AX/OCR/DOM/visual) first.
3. [ ] **Measure real monocular baseline:** Run `keyless gaze replay <session_dir>` on the recorded sessions to log real `hit@25/50/100` and `baseline_top1`.
4. [ ] **Phase 2 scorer (only after Step 2 > 95%):** Implement a real `EmbeddingExtractor` / monocular target scorer and benchmark via `keyless gaze probe` against `baseline_top1` on session-held-out splits.
5. [ ] **macOS verification (on a Mac):** Run `cd packaging/macos && swift build -c release && xcrun swift test` to close OpenSpec tasks `5.5` and `7.2`.
