"""Agent Client Protocol (ACP) provider: JSON-RPC over stdio to a spawned agent.

Safety: the agent runs in a fresh empty temporary cwd, the client advertises no
filesystem or terminal capabilities, and every permission request is rejected.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Sequence

from keyless.models.providers.acp_registry import AcpAgentSpec, resolve_agent_binary
from keyless.models.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderState,
    ProviderStatus,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class AcpError(ProviderError):
    pass


class AcpAuthRequired(AcpError):
    pass


class AcpProcessError(AcpError):
    pass


class AcpClient:
    """Minimal synchronous ACP client with request/notification dispatch."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout: float = 180.0,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = list(command)
        self.timeout = timeout
        self._owns_cwd = cwd is None
        self.cwd = Path(cwd) if cwd is not None else Path(tempfile.mkdtemp(prefix="kk-acp-"))
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._env = {**os.environ, "TERM": "dumb", **(env or {})}
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 0
        self._buffer = b""
        self._stderr: deque[str] = deque(maxlen=80)
        self._stderr_thread: threading.Thread | None = None
        self._session_id: str | None = None
        self._init_result: dict[str, Any] = {}
        self._session_result: dict[str, Any] = {}
        self._chunks: list[str] = []
        self._lock = threading.Lock()

    # --- process lifecycle ------------------------------------------------
    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        resolved = resolve_agent_binary(self.command[0])
        if not resolved:
            raise FileNotFoundError(self.command[0])
        cmd = [resolved, *self.command[1:]]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd),
                env=self._env,
                bufsize=0,
            )
        except OSError as exc:
            raise AcpProcessError(f"Could not start ACP agent: {exc}") from exc
        self._buffer = b""
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in iter(self._proc.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr.append(text)
                logger.debug("ACP stderr: %s", text[:300])

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
        if self._owns_cwd:
            try:
                self.cwd.rmdir()
            except OSError:
                pass

    # --- framing ----------------------------------------------------------
    def _send(self, payload: dict[str, Any]) -> None:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise AcpProcessError("ACP agent is not running.")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except OSError as exc:
            raise AcpProcessError("ACP agent closed its input.") from exc

    def _respond(self, request_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    def _next_message(self, deadline: float) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ACP request timed out.")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError("ACP request timed out.")
            chunk = os.read(fd, 65536)
            if not chunk:
                detail = " | ".join(list(self._stderr)[-3:])
                raise AcpProcessError(f"ACP agent exited ({self._proc.poll()}). {detail}".strip())
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        line = line.strip()
        if not line:
            return self._next_message(deadline)
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Ignoring non-JSON ACP stdout: %s", line[:300])
            return self._next_message(deadline)
        if not isinstance(message, dict):
            return self._next_message(deadline)
        return message

    def _handle_agent_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if method == "session/request_permission":
            options = params.get("options") or []
            reject = next(
                (
                    option
                    for option in options
                    if str(option.get("kind", "")).startswith("reject")
                ),
                None,
            )
            if reject is not None:
                self._respond(
                    request_id,
                    {"outcome": {"outcome": "selected", "optionId": reject.get("optionId")}},
                )
            else:
                self._respond(request_id, {"outcome": {"outcome": "cancelled"}})
            logger.info("Rejected ACP permission request (%s).", method)
            return
        # No fs/terminal capabilities were advertised.  Refuse these anyway.
        self._respond_error(request_id, -32601, f"Client does not support {method}")
        logger.info("Refused ACP agent request: %s", method)

    def _consume_notification(self, message: dict[str, Any]) -> None:
        if message.get("method") != "session/update":
            return
        update = (message.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            self._chunks.append(str(content.get("text", "")))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    self._chunks.append(str(part.get("text", "")))

    def _request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + (timeout or self.timeout)
            while True:
                message = self._next_message(deadline)
                if "method" in message and "id" in message:
                    self._handle_agent_request(message)
                    continue
                if message.get("id") != request_id:
                    self._consume_notification(message)
                    continue
                if "error" in message:
                    error = message["error"] or {}
                    text = str(error.get("message", "ACP error"))
                    if "auth" in text.lower() or "auth_required" in json.dumps(error).lower():
                        raise AcpAuthRequired(text)
                    raise AcpError(f"{method}: {text}")
                return message.get("result")

    # --- protocol ---------------------------------------------------------
    def initialize(self) -> dict[str, Any]:
        self.start()
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {"name": "keyless", "title": "Keyless", "version": "0.1.0"},
            },
        )
        self._init_result = result if isinstance(result, dict) else {}
        version = self._init_result.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise AcpError(f"Agent chose unsupported ACP protocol version {version!r}.")
        return self._init_result

    def new_session(self) -> str:
        if not self._init_result:
            self.initialize()
        result = self._request("session/new", {"cwd": str(self.cwd), "mcpServers": []})
        self._session_result = result if isinstance(result, dict) else {}
        session_id = str(self._session_result.get("sessionId", ""))
        if not session_id:
            raise AcpError("Agent did not return a session id.")
        self._session_id = session_id
        return session_id

    @property
    def config_options(self) -> list[dict[str, Any]]:
        opts = self._session_result.get("configOptions")
        return [o for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []

    @property
    def available_models(self) -> list[dict[str, Any]]:
        m = self._session_result.get("models")
        if isinstance(m, dict) and isinstance(m.get("availableModels"), list):
            return [x for x in m["availableModels"] if isinstance(x, dict)]
        return []

    def set_config_option(self, config_id: str, value: str) -> Any:
        if self._session_id is None:
            self.new_session()
        return self._request(
            "session/set_config_option",
            {"sessionId": self._session_id, "configId": config_id, "value": value},
        )

    @property
    def image_input(self) -> bool:
        capabilities = self._init_result.get("agentCapabilities") or {}
        prompt = capabilities.get("promptCapabilities") or {}
        return bool(prompt.get("image"))

    @property
    def auth_methods(self) -> tuple[str, ...]:
        methods = self._init_result.get("authMethods") or []
        return tuple(str(m.get("id", m.get("type", ""))) for m in methods if isinstance(m, dict))

    def prompt(self, text: str, images: Sequence[bytes] = ()) -> str:
        if self._session_id is None:
            self.new_session()
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if images:
            if not self.image_input:
                raise AcpError("This agent does not advertise image prompt support.")
            for image in images:
                content.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(image).decode("ascii"),
                        "mimeType": "image/jpeg" if image.startswith(b"\xff\xd8") else "image/png",
                    }
                )
        self._chunks = []
        result = self._request(
            "session/prompt",
            {"sessionId": self._session_id, "prompt": content},
            timeout=self.timeout,
        )
        text_out = "".join(self._chunks).strip()
        if not text_out:
            stop = (result or {}).get("stopReason", "")
            raise AcpError(f"Agent returned no text (stopReason={stop!r}).")
        return text_out


class AcpProvider(BaseProvider):
    """Text-only ACP agent; one warm process is kept per provider instance."""

    def __init__(
        self,
        spec: AcpAgentSpec,
        *,
        model: str = "",
        reasoning_effort: str = "",
        timeout: float = 180.0,
        cwd: Path | None = None,
    ) -> None:
        self.spec = spec
        self.id = f"acp:{spec.id}"
        self.display_name = f"{spec.name} (ACP)"
        self.model = (model or "").strip() or "agent-default"
        self.reasoning_effort = (reasoning_effort or "").strip()
        self.timeout = timeout
        self._cwd = cwd
        self._client: AcpClient | None = None
        self._session_configured = False
        self.capabilities = ProviderCapabilities(
            text=True,
            json_mode=False,
            image_input=False,
            streaming=True,
            latency_class="slow",
            local=False,
        )

    def _ensure_client(self) -> AcpClient:
        if self._client is None or not self._client.alive:
            if self._client is not None:
                self._client.close()
            if not self.spec.command:
                raise AcpError("No launch command is published for this agent.")
            self._client = AcpClient(self.spec.command, timeout=self.timeout, cwd=self._cwd)
            self._client.initialize()
            self._session_configured = False
        return self._client

    def _apply_session_preferences(self, client: AcpClient) -> None:
        if self._session_configured:
            return
        if client._session_id is None:
            client.new_session()
        opts = client.config_options
        if self.model and self.model != "agent-default":
            model_opt = next(
                (o for o in opts if o.get("category") == "model" or o.get("id") == "model"),
                None,
            )
            if model_opt is not None:
                try:
                    client.set_config_option(str(model_opt["id"]), self.model)
                except Exception as exc:
                    logger.warning("Could not set ACP model %r: %s", self.model, exc)
        if self.reasoning_effort and self.reasoning_effort != "default":
            eff_opt = next(
                (
                    o
                    for o in opts
                    if o.get("category") == "thought_level"
                    or o.get("id") in ("thought_level", "effort", "reasoning_effort")
                ),
                None,
            )
            if eff_opt is not None:
                allowed = {str(x.get("value")) for x in (eff_opt.get("options") or []) if isinstance(x, dict)}
                if not allowed or self.reasoning_effort in allowed:
                    try:
                        client.set_config_option(str(eff_opt["id"]), self.reasoning_effort)
                    except Exception as exc:
                        logger.warning("Could not set ACP reasoning effort %r: %s", self.reasoning_effort, exc)
        self._session_configured = True

    def status(self) -> ProviderStatus:
        if not self.spec.command:
            return ProviderStatus(
                ProviderState.NOT_INSTALLED,
                "No launch command is published for this agent.",
                model=self.model,
            )
        if resolve_agent_binary(self.spec.command[0]) is None:
            return ProviderStatus(
                ProviderState.NOT_INSTALLED,
                self.spec.install_hint or f"{self.spec.command[0]} is not on PATH.",
                model=self.model,
            )
        try:
            client = self._ensure_client()
            if not client._init_result:
                client.initialize()
            client.new_session()
            self.capabilities = ProviderCapabilities(
                text=True,
                json_mode=False,
                image_input=client.image_input,
                streaming=True,
                latency_class="slow",
                local=False,
            )
            return ProviderStatus(
                ProviderState.READY,
                "Agent initialized in an empty workspace; permissions are rejected.",
                model=self.model,
                auth_methods=client.auth_methods,
            )
        except AcpAuthRequired as exc:
            return ProviderStatus(
                ProviderState.NEEDS_AUTH,
                f"Sign in with the agent's own CLI. {exc}",
                model=self.model,
                auth_methods=self._client.auth_methods if self._client else (),
            )
        except FileNotFoundError:
            return ProviderStatus(
                ProviderState.NOT_INSTALLED,
                f"{self.spec.command[0]} is not on PATH.",
                model=self.model,
            )
        except Exception as exc:
            detail = " | ".join(list(self._client._stderr)[-2:]) if self._client else ""
            return ProviderStatus(
                ProviderState.UNREACHABLE,
                f"Agent failed: {exc} {detail}".strip(),
                model=self.model,
            )

    def complete(
        self,
        system: str,
        user: str,
        *,
        json: bool = False,
        images: Sequence[bytes] = (),
    ) -> str:
        if json:
            user = f"{user}\n\nRespond with valid JSON only."
        combined = f"System instructions:\n{system}\n\nUser request:\n{user}"
        client = self._ensure_client()
        self._apply_session_preferences(client)
        return client.prompt(combined, images=images)

    def warm(self) -> None:
        try:
            client = self._ensure_client()
            client.new_session()
            self._apply_session_preferences(client)
        except Exception as exc:
            logger.info("ACP warm-up failed: %s", exc)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
