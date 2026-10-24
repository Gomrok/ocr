"""Runs only in the separately provisioned Paddle environment, without networking."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import sys
from pathlib import Path


def no_network(*_args, **_kwargs):
    raise OSError("Network access is disabled in the local OCR worker")


def emit(message):
    sys.stdout.write(json.dumps({"ocr_response": message}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def failure(stage, error):
    # Categories are diagnostics, never exception messages or document contents.
    allowed = {
        "MemoryError",
        "OSError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "KeyError",
    }
    category = type(error).__name__
    emit(
        {"error": stage, "category": category if category in allowed else "WorkerError"}
    )


def main():
    from process_guard import enter_worker_job

    enter_worker_job()
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    with contextlib.redirect_stdout(sys.stderr):
        import cv2
        import numpy as np
        from paddleocr import TextDetection, TextRecognition

        root = Path(sys.argv[1]).resolve(strict=True)
        detector = TextDetection(
            model_name="PP-OCRv5_server_det",
            model_dir=str(root / "PP-OCRv5_server_det"),
            # The pinned Windows server detector hits an unsupported oneDNN
            # PIR attribute. Use its tested FP32 CPU path; never change weights.
            device="cpu",
            enable_mkldnn=False,
            cpu_threads=8,
        )
        reader = TextRecognition(
            model_name="arabic_PP-OCRv5_mobile_rec",
            model_dir=str(root / "arabic_PP-OCRv5_mobile_rec"),
            device="cpu",
            enable_mkldnn=True,
            mkldnn_cache_capacity=1,
            cpu_threads=4,
        )
    emit({"ready": True})
    while True:
        request = sys.stdin.buffer.readline(34 * 1024 * 1024)
        if not request:
            return
        if len(request) >= 34 * 1024 * 1024:
            emit({"error": "request_limit"})
            return
        try:
            body = json.loads(request)
            data = base64.b64decode(body["image"], validate=True)
            if len(data) > 24 * 1024 * 1024:
                raise ValueError("image_limit")
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[0] * image.shape[1] > 20_000_000:
                raise ValueError("image_limit")
            h, w = image.shape[:2]
            with contextlib.redirect_stdout(sys.stderr):
                if body.get("detect", True):
                    result = next(
                        iter(
                            detector.predict(
                                image,
                                batch_size=1,
                                limit_side_len=1024,
                                limit_type="max",
                                thresh=0.2,
                                box_thresh=0.35,
                                unclip_ratio=1.5,
                            )
                        )
                    )
                    polygons = result["dt_polys"]
                else:
                    polygons = [[[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]]
                if len(polygons) > 4096:
                    raise ValueError("region_limit")
                crops, bounds, crop_pixels = [], [], 0
                for polygon in polygons:
                    polygon = np.asarray(polygon, dtype=np.float32)
                    if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
                        continue
                    cw = max(
                        2,
                        int(
                            max(
                                np.linalg.norm(polygon[0] - polygon[1]),
                                np.linalg.norm(polygon[2] - polygon[3]),
                            )
                        ),
                    )
                    ch = max(
                        2,
                        int(
                            max(
                                np.linalg.norm(polygon[0] - polygon[3]),
                                np.linalg.norm(polygon[1] - polygon[2]),
                            )
                        ),
                    )
                    crop_pixels += cw * ch
                    if cw > 16000 or ch > 4096 or crop_pixels > 20_000_000:
                        raise ValueError("crop_pixel_budget")
                    transform = cv2.getPerspectiveTransform(
                        polygon,
                        np.float32(
                            [[0, 0], [cw - 1, 0], [cw - 1, ch - 1], [0, ch - 1]]
                        ),
                    )
                    crop = cv2.warpPerspective(
                        image,
                        transform,
                        (cw, ch),
                        borderMode=cv2.BORDER_CONSTANT,
                        borderValue=(255,) * 3,
                    )
                    crops.append(crop)
                    bounds.append(polygon.round(2).tolist())
                readings = list(reader.predict(crops, batch_size=4)) if crops else []
            lines = []
            for polygon, result in zip(bounds, readings):
                text = str(result["rec_text"])[:8192]
                if text.strip():
                    lines.append(
                        {
                            "text": text,
                            "polygon": polygon,
                            "confidence": float(result["rec_score"]),
                        }
                    )
            emit({"lines": lines})
        except Exception as exc:  # noqa: BLE001 - native library errors terminate this request.
            # Do not return input paths, document text or model tracebacks.
            failure("recognition_failed", exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - contain native initialization failures.
        failure("initialization_failed", exc)
