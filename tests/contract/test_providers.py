"""Contract tests for provider adapters against fake local servers."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from keyless.models.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderState,
    Role,
    check_provider_for_role,
    ProviderBinding,
)
from keyless.models.providers.ollama import OllamaProvider
from keyless.models.providers.openai_compat import OpenAICompatProvider
from keyless.models.providers.presets import get_preset, load_presets


class _OpenAIHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    api_key = "test-key"

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.api_key}"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"data": [{"id": "fake-model"}]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.requests.append(payload)
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path.endswith("/chat/completions"):
            user = payload["messages"][-1]["content"]
            if isinstance(user, list):
                images = sum(1 for part in user if part.get("type") == "image_url")
                text = f"images={images}"
            else:
                text = f"echo:{user}"
            if payload.get("response_format"):
                text = json.dumps({"action": "click", "args": {"selector": "#go"}})
            self._json(
                200,
                {"choices": [{"message": {"role": "assistant", "content": text}}]},
            )
            return
        self._json(404, {"error": "not found"})

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def openai_server():
    handler = type("Handler", (_OpenAIHandler,), {"requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_presets_cover_hosted_and_loopback_options() -> None:
    presets = {preset.id: preset for preset in load_presets()}
    assert {"openai", "openrouter", "groq", "gemini", "lmstudio", "llama_cpp", "vllm", "ollama_openai"} <= set(
        presets
    )
    assert presets["gemini"].base_url.endswith("/v1beta/openai")
    assert presets["lmstudio"].local
    assert get_preset("openai").requires_key


def test_openai_compat_status_and_completion(openai_server) -> None:
    base, handler = openai_server
    provider = OpenAICompatProvider("openai", "fake-model", base_url=base, api_key="test-key")
    assert provider.status().state is ProviderState.READY
    assert provider.complete("system", "click send") == "echo:click send"
    assert provider.complete("system", "plan", json=True).startswith('{"action"')
    assert provider.complete("system", "look", images=[b"\x89PNG\r\n\x1a\nfake"]) == "images=1"
    assert any(req.get("response_format") == {"type": "json_object"} for req in handler.requests)


def test_openai_compat_reports_auth_and_network_failures(openai_server) -> None:
    base, _ = openai_server
    provider = OpenAICompatProvider("openai", "fake-model", base_url=base, api_key="wrong")
    assert provider.status().state is ProviderState.NEEDS_AUTH
    with pytest.raises(ProviderError):
        provider.complete("system", "hello")

    offline = OpenAICompatProvider(
        "openai", "fake-model", base_url="http://127.0.0.1:9/v1", api_key="test-key"
    )
    assert offline.status().state is ProviderState.UNREACHABLE


def test_openai_compat_model_missing_and_local_no_key() -> None:
    local = OpenAICompatProvider("lmstudio", "")
    assert local.status().state is ProviderState.MODEL_MISSING
    assert local.capabilities.local is True
    hosted = OpenAICompatProvider("openai", "")
    assert hosted.model == "gpt-4o-mini"
    assert hosted.status().state is ProviderState.NEEDS_AUTH


class _OllamaHandler(BaseHTTPRequestHandler):
    pulled = False
    chats: list[dict[str, Any]] = []

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"models": [{"name": "fake:latest"}]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/api/chat"):
            self.chats.append(payload)
            text = json.dumps({"action": "click", "args": {}}) if payload.get("format") else "ok"
            self._json({"message": {"role": "assistant", "content": text}})
            return
        if self.path.endswith("/api/pull"):
            body = (
                json.dumps({"status": "pulling", "completed": 5, "total": 10}) + "\n"
                + json.dumps({"status": "success"})
                + "\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"ok": True})

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def ollama_server():
    handler = type("Handler", (_OllamaHandler,), {"chats": [], "pulled": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_ollama_status_chat_pull_and_warm(ollama_server) -> None:
    base, handler = ollama_server
    provider = OllamaProvider("fake:latest", base_url=base)
    assert provider.status().state is ProviderState.READY
    assert provider.complete("system", "hello") == "ok"
    assert provider.complete("system", "hello", json=True).startswith('{"action"')
    provider.complete("system", "hello", images=[b"\x89PNG\r\n\x1a\nfake"])
    assert "images" in handler.chats[-1]["messages"][-1]
    events: list[dict[str, Any]] = []
    provider.pull("fake:latest", progress=events.append)
    assert events[0]["completed"] == 5
    provider.warm()
    assert handler.chats[0]["messages"][-1]["content"] == "hello"


def test_ollama_model_missing_and_unreachable(ollama_server) -> None:
    base, _ = ollama_server
    assert OllamaProvider("other:latest", base_url=base).status().state is ProviderState.MODEL_MISSING
    assert OllamaProvider("fake:latest", base_url="http://127.0.0.1:9").status().state is ProviderState.UNREACHABLE


class _CapsProvider:
    id = "caps"
    display_name = "Caps"
    model = "m"

    def __init__(self, **caps: Any) -> None:
        self.capabilities = ProviderCapabilities(**caps)

    def status(self):  # pragma: no cover - contract shim
        raise NotImplementedError

    def complete(self, *_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        return ""

    def warm(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_role_capability_checks_and_remote_vlm_consent() -> None:
    fast_local = _CapsProvider(
        image_input=True, latency_class="fast", local=True, json_mode=True
    )
    assert check_provider_for_role(fast_local, Role.GAZE_VLM) == []

    text_only = _CapsProvider(image_input=False, latency_class="fast", local=True)
    assert "image input" in check_provider_for_role(text_only, Role.GAZE_VLM)[0]

    slow = _CapsProvider(image_input=True, latency_class="slow", local=True)
    assert any("fast" in reason for reason in check_provider_for_role(slow, Role.GAZE_VLM))

    remote = _CapsProvider(image_input=True, latency_class="fast", local=False)
    assert any("leave this device" in reason for reason in check_provider_for_role(remote, Role.GAZE_VLM))
    assert check_provider_for_role(remote, Role.GAZE_VLM, remote_vlm_consent=True) == []
    assert ProviderBinding(Role.GAZE_VLM, remote, remote_vlm_consent=False).eligible is False


def test_keychain_secret_resolution_prefers_keychain(monkeypatch) -> None:
    import keyless.models.providers.secrets as secrets

    stored: dict[str, str] = {}

    class FakeKeyring:
        @staticmethod
        def get_password(service: str, name: str) -> str | None:
            return stored.get(f"{service}/{name}")

        @staticmethod
        def set_password(service: str, name: str, value: str) -> None:
            stored[f"{service}/{name}"] = value

        @staticmethod
        def delete_password(service: str, name: str) -> None:
            stored.pop(f"{service}/{name}", None)

    monkeypatch.setattr(secrets, "keyring_available", lambda: True)
    monkeypatch.setattr(secrets, "_keyring", lambda: FakeKeyring)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    assert secrets.resolve_api_key("openai", "OPENAI_API_KEY") == "env-key"
    secrets.set_secret("openai", "keychain-key")
    assert secrets.resolve_api_key("openai", "OPENAI_API_KEY") == "keychain-key"
    secrets.delete_secret("openai")
    assert secrets.resolve_api_key("openai", "OPENAI_API_KEY") == "env-key"


def test_llm_agent_uses_provider_and_keeps_json_helpers() -> None:
    from keyless.routing.llm import LLMAgent

    class FakeProvider:
        id = "fake"
        display_name = "Fake"
        model = "fake-model"
        capabilities = ProviderCapabilities(local=True)

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, bool]] = []

        def status(self):
            raise NotImplementedError

        def complete(self, system: str, user: str, *, json: bool = False, images=()) -> str:
            self.calls.append((system, user, json))
            if json:
                return '{"action": "click", "args": {"selector": "#go"}}'
            if "bash command" in system:
                return "```bash\nls -lh\n```"
            return "ok"

        def warm(self) -> None:
            return None

        def close(self) -> None:
            return None

    provider = FakeProvider()
    agent = LLMAgent(provider=provider)
    assert agent.backend == "fake"
    assert agent.complete("s", "u") == "ok"
    assert agent.generate_shell_command("list files", cwd="/tmp") == "ls -lh"
    assert agent.plan_browser_action("click go")["action"] == "click"
    assert provider.calls[-1][2] is True


def test_llm_agent_resolves_gemini_through_openai_compat(monkeypatch) -> None:
    from keyless.models.providers.ollama import OllamaProvider
    from keyless.models.providers.openai_compat import OpenAICompatProvider
    from keyless.routing.llm import LLMAgent

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert isinstance(LLMAgent(backend="auto").provider, OllamaProvider)

    gemini = LLMAgent(backend="gemini", gemini_api_key="test-key")
    assert isinstance(gemini.provider, OpenAICompatProvider)
    assert gemini.provider.preset.id == "gemini"
    assert gemini.provider.model == "gemini-2.5-flash"


def test_role_resolution_requires_remote_vlm_consent() -> None:
    from keyless.config import AppConfig
    from keyless.models.providers.roles import resolve_gaze_vlm_role

    config = AppConfig(
        gaze_vlm_provider="openai_compat",
        gaze_vlm_preset="openai",
        gaze_vlm_model="gpt-4o-mini",
        gaze_vlm_remote_consent=False,
    )
    resolution = resolve_gaze_vlm_role(config)
    assert not resolution.eligible
    assert any("leave this device" in reason for reason in resolution.reasons)

    config.gaze_vlm_remote_consent = True
    resolution = resolve_gaze_vlm_role(config)
    assert resolution.eligible
