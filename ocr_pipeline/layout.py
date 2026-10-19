# -*- coding: utf-8 -*-
"""تحلیل چیدمان: ناحیه‌ها، جدول‌ها، تایل هم‌پوشان — نگاشت به مستر (بخش ۱۳ و ۹)."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Region:
    """ناحیهٔ OCR روی مستر."""

    kind: str           # text | table | graphic
    bbox: tuple         # (x0, y0, x1, y1) در مختصات مستر
    grid: tuple | None = None       # (ys, xs) مرزهای سلول برای جدول
    img: np.ndarray | None = None   # برش آماده‌شده


def _bands(binary: np.ndarray, axis: int, min_ratio: float, gap: int = 8) -> list[int]:
    """مرکز گروه‌های خط موجود در پروجکشن (axis=1: افقی)."""
    proj = (binary > 0).sum(axis=axis)
    thr = max(int(binary.shape[1 if axis == 1 else 0] * min_ratio), 1)
    idx = np.where(proj >= thr)[0]
    if len(idx) == 0:
        return []
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= gap:
            cur.append(i)
        else:
            groups.append(int(np.mean(cur)))
            cur = [i]
    groups.append(int(np.mean(cur)))
    return groups


def _without_scan_edges(binary: np.ndarray) -> np.ndarray:
    """Ignore long scanner-edge strokes in layout analysis, not source pixels.

    A dark page frame can otherwise connect unrelated paragraphs or become a
    table boundary. Only long, straight strokes in the outer 2% are eligible;
    text and internal rules remain available to recognition.
    """
    h, w = binary.shape
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                  np.ones((1, max(3, w // 3)), np.uint8))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                np.ones((max(3, h // 3), 1), np.uint8))
    my, mx = max(2, h // 50), max(2, w // 50)
    horizontal[my:h - my] = 0
    vertical[:, mx:w - mx] = 0
    edge = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    return cv2.bitwise_and(binary, cv2.bitwise_not(edge))


def detect_tables(img: np.ndarray) -> list[tuple]:
    """تشخیص جدول‌های خط‌دار: خروجی فهرست ((ys),(xs)) به مختصات تصویر ورودی."""
    h, w = img.shape[:2]
    bw = _without_scan_edges(cv2.threshold(
        img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1])
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 22, 1), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 22, 1)))
    horiz = cv2.dilate(cv2.erode(bw, hk), hk)
    vert = cv2.dilate(cv2.erode(bw, vk), vk)
    # Intersections are isolated tiny dots, not connected table extents. The
    # old global fallback combined a letterhead, a page frame and a real table
    # into one grid, then erased the intervening prose from text recognition.
    connected = cv2.morphologyEx(horiz | vert, cv2.MORPH_CLOSE,
                                 np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(connected)
    grids = []
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if ww * hh < w * h * 0.002 or ww < 40 or hh < 40:
            continue
        # مرزهای سلول فقط در محدودهٔ همین جدول
        sub_h = horiz[y:y + hh, x:x + ww]
        sub_v = vert[y:y + hh, x:x + ww]
        sys_ = _bands(sub_h, 1, 0.50, gap=max(2, h // 1000))
        sxs = _bands(sub_v, 0, 0.40, gap=max(2, w // 1000))
        if len(sys_) >= 3 and len(sxs) >= 3:
            grids.append((tuple(int(y + v) for v in sys_), tuple(int(x + u) for u in sxs)))
    return sorted(grids, key=lambda grid: (grid[0][0], -grid[1][-1]))


def detect_text_regions(img: np.ndarray, max_mp_region: float = 18.0) -> list[tuple]:
    """تشخیص نواحی متنی بزرگ به‌صورت مستطیل‌های اتصال جوهر (Morphological)."""
    h, w = img.shape[:2]
    small_scale = min(1.0, 1800.0 / max(h, w))
    small = cv2.resize(img, None, fx=small_scale, fy=small_scale,
                       interpolation=cv2.INTER_AREA) if small_scale < 1 else img
    bw = _without_scan_edges(cv2.threshold(
        small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1])
    # A decorative enclosing frame is not a paragraph. Remove only its outer
    # strokes from the analysis mask; the untouched source supplies OCR pixels.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(bw)
    sh, sw = bw.shape
    for index in range(1, count):
        x, y, ww, hh, area = map(int, stats[index])
        if ww < sw * .2 or hh < sh * .2 or area > ww * hh * .15:
            continue
        component = (labels[y:y + hh, x:x + ww] == index).astype(np.uint8)
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours or max(map(cv2.contourArea, contours)) < ww * hh * .7:
            continue
        rim = max(3, round(min(ww, hh) * .05))
        component[rim:hh - rim, rim:ww - rim] = 0
        bw[y:y + hh, x:x + ww][component > 0] = 0
    # اتصال کلمات همسایه به پاراگراف
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 7))
    dil = cv2.dilate(bw, k, iterations=2)
    n, _, stats, _ = cv2.connectedComponentsWithStats(dil)
    regions = []
    inv = 1.0 / small_scale
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < 400:
            continue
        rx0, ry0 = int(x * inv), int(y * inv)
        rx1, ry1 = int((x + ww) * inv), int((y + hh) * inv)
        mp = (rx1 - rx0) * (ry1 - ry0) / 1e6
        if mp > max_mp_region:  # ناحیهٔ عظیم → شکسته می‌شود به تایل
            regions.extend(split_into_tiles((rx0, ry0, rx1, ry1), max_mp_region))
        else:
            regions.append((rx0, ry0, rx1, ry1))
    return regions


def split_into_tiles(bbox: tuple, max_mp: float, overlap_pct: float = 0.10) -> list[tuple]:
    """تایلینگ مستطیل با هم‌پوشانی ۱۰٪ (بخش ۱۳)."""
    x0, y0, x1, y1 = bbox
    tw = th = int((max_mp * 1e6) ** 0.5)
    tw = min(tw, x1 - x0)
    th = min(th, y1 - y0)
    step_x = max(1, int(tw * (1 - overlap_pct)))
    step_y = max(1, int(th * (1 - overlap_pct)))
    tiles = []
    y = y0
    while y < y1:
        x = x0
        while x < x1:
            tx1, ty1 = min(x + tw, x1), min(y + th, y1)
            if tx1 - x > 50 and ty1 - y > 50:
                tiles.append((x, y, tx1, ty1))
            if tx1 >= x1:
                break
            x += step_x
        if ty1 >= y1:
            break
        y += step_y
    return tiles or [bbox]


def analyze_layout(img: np.ndarray) -> list[Region]:
    """تحلیل کامل چیدمان: جدول‌ها + نواحی متنی (خارج جدول‌ها)."""
    regions: list[Region] = []
    table_bboxes: list[tuple] = []
    for ys, xs in detect_tables(img):
        bbox = (min(xs), min(ys), max(xs), max(ys))
        table_bboxes.append(bbox)
        regions.append(Region(kind="table", bbox=bbox, grid=(ys, xs)))

    # جوهر جدول‌ها با حاشیه پاک می‌شود — خطوط مرزیِ باقی‌مانده نباید
    # نواحی متنیِ کاذب بسازند (باگ واقعی: فریمِ باقی‌مانده = ناحیهٔ متنی هم‌پوشان).
    img_for_text = img
    if table_bboxes:
        img_for_text = img.copy()
        H, W = img.shape[:2]
        pad = 8
        for (tx0, ty0, tx1, ty1) in table_bboxes:
            img_for_text[max(0, ty0 - pad):min(H, ty1 + pad),
                         max(0, tx0 - pad):min(W, tx1 + pad)] = 255

    # نواحی متنی خارج جدول‌ها (هم‌پوشانی > ۵۰٪ با جدول دور ریخته می‌شود)
    for (rx0, ry0, rx1, ry1) in detect_text_regions(img_for_text):
        area = max(1, (rx1 - rx0) * (ry1 - ry0))
        overlapped = False
        for (tx0, ty0, tx1, ty1) in table_bboxes:
            ox = max(0, min(rx1, tx1) - max(rx0, tx0))
            oy = max(0, min(ry1, ty1) - max(ry0, ty0))
            if ox * oy > 0.5 * area:
                overlapped = True
                break
        if not overlapped:
            regions.append(Region(kind="text", bbox=(rx0, ry0, rx1, ry1)))
    return regions
