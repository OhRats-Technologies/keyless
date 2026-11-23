"""The gaze seam must never move a pointer unless explicitly enabled."""

from __future__ import annotations

from keyless.gaze.runtime import run_gaze_loop
from keyless.gaze.types import GazeFrame


class _Source:
    def __init__(self) -> None:
        self.frames = [GazeFrame(x=10.0, y=20.0), GazeFrame(x=11.0, y=21.0)]

    def read(self):
        return self.frames.pop(0) if self.frames else None

    def close(self) -> None:
        pass


class _Pointer:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []

    def move_absolute(self, x: float, y: float) -> None:
        self.moves.append((x, y))


def test_gaze_loop_moves_the_cursor_when_enabled() -> None:
    pointer = _Pointer()
    run_gaze_loop(_Source(), pointer, duration_sec=0.01, hz=1000.0)
    assert pointer.moves == [(10.0, 20.0), (11.0, 21.0)]


def test_gaze_loop_leaves_the_pointer_alone_when_disabled() -> None:
    pointer = _Pointer()
    run_gaze_loop(_Source(), pointer, duration_sec=0.01, hz=1000.0, move_enabled=False)
    assert pointer.moves == []
