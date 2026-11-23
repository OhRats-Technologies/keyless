# LIVE STATE — working notes for the current autonomous run

Purpose: survive short context windows. Update this file after every completed
block. It is scratch/live state; `docs/HANDOFF.md` remains the polished handoff.

## Standing instructions (from user, 2026-11-23, user is napping)

1. Finish the stage-2 probe scaffold (tests green).
2. Rename branch `greenfield/gaze-v2` -> `alt`, push to GitHub, delete the
   remote `greenfield/gaze-v2`. Only `main` and `alt` should exist on GitHub.
3. Start the app (`keyless serve`) and confirm it runs (voice-only path is OK).
4. Check the user's camera works (webcam capture).
5. Build a synthetic test: fake person + fake eyes in a 3D environment, to
   exercise the gaze pipeline without a human. Check `legacy/gaze-v1` for any
   prior synthetic-scene code. Consider making the lab a web (HTML/three.js)
   thing if that is easier.

## Decisions

- 2026-11-23 (user): cold-start rule relaxed — online updates always
  reversible, but the frozen held-out eval gate defers to Phase 4 (no
  dataset yet); correction-rate stream must keep logging.
- 2026-11-23 (user): live preview from Phase 1 — show top-k guess in real
  time during use, display-only, using whatever scorer exists (baseline
  included). Pointer actuation stays explicit-only. GREENFIELD.md §6.3 +
  Appendix C.5 updated.

## Block log

- [x] Probe scaffold tests green (`tests/unit/test_gaze_experiment.py`) — 45 gaze tests, 296 full suite
- [x] Committed on `alt` (d74f397)
- [x] Branch rename + GitHub surgery done — GitHub has only `main` + `alt`; local `alt` tracks `origin/alt`; old `greenfield/gaze-v2` deleted on remote. `.env` added to `.gitignore` (was about to be committed — it holds local secrets, not pushed).
- [x] App runs (daemon voice-only OK) — `keyless serve` on :8766, `POST /v1/start` -> state "listening" on Linux/KDE/Wayland; doctor OK. Paused after check so the camera is free.
- [x] Camera capture works — UVC webcam /dev/video1 (+video2, media0), MJPG/YUYV/H264; ffmpeg 1280x720 frame saved at /tmp/opencode/keyless-camera/frame-092037.jpg, visually verified (user's room, empty chair).
- [ ] Synthetic gaze lab: status below

## Synthetic 3D gaze lab (experiments/synth_lab/)

- `build_scene.py` — Blender scene (headless via flatpak): fake person with mobile
  pupils converging on a target button, monitor UI with 8 labeled buttons, an
  ortho `screen_cam` (the "screenshot") and a `webcam_cam` at the face.
  Exports `ground_truth.json` (button bboxes + per-frame target + gaze pixel).
  Coordinate frame: monitor at y=0 facing -Y; person at y=-0.62; screen pixels
  map linearly to the panel.
- `make_session.py` — feeds the renders through the real `CandidateGenerator`
  (RapidOCR, same backend as `keyless gaze record`), matches ground-truth gaze
  to candidates, writes a schema-v0 session, prints recall + `metric_table`.
  Exit code 1 if recall < 95% (GREENFIELD candidate gate).
- First run: recall 0% — bezel/button z-order bug (camera saw the bezel back).
  Fixed, then a second bug: the fake person occluded the panel for the
  screenshot camera. Fixes: person meshes get `hide_render` on the screen pass
  (the screenshot is a framebuffer, not a camera), full-extent button planes,
  0.70 font ratio.
- `extractors.py` — `HashTextExtractor`: deterministic text→vector toy
  extractor for the probe CLI smoke test (no real semantics on purpose).
- Status 2026-11-23: DONE end-to-end. Two sessions written
  (`--session-id`, `--gaze-jitter` for split variety):
  - ground-truth candidate recall 100% (gate ≥95% passes)
  - `keyless gaze replay` clean; `baseline_top1` = 1.0 (sanity)
  - `keyless gaze probe .tmp-synth-lab/sessions --extractor
    'extractors:HashTextExtractor'` → train 1 / test 1, probe top1 0.125 vs
    baseline_top1 1.0 → honest "falsified" verdict printed. Semantic-free
    features losing to nearest-pixel is the expected outcome; the harness,
    split, fit/predict, and verdict all work. Next: plug a real EG2 wrapper
    with the same (event, candidate) -> vector signature.

## Cross-platform note (user, 2026-11-23)

App is cross-platform by design already: `src/keyless/platform/` = `base.py`
(protocols) + `linux/` `macos/` `windows/` `cua/` `stub.py` + `router.py`
(host selection); `build_live_generator`/`build_live_screen_provider` pick
sources per OS; the gaze seam + experiment harness are platform-neutral
(extractor injected as `module:callable`). The synth lab is a dev-time tool
(Blender not a runtime dep); keep new code platform-neutral through the same seam.

## Findings

- `keyless gaze probe <sessions_dir> --extractor module:callable` works
  end-to-end; verdict line is explicit (beats / falsified vs `baseline_top1`).
- Probe semantics learned the hard way: `fit` only embeds the *clicked*
  candidate (the label), and `predict` scores each candidate against its OWN
  centroid. Fixtures must reflect that.
- Rich console wraps long CLI lines; test assertions must use short fragments.
- Platform here is Linux: the SwiftUI host (packaging/macos) cannot be built
  on this machine; "app runs" is verified via `keyless serve` daemon path.
