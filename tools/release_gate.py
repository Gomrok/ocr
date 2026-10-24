"""Read-only deployment gate for measured automatic accuracy and offline artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ocr_pipeline.paddle_engine import PaddleEngine, verify_models
from ocr_pipeline.provenance import source_digest


def check_report(report, models, expected_digest=None):
    verify_models(models)
    for field in ("cer", "wer", "line_coverage"):
        value = report.get(field)
        if (
            type(value) not in (float, int)
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("Invalid accuracy measurement")
    if (
        report.get("passed") is not True
        or report.get("automatic_only") is not True
        or report.get("pages") != 5
        or report.get("missing_or_incomplete_pages")
        or report.get("critical_failures")
        or report.get("cell_failures")
        or report.get("cer", 1) > 0.005
        or report.get("wer", 1) > 0.02
        or report.get("line_coverage", 0) < 0.99
        or report.get("engine_failures", 1) != 0
        or report.get("quality_mode") != "maximum_local"
        or report.get("evaluation_kind") != "full_pipeline"
        or not isinstance(report.get("reference_sha256"), str)
        or len(report["reference_sha256"]) != 64
        or report.get("reference_characters", 0) < 1
        or report.get("reference_words", 0) < 1
        or report.get("reference_lines", 0) < 1
    ):
        raise ValueError("Automatic accuracy acceptance criteria have not passed")
    if report.get("source_digest") != (expected_digest or source_digest()):
        raise ValueError("Accuracy evidence belongs to another source revision")
    if (
        report.get("model_manifest_sha256")
        != hashlib.sha256((models / "manifest.json").read_bytes()).hexdigest()
    ):
        raise ValueError("Accuracy evidence belongs to other model artifacts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--accuracy-report", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--storage", type=Path, required=True)
    args = parser.parse_args()
    check_report(json.loads(args.accuracy_report.read_text("utf-8")), args.models)
    # Code archive + staging + small dependency additions, in addition to reserve.
    inventory = (
        subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    release_bytes = sum(
        (ROOT / name).stat().st_size
        for name in set(inventory)
        if name and (ROOT / name).is_file()
    )
    if (
        shutil.disk_usage(args.storage).free
        < 1536 * 1024 * 1024 + release_bytes * 2 + 32 * 1024 * 1024
    ):
        raise OSError(
            "Insufficient release space; preserve documents and previous releases"
        )
    with PaddleEngine(args.python, args.models):
        pass
    print(
        "Offline model initialization, integrity, space and automatic quality gate passed."
    )
