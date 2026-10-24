"""Private, revisioned reviews. Original OCR and uploaded PDFs are immutable."""

from __future__ import annotations

import contextlib
import copy
import hmac
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from . import engines as E
from . import tables as T

STORE_LOCK = threading.RLock()
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
DOC_ID = re.compile(r"[a-f0-9]{32}_\d{3}\Z")
MAX_REVISIONS = 100
MAX_REVIEW_BYTES = 64 * 1024 * 1024
MAX_BASE_BYTES = 16 * 1024 * 1024
DISK_RESERVE = 1536 * 1024 * 1024


class ReviewConflict(ValueError):
    pass


class ReviewLimitError(OSError):
    pass


def read_json(path: Path, limit=MAX_BASE_BYTES):
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ReviewLimitError("Review metadata exceeds its memory budget")
    return json.loads(content)


def check_metadata_size(payload):
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(
        payload
    ):
        size += len(chunk.encode("utf-8"))
        if size > MAX_BASE_BYTES:
            raise ReviewLimitError("Review metadata exceeds its memory budget")


@contextlib.contextmanager
def document_lock(out_dir: Path):
    """Serialize writers across service processes as well as HTTP threads."""
    with STORE_LOCK, (out_dir / ".review.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReviewConflict("Another review save is in progress") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@contextlib.contextmanager
def revision_directory(out_dir: Path, revision: int):
    """Remove only this attempt's unpublished exports on a failed save."""
    root = (out_dir / "reviews").resolve()
    version = root / f"v{revision:06d}-{uuid.uuid4().hex}"
    version.mkdir(parents=True, exist_ok=False)
    try:
        yield version
    except BaseException:
        if version.resolve().parent != root:
            raise OSError("Invalid review staging directory") from None
        shutil.rmtree(version)
        raise


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".pending-",
            delete=False,
        ) as stream:
            temp = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()


def save_job(data_dir: Path, job_id: str, job: dict):
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job")
    with STORE_LOCK:
        atomic_json(data_dir / "jobs" / (job_id + ".json"), copy.deepcopy(job))


def load_job(data_dir: Path, job_id: str, owner: str | None) -> dict | None:
    if not owner or not JOB_ID.fullmatch(job_id):
        return None
    try:
        job = read_json(data_dir / "jobs" / (job_id + ".json"))
    except (OSError, ValueError):
        return None
    if not hmac.compare_digest(str(job.get("owner_id", "")), owner):
        return None
    # Completed reviews survive restarts, but an interrupted task is not resumed.
    if job.get("state") == "running":
        job["state"] = "error"
        job.setdefault("log", []).append("پردازش متوقف شده است؛ فایل اصلی محفوظ است.")
    return job


def _bounds(words):
    boxes = [w["bbox"] for w in words]
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def create_document(out_dir: Path, document: dict) -> dict:
    """Capture a review base once, before any user changes."""
    from .precision import source_box

    out_dir.mkdir(parents=True, exist_ok=True)

    check_metadata_size(document)
    base = copy.deepcopy(document)
    summaries = []
    for page in base["pages"]:
        if page.get("error"):
            summaries.append({"page": page["page"], "units": [], "error": True})
            continue
        units = copy.deepcopy(page.get("review_units") or [])
        if not units:
            groups = {}
            for word in page.get("words", []):
                key = word.get("line_id") or "page"
                groups.setdefault(key, []).append(word)
            for words in sorted(
                groups.values(), key=lambda g: (_bounds(g)[1], -_bounds(g)[2])
            ):
                text = "\n".join(E.words_to_lines(words))
                units.append(
                    {
                        "kind": "line",
                        "text": text,
                        "bbox": _bounds(words),
                        "source_bbox": source_box(_bounds(words), page),
                        "needs_review": bool(page.get("precision_warnings"))
                        or any(w.get("needs_review") for w in words),
                        'candidates':[candidate for w in words for candidate in w.get('candidates',[])][:8],
                        'uncertainty_reasons':sorted({reason for w in words for reason in w.get('uncertainty_reasons',[])}),
                    }
                )
        for table in page.get("tables", []):
            path = (
                out_dir
                / "tables"
                / f"page_{page['page']:03d}_table_{table['table_id']:03d}.json"
            )
            if "cells" not in table and path.is_file():
                table.update(read_json(path))
            for cell in table.get("cells", []):
                units.append(
                    {
                        "kind": "cell",
                        "text": cell["normalized_text"],
                        "bbox": cell["bbox"],
                        "table_id": table["table_id"],
                        "source_bbox": cell.get("source_bbox")
                        or source_box(cell["bbox"], page),
                        "row": cell["row"],
                        "column": cell["column"],
                        "needs_review": bool(page.get("precision_warnings"))
                        or bool(cell.get("needs_review")),
                        "reason": cell.get("precision_warning")
                        or cell.get("reason", ""),
                        "numeric_checks": cell.get("numeric_checks", []),
                        "candidates": cell.get("candidates", []),
                        "uncertainty_reasons": cell.get("uncertainty_reasons", []),
                    }
                )
        for index, unit in enumerate(units, 1):
            unit["id"] = f"p{page['page']:04d}-u{index:05d}"
            unit["text"] = E.display_text(unit["text"])
            unit["original_text"] = unit["text"]
            unit["confirmed"] = False
        page["review_units"] = units
        summaries.append(
            {
                "page": page["page"],
                "width": page["width"],
                "height": page["height"],
                "units": units,
            }
        )
    with document_lock(out_dir):
        base_path = out_dir / "review-base.json"
        if not base_path.exists():
            check_metadata_size(base)
            atomic_json(base_path, base)
    return {
        "pages": summaries,
        "revision": 0,
        "unresolved": sum(
            u.get("needs_review", False) for p in summaries for u in p["units"]
        ),
    }


def read_document(out_dir: Path) -> dict:
    base = read_json(out_dir / "review-base.json")
    state_path = out_dir / "review-state.json"
    state = (
        read_json(state_path)
        if state_path.is_file()
        else {"revision": 0, "changes": {}, "files": []}
    )
    pages = []
    for page in base["pages"]:
        units = copy.deepcopy(page.get("review_units", []))
        for unit in units:
            update = state["changes"].get(unit["id"])
            if update:
                unit.update(update)
        pages.append(
            {
                "page": page["page"],
                "width": page.get("width", 1),
                "height": page.get("height", 1),
                "error": bool(page.get("error")),
                "warnings": page.get("precision_warnings", []),
                "engines": page.get("engines", []),
                "units": units,
            }
        )
    return {
        "revision": state["revision"],
        "pages": pages,
        "files": state["files"],
        'exports': state.get('exports', base.get('exports', {})),
        "unresolved": sum(
            bool(u.get("needs_review")) and not u.get("confirmed", False)
            for p in pages
            for u in p["units"]
        ),
    }


def save_document(out_dir: Path, payload: dict) -> dict:
    """Atomically publish a coherent revision only after all exports exist."""
    from .pipeline import build_txt

    if not isinstance(payload, dict) or set(payload) != {"revision", "edits"}:
        raise ValueError("Invalid review request")
    if type(payload["revision"]) is not int or not isinstance(payload["edits"], list):
        raise ValueError("Invalid review revision")
    if not 1 <= len(payload["edits"]) <= 2000:
        raise ValueError("Invalid review size")
    with document_lock(out_dir):
        current = read_document(out_dir)
        if payload["revision"] != current["revision"]:
            raise ReviewConflict("Review revision has changed")
        if current["revision"] >= MAX_REVISIONS:
            raise ReviewLimitError("Review revision budget exceeded")
        known = {u["id"]: u for p in current["pages"] for u in p["units"]}
        updates = {}
        for edit in payload["edits"]:
            if not isinstance(edit, dict) or set(edit) != {"id", "text", "confirmed"}:
                raise ValueError("Invalid edit")
            key, text = edit["id"], edit["text"]
            if not isinstance(key, str) or key not in known or key in updates:
                raise ValueError("Unknown or duplicate review unit")
            if (
                not isinstance(text, str)
                or len(text) > 8192
                or any(ord(c) < 32 and c not in "\t\n\r" for c in text)
                or any(0xD800 <= ord(c) <= 0xDFFF for c in text)
                or type(edit["confirmed"]) is not bool
            ):
                raise ValueError("Invalid review text")
            updates[key] = {
                "text": E.display_text(text),
                "confirmed": edit["confirmed"],
                "edited": text != known[key]["original_text"],
            }
        if sum(len(u["text"]) for u in updates.values()) > 500_000:
            raise ValueError("Review text exceeds limit")
        state_path = out_dir / "review-state.json"
        previous = read_json(state_path) if state_path.exists() else {"changes": {}}
        changes = previous["changes"] | updates
        base = read_json(out_dir / "review-base.json")
        estimated = 4 * (
            len(json.dumps(base, ensure_ascii=False).encode("utf-8"))
            + len(json.dumps(changes, ensure_ascii=False).encode("utf-8"))
        )
        estimated += sum(len(p.get("tables", [])) for p in base["pages"]) * 128 * 1024
        existing_bytes = sum(
            p.stat().st_size for p in (out_dir / "reviews").rglob("*") if p.is_file()
        )
        if existing_bytes + estimated > MAX_REVIEW_BYTES:
            raise ReviewLimitError("Review export storage budget exceeded")
        if shutil.disk_usage(out_dir).free < DISK_RESERVE + estimated:
            raise ReviewLimitError("Insufficient space to safely build review exports")
        revision = current["revision"] + 1
        # The context below also removes partial ZIP/XLSX files if export fails.
        with revision_directory(out_dir, revision) as version:
            files = _build_exports(base, changes, revision, out_dir, version, build_txt)
            actual_bytes = sum(
                p.stat().st_size for p in version.rglob("*") if p.is_file()
            )
            if existing_bytes + actual_bytes > MAX_REVIEW_BYTES:
                raise ReviewLimitError("Review export storage budget exceeded")
            if shutil.disk_usage(out_dir).free < DISK_RESERVE:
                raise ReviewLimitError("Safety reserve reached during review export")
            atomic_json(
                state_path, {"revision": revision, "changes": changes, "files": files,
                             'exports': base.get('exports', {})}
            )
        return read_document(out_dir)


def _build_exports(base, changes, revision, out_dir, version, build_txt):
    for page in base["pages"]:
        units = page.get("review_units", [])
        for unit in units:
            unit.update(changes.get(unit["id"], {}))
        if page.get("error"):
            continue
        lines = [u["text"] for u in units if u["kind"] == "line"]
        if lines:
            page["lines"] = lines
            page["text"] = page["normalized_text"] = "\n".join(lines)
            page["normalized_en_digits"] = E.en_digits_only(page["text"])
            page["identifiers"] = [
                E.display_text(value)
                for value in E.find_identifiers(page["normalized_en_digits"])
            ]
        for table in page.get("tables", []):
            cell_units = {
                (u["row"], u["column"]): u
                for u in units
                if u["kind"] == "cell" and u["table_id"] == table["table_id"]
            }
            for cell in table.get("cells", []):
                unit = cell_units.get((cell["row"], cell["column"]))
                if unit:
                    cell["normalized_text"] = unit["text"]
                    cell["normalized_en_digits"] = E.en_digits_only(unit["text"])
                    cell["review_confirmed"] = unit.get("confirmed", False)
            if table.get("cells"):
                table["markdown"] = T.markdown_table(table)
                T.write_table_outputs(table, version / "tables", page_no=page["page"],xlsx_required=False)
        page['exports'] = {'xlsx': {'status': 'unavailable' if any(
            t.get('export_status', {}).get('xlsx', {}).get('status') == 'unavailable'
            for t in page.get('tables', [])) else 'available' if page.get('tables') else 'not_applicable'}}
    base['exports'] = {'xlsx': {'status': 'unavailable' if any(
        p.get('exports', {}).get('xlsx', {}).get('status') == 'unavailable' for p in base['pages'])
        else 'available' if any(p.get('tables') for p in base['pages']) else 'not_applicable'}}
    if 'schema_v2' in base:
        # Immutable recognition snapshot is retained, never silently presented
        # as the current human-edited structured record.
        base['schema_v2'].update(status='stale_after_human_review', fields_current=False,
                                review_revision=revision, commercial_registration_allowed=False)
    base["review"] = {"revision": revision, "raw_ocr_preserved": True}
    stem = out_dir.name
    atomic_json(version / (stem + ".json"), base)
    (version / (stem + ".txt")).write_text(build_txt(base["pages"]), encoding="utf-8")
    members = sorted(p for p in version.rglob("*") if p.is_file())
    with zipfile.ZipFile(
        version / "reviewed.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for member in members:
            archive.write(member, member.relative_to(version).as_posix())
    return [
        p.relative_to(out_dir.parent.resolve()).as_posix()
        for p in sorted(version.rglob("*"))
        if p.is_file()
    ]
