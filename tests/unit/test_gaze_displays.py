"""Unit tests for 3D multi-display physical rig geometry (swapped, gapped, angled, N displays)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from keyless.gaze.displays import DisplayPlane, DisplayRig
from keyless.gaze.monocular import FEATURE_DIM, MonocularGazeEstimator


def test_swapped_gapped_angled_three_display_ray_intersection() -> None:
    """Three displays: physically swapped, 40mm bezel gaps, angled inward +/-25 deg, vertical offset."""
    left_phys = DisplayPlane(
        connector="HDMI-A-1",
        x=1920,
        y=0,
        width=1920,
        height=1080,
        width_mm=598.0,
        height_mm=336.0,
        cx_mm=-580.0,
        cy_mm=-20.0,
        cz_mm=540.0,
        yaw_deg=-25.0,
    )
    center_phys = DisplayPlane(
        connector="DP-2",
        x=0,
        y=0,
        width=1920,
        height=1080,
        width_mm=521.0,
        height_mm=293.0,
        cx_mm=0.0,
        cy_mm=0.0,
        cz_mm=600.0,
        yaw_deg=0.0,
    )
    right_phys = DisplayPlane(
        connector="DP-3",
        x=3840,
        y=120,
        width=1440,
        height=2560,
        width_mm=330.0,
        height_mm=590.0,
        cx_mm=490.0,
        cy_mm=40.0,
        cz_mm=550.0,
        yaw_deg=25.0,
        pitch_deg=-8.0,
    )
    rig = DisplayRig([left_phys, center_phys, right_phys])

    # Roundtrip pixel -> 3D ray -> pixel on every display including swapped HDMI-A-1 and portrait DP-3
    for px, py, expected_conn in (
        (2880.0, 540.0, "HDMI-A-1"),  # center of logically-right / physically-left monitor
        (960.0, 540.0, "DP-2"),       # center of physically-center monitor
        (4560.0, 1400.0, "DP-3"),     # center of angled portrait monitor
    ):
        az, el, conn, _u, _v = rig.pixel_to_ray_angles(px, py)
        assert conn == expected_conn
        hit = rig.ray_angles_to_pixel(az, el)
        assert hit.connector == expected_conn
        assert hit.in_gap is False
        assert abs(hit.x - px) < 1.5
        assert abs(hit.y - py) < 1.5

    # Looking straight left (-40 deg) hits the physically-left display (which has OS x=1920..3839!)
    hit_left = rig.ray_angles_to_pixel(math.radians(-40.0), 0.0)
    assert hit_left.connector == "HDMI-A-1"
    assert 1920.0 <= hit_left.x < 3840.0


def test_3d_angular_ridge_generalizes_across_angled_gapped_displays() -> None:
    """Clicks across two angled displays with a 60mm gap train one continuous 3D angular model."""
    left = DisplayPlane(
        connector="DP-2",
        x=0,
        y=0,
        width=1920,
        height=1080,
        width_mm=521.0,
        height_mm=293.0,
        cx_mm=-295.0,
        cy_mm=0.0,
        cz_mm=580.0,
        yaw_deg=-15.0,
    )
    right = DisplayPlane(
        connector="HDMI-A-1",
        x=1920,
        y=0,
        width=1920,
        height=1080,
        width_mm=598.0,
        height_mm=336.0,
        cx_mm=330.0,
        cy_mm=0.0,
        cz_mm=580.0,
        yaw_deg=15.0,
    )
    rig = DisplayRig([left, right])
    est = MonocularGazeEstimator(
        3840,
        1080,
        rig=rig,
        auto_rig=False,
        min_clicks=5,
        blend_clicks=5,
        ridge_alpha=1e-4,
        smooth=False,
    )

    # Synthetic linear eye-feature -> 3D gaze angle (az, el) ground truth
    rng = np.random.default_rng(123)
    for _ in range(30):
        az = float(rng.uniform(-0.75, 0.75))
        el = float(rng.uniform(-0.22, 0.22))
        hit = rig.ray_angles_to_pixel(az, el)
        if hit.in_gap:
            continue
        feat = np.zeros(FEATURE_DIM, dtype=np.float64)
        feat[0] = az * 0.4
        feat[2] = az * 0.4
        feat[4] = az * 0.2
        feat[1] = el * 0.5
        feat[3] = el * 0.5
        feat[5] = el * 0.2
        est.add_click(feat, hit.x, hit.y)

    # Test on right monitor at an unseen angle
    target_hit = rig.ray_angles_to_pixel(0.5, -0.08)
    assert target_hit.connector == "HDMI-A-1"
    test_feat = np.zeros(FEATURE_DIM, dtype=np.float64)
    test_feat[0] = 0.5 * 0.4
    test_feat[2] = 0.5 * 0.4
    test_feat[4] = 0.5 * 0.2
    test_feat[1] = -0.08 * 0.5
    test_feat[3] = -0.08 * 0.5
    test_feat[5] = -0.08 * 0.2
    px, py, _, _, using_ridge = est.predict(test_feat)
    assert using_ridge is True
    assert est.last_display == "HDMI-A-1"
    assert abs(px - target_hit.x) < 25.0
    assert abs(py - target_hit.y) < 25.0


def test_display_rig_persistence_roundtrip(tmp_path: Path) -> None:
    from keyless.platform.linux.display import DisplayLayout, MonitorInfo

    layout = DisplayLayout(
        monitors=(
            MonitorInfo("DP-2", 0, 0, 1920, 1080, 1920, 1080, 1.0, is_primary=True, width_mm=521.0, height_mm=293.0),
            MonitorInfo("HDMI-A-1", 1920, 0, 1920, 1080, 1920, 1080, 1.0, is_primary=False, width_mm=598.0, height_mm=336.0),
        ),
        total_width=3840,
        total_height=1080,
    )
    rig = DisplayRig.from_layout(layout, gap_mm=30.0)
    custom = DisplayRig([
        DisplayPlane(**{**rig.planes[0].as_dict(), "yaw_deg": -18.0, "cx_mm": 300.0}),
        DisplayPlane(**{**rig.planes[1].as_dict(), "yaw_deg": 18.0, "cx_mm": -300.0}),
    ])
    rig_path = tmp_path / "display_rig.json"
    custom.save(rig_path)
    loaded = DisplayRig.load(rig_path, layout)
    assert loaded.plane_for_connector("DP-2").yaw_deg == pytest.approx(-18.0)
    assert loaded.plane_for_connector("DP-2").cx_mm == pytest.approx(300.0)
    assert loaded.plane_for_connector("HDMI-A-1").cx_mm == pytest.approx(-300.0)


def test_auto_infer_rig_detects_swapped_monitors_and_gap_from_passive_clicks() -> None:
    """Zero manual setup: clicks with anatomical head yaw immediately detect physically swapped displays."""
    from keyless.gaze.displays import infer_rig_from_clicks

    # OS thinks DP-2 is left (x=0..1919) and HDMI-A-1 is right (x=1920..3839)
    os_rig = DisplayRig(
        [
            DisplayPlane("DP-2", 0, 0, 1920, 1080, width_mm=521.0, height_mm=293.0, cx_mm=-280.0, cz_mm=600.0),
            DisplayPlane("HDMI-A-1", 1920, 0, 1920, 1080, width_mm=598.0, height_mm=336.0, cx_mm=280.0, cz_mm=600.0),
        ]
    )

    # Physically, user looks RIGHT (negative MediaPipe yaw) to click DP-2 and LEFT (positive MediaPipe yaw) to click HDMI-A-1
    samples = []
    for u in (0.2, 0.5, 0.8):
        # Click on DP-2 (OS left), user looks right (screen az = +0.45 rad -> MediaPipe yaw = -0.45/0.85)
        f_dp = np.zeros(FEATURE_DIM, dtype=np.float64)
        f_dp[4] = -(0.45 + (u - 0.5) * 0.55) / 0.85
        samples.append((f_dp, u * 1920.0, 540.0))

        # Click on HDMI-A-1 (OS right), user looks left (screen az = -0.50 rad -> MediaPipe yaw = +0.50/0.85)
        f_hd = np.zeros(FEATURE_DIM, dtype=np.float64)
        f_hd[4] = -(-0.50 + (u - 0.5) * 0.60) / 0.85
        samples.append((f_hd, 1920.0 + u * 1920.0, 540.0))

    inferred, meta = infer_rig_from_clicks(os_rig, samples)
    assert meta["inferred"] is True
    assert meta["swapped"] is True
    dp = inferred.plane_for_connector("DP-2")
    hd = inferred.plane_for_connector("HDMI-A-1")
    assert dp is not None and hd is not None
    # DP-2 is inferred physically on the right (+X, +yaw), HDMI-A-1 on the left (-X, -yaw)
    assert dp.cx_mm > 150.0 and dp.yaw_deg > 5.0
    assert hd.cx_mm < -150.0 and hd.yaw_deg < -5.0
