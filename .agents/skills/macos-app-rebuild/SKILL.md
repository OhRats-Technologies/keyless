---
name: macos-app-rebuild
description: >-
  Build, install, sign, and relaunch the Keyless SwiftUI macOS host, then verify
  a single instance/window. Use whenever Swift code under packaging/macos changes
  and you need to see it in the running app: "rebuild the mac app", "install the
  new binary", "relaunch Keyless", "verify one window", "screenshot the app",
  "codesign the bundle", or after editing any Keyless/*.swift file. Covers the
  single-instance flock, the AppKit terminate veto, and AppleScript UI navigation.
---

# macOS app rebuild & verify

Keyless's macOS shell is a SwiftUI package at `packaging/macos`. The running app
is an ad-hoc-signed bundle at `~/Applications/Keyless.app`. Source edits do NOT
reach the running app until you build, copy the binary in, re-sign, and relaunch.

## Key paths

| Thing | Path |
| --- | --- |
| Swift package | `<repo>/packaging/macos` |
| Built binary | `$(cd packaging/macos && swift build -c release --show-bin-path)/Keyless` |
| Installed bundle | `$HOME/Applications/Keyless.app` |
| Installed binary | `$HOME/Applications/Keyless.app/Contents/MacOS/Keyless` |
| Single-instance lock | `$HOME/Library/Application Support/Keyless/keyless-app.lock` (POSIX `flock`) |
| Bundle id | `party.ohrats.Keyless` |

`Info.plist` is excluded from the SwiftPM target (`Package.swift`) and lives in
the `.app` bundle; TCC usage strings are there, not in Swift code.

## Build → install → relaunch → verify

Run from the repo root:

```bash
cd packaging/macos && swift build -c release
BIN=$(swift build -c release --show-bin-path)/Keyless
cp "$BIN" "$HOME/Applications/Keyless.app/Contents/MacOS/Keyless"
./sign-local.sh "$HOME/Applications/Keyless.app"   # Developer ID — stable TCC identity

# Restart cleanly: kill by full binary path so we never touch the engine.
pkill -f "$HOME/Applications/Keyless.app/Contents/MacOS/Keyless" 2>/dev/null || true
sleep 1
open "$HOME/Applications/Keyless.app"
sleep 3
pgrep -fl "$HOME/Applications/Keyless.app/Contents/MacOS/Keyless"   # must be exactly 1
osascript -e 'tell application "System Events" to get name of every window of (first process whose name is "Keyless")'  # exactly 1 window: Keyless
```

`open` (no `-n`) is required: LaunchServices plus the `flock` in `KeylessApp`
guarantee one process. Forced `open -n` is handled too — the second instance
sees the held lock and exits, so you still end up with one process/window.

## Restart from inside the app (no "knocking" veto)

Every in-app restart path (`Home → Relaunch`, setup sheet `Restart Keyless`,
revoke/reset) goes through `PermissionGuideController.relaunch()` in
`Keyless/PermissionGuide.swift`. It spawns a detached `/bin/sh -c 'sleep 1; open
"<bundle>"'`, flushes `UserDefaults` with `synchronize()`, then calls POSIX
`exit(0)`. Do **not** replace this with `NSApp.terminate()`: AppKit vetoes
termination while a setup sheet is presented, producing the macOS "knocking on
wood" sound and leaving the app open. `exit(0)` bypasses the veto; the lock FD
closes with the process.

To verify a restart: capture the pre-restart PID, trigger the in-app action,
wait ~2 s, and confirm the PID changed and the process/window count is still 1.

## Navigate / screenshot the UI (AppleScript)

The sidebar is an outline, not a plain list. The reliable selector:

```bash
osascript -e 'tell application "Keyless" to activate' \
  -e 'delay 0.4' \
  -e 'tell application "System Events" to tell process "Keyless" to set selected of row 3 of outline 1 of scroll area 1 of group 1 of splitter group 1 of group 1 of window 1 to true'
# row 1 = Home, row 2 = Activity, row 3 = Settings
screencapture -x /tmp/keyless.png
```

`click row N` is unreliable; `set selected ... to true` works. To read what is
on screen, `get entire contents of window 1` and grep for the expected static
text/buttons. This needs System Events accessibility access for the calling
process (Terminal/Terminal-like host); the same grant the app uses.

## Gotchas

- `swift build` must be run in `packaging/macos` (or with `--package-path`); the
  root has no `Package.swift`.
- Use `pkill -f <full binary path>` — a bare `pkill -f Keyless` can match the
  engine or unrelated processes.
- **NEVER ad-hoc sign the installed dev bundle.** `packaging/macos/sign-local.sh`
  signs with the shared Developer ID (`Developer ID Application: Fernando
  Nogueira (W9C4V23252)`, same cert WakeBar releases use). Ad-hoc `-s -` makes
  the designated requirement `cdhash H"…"`, so every rebuild silently drops
  the app's TCC grants. Developer ID gives a stable
  `identifier party.ohrats.Keyless … leaf[subject.OU] = W9C4V23252` requirement.
  The script falls back to ad-hoc (with a warning) if the cert is missing.
- The engine is a separate process. Restarting the app does not restart
  `keyless serve`; use the **keyless-engine** skill for that.
- After changing Swift, the engine control plane may also need a restart if you
  touched its routes — see **keyless-engine**.
