# Proposal

## Why

The macOS (SwiftUI, ~2,340 LOC) and Linux (PySide6, ~7,500 LOC) shells implement the same screens
twice with hand-coded copy, ordering, and gating, and have repeatedly drifted — the exact failure
`docs/plans/ui-unification-goal.md` was written to fix. The engine-side half of that plan is
already built (`src/keyless/ui/contract.py` schema-v1, `GET /v1/ui/state`, `POST /v1/ui/action`,
`design-tokens.json`), but no shell renders the contract, and the contract builder is registered
by the Qt app (`ui/app.py:840`) rather than the daemon, so it only exists when one specific shell
runs. Meanwhile `origin/main` merged n123yz's browser agent (~2,370 LOC of new engine
capabilities) that neither shell can surface without per-shell work. Unifying now means every
future engine capability — browser agent, profile sync, gaze scrolling — appears in every shell
with zero shell changes.

## What Changes

- Sync `alt` with `origin/main` (merge n123yz's Playwright Firefox browser agent, profile sync,
  gaze scrolling, extension auto-install into `alt`) before UI work begins.
- Move `ui_contract_builder` registration from the Qt app into the daemon (`keyless serve`), so
  `GET /v1/ui/state` is engine truth regardless of which shell is running.
- Extend the schema-v1 action registry (`POST /v1/ui/action`) to cover every action the shells
  currently call on dedicated routes (`start`, `pause`, `allow`, `select_camera`, `set_config`,
  model download).
- Add `GET /v1/events` (SSE) live state deltas, replacing 1 s polling in both shells.
- Generate a Swift token source from `design-tokens.json` at build time so the SwiftUI shell uses
  the same palette/spacing/type as Qt instead of system colors and inline literals.
- Convert the SwiftUI shell into a thin renderer: generic section/row views implementing the six
  row kinds (`status`, `action`, `permission`, `progress`, `picker`, `text`) fed by
  `GET /v1/ui/state`; delete hand-coded Home/Activity/Settings/Onboarding screen and gating code.
- Convert the Qt shell's product screens the same way and delete duplicated copy/gating from
  `app.py`, `onboarding/steps.py`, `settings/window.py`.
- Keep per-platform adapters as the only native/platform-owned code: permissions (TCC /
  input-groups), tray/menu bar, overlay window, process lifecycle.
- Out of scope: no web/Tauri app, no shared cross-platform widget toolkit, no changes to
  gaze/voice/routing logic, no visual redesign (the OhRats precision-instrument standard in
  `.agents/skills/ohrats-ui` stands), Keyless Lab stays a Linux dev tool outside the contract,
  no legacy calibration UI (GREENFIELD).

## Capabilities

### New Capabilities

- `ui/contract`: the engine serves one versioned screen document (six row kinds, capability-gated)
  and one validated action endpoint to any shell, independent of which shell is running.
- `ui/renderers`: each platform shell renders the contract document with a small fixed widget set
  and owns only OS-specific adapter behavior; adding an engine feature requires no shell edit.

### Modified Capabilities

(none — no specs exist yet; this change establishes both.)

## Impact

- Affected code: `src/keyless/daemon.py` (contract builder registration), `src/keyless/ipc/control.py`
  (action registry, SSE), `src/keyless/ui/contract.py`, `src/keyless/ui/app.py` +
  `ui/onboarding/` + `ui/settings/` (Qt renderer conversion), `packaging/macos/Keyless/ContentView.swift`,
  `OnboardingView.swift`, `EngineBridge.swift` (SwiftUI renderer conversion), new token codegen.
- Depends on: merge of `origin/main` (browser agent) into `alt`; browser-agent capabilities
  become contract rows once merged.
- Serves: GREENFIELD.md §6.3 (live preview surfaces predictions display-only) and the product
  readiness gates; executes Phases 1–3 of `docs/plans/ui-unification-goal.md`.
- Tests: contract fixture tests (capabilities present/absent), Qt golden tests
  (`tests/ui/test_ui.py`), a shared-fixture structural parity check between shells.
