"""Stage-2 probe harness tests: fit/predict/metric plumbing with known answers.

``IdentityExtractor`` reads each candidate's ``feature`` scalar, so centroid
distances have hand-computable answers — the harness itself is what is under
test here, not any backbone.
"""

from __future__ import annotations

import numpy as np
import pytest
from typer.testing import CliRunner

from keyless.gaze.experiment import EmbeddingHead, IdentityExtractor, evaluate_experiment
from keyless.main import app


def _event(target: str, mouse: list[float], pixel: list[float], candidates: list[dict]) -> dict:
    return {
        "ts_ns": 1_000_000_000,
        "screen": {"candidates": candidates},
        "prediction": {"top": None, "p": 0.0, "entropy": 0.0, "pixel": pixel},
        "interaction": {"kind": "click", "accepted": True, "target": target, "mouse": mouse},
    }


def _candidate(candidate_id: str, feature: float) -> dict:
    side = feature * 10.0
    return {
        "id": candidate_id,
        "bbox": [side, side, 20.0, 20.0],
        "role": "button",
        "score": 0.5,
        "feature": feature,
    }


# Centroids come only from frames where a candidate WAS the clicked target,
# and predict scores each candidate by distance to its own centroid.  So the
# fixtures put the clicked candidate's feature closest to its own centroid
# and let the others drift (as real embeddings vary frame to frame).
TRAIN = [
    _event("c1", [10.0, 10.0], [10.0, 10.0], [_candidate("c1", 10.0), _candidate("c2", 21.0), _candidate("c3", 38.3)]),
    _event("c2", [230.0, 230.0], [230.0, 230.0], [_candidate("c1", 9.7), _candidate("c2", 20.9), _candidate("c3", 37.9)]),
    _event("c3", [370.0, 370.0], [370.0, 370.0], [_candidate("c1", 10.2), _candidate("c2", 21.2), _candidate("c3", 37.8)]),
]

# Truth c2, but the recorded pixel sits at c1: the probe should get top1
# right from features while Baseline 0 gets it wrong from the pixel.
TEST = [
    _event("c2", [230.0, 230.0], [10.0, 10.0], [_candidate("c1", 9.6), _candidate("c2", 20.95), _candidate("c3", 38.0)]),
]


def test_probe_beats_pixel_baseline_on_heldout_split() -> None:
    report = evaluate_experiment(IdentityExtractor(), TRAIN, TEST)
    assert report["train"]["top1"] == 1.0
    assert report["test"]["top1"] == 1.0
    assert report["test"]["baseline_top1"] == 0.0
    assert report["test"]["top3"] == 1.0
    assert report["test"]["candidate_recall@3"] == 1.0
    assert report["extractor"]["fitted_ids"] == ["c1", "c2", "c3"]
    assert report["extractor"]["n_train_samples"] == 3


def test_probe_abstains_when_no_candidate_has_a_fitted_centroid() -> None:
    unseen = _event("c4", [440.0, 440.0], [440.0, 440.0], [_candidate("c4", 44.0)])
    head = EmbeddingHead(IdentityExtractor()).fit(TRAIN)
    assert head.predict(unseen) is None
    table = head.evaluate([unseen])
    assert table["abstention"] == 1.0
    assert table["top1"] == 0.0


def test_fit_rejects_unlabelled_or_unembedded_events() -> None:
    with pytest.raises(ValueError):
        EmbeddingHead(IdentityExtractor()).fit([])
    unembedded = _event("c1", [10.0, 10.0], [10.0, 10.0], [{"id": "c1", "bbox": [0.0, 0.0, 20.0, 20.0]}])
    with pytest.raises(ValueError):
        EmbeddingHead(IdentityExtractor()).fit([unembedded])


def test_extractor_must_be_callable_and_return_finite_1d_vectors() -> None:
    with pytest.raises(TypeError):
        EmbeddingHead("not-callable")  # type: ignore[arg-type]

    def bad(event, candidate):
        return np.array([np.nan])

    head = EmbeddingHead(bad)
    with pytest.raises(TypeError):
        head.fit(TRAIN)


def test_predict_rejects_extractors_that_change_feature_length() -> None:
    calls = {"n": 0}

    def shifting(event, candidate):
        calls["n"] += 1
        return np.array([1.0, 2.0]) if calls["n"] == 1 else np.array([1.0, 2.0, 3.0])

    head = EmbeddingHead(shifting).fit(TRAIN[:1])
    with pytest.raises(ValueError):
        head.predict(TRAIN[0])


runner = CliRunner()


def _record_probe_session(paths, session_id: str) -> None:
    """Two labelled clicks per session; targets share ids across sessions."""
    from keyless.gaze.collect import SessionCollector
    from keyless.gaze.schema import make_event

    collector = SessionCollector(paths, session_id=session_id)
    for ts, mouse in ((1_000_000_000, [110.0, 110.0]), (2_000_000_000, [112.0, 115.0])):
        collector.record(
            make_event(
                session_id,
                ts,
                screen={
                    "candidates": [
                        {"id": "c1", "bbox": [100.0, 100.0, 20.0, 20.0], "role": "button", "score": 0.5, "feature": 10.0},
                        {"id": "c2", "bbox": [230.0, 230.0, 20.0, 20.0], "role": "button", "score": 0.5, "feature": 23.0},
                    ]
                },
                # Pixel deliberately lands inside the WRONG box (c2) so Baseline 0
                # mispredicts while the feature probe has room to beat it.
                prediction={"top": None, "p": 0.0, "entropy": 0.0, "pixel": [240.0, 240.0]},
                interaction={"kind": "click", "mouse": mouse, "target": "c1", "accepted": True},
            )
        )
    collector.close()


def test_probe_cli_runs_end_to_end_on_recorded_sessions(tmp_path) -> None:
    from keyless.config.paths import HOME_ENV, default_paths

    paths = default_paths(env={HOME_ENV: str(tmp_path)}, platform_name="linux")
    paths.ensure_directories()
    _record_probe_session(paths, "s-a")
    _record_probe_session(paths, "s-b")

    result = runner.invoke(
        app,
        [
            "gaze",
            "probe",
            str(paths.data_dir / "sessions"),
            "--extractor",
            "keyless.gaze.experiment:IdentityExtractor",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "baseline_top1" in result.output
    assert "probe fitted 1 candidate id(s)" in result.output
    assert "labelled sample(s)" in result.output
    assert "beats baseline_top1 on held-out sessions" in result.output


def test_probe_cli_fails_honestly_without_sessions(tmp_path) -> None:
    empty = tmp_path / "sessions"
    empty.mkdir()
    result = runner.invoke(
        app,
        ["gaze", "probe", str(empty), "--extractor", "keyless.gaze.experiment:IdentityExtractor"],
    )
    assert result.exit_code == 1
    assert "no sessions" in result.output


def test_probe_cli_rejects_malformed_extractor_spec(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    result = runner.invoke(app, ["gaze", "probe", str(sessions), "--extractor", "no-colon"])
    assert result.exit_code == 1
    assert "module:callable" in result.output
