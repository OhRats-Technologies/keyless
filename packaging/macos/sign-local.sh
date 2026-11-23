#!/bin/bash
# Sign the local Keyless.app with the shared OhRats Developer ID.
#
# A stable signing identity is essential: ad-hoc (`-s -`) signatures are
# cdhash-based, so every rebuild changes the app's designated requirement and
# macOS silently drops its Accessibility / Input Monitoring / Screen Recording
# grants. Signing with the Developer ID makes the requirement
# `identifier party.ohrats.Keyless ... leaf[subject.OU] = W9C4V23252`, which
# is identical across rebuilds — grants survive.
#
# The cert must already be in a keychain (same one WakeBar's releases use).
# For CI/notarized builds see .github/workflows/build.yml.
set -euo pipefail

APP="${1:-$HOME/Applications/Keyless.app}"
TEAM_OU="W9C4V23252"

IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
  | sed -n 's/.*"\(Developer ID Application:.*\)"/\1/p' \
  | grep -F "($TEAM_OU)" | head -1 || true)

if [[ -z "$IDENTITY" ]]; then
  echo "warning: no Developer ID Application ($TEAM_OU) in keychain; falling back to ad-hoc." >&2
  echo "         TCC grants will reset on the next rebuild." >&2
  IDENTITY="-"
  codesign --force --deep --sign - "$APP"
else
  echo "signing $APP with: $IDENTITY"
  # --timestamp needs network access; fall back to an offline signature if it
  # is unavailable (still stable identity, just no secure timestamp).
  if ! codesign --force --deep --sign "$IDENTITY" --timestamp "$APP"; then
    echo "warning: timestamp failed; signing without it" >&2
    codesign --force --deep --sign "$IDENTITY" "$APP"
  fi
fi

codesign --verify --deep --strict "$APP"
echo "OK: $(codesign -d -r- "$APP" 2>&1 | grep designated)"
