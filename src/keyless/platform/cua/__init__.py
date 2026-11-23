"""cua-driver platform package: discrete input/capture/AX through a pinned binary.

Keyless stays the default program (gaze, voice, router, UI); control actions
go to cua-driver, which owns the per-OS input/Capture/AX machinery. The binary
is pinned in ``keyless.models.runtimes`` and fetched with
``keyless models download cua-driver`` — bumping it is a version + hash
change, not maintenance of platform code.
"""

from keyless.platform.cua.driver import CuaDriverError, CuaDriverManager
from keyless.platform.cua.pointer import CuaDriverPointerBackend

__all__ = ["CuaDriverError", "CuaDriverManager", "CuaDriverPointerBackend"]
