"""Fingerprint only application source/assets, never documents or private settings."""

import hashlib
from pathlib import Path


def source_digest(root=None):
    root = root or Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = [
        root / name
        for name in (
            "web_app.py",
            "serve.py",
            "requirements.txt",
            "requirements-paddle.lock",
            "deploy.ps1",
        )
    ]
    for folder in ("ocr_pipeline", "static", "templates", "tools", "tessdata"):
        files.extend(
            p
            for p in (root / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        content = path.read_bytes()
        if path.suffix in {
            ".py",
            ".ps1",
            ".js",
            ".html",
            ".css",
            ".md",
            ".txt",
            ".lock",
        }:
            content = content.replace(b"\r\n", b"\n")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()
