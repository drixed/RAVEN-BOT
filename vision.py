from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from mss import mss

from window_manager import WindowRect
import win32gui


@dataclass
class RadarROI:
    x: int = 0
    y: int = 0
    w: int = 200
    h: int = 200

    def as_dict(self) -> dict:
        return {"x": int(self.x), "y": int(self.y), "w": int(self.w), "h": int(self.h)}

    @staticmethod
    def from_dict(d: dict) -> "RadarROI":
        return RadarROI(
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", 200)),
            h=int(d.get("h", 200)),
        )


@dataclass
class EnemyColorHSV:
    # Default: "red" (often used for enemy dots). You can tune in UI.
    h1_low: int = 0
    s1_low: int = 140
    v1_low: int = 120
    h1_high: int = 10
    s1_high: int = 255
    v1_high: int = 255

    h2_low: int = 170
    s2_low: int = 140
    v2_low: int = 120
    h2_high: int = 180
    s2_high: int = 255
    v2_high: int = 255

    min_pixels: int = 25  # number of pixels in mask to trigger detection

    def as_dict(self) -> dict:
        return {
            "h1_low": self.h1_low,
            "s1_low": self.s1_low,
            "v1_low": self.v1_low,
            "h1_high": self.h1_high,
            "s1_high": self.s1_high,
            "v1_high": self.v1_high,
            "h2_low": self.h2_low,
            "s2_low": self.s2_low,
            "v2_low": self.v2_low,
            "h2_high": self.h2_high,
            "s2_high": self.s2_high,
            "v2_high": self.v2_high,
            "min_pixels": self.min_pixels,
        }

    @staticmethod
    def from_dict(d: dict) -> "EnemyColorHSV":
        obj = EnemyColorHSV()
        for k in obj.as_dict().keys():
            if k in d:
                setattr(obj, k, int(d[k]))
        return obj


class Vision:
    def __init__(self):
        # Important: on Windows, MSS internals are thread-local.
        # The bot runs in a background thread, so we create MSS inside grab()
        # (same thread) to avoid errors like: "_thread._local has no attribute 'srcdc'".
        pass

    def grab_radar_bgr(self, window_rect: WindowRect, roi: RadarROI) -> np.ndarray:
        mon = {
            "left": int(window_rect.left + roi.x),
            "top": int(window_rect.top + roi.y),
            "width": int(max(1, roi.w)),
            "height": int(max(1, roi.h)),
        }
        with mss() as sct:
            img = np.array(sct.grab(mon))  # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def grab_client_roi_bgr(self, hwnd: int, roi: RadarROI) -> np.ndarray:
        """
        Grab ROI relative to the *client area* of hwnd.
        This avoids window frame/titlebar offsets and matches ROI picked from client screenshots.
        """
        # Capture full client and crop (more consistent than grabbing ROI directly
        # when Windows scaling/offsets are involved).
        full = self.grab_client_bgr(hwnd)
        x0 = max(0, int(roi.x))
        y0 = max(0, int(roi.y))
        x1 = min(int(full.shape[1]), x0 + int(max(1, roi.w)))
        y1 = min(int(full.shape[0]), y0 + int(max(1, roi.h)))
        return full[y0:y1, x0:x1].copy()

    def grab_client_bgr(self, hwnd: int) -> np.ndarray:
        """
        Grab the full client area of hwnd as BGR.
        """
        cl, ct, cr, cb = win32gui.GetClientRect(int(hwnd))
        (cx, cy) = win32gui.ClientToScreen(int(hwnd), (int(cl), int(ct)))
        w = max(1, int(cr - cl))
        h = max(1, int(cb - ct))
        mon = {"left": int(cx), "top": int(cy), "width": int(w), "height": int(h)}
        with mss() as sct:
            img = np.array(sct.grab(mon))  # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def detect_enemy_by_color(
        self, radar_bgr: np.ndarray, cfg: EnemyColorHSV
    ) -> tuple[bool, int]:
        hsv = cv2.cvtColor(radar_bgr, cv2.COLOR_BGR2HSV)

        lower1 = np.array([cfg.h1_low, cfg.s1_low, cfg.v1_low], dtype=np.uint8)
        upper1 = np.array([cfg.h1_high, cfg.s1_high, cfg.v1_high], dtype=np.uint8)
        mask1 = cv2.inRange(hsv, lower1, upper1)

        lower2 = np.array([cfg.h2_low, cfg.s2_low, cfg.v2_low], dtype=np.uint8)
        upper2 = np.array([cfg.h2_high, cfg.s2_high, cfg.v2_high], dtype=np.uint8)
        mask2 = cv2.inRange(hsv, lower2, upper2)

        mask = cv2.bitwise_or(mask1, mask2)

        # clean-up small noise
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_DILATE, kernel, iterations=1)

        count = int(cv2.countNonZero(mask))
        return count >= int(cfg.min_pixels), count

    def detect_enemy_by_diff(
        self,
        radar_bgr: np.ndarray,
        empty_bgr: np.ndarray,
        min_changed_pixels: int = 400,
        diff_threshold: int = 22,
    ) -> tuple[bool, int]:
        """
        Detects "something appeared" in radar ROI by comparing with a baseline empty image.

        Returns (detected, changed_pixels).
        """
        if radar_bgr.shape[:2] != empty_bgr.shape[:2]:
            empty_bgr = cv2.resize(empty_bgr, (radar_bgr.shape[1], radar_bgr.shape[0]))

        a = cv2.cvtColor(radar_bgr, cv2.COLOR_BGR2GRAY)
        b = cv2.cvtColor(empty_bgr, cv2.COLOR_BGR2GRAY)

        a = cv2.GaussianBlur(a, (3, 3), 0)
        b = cv2.GaussianBlur(b, (3, 3), 0)

        diff = cv2.absdiff(a, b)
        _, mask = cv2.threshold(diff, int(diff_threshold), 255, cv2.THRESH_BINARY)

        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_DILATE, kernel, iterations=1)

        changed = int(cv2.countNonZero(mask))
        return changed >= int(min_changed_pixels), changed

    def empty_match_score(self, radar_bgr: np.ndarray, empty_bgr: np.ndarray) -> float:
        """
        Returns similarity score [~ -1..1] where higher means "looks like empty baseline".
        Uses normalized cross-correlation on grayscale.
        """
        if radar_bgr.shape[:2] != empty_bgr.shape[:2]:
            empty_bgr = cv2.resize(empty_bgr, (radar_bgr.shape[1], radar_bgr.shape[0]))

        a = cv2.cvtColor(radar_bgr, cv2.COLOR_BGR2GRAY)
        b = cv2.cvtColor(empty_bgr, cv2.COLOR_BGR2GRAY)

        # Stabilize against minor noise
        a = cv2.GaussianBlur(a, (3, 3), 0)
        b = cv2.GaussianBlur(b, (3, 3), 0)

        res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(res)
        return float(max_val)

    def text_match_score(self, cur_bgr: np.ndarray, tpl_bgr: np.ndarray) -> float:
        """
        Like empty_match_score(), but pre-processes images to emphasize text.

        This is much more stable when UI background animates, because we compare
        binarized "text silhouettes" instead of raw pixels.
        """
        def prep(img_bgr: np.ndarray) -> np.ndarray:
            g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            g = cv2.GaussianBlur(g, (3, 3), 0)
            # Adaptive threshold is robust to brightness shifts.
            bw = cv2.adaptiveThreshold(
                g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 7
            )
            # Remove small speckle noise; keep letter shapes.
            k = np.ones((2, 2), np.uint8)
            bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k, iterations=1)
            return bw

        a = prep(cur_bgr)   # current Text ROI (can be larger and include other UI)
        b = prep(tpl_bgr)   # template capture

        # Crop template down to just the text silhouette.
        ys, xs = np.where(b > 0)
        if len(xs) > 0 and len(ys) > 0:
            x0, x1 = int(xs.min()), int(xs.max())
            y0, y1 = int(ys.min()), int(ys.max())
            pad = 4
            x0 = max(0, x0 - pad)
            y0 = max(0, y0 - pad)
            x1 = min(b.shape[1] - 1, x1 + pad)
            y1 = min(b.shape[0] - 1, y1 + pad)
            b = b[y0 : y1 + 1, x0 : x1 + 1]

        # If ROI is smaller than template (bad config), resize template down to fit.
        if a.shape[0] < b.shape[0] or a.shape[1] < b.shape[1]:
            b = cv2.resize(b, (max(1, a.shape[1]), max(1, a.shape[0])))

        # Search template inside current ROI. This makes it robust even if ROI is larger
        # or text shifts a few pixels.
        res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(res)
        return float(max_val)

    def text_mask_pixel_count(self, img_bgr: np.ndarray) -> int:
        """
        Returns how many pixels look like "text" in the ROI after binarization.
        Used to detect unreliable/blank frames and avoid false positives.
        """
        g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        bw = cv2.adaptiveThreshold(
            g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 7
        )
        k = np.ones((2, 2), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k, iterations=1)
        return int(cv2.countNonZero(bw))

    def icon_match_score(self, cur_bgr: np.ndarray, tpl_bgr: np.ndarray) -> float:
        """
        Template match for small icons (no text preprocessing).
        Uses grayscale + slight blur + normalized correlation.
        """
        if cur_bgr is None or tpl_bgr is None:
            return -1.0
        a = cv2.cvtColor(cur_bgr, cv2.COLOR_BGR2GRAY)
        b = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
        a = cv2.GaussianBlur(a, (3, 3), 0)
        b = cv2.GaussianBlur(b, (3, 3), 0)

        # Resize template down if ROI is smaller (misconfig)
        if a.shape[0] < b.shape[0] or a.shape[1] < b.shape[1]:
            b = cv2.resize(b, (max(1, a.shape[1]), max(1, a.shape[0])))

        res = cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(res)
        return float(max_val)

    def hp_percent_from_bar(self, bar_bgr: np.ndarray) -> int | None:
        """
        Estimate HP percent from a horizontal red HP bar ROI.
        Works best when ROI tightly covers the bar fill area (without numbers).
        """
        if bar_bgr is None:
            return None
        h, w = bar_bgr.shape[:2]
        if w < 20 or h < 4:
            return None

        # Focus on the middle band to avoid borders/text (numbers often sit at top/bottom).
        y0 = int(h * 0.20)
        y1 = max(y0 + 1, int(h * 0.80))
        band = bar_bgr[y0:y1, :, :]

        hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)

        # Red/orange fill can be darker/striped; allow lower S/V.
        # Red wraps around 0/180; also include slight orange (up to ~18).
        mask1 = cv2.inRange(hsv, (0, 35, 30), (18, 255, 255))
        mask2 = cv2.inRange(hsv, (165, 35, 30), (180, 255, 255))
        mask = cv2.bitwise_or(mask1, mask2)

        # Clean speckles and fill small gaps (striped fill).
        k = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)

        # Column occupancy: fraction of "fill-colored" pixels per X.
        col = (np.sum(mask > 0, axis=0) / float(mask.shape[0] + 1e-9)).astype(np.float32)

        # Smooth 1D to ignore small holes/stripes.
        if w >= 9:
            col = cv2.GaussianBlur(col.reshape(1, -1), (9, 1), 0).reshape(-1)

        # Consider a column "filled" if >=10% of band matches fill color.
        filled = col >= 0.10

        # Fill small gaps in the boolean vector (e.g. stripes).
        if filled.any() and w >= 7:
            v = (filled.astype(np.uint8) * 255).reshape(1, -1)
            v = cv2.morphologyEx(v, cv2.MORPH_CLOSE, np.ones((1, 9), np.uint8), iterations=1)
            filled = (v.reshape(-1) > 0)

        idx = np.where(filled)[0]
        if idx.size == 0:
            return 0

        last = int(idx.max())
        pct = int(round((last + 1) * 100.0 / float(w)))
        return max(0, min(100, pct))

