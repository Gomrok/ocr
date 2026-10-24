"""Run the complete local pipeline against a private five-page reference.

Supply --source case=absolute.pdf once for each reference case. All generated
outputs must remain outside Git. No existing result or manual edit is reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ocr_pipeline.paddle_engine import PaddleEngine
from ocr_pipeline.pipeline import process_pdf
from ocr_pipeline.review import DISK_RESERVE, atomic_json
from tools.evaluate_accuracy import evaluate


def run(reference_path, sources, output):
    reference = json.loads(reference_path.read_text("utf-8"))
    expected = {(p["case"], p["page"]) for p in reference["pages"]}
    if len(expected) != 5 or len(reference["pages"]) != 5:
        raise ValueError("Exactly five distinct reference pages are required")
    if set(sources) != set(reference["sources"]):
        raise ValueError("Supply exactly the cases present in the private reference")
    output = output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("Acceptance evidence must be stored outside the Git checkout")
    if output.exists():
        raise ValueError("Use a new output directory; previous evidence is immutable")
    parent = output.parent
    if not parent.is_dir():
        raise ValueError("The private output parent must already exist")
    # Five worst-case grayscale masters plus result/ZIP overhead; preserve reserve.
    required = len(expected) * 400_000_000 + 64 * 1024**2
    if shutil.disk_usage(parent).free < DISK_RESERVE + required:
        raise OSError(f"Insufficient space: {DISK_RESERVE + required} bytes required")
    if not PaddleEngine.configured():
        raise RuntimeError(
            "The separately provisioned local OCR runtime is unavailable"
        )
    import pymupdf

    for case, source in sources.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", case):
            raise ValueError("Invalid case identifier")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest.lower() != reference["sources"][case].lower():
            raise ValueError("Source does not match the manual reference")
        with pymupdf.open(source) as pdf:
            if {(case, i + 1) for i in range(len(pdf))} != {
                key for key in expected if key[0] == case
            }:
                raise ValueError(
                    "Reference must cover every page of each sample document"
                )
    output.mkdir()
    results = []
    for case, source in sources.items():
        with source.open("rb") as stream:
            before = hashlib.file_digest(stream, "sha256").hexdigest()
        result = process_pdf(
            source,
            output / case,
            force_tesseract=True,
            quality_mode="maximum_local",
            debug=False,
        )
        with source.open("rb") as stream:
            after = hashlib.file_digest(stream, "sha256").hexdigest()
        if before != after:
            raise RuntimeError("Source changed while acceptance was running")
        results.append(dict(result, case=case, sha256=before))
        atomic_json(output / "automatic-results.json", results)
    report = evaluate(reference, results)
    atomic_json(output / "accuracy-report.json", report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "passed",
                    "pages",
                    "cer",
                    "wer",
                    "line_coverage",
                    "engine_failures",
                )
            }
        )
    )
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = {}
    for item in args.source:
        case, separator, path = item.partition("=")
        if not separator or not path or case in sources:
            parser.error("Use one --source case=absolute.pdf per case")
        sources[case] = Path(path).resolve(strict=True)
    raise SystemExit(0 if run(args.reference, sources, args.output) else 1)
