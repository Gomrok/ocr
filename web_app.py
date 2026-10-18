#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""سامانهٔ پردازش تصویری گمرک با وضوح قابل انتخاب.

آپلود گروهی PDF، پیشرفت زندهٔ صفحه‌به‌صفحه (progress_cb)،
خروجی TXT + JSON + جدول‌ها (CSV/HTML/XLSX) + دیباگ + ZIP.
"""

from __future__ import annotations

import hmac
import os
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
from flask import Flask, g, jsonify, render_template_string, request, send_file, session

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
        f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
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

PAGE = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#102A43">
<meta name="csrf-token" content="{{ csrf_token }}">
<meta name="description" content="سامانه پردازش تصویری سامانه هوش مصنوعی گمرک؛ استخراج متن و جدول از اسناد PDF با وضوح قابل انتخاب.">
<title>سامانه پردازش تصویری سامانه هوش مصنوعی گمرک</title>
<link rel="preload" href="{{ url_for('static', filename='fonts/Vazir-Bold.woff2') }}" as="font" type="font/woff2" crossorigin>
<link rel="icon" href="data:,">
<style nonce="{{ csp_nonce }}">
@font-face {
  font-family: Vazir;
  src: url("{{ url_for('static', filename='fonts/Vazir-Bold.woff2') }}") format('woff2');
  font-style: normal;
  font-weight: 700;
  font-display: swap;
}
:root {
  color-scheme: light;
  --gm-brand-navy: #102A43;
  --gm-brand-accent: #0F766E;
  --gm-bg-page: #F8FAFC;
  --gm-bg-surface: #FFFFFF;
  --gm-bg-subtle: #F1F5F9;
  --gm-text-primary: #102A43;
  --gm-text-body: #334155;
  --gm-text-secondary: #475569;
  --gm-text-muted: #64748B;
  --gm-border-subtle: #E2E8F0;
  --gm-border-control: #7A8CA3;
  --gm-action-primary: #1D4ED8;
  --gm-action-hover: #1E40AF;
  --gm-action-pressed: #1E3A8A;
  --gm-text-on-action: #FFFFFF;
  --gm-bg-selected: #EFF6FF;
  --gm-focus: #1D4ED8;
  --gm-success: #166534;
  --gm-bg-success: #F0FDF4;
  --gm-warning: #92400E;
  --gm-bg-warning: #FFFBEB;
  --gm-error: #B91C1C;
  --gm-bg-error: #FEF2F2;
  --gm-info: #1E40AF;
  --gm-bg-info: #EFF6FF;
  --gm-bg-accent: #F0FDFA;
  --gm-bg-disabled: #E2E8F0;
  --gm-text-disabled: #475569;
  --canvas: var(--gm-bg-page);
  --paper: var(--gm-bg-surface);
  --ink: var(--gm-text-primary);
  --muted: var(--gm-text-muted);
  --line: var(--gm-border-subtle);
  --accent: var(--gm-action-primary);
  --accent-dark: var(--gm-action-hover);
  --accent-soft: var(--gm-bg-selected);
  --green: var(--gm-success);
  --red: var(--gm-error);
  --radius: 20px;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { scroll-behavior: smooth; scroll-padding-top: 28px; }
body { margin: 0; background: var(--canvas); color: var(--ink); font: 700 14px/1.8 Vazir, Tahoma, sans-serif; }
button, input { font: inherit; }
button, a, [role="button"] { -webkit-tap-highlight-color: transparent; }
button { cursor: pointer; }
button:disabled { cursor: not-allowed; }
a { color: inherit; text-decoration: none; }
button, a, .drop { transition: background .18s, border-color .18s, color .18s, box-shadow .18s, transform .18s; }
:focus-visible { outline: 3px solid var(--gm-focus); outline-offset: 4px; }
::selection { color: var(--gm-info); background: var(--gm-bg-selected); }
h1, h2, h3, p { margin: 0; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.skip-link { position: fixed; top: -80px; right: 24px; z-index: 10; padding: 10px 20px; background: var(--ink); color: var(--gm-text-on-action); border-radius: 10px; }
.skip-link:focus { top: 12px; }
.app-shell { display: grid; grid-template-columns: 236px minmax(0, 1fr); min-height: 100vh; }
.sidebar { background: var(--paper); border-left: 1px solid var(--line); padding: 32px 20px 22px; display: flex; flex-direction: column; gap: 40px; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; gap: 12px; align-items: center; padding-inline: 10px; }
.brand-name { display: block; font-size: 18px; font-weight: 700; line-height: 1.9; }
.brand-caption { display: block; font-size: 11px; color: var(--gm-brand-accent); margin-top: 4px; }
.nav-caption { font-size: 10px; font-weight: 700; color: var(--muted); padding-inline: 15px; margin-bottom: 14px; }
.navigation { display: grid; gap: 8px; }
.nav-item { display: flex; align-items: center; gap: 11px; min-height: 46px; padding: 10px 14px; border: 0; border-radius: 10px; text-align: right; background: transparent; color: var(--gm-text-secondary); font-size: 12px; width: 100%; }
.nav-item:hover { background: var(--gm-bg-subtle); color: var(--ink); }
.nav-item.active { background: var(--accent-soft); color: var(--accent-dark); font-weight: 700; }
.nav-count { margin-inline-start: auto; font-size: 10px; background: var(--gm-bg-subtle); color: var(--gm-text-secondary); border-radius: 6px; min-width: 23px; text-align: center; padding: 0 5px; }
.sidebar-bottom { margin-top: auto; }
.sidebar-note { background: var(--gm-bg-accent); border: 1px solid var(--line); border-radius: 14px; padding: 18px 16px; }
.sidebar-note strong { display: block; font-size: 12px; font-weight: 700; }
.sidebar-note p { font-size: 10px; color: var(--muted); line-height: 1.9; margin-top: 5px; }
.sidebar-foot { display: flex; align-items: center; gap: 8px; padding: 20px 12px 0; color: var(--muted); font-size: 10px; }
.main-area { min-width: 0; }
.topbar { height: 73px; display: flex; justify-content: space-between; align-items: center; padding: 0 44px; border-bottom: 1px solid var(--line); background: var(--paper); }
.breadcrumb { display: flex; gap: 12px; align-items: center; font-size: 11px; color: var(--muted); }
.breadcrumb strong { color: var(--gm-text-body); font-weight: 700; }
.topbar-actions { display: flex; gap: 18px; align-items: center; }
.language { font-size: 10px; color: var(--muted); border-left: 1px solid var(--line); padding-left: 18px; letter-spacing: .4px; }
.help-button { display: flex; align-items: center; gap: 7px; min-height: 44px; padding: 4px 0; border: 0; background: transparent; color: var(--gm-text-secondary); font-size: 11px; }
.help-button:hover { color: var(--accent-dark); }
.content { max-width: 1250px; margin: auto; padding: 32px 44px 24px; }
.intro { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 26px; }
.eyebrow { display: flex; gap: 7px; align-items: center; color: var(--gm-brand-accent); font-size: 10px; font-weight: 700; margin-bottom: 9px; }
h1 { font-size: clamp(25px, 2.4vw, 35px); line-height: 1.7; font-weight: 700; letter-spacing: -1.1px; }
h1 em { font-style: normal; color: var(--gm-brand-accent); }
.intro-copy { font-size: 12px; color: var(--muted); margin-top: 8px; max-width: 660px; line-height: 2; }
.steps { display: flex; list-style: none; margin: 0 0 26px; padding: 16px 25px; border: 1px solid var(--line); border-radius: 13px; background: var(--paper); }
.step { display: flex; flex: 1; align-items: center; gap: 10px; color: var(--muted); position: relative; font-size: 11px; }
.step:not(:last-child)::after { content: ''; flex: 1; height: 1px; background: var(--line); margin: 0 22px; }
.step:last-child { flex: 0 0 auto; }
.step-number { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: var(--gm-bg-subtle); color: var(--gm-text-secondary); font-size: 11px; font-weight: 700; flex-shrink: 0; }
.step.active { color: var(--accent-dark); font-weight: 700; }
.step.active .step-number { color: var(--accent-dark); background: var(--accent-soft); box-shadow: 0 0 0 4px var(--canvas); }
.step.complete { color: var(--gm-text-secondary); }
.step.complete .step-number { color: var(--green); background: var(--gm-bg-success); }
.workspace-grid { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 22px; align-items: start; }
.panel { background: var(--paper); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: 0 3px 5px rgb(16 42 67 / 2%); min-width: 0; }
.upload-panel { padding: 26px; }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 21px; }
.heading-label { display: flex; align-items: center; gap: 10px; }
h2 { font-size: 15px; font-weight: 700; }
.section-index { font-size: 10px; color: var(--muted); font-variant-numeric: tabular-nums; }
.drop { position: relative; border: 1.5px dashed var(--gm-border-control); border-radius: 14px; background: var(--canvas); padding: 32px 16px 27px; text-align: center; cursor: pointer; }
.drop:hover, .drop.hover { background: var(--accent-soft); border-color: var(--accent); }
.drop.hover { box-shadow: inset 0 0 0 3px var(--line); }
.drop[aria-disabled="true"] { cursor: wait; opacity: .65; }
.drop-title { display: block; font-weight: 700; font-size: 15px; color: var(--gm-text-body); }
.drop-description { display: block; margin-top: 6px; font-size: 11px; color: var(--muted); }
.select-button { display: inline-flex; align-items: center; justify-content: center; gap: 9px; min-height: 44px; margin-top: 18px; border: 1px solid var(--gm-border-control); padding: 8px 18px; border-radius: 8px; background: var(--paper); color: var(--accent-dark); font-size: 12px; font-weight: 700; }
.upload-limits { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; color: var(--muted); font-size: 10px; margin-top: 11px; }
.upload-limits span { display: inline-flex; gap: 5px; align-items: center; }
.selection { margin-top: 24px; scroll-margin-top: 24px; }
.selection-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.selection-header h3 { font-size: 12px; font-weight: 700; color: var(--gm-text-secondary); }
.selection-count { font-size: 11px; color: var(--muted); }
.selection-empty { padding: 12px 0 18px; color: var(--muted); font-size: 11px; }
.file-list { display: grid; gap: 7px; max-height: 232px; overflow-y: auto; margin-top: 12px; scrollbar-width: thin; scrollbar-color: var(--gm-border-control) transparent; }
.file-list:empty { display: none; }
.file-row { display: flex; align-items: center; gap: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--canvas); }
.file-type { display: grid; place-items: center; flex-shrink: 0; width: 32px; height: 38px; border: 1px solid var(--line); border-radius: 7px; background: var(--accent-soft); color: var(--gm-info); font: 700 9px/1 Vazir, sans-serif; }
.file-description { flex: 1; min-width: 0; }
.file-name { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; font-weight: 700; }
.file-size { color: var(--muted); font-size: 10px; }
.text-button { display: inline-grid; place-items: center; min-width: 44px; min-height: 44px; padding: 7px 10px; font-size: 11px; border: 0; border-radius: 9px; background: transparent; color: var(--gm-text-secondary); flex-shrink: 0; }
.text-button:hover { background: var(--accent-soft); color: var(--accent-dark); }
.form-footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; border-top: 1px solid var(--line); padding-top: 20px; margin-top: 2px; }
.footer-hint { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 10px; }
.primary-button { display: inline-flex; align-items: center; justify-content: center; gap: 16px; min-height: 44px; padding: 10px 21px; background: var(--accent); color: var(--gm-text-on-action); border: 1px solid var(--accent); border-radius: 9px; font-size: 12px; font-weight: 700; white-space: nowrap; }
.primary-button:hover:not(:disabled) { background: var(--accent-dark); border-color: var(--accent-dark); box-shadow: 0 4px 12px rgb(29 78 216 / 16%); transform: translateY(-1px); }
.primary-button:active:not(:disabled) { background: var(--gm-action-pressed); border-color: var(--gm-action-pressed); transform: none; }
.primary-button:disabled { background: var(--gm-bg-disabled); color: var(--gm-text-disabled); border-color: var(--gm-bg-disabled); }
.output-column { display: grid; gap: 20px; min-width: 0; }
.results-panel { overflow: hidden; scroll-margin-top: 24px; }
.results-panel > .panel-heading { padding: 24px 22px 0; margin-bottom: 0; }
.result-count { font-size: 10px; padding: 0 6px; min-width: 22px; text-align: center; border-radius: 5px; color: var(--gm-text-secondary); background: var(--gm-bg-subtle); }
.result-empty { padding: 27px 22px 24px; text-align: center; }
.result-empty h3 { font-size: 13px; font-weight: 700; color: var(--gm-text-secondary); }
.result-empty p { margin: 7px auto 0; max-width: 210px; font-size: 11px; line-height: 2; color: var(--muted); }
.output-capabilities { list-style: none; margin: 0; padding: 17px 22px; border-top: 1px solid var(--line); display: grid; gap: 14px; }
.output-capabilities li { display: flex; align-items: center; gap: 9px; color: var(--gm-text-secondary); font-size: 11px; }
.output-capabilities small { margin-inline-start: auto; color: var(--muted); font-size: 9px; }
.formats { display: flex; justify-content: center; flex-wrap: wrap; gap: 5px; background: var(--canvas); border-top: 1px solid var(--line); padding: 13px 18px; }
.formats span { color: var(--gm-text-secondary); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; font-size: 9px; letter-spacing: .5px; background: var(--paper); }
.files { padding: 20px 16px; }
.download-list { display: grid; gap: 7px; max-height: 360px; overflow-y: auto; scrollbar-width: thin; }
.download-link { display: flex; align-items: center; gap: 8px; min-height: 48px; border: 1px solid var(--line); border-radius: 9px; padding: 9px; color: var(--gm-info); background: var(--paper); }
.download-link:hover { background: var(--accent-soft); border-color: var(--gm-border-control); }
.download-link .file-type { width: 30px; height: 31px; font-size: 8px; }
.download-link .file-name { flex: 1; min-width: 0; font-size: 11px; }
.zip-link { display: flex; justify-content: center; align-items: center; gap: 8px; min-height: 44px; background: var(--accent); color: var(--gm-text-on-action); border-radius: 8px; font-size: 10px; font-weight: 700; margin-bottom: 13px; }
.zip-link:hover { background: var(--accent-dark); }
.notice { display: flex; align-items: flex-start; gap: 9px; margin: 15px 0; border: 1px solid var(--gm-error); background: var(--gm-bg-error); border-radius: 10px; padding: 11px 13px; font-size: 11px; color: var(--red); }
.quality-panel { padding: 22px; }
.quality-panel h2 { font-size: 14px; }
.quality-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.quality-caption { margin-top: 3px; font-size: 10px; color: var(--gm-text-secondary); }
.quality-reading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 8px; }
.quality-value { color: var(--gm-brand-accent); background: var(--gm-bg-accent); border: 1px solid var(--line); border-radius: 8px; padding: 4px 8px; font-size: 14px; line-height: 1.8; font-variant-numeric: tabular-nums; white-space: nowrap; }
.quality-level { color: var(--gm-text-secondary); font-size: 10px; }
.quality-control { --dpi-fill: 100%; margin-top: 12px; direction: ltr; }
.quality-track { position: relative; height: 44px; }
.quality-range { appearance: none; -webkit-appearance: none; display: block; width: 100%; height: 44px; margin: 0; padding: 0; background: transparent; cursor: pointer; border-radius: 8px; }
.quality-range::-webkit-slider-runnable-track { height: 14px; border-radius: 20px; background: linear-gradient(to right, var(--gm-brand-accent) var(--dpi-fill), var(--gm-bg-disabled) var(--dpi-fill)); box-shadow: inset 0 0 0 1px var(--gm-border-control); }
.quality-range::-moz-range-track { height: 14px; border-radius: 20px; background: linear-gradient(to right, var(--gm-brand-accent) var(--dpi-fill), var(--gm-bg-disabled) var(--dpi-fill)); box-shadow: inset 0 0 0 1px var(--gm-border-control); }
.quality-range::-webkit-slider-thumb { appearance: none; -webkit-appearance: none; width: 28px; height: 28px; margin-top: -7px; border-radius: 50%; border: 2px solid var(--gm-brand-accent); background: var(--paper); box-shadow: 0 2px 6px rgb(16 42 67 / 18%); position: relative; z-index: 1; }
.quality-range::-moz-range-thumb { width: 24px; height: 24px; border-radius: 50%; border: 2px solid var(--gm-brand-accent); background: var(--paper); box-shadow: 0 2px 6px rgb(16 42 67 / 18%); }
.quality-range:disabled { cursor: not-allowed; }
.quality-range:disabled::-webkit-slider-thumb { border-color: var(--gm-text-disabled); background: var(--gm-bg-subtle); }
.quality-range:disabled::-moz-range-thumb { border-color: var(--gm-text-disabled); background: var(--gm-bg-subtle); }
.quality-ticks { display: flex; justify-content: space-between; position: absolute; inset: 20px 12px auto; pointer-events: none; }
.quality-ticks span { width: 4px; height: 4px; border-radius: 50%; background: var(--paper); box-shadow: 0 0 0 1px var(--gm-border-control); }
.quality-stops { display: flex; justify-content: space-between; margin: 0 -2px; font-size: 9px; color: var(--gm-text-secondary); font-variant-numeric: tabular-nums; }
.quality-stops span { width: 30px; text-align: center; }
.quality-stops .selected { color: var(--gm-brand-accent); }
.quality-endpoints { display: flex; justify-content: space-between; margin-top: 11px; font-size: 10px; color: var(--gm-text-secondary); }
.quality-hint { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--gm-text-secondary); font-size: 10px; line-height: 2; }
.run-resolution { color: var(--gm-text-secondary); font-size: 11px; margin-top: 8px; }
.run-panel { margin-top: 22px; padding: 24px 26px; }
.run-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.run-title { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 700; }
.tag { color: var(--accent-dark); background: var(--gm-bg-info); border-radius: 6px; padding: 3px 9px; font-size: 10px; }
.run-panel[data-state="done"] .tag { background: var(--gm-bg-success); color: var(--green); }
.run-panel[data-state="error"] .tag { background: var(--gm-bg-error); color: var(--red); }
.progress-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 16px 0 9px; color: var(--muted); font-size: 11px; }
.progress-value { color: var(--accent); font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.bar { height: 5px; border-radius: 5px; background: var(--gm-bg-disabled); overflow: hidden; }
.bar > div { width: 0; height: 100%; border-radius: inherit; background: var(--accent); transition: width .35s; }
.run-panel[data-state="done"] .bar > div { background: var(--green); }
.run-panel[data-state="error"] .bar > div { background: var(--red); }
.log-details { border-top: 1px solid var(--line); padding-top: 14px; margin-top: 20px; }
.log-details summary { color: var(--gm-text-secondary); font-size: 11px; cursor: pointer; min-height: 44px; }
.log { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 180px; overflow-y: auto; background: var(--canvas); border: 1px solid var(--line); padding: 13px; border-radius: 9px; font-size: 11px; line-height: 2; color: var(--gm-text-body); scrollbar-width: thin; }
.workspace-foot { display: flex; align-items: center; justify-content: space-between; gap: 14px; color: var(--muted); margin-top: 25px; font-size: 9px; }
.workspace-foot span { display: inline-flex; align-items: center; gap: 6px; }
.workspace-foot b { font-weight: 700; color: var(--muted); letter-spacing: .8px; font-size: 8px; }
dialog { width: min(460px, calc(100% - 32px)); padding: 26px; border: 1px solid var(--line); border-radius: 20px; background: var(--paper); color: var(--ink); box-shadow: 0 24px 90px rgb(16 42 67 / 18%); }
dialog::backdrop { background: rgb(16 42 67 / 32%); backdrop-filter: blur(4px); }
.dialog-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 13px; }
.dialog-heading h2 { font-size: 16px; }
.guide-steps { padding-right: 22px; margin: 0; display: grid; gap: 16px; font-size: 12px; }
.guide-steps li::marker { color: var(--accent); font-weight: 700; }
.guide-steps p { color: var(--muted); font-size: 11px; margin-top: 3px; }
.guide-note { font-size: 11px; line-height: 2; margin: 20px 0; padding: 12px; background: var(--gm-bg-subtle); border-radius: 9px; color: var(--gm-text-secondary); }
.dialog-close { width: 100%; }
@media (min-width: 1550px) { .content { padding-top: 48px; } .drop { padding-block: 45px 36px; } .intro { margin-bottom: 38px; } }
@media (max-width: 1190px) {
  .app-shell { grid-template-columns: 208px minmax(0, 1fr); }
  .sidebar { padding-inline: 14px; }
  .topbar { padding-inline: 28px; }
  .content { padding-inline: 28px; }
  .workspace-grid { grid-template-columns: minmax(0, 1fr) 248px; gap: 18px; }
  .upload-panel { padding: 22px; }
  .footer-hint { max-width: 135px; }
}
@media (max-width: 1020px) {
  .app-shell { grid-template-columns: 186px minmax(0, 1fr); }
  .brand { padding-inline: 5px; gap: 9px; }
  .brand-name { font-size: 23px; }
  .brand-caption { font-size: 9px; }
  .nav-item { padding-inline: 10px; font-size: 11px; }
  .topbar { padding-inline: 24px; }
  .content { padding-inline: 24px; }
  .workspace-grid { grid-template-columns: minmax(0, 1fr); }
  .results-panel { display: grid; grid-template-columns: 1fr 1fr; }
  .results-panel > .panel-heading, .files, .formats { grid-column: 1 / -1; }
  .result-empty { padding-block: 18px; }
  .output-capabilities { border-top: 0; border-right: 1px solid var(--line); align-content: center; margin-block: 20px; }
  .results-panel.has-files .output-capabilities { display: none; }
  .footer-hint { max-width: none; }
}
@media (max-width: 700px) {
  .app-shell { display: block; }
  .sidebar { position: relative; height: auto; padding: 16px 20px 0; gap: 17px; border-left: 0; border-bottom: 1px solid var(--line); }
  .brand { padding: 0; }
  .brand-name { font-size: 18px; }
  .brand-caption { font-size: 10px; }
  .nav-caption, .sidebar-bottom, .navigation .nav-count { display: none; }
  .navigation { display: flex; gap: 16px; }
  .nav-item { width: auto; padding: 10px 1px 12px; border-radius: 0; min-height: 44px; gap: 7px; font-size: 10px; white-space: nowrap; border-bottom: 2px solid transparent; }
  .nav-item.active { border-bottom-color: var(--accent); background: transparent; }
  .topbar { height: 55px; padding-inline: 20px; background: transparent; border-bottom: 0; }
  .breadcrumb { font-size: 10px; gap: 8px; }
  .language { display: none; }
  .help-button { font-size: 10px; }
  .content { padding: 17px 20px 24px; }
  .intro { margin-bottom: 24px; }
  h1 { font-size: 27px; }
  .intro-copy { font-size: 11px; margin-top: 8px; }
  .steps { padding: 13px 14px; margin-bottom: 20px; }
  .step { gap: 7px; font-size: 9px; }
  .step-number { width: 23px; height: 23px; font-size: 9px; }
  .step:not(:last-child)::after { margin-inline: 10px; min-width: 6px; }
  .upload-panel { padding: 20px; border-radius: 16px; }
  .panel-heading { margin-bottom: 18px; }
  .drop { padding: 28px 12px 24px; }
  .drop-title { font-size: 14px; }
  .drop-description { font-size: 11px; }
  .upload-limits { font-size: 10px; }
  .footer-hint { font-size: 10px; max-width: 120px; }
  .primary-button { padding-inline: 16px; gap: 12px; }
  .results-panel { border-radius: 16px; }
  .results-panel > .panel-heading { padding-top: 20px; }
  .output-capabilities { padding-inline: 17px; gap: 12px; }
  .output-capabilities li { font-size: 10px; gap: 6px; }
  .output-capabilities small { font-size: 8px; }
  .result-empty { padding-inline: 12px; }
  .result-empty h3 { font-size: 12px; }
  .result-empty p { font-size: 11px; }
  .run-panel { padding: 20px; border-radius: 16px; }
  .workspace-foot { font-size: 8px; align-items: flex-start; }
  .workspace-foot b { font-size: 7px; white-space: nowrap; }
}
@media (max-width: 420px) {
  .navigation { gap: 12px; flex-wrap: wrap; }
  .step, .step:last-child { flex: 1; flex-direction: column; gap: 6px; }
  .step:not(:last-child)::after { display: none; }
  .form-footer { flex-direction: column-reverse; align-items: stretch; }
  .footer-hint { max-width: none; justify-content: center; }
  .results-panel { display: block; }
  .output-capabilities { border-right: 0; border-top: 1px solid var(--line); margin: 0; padding: 18px 22px; }
  .output-capabilities li { font-size: 11px; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>

<a href="#workspace" class="skip-link">رفتن به محتوای اصلی</a>
<div class="app-shell">
  <aside class="sidebar" aria-label="ناوبری اصلی">
    <a class="brand" href="#workspace" aria-label="سامانه پردازش تصویری سامانه هوش مصنوعی گمرک">

      <span><strong class="brand-name">سامانه پردازش تصویری</strong><span class="brand-caption">سامانه هوش مصنوعی گمرک</span></span>
    </a>
    <div>
      <p class="nav-caption">خدمات پردازش اسناد</p>
      <nav class="navigation" aria-label="بخش‌های فضای کار">
        <a class="nav-item active" href="#workspace" aria-current="page">تبدیل سند</a>
        <a class="nav-item" href="#selection">فایل‌های انتخاب‌شده<span class="nav-count" id="navCount">۰</span></a>
        <a class="nav-item" href="#results">خروجی‌ها<span class="nav-count" id="navResults">۰</span></a>
      </nav>
    </div>
    <div class="sidebar-bottom">
      <div class="sidebar-note">

        <strong>استخراج متن و اطلاعات</strong>
        <p>استخراج متن و جدول از اسناد فارسی و دریافت خروجی در قالب‌های استاندارد.</p>
      </div>
      <div class="sidebar-foot">مرکز پردازش اسناد گمرک</div>
    </div>
  </aside>
  <div class="main-area">
    <header class="topbar">
      <div class="breadcrumb"><span>فضای کار</span><strong>تبدیل سند</strong></div>
      <div class="topbar-actions"><span class="language" lang="en" dir="ltr">FA / EN</span><button class="help-button" type="button" id="openHelp">راهنمای استفاده</button></div>
    </header>
    <main class="content" id="workspace" tabindex="-1">
      <section class="intro" aria-labelledby="pageTitle">
        <div><p class="eyebrow">پردازش و تبدیل اسناد</p><h1 id="pageTitle">استخراج متن از <em>اسناد PDF</em></h1><p class="intro-copy">فایل‌های PDF را بارگذاری کنید و متن استخراج‌شده، جدول‌ها و داده‌های ساختاریافته را دریافت نمایید.</p></div>

      </section>
      <ol class="steps" aria-label="مراحل تبدیل سند">
        <li class="step active" id="stepUpload" aria-current="step"><span class="step-number">۱</span><span>انتخاب فایل</span></li>
        <li class="step" id="stepProcess"><span class="step-number">۲</span><span>پردازش سند</span></li>
        <li class="step" id="stepDownload"><span class="step-number">۳</span><span>دریافت خروجی</span></li>
      </ol>
      <div class="workspace-grid">
        <section class="panel upload-panel" aria-labelledby="uploadHeading">
          <div class="panel-heading"><div class="heading-label"><h2 id="uploadHeading">بارگذاری اسناد</h2></div><span class="section-index" lang="en">PDF</span></div>
          <form id="f" novalidate>
            <input class="sr-only" type="file" id="file" accept=".pdf,application/pdf" multiple tabindex="-1" aria-label="انتخاب فایل‌های PDF">
            <div class="drop" id="drop" role="button" tabindex="0" aria-controls="file" aria-describedby="uploadLimits" aria-label="انتخاب یا کشیدن و رها کردن فایل‌های PDF" aria-disabled="false">
              <span class="drop-title">فایل‌های PDF را در این بخش رها کنید</span>
              <span class="drop-description">یا برای بارگذاری، فایل‌های موردنظر را از دستگاه انتخاب نمایید.</span>
              <span class="select-button">انتخاب فایل‌ها</span>
            </div>
            <div class="upload-limits" id="uploadLimits"><span>فقط PDF · امکان انتخاب گروهی</span><span>هر نوبت تا ۵۰۰ مگابایت</span></div>
            <div class="notice" id="formError" role="alert" hidden><span id="errorText"></span></div>
            <section class="selection" id="selection" aria-labelledby="selectionHeading">
              <div class="selection-header"><h3 id="selectionHeading">فایل‌های انتخاب‌شده</h3><span class="selection-count" id="selectionCount" role="status" aria-live="polite">۰ فایل</span></div>
              <p class="selection-empty" id="selectionEmpty">فایلی برای پردازش انتخاب نشده است.</p>
              <div class="file-list" id="flist" role="list" aria-label="فهرست فایل‌های انتخاب‌شده"></div>
            </section>
            <div class="form-footer"><span class="footer-hint">پردازش اسناد فارسی و انگلیسی</span><button class="primary-button" type="submit" id="go" disabled><span id="goLabel">شروع پردازش</span></button></div>
          </form>
        </section>
        <div class="output-column">
        <section class="panel results-panel" id="results" aria-labelledby="resultsHeading">
          <div class="panel-heading"><div class="heading-label"><h2 id="resultsHeading">خروجی‌های شما</h2></div><span class="result-count" id="resultCount">۰</span></div>
          <div class="result-empty" id="resultEmpty"><h3 id="resultEmptyTitle">فایلی برای دریافت موجود نیست</h3><p id="resultEmptyText">پس از پایان پردازش، فایل‌های خروجی در این بخش نمایش داده می‌شوند.</p></div>
          <div class="files" id="files" hidden><div id="zipFile"></div><div class="download-list" id="downloadList" aria-label="دانلود خروجی‌ها"></div></div>
          <ul class="output-capabilities"><li>متن قابل ویرایش<small lang="en">TXT</small></li><li>جدول‌های استخراج‌شده<small lang="en">XLSX</small></li><li>دادهٔ ساختاریافته<small lang="en">JSON</small></li></ul>
          <div class="formats" aria-label="قالب‌های خروجی" dir="ltr"><span>TXT</span><span>JSON</span><span>CSV</span><span>XLSX</span><span>HTML</span><span>ZIP</span></div>
        </section>
        <section class="panel quality-panel" aria-labelledby="qualityHeading">
          <div class="quality-heading"><h2 id="qualityHeading"><label for="dpiControl">قدرت استخراج</label></h2><output class="quality-value" id="dpiValue" for="dpiControl" dir="ltr" aria-live="off">۲۰۰۰ DPI</output></div>
          <div class="quality-reading"><span class="quality-caption">وضوح پردازش سند</span><span class="quality-level" id="dpiLevel">سطح ۶ از ۶</span></div>
          <div class="quality-control" id="qualityControl">
            <div class="quality-track">
              <div class="quality-ticks" aria-hidden="true">{% for dpi in dpi_levels %}<span></span>{% endfor %}</div>
              <input class="quality-range" id="dpiControl" type="range" min="0" max="{{ dpi_levels|length - 1 }}" step="1" value="{{ dpi_levels.index(default_dpi) }}" dir="ltr" aria-valuetext="۲۰۰۰ DPI، سطح ۶ از ۶" aria-describedby="qualityHint qualityScale">
            </div>
            <div class="quality-stops" id="dpiStops" aria-hidden="true" lang="en">{% for dpi in dpi_levels %}<span data-dpi="{{ dpi }}">{{ dpi }}</span>{% endfor %}</div>
          </div>
          <div class="quality-endpoints" dir="ltr" aria-hidden="true"><span dir="rtl">پردازش سبک‌تر</span><span dir="rtl">جزئیات بیشتر</span></div>
          <p class="quality-hint" id="qualityHint">وضوح بالاتر، پردازش سنگین‌تر. دقت استخراج به کیفیت فایل اصلی وابسته است.</p>
          <p class="sr-only" id="qualityScale">شش سطح وضوح: ۱۰۰، ۲۵۰، ۵۰۰، ۱۰۰۰، ۱۵۰۰ و ۲۰۰۰ DPI. برای انتخاب، نشانگر را بکشید یا از کلیدهای جهت‌نما استفاده کنید. این تنظیم فقط پیش از شروع هر نوبت قابل تغییر است.</p>
        </section>
        </div>
      </div>
      <section class="panel run-panel" id="status" data-state="running" aria-labelledby="runTitle" hidden>
        <div class="run-heading"><h2 class="run-title" id="runTitle"><span id="runTitleText">در حال پردازش سندها</span></h2><span class="tag" id="tag" role="status" aria-live="polite">در انتظار شروع</span></div>
        <p class="run-resolution">وضوح این نوبت: <bdi id="runDpi" dir="ltr"></bdi></p>
        <div class="progress-meta"><span id="info">در حال آماده‌سازی فایل‌ها…</span><span class="progress-value" id="progressValue">۰٪</span></div>
        <div class="bar" id="progressBar" role="progressbar" aria-label="پیشرفت پردازش" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="fill"></div></div>
        <button class="select-button" id="retryStatus" type="button" hidden>بررسی دوبارهٔ وضعیت</button>
        <details class="log-details"><summary>جزئیات پردازش</summary><div class="log" id="log" role="log" aria-live="off" aria-label="رویدادهای پردازش" tabindex="0"></div></details>
      </section>
      <footer class="workspace-foot"><span>سامانه هوش مصنوعی گمرک</span><b lang="en" dir="ltr">GOMROK IMAGE PROCESSING</b></footer>
      <noscript><p class="notice">برای انتخاب و پردازش فایل‌ها، جاوااسکریپت مرورگر را فعال کنید.</p></noscript>
    </main>
  </div>
</div>
<dialog id="helpDialog" aria-labelledby="helpTitle">
  <div class="dialog-heading"><h2 id="helpTitle">راهنمای پردازش اسناد</h2><button class="text-button" type="button" id="closeHelp" aria-label="بستن راهنما">بستن</button></div>
  <ol class="guide-steps"><li><strong>فایل‌های PDF را اضافه کنید</strong><p>انتخاب گروهی یا کشیدن و رها کردن؛ مجموع حجم فایل‌ها در هر نوبت کمتر از ۵۰۰ مگابایت باشد.</p></li><li><strong>وضوح را انتخاب و پردازش را شروع کنید</strong><p>در بخش «قدرت استخراج» یکی از شش سطح ۱۰۰، ۲۵۰، ۵۰۰، ۱۰۰۰، ۱۵۰۰ یا ۲۰۰۰ DPI را انتخاب نمایید. پیش‌فرض ۲۰۰۰ DPI است؛ وضوح بالاتر معمولاً زمان و منابع بیشتری نیاز دارد و دقت تضمین‌شده نیست.</p></li><li><strong>فایل‌های خروجی را دریافت کنید</strong><p>پیشرفت صفحه‌ها را دنبال کنید و فایل‌های خروجی را به‌صورت جداگانه یا در یک بستهٔ ZIP دریافت نمایید. خروجی جدول برای اسناد جدول‌دار تولید می‌شود.</p></li></ol>
  <p class="guide-note">پردازش با موتورهای OCR تنظیم‌شده در پروژه انجام می‌شود. در صورت دریافت خطای موتور، تنظیمات سرویس را بررسی کنید.</p>
  <button class="primary-button dialog-close" type="button" id="confirmHelp">متوجه شدم</button>
</dialog>
<script nonce="{{ csp_nonce }}">
(() => {
'use strict';
const byId = id => document.getElementById(id);
const drop = byId('drop');
const fileInput = byId('file');
const flist = byId('flist');
const statusBox = byId('status');
const tag = byId('tag');
const info = byId('info');
const fill = byId('fill');
const log = byId('log');
const files = byId('files');
const go = byId('go');
const form = byId('f');
const help = byId('helpDialog');
const number = value => new Intl.NumberFormat('fa-IR').format(value);
const dpiLevels = {{ dpi_levels|tojson }};
const defaultDpi = {{ default_dpi|tojson }};
const dpiControl = byId('dpiControl');
const selectedDpi = () => dpiLevels[Number(dpiControl.value)] || defaultDpi;
const maxBatchBytes = 500 * 1024 * 1024;
let selectedFiles = [];
let busy = false;
let pollTimer;
let controller;
let outputSignature = '';
let activeJobId = null;

function renderDpi() {
  const dpi = selectedDpi();
  const index = dpiLevels.indexOf(dpi);
  const valueText = number(dpi) + ' DPI';
  const levelText = 'سطح ' + number(index + 1) + ' از ' + number(dpiLevels.length);
  byId('dpiValue').textContent = valueText;
  byId('dpiLevel').textContent = levelText;
  dpiControl.setAttribute('aria-valuetext', valueText + '، ' + levelText);
  byId('qualityControl').style.setProperty('--dpi-fill', 100 * index / (dpiLevels.length - 1) + '%');
  byId('dpiStops').querySelectorAll('span').forEach(stop => {
    stop.classList.toggle('selected', Number(stop.dataset.dpi) === dpi);
  });
}
dpiControl.addEventListener('input', renderDpi);
dpiControl.addEventListener('change', renderDpi);
renderDpi();

function notice(message = '') {
  byId('errorText').textContent = message;
  byId('formError').hidden = !message;
}

function fileSize(bytes) {
  return bytes >= 1024 * 1024
    ? number(Math.round(bytes / 1024 / 1024 * 10) / 10) + ' مگابایت'
    : number(Math.max(1, Math.ceil(bytes / 1024))) + ' کیلوبایت';
}

function renderSelection() {
  flist.replaceChildren();
  byId('selectionEmpty').hidden = selectedFiles.length > 0;
  byId('selectionCount').textContent = number(selectedFiles.length) + ' فایل';
  byId('navCount').textContent = number(selectedFiles.length);
  selectedFiles.forEach((file, index) => {
    const row = document.createElement('div');
    row.className = 'file-row';
    row.setAttribute('role', 'listitem');
    const type = document.createElement('span');
    type.className = 'file-type';
    type.textContent = 'PDF';
    type.lang = 'en';
    const description = document.createElement('div');
    description.className = 'file-description';
    const name = document.createElement('bdi');
    name.className = 'file-name';
    name.textContent = file.name;
    name.title = file.name;
    const size = document.createElement('span');
    size.className = 'file-size';
    size.textContent = fileSize(file.size);
    description.append(name, size);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'text-button';
    remove.disabled = busy;
    remove.setAttribute('aria-label', 'کنار گذاشتن ' + file.name + ' از این نوبت');
    remove.textContent = 'حذف از فهرست';
    remove.addEventListener('click', () => {
      if (busy) return;
      selectedFiles.splice(index, 1);
      notice();
      renderSelection();
      const next = flist.querySelectorAll('button');
      (next[Math.min(index, next.length - 1)] || drop).focus();
    });
    row.append(type, description, remove);
    flist.append(row);
  });
  go.disabled = busy || selectedFiles.length === 0;
}

function addFiles(incoming) {
  if (busy) return;
  const valid = [];
  let rejected = 0;
  for (const file of incoming) {
    if (!/\.pdf$/i.test(file.name) || !file.size) { rejected++; continue; }
    const duplicate = [...selectedFiles, ...valid].some(existing =>
      existing.name === file.name && existing.size === file.size && existing.lastModified === file.lastModified);
    if (!duplicate) valid.push(file);
  }
  const combined = [...selectedFiles, ...valid];
  if (combined.length > 100) {
    notice('در هر نوبت حداکثر ۱۰۰ فایل PDF قابل پردازش است.');
    return;
  }
  if (combined.reduce((total, file) => total + file.size, 0) >= maxBatchBytes) {
    notice('حجم این نوبت باید کمتر از ۵۰۰ مگابایت باشد. فایل‌ها را در چند نوبت اضافه کنید.');
    return;
  }
  selectedFiles = combined;
  notice(rejected ? 'فقط فایل PDF با محتوای غیرخالی پذیرفته می‌شود؛ فایل‌های دیگر اضافه نشدند.' : '');
  renderSelection();
}

drop.addEventListener('click', () => { if (!busy) fileInput.click(); });
drop.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (!busy) fileInput.click(); }
});
drop.addEventListener('dragover', event => { event.preventDefault(); if (!busy) drop.classList.add('hover'); });
drop.addEventListener('dragleave', event => { if (!drop.contains(event.relatedTarget)) drop.classList.remove('hover'); });
drop.addEventListener('drop', event => {
  event.preventDefault();
  drop.classList.remove('hover');
  addFiles(event.dataTransfer.files);
});
fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

function setBusy(value, label = 'در حال پردازش…') {
  busy = value;
  form.setAttribute('aria-busy', String(value));
  drop.setAttribute('aria-disabled', String(value));
  fileInput.disabled = value;
  dpiControl.disabled = value;
  byId('goLabel').textContent = value ? label : 'شروع پردازش';
  renderSelection();
}

function setStep(index) {
  ['stepUpload', 'stepProcess', 'stepDownload'].forEach((id, position) => {
    const step = byId(id);
    step.classList.toggle('active', position === index);
    step.classList.toggle('complete', position < index);
    if (position === index) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
  });
}

function setProgress(percent) {
  const value = Math.round(Math.max(0, Math.min(100, percent)));
  fill.style.width = value + '%';
  byId('progressValue').textContent = number(value) + '٪';
  byId('progressBar').setAttribute('aria-valuenow', String(value));
}

function renderOutputs(id, names, zip) {
  const signature = JSON.stringify([id, names, zip]);
  if (signature === outputSignature) return;
  outputSignature = signature;
  const hasFiles = names.length > 0 || Boolean(zip);
  files.hidden = !hasFiles;
  byId('resultEmpty').hidden = hasFiles;
  byId('results').classList.toggle('has-files', hasFiles);
  byId('resultCount').textContent = number(names.length);
  byId('navResults').textContent = number(names.length);
  byId('downloadList').replaceChildren();
  byId('zipFile').replaceChildren();
  const downloadUrl = name => '/download/' + encodeURIComponent(id) + '/' + encodeURIComponent(name);
  if (zip) {
    const link = document.createElement('a');
    link.className = 'zip-link';
    link.href = downloadUrl(zip);
    link.textContent = 'دانلود همهٔ خروجی‌ها (ZIP)';
    byId('zipFile').append(link);
  }
  names.forEach(path => {
    const filename = path.split('/').pop();
    const link = document.createElement('a');
    link.className = 'download-link';
    link.href = downloadUrl(path);
    link.title = path;
    const type = document.createElement('span');
    type.className = 'file-type';
    type.textContent = filename.split('.').pop().toUpperCase().slice(0, 5);
    type.lang = 'en';
    const name = document.createElement('bdi');
    name.className = 'file-name';
    name.textContent = filename;
    link.append(type, name);
    byId('downloadList').append(link);
  });
}

async function requestJson(url, options) {
  let response;
  try { response = await fetch(url, { ...options, signal: controller.signal }); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('ارتباط با برنامه برقرار نشد. اتصال را بررسی و دوباره تلاش کنید.');
  }
  let data;
  try { data = await response.json(); } catch {
    throw new Error(response.status === 413
      ? 'حجم فایل‌ها از حد مجاز بیشتر است. آن‌ها را در چند نوبت ارسال کنید.'
      : 'پاسخ برنامه قابل خواندن نیست. لطفاً دوباره تلاش کنید.');
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('پاسخ برنامه قابل خواندن نیست. لطفاً دوباره تلاش کنید.');
  }
  if (!response.ok || data.error) {
    const error = new Error(typeof data.error === 'string' ? data.error
      : response.status === 404 ? 'این پردازش دیگر در دسترس نیست؛ ممکن است برنامه دوباره راه‌اندازی شده باشد.'
      : 'درخواست انجام نشد. لطفاً دوباره تلاش کنید.');
    error.status = response.status;
    throw error;
  }
  return data;
}

function failed(message, retryId = null) {
  statusBox.hidden = false;
  statusBox.dataset.state = 'error';
  tag.textContent = 'نیاز به بررسی';
  byId('runTitleText').textContent = 'پردازش با خطا متوقف شد';
  info.textContent = message;
  notice(message);
  activeJobId = retryId;
  byId('retryStatus').hidden = !retryId;
  setBusy(Boolean(retryId), 'منتظر اتصال…');
}

async function poll(id) {
  try {
    const state = await requestJson('/status/' + encodeURIComponent(id));
    if (!['running', 'done', 'error'].includes(state.state)) throw new Error('وضعیت پردازش قابل دریافت نیست. دوباره بررسی کنید.');
    byId('retryStatus').hidden = true;
    if (dpiLevels.includes(state.dpi)) byId('runDpi').textContent = number(state.dpi) + ' DPI';
    const names = Array.isArray(state.files) ? state.files.filter(name => typeof name === 'string') : [];
    const zip = typeof state.zip === 'string' ? state.zip : null;
    renderOutputs(id, names, zip);
    const messages = Array.isArray(state.log) ? state.log.map(String).join('\n') : '';
    if (log.textContent !== messages) {
      const follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.textContent = messages;
      if (follow) log.scrollTop = log.scrollHeight;
    }
    const done = state.state === 'done';
    const partial = done && Number(state.failed_pages) > 0;
    const error = state.state === 'error' || (done && !names.length && !zip);
    statusBox.dataset.state = error ? 'error' : done ? 'done' : 'running';
    tag.textContent = error ? 'نیاز به بررسی' : done ? 'پردازش پایان یافت' : 'در حال پردازش';
    byId('runTitleText').textContent = error ? 'پردازش با خطا متوقف شد' : done ? 'فایل‌های خروجی آماده‌اند' : 'در حال پردازش سندها';
    if (partial) {
      byId('runTitleText').textContent = 'پردازش پایان یافت؛ برخی صفحات نیاز به بررسی دارند';
      byId('status').querySelector('details').open = true;
    } else if (done && Number(state.review_pages) > 0) {
      byId('runTitleText').textContent = 'پردازش پایان یافت؛ بخشی از متن نیاز به بازبینی دارد';
      byId('status').querySelector('details').open = true;
    }
    if (state.pages_total) {
      setProgress(100 * state.pages_done / state.pages_total);
      info.textContent = 'صفحهٔ ' + number(state.pages_done) + ' از ' + number(state.pages_total)
        + ' · فایل ' + number(state.file_index) + ' از ' + number(state.file_count);
    }
    if (error) {
      const message = 'پردازش با خطا روبه‌رو شد. جزئیات پردازش را برای علت خطا باز کنید.';
      info.textContent = message;
      byId('status').querySelector('details').open = true;
      notice(message);
      if (!names.length && !zip) {
        byId('resultEmptyTitle').textContent = 'فایل خروجی تولید نشده است';
        byId('resultEmptyText').textContent = 'پس از بررسی خطا، می‌توانید تبدیل را دوباره شروع کنید.';
      }
      activeJobId = null;
      setBusy(false);
      return;
    }
    if (done) {
      setProgress(100);
      setStep(2);
      activeJobId = null;
      setBusy(false);
      return;
    }
    pollTimer = setTimeout(() => poll(id), 1200);
  } catch (error) { if (error.name !== 'AbortError') failed(error.message, error.status === 404 ? null : id); }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !selectedFiles.length) return;
  clearTimeout(pollTimer);
  controller?.abort();
  controller = new AbortController();
  activeJobId = null;
  byId('retryStatus').hidden = true;
  notice();
  const body = new FormData();
  selectedFiles.forEach(file => body.append('pdfs', file));
  body.append('dpi', String(selectedDpi()));
  setBusy(true, 'در حال ارسال…');
  setStep(0);
  try {
    const data = await requestJson('/upload', { method: 'POST', body,
      headers: { 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content } });
    if (typeof data.job_id !== 'string' || !data.job_id) throw new Error('شناسهٔ پردازش دریافت نشد. دوباره تلاش کنید.');
    activeJobId = data.job_id;
    renderOutputs(data.job_id, [], null);
    byId('resultEmptyTitle').textContent = 'پردازش اسناد در حال انجام است';
    byId('resultEmptyText').textContent = 'فایل‌های خروجی پس از آماده‌شدن در این بخش نمایش داده می‌شوند.';
    statusBox.hidden = false;
    statusBox.dataset.state = 'running';
    byId('runTitleText').textContent = 'در حال پردازش سندها';
    tag.textContent = 'در حال پردازش';
    byId('runDpi').textContent = number(data.dpi || selectedDpi()) + ' DPI';
    info.textContent = 'در حال آماده‌سازی فایل‌ها…';
    log.textContent = '';
    setProgress(0);
    setStep(1);
    byId('goLabel').textContent = 'در حال پردازش…';
    await poll(data.job_id);
  } catch (error) { if (error.name !== 'AbortError') failed(error.message); }
});

byId('openHelp').addEventListener('click', () => help.showModal());
byId('closeHelp').addEventListener('click', () => help.close());
byId('confirmHelp').addEventListener('click', () => help.close());
byId('retryStatus').addEventListener('click', () => {
  if (!activeJobId) return;
  byId('retryStatus').hidden = true;
  controller = new AbortController();
  notice();
  byId('goLabel').textContent = 'در حال پردازش…';
  poll(activeJobId);
});
help.addEventListener('click', event => { if (event.target === help) {
  const bounds = help.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) help.close();
} });
window.addEventListener('pagehide', () => { clearTimeout(pollTimer); controller?.abort(); });
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  if (activeJobId) {
    controller = new AbortController();
    poll(activeJobId);
  } else if (busy) failed('ارسال فایل‌ها متوقف شد. لطفاً دوباره تلاش کنید.');
});
})();
</script>
</body>
</html>"""


def _make_cb(job_id: str, fname: str):
    def cb(event: dict):
        with LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            t = event["type"]
            if t == "start":
                job["pages_total"] += event["pages"]
                job["log"].append(
                    f"سند {fname}: {event['pages']} صفحه با وضوح {event['dpi']} DPI")
            elif t == "page_ok":
                job["pages_done"] += 1
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
                job["log"].append(
                    f"[{fname}] استخراج متن صفحهٔ {event['page']} با خطا مواجه شد.")
            elif t == "done":
                job["log"].append(
                    f"[{fname}] {event['pages_ok']}/{event['pages']} صفحه، "
                    f"{event['words']} کلمه در {event['seconds']} ثانیه")
    return cb


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
            all_names: list[str] = []
            for idx, pdf_path in enumerate(pdf_paths, 1):
                with LOCK:
                    JOBS[job_id]["file_index"] = idx
                stem = pdf_path.stem
                out_dir = OUT_DIR / stem
                cb = _make_cb(job_id, JOBS[job_id]["documents"][pdf_path.name])
                try:
                    result = process_pdf(pdf_path, out_dir, dpi=dpi,
                                         force_tesseract=os.environ.get("OCR_TESSERACT_ONLY") == "1",
                                         progress_cb=cb)
                    if result["performance"]["failed_pages"] == result["document"]["pages"]:
                        continue
                except Exception:  # noqa: BLE001
                    app.logger.exception("OCR document failed for job %s", job_id)
                    with LOCK:
                        JOBS[job_id]["failed_pages"] += 1
                        JOBS[job_id]["log"].append("پردازش این سند انجام نشد. لطفاً قالب و محتوای فایل را بررسی نمایید.")
                    continue
                names = _collect_outputs(out_dir, stem)
                all_names.extend(names)
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
        except Exception:  # noqa: BLE001
            app.logger.exception("OCR job failed: %s", job_id)
            with LOCK:
                JOBS[job_id]["state"] = "error"
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
    return render_template_string(PAGE, csrf_token=session["csrf_token"], csp_nonce=g.csp_nonce,
                                  dpi_levels=DPI_LEVELS, default_dpi=DEFAULT_DPI)


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
        pages = 0
        for index, file in enumerate(fs, 1):
            path = UPLOAD_DIR / f"{job_id}_{index:03d}.pdf"
            saved.append(path)
            file.save(path)
            try:
                with pymupdf.open(path) as document:
                    if document.needs_pass or document.page_count == 0:
                        return jsonify(error="فایل رمزگذاری‌شده یا بدون صفحه قابل پردازش نیست."), 400
                    pages += document.page_count
            except (RuntimeError, ValueError):
                return jsonify(error="ساختار فایل PDF معتبر نیست."), 400
            if pages > MAX_BATCH_PAGES:
                return jsonify(error="مجموع صفحات هر نوبت نباید از ۳۰۰ صفحه بیشتر باشد."), 400
            name = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
            documents[path.name] = "".join(char for char in name if char.isprintable())[:240]
        with LOCK:
            PENDING_OWNERS.discard(owner)
            JOBS[job_id] = {
                "owner_id": owner, "documents": documents, "dpi": dpi,
                "state": "running", "log": [], "pages_total": 0, "pages_done": 0,
                "failed_pages": 0, "review_pages": 0, "files": [], "zip": None, "file_index": 0,
                "file_count": len(saved), "started": datetime.now().isoformat(timespec="seconds"),
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
        return jsonify({key: value for key, value in job.items() if key not in ("owner_id", "documents")}
                       | {"log": job["log"][-60:]})


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
