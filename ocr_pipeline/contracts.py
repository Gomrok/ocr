"""Additive schema 2. Recognition/format checks never grant human confirmation."""

from __future__ import annotations

import copy
import hashlib

from .benchmark import normalize_evaluation
from .engines import display_text, words_to_lines
from .precision import source_box


def observation(
    document_id: str, page: dict, word: dict, index: int, provenance: dict
) -> dict:
    raw = word.get("raw_ocr_text", word.get("text", ""))
    logical = word.get("logical_text", word.get("text", ""))
    reasons = set(word.get("uncertainty_reasons", []))
    if word.get("needs_review"):
        reasons.add("recognition_uncertainty")
    model_hashes = provenance.get("model_hashes", {})
    if not model_hashes or not provenance.get("source_digest"):
        reasons.add("incomplete_provenance")
    status = "uncertain" if reasons else "observed"
    if not logical.strip():
        status = "unreadable"
        reasons.add("no_readable_observation")
    box = word["bbox"]
    return {
        "document_id": document_id,
        "page": page["page"],
        "source_bbox": source_box(box, page),
        "bbox": list(box),
        "coordinate_space": "original_render_pixels",
        "region_id": word.get("region_id", f"p{page['page']}:w{index}"),
        "observation_id": f"p{page['page']}:w{index}",
        "field_name": None,
        "source_line_id": word.get("line_id"),
        "raw_ocr_text": raw,
        "logical_text": logical,
        "display_text": display_text(logical),
        "canonical_value": normalize_evaluation(logical),
        "value_type": "text",
        "unit": None,
        "candidates": copy.deepcopy(word.get("candidates", [])),
        "recognizer": word.get("engine", provenance.get("engine", "unknown")),
        "model_hash": copy.deepcopy(model_hashes),
        "uncertainty_reasons": sorted(reasons),
        "validation_results": [],
        "review_status": status,
        "human_review_revision": None,
        "provenance": {
            **copy.deepcopy(provenance),
            "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        },
        "transformations": copy.deepcopy(word.get("transformations", [])),
    }


def build_contract(document_id: str, pages: list[dict], provenance: dict) -> dict:
    observations: list[dict] = []
    failed = []
    for page in pages:
        if page.get("error"):
            failed.append(page["page"])
            continue
        observations.extend(
            observation(document_id, page, word, index, provenance)
            for index, word in enumerate(page.get("words", []))
        )
        for table in page.get("tables", []):
            for cell in table.get("cells", []):
                word = dict(
                    cell,
                    text=cell.get("normalized_text", ""),
                    logical_text=cell.get("logical_text", cell.get("raw_text", "")),
                )
                item = observation(
                    document_id, page, word, len(observations), provenance
                )
                item.update(
                    table_id=table["table_id"], row=cell["row"], column=cell["column"]
                )
                observations.append(item)
    lines = []
    groups: dict[tuple, list[dict]] = {}
    for item in observations:
        if "table_id" in item:
            continue
        groups.setdefault(
            (item["page"], item["source_line_id"] or item["observation_id"]), []
        ).append(item)
    for group in groups.values():
        line = copy.deepcopy(group[0])
        logical_words = [dict(item, text=item["logical_text"]) for item in group]
        line["logical_text"] = "\n".join(words_to_lines(logical_words))
        line["display_text"] = display_text(line["logical_text"])
        line["canonical_value"] = normalize_evaluation(line["logical_text"])
        line["raw_ocr_text"] = "\n".join(
            words_to_lines([dict(item, text=item["raw_ocr_text"]) for item in group])
        )
        line["provenance"]["raw_sha256"] = hashlib.sha256(
            line["raw_ocr_text"].encode("utf-8")
        ).hexdigest()
        for key in ("bbox", "source_bbox"):
            line[key] = [
                min(item[key][0] for item in group),
                min(item[key][1] for item in group),
                max(item[key][2] for item in group),
                max(item[key][3] for item in group),
            ]
        line["uncertainty_reasons"] = sorted(
            {r for item in group for r in item["uncertainty_reasons"]}
        )
        line["review_status"] = (
            "uncertain" if line["uncertainty_reasons"] else "observed"
        )
        line["source_observation_ids"] = [item["observation_id"] for item in group]
        line["candidates"] = [c for item in group for c in item["candidates"]][:8]
        lines.append(line)
    # Precision mode deliberately retains original Tesseract words. Only its
    # declared final review units may supply the selected contract/extractor.
    for page in pages:
        if page.get("error") or page.get("final_text_source") != "review_units":
            continue
        lines = [line for line in lines if line["page"] != page["page"]]
        for index, unit in enumerate(page.get("review_units", [])):
            if unit.get("kind") != "line":
                continue
            selected = observation(document_id, page, unit, index, provenance)
            if unit.get("source_bbox"):
                selected["source_bbox"] = copy.deepcopy(unit["source_bbox"])
            selected["provenance"]["selected_source"] = "review_units"
            if "raw_ocr_text" not in unit:
                selected["uncertainty_reasons"] = sorted(
                    set(selected["uncertainty_reasons"])
                    | {"selected_unit_raw_not_available"}
                )
                selected["review_status"] = "uncertain"
            lines.append(selected)
    return {
        "schema_version": 2,
        "contract_scope": "automatic_ocr_snapshot",
        "normalizer": "NFC_digits_v1",
        "document_id": document_id,
        "status": "incomplete" if failed else "observed",
        "failed_pages": failed,
        "observations": observations,
        "line_observations": lines,
        "regional_evidence": [
            {
                "page": page["page"],
                "regions": copy.deepcopy(page.get("observations", [])),
            }
            for page in pages
        ],
        "fields": [],
        "commercial_registration_allowed": False,
    }
