# Handoff — Gaze v2 greenfield, waves 1–5

**Date:** 2026-11-23
**Branch:** `work/arch-ui-flywheel` (base `greenfield/gaze-v2` + fast-forward of `port/greenfield-product`)
**Base:** `main` / `origin/main` = `5bad72c` (Noah's last commit; do not merge without explicit approval)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055` (audit only; never a code source)

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 1. What this branch adds (five waves, all gates green)

### Wave 1 — port merge, domain decoupling, SettingSpec, IPC client
- `5f162f1`/`3091ba4`/`036cef5`: fast-forwarded `port/greenfield-product` (Parakeet Unified default, auto exposure, provisional captions, default-skipped UI/slow tests).
- `d92efea`: domain logic moved to `keyless.core.{onboarding,history,lifecycle}`; minimal re-export shims remain at the old `keyless.ui` paths; `keyless.ipc.control` has ZERO `keyless.ui` imports (AST-guarded by `tests/unit/test_core_imports.py`).
- `5eb7132` + `6a241cf`: `keyless.config.specs` — one typed `SettingSpec` per `AppConfig` field (label, group, control, options/range, live-vs-restart, platforms). `GET /v1/config/schema` serves the registry; `POST /v1/config` validates strictly from it and returns `{config, restart_required, changed}`.
- `681dd86`: `keyless.ipc.client` — typed loopback `ControlClient` + `HttpSessionController`; `run_ui()` drives the daemon through an in-process control server instead of instantiating `DaemonController` directly.
- `ff2a700`: canonical `DisplayTopology` + one normalizer in `platform/base.py`; `platform/router.py` raises `NotSupportedError` on Windows/unknown hosts instead of silently falling back to Linux.

### Wave 2 — OhRats UI system, contract renderer, visual finish gate
- `9cf3c5c`: `ui/theme/design-tokens.json` is the single source of truth; `tokens.py` loads it.
- `0a1064e`: `.agents/skills/ohrats-ui/SKILL.md` — precision-instrument aesthetic, one primary CTA per surface, capability-gated rendering, mandatory offscreen-golden visual finish gate.
- `971f50c`: `ui/contract.py` (pure, schema v1, six row kinds: status/action/permission/progress/picker/text) + `GET /v1/ui/state` + `POST /v1/ui/action`.
- `9edf30f`: Settings form generated dynamically from `/v1/config/schema`.
- `0bcf04f`: Home + Onboarding redesigned to the skill; 4 goldens regenerated and visually audited (PASS, both themes).

### Wave 3 — ML Phase 0: collect, replay, metrics (NO model)
- `a7567c0`: `gaze/collect.py` — schema v0 (§7.1) `SessionCollector`: session-keyed storage under `<data_dir>/sessions/<id>/`, bounded ring-buffered async JSONL writer, sha256 content-addressed blob store, explicit `redact_text` toggle, provenance meta.
- `f01e886`: `gaze/replay.py` — deterministic validated reader; `split_sessions` is SESSION-level only.
- `4dc9eca`: `gaze/metrics.py` — decision metrics + Baseline 0 `NearestCandidateToPixelBaseline` + §5.1 MSE-vs-target counterexample test.
- `5da9d03`: CLI `keyless gaze replay <session_dir>` prints the Baseline 0 metric table.

### Wave 4 — ML Phase 1: unified candidates + recall harness (NO model)
- `eb49a56`: `gaze/candidates.py` — canonical `Candidate` (global DisplayTopology pixels), IoU/geometry helpers, explicit `__null__` option, priority dedup (`ax`/`dom` > `ocr` > `visual`).
- `9c298a6`: `gaze/candidate_generator.py` — `CandidateGenerator` uniting injected AX/DOM/OCR/visual sources with bounded LRU screen-state cache and honest source reporting.
- `7a727f8`: CLI `keyless gaze eval-candidates <sessions_dir>` — per-app + overall geometric recall vs logged clicks, latency p50/p95 with `--regenerate`, and the >95% Phase 1 gate verdict.

### Wave 5 — House cleaning, live session recorder, and Dream-RSI local+frontier harness
- `9312e73`: removed broken leftover `src/keyless/input/click_harvest.py`, purged 28 orphaned `.pyc` files, and stripped stale `recenter` / `steer_to_mouse` intents from `src/keyless/routing/llm.py`.
- `e2f6ba1`: added opt-in `gaze_collect_enabled` and `gaze_collect_redact_text` to `AppConfig` and `SettingSpec` (`Gaze & cursor`, `apply="restart"`).
- `fc04f92`: added `src/keyless/gaze/recorder.py` (`SessionRecorder`, `ScreenSnapshot`, `LinuxClickObserver`, live generator/screen factories), wired opt-in recording into `daemon.py`, and added CLI `keyless gaze record`.
- `32813da`: added Dream-RSI style harness (`scripts/agent_dream.py`, `.agents/skills/dream-harness/SKILL.md`, `.agents/harness/policy-ledger.jsonl`) wired to lean local `pi-strata` + Strata (`Qwen3.8-Flash-Next IQ3_S`) with deterministic invariant scoring, trace digests, and monotonic policy promotion.

## 2. Verification (current)

```bash
.venv/bin/python scripts/agent_dream.py eval
# 5/5 checks passed (score 1.0):
#   no_legacy_leakage, control_no_ui_import, compileall, pytest_core, pytest_ui
```

Known exclusions remain pre-existing environment failures (`test_packaging.py`, `test_release_assets.py`, `test_instance_lock.py`), not regressions.

## 3. THE NEXT GATE (before any Phase 2 model work)

**Record real user sessions, then measure candidate recall on real clicks.**

1. Record natural desktop sessions with `keyless gaze record --save-screenshots` (or enable `gaze_collect_enabled` in Settings and run the daemon).
2. Run `keyless gaze eval-candidates <data_dir>/sessions --regenerate` on those sessions.
3. **Phase 2 (offline target scorer) starts ONLY if overall candidate recall > 95%** (GREENFIELD §9 kill criterion). Below it: fix perception (AX/OCR/DOM/visual coverage), not the model.
4. Keep session-held-out splits (`split_sessions`) for every later evaluation; never split by frame.

No neural network, gaze prediction model, or training exists on this branch. That is deliberate.

## 4. Known issues / honest gaps

- macOS/Windows AX bridges are stubs (`UnsupportedAccessibilityBridge`); the generator falls back to DOM+OCR+visual and reports `sources["ax"]=False`.
- Global mouse click observation in `keyless gaze record` is wired for Linux (`evdev` + cursor position query) and degrades honestly when `/dev/input/event*` is unreadable or on macOS/Windows.
- DOM candidates carry points, not rects; the synthesized 120x32 bbox is a documented approximation.
- `SessionCollector` meta `config_hash` raises on a corrupt `settings.json`; writer-thread I/O crash loses in-flight events uncounted.
- Select-option lists in `config/specs.py` are static snapshots of the current catalogs.
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
4. `docs/decisions.md` -> D11
5. `docs/plans/ui-unification-goal.md` (UI contract; calibration criteria superseded)
6. `.agents/skills/ohrats-ui/SKILL.md` (before any UI work)
7. `.agents/skills/dream-harness/SKILL.md` (local Pi+Strata exploration + frontier refinement)
8. `docs/architecture.md` (gaze sections superseded)
9. `docs/file-map.md`
10. `.agents/skills/keyless-engine/SKILL.md`
11. `.agents/skills/macos-app-rebuild/SKILL.md`
12. `docs/platform-matrix.md`
