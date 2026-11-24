"""ACP registry fetch with a small offline fallback list."""

from __future__ import annotations

import json
import logging
import os
import platform as _host_platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from keyless.config.paths import AppPaths, current_paths

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
CACHE_FILE = "acp-registry.json"


@dataclass(frozen=True)
class AcpAgentSpec:
    id: str
    name: str
    description: str = ""
    command: tuple[str, ...] = ()
    repository: str = ""
    source: str = "registry"
    installed: bool = False
    install_hint: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "command": list(self.command),
            "repository": self.repository,
            "source": self.source,
            "installed": self.installed,
            "install_hint": self.install_hint,
        }


# Curated, minimal offline list.  These are launch commands, not bundled code.
# The retired "Gemini (agy)" entry was removed: its launch command is not a
# real ACP agent surface, so it only produced dead pickers.
OFFLINE_AGENTS: tuple[AcpAgentSpec, ...] = (
    AcpAgentSpec(
        "opencode",
        "OpenCode",
        "Open-source coding agent",
        ("opencode", "acp"),
        "https://github.com/anomalyco/opencode",
        source="offline",
        install_hint="Install the OpenCode CLI and run `opencode auth login`.",
    ),
    AcpAgentSpec(
        "claude-acp",
        "Claude Agent",
        "ACP wrapper for Anthropic's Claude",
        ("npx", "-y", "@agentclientprotocol/claude-agent-acp"),
        "https://github.com/agentclientprotocol/claude-agent-acp",
        source="offline",
        install_hint="Requires Node.js and a Claude subscription or API key.",
    ),
    AcpAgentSpec(
        "codex-acp",
        "Codex",
        "ACP adapter for OpenAI's coding assistant",
        ("npx", "-y", "@agentclientprotocol/codex-acp"),
        "https://github.com/agentclientprotocol/codex-acp",
        source="offline",
        install_hint="Requires Node.js and an OpenAI/Codex login.",
    ),
    AcpAgentSpec(
        "cline",
        "Cline",
        "Autonomous coding agent CLI",
        ("cline",),
        "https://github.com/cline/cline",
        source="offline",
        install_hint="Install the Cline CLI and sign in.",
    ),
    AcpAgentSpec(
        "copilot",
        "GitHub Copilot",
        "GitHub Copilot CLI",
        ("copilot",),
        "https://github.com/github/copilot-cli",
        source="offline",
        install_hint="Install the Copilot CLI and sign in.",
    ),
    AcpAgentSpec(
        "kimi",
        "Kimi CLI",
        "Moonshot Kimi coding agent",
        ("kimi",),
        "https://github.com/MoonshotAI/kimi-cli",
        source="offline",
        install_hint="Install the Kimi CLI and sign in.",
    ),
    AcpAgentSpec(
        "factory-droid",
        "Factory Droid",
        "Factory's coding agent",
        ("droid",),
        "https://factory.ai",
        source="offline",
        install_hint="Install Factory Droid and sign in.",
    ),
)


def _host_target() -> str:
    machine = "aarch64" if _host_platform.machine().lower() in {"aarch64", "arm64"} else "x86_64"
    if sys.platform == "darwin":
        return f"darwin-{machine}"
    if sys.platform.startswith("win"):
        return f"windows-{machine}"
    return f"linux-{machine}"


def _command_from_distribution(agent: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    distribution = agent.get("distribution") or {}
    npx = distribution.get("npx")
    if isinstance(npx, dict) and npx.get("package"):
        args = [str(a) for a in npx.get("args", [])]
        return ("npx", "-y", str(npx["package"]), *args), "npx"
    uvx = distribution.get("uvx")
    if isinstance(uvx, dict) and uvx.get("package"):
        args = [str(a) for a in uvx.get("args", [])]
        return ("uvx", str(uvx["package"]), *args), "uvx"
    binary = distribution.get("binary")
    if isinstance(binary, dict):
        target = binary.get(_host_target()) or binary.get("linux-x86_64") or {}
        cmd = str(target.get("cmd", "")).removeprefix("./")
        if cmd:
            args = [str(a) for a in target.get("args", [])]
            return (cmd, *args), "binary"
    return (), ""


def _expanded_path() -> str:
    home = Path.home()
    extra = [
        str(home / ".local" / "bin"),
        str(home / ".bun" / "bin"),
        str(home / ".opencode" / "bin"),
        str(home / ".npm-global" / "bin"),
        "/usr/local/bin",
    ]
    base = os.environ.get("PATH", "")
    parts = [p for p in extra if Path(p).is_dir()]
    if base:
        parts.append(base)
    return os.pathsep.join(parts)


def resolve_agent_binary(cmd: str) -> str | None:
    return shutil.which(cmd, path=_expanded_path())


def _installed(command: tuple[str, ...]) -> bool:
    if not command:
        return False
    if command[0] in {"npx", "uvx"}:
        # Runnable on demand, but not installed until the package is present.
        return False
    return resolve_agent_binary(command[0]) is not None


def _from_registry(payload: dict[str, Any]) -> list[AcpAgentSpec]:
    specs: list[AcpAgentSpec] = []
    for agent in payload.get("agents", []):
        command, kind = _command_from_distribution(agent)
        specs.append(
            AcpAgentSpec(
                id=str(agent.get("id", "")),
                name=str(agent.get("name", agent.get("id", ""))),
                description=str(agent.get("description", "")),
                command=command,
                repository=str(agent.get("repository", "")),
                source="registry",
                installed=_installed(command),
                install_hint=f"Distribution: {kind}" if kind else "No launch command published.",
            )
        )
    return specs


def _cache_path(paths: AppPaths) -> Path:
    return paths.cache_dir / CACHE_FILE


def fetch_registry(
    *,
    paths: AppPaths | None = None,
    url: str = REGISTRY_URL,
    timeout: float = 3.0,
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> list[AcpAgentSpec]:
    """Fetch the ACP registry, falling back to cache then the offline list."""
    resolved = paths or current_paths()
    cache = _cache_path(resolved)
    owns = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = http.get(url)
        response.raise_for_status()
        specs = _from_registry(response.json())
        if specs:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([spec.as_dict() for spec in specs], indent=2) + "\n")
            return specs
    except Exception as exc:
        logger.info("ACP registry fetch failed (%s); using cache/offline list.", exc)
    finally:
        if owns:
            http.close()

    if use_cache and cache.exists():
        try:
            payload = json.loads(cache.read_text())
            cached = [
                AcpAgentSpec(
                    id=item["id"],
                    name=item["name"],
                    description=item.get("description", ""),
                    command=tuple(item.get("command", [])),
                    repository=item.get("repository", ""),
                    source="cache",
                    installed=bool(item.get("installed")),
                    install_hint=item.get("install_hint", ""),
                )
                for item in payload
            ]
            if cached:
                return cached
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass

    return [
        AcpAgentSpec(
            **{
                **spec.__dict__,
                "installed": _installed(spec.command),
            }
        )
        for spec in OFFLINE_AGENTS
    ]


def find_installed(specs: list[AcpAgentSpec]) -> list[AcpAgentSpec]:
    return [spec for spec in specs if spec.installed and spec.command]
