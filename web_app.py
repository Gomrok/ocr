#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""سامانهٔ پردازش تصویری گمرک با وضوح قابل انتخاب.

آپلود گروهی PDF، پیشرفت زندهٔ صفحه‌به‌صفحه (progress_cb)،
خروجی TXT + JSON + جدول‌ها (CSV/HTML/XLSX) + دیباگ + ZIP.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import pymupdf
from flask import Flask, g, jsonify, render_template, request, send_file, session

from ocr_pipeline.engines import TesseractEngine, display_text
from ocr_pipeline.job_store import ACTIVE_STATES, JobStore
from ocr_pipeline.job_worker import run_isolated
from ocr_pipeline.review import ReviewConflict, ReviewStore
from ocr_pipeline.quality import page_quality, unknown_quality
from ocr_pipeline.release_identity import current_runtime, digest, resource_limits

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # سقف ۵۰۰ مگابایت
app.config.update(
    SECRET_KEY=os.environ.get("OCR_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_NAME="gomrok_ocr_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("OCR_SECURE_COOKIES") == "1",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_REFRESH_EACH_REQUEST=False,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
    MAX_FORM_PARTS=110,
    TRUSTED_HOSTS=[host.strip() for host in os.environ.get(
        "OCR_ALLOWED_HOSTS", "127.0.0.1,localhost,ocr.gomrok.org").split(",") if host.strip()],
)

_configured_data = os.environ.get("OCR_DATA_DIR")
if not _configured_data or not Path(_configured_data).is_absolute():
    raise RuntimeError("An explicit absolute OCR_DATA_DIR is required before importing the web app")
DATA_DIR = Path(_configured_data).resolve()
_release_dir = Path(__file__).resolve().parent
if DATA_DIR == Path(DATA_DIR.anchor) or DATA_DIR == _release_dir or _release_dir in DATA_DIR.parents:
    raise RuntimeError("OCR_DATA_DIR must be private storage outside the release directory")
UPLOAD_DIR = DATA_DIR / "uploads"
OUT_DIR = DATA_DIR / "output"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
QUEUE_SLOTS = threading.BoundedSemaphore(2)
PDF_IO_LOCK = threading.Lock()
OCR_RESOURCES = threading.Condition()
OCR_WAITERS = deque()
LIMITS = resource_limits(os.environ)
OCR_WORKERS = LIMITS["workers"]
OCR_RAM_MIB = LIMITS["ram_mib"]
OCR_USED = 0
OCR_ACTIVE = 0
RATE_LOCK = threading.Lock()
UPLOAD_TIMES: dict[str, deque] = {}
PENDING_OWNERS: set[str] = set()
LOCAL_ENGINE = TesseractEngine("fas")
MAX_BATCH_PAGES = 300
DPI_LEVELS = (100, 250, 300, 400, 500, 1000, 1500, 2000)
DEFAULT_DPI = int(os.environ.get("OCR_DEFAULT_DPI", 2000))
if DEFAULT_DPI not in DPI_LEVELS:
    raise RuntimeError("Unknown OCR_DEFAULT_DPI")
PREVIEW_SLOTS = threading.BoundedSemaphore(2)
REVIEW_SLOTS = threading.BoundedSemaphore(2)
QUALITY_SLOTS = threading.BoundedSemaphore(2)
MAX_PREVIEW_EDGE = 1400
MAX_PREVIEW_TEXT_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_TEXT_CHARS = 30_000
PIPELINE_MODE = os.environ.get("OCR_PIPELINE_MODE", "legacy")
if PIPELINE_MODE not in ("legacy", "compare", "candidate"):
    raise RuntimeError("Unknown OCR_PIPELINE_MODE")


def _review_store():
    return ReviewStore(DATA_DIR / "reviews")


def _store():
    return JobStore(DATA_DIR)


def _owned_job(job_id, owner):
    if not owner or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    current = JOBS.get(job_id)
    if current:
        return current if current["owner_id"] == owner else None
    return _store().get(job_id, owner)


@contextmanager
def _ocr_reservation(dpi):
    global OCR_USED, OCR_ACTIVE
    # High-resolution jobs may use the full RAM allowance; two small scans
    # may run together. FIFO ordering prevents large scans from starving.
    units = OCR_WORKERS if dpi >= 1000 else 1
    ticket = object()
    with OCR_RESOURCES:
        OCR_WAITERS.append(ticket)
        OCR_RESOURCES.wait_for(lambda: OCR_WAITERS[0] is ticket and OCR_USED + units <= OCR_WORKERS)
        OCR_WAITERS.popleft()
        OCR_USED += units
        OCR_ACTIVE += 1
        OCR_RESOURCES.notify_all()
    try:
        ram_mib = (OCR_RAM_MIB - 192) * units // OCR_WORKERS
        yield {
            "ram_mib": ram_mib,
            "render_budget_mp": min(400.0, (ram_mib - 320) * 2**20 / 10_000_000),
            "native_workers": LIMITS["native_workers"],
            "omp_threads": LIMITS["omp_threads"],
        }
    finally:
        with OCR_RESOURCES:
            OCR_USED -= units
            OCR_ACTIVE -= 1
            OCR_RESOURCES.notify_all()


def _bounded_review(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        if not REVIEW_SLOTS.acquire(blocking=False):
            return jsonify(error="بازبینی در حال ذخیره یا دریافت است. کمی بعد تلاش کنید."), 503
        try:
            return function(*args, **kwargs)
        except OSError:
            app.logger.exception("Review storage operation failed")
            return jsonify(error="دسترسی به فضای بازبینی انجام نشد. دوباره تلاش کنید."), 503
        finally:
            REVIEW_SLOTS.release()
    return guarded


@app.before_request
def _request_context():
    g.csp_nonce = secrets.token_urlsafe(24)
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or len(supplied) > 128 or not hmac.compare_digest(expected.encode(), supplied.encode()):
            return jsonify(error="نشست معتبر نیست. صفحه را تازه‌سازی و دوباره تلاش کنید."), 403


@app.after_request
def _security_headers(response):
    nonce = g.get("csp_nonce", "")
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
        "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.path not in ("/healthz",) and not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.errorhandler(413)
def _upload_too_large(_error):
    return jsonify(error="حجم فایل‌های ارسالی از حد مجاز بیشتر است."), 413


def _upload_rate_allowed() -> bool:
    now = time.monotonic()
    with RATE_LOCK:
        for address in list(UPLOAD_TIMES):
            times = UPLOAD_TIMES[address]
            while times and times[0] < now - 3600:
                times.popleft()
            if not times:
                del UPLOAD_TIMES[address]
        address = request.remote_addr or "unknown"
        times = UPLOAD_TIMES.setdefault(address, deque())
        if len(times) >= 10 or len(UPLOAD_TIMES) > 2048:
            return False
        times.append(now)
        return True

def _make_cb(job_id: str, fname: str, document_index: int | None = None):
    def cb(event: dict):
        with LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            summaries = job.get("file_statuses", [])
            summary = (summaries[document_index - 1]
                       if document_index and document_index <= len(summaries) else None)
            t = event["type"]
            if t == "start":
                job["log"].append(
                    f"سند {fname}: {event['pages']} صفحه با وضوح {event['dpi']} DPI")
            elif t == "page_ok":
                job["pages_done"] += 1
                if summary:
                    summary["pages_done"] += 1
                if event.get("needs_review"):
                    job["review_pages"] += 1
                    if summary:
                        summary["review_pages"] = summary.get("review_pages", 0) + 1
                src = event.get("source", "")
                src_fa = "گوگل" if "google" in src else ("محلی+eng" if "eng" in src else "محلی")
                tbl = f"، {event['tables']} جدول" if event.get("tables") else ""
                job["log"].append(
                    f"[{fname}] صفحه {event['page']} — {event['words']} کلمه{tbl} ({src_fa})")
                if event.get("needs_review"):
                    job["log"].append(f"[{fname}] بخشی از متن صفحهٔ {event['page']} نیاز به بازبینی دارد.")
            elif t == "page_error":
                job["pages_done"] += 1
                job["failed_pages"] += 1
                if summary:
                    summary["pages_done"] += 1
                    summary["failed_pages"] += 1
                job["log"].append(
                    f"[{fname}] استخراج متن صفحهٔ {event['page']} با خطا مواجه شد.")
            elif t == "done":
                job["log"].append(
                    f"[{fname}] {event['pages_ok']}/{event['pages']} صفحه، "
                    f"{event['words']} کلمه در {event['seconds']} ثانیه")
            job["log"] = job["log"][-120:]
            _store().save(job_id, job)
    return cb


def _finish_document(job_id: str, document_index: int, outcome: str,
                     outputs: list[str] | None = None) -> None:
    with LOCK:
        job = JOBS[job_id]
        summaries = job.get("file_statuses", [])
        if document_index > len(summaries):
            return
        summary = summaries[document_index - 1]
        missing = max(0, summary["page_count"] - summary["pages_done"])
        if missing:
            summary["pages_done"] += missing
            summary["failed_pages"] += missing
            job["pages_done"] += missing
            job["failed_pages"] += missing
        summary["state"] = "partial" if outcome == "done" and summary["failed_pages"] else outcome
        if outputs:
            summary["outputs"] = outputs
        _store().save(job_id, job)


def _collect_outputs(stem_dir: Path, stem: str, *, extraction_revision=None, pages=None) -> list[str]:
    """نسبی: TXT + JSON + جدول‌ها + دیباگ این سند."""
    names = [f"{stem}/{stem}.txt", f"{stem}/{stem}.json"]
    revision = extraction_revision
    if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{24}", revision):
        # Table exports are from this exact extraction; earlier revisions remain private.
        directory = stem_dir / "extractions" / revision / "tables"
        if directory.is_dir():
            names.extend(f"{stem}/extractions/{revision}/tables/{path.name}" for path in sorted(directory.iterdir()) if path.is_file())
        return names
    if pages is not None and all("artifacts" in page or page.get("error") for page in pages):
        names += [f"{stem}/{name}" for page in pages for name in page.get("artifacts", [])]
        return names
    for sub in ("tables", "debug"):
        d = stem_dir / sub
        if d.is_dir():
            for f in sorted(d.glob("*")):
                if f.is_file():
                    names.append(f"{stem}/{sub}/{f.name}")
    return names


def _zip_outputs(names: list[str], zip_path: Path) -> None:
    staging = zip_path.with_suffix(".working")
    try:
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                archive.write(OUT_DIR / name, name)
        os.replace(staging, zip_path)
    finally:
        staging.unlink(missing_ok=True)


def _reset_progress(job, summary):
    for key in ("pages_done", "failed_pages", "review_pages"):
        job[key] -= summary.get(key, 0)
        summary[key] = 0


def _run_document(source, output, job, summary, budget, cb):
    scratch = Path(os.environ.get("OCR_TEMP_DIR", DATA_DIR / "temp"))
    scratch.mkdir(parents=True, exist_ok=True)
    request = dict(budget, dpi=job["dpi"], mode=PIPELINE_MODE, pages=summary["page_count"],
                   handwriting=job.get("handwriting_declared", False),
                   candidate_settings=job.get("candidate_settings", {}))
    return run_isolated(source, output, scratch, request, cb)


def _process_job(job_id: str, pdf_paths: list[Path], dpi: int):
    try:
        for idx, pdf_path in enumerate(pdf_paths, 1):
            with LOCK:
                job = JOBS[job_id]
                summary = job["file_statuses"][idx - 1]
                if summary["state"] in ("done", "partial", "error"):
                    continue
                name = job["documents"][pdf_path.name]
                out_dir = OUT_DIR / pdf_path.stem
                cb = _make_cb(job_id, name, idx)
            try:
                with _ocr_reservation(dpi) as budget:
                    with LOCK:
                        job["state"] = "running"
                        job["file_index"] = idx
                        summary["state"] = "running"
                        if "started_processing_at" not in job:
                            job["started_processing_at"] = time.time()
                            job["queue_wait_seconds"] = round(job["started_processing_at"] - job["accepted_at"], 3)
                        summary["ocr_started_at"] = time.time()
                        _store().save(job_id, job)
                    result = _run_document(pdf_path, out_dir, job, summary, budget, cb)
                    with LOCK:
                        summary["ocr_finished_at"] = time.time()
                        if "worker_usage" in result:
                            summary["worker_usage"] = result["worker_usage"]
                candidate_revision = result.get("extraction_revision") or result.get("comparison", {}).get("extraction_revision")
                if candidate_revision:
                    snapshot = out_dir / "extractions" / candidate_revision
                    payload = (result if result.get("extraction_revision") else
                               json.loads((snapshot / "extraction.json").read_text("utf-8")))
                    _review_store().register(job["owner_id"], payload, snapshot, display_name=name)
                if not any(page.get("has_text", bool(page.get("text", "").strip()) or bool(page.get("tables")))
                           for page in result.get("pages", [])):
                    raise RuntimeError("No text extracted from document")
                names = _collect_outputs(out_dir, pdf_path.stem, extraction_revision=result.get("extraction_revision"),
                                         pages=result.get("pages"))
                if any(not (OUT_DIR / path).is_file() for path in names):
                    raise RuntimeError("Incomplete document exports")
                _finish_document(job_id, idx, "done", names)
                with LOCK:
                    job["files"].extend(names)
                    _store().save(job_id, job)
            except Exception as error:
                app.logger.exception("OCR document failed for job %s", job_id)
                with LOCK:
                    summary["error"] = (str(error) if str(error) in {"ram_budget_exhausted", "worker_timeout"}
                                        else "document_ocr_failed")
                    summary["ocr_finished_at"] = time.time()
                    job["log"].append(f"پردازش سند {name} انجام نشد.")
                _finish_document(job_id, idx, "error")

        with LOCK:
            # A restart between committing an individual file and committing the
            # batch still discovers the file outputs in deterministic order.
            names = [path for summary in job["file_statuses"] for path in summary["outputs"]]
        if names:
            zip_path = OUT_DIR / f"{job_id}_ocr_results.zip"
            _zip_outputs(names, zip_path)
        with LOCK:
            job["files"] = names
            if names:
                job["zip"] = zip_path.name
            complete = all(summary["state"] == "done" for summary in job["file_statuses"])
            job["state"] = "done" if complete else "partial" if names else "error"
            job["finished_at"] = time.time()
            _store().save(job_id, job)
    except Exception:
        app.logger.exception("OCR job failed: %s", job_id)
        with LOCK:
            for item in JOBS[job_id]["file_statuses"]:
                if item["state"] in ("waiting", "running"):
                    item["state"] = "error"
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["finished_at"] = time.time()
            _store().save(job_id, JOBS[job_id])


def _run_job(job_id: str, pdf_paths: list[Path], dpi: int):
    try:
        _process_job(job_id, pdf_paths, dpi)
    finally:
        with LOCK:
            JOBS.pop(job_id, None)
            QUEUE_SLOTS.release()


def recover_jobs():
    """Resume only the unfinished documents of persisted jobs on cold start."""
    _store().expire()
    for job_id, job in _store().pending():
        with LOCK:
            if job_id in JOBS:
                continue
            if not QUEUE_SLOTS.acquire(blocking=False):
                raise RuntimeError("Persisted jobs exceed the configured admission capacity")
            for summary in job["file_statuses"]:
                if summary["state"] == "running":
                    _reset_progress(job, summary)
                    summary["state"] = "waiting"
            job["state"] = "queued"
            job["log"].append("پردازش از آخرین صفحهٔ ذخیره‌شده ادامه می‌یابد.")
            JOBS[job_id] = job
            _store().save(job_id, job)
        paths = [UPLOAD_DIR / name for name in job["documents"]]
        threading.Thread(target=_run_job, args=(job_id, paths, job["dpi"]),
                         name="ocr-job-" + job_id, daemon=False).start()



def _candidate_options(settings):
    if PIPELINE_MODE == "legacy":
        return None
    from ocr_pipeline.precision.profiles import Options
    return Options(profile=settings.get("profile", "persian"), dpi=settings.get("candidate_dpi", 300),
                   handwriting=settings.get("handwriting", False))


@app.get("/")
def index():
    session.setdefault("owner_id", uuid.uuid4().hex)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    session.permanent = True
    return render_template("dashboard.html", csrf_token=session["csrf_token"],
                           dpi_levels=DPI_LEVELS, default_dpi=DEFAULT_DPI,
                           default_dpi_fa=display_text(str(DEFAULT_DPI)),
                           dpi_level_fa=display_text(str(DPI_LEVELS.index(DEFAULT_DPI) + 1)),
                           dpi_count_fa=display_text(str(len(DPI_LEVELS))),
                           dpi_options_fa=display_text("، ".join(map(str, DPI_LEVELS))),
                           csp_nonce=g.csp_nonce, pipeline_mode=PIPELINE_MODE)


@app.get("/healthz")
def health():
    ready = LOCAL_ENGINE.available()
    with LOCK:
        active_jobs = sum(job["state"] in ACTIVE_STATES for job in JOBS.values())
        queued = sum(job["state"] == "queued" for job in JOBS.values())
    fingerprint = None
    if ready and LOCAL_ENGINE.native_ok():
        try:
            fingerprint = digest(current_runtime(LOCAL_ENGINE))
        except (OSError, ValueError, IndexError):
            app.logger.warning("Could not resolve the native OCR fingerprint")
    return jsonify(status="ok" if ready else "unavailable", engine="tesseract" if ready else None,
                   release=os.environ.get("OCR_RELEASE", "development"),
                   active_jobs=active_jobs, pipeline_mode=PIPELINE_MODE,
                   queued_jobs=queued, active_ocr=OCR_ACTIVE, runtime_fingerprint=fingerprint), 200 if ready else 503


@app.post("/upload")
def upload():
    owner = session.get("owner_id")
    if not owner:
        return jsonify(error="لطفاً صفحه را تازه‌سازی کنید."), 403
    if not _upload_rate_allowed():
        return jsonify(error="تعداد درخواست‌ها بیش از حد مجاز است. لطفاً بعداً تلاش کنید."), 429
    with LOCK:
        if owner in PENDING_OWNERS or any(
                job["owner_id"] == owner and job["state"] in ACTIVE_STATES for job in JOBS.values()):
            return jsonify(error="درخواست قبلی شما هنوز در حال پردازش است."), 409
        PENDING_OWNERS.add(owner)
    if not QUEUE_SLOTS.acquire(blocking=False):
        with LOCK:
            PENDING_OWNERS.discard(owner)
        return jsonify(error="صف پردازش تکمیل است. لطفاً چند دقیقه بعد تلاش کنید."), 503
    started = False
    job_id = None
    saved: list[Path] = []
    try:
        if shutil.disk_usage(DATA_DIR).free < 1536 * 1024 * 1024:
            return jsonify(error="فضای پردازش موقتاً کافی نیست. لطفاً با پشتیبانی تماس بگیرید."), 503
        fs = request.files.getlist("pdfs")
        if not fs or len(fs) > 100:
            return jsonify(error="در هر نوبت از یک تا ۱۰۰ فایل PDF انتخاب نمایید."), 400
        image_signatures = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
                            ".tif": (b"II*\x00", b"MM\x00*"), ".tiff": (b"II*\x00", b"MM\x00*"), ".bmp": (b"BM",)}
        valid_headers = []
        for file in fs:
            suffix = Path(file.filename or "").suffix.lower()
            signature = file.stream.read(8)
            valid_pdf = suffix == ".pdf" and signature.startswith(b"%PDF-")
            valid_image = PIPELINE_MODE == "candidate" and suffix in image_signatures and signature.startswith(image_signatures[suffix])
            if suffix != ".pdf" and not (PIPELINE_MODE == "candidate" and suffix in image_signatures):
                return jsonify(error="فایل PDF یا تصویر معتبر انتخاب کنید." if PIPELINE_MODE == "candidate" else "فقط فایل PDF معتبر قابل پردازش است."), 400
            valid_headers.append(valid_pdf or valid_image)
            file.stream.seek(0)
        handwriting_values = request.form.getlist("handwriting")
        if len(handwriting_values) > 1 or (handwriting_values and handwriting_values[0] not in ("0", "1")):
            return jsonify(error="وضعیت دست‌نویس معتبر نیست."), 400
        handwriting = bool(handwriting_values and handwriting_values[0] == "1")
        candidate_settings = {}
        if PIPELINE_MODE != "legacy":
            allowed = {"profile": ("persian", "mixed", "english"), "candidate_dpi": ("300", "400"), "handwriting": ("0", "1")}
            for field, values_allowed in allowed.items():
                submitted = request.form.getlist(field)
                if len(submitted) > 1 or (submitted and submitted[0] not in values_allowed):
                    return jsonify(error="تنظیمات مسیر آزمایشی معتبر نیست."), 400
            candidate_settings = {"profile": request.form.get("profile", "persian"),
                                  "candidate_dpi": int(request.form.get("candidate_dpi", "300")),
                                  "handwriting": request.form.get("handwriting", "0") == "1"}
        values = request.form.getlist("dpi")
        if len(values) > 1 or (values and values[0] not in {str(value) for value in DPI_LEVELS}):
            return jsonify(error="وضوح پردازش باید یکی از مقدارهای ارائه‌شده در تنظیمات باشد."), 400
        dpi = int(values[0]) if values else DEFAULT_DPI
        job_id = uuid.uuid4().hex
        documents = {}
        file_statuses = []
        pages = 0
        for index, (file, header_is_valid) in enumerate(zip(fs, valid_headers), 1):
            suffix = Path(file.filename).suffix.lower()
            path = UPLOAD_DIR / f"{job_id}_{index:03d}{suffix}"
            saved.append(path)
            file.save(path)
            page_count = 0
            try:
                if not header_is_valid:
                    raise ValueError("Invalid source signature")
                if suffix != ".pdf":
                    from ocr_pipeline.precision.imaging import SourcePages
                    with SourcePages(path, _candidate_options(candidate_settings)) as document:
                        page_count = document.count
                        for number in range(page_count):
                            document.document.seek(number)
                            document._check_dimensions(*document.document.size)
                else:
                    with PDF_IO_LOCK, pymupdf.open(path) as document:
                        if document.needs_pass or document.page_count == 0:
                            raise ValueError("Encrypted or empty PDF")
                        page_count = document.page_count
            except (OSError, RuntimeError, ValueError):
                page_count = 0
            pages += page_count
            if pages > MAX_BATCH_PAGES:
                return jsonify(error="مجموع صفحات هر نوبت نباید از ۳۰۰ صفحه بیشتر باشد."), 400
            name = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
            safe_name = "".join(char for char in name if char.isprintable())[:240]
            documents[path.name] = safe_name
            file_statuses.append({
                "index": index, "id": path.stem, "name": safe_name,
                "size": path.stat().st_size, "page_count": page_count,
                "pages_done": 0, "failed_pages": 0, "review_pages": 0,
                "state": "waiting" if page_count else "error", "outputs": [],
                **({} if page_count else {"error": "invalid_document"}),
            })
        if not pages:
            return jsonify(error="هیچ سند معتبر و قابل پردازشی یافت نشد."), 400
        with LOCK:
            PENDING_OWNERS.discard(owner)
            JOBS[job_id] = {
                "owner_id": owner, "documents": documents, "file_statuses": file_statuses, "dpi": dpi,
                "state": "queued", "log": [], "pages_total": pages, "pages_done": 0,
                "failed_pages": 0, "review_pages": 0, "files": [], "zip": None, "file_index": 0,
                "file_count": len(saved), "started": datetime.now().isoformat(timespec="seconds"),
                "candidate_settings": candidate_settings,
                "handwriting_declared": handwriting,
                "accepted_at": time.time(),
            }
            _store().expire()
            _store().save(job_id, JOBS[job_id])
        threading.Thread(target=_run_job, args=(job_id, saved, dpi),
                         name="ocr-job-" + job_id, daemon=False).start()
        started = True
        return jsonify(job_id=job_id, count=len(saved), dpi=dpi)
    finally:
        if not started:
            QUEUE_SLOTS.release()
            with LOCK:
                PENDING_OWNERS.discard(owner)
                if job_id and JOBS.pop(job_id, None):
                    _store().forget(job_id)
            for path in saved:
                path.unlink(missing_ok=True)


@app.get("/status/<job_id>")
def status(job_id: str):
    with LOCK:
        job = _owned_job(job_id, session.get("owner_id"))
        if not job:
            return jsonify({"state": "unknown"}), 404
        elapsed = max(0, int(job.get("finished_at", time.time()) - job.get("accepted_at", time.time())))
        payload = {key: value for key, value in job.items()
                   if key not in ("owner_id", "documents")}
        payload["log"] = job["log"][-60:]
        payload["file_statuses"] = [dict(item) for item in job.get("file_statuses", [])]
        return jsonify(payload | {"elapsed_seconds": elapsed,
                                  "phase": "failed" if job["state"] == "error" else job["state"]})


@app.get("/jobs")
def recent_jobs():
    return jsonify(jobs=[
        {"job_id": job_id, "state": job["state"], "started": job["started"],
         "file_count": job["file_count"],
         "name": job["file_statuses"][0]["name"]}
        for job_id, job in _store().latest(session.get("owner_id"))
    ])


def _owned_document(job_id: str, document_index: int, page_number: int):
    if not (1 <= document_index <= 100 and 1 <= page_number <= MAX_BATCH_PAGES):
        return None
    with LOCK:
        job = _owned_job(job_id, session.get("owner_id"))
        if not job:
            return None
        summaries = job.get("file_statuses", [])
        if document_index > len(summaries):
            return None
        summary = dict(summaries[document_index - 1])
        source_name = next((name for name in job["documents"] if Path(name).stem == summary["id"]), "")
        if source_name not in job["documents"] or page_number > summary["page_count"]:
            return None
    return summary, source_name


@app.get("/preview/<job_id>/<int:document_index>/<int:page_number>")
def preview_page(job_id: str, document_index: int, page_number: int):
    document_info = _owned_document(job_id, document_index, page_number)
    if not document_info:
        return "یافت نشد", 404
    _, source_name = document_info
    source = (UPLOAD_DIR / source_name).resolve()
    if source.parent != UPLOAD_DIR.resolve() or not source.is_file():
        return "یافت نشد", 404
    if not PREVIEW_SLOTS.acquire(blocking=False):
        return jsonify(error="پیش‌نمایش در حال آماده‌سازی است. دوباره تلاش کنید."), 503
    try:
        if source.suffix.lower() != ".pdf":
            from ocr_pipeline.precision.imaging import SourcePages
            from ocr_pipeline.precision.profiles import Options
            import cv2
            with SourcePages(source, Options()) as pages:
                raster, _ = pages.read(page_number-1)
                scale = min(1., MAX_PREVIEW_EDGE / max(raster.shape))
                if scale < 1:
                    raster = cv2.resize(raster, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                success, encoded = cv2.imencode(".png", raster)
                if not success:
                    raise OSError("Preview encoding failed")
            return send_file(io.BytesIO(encoded.tobytes()), mimetype="image/png")
        with PDF_IO_LOCK, pymupdf.open(source) as document:
            page = document[page_number - 1]
            bounds = page.rect
            if bounds.width <= 0 or bounds.height <= 0:
                return jsonify(error="نمایش این صفحه انجام نشد."), 503
            scale = min(1.6, MAX_PREVIEW_EDGE / max(bounds.width, bounds.height))
            if scale < 0.0001:
                return jsonify(error="نمایش این صفحه انجام نشد."), 503
            raster = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                     colorspace=pymupdf.csRGB, alpha=False)
            data = raster.tobytes("png")
        return send_file(io.BytesIO(data), mimetype="image/png",
                         download_name=f"page-{page_number}.png")
    except (OSError, RuntimeError, ValueError, IndexError):
        app.logger.exception("Preview rendering failed for job %s", job_id)
        return jsonify(error="نمایش این صفحه انجام نشد."), 503
    finally:
        PREVIEW_SLOTS.release()


_PAGE_SEPARATOR = re.compile(r"^={20} PAGE ([0-9۰-۹٠-٩]+) ={20}\r?$", re.MULTILINE)


@app.get("/preview-text/<job_id>/<int:document_index>/<int:page_number>")
def preview_text(job_id: str, document_index: int, page_number: int):
    document_info = _owned_document(job_id, document_index, page_number)
    if not document_info:
        return jsonify(error="سند یافت نشد."), 404
    summary, _ = document_info
    if summary["state"] in ("waiting", "running"):
        return jsonify(state="pending", text="")
    document_id = summary["id"]
    with LOCK:
        handwriting = (_owned_job(job_id, session.get("owner_id")) or {}).get("handwriting_declared", False)
    quality, annotations = _preview_quality(document_id, page_number, handwriting=handwriting)
    source = (OUT_DIR / document_id / f"{document_id}.txt").resolve()
    if OUT_DIR.resolve() not in source.parents or not source.is_file():
        return jsonify(state="unavailable", text="")
    try:
        with source.open("rb") as handle:
            prefix = handle.read(MAX_PREVIEW_TEXT_BYTES + 1)
    except OSError:
        app.logger.exception("Preview text read failed for job %s", job_id)
        return jsonify(error="پیش‌نمایش متن انجام نشد."), 503
    source_limited = len(prefix) > MAX_PREVIEW_TEXT_BYTES
    content = prefix[:MAX_PREVIEW_TEXT_BYTES].decode("utf-8", errors="ignore")
    markers = list(_PAGE_SEPARATOR.finditer(content))
    for index, marker in enumerate(markers):
        if int(marker.group(1)) != page_number:
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
        page_text = content[marker.end():end].strip()
        # Strip only the exact system prefix recorded for this export. Literal
        # document phrases, ellipses and other text are never regex-filtered.
        for annotation in annotations:
            if annotation.get("kind") == "quality_notice":
                for ending in ("\r\n", "\n"):
                    prefix = annotation["text"] + ending
                    if page_text.startswith(prefix):
                        page_text = page_text[len(prefix):]
                        break
        truncated = len(page_text) > MAX_PREVIEW_TEXT_CHARS or (source_limited and end == len(content))
        return jsonify(state="ready", text=page_text[:MAX_PREVIEW_TEXT_CHARS],
                       truncated=truncated, quality=quality)
    return jsonify(state="unavailable", text="", truncated=source_limited,
                   reason="preview_limit" if source_limited else "page_not_found")


def _preview_quality(document_id: str, page_number: int, *, handwriting=False):
    """Read compact quality metadata; bounded fallback supports older exports."""
    unknown = (unknown_quality(handwriting=handwriting), [])
    if not QUALITY_SLOTS.acquire(blocking=False):
        return unknown
    try:
        for suffix, limit in (("quality.json", 1024 * 1024), ("json", MAX_PREVIEW_TEXT_BYTES)):
            path = (OUT_DIR / document_id / f"{document_id}.{suffix}").resolve()
            if OUT_DIR.resolve() not in path.parents or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    data = handle.read(limit + 1)
                if len(data) > limit:
                    continue
                payload = json.loads(data)
                if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
                    continue
                for page in payload["pages"]:
                    if not isinstance(page, dict) or page.get("page") != page_number:
                        continue
                    quality = page.get("quality")
                    if suffix == "json":
                        quality = page_quality(page, handwriting=handwriting or
                                               bool(page.get("handwriting_declared")) or
                                               bool(payload.get("document", {}).get("handwriting_declared")))
                    if not isinstance(quality, dict) or quality.get("human_verified") is not False:
                        continue
                    partial = OUT_DIR / document_id / ".review" / f"{page_number:04d}.json"
                    if partial.is_file() and partial.stat().st_size <= 64 * 1024:
                        quality = quality | {"review_items": json.loads(partial.read_text("utf-8"))}
                    annotations = [item for item in page.get("export_annotations", [])
                                   if isinstance(item, dict) and item.get("kind") == "quality_notice"
                                   and isinstance(item.get("text"), str) and len(item["text"]) <= 200]
                    return quality, annotations
            except (OSError, ValueError, TypeError, AttributeError):
                app.logger.warning("OCR quality metadata unavailable for a preview")
        return unknown
    finally:
        QUALITY_SLOTS.release()


@app.get("/download/<job_id>/<path:name>")
def download(job_id: str, name: str):
    with LOCK:
        job = _owned_job(job_id, session.get("owner_id"))
        if not job or (name not in job["files"] and name != job["zip"]):
            return "یافت نشد", 404
    p = (OUT_DIR / name).resolve()
    if not p.is_file() or OUT_DIR.resolve() not in p.parents:
        return "یافت نشد", 404
    return send_file(p, as_attachment=True)


@app.get("/reviews")
def review_workspace():
    if not session.get("owner_id"):
        return "یافت نشد", 404
    return render_template("review.html", csrf_token=session["csrf_token"], csp_nonce=g.csp_nonce)


@app.get("/reviews/data")
@_bounded_review
def review_documents():
    if not session.get("owner_id"):
        return jsonify(error="یافت نشد"), 404
    return jsonify(documents=_review_store().list_documents(session["owner_id"]))


@app.route("/reviews/<document_id>", methods=["GET", "POST"])
@_bounded_review
def review_document(document_id):
    owner = session.get("owner_id")
    if not owner:
        return jsonify(error="یافت نشد"), 404
    try:
        store = _review_store()
        current = store.get(owner, document_id)  # Ownership checked before mutation.
        if request.method == "GET":
            return jsonify(current)
        request.max_content_length = 128 * 1024
        body = request.get_json()
        if not isinstance(body, dict) or set(body) - {"version", "changes", "reviewer", "activate_revision"}:
            return jsonify(error="ساختار درخواست معتبر نیست."), 400
        return jsonify(store.save(owner, document_id, body.get("version"), body.get("changes", []),
                                  reviewer=body.get("reviewer"), activate_revision=body.get("activate_revision")))
    except ReviewConflict:
        return jsonify(error="این سند در نشست دیگری تغییر کرده است. نسخهٔ تازه را دریافت کنید."), 409
    except (FileNotFoundError, KeyError):
        return jsonify(error="یافت نشد"), 404
    except ValueError:
        return jsonify(error="اطلاعات بازبینی معتبر نیست یا از حد مجاز بیشتر است."), 400


@app.get("/reviews/<document_id>/pages/<int:page_number>")
@_bounded_review
def review_image(document_id, page_number):
    try:
        if not session.get("owner_id") or not 1 <= page_number <= MAX_BATCH_PAGES:
            return "یافت نشد", 404
        path = _review_store().image_path(session["owner_id"], document_id, page_number)
        return send_file(path, mimetype="image/png", conditional=True)
    except (FileNotFoundError, KeyError, ValueError):
        return "یافت نشد", 404


@app.get("/reviews/<document_id>/search")
@_bounded_review
def review_search_snapshot(document_id):
    try:
        if not session.get("owner_id"):
            return jsonify(error="یافت نشد"), 404
        return jsonify(_review_store().search_snapshot(session["owner_id"], document_id))
    except (FileNotFoundError, KeyError, ValueError):
        return jsonify(error="یافت نشد"), 404


if __name__ == "__main__":
    recover_jobs()
    print("رابط وب: http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
