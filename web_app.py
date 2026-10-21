#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""سامانهٔ پردازش تصویری گمرک با وضوح قابل انتخاب.

آپلود گروهی PDF، پیشرفت زندهٔ صفحه‌به‌صفحه (progress_cb)،
خروجی TXT + JSON + جدول‌ها (CSV/HTML/XLSX) + دیباگ + ZIP.
"""

from __future__ import annotations

import hmac
import io
import os
import re
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pymupdf
from flask import Flask, g, jsonify, render_template, request, send_file, session

from ocr_pipeline.pipeline import process_pdf
from ocr_pipeline.engines import TesseractEngine

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

DATA_DIR = Path(os.environ.get("OCR_DATA_DIR") or Path(__file__).resolve().parent)
UPLOAD_DIR = DATA_DIR / "uploads"
OUT_DIR = DATA_DIR / "output"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
RUN_LOCK = threading.Lock()  # یک پردازش در هر لحظه (مستر 2000DPI سنگین است)
QUEUE_SLOTS = threading.BoundedSemaphore(2)
RATE_LOCK = threading.Lock()
UPLOAD_TIMES: dict[str, deque] = {}
PENDING_OWNERS: set[str] = set()
LOCAL_ENGINE = TesseractEngine("fas")
MAX_BATCH_PAGES = 300
DPI_LEVELS = (100, 250, 500, 1000, 1500, 2000)
DEFAULT_DPI = 2000
PREVIEW_SLOTS = threading.BoundedSemaphore(2)
MAX_PREVIEW_EDGE = 1400
MAX_PREVIEW_TEXT_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_TEXT_CHARS = 30_000


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


def _collect_outputs(stem_dir: Path, stem: str) -> list[str]:
    """نسبی: TXT + JSON + جدول‌ها + دیباگ این سند."""
    names = [f"{stem}/{stem}.txt", f"{stem}/{stem}.json"]
    for sub in ("tables", "debug"):
        d = stem_dir / sub
        if d.is_dir():
            for f in sorted(d.glob("*")):
                if f.is_file():
                    names.append(f"{stem}/{sub}/{f.name}")
    return names


def _zip_outputs(names: list[str], zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in names:
            p = OUT_DIR / n
            if p.is_file():
                zf.write(p, n)


def _process_job(job_id: str, pdf_paths: list[Path], dpi: int):
    with RUN_LOCK:
        try:
            for idx, pdf_path in enumerate(pdf_paths, 1):
                with LOCK:
                    JOBS[job_id]["file_index"] = idx
                    if idx <= len(JOBS[job_id].get("file_statuses", [])):
                        JOBS[job_id]["file_statuses"][idx - 1]["state"] = "running"
                stem = pdf_path.stem
                out_dir = OUT_DIR / stem
                cb = _make_cb(job_id, JOBS[job_id]["documents"][pdf_path.name], idx)
                try:
                    result = process_pdf(pdf_path, out_dir, dpi=dpi,
                                         force_tesseract=os.environ.get("OCR_TESSERACT_ONLY") == "1",
                                         progress_cb=cb)
                    if result["performance"]["failed_pages"] == result["document"]["pages"]:
                        _finish_document(job_id, idx, "error")
                        continue
                except Exception:  # noqa: BLE001
                    app.logger.exception("OCR document failed for job %s", job_id)
                    _finish_document(job_id, idx, "error")
                    with LOCK:
                        JOBS[job_id]["log"].append("پردازش این سند انجام نشد. لطفاً قالب و محتوای فایل را بررسی نمایید.")
                    continue
                names = _collect_outputs(out_dir, stem)
                _finish_document(job_id, idx, "done", names)
                with LOCK:
                    JOBS[job_id]["files"].extend(names)

            with LOCK:
                names = list(JOBS[job_id]["files"])
            if names:
                zip_path = OUT_DIR / f"{job_id}_ocr_results.zip"
                _zip_outputs(names, zip_path)
                with LOCK:
                    JOBS[job_id]["zip"] = zip_path.name
            with LOCK:
                JOBS[job_id]["state"] = "done" if names else "error"
                JOBS[job_id]["finished_monotonic"] = time.monotonic()
        except Exception:  # noqa: BLE001
            app.logger.exception("OCR job failed: %s", job_id)
            with LOCK:
                unfinished = [item["index"] for item in JOBS[job_id].get("file_statuses", [])
                              if item["state"] in ("waiting", "running")]
            for document_index in unfinished:
                _finish_document(job_id, document_index, "error")
            with LOCK:
                JOBS[job_id]["state"] = "error"
                JOBS[job_id]["finished_monotonic"] = time.monotonic()
                JOBS[job_id]["log"].append("پردازش اسناد با خطا مواجه شد. لطفاً دوباره تلاش کنید.")


def _run_job(job_id: str, pdf_paths: list[Path], dpi: int):
    try:
        _process_job(job_id, pdf_paths, dpi)
    finally:
        QUEUE_SLOTS.release()


@app.get("/")
def index():
    session.setdefault("owner_id", uuid.uuid4().hex)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    session.permanent = True
    return render_template("dashboard.html", csrf_token=session["csrf_token"],
                           dpi_levels=DPI_LEVELS, default_dpi=DEFAULT_DPI,
                           csp_nonce=g.csp_nonce)


@app.get("/healthz")
def health():
    ready = LOCAL_ENGINE.available()
    with LOCK:
        active_jobs = sum(job["state"] == "running" for job in JOBS.values())
    return jsonify(status="ok" if ready else "unavailable", engine="tesseract" if ready else None,
                   release=os.environ.get("OCR_RELEASE", "development"),
                   active_jobs=active_jobs), 200 if ready else 503


@app.post("/upload")
def upload():
    owner = session.get("owner_id")
    if not owner:
        return jsonify(error="لطفاً صفحه را تازه‌سازی کنید."), 403
    if not _upload_rate_allowed():
        return jsonify(error="تعداد درخواست‌ها بیش از حد مجاز است. لطفاً بعداً تلاش کنید."), 429
    with LOCK:
        if owner in PENDING_OWNERS or any(job["owner_id"] == owner and job["state"] == "running" for job in JOBS.values()):
            return jsonify(error="درخواست قبلی شما هنوز در حال پردازش است."), 409
        if len(JOBS) >= 1024:
            return jsonify(error="ظرفیت سرویس تکمیل است. لطفاً با پشتیبانی تماس بگیرید."), 503
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
        for file in fs:
            if not file.filename or not file.filename.lower().endswith(".pdf") or file.stream.read(5) != b"%PDF-":
                return jsonify(error="فقط فایل PDF معتبر قابل پردازش است."), 400
            file.stream.seek(0)
        values = request.form.getlist("dpi")
        if len(values) > 1 or (values and values[0] not in {str(value) for value in DPI_LEVELS}):
            return jsonify(error="وضوح پردازش باید یکی از مقادیر ۱۰۰، ۲۵۰، ۵۰۰، ۱۰۰۰، ۱۵۰۰ یا ۲۰۰۰ DPI باشد."), 400
        dpi = int(values[0]) if values else DEFAULT_DPI
        job_id = uuid.uuid4().hex
        documents = {}
        file_statuses = []
        pages = 0
        for index, file in enumerate(fs, 1):
            path = UPLOAD_DIR / f"{job_id}_{index:03d}.pdf"
            saved.append(path)
            file.save(path)
            try:
                with pymupdf.open(path) as document:
                    if document.needs_pass or document.page_count == 0:
                        return jsonify(error="فایل رمزگذاری‌شده یا بدون صفحه قابل پردازش نیست."), 400
                    page_count = document.page_count
                    pages += page_count
            except (RuntimeError, ValueError):
                return jsonify(error="ساختار فایل PDF معتبر نیست."), 400
            if pages > MAX_BATCH_PAGES:
                return jsonify(error="مجموع صفحات هر نوبت نباید از ۳۰۰ صفحه بیشتر باشد."), 400
            name = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
            safe_name = "".join(char for char in name if char.isprintable())[:240]
            documents[path.name] = safe_name
            file_statuses.append({
                "index": index, "id": path.stem, "name": safe_name,
                "size": path.stat().st_size, "page_count": page_count,
                "pages_done": 0, "failed_pages": 0, "state": "waiting", "outputs": [],
            })
        with LOCK:
            PENDING_OWNERS.discard(owner)
            JOBS[job_id] = {
                "owner_id": owner, "documents": documents, "file_statuses": file_statuses, "dpi": dpi,
                "state": "running", "log": [], "pages_total": pages, "pages_done": 0,
                "failed_pages": 0, "review_pages": 0, "files": [], "zip": None, "file_index": 0,
                "file_count": len(saved), "started": datetime.now().isoformat(timespec="seconds"),
                "started_monotonic": time.monotonic(),
            }
        threading.Thread(target=_run_job, args=(job_id, saved, dpi), daemon=True).start()
        started = True
        return jsonify(job_id=job_id, count=len(saved), dpi=dpi)
    finally:
        if not started:
            QUEUE_SLOTS.release()
            with LOCK:
                PENDING_OWNERS.discard(owner)
                if job_id:
                    JOBS.pop(job_id, None)
            for path in saved:
                path.unlink(missing_ok=True)


@app.get("/status/<job_id>")
def status(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["owner_id"] != session.get("owner_id"):
            return jsonify({"state": "unknown"}), 404
        elapsed = max(0, int(job.get("finished_monotonic", time.monotonic())
                             - job.get("started_monotonic", time.monotonic())))
        payload = {key: value for key, value in job.items()
                   if key not in ("owner_id", "documents", "started_monotonic", "finished_monotonic")}
        payload["log"] = job["log"][-60:]
        payload["file_statuses"] = [dict(item) for item in job.get("file_statuses", [])]
        return jsonify(payload | {"elapsed_seconds": elapsed})


def _owned_document(job_id: str, document_index: int, page_number: int):
    if not (1 <= document_index <= 100 and 1 <= page_number <= MAX_BATCH_PAGES):
        return None
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["owner_id"] != session.get("owner_id"):
            return None
        summaries = job.get("file_statuses", [])
        if document_index > len(summaries):
            return None
        summary = dict(summaries[document_index - 1])
        source_name = f"{job_id}_{document_index:03d}.pdf"
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
        with pymupdf.open(source) as document:
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
        truncated = len(page_text) > MAX_PREVIEW_TEXT_CHARS or (source_limited and end == len(content))
        return jsonify(state="ready", text=page_text[:MAX_PREVIEW_TEXT_CHARS],
                       truncated=truncated)
    return jsonify(state="unavailable", text="", truncated=source_limited,
                   reason="preview_limit" if source_limited else "page_not_found")


@app.get("/download/<job_id>/<path:name>")
def download(job_id: str, name: str):
    with LOCK:
        job = JOBS.get(job_id)
        if (not job or job["owner_id"] != session.get("owner_id") or
                (name not in job["files"] and name != job["zip"])):
            return "یافت نشد", 404
    p = (OUT_DIR / name).resolve()
    if not p.is_file() or OUT_DIR.resolve() not in p.parents:
        return "یافت نشد", 404
    return send_file(p, as_attachment=True)


if __name__ == "__main__":
    print("رابط وب: http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
