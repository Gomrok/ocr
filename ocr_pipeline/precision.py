"""Evidence-preserving local line/cell reconciliation on original PDF pixels."""

from __future__ import annotations

import copy
import math
import re
import time

import cv2
import numpy as np
import pymupdf

from . import engines as E
from . import preprocess as P
from . import tables as T

MAX_LINES = 2048
MAX_TILES = 128
PAGE_SECONDS = 2400
MAX_NUMERIC_CHECKS = 256
TILE_SIDE = 1024
TILE_OVERLAP = 128


def detail_dpi(pdf_page, requested):
    """Keep all native scan detail; do not treat enlargement as new information."""
    dpi = min(600, requested)
    if not pdf_page.get_text("text").strip():
        for image in pdf_page.get_image_info():
            box = pymupdf.Rect(image["bbox"])
            if (
                box.width > 0
                and box.height > 0
                and box.get_area() >= 0.8 * pdf_page.rect.get_area()
            ):
                native = max(
                    image["width"] * 72 / box.width, image["height"] * 72 / box.height
                )
                # At least 1.5 samples per native pixel, plus >=300 DPI for small print.
                dpi = min(dpi, max(300, math.ceil(native * 1.5)))
                break
    return dpi


def encode(image):
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("Cannot encode recognition image")
    return data.tobytes()


def canonical(text):
    """Compare observed characters, not spelling guesses or arbitrary punctuation."""
    return " ".join(
        E.display_text(text).replace("\u0640", "").replace("\u200c", " ").split()
    )


def reconcile(readings: list[dict], original: str = "") -> dict:
    """Only cross-engine agreement may clear an uncertainty flag."""
    readings = [r for r in readings if str(r.get("text", "")).strip()]
    if not readings:
        return {
            "text": original,
            "needs_review": True,
            "reason": "no_reading",
            "candidates": [],
        }
    groups = {}
    for reading in readings:
        groups.setdefault(canonical(reading["text"]), []).append(reading)
    independent = [g for g in groups.values() if len({r['engine'] for r in g
        if type(r.get('confidence')) in (int,float) and math.isfinite(r['confidence']) and .75<=r['confidence']<=1})>=2]
    if independent:
        # Prefer preservation of the original when several independent matches exist.
        group = max(
            independent,
            key=lambda g: (
                canonical(g[0]["text"]) == canonical(original),
                len(canonical(g[0]["text"])),
            ),
        )
        # Paddle's line decoder preserves spacing independently of Tesseract word boxes.
        selected = next((r for r in group if r["engine"] == "paddleocr"), group[0])
        # Agreement on one variant must not hide contradictory observed pixels.
        uncertain = len(groups) > 1
        reason = "conflicting_agreement" if uncertain else "independent_agreement"
    elif original:
        same = [
            r
            for r in readings
            if original and canonical(r["text"]) == canonical(original)
        ]
        selected = same[0] if same else {"text": original}
        uncertain, reason = True, "engine_disagreement"
    else:
        # A confidence score alone is not proof. Keep the observed alternative
        # as provisional text and always expose its disagreement to the owner.
        selected = readings[0]
        uncertain, reason = True, "engine_disagreement"
    unique = {}
    inherited = {reason for r in readings for reason in r.get('uncertainty_reasons', [])}
    if any(r.get('needs_review') for r in readings):
        inherited.add('inherited_uncertainty')
    for reading in readings:
        key = (reading["engine"], reading["text"])
        unique.setdefault(key, {"engine": reading["engine"], "text": reading["text"],
                                'confidence':reading.get('confidence')})
    return {
        "text": E.display_text(selected["text"]),
        "needs_review": uncertain or bool(inherited),
        'uncertainty_reasons': sorted(inherited | ({reason} if uncertain else set())),
        "reason": reason,
        "candidates": list(unique.values())[:8],
    }


def source_box(box, page):
    """Undo the old pipeline's full-page affine deskew for an original-PDF overlay."""
    angle = page.get("analysis", {}).get("skew_angle", 0)
    if not any(op.startswith("deskew(") for op in page.get("preprocess_ops", [])):
        return list(box)
    matrix = cv2.invertAffineTransform(
        cv2.getRotationMatrix2D((page["width"] / 2, page["height"] / 2), angle, 1)
    )
    x0, y0, x1, y1 = box
    points = np.float64([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]]) @ matrix.T
    return [
        max(0, float(points[:, 0].min())),
        max(0, float(points[:, 1].min())),
        min(page["width"], float(points[:, 0].max())),
        min(page["height"], float(points[:, 1].max())),
    ]


def bounds(polygon):
    return [
        min(p[0] for p in polygon),
        min(p[1] for p in polygon),
        max(p[0] for p in polygon),
        max(p[1] for p in polygon),
    ]


def overlap(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    return intersection / max(
        1, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    )


def merge_detections(detections):
    """Join overlapping tile detections, never identical text at different positions."""
    groups = []
    for detection in sorted(
        detections, key=lambda d: (bounds(d["polygon"])[1], bounds(d["polygon"])[0])
    ):
        box = bounds(detection["polygon"])
        matches = []
        for group in groups:
            other = group["bbox"]
            height = min(box[3] - box[1], other[3] - other[1])
            vertical = min(box[3], other[3]) - max(box[1], other[1])
            if vertical > 0.6 * height and overlap(box, other) > 0.45:
                matches.append(group)
        if not matches:
            groups.append({"bbox": box, "readings": [detection]})
        else:
            # A later full-width detection can bridge two earlier tile fragments.
            # Coalesce both, otherwise the same line is exported twice.
            matched = matches[0]
            boxes = [box] + [g["bbox"] for g in matches]
            matched["bbox"] = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
            matched["readings"].append(detection)
            for other in matches[1:]:
                matched["readings"].extend(other["readings"])
                groups.remove(other)
    return sorted(groups, key=lambda d: (d["bbox"][1], -d["bbox"][2]))


def uncovered_ink(image, boxes):
    """Surface possible omissions even when neither OCR engine returned text."""
    height, width = image.shape[:2]
    if max(height, width) > TILE_SIDE:
        detections = []
        step = TILE_SIDE - TILE_OVERLAP
        tiles = ((x, y) for y in range(0, height, step) for x in range(0, width, step))
        for index, (x, y) in enumerate(tiles):
            if index >= MAX_TILES:
                break  # enhance_page reports the same shared coverage tile budget.
            right, bottom = min(width, x + TILE_SIDE), min(height, y + TILE_SIDE)
            local_boxes = [
                [a - x, b - y, c - x, d - y]
                for a, b, c, d in boxes
                if a < right and c > x and b < bottom and d > y
            ]
            for a, b, c, d in uncovered_ink(image[y:bottom, x:right], local_boxes):
                detections.append(
                    {
                        "text": "",
                        "polygon": [
                            [a + x, b + y],
                            [c + x, b + y],
                            [c + x, d + y],
                            [a + x, d + y],
                        ],
                    }
                )
        return [group["bbox"] for group in merge_detections(detections)]
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    scale = 1.0
    small = gray
    mask = cv2.adaptiveThreshold(
        small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 16
    )
    for x0, y0, x1, y1 in boxes:
        cv2.rectangle(
            mask,
            (max(0, int(x0 * scale) - 3), max(0, int(y0 * scale) - 3)),
            (
                min(mask.shape[1], math.ceil(x1 * scale) + 3),
                min(mask.shape[0], math.ceil(y1 * scale) + 3),
            ),
            0,
            -1,
        )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    keep = np.zeros(count, np.uint8)
    for i, (_x, _y, w, h, area) in enumerate(stats[1:], 1):
        if (
            3 <= h <= 120
            and 2 <= w <= 250
            and 6 <= area <= 6000
            and 0.08 <= w / h <= 20
        ):
            keep[i] = 255
    mask = cv2.morphologyEx(keep[labels], cv2.MORPH_CLOSE, np.ones((3, 19), np.uint8))
    count, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    remaining = []
    for x, y, w, h, area in stats[1:]:
        if area >= 16 and w >= 4:
            remaining.append([x / scale, y / scale, (x + w) / scale, (y + h) / scale])
    return sorted(remaining, key=lambda b: (b[1], -b[2]))


def _local_read(
    crop, local, paddle, original="", *, cell=False, angle=0.0, numeric_budget=None
):
    if not crop.size:
        return reconcile([], original)
    raw = crop
    rotated = 0.15 < abs(angle) < 10
    if rotated:
        h, w = crop.shape[:2]
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
        cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
        rw, rh = math.ceil(w * cosine + h * sine), math.ceil(w * sine + h * cosine)
        matrix[0, 2] += (rw - w) / 2
        matrix[1, 2] += (rh - h) / 2
        crop = cv2.warpAffine(
            crop,
            matrix,
            (rw, rh),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255) if crop.ndim == 3 else 255,
        )
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    factor = P.optimal_ocr_scale(gray, target_h=40)
    scaled = P.pad_white(P.resize_by_factor(gray, factor), pad=16)
    readings = []
    sources = [raw, crop] if rotated else [crop, scaled]
    if cell:
        sources.append(T.strip_grid_lines(gray))
    for source in sources:
        result = paddle.recognize(encode(source), detect=cell)
        if result:
            ordered = sorted(
                result,
                key=lambda r: (bounds(r["polygon"])[1], -bounds(r["polygon"])[2]),
            )
            readings.append(
                {
                    "engine": "paddleocr",
                    "text": "\n".join(r["text"] for r in ordered),
                    "confidence": sum(r["confidence"] for r in ordered) / len(ordered),
                }
            )
    predicted = readings[0]["text"] if readings else original
    latin = len(re.findall(r"[A-Za-z]", predicted)) > len(
        re.findall(r"[\u0620-\u06ff]", predicted)
    )
    language = "eng" if latin else "fas"
    numeric = bool(re.search(r"\d", E.en_digits_only(predicted + original)))
    numeric_words = []
    for size, psm in ((40, 6 if cell else 7), (56, 6 if cell else 13)):
        factor = P.optimal_ocr_scale(gray, target_h=size)
        image = P.pad_white(P.resize_by_factor(gray, factor))
        result = local.ocr_image(encode(image), psm=psm, langs=language)
        readings.append(
            {
                "engine": "tesseract",
                "text": result.get("text", ""),
                "confidence": E.result_confidence(result),
            }
        )
        if size == 40:
            pad = 30  # P.pad_white defaults to exactly 30 pixels on each axis.
            actual_factor = 1.0 if abs(factor - 1.0) < 0.01 else factor
            for word in result.get("words", []):
                if re.search(r"\d", word["text"]):
                    box = [(n - pad) / actual_factor for n in word["bbox"]]
                    numeric_words.append((word["text"], box))
    # Every numeric line is checked independently; not only low-confidence digits.
    if numeric and re.fullmatch(
        r"[\d\s/.,٬٫:()\-]+", E.en_digits_only(predicted).strip()
    ):
        result = local.ocr_image(
            encode(scaled),
            psm=7,
            langs="fas",
            whitelist="0123456789۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩/.,٬٫:-",
        )
        readings.append(
            {
                "engine": "tesseract",
                "text": result.get("text", ""),
                "confidence": E.result_confidence(result),
                "critical": True,
            }
        )
    if original:
        readings.append({"engine": "tesseract", "text": original, "confidence": 0})
    selected = reconcile(readings, original)
    if numeric_budget is not None:
        checks = []
        for text, box in numeric_words:
            if numeric_budget["remaining"] <= 0:
                checks.append(
                    {
                        "text": text,
                        "needs_review": True,
                        "reason": "numeric_budget_exceeded",
                    }
                )
                continue
            numeric_budget["remaining"] -= 1
            x0, y0, x1, y1 = box
            fragment = gray[
                max(0, int(y0) - 3) : min(gray.shape[0], math.ceil(y1) + 3),
                max(0, int(x0) - 3) : min(gray.shape[1], math.ceil(x1) + 3),
            ]
            checked = _local_read(fragment, local, paddle, text)
            checks.append({"text": text, **checked})
            prior = E.display_text(text)
            if not checked["needs_review"] and selected["text"].count(prior) == 1:
                selected["text"] = selected["text"].replace(prior, checked["text"], 1)
        observed = set(
            re.findall(
                r"\d[\d/.,٬٫:-]*",
                E.en_digits_only(
                    " ".join(r["text"] for r in readings) + " " + original
                ),
            )
        )
        covered = {
            token
            for text, _ in numeric_words
            for token in re.findall(r"\d[\d/.,٬٫:-]*", E.en_digits_only(text))
        }
        for token in sorted(observed - covered):
            checks.append(
                {
                    "text": E.display_text(token),
                    "needs_review": True,
                    "reason": "numeric_location_unresolved",
                }
            )
        if any(c["needs_review"] for c in checks):
            selected["needs_review"] = True
            selected["reason"] = "numeric_verification_incomplete"
        selected["numeric_checks"] = checks
    return selected


def enhance_page(pdf_page, page, local, paddle):
    """Additional coverage pass; legacy page data is retained for provenance."""
    started = time.monotonic()
    width, height = page["width"], page["height"]
    # Direct render avoids treating a 2000-DPI enlargement as new source detail.
    dpi = detail_dpi(pdf_page, page["requested_dpi"])
    scale = dpi / 72
    if pdf_page.rect.width * pdf_page.rect.height * scale * scale > 80_000_000:
        scale = math.sqrt(80_000_000 / (pdf_page.rect.width * pdf_page.rect.height))
    pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    image = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(
        pix.height, pix.width, 3
    )
    ih, iw = image.shape[:2]
    page["precision_render_dpi"] = round(scale * 72, 2)
    sx, sy = width / iw, height / ih
    detections, warnings = [], []
    preview_scale = min(1.0, TILE_SIDE / max(ih, iw))
    preview = cv2.resize(
        image, None, fx=preview_scale, fy=preview_scale, interpolation=cv2.INTER_AREA
    )
    for reading in paddle.recognize(encode(preview)):
        reading["polygon"] = [
            [x / preview_scale, y / preview_scale] for x, y in reading["polygon"]
        ]
        detections.append(reading)
    step = TILE_SIDE - TILE_OVERLAP
    tiles = [(x, y) for y in range(0, ih, step) for x in range(0, iw, step)]
    if len(tiles) > MAX_TILES:
        warnings.append("coverage_tile_budget_exceeded")
    tiles_done = 0
    for x, y in tiles[:MAX_TILES]:
        if time.monotonic() - started > PAGE_SECONDS or len(detections) > MAX_LINES * 4:
            warnings.append("coverage_budget_exceeded")
            break
        tile = image[y : min(ih, y + TILE_SIDE), x : min(iw, x + TILE_SIDE)]
        if min(tile.shape[:2]) < 20:
            continue
        for reading in paddle.recognize(encode(tile)):
            reading["polygon"] = [[px + x, py + y] for px, py in reading["polygon"]]
            detections.append(reading)
        tiles_done += 1
    groups = merge_detections(detections)
    if len(groups) > MAX_LINES:
        warnings.append("line_review_budget_exceeded")
    tables = page.get("tables", [])
    table_boxes = []
    for table in tables:
        for cell in table.get("cells", []):
            box = source_box(cell["bbox"], page)
            cell["source_bbox"] = box
            table_boxes.append(box)
    old_words = copy.deepcopy(page.get("words", []))
    for word in old_words:
        word["bbox"] = source_box(word["bbox"], page)
    used, units = set(), []
    numeric_budget = {"remaining": MAX_NUMERIC_CHECKS}
    processed_lines = 0
    for group_index, group in enumerate(groups):
        x0, y0, x1, y1 = group["bbox"]
        master_box = [x0 * sx, y0 * sy, x1 * sx, y1 * sy]
        if any(overlap(master_box, b) > 0.65 for b in table_boxes):
            continue
        matches = [
            (index, w)
            for index, w in enumerate(old_words)
            if index not in used and overlap(w["bbox"], master_box) > 0.6
        ]
        used.update(index for index, _ in matches)
        original = "\n".join(E.words_to_lines([w for _, w in matches]))
        margin = max(3, round((y1 - y0) * 0.18))
        crop = image[
            max(0, int(y0) - margin) : min(ih, math.ceil(y1) + margin),
            max(0, int(x0) - margin) : min(iw, math.ceil(x1) + margin),
        ]
        angles = [
            math.degrees(
                math.atan2(
                    r["polygon"][1][1] - r["polygon"][0][1],
                    r["polygon"][1][0] - r["polygon"][0][0],
                )
            )
            for r in group["readings"]
        ]
        if group_index >= MAX_LINES or time.monotonic() - started > PAGE_SECONDS:
            warnings.append("line_review_budget_exceeded")
            # Retain an observed provisional reading and its location even when
            # there is no budget to compare engines. Do not silently drop it.
            result = reconcile(
                [dict(r, engine="paddleocr") for r in group["readings"]], original
            )
            result.update(needs_review=True, reason="line_review_budget_exceeded")
        else:
            result = _local_read(
                crop,
                local,
                paddle,
                original,
                angle=float(np.median(angles)),
                numeric_budget=numeric_budget,
            )
            processed_lines += 1
        units.append(
            {
                "kind": "line",
                "bbox": master_box,
                "source_bbox": master_box,
                "original_ocr_text": original,
                **result,
            }
        )
    # Keep any original text that the second detector did not cover.
    leftovers = {}
    for index, word in enumerate(old_words):
        if index not in used:
            leftovers.setdefault(word.get("line_id") or str(index), []).append(word)
    for words in leftovers.values():
        box = [
            min(w["bbox"][0] for w in words),
            min(w["bbox"][1] for w in words),
            max(w["bbox"][2] for w in words),
            max(w["bbox"][3] for w in words),
        ]
        units.append(
            {
                "kind": "line",
                "bbox": box,
                "source_bbox": box,
                "text": "\n".join(E.words_to_lines(words)),
                "needs_review": True,
                "reason": "second_detector_uncovered",
                "candidates": [],
            }
        )
    known_boxes = [[b[0] / sx, b[1] / sy, b[2] / sx, b[3] / sy] for b in table_boxes]
    known_boxes.extend(
        [u["bbox"][0] / sx, u["bbox"][1] / sy, u["bbox"][2] / sx, u["bbox"][3] / sy]
        for u in units
        if u["text"].strip()
    )
    missing = uncovered_ink(image, known_boxes)
    if missing:
        warnings.append("unrecognized_ink_regions")
    if len(missing) > 128:
        warnings.append("unrecognized_ink_budget_exceeded")
    for box in missing[:128]:
        master_box = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy]
        units.append(
            {
                "kind": "line",
                "bbox": master_box,
                "source_bbox": master_box,
                "text": "",
                "needs_review": True,
                "reason": "unrecognized_ink",
                "candidates": [],
            }
        )
    units.sort(key=lambda u: (u["bbox"][1], -u["bbox"][2]))
    page["pre_precision_text"] = page["text"]
    page["review_units"] = units
    page["lines"] = [u["text"] for u in units]
    page["text"] = page["normalized_text"] = E.display_text("\n".join(page["lines"]))
    page["normalized_en_digits"] = E.en_digits_only(page["text"])
    page["identifiers"] = [
        E.display_text(value)
        for value in E.find_identifiers(page["normalized_en_digits"])
    ]
    page["final_text_source"] = "review_units"
    page["words_source"] = "original_tesseract_pass"
    for table in tables:
        for cell in table.get("cells", []):
            if time.monotonic() - started > PAGE_SECONDS:
                cell["needs_review"] = True
                cell["precision_warning"] = "cell_review_budget_exceeded"
                warnings.append("cell_review_budget_exceeded")
                continue
            bx = cell["source_bbox"]
            x0, y0, x1, y1 = (
                int(bx[0] / sx),
                int(bx[1] / sy),
                math.ceil(bx[2] / sx),
                math.ceil(bx[3] / sy),
            )
            crop = image[max(0, y0) : min(ih, y1), max(0, x0) : min(iw, x1)]
            result = _local_read(
                crop,
                local,
                paddle,
                cell["normalized_text"],
                cell=True,
                numeric_budget=numeric_budget,
            )
            cell["pre_precision_text"] = cell["normalized_text"]
            cell["normalized_text"] = result["text"]
            cell["normalized_en_digits"] = E.en_digits_only(result["text"])
            cell["needs_review"], cell["candidates"] = (
                result["needs_review"],
                result["candidates"],
            )
            cell["reason"] = result["reason"]
            cell["numeric_checks"] = result.get("numeric_checks", [])
        table["markdown"] = T.markdown_table(table)
    page["engines"] = sorted(set(page["engines"]) | {"paddleocr", "tesseract"})
    numeric_checks = [
        check
        for item in units + [c for t in tables for c in t.get("cells", [])]
        for check in item.get("numeric_checks", [])
    ]
    if any(
        check.get("reason") == "numeric_budget_exceeded" for check in numeric_checks
    ):
        warnings.append("numeric_review_budget_exceeded")
    page["ambiguities"] = {
        "lines": sum(bool(u["needs_review"]) for u in units),
        "cells": sum(
            bool(c.get("needs_review")) for t in tables for c in t.get("cells", [])
        ),
        "numeric_checks_pending": sum(bool(c["needs_review"]) for c in numeric_checks),
    }
    page["precision_warnings"] = sorted(set(warnings))
    page["coverage"] = {
        "tiles_total": len(tiles),
        "tiles_processed": tiles_done,
        "detected_lines": len(groups),
        "processed_lines": processed_lines,
        "uncovered_original_groups": len(leftovers),
        "unrecognized_ink_regions": len(missing),
        "numeric_checks": MAX_NUMERIC_CHECKS - numeric_budget["remaining"],
    }
    page["low_confidence_words"] = (
        sum(u["needs_review"] for u in units)
        + sum(c.get("needs_review", False) for t in tables for c in t.get("cells", []))
        + bool(warnings)
    )
    return page
