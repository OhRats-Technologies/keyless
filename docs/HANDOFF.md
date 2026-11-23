# Handoff — Gaze v2 greenfield

**Date:** 2026-11-23
**Branch:** `greenfield/gaze-v2` (tip)
**Base:** `main` / `origin/main` = `5bad72c` (Noah's last commit)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055`

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 1. What happened

The legacy gaze stack was removed from this branch and replaced with a model-agnostic seam.

**Deleted:**

- `vision/calibrator.py`, `vision/geo_gaze.py`, `vision/online_learner.py`, `vision/cursor_gimbal.py`
- macOS click harvester, scalar bias, `recenter_to`, `recenter gaze`, `steer to mouse` calibration route
- full-screen calibration overlay, Qt calibration page, gaze-dot overlay, screen-cursor launcher
- calibration paths, migrations, startup gate, `/v1/calibrate` model launch
- old tests: `test_phase1.py`, `test_phase3.py`, `test_gaze_core.py`, `test_geo_gaze.py`, `test_mouse_events.py`, `test_calibration_overlay.py`, `test_gaze_view.py`

**Added:**

- `src/keyless/gaze/` — `GazeFrame`, `GazeSource`, `SharedMemoryGazeSource`, `run_gaze_loop`
- `src/keyless/platform/macos/pointer_mode.py` — single-cursor arbitration
- `docs/GREENFIELD.md` — plan, pillars, theory, proofs, procedures, references
- ADR D11 in `docs/decisions.md`
- `AGENTS.md` + this handoff

**Kept:**

- camera capture + MediaPipe landmarks
- cua agent-cursor overlay and synchronous visibility handoff
- voice/STT/LLM routing, Firefox DOM bridge, UI/AX/OCR candidate sources
- SwiftUI host and control plane

## 2. What works now

- The app starts with **no calibration file**.
- The engine logs: `No gaze model is publishing frames yet; running voice-only until one starts.`
- Voice commands work; `click` falls back to the pointer when there is no gaze target.
- If a model process publishes `(x, y, flags)` on the `keyless_gaze_v1` shared-memory channel, the daemon attaches and moves the cua overlay.
- If the overlay is unavailable, `run_gaze_loop(..., move_enabled=False)` leaves the real pointer untouched.
- Single-cursor handoff: eye overlay while the mouse rests, hardware pointer on movement/click, never both.
- Python tests: `115 passed, 2 skipped` with the known exclusions (packaging ARM, release assets ARM, Unix-socket path length).
- Swift release build passes; the app is installed and signed at `~/Applications/Keyless.app`.

## 3. What does not exist yet

- No gaze model. No publisher writes the shared-memory channel outside tests.
- No event log, replay, candidate generator, target scorer, uncertainty, or online learner.
- The macOS accessibility bridge is a stub: `UnsupportedAccessibilityBridge` has no `match_and_execute`. Voice target grounding on macOS is incomplete.
- The SwiftUI onboarding "Gaze engine v2" page is informational; `/v1/calibrate` returns a disabled notice.
- The Qt calibration UI is gone; `/v1/setup` reports `calibration.ready = true` and `legacy_removed = true` so the app is not gated.

## 4. Next steps

### Step 1 — `keyless.gaze.collect` (first code)

Write the schema from `GREENFIELD.md` §7.1 to local, session-keyed storage:

- frame observations (landmarks, head pose, eye crops, features)
- screen candidates (bbox, role, text, visual hash, app/window)
- predictions (top target, probability, entropy, pixel)
- interactions (mouse, click, voice, correction, latency)

Acceptance:

- A real session produces a replayable file.
- No model code yet.
- The writer is bounded (no unbounded RAM) and redacts nothing silently.

### Step 2 — `keyless.gaze.replay` + metrics

- Replay a session offline.
- Compute candidate recall, top-1/3 target accuracy, hit@25/50/100 px, misclick rate, actions per correction.
- Session-held-out split, never frame-held-out.

Acceptance:

- `replay` reproduces a session and prints a metric table.
- A baseline "nearest candidate to the pixel" exists.

### Step 3 — Candidate generator

- Union of AX tree, OCR, DOM, and visual regions.
- Deduplicate, score, and cache per screen change.
- Measure recall against real clicks.

Acceptance:

- >95% candidate recall. If not, stop model work and fix perception.

### Step 4 — Offline target scorer

- Two-tower model from `GREENFIELD.md` §6.3.
- Listwise cross-entropy/InfoNCE over candidates + null.
- Baselines: nearest-candidate-to-pixel, geometry logistic regression, GBDT.

Acceptance:

- Beats the pixel baseline on held-out sessions by a clear margin.

### Step 5 — Temporal + uncertainty

- Causal TCN/GRU over 0.5–1.5 s.
- Heteroscedastic pixel head, temperature scaling, 3-seed ensemble.
- Split conformal target sets; act/confirm/abstain.

Acceptance:

- >90% hit@50 px, <1 misclick per 10 min, conformal coverage within 2% nominal.

### Step 6 — Online adaptation / DAgger

- Last-layer adapter with replay; conservative updates.
- Uncertainty-triggered labels; user corrections as hard negatives.

Acceptance:

- Actions per correction improves over two consecutive weeks.

## 5. Known issues

- `UnsupportedAccessibilityBridge.match_and_execute` is missing on macOS. Do not confuse this with the gaze model; it is a grounding gap.
- The shared-memory schema carries only `(x, y, flags, timestamp)`; features are not published yet. The collector will define its own richer schema.
- The `tests/ui/test_instance_lock.py` failure is an environment path-length issue, not a regression.
- Packaging/release tests fail on ARM; unrelated to gaze.
- `main` is intentionally behind this branch. Do not merge without the user's explicit approval.

## 6. How to run

```bash
# from the repo root, on greenfield/gaze-v2
.venv/bin/python -m pytest tests -q \
  --ignore=tests/unit/test_packaging.py \
  --ignore=tests/unit/test_release_assets.py \
  --ignore=tests/ui/test_instance_lock.py

# control plane
lsof -ti tcp:8766 | xargs kill 2>/dev/null || true
nohup .venv/bin/keyless serve --port 8766 > /tmp/keyless-serve.log 2>&1 &
curl -s http://127.0.0.1:8766/v1/status
curl -s -X POST http://127.0.0.1:8766/v1/start
tail -f ~/Library/Logs/Keyless/daemon.log

# SwiftUI host
cd packaging/macos && swift build -c release
# install/sign/relaunch: .agents/skills/macos-app-rebuild/SKILL.md
```

## 7. Do not

- Do not import or copy `legacy-gaze-v1`.
- Do not restore a calibration file, calibration grid, or startup gate.
- Do not reintroduce a global recenter or a scalar bias anchor.
- Do not show two cursors.
- Do not move the real pointer when the overlay is unavailable.
- Do not start model work before the event log and candidate recall exist.
- Do not merge to `main`.

## 8. Reading order

1. `AGENTS.md`
2. `docs/GREENFIELD.md`
3. this file
4. `docs/decisions.md` → D11
5. `docs/architecture.md` (banner; gaze sections superseded)
6. `docs/file-map.md`
7. `.agents/skills/keyless-engine/SKILL.md`
8. `.agents/skills/macos-app-rebuild/SKILL.md`
9. `docs/platform-matrix.md`
