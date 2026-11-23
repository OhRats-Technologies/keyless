"""Screen region used by the gaze preview and cursor overlay.

The old calibration file no longer defines a region. The eye cursor may roam
the whole virtual desktop, so the region is simply the layout bounds.
"""

from __future__ import annotations

from PySide6.QtGui import QRegion

from keyless.platform.base import DisplayLayout


def gaze_region(layout: DisplayLayout) -> QRegion:
    return QRegion(0, 0, int(layout.total_width), int(layout.total_height))
