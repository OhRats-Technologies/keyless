"""Resumable onboarding state machine (no Qt imports).

Streamlined 4-step flow: Welcome -> Devices -> Calibration -> Ready.
Legacy step names saved by older versions map cleanly onto the nearest step.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from keyless.config.paths import AppPaths, current_paths

STATE_VERSION = 1


class OnboardingStep(str, Enum):
    WELCOME = "welcome"
    DEVICES = "devices"
    CALIBRATION = "calibration"
    READY = "ready"


STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep.WELCOME,
    OnboardingStep.DEVICES,
    OnboardingStep.CALIBRATION,
    OnboardingStep.READY,
)

OPTIONAL_STEPS: set[OnboardingStep] = set()

_LEGACY_STEP_MAP: dict[str, OnboardingStep] = {
    "welcome": OnboardingStep.WELCOME,
    "permissions": OnboardingStep.DEVICES,
    "cameras": OnboardingStep.DEVICES,
    "stt": OnboardingStep.DEVICES,
    "devices": OnboardingStep.DEVICES,
    "calibration": OnboardingStep.CALIBRATION,
    "provider": OnboardingStep.READY,
    "practice": OnboardingStep.READY,
    "ready": OnboardingStep.READY,
}


@dataclass
class OnboardingState:
    step: OnboardingStep = OnboardingStep.WELCOME
    completed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    updated_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "version": STATE_VERSION,
            "step": self.step.value,
            "completed": list(self.completed),
            "skipped": list(self.skipped),
            "unresolved": list(self.unresolved),
            "updated_at": self.updated_at,
        }


class OnboardingFlow:
    """Persist the current step so a crash or relaunch resumes in place."""

    def __init__(self, paths: AppPaths | None = None, state_path: Path | None = None) -> None:
        self.paths = paths or current_paths()
        self.state_path = state_path or self.paths.onboarding_path
        self.state = self._load()

    def _load(self) -> OnboardingState:
        if not self.state_path.exists():
            return OnboardingState()
        try:
            payload = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return OnboardingState()
        if payload.get("version") != STATE_VERSION:
            return OnboardingState()
        raw_step = str(payload.get("step", "welcome"))
        step = _LEGACY_STEP_MAP.get(raw_step, OnboardingStep.WELCOME)
        return OnboardingState(
            step=step,
            completed=[str(item) for item in payload.get("completed", [])],
            skipped=[str(item) for item in payload.get("skipped", [])],
            unresolved=[str(item) for item in payload.get("unresolved", [])],
            updated_at=str(payload.get("updated_at", "")),
        )

    @property
    def step(self) -> OnboardingStep:
        return self.state.step

    @property
    def index(self) -> int:
        return STEPS.index(self.state.step)

    @property
    def total(self) -> int:
        return len(STEPS)

    @property
    def is_complete(self) -> bool:
        return self.state.step is OnboardingStep.READY and "ready" in self.state.completed

    @property
    def progress(self) -> float:
        return (self.index + 1) / len(STEPS)

    def save(self) -> None:
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.state_path.with_name(self.state_path.name + ".partial")
        partial.write_text(json.dumps(self.state.as_dict(), indent=2) + "\n")
        os.replace(partial, self.state_path)

    def advance(self, *, skipped: bool = False, unresolved: bool = False) -> OnboardingStep:
        current = self.state.step.value
        if skipped:
            if current not in self.state.skipped:
                self.state.skipped.append(current)
        elif current not in self.state.completed:
            self.state.completed.append(current)
        if unresolved:
            if current not in self.state.unresolved:
                self.state.unresolved.append(current)
        elif current in self.state.unresolved:
            self.state.unresolved.remove(current)
        next_index = min(len(STEPS) - 1, self.index + 1)
        self.state.step = STEPS[next_index]
        self.save()
        return self.state.step

    def back(self) -> OnboardingStep:
        self.state.step = STEPS[max(0, self.index - 1)]
        self.save()
        return self.state.step

    def go_to(self, step: OnboardingStep) -> OnboardingStep:
        self.state.step = step
        self.save()
        return self.state.step

    def mark_unresolved(self, step: OnboardingStep | None = None) -> None:
        name = (step or self.state.step).value
        if name not in self.state.unresolved:
            self.state.unresolved.append(name)
            self.save()

    def reset(self) -> None:
        self.state = OnboardingState()
        self.save()
