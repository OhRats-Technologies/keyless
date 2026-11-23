"""Local Ollama provider with status, pull, and warm progress."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Sequence

import httpx

from keyless.models.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderState,
    ProviderStatus,
)

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaProvider(BaseProvider):
    id = "ollama"
    display_name = "Ollama (local)"
    capabilities = ProviderCapabilities(
        text=True,
        json_mode=True,
        image_input=True,
        streaming=True,
        latency_class="fast",
        local=True,
    )

    def __init__(
        self,
        model: str = "",
        *,
        base_url: str = OLLAMA_DEFAULT_URL,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=timeout or self.timeout,
            transport=self._transport,
        )

    def list_models(self) -> list[str]:
        with self._client(timeout=2.0) as client:
            response = client.get("/api/tags")
        response.raise_for_status()
        return [str(item.get("name", "")) for item in response.json().get("models", [])]

    def status(self) -> ProviderStatus:
        try:
            names = self.list_models()
        except httpx.HTTPError as exc:
            return ProviderStatus(
                ProviderState.UNREACHABLE,
                f"Ollama is not reachable at {self.base_url}.",
                model=self.model,
            )
        if not self.model:
            return ProviderStatus(
                ProviderState.MODEL_MISSING,
                "Choose or pull a model.",
                model=self.model,
                extra={"models": names},
            )
        if self.model not in names and f"{self.model}:latest" not in names:
            return ProviderStatus(
                ProviderState.MODEL_MISSING,
                f"Model {self.model!r} is not pulled.",
                model=self.model,
                extra={"models": names},
            )
        return ProviderStatus(ProviderState.READY, "Ollama is running.", model=self.model, extra={"models": names})

    def complete(
        self,
        system: str,
        user: str,
        *,
        json: bool = False,
        images: Sequence[bytes] = (),
    ) -> str:
        import base64

        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = [base64.b64encode(image).decode("ascii") for image in images]
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": False,
            "options": {"draft_num_predict": 0},
            "messages": [
                {"role": "system", "content": system},
                message,
            ],
        }
        if json:
            payload["format"] = "json"
        try:
            with self._client() as client:
                response = client.post("/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama request failed: {type(exc).__name__}") from exc
        try:
            return str(data["message"]["content"]).strip()
        except (KeyError, TypeError) as exc:
            raise ProviderError("Ollama returned an unexpected response.") from exc

    def warm(self) -> None:
        if not self.model:
            return
        with self._client(timeout=30.0) as client:
            client.post(
                "/api/generate",
                json={
                    "model": self.model,
                    "prompt": "",
                    "stream": False,
                    "think": False,
                    "keep_alive": "10m",
                    "options": {"draft_num_predict": 0},
                },
            )

    def pull(
        self,
        tag: str,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        with self._client(timeout=None) as client:
            with client.stream("POST", "/api/pull", json={"name": tag, "stream": True}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if progress is not None:
                        progress(event)
                    if event.get("error"):
                        raise ProviderError(str(event["error"]))
