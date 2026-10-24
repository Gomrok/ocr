"""Conservative, opt-in native-text candidate detector. Suspicion means full OCR."""

from __future__ import annotations

import math
import re
import unicodedata

import numpy as np
import pymupdf

from . import engines as E


def inspect_page(page) -> dict:
    words = page.get_text("words", sort=True)
    images = page.get_image_info()
    kind = (
        "mixed"
        if images and words
        else "scan"
        if images
        else "native_text"
        if words
        else "empty_or_graphic"
    )
    reasons = []
    if images:
        reasons.append("raster_regions_require_ocr")
    if not words or len(" ".join(w[4] for w in words)) < 24:
        reasons.append("insufficient_text")
    if page.rotation or page.cropbox != page.mediabox:
        reasons.append("unvalidated_rotated_or_cropped_geometry")
    text = " ".join(w[4] for w in words)
    if re.search(r"[\u0600-\u06ff]", text):
        reasons.append("native_rtl_requires_representative_validation")
    if any(
        char in "\ufffd\x00" or unicodedata.category(char).startswith("C")
        for char in text
    ):
        reasons.append("invalid_unicode")
    traces = page.get_texttrace()
    if not traces or any(
        trace.get("type") != 0 or trace.get("opacity", 1) < 0.95 for trace in traces
    ):
        reasons.append("invisible_or_unvalidated_text_rendering")
    if len(page.get_drawings()) > 8:
        reasons.append("structured_graphics_require_ocr")
    rect = page.rect
    if rect.width <= 0 or rect.height <= 0:
        reasons.append("invalid_geometry")
    for word in words:
        box = pymupdf.Rect(word[:4])
        if box.is_empty or box.is_infinite or not rect.contains(box):
            reasons.append("invalid_word_geometry")
            break
    # A small pixel probe must contain ink in every proposed word box. Image
    # regions and invisible text are refused above, not accepted by text alone.
    if not reasons:
        scale = min(2.0, math.sqrt(2_000_000 / (rect.width * rect.height)))
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False
        )
        pixels = np.frombuffer(pix.samples_mv, np.uint8).reshape(pix.height, pix.width)
        for word in words:
            x0, y0, x1, y1 = word[:4]
            crop = pixels[
                max(0, int(y0 * scale)) : min(pix.height, math.ceil(y1 * scale)),
                max(0, int(x0 * scale)) : min(pix.width, math.ceil(x1 * scale)),
            ]
            if not crop.size or float((crop < 200).mean()) < 0.01:
                reasons.append("glyph_pixel_coverage_failed")
                break
    return {
        "classification": kind,
        "accepted": not reasons,
        "reasons": sorted(set(reasons)),
        "word_count": len(words),
        "policy_version": 1,
        "limitations": [
            "no_glyph_identity_proof",
            "no_human_confirmation",
            "latin_only_experimental",
        ],
    }


def native_page(page, page_no: int, dpi: int, metrics, assessment: dict) -> dict:
    if not assessment.get("accepted"):
        raise ValueError("NATIVE_TEXT_NOT_ACCEPTED")
    scale = dpi / 72
    words = [
        {
            "text": E.display_text(w[4]),
            "logical_text": w[4],
            "raw_ocr_text": w[4],
            "bbox": [round(v * scale, 3) for v in w[:4]],
            "engine": "native_pdf",
            "line_id": f"{w[5]}:{w[6]}",
            "confidence": None,
            "needs_review": True,
            "uncertainty_reasons": ["native_text_requires_review"],
        }
        for w in page.get_text("words", sort=True)
    ]
    raw = page.get_text(sort=True)
    return {
        "page": page_no,
        "width": math.ceil(page.rect.width * scale),
        "height": math.ceil(page.rect.height * scale),
        "requested_dpi": dpi,
        "effective_dpi": dpi,
        "source_pdf_pt": {"w": page.rect.width, "h": page.rect.height},
        "master_render": False,
        "render_method": "native_pdf_text",
        "text_first": assessment,
        "raw_text": raw,
        "text": E.display_text(raw),
        "normalized_text": E.display_text(raw),
        "normalized_en_digits": E.en_digits_only(raw),
        "words": words,
        "lines": raw.splitlines(),
        "tables": [],
        "engines": ["native_pdf"],
        "confidence": None,
        "low_confidence_words": len(words),
        "preprocess_ops": [],
        "analysis": {},
        "metrics": metrics.report(),
    }
