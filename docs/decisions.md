# Decisions

**Status:** implemented decisions, 2026-11-19. D1–D4, D7 (Linux), D8 (Linux-first), D9, and D10 are in code. D5 and D6 are partial. macOS/Windows execution of D7 is not started. Recommendations remain defaults, not irreversible choices.

| ID | Decision | Recommendation |
|---|---|---|
| D1 | UI stack | PySide6/Qt for the shared shell; native per-OS overlay |
| D2 | Model ownership | App-owned STT/landmarks; bring-your-own LLM/VLM |
| D3 | Overlay strategy | Per-OS native overlay; GNOME v1 falls back to tray/toast |
| D4 | Data roots | Split config/data/cache/logs per OS; env override |
| D5 | Camera identity | Stable per-OS IDs; calibration keyed by pair + monitor |
| D6 | Screen permissions | First-class permission state; disable click-by-name on denial |
| D7 | Packaging/signing | Linux .deb/AppImage; macOS notarized .dmg; Windows signed MSI/EXE |
| D8 | First cross-platform scope | Linux productization → Windows → macOS |
| D9 | LLM providers | OpenAI-compatible + Ollama + ACP agents; keychain secrets |
| D10 | Speech-to-text | Parakeet TDT 0.6B v2 via onnx-asr (CPU int8); single engine |

## Implementation status

| ID | Status | Evidence / remaining gap |
|---|---|---|
| D1 | **Done** | PySide6 control-room shell, tray, resumable onboarding, settings, history, tokens, headless goldens |
| D2 | **Done** | App-owned Parakeet/landmarks; no bundled LLM; role-split providers |
| D3 | **Done for Linux** | X11 click-through pill; Wayland tray/toast fallback with reason; no GNOME Shell extension |
| D4 | **Done** | `config/paths.py`, `KEYLESS_HOME`, non-destructive migration; models in data, landmarker in cache |
| D5 | **Partial** | Stable by-id discovery, camera picker, mono (1 camera) and stereo (2) paths; calibration still a single legacy file, not `<pair>-<monitor>.json` |
| D6 | **Partial** | Screen capability/permission is reported before clicks; the first capture still triggers the OS prompt |
| D7 | **Linux done** | `.deb` and AppImage built with udev/desktop/systemd; no signing, no macOS/Windows installers |
| D8 | **Linux done** | Linux productized; Windows/macOS are tested capability stubs |
| D9 | **Done** | Provider interface, openai_compat presets, Ollama, ACP, keychain, role checks, remote-VLM consent |
| D10 | **Done** | Parakeet CPU int8 default, CUDA opt-in; no fallback engine (2026-11-20); pinned downloader/receipt; benchmark recorded |

## D1 — UI stack

**Question:** what owns the main window, tray, settings, onboarding, and overlay?

**Options**

- **PySide6/Qt.** Python-native, mature widgets, tray, accessibility, packaging via PyInstaller/Nuitka. Styling is custom but capable. Overlay click-through remains a per-OS problem.
- **Tauri + web UI + Python sidecar.** Best visual control and animation; web design system; cross-platform shell. Adds Rust/JS, a second runtime, IPC, and a sidecar lifecycle to package and debug.
- **Rust + Rune (Hex's stack).** Excellent polish/performance but effectively a rewrite of the Python engine; not a first-release option.
- **GTK4/libadwaita.** Excellent Linux integration; weaker macOS/Windows fit and packaging.
- **Local web UI in a browser.** No native overlay/tray; not a product surface for this app.

**Recommendation:** **PySide6/Qt for the shared window, tray, settings, onboarding, and history; native overlay per OS.** Use a small token layer (colors, spacing, type, motion) so the UI is consistent and themeable; do not scatter raw colors.

**Why:** one language and one process; Qt covers window/tray/accessibility; packaging is well-trodden; the engine stays Python. Tauri's design advantage is real but the sidecar/IPC cost is high for a first productized release.

**Revisit if:** the UI becomes the main differentiator and Qt cannot reach the desired polish, or if web components/animation are required.

## D2 — Model ownership

**Question:** which models does the app own (pinned download, receipt, offline), and which does the user bring?

**Decision (revised 2026-11-19): split by role.**

| Role | Owner | Default | Why |
|---|---|---|---|
| STT | App-owned | Parakeet TDT 0.6B v2 (ONNX, int8) — see D10 | Small, English, runs on CPU; the layman never installs CUDA or a server. |
| Face landmarks | App-owned | MediaPipe face landmarker (cache) | Already auto-downloaded; add receipt/progress. |
| Text LLM (shell/browser planning, fallback intent) | **Bring your own** | none preselected | Provider-agnostic; see D9. |
| Gaze VLM (screenshot grounding) | **Bring your own**, optional | none; tiers 1–3 (DOM, AT-SPI, OCR) work without it | Needs image input and low latency; see D9. |

The app never bundles an LLM runtime. App-owned assets use the Hex pattern: pinned repo/revision/size/SHA-256, `.partial` resume, atomic promote, `.verified.json` receipt. Provider-owned models (Ollama tags, hosted APIs, agents) are reported by provider `status()`, never stored by the app.

**Why:** STT and landmarks are small, fixed, and needed for the core loop, so the app can guarantee them. LLM/VLM choice is personal (local, hosted, subscription agent), and the runtime is the largest cross-platform packaging burden (CUDA/Metal/Vulkan, licensing, multi-GB downloads).

**Revisit if:** offline install or a portable single-folder build becomes a requirement; then add an app-owned GGUF tier behind the same provider interface. The Linux `.deb`/AppImage already bundle Python and the Python dependencies, but not the LLM runtime or model files.

## D3 — Overlay strategy

**Question:** how do we get a gaze/click/status overlay that never steals focus on every OS?

**Options**

- **Shared logical spec + per-OS native overlay.** macOS `NSPanel`; Windows layered window; Linux depends on session.
- **Linux/GNOME:** (a) GNOME Shell extension (native, versioned), (b) XWayland override-redirect window with input shape (works under XWayland), (c) tray/toast + Firefox HUD only.
- **Single cross-platform toolkit overlay.** Qt/Tauri transparent window; still needs per-OS input-region/focus flags, and GNOME Wayland remains the hard case.

**Recommendation:** **Per-OS native overlay with one shared state model.** For v1 on GNOME Wayland, use a **tray/toast + Firefox HUD** fallback and keep the global overlay behind a capability. Ship the macOS/Windows global overlay when those hosts land. Add a GNOME Shell extension later only if it can be maintained.

**Why:** click-through and focus behavior are OS/compositor properties, not toolkit properties. Promising a global overlay on GNOME Wayland and failing is worse than a clear fallback.

**Revisit if:** GNOME exposes a stable overlay protocol, or the extension becomes maintainable.

## D4 — Data roots

**Recommendation:** split roots and migrate old paths.

| Root | Linux | macOS | Windows |
|---|---|---|---|
| config | `~/.config/keyless` | `~/Library/Application Support/Keyless` | `%APPDATA%\Keyless` |
| data | `~/.local/share/keyless` | `~/Library/Application Support/Keyless` | `%LOCALAPPDATA%\Keyless` |
| cache | `~/.cache/keyless` | `~/Library/Caches/Keyless` | `%LOCALAPPDATA%\Keyless\cache` |
| logs | `~/.local/state/keyless/logs` | `~/Library/Logs/Keyless` | `%LOCALAPPDATA%\Keyless\logs` |

- `KEYLESS_HOME` overrides all roots for tests/portable installs.
- Migrate the current `config.json`, `stereo_calib.json`, and `calib_dataset.npz` once; keep the old files.
- Models are data, not cache; the MediaPipe landmarker is cache.
- Windows: keep large models/logs in `LocalAppData`, not roaming `AppData`.

**Why:** conventions per OS, large files out of roaming profiles, cache clearing does not destroy calibration or models.

## D5 — Camera identity

**Recommendation:** introduce `CameraInfo { id, name, path, capabilities }` and key calibration by a stable pair ID plus monitor, not enumeration order.

- Linux: `/dev/v4l/by-id/*-video-index0` (serial included).
- macOS: AVFoundation `uniqueID`.
- Windows: Media Foundation symbolic link/device path.

- Support a mono model from one camera and a stereo model from two; store `calibration/<pair-hash>-<monitor>.json`.
- Provide "identify camera" (flash/swap) in setup so the user can tell which preview is which.
- Camera controls (exposure/gain/focus) are capability-gated; unsupported controls are reported, not fatal.

**Why:** today's hard-coded C270/CAM313 by-id paths fail for any other unit; a stable ID plus a visible picker is required for a layman.

**Status:** the picker and stable discovery shipped; the default `AppConfig` still starts with the old C270/CAM313 by-id paths until setup saves new ones.

## D6 — Screen-capture permissions

**Recommendation:** `ScreenSource.permission_state()` returns `granted | denied | unknown | needs_restart`; onboarding and `doctor` show it; the first capture requests the OS dialog.

- Linux: GNOME Shell `ScreenshotArea` → XDG portal; denial disables click-by-name and shows a pill.
- macOS: TCC Screen Recording; a grant may require relaunch.
- Windows: Windows.Graphics.Capture, no prompt; show the system capture indicator.

**Why:** today a denied portal surfaces as `Wayland screenshot failed…` at click time. The permission must be visible before the user tries the core action.

**Status:** `doctor` and the capability report show the screen row; capture still asks the desktop at first use. A dedicated preflight request is not implemented.

## D7 — Packaging and signing

**Recommendation:**

- **Linux:** `.deb` + AppImage; include the udev rule, `.desktop`, optional systemd user unit. Flatpak later only if uinput/portal behavior is proven.
- **macOS:** `.app` in a `.dmg`, Developer ID signed and notarized, hardened runtime, usage strings for camera/mic/screen/accessibility/input monitoring. No Mac App Store (global input/accessibility).
- **Windows:** MSI or signed EXE installer; Authenticode; startup task; uninstaller; loopback-only bridge to avoid firewall surprises.

- CI matrix per OS; hardware tests manual; `doctor --json` as the support artifact.

**Why:** the current `uv` + sudo + JSON flow is not a layman install. Packaging is part of the port, not a later task.

## D8 — First cross-platform scope

**Recommendation:** Linux productization first, then Windows, then macOS.

1. **Linux:** UI shell, onboarding, cameras/calibration/models, overlay fallback, packaging.
2. **Windows:** SendInput, low-level hook, Media Foundation, WGC, UIA, tray, layered overlay, signed installer.
3. **macOS:** CGEvent, event tap, AVFoundation, ScreenCaptureKit, AX, TCC, notarized DMG.

Per OS, the minimum cross-platform feature set is: pointer/click/type, PTT, two cameras, audio/STT, screen capture, accessibility, overlay, tray, and first-run setup. Browser DOM (Firefox extension) stays optional.

**Why:** the current code and hardware are Linux; Windows has the larger layman audience; macOS has the heaviest permission/signing work. Revisit the order based on actual users after the Linux release.

## D9 — LLM provider layer (bring your own)

**Question:** how does a user connect an LLM without the app picking one for them?

**Decision:** one `Provider` interface, four adapters, one settings page. Per **role** (D2 table) the user picks a provider + model; roles can use different providers.

| Adapter | Covers | Transport | Notes |
|---|---|---|---|
| `openai_compat` | OpenAI, OpenRouter, Groq, Together, LM Studio, llama.cpp server, vLLM, Ollama `/v1`, Gemini OpenAI-compat endpoint | `httpx` → `POST {base_url}/chat/completions` | One implementation, many presets (base URL + docs link). JSON mode via `response_format`; images as `image_url` parts. |
| `ollama` | Local Ollama | native API | Kept for `status()` / `pull()` / `warm()` progress, which `/v1` lacks. |
| `anthropic` (optional) | Claude API | Messages API | Only if users ask; Claude is also reachable through ACP or OpenRouter. |
| `acp` | Any [Agent Client Protocol](https://agentclientprotocol.com/get-started/introduction) agent | JSON-RPC over stdio to a spawned agent | See below. |

The native Gemini path in `llm_agent.py` is replaced by the `openai_compat` Gemini preset; `GEMINI_API_KEY` stays as an env fallback during migration.

### Provider contract

```
Provider.capabilities -> {text, json_mode, image_input, streaming, latency_class: fast|slow}
Provider.status()     -> ready | needs_auth | not_installed | unreachable | model_missing
Provider.complete(system, user, *, json=False, images=()) -> str
Provider.warm() / Provider.pull(tag, progress)   # optional
```

A role declares what it needs (`gaze_vlm` needs `image_input` and `latency_class=fast`). The settings UI disables providers that cannot satisfy the role and says why. A `complete()` failure degrades to the next deterministic tier; it never blocks pointer/voice commands.

### ACP adapter

ACP is an editor↔coding-agent protocol. Keyless acts as the **client**: it spawns the agent, calls `initialize` → (`authenticate`) → `session/new` → `session/prompt`, and concatenates `agent_message_chunk` updates into the reply.

- **Catalog:** read the [ACP registry](https://agentclientprotocol.com/registry) to populate the picker (launch command: `npx`/`uvx`/binary). Listed agents include Claude (via the `claude-agent-acp` adapter), Codex (`codex-acp`), Gemini CLI (`gemini --acp`), GitHub Copilot, OpenCode, Cline, Kimi CLI, and Factory Droid. Verify the list at build time; do not hard-code it. Ship a small offline fallback list.
- **Auth:** the agent owns it (the user signs in with that agent's own CLI/subscription). The app surfaces `needs_auth` and the agent's `authMethods`; it stores no credential.
- **Safety:** these are coding agents with their own shell/edit tools. Use an empty temp `cwd`; do not advertise client `fs`/`terminal` capabilities; answer every `session/request_permission` with reject; request a read-only/plan mode if the agent offers one. Show an "agent provider" notice in settings.
- **Latency:** keep one warm agent process per provider; seconds per reply is typical. Mark `latency_class=slow`. Allow only for the **text** roles (shell/browser planning, free-form intent). Never use it for the gaze VLM or the click hot path. Use image input only if the agent advertises `promptCapabilities.image`.
- **Library:** prefer the official Python ACP SDK (confirm the package name and version when implementing); fall back to a ~150-line JSON-RPC stdio client if the SDK lags the registry.

### Secrets and privacy

- API keys go in the OS keychain (`keyring`: Secret Service, Keychain, Credential Locker). Never in `settings.json`, logs, or `doctor` output. Env vars (`OPENAI_API_KEY`, etc.) are a fallback.
- Any non-local provider used for the `gaze_vlm` role sends **screenshots** off the machine. Require an explicit per-provider consent toggle and show a persistent "screen leaves this device" indicator. Text roles send only the transcribed command plus a trimmed page/UI summary.
- Local providers (Ollama, LM Studio, llama.cpp on loopback) are marked "stays on this device".

**Why:** one OpenAI-compatible client reaches most hosted and local servers; ACP reaches the subscription agents users already pay for without the app handling their credentials; Ollama keeps first-class local status/pull.

**Revisit if:** ACP gains a lightweight "completion only" profile, or most agents expose an OpenAI-compatible endpoint.

## D10 — Speech-to-text

**Decision:** English-first **NVIDIA Parakeet TDT 0.6B v2** through **`onnx-asr`** (ONNX Runtime). It is the only STT engine; faster-whisper was removed on 2026-11-20 to keep setup and the settings surface simple.

- **Runtime:** `onnx-asr` runs on Windows/Linux/macOS (x86 and Arm) and supports CUDA, TensorRT, DirectML, ROCm, and CoreML execution providers. Its README lists the `nemo-parakeet-tdt-0.6b-v2` model with an `int8` quantized variant.
- **Defaults:** CPU + int8 everywhere. CUDA is opt-in on Linux/Windows; macOS remains CPU. No CUDA toolkit is required.
- **Mode:** offline recognition on push-to-talk release. Utterances are short, so streaming is not required. Keep the existing VAD/endpointing in `active_listener.py`.
- **Throughput:** `onnx-asr` reports ≈36× real-time on a desktop CPU and ≈57× on a T4 for Parakeet v2. Measure on this app's own utterances before claiming a latency win over `base.en`.
- **License/size:** model card is CC-BY-4.0 (attribution in About). Pin repo, revision, bytes, and SHA-256 when adding to the catalog; do not trust the size from this note.
- **Vocabulary:** Parakeet has no `initial_prompt`; the command splitter uses fuzzy matching (`rapidfuzz`) for app names and commands.
- **Multilingual later:** Parakeet v3 is multilingual; switching is a catalog change, not an architecture change.

**Why:** a small, accurate, CPU-viable English model removes the largest first-run friction (CUDA + Hugging Face cache) and fits the "English-centric for now" scope.

**Revisit if:** accuracy on short command phrases regresses, or `onnx-asr` stalls; then `sherpa-onnx` runs the same Parakeet export.

## D11 — Gaze v2: target-level intent, not pixel calibration

**Decision (2026-11-23):** Delete the legacy calibration/regression/online-bias stack and rebuild gaze as a target-level intent model. The eye cursor becomes a rendering of a posterior over candidate targets; the physical mouse becomes a privileged teacher. The model-agnostic seam is `keyless.gaze` (`GazeFrame`, `GazeSource`, `run_gaze_loop`). The old model is tagged `legacy-gaze-v1` for audit only and must not be reused.

- **Objective:** listwise cross-entropy / InfoNCE over candidates, plus a heteroscedastic pixel head for free-space motion.
- **Inputs:** face/eye/head observations over a 0.5–1.5 s window; candidates from AX/OCR/DOM/visual regions with geometry, role, text, and visual embeddings.
- **Labels:** natural clicks (strong), voice confirmations (strong), hover (weak), corrections (hard negatives). "Steer to mouse" is a UI mode switch, never a global anchor.
- **Uncertainty:** temperature scaling, a small ensemble, and split/adaptive conformal sets; abstain or confirm when the top-2 margin is small.
- **Primary metric:** actions per correction. Pixel RMSE is diagnostic only.
- **Kill criteria:** candidate recall <95%, or the target model cannot beat "nearest candidate to the pixel prediction" after a few hundred labels.

**Why:** pixel MSE is not decision-sufficient (two targets can be closer than the error), the old global anchors bypassed every safety gate, and hand-tuned click thresholds encode failure modes the model should learn. Target-level supervision uses the mouse as free, continuous labels and matches the product decision.

**Architecture decision:** do not start with V-JEPA of the screen (wrong dynamics/granularity/compute) or LeJEPA on face landmarks (wrong symmetry: gaze is pose-equivariant, not invariant). Start with a two-tower target-conditioned energy model: small temporal gaze encoder, frozen UI candidate encoder, listwise scorer, pixel head. Optional later: a raw eye-crop encoder pretrained with equivariant objectives, with SIGReg as a regularizer if it improves held-out target accuracy.

**Revisit if:** a V-JEPA screen tower beats the candidate model by >10% top-1 on held-out sessions, or LeJEPA/SIGReg pretraining improves target accuracy by >5% with equivariant augmentations.

See [GREENFIELD.md](GREENFIELD.md) for the full plan, theory, proofs, procedures, and references.
