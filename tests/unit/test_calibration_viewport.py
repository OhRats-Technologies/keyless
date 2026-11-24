"""Browser-stage warm-up only advances while the page covers the whole display."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

from keyless.gaze.calibration import CalibrationSession
from keyless.gaze.displays import DisplayRig


class _Publisher:
    def __init__(self) -> None:
        mon = NS(connector="Built-in", x=0, y=0, width=1470, height=956, scale=2.0,
                 is_primary=True, width_mm=290.6, height_mm=189.0)
        self.estimator = NS(_base_rig=DisplayRig.from_layout(NS(monitors=[mon], total_width=1470, total_height=956)))
        self.paths = None
        self.calibrating = False

    def latest_prediction(self):  # face always visible
        return NS(face_detected=True, timestamp_ns=time.monotonic_ns(), tz=-0.5)

    def add_frame_listener(self, _cb) -> None:
        pass

    def remove_frame_listener(self, _cb) -> None:
        pass


def _wait_for(sess: CalibrationSession, state: str, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if sess.state == state:
            return True
        time.sleep(0.02)
    return False


def test_browser_stage_pauses_until_full_screen_then_resumes() -> None:
    sess = CalibrationSession(_Publisher(), use_overlay=False)
    assert sess.mode == "browser"
    sess.start()
    try:
        assert _wait_for(sess, "paused")
        assert sess.message == "not_fullscreen"
        sess.set_viewport(True)
        assert _wait_for(sess, "running")
    finally:
        sess.cancel()
