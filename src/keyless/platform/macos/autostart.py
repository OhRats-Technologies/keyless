"""macOS autostart via LaunchAgent plist (SMAppService is Swift-side)."""

from __future__ import annotations

import sys
from pathlib import Path


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "party.ohrats.keyless.plist"


class macOSAutostart:
    def __init__(self, agent_path: Path | None = None) -> None:
        self._path = agent_path or _agent_path()

    def enabled(self) -> bool:
        return self._path.exists()

    def enable(self) -> None:
        exe = sys.executable
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>party.ohrats.keyless</string>
    <key>ProgramArguments</key><array><string>{exe}</string><string>-m</string><string>keyless.main</string><string>run</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
</dict>
</plist>
"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(plist)

    def disable(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
