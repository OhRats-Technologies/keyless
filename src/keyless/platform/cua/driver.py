"""Embedded cua-driver lifecycle: private daemon + stdio MCP proxy.

Follows cua-driver's EMBEDDING.md exactly:

- spawn ``cua-driver serve --embedded --socket <private>`` as a **direct
  child** (never via ``open``/LaunchServices — that would break macOS TCC
  responsibility inheritance) with ``CUA_DRIVER_EMBEDDED=1``;
- wait for the socket, then spawn ``cua-driver mcp --embedded --socket``
  and speak MCP (``initialize`` + ``tools/call``) over its stdio;
- fixed permission mode at launch (``standard``: promptless routine
  automation — Keyless is a local accessibility tool, not an agent);
- restart the daemon after any grant change (TCC answers cache per process).

The gaze cursor uses the same embedded transport, but never waits for an
action reply: the eye overlay sends fire-and-forget ``move_cursor`` targets at
60 Hz (see ``keyless.platform.cua.agent_cursor``). Discrete actions still use
the synchronous ``call`` path.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class CuaDriverError(RuntimeError):
    """Transport, protocol, or tool failure talking to the embedded driver."""


class CuaDriverManager:
    """Owns one embedded cua-driver daemon + MCP proxy pair."""

    def __init__(
        self,
        binary: Path | None = None,
        *,
        host_bundle_id: str = "party.ohrats.Keyless",
        socket_dir: Path | None = None,
    ) -> None:
        from keyless.models import runtimes

        self.binary = Path(binary) if binary is not None else runtimes.binary_path()
        self.host_bundle_id = host_bundle_id
        self.socket_path = Path(
            socket_dir or Path(tempfile.gettempdir())
        ) / f"keyless-cua-{os.getpid()}.sock"
        self._daemon: subprocess.Popen[bytes] | None = None
        self._proxy: subprocess.Popen[bytes] | None = None
        self._lifetime_pipe: object | None = None
        self._reader: threading.Thread | None = None
        self._stderr_drain: threading.Thread | None = None
        self._pending: dict[int, queue.Queue[dict]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._running = False

    # -- lifecycle ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running and self._proxy is not None and self._proxy.poll() is None

    def start(self, timeout_s: float = 15.0) -> CuaDriverManager:
        if self.running:
            return self
        if not self.binary.exists():
            raise CuaDriverError(
                f"cua-driver binary missing at {self.binary} "
                f"(run `keyless models download cua-driver`)."
            )
        env = dict(os.environ)
        env["CUA_DRIVER_EMBEDDED"] = "1"
        env["CUA_DRIVER_HOST_BUNDLE_ID"] = self.host_bundle_id
        try:
            self._daemon = subprocess.Popen(
                [
                    str(self.binary),
                    "serve",
                    "--embedded",
                    "--parent-liveness-stdio",
                    "--socket",
                    str(self.socket_path),
                ],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # The daemon exits when this pipe closes, so a paused/killed engine
            # cannot leave an orphan cursor overlay on the user's screen.
            self._lifetime_pipe = self._daemon.stdin
        except OSError as exc:
            raise CuaDriverError(f"Could not spawn cua-driver daemon: {exc}") from exc
        deadline = time.monotonic() + timeout_s
        while not self.socket_path.exists():
            if self._daemon.poll() is not None:
                raise CuaDriverError("cua-driver daemon exited before creating its socket.")
            if time.monotonic() > deadline:
                self.stop()
                raise CuaDriverError("Timed out waiting for cua-driver daemon socket.")
            time.sleep(0.05)
        try:
            self._proxy = subprocess.Popen(
                [str(self.binary), "mcp", "--embedded", "--socket", str(self.socket_path)],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.stop()
            raise CuaDriverError(f"Could not spawn cua-driver MCP proxy: {exc}") from exc
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, name="cua-mcp-reader", daemon=True)
        self._reader.start()
        self._stderr_drain = threading.Thread(
            target=self._drain_stderr, name="cua-mcp-stderr", daemon=True
        )
        self._stderr_drain.start()
        try:
            self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "keyless", "version": "0.1.2"},
            }, timeout_s=timeout_s)
            self._notify("notifications/initialized", {})
        except Exception as exc:
            self.stop()
            raise CuaDriverError(f"cua-driver MCP handshake failed: {exc}") from exc
        return self

    @property
    def daemon_pid(self) -> int | None:
        """Process id of the embedded daemon (its overlay window is ours)."""
        proc = self._daemon
        return proc.pid if proc is not None else None

    def stop(self) -> None:
        self._running = False
        self._stop_proc(self._proxy)
        pipe = self._lifetime_pipe
        self._lifetime_pipe = None
        if pipe is not None:
            try:
                pipe.close()  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass
        self._stop_proc(self._daemon)
        self._proxy = None
        self._daemon = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _stop_proc(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except Exception:
            # Shutdown is best effort: a wedged daemon must not keep the
            # engine alive, so escalate once and move on.
            try:
                proc.kill()
            except Exception:
                pass

    def __enter__(self) -> CuaDriverManager:
        return self.start()

    def __exit__(self, *args: object) -> None:
        self.stop()

    # -- MCP transport ---------------------------------------------------

    def _send(self, message: dict) -> None:
        assert self._proxy is not None and self._proxy.stdin is not None
        data = (json.dumps(message) + "\n").encode()
        with self._write_lock:
            try:
                self._proxy.stdin.write(data)
                self._proxy.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CuaDriverError(f"cua-driver proxy write failed: {exc}") from exc

    @staticmethod
    def _read_available(fd: int, size: int = 65536) -> bytes:
        """Read whatever bytes are ready, without waiting to fill ``size``.

        ``BufferedReader.read(n)`` blocks until it has n bytes (or EOF), which
        deadlocks on a line-delimited MCP reply shorter than n. Unbuffered
        ``os.read`` returns the bytes already in the pipe immediately.
        """
        try:
            return os.read(fd, size)
        except (OSError, ValueError):
            return b""

    def _read_loop(self) -> None:
        assert self._proxy is not None and self._proxy.stdout is not None
        fd = self._proxy.stdout.fileno()
        buf = bytearray()
        while self._running:
            chunk = self._read_available(fd)
            if not chunk:
                break
            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                msg_id = message.get("id")
                if msg_id is None:
                    continue  # server notification; nothing to route
                with self._pending_lock:
                    waiter = self._pending.get(int(msg_id)) if isinstance(msg_id, int) else None
                if waiter is not None:
                    waiter.put(message)

    def _drain_stderr(self) -> None:
        assert self._proxy is not None and self._proxy.stderr is not None
        fd = self._proxy.stderr.fileno()
        while self._running:
            chunk = self._read_available(fd)
            if not chunk:
                break
            logger.debug("cua-driver stderr: %s", chunk.decode(errors="replace")[:500])

    def _request(self, method: str, params: dict, timeout_s: float = 60.0) -> dict:
        if not self.running:
            raise CuaDriverError("cua-driver is not running.")
        with self._pending_lock:
            self._next_id += 1
            msg_id = self._next_id
            waiter: queue.Queue[dict] = queue.Queue()
            self._pending[msg_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            try:
                reply = waiter.get(timeout=timeout_s)
            except queue.Empty as exc:
                raise CuaDriverError(f"cua-driver request timed out: {method}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(msg_id, None)
        if "error" in reply:
            raise CuaDriverError(f"cua-driver error for {method}: {reply['error']}")
        return reply.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call_soon(self, tool: str, arguments: dict | None = None) -> bool:
        """Send one MCP tool call without waiting for its reply.

        Used by the 60 Hz agent-cursor pump: the daemon applies each command as
        it arrives, while the reply (which can trail by a quarter second) is
        discarded. Returns False when no live proxy is available.
        """
        if not self.running:
            return False
        with self._pending_lock:
            self._next_id += 1
            msg_id = self._next_id
        try:
            self._send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            })
        except CuaDriverError:
            logger.debug("cua-driver fire-and-forget send failed: %s", tool)
            return False
        return True

    # -- tools ------------------------------------------------------------

    def call(self, tool: str, arguments: dict | None = None, timeout_s: float = 60.0) -> dict:
        """Call one MCP tool; returns {content, structured, isError}."""
        result = self._request(
            "tools/call",
            {"name": tool, "arguments": arguments or {}},
            timeout_s=timeout_s,
        )
        return {
            "content": result.get("content", []),
            "structured": result.get("structuredContent", {}),
            "isError": bool(result.get("isError", False)),
        }

    def check_permissions(self) -> dict:
        """Host-attributed TCC state; never prompts (embedded mode)."""
        return self.call("check_permissions", {}).get("structured", {})

    def health_report(self) -> dict:
        return self.call("health_report", {"include": ["bundle_identity"]}).get("structured", {})

    def version(self) -> str:
        try:
            out = subprocess.check_output(
                [str(self.binary), "--version"], text=True, timeout=10.0
            )
            return out.strip()
        except Exception as exc:
            raise CuaDriverError(f"Could not read cua-driver version: {exc}") from exc
