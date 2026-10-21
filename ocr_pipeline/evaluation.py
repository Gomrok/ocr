"""Reproducible edit accounting; never infer accuracy from confidence or abstention."""
from __future__ import annotations

from collections import Counter
import re


def edit_counts(reference, hypothesis):
    """Levenshtein on arbitrary sequences, with deterministic S/D/I accounting.

    Space O(len(hypothesis)); ties prefer substitution, then deletion, then insertion.
    No clipping of rates: insertions can legitimately produce CER > 100%.
    """
    previous = [(0, 0, j) for j in range(len(hypothesis)+1)]
    for i, expected in enumerate(reference, 1):
        current = [(0, i, 0)]
        for j, observed in enumerate(hypothesis, 1):
            if expected == observed:
                current.append(previous[j-1])
            else:
                s, d, ins = previous[j-1]
                sub = (s+1, d, ins)
                s, d, ins = previous[j]
                delete = (s, d+1, ins)
                s, d, ins = current[-1]
                insert = (s, d, ins+1)
                current.append(min((sub, delete, insert), key=sum))
        previous = current
    s, d, ins = previous[-1]
    return rate({"S": s, "D": d, "I": ins, "N": len(reference)})


def rate(counts):
    errors = counts["S"] + counts["D"] + counts["I"]
    fraction = errors / counts["N"] if counts["N"] else None
    return {**counts, "errors": errors, "fraction": fraction,
            "percent": fraction * 100 if fraction is not None else None,
            "status": "MEASURED" if counts["N"] else "EMPTY_REFERENCE"}


def micro(results):
    return rate({key: sum(item[key] for item in results) for key in ("S", "D", "I", "N")})


def text_metrics(reference: str, hypothesis: str):
    # CRLF is a serialization difference; spaces, punctuation and all digit values remain.
    r, h = reference.replace("\r\n", "\n"), hypothesis.replace("\r\n", "\n")
    return {"CER": edit_counts(r, h), "WER": edit_counts(r.split(), h.split()),
            "normalization": "CRLF_to_LF_only; WER_Unicode_whitespace_tokens"}


def content_from_segments(segments):
    """Remove only explicitly registered decorations, never regex-match document text."""
    if any(part.get("kind") not in ("content", "annotation") or not isinstance(part.get("text"), str)
           for part in segments):
        raise ValueError("Export segment metadata is incomplete")
    return "".join(part["text"] for part in segments if part["kind"] == "content")


def evaluate_text_layers(reference, engine_raw, content, export_segments):
    exported_content = content_from_segments(export_segments)
    if exported_content != content:
        raise ValueError("Export content differs from extraction revision")
    literal = "".join(part["text"] for part in export_segments)
    raw = text_metrics(reference, engine_raw) if engine_raw is not None else None
    content_metrics = text_metrics(reference, content)
    export_metrics = text_metrics(reference, literal)
    return {"CER_ENGINE_RAW": raw["CER"] if raw else {"status": "NOT_CAPTURED"},
            "CER_CONTENT": content_metrics["CER"], "WER_CONTENT": content_metrics["WER"],
            "CER_EXPORT_LITERAL": export_metrics["CER"],
            "ANNOTATION_CHAR_COUNT": sum(len(part["text"]) for part in export_segments if part["kind"] == "annotation"),
            "annotation_alignment_error_delta": export_metrics["CER"]["errors"]-content_metrics["CER"]["errors"],
            "normalization": content_metrics["normalization"]}


def validate_reference(manifest):
    sets = {}
    verified = True
    for document in manifest["documents"]:
        sha, split = document["source_sha256"], document["split"]
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or split not in ("tuning", "evaluation"):
            raise ValueError("Reference requires a source hash and document-level split")
        if sha in sets:
            raise ValueError("A source document cannot occur twice or in both splits")
        sets[sha] = split
        for page in document["pages"]:
            for region in page["regions"]:
                if region.get("readability") not in ("readable", "unreadable", "uncertain"):
                    raise ValueError("Readability must be declared before evaluation")
                if not (region.get("human_verified") is True and region.get("reviewer")
                        and region.get("reviewed_at") and region.get("version")):
                    verified = False
    return {"reference_status": "HUMAN_VERIFIED" if verified and sets else "PROVISIONAL",
            "tuning_documents": sum(value == "tuning" for value in sets.values()),
            "independent_evaluation_documents": sum(value == "evaluation" for value in sets.values()),
            "accuracy_acceptance_allowed": verified and "evaluation" in sets.values()}


def _alignment(a, b):
    # Field-sized inputs only; retain backtracking for exact digit confusion attribution.
    if len(a) * len(b) > 1_000_000:
        raise ValueError("Numeric field alignment exceeds budget")
    rows = [list(range(len(b)+1))]
    for i, char in enumerate(a, 1):
        row = [i]
        for j, other in enumerate(b, 1):
            row.append(min(rows[-1][j]+1, row[-1]+1, rows[-1][j-1]+(char != other)))
        rows.append(row)
    i, j, pairs = len(a), len(b), []
    while i or j:
        if i and j and rows[i][j] == rows[i-1][j-1]+(a[i-1] != b[j-1]):
            pairs.append((a[i-1], b[j-1])); i -= 1; j -= 1
        elif i and rows[i][j] == rows[i-1][j]+1:
            pairs.append((a[i-1], None)); i -= 1
        else:
            pairs.append((None, b[j-1])); j -= 1
    return pairs[::-1]


def numeric_field_metrics(reference_fields, observed_fields):
    """Caller matches fields by source/page/ID, never by occurrence elsewhere in text."""
    if not reference_fields:
        return {"status": "NOT_MEASURED", "reason": "No location-linked numeric reference fields"}
    def index(fields):
        result = {}
        for field in fields:
            key = (field["source_sha256"], field["page"], field["field_id"])
            if key in result or not isinstance(field["text"], str):
                raise ValueError("Duplicate field identity or non-string numeric field")
            result[key] = field
        return result
    refs, observed = index(reference_fields), index(observed_fields)
    counts = Counter()
    details = []
    digit_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    for key, expected in refs.items():
        actual = observed.get(key, {"text": "", "review_required": False})
        r, h = expected["text"], actual["text"]
        matched = r == h  # No value, separator, unit or Unicode normalization here.
        counts["numeric_field_exact_match"] += matched
        counts["silent_numeric_errors"] += not matched and not actual.get("review_required", False)
        counts["review_required_numeric_fields"] += bool(actual.get("review_required"))
        normalized_r, normalized_h = r.translate(digit_map), h.translate(digit_map)
        if normalized_r.startswith("0"):
            counts["leading_zero_fields"] += 1
            expected_zeros = len(normalized_r)-len(normalized_r.lstrip("0"))
            observed_zeros = len(normalized_h)-len(normalized_h.lstrip("0"))
            counts["leading_zero_preserved"] += expected_zeros == observed_zeros
        for a, b in _alignment(r, h):
            counts["4_to_6_substitutions"] += bool(a and b and a.translate(digit_map) == "4" and b.translate(digit_map) == "6")
            counts["6_to_4_substitutions"] += bool(a and b and a.translate(digit_map) == "6" and b.translate(digit_map) == "4")
            counts["digit_deletions"] += bool(a and a.isdecimal() and b is None)
            counts["digit_insertions"] += bool(b and b.isdecimal() and a is None)
        details.append({"field_id": key[2], "page": key[1], "source_sha256": key[0],
                        "bbox": expected.get("bbox"), "reference": r, "observed": h,
                        "exact_match": matched, "review_required": actual.get("review_required", False)})
    for key in observed.keys() - refs.keys():
        counts["extra_numeric_fields"] += 1
        counts["digit_insertions"] += sum(char.isdecimal() for char in observed[key]["text"])
    names = ("4_to_6_substitutions", "6_to_4_substitutions", "digit_deletions", "digit_insertions",
             "numeric_field_exact_match", "silent_numeric_errors", "review_required_numeric_fields",
             "leading_zero_fields", "leading_zero_preserved", "extra_numeric_fields")
    return {"status": "MEASURED", "numeric_fields_tested": len(refs),
            **{name: counts[name] for name in names}, "details": details}


def line_order_metrics(reference_ids, observed_ids):
    if reference_ids is None:
        return {"status": "NOT_MEASURED"}
    if len(set(reference_ids)) != len(reference_ids):
        raise ValueError("Reference line identities must be unique")
    counts = Counter(observed_ids)
    covered = sum(counts[key] > 0 for key in reference_ids)
    order = {key: i for i, key in enumerate(reference_ids)}
    sequence = [order[key] for key in observed_ids if key in order]
    inversions = sum(a > b for i, a in enumerate(sequence) for b in sequence[i+1:])
    return {"status": "MEASURED", "reference_lines": len(reference_ids), "covered_lines": covered,
            "coverage": covered/len(reference_ids) if reference_ids else None,
            "duplicates": sum(max(0, value-1) for value in counts.values()), "order_inversions": inversions}
