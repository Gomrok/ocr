"""Private-reference accuracy gate. Neither references nor customer PDFs belong in Git."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_pipeline import engines as E
from ocr_pipeline.precision import overlap, source_box


def normalize(text):
    from ocr_pipeline.benchmark import normalize_evaluation
    return normalize_evaluation(text)


def distance(a, b):
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right))
            )
        previous = current
    return previous[-1]


def region_lines(page, box):
    scaled = [
        box[0] * page["width"],
        box[1] * page["height"],
        box[2] * page["width"],
        box[3] * page["height"],
    ]
    if "review_units" in page:
        units = [
            u
            for u in page["review_units"]
            if u["kind"] == "line"
            and overlap(u.get("source_bbox", u["bbox"]), scaled) > 0.5
        ]
        return [
            u["text"]
            for u in sorted(units, key=lambda u: (u["bbox"][1], -u["bbox"][2]))
        ]
    words = [dict(w, bbox=source_box(w["bbox"], page)) for w in page.get("words", [])]
    return E.words_to_lines([w for w in words if overlap(w["bbox"], scaled) > 0.5])


def evaluate(reference, results):
    expected_keys = [(p["case"], p["page"]) for p in reference["pages"]]
    if len(expected_keys) != 5 or len(set(expected_keys)) != 5:
        raise ValueError("Acceptance requires five distinct reference pages")
    for expected in reference["pages"]:
        if (
            type(expected["page"]) is not int
            or expected["page"] < 1
            or not expected.get("regions")
            or "unreadable" not in expected
        ):
            raise ValueError(
                "Reference pages must record readable and unreadable regions"
            )
        for region in expected["regions"]:
            box = region["bbox"]
            if (
                len(box) != 4
                or any(
                    type(v) not in (int, float)
                    or not math.isfinite(v)
                    or not 0 <= v <= 1
                    for v in box
                )
                or box[0] >= box[2]
                or box[1] >= box[3]
                or not region["text"].strip()
            ):
                raise ValueError("Invalid reference region")
    actual = {}
    provenance_valid=bool(results)
    for document in results:
        metadata=document.get('document',{})
        provenance_valid = provenance_valid and all(
            isinstance(value,str) and re.fullmatch(r'[a-fA-F0-9]{64}',value)
            for value in (document.get('sha256'),reference.get('sources',{}).get(document['case']),
                          metadata.get('source_digest'),metadata.get('model_manifest_sha256')))
        if document.get("review", {}).get("revision", 0):
            raise ValueError(
                "Quality acceptance must use automatic OCR, before manual correction"
            )
        if (
            document.get("sha256", "").upper()
            != reference["sources"].get(document["case"], "").upper()
        ):
            raise ValueError(
                "Result source digest does not match the private reference"
            )
        for page in document["pages"]:
            if (document["case"], page["page"]) in actual:
                raise ValueError("Duplicate automatic result page")
            actual[(document["case"], page["page"])] = page
    details, char_errors, char_count, word_errors, word_count = [], 0, 0, 0, 0
    strict_errors, strict_chars, strict_word_errors, strict_words = 0, 0, 0, 0
    line_count = matched_lines = 0
    critical_failures, cell_failures, incomplete = [], [], []
    for expected in reference["pages"]:
        key = (expected["case"], expected["page"])
        page = actual.get(key)
        if not page or page.get("error") or not expected.get("reference_complete"):
            incomplete.append(list(key))
        observed_text = []
        for region in expected["regions"]:
            truth_lines = [
                normalize(s) for s in region["text"].splitlines() if s.strip()
            ]
            observed = (
                region_lines(page, region["bbox"])
                if page and not page.get("error")
                else []
            )
            raw_prediction = "\n".join(observed)
            raw_truth = region["text"]
            strict_errors += distance(raw_truth, raw_prediction)
            strict_chars += len(raw_truth)
            strict_word_errors += distance(raw_truth.split(), raw_prediction.split())
            strict_words += len(raw_truth.split())
            predicted = normalize(raw_prediction)
            truth = normalize(region["text"])
            ce, we = (
                distance(truth, predicted),
                distance(truth.split(), predicted.split()),
            )
            char_errors += ce
            char_count += len(truth)
            word_errors += we
            word_count += len(truth.split())
            available = [normalize(s).replace(" ", "") for s in observed if s.strip()]
            covered = 0
            for line in truth_lines:
                target = line.replace(" ", "")
                choices = [
                    (distance(target, s) / max(1, len(target)), i)
                    for i, s in enumerate(available)
                ]
                if choices and min(choices)[0] <= 0.2:
                    available.pop(min(choices)[1])
                    covered += 1
            line_count += len(truth_lines)
            matched_lines += covered
            details.append(
                {
                    "case": key[0],
                    "page": key[1],
                    "region": region["name"],
                    "cer": ce / max(1, len(truth)),
                    "wer": we / max(1, len(truth.split())),
                    "lines": len(truth_lines),
                    "covered_lines": covered,
                }
            )
            observed_text.extend(normalize(line) for line in observed)
        # Never assemble an identifier from separate lines or unrelated regions.
        texts = [value.replace(" ", "") for value in observed_text]
        for token in expected.get("critical", []):
            value = normalize(token).replace(" ", "")
            # Legacy unlocated critical tokens require their entire reference
            # line at its reference region. Field contracts should use located EM.
            matching_lines=[]
            for region in expected['regions']:
                actual_lines=[normalize(s) for s in region_lines(page,region['bbox'])] if page and not page.get('error') else []
                for line in region['text'].splitlines():
                    if value in normalize(line).replace(' ',''):
                        matching_lines.append(normalize(line) in actual_lines)
            if not matching_lines or not all(matching_lines):
                critical_failures.append(
                    {"case": key[0], "page": key[1], "value_sha256": hashlib.sha256(token.encode()).hexdigest()}
                )
        for table in expected.get("tables", []):
            matching_tables = [
                t
                for t in (page or {}).get("tables", [])
                if t["table_id"] == table["table_id"]
            ]
            observed_table = matching_tables[0] if matching_tables else {}
            cells = {
                (c["row"], c["column"]): c["normalized_text"]
                for c in observed_table.get("cells", [])
            }
            expected_cells = {
                (r, c) for r, row in enumerate(table["cells"]) for c in range(len(row))
            }
            if (
                len(matching_tables) != 1
                or set(cells) != expected_cells
                or len(cells) != len(observed_table.get("cells", []))
            ):
                cell_failures.append(
                    {
                        "case": key[0],
                        "page": key[1],
                        "table": table["table_id"],
                        "reason": "table_structure",
                    }
                )
            for row, values in enumerate(table["cells"]):
                for column, value in enumerate(values):
                    if normalize(cells.get((row, column), "")) != normalize(value):
                        cell_failures.append(
                            {
                                "case": key[0],
                                "page": key[1],
                                "table": table["table_id"],
                                "row": row,
                                "column": column,
                            }
                        )
    cer, wer, recall = (
        char_errors / max(1, char_count),
        word_errors / max(1, word_count),
        matched_lines / max(1, line_count),
    )
    full_pipeline = bool(results) and all(
        d.get("document", {}).get("evaluation_kind") == "full_pipeline" for d in results
    )
    maximum = bool(results) and all(
        d.get("document", {}).get("quality_mode") == "maximum_local" for d in results
    )
    engine_failures = sum(
        bool(p.get("error"))
        or any(
            w.startswith("second_engine_") or w.endswith("budget_exceeded")
            for w in p.get("precision_warnings", [])
        )
        for d in results
        for p in d["pages"]
    )
    provenance_valid = provenance_valid and len({d.get('document',{}).get('source_digest') for d in results}) == 1 and len({d.get('document',{}).get('model_manifest_sha256') for d in results}) == 1
    passed = (
        provenance_valid
        and not incomplete
        and set(actual) == set(expected_keys)
        and char_count > 0
        and word_count > 0
        and line_count > 0
        and full_pipeline
        and maximum
        and not engine_failures
        and cer <= 0.005
        and wer <= 0.02
        and recall >= 0.99
        and not critical_failures
        and not cell_failures
    )
    return {
        "passed": passed,
        "schema_version": 2,
        "acceptance_profile": "five_page_maximum_local",
        "provenance_valid": bool(provenance_valid),
        "critical_policy": "exact_reference_line_in_reference_region",
        "strict": {"cer": strict_errors / max(1,strict_chars), "wer": strict_word_errors / max(1,strict_words)},
        "normalizer": "NFC_digits_v1",
        "automatic_only": True,
        "pages": len(reference["pages"]),
        "evaluation_kind": "full_pipeline" if full_pipeline else "diagnostic_only",
        "reference_sha256": hashlib.sha256(
            json.dumps(reference, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "source_digest": (
            results[0].get("document", {}).get("source_digest")
            if results
            and len({d.get("document", {}).get("source_digest") for d in results}) == 1
            else None
        ),
        "model_manifest_sha256": (
            results[0].get("document", {}).get("model_manifest_sha256")
            if results
            and len(
                {d.get("document", {}).get("model_manifest_sha256") for d in results}
            )
            == 1
            else None
        ),
        "quality_mode": "maximum_local" if maximum else "legacy",
        "engine_failures": engine_failures,
        "cer": cer,
        "wer": wer,
        "line_coverage": recall,
        "reference_characters": char_count,
        "reference_words": word_count,
        "reference_lines": line_count,
        "missing_or_incomplete_pages": incomplete,
        "critical_failures": critical_failures,
        "cell_failures": cell_failures,
        "regions": details,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        json.loads(args.reference.read_text("utf-8")),
        json.loads(args.results.read_text("utf-8")),
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("regions", "critical_failures", "cell_failures")
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["passed"] else 1)
