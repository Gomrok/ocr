# -*- coding: utf-8 -*-
"""لایهٔ انتزاع موتور OCR + fallback چندموتوره + نرمال‌سازی + شناسه‌ها (بخش ۴ تا ۷ و ۱۵ و ۱۹).

موتورها:
  GoogleVisionEngine  — نماد سازگاری غیرفعال؛ بدون ارتباط شبکه
  TesseractEngine     — fas داخل داکر، PSM انتخابی بر اساس نوع ناحیه

خروجی استاندارد: dict کلمات + متن + اطمینان (بدون جعل اطمینان — بخش ۱۸).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

from .native_pool import NativePool

TESSERACT_IMAGE = "ocr-tesseract:fa"

CONF_ACCEPT = 0.90      # بخش ۱۹
CONF_RETRY = 0.75


# ───────────────────── نرمال‌سازی (بخش ۴ و ۶) ─────────────────────

AR_TO_FA = str.maketrans({
    "ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "ؤ": "و",
    "٠": "۰", "١": "۱", "٢": "۲", "٣": "۳", "٤": "۴", "٥": "۵",
    "٦": "۶", "٧": "۷", "٨": "۸", "٩": "۹",
})
FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
EN_DIGITS = "0123456789"
_FA2EN = str.maketrans(FA_DIGITS, EN_DIGITS)
_EN2FA = str.maketrans(EN_DIGITS, FA_DIGITS)
_ALL2EN = str.maketrans(FA_DIGITS + "٠١٢٣٤٥٦٧٨٩", EN_DIGITS * 2)
_ZWNJ = "\u200c"
_ZWNJ_SENTINEL = "\x00"


def normalize_text(raw: str, normalize_digits: bool = False,
                   collapse_spaces: bool = True) -> str:
    """نرمال‌سازی قابل‌پیکربندی — فقط در فیلد normalized_text؛ raw دست‌نخورده (بخش ۴)."""
    t = raw.replace(_ZWNJ, _ZWNJ_SENTINEL)
    t = t.translate(AR_TO_FA)
    # جداکنندهٔ هزارگان فارسی (٬ U+066C) → ویرگول لاتین در متن نرمال (بخش ۶)
    t = t.replace("\u066C", ",")
    if normalize_digits:
        t = t.translate(_FA2EN)
    if collapse_spaces:
        t = re.sub(r"[ \t]+", " ", t)
    return t.replace(_ZWNJ_SENTINEL, _ZWNJ).strip()


def en_digits_only(text: str) -> str:
    """نسخه با ارقام لاتین (فیلد مستقل — بخش ۶)."""
    return text.translate(_ALL2EN)


def display_text(raw: str) -> str:
    """Formal Persian exports use one digit alphabet; raw OCR stays separate."""
    return normalize_text(raw).translate(_EN2FA)


def result_confidence(result: dict) -> float:
    values = [w['confidence'] for w in result.get('words') or []
              if w.get('confidence') is not None]
    if values:
        return sum(values) / len(values)
    confidence = result.get('confidence')
    return float(confidence) if confidence is not None else 0.0


def prefer_latin_region(primary: dict, secondary: dict) -> bool:
    """Accept a complete English line only when the pixels give strong evidence."""
    words = secondary.get('words') or []
    text = secondary.get('text', '')
    if len(re.findall(r'[A-Za-z]{2,}', text)) < 2 or not words:
        return False
    return (all(_valid_latin_read(w['text']) for w in words)
            and result_confidence(secondary) >= 0.85
            and result_confidence(secondary) >= result_confidence(primary) + 0.10
            and not any(re.search(r'[آ-غف-ی]{2,}', w['text'])
                        and (w.get('confidence') or 0) >= 0.85
                        for w in primary.get('words') or []))


def select_consensus(results: list[dict]) -> dict:
    """Equal vote counts must be decided by confidence, not candidate order."""
    groups: dict[str, list[dict]] = {}
    for result in results:
        text = ' '.join(normalize_text(result.get('text', ''), normalize_digits=True).split())
        if text:
            groups.setdefault(text, []).append(result)
    if not groups:
        return dict(results[0]) if results else {'text': '', 'words': [], 'confidence': None}
    # Repeated low-quality readings must not outvote a clean recognition.
    trusted = [items for items in groups.values() if max(map(result_confidence, items)) >= 0.85]
    group = max(trusted or groups.values(),
                key=lambda items: (len(items), max(map(result_confidence, items))))
    best = dict(max(group, key=result_confidence))
    best['votes'] = len(group)
    if len(groups) > 1:
        best['needs_review'] = True
    return best


class _HocrWords(HTMLParser):
    """Read entities and nested emphasis without dropping recognized words."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.words = []
        self.current = None
        self.depth = 0
        self.parts = []
        self.line = None
        self.line_depth = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.line is not None:
            self.line_depth += 1
        elif set((values.get('class') or '').split()) & {'ocr_line', 'ocr_caption', 'ocr_header'}:
            self.line = values.get('id')
            self.line_depth = 1
        if self.current is not None:
            self.depth += 1
            return
        if 'ocrx_word' not in (values.get('class') or '').split():
            return
        title = values.get('title') or ''
        bbox = re.search(r'bbox (\d+) (\d+) (\d+) (\d+)', title)
        conf = re.search(r'x_wconf ([\d.]+)', title)
        if bbox:
            self.current = {'bbox': tuple(map(int, bbox.groups())),
                            'confidence': float(conf[1]) / 100 if conf else None}
            if self.line:
                self.current['line_id'] = self.line
            self.depth = 1
            self.parts = []

    def handle_data(self, data):
        if self.current is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.line is not None:
            self.line_depth -= 1
            if self.line_depth == 0:
                self.line = None
        if self.current is None:
            return
        self.depth -= 1
        if self.depth == 0:
            text = ''.join(self.parts).strip()
            if text:
                self.words.append({**self.current, 'text': text})
            self.current = None


# ───────────────────── شناسه‌ها و اعداد (بخش ۷ و ۲۸) ─────────────────────

IDENTIFIER_RE = re.compile(
    r"\b[A-Z]{3,4}[0-9]{7,11}\b|"          # کانتینر ISO: MSCU1234567
    r"\b[A-Z0-9]{2,}[-/][A-Z0-9/-]{1,}\b|"  # INV-2026-004291
    r"\b\d{2,4}[/.-]\d{1,2}[/.-]\d{1,4}\b|"  # تاریخ/شماره‌ها
    r"\b\d[\d,./×\-]{3,}\b"                  # اعداد با جداکننده
)


def find_identifiers(text: str) -> list[str]:
    """شناسه‌های تجاری که نباید تصحیح زبانی شوند (بخش ۷)."""
    return IDENTIFIER_RE.findall(text)


# ───────────────────── موتورها ─────────────────────

class OCREngine:
    name = "base"

    def ocr_image(self, png: bytes, psm: int = 3,
                  whitelist: str | None = None) -> dict:
        raise NotImplementedError


def _iou(a, b) -> float:
    if a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]:
        return 0.0
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    aa = (a[2] - a[0]) * (a[3] - a[1])
    bb = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (aa + bb - inter)


def merge_word_lists(lists: list[list[dict]]) -> list[dict]:
    """ادغام کلمات چندگذر/چندتایل با IoU + متن + اطمینان (بخش ۱۳).

    هم‌متنی با مرکز نزدیک هم تکراری است حتی اگر IoU کم باشد (کلمه‌های تکراری
    «در در» در اسکن کج — دو ناحیهٔ هم‌پوشان با کادر کمی جابه‌جا).
    """
    def center(b):
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

    kept: list[dict] = []
    for w in sorted((w for lst in lists for w in lst),
                    key=lambda d: -(d.get("confidence") or 0)):
        dup = False
        for k in kept:
            iou = _iou(w["bbox"], k["bbox"])
            if iou > 0.6:
                if w["text"] == k["text"] or (w.get("confidence") or 0) <= (k.get("confidence") or 0):
                    dup = True
                else:
                    kept.remove(k)
                break
            # هم‌متنِ چسبیده (IoU کم ولی مرکزها خیلی نزدیک نسبت به ارتفاع)
            if w["text"] == k["text"] and iou > 0.05:
                cx1, cy1 = center(w["bbox"])
                cx2, cy2 = center(k["bbox"])
                h = max(1, k["bbox"][3] - k["bbox"][1])
                if abs(cy1 - cy2) < 0.6 * h and abs(cx1 - cx2) < 1.2 * h:
                    dup = True
                    break
        if not dup:
            kept.append(w)
    kept.sort(key=lambda d: (d["bbox"][1], -d["bbox"][0]))  # RTL: راست به چپ
    return kept


_PERSIAN_ONLY_RE = re.compile(r"[\u0600-\u06FF\u0660-\u0669:/.,\u2010-\u2015\u066B\u066C]+\Z")
_LTR_ANCHOR_RE = re.compile(r"[A-Za-z0-9]")


def _is_ltr_token(text: str) -> bool:
    """توکن LTR (لاتین/ارقام انگلیسی) — داخل جزیرهٔ LTR خوانده می‌شود (بخش ۵).

    توکن‌های فقط‌فارسی/ارقام‌فارسی (مثل ۱۴۰۵/۰۷/۰۴) جزو جریان RTL می‌مانند؛
    ترتیب داخلی خودشان از موتور درست می‌آید.
    """
    t = (text or "").strip()
    if not t or not _LTR_ANCHOR_RE.search(t):
        return False
    if _PERSIAN_ONLY_RE.fullmatch(t):
        return False
    # پرانتز/قوسِ پیوسته به توکن لاتین مجاور (مثل «(A)» انتهای Institute Cargo Clauses)
    stripped = re.sub(r"^[()\[\]]+|[()\[\]]+$", "", t)
    return not _PERSIAN_ONLY_RE.fullmatch(stripped) if stripped != t else True


_NEUTRAL_RE = re.compile(r"[\s:؛،.,!؟?/\-\u2010-\u2015«»()\[\]&|]+\Z")


def _order_bidi_runs(ws: list[dict]) -> list[str]:
    """کلمات به ترتیب RTL؛ هر جزیرهٔ پیوستهٔ LTR معکوس می‌شود تا LTR خوانده شود (بخش ۵).

    در مرتب‌سازی -x هر جزیرهٔ LTR به‌طور طبیعی وارونه می‌آید (ترتیب منطقی جزیره
    صعودیِ x است) → معکوس‌سازی جزیرهٔ پیوسته ترتیب منطقی را برمی‌گرداند:
    «25,450 KG»، «HS Code: 8471.30.00»، «Institute Cargo Clauses (A)».
    توکن‌های خنثی (مثل «:» و «—») جهت جزیرهٔ مجاور را می‌گیرند (قانون BiDi).
    """
    out: list[str] = []
    run: list[str] = []      # جزیرهٔ LTR باز (به ترتیب -x)
    pending: list[str] = []  # توکن‌های خنثی بی‌مقصد
    for w in ws:
        t = w["text"]
        if _is_ltr_token(t):
            if pending:
                (run if run else out).extend(pending)
                pending = []
            run.append(t)
        elif _NEUTRAL_RE.fullmatch(t):
            pending.append(t)
        else:  # توکن RTL
            if run:
                out.extend(reversed(run))
                run = []
            out.extend(pending)
            pending = []
            out.append(t)
    if run:
        out.extend(reversed(run))
    out.extend(pending)
    # توکن‌های شکستهٔ خط‌تیره‌دار (INV- / 2026- / 004291) دوباره به هم می‌چسبند —
    # اتصال قطعیِ توکن‌های همان جزیره، نه تصحیح معنایی (بخش ۲۹)
    merged: list[str] = []
    for tok in out:
        if merged and (merged[-1].endswith("-") or tok.startswith("-")):
            merged[-1] = merged[-1] + tok
        else:
            merged.append(tok)
    return merged


def words_to_lines(words: list[dict], y_tol: float = 0.6) -> list[str]:
    """سطرهای مرتب RTL از کلمات مستطیلی (بخش ۵ و ۲۱) — خوشه‌بندی با مرکز y.

    خوشه‌بندی بر اساس مرکز عمودی کلمه (نه لبهٔ بالا) + مرتب‌سازی نهایی سطرها
    با کمینهٔ y — سطر ۵ دیگر قبل از سطر ۴ نمی‌آید (باگ بنچمارک قبلی).
    """
    if not words:
        return []
    def yc(w): return (w["bbox"][1] + w["bbox"][3]) / 2
    def h_of(w): return max(1, w["bbox"][3] - w["bbox"][1])
    med_h = sorted(h_of(w) for w in words)[len(words) // 2]
    tol = med_h * y_tol
    # Preserve the engine's actual line segmentation. A global median-height
    # tolerance mixes adjacent lines in skewed, tightly typeset newspaper text.
    named: dict[str, list[dict]] = {}
    unassigned = []
    for word in words:
        if word.get('line_id'):
            named.setdefault(word['line_id'], []).append(word)
        else:
            unassigned.append(word)
    clusters: list[list[dict]] = []
    def extent(line):
        return (min(w['bbox'][0] for w in line), min(w['bbox'][1] for w in line),
                max(w['bbox'][2] for w in line), max(w['bbox'][3] for w in line))

    # Separate crops (or sparse-text passes) can describe adjacent pieces of
    # one baseline. Join only nearly disjoint, vertically aligned pieces,
    # never overlapping lines merely because their word centres are close.
    for line in sorted(named.values(), key=lambda line: extent(line)[1]):
        a = extent(line)
        for cluster in clusters:
            b = extent(cluster)
            height = max(1, min(a[3] - a[1], b[3] - b[1]))
            overlap_x = min(a[2], b[2]) - max(a[0], b[0])
            overlap_y = min(a[3], b[3]) - max(a[1], b[1])
            if (overlap_x <= .05 * min(a[2] - a[0], b[2] - b[0])
                    and -overlap_x <= 3 * height
                    and overlap_y >= .4 * height
                    and abs((a[1] + a[3]) - (b[1] + b[3])) <= 1.2 * height):
                cluster.extend(line)
                break
        else:
            clusters.append(list(line))
    sums: list[float] = [sum(map(yc, line)) for line in clusters]
    for w in sorted(unassigned, key=yc):
        for i, ln in enumerate(clusters):
            if abs(yc(w) - sums[i] / len(ln)) <= tol:
                ln.append(w)
                sums[i] += yc(w)
                break
        else:
            clusters.append([w])
            sums.append(yc(w))
    clusters.sort(key=lambda ln: (min(w["bbox"][1] for w in ln),
                                  min(yc(w) for w in ln)))
    out = []
    for ln in clusters:
        ws = sorted(ln, key=lambda d: -d["bbox"][0])  # RTL: راست‌تر اول
        out.append(" ".join(_order_bidi_runs(ws)))
    return out


_ZERO_RUN_RE = re.compile(r"[0۰]{3,}")
# حروف مجاز آشغال لام-الف: ا ل أ إ آ + اعراب (خوانش خرابِ ارقام/لاتین توسط مدل fas)
_AR_GARBAGE_CHARS = "الآأإ\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652\u0670"


def _zero_garbage(text: str) -> bool:
    """خوانش خرابِ صفرمحور (مثل 000000 به‌جای 25,450)."""
    t = (text or "").strip()
    return len(t) >= 5 and bool(_ZERO_RUN_RE.search(t))


def _strip_punct(t: str) -> str:
    return (t or "").strip().strip(".,،؛:!؟?[]()«»{}\"'\u060C")


def _all_in(t: str, chars: str) -> bool:
    return bool(t) and all(ch in chars for ch in t)


_MIXED_DIGIT_RE = re.compile(r"[۰-۹][0-9]|[0-9][۰-۹]")


def _garbage_token(text: str) -> bool:
    """کلمهٔ خراب قطعی: صفرمحور، لام‌الفِ طولانی، یا ترکیب غیرعادی دو سیستم رقم
    (خوانش fas ارقام لاتین را گاهی فارسی‌نما می‌کند مثل ۱۸5011234567 — بخش ۷)."""
    t = _strip_punct(text)
    if _zero_garbage(t):
        return True
    if len(t) >= 6 and _all_in(t, _AR_GARBAGE_CHARS):
        return True
    return bool(_MIXED_DIGIT_RE.search(t))  # ۱۸5 / 8۵ — توکن کاندید ترمیم


def _weak_garbage_token(text: str) -> bool:
    """نامزد ترمیم در سطرِ دارای آشغال قطعی (مثل «لا» یا «الا» تنها)."""
    t = _strip_punct(text)
    return len(t) >= 2 and _all_in(t, _AR_GARBAGE_CHARS)


def needs_latin_repair(words: list[dict]) -> bool:
    """ناحیه نیازمند گذر ترمیم eng است؟ — فقط وقتی خوانش fas آشغال قطعی دارد."""
    return any(_garbage_token(w.get("text", "")) for w in words)


def region_has_latin_context(words: list[dict]) -> bool:
    """ناحیه دارای محتوای لاتین/ارقام لاتین است؟ (آستانهٔ گذر eng — بخش ۷)

    حتی بدون آشغال قطعی، ناحیهٔ لاتین‌محور مستحق گذر دوم eng است (مثلاً
    INV-2026-004291 که fas آن را «۱۱۷۷-2026001» خوانده — آشغال سه‌صفره ندارد).
    """
    for w in words:
        t = _strip_punct(w.get("text", ""))
        if re.search(r"[A-Za-z]{2,}", t) or re.search(r"[0-9]{3,}", t):
            return True
    return False


def _valid_latin_read(t: str) -> bool:
    """خوانش معتبر eng برای جزیرهٔ لاتین: لاتین/رقم و بدون حرف فارسی (بخش ۷)."""
    t = (t or "").strip()
    return bool(t) and bool(re.search(r"[A-Za-z0-9]", t)) and \
        not re.search(r"[\u0600-\u06FF]", t)


_MID_PUNCT_RE = re.compile(r"[.\[\]()«»{}]")


def _replaceable_token(text: str) -> bool:
    """توکن‌های مجازِ تعویض با هم‌پوشان eng در جزیرهٔ کثیف (بخش ۷).

    آشغال قطعی/ضعیف، همه-رقم (ارقام لاتین که fas فارسی‌نما خوانده)، ترکیب دو
    سیستم رقم، و نقطه/قوسِ میانیِ توکن کوتاه (مثل ت)ا — خوانش خرابِ KG).
    """
    t = _strip_punct(text)
    raw = (text or "").strip()
    if _garbage_token(raw) or _weak_garbage_token(raw):
        return True
    if t and all(ch in "0123456789" for ch in t):
        return True  # همه-رقم در جزیرهٔ لاتین = ارقام لاتینِ فارسی‌نما
    if bool(_MIXED_DIGIT_RE.search(t)):
        return True
    if 2 <= len(raw) <= 6 and _MID_PUNCT_RE.search(raw):
        return True
    return False


def merge_latin_secondary(fas_words: list[dict],
                          eng_words: list[dict]) -> tuple[list[dict], int]:
    """ادغام گذر eng خالص با گذر fas روی جزیره‌های لاتین (بخش ۷ — استخراجی).

    - هر توکن جایگزین‌پذیرِ fas با هم‌پوشان eng تعویض می‌شود فقط اگر خوانش eng
      لاتین/رقم معتبر باشد؛ وگرنه آشغال قطعی → needs_review (بخش ۲۷)
    - واژهٔ eng بی‌همتا فقط با اطمینان بالا اضافه می‌شود (خوانش eng از گلیف
      فارسی نویز لاتین کم‌اطمینان تولید می‌کند — فیلتر اطمینان)
    """
    out: list[dict] = []
    eng_left = list(eng_words)
    consumed = [False] * len(eng_left)
    repaired = 0
    island = any(_garbage_token(w.get("text", "")) for w in fas_words)
    for w in fas_words:
        raw = w.get("text", "")
        hard = _garbage_token(raw)
        repl = island and _replaceable_token(raw)
        if repl:
            best_j, best_iou = None, 0.0
            for j, e in enumerate(eng_left):
                if consumed[j]:
                    continue
                iou = _iou(w["bbox"], e["bbox"])
                if iou > best_iou:
                    best_iou, best_j = iou, j
            if best_j is not None and best_iou > 0.2 \
                    and _valid_latin_read(eng_left[best_j]["text"]):
                nw = dict(eng_left[best_j])
                nw["bbox"] = w["bbox"]
                if w.get('line_id'):
                    nw['line_id'] = w['line_id']
                nw['raw_ocr_text'] = w.get('raw_ocr_text', raw)
                nw['recognition_repaired'] = True
                nw["engine"] = "tesseract+eng"
                nw.pop("needs_review", None)
                out.append(nw)
                consumed[best_j] = True
                repaired += 1
                continue
            nw = dict(w)
            if hard:
                nw["needs_review"] = True  # بخش ۲۷: آشغال حل‌نشده پنهان نمی‌شود
            out.append(nw)
            continue
        out.append(w)
    # واژه‌های eng بی‌همتا — لاتین/رقمِ تمیزِ با اطمینانِ بالا. هم‌مکانِ توکن
    # ضعیف‌تر fas → جایگزینی مبتنی بر شواهد (eng بهتر از fas)؛ بی‌همتا → افزودن
    # (نویز eng روی حروف فارسی کم‌اطمینان است و قبلاً فیلتر شده)
    changes = repaired
    for j, e in enumerate(eng_left):
        if consumed[j] or not _valid_latin_read(e.get("text", "")):
            continue
        if (e.get("confidence") or 0) < 0.75:
            continue
        best_i, best_iou = None, 0.0
        for i, w in enumerate(out):
            if w.get("engine") == "tesseract+eng":
                continue
            iou = _iou(e["bbox"], w["bbox"])
            if iou > best_iou:
                best_iou, best_i = iou, i
        if best_i is not None and best_iou > 0.3:
            w = out[best_i]
            # فقط توکن جایگزین‌پذیر عوض می‌شود — توکن فارسیِ سالم هرگز با
            # خوانش eng (هرچند بااطمینان) تعویض نمی‌شود (بخش ۲۹)
            if _replaceable_token(w.get("text", "")):
                ne = dict(e)
                if w.get('line_id'):
                    ne['line_id'] = w['line_id']
                ne['raw_ocr_text'] = w.get('raw_ocr_text', w['text'])
                ne['recognition_repaired'] = True
                ne["engine"] = "tesseract+eng"
                out[best_i] = ne
                changes += 1
            continue
        if best_iou <= 0.3:
            ne = dict(e)
            ne["engine"] = "tesseract+eng"
            out.append(ne)
            changes += 1
    return out, changes


class GoogleVisionEngine(OCREngine):
    """Disabled compatibility symbol. No network implementation or credentials retained."""
    name = "disabled_external_ocr"
    available = False

    def __init__(self, api_key: str):
        pass

    def ocr_image(self, *_args, **_kwargs):
        raise RuntimeError("External document APIs are disabled")


class TesseractEngine(OCREngine):
    name = "tesseract"

    def __init__(self, langs: str = "fas", *, reuse_model: bool = False):
        # fas تنها پیش‌فرض — eng در فارسی نویز حرف تولید می‌کند (eng اختیاری با --langs)
        self.langs = langs
        self._docker_ok: bool | None = None
        self._native_ok: bool | None = None
        # Each engine belongs to one document. Never retain customer text in a
        # global cache, and return copies because callers adjust word geometry.
        self._cache: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
        self._cache_bytes = 0
        self.cache_enabled = os.environ.get("OCR_DISABLE_CACHE") != "1"
        self._cache_lock = threading.Lock()
        self._processes = 0
        self._cache_hits = 0
        self._reuse_model = reuse_model
        self._pool = None
        self._pool_disabled = False
        self._pool_lock = threading.Lock()
        self._pool_starts = 0
        self._native_calls = 0
        self.tessdata_dir = Path(os.environ.get("OCR_TESSDATA_DIR") or
                                 Path(__file__).resolve().parent.parent / "tessdata")
        candidates = [os.environ.get("TESSERACT_CMD", ""), "tesseract",
                      r"C:\Tools\GomrokOCR\Tesseract\tesseract.exe",
                      r"C:\Program Files\Tesseract-OCR\tesseract.exe"]
        self.executable = next((path for candidate in candidates
                                if candidate and (path := shutil.which(candidate))), None)

    @property
    def workers(self):
        return min(4, max(1, int(os.environ.get("OCR_NATIVE_WORKERS", (os.cpu_count() or 1) // 2))))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        with self._pool_lock:
            pool, self._pool = self._pool, None
            self._pool_disabled = True
        if pool is not None:
            pool.close()
        with self._cache_lock:
            self._cache.clear()
            self._cache_bytes = 0

    def _pooled_read(self, png, psm, whitelist, language):
        if not self._reuse_model or not NativePool.supported(self.executable):
            return None
        pool = None
        try:
            with self._pool_lock:
                if self._pool_disabled:
                    return None
                if self._pool is None:
                    self._pool = NativePool(Path(self.executable).resolve().parent,
                                            self.tessdata_dir.resolve(), workers=self.workers)
                    self._pool_starts += self._pool.workers
                pool = self._pool
            payload = pool.recognize(png, psm, whitelist, language)
            with self._cache_lock:
                self._native_calls += 1
            return payload
        except (OSError, RuntimeError, ValueError, EOFError, TimeoutError):
            with self._pool_lock:
                self._pool_disabled = True
                self._pool = None
            if pool is not None:
                pool.close()
            logging.getLogger(__name__).warning('Native OCR worker unavailable; using CLI fallback')
            return None

    def native_ok(self) -> bool:
        if self._native_ok is None:
            self._native_ok = False
            if self.executable and all((self.tessdata_dir / f"{lang}.traineddata").is_file()
                                       for lang in self.langs.split("+")):
                try:
                    result = subprocess.run([self.executable, "--version"],
                                            capture_output=True, timeout=10)
                    self._native_ok = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    self._native_ok = False
        return self._native_ok

    def available(self) -> bool:
        return self.native_ok() or self.docker_ok()

    def docker_ok(self) -> bool:
        if self._docker_ok is None:
            try:
                p = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
                self._docker_ok = p.returncode == 0
            except Exception:  # noqa: BLE001
                self._docker_ok = False
        return self._docker_ok

    def crop_for_repair(self, x0: int, y0: int, x1: int, y1: int) -> bytes | None:
        """برش واژهٔ خراب از مستر + پد نسبی + مقیاس بهینه (بخش ۷ — ترمیم eng).

        پد عمودی نسبی (ارتفاع گلیف در 2000DPI صدها پیکسل است؛ پد ثابت کوچک
        حرف را قطع می‌کند و psm=7 آشغال می‌دهد)؛ پد افقی کوچک تا همسایه نیاید.
        """
        try:
            import cv2  # موضعی — وابستگی سنگین برای موتورها نیست
            from . import preprocess as P
            h, w = y1 - y0, x1 - x0
            py = max(30, int(0.45 * h))
            px = max(20, int(0.05 * w))
            crop = self._repair_source[max(0, y0 - py):y1 + py,
                                       max(0, x0 - px):x1 + px]
            if crop.size == 0:
                return None
            f = P.optimal_ocr_scale(crop)
            s = P.pad_white(P.resize_by_factor(crop, f))
            ok, buf = cv2.imencode(".png", s)
            return buf.tobytes() if ok else None
        except Exception:
            return None

    def ocr_image(self, png: bytes, psm: int = 3,
                  whitelist: str | None = None, langs: str | None = None) -> dict:
        langs_eff = langs or self.langs
        key = (hashlib.sha256(png).digest(), psm, whitelist, langs_eff)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self._cache_hits += 1
                return copy.deepcopy(cached[0])
        native = self.native_ok()
        if not native and not self.docker_ok():
            raise RuntimeError("هیچ موتور محلی OCR در دسترس نیست")
        payload = self._pooled_read(png, psm, whitelist, langs_eff) if native else None
        if payload is None:
            payload = self._ocr_subprocess(png, psm, whitelist, langs_eff, native)
        text, hocr = payload
        words = self._parse_hocr(hocr)
        confs = [w["confidence"] for w in words if w.get("confidence") is not None]
        result = {"text": text, "words": words,
                  "confidence": sum(confs) / len(confs) if confs else None,
                  "engine": "tesseract", "psm": psm}
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        if self.cache_enabled and size <= 256 * 1024:
            with self._cache_lock:
                previous = self._cache.pop(key, None)
                if previous:
                    self._cache_bytes -= previous[1]
                self._cache[key] = (copy.deepcopy(result), size)
                self._cache_bytes += size
                while len(self._cache) > 128 or self._cache_bytes > 4 * 1024 * 1024:
                    _, (_, removed_size) = self._cache.popitem(last=False)
                    self._cache_bytes -= removed_size
        return result

    def _ocr_subprocess(self, png, psm, whitelist, langs_eff, native):
        # گذر دوم لاتین‌محور: زبان متفاوت (مثل fas+eng) فقط در نواحی لاتین‌محور (بخش ۷)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(png)
            tmp_in = tf.name
        outdir = tempfile.mkdtemp()
        base = os.path.join(outdir, "o")
        try:
            if native:
                cmd = [self.executable, tmp_in, base]
                data_path = str(self.tessdata_dir.resolve())
            else:
                cmd = ["docker", "run", "--rm", "--pull=never",
                       "-v", f"{tmp_in}:/tmp/page.png:ro",
                       "-v", f"{outdir}:/io",
                       "-v", f"{self.tessdata_dir.resolve()}:/tessdata:ro",
                       TESSERACT_IMAGE, "tesseract", "/tmp/page.png", "/io/o"]
                data_path = "/tessdata"
            cmd += ["-l", langs_eff, "--psm", str(psm),
                   "--tessdata-dir", data_path,
                   "-c", "tessedit_create_txt=1",
                   "-c", "tessedit_create_hocr=1",
                   "-c", "tessedit_do_invert=0"]  # متن تیره روی زمینهٔ روشن (متن معکوس ناخواسته)
            if whitelist:
                if "\n" in whitelist or "\r" in whitelist:
                    raise ValueError("Invalid OCR character whitelist")
                if native:
                    # Windows builds may encode argv using a legacy code page.
                    # Keep Persian configuration in UTF-8 instead of argv.
                    config = Path(outdir) / "whitelist.cfg"
                    config.write_text(f"tessedit_char_whitelist {whitelist}\n", encoding="utf-8")
                    cmd.append(str(config))
                else:
                    cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
            with self._cache_lock:
                self._processes += 1
            p = subprocess.run(cmd, capture_output=True, timeout=900)
            if p.returncode != 0:
                raise RuntimeError("tesseract: " + p.stderr.decode(errors="replace")[:300])
            text = Path(base + ".txt").read_text("utf-8")
            hocr = Path(base + ".hocr").read_text("utf-8") if Path(base + ".hocr").is_file() else ""
            return text, hocr
        finally:
            os.unlink(tmp_in)
            shutil.rmtree(outdir, ignore_errors=True)

    def statistics(self) -> dict[str, int]:
        with self._cache_lock:
            return {"ocr_processes": self._processes + self._pool_starts,
                    "ocr_cache_hits": self._cache_hits,
                    **({'ocr_native_calls': self._native_calls} if self._reuse_model else {})}

    @staticmethod
    def _parse_hocr(hocr_html: str) -> list[dict]:
        parser = _HocrWords()
        parser.feed(hocr_html)
        parser.close()
        return parser.words

    # PSM بر اساس نوع ناحیه (بخش ۱۶) + PSMهای بازیابی
    PSM_FOR = {"text": 3, "framed_text": 6, "table_cell": 12, "numeric": 7, "glyph": 10, "glyph_verify": 10,
               "table": 11, "sparse": 11, "line": 7, "raw_line": 13}
    RETRY_PSM = {"text": (11, 6), "framed_text": (11, 3), "table_cell": (3, 6), "numeric": (12, 6),
                 "glyph": (7, 6), "raw_line": (), "glyph_verify": ()}


def pick_engine(engines: list[OCREngine]) -> OCREngine:
    """اولین موتور در دسترس."""
    for e in engines:
        if isinstance(e, GoogleVisionEngine) and not e.available:
            continue
        if isinstance(e, TesseractEngine) and not e.available():
            continue
        return e
    raise RuntimeError("هیچ موتور OCR در دسترسی نیست")


def ocr_any(engines: list[OCREngine], png: bytes, region_kind: str = "text",
            variants: list[bytes] | None = None,
            thresholds: tuple = (CONF_ACCEPT, CONF_RETRY),
            whitelist: str | None = None,
            langs_override: str | None = None) -> dict:
    """OCR با fallback چندموتوره + بازیابی چندگذره (بخش ۱۴/۱۵/۱۹).

    موتور شکست‌خورده → موتور بعدی؛ اطمینان کم → گذر با PSM/variant جایگزین.
    """
    accept, retry = thresholds
    primary_psm = TesseractEngine.PSM_FOR.get(region_kind, 3)
    best: dict | None = None
    extra_passes = 0  # شمارش کامل: هر گذر اضافه/موتور دوم = fallback (بخش ۱۵)

    def score(r: dict) -> float:
        c = r.get("confidence")
        if c is not None:
            return float(c)
        return 0.5 if (r.get("text") or "").strip() else 0.0

    def better(r: dict) -> bool:
        if best is None:
            return True
        if (region_kind in ('text', 'framed_text') and not whitelist
                and len(best.get('words') or []) >= 20 and len(r.get('words') or []) >= 20):
            before = sum(char.isalnum() for char in best.get('text', ''))
            after = sum(char.isalnum() for char in r.get('text', ''))
            # Sparse mode can report a higher mean confidence simply by
            # omitting a difficult line. Require both coverage and a meaningful
            # confidence gain before replacing a dense paragraph with it.
            if r.get('psm') == 11:
                return after >= .98 * before and score(r) >= score(best) + .05
            # Block mode may retain the omitted prose at nearly the same
            # confidence. Never prefer a long, low-confidence hallucination.
            if (r.get('psm') == 6 and score(r) >= .65
                    and score(r) >= score(best) - .025 and after >= 1.02 * before):
                return True
        return score(r) > score(best) + 1e-9

    for eng in engines:
        if isinstance(eng, GoogleVisionEngine) and not eng.available:
            continue
        try:
            kw = {"langs": langs_override} if isinstance(eng, TesseractEngine) else {}
            r = eng.ocr_image(png, psm=primary_psm, whitelist=whitelist, **kw)
        except Exception:
            continue
        if better(r):
            best = r
        if (score(best) >= accept) and (best.get("text") or "").strip():
            break
        # بازیابی: PSMهای جایگزین مخصوص نوع ناحیه (فقط Tesseract) — بخش ۱۴
        if isinstance(eng, TesseractEngine):
            for alt_psm in TesseractEngine.RETRY_PSM.get(region_kind, (11, 6)):
                if alt_psm == primary_psm:
                    continue
                try:
                    r2 = eng.ocr_image(png, psm=alt_psm, whitelist=whitelist, **kw)
                except Exception:
                    continue
                extra_passes += 1
                if better(r2):
                    best, best["multipass"] = r2, True
                if score(best) >= accept and (best.get("text") or "").strip():
                    break
        # گذر روی variantهای پیش‌پردازش (فقط وقتی هنوز ضعیف است) — بخش ۱۴
        if score(best) < accept and variants:
            for v in variants[:2]:
                try:
                    r3 = eng.ocr_image(v, psm=primary_psm, whitelist=whitelist, **kw)
                except Exception:
                    continue
                extra_passes += 1
                if better(r3):
                    best, best["multipass"] = r3, True
                if score(best) >= accept and (best.get("text") or "").strip():
                    break
        if score(best) >= accept:
            break
    if best is None:
        raise RuntimeError("همهٔ موتورها شکست خوردند")
    if extra_passes:
        best["extra_passes"] = extra_passes
    return best


def ocr_many(engines: list[OCREngine], images: list[bytes],
             region_kind: str = "text", whitelist: str | None = None,
             langs_override: str | None = None) -> list[dict | None]:
    """Run independent candidates in input order without dropping any passes.

    Only native, local OCR is parallelized. Docker and cloud retain their
    sequential behavior. At most four subprocesses run, even on large servers.
    """
    def recognize(image: bytes) -> dict | None:
        try:
            return ocr_any(engines, image, region_kind=region_kind, whitelist=whitelist,
                           langs_override=langs_override)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            return None

    workers = min(getattr(engines[0], "workers", 1), len(images)) if engines else 0
    if (workers < 2 or len(engines) != 1 or
            not isinstance(engines[0], TesseractEngine) or not engines[0].native_ok()):
        return [recognize(image) for image in images]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr-candidate") as pool:
        return list(pool.map(recognize, images))
