# AGENTS.md — Keyless

**Read this first.** This file is the operating contract for any agent or contributor working in this repo.

## What this is

Keyless is a local-first desktop control product: cameras observe the user, a model maps attention to a target, and voice/pointer actions execute it. It is not a pixel-perfect eye mouse.

## Current branch: `alt`

The greenfield work lives on `alt` (renamed from `greenfield/gaze-v2` on
2026-11-23) and is **not** on `main`. GitHub holds exactly `main` and `alt`.

- `main` / `origin/main` = `5bad72c` (Noah's last commit).
- `alt` = branch tip (single-cursor overlay + old-model deletion + Gaze v2 plan + probe harness + synthetic lab + handoff docs).
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

OpenSpec is initialized in `openspec/`: change proposals, task lists, and
verifiable requirement deltas live there. Hierarchy: `docs/GREENFIELD.md`
owns direction, theory, phases, and kill criteria; `openspec/` holds the
machine-checkable layer and must cite GREENFIELD sections, never compete
with them. Start work with the `openspec-propose` skill; never restate
GREENFIELD content in specs.

## Live state tracking

Context windows run short on this project. Keep `docs/LIVE_STATE.md` as the
running scratch log during multi-block autonomous work: standing instructions,
block checklist, findings. Update it after every completed block. `docs/HANDOFF.md`
stays the polished end-of-session handoff; LIVE_STATE feeds it.

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
- Flywheel CLIs exist and work: `keyless gaze record|replay|eval-candidates|probe|lab`. Schema v0 events, `SessionCollector`, replay + metrics, `CandidateGenerator` (AX/DOM/OCR/visual), nearest-pixel baseline, Keyless Lab Qt UI.
- Probe harness: `gaze/experiment.py` + `gaze probe` CLI (frozen-embedding head vs `baseline_top1`, session-level split, explicit beats/falsified verdict). No backbone wired yet.
- Synthetic 3D lab: `experiments/synth_lab/` — Blender fake person + monitor UI with exact ground truth feeding the real OCR pipeline; 100% candidate recall; probe verdict exercised end-to-end. Dev-time only, not a runtime dep.
- Live-preview policy (user decision 2026-11-23, GREENFIELD §6.3 + Appendix C.5): predictions displayed in real time from Phase 1 with whatever scorer exists, display-only; frozen-eval gate for online updates deferred to Phase 4; reversibility always required.
- OpenSpec initialized (`openspec/`): change proposals + verifiable requirement deltas; GREENFIELD.md stays the north star.
- Daemon: attaches to the shared-memory gaze channel if present; otherwise voice-only. No in-process model. **No real gaze source publishes to that channel yet — that is the current gap.**
- Single cursor: `src/keyless/platform/macos/pointer_mode.py` + `src/keyless/platform/cua/agent_cursor.py`.
- UI: Qt and SwiftUI calibration copy replaced with "Gaze engine v2 / voice ready"; no calibration gate.
- Tests: Python unit/UI suite passes (excluding the known ARM packaging/release failures and the pre-existing Unix-socket path test). Swift release build passes.

## Next tasks (in order)

1. **Live gaze source** — webcam → MediaPipe face/iris landmarks → gaze pixel (cold-start mapping from natural clicks, no dot grid) → publish to the shared-memory channel. This is the only missing input for live preview.
2. **Live preview loop + UI** — throttled screen capture → `CandidateGenerator` → nearest-candidate scorer → overlay highlight of top-1/top-3, display-only; every frame/click logged as a schema-v0 session (GREENFIELD §6.3 Live preview).
3. **Real sessions, real gate** — record natural sessions on this machine; run `gaze eval-candidates` (Phase 1 gate >95% recall on real clicks) and `gaze replay`/`gaze probe` against `baseline_top1`.
4. **Candidate generator recall gate** — must clear >95% on real sessions, not just synthetic.
5. **Offline target scorer** — two-tower model from `GREENFIELD.md` §6.3; EG2 wrapper implementing the `EmbeddingExtractor` protocol; baseline against nearest-candidate-to-pixel.
6. **Temporal + uncertainty** — only after the offline scorer beats the baseline.
7. **Online adaptation / DAgger** — from Phase 4, with the frozen held-out gate; reversible updates.

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

- [ ] Working tree clean; commits on `alt`, not `main`.
- [ ] Tests run; known failures documented.
- [ ] New model work has a baseline, metric, and kill criterion.
- [ ] `docs/HANDOFF.md` updated with current state and next action.
- [ ] No old-model imports or calibration gates reintroduced.
