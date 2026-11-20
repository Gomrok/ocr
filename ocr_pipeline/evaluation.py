"""Reproducible edit accounting; never infer accuracy from confidence or abstention."""
from __future__ import annotations

from collections import Counter
import math
import re

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
NUMERIC_TOKEN = re.compile(
    r"[+\-−]?[0-9۰-۹٠-٩]+(?:[.,،٫٬/:\-‐‑–][0-9۰-۹٠-٩]+)*[%٪]?"
)


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


def valid_box(box):
    return (
        isinstance(box, (list, tuple)) and len(box) == 4
        and all(type(value) in (int, float) and math.isfinite(value) for value in box)
        and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1
    )


def _human_reviewed(item):
    return item.get("human_verified") is True and all(
        item.get(key) for key in ("reviewer", "reviewed_at", "version")
    )


def validate_reference(manifest):
    if not manifest.get("documents"):
        raise ValueError("Reference contains no documents")
    sets = {}
    sources = {}
    verified = True
    readable_count = 0
    numeric_count = 0
    for document in manifest["documents"]:
        sha, split = document["source_sha256"], document["split"]
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or split not in ("tuning", "evaluation"):
            raise ValueError("Reference requires a source hash and document-level split")
        if sha in sets:
            raise ValueError("A source document cannot occur twice or in both splits")
        sets[sha] = split
        source_group = document.get("source_group")
        if not source_group:
            verified = False
        elif sources.setdefault(source_group, split) != split:
            raise ValueError("A source group cannot occur in both splits")
        pages = document.get("pages")
        if not pages or [page.get("page") for page in pages] != list(range(1, len(pages) + 1)):
            raise ValueError("Reference pages must be complete, ordered, and unique")
        for page in pages:
            if not page.get("regions"):
                raise ValueError("Reference page contains no reading regions")
            region_ids = set()
            for region in page["regions"]:
                identifier = region.get("id")
                if not identifier or identifier in region_ids or not valid_box(region.get("bbox_relative")):
                    raise ValueError("Reference regions require unique IDs and relative bounding boxes")
                region_ids.add(identifier)
                if region.get("readability") not in ("readable", "unreadable", "uncertain"):
                    raise ValueError("Readability must be declared before evaluation")
                if region["readability"] == "readable":
                    if not isinstance(region.get("text"), str) or not region["text"].strip():
                        raise ValueError("Readable text reference is empty")
                    readable_count += 1
                elif not region.get("reason"):
                    raise ValueError("Unreadable or uncertain regions require a reason")
                verified &= _human_reviewed(region)
            field_ids = set()
            for field in page.get("numeric_fields", []):
                identifier = field.get("field_id")
                if (not identifier or identifier in field_ids or not valid_box(field.get("bbox"))
                        or field.get("source_sha256") != sha or field.get("page") != page["page"]
                        or not field.get("type") or "text" not in field):
                    raise ValueError("Numeric fields require a unique location, ID, source and type")
                field_ids.add(identifier)
                if field.get("readability") == "readable":
                    if not isinstance(field["text"], str) or not NUMERIC_TOKEN.search(field["text"]):
                        raise ValueError("Readable numeric field is missing its expected value")
                    numeric_count += 1
                elif field.get("readability") not in ("unreadable", "uncertain") or not field.get("reason"):
                    raise ValueError("Unresolved numeric reference fields require a reason")
                verified &= _human_reviewed(field)
    if not readable_count:
        raise ValueError("Reference contains no readable text")
    return {"reference_status": "HUMAN_VERIFIED" if verified and sets else "PROVISIONAL",
            "tuning_documents": sum(value == "tuning" for value in sets.values()),
            "independent_evaluation_documents": sum(value == "evaluation" for value in sets.values()),
            "accuracy_acceptance_allowed": verified and bool(numeric_count) and "evaluation" in sets.values()}


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


def spatial_numeric_fields(page, reference_fields, source_sha256, page_number):
    """Match OCR spans geometrically, then join fragments inside each field."""
    if not page or page.get("error") or page.get("status") == "blank":
        return []
    width, height = page["width"], page["height"]
    if width <= 0 or height <= 0:
        raise ValueError("Prediction dimensions are invalid")
    words = page.get("words", [])
    if not words and page.get("regions") and "words" in page["regions"][0]:
        words = [word for region in page["regions"] for word in region["words"]]
    words = words + [cell for table in page.get("tables", []) for cell in table.get("cells", [])]
    by_field = {}
    extras = []
    for index, word in enumerate(words):
        token = word.get("text", word.get("normalized_text", word.get("raw_text", "")))
        raw = word.get("raw_ocr_text", word.get("raw_text", token))
        value = _numeric_fragment(token)
        raw_value = _numeric_fragment(raw)
        if not value and not raw_value:
            continue
        a, b, c, d = word.get("source_bbox") or word["bbox"]
        box = (a / width, b / height, c / width, d / height)
        if not valid_box(box):
            raise ValueError("Numeric prediction bounding box is invalid")
        matches = []
        for field in reference_fields:
            x0, y0, x1, y1 = field["bbox"]
            area = max(0, min(x1, box[2]) - max(x0, box[0])) * max(0, min(y1, box[3]) - max(y0, box[1]))
            span_area = (box[2] - box[0]) * (box[3] - box[1])
            field_area = (x1 - x0) * (y1 - y0)
            if area >= (min(span_area, field_area) if "row" in word else span_area) * .35:
                matches.append((area, field["field_id"]))
        fragment = (box, value, raw_value, bool(word.get("needs_review") or word.get("review_required")))
        if len(matches) == 1:
            by_field.setdefault(matches[0][1], []).append(fragment)
        elif NUMERIC_TOKEN.search(value) or NUMERIC_TOKEN.search(raw_value):
            extras.append((f"extra:{index}", [fragment]))
    fields = []
    for identifier, fragments in [*by_field.items(), *extras]:
        # Numeric subexpressions are LTR even in RTL paragraphs.
        fragments.sort(key=lambda part: part[0][0])
        text = " ".join(f[1] for f in fragments if f[1])
        raw_text = " ".join(f[2] for f in fragments if f[2])
        if not NUMERIC_TOKEN.search(text) and not NUMERIC_TOKEN.search(raw_text):
            continue
        bbox = [min(f[0][0] for f in fragments), min(f[0][1] for f in fragments),
                max(f[0][2] for f in fragments), max(f[0][3] for f in fragments)]
        fields.append({"source_sha256": source_sha256, "page": page_number, "field_id": identifier,
                       "bbox": bbox, "text": text, "raw_text": raw_text,
                       "review_required": any(f[3] for f in fragments)})
    return fields


def _numeric_fragment(token):
    if re.fullmatch(r"[()\[\]+\-−‐‑–/.٬٫]+", token):
        return token
    spans = list(NUMERIC_TOKEN.finditer(token))
    if not spans:
        return ""
    first, last = spans[0].start(), spans[-1].end()
    if first and token[first - 1] in "([":
        first -= 1
    if last < len(token) and token[last] in ")]":
        last += 1
    # Keep the written separators of a compound number, even when malformed.
    return token[first:last]


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
    raw_counts, canonical_counts = [], []
    for key, expected in refs.items():
        actual = observed.get(key, {"text": "", "raw_text": "", "review_required": False})
        r, h = expected["text"], actual["text"]
        raw = actual.get("raw_text", h)
        normalized_r, normalized_h = r.translate(DIGIT_MAP), h.translate(DIGIT_MAP)
        review = bool(actual.get("review_required"))
        auto_accepted = bool(h) and not review and not actual.get("unresolved", False)
        matched = normalized_r == normalized_h
        counts["numeric_field_exact_match"] += r == raw
        counts["canonical_exact_match"] += matched
        counts["accepted_exact_match"] += matched and auto_accepted
        counts["auto_accepted_numeric_fields"] += auto_accepted
        counts["missing_numeric_fields"] += not h
        counts["digit_style_only_fields"] += matched and r != h
        counts["silent_numeric_errors"] += bool(h) and not matched and not review
        counts["review_required_numeric_fields"] += review
        raw_counts.append(edit_counts(r, raw))
        canonical_counts.append(edit_counts(normalized_r, normalized_h))
        if normalized_r.startswith("0"):
            counts["leading_zero_fields"] += 1
            expected_zeros = len(normalized_r)-len(normalized_r.lstrip("0"))
            observed_zeros = len(normalized_h)-len(normalized_h.lstrip("0"))
            counts["leading_zero_preserved"] += expected_zeros == observed_zeros
        for a, b in _alignment(normalized_r, normalized_h):
            counts["4_to_6_substitutions"] += a == "4" and b == "6"
            counts["6_to_4_substitutions"] += a == "6" and b == "4"
            counts["digit_deletions"] += bool(a and a.isdecimal() and b is None)
            counts["digit_insertions"] += bool(b and b.isdecimal() and a is None)
        details.append({"field_id": key[2], "page": key[1], "source_sha256": key[0],
                        "bbox": expected.get("bbox"), "reference": r, "observed": h,
                        "raw_observed": raw, "observed_bbox": actual.get("bbox"),
                        "exact_match": matched, "raw_exact_match": r == raw, "review_required": review,
                        "auto_accepted": auto_accepted})
    extras = []
    for key in sorted(observed.keys() - refs.keys()):
        extra = observed[key]
        counts["extra_numeric_fields"] += bool(extra["text"])
        counts["digit_insertions"] += sum(char.isdecimal() for char in extra["text"])
        raw_counts.append(edit_counts("", extra.get("raw_text", extra["text"])))
        canonical_counts.append(edit_counts("", extra["text"]))
        if extra["text"] and not extra.get("review_required"):
            counts["silent_numeric_errors"] += 1
        if extra["text"]:
            extras.append(extra)
    names = ("4_to_6_substitutions", "6_to_4_substitutions", "digit_deletions", "digit_insertions",
             "numeric_field_exact_match", "silent_numeric_errors", "review_required_numeric_fields",
             "leading_zero_fields", "leading_zero_preserved", "extra_numeric_fields", "canonical_exact_match",
             "accepted_exact_match", "auto_accepted_numeric_fields", "missing_numeric_fields",
             "digit_style_only_fields")
    return {"status": "MEASURED", "numeric_fields_tested": len(refs),
            **{name: counts[name] for name in names}, "details": details, "extras": extras,
            "raw_character_errors": micro(raw_counts), "canonical_character_errors": micro(canonical_counts),
            "field_exact_match": counts["canonical_exact_match"] / len(refs),
            "raw_field_exact_match": counts["numeric_field_exact_match"] / len(refs),
            "auto_acceptance_coverage": counts["auto_accepted_numeric_fields"] / len(refs)}


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
