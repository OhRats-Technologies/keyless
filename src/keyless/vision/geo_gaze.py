"""Geometric gaze model: per-camera eye rays cast onto physical monitor planes.

Contract
--------
Input is the existing 20-D stereo feature vector from ``landmarks.build_stereo_feature_vector``.
Each 10-D camera block is ``[dxA, dyA, dxB, dyB, yaw_los, pitch_los, roll, tx, ty, tz]`` with
MediaPipe pose in OpenGL camera axes (x right, y up, z toward the viewer) and translation in
metres (cm / 100).

For every camera block the model computes a 3-D gaze ray in that camera's frame:

* origin  ``o_c = t + R_h e``   (head translation + cyclopean eye offset in the face frame)
* direction ``d_c = R_h(g) d_h(theta, phi)`` with eye-in-head angles from a 6-term map of the
  binocular iris offset.

A per-camera rigid pose ``(R, c)`` maps the ray into the reference-monitor frame ``M0``
(origin = top-left corner of the calibrated monitor, x along pixel rows, y along pixel columns,
z into the screen, centimetres). Every monitor is a rectangle with its own pose in ``M0``, so
different sizes, portrait rotation, hinge angles and tilts are first-class. Head translation is
handled by geometry rather than regression extrapolation.

Online, ``GeoGazeTracker`` tracks a small drift state with an extended Kalman filter:
per camera ``[rot_x, rot_y, shift_x, shift_y]`` (camera bumps, slow angular bias) and per
non-reference monitor ``[yaw, pitch, tx, ty, tz]`` (unknown placement). Labels come from clicks
and from detected cursor chasing (see ``notes/gaze-calibration/README.md``).
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

BLOCK_DIM = 10
LOS_GAIN = 0.65  # Must match landmarks._extract_los_euler_and_translation.
# Cyclopean eyeball centre in the MediaPipe canonical face frame (cm). Approximate; an error of
# a few cm only produces a lever-arm error proportional to head rotation change.
EYE_MID_HEAD_CM = np.array([0.0, 2.6, 1.6], dtype=np.float64)
DEFAULT_CM_PER_PX = 0.0265
GEO_VERSION = "geo-1"
N_ANGLE_TERMS = 8
N_BLOCK_PARAMS = 3 + 3 + 2 + 2 * N_ANGLE_TERMS


# --------------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------------


def euler_zyx(yaw: np.ndarray, pitch: np.ndarray, roll: np.ndarray) -> np.ndarray:
    """Return ``Rz(roll) @ Ry(yaw) @ Rx(pitch)`` for arrays of angles, shape ``(N, 3, 3)``.

    Matches the decomposition in ``landmarks._extract_los_euler_and_translation``.
    """
    yaw = np.atleast_1d(yaw)
    pitch = np.atleast_1d(pitch)
    roll = np.atleast_1d(roll)
    cy, sy = np.cos(yaw), np.sin(yaw)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cr, sr = np.cos(roll), np.sin(roll)
    r = np.empty((yaw.shape[0], 3, 3), dtype=np.float64)
    r[:, 0, 0] = cr * cy
    r[:, 0, 1] = cr * sy * sp - sr * cp
    r[:, 0, 2] = cr * sy * cp + sr * sp
    r[:, 1, 0] = sr * cy
    r[:, 1, 1] = sr * sy * sp + cr * cp
    r[:, 1, 2] = sr * sy * cp - cr * sp
    r[:, 2, 0] = -sy
    r[:, 2, 1] = cy * sp
    r[:, 2, 2] = cy * cp
    return r


@dataclass(frozen=True)
class HeadObs:
    """Decoded head pose and binocular iris offsets for N frames of one camera block."""

    yaw: np.ndarray
    pitch: np.ndarray
    roll: np.ndarray
    t_cm: np.ndarray
    dx: np.ndarray
    dy: np.ndarray
    valid: np.ndarray


def decode_block(X: np.ndarray, block: int) -> HeadObs:
    """Invert the line-of-sight Euler correction and average the two eyes."""
    X = np.atleast_2d(np.asarray(X, dtype=np.float64))
    blk = X[:, block * BLOCK_DIM : (block + 1) * BLOCK_DIM]
    t = blk[:, 7:10] * 100.0
    depth = np.maximum(15.0, np.abs(t[:, 2]))
    yaw = blk[:, 4] + LOS_GAIN * np.arctan2(t[:, 0], depth)
    pitch = blk[:, 5] + LOS_GAIN * np.arctan2(t[:, 1], depth)
    valid = np.abs(t[:, 2]) > 1.0
    return HeadObs(
        yaw=yaw,
        pitch=pitch,
        roll=blk[:, 6].copy(),
        t_cm=t,
        dx=0.5 * (blk[:, 0] + blk[:, 2]),
        dy=0.5 * (blk[:, 1] + blk[:, 3]),
        valid=valid,
    )


def view_angles(obs: HeadObs) -> tuple[np.ndarray, np.ndarray]:
    """Direction from the eyes to the camera, in head coordinates (azimuth, elevation; rad).

    The iris-to-canthus offset measures eye rotation relative to this line of sight, so head
    translation (leaning, slouching) shifts the iris features exactly like a head rotation.
    """
    r_h = euler_zyx(obs.yaw, obs.pitch, obs.roll)
    o_c = obs.t_cm + np.einsum("nij,j->ni", r_h, EYE_MID_HEAD_CM)
    to_cam = -o_c / np.linalg.norm(o_c, axis=1, keepdims=True)
    v_h = np.einsum("nji,nj->ni", r_h, to_cam)
    return np.arctan2(v_h[:, 0], v_h[:, 2]), np.arcsin(np.clip(v_h[:, 1], -1.0, 1.0))


def angle_terms(dx: np.ndarray, dy: np.ndarray, vth: np.ndarray, vph: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones_like(dx), dx, dy, dx * dx, dx * dy, dy * dy, vth, vph])


def obs_terms(obs: HeadObs) -> np.ndarray:
    vth, vph = view_angles(obs)
    return angle_terms(obs.dx, obs.dy, vth, vph)


def _unpack(p: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rv = p[0:3]
    c = p[3:6]
    g = p[6:8]
    a_th = p[8 : 8 + N_ANGLE_TERMS]
    a_ph = p[8 + N_ANGLE_TERMS : 8 + 2 * N_ANGLE_TERMS]
    return rv, c, g, a_th, a_ph


def camera_rays(
    p: np.ndarray,
    obs: HeadObs,
    feats: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Gaze ray ``(origin, direction)`` in the camera frame for block parameters ``p``."""
    _, _, g, a_th, a_ph = _unpack(p)
    if feats is None:
        feats = obs_terms(obs)
    theta = feats @ a_th
    phi = feats @ a_ph
    d_h = np.column_stack([np.sin(theta) * np.cos(phi), np.sin(phi), np.cos(theta) * np.cos(phi)])
    r_h = euler_zyx(g[0] * obs.yaw, g[1] * obs.pitch, obs.roll)
    d_c = np.einsum("nij,nj->ni", r_h, d_h)
    o_c = obs.t_cm + np.einsum("nij,j->ni", r_h, EYE_MID_HEAD_CM)
    return o_c, d_c


def _rotvec_to_mat(rv: np.ndarray) -> np.ndarray:
    """Fast Rodrigues formula for a 3-vector rotation (avoids SciPy array-API overhead)."""
    rx, ry, rz = float(rv[0]), float(rv[1]), float(rv[2])
    theta2 = rx * rx + ry * ry + rz * rz
    if theta2 < 1e-16:
        return np.array(
            [[1.0, -rz, ry], [rz, 1.0, -rx], [-ry, rx, 1.0]],
            dtype=np.float64,
        )
    theta = math.sqrt(theta2)
    inv_t = 1.0 / theta
    kx, ky, kz = rx * inv_t, ry * inv_t, rz * inv_t
    c = math.cos(theta)
    s = math.sin(theta)
    v = 1.0 - c
    return np.array(
        [
            [c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s],
            [ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s],
            [kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v],
        ],
        dtype=np.float64,
    )


def rays_to_m0(
    p: np.ndarray,
    obs: HeadObs,
    delta: np.ndarray | None = None,
    feats: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Gaze rays in the reference-monitor frame, with optional drift ``[rx, ry, sx, sy]``."""
    rv, c, _, _, _ = _unpack(p)
    o_c, d_c = camera_rays(p, obs, feats=feats)
    rot = _rotvec_to_mat(rv)
    o = o_c @ rot.T + c
    d = d_c @ rot.T
    if delta is not None and (delta[0] != 0.0 or delta[1] != 0.0 or delta[2] != 0.0 or delta[3] != 0.0):
        rd = _rotvec_to_mat(np.array([delta[0], delta[1], 0.0]))
        o = (o - c) @ rd.T + c + np.array([delta[2], delta[3], 0.0])
        d = d @ rd.T
    return o, d


# --------------------------------------------------------------------------------------------
# Monitors
# --------------------------------------------------------------------------------------------


@dataclass
class MonitorGeom:
    """One physical monitor: layout rectangle (logical px) and a rectangle pose in M0 (cm)."""

    connector: str
    x: int
    y: int
    width: int
    height: int
    width_cm: float
    height_cm: float
    origin: np.ndarray = field(default_factory=lambda: np.zeros(3))
    u: np.ndarray = field(default_factory=lambda: np.array([1.0, 0.0, 0.0]))
    v: np.ndarray = field(default_factory=lambda: np.array([0.0, 1.0, 0.0]))

    @property
    def normal(self) -> np.ndarray:
        n = np.cross(self.u, self.v)
        return n / np.linalg.norm(n)

    @property
    def center(self) -> np.ndarray:
        return self.origin + 0.5 * self.width_cm * self.u + 0.5 * self.height_cm * self.v

    def contains_px(self, px: float, py: float, margin: float = 0.0) -> bool:
        return (
            self.x - margin <= px < self.x + self.width + margin
            and self.y - margin <= py < self.y + self.height + margin
        )

    def px_to_m0(self, px: np.ndarray, py: np.ndarray) -> np.ndarray:
        a = (np.asarray(px) - self.x) * (self.width_cm / self.width)
        b = (np.asarray(py) - self.y) * (self.height_cm / self.height)
        return self.origin + np.multiply.outer(a, self.u) + np.multiply.outer(b, self.v)

    def intersect(self, o: np.ndarray, d: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return ``(px, py, s)`` hits of rays on this plane; ``s <= 0`` marks a miss."""
        n = self.normal
        denom = d @ n
        safe = np.where(np.abs(denom) > 1e-6, denom, 1e-6)
        s = ((self.origin - o) @ n) / safe
        hit = o + s[:, None] * d
        rel = hit - self.origin
        a = rel @ self.u
        b = rel @ self.v
        px = self.x + a * (self.width / self.width_cm)
        py = self.y + b * (self.height / self.height_cm)
        return px, py, np.where(denom > 1e-6, s, -1.0)

    def perturbed(self, delta: np.ndarray) -> MonitorGeom:
        """Rotate about the monitor centre by ``[yaw, pitch]`` and translate by ``[tx, ty, tz]``."""
        if not np.any(delta):
            return self
        c = self.center
        rot = (
            Rotation.from_rotvec(self.v / np.linalg.norm(self.v) * delta[0])
            * Rotation.from_rotvec(self.u / np.linalg.norm(self.u) * delta[1])
        ).as_matrix()
        return MonitorGeom(
            connector=self.connector,
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            width_cm=self.width_cm,
            height_cm=self.height_cm,
            origin=c + rot @ (self.origin - c) + np.asarray(delta[2:5], dtype=np.float64),
            u=rot @ self.u,
            v=rot @ self.v,
        )

    def to_json(self) -> dict:
        return {
            "connector": self.connector,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "width_cm": self.width_cm,
            "height_cm": self.height_cm,
            "origin": self.origin.tolist(),
            "u": self.u.tolist(),
            "v": self.v.tolist(),
        }

    @classmethod
    def from_json(cls, d: dict) -> MonitorGeom:
        return cls(
            connector=d["connector"],
            x=int(d["x"]),
            y=int(d["y"]),
            width=int(d["width"]),
            height=int(d["height"]),
            width_cm=float(d["width_cm"]),
            height_cm=float(d["height_cm"]),
            origin=np.asarray(d["origin"], dtype=np.float64),
            u=np.asarray(d["u"], dtype=np.float64),
            v=np.asarray(d["v"], dtype=np.float64),
        )


@dataclass(frozen=True)
class MonitorSpec:
    """Layout rectangle in logical px plus the physical panel size in mm (0 if unknown)."""

    connector: str
    x: int
    y: int
    width: int
    height: int
    width_mm: float = 0.0
    height_mm: float = 0.0
    scale: float = 1.0


def monitor_specs_from_layout(layout: object, mm_by_connector: dict[str, tuple[float, float]] | None = None) -> list[MonitorSpec]:
    """Build specs from a ``DisplayLayout``; ``mm_by_connector`` overrides EDID sizes on MonitorInfo."""
    specs = []
    mm_by_connector = mm_by_connector or {}
    for mon in layout.monitors:  # type: ignore[attr-defined]
        default_mm = (float(getattr(mon, "width_mm", 0.0) or 0.0), float(getattr(mon, "height_mm", 0.0) or 0.0))
        w_mm, h_mm = mm_by_connector.get(mon.connector, default_mm)
        specs.append(
            MonitorSpec(
                connector=mon.connector,
                x=int(mon.x),
                y=int(mon.y),
                width=int(mon.width),
                height=int(mon.height),
                width_mm=float(w_mm),
                height_mm=float(h_mm),
                scale=float(getattr(mon, "scale", 1.0) or 1.0),
            )
        )
    return specs


def _physical_size_cm(spec: MonitorSpec) -> tuple[float, float]:
    w_mm, h_mm = spec.width_mm, spec.height_mm
    if w_mm <= 0 or h_mm <= 0:
        cm_px = DEFAULT_CM_PER_PX * spec.scale
        return spec.width * cm_px, spec.height * cm_px
    # EDID sizes describe the unrotated panel; a portrait output swaps them.
    if (spec.width >= spec.height) != (w_mm >= h_mm):
        w_mm, h_mm = h_mm, w_mm
    return w_mm / 10.0, h_mm / 10.0


def _face_viewer(mon: MonitorGeom, hinge_point: np.ndarray, axis: np.ndarray, viewer: np.ndarray) -> MonitorGeom:
    """Rotate ``mon`` about a hinge so its front face points at ``viewer`` (grid search)."""
    axis = axis / np.linalg.norm(axis)
    best, best_score = mon, -np.inf
    for deg in np.arange(-80.0, 80.5, 1.0):
        rot = Rotation.from_rotvec(axis * math.radians(deg)).as_matrix()
        cand = MonitorGeom(
            connector=mon.connector,
            x=mon.x,
            y=mon.y,
            width=mon.width,
            height=mon.height,
            width_cm=mon.width_cm,
            height_cm=mon.height_cm,
            origin=hinge_point + rot @ (mon.origin - hinge_point),
            u=rot @ mon.u,
            v=rot @ mon.v,
        )
        to_viewer = viewer - cand.center
        score = float(-(cand.normal @ to_viewer) / np.linalg.norm(to_viewer))
        if score > best_score:
            best, best_score = cand, score
    return best


def place_monitors(
    specs: Sequence[MonitorSpec],
    ref_connector: str,
    viewer_m0: np.ndarray,
) -> list[MonitorGeom]:
    """Place every monitor in M0 from the OS layout.

    The reference monitor is the M0 plane. Others are attached along shared layout edges, then
    hinged about that edge until they face the viewer, which is how people usually angle side
    and stacked monitors. Monitors without a shared edge are placed coplanar by layout offset and
    turned about their own centre. The online tracker refines every placement from labels.
    """
    geoms: dict[str, MonitorGeom] = {}
    by_conn = {s.connector: s for s in specs}
    ref = by_conn.get(ref_connector) or specs[0]
    w_cm, h_cm = _physical_size_cm(ref)
    geoms[ref.connector] = MonitorGeom(ref.connector, ref.x, ref.y, ref.width, ref.height, w_cm, h_cm)
    tol = 3

    pending = [s for s in specs if s.connector != ref.connector]
    progress = True
    while pending and progress:
        progress = False
        for spec in list(pending):
            sw, sh = _physical_size_cm(spec)
            for pc, pg in list(geoms.items()):
                p = by_conn[pc]
                ppx = pg.width_cm / pg.width
                ppy = pg.height_cm / pg.height
                overlap_y = min(p.y + p.height, spec.y + spec.height) - max(p.y, spec.y) > 0
                overlap_x = min(p.x + p.width, spec.x + spec.width) - max(p.x, spec.x) > 0
                base = MonitorGeom(spec.connector, spec.x, spec.y, spec.width, spec.height, sw, sh, u=pg.u.copy(), v=pg.v.copy())
                if overlap_y and abs(spec.x - (p.x + p.width)) <= tol:  # right of p
                    hinge = pg.origin + pg.u * pg.width_cm + pg.v * ((spec.y - p.y) * ppy)
                    base.origin = hinge.copy()
                    axis = pg.v
                elif overlap_y and abs(spec.x + spec.width - p.x) <= tol:  # left of p
                    hinge = pg.origin + pg.v * ((spec.y - p.y) * ppy)
                    base.origin = hinge - base.u * sw
                    axis = pg.v
                elif overlap_x and abs(spec.y - (p.y + p.height)) <= tol:  # below p
                    hinge = pg.origin + pg.v * pg.height_cm + pg.u * ((spec.x - p.x) * ppx)
                    base.origin = hinge.copy()
                    axis = pg.u
                elif overlap_x and abs(spec.y + spec.height - p.y) <= tol:  # above p
                    hinge = pg.origin + pg.u * ((spec.x - p.x) * ppx)
                    base.origin = hinge - base.v * sh
                    axis = pg.u
                else:
                    continue
                geoms[spec.connector] = _face_viewer(base, hinge, axis, viewer_m0)
                pending.remove(spec)
                progress = True
                break
    for spec in pending:  # disconnected in the layout: coplanar by offset, turned to face viewer
        sw, sh = _physical_size_cm(spec)
        rg = geoms[ref.connector]
        origin = np.array([(spec.x - ref.x) * rg.width_cm / rg.width, (spec.y - ref.y) * rg.height_cm / rg.height, 0.0])
        base = MonitorGeom(spec.connector, spec.x, spec.y, spec.width, spec.height, sw, sh, origin=origin)
        geoms[spec.connector] = _face_viewer(base, base.center.copy(), base.v.copy(), viewer_m0)
    return [geoms[s.connector] for s in specs]


# --------------------------------------------------------------------------------------------
# Calibration fit
# --------------------------------------------------------------------------------------------


def _orientation_from_view(head_cam: np.ndarray, cam_pos: np.ndarray, viewer: np.ndarray) -> np.ndarray:
    """Camera->M0 rotation mapping the camera->head direction onto cam_pos->viewer, roll level."""
    a = head_cam / np.linalg.norm(head_cam)
    b = np.array([0.0, 1.0, 0.0]) - a[1] * a
    b /= np.linalg.norm(b)
    a2 = viewer - cam_pos
    a2 /= np.linalg.norm(a2)
    b2 = np.array([0.0, -1.0, 0.0]) - (-a2[1]) * a2
    b2 /= np.linalg.norm(b2)
    src = np.column_stack([a, b, np.cross(a, b)])
    dst = np.column_stack([a2, b2, np.cross(a2, b2)])
    return dst @ src.T


def _solve_angle_map(rot: np.ndarray, cam_pos: np.ndarray, obs: HeadObs, targets: np.ndarray) -> tuple[np.ndarray, float]:
    """Closed-form eye-in-head map for a fixed camera pose; returns params and robust cost (rad)."""
    r_h = euler_zyx(obs.yaw, obs.pitch, obs.roll)
    o_c = obs.t_cm + np.einsum("nij,j->ni", r_h, EYE_MID_HEAD_CM)
    o = o_c @ rot.T + cam_pos
    d = targets - o
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    d_c = d @ rot
    d_h = np.einsum("nji,nj->ni", r_h, d_c)
    theta = np.arctan2(d_h[:, 0], d_h[:, 2])
    phi = np.arcsin(np.clip(d_h[:, 1], -1.0, 1.0))
    feats = obs_terms(obs)
    reg = np.diag([0.0, 1e-4, 1e-4, 1e-2, 1e-2, 1e-2, 1e-3, 1e-3]) * len(theta)
    a_th = np.linalg.solve(feats.T @ feats + reg, feats.T @ theta)
    a_ph = np.linalg.solve(feats.T @ feats + reg, feats.T @ phi)
    res = np.hypot(feats @ a_th - theta, feats @ a_ph - phi)
    forward = float(np.median(d_h[:, 2]))
    cost = float(np.median(res)) + (0.0 if forward > 0.3 else 1.0)
    p = np.concatenate([Rotation.from_matrix(rot).as_rotvec(), cam_pos, [1.0, 1.0], a_th, a_ph])
    return p, cost


def _residuals(
    p: np.ndarray,
    obs: HeadObs,
    feats: np.ndarray,
    targets_ab: np.ndarray,
    w: np.ndarray,
    n_eff: float,
    r_h_mean: np.ndarray,
    head_mean: np.ndarray,
    ref_w_cm: float,
    ref_h_cm: float,
) -> np.ndarray:
    o, d = rays_to_m0(p, obs, feats=feats)
    dz = np.where(d[:, 2] > 1e-3, d[:, 2], 1e-3)
    s = -o[:, 2] / dz
    hit = o[:, :2] + s[:, None] * d[:, :2]
    data = ((hit - targets_ab) * w[:, None]).ravel()
    rv, c, g, a_th, a_ph = _unpack(p)
    rot = _rotvec_to_mat(rv)
    # Physical soft priors eliminate the 6-DOF gauge freedom on a single calibration plane:
    # 1. During calibration on the reference monitor, the user's head roughly faces M0 (+z, up=-y).
    head_facing = np.diag([-1.0, -1.0, 1.0])
    r_err = Rotation.from_matrix(head_facing.T @ (rot @ r_h_mean)).as_rotvec()
    # 2. The camera is approximately level (no extreme roll).
    cam_roll = rot[0, 1]
    # 3. The viewer's head is in front of the reference monitor at roughly the measured distance,
    #    and the camera sits near the monitor plane (not floating 40 cm into the room).
    v_est = rot @ head_mean + c
    dist = float(np.linalg.norm(head_mean))
    k = math.sqrt(n_eff)
    prior = np.concatenate(
        [
            r_err / np.array([math.radians(15.0), math.radians(15.0), math.radians(8.0)]) * k * 0.6,
            [cam_roll / math.radians(12.0) * k * 0.5],
            (v_est[:2] - np.array([0.5 * ref_w_cm, 0.4 * ref_h_cm])) / 18.0 * k * 0.5,
            [(v_est[2] + 0.92 * dist) / 14.0 * k * 0.6],
            [(c[2] + 4.0) / 12.0 * k * 0.5],
            (g - 1.0) / 0.20 * k * 0.5,
            a_th[3:6] / 3.0 * k * 0.2,
            a_ph[3:6] / 3.0 * k * 0.2,
            # vth/vph are collinear with -yaw/-pitch during static calibration; keep strongly damped.
            a_th[6:8] / 0.06 * k * 0.6,
            a_ph[6:8] / 0.06 * k * 0.6,
        ]
    )
    return np.concatenate([data, prior])


@dataclass
class BlockFit:
    block: int
    params: np.ndarray
    sigma_px: float


def _fit_block(obs: HeadObs, targets_m0: np.ndarray, ref: MonitorGeom, weights: np.ndarray) -> BlockFit:
    r_hs = euler_zyx(obs.yaw, obs.pitch, obs.roll)
    r_h_mean = euler_zyx(
        np.array([float(np.median(obs.yaw))]),
        np.array([float(np.median(obs.pitch))]),
        np.array([float(np.median(obs.roll))]),
    )[0]
    head_mean = np.median(obs.t_cm + np.einsum("nij,j->ni", r_hs, EYE_MID_HEAD_CM), axis=0)
    dist = float(np.linalg.norm(head_mean))
    w_cm, h_cm = ref.width_cm, ref.height_cm

    head_facing = np.diag([-1.0, -1.0, 1.0])
    r0_base = head_facing @ r_h_mean.T
    z_init = -max(40.0, min(85.0, dist * 0.92))

    scored = []
    # Head-aligned initialisations (works for on-monitor, below-monitor, and off-axis cameras)
    for yaw_off_deg in (-12.0, 0.0, 12.0):
        rot = Rotation.from_rotvec([0.0, math.radians(yaw_off_deg), 0.0]).as_matrix() @ r0_base
        for hx in (-0.15 * w_cm, 0.0, 0.15 * w_cm):
            viewer = np.array([0.5 * w_cm + hx, 0.4 * h_cm, z_init])
            cam = viewer - rot @ head_mean
            p, cost = _solve_angle_map(rot, cam, obs, targets_m0)
            scored.append((cost, p))
    scored.sort(key=lambda it: it[0])

    # Subsample large replay datasets for the 24-param nonlinear fit so least_squares stays <1s.
    n_all = len(obs.yaw)
    if n_all > 900:
        idx = np.linspace(0, n_all - 1, 900, dtype=np.intp)
        fit_obs = HeadObs(
            yaw=obs.yaw[idx],
            pitch=obs.pitch[idx],
            roll=obs.roll[idx],
            t_cm=obs.t_cm[idx],
            dx=obs.dx[idx],
            dy=obs.dy[idx],
            valid=obs.valid[idx],
        )
        fit_targets_ab = targets_m0[idx, :2]
        fit_weights = weights[idx]
    else:
        fit_obs = obs
        fit_targets_ab = targets_m0[:, :2]
        fit_weights = weights

    fit_feats = obs_terms(fit_obs)
    n_eff = float(np.sum(fit_weights))
    best_p, best_cost = scored[0][1], np.inf
    for _, p0 in scored[:2]:
        sol = least_squares(
            _residuals,
            p0,
            args=(fit_obs, fit_feats, fit_targets_ab, fit_weights, n_eff, r_h_mean, head_mean, w_cm, h_cm),
            loss="soft_l1",
            f_scale=2.0,
            max_nfev=160,
            x_scale="jac",
        )
        if sol.cost < best_cost:
            best_p, best_cost = sol.x, sol.cost
    targets_ab = targets_m0[:, :2]
    o, d = rays_to_m0(best_p, obs)
    s = -o[:, 2] / np.where(d[:, 2] > 1e-3, d[:, 2], 1e-3)
    err_cm = np.linalg.norm(o[:, :2] + s[:, None] * d[:, :2] - targets_ab, axis=1)
    sigma_px = float(1.4826 * np.median(err_cm)) / (ref.width_cm / ref.width)
    return BlockFit(block=0, params=best_p, sigma_px=max(sigma_px, 5.0))


def _active_blocks(X: np.ndarray) -> list[int]:
    """Block 0 always; block 1 only when it carries a distinct second camera."""
    blocks = [0]
    b1 = X[:, BLOCK_DIM : 2 * BLOCK_DIM]
    if np.any(np.abs(b1) > 1e-9) and not np.allclose(b1, X[:, :BLOCK_DIM]):
        blocks.append(1)
    return blocks


class GeoGazeModel:
    """Fitted per-camera ray models plus the monitor arrangement."""

    def __init__(
        self,
        blocks: list[BlockFit],
        monitors: list[MonitorGeom],
        ref_connector: str,
        viewer_m0: np.ndarray,
        cam1_by_id: str = "",
        cam2_by_id: str = "",
        camera_mode: str = "mono",
        noise_px: dict[int, float] | None = None,
        n_train_samples: int = 0,
    ) -> None:
        self.blocks = blocks
        # Per-axis std (px) of k-frame prediction means during a held fixation, by k.
        self.noise_px = {int(k): float(v) for k, v in (noise_px or {1: 60.0, 4: 32.0, 8: 22.0}).items()}
        self.monitors = monitors
        self.ref_connector = ref_connector
        self.viewer_m0 = np.asarray(viewer_m0, dtype=np.float64)
        self.cam1_by_id = cam1_by_id
        self.cam2_by_id = cam2_by_id
        self.camera_mode = camera_mode
        self.n_train_samples = int(n_train_samples)
        self.bounds_min_x = float(min(m.x for m in monitors))
        self.bounds_min_y = float(min(m.y for m in monitors))
        self.bounds_max_x = float(max(m.x + m.width for m in monitors) - 1)
        self.bounds_max_y = float(max(m.y + m.height for m in monitors) - 1)
        ref = self.ref_monitor
        sigma = float(np.mean([b.sigma_px for b in blocks]))
        self.rmse_x = self.rmse_y = sigma
        self.cv_rmse_x = self.cv_rmse_y = sigma
        self.session_offset_x = 0.0
        self.session_offset_y = 0.0
        self._ref_cm_per_px = ref.width_cm / ref.width

    @property
    def ref_monitor(self) -> MonitorGeom:
        for m in self.monitors:
            if m.connector == self.ref_connector:
                return m
        return self.monitors[0]

    @classmethod
    def fit(
        cls,
        X: np.ndarray,
        targets_xy: np.ndarray,
        specs: Sequence[MonitorSpec],
        ref_connector: str | None = None,
        sample_weights: np.ndarray | None = None,
        cam1_by_id: str = "",
        cam2_by_id: str = "",
    ) -> GeoGazeModel:
        X = np.asarray(X, dtype=np.float64)
        targets_xy = np.asarray(targets_xy, dtype=np.float64)
        if X.ndim != 2 or X.shape[0] < 20:
            raise ValueError(f"Insufficient calibration samples: {X.shape}")
        ref_conn = ref_connector or specs[0].connector
        ref_spec = next((s for s in specs if s.connector == ref_conn), specs[0])
        rw, rh = _physical_size_cm(ref_spec)
        ref = MonitorGeom(ref_spec.connector, ref_spec.x, ref_spec.y, ref_spec.width, ref_spec.height, rw, rh)
        on_ref = np.array([ref.contains_px(x, y, margin=2) for x, y in targets_xy])
        weights = np.ones(len(X)) if sample_weights is None else np.asarray(sample_weights, dtype=np.float64)
        blocks = []
        viewers = []
        for b in _active_blocks(X):
            obs_all = decode_block(X, b)
            keep = on_ref & obs_all.valid
            obs = _subset(obs_all, keep)
            targets_m0 = ref.px_to_m0(targets_xy[keep, 0], targets_xy[keep, 1])
            bf = _fit_block(obs, targets_m0, ref, weights[keep])
            bf.block = b
            blocks.append(bf)
            o, _ = rays_to_m0(bf.params, obs)
            viewers.append(np.median(o, axis=0))
        viewer = np.mean(viewers, axis=0)
        monitors = place_monitors(specs, ref.connector, viewer)
        model = cls(
            blocks,
            monitors,
            ref.connector,
            viewer,
            cam1_by_id,
            cam2_by_id,
            "stereo" if len(blocks) > 1 else "mono",
            n_train_samples=int(len(X)),
        )
        preds_fit = model.predict_batch(X)
        model.noise_px = _fixation_noise(preds_fit, targets_xy) or model.noise_px
        ok = np.all(np.isfinite(preds_fit), axis=1) & on_ref
        if np.any(ok):
            ex = np.abs(preds_fit[ok, 0] - targets_xy[ok, 0])
            ey = np.abs(preds_fit[ok, 1] - targets_xy[ok, 1])
            model.rmse_x = float(np.median(ex) * 1.4826)
            model.rmse_y = float(np.median(ey) * 1.4826)
            print(
                f"[GeoGaze] Fit median abs err: X={np.median(ex):.1f}px, Y={np.median(ey):.1f}px | "
                f"blocks={[(b.block, round(b.sigma_px, 1)) for b in blocks]}"
            )
        return model

    # ---------------------------------------------------------------- prediction

    def block_hits(
        self,
        X: np.ndarray,
        block_deltas: dict[int, np.ndarray] | None = None,
        monitors: Sequence[MonitorGeom] | None = None,
    ) -> list[tuple[BlockFit, np.ndarray, np.ndarray, np.ndarray]]:
        """Per block: ``(fit, px, py, monitor_index)`` with the monitor chosen per frame."""
        X = np.atleast_2d(X)
        mons = list(monitors) if monitors is not None else self.monitors
        out = []
        for bf in self.blocks:
            obs = decode_block(X, bf.block)
            delta = None if block_deltas is None else block_deltas.get(bf.block)
            o, d = rays_to_m0(bf.params, obs, delta)
            px, py, idx = _choose_monitor(mons, o, d)
            px = np.where(obs.valid, px, np.nan)
            py = np.where(obs.valid, py, np.nan)
            out.append((bf, px, py, idx))
        return out

    def predict_batch(
        self,
        X: np.ndarray,
        block_deltas: dict[int, np.ndarray] | None = None,
        monitors: Sequence[MonitorGeom] | None = None,
    ) -> np.ndarray:
        hits = self.block_hits(X, block_deltas, monitors)
        num = np.zeros((np.atleast_2d(X).shape[0], 2))
        den = np.zeros(np.atleast_2d(X).shape[0])
        for bf, px, py, _ in hits:
            w = 1.0 / (bf.sigma_px**2)
            ok = np.isfinite(px)
            num[ok, 0] += w * px[ok]
            num[ok, 1] += w * py[ok]
            den[ok] += w
        den = np.where(den > 0, den, np.nan)
        return num / den[:, None]

    def predict(self, feature_vec: np.ndarray) -> tuple[float, float]:
        xy = self.predict_batch(np.asarray(feature_vec, dtype=np.float64)[None, :])[0]
        if not np.all(np.isfinite(xy)):
            return 0.5 * (self.bounds_min_x + self.bounds_max_x), 0.5 * (self.bounds_min_y + self.bounds_max_y)
        x = min(self.bounds_max_x, max(self.bounds_min_x, float(xy[0]) + self.session_offset_x))
        y = min(self.bounds_max_y, max(self.bounds_min_y, float(xy[1]) + self.session_offset_y))
        return x, y

    def recenter_to(self, target_x: float, target_y: float, feature_vec: np.ndarray) -> tuple[float, float]:
        x, y = self.predict(feature_vec)
        self.session_offset_x += target_x - x
        self.session_offset_y += target_y - y
        return self.session_offset_x, self.session_offset_y

    def px_per_degree(self, feature_vec: np.ndarray) -> float:
        """Local pixels per degree of visual angle on the reference monitor."""
        obs = decode_block(np.atleast_2d(feature_vec), self.blocks[0].block)
        o, _ = rays_to_m0(self.blocks[0].params, obs)
        dist = float(np.linalg.norm(o[0] - self.ref_monitor.center))
        return dist * math.tan(math.radians(1.0)) / self._ref_cm_per_px

    # ---------------------------------------------------------------- persistence

    def save(self, path: Path) -> None:
        payload = {
            "version": GEO_VERSION,
            "ref_connector": self.ref_connector,
            "viewer_m0": self.viewer_m0.tolist(),
            "cam1_by_id": self.cam1_by_id,
            "cam2_by_id": self.cam2_by_id,
            "camera_mode": self.camera_mode,
            "n_train_samples": self.n_train_samples,
            "blocks": [{"block": b.block, "params": b.params.tolist(), "sigma_px": b.sigma_px} for b in self.blocks],
            "monitors": [m.to_json() for m in self.monitors],
            "noise_px": {str(k): v for k, v in self.noise_px.items()},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> GeoGazeModel:
        d = json.loads(Path(path).read_text())
        if d.get("version") != GEO_VERSION:
            raise ValueError(f"Unsupported geometric calibration version: {d.get('version')}")
        return cls(
            blocks=[BlockFit(int(b["block"]), np.asarray(b["params"]), float(b["sigma_px"])) for b in d["blocks"]],
            monitors=[MonitorGeom.from_json(m) for m in d["monitors"]],
            ref_connector=d["ref_connector"],
            viewer_m0=np.asarray(d["viewer_m0"]),
            cam1_by_id=d.get("cam1_by_id", ""),
            cam2_by_id=d.get("cam2_by_id", ""),
            camera_mode=d.get("camera_mode", "mono"),
            noise_px={int(k): float(v) for k, v in d.get("noise_px", {}).items()} or None,
            n_train_samples=int(d.get("n_train_samples", 0)),
        )

    def with_layout(self, specs: Sequence[MonitorSpec]) -> None:
        """Re-place monitors after a layout change, keeping the calibrated reference plane."""
        if not any(s.connector == self.ref_connector for s in specs):
            return
        self.monitors = place_monitors(specs, self.ref_connector, self.viewer_m0)
        self.bounds_min_x = float(min(m.x for m in self.monitors))
        self.bounds_min_y = float(min(m.y for m in self.monitors))
        self.bounds_max_x = float(max(m.x + m.width for m in self.monitors) - 1)
        self.bounds_max_y = float(max(m.y + m.height for m in self.monitors) - 1)


def _fixation_noise(pred: np.ndarray, targets: np.ndarray) -> dict[int, float] | None:
    """Std of k-frame prediction means around each held-target run (anchor dots), by k."""
    runs: list[np.ndarray] = []
    start = 0
    for i in range(1, len(targets) + 1):
        if i == len(targets) or np.any(np.abs(targets[i] - targets[start]) > 0.5):
            if i - start >= 12:
                runs.append(pred[start:i])
            start = i
    if len(runs) < 3:
        return None
    out = {}
    for k in (1, 4, 8):
        devs = []
        for r in runs:
            r = r[np.all(np.isfinite(r), axis=1)]
            if len(r) < k + 2:
                continue
            mu = r.mean(axis=0)
            devs += [r[i : i + k].mean(axis=0) - mu for i in range(len(r) - k + 1)]
        if devs:
            out[k] = float(np.sqrt(np.mean(np.square(devs))))
    return out


def _subset(obs: HeadObs, mask: np.ndarray) -> HeadObs:
    return HeadObs(*(getattr(obs, f)[mask] for f in ("yaw", "pitch", "roll", "t_cm", "dx", "dy", "valid")))


def _choose_monitor(mons: Sequence[MonitorGeom], o: np.ndarray, d: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pick, per ray, the nearest in-bounds monitor hit; otherwise clamp to the closest rectangle."""
    n = o.shape[0]
    best_px = np.full(n, np.nan)
    best_py = np.full(n, np.nan)
    best_idx = np.full(n, -1)
    best_score = np.full(n, np.inf)
    for i, m in enumerate(mons):
        px, py, s = m.intersect(o, d)
        ox = np.maximum(0.0, np.maximum(m.x - px, px - (m.x + m.width - 1))) * (m.width_cm / m.width)
        oy = np.maximum(0.0, np.maximum(m.y - py, py - (m.y + m.height - 1))) * (m.height_cm / m.height)
        outside_cm = np.hypot(ox, oy)
        # In-bounds hits rank by ray distance; out-of-bounds hits rank behind all in-bounds ones.
        score = np.where(s > 0, np.where(outside_cm <= 0, s * 1e-3, 1e3 + outside_cm), np.inf)
        better = score < best_score
        best_score = np.where(better, score, best_score)
        best_px = np.where(better, np.clip(px, m.x, m.x + m.width - 1), best_px)
        best_py = np.where(better, np.clip(py, m.y, m.y + m.height - 1), best_py)
        best_idx = np.where(better, i, best_idx)
    return best_px, best_py, best_idx


# --------------------------------------------------------------------------------------------
# Fixation segmentation (also the visible-cursor smoother)
# --------------------------------------------------------------------------------------------


@dataclass
class Fixation:
    t0: float
    t1: float
    center: np.ndarray
    feat: np.ndarray
    n: int
    display_end: np.ndarray
    consumed: bool = False

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


class FixationSegmenter:
    """Online fixation segmentation robust to temporally correlated webcam noise.

    Two test scales run in parallel: the distance ``D_k`` between the mean of the last ``k``
    samples and the current fixation centre, for ``k = 4`` (fast, large saccades) and ``k = 8``
    (slower, small saccades such as cursor-chase steps). The null distribution of ``D_k`` is
    learned online with a sign-based running median (``D_k`` is Rayleigh with no saccade,
    ``sigma = median / sqrt(2 ln 2)``), which slow landmark noise raises automatically and
    sub-threshold saccades cannot inflate. A split needs ``D_k > k_sigma * sigma_k`` and
    ``> min_amp_px`` and a displaced recent half-window. The split point maximises the
    two-segment contrast. Fixations shorter than ``min_frames`` are transitional and dropped. A
    closed fixation is emitted only after the next one proves distinct; otherwise they merge.

    ``smoothed`` is the visible-cursor value: the current fixation mean.
    """

    SCALES = (4, 8)

    def __init__(
        self,
        noise_px: dict[int, float] | float = 40.0,
        min_amp_px: float = 25.0,
        k_sigma: float = 3.5,
        min_frames: int = 5,
        window: int = 30,
    ) -> None:
        self.min_amp_px = float(min_amp_px)
        self.k_sigma = float(k_sigma)
        self.min_frames = int(min_frames)
        self.window = int(window)
        if not isinstance(noise_px, dict):
            noise_px = {k: float(noise_px) / math.sqrt(k) for k in self.SCALES}
        # D_k also carries the uncertainty of the fixation-centre estimate (~window/2 samples).
        self._d_med0 = {k: 1.1774 * float(noise_px.get(k, 30.0)) * math.sqrt(1.0 + k / 15.0) for k in self.SCALES}
        self.d_med = dict(self._d_med0)
        self._cur: list[tuple[float, np.ndarray, np.ndarray]] = []
        self._cand: Fixation | None = None
        self._display: np.ndarray | None = None
        self.smoothed: np.ndarray | None = None

    def sigma_k(self, k: int) -> float:
        return self.d_med[k] / 1.1774

    @property
    def sigma(self) -> float:
        return self.sigma_k(self.SCALES[0])

    def threshold_k(self, k: int) -> float:
        return max(self.k_sigma * self.sigma_k(k), self.min_amp_px)

    @property
    def threshold(self) -> float:
        return self.threshold_k(self.SCALES[0])

    def set_display(self, xy: Sequence[float]) -> None:
        self._display = np.asarray(xy, dtype=np.float64)

    def _make(self, samples: list[tuple[float, np.ndarray, np.ndarray]]) -> Fixation:
        pts = np.asarray([p for _, p, _ in samples[-self.window :]])
        feat = np.mean([f for _, _, f in samples], axis=0)
        disp = self._display if self._display is not None else self.smoothed
        disp = pts.mean(axis=0) if disp is None else disp
        return Fixation(samples[0][0], samples[-1][0], pts.mean(axis=0), feat, len(samples), np.array(disp, dtype=np.float64))

    def current(self) -> Fixation | None:
        return self._make(self._cur) if self._cur else None

    @property
    def center(self) -> np.ndarray | None:
        if not self._cur:
            return None
        return np.mean([p for _, p, _ in self._cur[-self.window :]], axis=0)

    def _test(self, k: int) -> bool:
        n = len(self._cur)
        if n < k + 4:
            return False
        pts = np.asarray([q for _, q, _ in self._cur[-self.window - k :]])
        base = pts[:-k].mean(axis=0)
        recent = pts[-k:]
        dist = float(np.linalg.norm(recent.mean(axis=0) - base))
        thr = self.threshold_k(k)
        half = recent[-max(2, k // 2) :].mean(axis=0)
        if dist > thr and float(np.linalg.norm(half - base)) > 0.7 * thr:
            return True
        if len(pts) - k >= 8 and dist < thr:
            self.d_med[k] *= math.exp(0.02 if dist > self.d_med[k] else -0.02)
            self.d_med[k] = min(max(self.d_med[k], 0.8 * self._d_med0[k]), 1.6 * self._d_med0[k])
        return False

    def _split_index(self, k: int) -> int:
        n = len(self._cur)
        pts = np.asarray([q for _, q, _ in self._cur])
        best_j, best = n - 2, -1.0
        for j in range(max(2, n - k), n - 1):
            before = pts[max(0, j - self.window) : j].mean(axis=0)
            after = pts[j:].mean(axis=0)
            score = float(np.linalg.norm(after - before)) * math.sqrt(n - j)
            if score > best:
                best_j, best = j, score
        return best_j

    def push(self, ts: float, p: np.ndarray, feat: np.ndarray) -> Fixation | None:
        """Add a sample; return a confirmed, completed fixation when one becomes final."""
        self._cur.append((ts, np.asarray(p, dtype=np.float64), np.asarray(feat, dtype=np.float64)))
        fired = next((k for k in self.SCALES if self._test(k)), None)
        if fired is None:
            if len(self._cur) >= 3 or self.smoothed is None:
                self.smoothed = self.center
            return None

        split = self._split_index(fired)
        old, new = self._cur[:split], self._cur[split:]
        self._cur = new
        self.smoothed = np.mean([q for _, q, _ in new], axis=0)
        if len(old) < self.min_frames:
            return None  # transitional samples between two saccades
        closed = self._make(old)
        cand = self._cand
        merge_r = max(0.75 * self.threshold_k(self.SCALES[-1]), self.min_amp_px)
        if cand is not None and float(np.linalg.norm(closed.center - cand.center)) < merge_r:
            w = cand.n / (cand.n + closed.n)
            cand.center = w * cand.center + (1 - w) * closed.center
            cand.feat = w * cand.feat + (1 - w) * closed.feat
            cand.n += closed.n
            cand.t1 = closed.t1
            cand.display_end = closed.display_end
            return None
        self._cand = closed
        return cand


# --------------------------------------------------------------------------------------------
# Cursor-chase detection
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GazeLabel:
    feat: np.ndarray
    target: np.ndarray
    weight: float
    source: str
    t0: float = 0.0
    t1: float = 0.0
    group: int = 1  # labels sharing one chain carry correlated errors


def _reading_like(step: np.ndarray) -> bool:
    """Left-to-right text produces near-horizontal rightward saccade runs."""
    return step[0] > 0 and math.degrees(math.atan2(abs(step[1]), step[0])) < 25.0


class ChaseDetector:
    """Detect a user chasing a mis-calibrated visible cursor and turn it into labels.

    Model: true gaze ``g``, displayed cursor ``c = g + b`` with a locally constant bias ``b``.
    A user who looks at the cursor lands at ``g' = c``; the cursor then moves to ``c + b``. The
    predicted fixation centres form an arithmetic progression with step ``b``. The user usually
    gives up and returns to the intended target ``A``. Each chase fixation ``i`` therefore has
    ground truth = the cursor displayed at the end of fixation ``i-1``; the dwell and return
    fixations have ground truth ``A = c_0 - b``.
    """

    REACTION_S = 0.10  # cursor position the user acted on: shown this long before saccade onset

    def __init__(self, px_per_deg: float = 40.0, display_at: object = None) -> None:
        self.px_per_deg = px_per_deg
        self.display_at = display_at  # callable(t) -> displayed cursor (np.ndarray) or None
        self.fix: deque[Fixation] = deque(maxlen=10)
        self._single_loops: deque[tuple[float, np.ndarray, np.ndarray, list[GazeLabel]]] = deque(maxlen=6)
        self.confirmed_chains = 0

    def _step_ok(self, s: np.ndarray) -> bool:
        amp = float(np.linalg.norm(s)) / self.px_per_deg
        return 0.6 <= amp <= 8.0

    def push(self, fix: Fixation) -> list[GazeLabel]:
        self.fix.append(fix)
        f = list(self.fix)
        k = len(f) - 1
        for n in (4, 3, 2, 1):
            i0 = k - n - 1
            if i0 < 0:
                continue
            chain = f[i0 : k + 1]
            if any(c.consumed for c in chain):
                continue
            labels = self._closed_loop(chain, n)
            if labels is not None:
                return labels
        for n in (4, 3):
            i0 = k - n
            if i0 < 0:
                continue
            chain = f[i0 : k + 1]
            if any(c.consumed for c in chain):
                continue
            labels = self._open_chain(chain, n)
            if labels is not None:
                return labels
        return []

    def _consistent_steps(self, chain: list[Fixation], n: int) -> np.ndarray | None:
        steps = np.array([chain[i].center - chain[i - 1].center for i in range(1, n + 1)])
        if not all(self._step_ok(s) for s in steps):
            return None
        mean = steps.mean(axis=0)
        tol = 0.45 * float(np.linalg.norm(mean)) + 0.3 * self.px_per_deg
        if np.any(np.linalg.norm(steps - mean, axis=1) > tol):
            return None
        if any(chain[i].duration > 1.0 or chain[i].duration < 0.06 for i in range(1, n + 1)):
            return None
        return mean

    def _labels(self, chain: list[Fixation], n: int, b: np.ndarray, closed: bool) -> list[GazeLabel]:
        k = n + (2 if closed else 1)
        labels = []
        seen_biases = []
        for i in range(1, n + 1):
            seen = self.display_at(chain[i].t0 - self.REACTION_S) if self.display_at is not None else None
            target = seen if seen is not None else chain[i - 1].center
            if seen is not None:
                seen_biases.append(chain[i].center - seen)
            labels.append(GazeLabel(chain[i].feat, np.array(target, dtype=np.float64), 1.0, "chase", chain[i].t0, chain[i].t1, k))
        bias_est = np.mean(seen_biases, axis=0) if seen_biases else b
        a = chain[0].center - bias_est
        labels.append(GazeLabel(chain[0].feat, a, 0.6, "chase_dwell", chain[0].t0, chain[0].t1, k))
        if closed:
            labels.append(GazeLabel(chain[-1].feat, a, 0.6, "chase_return", chain[-1].t0, chain[-1].t1, k))
        return labels

    def _closed_loop(self, chain: list[Fixation], n: int) -> list[GazeLabel] | None:
        f0, ret = chain[0], chain[-1]
        if f0.duration < 0.25:
            return None
        b = self._consistent_steps(chain, n)
        if b is None:
            return None
        back = float(np.linalg.norm(ret.center - f0.center))
        if back > max(0.4 * float(np.linalg.norm(b)), 0.6 * self.px_per_deg):
            return None
        if n >= 2 and _reading_like(b):
            # Rightward steps + a return near the start is a line of text, not a chase.
            return None
        labels = self._labels(chain, n, b, closed=True)
        if n == 1:
            # A single look-and-return is common in normal viewing: require a repeat.
            now = ret.t1
            for t, b_prev, c_prev, prev_labels in list(self._single_loops):
                near = float(np.linalg.norm(c_prev - f0.center)) < 15.0 * self.px_per_deg
                same = float(np.linalg.norm(b_prev - b)) <= 0.4 * float(np.linalg.norm(b))
                if now - t < 30.0 and near and same:
                    self._single_loops.clear()
                    self._commit(chain)
                    return prev_labels + labels
            self._single_loops.append((now, b, f0.center.copy(), labels))
            return None
        self._commit(chain)
        return labels

    def _open_chain(self, chain: list[Fixation], n: int) -> list[GazeLabel] | None:
        if chain[0].duration < 0.25:
            return None
        b = self._consistent_steps(chain, n)
        if b is None:
            return None
        if _reading_like(b):  # rightward steps look like reading; require a closed loop instead
            return None
        self._commit(chain[:-1])
        return self._labels(chain, n, b, closed=False)

    def _commit(self, chain: list[Fixation]) -> None:
        for c in chain[:-1]:
            c.consumed = True
        self.confirmed_chains += 1


# --------------------------------------------------------------------------------------------
# Online tracker
# --------------------------------------------------------------------------------------------


SOURCE_SIGMA_DEG = {"click": 0.9, "chase": 1.0, "chase_dwell": 1.3, "chase_return": 1.4, "voice": 1.1}


class GeoGazeTracker:
    """Runtime wrapper: drift-corrected prediction, fixation smoothing and EKF label updates."""

    BLOCK_SIG0 = np.array([math.radians(2.0), math.radians(2.0), 0.8, 0.8])
    BLOCK_JUMP_SIG = np.array([math.radians(6.0), math.radians(6.0), 2.5, 2.5])
    BLOCK_Q = np.array([math.radians(0.06) ** 2, math.radians(0.06) ** 2, 0.02**2, 0.02**2])  # per second
    MON_SIG0 = np.array([math.radians(20.0), math.radians(10.0), 6.0, 6.0, 8.0])

    def __init__(self, model: GeoGazeModel, enable_chase: bool = True, enable_ekf: bool = True) -> None:
        self.model = model
        self.enable_chase = enable_chase
        self.enable_ekf = enable_ekf
        self._block_ids = [b.block for b in model.blocks]
        self._mon_ids = [i for i, m in enumerate(model.monitors) if m.connector != model.ref_connector]
        nb, nm = len(self._block_ids), len(self._mon_ids)
        self.state = np.zeros(4 * nb + 5 * nm)
        sig = np.concatenate([np.tile(self.BLOCK_SIG0, nb), np.tile(self.MON_SIG0, nm)])
        self.P = np.diag(sig**2)
        self._last_update_ts: float | None = None
        self._outliers: deque[tuple[float, np.ndarray, int]] = deque(maxlen=6)
        self._mons_cache: list[MonitorGeom] | None = None
        self._ppd = 40.0
        self.segmenter = FixationSegmenter(noise_px=model.noise_px)
        self.chase = ChaseDetector(display_at=self._display_at)
        self._disp_hist: deque[tuple[float, np.ndarray]] = deque(maxlen=300)
        self.label_log: list[tuple[float, str, bool]] = []
        self.harvested: list[GazeLabel] = []
        self._event_id = 0
        self._frame_count = 0
        self._display: np.ndarray | None = None

    # ------------------------------------------------------------ state <-> geometry

    def _split(self, x: np.ndarray) -> tuple[dict[int, np.ndarray], list[MonitorGeom]]:
        deltas = {b: x[4 * i : 4 * i + 4] for i, b in enumerate(self._block_ids)}
        mons = list(self.model.monitors)
        off = 4 * len(self._block_ids)
        for j, mi in enumerate(self._mon_ids):
            mons[mi] = self.model.monitors[mi].perturbed(x[off + 5 * j : off + 5 * j + 5])
        return deltas, mons

    def _monitors(self) -> list[MonitorGeom]:
        if self._mons_cache is None:
            self._mons_cache = self._split(self.state)[1]
        return self._mons_cache

    def _project_on(self, x: np.ndarray, feat: np.ndarray, mon_index: int) -> np.ndarray:
        deltas, mons = self._split(x)
        m = mons[mon_index]
        num = np.zeros(2)
        den = 0.0
        for bf in self.model.blocks:
            obs = decode_block(feat[None, :], bf.block)
            if not obs.valid[0]:
                continue
            o, d = rays_to_m0(bf.params, obs, deltas[bf.block])
            px, py, s = m.intersect(o, d)
            if s[0] <= 0:
                continue
            w = 1.0 / bf.sigma_px**2
            num += w * np.array([px[0], py[0]])
            den += w
        return num / den if den > 0 else np.array([np.nan, np.nan])

    # ------------------------------------------------------------ runtime API

    def predict_raw(self, feat: np.ndarray) -> np.ndarray:
        deltas = {b: self.state[4 * i : 4 * i + 4] for i, b in enumerate(self._block_ids)}
        return self.model.predict_batch(feat[None, :], deltas, self._monitors())[0]

    def observe_display(self, x: float, y: float, ts: float | None = None) -> None:
        """Report the cursor position the user currently sees (if a separate smoother draws it)."""
        self._display = np.array([x, y], dtype=np.float64)
        self.segmenter.set_display(self._display)
        if ts is not None:
            self._disp_hist.append((ts, self._display.copy()))

    def _display_at(self, t: float) -> np.ndarray | None:
        for ts, xy in reversed(self._disp_hist):
            if ts <= t:
                return xy
        return None

    def update(self, feat: np.ndarray, ts: float) -> np.ndarray:
        """Predict corrected gaze for one frame, segment fixations and harvest chase labels."""
        feat = np.asarray(feat, dtype=np.float64)
        p = self.predict_raw(feat)
        if not np.all(np.isfinite(p)):
            return p
        self._frame_count += 1
        if self._frame_count == 1 or (self._frame_count % 15) == 0:
            self._ppd = 0.9 * self._ppd + 0.1 * self.model.px_per_degree(feat)
            self.segmenter.min_amp_px = 0.6 * self._ppd
            self.chase.px_per_deg = self._ppd
        ended = self.segmenter.push(ts, p, feat)
        if self._display is None and self.segmenter.smoothed is not None:
            self._disp_hist.append((ts, self.segmenter.smoothed.copy()))
        if ended is not None and self.enable_chase:
            labels = self.chase.push(ended)
            if labels:
                self._event_id += 1
            for lab in labels:
                self.harvested.append(lab)
                # A chain's labels share one bias estimate: weight the chain like ~1.5 labels.
                self.add_label(lab.feat, lab.target, ts, lab.source, lab.weight * 1.5 / lab.group, self._event_id)
        return p

    @property
    def smoothed(self) -> np.ndarray | None:
        return self.segmenter.smoothed

    def record_click(self, x: float, y: float, ts: float, feat: np.ndarray | None = None) -> bool:
        """Use a click as ground truth for the fixation that was active ~120 ms before it."""
        if feat is None:
            cur = self.segmenter.current()
            if cur is None or cur.n < 3:
                return False
            feat = cur.feat
        self._event_id += 1
        return self.add_label(feat, np.array([x, y], dtype=np.float64), ts, "click", 1.0, self._event_id)

    def add_label(
        self,
        feat: np.ndarray,
        target: np.ndarray,
        ts: float,
        source: str,
        weight: float = 1.0,
        event_id: int | None = None,
    ) -> bool:
        if not self.enable_ekf:
            return False
        target = np.asarray(target, dtype=np.float64)
        mon_index = next((i for i, m in enumerate(self.model.monitors) if m.contains_px(target[0], target[1])), None)
        if mon_index is None:
            return False
        if self._last_update_ts is not None:
            dt = max(0.0, ts - self._last_update_ts)
            q = np.zeros_like(self.state)
            q[: 4 * len(self._block_ids)] = np.tile(self.BLOCK_Q, len(self._block_ids)) * dt
            self.P = self.P + np.diag(q)
        self._last_update_ts = ts

        if event_id is None:
            self._event_id += 1
            event_id = self._event_id
        ok = self._kalman_step(feat, target, mon_index, source, weight, ts, event_id)
        self.label_log.append((ts, source, ok))
        return ok

    def _kalman_step(
        self,
        feat: np.ndarray,
        target: np.ndarray,
        mon_index: int,
        source: str,
        weight: float,
        ts: float,
        event_id: int,
    ) -> bool:
        h0 = self._project_on(self.state, feat, mon_index)
        if not np.all(np.isfinite(h0)):
            return False
        n = len(self.state)
        jac = np.zeros((2, n))
        for k in range(n):
            eps = 1e-4 if self._is_angle(k) else 1e-2
            x1 = self.state.copy()
            x1[k] += eps
            h1 = self._project_on(x1, feat, mon_index)
            if np.all(np.isfinite(h1)):
                jac[:, k] = (h1 - h0) / eps
        sigma_px = SOURCE_SIGMA_DEG.get(source, 1.2) * self._ppd / math.sqrt(max(weight, 0.1))
        sigma_px = math.hypot(sigma_px, self.model.blocks[0].sigma_px * 0.5)
        r_mat = np.eye(2) * sigma_px**2
        innov = target - h0
        s_mat = jac @ self.P @ jac.T + r_mat
        nis = float(innov @ np.linalg.solve(s_mat, innov))
        if nis > 30.0:
            self._outliers.append((ts, innov, event_id))
            recent = [(inn, ev) for t, inn, ev in self._outliers if ts - t < 30.0]
            events = {ev for _, ev in recent}
            if len(events) >= 2 and _same_direction([inn for inn, _ in recent]):
                # Persistent large residuals in one direction: the camera moved. Re-open drift.
                nb = len(self._block_ids)
                for i in range(nb):
                    sl = slice(4 * i, 4 * i + 4)
                    self.P[sl, sl] = np.maximum(self.P[sl, sl], np.diag(self.BLOCK_JUMP_SIG**2))
                self._outliers.clear()
                s_mat = jac @ self.P @ jac.T + r_mat
                nis = float(innov @ np.linalg.solve(s_mat, innov))
            else:
                return False
        if nis > 6.0:  # Huber-style down-weighting of large but plausible residuals
            r_mat = r_mat * (nis / 6.0)
            s_mat = jac @ self.P @ jac.T + r_mat
        gain = self.P @ jac.T @ np.linalg.inv(s_mat)
        self.state = self.state + gain @ innov
        ikh = np.eye(n) - gain @ jac
        self.P = ikh @ self.P @ ikh.T + gain @ r_mat @ gain.T
        self._clamp()
        self._mons_cache = None
        return True

    def _is_angle(self, k: int) -> bool:
        nb4 = 4 * len(self._block_ids)
        if k < nb4:
            return (k % 4) < 2
        return ((k - nb4) % 5) < 2

    def _clamp(self) -> None:
        nb = len(self._block_ids)
        for i in range(nb):
            self.state[4 * i : 4 * i + 2] = np.clip(self.state[4 * i : 4 * i + 2], -0.25, 0.25)
            self.state[4 * i + 2 : 4 * i + 4] = np.clip(self.state[4 * i + 2 : 4 * i + 4], -25.0, 25.0)
        off = 4 * nb
        for j in range(len(self._mon_ids)):
            sl = slice(off + 5 * j, off + 5 * j + 5)
            self.state[sl] = np.clip(self.state[sl], [-1.2, -0.8, -40, -40, -40], [1.2, 0.8, 40, 40, 40])


def _same_direction(innovs: Iterable[np.ndarray]) -> bool:
    v = [i / (np.linalg.norm(i) + 1e-9) for i in innovs]
    return all(float(v[0] @ u) > 0.7 for u in v[1:])


def fit_from_dataset(
    X: np.ndarray,
    Y: np.ndarray,
    layout: object,
    ref_connector: str | None = None,
    mm_by_connector: dict[str, tuple[float, float]] | None = None,
    **kw: object,
) -> GeoGazeModel:
    """Convenience: fit from the replay dataset arrays and a ``DisplayLayout``."""
    specs = monitor_specs_from_layout(layout, mm_by_connector)
    if ref_connector is None or not any(s.connector == ref_connector for s in specs):
        ref_connector = next((m.connector for m in layout.monitors if getattr(m, "is_primary", False)), specs[0].connector)  # type: ignore[attr-defined]
    return GeoGazeModel.fit(X, Y, specs, ref_connector, **kw)  # type: ignore[arg-type]


__all__ = [
    "ChaseDetector",
    "FixationSegmenter",
    "GazeLabel",
    "GeoGazeModel",
    "GeoGazeTracker",
    "MonitorGeom",
    "MonitorSpec",
    "fit_from_dataset",
    "monitor_specs_from_layout",
    "place_monitors",
]

