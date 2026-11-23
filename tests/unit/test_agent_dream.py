"""Unit tests for the Dream-RSI local+frontier harness (scripts/agent_dream.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_dream_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "agent_dream.py"
    spec = importlib.util.spec_from_file_location("agent_dream", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_dream"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_skills_hash_changes_when_skill_changes(tmp_path: Path) -> None:
    mod = _load_dream_module()
    (tmp_path / "AGENTS.md").write_text("# Agents v1\n", encoding="utf-8")
    skill = tmp_path / ".agents" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Demo v1\n", encoding="utf-8")

    h1, files1 = mod.compute_skills_hash(tmp_path)
    assert files1 == ["AGENTS.md", ".agents/skills/demo/SKILL.md"]

    skill.write_text("# Demo v2\n", encoding="utf-8")
    h2, _ = mod.compute_skills_hash(tmp_path)
    assert h1 != h2


def test_repo_invariants_pass_on_current_tree() -> None:
    mod = _load_dream_module()
    assert mod.check_no_legacy_leakage().passed is True
    assert mod.check_control_no_ui_import().passed is True


def test_promote_is_monotonic_and_rejects_regression(tmp_path: Path) -> None:
    mod = _load_dream_module()
    ledger = tmp_path / "policy-ledger.jsonl"

    good = {
        "timestamp": "2026-11-23T00:00:00Z",
        "git_commit": "abc1234",
        "skills_hash": "a" * 64,
        "skill_files": ["AGENTS.md"],
        "score": 1.0,
        "all_passed": True,
        "checks": [{"name": "compileall", "passed": True, "duration_s": 0.1, "detail": "ok"}],
    }
    ok1, rec1 = mod.promote_policy(ledger_path=ledger, note="baseline", eval_report=good)
    assert ok1 is True
    assert rec1["score"] == 1.0
    assert len(mod.load_ledger(ledger)) == 1

    bad = {
        **good,
        "skills_hash": "b" * 64,
        "score": 0.8,
        "all_passed": False,
        "checks": [{"name": "compileall", "passed": False, "duration_s": 0.1, "detail": "fail"}],
    }
    ok2, _ = mod.promote_policy(ledger_path=ledger, note="regressed", eval_report=bad)
    assert ok2 is False
    # Ledger must remain unchanged on regression
    assert len(mod.load_ledger(ledger)) == 1


def test_run_and_digest_aggregate_by_skills_hash(tmp_path: Path) -> None:
    mod = _load_dream_module()
    traces_dir = tmp_path / "traces"
    ledger = tmp_path / "policy-ledger.jsonl"

    path1, tr1 = mod.run_task(
        "synthetic ok",
        traces_dir=traces_dir,
        custom_cmd=[sys.executable, "-c", "print('ok')"],
        with_eval=False,
    )
    assert path1.exists() and tr1["success"] is True

    path2, tr2 = mod.run_task(
        "synthetic fail",
        traces_dir=traces_dir,
        custom_cmd=[sys.executable, "-c", "import sys; sys.exit(2)"],
        with_eval=False,
    )
    assert path2.exists() and tr2["success"] is False

    md_path, json_path, summary = mod.build_digest(traces_dir=traces_dir, ledger_path=ledger)
    assert md_path.exists() and json_path.exists()
    assert summary["total_runs"] == 2
    assert len(summary["policies"]) == 1
    assert summary["policies"][0]["pass_rate"] == 0.5
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["top_failures"][0][0] == "exit_2"


def test_no_undefined_names_invariant_passes_on_current_tree() -> None:
    mod = _load_dream_module()
    res = mod.check_no_undefined_names()
    assert res.passed is True, res.detail
