"""PlannerFn adapter bridging VoiceBrowserAgent to keyless's Ollama and OpenAI-compatible providers."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from keyless.browser.agent import AgentAction, PlannerFn

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ProviderEndpoint:
    """Normalized connection details extracted from keyless's models/providers objects."""

    kind: str  # "ollama" or "openai_compat"
    model: str
    base_url: str
    api_key: str = ""
    timeout_sec: float = 35.0
    extra_headers: dict[str, str] | None = None


def endpoint_from_provider(provider: Any, fallback_model: str = "qwen2.5:14b") -> ProviderEndpoint:
    """Adapt an existing keyless provider/role object (ollama.py / openai_compat.py / roles.py)."""
    if isinstance(provider, ProviderEndpoint):
        return provider

    cls_name = type(provider).__name__.lower()
    raw_kind = str(
        getattr(provider, "id", "")
        or getattr(provider, "kind", "")
        or getattr(provider, "provider_type", "")
        or getattr(provider, "backend", "")
        or cls_name
    ).lower()

    is_ollama = "ollama" in raw_kind and "openai" not in cls_name
    model = str(
        getattr(provider, "model", "")
        or getattr(provider, "model_id", "")
        or getattr(provider, "default_model", "")
        or fallback_model
    )
    default_url = "http://127.0.0.1:11434" if is_ollama else "https://api.openai.com/v1"
    base_url = str(
        getattr(provider, "base_url", "")
        or getattr(provider, "endpoint", "")
        or getattr(provider, "host", "")
        or default_url
    ).rstrip("/")

    api_key = str(getattr(provider, "api_key", "") or "")
    if not api_key and callable(getattr(provider, "resolve_api_key", None)):
        api_key = str(provider.resolve_api_key() or "")

    raw_timeout = getattr(provider, "timeout_sec", None) or getattr(provider, "timeout", None)
    timeout_sec = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else 35.0

    return ProviderEndpoint(
        kind="ollama" if is_ollama else "openai_compat",
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_sec=timeout_sec,
    )


def _uses_direct_complete(provider: Any) -> bool:
    """Return True if we should invoke provider.complete() directly instead of raw HTTP."""
    if isinstance(provider, ProviderEndpoint):
        return False
    if not callable(getattr(provider, "complete", None)):
        return False
    # Use provider.complete() for ACP / custom providers without HTTP base_url,
    # or when a provider has an injected httpx test transport.
    if getattr(provider, "_transport", None) is not None:
        return True
    return not bool(
        getattr(provider, "base_url", "")
        or getattr(provider, "endpoint", "")
        or getattr(provider, "host", "")
    )


def _extract_json_payload(raw_text: str) -> str:
    """Strip reasoning tags and markdown code fences to isolate the JSON object."""
    cleaned = _THINK_BLOCK_RE.sub("", raw_text).strip()
    m_fence = _CODE_FENCE_RE.search(cleaned)
    if m_fence:
        cleaned = m_fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_sec: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_ollama_sync(
    ep: ProviderEndpoint,
    system_prompt: str,
    user_prompt: str,
    image_bytes: bytes | None,
    schema: dict[str, Any],
) -> str:
    url = f"{ep.base_url}/api/chat"
    user_msg: dict[str, Any] = {"role": "user", "content": user_prompt}
    if image_bytes:
        user_msg["images"] = [base64.b64encode(image_bytes).decode("ascii")]

    payload: dict[str, Any] = {
        "model": ep.model,
        "stream": False,
        "think": False,
        "format": schema,
        "keep_alive": -1,
        "options": {"temperature": 0.0, "num_predict": 320, "draft_num_predict": 0},
        "messages": [
            {"role": "system", "content": system_prompt},
            user_msg,
        ],
    }
    headers = {"Content-Type": "application/json", **(ep.extra_headers or {})}
    data = _post_json(url, payload, headers, ep.timeout_sec)
    return str(data.get("message", {}).get("content", ""))


def _call_openai_compat_sync(
    ep: ProviderEndpoint,
    system_prompt: str,
    user_prompt: str,
    image_bytes: bytes | None,
    schema: dict[str, Any],
) -> str:
    url = (
        ep.base_url
        if ep.base_url.endswith("/chat/completions")
        else f"{ep.base_url}/chat/completions"
    )

    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content: Any = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    else:
        user_content = user_prompt

    headers = {"Content-Type": "application/json", **(ep.extra_headers or {})}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"

    base_payload: dict[str, Any] = {
        "model": ep.model,
        "temperature": 0.0,
        "max_tokens": 320,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    # Try strict json_schema first; fall back to json_object for OpenAI-compat servers that reject strict schemas
    strict_payload = {
        **base_payload,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "browser_agent_action",
                "strict": True,
                "schema": schema,
            },
        },
    }
    try:
        data = _post_json(url, strict_payload, headers, ep.timeout_sec)
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 422):
            raise
        fallback_payload = {**base_payload, "response_format": {"type": "json_object"}}
        data = _post_json(url, fallback_payload, headers, ep.timeout_sec)

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Empty choices returned from {url}")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def build_browser_planner(
    text_provider: Any,
    vision_provider: Any | None = None,
) -> PlannerFn:
    """Create an async `PlannerFn` backed by keyless's Ollama or OpenAI-compatible providers.

    Pass `vision_provider` (e.g., local Ollama `gemma4:e4b`) if `text_provider` is text-only
    so `inspect_visual` steps automatically route to a vision-capable model.
    """
    vis_obj = vision_provider if vision_provider is not None else text_provider
    text_direct = _uses_direct_complete(text_provider)
    vis_direct = _uses_direct_complete(vis_obj)
    text_ep = None if text_direct else endpoint_from_provider(text_provider)
    vision_ep = None if vis_direct else endpoint_from_provider(vis_obj)
    schema = AgentAction.model_json_schema()

    async def _invoke_once(
        use_vision: bool,
        system_prompt: str,
        user_prompt: str,
        image_bytes: bytes | None,
    ) -> str:
        if use_vision and vis_direct:
            imgs = (image_bytes,) if image_bytes else ()
            return await asyncio.to_thread(
                vis_obj.complete, system_prompt, user_prompt, json=True, images=imgs
            )
        if not use_vision and text_direct:
            return await asyncio.to_thread(
                text_provider.complete, system_prompt, user_prompt, json=True
            )

        ep = vision_ep if use_vision else text_ep
        assert ep is not None
        if ep.kind == "ollama":
            return await asyncio.to_thread(
                _call_ollama_sync, ep, system_prompt, user_prompt, image_bytes, schema
            )
        return await asyncio.to_thread(
            _call_openai_compat_sync, ep, system_prompt, user_prompt, image_bytes, schema
        )

    async def plan(system_prompt: str, user_prompt: str, image_bytes: bytes | None) -> AgentAction:
        use_vision = image_bytes is not None
        raw = await _invoke_once(use_vision, system_prompt, user_prompt, image_bytes)

        try:
            return AgentAction.model_validate_json(_extract_json_payload(raw))
        except ValidationError as first_err:
            # One-shot self-repair pass if the model returned malformed or missing fields
            repair_prompt = (
                f"{user_prompt}\n\n"
                f"Your previous JSON was invalid ({first_err.errors()[0]['msg']}). "
                "Return ONLY valid JSON matching the AgentAction schema."
            )
            raw_retry = await _invoke_once(use_vision, system_prompt, repair_prompt, image_bytes)
            try:
                return AgentAction.model_validate_json(_extract_json_payload(raw_retry))
            except ValidationError:
                return AgentAction(
                    thought="Planner failed to produce valid JSON twice.",
                    tool="done",
                    text="I had trouble reading the page. Please try rephrasing that.",
                )

    return plan
