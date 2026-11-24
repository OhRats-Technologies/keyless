# Tasks

## 1. Sync with origin/main first
- [x] 1.1 Commit or stash local WIP (`daemon.py`, `active_listener.py`, `ipc/transcript.py`, `transcript_overlay.py`) on `alt`.
- [x] 1.2 Merge `origin/main` into `alt` (n123yz browser agent); resolve conflicts in `daemon.py`/`active_listener.py`.
- [x] 1.3 Run the baseline suite per AGENTS.md (pytest minus known ARM/packaging and instance-lock failures; `compileall -q src`).

## 2. Engine owns the contract
- [x] 2.1 Register `ui_contract_builder = build_ui_state` in the daemon composition root (`src/keyless/daemon.py`); drop the Qt-app registration.
- [x] 2.2 Verify `GET /v1/ui/state` responds under headless `keyless serve` (no shell running).
- [x] 2.3 Expand the `POST /v1/ui/action` registry: `start`, `pause`, `allow`, `select_camera`, `set_config`, model download/cancel; structured errors for unknown/invalid actions; outcomes reflected in the next state document.
- [x] 2.4 Add contract fixture tests: golden JSON for all-capabilities and no-capabilities variants; assert closed row-kind set and capability-gated omission.
- [x] 2.5 Add `GET /v1/events` (SSE): state-change notifications for capability/setup/progress/history; bounded concurrent streams; keep polling fallback.

## 3. Shared tokens reach every shell
- [x] 3.1 Add a codegen script emitting `Tokens.swift` from `design-tokens.json` into `packaging/macos/`.
- [x] 3.2 Add a unit test asserting the generated Swift constants match the JSON.
- [x] 3.3 Replace SwiftUI system-color/inline-literal styling with generated tokens.

## 4. Qt becomes a contract renderer
- [x] 4.1 Implement a generic row dispatcher in `src/keyless/ui/components.py` for the six row kinds.
- [x] 4.2 Render Home from `GET /v1/ui/state` via the generic section/row views; keep the old screen until goldens pass.
- [x] 4.3 Render Settings and Activity from the contract; delete duplicated gating/copy in `app.py`, `settings/window.py`, `onboarding/steps.py`.
- [x] 4.4 Render Setup steps from the contract document (flow state already engine-owned).
- [x] 4.5 Regenerate and review the four `tests/ui/goldens/` PNGs per the ohrats-ui visual finish gate.

## 5. SwiftUI becomes a contract renderer
- [ ] 5.1 Add Swift models decoding the schema-v1 document; XCTest against the shared golden fixture.
- [ ] 5.2 Implement the six row widgets natively (`Rows.swift`) plus a generic `SectionListView`.
- [ ] 5.3 Replace `HomeView` with contract rendering; then `SettingsView`, `ActivityView`, `MenuBarView`.
- [ ] 5.4 Replace `OnboardingView`'s hand-coded steps with contract-driven setup rendering; keep `PermissionGuide` as the TCC adapter only.
- [ ] 5.5 Delete superseded hand-coded screen code from `ContentView.swift` / `OnboardingView.swift`; `swift build -c release` passes.

## 6. Live events and cleanup
- [x] 6.1 Switch both shells from 1 s polling to `/v1/events` with polling fallback.
- [x] 6.2 Remove shell calls to compatibility routes for UI interactions (keep engine routes for CLI/automation).
- [x] 6.3 Structural parity test: both shells render the same fixture with identical screens/rows/order.

## 7. Verification (end-to-end, not only unit tests)
- [x] 7.1 Headless daemon + Qt shell on Linux: cold machine (no camera/models) renders Home/Settings with unavailable rows absent; start voice-only via the action endpoint.
- [ ] 7.2 macOS: build/sign/relaunch per the macos-app-rebuild skill; walk install → permissions (TCC adapter) → Ready without a terminal; grant a permission and see the row flip via SSE. (Needs the user's Mac: no Swift toolchain or linked macOS environment on this Linux host.)
- [x] 7.3 Add one row to the engine contract only; confirm it appears in both shells with zero shell edits, then remove it.
- [x] 7.4 Confirm no legacy calibration/gaze vocabulary in either shell (GREENFIELD; ohrats-ui copy rules).

## 8. Docs
- [ ] 8.1 Update `docs/plans/ui-unification-goal.md` status (Phases 1–3 closed) and `docs/HANDOFF.md`.
- [ ] 8.2 Update `docs/file-map.md` and `docs/architecture.md` §7 for the renderer layout; note the Lab stays outside the contract.
