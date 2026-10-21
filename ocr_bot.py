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
import os
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
    p.add_argument("--mode", choices=("legacy", "compare", "candidate"), default=None,
                   help="مسیر فعلی، مقایسهٔ جداگانه یا مسیر آزمایشی")
    p.add_argument("--profile", choices=("persian", "mixed", "english"), default="persian")
    p.add_argument("--candidate-dpi", type=int, choices=(300, 400), default=300)
    p.add_argument("--temp-dir", type=Path, default=None, help="مسیر موقت صریح برای مسیر آزمایشی")
    p.add_argument("--handwriting", action="store_true", help="اعلام دست‌نویس؛ تأیید انسانی پیش از جست‌وجو الزامی است")
    return p.parse_args(argv)


def collect_pdfs(inputs, *, images=False):
    pdfs = []
    extensions = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"} if images else {".pdf"}
    for raw in inputs or (["input"] if Path("input").is_dir() else []):
        path = Path(raw).expanduser()
        if path.is_dir():
            pdfs.extend(sorted(f for f in path.iterdir() if f.is_file() and f.suffix.lower() in extensions))
        elif path.is_file() and path.suffix.lower() in extensions:
            pdfs.append(path)
    return pdfs


def run_benchmark(out_dir: str, gt_path: str):
    from ocr_pipeline.evaluation import micro, text_metrics
    gt = json.loads(Path(gt_path).read_text("utf-8"))
    items = ([{"file": k, **v} for k, v in gt.items()] if isinstance(gt, dict) else gt)
    results = []
    for item in items:
        stem = item["file"]
        if Path(stem).name != stem or stem in (".", ".."):
            raise ValueError("Benchmark filename must be a basename")
        path = Path(out_dir) / stem / f"{stem}.json"
        if not path.is_file():
            # A missing extraction is an empty hypothesis, not an excluded easy subset.
            hypothesis = ""
        else:
            payload = json.loads(path.read_text("utf-8"))
            hypothesis = "\n".join(page.get("text", "") for page in payload.get("pages", []))
        results.append({"file": stem, **text_metrics(item["text"], hypothesis)})
    report = {"reference_status": "PROVISIONAL_LEGACY_REFERENCE_SCHEMA",
              "accuracy_acceptance": "NOT_EVALUATED", "numeric_quality": "NOT_MEASURED_REQUIRES_LOCATION_LINKED_FIELDS",
              "CER_CONTENT_micro": micro([r["CER"] for r in results]),
              "WER_CONTENT_micro": micro([r["WER"] for r in results]), "documents": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main(argv=None) -> int:
    args = parse_args(argv)
    mode = args.mode or os.environ.get("OCR_PIPELINE_MODE", "legacy")
    pdfs = collect_pdfs(args.inputs, images=mode == "candidate")
    if not pdfs:
        print("هیچ PDFی پیدا نشد.", file=sys.stderr)
        return 1
    print(f"{len(pdfs)} فایل PDF — مستر {args.dpi}DPI\n")

    for pdf in pdfs:
        out_dir = Path(args.out) / pdf.stem
        print(f"{pdf.name} → {out_dir}/")
        from ocr_pipeline.precision.profiles import Options
        payload = process_pdf(
            pdf, out_dir, dpi=args.dpi, api_key=args.api_key,
            force_tesseract=args.tesseract_only,
            max_pages=args.max_pages, debug=not args.no_debug, langs=args.langs,
            mode=mode, temp_root=args.temp_dir,
            candidate_options=Options(profile=args.profile, dpi=args.candidate_dpi, handwriting=args.handwriting))
        perf = payload.get("performance", {})
        print(f"  {perf.get('avg_page_seconds', '?')}s/صفحه، "
              f"RAM {perf.get('peak_rss_mb', '?')}MB، "
              f"جدول‌ها {sum(len(p.get('tables', [])) for p in payload['pages'])}، "
              f"کم‌اطمینان {perf.get('low_confidence_regions', 0)}، "
              f"خطاها {perf.get('failed_pages', 0)}")

    if args.benchmark:
        run_benchmark(args.out, args.benchmark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
