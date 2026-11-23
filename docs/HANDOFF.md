# Handoff — Gaze v2 greenfield, waves 1–4

**Date:** 2026-11-23
**Branch:** `work/arch-ui-flywheel` (base `greenfield/gaze-v2` + fast-forward of `port/greenfield-product`)
**Base:** `main` / `origin/main` = `5bad72c` (Noah's last commit; do not merge without explicit approval)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055` (audit only; never a code source)

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 1. What this branch adds (four waves, all gates green)

### Wave 1 — port merge, domain decoupling, SettingSpec, IPC client
- `5f162f1`/`3091ba4`/`036cef5`: fast-forwarded `port/greenfield-product` (Parakeet Unified default, auto exposure, provisional captions, default-skipped UI/slow tests).
- `d92efea`: domain logic moved to `keyless.core.{onboarding,history,lifecycle}`; minimal re-export shims remain at the old `keyless.ui` paths; `keyless.ipc.control` has ZERO `keyless.ui` imports (AST-guarded by `tests/unit/test_core_imports.py`).
- `5eb7132` + `6a241cf`: `keyless.config.specs` — one typed `SettingSpec` per `AppConfig` field (label, group, control, options/range, live-vs-restart, platforms). `GET /v1/config/schema` serves the registry; `POST /v1/config` validates strictly from it (unknown keys, wrong types, non-finite numbers, bad options → 400, nothing saved) and returns `{config, restart_required, changed}`.
- `681dd86`: `keyless.ipc.client` — typed loopback `ControlClient` + `HttpSessionController`; `run_ui()` drives the daemon through an in-process control server instead of instantiating `DaemonController` directly. Injected controllers (tests) unchanged.
- `ff2a700`: canonical `DisplayTopology` + one normalizer in `platform/base.py`; `platform/router.py` raises `NotSupportedError` on Windows/unknown hosts instead of silently falling back to Linux.

### Wave 2 — OhRats UI system, contract renderer, visual finish gate
- `9cf3c5c`: `ui/theme/design-tokens.json` is the single source of truth; `tokens.py` loads it (public API unchanged).
- `0a1064e`: `.agents/skills/ohrats-ui/SKILL.md` — precision-instrument aesthetic, one primary CTA per surface, capability-gated rendering, mandatory offscreen-golden visual finish gate.
- `971f50c`: `ui/contract.py` (pure, schema v1, six row kinds: status/action/permission/progress/picker/text) + `GET /v1/ui/state` (builder injected by `keyless serve`/`run_ui`; 503 when unregistered) + `POST /v1/ui/action` (engine-side registry: start, pause, set_config, setup, select_camera, calibrate-notice, diagnostics).
- `9edf30f`: Settings form generated from the config schema (grouped sections, live/restart indicators, inline validation); special capability panels (mic, speech test, model download, TTS, diagnostics) preserved.
- `0bcf04f`: Home + Onboarding redesigned to the skill; dead "steer to mouse"/"recenter gaze" copy and the "Align gaze" disabled-imitation CTA removed; 4 goldens regenerated and visually audited (PASS, both themes).

### Wave 3 — ML Phase 0: collect, replay, metrics (NO model)
- `a7567c0`: `gaze/collect.py` — schema v0 (§7.1) `SessionCollector`: session-keyed storage under `<data_dir>/sessions/<id>/`, bounded ring-buffered async JSONL writer (drop counter, never unbounded RAM), sha256 content-addressed blob store, explicit `redact_text` toggle (default False, never silent), provenance meta (model_version, config_hash, git commit).
- `f01e886`: `gaze/replay.py` — deterministic validated reader; `split_sessions` is SESSION-level only (seeded, disjoint, full coverage).
- `4dc9eca`: `gaze/metrics.py` — candidate_recall@k, top1, top3, hit@25/50/100, misclicks/h, abstention, actions/correction; Baseline 0 `NearestCandidateToPixelBaseline`; §5.1 MSE-vs-target counterexample encoded as a test.
- `5da9d03`: CLI `keyless gaze replay <session_dir>` prints the Baseline 0 metric table.

### Wave 4 — ML Phase 1: unified candidates + recall harness (NO model)
- `eb49a56`: `gaze/candidates.py` — canonical `Candidate` (bbox in global DisplayTopology pixels, source ax/dom/ocr/visual, confidence, visual_hash), IoU/offset/scale/to-global geometry, explicit `__null__` free-space option, deterministic priority dedup (ax/dom > ocr > visual).
- `9c298a6`: `gaze/candidate_generator.py` — `CandidateGenerator` unions injected AX/DOM/OCR/visual sources; unsupported bridges reported unavailable and skipped cleanly; DOM points get a documented synthesized bbox; bounded LRU cache keyed by (display_id, window_id, screenshot_hash, image-present); null appended on return.
- `7a727f8`: CLI `keyless gaze eval-candidates <sessions_dir>` — per-app + overall geometric recall vs logged clicks, target-id recall, `--regenerate` OCR latency p50/p95, and the >95% Phase 1 gate verdict.

## 2. Verification (current)

```bash
.venv/bin/python -m compileall -q src                     # exit 0
.venv/bin/python -m pytest tests -q \
  --ignore=tests/unit/test_packaging.py \
  --ignore=tests/unit/test_release_assets.py \
  --ignore=tests/ui/test_instance_lock.py
# 267 passed, 1 skipped, 17 deselected
.venv/bin/python -m pytest tests/ui/test_ui.py -o addopts="" -q
# 17 passed (includes the 4 regenerated goldens)
```

Known exclusions are pre-existing environment failures (ARM packaging/release, Unix-socket path length), not regressions.

## 3. THE NEXT GATE (before any Phase 2 model work)

**Record real user sessions, then measure candidate recall on real clicks.**

1. Wire `SessionCollector` into the daemon/interaction path and record natural sessions (clicks are the labels; §7.2 rules).
2. Run `keyless gaze eval-candidates <data_dir>/sessions` on those sessions.
3. **Phase 2 (offline target scorer) starts ONLY if overall candidate recall > 95%** (GREENFIELD §9 kill criterion). Below it: fix perception (AX/OCR/DOM/visual coverage), not the model.
4. Keep session-held-out splits (`split_sessions`) for every later evaluation; never split by frame.

No neural network, gaze prediction model, or training exists on this branch. That is deliberate.

## 4. Known issues / honest gaps

- macOS/Windows AX bridges are stubs (`UnsupportedAccessibilityBridge`); the generator falls back to DOM+OCR+visual and reports `sources["ax"]=False`.
- DOM candidates carry points, not rects; the synthesized 120×32 bbox is a documented approximation.
- `SessionCollector` meta `config_hash` raises on a corrupt `settings.json` (deployment error); writer-thread I/O crash loses in-flight events uncounted (exceptional path, documented in code).
- Select-option lists in `config/specs.py` are static snapshots of the current catalogs; new presets/assets require a spec update.
- `src/keyless/routing/llm.py` still advertises a `recenter` command to the LLM although the route was deleted — stale prompt copy, out of scope for these waves; fix before shipping voice routing.
- The client-backed Settings path is not exercised through a live-HTTP Qt test (engine client tests cover the transport).
- `main` is intentionally behind. Do not merge without the user's explicit approval.

## 5. Do not

- Do not import or copy `legacy-gaze-v1`.
- Do not restore a calibration file, calibration grid, scalar bias, or startup gate.
- Do not reintroduce a global recenter or "steer to mouse" anchor.
- Do not show two cursors; do not move the real pointer when the overlay is unavailable.
- Do not start model work before real-session candidate recall > 95%.
- Do not merge to `main`.

## 6. Reading order

1. `AGENTS.md`
2. `docs/GREENFIELD.md`
3. this file
4. `docs/decisions.md` → D11
5. `docs/plans/ui-unification-goal.md` (UI contract; calibration criteria superseded)
6. `.agents/skills/ohrats-ui/SKILL.md` (before any UI work)
7. `docs/architecture.md` (gaze sections superseded)
8. `docs/file-map.md`
9. `.agents/skills/keyless-engine/SKILL.md`
10. `.agents/skills/macos-app-rebuild/SKILL.md`
11. `docs/platform-matrix.md`