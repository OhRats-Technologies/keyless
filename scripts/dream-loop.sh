#!/usr/bin/env bash
# Wrapper for the Dream-RSI local Pi + Strata exploration and frontier refinement loop.
set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"
DREAM="$REPO_ROOT/scripts/agent_dream.py"
PRESET="${STRATA_PRESET:-qwen38-iq3s-128k}"

usage() {
  cat <<'EOF'
Usage: ./scripts/dream-loop.sh <command> [args...]

Commands:
  status               Show Strata server status, RAM/VRAM, traces, and promoted policy
  ensure-strata        Start local Strata server if not ready
  run "<task>" [args]  Run a local Pi+Strata task, log trace, and refresh dream-digest.md
  eval                 Run all 6 deterministic repo invariant checks
  digest               Compile and print .agents/traces/dream-digest.md
  promote "<note>"     Monotonically promote AGENTS.md + skills if eval score >= best
EOF
}

ensure_strata() {
  if strata launch --status 2>/dev/null | grep -q "ready at"; then
    return 0
  fi
  echo "Strata is not ready; starting preset '$PRESET'..."
  strata launch --preset "$PRESET" --no-tui --yes
}

cmd="${1:-status}"
shift || true

case "$cmd" in
  status)
    echo "=== Strata ==="
    strata launch --status 2>&1 || true
    echo "=== Resources ==="
    nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null || true
    free -h | awk 'NR==1 || NR==2 {print}'
    echo "=== Dream-RSI ==="
    traces=$(find "$REPO_ROOT/.agents/traces" -maxdepth 1 -name '*.json' ! -name 'dream-digest.json' 2>/dev/null | wc -l)
    echo "Local traces : $traces"
    if [[ -f "$REPO_ROOT/.agents/harness/policy-ledger.jsonl" ]]; then
      echo "Latest policy:"
      tail -n 1 "$REPO_ROOT/.agents/harness/policy-ledger.jsonl"
    fi
    ;;

  ensure-strata)
    ensure_strata
    ;;

  run)
    if (( $# == 0 )); then
      echo "error: provide a task string" >&2
      exit 2
    fi
    ensure_strata
    "$PY" "$DREAM" run "$@"
    "$PY" "$DREAM" digest >/dev/null
    echo "Updated $REPO_ROOT/.agents/traces/dream-digest.md"
    ;;

  eval)
    exec "$PY" "$DREAM" eval "$@"
    ;;

  digest)
    "$PY" "$DREAM" digest >/dev/null
    cat "$REPO_ROOT/.agents/traces/dream-digest.md"
    ;;

  promote)
    note="${1:-policy-update}"
    exec "$PY" "$DREAM" promote --note "$note"
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
