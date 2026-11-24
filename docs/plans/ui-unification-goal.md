# Goal: one shared UI foundation, thin platform shells

> **Partially superseded (2026-11-23):** the gaze engine is now target-level
> intent (`docs/GREENFIELD.md`). The calibration step, `POST /v1/calibrate`
> launch, and `calibration.ready` startup gate have been removed. Setup is
> **Welcome → Devices → Gaze engine → Ready**, and the Gaze engine step is
> informational until the v2 model ships. The UI-contract work below remains
> valid; ignore its calibration-specific acceptance criteria.

Status: **Phases 0–3 closed** (2026-11-24, OpenSpec change
`unify-ui-contract-renderers`). The engine owns the schema-v1 contract end to
end: `GET /v1/ui/state` (four screens, six row kinds), `POST /v1/ui/action`
(start, pause, setup, allow, select_camera, set_config, models_download,
models_cancel), and `GET /v1/events` (SSE, capped at four concurrent streams,
polling fallback). The Qt shell and the SwiftUI shell are renderers: each
implements exactly the six row widgets and dispatches every interaction
through the action registry; no shell hardcodes screens, rows, copy, or
gating. Shared design tokens reach SwiftUI via generated `Tokens.swift`
(`scripts/gen_swift_tokens.py`). The one open verification is the macOS build
gate (`swift build -c release` + the fixture XCTest): no Swift toolchain or
macOS host on the Linux dev box. Earlier setup-slice notes below are
historical. Supersedes the "scaffold" framing in `packaging/macos/README.md`.
Related: [architecture.md](../architecture.md),
[platform-matrix.md](../platform-matrix.md),
[cua-driver-handoff.md](cua-driver-handoff.md), [GREENFIELD.md](../GREENFIELD.md).

## Why

The app was incomplete: the macOS shell had Home/Activity/Settings and its own
onboarding, while the Linux shell had an independent PySide6 implementation of
the same screens. The shells had drifted — most importantly, macOS onboarding
had no calibration or model step even though the engine refuses to start
without calibration. Setup order and gating are now engine-owned and identical
in both shells (`Welcome → Devices → Calibration → Ready`); the remaining work
below is to move the rest of the screens behind the full row contract.

We do **not** need two product UIs. We need **one UI model owned by the engine**
and a thin native renderer per platform.

## Goal statement

> Define a single, versioned UI contract in the Python engine that describes
> every screen, its state, and its actions. Each platform shell renders that
> contract with a small fixed set of native widgets. Platform code owns only
> what the OS genuinely controls: TCC prompts, the menu bar/tray, the overlay
> window, and process lifecycle. No screen, copy, ordering, or gating logic is
> duplicated in a shell.

Non-goals: porting Hex's Rune/GPUI stack or visual identity; building a web
app; adding features to the engine that are not exposed through the contract;
rewriting gaze/voice/routing.

## Architecture

```
                ┌────────────────────────────────────────────┐
                │ Python engine (single source of truth)      │
                │  capabilities + doctor + config + history   │
                │  /v1/ui/state  → versioned screen document  │
                │  /v1/events    → SSE live deltas            │
                │  /v1/ui/action → one generic action endpoint│
                └───────────────────┬──────────────────────────┘
                                    │ loopback HTTP (already in place)
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
  SwiftUI renderer            Qt renderer                  (later) CLI/TUI
  6 row widgets               6 row widgets                 text rows
  + mac adapters              + linux adapters             + no adapters
```

### The contract (schema v1)

`GET /v1/ui/state` returns one document:

```jsonc
{
  "schema_version": 1,
  "screens": {
    "home":     { "title": "Home", "sections": [ /* rows */ ] },
    "activity": { "title": "Activity", "sections": [ /* rows */ ] },
    "settings": { "title": "Settings", "sections": [ /* rows */ ] },
    "setup":    { "title": "Set up Keyless", "steps": ["welcome", "devices", "calibration", "ready"] }
  },
  "actions": {
    "start":      { "method": "POST", "path": "/v1/start" },
    "calibrate":  { "method": "POST", "path": "/v1/ui/action", "id": "calibrate" },
    "allow":      { "method": "POST", "path": "/v1/ui/action", "id": "allow", "arg": "permission" }
  }
}
```

A row is one of a **closed set of six kinds** — this is the whole renderer
surface a shell must implement:

| kind | fields | example |
|---|---|---|
| `status` | `title`, `state` (`ok`/`warn`/`bad`/`idle`), `detail?` | "Setup needs Accessibility." |
| `action` | `title`, `action`, `style` (`primary`/`link`), `enabled` | "Start listening" |
| `permission` | `id`, `title`, `state`, `optional`, `needs_restart` | Camera / Accessibility rows |
| `progress` | `title`, `fraction?`, `indeterminate?` | model download, calibration |
| `picker` | `title`, `options[]`, `selected` | camera / model / provider |
| `text` | `title`, `detail?` | engine status, paths (no interaction) |

Rules:

- **Capability-gated.** Rows exist only when the corresponding
  `CapabilityReport` entry is available. Missing features are omitted, never
  shown as disabled imitations (already the project's policy in
  `architecture.md` §2).
- **One primary action per surface.** Same accent, sentence case, no intro
  paragraphs — the existing design-system rule, now enforced in one place.
- **Setup is data too.** `screens.setup.steps` is an ordered list of permission
  IDs and explanatory rows. The shell drives the OS TCC interaction for a step
  through a narrow adapter; the engine owns order, gating, and progress.

### Platform-owned adapters (the only native code)

| Adapter | macOS | Linux | Why it must be native |
|---|---|---|---|
| `Permissions` | TCC probes, native prompt/Deep-link, restart | file/group checks, udev hint | OS APIs only |
| `Shell` | `MenuBarExtra`, `NSStatusItem` | StatusNotifierItem | OS APIs only |
| `Overlay` | gaze dot drawn natively | Wayland caveat (see matrix D3) | window system only |
| `Lifecycle` | spawn engine as direct child | spawn/attach daemon | TCC responsibility / single instance |

Everything else — labels, order, states, empty states, actions — is rendered
from the contract.

## Setup slice — closed 2026-11-23

These were the observed gaps in the running app; the setup slice is now fixed:

1. **Calibration is reachable.** The canonical `Calibration` step calls
   `POST /v1/calibrate`, polls `calibration.ready`, and gates Continue. Home
   shows "Calibrate to begin" from structured state, never a raw path string.
2. **Quit & Reopen is handled.** `AppDelegate` intercepts the `quit`
   AppleEvent and exits directly; our own "Restart Keyless" is the reliable
   path after a grant.
3. **Permissions are honest.** Accessibility is detected with
   `AXIsProcessTrusted() || CGPreflightPostEventAccess()`; Input Monitoring is
   deliberately never requested (Hex model). A pending grant shows a spinner,
   not a stale "Fix".
4. **Home is a real dashboard.** Hero state, a Readiness card, and an Activity
   empty state replace the old "Ready." + button stub.
5. **Model download is engine-owned.** `POST /v1/models/download` runs a
   resumable, verified background job; onboarding shows percent/MB progress,
   Stop, and Try again — like Hex, without porting its stack.
6. **Setup state is shared.** Step order/completion live in the engine's
   `OnboardingFlow` file and are served by `/v1/setup`; both shells read them.
7. **Single window.** A POSIX `flock` plus a delayed plain `open` keeps exactly
   one process/window. Settings offers revoke and System Settings review.

## Still open

- macOS build gate for the SwiftUI renderer: `swift build -c release` and the
  fixture XCTest must run on a Mac (no Swift toolchain on the Linux dev box).
- Camera preview and gaze/device status rows beyond the current status rows.
- A gaze-dot overlay (an unused `GazeOverlayPanel` stub was removed). The
  GREENFIELD live-preview loop (Phase 1, display-only) will drive it.
- Engine spawned as a direct child so TCC attribution is the app's, not a
  terminal's.
- Practice mode (GREENFIELD supersedes the old calibration wizard).

## Delivery plan

### Phase 0 — Freeze the contract
- [x] Agree schema v1 and the six row kinds (this doc).
- [x] Write `src/keyless/ui/contract.py`: typed row models + version constant.
- [x] Document shared design tokens once (accent, spacing, type scale, copy
      rules): `src/keyless/ui/theme/design-tokens.json` is the single source;
      `.agents/skills/ohrats-ui/SKILL.md` carries the copy rules.
- **Exit:** contract types + a golden JSON fixture under tests. (Met:
  `tests/fixtures/ui/contract_full.json`, `contract_cold.json`.)

### Phase 1 — Engine serves the contract
- [x] Setup slice: `GET/POST /v1/setup`, `POST /v1/calibrate`,
      `GET /v1/models`, `POST /v1/models/download|cancel`.
- [x] Add `GET /v1/ui/state` built from `CapabilityReport`, `DoctorReport`,
      `AppConfig`, `HistoryStore`, and model/catalog status.
- [x] Add `POST /v1/ui/action` with a small, validated action registry
      (`start`, `pause`, `calibrate`, `allow`, `select_camera`, `set_config`).
      (Registry today: `start`, `pause`, `setup`, `allow`, `select_camera`,
      `set_config`, `models_download`, `models_cancel`; `calibrate` is the
      engine-owned disabled notice.)
- [x] Add `GET /v1/events` (SSE) for state changes and history appends.
- [x] Add `/v1/ui/action` outcomes back into `/v1/ui/state` (progress, errors).
- **Exit:** contract tests cover every screen with all capabilities present and
  absent; no shell change needed. (Met: the two fixtures +
  `tests/unit/test_ui_contract.py`.)

### Phase 2 — SwiftUI becomes a renderer
- [x] Replace hand-coded Home/Activity/Settings with generic section/row views.
- [x] Implement the six row widgets once, natively (`Rows.swift` +
      `SectionListView`; actions post through `EngineBridge.uiAction`).
- [x] Move TCC logic behind the `Permissions` adapter; onboarding steps come
      from `screens.setup.steps`.
- [ ] Implement the gaze-dot overlay natively and wire it to engine events
      (Idle/Listening/Heard/Clicked/Permission/Error). (Superseded by the
      GREENFIELD live-preview overlay; not part of this change.)
- [ ] Spawn the engine as a direct child (`Process`) so TCC attribution is the
      app's, not a terminal's. (Still open.)
- **Exit:** adding a Home row requires only an engine contract change; verified
  by a screenshot diff test or UI test. (Qt verified:
  `tests/ui/test_ui_contract_render.py`; Swift pinned structurally by
  `tests/unit/test_ui_contract_parity.py`; XCTest + build gate need a Mac.)

### Phase 3 — Linux adopts the same contract
- [x] Qt shell renders the same document with the same six row kinds.
- [x] Delete duplicated screen/copy/gating code from the PySide6 shell.
- [x] Linux `Permissions` adapter reports camera/uinput/input-group state.
      (Capability-driven permission rows come from `CapabilityReport`; the
      tray's legacy Calibrate item and `launch_calibration` are gone.)
- **Exit:** both shells render the same fixture identically in structure.
  (Met by `tests/unit/test_ui_contract_parity.py`.)

### Phase 4 — Complete the product surfaces
- [x] Calibration is reachable from setup/Home/menu bar with a gated Continue
      and an engine-owned launch. The richer camera check → capture → review
      → save wizard is still open.
- [ ] Practice mode from the architecture's onboarding story.
- [ ] Model/provider setup rows using the existing catalog and provider layer.
- [ ] Diagnostics screen rendering `/v1/doctor`, with a "Copy report" action.
- [ ] Real empty states for Activity and Settings.
- **Exit:** a new user can go from install → permissions → calibration →
      listening without a terminal or JSON edits.

### Phase 5 — Hardening
- [ ] Ad-hoc dev signing → Developer ID + notarization, hardened runtime.
- [ ] Autostart via `SMAppService` / XDG, single instance, update channel.
- [ ] Accessibility/a11y audit of both shells; keyboard navigation.
- [ ] Update `platform-matrix.md` from "stub" to implemented per host.

## Definition of done

- One file defines what the UI contains; both shells are interchangeable
  renderers of it.
- Every row in the contract has an owner in the engine and a renderer on each
  supported platform.
- The screenshot gaps above are zero: calibration reachable, permissions
  honest, overlay visible, Activity/Settings populated.
- A contributor adding a setting touches the engine contract, not N shells.
- `keyless doctor` and the UI agree about capabilities and permissions — the
  UI is a view of engine truth, not a second source of it.
