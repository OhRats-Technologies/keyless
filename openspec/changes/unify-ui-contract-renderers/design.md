# Design

## Context

Engine-side contract exists but is half-wired: `contract.py::build_ui_state` is injected into the
control server by the Qt app (`ui/app.py:840-843`), so `GET /v1/ui/state` 503s under plain
`keyless serve`. Both shells consume individual routes (`/v1/status`, `/v1/setup`, `/v1/config`,
`/v1/models`) with 1 s polling. SwiftUI screens are ~1,470 hand-coded LOC; Qt product screens are
~3,000. Tokens live only in `design-tokens.json` consumed by Qt at runtime. `origin/main` adds the
browser agent (`src/keyless/browser/`) with no UI surface. See proposal.md for motivation and the
two spec deltas for required behavior.

## Goals / Non-Goals

**Goals:**
- One document defines all product screens; shells become replaceable renderers.
- The merge of `origin/main` lands first so browser-agent capabilities are contract inputs, not
  an afterthought.
- Parity is machine-checkable (shared fixture), not aspirational.

**Non-Goals:**
- No visual redesign; the ohrats-ui standard and existing goldens define the look.
- No cross-platform widget framework (Qt-on-macOS, Tauri, Flutter) — native renderer per platform.
- No contract coverage for the Lab, transcript overlay, or gaze preview widgets.

## Decisions

**D1. Merge `origin/main` into `alt` before any UI edits.**
Alternative: rebase or cherry-pick — rejected; `alt` and `main` diverged once (main touches no
`ui/` files), so a single merge keeps n123yz's history intact and conflicts stay localized to
`daemon.py`/`active_listener.py` where the same feature area is touched. Local WIP on those files
must be committed first.

**D2. Inject `ui_contract_builder` in the daemon composition root (`daemon.py`), keep the builder
pure in `keyless.ui.contract`.**
The builder has no Qt imports, so importing `keyless.ui.contract` from the daemon does not violate
the "no UI imports in the engine" rule's intent (that rule targets widget code; contract is a
data model). Alternative: move `contract.py` to `keyless/ipc/` — deferred; renaming the module can
follow once parity is proven, and the seam (`ui_contract_builder` injection point) already exists.

**D3. Grow the existing action registry rather than adding a second command channel.**
`POST /v1/ui/action` in `ipc/control.py` already validates against a registry; add the missing
IDs. Dedicated routes (`/v1/start`, `/v1/config`) stay for one release as compatibility shims for
the Swift host during conversion, then shells stop calling them.

**D4. SSE endpoint `/v1/events` with polling fallback; no WebSocket.**
SSE is trivial over the existing `ThreadingHTTPServer`, one-directional, and reconnects cheaply.
Events are coarse ("state changed", with section hints); shells refetch the affected state. This
avoids a diff-protocol spec the shells would each have to implement.

**D5. Swift tokens: generate `Tokens.swift` from `design-tokens.json` via a build step.**
A small Python script in `src/keyless/ui/theme/` emits a Swift source checked into (or generated
into) `packaging/macos/`, verified by a unit test comparing constants to JSON. Alternative: parse
JSON at Swift runtime — rejected; compile-time constants keep views token-clean and let the
compiler catch drift.

**D6. Six row widgets per shell; screen assembly is generic.**
Qt: extend `components.py` with `RowWidget` dispatching on `kind`; `app.py` screens become
`SectionView(document.screens[x])`. SwiftUI: `Rows.swift` with `AnyView`-returning switch plus
`SectionListView`. Setup steps render as the same row kinds plus a step index the document
carries, replacing bespoke onboarding layouts in both shells.

**D7. Parity test via fixture.** A golden contract JSON fixture (all capabilities present and
absent variants) drives: pytest contract tests, Qt golden PNGs (existing `tests/ui/` harness), and
a SwiftUI `XCTest` decoding the same fixture. Structural equality is asserted on the document
side; visual review stays with the goldens.

## Risks / Trade-offs

- [Qt conversion regresses working Linux screens] → Convert screen-by-screen behind the golden
  tests; keep old code until the new renderer passes all four goldens.
- [Swift conversion is large (1,470 LOC replaced)] → Sequence: tokens, then Home, then Settings,
  then Setup/onboarding last; the flow state is already engine-owned (`/v1/setup`).
- [SSE over stdlib http.server is unproven under many clients] → Cap concurrent event streams;
  polling fallback is required by the spec, so worst case degrades to today's behavior.
- [Merge of main conflicts with local WIP on daemon/audio files] → Commit WIP first; resolve in
  the merge commit before starting UI tasks.
- [Contract grows to fit one shell's convenience] → Row kinds are a closed set; any new kind
  requires a spec delta, enforced by contract tests rejecting unknown kinds.

## Migration Plan

1. Commit local WIP; merge `origin/main` into `alt`; run the test suite baseline.
2. Daemon-side contract registration + expanded action registry (both shells still work
   untouched — additive).
3. Qt renders Home from the contract; goldens verify; delete superseded Qt screen code per screen.
4. Generate Swift tokens; SwiftUI renders Home, then Settings/Activity, then Setup.
5. Add SSE; switch both pollers.
6. Remove compatibility-route usage from shells; update `docs/plans/ui-unification-goal.md`
   status and `docs/file-map.md`.

Rollback: each step is additive until step 6; reverting a shell conversion is reverting the
screen files, with the engine contract left in place.
