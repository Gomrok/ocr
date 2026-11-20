"""Capture OCR on a fixed checkout and enforce the release rubric offline."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ocr_pipeline.release_identity import digest, profile, runtime_identity
from ocr_pipeline.storage import atomic_json, file_sha256
from evaluate_extractions import evaluate

SHA = re.compile(r"[0-9a-f]{64}")
PASS, FAIL, NOT_EVALUATED = "PASS", "FAIL", "NOT_EVALUATED"


def read(path):
    return json.loads(path.read_text("utf-8-sig"))


def clean_tree(root=ROOT):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        ).stdout.strip()
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Release evidence requires a clean committed tree")
    return {"commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}


def descriptor(root, path):
    return {"file": path.resolve().relative_to(root.resolve()).as_posix(), "sha256": file_sha256(path)}


def artifact(root, desc):
    path = (root / desc["file"]).resolve(strict=True)
    if root not in path.parents or not path.is_file() or file_sha256(path) != desc["sha256"]:
        raise ValueError("Artifact escaped the sealed directory or its SHA-256 changed")
    return path


def private_root(path):
    root = path.resolve()
    if root == ROOT or ROOT in root.parents or root == Path(root.anchor) or root in ROOT.parents:
        raise ValueError("Evidence must be in a dedicated directory outside the checkout")
    return root


def require_fresh(timestamp, oldest, newest):
    date = datetime.fromisoformat(timestamp)
    if date.tzinfo is None or not oldest <= date <= newest:
        raise ValueError("OCR artifacts do not belong to this evidence window")
    return date


def check_receipt(root, record, identity, run_id, started, finished):
    receipt = read(artifact(root, record))
    if receipt["identity"] != identity or receipt["run_id"] != run_id:
        raise ValueError("OCR receipt is for another revision, engine, configuration, or run")
    require_fresh(receipt["started_at"], started, finished)
    require_fresh(receipt["finished_at"], datetime.fromisoformat(receipt["started_at"]), finished)
    if not SHA.fullmatch(receipt["source_sha256"]):
        raise ValueError("Missing source digest")
    if file_sha256(artifact(root, receipt["source"])) != receipt["source_sha256"]:
        raise ValueError("OCR receipt source does not match")
    prediction = artifact(root, receipt["json"])
    content = read(prediction)
    if content["document"]["source_sha256"] != receipt["source_sha256"]:
        raise ValueError("OCR output belongs to another source")
    pages = content["pages"]
    perf = content["performance"]
    if (not pages or perf.get("failed_pages") != 0 or perf.get("checkpoint_hits") != 0
            or perf.get("total_seconds", 0) <= 0 or perf.get("peak_tree_rss_mb", 0) <= 0
            or perf.get("ocr_native_calls", 0) + perf.get("ocr_processes", 0) <= 0):
        raise ValueError("OCR receipt has no fresh complete engine execution")
    status = read(artifact(root, receipt["status"]))
    if (status["state"] != "done" or status["pages_done"] != len(pages)
            or status["dpi"] != int(identity["runtime"]["settings"]["OCR_DEFAULT_DPI"])):
        raise ValueError("Downloaded result does not match the tested route and resolution")
    text = artifact(root, receipt["txt"]).read_text("utf-8")
    page_markers = [int(value) for value in re.findall(r"^={20} PAGE ([0-9۰-۹٠-٩]+) ={20}$", text, re.M)]
    if page_markers != list(range(1, len(pages) + 1)):
        raise ValueError("TXT pages are missing, repeated, or out of order")
    return receipt["source_sha256"], prediction


def accuracy_gates(report):
    if report["input_status"] != "MEASURED":
        return {"G1": FAIL, "G2": NOT_EVALUATED}
    samples = [doc for doc in report["documents"] if doc["role"] == "sample" and doc["split"] == "tuning"]
    g1 = NOT_EVALUATED
    if len(samples) == 1 and len(samples[0]["pages"]) == 3:
        g1 = PASS
        for page, cer_limit, wer_limit in zip(samples[0]["pages"], (.02,.02,.05), (.05,.05,.10)):
            after = page["after"]
            nums = after["numeric"]
            if (nums.get("status") != "MEASURED" or nums["accepted_exact_match"] != nums["numeric_fields_tested"]
                    or nums["canonical_character_errors"]["errors"] != 0 or after["missing_required_text"]
                    or not _at_most(after["CER_CONTENT"], cer_limit) or not _at_most(after["WER_CONTENT"], wer_limit)):
                g1 = FAIL
    held_out = [doc for doc in report["documents"] if doc["split"] == "evaluation"]
    g2 = NOT_EVALUATED
    if report["accuracy_acceptance_allowed"] and (
            len(held_out) >= 10 and len({doc["source_group"] for doc in held_out}) >= 2):
        after = report["micro"]["evaluation"]["after"]
        nums = after["numeric"]
        if after["pages"] >= 30 and nums.get("numeric_fields_tested", 0) >= 300:
            g2 = PASS if (nums["field_exact_match"] >= .995 and nums["missing_numeric_fields"] == 0
                          and _at_most(nums["canonical_character_errors"], .001)
                          and _at_most(after["CER_CONTENT"], .05) and _at_most(after["WER_CONTENT"], .10)
                          and nums["auto_acceptance_coverage"] >= .99 and nums["silent_numeric_errors"] == 0) else FAIL
    return {"G1": g1, "G2": g2}


def _at_most(metric, limit):
    return metric.get("status") == "MEASURED" and metric["fraction"] is not None and metric["fraction"] <= limit


def _read_proof(root, manifest, name, identity):
    if not manifest.get(name):
        return None
    item = read(artifact(root, manifest[name]))
    if item["identity"] != identity or item["run_id"] != manifest["run_id"]:
        raise ValueError(f"{name} belongs to a different release evaluation")
    require_fresh(item["finished_at"], datetime.fromisoformat(manifest["started_at"]),
                  datetime.fromisoformat(manifest["finished_at"]))
    return item


def runtime_gates(root, manifest, identity):
    g3 = _read_proof(root, manifest, "length", identity)
    g4 = _read_proof(root, manifest, "jobs", identity)
    g5 = _read_proof(root, manifest, "compatibility", identity)
    gates = dict.fromkeys(("G3", "G4", "G5"), NOT_EVALUATED)
    if g3:
        evidence = read(artifact(root, g3["checks"]))
        packets = evidence["packets"]
        sizes = [row["pages"] for row in packets]
        required = {1, 3, 30, 100, 300}
        gates["G3"] = PASS if (
            required.issubset(sizes) and sizes.count(1) >= 3 and evidence["boundary_rejected_301"] is True
            and all(row["ordered"] and row["matching_sample_pages"] == row["expected_sample_pages"]
                    and row["failed_pages"] == 0 and row["cache_hits"] == 0
                    and row["checkpoint_hits"] == 0 and row["fresh_calls"] >= row["expected_sample_pages"]
                    and 0 < row["peak_tree_rss_mib"] <= row["budget_mib"]
                    for row in packets)
            and all(next(row for row in packets if row["pages"] == n)["expected_sample_pages"] >= 3
                    for n in (3,30,100))) else FAIL
    if g4:
        evidence = read(artifact(root, g4["checks"]))
        required = ("owner_isolated", "recovered_after_restart", "reused_only_complete_pages",
                    "duplicate_names_distinct", "corrupt_file_isolated", "admission_backpressure",
                    "download_equal_to_single", "next_batch_without_refresh", "refresh_restores_job")
        gates["G4"] = PASS if (all(evidence.get(key) is True for key in required)
                              and set(evidence["batch_sizes"]) == {3, 5} and evidence["sessions"] >= 2
                              and evidence["overlap_seconds"] > 0 and evidence["max_active_ocr"] >= 2
                              and 0 < evidence["peak_tree_rss_mib"] <= evidence["budget_mib"]) else FAIL
    if g5:
        results = read(artifact(root, g5["checks"]))
        unit_log = artifact(root, g5["unit_log"]).read_text("utf-8")
        table = results["table"]
        gates["G5"] = PASS if (results["unit_exit_code"] == 0 and results["powershell_exit_code"] == 0
                              and results["negative_gate_exit_code"] != 0
                              and bool(re.search(r"^Ran [1-9][0-9]* tests? in ", unit_log, re.M))
                              and bool(re.search(r"^OK\s*\Z", unit_log))
                              and table["cells_expected"] > 0
                              and table["candidate_cell_accuracy"] >= table["baseline_cell_accuracy"]
                              and table["candidate_cell_accuracy"] > 0) else FAIL
    return gates


def preflight(bundle, identity):
    manifest = read(bundle / "bundle.json")
    if manifest.get("schema") != 1 or manifest.get("identity") != identity or not SHA.fullmatch(manifest.get("run_id","")):
        raise ValueError("Evidence does not match the clean commit, tree, settings, native engine and models")
    now = datetime.now(timezone.utc)
    finished = require_fresh(manifest["finished_at"], now - timedelta(days=7), now + timedelta(minutes=2))
    started = require_fresh(manifest["started_at"], now - timedelta(days=7), finished)
    reference = read(artifact(bundle, manifest["reference"]))
    observed = dict(check_receipt(bundle, record, identity, manifest["run_id"], started, finished)
                    for record in manifest["ocr"])
    if len(observed) != len(manifest["ocr"]):
        raise ValueError("Duplicate OCR receipts")
    inputs = [{"source_sha256": before["source_sha256"], "before": artifact(bundle, before["result"]),
               "after": observed.pop(before["source_sha256"])} for before in manifest["baseline"]]
    if observed:
        raise ValueError("There are outputs with no baseline mapping")
    report = evaluate(reference, inputs)
    gates = {"G0": PASS if report["input_status"] == "MEASURED" else FAIL,
             **accuracy_gates(report), **runtime_gates(bundle, manifest, identity)}
    return {"gates": gates, "identity": identity, "report": report,
            "status": "READY_TO_RELEASE" if all(value == PASS for value in gates.values()) else "NOT_READY"}


def capture(bundle, settings, engine, identity, plan):
    """Capture actual downloads over the normal upload, worker and status routes."""
    run_id = plan["run_id"]
    if not SHA.fullmatch(run_id) or not plan["inputs"]:
        raise ValueError("Capture requires an evidence run ID and a nonempty input plan")
    mode = profile(settings)["OCR_PIPELINE_MODE"]
    scratch = bundle / "capture-temp"
    scratched_data = bundle / "capture-data"
    scratch.mkdir(exist_ok=False)
    scratched_data.mkdir(exist_ok=False)
    os.environ.update(profile(settings), TESSERACT_CMD=str(engine), OCR_TESSDATA_DIR=str(ROOT / "tessdata"),
                      OMP_THREAD_LIMIT="1", OCR_DATA_DIR=str(scratched_data), OCR_TEMP_DIR=str(scratch),
                      OCR_SECURE_COOKIES="0", OCR_ALLOWED_HOSTS="localhost", OCR_RELEASE=identity["commit"])
    from web_app import app

    results = []
    for index, entry in enumerate(plan["inputs"]):
        source = artifact(bundle, entry["source"])
        sha = file_sha256(source)
        out = bundle / "candidate" / sha
        out.mkdir(parents=True, exist_ok=False)
        started = datetime.now(timezone.utc).isoformat()
        client = app.test_client()
        index_page = client.get("/").get_data(as_text=True)
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', index_page)[1]
        valid = client.get("/healthz").get_json()
        if valid["status"] != "ok" or valid["pipeline_mode"] != mode:
            raise ValueError("The isolated service did not start in the requested mode")
        with source.open("rb") as handle:
            request = client.post("/upload", data={"pdfs": (handle, source.name)},
                                  headers={"X-CSRF-Token": csrf},
                                  environ_overrides={"REMOTE_ADDR": f"127.0.{index//254}.{index%254+1}"})
        if request.status_code != 200:
            raise ValueError(f"Fresh upload was rejected ({request.status_code})")
        job = request.get_json()["job_id"]
        deadline = time.monotonic() + 6 * 3600 + 60
        while time.monotonic() < deadline:
            status = client.get(f"/status/{job}").get_json()
            if status["state"] not in ("queued", "running"):
                break
            time.sleep(.5)
        else:
            raise ValueError("Fresh upload did not finish")
        if status["state"] != "done":
            raise ValueError("Fresh OCR did not complete the document")
        for ext in ("txt", "json"):
            name = next(name for name in status["files"] if name.endswith("." + ext))
            with client.get(f"/download/{job}/{name}") as response, (out / (source.stem+"."+ext)).open("xb") as handle:
                if response.status_code != 200:
                    raise ValueError("Fresh output is missing or not downloadable")
                for chunk in response.response:
                    handle.write(chunk)
        atomic_json(out / "status.json", status)
        receipt = {
            "run_id": run_id, "identity": identity, "source": entry["source"], "source_sha256": sha,
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": descriptor(bundle, out / "status.json"),
            **{ext: descriptor(bundle, out / (source.stem + "." + ext)) for ext in ("json", "txt")},
        }
        record = out / "receipt.json"
        atomic_json(record, receipt)
        results.append(descriptor(bundle, record))
    atomic_json(bundle / "ocr-receipts.json", results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capture", "preflight", "identity"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--tesseract", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        settings = read(args.settings)
        engine = args.tesseract.resolve(strict=True)
        identity = clean_tree() | {"runtime": runtime_identity(settings, engine, ROOT / "tessdata")}
        if args.command == "identity":
            print(json.dumps(identity | {"runtime_fingerprint": digest(identity["runtime"])}))
            return 0
        bundle = private_root(args.bundle)
        if args.command == "capture":
            capture(bundle, settings, engine, identity, read(args.plan))
            return 0
        report = preflight(bundle, identity)
        if args.report:
            report_path = args.report.resolve()
            if bundle not in report_path.parents:
                raise ValueError("Reports must stay inside the private evidence directory")
            atomic_json(report_path, report)
        print(json.dumps({"status": report["status"], "gates": report["gates"],
                          "commit": identity["commit"], "tree": identity["tree"],
                          "runtime_fingerprint": digest(identity["runtime"])}))
        return 0 if report["status"] == "READY_TO_RELEASE" else 1
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OSError,
            StopIteration, subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"NOT_READY", "gates":{"G0":"FAIL"}, "reason":str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
