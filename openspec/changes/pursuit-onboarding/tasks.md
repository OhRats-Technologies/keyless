# Tasks

## 1. Geometry
- [x] 1.1 `layout_solver.py`: PAVA chain solver, row grouping, order search with swap penalty
- [x] 1.2 `displays.place_displays`; `infer_rig_from_clicks` routed through it
- [x] 1.3 Lean: change of variables, no-overlap, sweep soundness/identity, PAVA invariant

## 2. Warm-up
- [x] 2.1 Pursuit plan (figure-eight sweeps <=16 deg/s, contracting holds, travel)
- [x] 2.2 Latency estimate, joint fit, held-out accuracy, blink/precision thresholds
- [x] 2.3 Session state machine with face-loss pause, stage shm, native stage mode
- [x] 2.4 `/v1/calibration{,/start,/cancel,/forget,/stream}`

## 3. Online learning
- [x] 3.1 `GazeLearner` with gated labels, drift, undo/reset replay
- [x] 3.2 Fixation-aware delayed click pairing in the publisher
- [x] 3.3 Profile as learner prior (`gaze_profile.json`)

## 4. UI
- [x] 4.1 Onboarding module + styles, Home/Gaze entry points
- [ ] 4.2 Run the warm-up on real hardware and record the grade/latency in HANDOFF
- [ ] 4.3 Held-out comparison: click-hit rate with vs without warm-up profile (GREENFIELD §9)
