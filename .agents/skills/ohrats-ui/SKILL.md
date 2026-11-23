---
name: ohrats-ui
description: >-
  The OhRats desktop UI standard for the native Qt (PySide6) and SwiftUI Keyless
  shells. Use before writing, reviewing, or finishing any UI work: screens,
  onboarding, settings, home/activity surfaces, copy, spacing, or goldens.
  Triggers: "restyle home", "redesign onboarding", "add a settings card",
  "fix the UI", "remove marketing copy", "capability-gated", "visual finish
  gate", "regenerate goldens", "does this screen look right".
---

# OhRats desktop UI standard

Keyless is a precision instrument, not a landing page. One UI model lives in the
engine (`docs/plans/ui-unification-goal.md`); Qt and SwiftUI are thin renderers
of it. Tokens live in `src/keyless/ui/theme/design-tokens.json` and are loaded by
`src/keyless/ui/theme/tokens.py`. Never hardcode a color, radius, or spacing
value in a widget.

## Rules

1. **Precision-instrument aesthetic.** Calm, high contrast, restrained
   warm-neutral palette from `design-tokens.json`. Hairline borders, generous
   whitespace, no gradients or decoration. Zero marketing copy, zero filler
   paragraphs, zero exclamation marks.
2. **ONE primary action per surface.** Exactly one accent-filled button
   (`objectName="Primary"`). Secondary actions are ghost (`Ghost`) or link
   (`Link`). A screen with two competing primary CTAs is wrong.
3. **Capability-gated rendering.** Render a feature only when the engine
   `CapabilityReport` / `DoctorReport` says it is available. Omit unsupported
   features entirely. Never ship a disabled imitation button or a dialog that
   only says "this feature was removed". The engine is the truth, not the shell.
4. **Progressive disclosure.** Everyday controls on the surface; advanced
   controls, diagnostics, and references behind a disclosure, dialog, or a
   separate Settings group. The Home "Recent" card appears only when there is
   activity.
5. **Typography.** Inter for UI and prose. Space Mono strictly for small
   telemetry numbers and identifiers, with tabular (monospaced) numerals; use
   `objectName="Telemetry"` for live counters, timings, and sizes. Sentence
   case everywhere; title case only for proper nouns.
6. **Spacing and shape.** Use the token grid: 4 / 8 / 16 / 24 / 32 px and radii
   10 / 7 / pill. Cards are `Card` or `SubtleCard`; dividers are `divider()`.
7. **The six row kinds are the renderer surface.** Everything a shell draws is
   one of `status`, `action`, `permission`, `progress`, `picker`, `text` (see
   `docs/plans/ui-unification-goal.md` and `src/keyless/ui/contract.py`). New UI
   behavior belongs in the contract first, then in the shell.

## Copy

- Say what is true and what the user can do now. If a capability is not shipped,
  omit it; if the user asks, the honest line is "not installed yet".
- Gaze v2 learns from natural clicks and voice corrections. There is no
  calibration grid, no "steer to mouse", and no "recenter gaze" step. Do not
  reintroduce that vocabulary.
- Prefer a short headline plus one supporting sentence. Delete any paragraph
  that only restates the headline.

## Visual finish gate (mandatory)

Before closing any UI task, render the offscreen Qt goldens and look at them.
Passing pytest is not visual verification.

```bash
cd <repo>
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui/test_ui.py -o addopts="" -q
```

Inspect every golden in `tests/ui/goldens/` (main-light, main-dark,
onboarding-light, onboarding-dark) for:

- contrast of muted text and borders against its surface,
- alignment of labels, controls, and card edges,
- clipping or truncated text at 980x640 / 740x580,
- horizontal overflow at the 820px minimum window width,
- competing primary CTAs or a stray accent fill,
- leftover legacy gaze/calibration copy.

When layout changes intentionally, regenerate the four PNGs, then review them:

```bash
rm tests/ui/goldens/main-dark.png tests/ui/goldens/main-light.png \
   tests/ui/goldens/onboarding-dark.png tests/ui/goldens/onboarding-light.png
.venv/bin/python -m pytest tests/ui/test_ui.py -o addopts="" -q   # writes, 2 fail
.venv/bin/python -m pytest tests/ui/test_ui.py -o addopts="" -q   # must pass
```

`_compare_golden` writes a missing golden and fails with "Golden did not exist;
wrote ... Re-run to compare." A second run must pass all 16 UI tests.

## References

- `docs/plans/ui-unification-goal.md` — contract, six row kinds, phases.
- `docs/GREENFIELD.md` — gaze v2 truth; no calibration, no pixel anchors.
- `src/keyless/ui/theme/design-tokens.json` — the only palette/type/spacing source.
- `src/keyless/ui/contract.py` — schema-v1 screen document.
- `docs/architecture.md` §2 — capability-gated rendering policy.
