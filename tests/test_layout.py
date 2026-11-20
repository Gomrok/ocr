import unittest

import cv2
import numpy as np

from ocr_pipeline import layout as L


class LayoutTests(unittest.TestCase):
    def test_local_rule_angle_uses_real_hough_output(self):
        outline = np.zeros((200, 520), np.uint8)
        for y in (20, 156):
            cv2.line(outline, (25, y), (493, y + 18), 255, 2)
        self.assertAlmostEqual(L.outline_angle(outline), 2.2, delta=.55)

    @staticmethod
    def table(image, xs, ys):
        for x in xs:
            cv2.line(image, (x, ys[0]), (x, ys[-1]), 0, 2)
        for y in ys:
            cv2.line(image, (xs[0], y), (xs[-1], y), 0, 2)

    def test_letterhead_and_scan_frame_do_not_expand_table(self):
        image = np.full((1400, 1000), 255, np.uint8)
        cv2.rectangle(image, (0, 0), (999, 1399), 0, 3)
        cv2.line(image, (70, 170), (930, 170), 0, 2)
        self.table(image, [120, 240, 470, 600, 710, 790, 850, 920], [720, 830, 910])
        original = image.copy()
        grids = L.detect_tables(image)
        self.assertEqual(len(grids), 1)
        ys, xs = grids[0]
        self.assertEqual(len(ys) - 1, 2)
        self.assertEqual(len(xs) - 1, 7)
        self.assertLessEqual(abs(ys[0] - 720), 2)
        self.assertLessEqual(abs(xs[0] - 120), 2)
        np.testing.assert_array_equal(image, original)

    def test_disconnected_tables_keep_separate_grids(self):
        image = np.full((1400, 1000), 255, np.uint8)
        self.table(image, [80, 200, 420], [200, 300, 400])
        self.table(image, [560, 700, 900], [850, 950, 1100])
        grids = L.detect_tables(image)
        self.assertEqual(len(grids), 2)
        self.assertTrue(all(len(ys) == len(xs) == 3 for ys, xs in grids))
        self.assertLess(grids[0][0][-1], grids[1][0][0])

    def test_unconnected_rules_are_not_a_global_table(self):
        image = np.full((1400, 1000), 255, np.uint8)
        for y in (100, 400, 700):
            cv2.line(image, (10, y), (450, y), 0, 2)
        for x in (600, 750, 900):
            cv2.line(image, (x, 850), (x, 1300), 0, 2)
        self.assertEqual(L.detect_tables(image), [])

    def test_scan_frame_no_longer_creates_a_page_sized_text_region(self):
        image = np.full((1400, 1000), 255, np.uint8)
        cv2.rectangle(image, (0, 0), (999, 1399), 0, 3)
        cv2.putText(image, 'DOCUMENT 123', (170, 340), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 0, 3)
        regions = L.detect_text_regions(image)
        self.assertTrue(regions)
        self.assertTrue(all((x1-x0)*(y1-y0) < image.size * .3 for x0,y0,x1,y1 in regions))
        self.assertTrue(any(y0 < 340 < y1 for x0,y0,x1,y1 in regions))

    def test_only_scan_edges_are_suppressed_and_input_is_unchanged(self):
        binary = np.zeros((600, 500), np.uint8)
        binary[:, :3] = 255
        binary[300:303, 50:450] = 255
        binary[200:220, 15:40] = 255  # text near, but not on, the scanner edge
        original = binary.copy()
        cleaned = L._without_scan_edges(binary)
        self.assertFalse(cleaned[:, :3].any())
        np.testing.assert_array_equal(cleaned[300:303, 50:450], original[300:303, 50:450])
        np.testing.assert_array_equal(cleaned[200:220, 15:40], original[200:220, 15:40])
        np.testing.assert_array_equal(binary, original)


if __name__ == '__main__':
    unittest.main()
