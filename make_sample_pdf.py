# -*- coding: utf-8 -*-
"""ساخت PDF آزمایشی فارسی برای تست پایپ‌لاین OCR (بدون نیاز به اینترنت)."""
import pymupdf

doc = pymupdf.open()
page = doc.new_page(width=595, height=842)  # A4

text = (
    "این یک متن آزمایشی فارسی است.\n"
    "ربات OCR باید این سطرها را استخراج کند.\n"
    "اعداد: ۱۲۳۴۵ و ۶۷۸۹۰\n"
    "سطح اطمینان و ساختار JSON بررسی می‌شود.\n"
)

# فونت فارسی سیستمی (macOS / Windows / Linux)
for fontpath in (
    "/System/Library/Fonts/GeezaPro.ttc",  # فونت عربی/فارسی مک
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
):
    try:
        page.insert_textbox(
            pymupdf.Rect(72, 72, 523, 500),
            text,
            fontname="fa",
            fontfile=fontpath,
            fontsize=14,
            align=pymupdf.TEXT_ALIGN_RIGHT,
        )
        print("فونت استفاده‌شده:", fontpath)
        break
    except Exception:
        continue
else:
    raise SystemExit("فونت مناسبی پیدا نشد")

doc.save("input/sample_persian.pdf")
print("ساخته شد: input/sample_persian.pdf")
