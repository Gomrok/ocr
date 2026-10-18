#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ربات OCR فارسی — Production Pipeline (مستر 2000 DPI)

مثال:
    python ocr_bot.py file.pdf
    python ocr_bot.py input/ --dpi 2000
    python ocr_bot.py input/ --tesseract-only
    python ocr_bot.py input/ --benchmark input/ground_truth.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ocr_pipeline.pipeline import process_pdf  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="ocr_bot",
                                description="OCR فارسی Production — مستر 2000DPI")
    p.add_argument("inputs", nargs="*", help="PDF یا پوشه (پیش‌فرض: input/)")
    p.add_argument("--dpi", type=int, default=2000, help="DPI مستر (پیش‌فرض 2000)")
    p.add_argument("--out", default="output", help="پوشهٔ خروجی")
    p.add_argument("--max-pages", type=int, default=0)
    p.add_argument("--api-key", default=None)
    p.add_argument("--langs", default="fas",
                   help="زبان‌های Tesseract مثل fas یا fas+eng (پیش‌فرض fas)")
    p.add_argument("--tesseract-only", action="store_true",
                   help="فقط Tesseract محلی (بدون گوگل)")
    p.add_argument("--no-debug", action="store_true", help="بدون تصویر دیباگ")
    p.add_argument("--benchmark", default=None,
                   help="فایل JSON ground truth برای محاسبهٔ CER/WER")
    return p.parse_args(argv)


def collect_pdfs(inputs):
    pdfs = []
    for raw in inputs or (["input"] if Path("input").is_dir() else []):
        path = Path(raw).expanduser()
        if path.is_dir():
            pdfs.extend(sorted(f for f in path.glob("*.pdf") if f.is_file()))
        elif path.is_file() and path.suffix.lower() == ".pdf":
            pdfs.append(path)
    return pdfs


def run_benchmark(out_dir: str, gt_path: str):
    from ocr_pipeline.benchmark import (cer, numeric_accuracy, table_accuracy, wer)
    gt = json.loads(Path(gt_path).read_text("utf-8"))
    # فرمت dict: {stem: {text, table_rows}} — یا لیست قدیمی [{file, text}]
    items = ([{"file": k, **v} for k, v in gt.items()]
             if isinstance(gt, dict) else gt)
    print("\n📊 بنچمارک دقت (بخش ۲۶):")
    agg = {"cer": [], "wer": [], "num": [], "cell": []}
    for item in items:
        stem = item["file"]
        base = Path(out_dir, stem)
        # CER روی متن JSON (بازسازی کلمه‌محور) — TXT جداکنندهٔ صفحه و جدول دارد
        jf = base / f"{stem}.json"
        if not jf.is_file():
            print(f"  {stem}: خروجی یافت نشد")
            continue
        d = json.loads(jf.read_text("utf-8"))
        hyp = "\n".join(p.get("text", "") for p in d.get("pages", []))
        ref = item["text"]
        c, w = cer(ref, hyp), wer(ref, hyp)
        n = numeric_accuracy(ref, hyp)
        agg["cer"].append(c)
        agg["wer"].append(w)
        agg["num"].append(n)
        line = f"  {stem}: CER={c:.3f} WER={w:.3f} NumericAcc={n:.3f}"
        if item.get("table_rows"):
            tj = base / "tables" / "table_001.json"
            if tj.is_file():
                from ocr_pipeline.tables import rows_of
                got_rows = rows_of(json.loads(tj.read_text("utf-8")))
                ta = table_accuracy(item["table_rows"], got_rows)
                agg["cell"].append(ta["cell_accuracy"])
                line += (f" | TableCell={ta['cell_accuracy']:.3f} "
                         f"Row={ta['row_accuracy']:.3f} "
                         f"Struct={ta['structure_accuracy']:.3f}")
            else:
                line += " | جدولی استخراج نشد"
        print(line)
    if agg["cer"]:
        print(f"\n  میانگین: CER={sum(agg['cer'])/len(agg['cer']):.3f} "
              f"WER={sum(agg['wer'])/len(agg['wer']):.3f} "
              f"NumericAcc={sum(agg['num'])/len(agg['num']):.3f}"
              + (f" TableCellAcc={sum(agg['cell'])/len(agg['cell']):.3f}"
                 if agg["cell"] else ""))
    return agg


def main(argv=None) -> int:
    args = parse_args(argv)
    pdfs = collect_pdfs(args.inputs)
    if not pdfs:
        print("هیچ PDFی پیدا نشد.", file=sys.stderr)
        return 1
    print(f"{len(pdfs)} فایل PDF — مستر {args.dpi}DPI\n")

    for pdf in pdfs:
        out_dir = Path(args.out) / pdf.stem
        print(f"▶ {pdf.name} → {out_dir}/")
        payload = process_pdf(
            pdf, out_dir, dpi=args.dpi, api_key=args.api_key,
            force_tesseract=args.tesseract_only,
            max_pages=args.max_pages, debug=not args.no_debug, langs=args.langs)
        perf = payload.get("performance", {})
        print(f"  ✅ {perf.get('avg_page_seconds', '?')}s/صفحه، "
              f"RAM {perf.get('peak_rss_mb', '?')}MB، "
              f"جدول‌ها {sum(len(p.get('tables', [])) for p in payload['pages'])}، "
              f"کم‌اطمینان {perf.get('low_confidence_regions', 0)}، "
              f"خطاها {perf.get('failed_pages', 0)}")

    if args.benchmark:
        run_benchmark(args.out, args.benchmark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
