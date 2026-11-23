"""Stage-2 experiment harness: frozen embeddings + a label-free linear head.

This is the first model-shaped step of the Gaze v2 flywheel, deliberately
the weakest possible one (GREENFIELD.md §6.3 scorer, §9 kill criteria):

1. A *frozen* extractor turns one candidate, given the whole frame, into a
   fixed-length numeric feature vector.  Nothing here trains it.  Embedding-
   Gemma 2 (or any future backbone) plugs in as a callable; nothing here
   depends on it.
2. A tiny linear head per feature id: the class centroid of that candidate's
   features over the training sessions.  This is a linear probe of whether
   the embedding space separates the clicked target from the other
   candidates at all — it says nothing about a trained scorer yet.
3. Evaluation reuses :func:`keyless.gaze.metrics.metric_table` with
   ``prediction.top`` replaced by the head's choice, so the numbers are the
   same contract every later model claim must match, including the
   ``baseline_top1`` (nearest-candidate-to-pixel) the head must beat on
   session-held-out data.

Splits are session-level (see :mod:`keyless.gaze.replay`): never mix frames
from one session across train and test.  If the probe's test ``top1`` does
not beat ``baseline_top1``, the backbone hypothesis is dead for this task —
that is the falsifier, not a reason to add layers.

No heavy model loads happen here: the extractor is injected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

import numpy as np

from keyless.gaze.metrics import Event, metric_table

__all__ = [
    "EmbeddingExtractor",
    "EmbeddingHead",
    "IdentityExtractor",
    "evaluate_experiment",
]


class EmbeddingExtractor(Protocol):
    """Map one whole event to a numeric candidate-feature vector.

    The extractor sees the entire frame — screen, candidates, interaction,
    prediction — and returns the feature vector for *one* candidate chosen
    by the caller (the harness iterates candidates and calls the extractor
    once per candidate via :meth:`EmbeddingHead.score`).  It must return the
    same fixed length every time, or ``None`` to mark the frame as
    un-embeddable (missing screenshot, extractor failure).  Returning
    ``None`` is honest abstention, not an error to paper over.
    """

    def __call__(self, event: Event, candidate: Mapping[str, Any]) -> np.ndarray | None: ...


def _validate_feature(value: Any) -> np.ndarray:
    if isinstance(value, np.ndarray) and value.ndim == 1 and value.size > 0:
        vector = value
    elif isinstance(value, (list, tuple)) and len(value) > 0:
        vector = np.asarray(value, dtype=np.float64)
    else:
        raise TypeError(
            "extractor must return a non-empty 1-D numeric array (or None); got "
            f"{type(value).__name__}"
        )
    if vector.dtype.kind not in "ifc" or not np.all(np.isfinite(vector)):
        raise TypeError("extractor features must be finite numbers")
    return vector


def _candidate_entries(event: Event) -> list[Mapping[str, Any]]:
    screen = event.get("screen")
    if not isinstance(screen, Mapping):
        return []
    candidates = screen.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, Mapping)]


class EmbeddingHead:
    """Per-candidate-id class centroids in frozen-feature space.

    ``fit(train_events)`` stores, per labelled target id, the mean feature
    vector of that candidate across training frames.  ``predict(event)``
    scores every candidate that has a centroid by negative Euclidean
    distance to its centroid and returns the best id; it abstains (``None``)
    when no candidate in the frame has a fitted centroid.  The centroid is
    the *linear probe*: a single weight vector per class, no hidden layers,
    no fine-tuning.
    """

    def __init__(self, extract: EmbeddingExtractor) -> None:
        if not callable(extract):
            raise TypeError("extract must be callable")
        self._extract = extract
        self._centroids: dict[str, np.ndarray] = {}
        self._counts: dict[str, int] = {}

    # --- fitting ---------------------------------------------------------

    def fit(self, events: Iterable[Event]) -> "EmbeddingHead":
        """Accumulate target-id centroids from labelled click events."""
        self._centroids.clear()
        self._counts.clear()
        for event in events:
            target_id, target_candidate = self._labelled_target(event)
            if target_id is None or target_candidate is None:
                continue
            feature = self._extract(event, target_candidate)
            if feature is None:
                continue
            vector = _validate_feature(feature)
            total = self._centroids.get(target_id)
            if total is None:
                self._centroids[target_id] = vector.copy()
                self._counts[target_id] = 1
            else:
                self._centroids[target_id] = total + vector
                self._counts[target_id] += 1
        if not self._centroids:
            raise ValueError(
                "EmbeddingHead.fit saw no labelled click events with candidate "
                "features; check the session labels and the extractor"
            )
        for target_id, count in self._counts.items():
            self._centroids[target_id] = self._centroids[target_id] / count
        return self

    def _labelled_target(
        self, event: Event
    ) -> tuple[str | None, Mapping[str, Any] | None]:
        interaction = event.get("interaction")
        if not isinstance(interaction, Mapping) or interaction.get("kind") != "click":
            return None, None
        target = interaction.get("target")
        if target is None:
            return None, None
        for candidate in _candidate_entries(event):
            if str(candidate.get("id", "")) == str(target):
                return str(target), candidate
        return str(target), None

    # --- inference -------------------------------------------------------

    def predict(self, event: Event) -> str | None:
        """Best candidate id by negative distance to its fitted centroid."""
        best_id: str | None = None
        best_score = -np.inf
        for candidate in _candidate_entries(event):
            candidate_id = str(candidate.get("id", ""))
            centroid = self._centroids.get(candidate_id)
            if centroid is None:
                continue
            feature = self._extract(event, candidate)
            if feature is None:
                continue
            vector = _validate_feature(feature)
            if vector.shape != centroid.shape:
                raise ValueError(
                    f"extractor returned a {vector.shape} feature for candidate "
                    f"{candidate_id!r}, but the fitted centroid is {centroid.shape}; "
                    "extractors must return a fixed length"
                )
            score = -float(np.linalg.norm(vector - centroid))
            if score > best_score or (
                score == best_score and best_id is not None and candidate_id < best_id
            ):
                best_score = score
                best_id = candidate_id
        return best_id

    # --- evaluation ------------------------------------------------------

    def evaluate(self, events: Iterable[Event]) -> dict[str, float | None]:
        """Run the standard metric suite with ``prediction.top`` replaced.

        Input events are not mutated.  The returned table is
        :func:`keyless.gaze.metrics.metric_table`'s contract verbatim:
        ``top1`` is this head, ``baseline_top1`` is the nearest-candidate
        baseline computed over the same events.
        """
        derived: list[dict[str, Any]] = []
        for event in events:
            prediction = event.get("prediction")
            if isinstance(prediction, Mapping):
                derived.append(
                    {**event, "prediction": {**prediction, "top": self.predict(event)}}
                )
            else:
                derived.append(dict(event))
        return metric_table(derived)

    # --- introspection ---------------------------------------------------

    def fitted_ids(self) -> list[str]:
        """Target ids with a centroid — what the probe can score at all."""
        return sorted(self._centroids)

    def n_train(self) -> int:
        """Total labelled candidate samples consumed by ``fit``."""
        return sum(self._counts.values())


def evaluate_experiment(
    extract: EmbeddingExtractor,
    train_events: Iterable[Event],
    test_events: Iterable[Event],
) -> dict[str, Any]:
    """Fit the probe on training sessions, report both splits.

    Returns ``{"train": table, "test": table, "extractor": {...}}``.  The
    headline comparison is ``test["top1"]`` vs ``test["baseline_top1"]``;
    per GREENFIELD.md §9 the model claim only survives if it wins on the
    held-out split.  ``extractor`` records how many ids were fitted and
    frames embedded, so an honest abstention rate is visible.
    """
    train = list(train_events)
    test = list(test_events)
    head = EmbeddingHead(extract).fit(train)
    return {
        "train": head.evaluate(train),
        "test": head.evaluate(test),
        "extractor": {
            "fitted_ids": head.fitted_ids(),
            "n_train_samples": head.n_train(),
        },
    }


class IdentityExtractor:
    """Trivial extractor for wiring the harness without any backbone.

    Reads ``event["probe_feature"]`` (or the candidate's ``"feature"`` key)
    as a scalar.  Used by tests to prove the fit/predict/metric plumbing
    end-to-end with a known answer.  Not a model, not a baseline — just the
    harness's own smoke test.
    """

    def __call__(self, event: Event, candidate: Mapping[str, Any]) -> np.ndarray | None:
        raw = candidate.get("feature", event.get("probe_feature"))
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return np.array([float(raw)], dtype=np.float64)
