"""3D multi-display physical rig geometry and ray-plane gaze intersection.

Handles arbitrary multi-monitor setups where displays may:
- be physically swapped relative to OS logical coordinates,
- have physical bezel gaps or vertical offsets,
- be angled inward/outward (yaw) or tilted (pitch/roll),
- have different physical sizes (width_mm, height_mm) and resolutions.

Coordinate system (right-handed, millimeters, camera/user eye origin at (0, 0, 0)):
- +X: user's right
- +Y: down (matches screen pixel Y increasing downward)
- +Z: forward from user toward displays
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_DISTANCE_MM = 600.0
DEFAULT_GAP_MM = 25.0
DEFAULT_WIDTH_MM = 531.0   # ~24" 16:9
DEFAULT_HEIGHT_MM = 299.0


def _rotation_matrix(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    """Return 3x3 rotation matrix R = R_y(yaw) @ R_x(pitch) @ R_z(roll)."""
    y = math.radians(float(yaw_deg))
    p = math.radians(float(pitch_deg))
    r = math.radians(float(roll_deg))

    cy, sy = math.cos(y), math.sin(y)
    cp, sp = math.cos(p), math.sin(p)
    cr, sr = math.cos(r), math.sin(r)

    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]], dtype=np.float64)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]], dtype=np.float64)
    rz = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    return ry @ rx @ rz


def ray_from_angles(az_rad: float, el_rad: float) -> np.ndarray:
    """Convert gaze azimuth (+right) and elevation (+down) radians to unit 3D ray."""
    ca, sa = math.cos(az_rad), math.sin(az_rad)
    ce, se = math.cos(el_rad), math.sin(el_rad)
    d = np.array([sa * ce, se, ca * ce], dtype=np.float64)
    norm = float(np.linalg.norm(d))
    return d / max(1e-9, norm)


def angles_from_ray(ray: np.ndarray) -> tuple[float, float]:
    """Convert a 3D ray vector to (azimuth_rad, elevation_rad)."""
    v = np.asarray(ray, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(v))
    if norm < 1e-9:
        return 0.0, 0.0
    v = v / norm
    az = math.atan2(float(v[0]), max(1e-6, float(v[2])))
    el = math.asin(max(-1.0, min(1.0, float(v[1]))))
    return az, el


@dataclass(frozen=True)
class DisplayPlane:
    """One physical display positioned and oriented in 3D space."""

    connector: str
    # Logical OS desktop rectangle (pixels)
    x: int
    y: int
    width: int
    height: int
    scale: float = 1.0
    is_primary: bool = False
    # Physical dimensions (mm)
    width_mm: float = DEFAULT_WIDTH_MM
    height_mm: float = DEFAULT_HEIGHT_MM
    # Physical 3D center (mm) relative to user eye origin (0, 0, 0)
    cx_mm: float = 0.0
    cy_mm: float = 0.0
    cz_mm: float = DEFAULT_DISTANCE_MM
    # Orientation (degrees): yaw>0 angles right edge away (left-side wing curves toward user)
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0

    def basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return (center_3d, u_axis_right, v_axis_down, normal_towards_back)."""
        rot = _rotation_matrix(self.yaw_deg, self.pitch_deg, self.roll_deg)
        u_axis = rot[:, 0]
        v_axis = rot[:, 1]
        normal = rot[:, 2]
        center = np.array([self.cx_mm, self.cy_mm, self.cz_mm], dtype=np.float64)
        return center, u_axis, v_axis, normal

    def contains_pixel(self, px: float, py: float) -> bool:
        return (
            self.x <= px < self.x + self.width
            and self.y <= py < self.y + self.height
        )

    def uv_to_pixel(self, u: float, v: float) -> tuple[float, float]:
        cu = max(0.0, min(1.0, float(u)))
        cv = max(0.0, min(1.0, float(v)))
        px = min(float(self.x + self.width - 1), self.x + cu * float(self.width))
        py = min(float(self.y + self.height - 1), self.y + cv * float(self.height))
        return px, py

    def pixel_to_uv(self, px: float, py: float) -> tuple[float, float]:
        u = (float(px) - self.x) / max(1.0, float(self.width))
        v = (float(py) - self.y) / max(1.0, float(self.height))
        return u, v

    def uv_to_3d(self, u: float, v: float) -> np.ndarray:
        center, u_axis, v_axis, _ = self.basis()
        return (
            center
            + (float(u) - 0.5) * self.width_mm * u_axis
            + (float(v) - 0.5) * self.height_mm * v_axis
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "is_primary": self.is_primary,
            "width_mm": round(self.width_mm, 1),
            "height_mm": round(self.height_mm, 1),
            "cx_mm": round(self.cx_mm, 1),
            "cy_mm": round(self.cy_mm, 1),
            "cz_mm": round(self.cz_mm, 1),
            "yaw_deg": round(self.yaw_deg, 1),
            "pitch_deg": round(self.pitch_deg, 1),
            "roll_deg": round(self.roll_deg, 1),
        }


@dataclass(frozen=True)
class RayHit:
    """Result of intersecting a 3D gaze ray with a multi-display rig."""

    x: float
    y: float
    connector: str
    u: float
    v: float
    in_gap: bool
    distance_mm: float


class DisplayRig:
    """Ordered collection of 3D DisplayPlanes with ray intersection and persistence."""

    def __init__(self, planes: list[DisplayPlane] | tuple[DisplayPlane, ...]) -> None:
        if not planes:
            raise ValueError("DisplayRig requires at least one DisplayPlane")
        self.planes: tuple[DisplayPlane, ...] = tuple(planes)

    @classmethod
    def from_single_rect(
        cls,
        width: int,
        height: int,
        *,
        origin_x: int = 0,
        origin_y: int = 0,
        distance_mm: float = DEFAULT_DISTANCE_MM,
    ) -> DisplayRig:
        aspect = float(width) / max(1.0, float(height))
        h_mm = DEFAULT_HEIGHT_MM
        w_mm = h_mm * aspect
        return cls(
            [
                DisplayPlane(
                    connector="DISPLAY-0",
                    x=origin_x,
                    y=origin_y,
                    width=width,
                    height=height,
                    is_primary=True,
                    width_mm=w_mm,
                    height_mm=h_mm,
                    cx_mm=0.0,
                    cy_mm=0.0,
                    cz_mm=distance_mm,
                )
            ]
        )

    @classmethod
    def from_layout(
        cls,
        layout: Any,
        *,
        gap_mm: float = DEFAULT_GAP_MM,
        distance_mm: float = DEFAULT_DISTANCE_MM,
    ) -> DisplayRig:
        monitors = list(getattr(layout, "monitors", ()) or ())
        if not monitors:
            return cls.from_single_rect(
                int(getattr(layout, "total_width", 1920)),
                int(getattr(layout, "total_height", 1080)),
                distance_mm=distance_mm,
            )

        # Sort by OS logical X, then Y for default left-to-right physical placement.
        ordered = sorted(monitors, key=lambda m: (m.x, m.y))
        widths_mm: list[float] = []
        heights_mm: list[float] = []
        for m in ordered:
            w_mm = float(getattr(m, "width_mm", 0.0) or 0.0)
            h_mm = float(getattr(m, "height_mm", 0.0) or 0.0)
            if w_mm < 100.0 or h_mm < 60.0:
                aspect = float(m.width) / max(1.0, float(m.height))
                h_mm = DEFAULT_HEIGHT_MM
                w_mm = h_mm * aspect
            widths_mm.append(w_mm)
            heights_mm.append(h_mm)

        total_span = sum(widths_mm) + gap_mm * max(0, len(ordered) - 1)
        cursor_x = -0.5 * total_span
        planes: list[DisplayPlane] = []
        min_y = min(m.y for m in ordered)
        for idx, m in enumerate(ordered):
            w_mm = widths_mm[idx]
            h_mm = heights_mm[idx]
            cx = cursor_x + 0.5 * w_mm
            # Reflect vertical logical offset in mm proportionally
            px_per_mm = float(m.height) / max(1.0, h_mm)
            cy = (float(m.y - min_y)) / max(0.1, px_per_mm)
            planes.append(
                DisplayPlane(
                    connector=str(m.connector),
                    x=int(m.x),
                    y=int(m.y),
                    width=int(m.width),
                    height=int(m.height),
                    scale=float(getattr(m, "scale", 1.0) or 1.0),
                    is_primary=bool(getattr(m, "is_primary", False)),
                    width_mm=w_mm,
                    height_mm=h_mm,
                    cx_mm=cx,
                    cy_mm=cy,
                    cz_mm=distance_mm,
                    yaw_deg=0.0,
                    pitch_deg=0.0,
                    roll_deg=0.0,
                )
            )
            cursor_x += w_mm + gap_mm
        return cls(planes)

    @classmethod
    def load(cls, path: Path, layout: Any) -> DisplayRig:
        base = cls.from_layout(layout)
        if not path.exists():
            return base
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return base
        overrides = {
            str(item.get("connector")): item
            for item in (raw.get("displays") or [])
            if isinstance(item, dict) and item.get("connector")
        }
        updated: list[DisplayPlane] = []
        for p in base.planes:
            ov = overrides.get(p.connector)
            if ov is None:
                updated.append(p)
                continue
            updated.append(
                DisplayPlane(
                    connector=p.connector,
                    x=p.x,
                    y=p.y,
                    width=p.width,
                    height=p.height,
                    scale=p.scale,
                    is_primary=p.is_primary,
                    width_mm=max(80.0, float(ov.get("width_mm", p.width_mm))),
                    height_mm=max(50.0, float(ov.get("height_mm", p.height_mm))),
                    cx_mm=float(ov.get("cx_mm", p.cx_mm)),
                    cy_mm=float(ov.get("cy_mm", p.cy_mm)),
                    cz_mm=max(200.0, float(ov.get("cz_mm", p.cz_mm))),
                    yaw_deg=max(-75.0, min(75.0, float(ov.get("yaw_deg", p.yaw_deg)))),
                    pitch_deg=max(-60.0, min(60.0, float(ov.get("pitch_deg", p.pitch_deg)))),
                    roll_deg=max(-90.0, min(90.0, float(ov.get("roll_deg", p.roll_deg)))),
                )
            )
        return cls(updated)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "displays": [p.as_dict() for p in self.planes],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def plane_for_connector(self, connector: str) -> DisplayPlane | None:
        for p in self.planes:
            if p.connector == connector:
                return p
        return None

    def plane_for_pixel(self, px: float, py: float) -> DisplayPlane:
        for p in self.planes:
            if p.contains_pixel(px, py):
                return p
        # Nearest monitor bounding box in pixel space
        best = self.planes[0]
        best_dist = float("inf")
        for p in self.planes:
            cx = max(p.x, min(p.x + p.width - 1, px))
            cy = max(p.y, min(p.y + p.height - 1, py))
            d = (px - cx) ** 2 + (py - cy) ** 2
            if d < best_dist:
                best_dist = d
                best = p
        return best

    def pixel_to_ray_angles(
        self,
        px: float,
        py: float,
        *,
        eye_origin_mm: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> tuple[float, float, str, float, float]:
        """Map an OS desktop pixel to 3D gaze ray (az_rad, el_rad, connector, u, v)."""
        plane = self.plane_for_pixel(px, py)
        u, v = plane.pixel_to_uv(px, py)
        pt_3d = plane.uv_to_3d(u, v)
        origin = np.asarray(eye_origin_mm, dtype=np.float64)
        az, el = angles_from_ray(pt_3d - origin)
        return az, el, plane.connector, u, v

    def ray_angles_to_pixel(
        self,
        az_rad: float,
        el_rad: float,
        *,
        eye_origin_mm: tuple[float, float, float] = (0.0, 0.0, 0.0),
        local_offsets: dict[str, tuple[float, float]] | None = None,
    ) -> RayHit:
        """Intersect a 3D gaze ray with all DisplayPlanes and return the best screen hit."""
        origin = np.asarray(eye_origin_mm, dtype=np.float64)
        d = ray_from_angles(az_rad, el_rad)

        best_in: tuple[float, DisplayPlane, float, float] | None = None
        best_gap: tuple[float, float, DisplayPlane, float, float] | None = None

        for plane in self.planes:
            center, u_axis, v_axis, normal = plane.basis()
            denom = float(np.dot(d, normal))
            if abs(denom) < 1e-6:
                continue
            t = float(np.dot(center - origin, normal)) / denom
            if t <= 10.0:
                continue
            hit_3d = origin + t * d
            rel = hit_3d - center
            u = 0.5 + float(np.dot(rel, u_axis)) / max(1.0, plane.width_mm)
            v = 0.5 + float(np.dot(rel, v_axis)) / max(1.0, plane.height_mm)

            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                if best_in is None or t < best_in[0]:
                    best_in = (t, plane, u, v)
            else:
                du_mm = max(0.0, -u, u - 1.0) * plane.width_mm
                dv_mm = max(0.0, -v, v - 1.0) * plane.height_mm
                edge_dist = math.hypot(du_mm, dv_mm)
                if best_gap is None or edge_dist < best_gap[0]:
                    best_gap = (edge_dist, t, plane, u, v)

        if best_in is not None:
            t, plane, u, v = best_in
            in_gap = False
        elif best_gap is not None:
            _ed, t, plane, u, v = best_gap
            in_gap = True
        else:
            plane = self.planes[0]
            t, u, v, in_gap = plane.cz_mm, 0.5, 0.5, True

        if local_offsets and plane.connector in local_offsets:
            du, dv = local_offsets[plane.connector]
            u += du
            v += dv

        px, py = plane.uv_to_pixel(u, v)
        return RayHit(
            x=px,
            y=py,
            connector=plane.connector,
            u=max(0.0, min(1.0, u)),
            v=max(0.0, min(1.0, v)),
            in_gap=in_gap,
            distance_mm=float(t),
        )

    def angular_bounds(self) -> tuple[float, float, float, float]:
        """Return (min_az, max_az, min_el, max_el) radians spanning all display corners."""
        azs: list[float] = []
        els: list[float] = []
        for p in self.planes:
            for u in (0.0, 1.0):
                for v in (0.0, 1.0):
                    az, el = angles_from_ray(p.uv_to_3d(u, v))
                    azs.append(az)
                    els.append(el)
        return min(azs), max(azs), min(els), max(els)

    def as_dict(self) -> dict[str, Any]:
        return {
            "displays": [p.as_dict() for p in self.planes],
        }


__all__ = [
    "DEFAULT_DISTANCE_MM",
    "DEFAULT_GAP_MM",
    "DisplayPlane",
    "DisplayRig",
    "RayHit",
    "angles_from_ray",
    "ray_from_angles",
]
