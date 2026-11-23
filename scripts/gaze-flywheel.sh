#!/usr/bin/env bash
# Ergonomic wrapper for Gaze v2 Phase 0/1 recording, replay, and candidate recall evaluation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYLESS="$REPO_ROOT/.venv/bin/keyless"
PY="$REPO_ROOT/.venv/bin/python"
SESSIONS_DIR="$("$PY" -c 'from keyless.config.paths import current_paths; print(current_paths().data_dir / "sessions")')"

usage() {
  cat <<'EOF'
Usage: ./scripts/gaze-flywheel.sh <command> [args...]

Commands:
  record [args...]     Record live clicks & candidates (default: --save-screenshots)
  eval [args...]       Evaluate Phase 1 >95% candidate recall (default: --regenerate)
  replay [dir]         Replay a session (defaults to the most recent session)
  status               Show input-group status and recorded session summary
EOF
}

cmd="${1:-status}"
shift || true

case "$cmd" in
  status)
    echo "Sessions dir : $SESSIONS_DIR"
    if id -nG | tr ' ' '\n' | grep -qx input; then
      echo "input group  : active (physical clicks readable)"
    elif getent group input 2>/dev/null | grep -qw "$USER"; then
      echo "input group  : added in /etc/group, but NOT active in this shell yet"
      echo "               Run: newgrp input   (or log out and back in)"
    else
      echo "input group  : missing -> run: sudo usermod -aG input \"\$USER\""
    fi
    if [[ -d "$SESSIONS_DIR" ]]; then
      count=$(find "$SESSIONS_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)
      echo "Sessions     : $count"
      if (( count > 0 )); then
        latest=$(ls -1dt "$SESSIONS_DIR"/*/ 2>/dev/null | head -n 1)
        echo "Latest       : ${latest%/}"
      fi
    else
      echo "Sessions     : 0 (directory not created yet)"
    fi
    ;;

  record)
    if ! id -nG | tr ' ' '\n' | grep -qx input; then
      if getent group input 2>/dev/null | grep -qw "$USER"; then
        echo "Note: 'input' group is not active in this shell yet."
        echo "Run 'newgrp input' once in your terminal, then re-run:"
        echo "  ./scripts/gaze-flywheel.sh record $*"
        exit 1
      fi
    fi
    if (( $# == 0 )); then
      exec "$KEYLESS" gaze record --save-screenshots
    else
      exec "$KEYLESS" gaze record "$@"
    fi
    ;;

  eval)
    if (( $# == 0 )); then
      exec "$KEYLESS" gaze eval-candidates "$SESSIONS_DIR" --regenerate
    else
      exec "$KEYLESS" gaze eval-candidates "$@"
    fi
    ;;

  replay)
    if (( $# == 0 )); then
      if [[ ! -d "$SESSIONS_DIR" ]]; then
        echo "error: no sessions directory at $SESSIONS_DIR" >&2
        exit 1
      fi
      latest=$(ls -1dt "$SESSIONS_DIR"/*/ 2>/dev/null | head -n 1 || true)
      if [[ -z "$latest" ]]; then
        echo "error: no recorded sessions found under $SESSIONS_DIR" >&2
        exit 1
      fi
      exec "$KEYLESS" gaze replay "${latest%/}"
    else
      exec "$KEYLESS" gaze replay "$@"
    fi
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
