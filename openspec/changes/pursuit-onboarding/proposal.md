# Proposal

## Why

New users had no guided setup: tracking started from the anatomical prior and needed
natural clicks before it was usable. The click learner treated every click as ground
truth paired with the newest frame, so a click made without looking, or after the eyes
had already moved on, corrupted the model. Multi-display rig inference estimated each
display independently, which let adjacent displays collapse onto the same angles
(reported as "overlapping monitors that had to be split"). GREENFIELD §6.3 "Cold start"
allows a short warm-up when it is a training set and never a gate.

## What Changes

- Onboarding in the web app (hello -> camera -> follow the light -> result), auto-opened
  once when no profile exists, always skippable, reachable from Home and Gaze.
- Smooth-pursuit warm-up (`gaze/pursuit.py`, `gaze/calibration.py`): one moving stimulus
  per display, rendered by the native Wayland stage (`overlay_wayland.c` stage mode)
  with all other screen content dimmed. Browser full-screen fallback covers one display.
- Eye-to-sensor latency estimated from pursuit lag; per-user blink and movement thresholds.
- Joint rig-free fit (shared gaze weights + one angular offset per display), then a
  no-overlap projection (`gaze/layout_solver.py`, PAVA) with OS order as the prior.
- Online learner (`gaze/learner.py`): Kalman filter on linear gaze weights with drift;
  each click is a label weighted by the posterior probability that it was looked at;
  paired with the stable fixation around the click, not the newest frame.
- Lean proofs (`proofs/lean/`) of layout feasibility, PAVA invariant, and gate bounds.

## Non-goals

- No startup gate, no dot grid, no global offset, no legacy code (AGENTS rules 1, 7).
- Target scorer work (Phase 2) is unchanged.
