"""Run repository tests only after configuring and guarding an explicit private workspace."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pattern", default="test_*.py")
    args = parser.parse_args()
    root = args.root.resolve()
    source = Path(__file__).resolve().parents[1]
    if not args.root.is_absolute() or root == Path(root.anchor) or root == source or source in root.parents:
        parser.error("Test root must be an explicit directory outside the source tree")
    protected = [Path(path).resolve() for path in (
        "C:/Websites/ocr.gomrok.org", "E:/GomrokOCR", "C:/Users/Administrator/Music/LLM-FINAL")]
    if any(root == path or path in root.parents for path in protected):
        parser.error("Test root overlaps protected production storage")
    marker = root / ".ocr-test-workspace"
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        parser.error("Existing non-test storage cannot be reused as a test workspace")
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 1536 * 1024 * 1024:
        parser.error("Test workspace does not retain the storage safety reserve")
    marker.write_text("isolated OCR tests only\n", encoding="utf-8")
    for name in ("temp", "storage", "cache", "logs", "index"):
        (root / name).mkdir(exist_ok=True)
    os.environ.update(OCR_TEST_ROOT=str(root), OCR_DATA_DIR=str(root/"storage"),
                      OCR_TEMP_DIR=str(root/"temp"), OCR_LOG_DIR=str(root/"logs"),
                      TEMP=str(root/"temp"), TMP=str(root/"temp"), TMPDIR=str(root/"temp"),
                      XDG_CACHE_HOME=str(root/"cache"), PIP_CACHE_DIR=str(root/"cache"),
                      PYTHONDONTWRITEBYTECODE="1", OCR_PIPELINE_MODE="legacy", OCR_TESSERACT_ONLY="1")
    guard_path = source / "tests" / "isolation"
    os.environ["PYTHONPATH"] = os.pathsep.join([str(guard_path), str(source), os.environ.get("PYTHONPATH", "")])
    sys.path[:0] = [str(guard_path), str(source)]
    sys.dont_write_bytecode = True
    import sitecustomize
    sitecustomize.install_guard()
    import tempfile
    tempfile.tempdir = str(root / "temp")
    import unittest
    suite = unittest.defaultTestLoader.discover(str(source/"tests"), pattern=args.pattern)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if outcome.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
