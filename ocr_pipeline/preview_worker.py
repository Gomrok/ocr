"""Private CLI worker: stdout is PNG only; never an HTTP service."""

import math
import sys

from process_guard import enter_worker_job


def main():
    enter_worker_job()
    import pymupdf

    page_number = int(sys.argv[2])
    with pymupdf.open(sys.argv[1]) as document:
        if not 1 <= page_number <= document.page_count:
            return 4
        page = document[page_number - 1]
        side = max(page.rect.width, page.rect.height)
        if not math.isfinite(side) or side <= 0:
            return 3
        scale = min(2.0, 1999 / side)
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False
        )
        content = pixmap.tobytes("png")
        if len(content) > 16 * 1024 * 1024:
            return 3
        sys.stdout.buffer.write(content)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - native decoder failures must not escape the worker.
        code = 3
    raise SystemExit(code)
