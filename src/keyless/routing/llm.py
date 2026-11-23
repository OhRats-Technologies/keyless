"""Provider-backed LLM client for Terminal and Browser voice commands."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from keyless.models.providers.acp import AcpProvider
from keyless.models.providers.acp_registry import fetch_registry
from keyless.models.providers.base import Provider, ProviderError
from keyless.models.providers.ollama import OllamaProvider
from keyless.models.providers.openai_compat import OpenAICompatProvider
from keyless.models.providers.secrets import resolve_api_key


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\n(.*?)\n```$", cleaned, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return cleaned.strip("`").strip()


def _acp_provider(agent_id: str, *, timeout: float = 180.0) -> AcpProvider:
    registry = fetch_registry()
    spec = next((item for item in registry if item.id == agent_id), None)
    if spec is None:
        raise ProviderError(f"ACP agent {agent_id!r} is not in the registry.")
    return AcpProvider(spec, timeout=timeout)


def build_text_provider(
    backend: str = "auto",
    *,
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5-coder:7b",
    gemini_model: str = "gemini-2.5-flash",
    gemini_api_key: str | None = None,
    preset: str = "openai",
    model: str = "",
    acp_agent_id: str = "opencode",
    timeout_sec: float = 20.0,
) -> tuple[str, Provider]:
    """Resolve the legacy backend names to provider adapters."""
    if backend == "auto":
        backend = "gemini" if os.environ.get("GEMINI_API_KEY") else "ollama"
    if backend == "ollama":
        return "ollama", OllamaProvider(ollama_model, base_url=ollama_url)
    if backend in ("gemini", "openai_compat", "openai"):
        preset_id = "gemini" if backend == "gemini" else preset
        chosen_model = gemini_model if backend == "gemini" else model
        provider = OpenAICompatProvider(
            preset_id,
            chosen_model,
            api_key=gemini_api_key if backend == "gemini" else None,
        )
        return backend, provider
    if backend == "acp":
        return "acp", _acp_provider(acp_agent_id, timeout=max(120.0, timeout_sec))
    raise ValueError(f"Unsupported LLM backend: {backend!r}")


class LLMAgent:
    """Unified synchronous client over any configured provider."""

    def __init__(
        self,
        backend: str = "auto",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_model: str = "qwen2.5-coder:7b",
        gemini_model: str = "gemini-2.5-flash",
        gemini_api_key: str | None = None,
        timeout_sec: float = 20.0,
        *,
        preset: str = "openai",
        model: str = "",
        acp_agent_id: str = "opencode",
        provider: Provider | None = None,
    ) -> None:
        self.ollama_url = ollama_url.rstrip("/")
        self.ollama_model = ollama_model
        self.gemini_model = gemini_model
        self.timeout_sec = float(timeout_sec)
        if provider is not None:
            self.provider = provider
            self.backend = backend if backend != "auto" else provider.id
        else:
            self.backend, self.provider = build_text_provider(
                backend,
                ollama_url=ollama_url,
                ollama_model=ollama_model,
                gemini_model=gemini_model,
                gemini_api_key=gemini_api_key,
                preset=preset,
                model=model,
                acp_agent_id=acp_agent_id,
                timeout_sec=timeout_sec,
            )

    def complete(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        """Execute a synchronous completion against the configured provider."""
        return self.provider.complete(system_prompt, user_prompt, json=json_mode)

    def answer_question(self, question: str, *, context: str = "") -> str:
        """Generate a concise spoken-friendly response for Keyless voice assistance."""
        system = (
            "You are Keyless, a calm, concise hands-free voice and gaze assistant on Linux. "
            "Answer in 1 to 3 clear spoken sentences using plain text with no markdown formatting, "
            "no bullet lists, and no code fences."
        )
        user = f"Context:\n{context[:2000]}\n\nQuestion: {question}" if context else question
        return _strip_code_fences(self.complete(system, user, json_mode=False))

    def generate_shell_command(
        self,
        instruction: str,
        cwd: str | None = None,
        files_preview: list[str] | None = None,
    ) -> str:
        """Translate natural-language terminal request into a single bash command."""
        cur_dir = cwd or os.getcwd()
        if files_preview is None:
            try:
                files_preview = sorted(os.listdir(cur_dir))[:30]
            except OSError:
                files_preview = []
        system = (
            "You translate voice requests into a single concise Linux bash command. "
            "Return ONLY the raw bash command on one line with no explanation or markdown."
        )
        user = (
            f"Working directory: {cur_dir}\n"
            f"Files: {', '.join(files_preview)}\n"
            f"Request: {instruction}"
        )
        return _strip_code_fences(self.complete(system, user, json_mode=False))

    def plan_browser_action(
        self,
        instruction: str,
        gaze_xy: tuple[float, float] | None = None,
        dom_snapshot: str = "",
    ) -> dict[str, Any]:
        """Generate a structured Firefox extension tool call payload."""
        system = (
            "You control a Firefox WebExtension via structured JSON tool calls. "
            "Respond strictly with JSON containing keys `action` and `args`. "
            "Allowed actions: `click_at_gaze`, `click_selector`, `fill_input`, "
            "`summarize_near_gaze`, `search_tabs`, `scroll`."
        )
        gaze_txt = f"({gaze_xy[0]:.0f}, {gaze_xy[1]:.0f})" if gaze_xy else "unknown"
        user = (
            f"Gaze screen coordinates: {gaze_txt}\n"
            f"DOM snippet:\n{dom_snapshot[:4000]}\n\n"
            f"User request: {instruction}"
        )
        raw = _strip_code_fences(self.complete(system, user, json_mode=True))
        return dict(json.loads(raw))

    def route_voice_intent(
        self,
        transcript: str,
        *,
        active_app: str = "",
    ) -> dict[str, Any]:
        """Classify a natural-language voice utterance into a structured desktop intent JSON."""
        system = (
            "You are the intent router for Keyless, a Linux hands-free gaze+voice controller. "
            "Given a spoken utterance (which may contain ASR typos like 'geeless' for 'keyless'), "
            "return ONLY valid JSON with key `intent` and any relevant fields.\n"
            "Allowed intents:\n"
            "- `click_target`: {\"intent\": \"click_target\", \"target\": \"<short UI element label>\", \"button\": \"left|right|double\"}\n"
            "- `click_here`: {\"intent\": \"click_here\", \"button\": \"left|right|double\"}\n"
            "- `switch_tab`: {\"intent\": \"switch_tab\", \"target\": \"<tab name or number>\"}\n"
            "- `focus_app`: {\"intent\": \"focus_app\", \"target\": \"<app name>\"}\n"
            "- `next_window` / `prev_window` / `close_window` / `minimize_window` / `maximize_window`\n"
            "- `scroll`: {\"intent\": \"scroll\", \"direction\": \"down|up|left|right|top|bottom\", \"amount\": \"small|normal|large\"}\n"
            "- `type_text`: {\"intent\": \"type_text\", \"text\": \"<literal text>\"}\n"
            "- `type_into`: {\"intent\": \"type_into\", \"target\": \"<field name>\", \"text\": \"<literal text>\"}\n"
            "- `key_press`: {\"intent\": \"key_press\", \"key\": \"enter|escape|tab|space|backspace|copy|paste|undo|redo\"}\n"
            "- `media`: {\"intent\": \"media\", \"action\": \"play|pause|mute|unmute|fullscreen|exit_fullscreen\"}\n"
            "- `terminal`: {\"intent\": \"terminal\", \"instruction\": \"<shell request>\"}\n"
            "- `ask`: {\"intent\": \"ask\", \"question\": \"<question>\"}\n"
            "- `ignore`: {\"intent\": \"ignore\"} ONLY if clearly casual conversation not aimed at the computer.\n"
            "Prefer `click_target` when the user wants to press/click a named button, link, or tab."
        )
        ctx = f"Active app: {active_app}\n" if active_app else ""
        user = f"{ctx}Spoken utterance: {transcript}"
        raw = _strip_code_fences(self.complete(system, user, json_mode=True))
        return dict(json.loads(raw))

    @property
    def api_key_configured(self) -> bool:
        if isinstance(self.provider, OpenAICompatProvider):
            return bool(resolve_api_key(self.provider.id, self.provider.preset.api_key_env))
        return False

    def close(self) -> None:
        self.provider.close()
