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

import json
import os
import re
import time
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from . import engines as E
from . import layout as L
from . import preprocess as P
from . import render as R
from . import tables as T
from .metrics import Metrics

CONF_REVIEW = 0.75   # بخش ۲۷: زیر این اطمینان → needs_review
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
             debug_dir: Path | None = None, page_no: int = 1) -> dict:
    """OCR یک صفحه از مستر — معماری ناحیه‌محور، بدون نگه‌داشتن چند نسخهٔ کامل."""
    with m.step("master_read"):
        img = master.read_full()          # مستر 2000DPI (حافظهٔ خط ۲)
    if img is None:
        raise RuntimeError(f"master image unreadable: {master.path}")
    H, W = img.shape[:2]
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

    words_all: list[dict] = []
    text_parts: list[str] = []
    tables_out: list[dict] = []
    table_files: list[str] = []
    low_conf = 0
    page_engines: set[str] = set()

    def _crop_from_master(bbox: tuple) -> np.ndarray:
        """برش ناحیه از مستر پیش‌پردازش‌شده (بخش ۱۰)."""
        x0, y0, x1, y1 = bbox
        return proc[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]

    for region in regions:
        if region.kind == "table":
            with m.step("table_ocr"):
                tmodel = _ocr_table(region, proc, engine_list, m,
                                    len(tables_out) + 1, (H, W),
                                    raw_img if clean_page else None,
                                    dpi=master.effective_dpi)
            if tmodel:
                tables_out.append(tmodel)
                table_files.extend(T.write_table_outputs(
                    tmodel, master.path.parent.parent / "tables"))
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
            if max(crop.shape[:2]) > 4500:
                ladder = [f0, *(min(2.0, fac * reference_scale) for fac in (0.16, 0.13, 0.10))]
            else:
                ladder = [f0, round(f0 * 0.5, 3)]
            candidates = []
            for fac in dict.fromkeys(round(x, 3) for x in ladder if x >= 0.04):
                ok, buf = cv2.imencode(
                    ".png", P.pad_white(P.resize_by_factor(crop, fac)))
                if not ok:
                    continue
                candidates.append((fac, buf.tobytes()))
            m.inc("ocr_calls", len(candidates))
            results = []
            readings = E.ocr_many(engine_list, [png for _, png in candidates], region_kind="text")
            for (fac, _), r in zip(candidates, readings):
                if r and r.get("words"):
                    r["_scale"] = fac
                    results.append(r)
            if not results:
                continue
            # رأی‌گیری: اکثریت متن غیرخالی؛ تساوی → اطمینان بالاتر (بخش ۱۴)
            def _txt_of(rr):
                return " ".join((rr.get("text") or "").split())

            def _conf_of(rr):
                cs = [w.get("confidence") for w in rr.get("words") or []
                      if w.get("confidence") is not None]
                return sum(cs) / len(cs) if cs else 0.0

            groups: dict[str, list[dict]] = {}
            for rr in results:
                t = _txt_of(rr)
                if t:
                    groups.setdefault(t, []).append(rr)
            if groups:
                best_group = max(groups.values(), key=len)
                res = max(best_group, key=_conf_of)
                f = res["_scale"]
                if len(best_group) == 1 and len(groups) > 1:
                    res = dict(res)
                    res["needs_review"] = True  # سازگاری ضعیف — بخش ۲۷
            else:
                res = results[0]
                f = res["_scale"]
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
                         or E.region_has_latin_context(res["words"])):
                # مقیاس گذر eng: هدف ~35px گلیف (ارقام لاتین بزرگ‌تر از فارسی‌اند)
                f_eng = max(0.10, min(0.35 * reference_scale, P.optimal_ocr_scale(crop,
                                                                target_h=35.0)))
                ok2, buf2 = cv2.imencode(
                    ".png", P.pad_white(P.resize_by_factor(crop, f_eng)))
                if ok2:
                    m.inc("ocr_calls")
                    m.inc("fallbacks")
                    try:
                        r_eng = engine_list[-1].ocr_image(buf2.tobytes(),
                                                          psm=3, langs="eng")
                        k = f / f_eng  # کادر eng → قاب مختصات گذر fas
                        ew = []
                        for x in r_eng.get("words") or []:
                            bx = x["bbox"]
                            x["bbox"] = (int((bx[0] - 30) * k),
                                         int((bx[1] - 30) * k),
                                         int((bx[2] - 30) * k),
                                         int((bx[3] - 30) * k))
                            ew.append(x)
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
            if res.get("text", "").strip():
                text_parts.append(res["text"].strip())

    # ── ساختار سطری متن صفحه از کلمات (بخش ۵ و ۲۱) ──
    words_all = E.merge_word_lists([words_all])
    with m.step("numeric_verification"):
        _verify_text_numbers(words_all, proc, engine_list, m, dpi=master.effective_dpi)
    for w in words_all:
        conf = w.get("confidence")
        if conf is not None and conf < CONF_REVIEW:
            w["needs_review"] = True
        if w.get("needs_review"):
            low_conf += 1
    lines = E.words_to_lines(words_all)
    raw_text = "\n".join(text_parts)

    m.low_conf_regions += low_conf
    confs = [w["confidence"] for w in words_all if w.get("confidence") is not None]
    page_conf = round(sum(confs) / len(confs), 4) if confs else None

    # متن نهایی صفحه: سطرهای بازسازی‌شده (حفظ ترتیب بصری RTL)
    final_text = "\n".join(lines) if lines else raw_text
    normalized = E.normalize_text(final_text)

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
        "source_pdf_pt": {"w": round(master.pdf_w_pt, 1), "h": round(master.pdf_h_pt, 1)},
        "image_mb": master.approx_mem_mb,
        "render_seconds": master.render_seconds,
        "analysis": analysis,
        "preprocess_ops": ops,
        "regions": [{"kind": r.kind, "bbox": list(r.bbox)} for r in regions],
        "blocks": [{"kind": r.kind, "bbox": list(r.bbox)} for r in regions],
        "lines": lines,
        "words": [{"text": w["text"], "bbox": list(w["bbox"]),
                   "confidence": w.get("confidence"),
                   **({"raw_ocr_text": w["raw_ocr_text"], "numeric_verified": True}
                      if w.get("numeric_verified") else {}),
                   **({"needs_review": True} if w.get("needs_review") else {})}
                  for w in words_all],
        "raw_text": raw_text,
        "normalized_text": normalized,
        "normalized_en_digits": E.en_digits_only(normalized),
        "text": E.normalize_text(final_text),
        "tables": [{"table_id": t["table_id"], "rows": t["rows"],
                    "columns": t["columns"],
                    "markdown": T.markdown_table(t)} for t in tables_out],
        "confidence": page_conf,
        "engines": sorted(page_engines) if page_engines else [engine_list[0].name],
        "identifiers": E.find_identifiers(normalized),
        "validation": validate_text(raw_text),
        "low_confidence_words": low_conf,
    }

    if debug_dir and tables_out:
        with m.step("debug"):
            _debug_table_image(master, regions, tables_out, debug_dir, page_no, source=img)

    page_res["metrics"] = m.report()
    page_res["_table_files"] = table_files
    img = proc = proc_small = None
    return page_res


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
                   m: Metrics, kind: str = "table_cell", dpi: int = 2000) -> dict:
    """OCR چندمقیاسی سلول + رأی‌گیری سازگاری (بخش ۱۴ — انتخاب با consistency).

    متن درست بین مقیاس‌های مختلف تکرار می‌شود؛ آشغالِ مقیاس‌محور تصادفی است.
    اکثریتِ متن‌های غیرخالی برنده است؛ در نبود اکثریت، بالاترین اطمینان.
    """
    crop = _ink_tight_crop(crop)
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

    def txt_of(r: dict) -> str:
        return " ".join((r.get("text") or "").split())

    def score(r: dict) -> float:
        c = r.get("confidence")
        return float(c) if c is not None else (0.5 if txt_of(r) else 0.0)

    # گروه‌بندی متن‌های غیرخالی (خالی هرگز برنده نمی‌شود — بخش ۲۷)
    groups: dict[str, list[dict]] = {}
    for r in results:
        t = txt_of(r)
        if t:
            groups.setdefault(t, []).append(r)
    if groups:
        best_group = max(groups.values(), key=len)
        best = max(best_group, key=score)
        best = dict(best)
        best["votes"] = len(best_group)
        # سازگاری ضعیف + اطمینان پایین → نیاز به بازبینی (بخش ۲۷)
        if len(best_group) == 1 and (score(best) < 0.9 or len(groups) > 2):
            best["needs_review"] = True
    else:
        best = {"text": "", "words": [], "confidence": None,
                "engine": results[0].get("engine", "tesseract")}
    return best


FA_DIGIT_CHARS = "۰۱۲۳۴۵۶۷۸۹"
EN_DIGIT_CHARS = "0123456789"


def _norm_digits(s: str) -> str:
    """هم‌سطح‌سازی هر دو سیستم رقم برای مقایسهٔ توافق — سیستم رقمِ متن اصلی حفظ می‌شود."""
    return s.translate(str.maketrans(FA_DIGIT_CHARS + "٠١٢٣٤٥٦٧٨٩",
                                     EN_DIGIT_CHARS * 2))


def _glyph_digit_ocr(engine_list: list[E.OCREngine], crop: np.ndarray,
                     m: Metrics, dpi: int = 2000) -> dict:
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
    digit_band_top = min((b[1] for b in bases), default=0)
    digit_band_bot = max((b[1] + b[3] for b in bases), default=H)
    out = []
    uncertain = False
    glyphs = []
    images = []
    for (x, y, w, h) in bases:
        # قانون شکل صفر فارسی: دایرهٔ کوچکِ در نیمهٔ پایینیِ نوار ارقام
        if h < 0.45 * med_h and w <= 1.8 * h and \
                (y + h / 2) > (digit_band_top + digit_band_bot) / 2:
            glyphs.append((None, []))
            continue
        x0m, y0m = max(0, x - h // 3), max(0, y - h // 3)
        x1m, y1m = min(W, x + w + h // 3), min(H, y + h + h // 3)
        # Preserve padding geometry without allowing a neighbouring digit into
        # this single-glyph recognition pass.
        g = np.full((y1m - y0m, x1m - x0m), 255, dtype=crop.dtype)
        g[y - y0m:y - y0m + h, x - x0m:x - x0m + w] = crop[y:y + h, x:x + w]
        # هدف 80px — کالیبره‌شده روی تست confusion ارقام (۳/۴ در ارتفاع کم مخلوط می‌شوند)
        f = P.optimal_ocr_scale(g, target_h=80.0)
        indices = []
        for fac in dict.fromkeys([f, round(f * 0.55, 3)]):
            g2 = P.pad_white(P.resize_by_factor(g, fac))
            ok, buf = cv2.imencode(".png", g2)
            if not ok:
                continue
            indices.append(len(images))
            images.append(buf.tobytes())
        glyphs.append((g, indices))
    m.inc("ocr_calls", len(images))
    readings = E.ocr_many(engine_list, images, region_kind="glyph",
                          whitelist=FA_DIGIT_CHARS + EN_DIGIT_CHARS)
    for g, indices in glyphs:
        if g is None:
            out.append("۰")
            continue
        reads = []
        for index in indices:
            res = readings[index] or {}
            ch = (res.get("text") or "").strip().replace("\n", " ")
            if len(ch) == 1 and ch in (FA_DIGIT_CHARS + EN_DIGIT_CHARS):
                reads.append(ch)
        if len(reads) < 2 or len({_norm_digits(ch) for ch in reads}) != 1:
            uncertain = True
        if not reads:
            out.append("?")
            uncertain = True
        else:
            # قانون شکل ۳/۴ (استخراجی — بخش ۲۹، تأیید روی گلیف واقعی و مرجع فونت):
            # ۳ در نوارِ راستِ گلیف یک ران پیوسته دارد؛ ۴ دو ران (ران بالای کوچک +
            # ساقهٔ پایینی جدا). بدون این، ۴ در ۲۰۰۰DPI اغلب ۳ خوانده می‌شود.
            if reads[0] == "۳":
                gb = (cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV +
                                    cv2.THRESH_OTSU)[1] > 0)
                band = gb[:, int(0.72 * g.shape[1]):]
                proj = band.sum(axis=1)
                thr = max(1.0, 0.05 * band.shape[1])
                min_run = max(2, int(0.10 * g.shape[0]))
                runs, inr, rs = 0, False, 0
                for yy, v in enumerate(proj > thr):
                    if v and not inr:
                        inr, rs = True, yy
                    elif not v and inr:
                        inr = False
                        if yy - rs >= min_run:
                            runs += 1
                if inr and g.shape[0] - rs >= min_run:
                    runs += 1
                if runs >= 2:
                    reads[0] = "۴"
            out.append(reads[0])
    text = "".join(out).replace("\u200c", "").strip()
    if not text:
        return {"text": ""}
    if uncertain:
        return {"text": text, "engine": "glyph_pass", "confidence": None,
                "needs_review": True}
    return {"text": text, "engine": "glyph_pass", "confidence": None}


def _verify_text_numbers(words: list[dict], source: np.ndarray,
                         engines: list[E.OCREngine], metrics: Metrics, dpi: int = 2000) -> None:
    """Verify confusable Persian numerals from isolated glyphs in the master.

    Keep the initial reading for audit, and flag any disagreement for review.
    Uncertain, incomplete, or mixed-text candidates never replace the original.
    """
    remaining = 16  # Bound extra OCR work per page.
    height, width = source.shape[:2]
    for word in words:
        original = word.get("text", "")
        if not re.fullmatch(r"[۰-۹]{2,16}", original) or not set(original) & {"۲", "۳"}:
            continue
        if not remaining:
            word["needs_review"] = True
            continue
        remaining -= 1
        x0, y0, x1, y1 = word["bbox"]
        pad = max(4, int((y1 - y0) * 0.04))
        crop = source[max(0, y0 - pad):min(height, y1 + pad),
                      max(0, x0 - pad):min(width, x1 + pad)]
        if not crop.size:
            word["needs_review"] = True
            continue
        try:
            result = _glyph_digit_ocr(engines, crop, metrics, dpi=dpi)
        except (RuntimeError, OSError, ValueError):
            word["needs_review"] = True
            continue
        candidate = _norm_digits(result.get("text", ""))
        if result.get("needs_review") or not re.fullmatch(r"[0-9]{2,16}", candidate) or len(candidate) != len(original):
            word["needs_review"] = True
            continue
        verified = candidate.translate(str.maketrans(EN_DIGIT_CHARS, FA_DIGIT_CHARS))
        word["numeric_verified"] = True
        word["raw_ocr_text"] = original
        if verified != original:
            word["text"] = verified
            word["needs_review"] = True


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
        conf = res.get("confidence")
        # گذر عددی (بخش ۲۸): سلول عمدتاً عددی → گلیف‌به‌گلیف با whitelist
        digits = sum(ch.isdigit() or ch in FA_DIGIT_CHARS for ch in txt)
        if (txt and len(txt) <= 16 and digits >= max(2, len(txt.replace(" ", "")) - 2)) \
                or (txt and len(txt) == 1 and txt in FA_DIGIT_CHARS):
            # گذر گلیف از مستر خام (صفحهٔ تمیز) — فیلترها confusion ارقام می‌سازند
            glyph_src = raw_img if raw_img is not None else proc
            x0g, y0g, x1g, y1g = bx
            crop_g = glyph_src[y0g:min(img_shape[0], y1g),
                               x0g:min(img_shape[1], x1g)]
            crop_g = T.strip_grid_lines(crop_g)  # خطوط شبکه در برش خام هستند
            r2 = _glyph_digit_ocr(engine_list, crop_g, m, dpi=dpi)
            t2 = r2.get("text", "")
            complete = re.fullmatch(r"[۰-۹0-9]{1,16}", t2)
            if not complete:
                # A failed parallel candidate must not turn a partial number
                # into a replacement for the full original cell value.
                res["needs_review"] = True
            elif _norm_digits(t2) == _norm_digits(txt) and not r2.get("needs_review"):
                conf = max(conf or 0, 0.97)  # دو مسیر مستقل تأیید کردند (هر سیستمی که باشد)
            elif _norm_digits(t2) != _norm_digits(txt):
                # عدم توافق واقعی: نتیجهٔ گذر تمیزتر (مستر خام) + پرچم بازبینی صریح
                txt = t2
                conf = None
                res["needs_review"] = True  # بخش ۲۷: عدم توافق پنهان نمی‌شود
            elif r2.get("needs_review"):
                res["needs_review"] = True
        if res.get("needs_review"):
            m.low_conf_regions += 1
        cell_texts[(r, c)] = txt
        cell_confs[(r, c)] = conf
        if res.get("needs_review"):
            cell_needs_review[(r, c)] = True
    model = T.build_table_model(table_id, grid_abs, cell_texts, cell_confs,
                                ",".join(sorted(engines_used)) or "tesseract",
                                cell_needs_review)
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
                langs: str = "fas", progress_cb=None) -> dict:
    """پردازش کامل یک PDF — مستر 2000DPI (بخش ۳۰)."""
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    master_dir = out_dir / "master"
    master_dir.mkdir(parents=True, exist_ok=True)

    engine_list: list[E.OCREngine] = []
    if not force_tesseract:
        key = api_key or os_key()
        if key:
            engine_list.append(E.GoogleVisionEngine(key))
    engine_list.append(E.TesseractEngine(langs))
    engine = E.pick_engine(engine_list)

    doc = pymupdf.open(pdf_path)
    n_pages = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
    if progress_cb:
        progress_cb({"type": "start", "pages": n_pages, "dpi": dpi})

    pages_res = []
    all_table_files: list[str] = []

    for i in range(n_pages):
        m = Metrics()
        engine_before = engine_list[-1].statistics()
        try:
            with m.step("render"):
                master = R.render_master(doc[i], i + 1, master_dir, dpi=dpi)
            res = ocr_page(master, engine_list, m,
                           out_dir / "debug" if debug else None, i + 1)
            res["metrics"].update({key: value - engine_before[key]
                                   for key, value in engine_list[-1].statistics().items()})
            res["filename"] = pdf_path.name
            for f in res.pop("_table_files", []):
                all_table_files.append(f)
            pages_res.append(res)
            if progress_cb:
                progress_cb({"type": "page_ok", "page": i + 1,
                             "words": len(res.get("words", [])),
                             "needs_review": bool(res.get("low_confidence_words")),
                             "tables": len(res.get("tables", [])),
                             "source": (res.get("engines") or [engine.name])[0]})
        except Exception as exc:  # noqa: BLE001
            pages_res.append({"page": i + 1, "error": str(exc),
                              "filename": pdf_path.name, "metrics": m.report()})
            if progress_cb:
                progress_cb({"type": "page_error", "page": i + 1, "error": str(exc)})
        finally:
            m.stop()

    doc.close()

    stem = pdf_path.stem
    txt = build_txt(pages_res)
    (out_dir / f"{stem}.txt").write_text(txt, "utf-8")
    payload = {
        "document": {"filename": pdf_path.name, "pages": n_pages,
                     "source_dpi": dpi, "engine": engine.name},
        "pages": pages_res,
        "performance": {
            "total_seconds": round(time.perf_counter() - t0, 2),
            "peak_rss_mb": max((p.get("metrics", {}).get("peak_rss_mb", 0)
                                for p in pages_res), default=0),
            "avg_page_seconds": round((time.perf_counter() - t0) / max(n_pages, 1), 2),
            "ocr_calls": sum(p.get("metrics", {}).get("ocr_calls", 0) for p in pages_res),
            "ocr_processes": engine_list[-1].statistics()["ocr_processes"],
            "ocr_cache_hits": engine_list[-1].statistics()["ocr_cache_hits"],
            "fallbacks": sum(p.get("metrics", {}).get("fallbacks", 0) for p in pages_res),
            "low_confidence_regions": sum(p.get("low_confidence_words", 0)
                                          for p in pages_res),
            "failed_pages": sum(1 for p in pages_res if p.get("error")),
        },
    }
    (out_dir / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    if progress_cb:
        progress_cb({"type": "done", "pages": n_pages,
                     "pages_ok": sum(1 for p in pages_res if not p.get("error")),
                     "words": sum(len(p.get("words", [])) for p in pages_res),
                     "seconds": payload["performance"]["total_seconds"],
                     "txt": f"{stem}.txt", "json": f"{stem}.json"})
    return payload


def os_key() -> str | None:
    key = os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    p = Path(__file__).resolve().parent.parent / "api_key.txt"
    return p.read_text().strip() if p.is_file() else None


def build_txt(pages: list[dict]) -> str:
    """TXT خوانا با جداکنندهٔ صفحه و جدول Markdown (بخش ۲۳)."""
    parts = []
    for rec in pages:
        parts.append("=" * 20 + f" PAGE {rec['page']} " + "=" * 20)
        if rec.get("error"):
            parts.append(f"[ERROR] {rec['error']}")
            continue
        parts.append(rec.get("text") or "[بدون متن]")
        for t in rec.get("tables", []):
            parts.append(f"\nجدول {t['table_id']} ({t['rows']}×{t['columns']}):")
            parts.append(t["markdown"])
        parts.append("")
    return "\n".join(parts).strip() + "\n"
