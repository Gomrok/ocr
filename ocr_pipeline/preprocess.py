# -*- coding: utf-8 -*-
"""پیش‌پردازش تحلیل‌محور — حفظ نقطه‌های حروف فارسی (بخش ۳).

هر فیلتر فقط در صورت لزوم (تشخیص از روی تصویر) اعمال می‌شود.
نقطه‌های پ/چ/ژ/گ/ث/ش/ض/ظ/ق/ف/ن/ی هرگز نباید حذف شوند:
  - حذف نویز فقط Connected-Component های خیلی کوچکِ دور از جوهر اصلی
  - شارپنینگ ملایم unsharp
  - باینری‌زاسیون تطبیقی گاوسی (حفظ استروک‌های نازک)
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

_PARALLEL_MEDIAN_PIXELS = 16_000_000


def analyze(img: np.ndarray) -> dict:
    """تحلیل صفحه برای انتخاب عملیات پیش‌پردازش مناسب."""
    small = cv2.resize(img, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    pct_black = float((small < 128).mean())
    mean, std = float(small.mean()), float(small.std())
    # کنتراست پایین: std کم
    low_contrast = std < 40
    # پس‌زمینهٔ غیرسفید (اسکن تیره)
    dark_bg = mean < 200
    # نویز: جزیره‌های ریز زیاد
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n_cc = int(cv2.connectedComponentsWithStats(bw)[0])
    noisy = n_cc > 2500
    # کجی
    angle = estimate_skew(img)
    return {
        "pct_black": round(pct_black, 4),
        "mean": round(mean, 1),
        "std": round(std, 1),
        "low_contrast": low_contrast,
        "dark_background": dark_bg,
        "noisy": noisy,
        "skew_angle": angle,
        "needs_deskew": abs(angle) >= 0.25,
    }


def estimate_skew(img: np.ndarray) -> float:
    """برآورد زاویهٔ کجی (درجه)."""
    scale = 1000.0 / max(img.shape[:2])
    small = cv2.resize(img, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA) if scale < 1 else img
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(bw > 0))
    if len(coords) < 200:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    if angle > 45:
        angle -= 90
    return 0.0 if abs(angle) > 7 else round(float(angle), 2)


def deskew(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def deskew_safe(img: np.ndarray, angle: float):
    """Keep the original canvas if rotating ink could clip a mark."""
    ink = img < 200
    ys = np.flatnonzero(ink.any(axis=1))
    xs = np.flatnonzero(ink.any(axis=0))
    if not len(xs) or not len(ys):
        return img, False
    h, w = img.shape[:2]
    matrix = cv2.getRotationMatrix2D((w/2, h/2), angle, 1)
    corners = np.float64([[xs[0],ys[0],1],[xs[-1],ys[0],1],
                          [xs[-1],ys[-1],1],[xs[0],ys[-1],1]]) @ matrix.T
    if (corners[:,0].min()<2 or corners[:,1].min()<2
            or corners[:,0].max()>w-3 or corners[:,1].max()>h-3):
        return img, False
    return deskew(img, angle), True


def illumination_correct(img: np.ndarray) -> np.ndarray:
    """حذف سایه/روشنایی ناهمگون با تقسیم بر پس‌زمینهٔ بلور شده."""
    workers = min(4, max(1, (os.cpu_count() or 1) // 2))
    height = img.shape[0]
    if img.size < _PARALLEL_MEDIAN_PIXELS or workers == 1 or height < 31 * workers:
        bg = cv2.medianBlur(img, 31)
    else:
        bg = np.empty_like(img)

        def blur_stripe(index):
            first, last = index * height // workers, (index + 1) * height // workers
            # The 15-pixel halo preserves every 31x31 neighbourhood, including
            # stripe boundaries. Workers write disjoint rows of the output.
            top, bottom = max(0, first - 15), min(height, last + 15)
            tile = cv2.medianBlur(img[top:bottom], 31)
            bg[first:last] = tile[first - top:last - top]

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr-background") as pool:
            list(pool.map(blur_stripe, range(workers)))
    norm = cv2.divide(img, bg, scale=255)
    return norm


def denoise_preserve_dots(img: np.ndarray, max_cc_px: int | None = None) -> np.ndarray:
    """Conservative original-pixel fallback until a DPI/x-height policy is validated.

    Explicit max_cc_px retains the legacy experimental variant for ablation;
    production callers use the default and never erase unclassified small marks.
    """
    if max_cc_px is None:
        return img.copy()
    bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    _, labels, stats, _ = cv2.connectedComponentsWithStats(bw)
    # One lookup per pixel instead of scanning the entire 2000-DPI page for
    # every speck. Label zero is background and must never be removed.
    remove = stats[:, cv2.CC_STAT_AREA] <= max_cc_px
    remove[0] = False
    out = img.copy()
    out[remove[labels]] = 255
    return out


def unsharp(img: np.ndarray, amount: float = 0.6) -> np.ndarray:
    blur = cv2.GaussianBlur(img, (0, 0), 2.0)
    return cv2.addWeighted(img, 1 + amount, blur, -amount, 0)


def normalize_contrast(img: np.ndarray) -> np.ndarray:
    """کشش خطی درصدی — روی خاکستری امن."""
    lo, hi = np.percentile(img, (1, 99))
    if hi <= lo:
        return img
    return np.clip((img.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)


def binarize_adaptive(img: np.ndarray) -> np.ndarray:
    """باینریزاسیون تطبیقی — استروک نازک و نقطه حفظ می‌شود."""
    return cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 41, 18)


def remove_quiet_borders(img: np.ndarray, margin: int = 40) -> np.ndarray:
    """حذف حاشیهٔ سفید خالی اطراف (بخش ۳: blank margin removal)."""
    bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    proj_y = (bw > 0).sum(axis=1)
    proj_x = (bw > 0).sum(axis=0)
    ys = np.where(proj_y > bw.shape[1] * 0.002)[0]
    xs = np.where(proj_x > bw.shape[0] * 0.002)[0]
    if len(ys) < 10 or len(xs) < 10:
        return img
    y0, y1 = max(0, ys[0] - margin), min(img.shape[0], ys[-1] + margin)
    x0, x1 = max(0, xs[0] - margin), min(img.shape[1], xs[-1] + margin)
    return img[y0:y1, x0:x1]


def pad_white(img: np.ndarray, pad: int = 30) -> np.ndarray:
    """حاشیهٔ سفید — Tesseract بدون حاشیه در نواحی بریده‌شده نتیجهٔ خالی می‌دهد."""
    return cv2.copyMakeBorder(img, pad, pad, pad, pad,
                              cv2.BORDER_CONSTANT, value=255)


def optimal_ocr_scale(crop: np.ndarray, target_h: float = 38.0) -> float:
    """مقیاس بهینهٔ OCR — کالیبره‌شده روی تست واقعی (Tesseract در ارتفاع حرف ~38px بهترین است).

    روش پایدار: ارتفاع میانهٔ CCها را در نمای پیش‌نمایش می‌سنجیم و ضریب را
    نسبت به هدف حساب می‌کنیم (نه نسبت به مستر — چون نمای پیش‌نمایش خودش مقیاس دارد).
    در 2000DPI حروف ~230px هستند → f≈0.16؛ در اسناد کوچک‌تر خودکار بزرگ‌تر می‌شود.
    مختصات‌ها بعداً با تقسیم بر f به مستر برمی‌گردند.
    """
    h, w = crop.shape[:2]
    if h < 10 or w < 10:
        return 1.0
    pv = min(1.0, 1500.0 / max(h, w))  # مقیاس نمای پیش‌نمایش
    small = cv2.resize(crop, None, fx=pv, fy=pv,
                       interpolation=cv2.INTER_AREA) if pv < 1 else crop
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw)
    heights = [stats[i, cv2.CC_STAT_HEIGHT] for i in range(1, n)
               if 10 < stats[i, cv2.CC_STAT_HEIGHT] < small.shape[0] * 0.5
               and stats[i, cv2.CC_STAT_AREA] > 30]
    if not heights:
        f = pv  # متن تشخیص داده نشد — همان مقیاس پیش‌نمایش امن
    else:
        med = float(np.median(heights))
        f = pv * target_h / med if med > 0 else pv
    f = max(0.04, min(f, 2.0))
    # سقف ابعاد ارسال به موتور (سلول/ناحیه نباید عظیم شود)
    if max(h, w) * f > 3500:
        f = 3500.0 / max(h, w)
    return round(f, 3)


def resize_by_factor(crop: np.ndarray, f: float) -> np.ndarray:
    if abs(f - 1.0) < 0.01:
        return crop
    interp = cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC
    return cv2.resize(crop, None, fx=f, fy=f, interpolation=interp)


def cell_line_bands(crop: np.ndarray) -> list[tuple[int, int]]:
    """Locate clearly separated cell lines without splitting detached dots.

    The scale follows substantial ink components, not a fixed DPI. Work on a
    bounded mask and return coordinates in the untouched source cell.
    """
    h, w = crop.shape[:2]
    if not crop.size:
        return [(0, h)]
    factor = min(1.0, 1600 / max(h, w))
    small = cv2.resize(crop, (max(1, round(w * factor)), max(1, round(h * factor))),
                       interpolation=cv2.INTER_AREA) if factor < 1 else crop
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    count, labels, stats, _ = cv2.connectedComponentsWithStats(bw)
    keep = np.zeros(count, dtype=bool)
    heights = []
    for i in range(1, count):
        _, _, cw, ch, area = map(int, stats[i])
        if area < 3 or cw > ch * 25 or ch > cw * 25:
            continue
        keep[i] = True
        heights.append(ch)
    if not heights:
        return [(0, h)]
    occupied = np.flatnonzero(keep[labels].sum(axis=1) >= max(2, round(small.shape[1] * .003)))
    if not len(occupied):
        return [(0, h)]
    gap = max(4, round(max(heights) * .4))
    cuts = [round((a + b) / (2 * factor)) for a, b in zip(occupied, occupied[1:]) if b - a > gap]
    if not 1 <= len(cuts) <= 5:
        return [(0, h)]
    edges = [0, *cuts, h]
    return list(zip(edges, edges[1:]))


def preprocess_master(img: np.ndarray, variants: bool = False) -> dict:
    """پیش‌پردازش تحلیل‌محور مستر — برمی‌گرداند dict نسخه‌ها.

    خروجی:
      base: تصویر پیش‌پردازش‌شدهٔ اصلی (خاکستری)
      analysis: تحلیل انجام‌شده
      ops: فهرست عملیات اعمال‌شده
      variants: نسخه‌های جایگزین برای multi-pass (بخش ۱۴)
    """
    analysis = analyze(img)
    ops: list[str] = []
    out = img.copy()

    if analysis["low_contrast"] or analysis["dark_background"]:
        out = illumination_correct(out)
        ops.append("illumination_correction")
    if analysis["low_contrast"]:
        out = normalize_contrast(out)
        ops.append("contrast_normalization")
    if analysis["needs_deskew"]:
        out = deskew(out, analysis["skew_angle"])
        ops.append(f"deskew({analysis['skew_angle']})")
    if analysis["noisy"]:
        out = denoise_preserve_dots(out)
        ops.append("denoise_preserve_dots")
    # شارپ ملایم همیشه (کمک به نقطه‌ها) — unsharp
    out = unsharp(out)
    ops.append("unsharp")

    res = {"base": out, "analysis": analysis, "ops": ops}
    return res
