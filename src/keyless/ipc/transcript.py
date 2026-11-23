"""Latest Parakeet transcript (final or provisional) shared with the overlay."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptState:
    text: str
    timestamp_ns: int
    seq: int
    source: str = "parakeet"
    final: bool = True

    @property
    def age_s(self) -> float:
        return max(0.0, (time.monotonic_ns() - self.timestamp_ns) / 1_000_000_000.0)


def read_transcript(path: Path) -> TranscriptState | None:
    try:
        data = json.loads(path.read_text())
        text = str(data.get("text", "")).strip()
        if not text:
            return None
        return TranscriptState(
            text=text,
            timestamp_ns=int(data.get("timestamp_ns", 0)),
            seq=int(data.get("seq", 0)),
            source=str(data.get("source", "parakeet")),
            final=bool(data.get("final", True)),
        )
    except (OSError, ValueError, TypeError):
        return None


def write_transcript(
    path: Path,
    text: str,
    *,
    source: str = "parakeet",
    final: bool = True,
    timestamp_ns: int | None = None,
) -> TranscriptState | None:
    clean = " ".join(str(text).split())
    if not clean:
        return None
    previous = read_transcript(path)
    state = TranscriptState(
        text=clean,
        timestamp_ns=time.monotonic_ns() if timestamp_ns is None else int(timestamp_ns),
        seq=(previous.seq + 1) if previous is not None else 1,
        source=source,
        final=bool(final),
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "text": state.text,
                    "timestamp_ns": state.timestamp_ns,
                    "seq": state.seq,
                    "source": state.source,
                    "final": state.final,
                }
            )
        )
        os.replace(tmp, path)
    except OSError:
        return None
    return state


__all__ = ["TranscriptState", "read_transcript", "write_transcript"]
