## ADDED Requirements

### Requirement: Warm-up never gates
The app SHALL start and track without a gaze profile; onboarding SHALL be skippable at every step.

#### Scenario: no profile
- **WHEN** `gaze_profile.json` is absent
- **THEN** the publisher runs on the cold-start prior and the daemon starts normally

### Requirement: Displays never overlap
Solved display layouts SHALL keep every pair of displays in a row disjoint in azimuth.

#### Scenario: coincident estimates
- **WHEN** two displays in one OS row are estimated at the same angle
- **THEN** `place_displays` returns them separated by at least the sum of their half-widths
  (Lean: `Keyless.Layout.no_overlap`; test: `test_overlapping_estimates_get_split`)

### Requirement: Clicks are gated labels
A click far from a confident prediction SHALL NOT change the model.

#### Scenario: click without looking
- **WHEN** the model is confident and a click lands far from the predicted gaze
- **THEN** the label weight is below the threshold and the weights are unchanged
  (test: `test_clicks_without_looking_are_ignored_once_confident`)
