"""بنچمارک دقت: CER/WER/Numeric/Table (بخش ۲۶)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

NORMALIZER = "NFC_digits_v1"


def normalize_evaluation(text: str) -> str:
    """No spelling, whitespace, separator, ordering or leading-zero repair."""
    return unicodedata.normalize("NFC", text).translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )


def _levenshtein(a, b) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    ref_n, hyp_n = ref, hyp
    if not ref_n:
        return 0.0 if not hyp_n else 1.0
    return _levenshtein(ref_n, hyp_n) / len(ref_n)


def wer(ref: str, hyp: str) -> float:
    r, h = ref.split(), hyp.split()
    if not r:
        return 0.0 if not h else 1.0
    return _levenshtein(r, h) / len(r)


_NUM_RE = re.compile(r"[-+]?\d+(?:[.,٬٫/×-]\d+)*")


def numeric_tokens(text: str) -> list[str]:
    return _NUM_RE.findall(normalize_evaluation(text))


def numeric_accuracy(ref: str, hyp: str) -> float | None:
    """دقت اعداد: تطبیق ترتیبی توکن‌های عددی پس از نرمال‌سازی ارقام."""
    r, h = numeric_tokens(ref), numeric_tokens(hyp)
    return max(0.0, 1 - _levenshtein(r, h) / len(r)) if r else None


def table_accuracy(ref_rows: list[list[str]], got_rows: list[list[str]]) -> dict:
    """دقت سلول/سطر/ستون/ساختار (بخش ۲۶)."""

    def norm(s):
        return normalize_evaluation(str(s))

    n_ref = sum(map(len, ref_rows))
    cell_ok = 0
    positions = {
        (i, j)
        for rows in (ref_rows, got_rows)
        for i, row in enumerate(rows)
        for j in range(len(row))
    }
    total = len(positions)
    for i, rrow in enumerate(ref_rows):
        grow = got_rows[i] if i < len(got_rows) else []
        for j, rv in enumerate(rrow):
            gv = grow[j] if j < len(grow) else ""
            if j < len(grow) and norm(rv) == norm(gv):
                cell_ok += 1
    row_ok = sum(
        1
        for i, rrow in enumerate(ref_rows)
        if i < len(got_rows)
        and [norm(c) for c in rrow] == [norm(c) for c in got_rows[i]]
    )
    col_ok = 0
    ncols = max([len(row) for row in ref_rows + got_rows] or [0])
    for j in range(ncols):
        rc = [(i, norm(r[j])) for i, r in enumerate(ref_rows) if j < len(r)]
        gc = [(i, norm(g[j])) for i, g in enumerate(got_rows) if j < len(g)]
        col_ok += rc == gc
    struct_ok = [len(row) for row in ref_rows] == [len(row) for row in got_rows]
    return {
        "status": "MEASURED" if n_ref else "EMPTY_REFERENCE",
        "cell_accuracy": round(cell_ok / total, 4) if total and n_ref else None,
        "row_accuracy": round(row_ok / max(len(ref_rows), len(got_rows)), 4)
        if ref_rows
        else None,
        "column_accuracy": round(col_ok / ncols, 4) if ncols and n_ref else None,
        "structure_accuracy": float(struct_ok) if n_ref else None,
        "cells_total": total,
        "cells_matched": cell_ok,
    }


def text_metrics(ref, hyp):
    return {
        "status": "MEASURED" if ref else "EMPTY_REFERENCE",
        "strict": {"cer": cer(ref, hyp), "wer": wer(ref, hyp)},
        "normalized": {
            "cer": cer(normalize_evaluation(ref), normalize_evaluation(hyp)),
            "wer": wer(normalize_evaluation(ref), normalize_evaluation(hyp)),
        },
        "reference_characters": len(ref),
        "reference_words": len(ref.split()),
    }


def token_metrics(reference, observed):
    r, h = list(reference), list(observed)
    rc, hc = Counter(r), Counter(h)
    matched = (rc & hc).total()
    return {
        "status": "MEASURED" if r else "NO_REFERENCE_TOKENS",
        "reference_count": len(r),
        "observed_count": len(h),
        "matched": matched,
        "missing": (rc - hc).total(),
        "extra": (hc - rc).total(),
        "duplicated": sum(max(0, count - rc[t]) for t, count in hc.items() if rc[t]),
        "precision": matched / len(h) if h else (0.0 if r else None),
        "recall": matched / len(r) if r else None,
        "exact_match": r == h if r else None,
        "incorrect_order": r != h and rc == hc,
        "ordered_edit_distance": _levenshtein(r, h),
    }


def field_accuracy(reference, observed):
    """Match named context, page, box AND value. Never page-wide occurrence."""
    used, matched = set(), 0

    def valid(f):
        box = f.get("source_bbox")
        return (
            all(
                isinstance(f.get(k), str) and f[k]
                for k in ("document_id", "field_id", "context")
            )
            and type(f.get("page")) is int
            and f["page"] > 0
            and f.get("canonical_value") is not None
            and str(f["canonical_value"]) != ""
            and isinstance(box, (list, tuple))
            and len(box) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in box)
            and box[2] > box[0]
            and box[3] > box[1]
        )

    for field in reference:
        if not valid(field):
            continue
        box = field.get("source_bbox")
        for i, other in enumerate(observed):
            obox = other.get("source_bbox")
            if (
                i in used
                or not valid(other)
                or any(
                    field.get(k) != other.get(k)
                    for k in ("document_id", "field_id", "context", "page")
                )
            ):
                continue
            intersection = max(0, min(box[2], obox[2]) - max(box[0], obox[0])) * max(
                0, min(box[3], obox[3]) - max(box[1], obox[1])
            )
            union = (
                (box[2] - box[0]) * (box[3] - box[1])
                + (obox[2] - obox[0]) * (obox[3] - obox[1])
                - intersection
            )
            if (
                union > 0
                and intersection / union >= 0.5
                and normalize_evaluation(str(field["canonical_value"]))
                == normalize_evaluation(str(other.get("canonical_value")))
            ):
                used.add(i)
                matched += 1
                break
    total = max(len(reference), len(observed))
    return {
        "status": "MEASURED" if reference else "NOT_MEASURED",
        "matched": matched,
        "reference_count": len(reference),
        "observed_count": len(observed),
        "exact_match": matched / total if total and reference else None,
    }


def evaluate_outputs(reference, out_dir: Path):
    from .engines import find_identifiers
    from .provenance import source_digest
    from .tables import rows_of

    cases = []
    for case, truth in reference.items():
        if (
            not isinstance(case, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", case)
            or not isinstance(truth, dict)
        ):
            cases.append({"status": "UNSUPPORTED_CASE"})
            continue
        path = out_dir / case / (case + ".json")
        if not path.is_file():
            cases.append({"case": case, "status": "MISSING_OUTPUT"})
            continue
        try:
            if path.stat().st_size > 32 * 1024**2:
                raise ValueError("report_budget")
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
                raise TypeError("report_schema")
        except (OSError, ValueError, TypeError):
            cases.append({"case": case, "status": "INVALID_OUTPUT"})
            continue
        pages = data.get("pages", [])
        status = (
            "FAILED_PAGE"
            if any(p.get("error") for p in pages)
            else "MEASURED"
            if pages
            else "MISSING_OUTPUT"
        )
        if data.get("review", {}).get("revision"):
            status = "MANUALLY_REVIEWED_OUTPUT"
        hyp = "\n".join(p.get("text", "") for p in pages)
        ref = truth.get("text", "")
        structured = data.get("schema_v2") or data.get("structured") or {}
        if not isinstance(structured, dict):
            structured = {}
            status = "INVALID_OUTPUT"
        if not ref:
            status = "EMPTY_REFERENCE"
        elif not (path.parent / (case + ".txt")).is_file() and status == "MEASURED":
            status = "MISSING_OUTPUT"
        item = {
            "case": case,
            "status": status,
            "text": text_metrics(ref, hyp),
            "numeric": token_metrics(numeric_tokens(ref), numeric_tokens(hyp)),
            "identifiers": token_metrics(
                find_identifiers(normalize_evaluation(ref)),
                find_identifiers(normalize_evaluation(hyp)),
            ),
            "fields": field_accuracy(
                truth.get("fields", []),
                structured.get("fields", []),
            ),
            "exports": data.get("exports", {}),
            "performance": data.get("performance", {}),
        }
        if truth.get("table_rows"):
            tables = [t for p in pages for t in p.get("tables", [])]
            # Mapping v2 uses page-prefixed files; legacy mapping v1 remains readable.
            if tables and "cells" not in tables[0]:
                candidates = sorted(
                    (path.parent / "tables").glob("page_*_table_*.json")
                ) or sorted((path.parent / "tables").glob("table_*.json"))
                if candidates:
                    tables = [json.loads(candidates[0].read_text("utf-8"))]
            item["table"] = (
                table_accuracy(truth["table_rows"], rows_of(tables[0]))
                if status == "MEASURED" and tables and "cells" in tables[0]
                else {
                    "status": "END_TO_END_FAILED"
                    if status != "MEASURED"
                    else "MISSING_TABLE"
                }
            )
            if item["table"]["status"] != "MEASURED" and status == "MEASURED":
                item["status"] = "MISSING_TABLE"
        item["recognition_provenance"] = {
            k: data.get("document", {}).get(k, "UNKNOWN")
            for k in (
                "source_digest",
                "model_manifest_sha256",
                "engine",
                "quality_mode",
            )
        }
        cases.append(item)
    root = Path(__file__).resolve().parents[1]
    return {
        "schema_version": 2,
        "output_mapping_version": 2,
        "normalizer": NORMALIZER,
        "evaluator_source_digest": source_digest(),
        "evaluator_model_hashes": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / "tessdata").glob("*.traineddata")
        },
        "engine": "see_case_recognition_provenance",
        "n": len(reference),
        "cases": cases,
        "status": "EMPTY_DATASET"
        if not cases
        else "MEASURED"
        if all(c["status"] == "MEASURED" for c in cases)
        else "INCOMPLETE",
        "limitations": ["not_production_accuracy", "no_acceptance_threshold_implied"],
    }
