import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
import pymupdf

from ocr_pipeline import pipeline
from ocr_pipeline import precision as P
from ocr_pipeline.paddle_engine import MODELS, PaddleEngine, verify_models
from tools.evaluate_accuracy import distance, normalize


class PrecisionTests(unittest.TestCase):
    def test_model_files_must_be_complete_unchanged_and_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {}
            for model in MODELS:
                (root / model).mkdir()
                for name in ("inference.json", "inference.pdiparams", "inference.yml"):
                    p = root / model / name
                    p.write_bytes(b"fixed-test-artifact")
                    files[p.relative_to(root).as_posix()] = hashlib.sha256(
                        p.read_bytes()
                    ).hexdigest()
            manifest = {"models": list(MODELS), "files": files}
            (root / "manifest.json").write_text(json.dumps(manifest), "utf-8")
            approved = patch.dict(
                "ocr_pipeline.paddle_engine.EXPECTED_FILES", files, clear=True
            )
            approved.start()
            self.addCleanup(approved.stop)
            verify_models(root)
            (root / MODELS[0] / "inference.yml").write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                verify_models(root)
            manifest["files"]["../outside"] = "0" * 64
            (root / "manifest.json").write_text(json.dumps(manifest), "utf-8")
            with self.assertRaises(ValueError):
                verify_models(root)

    def test_invalid_images_are_rejected_before_worker_or_decoder(self):
        engine = PaddleEngine()
        with self.assertRaises(ValueError):
            engine.recognize(b"not-png")
        image = cv2.imencode(".png", np.full((2, 2), 255, np.uint8))[1].tobytes()
        with self.assertRaises(RuntimeError):
            engine.recognize(image)
        import struct

        over = image[:16] + struct.pack(">II", 100000, 100000) + image[24:]
        with self.assertRaises(ValueError):
            engine.recognize(over)

    def test_worker_failure_is_explicit_and_deadline_is_bounded(self):
        engine = PaddleEngine()
        engine.messages.put({"error": "recognition_failed"})
        with self.assertRaises(RuntimeError):
            engine._receive(1)
        self.assertEqual(engine.failure, "second_engine_failed")
        with self.assertRaises(RuntimeError):
            PaddleEngine()._receive(0)

    def test_agreement_never_hides_conflicting_variant(self):
        result = P.reconcile(
            [
                {"engine": "tesseract", "text": "0042/01"},
                {"engine": "paddleocr", "text": "0042/01"},
                {"engine": "paddleocr", "text": "0042/07"},
            ]
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(len(result["candidates"]), 3)

    def test_a_full_line_coalesces_both_overlapping_tile_fragments(self):
        def detection(x0, x1, y):
            return {
                "text": "سطر",
                "polygon": [[x0, y], [x1, y], [x1, y + 20], [x0, y + 20]],
            }

        groups = P.merge_detections(
            [detection(0, 100, 0), detection(95, 200, 0), detection(0, 200, 1)]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["readings"]), 3)

    def test_line_budget_keeps_provisional_detection_at_original_location(self):
        with pymupdf.open() as doc:
            source = doc.new_page(width=72, height=72)
            page = {
                "page": 1,
                "width": 100,
                "height": 100,
                "requested_dpi": 100,
                "text": "",
                "raw_text": "",
                "engines": ["tesseract"],
                "tables": [],
                "words": [],
            }
            paddle = Mock()
            paddle.recognize.return_value = [
                {
                    "text": "حاشیه",
                    "confidence": 0.8,
                    "polygon": [[1, 70], [90, 70], [90, 90], [1, 90]],
                }
            ]
            with patch.object(P, "MAX_LINES", 0):
                result = P.enhance_page(source, page, Mock(), paddle)
            self.assertEqual(result["text"], "حاشیه")
            self.assertTrue(result["review_units"][0]["needs_review"])
            self.assertEqual(result["coverage"]["processed_lines"], 0)

    def test_offline_worker_network_guard_rejects_calls(self):
        from ocr_pipeline.paddle_worker import no_network

        with self.assertRaises(OSError):
            no_network(("example.com", 443))

    def test_tiles_preserve_margins_footer_and_raw_ocr(self):
        with pymupdf.open() as doc:
            source = doc.new_page(width=72, height=72)
            page = {
                "page": 1,
                "width": 100,
                "height": 100,
                "requested_dpi": 100,
                "text": "اصل",
                "raw_text": "raw-unchanged",
                "words": [],
                "engines": ["tesseract"],
                "tables": [],
            }
            detection = {
                "text": "ریزمتن",
                "confidence": 0.9,
                "polygon": [[1, 90], [99, 90], [99, 98], [1, 98]],
            }
            paddle = Mock()
            paddle.recognize.return_value = [detection]
            verified = {"text": "ریزمتن", "needs_review": False, "candidates": []}
            with patch.object(P, "_local_read", return_value=verified):
                result = P.enhance_page(source, copy.deepcopy(page), Mock(), paddle)
            self.assertEqual(result["text"], "ریزمتن")
            self.assertEqual(result["raw_text"], "raw-unchanged")
            self.assertEqual(result["coverage"]["tiles_processed"], 1)
            self.assertEqual(result["review_units"][0]["source_bbox"], [1, 90, 99, 98])

    def test_uncovered_original_lines_are_retained_and_flagged(self):
        with pymupdf.open() as doc:
            source = doc.new_page(width=72, height=72)
            page = {
                "page": 1,
                "width": 100,
                "height": 100,
                "requested_dpi": 100,
                "text": "حاشیه",
                "raw_text": "حاشیه",
                "engines": ["tesseract"],
                "tables": [],
                "words": [{"text": "حاشیه", "bbox": [2, 2, 20, 12], "line_id": "one"}],
            }
            paddle = Mock()
            paddle.recognize.return_value = []
            result = P.enhance_page(source, page, Mock(), paddle)
            self.assertIn("حاشیه", result["text"])
            self.assertTrue(result["review_units"][0]["needs_review"])

    def test_exhausted_coverage_budget_is_not_silent(self):
        with pymupdf.open() as doc:
            source = doc.new_page(width=72, height=72)
            page = {
                "page": 1,
                "width": 100,
                "height": 100,
                "requested_dpi": 100,
                "text": "",
                "raw_text": "",
                "engines": ["tesseract"],
                "tables": [],
                "words": [],
            }
            paddle = Mock()
            paddle.recognize.return_value = []
            with patch.object(P, "MAX_TILES", 0):
                result = P.enhance_page(source, page, Mock(), paddle)
            self.assertIn("coverage_tile_budget_exceeded", result["precision_warnings"])

    def test_numeric_locations_and_budget_are_always_reported(self):
        image = np.full((70, 200), 255, np.uint8)
        cv2.putText(image, "0042", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)
        paddle = Mock()
        paddle.recognize.return_value = [
            {
                "text": "0042",
                "confidence": 0.9,
                "polygon": [[10, 10], [130, 10], [130, 40], [10, 40]],
            }
        ]
        local = Mock()
        local.ocr_image.return_value = {
            "text": "0042",
            "confidence": 0.9,
            "words": [{"text": "0042", "bbox": [15, 10, 140, 50]}],
        }
        result = P._local_read(
            image, local, paddle, "0042", numeric_budget={"remaining": 0}
        )
        self.assertTrue(result["needs_review"])
        self.assertIn(
            "numeric_budget_exceeded", [c["reason"] for c in result["numeric_checks"]]
        )

    def test_quality_scoring_does_not_hide_digit_or_space_errors(self):
        self.assertEqual(normalize("١٢٣"), normalize("۱۲۳"))
        self.assertNotEqual(normalize('ؤ'),normalize('و'))
        self.assertNotEqual(normalize("۱۲۳"), normalize("۱۲۴"))
        self.assertNotEqual(normalize("دو کلمه"), normalize("دوکلمه"))
        self.assertEqual(distance("abc", "adc"), 1)
        self.assertEqual(distance(["دو", "کلمه"], ["دوکلمه"]), 2)

    def test_independent_agreement_does_not_join_words_or_number_groups(self):
        for left, right in (
            ("دو کلمه", "دوکلمه"),
            ("۱۰ ۲۰", "۱۰۲۰"),
            ("۱۲/۰۴", "۱۲/۴"),
        ):
            with self.subTest(left=left):
                result = P.reconcile(
                    [
                        {"engine": "tesseract", "text": left},
                        {"engine": "paddleocr", "text": right},
                    ],
                    left,
                )
                self.assertTrue(result["needs_review"])
                self.assertEqual(result["text"], left)

    def test_uncovered_ink_is_flagged_without_inventing_text(self):
        image = np.full((200, 400), 255, np.uint8)
        cv2.putText(
            image, "small print", (10, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1
        )
        boxes = P.uncovered_ink(image, [])
        self.assertTrue(boxes)
        self.assertFalse(P.uncovered_ink(image, [[0, 130, 400, 190]]))

    def test_tiled_omission_check_retains_tiny_footer_without_downsampling(self):
        image = np.full((4000, 400), 255, np.uint8)
        cv2.putText(
            image, "footer 0042", (10, 3985), cv2.FONT_HERSHEY_SIMPLEX, 0.3, 0, 1
        )
        original = image.copy()
        boxes = P.uncovered_ink(image, [])
        self.assertTrue(any(box[1] > 3900 for box in boxes))
        np.testing.assert_array_equal(image, original)

    def test_no_cloud_path_in_maximum_local_even_with_key(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            fake_doc = Mock(page_count=0)
            engine = Mock(name="local")
            engine.name = "tesseract"
            engine.model_hashes = {}
            engine.statistics.return_value = {"ocr_processes": 0, "ocr_cache_hits": 0}
            with (
                patch.object(pipeline.E, "GoogleVisionEngine") as google,
                patch.object(pipeline.E, "pick_engine", return_value=engine),
            ):
                # Avoid isinstance against a Mock constructor in metadata.
                class Forbidden:
                    def __init__(self, *args):
                        raise AssertionError("Cloud engine was constructed")

                with patch.object(pipeline.E, "GoogleVisionEngine", Forbidden):
                    result = pipeline._process_document(
                        fake_doc,
                        engine,
                        target / "source.pdf",
                        target,
                        100,
                        "not-a-real-key",
                        False,
                        0,
                        False,
                        None,
                        quality_mode="maximum_local",
                    )
            self.assertTrue(result["document"]["local_only"])
            google.assert_not_called()


if __name__ == "__main__":
    unittest.main()
