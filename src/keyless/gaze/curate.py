"""Pure-Python session inspection and atomic dataset curation (Gaze v2).

No Qt or UI imports live here so curation logic can be driven from the Lab UI,
CLI scripts, or offline pipelines with identical invariants:
- Every edited event is validated with :func:`validate_event` before touching disk.
- ``events.jsonl`` and ``meta.json`` are rewritten atomically (temp + replace).
- Dataset exports split strictly by session (:func:`split_sessions`), never by frame.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keyless.gaze.metrics import bbox_contains
from keyless.gaze.replay import SchemaError, iter_sessions, load_session, split_sessions
from keyless.gaze.schema import validate_event

PHASE1_RECALL_GATE = 0.95


@dataclass(frozen=True)
class SessionSummary:
    """Compact summary of one recorded session directory."""

    session_id: str
    session_dir: Path
    started_ts_ns: int
    event_count: int
    click_count: int
    covered_clicks: int
    accepted_clicks: int
    rejected_clicks: int
    apps: tuple[str, ...]
    has_screenshots: bool
    redact_text: bool
    dropped: int

    @property
    def recall(self) -> float | None:
        if self.click_count <= 0:
            return None
        return self.covered_clicks / self.click_count


@dataclass(frozen=True)
class AppRecallRow:
    app: str
    clicks: int
    hits: int

    @property
    def recall(self) -> float:
        return self.hits / self.clicks if self.clicks > 0 else 0.0


@dataclass(frozen=True)
class RecallSummary:
    sessions: int
    clicks: int
    hits: int
    per_app: tuple[AppRecallRow, ...]

    @property
    def overall_recall(self) -> float | None:
        if self.clicks <= 0:
            return None
        return self.hits / self.clicks

    @property
    def gate_met(self) -> bool:
        return self.clicks > 0 and (self.hits / self.clicks) > PHASE1_RECALL_GATE


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _rewrite_session_events(session_dir: Path, events: list[dict[str, Any]]) -> None:
    for idx, ev in enumerate(events):
        errors = validate_event(ev)
        if errors:
            raise ValueError(f"event[{idx}] invalid: {'; '.join(errors)}")

    lines = [
        json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
        for ev in events
    ]
    payload = ("\n".join(lines) + "\n") if lines else ""
    _atomic_write_text(session_dir / "events.jsonl", payload)

    meta_path = session_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        counts = dict(meta.get("counts") or {})
        counts["events"] = len(events)
        meta["counts"] = counts
        meta["curated_ts_ns"] = time.time_ns()
        _atomic_write_text(meta_path, json.dumps(meta, indent=2, sort_keys=True) + "\n")


def event_click_covered(event: dict[str, Any]) -> bool | None:
    """Return True/False for click events with mouse coords; None otherwise."""
    interaction = event.get("interaction") or {}
    if interaction.get("kind") != "click":
        return None
    mouse = interaction.get("mouse")
    if not mouse or len(mouse) != 2:
        return None
    screen = event.get("screen") or {}
    candidates = screen.get("candidates") or []
    return any(
        bbox_contains(c.get("bbox", (0, 0, 0, 0)), mouse[0], mouse[1])
        for c in candidates
    )


def summarize_session(session_dir: Path) -> SessionSummary:
    """Load one session and compute its click coverage and curation counts."""
    session = load_session(session_dir)
    apps: set[str] = set()
    clicks = covered = accepted = rejected = 0
    has_screenshots = False

    for ev in session.events:
        screen = ev.get("screen") or {}
        if screen.get("app"):
            apps.add(str(screen["app"]))
        if screen.get("screenshot"):
            has_screenshots = True
        interaction = ev.get("interaction") or {}
        if interaction.get("kind") == "click":
            clicks += 1
            if event_click_covered(ev) is True:
                covered += 1
            if interaction.get("accepted") is True:
                accepted += 1
            elif interaction.get("accepted") is False:
                rejected += 1

    counts = session.meta.get("counts") or {}
    return SessionSummary(
        session_id=session.session_id,
        session_dir=Path(session_dir),
        started_ts_ns=int(session.meta.get("started_ts_ns") or 0),
        event_count=len(session.events),
        click_count=clicks,
        covered_clicks=covered,
        accepted_clicks=accepted,
        rejected_clicks=rejected,
        apps=tuple(sorted(apps)),
        has_screenshots=has_screenshots,
        redact_text=bool(session.meta.get("redact_text", False)),
        dropped=int(counts.get("dropped") or 0),
    )


def list_session_summaries(sessions_dir: Path) -> list[SessionSummary]:
    """Return all valid sessions under ``sessions_dir``, newest first."""
    if not sessions_dir.exists():
        return []
    summaries: list[SessionSummary] = []
    for sdir in iter_sessions(sessions_dir):
        try:
            summaries.append(summarize_session(sdir))
        except (SchemaError, FileNotFoundError, OSError):
            continue
    summaries.sort(key=lambda s: (s.started_ts_ns, s.session_id), reverse=True)
    return summaries


def evaluate_recall_summary(sessions_dir: Path) -> RecallSummary:
    """Compute overall and per-app Phase 1 candidate recall across all sessions."""
    if not sessions_dir.exists():
        return RecallSummary(sessions=0, clicks=0, hits=0, per_app=())
    per_app: dict[str, list[int]] = {}
    sessions_used = 0
    total_clicks = 0
    total_hits = 0

    for sdir in iter_sessions(sessions_dir):
        try:
            session = load_session(sdir)
        except (SchemaError, FileNotFoundError, OSError):
            continue
        sessions_used += 1
        for ev in session.events:
            interaction = ev.get("interaction") or {}
            screen = ev.get("screen") or {}
            if interaction.get("kind") != "click":
                continue
            mouse = interaction.get("mouse")
            candidates = screen.get("candidates") or []
            if not mouse or not candidates:
                continue
            app = str(screen.get("app") or "unknown")
            counts = per_app.setdefault(app, [0, 0])
            counts[0] += 1
            total_clicks += 1
            if any(bbox_contains(c.get("bbox", (0, 0, 0, 0)), mouse[0], mouse[1]) for c in candidates):
                counts[1] += 1
                total_hits += 1

    rows = tuple(
        AppRecallRow(app=app, clicks=vals[0], hits=vals[1])
        for app, vals in sorted(per_app.items())
    )
    return RecallSummary(
        sessions=sessions_used,
        clicks=total_clicks,
        hits=total_hits,
        per_app=rows,
    )


_UNSET = object()


def update_event(
    session_dir: Path,
    index: int,
    *,
    accepted: Any = _UNSET,
    target: Any = _UNSET,
) -> dict[str, Any]:
    """Update ``accepted`` and/or ``target`` on event ``index`` atomically."""
    session = load_session(session_dir)
    if index < 0 or index >= len(session.events):
        raise IndexError(f"event index {index} out of range (len={len(session.events)})")

    events = [dict(ev) for ev in session.events]
    ev = dict(events[index])
    interaction = dict(ev.get("interaction") or {})
    if not interaction:
        raise ValueError("cannot curate an event without an interaction section")

    if accepted is not _UNSET:
        if accepted is not None and not isinstance(accepted, bool):
            raise ValueError("accepted must be bool or None")
        interaction["accepted"] = accepted
    if target is not _UNSET:
        if target is not None and not isinstance(target, str):
            raise ValueError("target must be str or None")
        interaction["target"] = target

    ev["interaction"] = interaction
    events[index] = ev
    _rewrite_session_events(session_dir, events)
    return ev


def delete_event(session_dir: Path, index: int) -> int:
    """Remove event ``index`` atomically and return the new event count."""
    session = load_session(session_dir)
    if index < 0 or index >= len(session.events):
        raise IndexError(f"event index {index} out of range (len={len(session.events)})")
    events = [ev for i, ev in enumerate(session.events) if i != index]
    _rewrite_session_events(session_dir, events)
    return len(events)


def delete_session(sessions_dir: Path, session_dir: Path) -> None:
    """Delete one session directory after verifying it lives inside ``sessions_dir``."""
    root = sessions_dir.resolve()
    target = session_dir.resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"refusing to delete {target}: not inside {root}")
    if target.exists():
        shutil.rmtree(target)


def export_curated_split(
    sessions_dir: Path,
    out_dir: Path,
    *,
    seed: int = 42,
    accepted_only: bool = True,
) -> dict[str, Any]:
    """Export curated sessions into disjoint session-level train/val/test JSONL files."""
    session_dirs = iter_sessions(sessions_dir)
    if not session_dirs:
        raise ValueError(f"no sessions found under {sessions_dir}")

    splits = split_sessions(session_dirs, seed=seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "exported_ts_ns": time.time_ns(),
        "seed": seed,
        "accepted_only": accepted_only,
        "splits": {},
    }

    for split_name, dirs in splits.items():
        rows: list[str] = []
        session_ids: list[str] = []
        for sdir in dirs:
            try:
                session = load_session(sdir)
            except (SchemaError, FileNotFoundError, OSError):
                continue
            session_ids.append(session.session_id)
            for ev in session.events:
                if accepted_only:
                    interaction = ev.get("interaction") or {}
                    if interaction.get("accepted") is False:
                        continue
                rows.append(json.dumps(ev, separators=(",", ":"), ensure_ascii=False))
        out_file = out_dir / f"{split_name}.jsonl"
        _atomic_write_text(out_file, ("\n".join(rows) + "\n") if rows else "")
        manifest["splits"][split_name] = {
            "sessions": session_ids,
            "events": len(rows),
            "file": out_file.name,
        }

    _atomic_write_text(out_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


__all__ = [
    "LabEnvironmentStatus",
    "PHASE1_RECALL_GATE",
    "AppRecallRow",
    "RecallSummary",
    "SessionSummary",
    "delete_event",
    "delete_session",
    "evaluate_recall_summary",
    "event_click_covered",
    "export_curated_split",
    "list_session_summaries",
    "summarize_session",
    "grant_linux_input_access",
    "probe_lab_environment",
    "seed_demo_session",
    "update_event",
]


@dataclass(frozen=True)
class LabEnvironmentStatus:
    """Host capability and local service snapshot for Keyless Lab."""

    host: str
    input_group_active: bool
    input_group_configured: bool
    pkexec_available: bool
    strata_cli_available: bool
    strata_serving: bool
    pi_cli_available: bool


def _tcp_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_lab_environment(
    *,
    host: str | None = None,
    strata_port: int = 8080,
) -> LabEnvironmentStatus:
    """Inspect click-capture permissions and local Strata/Pi availability."""
    import getpass
    import os
    from keyless.platform.router import current_host

    resolved_host = host or current_host()
    home = Path.home()

    if resolved_host == "linux":
        events = sorted(Path("/dev/input").glob("event*"))
        input_active = any(os.access(p, os.R_OK) for p in events)
        user = os.environ.get("USER") or getpass.getuser()
        input_configured = input_active
        try:
            import grp

            members = grp.getgrnam("input").gr_mem
            if user in members:
                input_configured = True
        except Exception:
            pass
        pkexec_ok = shutil.which("pkexec") is not None
    else:
        input_active = True
        input_configured = True
        pkexec_ok = False

    strata_cli = (
        shutil.which("strata") is not None
        or (home / ".local" / "bin" / "strata").exists()
    )
    pi_cli = (
        shutil.which("pi-strata") is not None
        or (home / ".local" / "bin" / "pi-strata").exists()
        or shutil.which("pi") is not None
        or (home / ".bun" / "bin" / "pi").exists()
    )
    return LabEnvironmentStatus(
        host=resolved_host,
        input_group_active=input_active,
        input_group_configured=input_configured,
        pkexec_available=pkexec_ok,
        strata_cli_available=strata_cli,
        strata_serving=_tcp_port_open("127.0.0.1", strata_port),
        pi_cli_available=pi_cli,
    )


def grant_linux_input_access() -> tuple[bool, str]:
    """Grant persistent + immediate /dev/input/event* read access via PolicyKit (pkexec)."""
    import getpass
    import os
    import subprocess

    pkexec = shutil.which("pkexec")
    if not pkexec:
        return False, "pkexec is not installed on this system."

    user = os.environ.get("USER") or getpass.getuser()
    if not user or not all(ch.isalnum() or ch in ("_", "-", ".") for ch in user):
        return False, f"Refusing unsafe username {user!r}."

    script = (
        'usermod -aG input "$1" && '
        '(command -v udevadm >/dev/null 2>&1 && udevadm control --reload-rules && udevadm trigger --subsystem-match=input || true) && '
        '(command -v setfacl >/dev/null 2>&1 && setfacl -m "u:$1:rw" /dev/input/event* || true)'
    )
    try:
        proc = subprocess.run(
            [pkexec, "/bin/sh", "-c", script, "keyless-grant-input", user],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        return False, str(exc)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"Authorization cancelled or failed (exit {proc.returncode})."

    events = sorted(Path("/dev/input").glob("event*"))
    if any(os.access(p, os.R_OK) for p in events):
        return True, "Input capture granted and active immediately."
    return (
        True,
        "Added to the input group. Sign out and back in once if event devices are still locked.",
    )


def seed_demo_session(paths: Any, *, session_id: str = "sample-session-01") -> Path:
    """Create a realistic sample session so newcomers can explore the curator immediately."""
    from keyless.gaze.collect import SessionCollector
    from keyless.gaze.schema import make_event

    collector = SessionCollector(paths, session_id=session_id)
    candidates = [
        {
            "id": "btn-primary",
            "bbox": [140, 140, 240, 56],
            "role": "button",
            "text": "Start listening",
            "score": 0.96,
            "source": "ax",
        },
        {
            "id": "btn-settings",
            "bbox": [420, 140, 180, 56],
            "role": "button",
            "text": "Settings",
            "score": 0.89,
            "source": "ax",
        },
        {
            "id": "tab-activity",
            "bbox": [140, 240, 260, 44],
            "role": "tab",
            "text": "Recent activity",
            "score": 0.84,
            "source": "ocr",
        },
        {
            "id": "link-docs",
            "bbox": [440, 240, 220, 44],
            "role": "link",
            "text": "Architecture docs",
            "score": 0.78,
            "source": "dom",
        },
    ]
    base_ts = time.time_ns()
    samples = [
        ("Firefox", 101, [255.0, 165.0], [260.0, 168.0], "btn-primary", True),
        ("Firefox", 101, [505.0, 168.0], [510.0, 166.0], "btn-settings", True),
        ("Code", 202, [270.0, 260.0], [265.0, 262.0], "tab-activity", None),
        ("Code", 202, [760.0, 460.0], [550.0, 262.0], None, None),
    ]
    for idx, (app_name, win_id, mouse_xy, pred_xy, target_id, accepted) in enumerate(samples):
        collector.record(
            make_event(
                session_id,
                base_ts + idx * 500_000_000,
                screen={
                    "display_id": 0,
                    "app": app_name,
                    "window_id": win_id,
                    "candidates": candidates,
                },
                prediction={
                    "top": target_id or "link-docs",
                    "p": 0.91 if target_id else 0.38,
                    "entropy": 0.24 if target_id else 0.92,
                    "pixel": pred_xy,
                },
                interaction={
                    "kind": "click",
                    "mouse": mouse_xy,
                    "target": target_id,
                    "accepted": accepted,
                    "latency_ms": 85 + idx * 12,
                },
            )
        )
    collector.close()
    return collector.session_dir
