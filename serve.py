"""Production WSGI entry point for the existing Flask application."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path


DISK_RESERVE = 1536 * 1024 * 1024


def configure_storage(settings: dict[str, str], settings_path: Path) -> Path:
    """Configure private writable paths before importing any server/OCR modules."""
    if not settings.get("OCR_DATA_DIR"):
        raise RuntimeError("An explicit private OCR_DATA_DIR is required")
    configured = {
        "OCR_DATA_DIR": settings["OCR_DATA_DIR"],
        "OCR_TEMP_DIR": settings.get(
            "OCR_TEMP_DIR", str(Path(settings["OCR_DATA_DIR"]) / "temp")
        ),
        "OCR_LOG_DIR": settings.get("OCR_LOG_DIR", str(settings_path.parent / "logs")),
    }
    directories = {}
    code = Path(__file__).resolve().parent
    for name, value in configured.items():
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeError(f"{name} must be an absolute private directory")
        path = path.resolve()
        if path == Path(path.anchor) or path == code or code in path.parents:
            raise RuntimeError(f"{name} must be outside the release directory")
        path.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(path).free < DISK_RESERVE:
            raise RuntimeError(
                f"{name} has insufficient space above the safety reserve"
            )
        # Check actual service-account permissions, not only administrator preflight.
        with tempfile.TemporaryFile(dir=path) as probe:
            probe.write(b"ocr-storage-probe")
        directories[name] = path
    for name, path in directories.items():
        os.environ[name] = str(path)
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(directories["OCR_TEMP_DIR"])
    # tempfile may have cached the old system drive before startup.
    tempfile.tempdir = str(directories["OCR_TEMP_DIR"])
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return directories["OCR_LOG_DIR"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text(encoding="utf-8-sig"))
    allowed = {"OCR_SECRET_KEY", "OCR_SECURE_COOKIES", "OCR_ALLOWED_HOSTS",
               "OCR_DATA_DIR", "OCR_TESSERACT_ONLY", "OCR_TESSDATA_DIR",
               "TESSERACT_CMD", "OMP_THREAD_LIMIT", "OCR_PERSISTENT_ENGINE",
               "OCR_REQUIRE_XLSX", "OCR_SCHEMA_V2", "OCR_DOCUMENT_SCHEMA", "OCR_PDF_TEXT_FIRST",
               "OCR_LOCAL_ONLY", "OCR_ACCURACY_MODE", "OCR_PADDLE_PYTHON", "OCR_PADDLE_MODELS"}
    for key, value in settings.items():
        if key in allowed:
            os.environ[key] = str(value)
    if len(os.environ.get("OCR_SECRET_KEY", "")) < 32:
        raise RuntimeError("A persistent production session key is required")
    log_dir = configure_storage(settings, args.settings)
    os.environ["OCR_LOCAL_ONLY"] = "1"
    os.environ["OCR_RELEASE"] = args.release
    os.chdir(Path(__file__).resolve().parent)
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
