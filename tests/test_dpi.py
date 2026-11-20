import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pymupdf

from ocr_pipeline import pipeline as pipeline
from ocr_pipeline.metrics import Metrics
from ocr_pipeline.render import render_master


class ResolutionTests(unittest.TestCase):
    def test_six_resolutions_render_real_pixels_and_preserve_analysis_detail(self):
        with pymupdf.open() as document, tempfile.TemporaryDirectory() as directory:
            page = document.new_page(width=72, height=72)
            page.insert_text((8, 32), "OCR 2026", fontsize=9)
            for dpi in (100, 250, 500, 1000, 1500, 2000):
                with self.subTest(dpi=dpi):
                    master = render_master(page, 1, Path(directory), dpi=dpi)
                    actual = master.read_full()
                    self.assertEqual(actual.shape, (dpi, dpi))
                    metrics = Metrics()
                    try:
                        with patch.object(pipeline.L, "analyze_layout", return_value=[]) as layout:
                            reading = {"text":"آزمایش","engine":"test_engine","words":[
                                {"text":"آزمایش","bbox":[35,35,65,55],"confidence":.99}]}
                            with patch.object(pipeline.E, "ocr_many", return_value=[reading.copy() for _ in range(4)]) as recognize:
                                result = pipeline.ocr_page(master, [SimpleNamespace(name="test_engine")], metrics)
                            recognize.assert_called()
                            self.assertTrue(result["has_text"])
                        target = min(dpi, 500)
                        self.assertEqual(layout.call_args.args[0].shape, (target, target))
                    finally:
                        metrics.stop()
                    self.assertEqual(result["requested_dpi"], dpi)
                    self.assertEqual(result["effective_dpi"], dpi)
                    self.assertEqual((result["width"], result["height"]), (dpi, dpi))

    def test_low_resolution_table_candidates_are_not_fixed_to_2000_dpi(self):
        image = np.full((100, 1600), 255, np.uint8)
        metrics = Metrics()
        original_resize = pipeline.P.resize_by_factor
        try:
            for dpi, expected in ((2000, [0.22, 0.10, 0.17]), (500, [0.88, 0.40, 0.68])):
                with self.subTest(dpi=dpi), \
                        patch.object(pipeline, "_ink_tight_crop", return_value=image), \
                        patch.object(pipeline.P, "resize_by_factor", wraps=original_resize) as resize, \
                        patch.object(pipeline.E, "ocr_many", return_value=[]):
                    pipeline._cell_ocr_best([], image, metrics, dpi=dpi)
                    self.assertEqual([call.args[1] for call in resize.call_args_list], expected)
        finally:
            metrics.stop()

    def test_small_low_dpi_digits_are_not_discarded_as_high_dpi_noise(self):
        image = np.full((20, 40), 255, np.uint8)
        cv2.rectangle(image, (12, 6), (16, 13), 0, -1)  # 40px of real low-DPI ink.
        metrics = Metrics()
        try:
            with patch.object(pipeline.E, "ocr_many", return_value=[{"text": "۱", "confidence": .9}] * 2) as ocr:
                low = pipeline._glyph_digit_ocr([], image, metrics, dpi=100)
                self.assertEqual(low["text"], "۱")
                self.assertEqual(len(ocr.call_args.args[1]), 2)
            with patch.object(pipeline.E, "ocr_many") as ocr:
                high = pipeline._glyph_digit_ocr([], image, metrics, dpi=2000)
                self.assertEqual(high["text"], "")
                ocr.assert_not_called()
        finally:
            metrics.stop()


if __name__ == "__main__":
    unittest.main()
