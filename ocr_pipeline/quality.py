"""Expose recognition uncertainty without editing or certifying OCR content."""
from __future__ import annotations

import math

CONF_REVIEW = 0.75


def word_needs_review(word: dict) -> bool:
    confidence = word.get("confidence")
    valid = (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
             and math.isfinite(confidence) and 0 <= confidence <= 1)
    return bool(word.get("needs_review")) or not valid or confidence < CONF_REVIEW


def page_quality(page: dict, *, handwriting: bool = False) -> dict:
    """Confidence is a warning signal, never a measured correctness percentage."""
    words = page.get("words", [])
    cells = [cell for table in page.get("tables", []) for cell in table.get("cells", [])]
    flagged_words = sum(word_needs_review(word) for word in words)
    flagged_cells = sum(bool(cell.get("needs_review")) for cell in cells)
    # Older legacy exports include table warnings in this combined count but
    # omit the cells themselves. Preserve that evidence without inventing cells.
    reported = page.get("low_confidence_words", 0)
    reported = reported if isinstance(reported, int) and reported >= 0 else 0
    warnings = max(reported, flagged_words + flagged_cells)
    failed = bool(page.get("error"))
    blank = page.get("status") == "blank"
    reasons = []
    if handwriting and not blank:
        reasons.append("declared_handwriting_requires_human")
    if warnings:
        reasons.append("uncertain_recognition")
    status = "failed" if failed else "blank" if blank else "needs_review" if reasons else "unverified"
    return {"status": status, "human_verified": False,
            "handwriting_declared": bool(handwriting), "metadata_available": True,
            "review_required": failed or bool(reasons), "reasons": reasons,
            "flagged_words": flagged_words, "flagged_items": warnings,
            "word_count": len(words), "confidence_is_accuracy": False}


def unknown_quality(*, handwriting: bool = False) -> dict:
    return {"status": "needs_review" if handwriting else "unverified",
            "human_verified": False, "handwriting_declared": bool(handwriting),
            "metadata_available": False, "review_required": True,
            "reasons": ["quality_metadata_unavailable"],
            "flagged_words": None, "flagged_items": None, "word_count": None,
            "confidence_is_accuracy": False}


def quality_notice(quality: dict) -> str:
    if quality.get("handwriting_declared"):
        return "[پیش‌نویس سند دارای دست‌نویس؛ نیازمند بازبینی انسانی]"
    if quality.get("review_required"):
        return "[پیش‌نویس نیازمند بازبینی]"
    return "[متن خودکار؛ تأیید انسانی نشده]"
