# -*- coding: utf-8 -*-
"""ساخت اسناد آزمون واقعی + ground truth (بخش ۲۶ و ۳۰).

چهار سند:
  bench_persian — سند فارسی معمولی (پاراگراف، تاریخ، عدد)
  bench_table   — سند جدول‌محور
  bench_mixed   — مخلوط فارسی/انگلیسی + شناسه‌ها + ارقام دو سیستم
  bench_lowq    — اسکن کم‌کیفیت (نویز + کجی + کنتراست پایین)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import arabic_reshaper
import numpy as np
import pymupdf
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/GeezaPro.ttc"
# GeezaPro حروف/ارقام لاتین ندارد (notdef=جعبهٔ توخالی رسم می‌کند!) —
# سطرهای مخلوط باید رندر دوموتوره شوند: اجراهای لاتین با فونت لاتین
LATIN_FONT = "/System/Library/Fonts/Helvetica.ttc"
_AR_RUN_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def rtl(t: str) -> str:
    return get_display(arabic_reshaper.reshape(t))


def draw_mixed_line(d: ImageDraw.ImageDraw, text: str, size: int,
                    y_baseline: int, x_right: int) -> None:
    """رندر دوموتورهٔ سطر مخلوط RTL: اجراهای عربی با GeezaPro، لاتین با Helvetica.

    ورودی: متن منطقی؛ پس از reshape+bidi به ترتیب بصری (چپ→راست) تفکیک و
    هر اجرا با فونت خودش روی خط پایهٔ مشترک رسم می‌شود — ارقام/کدهای لاتین
    واقعاً به‌صورت گلیف لاتین در PDF می‌نشینند (قابل OCR)، نه جعبهٔ notdef.
    """
    vis = rtl(text)
    runs: list[tuple[bool, list[str]]] = []
    for ch in vis:
        ar = bool(_AR_RUN_RE.match(ch))
        if runs and runs[-1][0] == ar:
            runs[-1][1].append(ch)
        else:
            runs.append((ar, [ch]))
    fonts = [ImageFont.truetype(FONT if ar else LATIN_FONT, size)
             for ar, _ in runs]
    total_w = sum(d.textlength("".join(chars), font=f)
                  for (_, chars), f in zip(runs, fonts))
    x = x_right - total_w
    for (_, chars), f in zip(runs, fonts):
        s = "".join(chars)
        d.text((x, y_baseline), s, font=f, fill="black", anchor="ls")
        x += d.textlength(s, font=f)


def draw_mixed_lines(d: ImageDraw.ImageDraw, lines: list[dict], y: int,
                     font_size: int = 40, x_right: int = 1900) -> int:
    """سطرهای مخلوط — y از بالا؛ خط پایه ≈ y + 0.8×size."""
    for ln in lines:
        size = ln.get("size", font_size)
        draw_mixed_line(d, ln["text"], size,
                        y + int(size * 0.8), x_right)
        y += size + ln.get("gap", 28)
    return y


def draw_lines(d: ImageDraw.ImageDraw, lines: list[dict], y: int,
               font_size: int = 40, x_right: int = 1900) -> int:
    """رسم سطرها از راست (RTL) — هر سطر: {text, size?, gap?}."""
    for ln in lines:
        size = ln.get("size", font_size)
        f = ImageFont.truetype(FONT, size)
        d.text((x_right, y), rtl(ln["text"]), font=f, fill="black",
               anchor="ra")
        y += size + ln.get("gap", 28)
    return y


def to_pdf(img: Image.Image, name: str) -> None:
    tmp = f"/tmp/{name}.png"
    img.save(tmp)
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(0, 0, 595, 842), filename=tmp)
    pdf.save(f"input/{name}.pdf")
    pdf.close()
    print(f"built input/{name}.pdf")


# ───────────── ۱) سند فارسی معمولی ─────────────
PERSIAN_LINES = [
    {"text": "گزارش وضعیت انبار مرکزی", "size": 56, "gap": 60},
    {"text": "در سال جاری تعداد کالاهای ورودی به انبار مرکزی شرکت با رشد چشمگیری مواجه بوده است."},
    {"text": "بر اساس آمار مورخ ۱۴۰۵/۰۷/۰۴ مجموع تعداد اقلام موجود به ۱۲۳٬۴۵۶ عدد رسیده است."},
    {"text": "ارزش ریالی موجودی انبار بالغ بر ۴٬۵۰۰٬۰۰۰٬۰۰۰ ریال برآورد می‌گردد."},
    {"text": "لازم به ذکر است اقلام فاسدشدنی باید پیش از پایان مهلت سه‌ماهه توزیع شوند."},
    {"text": "مدیریت محترم انبار موظف است گزارش ماهانه را حداکثر تا پنجم هر ماه ارسال نماید."},
    {"text": "شماره پرونده: ۱۴۰۵/۲۳۴۵/۶۷ — کد انبار: ۸۹۰۱"},
]

# ───────────── ۲) سند جدول‌محور ─────────────
TABLE_DATA = [
    ["ردیف", "نام کالا", "تعداد", "مبدأ"],
    ["۱", "قطعات خودرو", "۱۲۰", "تهران"],
    ["۲", "لوازم برقی", "۸۵", "اصفهان"],
    ["۳", "کاشی و سرامیک", "۲۰۰", "یزد"],
    ["۴", "مبل خانگی", "۴۵", "تبریز"],
]


def make_table_page() -> Image.Image:
    img = Image.new("RGB", (2000, 1200), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 34)
    title = "جدول گزارش بارگیری کالا"
    w = d.textlength(rtl(title), font=f)
    d.text(((2000 - w) / 2, 60), rtl(title), font=f, fill="black")
    cols_x = [200, 700, 1200, 1600, 1800]
    rows_y = [200, 300, 400, 500, 600, 700]
    for r, row in enumerate(TABLE_DATA):
        for c, txt in enumerate(row):
            cx, cy = (cols_x[c] + cols_x[c + 1]) / 2, (rows_y[r] + rows_y[r + 1]) / 2
            tw = d.textlength(rtl(txt), font=f)
            d.text((cx - tw / 2, cy - 22), rtl(txt), font=f, fill="black")
    for x in cols_x:
        d.line([(x, rows_y[0]), (x, rows_y[-1])], fill="black", width=4)
    for y in rows_y:
        d.line([(cols_x[0], y), (cols_x[-1], y)], fill="black", width=4)
    return img


# ───────────── ۳) سند مخلوط فارسی/انگلیسی ─────────────
MIXED_LINES = [
    {"text": "بارنامهٔ حمل بین‌المللی", "size": 56, "gap": 60},
    {"text": "شماره کانتینر: MSCU1234567", "size": 44},
    {"text": "وزن ناخالص: 25,450 KG", "size": 44},
    {"text": "HS Code: 8471.30.00", "size": 44},
    {"text": "Invoice: INV-2026-004291", "size": 44},
    {"text": "تاریخ صدور: ۱۴۰۵/۰۷/۰۴", "size": 44},
    {"text": "مبدأ: بندرعباس — مقصد: Rotterdam", "size": 44},
    {"text": "بیمهٔ بار: مطابق Institute Cargo Clauses (A)", "size": 44},
]

# ───────────── ساخت اسناد ─────────────


def build_persian() -> None:
    img = Image.new("RGB", (2000, 1400), "white")
    d = ImageDraw.Draw(img)
    draw_lines(d, PERSIAN_LINES, 100)
    to_pdf(img, "bench_persian")


def build_table() -> None:
    to_pdf(make_table_page(), "bench_table")


def build_mixed() -> None:
    img = Image.new("RGB", (2000, 1400), "white")
    d = ImageDraw.Draw(img)
    # سطر عنوان فارسی خالص با draw_lines؛ بقیهٔ سطرهای مخلوط دوموتوره
    draw_lines(d, MIXED_LINES[:1], 100)
    draw_mixed_lines(d, MIXED_LINES[1:], 100 + MIXED_LINES[0]["size"] + MIXED_LINES[0]["gap"],
                     font_size=44)
    to_pdf(img, "bench_mixed")


def build_lowq() -> None:
    """اسکن کم‌کیفیت: همان سند فارسی + نویز + کجی + کنتراست پایین."""
    img = Image.new("RGB", (2000, 1400), "white")
    d = ImageDraw.Draw(img)
    draw_lines(d, PERSIAN_LINES, 100)
    arr = np.asarray(img).astype(np.float32)
    # کنتراست پایین + زمینهٔ خاکستری
    arr = arr * 0.55 + 90
    # نویز گاوسی
    rng = np.random.default_rng(42)
    arr += rng.normal(0, 22, arr.shape)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    img2 = Image.fromarray(arr)
    # کجی ۱٫۶ درجه
    img2 = img2.rotate(1.6, expand=True, fillcolor=(150, 150, 150),
                       resample=Image.BICUBIC)
    # کاهش وضوح (اسکن بی‌کیفیت)
    img2 = img2.resize((img2.width // 3, img2.height // 3), Image.BILINEAR)
    img2 = img2.resize((img2.width * 3, img2.height * 3), Image.BILINEAR)
    to_pdf(img2, "bench_lowq")


def main() -> None:
    Path("input").mkdir(exist_ok=True)
    build_persian()
    build_table()
    build_mixed()
    build_lowq()
    gt = {
        "bench_persian": {
            "text": "\n".join(ln["text"] for ln in PERSIAN_LINES),
        },
        "bench_table": {
            "text": "جدول گزارش بارگیری کالا",
            "table_rows": TABLE_DATA,
        },
        "bench_mixed": {
            "text": "\n".join(ln["text"] for ln in MIXED_LINES),
        },
        "bench_lowq": {
            "text": "\n".join(ln["text"] for ln in PERSIAN_LINES),
        },
    }
    Path("input/ground_truth.json").write_text(
        json.dumps(gt, ensure_ascii=False, indent=2), "utf-8")
    print("built input/ground_truth.json")


if __name__ == "__main__":
    main()
