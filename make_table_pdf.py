# -*- coding: utf-8 -*-
"""ساخت PDF جدول‌دار فارسی با PIL (شکل‌دهی درست RTL) برای تست تشخیص جدول."""
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display
import pymupdf


def rtl(t: str) -> str:
    return get_display(arabic_reshaper.reshape(t))


F = ImageFont.truetype("/System/Library/Fonts/GeezaPro.ttc", 34)
img = Image.new("RGB", (2000, 1400), "white")
d = ImageDraw.Draw(img)

title = "جدول گزارش بارگیری کالا — نمونه آزمایشی"
w = d.textlength(rtl(title), font=F)
d.text(((2000 - w) / 2, 60), rtl(title), font=F, fill="black")

# شبکه جدول: 4 ستون × 5 سطر (با سربرگ)
cols_x = [200, 700, 1200, 1600, 1800]  # مرزهای عمودی
rows_y = [200, 300, 400, 500, 600, 700]  # مرزهای افقی
LW = 4

data = [
    ["ردیف", "نام کالا", "تعداد", "مبدأ"],
    ["۱", "قطعات خودرو", "۱۲۰", "تهران"],
    ["۲", "لوازم برقی", "۸۵", "اصفهان"],
    ["۳", "کاشی و سرامیک", "۲۰۰", "یزد"],
    ["۴", "مبل خانگی", "۴۵", "تبریز"],
]

def cell_center(r, c):
    x0, x1 = cols_x[c], cols_x[c + 1]
    y0, y1 = rows_y[r], rows_y[r + 1]
    return (x0 + x1) / 2, (y0 + y1) / 2

for r, row in enumerate(data):
    for c, txt in enumerate(row):
        cx, cy = cell_center(r, c)
        tw = d.textlength(rtl(txt), font=F)
        d.text((cx - tw / 2, cy - 22), rtl(txt), font=F, fill="black")

for x in cols_x:
    d.line([(x, rows_y[0]), (x, rows_y[-1])], fill="black", width=LW)
for y in rows_y:
    d.line([(cols_x[0], y), (cols_x[-1], y)], fill="black", width=LW)

img.save("/tmp/table_fa.png")

# تبدیل به PDF
pdf = pymupdf.open()
page = pdf.new_page(width=595, height=842)
page.insert_image(pymupdf.Rect(0, 0, 595, 842), filename="/tmp/table_fa.png")
pdf.save("input/sample_table.pdf")
print("built input/sample_table.pdf")
