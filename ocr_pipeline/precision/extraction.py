"""Opt-in region extraction, preserving the current pipeline and its output contract."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import uuid

import cv2
import numpy as np

from ..layout import detect_tables
from ..tables import cell_bboxes, markdown_table, write_table_outputs
from .engine import LocalRecognizer, file_hash
from .imaging import SourcePages, blank_evidence, uncovered_regions, variants
from .profiles import Options


def normalize_search(text):
    """Derived only: retain lines, separators, leading zeros and word boundaries."""
    table = str.maketrans("يك۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "یک01234567890123456789")
    return "\n".join(re.sub(r"[^\S\n]+", " ", line.translate(table)).strip() for line in text.split("\n"))


def _numeric(text):
    return [token for token in text.split() if any(char.isdecimal() for char in token)]


def _box(words):
    boxes = [word["bbox"] for word in words]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _inside(box, container):
    x, y = (box[0]+box[2])/2, (box[1]+box[3])/2
    return container[0] <= x <= container[2] and container[1] <= y <= container[3]


def _intersection(a, b):
    intersection = max(0, min(a[2],b[2])-max(a[0],b[0])) * max(0, min(a[3],b[3])-max(a[1],b[1]))
    return intersection / max(1, min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])))


def _line_text(words):
    lines = {}
    for word in words:
        lines.setdefault(tuple(word["line_key"]), []).append(word["text"])
    return "\n".join(" ".join(line) for line in lines.values())


def _padded(box, shape):
    h, w = shape
    pad = max(12, round((box[3]-box[1])*.4))
    return [max(0,box[0]-pad), max(0,box[1]-pad), min(w,box[2]+pad), min(h,box[3]+pad)]


def _grid_clear(image, grid):
    ys, xs = grid
    # Every proposed internal boundary must be visible in every corresponding segment.
    # Missing segments may mean merged cells: no invented rectangular structure.
    for y in ys:
        for a, b in zip(xs, xs[1:]):
            strip = image[max(0,y-3):y+4, a:b]
            if strip.size == 0 or np.mean(np.min(strip, axis=0) < 200) < .7:
                return False
    for x in xs:
        for a, b in zip(ys, ys[1:]):
            strip = image[a:b, max(0,x-3):x+4]
            if strip.size == 0 or np.mean(np.min(strip, axis=1) < 200) < .7:
                return False
    return True


def reading_order(regions, *, rtl=True):
    """Recursive whitespace cuts order blocks, never horizontally sort individual words."""
    if any(region.get("kind") == "cell" for region in regions):
        by_table, blocks = {}, []
        for region in regions:
            if region.get("kind") == "cell":
                by_table.setdefault(region["table_id"], []).append(region)
            else:
                blocks.append(region)
        for cells in by_table.values():
            blocks.append({"kind": "table_block", "bbox": _box(cells),
                           "source_order": min(c["source_order"] for c in cells), "cells": cells})
        ordered = []
        for block in reading_order(blocks, rtl=rtl):
            if block["kind"] == "table_block":
                ordered.extend(sorted(block["cells"], key=lambda c: (c["row"], -c["column"] if rtl else c["column"])))
            else:
                ordered.append(block)
        return ordered
    if len(regions) < 2:
        return regions
    def gap(axis):
        intervals = sorted((r["bbox"][axis], r["bbox"][axis+2]) for r in regions)
        end, candidates = intervals[0][1], []
        for start, finish in intervals[1:]:
            if start > end:
                candidates.append((start-end, (start+end)/2))
            end = max(end, finish)
        return max(candidates, default=(0, 0))
    gx, gy = gap(0), gap(1)
    bounds = [min(r["bbox"][i] for r in regions) for i in (0,1)] + [max(r["bbox"][i] for r in regions) for i in (2,3)]
    # A wide vertical gutter wins within a content band; full-width headers force a horizontal cut first.
    axis = 0 if gx[0] > max(24, (bounds[2]-bounds[0])*.035) else 1
    size, cut = gx if axis == 0 else gy
    if size <= 0:
        return sorted(regions, key=lambda r: r.get("source_order", 10**9))
    before = [r for r in regions if r["bbox"][axis+2] <= cut]
    after = [r for r in regions if r["bbox"][axis] >= cut]
    if not before or not after or len(before)+len(after) != len(regions):
        return sorted(regions, key=lambda r: r.get("source_order", 10**9))
    groups = [after, before] if axis == 0 and rtl else [before, after]
    return [r for group in groups for r in reading_order(group, rtl=rtl)]


def _initial_regions(image, words):
    regions, tables, assigned = [], [], set()
    remaining_cells = 2000
    for table_index, grid in enumerate(detect_tables(image), 1):
        ys, xs = grid
        bbox = [xs[0], ys[0], xs[-1], ys[-1]]
        cells = (len(ys)-1)*(len(xs)-1)
        within_budget = cells <= remaining_cells
        clear = within_budget and _grid_clear(image, grid)
        table = {"table_id": table_index, "bbox": bbox, "structure_status": "geometric_candidate" if clear else "needs_review",
                 "rows": len(ys)-1 if clear else None, "columns": len(xs)-1 if clear else None,
                 "cells": [], "reason": ("visible_grid" if clear else "missing_or_merged_boundaries")
                 if within_budget else "table_structure_budget_exhausted"}
        tables.append(table)
        if not clear:
            continue
        remaining_cells -= cells
        for row, col, cell_box in cell_bboxes(grid):
            selected = [word for i, word in enumerate(words) if i not in assigned and _inside(word["bbox"], cell_box)]
            assigned.update(i for i, word in enumerate(words) if _inside(word["bbox"], cell_box))
            region = {"kind": "cell", "bbox": list(cell_box), "words": selected, "table_id": table_index,
                      "row": row, "column": col, "source_order": min((w["order"] for w in selected), default=10**9)}
            regions.append(region)
            table["cells"].append(region)
    lines = {}
    for i, word in enumerate(words):
        if i not in assigned:
            lines.setdefault(tuple(word["line_key"]), []).append(word)
    for line in lines.values():
        regions.append({"kind": "line", "bbox": _box(line), "words": line, "source_order": line[0]["order"]})
    return regions, tables


def choose_observation(attempts):
    """Conservative content selection; all disagreements remain visible to review."""
    usable = [a for a in attempts if not a.get("error")]
    if not usable:
        return {"engine_text_raw": "", "words": [], "attempt_id": None}, ["all_attempts_failed"]
    base = usable[0]
    distinct = {a["engine_text_raw"].strip() for a in usable}
    reasons = ["reading_disagreement"] if len(distinct) > 1 else []
    numeric = {tuple(_numeric(a["engine_text_raw"])) for a in usable}
    if len(numeric) > 1:
        reasons.append("numeric_disagreement")
        return base, reasons
    chosen = base
    for candidate in usable[1:]:
        if not base["words"] and candidate["words"]:
            chosen = candidate
            reasons.append("recovered_unverified_text")
            break
        # Same observation in a different pass is not independent verification.
        covers_base = all(any(_intersection(w["bbox"], other["bbox"]) >= .6
                             for other in candidate["words"]) for w in base["words"])
        base_chars = sum(char.isalnum() for char in base["engine_text_raw"])
        candidate_chars = sum(char.isalnum() for char in candidate["engine_text_raw"])
        confs = [w["confidence_raw"] for w in candidate["words"] if w.get("confidence_raw") is not None]
        old_confs = [w["confidence_raw"] for w in base["words"] if w.get("confidence_raw") is not None]
        if (covers_base and base_chars < candidate_chars <= base_chars * 1.2 and confs and old_confs
                and sum(confs)/len(confs) >= sum(old_confs)/len(old_confs)
                and len(candidate["words"]) >= len(base["words"])):
            chosen = candidate
            reasons.append("greater_spatial_and_text_coverage_candidate")
    return chosen, reasons


def _attempt(recognizer, variant, psm, page_dir, attempt_id, reference_shape, *, languages=None):
    result = recognizer.recognize(variant.image, psm=psm, languages=languages)
    (page_dir / f"{attempt_id}.tsv").write_text(result.pop("tsv_raw"), encoding="utf-8")
    (page_dir / f"{attempt_id}.txt").write_text(result["engine_text_raw"], encoding="utf-8")
    result["attempt_id"] = attempt_id
    result["transform"] = variant.metadata()
    result["raw_artifact"] = f"raw/{page_dir.name}/{attempt_id}.tsv"
    for word in result["words"]:
        word["bbox_variant"] = word["bbox"]
        word["bbox"] = variant.reference_box(word["bbox"], reference_shape)
    return result


def process_page(image, page_number, recognizer, options, directory, *, deadline=None):
    page_dir = directory / "raw" / f"p{page_number:04d}"
    page_dir.mkdir(parents=True)
    h, w = image.shape
    whole = variants(image, [0,0,w,h], options, full_page=True)
    baseline = _attempt(recognizer, next(whole), 3, page_dir, "page-base", image.shape)
    main = baseline
    page_attempts = [baseline]
    if options.orientation_degrees or options.perspective_corners:
        main = _attempt(recognizer, next(whole), 3, page_dir, "page-declared-geometry", image.shape)
        page_attempts.append(main)
    del whole
    evidence = blank_evidence(image)
    regions, tables = _initial_regions(image, main["words"])
    for box in uncovered_regions(image, main["words"]):
        if not any(_intersection(box, r["bbox"]) > .85 for r in regions if r["kind"] == "coverage"):
            regions.append({"kind": "coverage", "bbox": box, "words": [], "source_order": 10**9})
    if not regions and not evidence["verified_uniform_white"]:
        regions.append({"kind": "coverage", "bbox": [0,0,w,h], "words": [], "source_order": 0})
    # Prioritise explicit missing output and numeric fields; all unprocessed regions are still reported.
    regions = reading_order(regions, rtl=options.profile != "english")
    prioritized = sorted(regions, key=lambda r: (r["kind"] != "coverage", not bool(_numeric(_line_text(r["words"])))))
    retry_ids = {id(r) for r in prioritized[:options.max_regions]}
    extra_calls = len(page_attempts)-1
    evidence_budget = 16 * 1024 * 1024 - len(json.dumps(page_attempts, ensure_ascii=False).encode("utf-8"))
    all_words = list(main["words"])
    for order, region in enumerate(regions):
        identity = json.dumps([page_number, region["kind"], region["bbox"], region.get("row"),region.get("column")])
        region["span_id"] = f"P{page_number:04d}-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
        region["page"] = page_number
        region["order"] = order
        initial = {"attempt_id": "page-base-projection", "engine_text_raw": _line_text(region["words"]),
                   "words": copy.deepcopy(region["words"]), "derived_from": main["attempt_id"],
                   "text_representation": "TSV logical line projection", "psm": 3}
        attempts = [initial]  # The full-page observation counts towards the six-attempt region limit.
        if len(page_attempts) == 2:
            original_words = [word for word in baseline["words"] if _inside(word["bbox"], region["bbox"])]
            attempts.append({"attempt_id": "page-original-projection", "engine_text_raw": _line_text(original_words),
                             "words": original_words, "derived_from": baseline["attempt_id"], "psm": 3})
        reasons = ["experimental_profile"]
        if options.handwriting:
            reasons.append("declared_handwriting_requires_human")
        confs = [word["confidence_raw"] for word in region["words"] if word.get("confidence_raw") is not None]
        if confs and min(confs) < 85:
            reasons.append("low_engine_confidence_signal")
        hard = (region["kind"] in ("coverage", "cell") or bool(_numeric(initial["engine_text_raw"])) or
                not confs or min(confs) < 85)
        if hard and id(region) in retry_ids:
            box = _padded(region["bbox"], image.shape)
            psm = 7 if region["kind"] == "line" else 6
            seen = set()
            for variant in variants(image, box, options, remove_rules=region["kind"] == "cell"):
                if deadline is not None and time.monotonic() >= deadline:
                    reasons.append("document_time_budget_exhausted")
                    break
                if len(attempts) >= options.max_attempts or extra_calls >= options.max_extra_calls:
                    reasons.append("retry_budget_exhausted")
                    break
                if evidence_budget <= 0:
                    reasons.append("evidence_budget_exhausted")
                    break
                signature = hashlib.sha256(variant.image.tobytes()).digest()
                if signature in seen:
                    continue
                seen.add(signature)
                attempt_id = f"r{order:04d}-{len(attempts)}"
                extra_calls += 1
                try:
                    result = _attempt(recognizer, variant, psm, page_dir, attempt_id, image.shape)
                    evidence_budget -= len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    # Margin/context is for recognition; never import neighbouring lines into this unit.
                    result["context_text_raw"] = result["engine_text_raw"]
                    result["words"] = [word for word in result["words"] if _inside(word["bbox"], region["bbox"])]
                    result["engine_text_raw"] = _line_text(result["words"])
                    result["text_representation"] = "TSV region projection; exact engine text in context_text_raw"
                    attempts.append(result)
                except Exception as error:
                    attempts.append({"attempt_id": attempt_id, "error": type(error).__name__})
                    reasons.append("attempt_failed")
        elif hard:
            reasons.append("retry_not_run_region_budget")
        selected, disagreement = choose_observation(attempts)
        words = selected["words"]
        if region["kind"] == "coverage":
            # Tiles overlap. Only spatially new words are appended; disagreements stay in evidence.
            unique = []
            for word in words:
                neighbours = [old for old in all_words if _intersection(old["bbox"], word["bbox"]) > .5]
                if not neighbours:
                    unique.append(word)
                    all_words.append(word)
                elif not any(old["text"] == word["text"] for old in neighbours):
                    reasons.append("overlapping_reading_disagreement")
            words = unique
            content = _line_text(words)
        else:
            content = selected["engine_text_raw"]
        if not content.strip():
            reasons.append("no_readable_observation")
        region.update(engine_text_raw=selected["engine_text_raw"], content_text=content, words=words,
                      normalized_text=normalize_search(content), verified_text=None, selected_attempt=selected["attempt_id"],
                      attempts=attempts, review_required=True, review_status="not_reviewed",
                      status="needs_review", reasons=list(dict.fromkeys(reasons+disagreement)), search_text="")
        region["uncertainty_id"] = f"U{page_number:03d}{order+1:03d}"
    content = "\n".join(region["content_text"] for region in regions)
    for table in tables:
        table["cells"] = [{key: cell.get(key) for key in ("span_id", "row", "column", "bbox", "content_text", "review_required")}
                          for cell in table["cells"]]
        for cell in table["cells"]:
            cell.update(raw_text=cell["content_text"], normalized_text=cell["content_text"],
                        row_span=1, column_span=1, needs_review=True)
        table["markdown"] = markdown_table(table) if table["rows"] is not None else ""
    return {"page": page_number, "width": w, "height": h, "coordinate_space": "reference_pixels",
            "reference_image": f"reference/p{page_number:04d}.png", "engine_text_raw": main["engine_text_raw"],
            "page_attempts": page_attempts, "text": content, "content_text": content,
            "normalized_text": normalize_search(content), "search_text": "", "regions": regions,
            "words": [word for region in regions for word in region["words"]], "tables": tables,
            "engines": ["tesseract"], "status": "blank" if evidence["verified_uniform_white"] else "needs_review",
            "blank_evidence": evidence, "low_confidence_words": len(regions), "handwriting_declared": options.handwriting,
            "reading_order_status": "experimental_geometry_and_TSV; requires_reference_validation",
            "uncertainties": [{"uncertainty_id": r["uncertainty_id"], "span_id": r["span_id"], "page": page_number,
                               "bbox": r["bbox"], "observed_text": r["content_text"], "verified_text": None,
                               "reasons": r["reasons"], "review_status": "not_reviewed",
                               "alternatives": list(dict.fromkeys(a.get("engine_text_raw", "") for a in r["attempts"] if not a.get("error")))}
                              for r in regions],
            "metrics": {"extra_ocr_calls": extra_calls, "regions": len(regions),
                        "unprocessed_regions": sum("retry_not_run_region_budget" in r["reasons"] for r in regions)}}


def export_segments(pages):
    parts = []
    def add(kind, text, **metadata):
        parts.append({"kind": kind, "text": text, **metadata})
    add("annotation", "پیش‌نویس نیازمند بازبینی\n")
    for page in pages:
        add("annotation", "="*20 + f" PAGE {page['page']} " + "="*20 + "\n", page=page["page"])
        if page.get("error"):
            add("annotation", "[استخراج این صفحه انجام نشد]\n", page=page["page"])
        for index, region in enumerate(page.get("regions", [])):
            if index:
                add("content", "\n", page=page["page"])
            add("content", region["content_text"], span_id=region["span_id"], page=page["page"])
            label = f"[{region['uncertainty_id']}]" if region["content_text"] else f"[ناحیه ناخوانده {region['uncertainty_id']}]"
            add("annotation", label, span_id=region["span_id"], page=page["page"])
        add("annotation", "\n", page=page["page"])
    add("annotation", "\nبازبینی: نشانه‌ها به صفحه و ناحیهٔ متن در JSON ارجاع دارند؛ تأیید صحت خودکار نیست.\n")
    return parts


def process_document(source: Path, out_dir: Path, *, options=None, temp_root: Path,
                     max_pages=0, progress_cb=None, recognizer=None):
    options = options or Options()
    source, out_dir, temp_root = Path(source).resolve(strict=True), Path(out_dir).resolve(), Path(temp_root).resolve(strict=True)
    if source == out_dir or source in out_dir.parents or source == temp_root:
        raise ValueError("Output and temporary paths must be separate from the source")
    source_sha = file_hash(source)
    # A different page limit is a different immutable extraction revision.
    implementation = hashlib.sha256()
    package = Path(__file__).resolve().parent
    for path in [package/name for name in ("engine.py", "imaging.py", "extraction.py", "profiles.py")] + [package.parent/"layout.py", package.parent/"tables.py"]:
        implementation.update(path.name.encode()); implementation.update(path.read_bytes())
    implementation_hash = implementation.hexdigest()
    revision = hashlib.sha256(f"{source_sha}:{options.revision}:{max_pages}:{implementation_hash}".encode()).hexdigest()[:24]
    snapshots = out_dir / "extractions"
    snapshots.mkdir(parents=True, exist_ok=True)
    final = snapshots / revision
    cached = final / "extraction.json"
    if cached.is_file():
        if cached.stat().st_size > options.max_snapshot_bytes:
            raise ValueError("Cached extraction exceeds snapshot budget")
        return json.loads(cached.read_text("utf-8"))
    recognizer = recognizer or LocalRecognizer(options, temp_root)
    if shutil.disk_usage(out_dir).free < options.min_free_bytes + options.max_pixels * 8:
        raise OSError("Insufficient output storage")
    stage = snapshots / f".pending-{uuid.uuid4().hex}"
    stage.mkdir()
    (stage / "reference").mkdir()
    started = time.monotonic()
    pages = []
    metadata_bytes = 0
    try:
        with SourcePages(source, options) as document:
            count = min(max_pages, document.count) if max_pages > 0 else document.count
            if progress_cb:
                progress_cb({"type": "start", "pages": count, "dpi": options.dpi})
            for index in range(count):
                failure_reason = "page_extraction_failed"
                try:
                    if time.monotonic()-started >= options.max_document_seconds:
                        failure_reason = "document_time_budget_exhausted"
                        raise RuntimeError("Document time budget exhausted")
                    if metadata_bytes >= options.max_snapshot_bytes // 2:
                        failure_reason = "document_metadata_budget_exhausted"
                        raise RuntimeError("Document metadata budget exhausted")
                    if sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) > options.max_artifact_bytes:
                        failure_reason = "document_artifact_budget_exhausted"
                        raise OSError("Document artifact budget exhausted")
                    if shutil.disk_usage(out_dir).free < options.min_free_bytes + options.max_pixels * 8:
                        failure_reason = "storage_reserve_reached"
                        raise OSError("Output storage reserve reached")
                    image, metadata = document.read(index)
                    reference = stage / "reference" / f"p{index+1:04d}.png"
                    if not cv2.imwrite(str(reference), image):
                        raise OSError("Reference image could not be saved")
                    page = process_page(image, index+1, recognizer, options, stage,
                                        deadline=started+options.max_document_seconds)
                    page["reference"] = {**metadata, "sha256": file_hash(reference), "width": image.shape[1], "height": image.shape[0]}
                    page["filename"] = source.name
                    page["table_exports"] = []
                    for table in page["tables"]:
                        if table["structure_status"] != "geometric_candidate":
                            continue
                        filenames = write_table_outputs(table, stage / "tables", page_no=index+1)
                        renamed = []
                        for name in filenames:
                            original_path = stage / "tables" / name
                            draft_name = "draft_" + name
                            if original_path.suffix == ".html":
                                original_path.write_text("<p dir='rtl'>پیش‌نویس نیازمند بازبینی؛ شناسهٔ سلول‌ها در JSON همراه است.</p>\n" + original_path.read_text("utf-8"), encoding="utf-8")
                            original_path.rename(original_path.with_name(draft_name))
                            renamed.append(draft_name)
                        page["table_exports"].extend(renamed)
                    page["table_export_status"] = {"xlsx": "available" if any(name.endswith(".xlsx") for name in page["table_exports"])
                                                    else "not_available_optional_writer_or_no_reliable_table"}
                    size = len(json.dumps(page, ensure_ascii=False).encode("utf-8"))
                    if metadata_bytes+size > options.max_snapshot_bytes // 2:
                        failure_reason = "document_metadata_budget_exhausted"
                        raise RuntimeError("Document metadata budget exhausted")
                    metadata_bytes += size
                    pages.append(page)
                    if progress_cb:
                        progress_cb({"type": "page_ok", "page": index+1, "words": len(page["words"]),
                                     "needs_review": bool(page["regions"]), "tables": len(page["tables"]), "source": "tesseract"})
                except Exception as error:
                    logging.getLogger(__name__).exception("Experimental extraction failed on page %d", index+1)
                    # A failed page has an explicit placeholder; subsequent pages are still attempted.
                    pages.append({"page": index+1, "filename": source.name, "status": "failed",
                                  "error": type(error).__name__, "reason": failure_reason, "text": "", "regions": []})
                    if progress_cb:
                        progress_cb({"type": "page_error", "page": index+1, "error": failure_reason})
        payload = {"schema_version": 2, "extraction_revision": revision,
                   "document": {"id": source_sha, "source_sha256": source_sha, "filename": source.name,
                                "pages": count, "source_dpi": options.dpi, "engine": "tesseract"},
                   "profile": options.manifest(), "provenance": {**recognizer.manifest, "implementation_sha256": implementation_hash}, "pages": pages,
                   "search": {"automatic_acceptance_validated": False, "entries": [], "state": "human_approval_required"},
                   "performance": {"total_seconds": round(time.monotonic()-started, 2), "ocr_calls": recognizer.calls,
                                   "failed_pages": sum(bool(p.get("error")) for p in pages),
                                   "low_confidence_regions": sum(len(p.get("regions", [])) for p in pages)}}
        payload["export_segments"] = export_segments(pages)
        # Compact JSON keeps the enforced byte limit independent of indentation depth.
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(serialized) > options.max_snapshot_bytes:
            raise RuntimeError("Extraction snapshot exceeds metadata budget")
        (stage / "extraction.txt").write_text("".join(part["text"] for part in payload["export_segments"]), encoding="utf-8")
        (stage / "extraction.json").write_bytes(serialized)
        if source_sha != file_hash(source):
            raise RuntimeError("Source changed during extraction")
        try:
            stage.rename(final)
        except FileExistsError:
            return json.loads((final / "extraction.json").read_text("utf-8"))
        if progress_cb:
            progress_cb({"type": "done", "pages": count, "pages_ok": count-payload["performance"]["failed_pages"],
                         "words": sum(len(p.get("words", [])) for p in pages),
                         "seconds": payload["performance"]["total_seconds"]})
        return payload
    finally:
        # Only the newly-created, verified staging directory may be removed.
        if stage.exists() and stage.resolve().parent == snapshots.resolve() and stage.name.startswith(".pending-"):
            shutil.rmtree(stage)
