# File Map

**Status:** migration executed for steps 1–5 (2026-11-19). The tree below is the shipped Linux layout with a few remaining compatibility shims; Windows/macOS files are stubs. Remaining moves are listed under "Migration status".

The goal is a layout where the shared engine is obvious, every OS-specific API is behind `platform/`, and the product surfaces (UI, onboarding, overlay) are separate from the daemon.

## Target repository tree

```
keyless/
├── docs/
│   ├── README.md
│   ├── architecture.md
│   ├── file-map.md
│   ├── platform-matrix.md
│   ├── decisions.md
│   ├── features/                 # future: behavior maps per feature
│   ├── plans/                    # future: design/decision records
│   └── design/                   # future: tokens, overlay, onboarding specs
├── integrations/
│   └── firefox_ext/              # current firefox_ext/, moved when packaging settles
├── packaging/
│   ├── linux/
│   │   ├── udev/99-keyless-uinput.rules
│   │   ├── keyless.desktop
│   │   └── keyless.service
│   ├── macos/
│   │   ├── Info.plist
│   │   ├── keyless.entitlements
│   │   └── launchagent.plist
│   └── windows/
│       ├── installer.iss
│       └── keyless.ico
├── scripts/                      # developer helpers only; no end-user setup
├── src/keyless/
│   ├── __init__.py
│   ├── app/
│   │   ├── __main__.py
│   │   ├── cli.py
│   │   ├── daemon.py
│   │   ├── lifecycle.py
│   │   └── single_instance.py
│   ├── config/
│   │   ├── __init__.py
│   │   ├── schema.py
│   │   ├── paths.py
│   │   └── migrations.py
│   ├── platform/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── capabilities.py
│   │   ├── linux/
│   │   │   ├── pointer.py        # uinput
│   │   │   ├── hotkeys.py        # evdev
│   │   │   ├── cameras.py        # V4L2
│   │   │   ├── v4l2.py           # exposure/gain ioctls
│   │   │   ├── display.py        # Mutter logical monitor layout
│   │   │   ├── hotkeys.py        # evdev PTT, click harvesting, cursor resolver
│   │   │   ├── screen.py         # GNOME Shell + portal
│   │   │   ├── accessibility.py  # AT-SPI
│   │   │   ├── permissions.py    # udev/uinput/portal
│   │   │   # tray is ui/tray.py (Qt); no native StatusNotifierItem adapter yet
│   │   │   └── autostart.py
│   │   ├── macos/
│   │   │   ├── pointer.py        # CGEvent
│   │   │   ├── hotkeys.py        # CGEventTap / IOHID
│   │   │   ├── cameras.py        # AVFoundation
│   │   │   ├── screen.py         # ScreenCaptureKit
│   │   │   ├── accessibility.py  # AXUIElement
│   │   │   ├── permissions.py    # TCC
│   │   │   ├── tray.py           # NSStatusItem
│   │   │   └── autostart.py
│   │   └── windows/
│   │       ├── pointer.py        # SendInput
│   │       ├── hotkeys.py        # WH_KEYBOARD_LL
│   │       ├── cameras.py        # Media Foundation
│   │       ├── screen.py         # Windows.Graphics.Capture
│   │       ├── accessibility.py  # UI Automation
│   │       ├── permissions.py    # privacy settings/UAC
│   │       ├── tray.py           # Shell_NotifyIcon
│   │       └── autostart.py
│   ├── capture/
│   │   ├── cameras.py            # device discovery + preview model
│   │   ├── stereo.py             # paired-frame orchestration
│   │   ├── audio.py              # mic stream + levels
│   │   └── screen.py             # screen capture orchestration
│   ├── gaze/
│   │   ├── types.py              # GazeFrame / GazeSource
│   │   ├── shared_memory.py      # keyless_gaze_v1 adapter
│   │   ├── runtime.py            # model-agnostic frame loop
│   │   ├── collect.py            # next: replayable event log
│   │   ├── replay.py             # next: offline metrics
│   │   └── model/                # next: target encoder, scorer, uncertainty
│   ├── voice/
│   │   ├── listener.py
│   │   ├── stt/
│   │   │   ├── base.py           # SttEngine contract
│   │   │   └── parakeet.py       # onnx-asr, CPU int8 default
│   │   ├── audio_io.py
│   │   ├── pop.py
│   │   └── commands.py
│   ├── input/
│   │   ├── pointer.py            # PointerBackend + dry-run
│   │   ├── hotkeys.py            # HotkeySource contract
│   │   ├── typing.py
│   │   └── click_harvest.py
│   ├── grounding/
│   │   ├── clicker.py            # tiered target resolution
│   │   ├── detect.py             # OCR + UI boxes from a captured image
│   │   ├── ocr.py
│   │   ├── vlm.py
│   │   └── accessibility/
│   │       ├── base.py
│   │       ├── linux_atspi.py
│   │       ├── macos_ax.py
│   │       └── windows_uia.py
│   ├── routing/
│   │   ├── dispatcher.py
│   │   ├── llm.py
│   │   ├── actions.py
│   │   └── scroll.py
│   ├── models/
│   │   ├── catalog.py            # pinned app-owned assets
│   │   ├── store.py              # paths, receipts, status
│   │   ├── downloader.py         # partial/resume/verify/promote
│   │   ├── receipts.py
│   │   └── providers/
│   │       ├── base.py           # Provider contract, capabilities, status
│   │       ├── openai_compat.py  # httpx; presets for OpenAI/OpenRouter/Groq/LM Studio/…
│   │       ├── ollama.py         # status/pull/warm
│   │       ├── acp.py            # Agent Client Protocol client over stdio
│   │       ├── acp_registry.py   # registry fetch + offline fallback list
│   │       ├── roles.py          # text/VLM role bindings and consent checks
│   │       ├── presets.json
│   │       └── secrets.py        # OS keychain via keyring
│   ├── ui/
│   │   ├── app.py                # main window shell
│   │   ├── overlay/macos.py      # overlay stub
│   │   ├── overlay/windows.py    # overlay stub
│   │   ├── session.py            # UI <-> daemon child process
│   │   ├── tray.py
│   │   ├── overlay/
│   │   │   ├── base.py
│   │   │   ├── linux.py
│   │   │   ├── macos.py
│   │   │   └── windows.py
│   │   ├── onboarding/
│   │   │   ├── flow.py
│   │   │   └── steps.py
│   │   ├── settings/
│   │   │   └── window.py
│   │   ├── history/
│   │   │   ├── db.py
│   │   │   └── window.py
│   │   └── theme/
│   │       └── tokens.py
│   ├── diagnostics/
│   │   ├── doctor.py
│   │   └── logs.py
│   └── ipc/
│       ├── state.py
│       └── browser/
│           └── firefox.py
├── tests/
│   ├── unit/
│   ├── contract/                 # platform protocol fakes and invariants
│   ├── ui/                       # headless/golden surfaces
│   └── hardware/                 # manual, skipped by default
├── pyproject.toml
└── uv.lock
```

## Migration status

| Area | Status | Notes |
|---|---|---|
| `config/` package (schema, paths, migrations) | **Done** | `settings.json`, one-time copy migration, `KEYLESS_HOME` |
| `platform/base.py`, `platform/capabilities.py` | **Done** | One capability report for doctor/UI |
| Linux pointer/hotkeys/cameras/V4L2/screen/AT-SPI/display | **Done** | Behavior-preserving moves; compatibility imports updated |
| STT (`voice/stt/`) | **Done** | Parakeet only (Whisper removed 2026-11-20), catalog/downloader/store/receipts |
| Providers (`models/providers/`) | **Done** | openai_compat, Ollama, ACP, registry, secrets, roles |
| Diagnostics (`diagnostics/doctor.py`) | **Done** | `doctor` and `doctor --json` |
| UI (`ui/`) | **Done for Linux** | Main window, tray, onboarding, settings, history, tokens, overlay fallback |
| Packaging (`packaging/linux/`) | **Done** | `.deb` and AppImage built and smoke-tested |
| Windows/macOS seams | **Stubs** | Capability stubs return `unsupported_platform`; backends raise `NotSupportedError` |
| `audio/`, `router/`, `vision/` shims | **Partial** | The engine modules still live here with moved OS details behind `platform/`; not a behavior problem |

### Remaining moves (not required for the first Linux release)

- `vision/ui_detector.py` still contains OCR/box detection; the capture API moved to `platform/linux/screen.py`.
- `vision/stereo_capture.py` and `vision/landmarks.py` remain sensor modules; the old `cursor_gimbal.py`, `calibrator.py`, `geo_gaze.py`, and `online_learner.py` have been removed. `keyless/gaze/` is the new model-agnostic seam.
- `audio/` still holds the listener/pop/STT compatibility shim; `voice/` has the audio loop and STT.
- `ipc/firefox_bridge.py` remains under `ipc/`; the optional extension still lives at `firefox_ext/` until packaging settles.

## Current-to-target map

| Current | Target | Notes |
|---|---|---|
| `main.py` (root) | `src/keyless/app/__main__.py` | Keep the root shim until packaging updates. |
| `src/keyless/main.py` | `app/cli.py` | Typer commands stay the public CLI during migration. |
| `src/keyless/daemon.py` | `app/daemon.py` | Composition root; move engine imports behind services. |
| `src/keyless/config.py` | `config/schema.py` + `config/paths.py` + `config/migrations.py` | Split settings schema, OS paths, and migrations. |
| `vision/landmarks.py` | `gaze/landmarks.py` | No behavior change. |
| `vision/stereo_capture.py` | `capture/stereo.py` + `platform/*/cameras.py` | Capture orchestration vs OS camera API. |
| `vision/v4l2_config.py` | `platform/linux/cameras.py` + `platform/linux/v4l2.py` | Linux-only. |
| `gaze/types.py` | `gaze/types.py` | New model-agnostic `GazeFrame` / `GazeSource` contract. |
| `vision/ui_detector.py` | `grounding/detect.py` + `platform/linux/screen.py` | Screen capture API vs OCR/box detection. |
| `vision/desktop_atspi.py` | `grounding/accessibility/linux_atspi.py` | Add `macos_ax.py` and `windows_uia.py` beside it. |
| `input/uinput_device.py` | `input/pointer.py` + `platform/linux/pointer.py` | `PointerBackend` contract + uinput implementation. |
| `input/evdev_mouse_listener.py` | `input/click_harvest.py` + `platform/linux/hotkeys.py` | Passive click harvesting vs evdev hotkeys. |
| `audio/stt_engine.py` | `voice/stt/` + `voice/audio_io.py` | Separate the Parakeet engine from the PortAudio stream. |
| `audio/active_listener.py` | `voice/listener.py` | Shared VAD/command listener. |
| `audio/pop_detector.py` | `voice/pop.py` | Shared. |
| `router/command_splitter.py` | `voice/commands.py` | Command parsing belongs with voice input. |
| `router/dispatcher.py` | `routing/dispatcher.py` | Shared routing; deterministic first. |
| `router/gaze_vlm_clicker.py` | `grounding/clicker.py` | Tiered target resolution. |
| `router/llm_agent.py` | `routing/llm.py` + `models/providers/` | Prompts stay in routing; Ollama/Gemini calls become provider adapters (Gemini via `openai_compat`). |
| `router/scroll_handler.py` | `routing/scroll.py` | Shared. |
| `ipc/firefox_bridge.py` | `ipc/browser/firefox.py` | Browser integration, not core IPC. |
| `ipc/state.py` | `ipc/state.py` | Shared memory; keep small and OS-neutral. |
| `scripts/setup_uinput_udev.sh` | `packaging/linux/udev/` + install hook | End-user setup belongs in the installer. |
| `scripts/lock_cameras_v4l2.sh` | `platform/linux/v4l2.py` helper | Developer/diagnostic helper only. |
| `firefox_ext/` | `integrations/firefox_ext/` | Keep root until packaging changes. |
| `tests/test_phase1.py` | `tests/unit/test_gaze.py` + `tests/contract/test_cameras.py` | Split by concern. |
| `tests/test_phase2.py` | `tests/unit/test_voice.py` + `tests/contract/test_audio.py` | Split by concern. |
| `tests/test_phase3.py` | `tests/unit/test_grounding.py` + `tests/ui/` | Add surface goldens later. |

## Rules

1. **No OS imports in the engine.** `gaze/`, `voice/`, `routing/`, and `grounding/` import protocols from `platform/base.py`, never `platform/linux/...`.
2. **No UI imports in the engine.** The overlay and window observe state; they do not own actions.
3. **One path service.** No module computes its own config/cache/model path.
4. **One capability report.** Onboarding, tray, settings, and `doctor` read the same `Capabilities`/`permission_state` values.
5. **Do not move and change behavior in the same review.** A pure move is its own commit.
6. **Keep the CLI.** `uv run keyless ...` remains valid through the migration and is the fallback for debugging.
7. **Keep the Firefox extension optional.** The product works without it; its absence is a capability state, not a crash.

## Docs tree (grow into this)

Hex separates user guides, feature maps, plans, and design rules. The equivalent here:

```
docs/
  README.md
  architecture.md
  file-map.md
  platform-matrix.md
  decisions.md
  features/        behavior and entry points per feature
  plans/           design records and implementation plans
  design/          tokens, surfaces, onboarding, overlay specs
  platform/        linux.md, macos.md, windows.md
```

Only the first five exist today; add the others when there is content, not as empty placeholders.
