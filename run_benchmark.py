#!/usr/bin/env python3
"""Run the legacy fixture benchmark in a fresh output directory."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

from ocr_pipeline.benchmark import numeric_accuracy, numeric_tokens, table_accuracy
from ocr_pipeline.evaluation import micro, text_metrics
from ocr_pipeline.storage import atomic_json

BASE = Path(__file__).resolve().parent
DOCS = ("bench_persian", "bench_table", "bench_mixed", "bench_lowq")


def report(output, truth):
    results = {}
    for stem in DOCS:
        reference = truth[stem]
        if not reference.get("text", "").strip():
            raise ValueError(f"{stem}: empty text reference")
        directory = output / stem
        result = json.loads((directory / (stem + ".json")).read_text("utf-8"))
        pages, perf = result["pages"], result["performance"]
        if not pages or perf.get("failed_pages") != 0 or not (perf.get("ocr_native_calls", 0) + perf.get("ocr_processes", 0)):
            raise ValueError(f"{stem}: no complete OCR execution")
        text = "\n".join(page.get("text", "") for page in pages)
        row = {**text_metrics(reference["text"], text), "performance": perf}
        if numeric_tokens(reference["text"]):
            metric = numeric_accuracy(reference["text"], text)
            if not math.isfinite(metric):
                raise ValueError(f"{stem}: numeric metric unavailable")
            row["ordered_number_diagnostic"] = metric
        if reference.get("table_rows"):
            from ocr_pipeline.tables import rows_of
            files = sorted((directory / "tables").glob("page_*_table_001.json"))
            if not files:
                raise ValueError(f"{stem}: expected table is missing")
            cells = rows_of(json.loads(files[0].read_text("utf-8")))
            row["table"] = table_accuracy(reference["table_rows"], cells)
        results[stem] = row
    if not any("ordered_number_diagnostic" in row for row in results.values()):
        raise ValueError("No expected numbers in the fixture benchmark")
    return {"status": "MEASURED_DIAGNOSTIC", "release_status": "NOT_EVALUATED",
            "CER": micro([doc["CER"] for doc in results.values()]),
            "WER": micro([doc["WER"] for doc in results.values()]),
            "documents": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="Compatibility flag; OCR is always local")
    parser.add_argument("--inputs", type=Path, default=BASE / "input")
    parser.add_argument("--out", type=Path, default=BASE / "output_bench")
    args = parser.parse_args()
    try:
        reference = json.loads((args.inputs / "ground_truth.json").read_text("utf-8"))
        inputs = [args.inputs.resolve() / (stem + ".pdf") for stem in DOCS]
        if set(reference) != set(DOCS) or not all(path.is_file() for path in inputs):
            raise ValueError("Missing fixture documents or ground truth")
        args.out.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="run-", dir=args.out.resolve()))
        subprocess.run(
            [sys.executable, "-B", str(BASE / "ocr_bot.py"), *map(str, inputs), "--out", str(output),
             "--mode", "legacy", "--tesseract-only"],
            check=True, cwd=BASE,
        )
        measured = report(output, reference)
        atomic_json(output / "benchmark.json", measured)
        print(json.dumps(measured, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status":"FAIL","reason":str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
