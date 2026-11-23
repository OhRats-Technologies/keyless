# AGENTS.md — Keyless

**Read this first.** This file is the operating contract for any agent or contributor working in this repo.

## What this is

Keyless is a local-first desktop control product: cameras observe the user, a model maps attention to a target, and voice/pointer actions execute it. It is not a pixel-perfect eye mouse.

## Current branch: `greenfield/gaze-v2`

The greenfield work lives on `greenfield/gaze-v2` and is **not** on `main`.

- `main` / `origin/main` = `5bad72c` (Noah's last commit).
- `greenfield/gaze-v2` = branch tip (single-cursor overlay + old-model deletion + Gaze v2 plan + handoff docs).
- `legacy-gaze-v1` tag / `legacy/gaze-v1` branch = `641d055` (pre-deletion audit snapshot).

Do not merge to `main` unless the user explicitly asks.

## Read in this order

1. **`docs/GREENFIELD.md`** — the approved direction, pillars, theory, proofs, architecture decision, data flywheel, phases, metrics, kill criteria, references.
2. **`docs/HANDOFF.md`** — what is done, what works, what is next, known issues.
3. **`docs/decisions.md` → D11** — the short architecture decision record.
4. **`docs/architecture.md`** — platform/UI organization; the gaze sections are superseded by `GREENFIELD.md`.
5. **`docs/file-map.md`** — where code lives and what changed.
6. **`.agents/skills/keyless-engine/SKILL.md`** — restart/probe the control plane.
7. **`.agents/skills/macos-app-rebuild/SKILL.md`** — build/install/sign the SwiftUI host.
8. **`docs/platform-matrix.md`** — per-OS capability status.
9. **`CONCERNS.md`** — historical concerns; not a plan.

## Non-negotiable rules

1. **No old model.** Do not import, copy, or paraphrase `legacy-gaze-v1` code, losses, gates, anchors, or calibration UX. The old stack was deleted for a reason.
2. **Target-level intent, not pixel calibration.** The product decision is a distribution over candidate targets. Pixel error is diagnostic only.
3. **Never move the real pointer implicitly.** The eye cursor is the cua-driver overlay. The hardware pointer moves only when the user moves it. `run_gaze_loop(..., move_enabled=False)` must stay the safe default when no overlay is available.
4. **Exactly one cursor.** The handoff in `platform/macos/pointer_mode.py` is synchronous: disable the eye overlay, then show the hardware pointer (and the reverse). Do not reintroduce a second overlay or an idle-hidden eye cursor.
5. **The mouse is a teacher.** Natural clicks and corrections are labels. `steer to mouse` is a UI mode switch, never a global calibration anchor.
6. **Data flywheel first.** No model work before `keyless.gaze.collect` + replay + metrics exist.
7. **The app must start voice-only.** If no gaze publisher exists, the daemon prints a notice and runs voice. Do not restore a calibration-file startup gate.
8. **Proofs, baselines, kill criteria.** Every model claim needs a baseline, a session-held-out metric, and a falsifier in `GREENFIELD.md` §9–10.
9. **No universal architecture claims.** "Provably best" is conditional; state the assumptions.

## Current state (2026-11-23)

- Legacy model deleted: `calibrator.py`, `geo_gaze.py`, `online_learner.py`, `cursor_gimbal.py`, click harvester, calibration overlay, gaze-dot overlay, screen cursor, recenter/steer routes, calibration paths and migrations.
- New seam: `src/keyless/gaze/` (`GazeFrame`, `GazeSource`, `SharedMemoryGazeSource`, `run_gaze_loop`).
- Daemon: attaches to the shared-memory gaze channel if present; otherwise voice-only. No in-process model.
- Single cursor: `src/keyless/platform/macos/pointer_mode.py` + `src/keyless/platform/cua/agent_cursor.py`.
- UI: Qt and SwiftUI calibration copy replaced with "Gaze engine v2 / voice ready"; no calibration gate.
- Tests: Python unit/UI suite passes (excluding the known ARM packaging/release failures and the pre-existing Unix-socket path test). Swift release build passes.

## Next tasks (in order)

1. **`keyless.gaze.collect`** — write the event schema in `GREENFIELD.md` §7.1 locally, session-keyed. No model.
2. **`keyless.gaze.replay` + metrics** — replay a session offline and compute candidate recall, target accuracy, hit rates, misclick rate, actions per correction.
3. **Candidate generator** — union of AX tree, OCR, DOM, and visual regions; measure recall against real clicks. Gate: >95%.
4. **Offline target scorer** — two-tower model from `GREENFIELD.md` §6.3; baseline against nearest-candidate-to-pixel.
5. **Temporal + uncertainty** — only after the offline scorer beats the baseline.
6. **Online adaptation / DAgger** — only after the offline evaluation is trustworthy.

## Commands

```bash
# Python tests (known unrelated failures excluded)
.venv/bin/python -m pytest tests -q \
  --ignore=tests/unit/test_packaging.py \
  --ignore=tests/unit/test_release_assets.py \
  --ignore=tests/ui/test_instance_lock.py

# Compile check
.venv/bin/python -m compileall -q src

# Control plane
lsof -ti tcp:8766 | xargs kill 2>/dev/null || true
nohup .venv/bin/keyless serve --port 8766 > /tmp/keyless-serve.log 2>&1 &
curl -s http://127.0.0.1:8766/v1/status
curl -s -X POST http://127.0.0.1:8766/v1/start

# Logs
tail -f ~/Library/Logs/Keyless/daemon.log

# SwiftUI host
cd packaging/macos && swift build -c release
# install/sign/relaunch: see .agents/skills/macos-app-rebuild/SKILL.md
```

## Where things live

| Area | Path |
|---|---|
| Gaze seam | `src/keyless/gaze/` |
| Daemon composition | `src/keyless/daemon.py` |
| cua eye overlay | `src/keyless/platform/cua/agent_cursor.py` |
| Single-cursor arbitration | `src/keyless/platform/macos/pointer_mode.py` |
| Shared-memory channel | `src/keyless/ipc/state.py`, `src/keyless/gaze/shared_memory.py` |
| Control plane | `src/keyless/ipc/control.py` |
| Voice routing | `src/keyless/router/`, `src/keyless/audio/` |
| UI detector / candidates | `src/keyless/vision/ui_detector.py` |
| SwiftUI host | `packaging/macos/` |
| Plan / theory | `docs/GREENFIELD.md` |
| Handoff | `docs/HANDOFF.md` |

## Handoff checklist

Before ending a session:

- [ ] Working tree clean; commits on `greenfield/gaze-v2`, not `main`.
- [ ] Tests run; known failures documented.
- [ ] New model work has a baseline, metric, and kill criterion.
- [ ] `docs/HANDOFF.md` updated with current state and next action.
- [ ] No old-model imports or calibration gates reintroduced.
