# macOS native host (SwiftUI)

**Status:** working native host on `greenfield/gaze-v2`. The gaze model is
being rebuilt as target-level intent; see
[`docs/GREENFIELD.md`](../../docs/GREENFIELD.md) and
[`docs/HANDOFF.md`](../../docs/HANDOFF.md). The UI contract work is tracked in
[`docs/plans/ui-unification-goal.md`](../../docs/plans/ui-unification-goal.md).
The host runs voice-only until a gaze publisher exists.

## Architecture

```
SwiftUI host (this dir)  ──HTTP──▶  keyless serve (127.0.0.1:8766)
 MenuBarExtra, Home/Activity/Settings, engine-owned setup, TCC actions
                          │
Python engine (../src)  ◀──┘
 daemon, STT, routing, doctor, history, capabilities
```

- Engine stays Python: no gaze/voice rewrite. macOS engine backends
  (`CGEventPost`, AVFoundation, ScreenCaptureKit, AXUIElement) land behind
  `platform/macos/*` protocols; until then `doctor` reports them as
  `unsupported_platform` and the SwiftUI host disables those rows.
- `Capability.swiftui` (`keyless.platform.capabilities.probe_swiftui`) reports
  whether this host can be built here: Swift toolchain + these sources.
- Control plane: `src/keyless/ipc/control.py` — status, capabilities, doctor,
  config, history, start/pause, plus engine-owned setup (`GET/POST /v1/setup`)
  and model downloads (`GET /v1/models`, `POST /v1/models/download|cancel`).
  `POST /v1/calibrate` returns a disabled notice; there is no calibration gate.
- Single cursor: the cua agent cursor at rest, the hardware pointer while the
  mouse moves or clicks. See `src/keyless/platform/macos/pointer_mode.py`.

## Build

```sh
cd packaging/macos
swift build -c release
# or open in Xcode 15+ on macOS 14+ (Package.swift is the project)
```

Run the engine alongside:

```sh
uv run keyless serve --port 8766
```

## Signing + packaging

- Developer ID + notarization, hardened runtime, no App Store sandbox
  (global input/accessibility taps are incompatible with it).
- Autostart: `SMAppService` on macOS 13+, LaunchAgent fallback.
- `.dmg` with the `.app`, usage strings from `Keyless/Info.plist`.

## Native backends

`src/keyless/platform/macos/` already provides pointer (`CGEventPost`),
display, screen capture, AVFoundation cameras, autostart, and TCC probes. Like
Hex, Accessibility covers the event taps our hotkeys need, so the app never
asks for Input Monitoring. `keyless doctor` reports what is actually usable.

Still open: a gaze-dot overlay, richer AXUIElement bridges, and spawning the
engine as a direct child so TCC attribution is the app's rather than a
terminal's.
