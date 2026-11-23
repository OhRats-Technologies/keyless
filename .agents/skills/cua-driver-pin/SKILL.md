---
name: cua-driver-pin
description: >-
  Inspect, download, verify, and upgrade the pinned cua-driver runtime binary.
  Use for anything about the discrete-action driver: "bump cua-driver", "upgrade
  the pinned runtime", "cua-driver version/hash", "download cua-driver", "is the
  driver ready", "action backend cua vs native", or when doctor/permissions show
  cua-driver missing. Upgrades are strictly a version + hash bump in one file.
---

# Pinned cua-driver runtime

Keyless ships a pinned `cua-driver` binary for discrete actions (click, type,
scroll, hotkey) and for the macOS eye cursor. Eye motion draws on cua-driver's
per-session agent overlay at 60 Hz through fire-and-forget `move_cursor`
targets; it never warps the real pointer. Discrete actions use the hybrid
pointer in `src/keyless/platform/cua/pointer.py`, and the eye overlay pump lives
in `src/keyless/platform/cua/agent_cursor.py`.

## Where the pin lives

Single source of truth: `src/keyless/models/runtimes.py`.

- `CUA_DRIVER_VERSION` — current pin (e.g. `0.59.2`)
- `CUA_DRIVER_TAG` / `CUA_DRIVER_BASE` — GitHub release tag/download base
  (`https://github.com/trycua/cua`)
- `CUA_DRIVER_TARGETS` — one `RuntimeTarget` per OS with `asset`, exact
  `bytes`, and `sha256`
- `current_target()` maps host → label: `darwin-universal`, `linux-x86_64`,
  `linux-arm64`, `windows-x86_64`, `windows-arm64`
- Assets: `cua-driver-rs-<version>-<label>-binary.{tar.gz,zip}`
- Binaries install to `<data_dir>/runtimes/cua-driver/<version>/<exe>` with a
  `receipt.json` written on success. `status().ready` is true only when the exe
  exists AND the receipt sha256 matches the pin.

License policy: **MIT core only (`CUA_DRIVER_LICENSE = "MIT"`)**. Do NOT pull in
the AGPL-licensed perception extension. This is why the pin is a specific
release asset, not a latest-tag install.

## Inspect / download / verify

```bash
cd <repo>
.venv/bin/keyless models status                 # shows pinned runtimes table (version, target, ready, path)
.venv/bin/keyless models download cua-driver    # SHA-256 verify + extract + receipt
.venv/bin/keyless doctor --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["cua_driver"])'
```

Direct Python API (same as the CLI uses):

```python
from keyless.models import runtimes
print(runtimes.status())
runtimes.download()   # verifies bytes/hash, writes receipt
```

Verify the binary reports the pinned version:

```bash
"<data_dir>/runtimes/cua-driver/$(python -c 'from keyless.models.runtimes import CUA_DRIVER_VERSION as v;print(v)')/cua-driver" --version
# expect: 0.59.2 (matching CUA_DRIVER_VERSION)
```

## Upgrade procedure (the whole point of the pin)

1. Pick the new upstream release tag `cua-driver-rs-v<NEW>` from trycua/cua.
   Confirm the distribution is MIT core only — skip any AGPL perception bundle.
2. In `src/keyless/models/runtimes.py`, bump `CUA_DRIVER_VERSION`. The tag/base
   and asset filenames are f-strings derived from it, so they update automatically.
3. For **every** label in `CUA_DRIVER_TARGETS`, download the new asset and
   recompute `bytes` and `sha256` from the actual bytes:
   ```bash
   curl -L -o /tmp/driver.bin "<CUA_DRIVER_BASE>/<asset>"
   shasum -a 256 /tmp/driver.bin
   stat -f '%z' /tmp/driver.bin     # macOS; use `stat -c %s` on Linux
   ```
   Update each `RuntimeTarget` with the exact values. Do not reuse hashes from a
   different label or a different version.
4. Run `keyless models download cua-driver` on this host, then
   `keyless doctor` and confirm `cua-driver: ready (<NEW>, <target>)`.
5. Run the test suite. Keep the change to this one file plus tests/docs; no
   backend code changes should be needed for a version bump.

## Embedded mode (how the driver is launched)

`src/keyless/platform/cua/driver.py` runs the pinned binary as a direct child:

- `serve --embedded --socket <path>` launches the daemon in embedded mode.
- `mcp --embedded` is a stdio MCP proxy for tool calls.
- `CUA_DRIVER_EMBEDDED=1` and a fixed `standard` permission mode are set.
- Transport reads use unbuffered `os.read(fd)` with a `bytearray` frame buffer —
  do not regress this to buffered reads, which caused a hang.

On mac, a live check reports `source.attribution: host`, `accessibility: True`,
`screen_recording: True`, responsible bundle `com.apple.Terminal`, and ~62 tools.

## Config / backend flag

`action_backend` in engine config selects `native` or `cua` (CLI `--action-backend`
and `Keyless` config). The hybrid pointer uses cua for clicks/scroll/keys/type
with a per-call native fallback, and native for moves.

## Gotchas

- `ready: false` almost always means a hash/bytes mismatch after a bump, or a
  missing receipt — re-download rather than hand-editing the receipt.
- Verify per-OS: an asset can be correct on darwin and wrong on linux if a hash
  was copied across rows.
- Keep eye-cursor motion on the agent overlay; never call `move_cursor` with
  `scope:"desktop"` or fall back to the native pointer when the overlay is
  unavailable.
