import unittest

import cv2
import numpy as np

from ocr_pipeline import engines as E
from ocr_pipeline import preprocess as P
from ocr_pipeline.precision import source_box


class GlyphSafetyTests(unittest.TestCase):
    def test_dots_decimal_hyphen_slash_survive_at_multiple_scales(self):
        for scale in (1, 2, 4):
            image = np.full((80 * scale, 150 * scale), 255, np.uint8)
            for x, y, w, h in (
                (10, 10, 1, 1),
                (20, 20, 2, 2),
                (30, 30, 5, 1),
                (40, 40, 1, 6),
            ):
                image[y * scale : (y + h) * scale, x * scale : (x + w) * scale] = 0
            actual = P.denoise_preserve_dots(image)
            np.testing.assert_array_equal(actual, image)
            self.assertFalse(np.shares_memory(actual, image))
        small = np.full((20, 20), 255, np.uint8)
        small[5:7, 5:7] = 0
        self.assertEqual(int((P.denoise_preserve_dots(small, 6) < 128).sum()), 0)
        self.assertEqual(int((P.denoise_preserve_dots(small) < 128).sum()), 4)

    def test_semantic_letters_survive_display(self):
        self.assertEqual(E.display_text("ؤ أ إ ة ۀ"), "ؤ أ إ ة ۀ")
        self.assertEqual(
            E.en_digits_only(E.display_text("INV-001/02 0.50")), "INV-001/02 0.50"
        )

    def test_inverse_geometry_contains_original_corners(self):
        page = {
            "width": 500,
            "height": 400,
            "analysis": {"skew_angle": 3},
            "preprocess_ops": ["deskew(3)"],
        }
        original = np.float64([[150, 160], [250, 160], [250, 190], [150, 190]])
        matrix = cv2.getRotationMatrix2D((250, 200), 3, 1)
        rotated = np.c_[original, np.ones(4)] @ matrix.T
        box = [*rotated.min(axis=0), *rotated.max(axis=0)]
        back = source_box(box, page)
        self.assertLessEqual(back[0], 150)
        self.assertLessEqual(back[1], 160)
        self.assertGreaterEqual(back[2], 250)
        self.assertGreaterEqual(back[3], 190)
