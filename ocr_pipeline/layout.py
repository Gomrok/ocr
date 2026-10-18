# -*- coding: utf-8 -*-
"""تحلیل چیدمان: ناحیه‌ها، جدول‌ها، تایل هم‌پوشان — نگاشت به مستر (بخش ۱۳ و ۹)."""
from __future__ import annotations

from dataclasses import dataclass, field

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


def detect_tables(img: np.ndarray) -> list[tuple]:
    """تشخیص جدول‌های خط‌دار: خروجی فهرست ((ys),(xs)) به مختصات تصویر ورودی."""
    h, w = img.shape[:2]
    bw = cv2.threshold(~img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 22, 1), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 22, 1)))
    horiz = cv2.dilate(cv2.erode(bw, hk), hk)
    vert = cv2.dilate(cv2.erode(bw, vk), vk)
    ys = _bands(horiz, 1, 0.30, gap=10)
    xs = _bands(vert, 0, 0.12, gap=10)
    if len(ys) < 3 or len(xs) < 3:
        return []
    inter = cv2.bitwise_and(horiz, vert)
    n, _, stats, _ = cv2.connectedComponentsWithStats(inter)
    grids = []
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < (w * h) * 0.002:  # جدول‌های خیلی ریز بی‌اهمیت
            continue
        # مرزهای سلول فقط در محدودهٔ همین جدول
        sub_h = horiz[y:y + hh, x:x + ww]
        sub_v = vert[y:y + hh, x:x + ww]
        sys_ = _bands(sub_h, 1, 0.25, gap=8)
        sxs = _bands(sub_v, 0, 0.08, gap=8)
        if len(sys_) >= 3 and len(sxs) >= 3:
            grids.append((tuple(y + v for v in sys_), tuple(x + u for u in sxs)))
    if not grids:  # کل تصویر یک جدول
        grids.append((tuple(ys), tuple(xs)))
    return grids


def detect_text_regions(img: np.ndarray, max_mp_region: float = 18.0) -> list[tuple]:
    """تشخیص نواحی متنی بزرگ به‌صورت مستطیل‌های اتصال جوهر (Morphological)."""
    h, w = img.shape[:2]
    small_scale = min(1.0, 1800.0 / max(h, w))
    small = cv2.resize(img, None, fx=small_scale, fy=small_scale,
                       interpolation=cv2.INTER_AREA) if small_scale < 1 else img
    bw = cv2.threshold(~small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
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
