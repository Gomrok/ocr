"""Optional second optical reading for disputed numbers in scanned documents."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from itertools import groupby
import json
import math
from pathlib import Path
import re

import numpy as np
import pymupdf

from .engines import EN_DIGITS, FA_DIGITS, en_digits_only
from .storage import file_sha256

MODEL_DIR = "OCR_NUMBER_MODEL_DIR"
MODEL_SOURCE = "cf41676c0cf590cd0a01ebb342faf1ce72c42e19a213984cbad6e5ddc91ae265"
NUMBER = re.compile(r"[+\-−]?[0-9۰-۹٠-٩]+(?:[.,،٫٬/:\-‐‑–][0-9۰-۹٠-٩]+)*[%٪]?")
WRAPPERS = "()[]«»؛،,\u061c\u200e\u200f \t\r\n"
PERSIAN_DIGITS = str.maketrans(EN_DIGITS, FA_DIGITS)
FULL_WIDTH = 2560
INPUT_HEIGHT = 96


def number_span(text):
    token = text.strip(WRAPPERS)
    if not NUMBER.fullmatch(token):
        return None
    start = text.index(token)
    return en_digits_only(token), start, start + len(token)


def _bundle(directory):
    root = Path(directory)
    if not root.is_absolute():
        raise ValueError("The numeric model directory must be absolute")
    manifest = root / "manifest.json"
    spec = json.loads(manifest.read_text("utf-8"))
    if spec.get("format") != 1 or spec.get("source_sha256") != MODEL_SOURCE or spec.get("height") != INPUT_HEIGHT:
        raise ValueError("Unsupported numeric recognition model")
    model = root / "recognizer.onnx"
    if file_sha256(model) != spec.get("model_sha256"):
        raise ValueError("Numeric model digest does not match its manifest")
    return model, manifest, spec


def model_identity(directory):
    _, manifest, spec = _bundle(directory)
    try:
        packages = {name: version(name) for name in ("onnxruntime", "Pillow")}
    except PackageNotFoundError as exc:
        raise ValueError("The numeric model runtime is missing") from exc
    return {"model_sha256": spec["model_sha256"], "manifest_sha256": file_sha256(manifest),
            "source_sha256": MODEL_SOURCE, "packages": packages}


class NumberReader:
    def __init__(self, directory):
        import onnxruntime

        model, _, spec = _bundle(directory)
        self.labels = {int(index): char for index, char in spec["labels"].items()}
        self.model_sha256 = spec["model_sha256"]
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(model.read_bytes(), sess_options=options,
                                                    providers=["CPUExecutionProvider"])

    def read(self, crop, width):
        from PIL import Image

        scaled_width = min(width - 32, max(1, int(crop.shape[1] * INPUT_HEIGHT / crop.shape[0])))
        image = Image.fromarray(crop).resize((scaled_width, INPUT_HEIGHT), Image.Resampling.LANCZOS)
        ink = 1 - np.asarray(image, dtype=np.float32) / 255
        pixels = np.zeros((1, 3, INPUT_HEIGHT, width), dtype=np.float32)
        pixels[0, :, :, 16:16 + scaled_width] = ink
        logits, length = self.session.run(None, {"pixels": pixels,
                                                "valid_width": np.array([scaled_width + 32], dtype=np.int64)})
        scores = logits[0, :, 0, :int(length[0])]
        winning = scores.argmax(axis=0)
        confidence = 1 / np.exp(scores - scores.max(axis=0)).sum(axis=0)
        text, digit_scores = [], []
        for index, group in groupby(range(len(winning)), key=winning.__getitem__):
            char = self.labels.get(index, "")
            text.append(char)
            if char.isdecimal():
                digit_scores.append(max(float(confidence[frame]) for frame in group))
        return {"text": "".join(text), "confidence": min(digit_scores, default=0.0)}


@lru_cache(maxsize=1)
def _reader(directory):
    return NumberReader(directory)


def _replacement(old, first, second):
    one = number_span(first["text"])
    two = number_span(second["text"])
    if one is None or two is None or one[0] != two[0] or one[0] == old:
        return None
    new = one[0]
    lo, hi = sorted((first["confidence"], second["confidence"]))
    if hi < .90 or lo < .65:
        return None
    # For ordinary substitutions keep every sign, separator and digit slot.
    if re.sub(r"\d", "d", new) == re.sub(r"\d", "d", old):
        return new
    # A mark read as an extra zero must be visible in both views. An omitted
    # first digit or a disagreement about the mark is insufficient evidence.
    prefix = first["text"].lstrip()[:1]
    if prefix in {"،", ","} and prefix == second["text"].lstrip()[:1] and old == "0" + new and lo >= .90:
        return prefix + new
    return None


def recheck_numbers(directory, words, page, master_size, source_box, metrics):
    """Reread location-linked flagged fields without changing formatting."""
    candidates = [(word, number_span(word["text"])) for word in words if word.get("needs_review")]
    candidates = [(word, span) for word, span in candidates if span is not None and word.get("bbox")]
    if not candidates:
        return
    # Render directly from the PDF. Downsampling the large preprocessed
    # master would re-use distortions introduced by the primary path.
    scale = min(200 / 72, math.sqrt(8_000_000 / page.rect.get_area()))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
    w, h = pix.width, pix.height
    small = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(h, w)
    sx, sy = w / master_size[0], h / master_size[1]
    reader = _reader(directory)
    candidates.sort(key=lambda item: not any(mark in item[1][0] for mark in "/:٫"))
    for word, (old, start, end) in candidates[:64]:
        a, b, c, d = source_box(word["bbox"])
        pad = max(1, round((d - b) * sy * .07))
        x0, y0 = max(0, math.floor(a * sx) - pad), max(0, math.floor(b * sy) - pad)
        x1, y1 = min(w, math.ceil(c * sx) + pad), min(h, math.ceil(d * sy) + pad)
        crop = small[y0:y1, x0:x1]
        if crop.size < 4:
            continue
        width = min(FULL_WIDTH, max(256, 64 * math.ceil((crop.shape[1] * INPUT_HEIGHT / crop.shape[0] + 192) / 64)))
        if width == FULL_WIDTH:
            continue
        first = reader.read(crop, width)
        metrics.inc("ocr_calls")
        value = number_span(first["text"])
        if value is None or value[0] == old or first["confidence"] < .75:
            continue
        second = reader.read(crop, FULL_WIDTH)
        metrics.inc("ocr_calls")
        new = _replacement(old, first, second)
        if new is None:
            continue
        original = word["text"]
        word.setdefault("raw_ocr_text", original)
        word["text"] = original[:start] + new.translate(PERSIAN_DIGITS) + original[end:]
        word["secondary_numeric_reading"] = {"original": original, "model_sha256": reader.model_sha256,
                                             "source_dpi": round(scale * 72, 2), "crop_bbox": [x0, y0, x1, y1],
                                             "compact": first, "full": second}
        word.pop("numeric_verified", None)
        word.update(numeric_status="unresolved", review_reason="numeric_disagreement")
