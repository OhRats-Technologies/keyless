# LIVE STATE — working notes for the current autonomous run

Purpose: survive short context windows. Update this file after every completed
block. It is scratch/live state; `docs/HANDOFF.md` remains the polished handoff.

## Standing instructions (from user, 2026-11-23, user is napping)

1. Finish the stage-2 probe scaffold (tests green).
2. Rename branch `greenfield/gaze-v2` -> `alt`, push to GitHub, delete the
   remote `greenfield/gaze-v2`. Only `main` and `alt` should exist on GitHub.
3. Start the app (`keyless serve`) and confirm it runs (voice-only path is OK).
4. Check the user's camera works (webcam capture).
5. Build a synthetic test: fake person + fake eyes in a 3D environment, to
   exercise the gaze pipeline without a human. Check `legacy/gaze-v1` for any
   prior synthetic-scene code. Consider making the lab a web (HTML/three.js)
   thing if that is easier.

## Block log

- [ ] Probe scaffold tests green (`tests/unit/test_gaze_experiment.py`)
- [ ] Committed on `alt`
- [ ] Branch rename + GitHub surgery done
- [ ] App runs (daemon voice-only OK)
- [ ] Camera capture works (report device + frame path)
- [ ] Synthetic gaze lab: status below

## Findings

(append findings here as work proceeds)
