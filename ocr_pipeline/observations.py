"""Bounded, conservative evidence propagation; only human review can confirm."""

from __future__ import annotations

import copy
import json

from .benchmark import normalize_evaluation


def carry_observations(
    selected: dict, *others: dict, stage: str, reason: str = ""
) -> dict:
    result = copy.deepcopy(selected)
    sources = (selected, *others)
    reasons = {r for source in sources for r in source.get("uncertainty_reasons", [])}
    if reason:
        reasons.add(reason)
    if any(source.get("needs_review") for source in sources):
        reasons.add("inherited_uncertainty")
    texts = {normalize_evaluation(str(source.get("text", ""))) for source in sources}
    if len(texts) > 1:
        reasons.add("reading_disagreement")
    choices: dict[tuple[str, str], dict] = {}
    for source in sources:
        for choice in [source, *source.get("candidates", [])]:
            item = {
                k: copy.deepcopy(choice[k])
                for k in ("text", "engine", "confidence", "bbox", "psm", "_scale")
                if k in choice
            }
            if "text" not in item:
                continue
            item["stage"] = choice.get("stage", stage)
            choices.setdefault((item.get("engine", "unknown"), item["text"]), item)
    # A word may inherit an engine-labelled regional candidate for the same
    # text. Do not spend the evidence budget twice on its unlabelled copy.
    for key in list(choices):
        if key[0] == "unknown" and any(
            other[0] != "unknown" and other[1] == key[1] for other in choices
        ):
            del choices[key]
    if len(choices) > 8:
        reasons.add("candidate_budget_exceeded")
    result["candidates"] = list(choices.values())[:8]
    result["uncertainty_reasons"] = sorted(reasons)
    result["needs_review"] = bool(reasons)
    result.setdefault(
        "raw_ocr_text",
        others[0].get("raw_ocr_text", others[0].get("text", ""))
        if others
        else selected.get("text", ""),
    )
    histories: dict[str, dict] = {}
    for source in sources:
        for entry in source.get("transformations", []):
            histories.setdefault(
                json.dumps(entry, sort_keys=True, ensure_ascii=False),
                copy.deepcopy(entry),
            )
    if len(histories) > 16:
        reasons.add("transformation_budget_exceeded")
    result["transformations"] = list(histories.values())[:16]
    if len(texts) > 1:
        result["transformations"] = [
            *result.get("transformations", []),
            {
                "stage": stage,
                "reason": reason or "reading_disagreement",
                "before": others[0].get("text", "")
                if others
                else selected.get("text", ""),
                "selected": selected.get("text", ""),
            },
        ][-16:]
    result["uncertainty_reasons"] = sorted(reasons)
    result["needs_review"] = bool(reasons)
    return result
