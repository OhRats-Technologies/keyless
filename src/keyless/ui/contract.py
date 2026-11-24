"""Schema-v1 UI contract: the engine-owned screen document every shell renders.

Pure Python — no Qt imports, no I/O. The builder takes plain dictionaries that
the control plane already has (capabilities, doctor, config, history, model,
setup, status, setting specs) and returns one JSON-serialisable document.

The renderer surface is a closed set of six row kinds (see
``docs/plans/ui-unification-goal.md``):

    status      title, state (ok|warn|bad|idle), detail?
    action      title, action, style (primary|link), enabled
    permission  id, title, state, optional, needs_restart
    progress    title, fraction?, indeterminate?
    picker      title, options [{label, value}], selected
    text        title, detail?

Screens are Home, Activity, Settings, and Setup. Rows are capability-gated:
a row exists only when the matching ``CapabilityReport`` entry is available or
degraded. Missing features are omitted, never rendered as disabled imitations.

The screen document lives here; the action registry lives in the engine
(``keyless.ipc.control``). That split keeps ``control.py`` free of
``keyless.ui`` imports while the control plane still serves a complete UI
contract through the injected ``ui_contract_builder``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable

SCHEMA_VERSION = 1

ROW_KINDS: tuple[str, ...] = ("status", "action", "permission", "progress", "picker", "text")

_AVAILABLE_STATUSES = ("available", "degraded")


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields so the document stays terse."""
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


@dataclass(frozen=True)
class StatusRow:
    title: str
    state: str = "idle"
    detail: str = ""
    kind: ClassVar[str] = "status"

    def as_dict(self) -> dict[str, Any]:
        return _compact(
            {"kind": self.kind, "title": self.title, "state": self.state, "detail": self.detail}
        )


@dataclass(frozen=True)
class ActionRow:
    title: str
    action: str
    style: str = "link"
    enabled: bool = True
    kind: ClassVar[str] = "action"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "action": self.action,
            "style": self.style,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class PermissionRow:
    id: str
    title: str
    state: str
    optional: bool = False
    needs_restart: bool = False
    kind: ClassVar[str] = "permission"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "optional": self.optional,
            "needs_restart": self.needs_restart,
        }


@dataclass(frozen=True)
class ProgressRow:
    title: str
    fraction: float | None = None
    indeterminate: bool = False
    kind: ClassVar[str] = "progress"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "title": self.title}
        if self.fraction is not None:
            payload["fraction"] = self.fraction
        payload["indeterminate"] = self.indeterminate
        return payload


@dataclass(frozen=True)
class PickerRow:
    title: str
    options: tuple[dict[str, Any], ...]
    selected: Any = None
    kind: ClassVar[str] = "picker"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "options": list(self.options),
            "selected": self.selected,
        }


@dataclass(frozen=True)
class TextRow:
    title: str
    detail: str = ""
    kind: ClassVar[str] = "text"

    def as_dict(self) -> dict[str, Any]:
        return _compact({"kind": self.kind, "title": self.title, "detail": self.detail})


Row = StatusRow | ActionRow | PermissionRow | ProgressRow | PickerRow | TextRow


@dataclass(frozen=True)
class Section:
    title: str
    rows: tuple[Row, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "rows": [row.as_dict() for row in self.rows]}


@dataclass(frozen=True)
class Screen:
    title: str
    sections: tuple[Section, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "sections": [section.as_dict() for section in self.sections]}


def _capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    items = capabilities.get("capabilities")
    return items if isinstance(items, dict) else {}


def _capability(capabilities: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = _capabilities(capabilities).get(name)
    return value if isinstance(value, dict) else None


def _cap_status(capabilities: dict[str, Any], name: str) -> str | None:
    cap = _capability(capabilities, name)
    return str(cap.get("status", "")) if cap is not None else None


def _available(capabilities: dict[str, Any], name: str) -> bool:
    return _cap_status(capabilities, name) in _AVAILABLE_STATUSES


def _status_row(title: str, status: str | None, detail: str = "", *, degraded_state: str = "warn") -> StatusRow | None:
    if status is None:
        return None
    if status == "available":
        state = "ok"
    elif status == "degraded":
        state = degraded_state
    elif status in ("needs_permission", "needs_restart"):
        state = "warn"
    elif status in ("unavailable", "unsupported_platform"):
        return None
    else:
        state = "idle"
    return StatusRow(title=title, state=state, detail=detail)


def _permission_rows(capabilities: dict[str, Any]) -> list[PermissionRow]:
    rows: list[PermissionRow] = []
    permissions = capabilities.get("permissions")
    if not isinstance(permissions, dict):
        return rows
    for name, info in permissions.items():
        if not isinstance(info, dict):
            continue
        state = str(info.get("state", "unknown"))
        rows.append(
            PermissionRow(
                id=str(name),
                title=str(name).replace("_", " ").capitalize(),
                state=state,
                optional=False,
                needs_restart=state == "needs_restart",
            )
        )
    return rows


def _display_value(value: Any) -> str:
    if value is None:
        return "System default"
    if isinstance(value, bool):
        return "On" if value else "Off"
    text = str(value).strip()
    return text or "Not set"


def _apply_suffix(spec: dict[str, Any]) -> str:
    return "restart required" if spec.get("apply") == "restart" else "applies immediately"


def _home_rows(
    *,
    capabilities: dict[str, Any],
    doctor: dict[str, Any],
    config: dict[str, Any],
    history: list[dict[str, Any]],
    model: dict[str, Any],
    status: dict[str, Any],
    specs: list[dict[str, Any]],
) -> list[Section]:
    sections: list[Section] = []
    state = str(status.get("state", "stopped"))
    detail = str(status.get("detail", ""))

    status_rows: list[Row] = []
    engine_states = {
        "listening": ("Listening", "ok"),
        "paused": ("Paused", "idle"),
        "error": ("Needs attention", "bad"),
        "stopped": ("Ready", "idle"),
    }
    engine_title, engine_state = engine_states.get(state, ("Ready", "idle"))
    status_rows.append(StatusRow(title=f"Engine: {engine_title}", state=engine_state, detail=detail))
    sections.append(Section("Status", tuple(status_rows)))

    readiness: list[Row] = []
    camera_status = _cap_status(capabilities, "cameras")
    camera_row = _status_row("Camera", camera_status, _cap_detail(capabilities, "cameras"))
    if camera_row is not None:
        readiness.append(camera_row)
    audio_status = _cap_status(capabilities, "audio")
    audio_row = _status_row("Microphone", audio_status, _cap_detail(capabilities, "audio"))
    if audio_row is not None:
        readiness.append(audio_row)
    if _available(capabilities, "stt") and model:
        ready = bool(model.get("ready"))
        label = str(model.get("display_name") or model.get("name") or "Speech model")
        readiness.append(
            StatusRow(
                title=label,
                state="ok" if ready else "warn",
                detail="Ready offline" if ready else "Download in Setup",
            )
        )
    permission_rows = _permission_rows(capabilities)
    if permission_rows:
        states = [row.state for row in permission_rows]
        if any(item in ("denied", "needs_restart") for item in states):
            readiness.append(
                StatusRow(
                    title="Permissions",
                    state="warn",
                    detail="Some permissions need attention in Setup.",
                )
            )
        elif all(item == "granted" for item in states):
            readiness.append(StatusRow(title="Permissions", state="ok", detail="All granted."))
    if readiness:
        sections.append(Section("Readiness", tuple(readiness)))

    primary = _primary_action(state)
    sections.append(Section("Action", (primary,)))

    job = model.get("job") if isinstance(model, dict) else None
    if isinstance(job, dict) and str(job.get("state", "")) in ("downloading", "verifying"):
        percent = float(job.get("percent") or 0.0)
        sections.append(
            Section(
                "Download",
                (
                    ProgressRow(
                        title="Downloading speech model",
                        fraction=round(percent / 100.0, 3) if percent > 0 else None,
                        indeterminate=percent <= 0,
                    ),
                ),
            )
        )

    pickers: list[Row] = []
    if _available(capabilities, "cameras"):
        options = _camera_options(doctor)
        if options:
            pickers.append(
                PickerRow(title="Camera", options=options, selected=config.get("cam1_by_id"))
            )
    if _available(capabilities, "audio"):
        inputs = _audio_options(doctor)
        if inputs:
            pickers.append(
                PickerRow(
                    title="Microphone",
                    options=({"label": "System default", "value": None},) + inputs,
                    selected=config.get("audio_device"),
                )
            )
    if _available(capabilities, "llm_provider"):
        provider_spec = _spec_by_key(specs, "llm_provider")
        if provider_spec is not None:
            pickers.append(
                PickerRow(
                    title=str(provider_spec.get("label") or "Provider"),
                    options=tuple(
                        {"label": str(option), "value": str(option)}
                        for option in provider_spec.get("options") or ()
                    ),
                    selected=config.get("llm_provider"),
                )
            )
    if pickers:
        sections.append(Section("Devices and provider", tuple(pickers)))

    if history:
        recent: list[Row] = []
        for item in history[:3]:
            if not isinstance(item, dict):
                continue
            heard = str(item.get("transcript") or "").strip()
            outcome = str(
                item.get("target") or item.get("result") or item.get("route") or ""
            ).strip()
            recent.append(TextRow(title=heard or "Action", detail=outcome))
        if recent:
            sections.append(Section("Recent", tuple(recent)))
    return sections


def _cap_detail(capabilities: dict[str, Any], name: str) -> str:
    cap = _capability(capabilities, name)
    return str(cap.get("detail", "")) if cap is not None else ""


def _camera_options(doctor: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cameras = doctor.get("cameras")
    if not isinstance(cameras, dict):
        return ()
    discovered = cameras.get("discovered")
    if not isinstance(discovered, list):
        return ()
    options: list[dict[str, Any]] = []
    for camera in discovered:
        if not isinstance(camera, dict):
            continue
        value = camera.get("path") or camera.get("id")
        label = camera.get("name") or camera.get("path") or camera.get("id")
        if value and label:
            options.append({"label": str(label), "value": str(value)})
    return tuple(options)


def _audio_options(doctor: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    audio = doctor.get("audio")
    if not isinstance(audio, dict):
        return ()
    inputs = audio.get("inputs")
    if not isinstance(inputs, list):
        return ()
    options: list[dict[str, Any]] = []
    for device in inputs:
        if not isinstance(device, dict):
            continue
        name = str(device.get("name") or "").strip()
        if name:
            options.append({"label": name, "value": name})
    return tuple(options)


def _primary_action(state: str) -> ActionRow:
    if state == "listening":
        return ActionRow(title="Pause", action="pause", style="primary", enabled=True)
    if state == "paused":
        return ActionRow(title="Resume", action="start", style="primary", enabled=True)
    if state == "error":
        return ActionRow(title="Try again", action="start", style="primary", enabled=True)
    return ActionRow(title="Start listening", action="start", style="primary", enabled=True)


def _spec_by_key(specs: Iterable[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for spec in specs:
        if isinstance(spec, dict) and spec.get("key") == key:
            return spec
    return None


def _platform_specs(specs: Iterable[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        platforms = spec.get("platforms") or ["all"]
        if "all" in platforms or platform in platforms:
            selected.append(spec)
    return selected


def _settings_sections(
    *,
    capabilities: dict[str, Any],
    config: dict[str, Any],
    specs: list[dict[str, Any]],
) -> list[Section]:
    platform = str(capabilities.get("platform", ""))
    grouped: dict[str, list[Row]] = {}
    for spec in _platform_specs(specs, platform):
        key = str(spec.get("key", ""))
        label = str(spec.get("label") or key)
        value = config.get(key)
        control = str(spec.get("control", "text"))
        group = str(spec.get("group") or "Settings")
        rows = grouped.setdefault(group, [])
        if control == "select":
            options = tuple(
                {"label": str(option), "value": str(option)}
                for option in spec.get("options") or ()
            )
            rows.append(PickerRow(title=label, options=options, selected=str(value)))
        else:
            rows.append(
                TextRow(title=label, detail=f"{_display_value(value)} ({_apply_suffix(spec)})")
            )
    sections = [Section(group, tuple(rows)) for group, rows in grouped.items()]
    sections.append(
        Section(
            "Diagnostics",
            (
                TextRow(
                    title="Full report",
                    detail="Capabilities, cameras, audio, speech, and providers.",
                ),
                ActionRow(title="Run diagnostics", action="diagnostics", style="link"),
            ),
        )
    )
    return sections


def _activity_sections(history: list[dict[str, Any]]) -> list[Section]:
    rows: list[Row] = []
    for item in history[:10]:
        if not isinstance(item, dict):
            continue
        heard = str(item.get("transcript") or "").strip() or "Action"
        outcome = str(item.get("target") or item.get("result") or item.get("route") or "").strip()
        rows.append(TextRow(title=heard, detail=outcome))
    if not rows:
        rows.append(
            TextRow(
                title="No activity yet",
                detail="Spoken actions and clicks appear here.",
            )
        )
    return [Section("Recent activity", tuple(rows))]


def _setup_screen(
    *,
    capabilities: dict[str, Any],
    setup: dict[str, Any],
) -> dict[str, Any]:
    steps = [str(step) for step in setup.get("steps") or ()]
    state = setup.get("state") if isinstance(setup.get("state"), dict) else {}
    current = str(state.get("step", ""))
    completed = {str(item) for item in state.get("completed") or ()}
    complete = bool(setup.get("complete"))

    sections: list[Section] = []
    step_rows: list[Row] = []
    for step in steps:
        if complete or step in completed:
            step_rows.append(StatusRow(title=step.capitalize(), state="ok", detail="Done"))
        elif step == current:
            step_rows.append(StatusRow(title=step.capitalize(), state="idle", detail="Current step"))
        else:
            step_rows.append(StatusRow(title=step.capitalize(), state="idle", detail="Not started"))
    if step_rows:
        sections.append(Section("Steps", tuple(step_rows)))

    permission_rows = _permission_rows(capabilities)
    if permission_rows:
        sections.append(Section("Permissions", tuple(permission_rows)))

    sections.append(
        Section(
            "Gaze engine",
            (
                TextRow(
                    title="Voice control is ready",
                    detail="Gaze v2 learns from natural clicks when it ships. No calibration.",
                ),
            ),
        )
    )
    return {
        "title": "Set up Keyless",
        "steps": steps,
        "current": current,
        "complete": complete,
        "sections": [section.as_dict() for section in sections],
    }


def _actions_document() -> dict[str, Any]:
    ui = {"method": "POST", "path": "/v1/ui/action"}
    return {
        "start": {**ui, "id": "start"},
        "pause": {**ui, "id": "pause"},
        "allow": {**ui, "id": "allow"},
        "set_config": {**ui, "id": "set_config"},
        "setup": {**ui, "id": "setup"},
        "select_camera": {**ui, "id": "select_camera"},
        "models_download": {**ui, "id": "models_download"},
        "models_cancel": {**ui, "id": "models_cancel"},
        "diagnostics": {**ui, "id": "diagnostics"},
    }


def build_ui_state(
    *,
    capabilities: dict[str, Any],
    doctor: dict[str, Any],
    config: dict[str, Any],
    history: list[dict[str, Any]],
    model: dict[str, Any],
    setup: dict[str, Any],
    status: dict[str, Any],
    specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the schema-v1 screen document for every shell."""
    home = Screen(
        "Home",
        tuple(
            _home_rows(
                capabilities=capabilities,
                doctor=doctor,
                config=config,
                history=history,
                model=model,
                status=status,
                specs=specs,
            )
        ),
    )
    activity = Screen("Activity", tuple(_activity_sections(history)))
    settings = Screen(
        "Settings",
        tuple(_settings_sections(capabilities=capabilities, config=config, specs=specs)),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "screens": {
            "home": home.as_dict(),
            "activity": activity.as_dict(),
            "settings": settings.as_dict(),
            "setup": _setup_screen(capabilities=capabilities, setup=setup),
        },
        "actions": _actions_document(),
    }
