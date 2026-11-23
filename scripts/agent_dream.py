#!/usr/bin/env python3
"""Dream-RSI style harness for local Pi + Strata exploration and frontier policy refinement.

Inspired by Dream-RSI (arXiv:2609.14858):
1. Online Explore (`run`): execute tasks with the lean local Pi + Strata harness and log
   structured traces pinned to the exact SHA-256 of AGENTS.md + .agents/skills/**/SKILL.md.
2. Deterministic Evaluator (`eval`): score repo invariants objectively (compileall, pytest,
   UI goldens, zero legacy-gaze-v1 leakage, ipc.control UI isolation).
3. Offline Replay Digest (`digest`): aggregate traces by policy (`skills_hash`) into a
   compact frontier-ready report (`dream-digest.md` / `dream-digest.json`).
4. Monotonic Promotion (`promote`): accept a new skill/instruction policy into
   `.agents/harness/policy-ledger.jsonl` only if its verified score is >= the current best.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TRACES_DIR = REPO_ROOT / ".agents" / "traces"
LEDGER_PATH = REPO_ROOT / ".agents" / "harness" / "policy-ledger.jsonl"

LEGACY_FORBIDDEN_RE = re.compile(
    r"\b(StereoGazeModel|OnlineGazeLearner|calibrator|geo_gaze|online_learner|cursor_gimbal|recenter_to)\b"
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    duration_s: float
    detail: str


def compute_skills_hash(repo_root: Path = REPO_ROOT) -> tuple[str, list[str]]:
    """Deterministic SHA-256 over AGENTS.md and all .agents/skills/**/SKILL.md files."""
    files: list[Path] = []
    agents_md = repo_root / "AGENTS.md"
    if agents_md.exists():
        files.append(agents_md)
    skills_dir = repo_root / ".agents" / "skills"
    if skills_dir.exists():
        files.extend(sorted(skills_dir.rglob("SKILL.md")))

    h = hashlib.sha256()
    rel_paths: list[str] = []
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        rel_paths.append(rel)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest(), rel_paths


def git_head(repo_root: Path = REPO_ROOT) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _python_bin(repo_root: Path) -> str:
    venv_py = repo_root / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def check_no_legacy_leakage(repo_root: Path = REPO_ROOT) -> CheckResult:
    t0 = time.monotonic()
    hits: list[str] = []
    src_dir = repo_root / "src" / "keyless"
    if src_dir.exists():
        for py_file in sorted(src_dir.rglob("*.py")):
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), 1):
                if LEGACY_FORBIDDEN_RE.search(line):
                    rel = py_file.relative_to(repo_root).as_posix()
                    hits.append(f"{rel}:{lineno}")
    return CheckResult(
        name="no_legacy_leakage",
        passed=len(hits) == 0,
        duration_s=round(time.monotonic() - t0, 3),
        detail="clean" if not hits else f"legacy matches: {', '.join(hits[:5])}",
    )


def check_control_no_ui_import(repo_root: Path = REPO_ROOT) -> CheckResult:
    t0 = time.monotonic()
    control_py = repo_root / "src" / "keyless" / "ipc" / "control.py"
    if not control_py.exists():
        return CheckResult("control_no_ui_import", False, 0.0, "missing src/keyless/ipc/control.py")
    tree = ast.parse(control_py.read_text(encoding="utf-8"), filename=str(control_py))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "keyless.ui" or alias.name.startswith("keyless.ui."):
                    bad.append(f"import {alias.name} (L{node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "keyless.ui" or mod.startswith("keyless.ui."):
                bad.append(f"from {mod} (L{node.lineno})")
    return CheckResult(
        name="control_no_ui_import",
        passed=len(bad) == 0,
        duration_s=round(time.monotonic() - t0, 3),
        detail="clean" if not bad else "; ".join(bad[:5]),
    )


def _run_subprocess_check(name: str, cmd: list[str], repo_root: Path) -> CheckResult:
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
    dur = round(time.monotonic() - t0, 3)
    combined = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
    tail = combined[-1] if combined else ("ok" if proc.returncode == 0 else f"exit {proc.returncode}")
    return CheckResult(name=name, passed=(proc.returncode == 0), duration_s=dur, detail=tail[:200])


def run_eval(repo_root: Path = REPO_ROOT, *, include_ui: bool = True) -> dict[str, Any]:
    """Run all deterministic Keyless quality invariants and return a scored dict."""
    py = _python_bin(repo_root)
    checks: list[CheckResult] = [
        check_no_legacy_leakage(repo_root),
        check_control_no_ui_import(repo_root),
        _run_subprocess_check("compileall", [py, "-m", "compileall", "-q", "src"], repo_root),
        _run_subprocess_check(
            "pytest_core",
            [
                py,
                "-m",
                "pytest",
                "tests",
                "-q",
                "--ignore=tests/unit/test_packaging.py",
                "--ignore=tests/unit/test_release_assets.py",
                "--ignore=tests/ui/test_instance_lock.py",
            ],
            repo_root,
        ),
    ]
    if include_ui:
        checks.append(
            _run_subprocess_check(
                "pytest_ui",
                [py, "-m", "pytest", "tests/ui/test_ui.py", "-q", "-m", "ui"],
                repo_root,
            )
        )

    passed_count = sum(1 for c in checks if c.passed)
    score = round(passed_count / len(checks), 4) if checks else 0.0
    skills_hash, skill_files = compute_skills_hash(repo_root)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_head(repo_root),
        "skills_hash": skills_hash,
        "skill_files": skill_files,
        "score": score,
        "all_passed": passed_count == len(checks),
        "checks": [asdict(c) for c in checks],
    }


def _build_pi_strata_command(task: str, *, allow_tools: bool = True) -> tuple[list[str], dict[str, str]]:
    pi_strata = Path.home() / ".local" / "bin" / "pi-strata"
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(Path.home() / ".pi" / "agent-strata")
    env["PI_SKIP_VERSION_CHECK"] = "1"
    if pi_strata.exists():
        cmd = [str(pi_strata), "--no-session"]
    else:
        cmd = [
            "pi",
            "--no-extensions",
            "--no-mcp",
            "--no-themes",
            "--no-prompt-templates",
            "--no-session",
        ]
    if not allow_tools:
        cmd.append("--no-tools")
    cmd.extend(["-p", task])
    return cmd, env


def run_task(
    task: str,
    *,
    repo_root: Path = REPO_ROOT,
    traces_dir: Path = TRACES_DIR,
    custom_cmd: list[str] | None = None,
    allow_tools: bool = True,
    with_eval: bool = True,
    include_ui: bool = False,
    timeout_s: int = 900,
) -> tuple[Path, dict[str, Any]]:
    """Run one local exploration task and record a Dream-RSI trace JSON."""
    traces_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    skills_hash, skill_files = compute_skills_hash(repo_root)
    commit_before = git_head(repo_root)

    if custom_cmd is not None:
        cmd = custom_cmd
        env = dict(os.environ)
        harness = "custom"
    else:
        cmd, env = _build_pi_strata_command(task, allow_tools=allow_tools)
        harness = "pi-strata"

    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        rc = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    duration_s = round(time.monotonic() - t0, 3)

    commit_after = git_head(repo_root)
    eval_report = run_eval(repo_root, include_ui=include_ui) if with_eval else None
    failed_checks = (
        [c["name"] for c in eval_report["checks"] if not c["passed"]]
        if eval_report is not None
        else []
    )
    success = (rc == 0) and (not timed_out) and (eval_report is None or eval_report["all_passed"])

    trace: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "harness": harness,
        "task": task,
        "skills_hash": skills_hash,
        "skill_files": skill_files,
        "commit_before": commit_before,
        "commit_after": commit_after,
        "exit_code": rc,
        "timed_out": timed_out,
        "duration_s": duration_s,
        "success": success,
        "failed_checks": failed_checks,
        "eval": eval_report,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-1000:],
    }
    out_path = traces_dir / f"{run_id}.json"
    out_path.write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")
    return out_path, trace


def build_digest(
    traces_dir: Path = TRACES_DIR,
    ledger_path: Path = LEDGER_PATH,
) -> tuple[Path, Path, dict[str, Any]]:
    """Aggregate local traces into a frontier-ready Dream-RSI policy digest."""
    traces_dir.mkdir(parents=True, exist_ok=True)
    traces: list[dict[str, Any]] = []
    for path in sorted(traces_dir.glob("*.json")):
        if path.name.startswith("dream-digest"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "run_id" in data:
                traces.append(data)
        except Exception:
            continue

    by_policy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failure_counts: Counter[str] = Counter()
    for tr in traces:
        by_policy[str(tr.get("skills_hash", "unknown"))].append(tr)
        for chk in tr.get("failed_checks") or []:
            failure_counts[str(chk)] += 1
        if tr.get("timed_out"):
            failure_counts["timeout"] += 1
        elif tr.get("exit_code", 0) != 0:
            failure_counts[f"exit_{tr.get('exit_code')}"] += 1

    policies: list[dict[str, Any]] = []
    for shash, runs in sorted(by_policy.items()):
        n = len(runs)
        passes = sum(1 for r in runs if r.get("success"))
        avg_dur = round(sum(float(r.get("duration_s", 0.0)) for r in runs) / n, 2)
        eval_scores = [
            float(r["eval"]["score"])
            for r in runs
            if isinstance(r.get("eval"), dict) and "score" in r["eval"]
        ]
        avg_score = round(sum(eval_scores) / len(eval_scores), 4) if eval_scores else None
        policies.append(
            {
                "skills_hash": shash,
                "runs": n,
                "passes": passes,
                "pass_rate": round(passes / n, 4),
                "avg_eval_score": avg_score,
                "avg_duration_s": avg_dur,
            }
        )

    ledger = load_ledger(ledger_path)
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_runs": len(traces),
        "policies": policies,
        "top_failures": failure_counts.most_common(10),
        "promoted_policies": len(ledger),
        "latest_promoted": ledger[-1] if ledger else None,
        "recent_failures": [
            {
                "run_id": r["run_id"],
                "task": str(r.get("task", ""))[:160],
                "failed_checks": r.get("failed_checks", []),
                "exit_code": r.get("exit_code"),
                "stderr_tail": str(r.get("stderr_tail", ""))[-300:],
            }
            for r in traces[-10:]
            if not r.get("success")
        ],
    }

    json_path = traces_dir / "dream-digest.json"
    md_path = traces_dir / "dream-digest.md"
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Keyless Dream-RSI Trace Digest",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- Total local runs analyzed: `{summary['total_runs']}`",
        f"- Promoted policy revisions in ledger: `{summary['promoted_policies']}`",
        "",
        "## Performance by Policy (`skills_hash`)",
        "",
        "| `skills_hash` | Runs | Pass Rate | Avg Eval Score | Avg Duration (s) |",
        "|---|---:|---:|---:|---:|",
    ]
    for p in policies:
        score_s = "n/a" if p["avg_eval_score"] is None else f"{p['avg_eval_score']:.2%}"
        lines.append(
            f"| `{p['skills_hash'][:12]}` | {p['runs']} | {p['pass_rate']:.1%} | {score_s} | {p['avg_duration_s']:.1f} |"
        )
    if not policies:
        lines.append("| _(none yet)_ | 0 | n/a | n/a | n/a |")

    lines.extend(["", "## Recurring Failure Signatures", ""])
    if summary["top_failures"]:
        for name, count in summary["top_failures"]:
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- None recorded.")

    lines.extend(
        [
            "",
            "## Frontier Policy Refinement Prompt",
            "",
            "Give this file together with `AGENTS.md` and `.agents/skills/**/SKILL.md` to a frontier model:",
            "",
            "```text",
            "Read .agents/skills/dream-harness/SKILL.md and .agents/traces/dream-digest.md.",
            "Propose minimal, surgical edits to AGENTS.md or .agents/skills/**/SKILL.md that eliminate the recurring failure signatures above without bloating the prompt.",
            "After editing, run: .venv/bin/python scripts/agent_dream.py promote --note '<short rationale>'",
            "Only keep the policy revision if promote exits 0 (monotonic non-regression).",
            "```",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path, summary


def load_ledger(ledger_path: Path = LEDGER_PATH) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def promote_policy(
    *,
    repo_root: Path = REPO_ROOT,
    ledger_path: Path = LEDGER_PATH,
    note: str = "",
    include_ui: bool = True,
    eval_report: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Monotonically promote the current skills policy iff score >= best promoted score."""
    report = eval_report if eval_report is not None else run_eval(repo_root, include_ui=include_ui)
    ledger = load_ledger(ledger_path)
    best_score = max((float(entry.get("score", 0.0)) for entry in ledger), default=0.0)
    current_score = float(report["score"])

    accepted = bool(report["all_passed"]) and (current_score >= best_score)
    record: dict[str, Any] = {
        "timestamp": report["timestamp"],
        "git_commit": report["git_commit"],
        "skills_hash": report["skills_hash"],
        "skill_files": report["skill_files"],
        "score": current_score,
        "previous_best": best_score,
        "all_passed": report["all_passed"],
        "note": note,
        "checks": {c["name"]: c["passed"] for c in report["checks"]},
    }
    if accepted:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    return accepted, record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keyless Dream-RSI local+frontier harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_eval = sub.add_parser("eval", help="Run deterministic repo invariant evaluator")
    p_eval.add_argument("--no-ui", action="store_true", help="Skip Qt UI golden tests")

    p_run = sub.add_parser("run", help="Run a local Pi+Strata task and record a trace")
    p_run.add_argument("task", help="Prompt/task for the local agent")
    p_run.add_argument("--no-tools", action="store_true", help="Disable Pi tools (smoke test)")
    p_run.add_argument("--no-eval", action="store_true", help="Skip post-run invariant eval")
    p_run.add_argument("--with-ui", action="store_true", help="Include UI tests in post-run eval")
    p_run.add_argument("--timeout", type=int, default=900, help="Seconds before aborting run")

    sub.add_parser("digest", help="Compile traces into .agents/traces/dream-digest.{md,json}")

    p_prom = sub.add_parser("promote", help="Monotonically promote current skills policy if score >= best")
    p_prom.add_argument("--note", default="", help="Short rationale for this policy revision")
    p_prom.add_argument("--no-ui", action="store_true", help="Skip Qt UI golden tests")

    args = parser.parse_args(argv)

    if args.cmd == "eval":
        rep = run_eval(include_ui=not args.no_ui)
        print(json.dumps(rep, indent=2))
        return 0 if rep["all_passed"] else 1

    if args.cmd == "run":
        path, trace = run_task(
            args.task,
            allow_tools=not args.no_tools,
            with_eval=not args.no_eval,
            include_ui=args.with_ui,
            timeout_s=args.timeout,
        )
        print(
            json.dumps(
                {
                    "trace_path": str(path),
                    "run_id": trace["run_id"],
                    "skills_hash": trace["skills_hash"][:12],
                    "success": trace["success"],
                    "duration_s": trace["duration_s"],
                    "failed_checks": trace["failed_checks"],
                },
                indent=2,
            )
        )
        return 0 if trace["success"] else 1

    if args.cmd == "digest":
        md_path, json_path, summary = build_digest()
        print(
            json.dumps(
                {
                    "markdown": str(md_path),
                    "json": str(json_path),
                    "total_runs": summary["total_runs"],
                    "policies": summary["policies"],
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "promote":
        accepted, record = promote_policy(note=args.note, include_ui=not args.no_ui)
        print(json.dumps({"promoted": accepted, "record": record}, indent=2))
        return 0 if accepted else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
