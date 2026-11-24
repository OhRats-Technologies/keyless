"""Smooth-pursuit warm-up: stimulus plan, latency estimate, and joint gaze + rig fit.

The user follows one salient, slowly moving target ("follow the light") for
about 10 s per display. Compared with a dot grid this yields
- ~30 labels per second instead of one per dot, with no clicking or waiting;
- continuous coverage of each display, including corners;
- a direct estimate of eye-to-sensor latency: the lag ``tau`` that best aligns
  the feature stream with the known target trajectory;
- relative display placement: each display's angular centre is fitted jointly
  with the gaze weights, then projected onto the no-overlap constraint set
  (``displays.place_displays``).

This is a short warm-up training set (GREENFIELD §6.3 "Cold start"), never a
startup gate and never a global offset. Its solution becomes the prior of the
online learner (``learner.GazeLearner``); natural clicks refine it from there.

Pursuit physiology used here: smooth pursuit gain stays near 0.9 below about
20-30 deg/s; peak target speed is capped at 16 deg/s. The first 350 ms of each
sweep (catch-up saccade) and the first 450 ms of each hold (re-fixation) are
excluded from labels.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from keyless.gaze.displays import DisplayPlane, DisplayRig, place_displays
from keyless.gaze.layout_solver import os_rows

PEAK_SPEED_DEG_S = 16.0
SWEEP_SETTLE_S = 0.35
HOLD_SETTLE_S = 0.45
HOLD_S = 1.15
TRAVEL_S = 0.7
INTRO_S = 1.4
OUTRO_S = 0.9
PURSUIT_SWAP_PENALTY = 12.0


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


@dataclass(frozen=True)
class Segment:
    kind: str            # intro | hold | sweep | travel | outro
    t0: float
    t1: float
    connector: str
    p0: tuple[float, float]
    p1: tuple[float, float]
    # sweep parameters (pixel amplitudes, frequencies)
    ax: float = 0.0
    ay: float = 0.0
    fx: float = 0.0
    fy: float = 0.0

    def at(self, t: float) -> tuple[float, float, float]:
        """Return ``(x, y, size)`` where size in [0, 1] drives the stimulus radius."""
        u = (t - self.t0) / max(1e-6, self.t1 - self.t0)
        if self.kind == "sweep":
            s = t - self.t0
            x = self.p0[0] + self.ax * math.sin(2.0 * math.pi * self.fx * s)
            y = self.p0[1] + self.ay * math.sin(2.0 * math.pi * self.fy * s)
            return x, y, 0.62
        if self.kind == "travel":
            e = _ease(u)
            return (
                self.p0[0] + (self.p1[0] - self.p0[0]) * e,
                self.p0[1] + (self.p1[1] - self.p0[1]) * e,
                0.62,
            )
        if self.kind == "hold":
            # Contract onto the centre point: the shrinking ring pulls fixation
            # onto the pinpoint where the label is defined.
            return self.p0[0], self.p0[1], 0.62 - 0.30 * _ease(u / 0.8)
        if self.kind == "intro":
            return self.p0[0], self.p0[1], 0.62 * _ease(u)
        # outro
        return self.p0[0], self.p0[1], 0.62 * (1.0 - _ease(u))

    def label_ok(self, t: float) -> bool:
        if self.kind == "sweep":
            return t - self.t0 >= SWEEP_SETTLE_S
        if self.kind == "hold":
            return t - self.t0 >= HOLD_SETTLE_S
        return False


@dataclass
class PursuitPlan:
    segments: list[Segment]
    duration: float
    order: list[str]

    def segment_at(self, t: float) -> Segment | None:
        if t < 0.0 or t >= self.duration:
            return None
        lo, hi = 0, len(self.segments) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            seg = self.segments[mid]
            if t < seg.t0:
                hi = mid - 1
            elif t >= seg.t1:
                lo = mid + 1
            else:
                return seg
        return None

    def at(self, t: float) -> tuple[float, float, float, Segment | None]:
        seg = self.segment_at(t)
        if seg is None:
            last = self.segments[-1]
            return last.p0[0], last.p0[1], 0.0, None
        x, y, size = seg.at(t)
        return x, y, size, seg

    def progress(self, t: float) -> float:
        return max(0.0, min(1.0, t / self.duration))

    def as_dict(self) -> dict[str, Any]:
        return {"duration": round(self.duration, 2), "order": self.order}


def _display_order(rig: DisplayRig) -> list[DisplayPlane]:
    rects = [(p.x, p.y, p.width, p.height) for p in rig.planes]
    return [rig.planes[i] for row in os_rows(rects) for i in row]


def build_plan(rig: DisplayRig, *, distance_mm: float = 600.0, seed: int = 7) -> PursuitPlan:
    """Build the stimulus timeline across every display in OS reading order."""
    rng = np.random.default_rng(seed)
    planes = _display_order(rig)
    segs: list[Segment] = []
    t = 0.0

    def add(kind: str, dur: float, conn: str, p0, p1=None, **kw) -> None:
        nonlocal t
        segs.append(Segment(kind, t, t + dur, conn, tuple(p0), tuple(p1 or p0), **kw))
        t += dur

    prev: tuple[float, float] | None = None
    for plane in planes:
        cx = plane.x + 0.5 * plane.width
        cy = plane.y + 0.5 * plane.height
        if prev is None:
            add("intro", INTRO_S, plane.connector, (cx, cy))
        else:
            add("travel", TRAVEL_S + 0.3, plane.connector, prev, (cx, cy))
        add("hold", HOLD_S, plane.connector, (cx, cy))

        # Figure-eight Lissajous (1:2) starting and ending at the centre.
        ax = 0.38 * plane.width
        ay = 0.36 * plane.height
        deg_per_px_x = math.degrees(plane.width_mm / plane.width / distance_mm)
        deg_per_px_y = math.degrees(plane.height_mm / plane.height / distance_mm)
        ax_deg, ay_deg = ax * deg_per_px_x, ay * deg_per_px_y
        period = max(7.0, 2.0 * math.pi * ax_deg / PEAK_SPEED_DEG_S,
                     2.0 * math.pi * 2.0 * ay_deg / PEAK_SPEED_DEG_S)
        add("sweep", period, plane.connector, (cx, cy), ax=ax, ay=ay, fx=1.0 / period, fy=2.0 / period)

        # Two off-centre holds give static labels for held-out validation.
        pts = [
            (plane.x + plane.width * float(rng.uniform(0.15, 0.35)), plane.y + plane.height * float(rng.uniform(0.2, 0.8))),
            (plane.x + plane.width * float(rng.uniform(0.65, 0.85)), plane.y + plane.height * float(rng.uniform(0.2, 0.8))),
        ]
        cur = (cx, cy)
        for p in pts:
            add("travel", TRAVEL_S, plane.connector, cur, p)
            add("hold", HOLD_S, plane.connector, p)
            cur = p
        prev = cur
    assert prev is not None
    add("outro", OUTRO_S, planes[-1].connector, prev)
    return PursuitPlan(segs, t, [p.connector for p in planes])


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameSample:
    t: float               # seconds since plan start (camera capture clock)
    feat: np.ndarray
    blink: float
    confidence: float
    distance_mm: float = 0.0


@dataclass
class PursuitResult:
    ok: bool
    reason: str = ""
    weights: np.ndarray | None = None           # (d+1, 2) angles = [f,1] @ W
    sample_weights_n: float = 0.0                # effective number of labels
    rig: DisplayRig | None = None
    latency_ms: float = 0.0
    latency_confidence: float = 0.0
    accuracy_deg: float = float("nan")           # held-out median angular error
    accuracy_px: float = float("nan")
    precision_deg: float = float("nan")          # fixation noise (1 sigma)
    blink_enter: float = 0.25
    blink_exit: float = 0.16
    distance_mm: float = 600.0
    swapped: bool = False
    order: list[list[str]] = field(default_factory=list)
    labels_used: int = 0
    labels_total: int = 0
    grade: str = "retry"
    covariance: np.ndarray | None = None        # learner prior covariance

    def summary(self) -> dict[str, Any]:
        def r(v: float, n: int = 2) -> float | None:
            return None if v is None or not math.isfinite(v) else round(float(v), n)

        return {
            "ok": self.ok,
            "reason": self.reason,
            "grade": self.grade,
            "latency_ms": r(self.latency_ms, 0),
            "latency_confidence": r(self.latency_confidence),
            "accuracy_deg": r(self.accuracy_deg),
            "accuracy_px": r(self.accuracy_px, 0),
            "precision_deg": r(self.precision_deg),
            "blink_enter": r(self.blink_enter, 3),
            "blink_exit": r(self.blink_exit, 3),
            "distance_mm": r(self.distance_mm, 0),
            "swapped": self.swapped,
            "order": self.order,
            "labels_used": self.labels_used,
            "labels_total": self.labels_total,
            "rig": self.rig.as_dict() if self.rig is not None else None,
        }


def grade_for(accuracy_deg: float) -> str:
    if not math.isfinite(accuracy_deg):
        return "retry"
    if accuracy_deg <= 2.0:
        return "great"
    if accuracy_deg <= 3.5:
        return "good"
    if accuracy_deg <= 6.0:
        return "okay"
    return "retry"


def _wls(X: np.ndarray, Y: np.ndarray, w: np.ndarray, alpha: float = 1e-3, free: Sequence[int] = (-1,)) -> np.ndarray:
    d = X.shape[1]
    reg = np.eye(d) * alpha
    for j in free:
        reg[j, j] = 1e-9
    Xw = X * w[:, None]
    return np.linalg.solve(X.T @ Xw + reg, Xw.T @ Y)


def _huber_fit(
    X: np.ndarray, Y: np.ndarray, base_w: np.ndarray, iters: int = 4, free: Sequence[int] = (-1,)
) -> tuple[np.ndarray, np.ndarray]:
    """IRLS Huber regression. Returns (W, robust weights)."""
    w = base_w.copy()
    W = _wls(X, Y, w, free=free)
    for _ in range(iters):
        r = np.linalg.norm(X @ W - Y, axis=1)
        scale = 1.4826 * float(np.median(r)) + 1e-6
        k = 1.5 * scale
        hub = np.where(r <= k, 1.0, k / np.maximum(r, 1e-12))
        # Hard-reject gross outliers (looked away): beyond 4 scales.
        hub[r > 4.0 * scale] = 0.0
        w = base_w * hub
        W = _wls(X, Y, w, free=free)
    return W, w


def _local_delta(plane: DisplayPlane, x: float, y: float, d: float) -> tuple[float, float]:
    """Angular offset of pixel (x, y) from its display centre, display facing the eye."""
    u, v = plane.pixel_to_uv(x, y)
    xo = (u - 0.5) * plane.width_mm
    yo = (v - 0.5) * plane.height_mm
    return math.atan2(xo, d), math.atan2(yo, math.hypot(d, xo))


def _joint_fit(
    X: np.ndarray, D: np.ndarray, mon: np.ndarray, n_mon: int, base_w: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Solve ``[x,1] W - c_m = delta`` jointly: shared weights, one offset per display.

    Rig-independent, so it needs no initial layout and cannot get stuck in the
    OS order. Display 0 is the gauge (c_0 = 0; the bias column absorbs it).
    Returns ``(W, offsets (n_mon, 2), robust weights)``.
    """
    d1 = X.shape[1]
    E = np.zeros((X.shape[0], max(0, n_mon - 1)))
    for k in range(1, n_mon):
        E[mon == k, k - 1] = -1.0
    Z = np.hstack([X, E])
    free = [d1 - 1] + list(range(d1, d1 + n_mon - 1))
    coef, rw = _huber_fit(Z, D, base_w, free=free)
    offsets = np.vstack([np.zeros((1, 2)), coef[d1:]]) if n_mon > 1 else np.zeros((1, 2))
    return coef[:d1], offsets, rw


def _targets(rig: DisplayRig, pts: np.ndarray) -> np.ndarray:
    out = np.empty((pts.shape[0], 2))
    for i, (x, y) in enumerate(pts):
        az, el, *_ = rig.pixel_to_ray_angles(float(x), float(y))
        out[i] = (az, el)
    return out


def estimate_latency(
    plan: PursuitPlan,
    frames: Sequence[FrameSample],
    rig: DisplayRig,
    *,
    distance_mm: float = 600.0,
    max_ms: float = 600.0,
    step_ms: float = 10.0,
) -> tuple[float, float, list[tuple[float, float]]]:
    """Return ``(tau_ms, confidence, curve)``: the lag that best explains sweeps.

    For each candidate lag the target is evaluated at ``t - tau`` and the
    rig-free joint model is fitted; the residual curve is minimised and refined
    parabolically. Confidence = 1 - min/max residual over the curve, and 0 when
    the minimum sits on the window edge (the true lag is outside the window).
    The window is wide because a Mac camera + browser stage measured >= 320 ms.
    """
    usable = [f for f in frames if f.blink < 0.3]
    if len(usable) < 60:
        return 0.0, 0.0, []
    planes = {p.connector: (k, p) for k, p in enumerate(rig.planes)}
    X = np.array([np.concatenate([f.feat, [1.0]]) for f in usable])
    ts = np.array([f.t for f in usable])
    curve: list[tuple[float, float]] = []
    for tau_ms in np.arange(0.0, max_ms + 1e-9, step_ms):
        tau = tau_ms / 1000.0
        rows, deltas, mons = [], [], []
        for i, t in enumerate(ts):
            seg = plan.segment_at(t - tau)
            if seg is not None and seg.kind == "sweep" and seg.label_ok(t - tau):
                x, y, _ = seg.at(t - tau)
                k, plane = planes[seg.connector]
                rows.append(i)
                deltas.append(_local_delta(plane, x, y, distance_mm))
                mons.append(k)
        if len(rows) < 40:
            continue
        Xs = X[rows]
        D = np.array(deltas)
        mon = np.array(mons)
        W, off, _ = _joint_fit(Xs, D, mon, len(rig.planes), np.ones(len(rows)))
        pred = Xs @ W - off[mon]
        # Median residual: robust to look-away frames that plain RMS would chase.
        res = float(np.median(np.linalg.norm(pred - D, axis=1)))
        curve.append((float(tau_ms), res))
    if len(curve) < 3:
        return 0.0, 0.0, curve
    vals = np.array([c[1] for c in curve])
    k = int(np.argmin(vals))
    tau = curve[k][0]
    if 0 < k < len(curve) - 1:
        a, b, c = vals[k - 1], vals[k], vals[k + 1]
        denom = a - 2.0 * b + c
        if denom > 1e-12:
            tau += 0.5 * step_ms * (a - c) / denom
    if k == len(curve) - 1:
        return float(tau), 0.0, curve
    conf = max(0.0, min(1.0, 1.0 - float(vals[k]) / max(1e-9, float(vals.max()))))
    return float(tau), conf, curve


def adaptive_blink_thresholds(levels: Sequence[float]) -> tuple[float, float]:
    """Per-user blink thresholds from the open-eye baseline (median + robust spread)."""
    arr = np.asarray([v for v in levels if math.isfinite(v)], dtype=np.float64)
    if arr.size < 30:
        return 0.25, 0.16
    base = float(np.median(arr))
    mad = float(np.median(np.abs(arr - base))) * 1.4826
    enter = min(0.6, max(0.18, base + max(0.12, 6.0 * mad)))
    exit_ = base + 0.5 * (enter - base)
    return enter, exit_


def solve_pursuit(
    plan: PursuitPlan,
    frames: Sequence[FrameSample],
    base_rig: DisplayRig,
    *,
    distance_mm: float | None = None,
) -> PursuitResult:
    """Fit gaze weights, display placement, latency, and thresholds from one run."""
    if len(frames) < 90:
        return PursuitResult(ok=False, reason="not_enough_face", labels_total=len(frames))

    dists = [f.distance_mm for f in frames if 250.0 < f.distance_mm < 1500.0]
    d = float(distance_mm or (np.median(dists) if dists else 600.0))
    d = max(350.0, min(1100.0, d))

    blink_enter, blink_exit = adaptive_blink_thresholds([f.blink for f in frames])

    flat_rig, _ = place_displays(base_rig, {}, distance_mm=d)
    clean = [f for f in frames if f.blink < blink_enter]
    tau_ms, tau_conf, _ = estimate_latency(plan, clean, base_rig, distance_mm=d)
    tau = tau_ms / 1000.0

    planes = {p.connector: (k, p) for k, p in enumerate(base_rig.planes)}
    # Label every clean frame whose lagged target sits in a sweep or settled hold.
    rows: list[int] = []
    pts: list[tuple[float, float]] = []
    deltas: list[tuple[float, float]] = []
    mons: list[int] = []
    kinds: list[str] = []
    hold_ids: list[int] = []
    for i, f in enumerate(clean):
        seg = plan.segment_at(f.t - tau)
        if seg is None or not seg.label_ok(f.t - tau):
            continue
        x, y, _ = seg.at(f.t - tau)
        k, plane = planes[seg.connector]
        rows.append(i)
        pts.append((x, y))
        deltas.append(_local_delta(plane, x, y, d))
        mons.append(k)
        kinds.append(seg.kind)
        hold_ids.append(id(seg) if seg.kind == "hold" else -1)
    if len(rows) < 60:
        return PursuitResult(ok=False, reason="not_following", labels_total=len(frames))

    X = np.array([np.concatenate([clean[i].feat, [1.0]]) for i in rows])
    P = np.array(pts)
    D = np.array(deltas)
    mon = np.array(mons)
    is_sweep = np.array([k == "sweep" for k in kinds])
    base_w = np.array([max(0.2, min(1.0, clean[i].confidence)) for i in rows])

    # 1) Rig-free joint fit -> each display's angular centre (up to a common shift).
    meta: dict[str, Any] = {"reordered": False, "order": []}
    rig = flat_rig
    if len(base_rig.planes) >= 2:
        _, offsets, rw = _joint_fit(X, D, mon, len(base_rig.planes), base_w)
        estimates: dict[str, tuple[float, float, float]] = {}
        for conn, (k, _plane) in planes.items():
            m = (mon == k) & (rw > 0)
            if int(m.sum()) < 20:
                continue
            # Frames are autocorrelated: ~1 independent label per 100 ms.
            n_eff = max(1.0, float(m.sum()) / 3.0)
            sigma = math.radians(1.5)
            estimates[conn] = (float(offsets[k, 0]), float(offsets[k, 1]), n_eff / sigma**2)
        if estimates:
            az_vals = [v[0] for v in estimates.values()]
            shift_az = 0.5 * (min(az_vals) + max(az_vals))
            shift_el = float(np.mean([v[1] for v in estimates.values()]))
            estimates = {k: (v[0] - shift_az, v[1] - shift_el, v[2]) for k, v in estimates.items()}
            rig, meta = place_displays(
                base_rig, estimates, distance_mm=d, swap_penalty=PURSUIT_SWAP_PENALTY
            )

    # 2) Final weights against the solved rig's exact ray angles.
    T = _targets(rig, P)
    W, robust_w = _huber_fit(X, T, base_w)

    # Held-out accuracy: fit on sweeps only, score on settled holds.
    hold_mask = ~is_sweep
    acc_deg = float("nan")
    acc_px = float("nan")
    precision = float("nan")
    if int(is_sweep.sum()) >= 40 and int(hold_mask.sum()) >= 10:
        Ws, _ = _huber_fit(X[is_sweep], T[is_sweep], base_w[is_sweep])
        pred = X[hold_mask] @ Ws
        # Score each hold by its median prediction (what a dwell-based UI sees).
        hold_arr = np.array(hold_ids)[hold_mask]
        errs, errs_px, spreads = [], [], []
        for hid in np.unique(hold_arr):
            m = hold_arr == hid
            med = np.median(pred[m], axis=0)
            tgt = T[hold_mask][m][0]
            errs.append(float(np.hypot(*(med - tgt))))
            spreads.append(float(np.sqrt(np.mean(np.sum((pred[m] - med) ** 2, axis=1)))))
            hit = rig.ray_angles_to_pixel(float(med[0]), float(med[1]))
            tp = P[hold_mask][m][0]
            errs_px.append(float(np.hypot(hit.x - tp[0], hit.y - tp[1])))
        acc_deg = math.degrees(float(np.median(errs)))
        acc_px = float(np.median(errs_px))
        precision = math.degrees(float(np.median(spreads)))

    used = int((robust_w > 0).sum())
    grade = grade_for(acc_deg)
    # Learner prior covariance. Frames are autocorrelated (~1 independent label
    # per 100 ms, hence /3), and the next session will sit slightly differently
    # (x4 inflation), so clicks keep real influence after calibration.
    from keyless.gaze.learner import DEFAULT_R

    d1 = X.shape[1]
    A = np.eye(d1) / 0.5 + (X.T * (robust_w / 3.0)) @ X / DEFAULT_R
    cov = 4.0 * np.linalg.inv(A)
    return PursuitResult(
        ok=grade != "retry",
        reason="" if grade != "retry" else "low_accuracy",
        weights=W,
        sample_weights_n=float(robust_w.sum()) / 3.0,
        rig=rig,
        latency_ms=tau_ms,
        latency_confidence=tau_conf,
        accuracy_deg=acc_deg,
        accuracy_px=acc_px,
        precision_deg=precision,
        blink_enter=blink_enter,
        blink_exit=blink_exit,
        distance_mm=d,
        swapped=bool(meta.get("reordered")),
        order=list(meta.get("order") or []),
        labels_used=used,
        labels_total=len(frames),
        grade=grade,
        covariance=0.5 * (cov + cov.T),
    )


def _plane_center_angles(p: DisplayPlane) -> tuple[float, float]:
    from keyless.gaze.displays import angles_from_ray

    return angles_from_ray(p.uv_to_3d(0.5, 0.5))


__all__ = [
    "FrameSample",
    "PursuitPlan",
    "PursuitResult",
    "Segment",
    "adaptive_blink_thresholds",
    "build_plan",
    "estimate_latency",
    "grade_for",
    "solve_pursuit",
]
