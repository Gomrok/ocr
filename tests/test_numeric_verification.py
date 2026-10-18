import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from ocr_pipeline.engines import TesseractEngine
from ocr_pipeline.metrics import Metrics
from ocr_pipeline.pipeline import _glyph_digit_ocr, _verify_text_numbers


class NumericVerificationTests(unittest.TestCase):
    def test_confirmed_numeric_correction_preserves_original_for_review(self):
        words = [{"text": "۱۳۳۴۵", "bbox": (10, 10, 90, 40), "confidence": 0.92}]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value={"text": "۱۲۳۴۵"}):
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(words[0]["text"], "۱۲۳۴۵")
        self.assertEqual(words[0]["raw_ocr_text"], "۱۳۳۴۵")
        self.assertTrue(words[0]["numeric_verified"])
        self.assertTrue(words[0]["needs_review"])

    def test_ambiguous_candidates_do_not_replace_the_source(self):
        for result in ({"text": "۱۲۳۴۵", "needs_review": True}, {"text": "۱۲۳"}, {"text": "?"}):
            words = [{"text": "۱۳۳۴۵", "bbox": (10, 10, 90, 40)}]
            with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value=result):
                _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
            self.assertEqual(words[0]["text"], "۱۳۳۴۵")
            self.assertTrue(words[0]["needs_review"])

    def test_prose_and_latin_identifiers_are_preserved(self):
        words = [{"text": text} for text in ("شرکت", "INV-2026-0042", "2026", "۱۲۳,۴۵۶")]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr") as glyph:
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
            glyph.assert_not_called()

    def test_page_verification_work_is_bounded(self):
        words = [{"text": "۱۲۳", "bbox": (10, 10, 90, 40)} for _ in range(20)]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value={"text": "۱۲۳"}) as glyph:
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(glyph.call_count, 16)
        self.assertTrue(all(word["needs_review"] for word in words[16:]))

    def test_real_isolated_persian_digits(self):
        engine = TesseractEngine("fas")
        if not engine.native_ok():
            self.skipTest("Native Tesseract is not installed")
        fixture = Path(__file__).parent / "fixtures" / "persian_digits.png"
        image = cv2.imread(str(fixture), cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(image)
        metrics = Metrics()
        try:
            result = _glyph_digit_ocr([engine], image, metrics)
        finally:
            metrics.stop()
        self.assertEqual(result["text"], "۱۲۳۴۵")
        self.assertFalse(result.get("needs_review", False))


if __name__ == "__main__":
    unittest.main()
