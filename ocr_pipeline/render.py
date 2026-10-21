# -*- coding: utf-8 -*-
"""رندر مستر PDF — 2000 DPI به‌صورت پیش‌فرض، ایمن از نظر حافظه (بخش ۱ و ۲).

تصویر مستر هرگز پایین‌میاید (downscale نمی‌شود). برای صفحات بزرگ:
  - رندر مستقیم با pymupdf
  - ذخیرهٔ lossless روی دیسک (TIFF/PNG) و دسترسی ناحیه‌ای با cv2
"""
from __future__ import annotations

import os
import math
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import psutil
import pymupdf

PROC = psutil.Process()
MAX_RENDER_MP = 400.0  # سقف ایمنی سخت‌افزاری (مگاپیکسل) — A4@2000 = 387MP
TIFF_EXT = ".tiff"
RENDER_TILE_EDGE = 3000


@dataclass
class MasterPage:
    """نمای دسترسی به تصویر مستر یک صفحه."""

    path: Path            # فایل lossless مستر
    width: int
    height: int
    requested_dpi: int
    effective_dpi: int
    render_seconds: float
    source_rect: tuple    # (x0, y0, x1, y1) در PDF
    pdf_w_pt: float
    pdf_h_pt: float
    approx_mem_mb: float  # حافظهٔ تصویر کامل
    render_method: str = "single"

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1e6

    def read_full(self) -> np.ndarray:
        return cv2.imread(str(self.path), cv2.IMREAD_UNCHANGED)

    def read_region(self, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
        """خواندن ناحیه از مستر بدون بارگذاری کل تصویر."""
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.width, x1), min(self.height, y1)
        if x1 <= x0 or y1 <= y0:
            return np.zeros((1, 1), np.uint8)
        with open(self.path, "rb") as fh:
            data = fh.read()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        return img[y0:y1, x0:x1]


def estimate_render(page: "pymupdf.Page", dpi: int) -> tuple[int, int, float]:
    """برآورد ابعاد و حافظهٔ رندر (بخش ۲)."""
    w_in, h_in = page.rect.width / 72.0, page.rect.height / 72.0
    w_px, h_px = int(w_in * dpi), int(h_in * dpi)
    # 2 بایت/پیکسل (خاکستری 8بیتی + سربار)
    mem_mb = w_px * h_px * 2 / (1024 * 1024)
    return w_px, h_px, mem_mb


def render_master(page: "pymupdf.Page", page_no: int, tmp_dir: Path,
                  dpi: int = 2000, max_mp: float = MAX_RENDER_MP) -> MasterPage:
    """رندر مستر 2000DPI — بدون downscale خودکار (بخش ۱).

    در صفحات عجیب‌بزرگ (>max_mp) برای جلوگیری از کرش سخت‌افزار، DPI مؤثر
    تنها تا مرز امن کاهش می‌یابد و در متادیتا ثبت می‌شود (effective_dpi).
    """
    t0 = time.perf_counter()
    if dpi <= 0 or page.rect.is_empty or page.rect.is_infinite:
        raise ValueError("Invalid page render dimensions")
    req_dpi = dpi
    w_px, h_px, mem_mb = estimate_render(page, dpi)
    eff = dpi
    if (w_px * h_px) / 1e6 > max_mp:
        eff = int((max_mp * 1e6 / (w_px * h_px)) ** 0.5 * dpi)
        if eff < 1:
            raise ValueError("Page dimensions exceed render budget")
        w_px, h_px, mem_mb = estimate_render(page, eff)

    matrix = pymupdf.Matrix(eff / 72, eff / 72)
    bounds = (page.rect * matrix).irect
    pix = None
    method = "single"
    if bounds.width * bounds.height <= RENDER_TILE_EDGE ** 2:
        pix = page.get_pixmap(dpi=eff, colorspace=pymupdf.csGRAY, alpha=False)
        arr = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(pix.height, pix.width)
    else:
        # MuPDF can return an all-white pixmap after a non-raising render-surface
        # limit error. Bound its draw surface, without lowering DPI.
        method = "tiled"
        arr = np.full((bounds.height, bounds.width), 255, np.uint8)
        for y in range(bounds.y0, bounds.y1, RENDER_TILE_EDGE):
            for x in range(bounds.x0, bounds.x1, RENDER_TILE_EDGE):
                tile = pymupdf.IRect(x, y, min(x+RENDER_TILE_EDGE, bounds.x1),
                                    min(y+RENDER_TILE_EDGE, bounds.y1))
                # Render a two-pixel halo and copy just the interior, on the exact
                # full-page pixel grid. This retains strokes crossing tile edges.
                expanded = pymupdf.Rect(tile) + (-2, -2, 2, 2)
                clip = (expanded / matrix) & page.rect
                piece = page.get_pixmap(dpi=eff, colorspace=pymupdf.csGRAY, alpha=False, clip=clip)
                pixels = np.frombuffer(piece.samples_mv, dtype=np.uint8).reshape(piece.height, piece.width)
                sx, sy = tile.x0-piece.x, tile.y0-piece.y
                if sx < 0 or sy < 0 or sx+tile.width > piece.width or sy+tile.height > piece.height:
                    raise RuntimeError("Incomplete PDF render tile")
                arr[tile.y0-bounds.y0:tile.y1-bounds.y0, tile.x0-bounds.x0:tile.x1-bounds.x0] = pixels[sy:sy+tile.height, sx:sx+tile.width]
                del pixels, piece
    if int(arr.min()) == 255:
        # An empty high-resolution render is not proof that the PDF is blank.
        # This probe only validates pixels; embedded PDF text never replaces OCR.
        probe_scale = min(eff/72, 150/72, math.sqrt(2_000_000/(page.rect.width*page.rect.height)))
        probe = page.get_pixmap(matrix=pymupdf.Matrix(probe_scale, probe_scale),
                                colorspace=pymupdf.csGRAY, alpha=False)
        if np.frombuffer(probe.samples_mv, dtype=np.uint8).min() < 255:
            raise RuntimeError("PDF render lost visible page content")
    out = tmp_dir / f"master_p{page_no:04d}{TIFF_EXT}"
    if not cv2.imwrite(str(out), arr, [cv2.IMWRITE_TIFF_COMPRESSION, 8]):  # Deflate, lossless
        raise OSError("Could not write the lossless master image")
    ms = MasterPage(
        path=out, width=arr.shape[1], height=arr.shape[0],
        requested_dpi=req_dpi, effective_dpi=eff,
        render_seconds=round(time.perf_counter() - t0, 2),
        source_rect=tuple(page.rect),
        pdf_w_pt=page.rect.width, pdf_h_pt=page.rect.height,
        approx_mem_mb=round(mem_mb, 1), render_method=method,
    )
    arr = None
    pix = None
    return ms
