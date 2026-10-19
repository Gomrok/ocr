"""Production WSGI entry point for the existing Flask application."""
from __future__ import annotations

import argparse
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding="utf-8-sig"))
    allowed = {"OCR_SECRET_KEY", "OCR_SECURE_COOKIES", "OCR_ALLOWED_HOSTS",
               "OCR_DATA_DIR", "OCR_TESSERACT_ONLY", "OCR_TESSDATA_DIR",
               "TESSERACT_CMD", "GOOGLE_API_KEY", "OMP_THREAD_LIMIT", "OCR_PERSISTENT_ENGINE"}
    for key, value in settings.items():
        if key in allowed:
            os.environ[key] = str(value)
    if len(os.environ.get("OCR_SECRET_KEY", "")) < 32:
        raise RuntimeError("A persistent production session key is required")
    os.environ["OCR_RELEASE"] = args.release
    os.chdir(Path(__file__).resolve().parent)
    log_dir = args.settings.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        log_dir / "application.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8")])
    from waitress import serve
    from web_app import app
    serve(app, host="127.0.0.1", port=15101, threads=6,
          trusted_proxy="127.0.0.1", trusted_proxy_count=1,
          trusted_proxy_headers={"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"},
          clear_untrusted_proxy_headers=True,
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
          connection_limit=32, channel_timeout=120, ident="GomrokOCR")


if __name__ == "__main__":
    main()
