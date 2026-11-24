#!/bin/sh
# Fast local installer for a cloned Keyless repository.
# Installs an editable (or wheel) environment in .venv and wires the desktop
# entry, icons, and ~/.local/bin/keyless in ~1 second (no AppImage repack).
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${KEYLESS_BIN_DIR:-${HOME}/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
DESKTOP_DIR="${DATA_HOME}/applications"
ICON_DIR="${DATA_HOME}/icons/hicolor"
VENV_DIR="${KEYLESS_VENV_DIR:-${REPO_ROOT}/.venv}"

EDITABLE=1
OPEN_AFTER=0
SKIP_SYNC=0

for arg in "$@"; do
  case "$arg" in
    --no-editable|--wheel) EDITABLE=0 ;;
    --editable) EDITABLE=1 ;;
    --open) OPEN_AFTER=1 ;;
    --skip-sync) SKIP_SYNC=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./scripts/install.sh [--editable | --wheel] [--open] [--skip-sync]

Installs Keyless locally from this repository into ~/.local/bin and
your desktop application menu.

Options:
  --editable   Install in editable mode (default; repo edits apply immediately)
  --wheel      Install as a standard wheel into .venv
  --open       Launch the Keyless UI after installing
  --skip-sync  Skip uv sync (reuse an already-prepared .venv)
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
export PATH

cd "$REPO_ROOT"

if [ "$SKIP_SYNC" -eq 0 ]; then
  command -v uv >/dev/null 2>&1 || {
    echo "keyless: 'uv' is required (https://docs.astral.sh/uv/)" >&2
    exit 1
  }
  if [ "$EDITABLE" -eq 1 ]; then
    if [ -x "${VENV_DIR}/bin/python" ]; then
      uv pip install --python "${VENV_DIR}/bin/python" --no-deps -e "$REPO_ROOT"
    else
      uv sync --python 3.13
    fi
  else
    if [ ! -x "${VENV_DIR}/bin/python" ]; then
      uv sync --python 3.13 --no-editable
    else
      uv pip install --python "${VENV_DIR}/bin/python" --no-deps --reinstall "$REPO_ROOT"
    fi
  fi
fi

EXEC_PY="${VENV_DIR}/bin/python"
[ -x "$EXEC_PY" ] || {
  echo "keyless: python not found at $EXEC_PY" >&2
  exit 1
}

mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "${ICON_DIR}/scalable/apps" "${ICON_DIR}/256x256/apps"

cat > "${BIN_DIR}/keyless" <<EOF
#!/bin/sh
exec "${EXEC_PY}" -m keyless.main "\$@"
EOF
chmod 0755 "${BIN_DIR}/keyless"

cat > "${DESKTOP_DIR}/keyless.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Keyless
Comment=Gaze and voice control
Exec=${BIN_DIR}/keyless ui
Icon=keyless
Terminal=false
Categories=Utility;Accessibility;
EOF

cp "${REPO_ROOT}/packaging/linux/keyless.svg" "${ICON_DIR}/scalable/apps/keyless.svg"
cp "${REPO_ROOT}/packaging/linux/keyless.png" "${ICON_DIR}/256x256/apps/keyless.png"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q "$DESKTOP_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q "$ICON_DIR" 2>/dev/null || true
fi

echo "Installed Keyless -> ${BIN_DIR}/keyless"
echo "Desktop entry -> ${DESKTOP_DIR}/keyless.desktop"

if [ "$OPEN_AFTER" -eq 1 ]; then
  pkill -f "keyless\.(main|ui)" 2>/dev/null || true
  sleep 0.4
  nohup "${BIN_DIR}/keyless" ui >/tmp/keyless-ui.log 2>&1 &
  echo "Launched Keyless UI (PID $!)"
fi
