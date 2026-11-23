"""Bind providers to the text-LLM and gaze-VLM roles with capability checks."""

from __future__ import annotations

import os
from dataclasses import dataclass

from keyless.config.schema import AppConfig
from keyless.models.providers.acp import AcpProvider
from keyless.models.providers.acp_registry import fetch_registry
from keyless.models.providers.base import (
    Provider,
    ProviderBinding,
    ProviderState,
    Role,
    check_provider_for_role,
)
from keyless.models.providers.ollama import OllamaProvider
from keyless.models.providers.openai_compat import OpenAICompatProvider
from keyless.models.providers.secrets import get_secret


@dataclass
class RoleResolution:
    role: Role
    provider: Provider
    reasons: list[str]

    @property
    def eligible(self) -> bool:
        return not self.reasons


def _acp(agent_id: str, timeout: float = 180.0) -> AcpProvider:
    specs = fetch_registry()
    spec = next((item for item in specs if item.id == agent_id), None)
    if spec is None:
        raise KeyError(f"ACP agent {agent_id!r} is not in the registry.")
    return AcpProvider(spec, timeout=timeout)


def resolve_text_role(config: AppConfig) -> RoleResolution:
    backend = config.llm_provider if (config.llm_provider or "auto") != "auto" else config.llm_backend
    if backend == "auto":
        backend = "gemini" if (os.environ.get("GEMINI_API_KEY") or get_secret("gemini")) else "ollama"
    if backend in ("gemini", "openai_compat", "openai"):
        preset = "gemini" if backend == "gemini" else (config.llm_preset or "openai")
        model = config.gemini_model if backend == "gemini" else (config.llm_model or "")
        if preset == "ollama_openai" and not model:
            model = config.gaze_vlm_model or config.ollama_model or "gemma4:e4b"
        provider: Provider = OpenAICompatProvider(preset, model)
    elif backend == "acp":
        provider = _acp(config.acp_agent_id)
    else:
        chosen = config.llm_model or config.ollama_model
        provider = OllamaProvider(chosen)
        st = provider.status()
        if st.state is ProviderState.MODEL_MISSING:
            avail = list(st.extra.get("models") or [])
            if config.gaze_vlm_model and config.gaze_vlm_model in avail:
                provider = OllamaProvider(config.gaze_vlm_model)
            elif avail:
                provider = OllamaProvider(str(avail[0]))
    return RoleResolution(Role.TEXT_LLM, provider, check_provider_for_role(provider, Role.TEXT_LLM))


def resolve_gaze_vlm_role(config: AppConfig) -> RoleResolution:
    backend = (config.gaze_vlm_provider or "auto").lower()
    if backend == "auto":
        backend = "ollama"
    if backend == "openai_compat":
        provider: Provider = OpenAICompatProvider(config.gaze_vlm_preset, config.gaze_vlm_model)
    elif backend == "acp":
        provider = _acp(config.acp_agent_id)
    else:
        provider = OllamaProvider(config.gaze_vlm_model)
    reasons = check_provider_for_role(
        provider,
        Role.GAZE_VLM,
        remote_vlm_consent=config.gaze_vlm_remote_consent,
    )
    return RoleResolution(Role.GAZE_VLM, provider, reasons)


def binding_for(config: AppConfig, role: Role) -> ProviderBinding:
    resolution = resolve_text_role(config) if role is Role.TEXT_LLM else resolve_gaze_vlm_role(config)
    return ProviderBinding(
        role=role,
        provider=resolution.provider,
        model=getattr(resolution.provider, "model", ""),
        remote_vlm_consent=config.gaze_vlm_remote_consent,
    )
