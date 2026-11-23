# Handoff — Gaze v2 greenfield

**Date:** 2026-11-23
**Branch:** `greenfield/gaze-v2` (single active development branch)
**Base:** `main` / `origin/main` = `5bad72c` (Noah's last commit; do not merge without explicit approval)
**Audit snapshot:** tag `legacy-gaze-v1` = `641d055` (audit only; never a code source)

> Read `AGENTS.md` first, then `docs/GREENFIELD.md`, then this file.

## 1. What is on `greenfield/gaze-v2`

### Product fixes (ported from `port/greenfield-product`)
- `5f162f1` / `3091ba4` / `036cef5`: Parakeet Unified default STT, configurable camera auto-exposure, provisional captions + command confirmation fixes, default-skipped UI/slow tests.

### Architecture & UI unification (Waves 1–2)
- `d92efea`: domain logic moved to `keyless.core.{onboarding,history,lifecycle}`; `keyless.ipc.control` has ZERO `keyless.ui` imports (AST-guarded by `tests/unit/test_core_imports.py`).
- `5eb7132` + `6a241cf` + `e2f6ba1`: `keyless.config.specs` — one typed `SettingSpec` per `AppConfig` field; `GET /v1/config/schema` serves the registry; `POST /v1/config` validates strictly and returns `{config, restart_required, changed}`.
- `681dd86` + `65511b5`: `keyless.ipc.client` — typed loopback `ControlClient` (`status`, `get_config`, `config_schema`, `patch_config`, `setup`, `models`, `history`, `ui_state`, `ui_action`, `start`, `pause`) + `HttpSessionController`; `run_ui()` drives the daemon through the control plane.
- `ff2a700`: canonical `DisplayTopology` + normalizer in `platform/base.py`; `platform/router.py` raises `NotSupportedError` on Windows/unknown hosts instead of silently falling back to Linux.
- `9cf3c5c` + `0a1064e` + `971f50c` + `9edf30f` + `0bcf04f`: `design-tokens.json` single source of truth, `.agents/skills/ohrats-ui/SKILL.md`, schema-v1 `ui/contract.py` + `GET /v1/ui/state` + `POST /v1/ui/action`, schema-driven Settings window, redesigned Home/Onboarding + 4 regenerated goldens.

### Gaze v2 data flywheel, recall gate, Lab UI & Dream-RSI harness (Waves 3–5)
- `a7567c0` + `f01e886` + `4dc9eca` + `5da9d03`: `gaze/schema.py` (v0), `gaze/collect.py` (`SessionCollector` bounded async JSONL writer + sha256 blob store + explicit `redact_text`), `gaze/replay.py` (session-level `split_sessions`), `gaze/metrics.py` (decision metrics + Baseline 0 `NearestCandidateToPixelBaseline`), CLI `keyless gaze replay`.
- `eb49a56` + `9c298a6` + `7a727f8`: `gaze/candidates.py`, `gaze/candidate_generator.py` (AX/DOM/OCR/visual union with LRU cache and honest unavailable-source reporting), CLI `keyless gaze eval-candidates` (>95% Phase 1 gate).
- `9312e73` + `fc04f92` + `65511b5` + `274ab99`: removed broken `click_harvest.py`, 28 orphaned `.pyc` files, and stale `recenter`/`steer_to_mouse` LLM intents; added `gaze/recorder.py` (`SessionRecorder`, `LinuxClickObserver`, KDE Plasma Wayland capture/cursor support), opt-in `gaze_collect_enabled` daemon hook, CLI `keyless gaze record`, `scripts/gaze-flywheel.sh`, and `scripts/dream-loop.sh`.
- `32813da`: Dream-RSI harness (`scripts/agent_dream.py`, `.agents/skills/dream-harness/SKILL.md`, `.agents/harness/policy-ledger.jsonl`) wired to local `pi-strata` + Strata (`Qwen3.8-Flash-Next IQ3_S`) with 6 deterministic invariants (including AST `no_undefined_names`).
- Keyless Lab UI (`src/keyless/gaze/curate.py`, `src/keyless/ui/lab/window.py`, `keyless gaze lab` / `./scripts/gaze-flywheel.sh lab`): visual candidate/click inspector, accept/reject/retarget/delete curation, per-app Phase 1 recall dashboard, session-level split exporter, and Dream-RSI console.

## 2. Verification (current)

```bash
.venv/bin/python scripts/agent_dream.py eval
# 6/6 checks passed (score 1.0):
#   no_legacy_leakage, control_no_ui_import, no_undefined_names,
#   compileall, pytest_core, pytest_ui
```

Known exclusions remain pre-existing environment failures (`test_packaging.py`, `test_release_assets.py`, `test_instance_lock.py`), not regressions.

## 3. THE NEXT GATE (before any Phase 2 model work)

**Record real user sessions, then measure candidate recall on real clicks.**

1. Inside a shell with the `input` group active (`newgrp input`), record natural desktop sessions:
   - CLI: `./scripts/gaze-flywheel.sh record`
   - GUI: `./scripts/gaze-flywheel.sh lab` -> click **Record session**
2. Curate/inspect events in Keyless Lab if needed, then run `./scripts/gaze-flywheel.sh eval` (`keyless gaze eval-candidates --regenerate`).
3. **Phase 2 (offline target scorer) starts ONLY if overall candidate recall > 95%** (GREENFIELD §9 kill criterion). Below it: fix perception (AX/OCR/DOM/visual coverage), not the model.
4. Keep session-held-out splits (`split_sessions` / Lab **Export split**) for every later evaluation; never split by frame.

No neural network, gaze prediction model, or training exists on this branch. That is deliberate.

## 4. Known issues / honest gaps

- macOS/Windows AX bridges are stubs (`UnsupportedAccessibilityBridge`); the generator falls back to DOM+OCR+visual and reports `sources["ax"]=False`.
- Global mouse click observation in `keyless gaze record` / Lab is wired for Linux (`evdev` + KWin/Hyprland/X11 cursor query) and degrades honestly when `/dev/input/event*` is unreadable or on macOS/Windows.
- DOM candidates carry points, not rects; the synthesized 120x32 bbox is a documented approximation.
- Select-option lists in `config/specs.py` are static snapshots of the current catalogs.
- `main` is intentionally behind. Do not merge without the user's explicit approval.

## 5. Do not

- Do not create extra side branches unless explicitly asked; keep active greenfield work on `greenfield/gaze-v2`.
- Do not import or copy `legacy-gaze-v1`.
- Do not restore a calibration file, calibration grid, scalar bias, or startup gate.
- Do not reintroduce a global recenter or "steer to mouse" anchor.
- Do not show two cursors; do not move the real pointer when the overlay is unavailable.
- Do not start model work before real-session candidate recall > 95%.
- Do not merge to `main`.
