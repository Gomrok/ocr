"""Offline, process-isolated second OCR engine. No dependency changes in Flask."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path

from .model_artifacts import EXPECTED_FILES
from .process_guard import ProcessGuard

MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_WORKER_RSS = 8 * 1024 * 1024 * 1024
MODELS = ("PP-OCRv5_server_det", "arabic_PP-OCRv5_mobile_rec")


def verify_models(root: Path) -> None:
    """Verify the exact offline artifacts approved during provisioning."""
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    if manifest_path.stat().st_size > 65536:
        raise ValueError("Invalid offline OCR manifest size")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    required = {
        f"{model}/{name}"
        for model in MODELS
        for name in ("inference.json", "inference.pdiparams", "inference.yml")
    }
    if (
        manifest.get("models") != list(MODELS)
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != required
    ):
        raise ValueError("Invalid offline OCR model manifest")
    for name, digest in manifest["files"].items():
        path = (root / name).resolve(strict=True)
        if (
            root not in path.parents
            or not path.is_file()
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("Invalid offline OCR model path")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest or digest != EXPECTED_FILES.get(name):
            raise ValueError("Offline OCR model integrity check failed")
    for model in MODELS:
        if not (root / model / "inference.pdiparams").is_file():
            raise ValueError("Offline OCR model is incomplete")


class PaddleEngine:
    """One bounded JSON pipe, one worker, and one document; never a public service."""

    name = "paddleocr"

    def __init__(self, python: Path | None = None, models: Path | None = None):
        self.python = python or Path(os.environ.get("OCR_PADDLE_PYTHON", ""))
        self.models = models or Path(os.environ.get("OCR_PADDLE_MODELS", ""))
        self.process = None
        self.messages = queue.Queue(maxsize=4)
        self.lock = threading.Lock()
        self.failure = None
        self.budget = None

    @staticmethod
    def configured() -> bool:
        return (
            Path(os.environ.get("OCR_PADDLE_PYTHON", "")).is_file()
            and (
                Path(os.environ.get("OCR_PADDLE_MODELS", "")) / "manifest.json"
            ).is_file()
        )

    def __enter__(self):
        if not self.python.is_file():
            raise RuntimeError("Local second OCR runtime is not configured")
        verify_models(self.models)
        env = {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "OMP_NUM_THREADS": "4",
        }
        self.budget = ProcessGuard(MAX_WORKER_RSS)
        self.process = self.budget.start(
            [
                str(self.python),
                "-X",
                "utf8",
                "-u",
                str(Path(__file__).with_name("paddle_worker.py")),
                str(self.models.resolve()),
            ],
            stdin=subprocess.PIPE,
            env=env,
        )
        threading.Thread(
            target=self._read, daemon=True, name="local-ocr-reader"
        ).start()
        try:
            message = self._receive(180)
            if message != {"ready": True}:
                raise RuntimeError("Local second OCR initialization failed")
        except Exception:
            self.close()
            raise
        return self

    def _read(self):
        process = self.process
        try:
            while process and process.stdout:
                line = process.stdout.readline(4 * 1024 * 1024 + 1)
                if not line or len(line) > 4 * 1024 * 1024:
                    break
                if not line.startswith(b'{"ocr_response":'):
                    continue
                message = json.loads(line)["ocr_response"]
                self.messages.put(message, timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                self.messages.put_nowait({"error": "worker_stopped"})
            except queue.Full:
                pass

    def _receive(self, seconds: int):
        deadline = time.monotonic() + seconds
        while True:
            if self.budget:
                self.budget.check()
            if time.monotonic() >= deadline:
                self.failure = "second_engine_timeout"
                self.close()
                raise RuntimeError("Local second OCR timed out")
            try:
                response = self.messages.get(
                    timeout=max(0.01, min(0.5, deadline - time.monotonic()))
                )
                if self.budget:
                    self.budget.check()
                break
            except queue.Empty:
                continue
        if not isinstance(response, dict):
            self.failure = "invalid_second_engine_response"
            raise RuntimeError("Invalid local OCR response")  # noqa: TRY004 - worker protocol failure.
        if response.get("error"):
            self.failure = "second_engine_failed"
            category = response.get("category")
            if category in {
                "MemoryError",
                "OSError",
                "RuntimeError",
                "ValueError",
                "TypeError",
                "KeyError",
                "WorkerError",
            }:
                logging.getLogger(__name__).warning(
                    "Local OCR worker failed (%s)", category
                )
            raise RuntimeError("Local second OCR failed")
        return response

    def recognize(self, png: bytes, *, detect: bool = True) -> list[dict]:
        if (
            len(png) > MAX_IMAGE_BYTES
            or png[:8] != b"\x89PNG\r\n\x1a\n"
            or len(png) < 24
        ):
            raise ValueError("Invalid OCR image")
        width, height = struct.unpack(">II", png[16:24])
        if not width or not height or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("OCR image exceeds its pixel budget")
        with self.lock:
            if not self.process or self.process.poll() is not None or self.failure:
                raise RuntimeError("Local second OCR is unavailable")
            payload = (
                json.dumps(
                    {
                        "image": base64.b64encode(png).decode("ascii"),
                        "detect": bool(detect),
                    },
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            try:
                completed, failures = threading.Event(), []
                stream = self.process.stdin

                def write():
                    try:
                        stream.write(payload)
                        stream.flush()
                    except (OSError, ValueError) as exc:
                        failures.append(exc)
                    finally:
                        completed.set()

                writer = threading.Thread(
                    target=write, daemon=True, name="local-ocr-writer"
                )
                writer.start()
                if not completed.wait(30):
                    self.failure = "second_engine_pipe_timeout"
                    self.close()
                    writer.join(timeout=2)
                    raise RuntimeError("Local second OCR input timed out")
                if failures:
                    raise OSError("Local second OCR input failed") from failures[0]
                result = self._receive(600 if detect else 120)
            except (OSError, RuntimeError):
                self.close()
                raise
            lines = result.get("lines")
            if not isinstance(lines, list) or len(lines) > 4096:
                raise RuntimeError("Invalid local OCR response")
            for line in lines:
                if (
                    not isinstance(line, dict)
                    or not isinstance(line.get("text"), str)
                    or len(line["text"]) > 8192
                    or not isinstance(line.get("polygon"), list)
                    or len(line["polygon"]) != 4
                    or not isinstance(line.get("confidence"), (int, float))
                    or not math.isfinite(line["confidence"])
                ):
                    raise RuntimeError("Invalid local OCR line")
                if any(
                    not isinstance(p, list)
                    or len(p) != 2
                    or any(
                        not isinstance(n, (int, float)) or not math.isfinite(n)
                        for n in p
                    )
                    for p in line["polygon"]
                ):
                    raise RuntimeError("Invalid local OCR coordinates")
            return lines

    def close(self):
        process, self.process = self.process, None
        budget, self.budget = self.budget, None
        if budget:
            budget.close()
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def __exit__(self, *_):
        self.close()
