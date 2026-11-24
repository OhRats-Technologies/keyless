# LIVE STATE - Active Execution Plan (`alt`)

**Updated:** 2026-11-24
**Active Goal:** Ship working monocular eye tracking end-to-end inside one unified local HTTP web app (`keyless ui`).
**Superseded / Removed:** Both Qt (`src/keyless/ui/`, `pyside6`) and macOS SwiftUI (`packaging/macos/`) UIs and the separate Lab window have been deleted and replaced by `src/keyless/web/` served directly by `ControlServer`.

## Completed Milestones

- [x] `origin/main` (`n123xyz` browser agent + Split View) merged into `alt`.
- [x] Monocular gaze publisher (`src/keyless/gaze/monocular.py`): cold-start geometric prior + reversible passive online ridge regression, verified live at ~30.4 FPS on `fernix` UVC webcam + RTX 3090 EGL.
- [x] Unified local HTTP web UI (`src/keyless/web/` + `ControlServer`):
  - Single OhRats-styled web app at `http://127.0.0.1:8766` (Home, Gaze, Sessions, Activity, Settings).
  - Live monocular SSE preview (`/v1/gaze/stream`), passive click adaptation controls, session recording, visual candidate/click curation, Phase 1 >95% recall dashboard, schema-driven Settings, and copyable error callout.
  - `keyless ui` opens the local URL via `xdg-open` on Linux.
- [x] Gaze v2 flywheel (`collect`, `replay`, `metrics`, `candidates`, `candidate_generator`, `recorder`, `curate`, `experiment` / `gaze probe`) and synthetic 3D Blender lab (`experiments/synth_lab/`).

- [x] Onboarding + pursuit warm-up + gated click learner + no-overlap rig solver + Lean proofs (D12).

## Immediate Next Steps (In Order)

0. [ ] **Run onboarding on real hardware** (`keyless ui`, restart server first); record grade, latency, accuracy in HANDOFF.

1. [ ] **Record 2 to 3 real desktop sessions** using `keyless ui` (**Record session**) or `keyless gaze record --save-screenshots` while clicking normally across Firefox, terminal, and IDE.
2. [ ] **Verify Phase 1 recall gate (>95%):** inspect in **Sessions** or run `keyless gaze eval-candidates --regenerate`. If < 95%, inspect missed clicks and fix perception (AX/OCR/DOM/visual) before modeling.
3. [ ] **Measure real monocular baseline:** review `hit@25/50/100` and `baseline_top1` on the recorded sessions.
4. [ ] **Phase 2 target scorer (only after Step 2 > 95%):** implement a real `EmbeddingExtractor` / monocular target scorer and benchmark via `keyless gaze probe` against `baseline_top1` on session-held-out splits.
