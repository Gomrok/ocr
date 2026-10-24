"""Explicit synthetic/common labelled schema, not a document classifier."""

from __future__ import annotations

import copy
import re

from . import validators as V
from .benchmark import normalize_evaluation

SCHEMA = "common_labeled_v1"
REGISTRY: tuple[dict, ...] = tuple(
    {
        "document_type": SCHEMA,
        "schema_version": 1,
        "field_id": name,
        "aliases": aliases,
        "value_type": kind,
        "required_context": context,
        "parser_strategy": "exact_label_colon_single_observation",
    }
    for name, aliases, kind, context in (
        (
            "document_number",
            ("Document Number", "شماره سند"),
            "identifier",
            ("identifier_schema",),
        ),
        ("date", ("Date", "تاریخ"), "date", ("calendar",)),
        (
            "container",
            ("Container", "شماره کانتینر"),
            "identifier",
            ("iso6346_applicable",),
        ),
        (
            "net_weight",
            ("Net Weight", "وزن خالص"),
            "decimal",
            ("decimal_separator", "unit"),
        ),
        (
            "gross_weight",
            ("Gross Weight", "وزن ناخالص"),
            "decimal",
            ("decimal_separator", "unit"),
        ),
        ("currency", ("Currency", "ارز"), "currency", ("currency_reference",)),
        ("amount", ("Amount", "مبلغ"), "decimal", ("decimal_separator", "currency")),
        (
            "hs",
            ("HS Code", "کد تعرفه"),
            "identifier",
            ("country", "document_type", "edition"),
        ),
    )
)


def extract(observations: list[dict], *, document_type: str, context=None) -> dict:
    if document_type != SCHEMA:
        return {
            "status": "UNSUPPORTED_DOCUMENT_SCHEMA",
            "fields": [],
            "schema": document_type,
        }
    context = context or {}
    fields = []
    for spec in REGISTRY:
        matches = []
        for item in observations:
            text = item["logical_text"]
            for alias in spec["aliases"]:
                match = re.fullmatch(
                    re.escape(alias) + r"\s*:\s*(.+)", text, re.IGNORECASE
                )
                if match:
                    matches.append((item, match.group(1), match.span(1)))
                    break
        base = {
            "document_id": observations[0]["document_id"] if observations else None,
            "field_id": spec["field_id"],
            "field_name": spec["field_id"],
            "value_type": spec["value_type"],
            "context": document_type,
            "schema_version": 2,
            "human_review_revision": None,
            "validation_results": [],
            "page": None,
            "source_bbox": None,
            "region_id": None,
            "raw_ocr_text": None,
            "logical_text": None,
            "display_text": None,
            "canonical_value": None,
            "unit": context.get("unit"),
            "candidates": [],
            "recognizer": None,
            "model_hash": None,
            "provenance": {"schema": SCHEMA},
            "review_status": "missing",
            "uncertainty_reasons": ["label_not_found"],
        }
        if not matches:
            fields.append(base)
            continue
        item, value, span = matches[0]
        base.update(copy.deepcopy(item))
        base.update(
            field_id=spec["field_id"],
            field_name=spec["field_id"],
            value_type=spec["value_type"],
            context=document_type,
            canonical_value=normalize_evaluation(value),
            extraction={
                "strategy": spec["parser_strategy"],
                "logical_span": list(span),
                "bbox_precision": "label_and_value_observation",
            },
        )
        reasons = set(item["uncertainty_reasons"])
        if len(matches) != 1:
            reasons.add("multiple_label_observations")
            base["canonical_value"] = None
            base["candidates"] = [
                {"text": v, "page": o["page"], "bbox": o["source_bbox"]}
                for o, v, _ in matches[:8]
            ]
        checks = []
        if spec["field_id"] == "container":
            checks.append(
                V.container(value, iso6346_applicable=context.get("iso6346_applicable"))
            )
        elif spec["field_id"] == "date":
            checks.append(V.date(value, calendar=context.get("calendar", "unknown")))
        elif spec["field_id"] == "hs":
            checks.append(
                V.hs(
                    value,
                    **{
                        k: context.get(k)
                        for k in (
                            "country",
                            "document_type",
                            "edition",
                            "national_length",
                        )
                    },
                )
            )
        elif spec["value_type"] == "decimal":
            number, check = V.parse_decimal(
                value,
                **{
                    k: context.get(k)
                    for k in ("decimal_separator", "grouping_separator")
                },
            )
            checks.append(check)
            if number is not None and len(matches) == 1:
                base["canonical_value"] = str(number)
        else:
            checks.append(V.result("field_context", "unverified_context"))
        if any(check["result"] not in ("valid", "format_valid") for check in checks):
            reasons.add("validation_requires_review")
        if any(not context.get(key) for key in spec["required_context"]):
            reasons.add("missing_schema_context")
        base.update(
            validation_results=checks,
            uncertainty_reasons=sorted(reasons),
            review_status="uncertain" if reasons else "observed",
            human_review_revision=None,
        )
        fields.append(base)
    return {
        "status": "EXTRACTED_COMMON_LABELS_ONLY",
        "schema": SCHEMA,
        "fields": fields,
        "family_accuracy": "NOT_MEASURED",
        "classification": "NOT_IMPLEMENTED",
    }
