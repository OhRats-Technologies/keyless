# Architecture and Product Organization

**Status:** implemented Linux design (2026-11-19). The shared engine, platform seams, STT/provider layer, diagnostics, PySide6 shell, and Linux packaging now exist. Windows and macOS are capability stubs; the vision/voice core was not rewritten. This document records the target organization and what actually shipped.

**Reference:** `anomalyco/hex` @ `6435633` (read-only). Borrow the organization and the outcomes, not the code, the visual identity, or the Rust/Rune stack.

## 1. Goals and non-goals

### Goals

- **One shared engine, per-OS hosts.** Gaze, voice, routing, grounding, calibration, and model logic stay shared. OS-specific input, capture, permissions, tray, and packaging live behind interfaces.
- **A layman surface.** Install, first-run setup, permissions, camera check, calibration, model setup, practice, and daily use must not require a terminal or JSON edits.
- **Visible state.** A small always-available surface shows Listening/Paused, what was heard, what was clicked, and how to stop.
- **Local-first, bring-your-own AI.** STT, vision, and history live on the machine. Core pointer/voice commands need no LLM. Any LLM is optional and chosen by the user.
- **English-first.** The first release targets English; the model catalog leaves room for multilingual STT later.
- **Low latency stays the point.** UI and packaging must not add per-action overhead to the existing deterministic paths.

### Non-goals

- Porting Hex, Rune, or its design system.
- Bundling or choosing an LLM. The user brings one (OpenAI-compatible, Ollama, or an ACP agent).
- macOS App Store distribution (global input/accessibility taps are incompatible with the usual sandbox).
- Rewriting the gaze/voice math before the platform seams exist.

## 2. What to borrow from Hex

| Hex pattern | Why it fits Keyless |
|---|---|
| One shared window; per-OS hosts | The app needs one product identity while input/capture/permissions differ by OS. |
| `Capabilities` decides which rows exist | Missing OS features should be omitted, not shown as disabled imitations. |
| `app_paths` owns config/data/models/logs | Fixes today's split between `~/.config`, `~/.cache`, and library caches. |
| Pinned model catalog + downloader + receipts | Gives progress, resume, verification, and offline preflight for app-owned assets. |
| Onboarding as a story with a real practice | Teaches look/speak/click safely before the live desktop. |
| Capsule overlay that never takes focus | The right shape for gaze/click feedback: small, state-specific, click-through, one action. |
| Design-system rules (one accent, few actions, sentence case) | Keeps the layered UI coherent without inventing a new visual language. |
| LLM features through an external service | Hex does not bundle an LLM; it reports when OpenCode is absent. Keyless generalizes this: provider interface with OpenAI-compatible, Ollama, and ACP adapters. |

Do **not** borrow: Rune/GPUI, macOS-first assumptions, the dictation data model, or Hex's exact visual identity.

## 3. Target layers

```
┌──────────────────────────────────────────────────────────────┐
│ UI: window, tray/menu bar, overlay, onboarding, settings,    │
│ history, diagnostics                                         │
├──────────────────────────────────────────────────────────────┤
│ App: composition root, lifecycle, single instance, CLI       │
├──────────────────────────────────────────────────────────────┤
│ Engine: capture → gaze → voice → routing → grounding → input│
├──────────────────────────────────────────────────────────────┤
│ Services: config, models, history, permissions, logging      │
├──────────────────────────────────────────────────────────────┤
│ Platform: linux | macos | windows (sources, sinks, hosts)    │
└──────────────────────────────────────────────────────────────┘
```

Dependency rule: **UI → App → Engine → Services → Platform**. Platform code never imports UI; the engine never imports a specific OS module directly.

## 4. Repository organization

The detailed tree and migration table are in [file-map.md](file-map.md). The shape:

```
src/keyless/
  app/          CLI, daemon, lifecycle, single instance
  config/       schema, migrations, paths
  platform/     protocols + linux/ macos/ windows/
  capture/      cameras, stereo, audio, screen orchestration
  gaze/         landmarks, calibration, cursor, filters, learner
  voice/        listener, STT, pop, command parsing
  input/        pointer, hotkeys, typing, click harvesting
  grounding/    clicker, screen OCR, accessibility, VLM
  routing/      dispatcher, LLM, actions, scroll
  models/       catalog, store, downloader, receipts, providers (openai_compat, ollama, acp)
  ui/           app shell, tray, overlay, onboarding, settings, history
  diagnostics/  doctor, logs, capability report
  ipc/          shared state, Firefox bridge
```

Naming rules:

- `platform/<os>/` contains OS APIs only; no product copy, no UI.
- Protocols and capability enums live in `platform/base.py` and `platform/capabilities.py`.
- `capture/` orchestrates; it does not know whether a camera is V4L2, AVFoundation, or Media Foundation.
- `models/` owns app-managed assets and provider status; it is not a UI module.
- `ui/` is allowed to depend on engine services through narrow interfaces, never on OS modules.

## 5. Cross-platform strategy

Full matrix and per-OS notes: [platform-matrix.md](platform-matrix.md).

Summary:

| Capability | Linux | macOS | Windows | Shared contract |
|---|---|---|---|---|
| Pointer/click/type | `/dev/uinput` | CGEvent | `SendInput` | `PointerBackend` |
| PTT hotkey | evdev | CGEventTap / IOHID | low-level keyboard hook | `HotkeySource` |
| Cameras | V4L2 / OpenCV (mono or stereo) | AVFoundation | Media Foundation | `CameraSource` |
| Audio | PipeWire via PortAudio | CoreAudio via PortAudio | WASAPI via PortAudio | `AudioSource` |
| Screen | GNOME Shell/portal | ScreenCaptureKit | Windows.Graphics.Capture | `ScreenSource` |
| Accessibility | AT-SPI2 | AXUIElement | UI Automation | `AccessibilityBridge` |
| Permissions | udev/uinput, portal | TCC | privacy settings/UAC | `PermissionService` |
| Tray | StatusNotifierItem | NSStatusItem | `Shell_NotifyIcon` | `Tray` |
| Autostart | XDG/systemd | SMAppService/LaunchAgent | Run key/startup task | `Autostart` |
| Paths | XDG | Application Support | AppData/LocalAppData | `AppPaths` |

### Capability policy

- If an OS lacks a capability, **omit the feature** and say why in the capability report; do not present a dead control.
- If a permission is denied, **show the reason and one action** (Allow, Open Settings, Retry), not a stack trace.
- If a provider (an LLM provider, ACP agent, or the Firefox bridge) is absent, the core pointer/voice paths still run and the dependent feature says it is unavailable.
- Every capability gets a `status()` used by onboarding, `doctor`, and the UI; no capability is assumed from the OS name alone.

### Cross-platform risks

- **Global click-through overlays on GNOME Wayland** are the hardest surface. A normal Wayland window cannot be made click-through with a portable API. Per-OS options: GNOME Shell extension (native but GNOME-versioned), XWayland input shape (fallback), tray/toast only (safe v1). This is a decision, not a detail: see D3.
- **Input injection on macOS** needs Accessibility and Input Monitoring; **on Windows** it can be blocked by UIPI for elevated apps and flagged by anti-cheat/AV; **on Linux** it needs `/dev/uinput`.
- **Screen capture** on macOS needs Screen Recording; on GNOME it needs the Shell/portal permission; denial must disable click-by-name, not crash at click time.
- **Camera identity** differs by OS. Calibration must be keyed by a stable camera pair ID, not `/dev/videoN` or a Windows index.
- **Packaging and signing** are part of the port, not an afterthought: notarization, code signing, udev rules, and installers are required for a layman.

## 6. Data, models, and providers

### App data layout

Use one `AppPaths` service with four roots:

| Root | Linux | macOS | Windows |
|---|---|---|---|
| config | `~/.config/keyless` | `~/Library/Application Support/Keyless` | `%APPDATA%\Keyless` |
| data | `~/.local/share/keyless` | `~/Library/Application Support/Keyless` | `%LOCALAPPDATA%\Keyless` |
| cache | `~/.cache/keyless` | `~/Library/Caches/Keyless` | `%LOCALAPPDATA%\Keyless\cache` |
| logs | `~/.local/state/keyless/logs` | `~/Library/Logs/Keyless` | `%LOCALAPPDATA%\Keyless\logs` |

- `KEYLESS_HOME` overrides all roots for tests and portable installs.
- macOS uses one Application Support root for both config and data; Linux and Windows split them.
- Provider-owned models stay in the provider's store; `models/providers.json` records status, not files.
- Migrate the current `~/.config/keyless/config.json` and `stereo_calib.json` into the new layout once; never delete the old files.
- Keep models in **data**, not cache: they are user assets that survive cache clearing.
- Keep the MediaPipe landmarker in **cache**: it is re-downloadable. The Parakeet ONNX files are **data** with a verified receipt.
- Windows: keep large models/logs in `LocalAppData`, not roaming `AppData`.

### Shipped folders

```
<data>/keyless/
  models/
    <asset-id>/
      <pinned files>
      <file>.partial
      receipt.json
    providers.json
    custom/
  history/
    actions.db
<config>/keyless/
  settings.json
  onboarding.json
  calibration/
    <pair-id>-<monitor>.json
<cache>/keyless/
  face_landmarker.task
  downloads/
<logs>/keyless/
  app.jsonl
```

### Model and provider strategy (decided: D2, D9, D10)

Split by role. The app owns what the core loop needs; the user brings the LLM.

| Role | Owner | Default |
|---|---|---|
| STT | App (pinned download, receipt) | Parakeet TDT 0.6B v2, ONNX int8 via `onnx-asr`; single engine |
| Face landmarks | App (cache) | MediaPipe face landmarker |
| Text LLM | User | none preselected |
| Gaze VLM | User, optional | none; DOM, AT-SPI, and OCR tiers work without it |

LLM/VLM connect through one `Provider` interface (`models/providers/`):

- `openai_compat` — OpenAI, OpenRouter, Groq, LM Studio, llama.cpp, vLLM, Gemini's OpenAI-compatible endpoint, Ollama `/v1`. One `httpx` client plus presets.
- `ollama` — local status/pull/warm progress.
- `acp` — any [Agent Client Protocol](https://agentclientprotocol.com/get-started/introduction) agent (Claude, Codex, Gemini CLI, Copilot, OpenCode, Cline, Kimi, …) spawned over stdio, discovered through the ACP registry. Text roles only; the agent owns authentication; the app rejects its tool/permission requests.

Roles declare what they need (`image_input`, `fast`); the settings page shows which providers qualify. API keys live in the OS keychain. A remote provider on the VLM role sends screenshots off-device and needs explicit consent. Details: D9, D10.

### First-run model flow (target)

1. Download STT (about the size of one model file; progress, resume, verify). This is the only required download.
2. Offer "Connect an AI (optional)": pick OpenAI-compatible, local Ollama/LM Studio (auto-detected on loopback), or an installed ACP agent (auto-detected on `PATH`). Test the connection with one real request.
3. Offer the gaze VLM only if the chosen provider has image input; otherwise explain which commands remain available.
4. Never block deterministic pointer/voice commands on an optional provider.

## 7. UI surfaces

The UI is its own layer; it is not embedded in the daemon. Surfaces:

| Surface | Purpose |
|---|---|
| Main window | Home/status, Setup, Cameras, Calibration, Models, Settings, History, Diagnostics, About |
| Tray/menu bar | Listening/Paused, Open, Pause, Calibrate, Quit |
| Overlay | Gaze cursor/target, click confirmation, heard/understood pill, permission/error pill |
| Onboarding | Engine-owned first-run story: welcome → devices → calibration → ready |
| Settings | Hotkey/mode, pointer, gaze, cameras, audio, models, overlay, privacy, startup |
| History | Recent actions, target, result, latency, tier, app; retry/copy |
| Diagnostics | `doctor` summary, camera/mic previews, capability report, logs |

The desktop shell and landing page use the OhRats UI kit as the shared visual language: Inter for prose, Space Mono for labels/state, warm neutrals, one brown border, sharp edges, and light/dark tokens. Design rules adapted from Hex:

- One primary action per surface; everything else is Secondary/Ghost.
- Sentence case; one line per control; no intro paragraphs above cards.
- One accent; light/dark follows the system.
- Destructive actions last and two-step.
- Every missing permission or provider gets a plain-language state and one action.
- The overlay never takes focus; only its controls take clicks.

### Onboarding state machine

```
Welcome
  → Devices           (TCC rows, camera/mic checks, offline speech model progress)
  → Calibration       (guided dots; Continue gated on calibration.ready)
  → Ready             ("Start listening"; where it lives; pause/quit)
```

- The state machine lives in `keyless.ui.onboarding.flow` and is shared by both
  shells; the step and completion are persisted in `onboarding.json` and served
  by `GET/POST /v1/setup`. Resume after a crash/relaunch.
- Camera/mic and speech-model readiness gate the Devices step; a "Skip for now"
  path is available so a denied permission or failed download never dead-ends.
- A denied permission offers the exact Settings pane and marks the step
  unresolved; model downloads resume from `.partial` with Try again.

## 8. Migration order and status

Keep behavior stable; extract seams before adding surfaces.

| Step | Status | Evidence |
|---|---|---|
| 1. Paths and capabilities | **Done** | `config/paths.py`, `config/migrations.py`, `platform/base.py`, `platform/capabilities.py`; `KEYLESS_HOME`; migration copies and keeps legacy files |
| 2. Linux behind protocols | **Done** | uinput, evdev, V4L2, AT-SPI, screen, Mutter layout under `platform/linux/`; CLI/daemon behavior preserved |
| 3. Models, providers, diagnostics | **Done** | Parakeet + pinned downloader (Whisper removed in 0.1.1); provider adapters; replaced native Gemini; `doctor`/`--json` |
| 4. UI shell | **Done for Linux** | main window, tray, onboarding, settings, history, theme, D3 Wayland fallback; headless tests and light/dark goldens |
| 5. Second OS | **Stubs only** | Windows/macOS capability reports and Pointer/Hotkey/Camera/Screen/Accessibility/Permission/Tray/Autostart stubs raise `NotSupportedError` |
| 6. Third OS and packaging | **Linux done; others not started** | `.deb` and AppImage built and smoke-tested; no signing/notarization |

The model/provider split and role bindings are in D2/D9/D10 and shipped. The remaining migration is UI-to-daemon lifecycle integration (the UI currently starts the CLI daemon as a child) and the OS ports.

Do not combine a file move with a behavior change in the same review; commits `9053fca`, `5d7b684`, `ef6721b`, `b373998`, and the packaging commit follow that rule.

## 9. Testing, diagnostics, packaging

- **Unit tests** per engine module; **contract tests** for each platform protocol with a fake.
- **UI goldens** for the main window, onboarding steps, overlay states, settings, and history; render light/dark.
- **Hardware tests** are manual and marked skipped; record OS, camera pair, GPU, and permission state.
- **`doctor --json`** reports paths, cameras, audio, permissions, model/provider status, capabilities, and path errors; the UI reads the same `DoctorReport`.
- **Verified here**: `uv run pytest` (97 tests), headless Qt goldens, a fake OpenAI-compatible server, a fake ACP agent, a real Claude Agent ACP round-trip, a 675 MB `.deb` and a 561 MB AppImage. Hardware-dependent jobs remain manual.
- **Packaging**: Linux `.deb`/AppImage (udev rule included), macOS `.dmg` notarized (outside MAS), Windows MSI/EXE signed; uninstall and update paths from day one.

## 10. Decisions now settled in code

- **D1** PySide6 shell is implemented. The overlay remains per-OS; GNOME Wayland keeps the tray/toast fallback.
- **D2** App-owned Parakeet/landmarks; bring-your-own text LLM and optional VLM (D9/D10).
- **D3** X11 click-through pill; Wayland tray/toast fallback with an explicit capability reason.
- **D4** Four roots and `KEYLESS_HOME` are implemented; one-time migration keeps old files.
- **D5** Stable `/dev/v4l/by-id` identity and a camera picker exist; calibration is still one legacy file rather than `<pair>-<monitor>.json` (partial).
- **D6** Screen permission is reported, but the first capture still triggers the desktop prompt rather than a preflight request (partial).
- **D7** Linux `.deb`/AppImage are built; macOS/Windows signing and installers are not started.
- **D8** Linux productization first; Windows/macOS are stubs.

See [decisions.md](decisions.md) for the full status table.
