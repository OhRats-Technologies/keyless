# Handoff: cua-driver as Keyless's action backend

Date: 2026-11-23. Working tree is **uncommitted** (`git status` shows ~35 changed/new files).

## Goal

Keep the Keyless program as-is (gaze, voice, router, UI) but route **discrete
control actions** (click/type/scroll/hotkey at the gaze point) through a
**pinned cua-driver binary** instead of hand-maintained per-OS input code.
The 60Hz gaze cursor loop stays native (uinput/CGEvent) — cua actions cost
250ms–1s each, fine for voice clicks, fatal for the cursor loop.

## Decisions already made (with evidence)

- **Canonical key names at the engine boundary** (`src/keyless/input/keys.py`):
  dispatcher speaks `"LEFTCTRL"`, backends map to native codes. Mirrors
  cua-driver's `press_key(pid, key: &str, modifiers)` and SDL scancodes.
  Names are positional; layout-aware resolution (UCKeyTranslate, as cua does
  for AZERTY `cmd+a`) is a documented follow-up.
- **Hybrid pointer** (`src/keyless/platform/cua/pointer.py`):
  moves → inner native backend; clicks/scroll/keys/type → cua, with
  per-call native fallback. Still a `PointerBackend`, so zero router churn.
- **Pinned runtime, not a dependency** (`src/keyless/models/runtimes.py`):
  cua-driver 0.59.2, per-OS asset + sha256 (darwin-universal 44MB
  `85ac3cae…`, linux-x86_64 30MB `6aa6b740…`, linux-arm64, windows x86_64/arm64).
  `keyless models download cua-driver` fetches + verifies + writes receipt.
  Upgrade = version + hash bump. MIT-licensed core (perception ext is AGPL —
  do NOT install it).
- **Embedding model** (from cua's EMBEDDING.md): spawn
  `serve --embedded --socket <private>` as a direct child with
  `CUA_DRIVER_EMBEDDED=1`, then `mcp --embedded` stdio proxy speaking MCP
  JSON-RPC (`initialize`, `tools/call`). Never via `open`/NSWorkspace (breaks
  macOS TCC responsibility chain). `check_permissions` must report
  `attribution: host`. Restart daemon after grant changes (per-process TCC cache).
- **Button mapping**: 1/2/3 = left/right/middle everywhere (`keys.py`).

## What is implemented (uncommitted)

- `src/keyless/models/runtimes.py` — pinning, `status()`, `download()` (verified working, 42MB downloaded).
- `src/keyless/platform/cua/{__init__,driver,pointer}.py` — manager (embedded spawn + JSON-RPC), hybrid backend.
- `src/keyless/config/schema.py` — `action_backend: str = "native"` (+ `keyless run --action-backend`).
- `src/keyless/platform/{router,capabilities}.py`, `macos/capabilities.py` — `cua_driver` capability row; router `create_pointer_backend(..., action_backend)`.
- `src/keyless/diagnostics/doctor.py` — `cua_driver` section + text row.
- `src/keyless/main.py` — `models download/status cua-driver`.
- `src/keyless/input/{keys,pointer}.py`, `platform/{base,stub}.py`,
  `platform/{linux,macos}/pointer.py`, `router/dispatcher.py` — canonical keys end-to-end, evdev shim deleted.
- `tests/unit/test_cua_driver.py` — 8 passed, 1 skipped (evdev-gated).
- `packaging/macos/*` SwiftUI host + Hex-style permission guide/onboarding (builds clean).

## What is verified working

- `keyless doctor` on this Mac: pointer/cameras/audio/keychain/providers/swiftui/cua rows real.
- `keyless models download cua-driver` → verified binary; `doctor` then reports `cua-driver: ready (0.59.2, darwin-universal)`.
- `cua-driver --version` → 0.59.2; manual stdio `initialize` over shell pipes → full valid response.
- `swift build` clean; pytest 142 passed (3 pre-existing env failures: BSD `ar`, ARM64 installer script — fail on pristine tree too).

## Open blocker: transport hang (active investigation)

`CuaDriverManager.start()` times out on `initialize` **only when a reader
thread pre-exists**. Bisected to a minimal repro (no cua involved):

- `os.pipe` or `subprocess(PIPE)` + **main-thread read after write** → works.
- Same + **reader thread blocked in `BufferedReader.read(4096)` before the write** → starves forever, even with data pre-buffered (`select` confirms absent).
- Same + reader thread using **unbuffered `os.read(fd)`** → works.
- Even plain `cat` + threaded `BufferedReader.read` starves.

Working conclusion: **`io.BufferedReader.read()` from a non-main thread never
returns in this env** (macOS, `.venv` Python 3.13.13). Not yet root-caused;
main-thread `BufferedReader` reads are fine.

**Fix to apply**: reader thread must use unbuffered reads — `os.read(fd, N)`
on `proxy.stdout.fileno()` (set the pipe non-blocking or keep blocking +
`select` wakeups), accumulate in `bytearray`, split on `\n`. Same for stderr
drain (or keep DEVNULL — stderr is only diagnostics). Then re-run live
`check_permissions` → expect `attribution` field, then `get_desktop_state`.

## Next steps in order

1. Fix `_read_loop`/`_drain_stderr` to unbuffered `os.read` (+ regression test with real pipes, no mocks).
2. Live `check_permissions` + `health_report` via manager; record attribution on this Mac.
3. First cua-driven voice click end-to-end (`--action-backend cua`, dry-run gaze target).
4. macOS spawn-chain note: engine must be a direct child of the signed app for TCC inheritance (Swift `Process`, never `open`); terminal-spawned runs attribute to the terminal (works, `caller` attribution).
5. Docs: `docs/platform-matrix.md` cua row (Linux/Wayland stays native fallback — cua needs portal/X11 paths there; GNOME helper + Shell restart is theirs, not ours).
6. Commit.
