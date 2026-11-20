"""Evaluate fresh OCR output against a private, document-split reference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_pipeline.evaluation import (
    edit_counts, micro, numeric_field_metrics, spatial_numeric_fields, text_metrics, validate_reference,
)


def scope_text(page, relative, *, raw=False):
    if not page or page.get("error"):
        return ""
    width, height = page["width"], page["height"]

    def inside(word):
        a, b, c, d = word.get("source_bbox") or word["bbox"]
        return relative[0] <= (a+c)/2/width < relative[2] and relative[1] <= (b+d)/2/height < relative[3]

    if raw:
        if not page.get("page_attempts"):
            if "raw_ocr_text" not in next(iter(page.get("words", [])), {}):
                return None
            from ocr_pipeline.engines import words_to_lines
            words = [dict(word, text=word["raw_ocr_text"]) for word in page["words"] if inside(word)]
            return "\n".join(words_to_lines(words))
        groups = [page["page_attempts"][0]["words"]]
    elif page.get("regions") and "content_text" in page["regions"][0]:
        groups = [region["words"] for region in page["regions"]]
    else:
        from ocr_pipeline.engines import words_to_lines
        return "\n".join(words_to_lines([word for word in page.get("words", []) if inside(word)]))

    text_lines = []
    for words in groups:
        lines = {}
        for word in words:
            if inside(word):
                key = str(word.get("line_id", word.get("line_key", "unknown")))
                lines.setdefault(key, []).append(word["text"])
        text_lines.extend(" ".join(line) for line in lines.values())
    return "\n".join(text_lines)


def _predicted_pages(path, identity, expected, errors):
    if not path or not Path(path).is_file():
        errors.append("Missing OCR output")
        return {}, {}
    data = json.loads(Path(path).read_text("utf-8"))
    source_hash = data.get("document", {}).get("source_sha256")
    if source_hash and source_hash != identity:
        raise ValueError("Prediction belongs to a different source document")
    pages = data.get("pages")
    numbers = [p.get("page") for p in pages] if pages else []
    if len(set(numbers)) != len(numbers):
        raise ValueError("Duplicate prediction page")
    if numbers != expected or any(page.get("error") for page in pages):
        errors.append("Missing, failed, extra, or out-of-order OCR page")
    if not any(p.get("status") == "blank" or p.get("text", "").strip() for p in pages or []):
        errors.append("No completed OCR content")
    return {p["page"]:p for p in pages or []}, data.get("performance", {})


def _summary(rows):
    return {
        "pages": len(rows),
        "CER_CONTENT": micro([r["CER_CONTENT"] for r in rows]),
        "WER_CONTENT": micro([r["WER_CONTENT"] for r in rows]),
        "CER_ENGINE_RAW": micro([r["CER_ENGINE_RAW"] for r in rows])
        if rows and all(r["CER_ENGINE_RAW"] is not None for r in rows) else {"status":"NOT_EVALUATED"},
    }


def evaluate(reference, prediction_paths):
    status = validate_reference(reference)
    predictions = {row["source_sha256"]: row for row in prediction_paths}
    sources = {doc["source_sha256"] for doc in reference["documents"]}
    if len(predictions) != len(prediction_paths) or set(predictions) != sources:
        raise ValueError("Prediction mappings must match reference documents exactly")

    errors, documents, unreadable = [], [], []
    all_refs = {"tuning": [], "evaluation": []}
    all_observed = {split: {"before": [], "after": []} for split in all_refs}
    all_text = {split: {"before": [], "after": []} for split in all_refs}
    for document in reference["documents"]:
        identity, split = document["source_sha256"], document["split"]
        expected = [p["page"] for p in document["pages"]]
        outputs = {
            label: _predicted_pages(predictions[identity].get(label), identity, expected, errors)
            for label in ("before", "after")
        }
        row = {
            "source_sha256": identity, "split": split, "role": document.get("role"),
            "source_group": document.get("source_group"), "groups": document.get("groups", []),
            "pages": [], "before": {}, "after": {},
        }
        document_ref, observed = [], {"before": [], "after": []}
        for page in document["pages"]:
            number = page["page"]
            readable = [r for r in page["regions"] if r["readability"] == "readable"]
            text = "\n".join(r["text"] for r in readable)
            fields = page.get("numeric_fields", [])
            ref_fields = [field for field in fields if field["readability"] == "readable"]
            document_ref.extend(ref_fields)
            for item in [*page["regions"], *fields]:
                if item["readability"] != "readable":
                    unreadable.append({"source_sha256": identity, "page": number, **item})
            entry = {"page": number, "regions": len(readable)}
            for label, (pages, _) in outputs.items():
                result = pages.get(number, {})
                text_out = result.get("text", "") if "error" not in result else ""
                raw = result.get("raw_text")
                metrics = text_metrics(text, text_out)
                raw_metrics = text_metrics(text, raw)["CER"] if isinstance(raw, str) else None
                missing_phrases = [phrase for phrase in page.get("required_text", [])
                                   if " ".join(phrase.split()) not in " ".join(text_out.split())]
                nums = spatial_numeric_fields(result, fields, identity, number)
                excluded = {f["field_id"] for f in fields if f["readability"] != "readable"}
                nums = [item for item in nums if item["field_id"] not in excluded]
                observed[label].extend(nums)
                entry[label] = {
                    "CER_CONTENT": metrics["CER"], "WER_CONTENT": metrics["WER"],
                    "CER_ENGINE_RAW": raw_metrics,
                    "numeric": numeric_field_metrics(ref_fields, nums),
                    "missing_required_text": missing_phrases,
                    "regions": [
                        {"id": r["id"], **text_metrics(r["text"], scope_text(result, r["bbox_relative"]))}
                        for r in readable
                    ],
                }
                all_text[split][label].append(entry[label])
            row["pages"].append(entry)
        all_refs[split].extend(document_ref)
        for label in observed:
            all_observed[split][label].extend(observed[label])
            metrics = _summary([p[label] for p in row["pages"]])
            row[label] = metrics | {
                "numeric": numeric_field_metrics(document_ref, observed[label]),
                "performance": outputs[label][1],
            }
        if not document_ref:
            errors.append("Document has no readable numeric reference fields")
        documents.append(row)

    aggregates = {
        split: {
            label: _summary(all_text[split][label]) | {
                "numeric": numeric_field_metrics(all_refs[split], all_observed[split][label]),
            }
            for label in ("before", "after")
        }
        for split in all_refs
    }
    groups = {}
    for group in sorted({g for doc in documents for g in doc["groups"]}):
        selected = [doc for doc in documents if group in doc["groups"]]
        identities = {doc["source_sha256"] for doc in selected}
        groups[group] = {
            "documents": len(selected),
            **{
                label: _summary([page[label] for doc in selected for page in doc["pages"]]) | {
                    "numeric": numeric_field_metrics(
                        [f for items in all_refs.values() for f in items if f["source_sha256"] in identities],
                        [f for items in all_observed.values() for f in items[label] if f["source_sha256"] in identities],
                    )
                }
                for label in ("before", "after")
            },
        }
    return {
        **status, "input_status": "FAIL" if errors else "MEASURED", "errors": sorted(set(errors)),
        "quality_acceptance": "NOT_ESTABLISHED",
        "normalization": "numeric_digit_shape_only; text_CRLF_to_LF_only",
        "documents": documents, "micro": aggregates, "groups": groups,
        "unreadable_or_uncertain": unreadable,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = evaluate(
            json.loads(args.reference.read_text("utf-8-sig")),
            json.loads(args.predictions.read_text("utf-8-sig")),
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        report = {"input_status":"FAIL", "errors":[str(exc)], "quality_acceptance":"NOT_ESTABLISHED"}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key:report[key] for key in ("input_status", "quality_acceptance")}))
    # This command emits diagnostic metrics. Release requires provenance preflight.
    return 1


if __name__ == "__main__":
    sys.exit(main())
