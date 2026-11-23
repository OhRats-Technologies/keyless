---
name: keyless-engine
description: >-
  Restart and probe the Keyless Python control plane (`keyless serve`) and its
  /v1 HTTP endpoints. Use whenever UI controls read engine data, after changing
  src/keyless/ipc/control.py or any backend, or when the app shows "Not connected
  to keyless serve": "restart the engine", "is the engine up", "check /v1/status",
  "verify config endpoint", "run doctor", "port 8766".
---

# Keyless engine control plane

The engine is a separate Python process from the SwiftUI app. The app talks to
it over loopback HTTP on port **8766** (configurable with `--port`). The CLI
entry point is `keyless = keyless.main:app` (Typer), run from the project venv.

## Start / restart

```bash
cd <repo>
lsof -ti tcp:8766 | xargs kill 2>/dev/null || true   # stop any old server
sleep 1
nohup .venv/bin/keyless serve --port 8766 > /tmp/keyless-serve.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8766/v1/status
```

Always restart the server after editing route code (e.g. adding `/v1/config`) —
a running server keeps the old route table and will return 404/empty data.
The SwiftUI Settings page reads `/v1/config` and `/v1/doctor`; if those changed,
restart the engine **before** rebuilding the app.

## Endpoints (src/keyless/ipc/control.py)

| Method | Route | Returns |
| --- | --- | --- |
| GET | `/v1/status` | `{state, detail}` — state: stopped/listening/paused/error |
| GET | `/v1/capabilities` | `CapabilityReport.as_dict()` |
| GET | `/v1/doctor` | `DoctorReport.as_dict()` (no provider probes) |
| GET | `/v1/config` | full engine config as JSON object |
| POST | `/v1/config` | patch config with a JSON object, returns updated config |
| GET | `/v1/history?limit=N` | `{actions: [...]}` |
| POST | `/v1/start` | start daemon child |
| POST | `/v1/pause` | pause daemon child |
| GET | `/v1/setup` | canonical `{steps, state, complete, calibration}` document |
| POST | `/v1/setup` | `{action: reset|go_to|advance|back|complete, step?}` |
| POST | `/v1/calibrate` | voice-guided eye alignment guidance (no overlay) |

Quick probes:

```bash
curl -s http://127.0.0.1:8766/v1/status
curl -s http://127.0.0.1:8766/v1/config | python3 -m json.tool
curl -s http://127.0.0.1:8766/v1/doctor | python3 -c 'import sys,json;d=json.load(sys.stdin);print(list(d));print(d["stt"]);print(list(d["capabilities"]["capabilities"]))'
# no-op config write (echoes current value):
curl -s -X POST -H 'Content-Type: application/json' -d '{"transcript_overlay":false}' http://127.0.0.1:8766/v1/config
curl -s http://127.0.0.1:8766/v1/setup | python3 -m json.tool
curl -s http://127.0.0.1:8766/v1/models | python3 -m json.tool
```

## Doctor from the CLI

```bash
.venv/bin/keyless doctor                 # human readable
.venv/bin/keyless doctor --json          # one JSON document (used by UI/help)
.venv/bin/keyless doctor --no-provider-check   # skip network/process probes
```

`doctor` reports paths, cameras, audio, permissions, STT, providers, keychain,
cua-driver readiness, and the capability matrix. On mac, it depends on the
native backends under `src/keyless/platform/`; a missing/old binary or missing
TCC grant shows up here first.

## Config keys the UI binds to

These exist in `/v1/config` and are used by the SwiftUI Settings form:
`camera_mode`, `action_backend`, `ptt_hotkey`, `always_on_voice`,
`require_command_prefix`, `enable_pop_click`, `gaze_cursor_control`,
`gaze_online_ekf`, `llm_provider`, `llm_model`, `acp_agent_id`,
`gaze_vlm_remote_consent`. Engine-backed binary/status shown in Settings
(`stt.selected`, capability rows) come from `/v1/doctor`.

## Gotchas

- `/v1/config` is a **full object** on GET and a **partial patch** on POST.
  The Swift bridge `saveConfig(_:)` posts one key at a time and refreshes.
- The engine does not auto-restart the app, and the app does not auto-restart
  the engine. Treat them as independent processes.
- `lsof -ti tcp:8766` is the safest way to find the server; do not kill by
  process name (would also match the CLI or other Python processes).
- Calibration is engine-side and launched by `POST /v1/calibrate`; readiness is
  `GET /v1/setup` → `calibration.ready`. `POST /v1/start` stays gated until the
  file exists, but the shell now shows "Calibrate to begin" with an action
  instead of the raw missing-file path.
- The pinned Parakeet speech model is downloaded by `POST /v1/models/download`
  on a daemon thread; poll `GET /v1/models` for `{state, percent, bytes_done}`
  or read `GET /v1/setup` → `model`. `POST /v1/models/cancel` is cooperative
  and leaves `.partial` files resumable. Both shells gate setup on this.
