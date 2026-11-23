# Spec Delta

## Purpose

One engine-owned, versioned UI contract: a single screen document plus a single validated action
endpoint that every platform shell consumes, so UI content, ordering, and gating exist in exactly
one place (executes `docs/plans/ui-unification-goal.md` Phases 1-2).

## ADDED Requirements

### Requirement: Contract served by the engine, not by a shell
The daemon (`keyless serve`) SHALL serve `GET /v1/ui/state` returning a schema-versioned screen
document for Home, Activity, Settings, and Setup, built from engine truth (`CapabilityReport`,
`DoctorReport`, `AppConfig`, history, model/catalog, setup state). The document SHALL be available
regardless of which shell, if any, is connected.

#### Scenario: Contract exists with no shell running
- **WHEN** the daemon is started headless and `GET /v1/ui/state` is requested
- **THEN** the response is a schema-v1 document with all four screens, not 503

#### Scenario: Document reflects capability state
- **WHEN** a capability (e.g. camera) is reported unavailable by the engine
- **THEN** rows depending on that capability are absent from the document, not present-and-disabled

### Requirement: Closed set of six row kinds
Every entry in every screen section SHALL be one of `status`, `action`, `permission`, `progress`,
`picker`, `text`, with the field shapes defined in the contract. The document SHALL declare its
`schema_version` so a shell can reject unknown versions.

#### Scenario: Unknown kind is impossible
- **WHEN** the contract builder is given data that would produce a row outside the six kinds
- **THEN** contract construction fails at build/test time rather than emitting an unknown kind

### Requirement: Single validated action endpoint
`POST /v1/ui/action` SHALL execute actions from a closed registry (`start`, `pause`, `allow`,
`select_camera`, `set_config`, model download/cancel, setup steps) and return
`{ok, action, result}`. Unknown action IDs, unknown arguments, and actions invalid for the current
engine state SHALL be rejected with a structured error, and the outcome SHALL be visible in the
next `GET /v1/ui/state` (progress, errors inline on the originating row).

#### Scenario: Invalid action rejected structurally
- **WHEN** a shell posts `{"action": "recenter_gaze"}`
- **THEN** the response is a structured error naming the unknown action and no state changes

#### Scenario: Action outcome surfaces in state
- **WHEN** a model download action is accepted and then fails
- **THEN** the next state document carries the failure detail on the corresponding row

### Requirement: Live change stream
The engine SHALL expose `GET /v1/events` (SSE) emitting state-change notifications for capability,
setup, progress, and history updates, so shells render without fixed-interval polling. Polling
`/v1/ui/state` SHALL remain a supported fallback.

#### Scenario: Grant detected without polling
- **WHEN** a permission is granted while a shell is connected to the event stream
- **THEN** the shell receives an event carrying the updated permission row within 2 s

### Requirement: Shared design tokens are machine-readable
The single palette/typography/spacing source (`design-tokens.json`) SHALL be consumable by every
platform shell, including a generated native token definition for compiled shells, so no shell
hardcodes colors, radii, or spacing.

#### Scenario: Token change propagates
- **WHEN** the accent color in `design-tokens.json` changes and shells rebuild or restart
- **THEN** every shell's rendered accent matches without editing shell source
