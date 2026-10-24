"""Reproduce the audit using repository synthetic fixtures only; never download.

Run with Python 3.11 from the repository root. Only aggregate evidence is saved.
PDFs, OCR text, and test workspaces are confined to new temporary directories.
This is an audit harness, not an application change or an acceptance gate.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import inspect
import io
import json
import logging
import os
import re
import socket
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(ROOT))


def deny_network(*_args, **_kwargs):
    raise OSError("Network disabled by the audit harness")


def setup():
    os.environ.update(OCR_LOCAL_ONLY="1", OCR_TESSERACT_ONLY="1",
                      OCR_ACCURACY_MODE="legacy", OMP_THREAD_LIMIT="2")
    # This is a Python-level guard, not an OS firewall or a native-DLL guarantee.
    socket.create_connection = deny_network
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    logging.disable(logging.CRITICAL)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        new = [i]
        for j, right in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = new
    return row[-1]


def metric_pair(ref, hyp):
    return {"cer": distance(ref, hyp) / max(1, len(ref)),
            "wer": distance(ref.split(), hyp.split()) / max(1, len(ref.split()))}


def diagnostics():
    import numpy as np

    from ocr_pipeline import benchmark as B
    from ocr_pipeline import engines as E
    from ocr_pipeline import pipeline as P
    from ocr_pipeline import preprocess as PP
    from ocr_pipeline import tables as T
    from ocr_pipeline.metrics import Metrics
    from ocr_pipeline.precision import reconcile

    observations = {}
    observations["legacy_wer_one_word_substitution"] = {
        "reported": B.wer("ABCD EFGH", "WXYZ EFGH"), "token_wer": .5}
    observations["legacy_cer_missing_space"] = {
        "reported": B.cer("AB CD", "ABCD"), "space_sensitive_cer": .2}
    observations["legacy_numeric_duplicate_omission"] = {
        "reported": B.numeric_accuracy("12 12", "12"), "multiset_recall": .5}
    observations["legacy_numeric_order_reversal"] = {
        "reported": B.numeric_accuracy("12 34", "34 12"), "position_exact_match": 0}
    choices = [{"text": value, "confidence": .99,
                "words": [{"text": value, "bbox": (20, 20, 80, 50), "confidence": .99}]}
               for value in ("12345", "12346")]
    selected = E.select_consensus(choices)
    observations["region_disagreement"] = {
        "result_needs_review": selected.get("needs_review", False),
        "word_needs_review": selected["words"][0].get("needs_review", False)}
    # Exercise the actual caller, with synthetic engine readings (not a quality test).
    region = __import__("ocr_pipeline.layout", fromlist=["Region"]).Region(
        "text", (0, 0, 300, 160))
    master = __import__("types").SimpleNamespace(
        read_full=lambda: np.full((160, 300), 255, np.uint8),
        effective_dpi=2000, requested_dpi=2000, width=300, height=160,
        pdf_w_pt=10.8, pdf_h_pt=5.76, approx_mem_mb=.05, render_seconds=0)
    meter = Metrics()
    try:
        with (patch.object(P.L, "analyze_layout", return_value=[region]),
              patch.object(P.P, "optimal_ocr_scale", return_value=.2),
              patch.object(E, "ocr_many", return_value=choices)):
            page = P.ocr_page(master, [E.OCREngine()], meter, export_tables=False)
        observations["region_disagreement_caller"] = {
            "low_confidence_words": page["low_confidence_words"],
            "word_needs_review": [w.get("needs_review", False) for w in page["words"]]}
    finally:
        meter.stop()
    before = [{"text": "000000", "confidence": .4, "bbox": (0, 0, 100, 20),
               "needs_review": True}]
    after, count = E.merge_latin_secondary(before, [
        {"text": "123456", "confidence": .99, "bbox": (0, 0, 100, 20)}])
    observations["latin_repair_uncertainty"] = {
        "changes": count, "needs_review": after[0].get("needs_review", False),
        "raw_preserved": after[0].get("raw_ocr_text") == "000000"}
    image = np.full((100, 100), 255, np.uint8)
    image[50:52, 50:52] = 0
    cleaned = PP.denoise_preserve_dots(image)
    observations["isolated_four_pixel_mark"] = {
        "ink_before": int((image < 128).sum()), "ink_after": int((cleaned < 128).sum())}
    observations["unicode_semantic_changes"] = {"input": "أ إ ؤ ة ۀ ي ك A123",
        "output": E.display_text("أ إ ؤ ة ۀ ي ك A123")}
    observations["zero_confidence_cell"] = T.cell_confidence([
        {"confidence": 0.0}, {"confidence": 1.0}])
    observations["independent_low_confidence_agreement"] = reconcile([
        {"engine": "tesseract", "text": "12345", "confidence": .01},
        {"engine": "paddleocr", "text": "12345", "confidence": .01}])["needs_review"]
    return observations


def run_tests():
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    started = time.perf_counter()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    return {"tests": result.testsRun, "elapsed_seconds": round(time.perf_counter()-started, 3),
            "failures": [{"test": t.id(), "category": trace.splitlines()[-1].split(":")[0]}
                         for t, trace in result.failures],
            "errors": [{"test": t.id(), "category": trace.splitlines()[-1].split(":")[0]}
                       for t, trace in result.errors],
            "skipped": [{"test": t.id(), "reason": reason} for t, reason in result.skipped],
            "successful": result.wasSuccessful()}


def synthetic_reference():
    tree = ast.parse((ROOT / "make_bench_docs.py").read_text("utf-8-sig"))
    names = {"PERSIAN_LINES", "MIXED_LINES", "TABLE_DATA"}
    constants = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in names}
    expected = {
        "bench_persian": {"text": "\n".join(x["text"] for x in constants["PERSIAN_LINES"])},
        "bench_lowq": {"text": "\n".join(x["text"] for x in constants["PERSIAN_LINES"])},
        "bench_mixed": {"text": "\n".join(x["text"] for x in constants["MIXED_LINES"])},
        "bench_table": {"text": "جدول گزارش بارگیری کالا", "table_rows": constants["TABLE_DATA"]}}
    actual = json.loads((ROOT / "input/ground_truth.json").read_text("utf-8-sig"))
    if actual != expected:
        raise ValueError("Synthetic reference does not match source constants")
    return actual


def benchmark_case(name):
    from collections import Counter

    import pymupdf

    from ocr_pipeline import engines as E
    from ocr_pipeline.benchmark import table_accuracy
    from ocr_pipeline.pipeline import process_pdf

    def normalize(text):
        return " ".join(E.display_text(unicodedata.normalize("NFC", text))
                        .replace("\u0640", "").replace("\u200c", " ").split())

    references = synthetic_reference()
    if name not in references:
        raise ValueError("Unknown synthetic fixture")
    source = ROOT / "input" / (name + ".pdf")
    before = digest(source)
    with pymupdf.open(source) as pdf:
        expected_size = {"bench_persian": (2000, 1400), "bench_mixed": (2000, 1400),
                         "bench_table": (2000, 1200), "bench_lowq": (2040, 1455)}[name]
        images = pdf[0].get_image_info()
        if (len(pdf) != 1 or pdf[0].get_text().strip() or len(images) != 1
                or (images[0]["width"], images[0]["height"]) != expected_size
                or pdf.embfile_count()):
            raise ValueError("Unexpected synthetic fixture structure")
    with tempfile.TemporaryDirectory(prefix="ocr-audit-synthetic-") as scratch:
        out = Path(scratch)
        started = time.perf_counter()
        mode = {"quality_mode": "legacy"} if "quality_mode" in inspect.signature(process_pdf).parameters else {"mode": "legacy"}
        payload = process_pdf(source, out, dpi=2000, force_tesseract=True, debug=False, **mode)
        elapsed = time.perf_counter() - started
        page = payload["pages"][0]
        ref, hyp = references[name]["text"], page.get("text", "")
        numbers = lambda s: Counter(re.findall(r"\d+(?:[.,٬٫/\-]\d+)*", E.en_digits_only(s)))
        rnum, hnum = numbers(ref), numbers(hyp)
        identifiers = E.find_identifiers(E.en_digits_only(ref))
        words = page.get("words", [])
        result = {"case": name, "source_sha256": before,
            "implementation": Path(inspect.getfile(process_pdf)).parent.parent.name,
            "source_unchanged": digest(source) == before, "dpi": 2000,
            "elapsed_seconds": round(elapsed, 3), "pages": 1,
            "failed_pages": payload["performance"]["failed_pages"],
            "strict": metric_pair(ref, hyp), "normalized": metric_pair(normalize(ref), normalize(hyp)),
            "reference_characters": len(ref), "reference_words": len(ref.split()),
            "numeric_token_reference_count": rnum.total(),
            "numeric_token_multiset_matches": (rnum & hnum).total(),
            "numeric_token_extra_count": (hnum-rnum).total(),
            "identifier_reference_count": len(identifiers),
            "identifier_exact_matches": sum(bool(re.search(r"(?<![A-Z0-9])" + re.escape(t)
                + r"(?![A-Z0-9])", E.en_digits_only(hyp))) for t in identifiers),
            "words": len(words), "word_review_count": sum(bool(w.get("needs_review")) for w in words),
            "exports": payload.get("exports", {}), "model_hashes": payload.get("document",{}).get("model_hashes",{}),
            "performance": payload["performance"], "page_metrics": page.get("metrics"),
            "analysis": page.get("analysis"), "page_quality": page.get("quality"),
            "raw_text_normalized": metric_pair(normalize(ref), normalize(page.get("raw_text", ""))),
            "synthetic_numeric_changes": [{"before": w.get("raw_ocr_text", w["text"]),
                "after": w["text"], "needs_review": w.get("needs_review", False),
                "numeric_verified": w.get("numeric_verified", False),
                "recognition_repaired": w.get("recognition_repaired", False)} for w in words
                if re.search(r"\d", w["text"]) and E.en_digits_only(w.get("raw_ocr_text", w["text"]))
                != E.en_digits_only(w["text"])],
            "preprocess_ops": page.get("preprocess_ops"), "regions": len(page.get("regions", [])),
            "tables": len(page.get("tables", [])), "effective_dpi": page.get("effective_dpi")}
        from ocr_pipeline import benchmark as B
        if hasattr(B, "text_metrics"):
            result["evaluation_v2"] = {"schema_version": 2, "normalizer": B.NORMALIZER,
                "text": B.text_metrics(ref,hyp),
                "numeric": B.token_metrics(B.numeric_tokens(ref),B.numeric_tokens(hyp)),
                "fields": {"status":"NOT_MEASURED"},
                "source_digest": payload["document"].get("source_digest"),
                "status": "FAILED_PAGE" if page.get("error") else "MEASURED"}
        if references[name].get("table_rows"):
            tables = page.get("tables", [])
            # If mandatory XLSX export failed, only a pre-export artifact may exist.
            partial = out / "tables/page_001_table_001.json"
            artifact = (tables[0] if tables and "cells" in tables[0]
                        else json.loads(partial.read_text("utf-8")) if partial.exists() else None)
            if artifact:
                from ocr_pipeline.tables import rows_of
                result["table_diagnostic"] = {"scope": "full_pipeline" if tables else "pre_export_only",
                    **table_accuracy([[normalize(c) for c in row] for row in references[name]["table_rows"]],
                                    [[normalize(c) for c in row] for row in rows_of(artifact)])}
        return result


def baseline(release_root=None, selected_case=None):
    import psutil

    from ocr_pipeline.process_guard import ProcessGuard

    results = []
    with tempfile.TemporaryDirectory(prefix="ocr-audit-supervisor-") as scratch:
        cases = (selected_case,) if selected_case else ("bench_persian", "bench_mixed", "bench_lowq", "bench_table")
        for name in cases:
            output = Path(scratch) / (name + ".json")
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                       "benchmark", "--case", name, "--output", str(output)]
            if release_root:
                command.extend(["--release-root", str(release_root)])
            guard = ProcessGuard(8 * 1024**3)
            started, peak_rss, peak_cpu, peak_children = time.perf_counter(), 0, {}, 0
            status, exit_code = "completed", None
            process = guard.start(command, env={"PYTHONDONTWRITEBYTECODE": "1"})
            # Results are written to a bounded synthetic JSON file; consume stdout
            # after exit. Aggregate output stays below the OS pipe buffer.
            try:
                process_root = psutil.Process(process.pid)
                process_root.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
                while process.poll() is None:
                    guard.check()
                    try:
                        processes = [process_root] + process_root.children(recursive=True)
                    except psutil.NoSuchProcess:
                        if process.poll() is not None:
                            break
                        raise
                    peak_children = max(peak_children, len(processes)-1)
                    rss = 0
                    for child in processes:
                        try:
                            rss += child.memory_info().rss
                            cpu = child.cpu_times()
                            key = (child.pid, child.create_time())
                            peak_cpu[key] = max(peak_cpu.get(key, 0), cpu.user + cpu.system)
                        except psutil.NoSuchProcess:
                            continue
                    peak_rss = max(peak_rss, rss)
                    if time.perf_counter()-started > 600:
                        status = "audit_timeout_600s"
                        break
                    time.sleep(.1)
                exit_code = process.poll()
            except (RuntimeError, psutil.Error):
                status = "audit_supervision_failed"
            finally:
                guard.close()
                if process.stdout:
                    process.stdout.close()
            elapsed = time.perf_counter()-started
            value = json.loads(output.read_text("utf-8")) if output.exists() else {"case": name}
            value["supervision"] = {"status": status, "exit_code": exit_code,
                "wall_seconds_including_startup": round(elapsed, 3),
                "peak_tree_rss_mib_sampled": round(peak_rss/1024**2, 1),
                "tree_cpu_seconds_sampled_lower_bound": round(sum(peak_cpu.values()), 3),
                "average_core_equivalents_lower_bound": round(sum(peak_cpu.values())/max(.001, elapsed), 3),
                "peak_children": peak_children, "rss_sample_interval_seconds": .1,
                "memory_limit_gib": 8, "deadline_seconds": 600}
            results.append(value)
            print(json.dumps({"completed_case": name, "supervision": value["supervision"]}), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("diagnostics", "tests", "benchmark", "baseline"))
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--case", choices=("bench_persian", "bench_table", "bench_mixed", "bench_lowq"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "benchmark":
        # Join the supervisor's Job Object before native imports or worker spawn.
        import importlib.util
        spec = importlib.util.spec_from_file_location("audit_process_guard", ROOT / "ocr_pipeline/process_guard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.enter_worker_job()
        if args.release_root:
            sys.path.insert(0, str(args.release_root.resolve(strict=True)))
    setup()
    with tempfile.TemporaryDirectory(prefix="ocr-audit-session-") as session_dir:
        os.environ["OCR_DATA_DIR"] = session_dir
        value = (diagnostics() if args.mode == "diagnostics" else
                 run_tests() if args.mode == "tests" else
                 baseline(args.release_root, args.case) if args.mode == "baseline" else benchmark_case(args.case))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError("Use a new evidence filename")
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({"case":args.case,"status":"written"}) if args.mode == "benchmark" else json.dumps(value, ensure_ascii=True))
    if args.mode == "tests" and not value["successful"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
