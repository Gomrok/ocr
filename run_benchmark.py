"""Local synthetic benchmark. Missing outputs are explicit, never certified."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ocr_pipeline.benchmark import evaluate_outputs

BASE = Path(__file__).resolve().parent
OUT = BASE / "output_bench"
GT = BASE / "input/ground_truth.json"
DOCS = ["bench_persian", "bench_table", "bench_mixed", "bench_lowq"]


def run_pipeline(tesseract_only=True):
    command = [
        sys.executable,
        "-B",
        str(BASE / "ocr_bot.py"),
        *[str(BASE / "input" / (name + ".pdf")) for name in DOCS],
        "--out",
        str(OUT),
        "--tesseract-only",
    ]
    return subprocess.run(command, cwd=BASE, check=False).returncode


def report():
    result = evaluate_outputs(json.loads(GT.read_text("utf-8")), Path(OUT))
    result["command"] = ["run_benchmark.py", "--local"]
    result["environment"] = {
        "local_only": True,
        "dataset": "repository_synthetic",
        "n_per_case": 1,
    }
    Path(OUT).mkdir(parents=True, exist_ok=True)
    (Path(OUT) / "benchmark_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(result, ensure_ascii=True))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--local",
        action="store_true",
        help="Compatibility flag; processing is always local",
    )
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    code = 0 if args.report_only else run_pipeline()
    result = report()
    raise SystemExit(0 if code == 0 and result["status"] == "MEASURED" else 1)
