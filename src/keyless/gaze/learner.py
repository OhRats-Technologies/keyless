"""Silent online gaze learner: Bayesian random-walk regression with gated click labels.

Replaces "every click is ground truth" (brittle: users click without looking,
click remembered button positions, or have already moved their eyes on) with
a model that treats a click as a *possibly* looked-at label.

Model. Gaze angles ``a = W^T x`` with ``x = [features, 1]``. The weights follow
a random walk ``W_t = W_{t-1} + noise`` (head pose, lighting, and seating drift
over minutes), so this is a Kalman filter on ``W`` with a matrix-normal
posterior ``W ~ MN(M, P, r I_2)``. Both outputs share ``P`` because they share
the same regressors.

Prior. Either the pursuit-calibration solution (tight ``P``) or the linear
cold-start prior (wide ``P``). There is no ``min_clicks`` switch and no
prior/ridge blend ramp: the posterior moves smoothly from prior to data.

Label gate. For a click at target angles ``t`` the predictive distribution is
``N(M^T x, s I)`` with ``s = x^T P x + r + r_click``. The label weight is the
posterior probability that the user looked at the target:

    w = pi * N(t) / (pi * N(t) + (1 - pi) * U)

with ``U`` uniform over the display's angular area. When the model is
uncertain every click counts; once it is confident, clicks far from the gaze
are ignored. The weight lies in [0, 1] and is monotone in the likelihood (Lean:
``ClickGate.lean``). The update uses noise ``r / w``.

Everything is reversible: the log of accepted updates replays from the prior.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

# Observation noise on angles (rad^2): ~1.2 deg webcam iris noise.
DEFAULT_R = math.radians(1.2) ** 2
# Click target extent: users look somewhere on the control, not its exact pixel.
DEFAULT_R_CLICK = math.radians(1.0) ** 2
# Drift per second (rad^2 per weight unit). Bias drifts faster than gains.
DEFAULT_Q_BIAS = math.radians(0.6) ** 2 / 60.0
DEFAULT_Q_GAIN = 1e-5 / 60.0
LOOK_PRIOR = 0.85  # prior P(user looked at what they clicked)


def inlier_weight(residual: np.ndarray, s: float, area_sr: float, pi: float = LOOK_PRIOR) -> float:
    """Posterior probability that a click was looked at (2-D isotropic Gaussian vs uniform)."""
    r2 = float(residual @ residual)
    s = max(float(s), 1e-12)
    like = math.exp(-0.5 * r2 / s) / (2.0 * math.pi * s)
    unif = 1.0 / max(area_sr, 1e-6)
    num = pi * like
    return num / (num + (1.0 - pi) * unif)


@dataclass
class _Update:
    x: np.ndarray
    y: np.ndarray
    weight: float
    t: float


@dataclass
class GazeLearner:
    """Kalman filter over linear gaze weights with gated, reversible click updates."""

    M0: np.ndarray
    P0: np.ndarray
    r: float = DEFAULT_R
    r_click: float = DEFAULT_R_CLICK
    q_bias: float = DEFAULT_Q_BIAS
    q_gain: float = DEFAULT_Q_GAIN
    max_log: int = 512
    M: np.ndarray = field(init=False)
    P: np.ndarray = field(init=False)
    _log: list[_Update] = field(init=False, default_factory=list)
    _t_last: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.M0 = np.asarray(self.M0, dtype=np.float64).copy()
        self.P0 = np.asarray(self.P0, dtype=np.float64).copy()
        self.M = self.M0.copy()
        self.P = self.P0.copy()

    @property
    def dim(self) -> int:
        return int(self.M.shape[0])

    @property
    def n_updates(self) -> int:
        return len(self._log)

    @staticmethod
    def augment(feat: np.ndarray) -> np.ndarray:
        f = np.asarray(feat, dtype=np.float64).reshape(-1)
        return np.concatenate([f, [1.0]])

    def predict(self, x: np.ndarray) -> tuple[np.ndarray, float]:
        """Return ``(angles, predictive weight variance x^T P x)``."""
        return x @ self.M, float(x @ self.P @ x)

    def _diffuse(self, dt: float) -> None:
        if dt <= 0.0:
            return
        dt = min(dt, 3600.0)
        q = np.full(self.dim, self.q_gain * dt)
        q[-1] = self.q_bias * dt
        self.P = self.P + np.diag(q)

    def _apply(self, x: np.ndarray, y: np.ndarray, weight: float) -> None:
        r_eff = (self.r + self.r_click) / max(weight, 1e-3)
        Px = self.P @ x
        s = float(x @ Px) + r_eff
        k = Px / s
        self.M = self.M + np.outer(k, y - x @ self.M)
        self.P = self.P - np.outer(k, Px)
        self.P = 0.5 * (self.P + self.P.T)

    def gate(self, x: np.ndarray, y: np.ndarray, area_sr: float) -> float:
        pred, var_w = self.predict(x)
        return inlier_weight(np.asarray(y) - pred, var_w + self.r + self.r_click, area_sr)

    def observe(
        self,
        feat: np.ndarray,
        target: tuple[float, float],
        *,
        area_sr: float,
        t: float | None = None,
        min_weight: float = 0.05,
    ) -> float:
        """Gate and apply one click label. Returns the weight used (0 = rejected)."""
        now = time.monotonic() if t is None else float(t)
        x = self.augment(feat)
        if x.shape[0] != self.dim:
            raise ValueError("feature dimension mismatch")
        y = np.asarray(target, dtype=np.float64)
        if self._t_last is not None:
            self._diffuse(now - self._t_last)
        self._t_last = now
        w = self.gate(x, y, area_sr)
        if w < min_weight:
            return 0.0
        self._apply(x, y, w)
        self._log.append(_Update(x, y, w, now))
        if len(self._log) > self.max_log:
            # Fold the oldest update into the prior so replay stays bounded.
            self._log.pop(0)
            self._rebase_from_log()
        return w

    def fit_batch(self, X: np.ndarray, Y: np.ndarray, weights: np.ndarray | None = None) -> None:
        """Set the prior to a weighted batch posterior (used by pursuit calibration)."""
        X = np.asarray(X, dtype=np.float64)
        Y = np.asarray(Y, dtype=np.float64)
        w = np.ones(X.shape[0]) if weights is None else np.asarray(weights, dtype=np.float64)
        P0_inv = np.linalg.inv(self.P0)
        A = P0_inv + (X.T * w) @ X / self.r
        P = np.linalg.inv(A)
        M = P @ (P0_inv @ self.M0 + (X.T * w) @ Y / self.r)
        self.M0, self.P0 = M, 0.5 * (P + P.T)
        self.reset()

    def _rebase_from_log(self) -> None:
        self.M, self.P = self.M0.copy(), self.P0.copy()
        t_prev: float | None = None
        for u in self._log:
            if t_prev is not None:
                self._diffuse(u.t - t_prev)
            t_prev = u.t
            self._apply(u.x, u.y, u.weight)

    def undo_last(self) -> int:
        if self._log:
            self._log.pop()
            self._rebase_from_log()
            self._t_last = self._log[-1].t if self._log else None
        return len(self._log)

    def reset(self) -> None:
        self._log.clear()
        self.M, self.P = self.M0.copy(), self.P0.copy()
        self._t_last = None


__all__ = ["GazeLearner", "inlier_weight", "LOOK_PRIOR"]
