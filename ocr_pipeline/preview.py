"""Authorized callers render PDFs in a disposable, resource-bounded worker."""

import struct
import sys
from pathlib import Path

from .process_guard import run_worker

PREVIEW_SECONDS = 20
PREVIEW_MEMORY = 512 * 1024 * 1024
PREVIEW_BYTES = 16 * 1024 * 1024


def render_preview(source: Path, page_number: int) -> bytes:
    code, content = run_worker(
        [
            sys.executable,
            "-X",
            "utf8",
            "-u",
            str(Path(__file__).with_name("preview_worker.py")),
            str(source),
            str(page_number),
        ],
        memory_bytes=PREVIEW_MEMORY,
        seconds=PREVIEW_SECONDS,
        output_limit=PREVIEW_BYTES,
    )
    if code == 4:
        raise IndexError("Page not found")
    if code or len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Preview worker failed")
    width, height = struct.unpack(">II", content[16:24])
    if not 1 <= width <= 2000 or not 1 <= height <= 2000:
        raise RuntimeError("Invalid preview dimensions")
    return content
