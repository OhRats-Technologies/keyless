"""Configuration schema and migration-aware persistence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from keyless.config.migrations import ensure_migrated
from keyless.config.paths import current_paths, default_paths


DEFAULT_CAM1_ID = "/dev/v4l/by-id/usb-046d_0825_4BEC6610-video-index0"
DEFAULT_CAM2_ID = (
    "/dev/v4l/by-id/usb-Sunplus_IT_Co_Live_Streamer_CAM_313_20200529002-video-index0"
)

_PATHS = default_paths()

CONFIG_DIR = _PATHS.config_dir
CACHE_DIR = _PATHS.cache_dir

# Kept as import-time constants for CLI option defaults and existing imports.
# New code should use `current_paths()` so `KEYLESS_HOME` applies.
DEFAULT_CONFIG_PATH = _PATHS.settings_path
DEFAULT_CALIB_PATH = _PATHS.calibration_path
DEFAULT_LANDMARKER_MODEL_PATH = _PATHS.landmarker_path

FACE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)


@dataclass
class AppConfig:
    """Persistent user configuration for keyless."""

    camera_mode: str = "auto"
    camera_backend: str = "v4l2"
    transcript_overlay: bool = False
    cam1_by_id: str = DEFAULT_CAM1_ID
    cam2_by_id: str = DEFAULT_CAM2_ID
    frame_width: int = 1280
    frame_height: int = 720
    fps: int = 30
    cam1_exposure: int = 300
    cam1_gain: int = 180
    cam2_exposure: int = 300
    cam2_gain: int = 160
    saccade_threshold_px: float = 110.0
    saccade_hold_ms: float = 16.0
    gimbal_gain: float = 2800.0
    one_euro_min_cutoff: float = 1.4
    one_euro_beta: float = 0.008
    blink_threshold: float = 0.55
    use_gpu: bool = True
    # Phase 2: audio pop-click, speech-to-text, and LLM routing
    ptt_hotkey: str = "KEY_RIGHTCTRL"
    # D10: Parakeet is the only STT engine.
    stt_model_id: str = "parakeet-tdt-0.6b-v2"
    stt_quantization: str = "int8"
    stt_provider: str = "cpu"
    llm_backend: str = "auto"
    llm_provider: str = "auto"
    llm_preset: str = "openai"
    llm_model: str = ""
    acp_agent_id: str = "opencode"
    ollama_model: str = "qwen2.5-coder:7b"
    gemini_model: str = "gemini-2.5-flash"
    pop_min_peak: float = 0.25
    pop_min_rms: float = 0.032
    audio_device: int | str | None = None
    # Phase 3: Gaze-Foveated Voice + Vision Grounding & Always-On VAD
    gaze_model: str = "rbf"
    gaze_online_ekf: bool = False
    gaze_online_chase: bool = False
    gaze_cursor_control: bool = False
    gaze_vlm_enabled: bool = True
    gaze_vlm_provider: str = "auto"
    gaze_vlm_preset: str = "openai"
    gaze_vlm_model: str = "gemma4:e4b"
    gaze_vlm_remote_consent: bool = False
    gaze_crop_w: int = 680
    gaze_crop_h: int = 480
    gaze_keyword_threshold: float = 82.0
    harvest_calib: bool = True
    always_on_voice: bool = True
    enable_pop_click: bool = False
    require_command_prefix: bool = True
    attention_max_yaw_deg: float = 38.0
    firefox_bridge_enabled: bool = True
    firefox_bridge_port: int = 8765
    # Phase 4: Local Underdog-Woof TTS & t3code-style update checks
    tts_enabled: bool = False
    tts_voice: str = "juno"
    tts_onboarding_help: bool = True
    tts_speak_replies: bool = True
    auto_update_checks: bool = True
    update_channel: str = "stable"
    skipped_update_version: str = ""

    def save(self, path: Path | None = None) -> None:
        target = Path(path) if path is not None else current_paths().settings_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        if path is None:
            paths = current_paths()
            try:
                ensure_migrated(paths)
            except OSError:
                # A read-only config directory must not prevent startup; the legacy
                # file below is still readable.
                pass
            target = paths.settings_path
            if not target.exists() and paths.legacy_settings_path.exists():
                target = paths.legacy_settings_path
        else:
            target = Path(path)
        if not target.exists():
            return cls()
        data = json.loads(target.read_text())
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

