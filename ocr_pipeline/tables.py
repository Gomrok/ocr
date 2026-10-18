# -*- coding: utf-8 -*-
"""مدل جدول + OCR سلول‌به‌سلول + خروجی‌های CSV/HTML/JSON/XLSX (بخش ۹ تا ۱۱ و ۱۲)."""
from __future__ import annotations

import csv
import html
import io
import json as _json
from pathlib import Path

import cv2
import numpy as np

from .engines import normalize_text


def cell_bboxes(grid: tuple) -> list[tuple[int, int, tuple]]:
    """تولید مستطیل سلول‌ها از شبکهٔ (ys, xs). خروجی: (row, col, bbox)."""
    ys, xs = grid
    cells = []
    for r in range(len(ys) - 1):
        for c in range(len(xs) - 1):
            cells.append((r, c, (xs[c] + 2, ys[r] + 2, xs[c + 1] - 2, ys[r + 1] - 2)))
    return cells


def pad_cell_bbox(bbox: tuple, img_shape: tuple, pad_ratio: float = 0.06) -> tuple:
    """حاشیهٔ کوچک برای نجات حروف لب‌به‌لب با خط (حفظ نقطه/استروک)."""
    x0, y0, x1, y1 = bbox
    pw = int((x1 - x0) * pad_ratio)
    ph = int((y1 - y0) * pad_ratio)
    H, W = img_shape[:2]
    return (max(0, x0 - pw), max(0, y0 - ph), min(W, x1 + pw), min(H, y1 + ph))


def strip_grid_lines(crop: np.ndarray) -> np.ndarray:
    """حذف دقیق خطوط جدول از لبه‌های سلول بر اساس ضخامت واقعی خط (بخش ۱۰).

    فقط سطر/ستون‌هایی که پوشش جوهر > ۴۰٪ دارند (خط افقی/عمودی واقعی) پاک
    می‌شوند — حروف و نقطه‌های فارسی هرگز پوشش تمام‌عرض ندارند و مصون می‌مانند.
    """
    if crop.size == 0:
        return crop
    bw = cv2.threshold(crop, 0, 255,
                       cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = bw.shape[:2]
    out = crop.copy()
    cov_rows = (bw > 0).mean(axis=1)
    cov_cols = (bw > 0).mean(axis=0)
    limit = h // 3
    k = 0
    while k < limit and cov_rows[k] > 0.4:
        k += 1
    if k:
        out[:min(h, k + 2), :] = 255
    k = 0
    while k < limit and cov_rows[h - 1 - k] > 0.4:
        k += 1
    if k:
        out[max(0, h - k - 2):, :] = 255
    limit = w // 3
    k = 0
    while k < limit and cov_cols[k] > 0.4:
        k += 1
    if k:
        out[:, :min(w, k + 2)] = 255
    k = 0
    while k < limit and cov_cols[w - 1 - k] > 0.4:
        k += 1
    if k:
        out[:, max(0, w - k - 2):] = 255
    return out


def cell_confidence(words: list[dict]) -> float | None:
    confs = [w.get("confidence") for w in words if w.get("confidence")]
    return round(sum(confs) / len(confs), 3) if confs else None


def digit_type(text: str) -> str:
    if any(ch in text for ch in "۰۱۲۳۴۵۶۷۸۹"):
        return "fa"
    if any(ch.isdigit() for ch in text):
        return "en"
    return "none"


def build_table_model(table_id: int, grid: tuple, cell_texts: dict,
                      cell_confs: dict, engine: str,
                      needs_review: dict | None = None) -> dict:
    """مدل منطقی جدول با سلول‌های کامل (بخش ۹)."""
    ys, xs = grid
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    needs_review = needs_review or {}
    cells = []
    for (r, c, bbox) in cell_bboxes(grid):
        raw = cell_texts.get((r, c), "")
        review = bool(needs_review.get((r, c))) or raw == ""
        cells.append({
            "row": r,
            "column": c,
            "row_span": 1,
            "column_span": 1,
            "bbox": list(bbox),
            "raw_text": raw,
            "normalized_text": normalize_text(raw),
            "confidence": cell_confs.get((r, c)),
            "engine": engine,
            "language_estimate": "fa" if any("\u0600" <= ch <= "\u06FF" for ch in raw) else "en",
            "digit_type": digit_type(raw),
            "needs_review": review,
        })
    return {
        "table_id": table_id,
        "rows": n_rows,
        "columns": n_cols,
        "cells": cells,
    }


# ─────────────────── خروجی‌ها (بخش ۱۱) ───────────────────

def rows_of(table: dict) -> list[list[str]]:
    out = [["" for _ in range(table["columns"])] for _ in range(table["rows"])]
    for cell in table["cells"]:
        out[cell["row"]][cell["column"]] = cell["normalized_text"]
    return out


def to_csv(table: dict) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows_of(table))
    return buf.getvalue()


def to_html(table: dict) -> str:
    rows = rows_of(table)
    h = ["<table border='1' style='border-collapse:collapse'>",
         "<thead><tr>" + "".join(f"<th>{html.escape(v)}</th>" for v in rows[0])
         + "</tr></thead>"] if rows else []
    for row in rows[1:]:
        h.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in row) + "</tr>")
    if rows:
        h.append("</table>")
    return "\n".join(h)


def to_xlsx(table: dict, path: Path) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = f"table_{table['table_id']}"
    for row in rows_of(table):
        ws.append(row)
    wb.save(path)


def write_table_outputs(table: dict, out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tid = f"table_{table['table_id']:03d}"
    paths = []
    p = out_dir / f"{tid}.json"
    p.write_text(_json.dumps(table, ensure_ascii=False, indent=2), "utf-8")
    paths.append(p.name)
    for ext, content in (("csv", to_csv(table)), ("html", to_html(table))):
        p = out_dir / f"{tid}.{ext}"
        p.write_text(content, "utf-8")
        paths.append(p.name)
    try:
        xp = out_dir / f"{tid}.xlsx"
        to_xlsx(table, xp)
        paths.append(xp.name)
    except Exception:  # noqa: BLE001
        pass
    return paths


def markdown_table(table: dict) -> str:
    rows = rows_of(table)
    if not rows:
        return ""
    esc = lambda s: s.replace("|", "\\|") or " "  # noqa: E731
    out = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
           "|" + "---|" * len(rows[0])]
    for row in rows[1:]:
        out.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(out)
