"""Per-user gaze profile produced by the pursuit warm-up.

The profile is a *prior*, never a gate: the app runs without it (cold-start
prior + natural clicks), and every field is optional on load. The display rig
itself lives in ``display_rig.json``; the profile records whether the rig came
from pursuit (then click-based rig inference stays off) and the learner prior.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

PROFILE_VERSION = 1


@dataclass
class GazeProfile:
    feature_dim: int
    M0: list[list[float]]
    P0: list[list[float]]
    latency_ms: float = 80.0
    blink_enter: float = 0.25
    blink_exit: float = 0.16
    precision_deg: float = 1.0
    accuracy_deg: float = float("nan")
    accuracy_px: float = float("nan")
    grade: str = "okay"
    rig_from_pursuit: bool = True
    created_at: float = field(default_factory=time.time)
    version: int = PROFILE_VERSION

    @property
    def m0(self) -> np.ndarray:
        return np.asarray(self.M0, dtype=np.float64)

    @property
    def p0(self) -> np.ndarray:
        return np.asarray(self.P0, dtype=np.float64)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = asdict(self)
        for k in ("accuracy_deg", "accuracy_px"):
            v = payload[k]
            payload[k] = None if v is None or v != v else v
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> GazeProfile | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if int(raw.get("version", 0)) != PROFILE_VERSION:
                return None
            raw = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
            for k in ("accuracy_deg", "accuracy_px"):
                if raw.get(k) is None:
                    raw[k] = float("nan")
            prof = cls(**raw)
            if prof.m0.shape != (prof.feature_dim + 1, 2):
                return None
            if prof.p0.shape != (prof.feature_dim + 1, prof.feature_dim + 1):
                return None
            return prof
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def public(self) -> dict[str, Any]:
        def num(v: float, n: int = 2) -> float | None:
            return None if v is None or v != v else round(float(v), n)

        return {
            "grade": self.grade,
            "latency_ms": num(self.latency_ms, 0),
            "accuracy_deg": num(self.accuracy_deg),
            "accuracy_px": num(self.accuracy_px, 0),
            "precision_deg": num(self.precision_deg),
            "created_at": self.created_at,
            "feature_dim": self.feature_dim,
        }


def profile_path(config_dir: Path) -> Path:
    return config_dir / "gaze_profile.json"


__all__ = ["GazeProfile", "PROFILE_VERSION", "profile_path"]
