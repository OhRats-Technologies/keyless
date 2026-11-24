"""Unified local HTTP web UI for Keyless (served by ControlServer)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")


def open_browser_url(url: str) -> bool:
    """Open ``url`` in the user's browser, preferring ``xdg-open`` on Linux."""
    if sys.platform.startswith("linux"):
        xdg = shutil.which("xdg-open")
        if xdg:
            try:
                subprocess.Popen(
                    [xdg, url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    elif sys.platform == "darwin":
        opener = shutil.which("open")
        if opener:
            try:
                subprocess.Popen(
                    [opener, url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


__all__ = ["STATIC_DIR", "open_browser_url"]
