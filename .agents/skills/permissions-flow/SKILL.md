---
name: permissions-flow
description: >-
  Verify, troubleshoot, and reset macOS TCC permissions for Keyless
  (Accessibility, Screen Recording, Camera, Microphone). Use when onboarding
  or permission grants look stuck, toggles don't register, "already enabled it
  but Keyless doesn't see it", switching panes steals focus, restarting after a
  grant, or resetting/revoking permissions: "check permissions", "permission
  flow", "tccutil reset", "restart to apply grant".
---

# macOS permissions / TCC flow

The SwiftUI host implements a Hex-style permission UX in
`packaging/macos/Keyless/OnboardingView.swift` and
`PermissionGuide.swift`. Understand it before changing anything — several TCC
quirks make naive implementations look broken.

**Onboarding is an immersive full-window surface** with the canonical,
engine-owned steps **Welcome → Devices → Calibration → Ready** — the same
order as the Linux shell and `keyless.ui.onboarding.flow`. It is not a sheet,
and there is no floating overlay panel. Rows use
`PermissionGuideController.allowInline(_:)`: native prompt where one exists,
otherwise the exact Settings deep-link. An in-window toast confirms each grant
and a "waiting for the switch" spinner shows while a grant is pending. The
Devices step also shows the offline speech model download, an auto-detection
checklist (engine, cua-driver, agents), and requires the model before
continuing. The Calibration step launches the overlay through
`POST /v1/calibrate` and gates Continue on `GET /v1/setup`'s `calibration.ready`.

## The permissions and how each is checked

Defined by `KeylessPermission` in `PermissionGuide.swift`:

| Permission | Pane | Check (`currentGrant`) | Native prompt? |
| --- | --- | --- | --- |
| Camera | `Privacy_Camera` | `AVCaptureDevice.authorizationStatus(for: .video)` | Yes (`requestAccess`) |
| Microphone | `Privacy_Microphone` | `authorizationStatus(for: .audio)` | Yes |
| Accessibility | `Privacy_Accessibility` | `AXIsProcessTrusted() || CGPreflightPostEventAccess()` | Yes (`kAXTrustedCheckOptionPrompt`) |
| Screen Recording | `Privacy_ScreenCapture` | `CGPreflightScreenCaptureAccess()` | No public prompt |
| Input Monitoring | `Privacy_ListenEvent` | `CGPreflightListenEventAccess()` (legacy only) | Not requested |

**We do not ask for Input Monitoring at all** — Hex's model: Accessibility
grants both post and listen access for the event taps our hotkeys need. The
old throwaway `CGEvent.tapCreate` probe was wrong: `.defaultTap` (options=0) is
an active filter that checks Accessibility, so it stayed false when Input
Monitoring was granted. `CGPreflightListenEventAccess()` is the correct
Input Monitoring query, but it isn't used in the flow now. Accessibility is
detected with `AXIsProcessTrusted() || CGPreflightPostEventAccess()` (Hex's
pattern). `KeylessSetup.required` is `[.camera, .microphone, .accessibility]`;
`optional` is `[.screenRecording]`.

If grants seem enabled in System Settings but read as denied in the app, check
for **stale TCC entries from SwiftUI drag-copies** — each drag of the app tile
created a duplicate grant (`tccutil reset ListenEvent/… party.ohrats.Keyless`
showed 10 stale entries). Clear with `tccutil reset <service> party.ohrats.Keyless`
and re-grant from the real app.

## Strict sequential flow

`OnboardingFlow.setUpAll()` walks exactly one permission at a time in this order:

```
camera → microphone → accessibility → screenRecording
```

It filters out any whose grant is already `.granted`. Rationale: parallel
Settings panes steal focus from each other — never open more than one.

Each step calls `PermissionGuideController.allowInline(kind)`: native prompt for
Camera/Microphone/Accessibility, or a deep-link to the exact Settings pane for
Screen Recording. Nothing floats over System Settings. Grant detected by the
1 s poll → auto-advance after 0.7 s. `Next`/`Stop` are available during setup,
plus `Restart Keyless` when a restart hint is shown.

The onboarding rows show a "Waiting for the switch" spinner while a grant is
pending and an in-window toast once it is requested. The Devices step then
shows an auto-detection checklist from `/v1/doctor`.

## Restart to apply grants

macOS TCC caches grants per running process. A toggle flipped in System Settings
is invisible to the already-running Keyless until relaunch. The UI states this
explicitly via `restartHint`:

- Accessibility → "Already enabled it? Restart Keyless to apply the grant."
- Screen Recording → "Switch on Keyless, then restart the app to apply."

`Restart Keyless` calls `PermissionGuideController.relaunch()` — detached
`sleep 1; open <bundle>` then `UserDefaults.synchronize()` + POSIX `exit(0)`.
Never use `NSApp.terminate()` here: AppKit vetoes termination while onboarding
is on screen ("knocking" sound, app stays open). See **macos-app-rebuild**.

The same veto applies to macOS's **own "Quit & Reopen"** button — it sends a
`quit` AppleEvent while onboarding is up, AppKit routes it to the cancel path,
and the app never actually quits/relaunches. `AppDelegate` intercepts
`kAEQuitApplication` via `NSAppleEventManager` before AppKit sees it and calls
`exit(0)` directly, so the OS can relaunch with the fresh grant.

**Critical: stable code identity.** Grants are tied to the app's designated
requirement. The installed bundle MUST be signed with the shared Developer ID
via `packaging/macos/sign-local.sh` (see **macos-app-rebuild**). An ad-hoc
signature is cdhash-based and changes on every rebuild, which silently wipes
Accessibility / Input Monitoring / Screen Recording grants — this is why a
grant can look enabled in System Settings yet read as denied in the app. After
switching identity, re-grant each permission once; subsequent Developer ID
rebuilds preserve them.

## Skip / persistence / resume

**Step order and completion are engine-owned** (`keyless.ui.onboarding.flow`,
persisted at `paths.onboarding_path`, served by `GET/POST /v1/setup`). The
SwiftUI `OnboardingFlow` reads `engine.setup.state.step` and calls
`setupAction("go_to"/"advance"/"complete")`; it never keeps its own step copy.
The local `UserDefaults.standard` keys are only the resumable permission-tour
queue and an offline cache of completion:

- `keyless.onboarding.queue` — pending permission raw values
- `keyless.onboarding.index` — current index
- `keyless.onboarding.settingUp` — whether a run is active
- `keyless.onboarding.complete` — set true by `finish()` / "Skip for now"

On init the queue resumes after a close/reopen. `KeylessApp.init()` strips any
persisted key containing `SheetPresentationModifier` so a dead sheet doesn't
resurrect onboarding.

Verify state:

```bash
curl -s http://127.0.0.1:8766/v1/setup
defaults read party.ohrats.Keyless keyless.onboarding.queue
defaults read party.ohrats.Keyless keyless.onboarding.index
```

## Revoke / reset

`revokeAllPermissions()` (Settings → "Reset all permissions…") runs
`tccutil reset` for `Accessibility`, `ScreenCapture`, `Microphone`, `Camera`,
and `ListenEvent` against the bundle id, then relaunches. Treat this as
destructive: only run on explicit user request. "Review in System Settings"
deep-links the Privacy pane for individual changes.

## Verification checklist

1. `keyless doctor` (or `/v1/doctor` → `permissions`) — reports TCC state.
2. AX/Accessibility: `osascript -e 'tell application "System Events" to get name of every window of (first process whose name is "Keyless")'` works only if Accessibility is granted to the calling host.
3. Flip a grant in System Settings, confirm the onboarding row flips to
   "Allowed" or the "Restart to apply" hint appears, then relaunch and confirm it persists.
4. After any relaunch, confirm exactly one process/window (single-instance flock).

## Gotchas

- The TCC grant for the *host* (Terminal) is separate from the app bundle's
  grant. `doctor` may show cua-driver attribution to `com.apple.Terminal`.
- Do not try to bypass TCC — there is no supported programmatic grant. The UX
  must guide the user and tell them when a restart is required.
- Screen Recording being optional is deliberate: a grant made in another
  process may not apply to this one until relaunch, so requiring it could
  strand onboarding.
- A "Set up all" button is shown only while some permission is still missing;
  once everything is granted it disappears.
