"""Typed metadata for every ``AppConfig`` field.

One ``SettingSpec`` describes one setting for every shell: its label, group,
editor control, valid range/options, and whether a change needs a daemon
restart. The control plane derives its allow-list and validation from this
registry, so a field added to ``AppConfig`` without a spec fails the coverage
test instead of becoming silently unpatchable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from keyless.config.schema import (
    DEFAULT_CAM1_ID,
    DEFAULT_CAM2_ID,
    AppConfig,
)

ALL_PLATFORMS: tuple[str, ...] = ("all",)

# Verified against the code that consumes them; see the module docstring of
# each consumer before changing these sets.
_CAMERA_MODES = ("auto", "mono", "stereo")
_CAMERA_BACKENDS = ("v4l2", "pipewire")
_ACTION_BACKENDS = ("native", "cua")
_STT_QUANTIZATIONS = ("q8", "int8")
_STT_PROVIDERS = ("cpu", "cuda")
_STT_MODELS = ("parakeet-unified-en-0.6b", "parakeet-tdt-0.6b-v2")
_LLM_BACKENDS = ("auto", "ollama", "gemini", "openai_compat", "acp")
_LLM_PROVIDERS = ("auto", "ollama", "gemini", "openai_compat", "acp")
_LLM_PRESETS = (
    "openai",
    "openrouter",
    "groq",
    "together",
    "gemini",
    "lmstudio",
    "llama_cpp",
    "vllm",
    "ollama_openai",
)
_GAZE_VLM_PROVIDERS = ("auto", "ollama", "openai_compat", "acp")
_UPDATE_CHANNELS = ("stable", "prerelease")


@dataclass(frozen=True)
class SettingSpec:
    """One editable setting, shared by the Qt settings pane and HTTP clients."""

    key: str
    value_type: str
    default: object
    label: str
    help_text: str
    group: str
    control: str
    options: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    apply: str = "live"
    platforms: tuple[str, ...] = field(default=ALL_PLATFORMS)

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "value_type": self.value_type,
            "default": self.default,
            "label": self.label,
            "help_text": self.help_text,
            "group": self.group,
            "control": self.control,
            "options": list(self.options) if self.options is not None else None,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "apply": self.apply,
            "platforms": list(self.platforms),
        }


def _bool(key: str, default: bool, label: str, help_text: str, group: str, *, apply: str = "live", platforms: tuple[str, ...] = ALL_PLATFORMS) -> SettingSpec:
    return SettingSpec(key, "bool", default, label, help_text, group, "toggle", None, None, None, apply, platforms)


def _int(key: str, default: int, label: str, help_text: str, group: str, *, minimum: float, maximum: float, apply: str = "live", platforms: tuple[str, ...] = ALL_PLATFORMS) -> SettingSpec:
    return SettingSpec(key, "int", default, label, help_text, group, "number", None, minimum, maximum, apply, platforms)


def _float(key: str, default: float, label: str, help_text: str, group: str, *, minimum: float, maximum: float, apply: str = "live", platforms: tuple[str, ...] = ALL_PLATFORMS) -> SettingSpec:
    return SettingSpec(key, "float", default, label, help_text, group, "number", None, minimum, maximum, apply, platforms)


def _select(key: str, default: str, label: str, help_text: str, group: str, options: tuple[str, ...], *, apply: str = "live", platforms: tuple[str, ...] = ALL_PLATFORMS) -> SettingSpec:
    return SettingSpec(key, "str", default, label, help_text, group, "select", options, None, None, apply, platforms)


def _text(key: str, default: str, label: str, help_text: str, group: str, *, control: str = "text", apply: str = "live", platforms: tuple[str, ...] = ALL_PLATFORMS) -> SettingSpec:
    return SettingSpec(key, "str", default, label, help_text, group, control, None, None, None, apply, platforms)


SETTING_SPECS: tuple[SettingSpec, ...] = (
    # --- Cameras -------------------------------------------------------
    _select("camera_mode", "auto", "Camera setup", "Automatic picks one or two cameras; mono and stereo force the count.", "Cameras", _CAMERA_MODES, apply="restart"),
    _select("camera_backend", "v4l2", "Camera backend", "PipeWire shares a camera that another app already opened.", "Cameras", _CAMERA_BACKENDS, apply="restart", platforms=("linux",)),
    _text("cam1_by_id", DEFAULT_CAM1_ID, "Primary camera", "Stable by-id path for the first camera.", "Cameras", control="path", apply="restart"),
    _text("cam2_by_id", DEFAULT_CAM2_ID, "Secondary camera", "Stable by-id path for the optional second camera.", "Cameras", control="path", apply="restart"),
    _bool("cam_auto_exposure", True, "Automatic exposure", "Adapt exposure and gain to room light instead of using fixed values.", "Cameras", apply="restart"),
    # --- Voice & audio -------------------------------------------------
    _text("ptt_hotkey", "KEY_RIGHTCTRL", "Push-to-talk key", "Key name that starts and stops listening.", "Voice & audio"),
    _select("stt_model_id", "parakeet-unified-en-0.6b", "Speech model", "Pinned offline speech-recognition model.", "Voice & audio", _STT_MODELS, apply="restart"),
    _select("stt_quantization", "q8", "Speech precision", "Quantization used by the local speech model.", "Voice & audio", _STT_QUANTIZATIONS, apply="restart"),
    _select("stt_provider", "cpu", "Speech device", "Run the speech model on CPU or an available CUDA device.", "Voice & audio", _STT_PROVIDERS, apply="restart"),
    SettingSpec("audio_device", "int_or_str_or_none", None, "Microphone", "Device index, device name, or null for the system default.", "Voice & audio", "text"),
    _bool("always_on_voice", True, "Always-on voice", "Listen for commands without holding the push-to-talk key.", "Voice & audio"),
    _bool("enable_pop_click", False, "Mouth-pop clicks", "Treat a detected mouth pop as a click.", "Voice & audio"),
    _bool("require_command_prefix", True, "Require command prefix", "Only route speech that starts with a command word.", "Voice & audio"),
    _bool("tts_enabled", False, "Spoken replies", "Speak short replies through the local voice.", "Voice & audio"),
    _text("tts_voice", "juno", "Voice", "Local text-to-speech voice name.", "Voice & audio"),
    _bool("tts_onboarding_help", True, "Speak setup hints", "Read onboarding hints aloud.", "Voice & audio"),
    _bool("tts_speak_replies", True, "Speak every reply", "Speak assistant replies instead of showing them only.", "Voice & audio"),
    # --- Language model ------------------------------------------------
    _select("llm_backend", "auto", "Backend", "Preferred text-model backend.", "Language model", _LLM_BACKENDS),
    _select("llm_provider", "auto", "Provider", "Explicit provider; automatic detects from the environment.", "Language model", _LLM_PROVIDERS),
    _select("llm_preset", "openai", "API preset", "OpenAI-compatible endpoint preset.", "Language model", _LLM_PRESETS),
    _text("llm_model", "", "Model", "Model id for the selected provider; empty uses the preset default.", "Language model"),
    _text("acp_agent_id", "opencode", "ACP agent", "Agent id used when the provider is acp.", "Language model"),
    _text("ollama_model", "qwen2.5-coder:7b", "Ollama model", "Default local Ollama model.", "Language model"),
    _text("gemini_model", "gemini-2.5-flash", "Gemini model", "Default Gemini model.", "Language model"),
    # --- Gaze & cursor -------------------------------------------------
    _bool("gaze_cursor_control", False, "Gaze cursor control", "Let gaze move the pointer toward the attended target.", "Gaze & cursor"),
    _bool("gaze_collect_enabled", False, "Record gaze sessions", "Log local session-keyed clicks, candidates, and observations for Gaze v2 replay.", "Gaze & cursor", apply="restart"),
    _bool("gaze_collect_redact_text", False, "Redact recorded text", "Replace candidate text and spoken transcripts with [redacted] in recorded sessions.", "Gaze & cursor", apply="restart"),
    _bool("gaze_cursor_overlay", True, "Eye cursor overlay", "Draw a click-through eye reticle overlay on Linux (Wayland) and macOS instead of moving the real pointer.", "Gaze & cursor", platforms=("linux", "macos")),
    _bool("gaze_vlm_enabled", True, "Visual grounding", "Use a vision model to ground spoken references on screen.", "Gaze & cursor"),
    _select("gaze_vlm_provider", "auto", "Visual provider", "Provider used for visual grounding.", "Gaze & cursor", _GAZE_VLM_PROVIDERS),
    _select("gaze_vlm_preset", "openai", "Visual API preset", "OpenAI-compatible endpoint preset for visual grounding.", "Gaze & cursor", _LLM_PRESETS),
    _text("gaze_vlm_model", "gemma4:e4b", "Visual model", "Model used for visual grounding.", "Gaze & cursor"),
    _bool("gaze_vlm_remote_consent", False, "Allow remote screen crops", "Permit sending screen crops to a remote model.", "Gaze & cursor"),
    # --- Privacy & updates ---------------------------------------------
    _bool("auto_update_checks", True, "Check for updates", "Look for new releases in the background.", "Privacy & updates"),
    _select("update_channel", "stable", "Update channel", "Stable builds only, or include prereleases.", "Privacy & updates", _UPDATE_CHANNELS),
    _text("skipped_update_version", "", "Skipped version", "Version the user asked not to be reminded about.", "Privacy & updates"),
    # --- Developer -----------------------------------------------------
    _bool("dev_active_monitor_border", False, "Active monitor border", "Draw a green border around the monitor you are actively looking at (Wayland overlay).", "Developer", platforms=("linux",)),
    # --- Advanced ------------------------------------------------------
    _bool("transcript_overlay", False, "Transcript overlay", "Show a floating transcript while listening.", "Advanced"),
    _select("action_backend", "native", "Input backend", "Native input injection or the pinned cua-driver binary.", "Advanced", _ACTION_BACKENDS, apply="restart"),
    _int("frame_width", 1280, "Frame width", "Capture width in pixels.", "Advanced", minimum=160, maximum=7680, apply="restart"),
    _int("frame_height", 720, "Frame height", "Capture height in pixels.", "Advanced", minimum=120, maximum=4320, apply="restart"),
    _int("fps", 30, "Capture rate", "Frames per second.", "Advanced", minimum=1, maximum=240, apply="restart"),
    _int("cam1_exposure", 300, "Primary exposure", "Fixed exposure for the first camera when auto exposure is off.", "Advanced", minimum=0, maximum=100000, apply="restart", platforms=("linux",)),
    _int("cam1_gain", 180, "Primary gain", "Fixed gain for the first camera when auto exposure is off.", "Advanced", minimum=0, maximum=10000, apply="restart", platforms=("linux",)),
    _int("cam2_exposure", 300, "Secondary exposure", "Fixed exposure for the second camera when auto exposure is off.", "Advanced", minimum=0, maximum=100000, apply="restart", platforms=("linux",)),
    _int("cam2_gain", 160, "Secondary gain", "Fixed gain for the second camera when auto exposure is off.", "Advanced", minimum=0, maximum=10000, apply="restart", platforms=("linux",)),
    _float("blink_threshold", 0.55, "Blink threshold", "Eye-aspect ratio below which a blink is counted.", "Advanced", minimum=0.0, maximum=1.0, apply="restart"),
    _bool("use_gpu", True, "Use GPU", "Allow GPU inference where available.", "Advanced", apply="restart"),
    _float("pop_min_peak", 0.25, "Pop peak floor", "Minimum peak amplitude for a mouth-pop click.", "Advanced", minimum=0.0, maximum=1.0, apply="restart"),
    _float("pop_min_rms", 0.032, "Pop RMS floor", "Minimum RMS energy for a mouth-pop click.", "Advanced", minimum=0.0, maximum=1.0, apply="restart"),
    _int("gaze_crop_w", 680, "Gaze crop width", "Width of the crop sent to the visual model.", "Advanced", minimum=64, maximum=4096, apply="restart"),
    _int("gaze_crop_h", 480, "Gaze crop height", "Height of the crop sent to the visual model.", "Advanced", minimum=64, maximum=4096, apply="restart"),
    _float("gaze_keyword_threshold", 82.0, "Gaze keyword threshold", "Score required to ground a spoken keyword.", "Advanced", minimum=0.0, maximum=100.0, apply="restart"),
    _bool("firefox_bridge_enabled", True, "Firefox bridge", "Enable the local Firefox automation bridge.", "Advanced", apply="restart"),
    _int("firefox_bridge_port", 8765, "Firefox bridge port", "Loopback port for the Firefox bridge.", "Advanced", minimum=1, maximum=65535, apply="restart"),
    _bool("playwright_browser_enabled", True, "Browser agent", "Run the visible Playwright Firefox browser agent for spoken browser tasks.", "Advanced", apply="restart"),
    _bool("playwright_auto_seed_firefox", True, "Seed browser profile", "Copy the Firefox profile into the browser agent's profile on first run.", "Advanced"),
)

_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTING_SPECS}


def specs_by_key() -> dict[str, SettingSpec]:
    """Return the registry keyed by setting name."""
    return dict(_BY_KEY)


def spec_for(key: str) -> SettingSpec | None:
    """Return the spec for ``key``, or ``None`` for an unknown setting."""
    return _BY_KEY.get(key)


def _type_error(spec: SettingSpec, value: object) -> str | None:
    if spec.value_type == "bool":
        if not isinstance(value, bool):
            return "expected a boolean"
        return None
    if spec.value_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected an integer"
        return None
    if spec.value_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "expected a number"
        # NaN compares false against every bound, so range checks cannot catch
        # it; reject non-finite numbers here before they reach disk.
        if not math.isfinite(value):
            return "expected a finite number"
        return None
    if spec.value_type == "str":
        if not isinstance(value, str):
            return "expected a string"
        return None
    if spec.value_type == "int_or_str_or_none":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return "expected an integer, a string, or null"
        return None
    return f"unsupported value type {spec.value_type!r}"


def validate_patch(patch: dict[str, object]) -> dict[str, str]:
    """Validate a partial config update; returns ``{key: message}`` for bad keys."""
    errors: dict[str, str] = {}
    for key, value in patch.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            errors[key] = "unknown setting"
            continue
        type_error = _type_error(spec, value)
        if type_error is not None:
            errors[key] = type_error
            continue
        if spec.options is not None and value not in spec.options:
            errors[key] = f"expected one of {', '.join(spec.options)}"
            continue
        if spec.minimum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < spec.minimum:
                errors[key] = f"must be at least {spec.minimum:g}"
                continue
        if spec.maximum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > spec.maximum:
                errors[key] = f"must be at most {spec.maximum:g}"
                continue
    return errors


def _assert_registry_matches_schema() -> None:
    fields = AppConfig.__dataclass_fields__
    keys = [spec.key for spec in SETTING_SPECS]
    if len(keys) != len(set(keys)):
        raise RuntimeError("duplicate setting keys in SETTING_SPECS")
    missing = sorted(set(fields) - set(keys))
    extra = sorted(set(keys) - set(fields))
    if missing or extra:
        raise RuntimeError(f"setting registry drift: missing={missing} extra={extra}")


_assert_registry_matches_schema()
