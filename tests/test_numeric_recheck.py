import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymupdf

from ocr_pipeline import numeric_recheck


def reading(text, confidence=.97):
    return {"text": text, "confidence": confidence}


class NumericRecheckTests(unittest.TestCase):
    def test_only_digit_shape_is_normalized_for_comparison(self):
        self.assertEqual(numeric_recheck.number_span("(−۰٠6۶٫۵٠٪)"), ("−0066٫50٪", 1, 10))
        self.assertIsNone(numeric_recheck.number_span("عدد۱۶"))

    def test_accepts_digit_substitution_with_same_structure(self):
        actual = numeric_recheck._replacement("−0066٫50٪", reading("−۰۰۶۴٫۵۰٪"), reading("−٠٠٦٤٫٥٠٪", .81))
        self.assertEqual(actual, "−0064٫50٪")

    def test_does_not_remove_digits_or_change_separators(self):
        for original, alternate in (("021", "۲۱"), ("14", "۴"), ("14/12", "۱۴-۱۲"),
                                    ("−0014٫50٪", "۰۰۱۴٫۵۰٪"), ("0066٫50٪", "۰۰۶۶.۵۰٪")):
            with self.subTest(original=original, alternate=alternate):
                self.assertIsNone(numeric_recheck._replacement(original, reading(alternate), reading(alternate)))

    def test_requires_two_agreeing_readings_with_explicit_punctuation(self):
        self.assertEqual(numeric_recheck._replacement("096", reading("،۹۶"), reading("،٩٦")), "،96")
        self.assertIsNone(numeric_recheck._replacement("096", reading("،۹۶"), reading("۹۶")))
        self.assertIsNone(numeric_recheck._replacement("096", reading("،۹۶"), reading("،۹۶", .88)))
        self.assertIsNone(numeric_recheck._replacement("46", reading("۴۴"), reading("۴۵")))

    def test_source_crop_and_review_metadata_follow_the_word(self):
        word = {"text": "(۱۶)", "raw_ocr_text": "١٦", "needs_review": True, "numeric_verified": True,
                "bbox": (100, 80, 200, 120)}
        other = {"text": "۰۰۲۱", "bbox": (200, 140, 270, 205)}
        read = Mock(side_effect=[reading("۱۴"), reading("١٤")])
        metrics = Mock()
        with pymupdf.open() as document:
            page = document.new_page(width=72, height=72)
            page.draw_rect(pymupdf.Rect(20, 16, 30, 20), fill=(0, 0, 0))
            with patch.object(numeric_recheck, "_reader", return_value=SimpleNamespace(read=read, model_sha256="test")):
                numeric_recheck.recheck_numbers("unused", [word, other], page, (400, 400), lambda box: box, metrics)
            crop, width = read.call_args_list[0].args
            self.assertEqual(crop.shape, (22, 52))
            self.assertEqual(crop.min(), 0)
            self.assertLess(width, numeric_recheck.FULL_WIDTH)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(metrics.inc.call_count, 2)
        self.assertEqual(word["text"], "(۱۴)")
        self.assertEqual(word["raw_ocr_text"], "١٦")
        self.assertTrue(word["needs_review"])
        self.assertNotIn("numeric_verified", word)
        self.assertEqual(word["secondary_numeric_reading"]["crop_bbox"], [49, 39, 101, 61])
        self.assertEqual(word["secondary_numeric_reading"]["original"], "(۱۶)")
        self.assertEqual(other["text"], "۰۰۲۱")
