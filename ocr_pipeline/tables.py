"""مدل جدول + OCR سلول‌به‌سلول + خروجی‌های CSV/HTML/JSON/XLSX (بخش ۹ تا ۱۱ و ۱۲)."""
from __future__ import annotations

import csv
import html
import io
import json as _json
import re
from pathlib import Path

import cv2
import numpy as np

from .engines import display_text, en_digits_only


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
    # A skewed/faded grid may start several pixels inside the crop, or occupy
    # only part of an edge row. It still must not determine the OCR ink height.
    # Use a lighter mask to find long rule fragments, only within the outer 5%.
    # The mask removes strokes, not entire edge bands or the original document.
    threshold = min(245, max(0, int(np.percentile(crop, 90)) - 10))
    rules = cv2.threshold(crop, threshold, 255, cv2.THRESH_BINARY_INV)[1]
    horizontal = cv2.morphologyEx(rules, cv2.MORPH_OPEN,
                                  np.ones((1, max(8, w // 4)), np.uint8))
    vertical = cv2.morphologyEx(rules, cv2.MORPH_OPEN,
                                np.ones((max(8, h // 4), 1), np.uint8))
    my, mx = max(3, h // 20), max(3, w // 20)
    horizontal[my:h - my] = 0
    vertical[:, mx:w - mx] = 0
    mask = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    out[mask > 0] = 255
    return out


def cell_confidence(words: list[dict]) -> float | None:
    confs = [w['confidence'] for w in words if w.get('confidence') is not None]
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
            "normalized_text": display_text(raw),
            "normalized_en_digits": en_digits_only(display_text(raw)),
            "confidence": cell_confs.get((r, c)),
            "engine": engine,
            "language_estimate": "fa" if any("\u0600" <= ch <= "\u06FF" for ch in raw) else "en",
            "digit_type": digit_type(display_text(raw)),
            "raw_digit_type": digit_type(raw),
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
    def spreadsheet_text(value):
        probe = value.lstrip()
        if probe.startswith(('=', '+', '-', '@')) and not re.fullmatch(r'[-+]?[\d,٬٫.]+%?', probe):
            return "'" + value
        return value
    csv.writer(buf).writerows([spreadsheet_text(value) for value in row] for row in rows_of(table))
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
    for row_index, row in enumerate(rows_of(table), 1):
        for column, value in enumerate(row, 1):
            cell = ws.cell(row_index, column, value)
            cell.data_type = 's'  # OCR content is text, never an executable formula.
    wb.save(path)


def xlsx_available() -> bool:
    try:
        from openpyxl import Workbook
        return callable(Workbook)
    except ImportError:
        return False


def write_table_outputs(table: dict, out_dir: Path, *, page_no: int | None = None,
                        xlsx_required: bool = True) -> list[str]:
    """Strict by default; OCR explicitly permits a declared degraded export.

    Only a dependency ImportError is degradable. Disk/format failures propagate
    so that atomic review publication cannot silently publish broken exports.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f'page_{page_no:03d}_' if page_no is not None else ''
    tid = f"{prefix}table_{table['table_id']:03d}"
    paths = []
    p = out_dir / f"{tid}.json"
    p.write_text(_json.dumps(table, ensure_ascii=False, indent=2), "utf-8")
    paths.append(p.name)
    for ext, content in (("csv", to_csv(table)), ("html", to_html(table))):
        p = out_dir / f"{tid}.{ext}"
        p.write_text(content, "utf-8")
        paths.append(p.name)
    xp = out_dir / f"{tid}.xlsx"
    try:
        to_xlsx(table, xp)
    except ImportError:
        if xlsx_required:
            raise
        table['export_status']={'xlsx':{'status':'unavailable','reason':'BLOCKED_DEPENDENCY'}}
    else:
        paths.append(xp.name)
        table['export_status']={'xlsx':{'status':'available','reason':None}}
    # The JSON describes actual availability, not an attempted filename.
    (out_dir / f'{tid}.json').write_text(_json.dumps(table,ensure_ascii=False,indent=2),'utf-8')
    return paths


def markdown_table(table: dict) -> str:
    rows = rows_of(table)
    if not rows:
        return ""
    esc = lambda s: s.replace("|", "\\|") or " "
    out = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
           "|" + "---|" * len(rows[0])]
    for row in rows[1:]:
        out.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(out)
