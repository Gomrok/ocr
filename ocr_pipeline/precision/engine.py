"""One bounded native Tesseract invocation per attempt. No download/fallback."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import cv2

from .profiles import ENGINE_SHA256, ENGINE_VERSION, MODEL_COMMIT, MODEL_HASHES, Options


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_tsv(text: str) -> list[dict]:
    """Preserve strings and TSV logical order, including NA, 0 and single letters."""
    words = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        try:
            x, y, w, h = (int(row[key]) for key in ("left", "top", "width", "height"))
            confidence = float(row["conf"])
            line = tuple(int(row[key]) for key in ("block_num", "par_num", "line_num"))
        except (KeyError, ValueError, TypeError):
            raise ValueError("Malformed Tesseract word record") from None
        if min(x, y) < 0 or min(w, h) <= 0:
            raise ValueError("Invalid Tesseract word geometry")
        words.append({"text": row["text"], "bbox": [x, y, x+w, y+h],
                      "line_key": list(line), "order": len(words),
                      "confidence_raw": confidence if math.isfinite(confidence) and 0 <= confidence <= 100 else None})
        if len(words) > 10_000:
            raise ValueError("Tesseract word limit exceeded")
    return words


class LocalRecognizer:
    def __init__(self, options: Options, temp_root: Path, *, executable=None, data_dir=None):
        self.options = options
        self.temp_root = Path(temp_root).resolve(strict=True)
        if not self.temp_root.is_dir():
            raise ValueError("Explicit temporary directory required")
        self.executable = executable or os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
        if not self.executable:
            default = Path(r"C:\Tools\GomrokOCR\Tesseract\tesseract.exe")
            self.executable = str(default) if default.is_file() else None
        if not self.executable:
            raise RuntimeError("Pinned local Tesseract is unavailable")
        self.executable = str(Path(self.executable).resolve(strict=True))
        self.data_dir = Path(data_dir or os.environ.get("OCR_TESSDATA_DIR") or
                             Path(__file__).resolve().parents[2] / "tessdata").resolve(strict=True)
        self.calls = 0
        self.manifest = self._validate_installation()

    def _environment(self):
        # Children inherit explicit isolated writable paths; never consult a cloud key.
        env = os.environ.copy()
        env.update(TEMP=str(self.temp_root), TMP=str(self.temp_root), TMPDIR=str(self.temp_root),
                   TESSDATA_PREFIX=str(self.data_dir), OMP_THREAD_LIMIT="1")
        env.pop("GOOGLE_API_KEY", None)
        return env

    def _validate_installation(self):
        engine_hash = file_hash(Path(self.executable))
        if engine_hash != ENGINE_SHA256:
            raise RuntimeError("Tesseract executable differs from the evaluated installation")
        result = subprocess.run([self.executable, "--version"], capture_output=True,
                                timeout=10, env=self._environment(), cwd=self.temp_root)
        version = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
        if result.returncode or version != ENGINE_VERSION:
            raise RuntimeError("Pinned Tesseract version mismatch")
        models = {}
        for lang in self.options.languages.split("+"):
            digest = file_hash(self.data_dir / f"{lang}.traineddata")
            if digest != MODEL_HASHES[lang]:
                raise RuntimeError("Pinned language data mismatch")
            models[lang] = {"sha256": digest, "repository": "tesseract-ocr/tessdata_best",
                            "commit": MODEL_COMMIT, "historical_download_source": "unknown",
                            "identity": "byte-matched official asset"}
        return {"engine": "tesseract", "version": version, "executable_sha256": engine_hash,
                "models": models, "osd": "not_installed_orientation_requires_explicit_input"}

    def recognize(self, image, *, psm: int, languages: str | None = None, internal_image=False):
        languages = languages or self.options.languages
        if psm not in (3, 6, 7, 11) or languages not in (self.options.languages, "fas"):
            raise ValueError("Recognition settings outside the named profile")
        if not (0 < image.size <= self.options.max_pixels):
            raise ValueError("Recognition image exceeds budget")
        if shutil.disk_usage(self.temp_root).free < self.options.min_free_bytes + image.nbytes * 3:
            raise OSError("Insufficient isolated temporary storage")
        with tempfile.TemporaryDirectory(prefix="tess-attempt-", dir=self.temp_root) as directory:
            root = Path(directory)
            input_file, output = root / "input.png", root / "result"
            if not cv2.imwrite(str(input_file), image):
                raise OSError("Failed to write recognition candidate")
            command = [self.executable, str(input_file), str(output), "--tessdata-dir", str(self.data_dir),
                       "-l", languages, "--oem", "1", "--psm", str(psm), "--dpi", str(self.options.dpi),
                       "-c", "tessedit_create_tsv=1", "-c", "tessedit_create_txt=1"]
            if internal_image:
                command += ["-c", "tessedit_write_images=1"]
            self.calls += 1
            with (root / "stderr.log").open("wb") as error_log:
                completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=error_log,
                                           timeout=self.options.timeout_seconds,
                                           cwd=root, env=self._environment())
            if completed.returncode:
                raise RuntimeError("Local Tesseract attempt failed")
            paths = [output.with_suffix(".txt"), output.with_suffix(".tsv")]
            if paths[0].stat().st_size > 1024 * 1024 or paths[1].stat().st_size > 4 * 1024 * 1024:
                raise RuntimeError("Recognition output exceeds budget")
            raw_text, tsv = (path.read_text("utf-8") for path in paths)
            result = {"engine_text_raw": raw_text, "words": parse_tsv(tsv), "tsv_raw": tsv,
                      "psm": psm, "languages": languages, "oem": 1,
                      "input_sha256": file_hash(input_file)}
            if internal_image:
                processed = output.with_suffix(".processed.tif")
                if processed.is_file() and processed.stat().st_size <= 32 * 1024 * 1024:
                    result["internal_image"] = processed.read_bytes()
            return result
