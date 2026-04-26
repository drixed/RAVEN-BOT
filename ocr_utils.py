from __future__ import annotations

import re
from typing import Optional, Tuple, Dict, Any, List

import cv2
import numpy as np


def _prep_digits(img_bgr: np.ndarray) -> np.ndarray:
    # Heuristic: many game UIs show an icon above the number. Prefer lower part.
    try:
        h = int(img_bgr.shape[0])
        if h >= 18:
            y0 = int(h * 0.35)
            img_bgr = img_bgr[y0:, :]
    except Exception:
        pass

    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    g = cv2.GaussianBlur(g, (3, 3), 0)

    # Improve local contrast (helps white digits on textured background)
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        g = clahe.apply(g)
    except Exception:
        pass

    # Adaptive threshold is usually more stable than Otsu on textured icons.
    bw = cv2.adaptiveThreshold(
        g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 7
    )  # digits -> white

    # Clear border to avoid accidental blobs
    bw[:2, :] = 0
    bw[-2:, :] = 0
    bw[:, :2] = 0
    bw[:, -2:] = 0

    k = np.ones((2, 2), np.uint8)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k, iterations=1)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, k, iterations=1)

    # Remove tiny connected components (noise)
    try:
        num, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        out = np.zeros_like(bw)
        min_area = max(12, int(bw.shape[0] * bw.shape[1] * 0.002))
        for i in range(1, num):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area >= min_area:
                out[labels == i] = 255
        bw = out
    except Exception:
        pass

    return bw


def _render_digit_templates(size: int = 28) -> dict[int, np.ndarray]:
    """
    Render a small set of digit templates using OpenCV fonts.
    Used as a no-external-deps fallback when Tesseract isn't available.
    """
    fonts = [
        cv2.FONT_HERSHEY_SIMPLEX,
        cv2.FONT_HERSHEY_DUPLEX,
        cv2.FONT_HERSHEY_TRIPLEX,
    ]
    out: dict[int, list[np.ndarray]] = {d: [] for d in range(10)}
    for d in range(10):
        for font in fonts:
            for thickness in (2, 3, 4):
                img = np.zeros((size, size), dtype=np.uint8)
                txt = str(d)
                (tw, th), _ = cv2.getTextSize(txt, font, 0.9, thickness)
                x = max(0, (size - tw) // 2)
                y = max(th + 1, (size + th) // 2)
                cv2.putText(img, txt, (x, y), font, 0.9, 255, thickness, cv2.LINE_AA)
                # binarize to reduce anti-aliasing differences
                _, img = cv2.threshold(img, 60, 255, cv2.THRESH_BINARY)
                out[d].append(img)
    # keep best single template per digit by averaging variants
    best: dict[int, np.ndarray] = {}
    for d, imgs in out.items():
        stack = np.stack(imgs, axis=0).astype(np.float32)
        avg = np.mean(stack, axis=0)
        _, avg = cv2.threshold(avg.astype(np.uint8), 80, 255, cv2.THRESH_BINARY)
        best[d] = avg
    return best


_TPL = _render_digit_templates()


def _hog(img_bin_28: np.ndarray) -> np.ndarray:
    hog = cv2.HOGDescriptor(
        _winSize=(28, 28),
        _blockSize=(14, 14),
        _blockStride=(7, 7),
        _cellSize=(7, 7),
        _nbins=9,
    )
    f = hog.compute(img_bin_28)
    return f.reshape(1, -1).astype(np.float32)


def _build_knn() -> "cv2.ml_KNearest":
    fonts = [
        cv2.FONT_HERSHEY_SIMPLEX,
        cv2.FONT_HERSHEY_DUPLEX,
        cv2.FONT_HERSHEY_TRIPLEX,
        cv2.FONT_HERSHEY_COMPLEX,
    ]
    xs: List[np.ndarray] = []
    ys: List[int] = []
    rng = np.random.default_rng(1234)
    for d in range(10):
        for font in fonts:
            for thickness in (2, 3, 4):
                for scale in (0.78, 0.86, 0.94, 1.02):
                    for _ in range(8):
                        img = np.zeros((28, 28), dtype=np.uint8)
                        txt = str(d)
                        (tw, th), _ = cv2.getTextSize(txt, font, float(scale), thickness)
                        x = int(max(0, (28 - tw) // 2 + rng.integers(-2, 3)))
                        y = int(max(th + 1, (28 + th) // 2 + rng.integers(-2, 3)))
                        cv2.putText(img, txt, (x, y), font, float(scale), 255, thickness, cv2.LINE_AA)
                        if rng.random() < 0.6:
                            k = np.ones((2, 2), np.uint8)
                            img = cv2.dilate(img, k, iterations=1)
                        if rng.random() < 0.35:
                            img = cv2.GaussianBlur(img, (3, 3), 0)
                        _, img = cv2.threshold(img, 60, 255, cv2.THRESH_BINARY)
                        xs.append(_hog(img))
                        ys.append(d)
    X = np.vstack(xs)
    y = np.array(ys, dtype=np.int32).reshape(-1, 1)
    knn = cv2.ml.KNearest_create()
    knn.train(X, cv2.ml.ROW_SAMPLE, y)
    return knn


_KNN = _build_knn()


def _classify_digit_knn(img_bin_28: np.ndarray) -> Tuple[int, float]:
    feat = _hog(img_bin_28)
    _ret, results, _neigh, dist = _KNN.findNearest(feat, k=3)
    digit = int(results[0, 0])
    d0 = float(dist[0, 0]) if dist is not None and dist.size else 1e9
    conf = float(1.0 / (1.0 + d0))
    return digit, conf


def _segment_digit_boxes(bw: np.ndarray) -> List[Tuple[int, int, int, int]]:
    H, W = bw.shape[:2]
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: List[Tuple[int, int, int, int]] = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w < 3 or h < 8:
            continue
        if h < int(H * 0.35):
            continue
        if w > int(W * 0.80):
            continue
        boxes.append((x, y, w, h))
    boxes.sort(key=lambda b: b[0])
    return boxes


def _segment_projection_boxes(bw: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """
    Segment digits by vertical projection gaps (works well when mask is readable
    but contours are merged or split oddly).
    """
    H, W = bw.shape[:2]
    col = np.sum(bw > 0, axis=0).astype(np.int32)
    # columns treated as "gap" if almost empty
    gap_thr = max(1, int(0.03 * H))
    gap = col <= gap_thr

    # Find continuous non-gap runs
    runs: List[Tuple[int, int]] = []
    in_run = False
    start = 0
    for i in range(W):
        if not gap[i] and not in_run:
            in_run = True
            start = i
        elif gap[i] and in_run:
            in_run = False
            runs.append((start, i))
    if in_run:
        runs.append((start, W))

    boxes: List[Tuple[int, int, int, int]] = []
    for a, b in runs:
        ww = int(b - a)
        if ww < 4:
            continue
        seg = bw[:, a:b]
        rows = np.where(np.sum(seg > 0, axis=1) > 0)[0]
        cols = np.where(np.sum(seg > 0, axis=0) > 0)[0]
        if len(rows) == 0 or len(cols) == 0:
            continue
        y0, y1 = int(rows.min()), int(rows.max())
        x0, x1 = int(cols.min()), int(cols.max())
        w = int(x1 - x0 + 1)
        h = int(y1 - y0 + 1)
        if w < 3 or h < 8:
            continue
        boxes.append((int(a + x0), int(y0), int(w), int(h)))

    boxes.sort(key=lambda b: b[0])
    return boxes


def _tighten_mask_to_text(bw: np.ndarray) -> np.ndarray:
    """
    Reduce noise by keeping only the densest horizontal band + largest components.
    This helps when PREP mask is readable but has many stray pixels/lines.
    """
    try:
        H, W = bw.shape[:2]
        # Crop to densest row band
        rows = np.sum(bw > 0, axis=1).astype(np.int32)
        if rows.max() > 0:
            thr = max(1, int(rows.max() * 0.25))
            ys = np.where(rows >= thr)[0]
            if len(ys) > 0:
                y0 = max(0, int(ys.min()) - 2)
                y1 = min(H - 1, int(ys.max()) + 2)
                bw = bw[y0 : y1 + 1, :]

        # Keep only largest connected components (digits), drop tiny noise/lines
        num, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        if num <= 1:
            return bw
        areas = [(i, int(stats[i, cv2.CC_STAT_AREA])) for i in range(1, num)]
        areas.sort(key=lambda x: x[1], reverse=True)
        keep = {i for i, a in areas[:12] if a >= 10}
        out = np.zeros_like(bw)
        for i in keep:
            out[labels == i] = 255
        bw = out

        # Final tighten to bounding box of remaining pixels
        ys, xs = np.where(bw > 0)
        if len(xs) > 0 and len(ys) > 0:
            x0, x1 = int(xs.min()), int(xs.max())
            y0, y1 = int(ys.min()), int(ys.max())
            pad = 2
            x0 = max(0, x0 - pad)
            y0 = max(0, y0 - pad)
            x1 = min(bw.shape[1] - 1, x1 + pad)
            y1 = min(bw.shape[0] - 1, y1 + pad)
            bw = bw[y0 : y1 + 1, x0 : x1 + 1]
        return bw
    except Exception:
        return bw


def _split_wide_box(bw: np.ndarray, box: Tuple[int, int, int, int]) -> List[Tuple[int, int, int, int]]:
    """
    If digits are connected into one wide blob, split by vertical projection gaps.
    Returns list of sub-boxes (x,y,w,h) in bw coords.
    """
    x, y, w, h = box
    roi = bw[y : y + h, x : x + w]
    col = np.sum(roi > 0, axis=0).astype(np.int32)
    # columns considered "gap" if very few white pixels
    thr = max(1, int(0.06 * h))
    gaps = col <= thr
    # find runs of gaps
    cuts: List[int] = []
    run = 0
    for i, is_gap in enumerate(gaps.tolist()):
        if is_gap:
            run += 1
        else:
            if run >= 2:
                cuts.append(i - run // 2)
            run = 0
    if run >= 2:
        cuts.append(len(gaps) - run // 2)
    # keep only reasonable cuts (not too close to edges)
    cuts = [c for c in cuts if 2 < c < (w - 2)]
    if not cuts:
        return [box]

    # build segments
    xs = [0] + cuts + [w]
    out: List[Tuple[int, int, int, int]] = []
    for a, b in zip(xs[:-1], xs[1:]):
        ww = int(b - a)
        if ww < 4:
            continue
        # tighten vertically inside segment
        seg = roi[:, a:b]
        rows = np.where(np.sum(seg > 0, axis=1) > 0)[0]
        cols = np.where(np.sum(seg > 0, axis=0) > 0)[0]
        if len(rows) == 0 or len(cols) == 0:
            continue
        y0, y1 = int(rows.min()), int(rows.max())
        x0, x1 = int(cols.min()), int(cols.max())
        out.append((x + a + x0, y + y0, (x1 - x0 + 1), (y1 - y0 + 1)))
    return out if out else [box]


def _force_split_n(bw: np.ndarray, n: int = 4) -> List[Tuple[int, int, int, int]]:
    """
    Force split the mask into N digit boxes using minima of vertical projection
    near expected cut positions. This is useful when digits are readable but
    contours/CC segmentation is unstable.
    """
    H, W = bw.shape[:2]
    if W < 12 or n < 2:
        return _segment_projection_boxes(bw)

    col = np.sum(bw > 0, axis=0).astype(np.int32)

    cuts: List[int] = []
    for k in range(1, n):
        target = int(round(W * (k / n)))
        a = max(2, target - max(3, W // 20))
        b = min(W - 3, target + max(3, W // 20))
        if b <= a:
            continue
        # pick column with minimum ink in window
        j = int(a + int(np.argmin(col[a:b])))
        cuts.append(j)

    xs = [0] + sorted(set(cuts)) + [W]
    out: List[Tuple[int, int, int, int]] = []
    for a, b in zip(xs[:-1], xs[1:]):
        ww = int(b - a)
        if ww < 3:
            continue
        seg = bw[:, a:b]
        rows = np.where(np.sum(seg > 0, axis=1) > 0)[0]
        cols = np.where(np.sum(seg > 0, axis=0) > 0)[0]
        if len(rows) == 0 or len(cols) == 0:
            continue
        y0, y1 = int(rows.min()), int(rows.max())
        x0, x1 = int(cols.min()), int(cols.max())
        out.append((int(a + x0), int(y0), int(x1 - x0 + 1), int(y1 - y0 + 1)))

    out.sort(key=lambda b: b[0])
    return out


def _read_int_opencv_fallback(img_bgr: np.ndarray) -> Optional[int]:
    """
    Fallback OCR (digits-only) without Tesseract.
    Works by contour-segmenting digits and matching against simple templates.
    """
    try:
        bw = _prep_digits(img_bgr)  # white digits on black
        bw = _tighten_mask_to_text(bw)
        # Prefer projection segmentation when mask looks readable.
        boxes = _segment_projection_boxes(bw)
        if len(boxes) < 2:
            boxes = _segment_digit_boxes(bw)
        if not boxes:
            return None

        # If we got one big blob, try splitting it into digits
        if len(boxes) == 1:
            x, y, w, h = boxes[0]
            if w >= int(bw.shape[1] * 0.55):
                boxes = _split_wide_box(bw, boxes[0])

        digits: list[int] = []
        confs: list[float] = []
        for (x, y, w, h) in boxes[:10]:  # safety cap
            crop = bw[y : y + h, x : x + w]
            # normalize to 28x28
            pad = 2
            crop = cv2.copyMakeBorder(crop, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
            crop = cv2.resize(crop, (28, 28), interpolation=cv2.INTER_AREA)
            _, crop = cv2.threshold(crop, 40, 255, cv2.THRESH_BINARY)

            d, conf = _classify_digit_knn(crop)
            if conf < 0.03:
                continue
            digits.append(int(d))
            confs.append(float(conf))

        if not digits:
            # If segmentation failed but mask is wide, try forced 4-digit split
            if bw.shape[1] >= 36:
                boxes2 = _force_split_n(bw, n=4)
                ds2: list[int] = []
                for (x, y, w, h) in boxes2[:4]:
                    crop = bw[y : y + h, x : x + w]
                    crop = cv2.copyMakeBorder(crop, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0)
                    crop = cv2.resize(crop, (28, 28), interpolation=cv2.INTER_AREA)
                    _, crop = cv2.threshold(crop, 40, 255, cv2.THRESH_BINARY)
                    d, _conf = _classify_digit_knn(crop)
                    ds2.append(int(d))
                if len(ds2) == 4:
                    val2 = ds2[0] * 1000 + ds2[1] * 100 + ds2[2] * 10 + ds2[3]
                    if 0 <= val2 <= 500000:
                        return int(val2)
            return None
        if len(digits) > 6:
            return None
        # join digits
        val = 0
        for d in digits:
            val = val * 10 + d
        if val > 500000:  # sanity guard for potions count
            return None
        if len(boxes) >= 3 and val == 0:
            return None
        return int(val)
    except Exception:
        return None


def read_int_from_bgr(img_bgr: np.ndarray) -> Optional[int]:
    """
    Best-effort OCR for a small ROI that contains only digits.
    Requires pytesseract + installed Tesseract OCR engine in PATH.
    Returns int or None if cannot read.
    """
    try:
        import pytesseract  # type: ignore
    except Exception:
        return _read_int_opencv_fallback(img_bgr)

    try:
        bw = _prep_digits(img_bgr)
        # Tesseract expects black text on white background; invert back
        inv = cv2.bitwise_not(bw)
        txt = pytesseract.image_to_string(
            inv,
            config="--psm 7 -c tessedit_char_whitelist=0123456789",
        )
        txt = (txt or "").strip()
        parts = re.findall(r"\d+", txt)
        if not parts:
            return _read_int_opencv_fallback(img_bgr)
        joined = "".join(parts)
        if len(joined) > 6:
            return _read_int_opencv_fallback(img_bgr)
        val = int(joined)
        if val > 500000:
            return _read_int_opencv_fallback(img_bgr)
        return val
    except Exception:
        return _read_int_opencv_fallback(img_bgr)


def read_int_debug(img_bgr: np.ndarray) -> Tuple[Optional[int], Dict[str, Any], np.ndarray]:
    """
    Like read_int_from_bgr(), but also returns debug info + prepared bw mask.
    Debug dict keys are best-effort (may change).
    """
    dbg: Dict[str, Any] = {
        "method": None,
        "text": None,
        "joined": None,
        "val": None,
        "digits": None,
        "digit_count": None,
        "forced4": None,
    }
    bw = _prep_digits(img_bgr)
    bw = _tighten_mask_to_text(bw)

    # Try Tesseract if available
    try:
        import pytesseract  # type: ignore

        inv = cv2.bitwise_not(bw)
        txt = pytesseract.image_to_string(inv, config="--psm 7 -c tessedit_char_whitelist=0123456789")
        txt = (txt or "").strip()
        parts = re.findall(r"\d+", txt)
        joined = "".join(parts) if parts else ""
        dbg.update({"method": "tesseract", "text": txt, "joined": joined})
        if joined and len(joined) <= 6:
            val = int(joined)
            if val <= 500000:
                dbg["val"] = val
                return val, dbg, bw
    except Exception:
        pass

    # Fallback with extra debug: show how many components we see
    try:
        boxes = _segment_projection_boxes(bw)
        if len(boxes) < 2:
            boxes = _segment_digit_boxes(bw)
        dbg["components"] = int(len(boxes))
        dbg["digit_count"] = int(len(boxes))
        # also attempt to classify per-box for debugging
        ds: List[int] = []
        for (x, y, w, h) in boxes[:10]:
            crop = bw[y : y + h, x : x + w]
            crop = cv2.copyMakeBorder(crop, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0)
            crop = cv2.resize(crop, (28, 28), interpolation=cv2.INTER_AREA)
            _, crop = cv2.threshold(crop, 40, 255, cv2.THRESH_BINARY)
            d, conf = _classify_digit_knn(crop)
            ds.append(int(d))
        dbg["digits"] = ds
    except Exception:
        pass
    val = _read_int_opencv_fallback(img_bgr)
    if val is not None and bw.shape[1] >= 36 and (val < 1000 or val > 99999):
        # informational only; forced split attempt happens inside fallback when needed
        dbg["forced4"] = True
    dbg.update({"method": "opencv_fallback", "val": val})
    return val, dbg, bw

