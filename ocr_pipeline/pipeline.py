# -*- coding: utf-8 -*-
"""ارکستراتور پایپ‌لاین Production — مستر 2000DPI (بخش ۱ تا ۳ و ۹ تا ۱۳ و ۲۰ تا ۲۵ و ۲۷).

جریان هر صفحه:
  رندر مستر 2000DPI (lossless TIFF روی دیسک)
  → پیش‌پردازش روی تصویر کوچک‌شدهٔ تحلیل (نقطه‌ها حفظ)
  → اعمال همان عملیات روی مستر (یک‌بار)
  → تحلیل چیدمان در مقیاس کوچک + نگاشت به مستر (جدول/متن/گرافیک)
  → OCR ناحیه‌ای (جدول سلول‌به‌سلول از مستر) + fallback چندموتوره
  → ساختار بلوکی/سطری + شناسه‌ها + اعتبارسنجی + متریکس + دیباگ
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from . import engines as E
from . import layout as L
from . import numeric_recheck as N
from . import preprocess as P
from . import render as R
from . import tables as T
from .metrics import Metrics
from .quality import CONF_REVIEW, page_quality, quality_notice
from .storage import atomic_json, atomic_text, file_sha256

WORK_DPI = 500      # تحلیل تا ۵۰۰ DPI؛ مسترهای کم‌وضوح دوباره کوچک نمی‌شوند.


def validate_text(raw: str) -> dict:
    """اعتبارسنجی فارسی‌آگاه (بخش ۲۰) — بدون تصحیح خودکار."""
    issues = []
    if "ي" in raw or "ك" in raw:
        issues.append("arabic_variants_present")
    if re.search(r"\.\.", raw.replace("...", "")):
        issues.append("suspicious_punctuation")
    if re.search(r"[،,]\s*$", raw, re.M):
        issues.append("trailing_separator")
    if re.search(r"[\u0600-\u06FF]\s[\u0600-\u06FF]\s[\u0600-\u06FF]\s[\u0600-\u06FF]"
                 r"\s[\u0600-\u06FF]\s[\u0600-\u06FF]", raw):
        issues.append("possible_isolated_letters")
    return {"issues": issues or None}


def ocr_page(master: R.MasterPage, engine_list: list[E.OCREngine], m: Metrics,
             debug_dir: Path | None = None, page_no: int = 1, *, source_page: pymupdf.Page | None = None) -> dict:
    """OCR یک صفحه از مستر — معماری ناحیه‌محور، بدون نگه‌داشتن چند نسخهٔ کامل."""
    with m.step("master_read"):
        img = master.read_full()          # مستر 2000DPI (حافظهٔ خط ۲)
    if img is None:
        raise RuntimeError(f"master image unreadable: {master.path}")
    H, W = img.shape[:2]
    uniform_white = int(img.min()) == 255
    ws = min(1.0, WORK_DPI / master.effective_dpi)
    reference_scale = 2000.0 / master.effective_dpi
    raw_img = img  # مستر خام — برای گذرهای مخصوص (مثل گلیف عددی در صفحات تمیز)
    with m.step("working_image"):
        proc_small = cv2.resize(img, None, fx=ws, fy=ws, interpolation=cv2.INTER_AREA)

    # ── پیش‌پردازش: تحلیل روی کوچک، اعمال روی مستر (بخش ۳) ──
    with m.step("preprocess"):
        analysis = P.analyze(proc_small)
        ops: list[str] = []
        proc = img  # در صورت عدم عملیات، همان مستر (بدون کپی)
        # محافظ صفحهٔ تمیز: سند دیجیتالِ سفید با فیلترهای سنگین آسیب می‌بیند
        clean_page = (not analysis["dark_background"]) and analysis["mean"] > 225
        if not clean_page and (analysis["low_contrast"] or analysis["dark_background"]):
            proc = P.illumination_correct(proc)
            ops.append("illumination_correction")
        if not clean_page and analysis["low_contrast"]:
            proc = P.normalize_contrast(proc)
            ops.append("contrast_normalization")
        if analysis["needs_deskew"]:
            proc = P.deskew(proc, analysis["skew_angle"])
            ops.append(f"deskew({analysis['skew_angle']})")
        if not clean_page and analysis["noisy"]:
            proc = P.denoise_preserve_dots(proc)
            ops.append("denoise_preserve_dots")
        # بدون شارپ صفحه‌ای — در 2000DPI روی سلول‌ها اثر مخرب دارد (تست تجربی t9/t20)
        proc_small = cv2.resize(proc, None, fx=ws, fy=ws,
                                interpolation=cv2.INTER_AREA)

    # ── چیدمان در مقیاس کوچک + نگاشت به مستر (بخش ۹ و ۱۳) ──
    with m.step("layout"):
        regions = L.analyze_layout(proc_small)
        for r in regions:
            r.bbox = (int(r.bbox[0] / ws), int(r.bbox[1] / ws),
                      int(r.bbox[2] / ws), int(r.bbox[3] / ws))
            if r.grid:  # شبکهٔ جدول: مرزها به مختصات مستر
                r.grid = (tuple(int(v / ws) for v in r.grid[0]),
                          tuple(int(v / ws) for v in r.grid[1]))
        if not regions and not uniform_white:
            # A missed layout must not skip recognition of a nonblank page.
            regions = [L.Region(kind="text", bbox=(0, 0, W, H))]

    words_all: list[dict] = []
    text_parts: list[str] = []
    tables_out: list[dict] = []
    region_reads: dict[int, dict] = {}
    table_files: list[str] = []
    low_conf = 0
    page_engines: set[str] = set()

    def _crop_from_master(bbox: tuple) -> np.ndarray:
        """برش ناحیه از مستر پیش‌پردازش‌شده (بخش ۱۰)."""
        x0, y0, x1, y1 = bbox
        return proc[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]

    for region_index, region in enumerate(regions):
        if region.kind == "table":
            with m.step("table_ocr"):
                tmodel = _ocr_table(region, proc, engine_list, m,
                                    len(tables_out) + 1, (H, W),
                                    raw_img if clean_page else None,
                                    dpi=master.effective_dpi)
            if tmodel:
                tables_out.append(tmodel)
                table_files.extend(T.write_table_outputs(
                    tmodel, master.path.parent.parent / "tables", page_no=page_no))
            continue  # متن جدول از سلول‌ها بازسازی می‌شود (بخش ۱۰)

        with m.step("ocr"):
            crop = _crop_from_master(region.bbox)
            if crop.size == 0:
                continue
            # گذر اول در مقیاس بهینه + رأی‌گیری چندمقیاسی: Tesseract fas روی
            # برش‌های عریض در مقیاس محاسبه‌شده گاهی خالی/خطا می‌دهد؛ متن درست بین
            # مقیاس‌های مختلف تکرار می‌شود (اکثریت غیرخالی برنده — مثل سلول‌ها).
            # پنجرهٔ طلایی ارقام ~33–37px (0.13–0.16 در مستر ۲۰۰۰DPI — تأیید تجربی)
            f0 = P.optimal_ocr_scale(crop)
            outline = L.outline_mask(crop)
            region_angle = L.outline_angle(outline) if outline is not None else 0.0
            ladder = [f0, *(min(2.0, fac * reference_scale) for fac in (0.16, 0.13, 0.10))]
            candidates = []
            for fac in dict.fromkeys(round(x, 3) for x in ladder if x >= 0.04):
                image, to_unrotated = _region_view(crop, fac, outline, region_angle)
                ok, buf = cv2.imencode(".png", image)
                if not ok:
                    continue
                candidates.append((fac, buf.tobytes(), to_unrotated))
            m.inc("ocr_calls", len(candidates))
            results = []
            kind = "framed_text" if region_angle else "text"
            readings = E.ocr_many(engine_list, [png for _, png, _ in candidates], region_kind=kind)
            for (fac, _, transform), r in zip(candidates, readings):
                if r and r.get("words"):
                    r["_scale"] = fac
                    if transform is not None:
                        for word in r["words"]:
                            word["bbox"] = _enclose(word["bbox"], transform)
                    results.append(r)
            if not results:
                continue
            res = E.select_consensus(results)
            _record_region_readings(res, results)
            region_reads[region_index] = {
                "outline_mask": outline is not None,
                "local_rotation": region_angle,
                "recognition_disagreement": bool(res.get("needs_review")),
                "readings": [{"text": r["text"], "engine": r.get("engine"), "psm": r.get("psm"),
                              "scale": r["_scale"], "confidence": r.get("confidence")} for r in results],
            }
            # HOCR identifiers restart on every subprocess. Namespace them by
            # region and recognition pass before merging words across crops.
            for word in res.get('words') or []:
                if word.get('line_id'):
                    word['line_id'] = f'{region_index}:primary:{word["line_id"]}'
            f = res["_scale"]
            region_raw_text = res.get('text', '')
            page_engines.add(res.get("engine", "?"))
            if res.get("multipass"):
                m.inc("fallbacks")
            m.inc("fallbacks", int(res.get("extra_passes", 0)))
            # ── گذر ترمیم eng (بخش ۷): مدل fas ارقام/لاتین را «لالالا»/صفر خراب می‌خواند ──
            # یک گذر کامل eng روی کل ناحیه؛ ادغام واژه‌محور فقط توکن‌های آشغال را
            # با هم‌پوشان eng تعویض می‌کند — حروف فارسی سالم هرگز دست نمی‌خورند
            # (بخش ۲۹ — استخراجی، بدون تصحیح حدسی)
            if res.get("words") and isinstance(engine_list[-1], E.TesseractEngine) \
                    and (E.needs_latin_repair(res["words"])
                         or E.region_has_latin_context(res["words"])
                         or E.result_confidence(res) < 0.75):
                # مقیاس گذر eng: هدف ~35px گلیف (ارقام لاتین بزرگ‌تر از فارسی‌اند)
                f_eng = max(0.10, min(0.35 * reference_scale, P.optimal_ocr_scale(crop,
                                                                target_h=35.0)))
                english, transform = _region_view(crop, f_eng, outline, region_angle)
                ok2, buf2 = cv2.imencode(".png", english)
                if ok2:
                    m.inc("ocr_calls")
                    m.inc("fallbacks")
                    try:
                        r_eng = engine_list[-1].ocr_image(buf2.tobytes(),
                                                          psm=3, langs="eng")
                        k = f / f_eng  # کادر eng → قاب مختصات گذر fas
                        ew = []
                        for x in r_eng.get("words") or []:
                            if x.get('line_id'):
                                x['line_id'] = f'{region_index}:english:{x["line_id"]}'
                            bx = _enclose(x["bbox"], transform) if transform is not None else x["bbox"]
                            x["bbox"] = tuple(int((v - 30) * k) + 30 for v in bx)
                            ew.append(x)
                        if E.prefer_latin_region(res, r_eng):
                            mw, nch = ew, len(ew)
                        else:
                            mw, nch = E.merge_latin_secondary(res["words"], ew)
                        if nch:
                            res["words"] = mw
                            res["text"] = "\n".join(E.words_to_lines(mw))
                            res["engine"] = str(res.get("engine", "tesseract")) \
                                + "+eng_repair"
                            page_engines.add("tesseract+eng_repair")
                    except Exception:
                        pass
            # نگاشت مختصات به مستر (بخش ۱۲): پدِ ۳۰ پیکسلیِ pad_white (در مقیاس
            # ارسالی) کم می‌شود — وگرنه همهٔ کادرها آفست می‌خورند و ترمیم/دیباگ
            # روی جای اشتباه برش می‌زند
            ws_inv = 1.0 / f
            pad_s = 30  # P.pad_white پیش‌فرض
            rx0, ry0, rx1, ry1 = region.bbox
            for w in res.get("words") or []:
                bx = w["bbox"]
                w["bbox"] = (max(rx0, int((bx[0] - pad_s) * ws_inv) + rx0),
                             max(ry0, int((bx[1] - pad_s) * ws_inv) + ry0),
                             min(rx1, int((bx[2] - pad_s) * ws_inv) + rx0),
                             min(ry1, int((bx[3] - pad_s) * ws_inv) + ry0))
                words_all.append(w)
            if region_raw_text.strip():
                text_parts.append(region_raw_text.strip())

    # ── ساختار سطری متن صفحه از کلمات (بخش ۵ و ۲۱) ──
    words_all = E.merge_word_lists([words_all])
    with m.step('recognition_repair'):
        _repair_suspicious_words(words_all, proc, engine_list, m, dpi=master.effective_dpi)
    with m.step("numeric_verification"):
        _recover_header_numbers(words_all, proc, engine_list, m, dpi=master.effective_dpi)
        _verify_text_numbers(words_all, proc, engine_list, m, dpi=master.effective_dpi)
    for w in words_all:
        conf = w.get("confidence")
        if conf is not None and conf < CONF_REVIEW:
            w["needs_review"] = True
        if w.get("needs_review"):
            low_conf += 1
    model_dir = os.environ.get(N.MODEL_DIR)
    if model_dir and source_page is not None:
        with m.step("secondary_numeric_ocr"):
            N.recheck_numbers(model_dir, words_all, source_page, (W, H),
                              lambda box: _source_bbox(box, W, H, analysis), m)
    lines = [E.display_text(line) for line in E.words_to_lines(words_all)]
    raw_text = "\n".join(text_parts)

    m.low_conf_regions += low_conf
    confs = [w["confidence"] for w in words_all if w.get("confidence") is not None]
    page_conf = round(sum(confs) / len(confs), 4) if confs else None

    # متن نهایی صفحه: سطرهای بازسازی‌شده (حفظ ترتیب بصری RTL)
    final_text = "\n".join(lines) if lines else raw_text
    normalized = E.display_text(final_text)
    has_text = bool(normalized.strip()) or any(
        cell.get("raw_text", "").strip() for table in tables_out for cell in table["cells"])
    if not has_text and not uniform_white:
        raise RuntimeError("OCR returned no text from a nonblank page")

    # متن جدول‌ها هم در متن صفحه (Markdown) — بخش ۲۳
    tables_out.sort(key=lambda t: min(c["bbox"][1] for c in t["cells"])
                    if t["cells"] else 0)

    page_res = {
        "page": page_no,
        "width": master.width,
        "height": master.height,
        "requested_dpi": master.requested_dpi,
        "effective_dpi": master.effective_dpi,
        "master_render": True,
        "render_method": getattr(master, "render_method", "single"),
        "status": "blank" if uniform_white else "extracted",
        "has_text": has_text,
        "source_pdf_pt": {"w": round(master.pdf_w_pt, 1), "h": round(master.pdf_h_pt, 1)},
        "image_mb": master.approx_mem_mb,
        "render_seconds": master.render_seconds,
        "analysis": analysis,
        "preprocess_ops": ops,
        "regions": [{"kind": r.kind, "bbox": list(r.bbox), **region_reads.get(index, {})}
                    for index, r in enumerate(regions)],
        "blocks": [{"kind": r.kind, "bbox": list(r.bbox)} for r in regions],
        "lines": lines,
        "words": [{"text": E.display_text(w["text"]), "bbox": list(w["bbox"]),
                   "confidence": w.get("confidence"),
                   "raw_ocr_text": w.get("raw_ocr_text", w["text"]),
                   "source_bbox": _source_bbox(w["bbox"], W, H, analysis),
                   **{key: w[key] for key in ("review_reason", "recognition_readings",
                                              "numeric_status", "numeric_reading", "secondary_numeric_reading")
                      if key in w},
                   **({'line_id': w['line_id']} if w.get('line_id') else {}),
                   **({"numeric_verified": True} if w.get("numeric_verified") else {}),
                   **({"recognition_repaired": True} if w.get("recognition_repaired") else {}),
                   **({"needs_review": True} if w.get("needs_review") else {})}
                  for w in words_all],
        "raw_text": raw_text,
        "normalized_text": normalized,
        "normalized_en_digits": E.en_digits_only(normalized),
        "text": normalized,
        "digit_style": "persian",
        "tables": [{"table_id": t["table_id"], "rows": t["rows"],
                    "columns": t["columns"],
                    "cells": [{**cell, "source_bbox": _source_bbox(cell["bbox"], W, H, analysis)}
                              for cell in t["cells"]],
                    "markdown": T.markdown_table(t)} for t in tables_out],
        "confidence": page_conf,
        "engines": sorted(page_engines) if page_engines else [engine_list[0].name],
        "identifiers": [E.display_text(value) for value in E.find_identifiers(E.en_digits_only(normalized))],
        "validation": validate_text(raw_text),
        "low_confidence_words": low_conf + sum(
            bool(cell.get('needs_review')) for table in tables_out for cell in table['cells']),
    }

    if debug_dir and tables_out:
        with m.step("debug"):
            _debug_table_image(master, regions, tables_out, debug_dir, page_no, source=img)

    page_res["metrics"] = m.report()
    page_res["_table_files"] = table_files
    img = proc = proc_small = None
    return page_res


def _source_bbox(box, width, height, analysis):
    if not analysis["needs_deskew"]:
        return list(box)
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), -analysis["skew_angle"], 1.0)
    a, b, c, d = _enclose(box, matrix)
    return [max(0, a), max(0, b), min(width, c), min(height, d)]


def _enclose(box, matrix):
    x0, y0, x1, y1 = box
    corners = np.array([[x0,y0], [x0,y1], [x1,y0], [x1,y1]], dtype=float)
    points = corners @ matrix[:, :2].T + matrix[:, 2]
    return (float(points[:, 0].min()), float(points[:, 1].min()),
            float(points[:, 0].max()), float(points[:, 1].max()))


def _region_view(crop, scale, outline, angle):
    scaled = P.resize_by_factor(crop, scale)
    if outline is not None:
        mask = cv2.resize(outline, (scaled.shape[1], scaled.shape[0]), interpolation=cv2.INTER_NEAREST)
        scaled = scaled.copy()
        scaled[mask > 0] = 255
    if not angle:
        return P.pad_white(scaled), None
    extra = int(abs(angle) * max(scaled.shape) * .01) + 1
    padded = P.pad_white(scaled, 30 + extra)
    height, width = padded.shape
    inverse = cv2.getRotationMatrix2D((width / 2, height / 2), -angle, 1.0)
    inverse[:, 2] -= extra
    return P.deskew(padded, angle), inverse


def _record_region_readings(selected, results):
    """Carry localized disagreements through renumbering and later OCR repairs."""
    if not selected.get("needs_review"):
        return

    def box(word, scale):
        return tuple((value - 30) / scale for value in word["bbox"])

    best_scale = selected["_scale"]
    alternatives = [(reading, [(word, box(word, reading["_scale"])) for word in reading["words"]])
                    for reading in results if reading["words"] is not selected["words"]]
    for word in selected["words"]:
        word_box = box(word, best_scale)
        for reading, positions in alternatives:
            other, overlap = None, .35
            for alternate, alternate_box in positions:
                score = E._iou(word_box, alternate_box)
                if score > overlap:
                    other, overlap = alternate, score
            if other and _norm_digits(other["text"]) != _norm_digits(word["text"]):
                word.setdefault("recognition_readings", []).append({
                    "text": other["text"], "confidence": other.get("confidence"),
                    "engine": reading.get("engine"), "scale": reading["_scale"], "psm": reading.get("psm"),
                })
                word.update(needs_review=True, review_reason="recognition_disagreement")


def _ink_tight_crop(crop: np.ndarray, margin: int = 14) -> np.ndarray:
    """برش تنگ به جوهر — سلول‌های عریض خالی تحلیل چیدمان Tesseract را می‌شکنند.

    محتوای سلول به مستطیل جوهر + حاشیه کوچک بریده می‌شود؛ مشکل سلول‌های
    عظق‌مطرحِ با یک کاراکتر کوچک در گوشه به‌کل حل می‌شود.
    """
    bw = cv2.threshold(crop, 0, 255,
                       cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    ys, xs = np.where(bw > 0)
    if len(ys) < 15:  # عملاً خالی
        return crop
    h, w = crop.shape[:2]
    y0 = max(0, int(ys.min()) - margin)
    y1 = min(h, int(ys.max()) + margin)
    x0 = max(0, int(xs.min()) - margin)
    x1 = min(w, int(xs.max()) + margin)
    tight = crop[y0:y1, x0:x1]
    return tight if tight.size else crop


def _cell_ocr_best(engine_list: list[E.OCREngine], crop: np.ndarray,
                   m: Metrics, kind: str = "table_cell", dpi: int = 2000,
                   split_lines: bool = True) -> dict:
    """OCR چندمقیاسی سلول + رأی‌گیری سازگاری (بخش ۱۴ — انتخاب با consistency).

    متن درست بین مقیاس‌های مختلف تکرار می‌شود؛ آشغالِ مقیاس‌محور تصادفی است.
    اکثریتِ متن‌های غیرخالی برنده است؛ در نبود اکثریت، بالاترین اطمینان.
    """
    crop = _ink_tight_crop(crop)
    if split_lines:
        bands = P.cell_line_bands(crop)
        if len(bands) > 1:
            lines = [_cell_ocr_best(engine_list, crop[y0:y1], m, kind, dpi,
                                   split_lines=False) for y0, y1 in bands]
            if all(r.get('text', '').strip() and E.result_confidence(r) >= .55 for r in lines):
                return {'text': '\n'.join(r['text'].strip() for r in lines),
                        'words': [w for r in lines for w in r.get('words', [])],
                        'confidence': sum(E.result_confidence(r) for r in lines) / len(lines),
                        'engine': ','.join(sorted({r.get('engine', 'tesseract') for r in lines})),
                        'raw_ocr_text': '\n'.join(r.get('raw_ocr_text', r['text']).strip() for r in lines),
                        'needs_review': any(r.get('needs_review') or E.result_confidence(r) < CONF_REVIEW
                                            for r in lines)}
    f = P.optimal_ocr_scale(crop)
    # نامزدهای مقیاس — سلول‌های بزرگ مستر در ~0.05–0.2 و سلول‌های کوچک بالاتر
    if max(crop.shape[:2]) > 1200:
        # 0.22 از رأی‌گیری مقیاس‌های متن سلول جلو می‌زند (تأیید تجربی روی bench_table)
        cands = [round(min(2.0, fac * 2000.0 / dpi), 3) for fac in (0.22, 0.10, 0.17)]
    else:
        cands = [f, round(f * 0.6, 3), round(f * 0.36, 3)]
    images = []
    for fac in dict.fromkeys(cands):
        s = P.pad_white(P.resize_by_factor(crop, fac))
        ok, buf = cv2.imencode(".png", s)
        if not ok:
            continue
        images.append(buf.tobytes())
    m.inc("ocr_calls", len(images))
    results = [r for r in E.ocr_many(engine_list, images, region_kind=kind) if r is not None]
    if not results:
        return {"text": "", "words": [], "confidence": None, "engine": "none"}

    best = E.select_consensus(results)
    if E.result_confidence(best) < .80 and len(P.cell_line_bands(crop)) == 1:
        ink_height = max(1, _ink_tight_crop(crop, margin=0).shape[0])
        raw_images = []
        for target in (128, 160):
            fac = min(2.0, target / ink_height, 3500 / max(crop.shape))
            if any(round(size * fac) < 1 for size in crop.shape):
                continue
            ok, buf = cv2.imencode('.png', P.pad_white(P.resize_by_factor(crop, fac)))
            if ok and buf.tobytes() not in raw_images:
                raw_images.append(buf.tobytes())
        if len(raw_images) == 2:
            m.inc('ocr_calls', 2)
            reads = E.ocr_many(engine_list, raw_images, region_kind='raw_line')
            if all(r and E.result_confidence(r) >= .55 for r in reads):
                raw = E.select_consensus(reads)
                if (raw.get('votes') == 2
                        and E.result_confidence(raw) >= E.result_confidence(best) + .10):
                    raw['raw_ocr_text'] = best.get('text', '')
                    raw['needs_review'] = True
                    return raw
    return best


def _confirmed_crop_read(engine_list: list[E.OCREngine], crop: np.ndarray,
                         m: Metrics, *, langs: str | None = None) -> dict | None:
    """Two independent raster scales must agree before a targeted repair."""
    crop = _ink_tight_crop(crop)
    scale = min(2.0, 48.0 / max(1, _ink_tight_crop(crop, margin=0).shape[0]),
                3500.0 / max(crop.shape))
    images = []
    for factor in (scale, round(scale * 0.72, 3)):
        if any(round(size * factor) < 1 for size in crop.shape):
            return None
        ok, encoded = cv2.imencode('.png', P.pad_white(P.resize_by_factor(crop, factor)))
        if ok:
            images.append(encoded.tobytes())
    if len(images) != 2 or images[0] == images[1]:
        return None
    m.inc('ocr_calls', 2)
    readings = E.ocr_many(engine_list, images, region_kind='line', langs_override=langs)
    if any(not r or E.result_confidence(r) < 0.85 for r in readings):
        return None
    texts = [' '.join(E.normalize_text(r.get('text', ''), normalize_digits=True).split())
             for r in readings]
    if not texts[0] or texts[0] != texts[1]:
        return None
    return max(readings, key=E.result_confidence)


def _repair_suspicious_words(words: list[dict], source: np.ndarray,
                             engines: list[E.OCREngine], metrics: Metrics, dpi: int = 2000) -> None:
    """Re-read rejected symbols from pixels. Never erase or guess an asterisk."""
    remaining = 16
    height, width = source.shape[:2]
    for word in words:
        original = word.get('text', '')
        if '*' not in original and '\ufffd' not in original:
            continue
        word['needs_review'] = True
        # A confident literal star or masking sequence is source content.
        if (word.get('confidence') or 0) >= 0.85 or '**' in original or not remaining:
            continue
        remaining -= 1
        x0, y0, x1, y1 = word['bbox']
        pad = max(4, round((y1 - y0) * 0.15))
        crop = source[max(0, y0-pad):min(height, y1+pad), max(0, x0-pad):min(width, x1+pad)]
        if not crop.size:
            continue
        langs = 'eng' if re.search(r'[A-Za-z]', original) and not re.search(r'[آ-ی]', original) else None
        repair = _confirmed_crop_read(engines, crop, metrics, langs=langs)
        pattern = ''.join('.' if c in '*\ufffd' else re.escape(c)
                          for c in E.en_digits_only(original))
        if (not repair or not re.fullmatch(pattern, E.en_digits_only(repair.get('text', '').strip()))) \
                and re.fullmatch(r'[۰-۹0-9*\ufffd]{2,16}', original):
            glyph = _glyph_digit_ocr(engines, crop, metrics, dpi=dpi)
            if not glyph.get('needs_review') and re.fullmatch(r'[۰-۹0-9]{2,16}', glyph.get('text', '')):
                repair = glyph
        if not repair:
            continue
        candidate = ' '.join(repair.get('text', '').split())
        # Both readings must fill the same rejected positions and retain every
        # recognized character, including punctuation and numeric separators.
        if re.search(r'[*\ufffd]', candidate) or not re.fullmatch(pattern, E.en_digits_only(candidate)):
            continue
        word['raw_ocr_text'] = word.get('raw_ocr_text', original)
        word['text'] = candidate
        word['confidence'] = repair.get('confidence')
        word['recognition_repaired'] = True

FA_DIGIT_CHARS = "۰۱۲۳۴۵۶۷۸۹"
EN_DIGIT_CHARS = "0123456789"
AR_DIGIT_CHARS = "٠١٢٣٤٥٦٧٨٩"
DIGIT_WHITELIST = FA_DIGIT_CHARS + AR_DIGIT_CHARS + EN_DIGIT_CHARS


def _norm_digits(s: str) -> str:
    """هم‌سطح‌سازی هر دو سیستم رقم برای مقایسهٔ توافق — سیستم رقمِ متن اصلی حفظ می‌شود."""
    return s.translate(str.maketrans(FA_DIGIT_CHARS + "٠١٢٣٤٥٦٧٨٩",
                                     EN_DIGIT_CHARS * 2))


def _script_digit_read(engine_list: list[E.OCREngine], glyph: np.ndarray,
                       metrics: Metrics) -> str | None:
    """Read alternate Arabic-script numeral shapes, never language-correct them."""
    local = next((engine for engine in engine_list if isinstance(engine, E.TesseractEngine)
                  and (engine.tessdata_dir / 'Arabic.traineddata').is_file()), None)
    if local is None:
        return None
    crop = _ink_tight_crop(glyph, margin=0)
    images = []
    for target in (60, 100):
        factor = min(6.0, target / max(1, crop.shape[0]), 3500 / max(crop.shape))
        if any(round(size * factor) < 1 for size in crop.shape):
            return None
        ok, encoded = cv2.imencode('.png', P.pad_white(P.resize_by_factor(crop, factor)))
        if ok:
            images.append(encoded.tobytes())
    if len(images) != 2 or images[0] == images[1]:
        return None
    metrics.inc('ocr_calls', 2)
    readings = E.ocr_many([local], images, region_kind='raw_line',
                          whitelist=DIGIT_WHITELIST, langs_override='Arabic')
    def agreed_digit(results):
        eligible = [reading for reading in results if reading
                    and E.result_confidence(reading) >= .50
                    and re.fullmatch('[0-9]', _norm_digits(reading.get('text', '').strip()))]
        digits = [_norm_digits(reading['text'].strip()) for reading in eligible]
        if (len(digits) >= 2 and len(set(digits)) == 1
                and max(map(E.result_confidence, eligible)) >= .70):
            return digits[0]
        return None

    digit = agreed_digit(readings)
    if digit is not None:
        return digit
    images = []
    for target in (44, 80):
        factor = min(6.0, target / max(1, crop.shape[0]), 3500 / max(crop.shape))
        if any(round(size * factor) < 1 for size in crop.shape):
            return None
        ok, encoded = cv2.imencode('.png', P.pad_white(P.resize_by_factor(crop, factor)))
        if ok:
            images.append(encoded.tobytes())
    if len(images) == 2 and images[0] != images[1]:
        metrics.inc('ocr_calls', 2)
        readings += E.ocr_many([local], images, region_kind='line',
                               whitelist=DIGIT_WHITELIST, langs_override='Arabic')
        return agreed_digit(readings)
    return None


def _is_slash(ink: np.ndarray) -> bool:
    """A narrow, straight rising stroke; unlike the descending Persian one."""
    height, width = ink.shape
    if height < 6 or not .15 <= width / height <= .85:
        return False
    ys, xs = np.where(ink > 0)
    if len(ys) < 8 or np.ptp(ys) < 5:
        return False
    slope, offset = np.polyfit(ys, xs, 1)
    residual = np.sqrt(np.mean((xs - (slope * ys + offset)) ** 2))
    return -.85 <= slope <= -.15 and residual < .065 * height


def _glyph_digit_ocr(engine_list: list[E.OCREngine], crop: np.ndarray,
                     m: Metrics, dpi: int = 2000, expected_text: str | None = None,
                     detect_separators: bool = False) -> dict:
    """گذر عددی گلیف‌به‌گلیف (بخش ۲۸ — دقت عدد حیاتی است).

    قواعد مبتنی بر شکل (استخراجی، نه حدسی — بخش ۲۹):
    - ارقام داخل عدد LTR می‌شینند → مرتب‌سازی صعودی x
    - نقطهٔ ارقام (۲/۳/...) به گلیف پایه‌اش وصل می‌شود (کامل‌شدن حرف)
    - صفر فارسی دایرهٔ کوچکِ هم‌ترازِ خط پایه است؛ OCR ناتوان → قانون شکل
    """
    crop = _ink_tight_crop(crop)
    H, W = crop.shape[:2]
    bw = cv2.threshold(crop, 0, 255,
                       cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw)
    ccs = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < max(2, round(60 * min(1.0, dpi / 2000.0) ** 2)):
            continue
        # فقط قابِ کاملِ نازک (بقایای شبکه) حذف می‌شود — گلیفِ پرکنندهٔ برشِ
        # تنگ (h≈H) خودِ رقم است و نباید فیلتر شود
        if h > 0.95 * H and w > 0.95 * W and area < 0.5 * H * W:
            continue
        ccs.append([x, y, w, h])
    if not 1 <= len(ccs) <= 20:  # تک‌رقمی (سلول «۴») هم باید خوانده شود
        return {"text": ""}
    # ارتفاع مرجع فقط از گلیف‌های بزرگ (خود ارقام) — دایره‌ها/نقطه‌ها میانه را خراب می‌کنند
    hmax = max(c[3] for c in ccs)
    if detect_separators:
        # Field recovery accepts slash-separated identifiers only. Tiny edge
        # specks are not digits; decimal/thousand punctuation uses the old path.
        ccs = [c for c in ccs if c[3] >= .15 * hmax]
    big_h = sorted(c[3] for c in ccs if c[3] >= 0.35 * hmax)
    med_h = big_h[len(big_h) // 2]
    # نقطه‌ها/نشانه‌های ریز به گلیف پایهٔ هم‌ستون وصل می‌شوند
    bases = [c for c in ccs if c[3] >= 0.5 * med_h]
    smalls = [c for c in ccs if c[3] < 0.5 * med_h]
    for s in smalls:
        sx0, sx1 = s[0], s[0] + s[2]
        best_j, best_d = None, None
        for j, b in enumerate(bases):
            ox = max(0, min(sx1, b[0] + b[2]) - max(sx0, b[0]))
            if ox <= 0:
                continue
            d = min(abs(s[1] - b[1]), abs((s[1] + s[3]) - (b[1] + b[3])))
            if best_d is None or d < best_d:
                best_j, best_d = j, d
        if best_j is not None:
            b = bases[best_j]
            bases[best_j] = [min(b[0], s[0]), min(b[1], s[1]),
                             max(b[0] + b[2], sx1) - min(b[0], s[0]),
                             max(b[1] + b[3], s[1] + s[3]) - min(b[1], s[1])]
        else:
            bases.append(s)  # دایرهٔ صفر و امثالش — گلیف مستقل
    bases.sort(key=lambda c: c[0])  # ارقام LTR می‌شینند
    # Separator positions come only from an existing recognition. A component
    # count mismatch is ambiguous: do not turn commas, dates or decimals into zeros.
    if expected_text is not None and len(bases) != len(expected_text):
        return {"text": "", "needs_review": True}
    baseline = float(np.median([b[1] + b[3] for b in bases if b[3] >= 0.65 * med_h]))
    out = []
    confirmed_positions = []
    independent_positions = []
    evidence = []
    uncertain = False
    glyphs = []
    images = []
    for glyph_index, (x, y, w, h) in enumerate(bases):
        if expected_text is not None and not expected_text[glyph_index].isdigit():
            char = expected_text[glyph_index]
            glyphs.append((char, [char != '/' or _is_slash(bw[y:y+h, x:x+w])]))
            continue
        if detect_separators and h >= .65 * med_h and _is_slash(bw[y:y+h, x:x+w]):
            glyphs.append(('/', [True]))
            continue
        if (expected_text is None and h < 0.65 * med_h and w < h
                and y + h > baseline + 0.15 * med_h):
            # A comma descends below the digit baseline; Persian zero does not.
            # Keep the visible separator instead of feeding it to a digit-only pass.
            glyphs.append((',', [True]))
            continue
        # قانون شکل صفر فارسی: دایرهٔ کوچکِ در نیمهٔ پایینیِ نوار ارقام
        if (h < 0.60 * hmax and 0.65 * h <= w <= 1.8 * h
                and y + h >= baseline - (0.55 if detect_separators else 0.25) * hmax
                and y + h <= baseline + 0.15 * hmax):
            glyphs.append((None, []))
            continue
        x0m, y0m = max(0, x - h // 3), max(0, y - h // 3)
        x1m, y1m = min(W, x + w + h // 3), min(H, y + h + h // 3)
        # Preserve padding geometry without allowing a neighbouring digit into
        # this single-glyph recognition pass.
        g = np.full((y1m - y0m, x1m - x0m), 255, dtype=crop.dtype)
        g[y - y0m:y - y0m + h, x - x0m:x - x0m + w] = crop[y:y + h, x:x + w]
        # هدف 80px — کالیبره‌شده روی تست confusion ارقام (۳/۴ در ارتفاع کم مخلوط می‌شوند)
        # We already know this glyph's ink height. The paragraph estimator
        # excludes tall components and can leave a single 2000-DPI digit huge.
        f = min(5.0, 80.0 / h, 3500.0 / max(g.shape))
        indices = []
        for fac in dict.fromkeys([f, round(f * 0.55, 3)]):
            if any(round(size * fac) < 1 for size in g.shape):
                continue
            g2 = P.pad_white(P.resize_by_factor(g, fac))
            ok, buf = cv2.imencode(".png", g2)
            if not ok:
                continue
            indices.append(len(images))
            images.append(buf.tobytes())
        glyphs.append((g, indices))
    m.inc("ocr_calls", len(images))
    kind = "glyph_verify" if expected_text is not None else "glyph"
    readings = E.ocr_many(engine_list, images, region_kind=kind,
                          whitelist=DIGIT_WHITELIST)
    for g, indices in glyphs:
        position = len(out)
        if isinstance(g, str):
            if indices[0]:
                confirmed_positions.append(position)
            else:
                uncertain = True
            evidence.append({"position": position, "selected": g, "provenance": "separator_shape_or_original"})
            out.append(g)
            continue
        if g is None:
            # The isolated mark could be a decimal point or scan noise. A
            # shape-only zero is usable if it preserves an observed zero.
            same_zero = expected_text is None or _norm_digits(expected_text[position]) == '0'
            if same_zero:
                confirmed_positions.append(position)
            else:
                uncertain = True
            evidence.append({"position": position, "selected": "۰", "provenance": "round_mark_at_baseline"})
            out.append("۰" if same_zero else "?")
            continue
        reads = []
        attempted = []
        for index in indices:
            res = readings[index] or {}
            ch = (res.get("text") or "").strip().replace("\n", " ")
            confidence = E.result_confidence(res)
            attempted.append({"text": ch, "confidence": confidence, "model": "primary"})
            if len(ch) == 1 and ch in DIGIT_WHITELIST and confidence >= .50:
                reads.append(ch)
        agreed = len(reads) >= 2 and len({_norm_digits(ch) for ch in reads}) == 1
        if not agreed:
            # Only disputed glyphs pay for a tie-breaker. Do not pick the first
            # reading simply because it was scheduled first.
            factor = min(5.0, 60.0 / max(1, _ink_tight_crop(g, margin=0).shape[0]),
                         3500.0 / max(g.shape))
            ok, encoded = (False, None)
            if all(round(size * factor) >= 1 for size in g.shape):
                ok, encoded = cv2.imencode('.png', P.pad_white(P.resize_by_factor(g, factor)))
            if ok and all(encoded.tobytes() != images[index] for index in indices):
                m.inc('ocr_calls')
                extra = E.ocr_many(engine_list, [encoded.tobytes()], region_kind=kind,
                                   whitelist=DIGIT_WHITELIST)[0] or {}
                ch = extra.get('text', '').strip()
                confidence = E.result_confidence(extra)
                attempted.append({"text": ch, "confidence": confidence, "model": "primary"})
                if len(ch) == 1 and ch in DIGIT_WHITELIST and confidence >= .50:
                    reads.append(ch)
            counts = {digit: sum(_norm_digits(ch) == digit for ch in reads)
                      for digit in map(_norm_digits, reads)}
            winner = max(counts, key=counts.get) if counts else None
            agreed = winner is not None and counts[winner] >= 2
            if agreed:
                reads = [next(ch for ch in reads if _norm_digits(ch) == winner)]
        expected = _norm_digits(expected_text[position]) if expected_text is not None else None
        primary = _norm_digits(reads[0]) if reads else ""
        confusable = primary in {"2", "3", "4", "5", "6", "9"} or expected in {"3", "4", "6"}
        differs = expected is not None and expected != primary
        require_other = (expected is not None or detect_separators) and (confusable or differs)
        if not agreed or require_other:
            alternate = _script_digit_read(engine_list, g, m)
            attempted.append({"text": alternate, "model": "Arabic"})
            if alternate is not None:
                # A model disagreement may repair 4/6 when the alternate has
                # two clean readings. Other substitutions need agreement with
                # the primary glyph or the text already seen on the page.
                permitted = not primary or alternate in {primary, expected} or {primary, alternate} <= {"3", "4", "6", "9"}
                if permitted:
                    reads = [alternate.translate(str.maketrans(EN_DIGIT_CHARS, FA_DIGIT_CHARS))]
                    agreed = True
                    independent_positions.append(position)
                else:
                    agreed = False
            elif require_other:
                agreed = False
        if agreed:
            confirmed_positions.append(len(out))
        else:
            uncertain = True
        if not reads:
            out.append("?")
            uncertain = True
        else:
            out.append(reads[0])
        evidence.append({"position": position, "selected": out[-1], "readings": attempted})
    text = "".join(out).replace("\u200c", "").strip()
    if not text:
        return {"text": ""}
    return {"text": text, "engine": "glyph_pass", "confidence": None,
            "needs_review": uncertain, "confirmed_positions": confirmed_positions,
            "independent_positions": independent_positions, "glyph_evidence": evidence}


_NUMBER = N.NUMBER
_NUMERIC_BOUNDARY = "()[]،؛\u061c\u200e\u200f"


def _verify_text_numbers(words: list[dict], source: np.ndarray,
                         engines: list[E.OCREngine], metrics: Metrics, dpi: int = 2000) -> None:
    """Review location-linked numeric readings without overwriting unsupported values."""
    height, width = source.shape[:2]
    candidates = []
    for word in words:
        original = word.get("text", "")
        alternatives = [item["text"] for item in word.get("recognition_readings", []) if item.get("text")]
        # Tesseract may wrap a standalone RTL number in directional marks.
        # Keep them in the reading and output, but do not count them as glyphs.
        pattern_read = next((text for text in [original, *alternatives]
                             if _NUMBER.fullmatch(text.strip(_NUMERIC_BOUNDARY))), None)
        if pattern_read is None or not word.get("bbox") or word.get("numeric_verified"):
            if _NUMBER.search(original):
                word.setdefault("numeric_status", "observed")
            continue
        candidates.append((word, pattern_read))
    candidates.sort(key=lambda pair: (
        not any(sep in pair[1] for sep in "/:.٫"), len(pair[1]) != 4,
        not pair[0].get("recognition_readings"), pair[0].get("confidence") or 0,
    ))
    # A budget in glyphs scales with page density, rather than discarding
    # every field after the first sixteen. Deferred fields retain their bbox.
    remaining = min(640, max(48, len(candidates) * 5 + len(words) // 3))
    for word, reading in candidates:
        original = word["text"]
        token = _NUMBER.search(reading)
        expected = token.group()
        digit_count = sum(ch.isdecimal() for ch in expected)
        x0, y0, x1, y1 = word["bbox"]
        word.setdefault("raw_ocr_text", original)
        word["numeric_status"] = "unresolved"
        if len(expected) > 24 or digit_count > remaining:
            word.update(needs_review=True, numeric_status="deferred", review_reason="numeric_budget_deferred")
            continue
        if y1 <= y0 or x1 <= x0:
            word.update(needs_review=True, review_reason="numeric_unresolved")
            continue
        remaining -= digit_count
        pad = max(1, int((y1 - y0) * .035))
        crop = source[max(0, y0-pad):min(height, y1+pad), max(0, x0-pad):min(width, x1+pad)]
        if not crop.size:
            word.update(needs_review=True, review_reason="numeric_unresolved")
            continue
        try:
            result = _glyph_digit_ocr(
                engines, crop, metrics, dpi=dpi, expected_text=expected,
            )
        except (RuntimeError, OSError, ValueError):
            word.update(needs_review=True, review_reason="numeric_unresolved")
            continue
        candidate = _norm_digits(result.get("text", ""))
        word["numeric_reading"] = {"text": result.get("text", ""), "engine": "glyph_pass",
                                   "glyphs": result.get("glyph_evidence", [])}
        if (len(candidate) != len(expected)
                or [(i, c) for i, c in enumerate(candidate) if not c.isdecimal() and c != "?"]
                != [(i, c) for i, c in enumerate(expected) if not c.isdecimal()]):
            word.update(needs_review=True, review_reason="numeric_unresolved")
            continue
        # Only edit positions supported by the separately trained glyph model.
        # This preserves a clean word when glyph segmentation reads ۹ as ۵.
        independent = set(result.get("independent_positions", []))
        original_digits = _norm_digits(expected)
        chosen = "".join(new if new.isdecimal() and i in independent else old
                         for i, (new, old) in enumerate(zip(candidate, original_digits)))
        whole_read = ("?" not in candidate and not result.get("needs_review") and chosen == candidate)
        if reading != original and not (whole_read and len(independent) == digit_count):
            word.update(needs_review=True, review_reason="numeric_unresolved")
            continue
        word["text"] = reading[:token.start()] + E.display_text(chosen) + reading[token.end():]
        disagreed = candidate != original_digits or reading != original
        if disagreed or not whole_read:
            word.update(needs_review=True, review_reason="numeric_disagreement" if disagreed else "numeric_unresolved")
        if whole_read:
            word["numeric_status"] = "checked"
            word["numeric_verified"] = True



def _recover_header_numbers(words: list[dict], source: np.ndarray,
                            engines: list[E.OCREngine], metrics: Metrics, dpi: int = 2000) -> None:
    """Recover collapsed numeric fields only beside a recognized header label.

    No expected date, filename, calendar range or letter-number template is
    supplied to recognition. Every digit and slash must come from the pixels.
    """
    height, width = source.shape[:2]
    labels = [word for word in words if word.get('bbox') and
              re.sub(r'[^آ-یA-Za-z]', '', word.get('text', '')).lower()
              in {'تاریخ', 'تار', 'شماره', 'شما', 'date', 'no'}]
    remaining = 6
    for word in words:
        original = word.get('text', '')
        if (not remaining or not word.get('bbox') or word.get('numeric_verified')
                or (word.get('confidence') or 0) >= .85):
            continue
        x0, y0, x1, y1 = word['bbox']
        word_height = max(1, y1-y0)
        if (y0 > .30 * height or not 3.5 <= (x1-x0)/word_height <= 24
                or not re.fullmatch(r'[۰-۹٠-٩0-9/.-]+|[آ-ی]{1,4}', original)):
            continue
        nearby = any(0 <= label['bbox'][0]-x1 <= .18*width
                     and abs((label['bbox'][1]+label['bbox'][3]-y0-y1)/2) <= word_height
                     for label in labels if label is not word)
        if not nearby:
            continue
        remaining -= 1
        word['needs_review'] = True
        pad = max(3, round(.08 * word_height))
        crop = source[max(0,y0-pad):min(height,y1+pad),max(0,x0-pad):min(width,x1+pad)]
        if not crop.size:
            continue
        try:
            result = _glyph_digit_ocr(engines, crop, metrics, dpi=dpi, detect_separators=True)
        except (RuntimeError, OSError, ValueError):
            continue
        candidate = _norm_digits(result.get('text', ''))
        if result.get('needs_review') or not re.fullmatch(r'[0-9]{1,10}(?:/[0-9]{1,10}){1,3}', candidate):
            continue
        word.setdefault('raw_ocr_text', original)
        word['text'] = E.display_text(candidate)
        word['numeric_verified'] = True
        word['numeric_status'] = "checked"
        word['review_reason'] = "numeric_disagreement"
        word['numeric_reading'] = {"text": result["text"], "engine": "glyph_pass",
                                   "glyphs": result.get("glyph_evidence", [])}
        word['recognition_repaired'] = True


def _ocr_table(region: L.Region, proc: np.ndarray,
               engine_list: list[E.OCREngine], m: Metrics, table_id: int,
               img_shape: tuple, raw_img: np.ndarray | None = None, dpi: int = 2000) -> dict | None:
    """OCR سلول‌به‌سلول جدول از مستر 2000DPI (بخش ۱۰)."""
    if not region.grid:
        return None
    grid_abs = region.grid  # مطلق مستر
    cell_texts: dict = {}
    cell_confs: dict = {}
    cell_needs_review: dict = {}
    original_readings: dict = {}
    engines_used: set[str] = set()
    H, W = img_shape
    for (r, c, bbox) in T.cell_bboxes(grid_abs):
        bx = T.pad_cell_bbox(bbox, (H, W), pad_ratio=0.0)  # بدون پد — خطوط مرزی خارج
        crop_raw = proc[max(0, bx[1]):min(H, bx[3]), max(0, bx[0]):min(W, bx[2])]
        if crop_raw.size == 0 or (crop_raw < 128).mean() < 0.002:  # سلول خالی
            cell_texts[(r, c)] = ""
            cell_confs[(r, c)] = None
            continue
        # بقایای خطوط شبکه پاک می‌شوند — وگرنه تحلیل چیدمان موتور می‌شکند (بخش ۱۰)
        crop = T.strip_grid_lines(crop_raw)
        res = _cell_ocr_best(engine_list, crop, m, dpi=dpi)
        engines_used.add(res.get("engine", "?"))
        if res.get("multipass"):
            m.inc("fallbacks")
        m.inc("fallbacks", int(res.get("extra_passes", 0)))
        txt = res.get("text", "").strip().replace("\n", " ")
        original_readings[(r, c)] = res.get('raw_ocr_text', txt)
        conf = res.get("confidence")
        # English numerals need the English model; Persian-only recognition can
        # drop a decimal point or read 500 as 00. Require matching full readings.
        if re.fullmatch(r'[0-9۰-۹٠-٩,٬٫./+%\s-]{1,32}', txt) and re.search(r'[0-9]', txt):
            numeric = _confirmed_crop_read(engine_list, crop, m, langs='eng')
            candidate = (numeric or {}).get('text', '').strip()
            if re.fullmatch(r'[0-9][0-9,./+%\s-]{0,31}', candidate):
                txt = candidate
                conf = E.result_confidence(numeric)
                if E.en_digits_only(txt) != E.en_digits_only(original_readings[(r, c)]):
                    res['needs_review'] = True
        if '*' in txt or '\ufffd' in txt:
            suspect = {'text': txt, 'confidence': conf, 'bbox': (0, 0, crop.shape[1], crop.shape[0])}
            _repair_suspicious_words([suspect], crop, engine_list, m, dpi=dpi)
            txt, conf = suspect['text'], suspect.get('confidence')
            res['needs_review'] = True
        # گذر عددی (بخش ۲۸): سلول عمدتاً عددی → گلیف‌به‌گلیف با whitelist
        if re.fullmatch(r'[۰-۹][۰-۹,٬٫./-]{0,23}', txt):
            # گذر گلیف از مستر خام (صفحهٔ تمیز) — فیلترها confusion ارقام می‌سازند
            glyph_src = raw_img if raw_img is not None else proc
            x0g, y0g, x1g, y1g = bx
            crop_g = glyph_src[y0g:min(img_shape[0], y1g),
                               x0g:min(img_shape[1], x1g)]
            crop_g = T.strip_grid_lines(crop_g)  # خطوط شبکه در برش خام هستند
            separators = any(not char.isdigit() for char in txt)
            r2 = _glyph_digit_ocr(engine_list, crop_g, m, dpi=dpi,
                                  **({'expected_text': txt} if separators else {}))
            if separators and not r2.get('text') and (conf or 0) < CONF_REVIEW:
                # The first read may have merged a digit with a comma. A
                # count mismatch cannot be verified against that broken layout;
                # allow only a fully agreed, image-segmented replacement below.
                recovered = _glyph_digit_ocr(engine_list, crop_g, m, dpi=dpi)
                if not recovered.get('needs_review'):
                    r2 = recovered
            t2 = r2.get("text", "")
            complete = re.fullmatch(r"[۰-۹0-9][۰-۹0-9,٬٫./-]{0,23}", t2)
            if complete and r2.get('needs_review') and len(t2) == len(txt):
                confirmed = r2.get('confirmed_positions', [])
                t2 = ''.join(char if i in confirmed else txt[i] for i, char in enumerate(t2))
                if confirmed:
                    txt, conf = t2, None
                res['needs_review'] = True
            elif not complete or r2.get('needs_review'):
                # A failed parallel candidate must not turn a partial number
                # into a replacement for the full original cell value.
                res["needs_review"] = True
            elif _norm_digits(t2) != _norm_digits(txt):
                # عدم توافق واقعی: نتیجهٔ گذر تمیزتر (مستر خام) + پرچم بازبینی صریح
                txt = t2
                conf = None
                res["needs_review"] = True  # بخش ۲۷: عدم توافق پنهان نمی‌شود
        if conf is not None and conf < CONF_REVIEW:
            res['needs_review'] = True
        if res.get("needs_review"):
            m.low_conf_regions += 1
        cell_texts[(r, c)] = txt
        cell_confs[(r, c)] = conf
        if res.get("needs_review"):
            cell_needs_review[(r, c)] = True
    model = T.build_table_model(table_id, grid_abs, cell_texts, cell_confs,
                                ",".join(sorted(engines_used)) or "tesseract",
                                cell_needs_review)
    for cell in model['cells']:
        cell['raw_ocr_text'] = original_readings.get((cell['row'], cell['column']), '')
    return model


def _debug_table_image(master: R.MasterPage, regions, tables, debug_dir: Path,
                       page_no: int, source: np.ndarray | None = None):
    """دیباگ جدول‌ها از مستر — مستر تغییر نمی‌کند (بخش ۲۴)."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    proc = source if source is not None else cv2.imread(str(master.path), cv2.IMREAD_GRAYSCALE)
    if proc is None:
        return
    # Resize grayscale first: an A4 master is ~387MP. Expanding it to BGR
    # before shrinking needlessly allocates over 1GB for a 2400px preview.
    scale = min(1.0, 2400.0 / max(proc.shape[:2]))
    if scale < 1:
        proc = cv2.resize(proc, None, fx=scale, fy=scale)
    vis = cv2.cvtColor(proc, cv2.COLOR_GRAY2BGR)

    def preview_box(bbox):
        return tuple(round(value * scale) for value in bbox)

    for r in regions:
        x0, y0, x1, y1 = preview_box(r.bbox)
        color = (0, 200, 0) if r.kind == "text" else (0, 120, 255)
        cv2.rectangle(vis, (x0, y0), (x1, y1), color, max(1, round(6 * scale)))
    for t in tables:
        for cell in t["cells"]:
            x0, y0, x1, y1 = preview_box(cell["bbox"])
            color = (0, 0, 255) if cell.get("needs_review") else (255, 0, 0)
            cv2.rectangle(vis, (x0, y0), (x1, y1), color, max(1, round(3 * scale)))
    cv2.imwrite(str(debug_dir / f"page_{page_no:03d}_layout.png"), vis)


def process_pdf(pdf_path: Path, out_dir: Path, dpi: int = 2000,
                api_key: str | None = None, force_tesseract: bool = False,
                max_pages: int = 0, debug: bool = True,
                langs: str = "fas", progress_cb=None, *, mode: str | None = None,
                candidate_options=None, temp_root: Path | None = None,
                handwriting: bool = False, resume: bool = False, keep_masters: bool = True,
                render_budget_mp: float = R.MAX_RENDER_MP) -> dict:
    """Own native workers and PDF handles, including exceptional exits."""
    if api_key:
        raise ValueError("External document APIs are disabled; use local Tesseract")
    mode = mode or os.environ.get("OCR_PIPELINE_MODE", "legacy")
    if mode not in ("legacy", "compare", "candidate"):
        raise ValueError("Unknown OCR pipeline mode")
    if handwriting and mode in ("candidate", "compare"):
        from dataclasses import replace
        from .precision.profiles import Options
        candidate_options = replace(candidate_options or Options(), handwriting=True)
    if mode == "candidate":
        return _candidate_document(pdf_path, out_dir, candidate_options, temp_root,
                                   max_pages, progress_cb, publish_exports=True)
    reuse = os.environ.get('OCR_PERSISTENT_ENGINE', '1') != '0'
    with E.TesseractEngine(langs, reuse_model=reuse) as local, pymupdf.open(pdf_path) as doc:
        payload = _process_document(doc, local, pdf_path, out_dir, dpi, api_key,
                                    True, max_pages, debug, progress_cb, handwriting=handwriting,
                                    resume=resume, keep_masters=keep_masters, render_budget_mp=render_budget_mp)
    if mode == "compare":
        try:
            candidate = _candidate_document(pdf_path, out_dir, candidate_options, temp_root,
                                            max_pages, None, publish_exports=False)
            payload["comparison"] = {"status": "completed", "extraction_revision": candidate["extraction_revision"],
                                     "document_id": candidate["document"]["id"], "active_text_replaced": False}
        except Exception as error:
            payload["comparison"] = {"status": "failed", "error": type(error).__name__, "active_text_replaced": False}
        (out_dir / "comparison.json").write_text(json.dumps(payload["comparison"], indent=2), encoding="utf-8")
    return payload


def _candidate_document(source, out_dir, options, temp_root, max_pages, callback, *, publish_exports):
    from .precision.extraction import process_document
    configured_temp = temp_root or os.environ.get("OCR_TEMP_DIR")
    if not configured_temp:
        raise ValueError("An explicit OCR_TEMP_DIR is required for experimental extraction")
    result = process_document(source, out_dir, options=options, temp_root=Path(configured_temp),
                              max_pages=max_pages, progress_cb=callback)
    if publish_exports:
        # These compatibility exports are copies of one immutable snapshot. No repeated OCR.
        snapshot = out_dir / "extractions" / result["extraction_revision"]
        for extension in ("txt", "json"):
            import shutil
            shutil.copyfile(snapshot / f"extraction.{extension}", out_dir / f"{source.stem}.{extension}")
    return result


def _checkpoint_key(source_hash, engine, dpi, render_budget_mp, n_pages, debug, handwriting):
    code_root = Path(__file__).resolve().parent
    models = {path.name: file_sha256(path) for path in sorted(engine.tessdata_dir.glob("*.traineddata"))}
    executable = file_sha256(Path(engine.executable)) if engine.executable else None
    identity = (source_hash, dpi, render_budget_mp, n_pages, bool(debug), bool(handwriting), engine.langs,
                models, executable, cv2.__version__, pymupdf.VersionBind, np.__version__,
                engine._reuse_model, os.environ.get("OMP_THREAD_LIMIT"), engine.workers,
                {path.name: file_sha256(path) for path in sorted(code_root.glob("*.py"))})
    model_dir = os.environ.get(N.MODEL_DIR)
    if model_dir:
        identity += (N.model_identity(model_dir),)
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _read_checkpoint(path, key, page, out_dir):
    try:
        data = json.loads(path.read_text("utf-8"))
        result = data["page"]
        if data["key"] != key or result["page"] != page or result.get("error"):
            return None
        if not all((out_dir / name).is_file() for name in result.get("artifacts", [])):
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _extract_page(doc, number, master_dir, out_dir, filename, engine, dpi, debug, handwriting,
                  keep_masters, render_budget_mp):
    for attempt in (1, 2):
        metrics = Metrics()
        before = engine.statistics()
        master_path = master_dir / f"master_p{number:04d}{R.TIFF_EXT}"
        try:
            with metrics.step("render"):
                if shutil.disk_usage(master_dir).free < 1536 * 2**20:
                    raise OSError("Insufficient OCR scratch space")
                source_page = doc[number - 1]
                master = R.render_master(source_page, number, master_dir, dpi=dpi, max_mp=render_budget_mp)
            result = ocr_page(master, [engine], metrics, out_dir / "debug" if debug else None, number,
                              source_page=source_page)
            result["metrics"].update({key: value - before[key] for key, value in engine.statistics().items()})
            result["master_bytes"] = master.path.stat().st_size
            result["filename"] = filename
            result["attempts"] = attempt
            result["quality"] = page_quality(result, handwriting=handwriting)
            result["export_annotations"] = ([] if result.get("status") == "blank" else
                                             [{"kind": "quality_notice", "text": quality_notice(result["quality"])}])
            paths = [out_dir / "tables" / name for name in result.pop("_table_files", [])]
            debug_path = out_dir / "debug" / f"page_{number:03d}_layout.png"
            if debug and result.get("tables") and debug_path.is_file():
                paths.append(debug_path)
            result["artifacts"] = [path.resolve().relative_to(out_dir).as_posix() for path in paths]
            return result
        except Exception as exc:  # Keep the failure local to the affected page.
            logging.getLogger(__name__).warning("OCR page %d attempt %d failed", number, attempt, exc_info=True)
            reason = {"PDF render lost visible page content": "render_content_lost",
                      "Incomplete PDF render tile": "render_tile_incomplete",
                      "OCR returned no text from a nonblank page": "nonblank_page_no_text",
                      "Insufficient OCR scratch space": "disk_budget_exhausted"}.get(str(exc), "page_processing_failed")
            # Retrying a deterministic input or capacity error cannot fix it.
            if attempt == 2 or reason != "page_processing_failed" or isinstance(exc, (MemoryError, ValueError)):
                failed = {"page": number, "error": reason, "filename": filename,
                          "attempts": attempt, "metrics": metrics.report()}
                failed["quality"] = page_quality(failed, handwriting=handwriting)
                return failed
        finally:
            metrics.stop()
            if not keep_masters:
                master_path.unlink(missing_ok=True)


def _process_document(doc, local_engine, pdf_path, out_dir, dpi, api_key,
                      force_tesseract, max_pages, debug, progress_cb, *, handwriting=False,
                      resume=False, keep_masters=True, render_budget_mp=R.MAX_RENDER_MP):
    """Preserve completed pages across interruptions of the same document revision."""
    t0 = time.perf_counter()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    master_dir = out_dir / ("master" if keep_masters else ".scratch-master")
    master_dir.mkdir(exist_ok=True)
    engine = E.pick_engine([local_engine])
    n_pages = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
    source_hash = file_sha256(pdf_path)
    checkpoint_key = (_checkpoint_key(source_hash, local_engine, dpi, render_budget_mp, n_pages, debug, handwriting)
                      if resume else None)
    if progress_cb:
        progress_cb({"type": "start", "pages": n_pages, "dpi": dpi})
    pages_res = []
    reused_pages = 0
    for number in range(1, n_pages + 1):
        checkpoint = out_dir / ".checkpoints" / f"{number:04d}.json"
        res = _read_checkpoint(checkpoint, checkpoint_key, number, out_dir) if resume else None
        reused = res is not None
        reused_pages += reused
        if res is None:
            res = _extract_page(doc, number, master_dir, out_dir, pdf_path.name, local_engine,
                                dpi, debug, handwriting, keep_masters, render_budget_mp)
            if resume and not res.get("error"):
                atomic_json(checkpoint, {"key": checkpoint_key, "page": res})
        pages_res.append(res)
        review_items = []
        for item in [*res.get("words", []), *(cell for table in res.get("tables", [])
                                             for cell in table.get("cells", []))]:
            if item.get("needs_review"):
                review_items.append({"bbox": item.get("source_bbox", item.get("bbox")),
                                     "text": item.get("text", item.get("normalized_text", ""))[:100],
                                     "reason": item.get("review_reason", "uncertain_recognition")})
        atomic_json(out_dir / ".review" / f"{number:04d}.json", review_items[:120])
        if progress_cb:
            if res.get("error"):
                progress_cb({"type": "page_error", "page": number, "error": res["error"]})
            else:
                progress_cb({"type": "page_ok", "page": number, "words": len(res.get("words", [])),
                             "needs_review": res["quality"]["review_required"], "reused": reused,
                             "tables": len(res.get("tables", [])),
                             "source": (res.get("engines") or [engine.name])[0]})

    stem = pdf_path.stem
    atomic_text(out_dir / f"{stem}.txt", build_txt(pages_res))
    quality_snapshot = {"pages": [{"page": page["page"], "quality": page["quality"],
                                   "export_annotations": page.get("export_annotations", [])}
                                  for page in pages_res]}
    atomic_json(out_dir / f"{stem}.quality.json", quality_snapshot)
    perf = local_engine.statistics()
    elapsed = time.perf_counter() - t0
    payload = {
        "document": {"filename": pdf_path.name, "pages": n_pages, "source_sha256": source_hash,
                     "source_dpi": dpi, "engine": engine.name,
                     "handwriting_declared": bool(handwriting), "human_verified": False},
        "pages": pages_res,
        "performance": {
            "total_seconds": round(elapsed, 2),
            "avg_page_seconds": round(elapsed / max(n_pages, 1), 2),
            **{key: max((p.get("metrics", {}).get(key, 0) for p in pages_res), default=0)
               for key in ("peak_rss_mb", "peak_tree_rss_mb", "peak_children_rss_mb")},
            "master_bytes": sum(p.get("master_bytes", 0) for p in pages_res),
            "checkpoint_hits": reused_pages, "cache_enabled": local_engine.cache_enabled,
            "ocr_calls": sum(p.get("metrics", {}).get("ocr_calls", 0) for p in pages_res),
            "ocr_processes": perf["ocr_processes"],
            "ocr_cache_hits": perf["ocr_cache_hits"],
            "ocr_native_calls": perf.get("ocr_native_calls", 0),
            "fallbacks": sum(p.get("metrics", {}).get("fallbacks", 0) for p in pages_res),
            "low_confidence_regions": sum(p.get("low_confidence_words", 0) for p in pages_res),
            "failed_pages": sum(1 for p in pages_res if p.get("error")),
        },
    }
    atomic_json(out_dir / f"{stem}.json", payload)
    if progress_cb:
        progress_cb({"type": "done", "pages": n_pages,
                     "pages_ok": n_pages - payload["performance"]["failed_pages"],
                     "words": sum(len(p.get("words", [])) for p in pages_res),
                     "seconds": payload["performance"]["total_seconds"],
                     "txt": f"{stem}.txt", "json": f"{stem}.json"})
    return payload



def build_txt(pages: list[dict]) -> str:
    """TXT خوانا با جداکنندهٔ صفحه و جدول Markdown (بخش ۲۳)."""
    parts = []
    for rec in pages:
        parts.append("=" * 20 + f" PAGE {rec['page']} " + "=" * 20)
        if rec.get("error"):
            parts.append(f"[ERROR] {rec['error']}")
            continue
        for annotation in rec.get("export_annotations", []):
            if annotation.get("kind") == "quality_notice":
                parts.append(annotation["text"])
        parts.append(rec.get("text") or ("[صفحهٔ سفید]" if rec.get("status") == "blank" else "[متنی استخراج نشد]"))
        for t in rec.get("tables", []):
            parts.append(f"\nجدول {t['table_id']} ({t['rows']}×{t['columns']}):")
            parts.append(t["markdown"])
        parts.append("")
    return E.display_text("\n".join(parts)) + "\n"
