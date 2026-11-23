"""Linux screen capture (GNOME Shell D-Bus + XDG portal) and monitor routing."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from keyless.platform.base import DisplayLayout, MonitorInfo

logger = logging.getLogger(__name__)

try:
    import gi  # type: ignore[import-not-found]

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # type: ignore[import-not-found]

    _HAS_ATSPI = True
except Exception:
    _HAS_ATSPI = False

try:
    from jeepney import DBusAddress, HeaderFields, MatchRule, new_method_call
    from jeepney.io.blocking import DBusConnection, open_dbus_connection
    from jeepney.wrappers import DBusErrorResponse

    _HAS_JEEPNEY = True
except ImportError:
    _HAS_JEEPNEY = False


@dataclass(frozen=True)
class ActiveWindowContext:
    """Focused desktop window identity and screen rectangle."""

    app_id: str
    title: str
    rect: tuple[int, int, int, int] | None


class WaylandScreenCapturer:
    """Tear-free area/desktop capture on Ubuntu GNOME Wayland via D-Bus."""

    def __init__(self, shm_path: Path | None = None) -> None:
        self._shm_path = shm_path or Path("/dev/shm/kk_gaze_roi.bmp")
        self._lock = threading.Lock()
        self._conn: DBusConnection | None = None
        self._shell_area_denied = False
        self._portal_match_rules: set[str] = set()

    def _get_conn(self) -> DBusConnection | None:
        if not _HAS_JEEPNEY:
            return None
        if self._conn is None:
            self._conn = open_dbus_connection(bus="SESSION")
        return self._conn

    def _try_gnome_shell_area(self, x: int, y: int, w: int, h: int) -> np.ndarray | None:
        """Try `org.gnome.Shell.Screenshot.ScreenshotArea` (~12ms when permitted)."""
        if self._shell_area_denied:
            return None
        conn = self._get_conn()
        if conn is not None:
            try:
                msg = new_method_call(
                    DBusAddress(
                        "/org/gnome/Shell/Screenshot",
                        bus_name="org.gnome.Shell.Screenshot",
                        interface="org.gnome.Shell.Screenshot",
                    ),
                    "ScreenshotArea",
                    "iiiibs",
                    (int(x), int(y), int(w), int(h), False, str(self._shm_path)),
                )
                reply = conn.send_and_get_reply(msg, timeout=1.0)
                ok = bool(reply.body[0]) if reply.body else False
                if ok and self._shm_path.exists():
                    img = cv2.imread(str(self._shm_path), cv2.IMREAD_COLOR)
                    if img is not None and img.size > 0:
                        return img
                self._shell_area_denied = True
                return None
            except DBusErrorResponse as exc:
                logger.debug("GNOME Shell ScreenshotArea denied/failed (%s); using portal.", exc)
                self._shell_area_denied = True
                return None
            except Exception as exc:
                logger.debug("GNOME Shell ScreenshotArea error (%s); using portal.", exc)
                self._shell_area_denied = True
                return None

        # Fallback to gdbus CLI if jeepney is unavailable
        try:
            res = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.gnome.Shell.Screenshot",
                    "--object-path",
                    "/org/gnome/Shell/Screenshot",
                    "--method",
                    "org.gnome.Shell.Screenshot.ScreenshotArea",
                    str(int(x)),
                    str(int(y)),
                    str(int(w)),
                    str(int(h)),
                    "false",
                    str(self._shm_path),
                ],
                check=False,
                capture_output=True,
                timeout=1.5,
            )
            if res.returncode == 0 and self._shm_path.exists():
                img = cv2.imread(str(self._shm_path), cv2.IMREAD_COLOR)
                if img is not None and img.size > 0:
                    return img
        except Exception:
            pass
        self._shell_area_denied = True
        return None

    def _wayland_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
        if not env.get("WAYLAND_DISPLAY"):
            runtime_dir = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
            sockets = [s for s in sorted(runtime_dir.glob("wayland-[0-9]*")) if not s.name.endswith(".lock")]
            if sockets:
                env["WAYLAND_DISPLAY"] = sockets[0].name
        return env

    def _try_kde_spectacle_full(self) -> np.ndarray | None:
        """Capture full desktop non-interactively via KDE Spectacle on Plasma Wayland."""
        import shutil
        import tempfile

        if shutil.which("spectacle") is None:
            return None
        fd, tmp_name = tempfile.mkstemp(prefix="kk_spectacle_", suffix=".png")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            res = subprocess.run(
                ["spectacle", "-b", "-n", "-f", "-o", str(tmp_path)],
                env=self._wayland_env(),
                capture_output=True,
                timeout=2.5,
                check=False,
            )
            if res.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
                img = cv2.imread(str(tmp_path), cv2.IMREAD_COLOR)
                if img is not None and img.size > 0:
                    return img
        except Exception as exc:
            logger.debug("KDE spectacle capture failed: %s", exc)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    def capture_full_desktop(self) -> np.ndarray | None:
        """Capture full desktop on Wayland (KDE Spectacle first, then XDG Portal)."""
        img = self._try_kde_spectacle_full()
        if img is not None:
            return img
        return self.capture_full_desktop_portal()

    def capture_full_desktop_portal(self) -> np.ndarray | None:
        """Capture full desktop via sanctioned `org.freedesktop.portal.Screenshot`."""
        conn = self._get_conn()
        if conn is None:
            return None
        try:
            token = f"kk_{time.monotonic_ns()}"
            sender = conn.unique_name[1:].replace(".", "_")
            req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

            if sender not in self._portal_match_rules:
                rule = MatchRule(
                    type="signal",
                    interface="org.freedesktop.portal.Request",
                    member="Response",
                )
                conn.send_and_get_reply(
                    new_method_call(
                        DBusAddress(
                            "/org/freedesktop/DBus",
                            bus_name="org.freedesktop.DBus",
                            interface="org.freedesktop.DBus",
                        ),
                        "AddMatch",
                        "s",
                        (rule.serialise(),),
                    ),
                    timeout=1.0,
                )
                self._portal_match_rules.add(sender)

            call_msg = new_method_call(
                DBusAddress(
                    "/org/freedesktop/portal/desktop",
                    bus_name="org.freedesktop.portal.Desktop",
                    interface="org.freedesktop.portal.Screenshot",
                ),
                "Screenshot",
                "sa{sv}",
                ("", {"handle_token": ("s", token), "interactive": ("b", False)}),
            )
            reply = conn.send_and_get_reply(call_msg, timeout=1.5)
            if reply.body:
                req_path = str(reply.body[0])

            deadline = time.monotonic() + 3.0
            uri: str | None = None
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                msg = conn.receive(timeout=remaining)
                if (
                    msg.header.fields.get(HeaderFields.path) == req_path
                    and msg.header.fields.get(HeaderFields.member) == "Response"
                ):
                    code, results = msg.body
                    if code == 0 and "uri" in results:
                        uri = str(results["uri"][1])
                    break

            if not uri:
                return None
            file_path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
            try:
                img = cv2.imread(file_path, cv2.IMREAD_COLOR)
            finally:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            return img
        except Exception as exc:
            logger.debug("Portal Screenshot failed: %s", exc)
            # Reset connection on unexpected D-Bus transport error
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._portal_match_rules.clear()
            return None

    def capture_rect_with_full(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        desktop_w: int | None = None,
        desktop_h: int | None = None,
        cached_full_bgr: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        """Capture `(x, y, w, h)` in logical desktop coords, returning `(crop_bgr, full_bgr)`."""
        if cached_full_bgr is not None:
            crop = self._crop_from_full(
                cached_full_bgr, x, y, w, h, desktop_w=desktop_w, desktop_h=desktop_h
            )
            return crop, cached_full_bgr

        with self._lock:
            area_img = self._try_gnome_shell_area(x, y, w, h)
            if area_img is not None:
                return area_img, None

            full_img = self.capture_full_desktop()
            if full_img is not None and full_img.size > 0:
                crop = self._crop_from_full(
                    full_img, x, y, w, h, desktop_w=desktop_w, desktop_h=desktop_h
                )
                return crop, full_img

        raise RuntimeError(
            "Wayland screenshot failed (both GNOME Shell ScreenshotArea and XDG Portal Screenshot failed)."
        )

    def get_active_window_rect(
        self,
        screen_w: int,
        screen_h: int,
    ) -> tuple[int, int, int, int] | None:
        """Attempt to resolve active window `(x, y, w, h)` via AT-SPI root frame or XWayland."""
        if _HAS_ATSPI:
            try:
                desktop = Atspi.get_desktop(0)
                for i in range(desktop.get_child_count()):
                    app = desktop.get_child_at_index(i)
                    if not app:
                        continue
                    for j in range(app.get_child_count()):
                        win = app.get_child_at_index(j)
                        if not win:
                            continue
                        st = win.get_state_set()
                        if st and st.contains(Atspi.StateType.ACTIVE):
                            comp = win.get_component_iface()
                            if comp:
                                ext = comp.get_extents(Atspi.CoordType.SCREEN)
                                if ext.width >= 120 and ext.height >= 80:
                                    x = max(0, min(screen_w - 1, int(ext.x)))
                                    y = max(0, min(screen_h - 1, int(ext.y)))
                                    w = min(screen_w - x, int(ext.width))
                                    h = min(screen_h - y, int(ext.height))
                                    return x, y, w, h
            except Exception:
                pass

        try:
            out = subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
                stderr=subprocess.DEVNULL,
                timeout=0.04,
                text=True,
            )
            vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            wx, wy = int(vals["X"]), int(vals["Y"])
            ww, wh = int(vals["WIDTH"]), int(vals["HEIGHT"])
            if ww >= 120 and wh >= 80:
                x = max(0, min(screen_w - 1, wx))
                y = max(0, min(screen_h - 1, wy))
                return x, y, min(screen_w - x, ww), min(screen_h - y, wh)
        except Exception:
            pass

        return None

    def get_active_window_context(
        self,
        screen_w: int,
        screen_h: int,
        desktop_atspi: object | None = None,
    ) -> ActiveWindowContext:
        """Return `(app_id, title, rect)` of the focused window via AT-SPI or XWayland."""
        app_id = ""
        title = ""
        if desktop_atspi is not None and getattr(desktop_atspi, "available", False):
            try:
                raw_app, raw_title = desktop_atspi.get_active_window_info()  # type: ignore[attr-defined]
                app_id = str(raw_app or "").strip().lower()
                title = str(raw_title or "").strip()
            except Exception:
                pass
        elif _HAS_ATSPI:
            try:
                desktop = Atspi.get_desktop(0)
                for i in range(desktop.get_child_count()):
                    app = desktop.get_child_at_index(i)
                    if not app:
                        continue
                    for j in range(app.get_child_count()):
                        win = app.get_child_at_index(j)
                        if not win:
                            continue
                        st = win.get_state_set()
                        if st and st.contains(Atspi.StateType.ACTIVE):
                            app_id = (app.get_name() or "").strip().lower()
                            title = (win.get_name() or "").strip()
                            break
                    if app_id:
                        break
            except Exception:
                pass

        rect = self.get_active_window_rect(screen_w, screen_h)
        return ActiveWindowContext(app_id=app_id, title=title, rect=rect)

    def capture_rect(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        """Capture desktop rectangle `(x, y, w, h)`."""
        crop, _ = self.capture_rect_with_full(x, y, w, h)
        return crop

    @staticmethod
    def _crop_from_full(
        full_bgr: np.ndarray,
        x: int,
        y: int,
        w: int,
        h: int,
        desktop_w: int | None = None,
        desktop_h: int | None = None,
    ) -> np.ndarray:
        fh, fw = full_bgr.shape[:2]
        sx = (fw / float(desktop_w)) if desktop_w and desktop_w > 0 else 1.0
        sy = (fh / float(desktop_h)) if desktop_h and desktop_h > 0 else 1.0

        px1 = max(0, min(fw - 1, int(round(x * sx))))
        py1 = max(0, min(fh - 1, int(round(y * sy))))
        px2 = max(px1 + 1, min(fw, int(round((x + w) * sx))))
        py2 = max(py1 + 1, min(fh, int(round((y + h) * sy))))
        crop = full_bgr[py1:py2, px1:px2]
        if abs(sx - 1.0) > 0.02 or abs(sy - 1.0) > 0.02:
            crop = cv2.resize(crop, (max(1, int(w)), max(1, int(h))), interpolation=cv2.INTER_AREA)
        return crop.copy()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._portal_match_rules.clear()


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(area_a + area_b - inter + 1e-5)


def monitor_at_point(layout: DisplayLayout, x: float, y: float) -> MonitorInfo:
    """Return the monitor containing `(x, y)`, falling back to the primary monitor."""
    for mon in layout.monitors:
        if mon.x <= x < (mon.x + mon.width) and mon.y <= y < (mon.y + mon.height):
            return mon
    return layout.primary
