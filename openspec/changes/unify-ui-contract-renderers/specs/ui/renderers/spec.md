# Spec Delta

## Purpose

Each platform shell (SwiftUI on macOS, PySide6 on Linux, future Windows host) is a thin renderer
of the engine contract plus a narrow set of OS-owned adapters, so screens, copy, ordering, and
gating are never duplicated across shells.

## ADDED Requirements

### Requirement: Shells render the contract, not their own screens
Each product shell SHALL build its Home, Activity, Settings, and Setup screens solely from
`GET /v1/ui/state`, implementing each of the six row kinds once as a native widget. Shells SHALL
contain no screen-specific copy, step ordering, capability gating, or empty-state logic.

#### Scenario: New row needs no shell change
- **WHEN** the engine adds a row to the Home section and the shell is rebuilt with no code change
- **THEN** the row appears in every shell's Home screen rendered by the shared row widget

#### Scenario: Structural parity across shells
- **WHEN** two shells render the same contract fixture document
- **THEN** both present the same screens, sections, rows, labels, and actions in the same order

### Requirement: Engine actions invoked generically
Shell buttons and pickers SHALL invoke the action referenced by the row via `POST /v1/ui/action`;
shells SHALL NOT call screen-specific engine routes for these interactions.

#### Scenario: Start button
- **WHEN** the user activates an `action` row whose action is `start`
- **THEN** the shell posts `{action: "start"}` to the action endpoint and re-renders from the
  updated state document

### Requirement: Platform adapters own only OS behavior
Native shell code SHALL be limited to: permission interaction (macOS TCC prompts and probes;
Linux camera/input-group guidance), tray/menu-bar presence, overlay windows, and process
lifecycle/single-instance. Adapters MAY trigger OS UIs and deeplinks but SHALL NOT decide whether
a feature is available — availability comes from the contract.

#### Scenario: Permission row drives native grant
- **WHEN** the contract contains a `permission` row in state `missing` and the user activates it
- **THEN** the macOS adapter opens the TCC interaction for that permission id, and the row's state
  updates only when the engine re-detects it

### Requirement: Voice-only startup preserved
A shell SHALL start and render the contract even when no gaze source or model is installed; the
document omits unavailable features rather than blocking startup (AGENTS.md rule 7; no
calibration-file gate).

#### Scenario: Headless engine, cold machine
- **WHEN** the daemon starts on a machine with no camera and no models and a shell connects
- **THEN** the shell renders Home/Settings from the document with camera and model rows absent,
  and voice actions remain startable

### Requirement: Dev tooling stays outside the contract
The Keyless Lab and overlay preview widgets SHALL remain platform-local developer surfaces and
SHALL NOT be added to or rendered by the product contract.

#### Scenario: Lab untouched by contract changes
- **WHEN** the contract schema evolves
- **THEN** Lab behavior and its direct engine imports continue unaffected
