"""Offline, document-split evaluation. Reference and predictions stay outside Git.

Reference schema: evaluation.validate_reference plus each region's text, id and
bbox_relative [left, top, right, bottom]. Predictions: a list of {source_sha256,
before, after} paths. No missing page/region is silently excluded.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_pipeline.evaluation import edit_counts, micro, validate_reference


def scope_text(page, relative, *, raw=False):
    if not page or page.get("error"):
        return ""
    width, height = page["width"], page["height"]
    if len(relative) != 4 or not (0 <= relative[0] < relative[2] <= 1 and 0 <= relative[1] < relative[3] <= 1):
        raise ValueError("Reference scope must be an explicit relative bounding box")
    def inside(word):
        a,b,c,d = word["bbox"]
        return relative[0] <= (a+c)/2/width <= relative[2] and relative[1] <= (b+d)/2/height <= relative[3]
    if raw:
        if not page.get("page_attempts"):
            return None  # Older processed text is not relabeled as raw engine text.
        groups = [page["page_attempts"][0]["words"]]
    elif page.get("regions") and "content_text" in page["regions"][0]:
        groups = [region["words"] for region in page["regions"]]
    else:
        # Reproduce the legacy producer's actual logical order. Storage-array order
        # is not its delivered text order and must not inflate the baseline error.
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


def evaluate(reference, prediction_paths):
    status = validate_reference(reference)
    predictions = {row["source_sha256"]:row for row in prediction_paths}
    if len(predictions) != len(prediction_paths):
        raise ValueError("Duplicate prediction document identity")
    rows = []
    unreadable = []
    for document in reference["documents"]:
        identity = document["source_sha256"]
        if identity not in predictions:
            raise ValueError("Missing prediction mapping; declare an empty output explicitly")
        outputs = {}
        for label in ("before", "after"):
            path = predictions[identity].get(label)
            data = json.loads(Path(path).read_text("utf-8")) if path else {"pages":[]}
            source_hash = data.get("document", {}).get("source_sha256")
            if source_hash and source_hash != identity:
                raise ValueError("Prediction belongs to a different source document")
            outputs[label] = {p["page"]:p for p in data["pages"]}
        for page in document["pages"]:
            for region in page["regions"]:
                identifier = {"source_sha256":identity,"page":page["page"],"region_id":region["id"],
                              "split":document["split"],"groups":document.get("groups", [])}
                if region["readability"] != "readable":
                    unreadable.append({**identifier,"readability":region["readability"]})
                    continue
                reference_text = region["text"].replace("\r\n", "\n")
                item = {**identifier,"N":len(reference_text),"metrics":{}}
                for label in ("before", "after"):
                    page_output = outputs[label].get(page["page"])
                    content = scope_text(page_output,region["bbox_relative"])
                    raw = scope_text(page_output,region["bbox_relative"],raw=True)
                    item["metrics"][label] = {"CER_CONTENT":edit_counts(reference_text,content),
                                               "WER_CONTENT":edit_counts(reference_text.split(),content.split()),
                                               "CER_ENGINE_RAW":edit_counts(reference_text,raw) if raw is not None else None}
                rows.append(item)
    aggregated = {}
    for split in ("tuning", "evaluation"):
        selected = [row for row in rows if row["split"] == split]
        aggregated[split] = {"regions":len(selected)}
        for label in ("before", "after"):
            aggregate = {}
            for name in ("CER_CONTENT", "WER_CONTENT", "CER_ENGINE_RAW"):
                counts = [row["metrics"][label][name] for row in selected]
                aggregate[name] = micro(counts) if counts and all(c is not None for c in counts) else {"status":"NOT_MEASURED"}
            aggregated[split][label] = aggregate
    groups = {}
    for group in sorted({g for row in rows for g in row["groups"]}):
        selected = [row for row in rows if group in row["groups"]]
        groups[group] = {"regions":len(selected),"source_documents":len({row["source_sha256"] for row in selected}),
                         **{label:micro([row["metrics"][label]["CER_CONTENT"] for row in selected]) for label in ("before","after")}}
    return {**status, "normalization":"CRLF_to_LF_only", "representation":"TSV/word strings in declared spatial scopes; no confidence filtering",
            "scope":"all declared readable regions on all reference pages; not whole-page accuracy",
            "micro":aggregated,"groups":groups,"regions":rows,"unreadable_or_uncertain":unreadable,
            "NUMERIC_QUALITY":"NOT_MEASURED_without_location_linked_human_numeric_fields",
            "READING_ORDER_AND_LINE_COVERAGE":"NOT_MEASURED_without_reference_line_identities",
            "CER_EXPORT_LITERAL":"NOT_MEASURED_reference_does_not_cover_complete_export",
            "quality_acceptance":"NOT_ESTABLISHED"}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--reference",type=Path,required=True)
    parser.add_argument("--predictions",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    report=evaluate(json.loads(args.reference.read_text("utf-8")),json.loads(args.predictions.read_text("utf-8")))
    with args.output.open("x",encoding="utf-8") as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2)
    print(json.dumps({"reference_status":report["reference_status"],"quality_acceptance":report["quality_acceptance"]}))


if __name__ == "__main__":
    main()
