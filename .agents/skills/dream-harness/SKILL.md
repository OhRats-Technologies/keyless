# Skill: Dream-RSI Harness (Local Pi + Strata Explorer, Frontier Policy Optimizer)

Use this workflow to improve Keyless's local agent performance over time without weight fine-tuning.

## Architecture

1. **Online Explorer (Local):** `pi-strata` (`PI_CODING_AGENT_DIR=~/.pi/agent-strata`) drives `Qwen3.8-Flash-Next IQ3_S` on local Strata (`http://127.0.0.1:8080/v1`) with a 1.5K-token prompt, 1 active slot (preserving >90% VRAM expert-cache hit rate), and zero heavy extensions.
2. **Trace Pool:** Every task run via `scripts/agent_dream.py run "<task>"` saves a structured trace in `.agents/traces/` (gitignored) pinned to `skills_hash` (SHA-256 of `AGENTS.md` + `.agents/skills/**/SKILL.md`).
3. **Offline Dreaming (Frontier):** When frontier quota is available, run `scripts/agent_dream.py digest` and hand `.agents/traces/dream-digest.md` + `AGENTS.md` + `.agents/skills/**/SKILL.md` to a frontier model.
4. **Monotonic Promotion Gate:** Every proposed instruction/skill revision must pass `scripts/agent_dream.py promote --note "<reason>"`. It verifies all deterministic invariants (`no_legacy_leakage`, `control_no_ui_import`, `compileall`, `pytest_core`, `pytest_ui`) and only appends to `.agents/harness/policy-ledger.jsonl` if `all_passed == true` and `score >= previous_best`.

## Rules for Frontier Policy Edits

- **Keep prompts short.** Pi + Strata relies on a small prompt to avoid prefill spikes and protect the 64 GB RAM budget. Prefer removing ambiguity or tightening a checklist over adding long prose.
- **Target observed failures.** Every edit must address a concrete signature in `dream-digest.md` (e.g., wrong pytest flags, forgotten `SettingSpec` parity, UI golden drift).
- **Never weaken invariants.** Do not disable tests, loosen AST import guards, or bypass `GREENFIELD.md` kill criteria to make a policy score higher.
- **One logical policy change per promotion.** Commit the skill edit together with the updated `.agents/harness/policy-ledger.jsonl` entry so every policy revision is auditable and reversible.

## Commands

```bash
# Run a local Pi + Strata task with automatic trace + invariant scoring
.venv/bin/python scripts/agent_dream.py run "<scoped task>"

# Evaluate all repo invariants right now
.venv/bin/python scripts/agent_dream.py eval

# Compile trace history into a frontier-ready digest
.venv/bin/python scripts/agent_dream.py digest

# Monotonically promote current AGENTS.md + skills into policy-ledger.jsonl
.venv/bin/python scripts/agent_dream.py promote --note "baseline"
```
