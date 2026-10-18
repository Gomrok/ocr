import tempfile
import threading
import unittest
import random
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import cv2
import pymupdf

from ocr_pipeline import engines as E
from ocr_pipeline import pipeline as P
from ocr_pipeline import preprocess as PRE
from ocr_pipeline.layout import Region
from ocr_pipeline.metrics import Metrics
from ocr_pipeline.render import render_master


class PerformanceTests(unittest.TestCase):
    @staticmethod
    def execute(command, **_kwargs):
        Path(command[2] + ".txt").write_text("متن آزمایشی", encoding="utf-8")
        Path(command[2] + ".hocr").write_text(
            "<span class='ocrx_word' title='bbox 10 20 80 40; x_wconf 95'>متن</span>",
            encoding="utf-8")
        return Mock(returncode=0)

    def test_document_cache_returns_independent_copies(self):
        engine = E.TesseractEngine()
        engine.executable = "trusted-tesseract.exe"
        with patch.object(engine, "native_ok", return_value=True), \
                patch.object(E.subprocess, "run", side_effect=self.execute) as command:
            first = engine.ocr_image(b"image")
            first["words"][0]["bbox"] = (0, 0, 1, 1)
            first["extra_passes"] = 99
            second = engine.ocr_image(b"image")
            self.assertEqual(command.call_count, 1)
        self.assertEqual(second["words"][0]["bbox"], (10, 20, 80, 40))
        self.assertNotIn("extra_passes", second)
        self.assertEqual(engine.statistics(), {"ocr_processes": 1, "ocr_cache_hits": 1})

    def test_cache_parameters_and_documents_are_isolated(self):
        engines = [E.TesseractEngine(), E.TesseractEngine()]
        with patch.object(E.TesseractEngine, "native_ok", return_value=True), \
                patch.object(E.subprocess, "run", side_effect=self.execute) as command:
            for engine in engines:
                engine.executable = "trusted-tesseract.exe"
                for kwargs in ({}, {"psm": 7}, {"whitelist": "123"}, {"langs": "eng"}):
                    engine.ocr_image(b"image", **kwargs)
            self.assertEqual(command.call_count, 8)

    def test_cache_has_entry_and_byte_limits(self):
        engine = E.TesseractEngine()
        engine.executable = "trusted-tesseract.exe"
        with patch.object(engine, "native_ok", return_value=True), \
                patch.object(E.subprocess, "run", side_effect=self.execute):
            for index in range(130):
                engine.ocr_image(str(index).encode())
        self.assertEqual(len(engine._cache), 128)
        self.assertLessEqual(engine._cache_bytes, 4 * 1024 * 1024)

        def large_result(command, **_kwargs):
            Path(command[2] + ".txt").write_text("x" * 200000, encoding="utf-8")
            return Mock(returncode=0)

        with patch.object(engine, "native_ok", return_value=True), \
                patch.object(E.subprocess, "run", side_effect=large_result):
            for index in range(24):
                engine.ocr_image(f"large-{index}".encode())
        self.assertLessEqual(engine._cache_bytes, 4 * 1024 * 1024)
        self.assertLess(len(engine._cache), 24)

    def test_parallel_candidates_preserve_order_and_four_worker_limit(self):
        engine = E.TesseractEngine()
        barrier = threading.Barrier(4, timeout=5)
        lock = threading.Lock()
        active = peak = 0

        def recognize(_engines, image, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
            return {"text": image.decode()}

        with patch.object(engine, "native_ok", return_value=True), \
                patch.object(E.os, "cpu_count", return_value=24), \
                patch.object(E, "ocr_any", side_effect=recognize):
            results = E.ocr_many([engine], [str(index).encode() for index in range(12)])
        self.assertEqual([result["text"] for result in results], [str(i) for i in range(12)])
        self.assertEqual(peak, 4)

    def test_cloud_docker_and_small_cpu_remain_sequential(self):
        engine = E.TesseractEngine()
        owner_thread = threading.get_ident()

        def recognize(*_args, **_kwargs):
            self.assertEqual(threading.get_ident(), owner_thread)
            return {"text": "متن"}

        with patch.object(E, "ocr_any", side_effect=recognize):
            with patch.object(engine, "native_ok", return_value=False):
                E.ocr_many([engine], [b"first", b"second"])
            E.ocr_many([E.GoogleVisionEngine("not-a-real-key"), engine], [b"first", b"second"])
            with patch.object(engine, "native_ok", return_value=True), \
                    patch.object(E.os, "cpu_count", return_value=2):
                E.ocr_many([engine], [b"first", b"second"])

    def test_failed_candidate_keeps_its_position(self):
        engine = E.TesseractEngine()

        def recognize(_engines, image, **_kwargs):
            if image == b"bad":
                raise RuntimeError("OCR unavailable")
            return {"text": image.decode()}

        with patch.object(engine, "native_ok", return_value=True), \
                patch.object(E, "ocr_any", side_effect=recognize):
            results = E.ocr_many([engine], [b"first", b"bad", b"last"])
        self.assertEqual(results, [{"text": "first"}, None, {"text": "last"}])

    def test_parallel_candidates_preserve_the_numeric_whitelist(self):
        engine = E.TesseractEngine()
        with patch.object(engine, "native_ok", return_value=True), \
                patch.object(E, "ocr_any", return_value={"text": "۱"}) as recognize:
            E.ocr_many([engine], [b"first", b"second"], region_kind="glyph", whitelist="۰۱۲۳")
        self.assertEqual(recognize.call_count, 2)
        for call in recognize.call_args_list:
            self.assertEqual(call.kwargs["whitelist"], "۰۱۲۳")
            self.assertEqual(call.kwargs["region_kind"], "glyph")

    def test_table_preview_is_shrunk_before_bgr_allocation(self):
        master = Mock(path=Path("test-master.tiff"))
        original = np.full((3200, 1200), 255, np.uint8)
        convert = P.cv2.cvtColor

        def check_size(image, conversion):
            self.assertLessEqual(max(image.shape), 2400)
            return convert(image, conversion)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(P.cv2, "imread", return_value=original), \
                patch.object(P.cv2, "cvtColor", side_effect=check_size), \
                patch.object(P.cv2, "imwrite") as write:
            P._debug_table_image(master, [Region("text", (100, 200, 500, 600))],
                                 [], Path(directory), 1)
        self.assertEqual(write.call_args.args[1].shape, (2400, 900, 3))

    def test_debug_reuses_existing_master_pixels(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(P.cv2, "imread") as read, patch.object(P.cv2, "imwrite"):
            P._debug_table_image(Mock(), [], [], Path(directory), 1,
                                 source=np.full((30, 50), 255, np.uint8))
        read.assert_not_called()

    def test_master_view_preserves_lossless_pixels_and_dpi(self):
        with pymupdf.open() as document, tempfile.TemporaryDirectory() as directory:
            page = document.new_page(width=36, height=36)
            page.insert_text((2, 18), "123", fontsize=8)
            expected = page.get_pixmap(dpi=2000, colorspace=pymupdf.csGRAY, alpha=False)
            master = render_master(page, 1, Path(directory))
            actual = cv2.imread(str(master.path), cv2.IMREAD_GRAYSCALE)
            reference = np.frombuffer(expected.samples, np.uint8).reshape(expected.height, expected.width)
            np.testing.assert_array_equal(actual, reference)
            self.assertEqual(master.effective_dpi, 2000)

    def test_iou_fast_rejection_is_equivalent_to_previous_geometry(self):
        def reference(a, b):
            ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
            iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
            intersection = ix * iy
            if intersection <= 0:
                return 0.0
            return intersection / ((a[2] - a[0]) * (a[3] - a[1]) +
                                   (b[2] - b[0]) * (b[3] - b[1]) - intersection)

        rng = random.Random(426)
        for _ in range(1000):
            boxes = []
            for _ in range(2):
                x, y = rng.randrange(-100, 200), rng.randrange(-100, 200)
                boxes.append((x, y, x + rng.randrange(1, 100), y + rng.randrange(1, 100)))
            self.assertEqual(E._iou(*boxes), reference(*boxes))

    def test_parallel_background_preserves_every_pixel_and_stripe_boundary(self):
        rng = np.random.default_rng(42)
        image = rng.integers(0, 256, (263, 317), dtype=np.uint8)
        reference = cv2.divide(image, cv2.medianBlur(image, 31), scale=255)
        pool = PRE.ThreadPoolExecutor
        with patch.object(PRE, "_PARALLEL_MEDIAN_PIXELS", 0), \
                patch.object(PRE.os, "cpu_count", return_value=24), \
                patch.object(PRE, "ThreadPoolExecutor", wraps=pool) as executor:
            actual = PRE.illumination_correct(image)
        self.assertEqual(executor.call_args.kwargs["max_workers"], 4)
        np.testing.assert_array_equal(actual, reference)

    def test_small_background_and_small_cpu_do_not_start_workers(self):
        image = np.full((160, 190), 200, np.uint8)
        image[40:90, 30:80] = 20
        reference = cv2.divide(image, cv2.medianBlur(image, 31), scale=255)
        with patch.object(PRE, "ThreadPoolExecutor") as executor:
            np.testing.assert_array_equal(PRE.illumination_correct(image), reference)
            with patch.object(PRE, "_PARALLEL_MEDIAN_PIXELS", 0), \
                    patch.object(PRE.os, "cpu_count", return_value=2):
                np.testing.assert_array_equal(PRE.illumination_correct(image), reference)
        executor.assert_not_called()

    def test_vectorized_denoising_matches_original_algorithm(self):
        rng = np.random.default_rng(13)
        image = np.full((160, 190), 230, np.uint8)
        image[rng.random(image.shape) < 0.035] = 10
        image[60:70, 30:45] = 30
        original = image.copy()
        bw = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        count, labels, stats, _ = cv2.connectedComponentsWithStats(bw)
        for limit in (0, 6, 40):
            reference = image.copy()
            for index in range(1, count):
                if stats[index, cv2.CC_STAT_AREA] <= limit:
                    reference[labels == index] = 255
            np.testing.assert_array_equal(PRE.denoise_preserve_dots(image, limit), reference)
        np.testing.assert_array_equal(image, original)

    def test_persian_dot_sized_components_are_preserved(self):
        image = np.full((80, 100), 255, np.uint8)
        image[10, 10] = 0
        image[35:39, 40:44] = 0
        actual = PRE.denoise_preserve_dots(image)
        self.assertEqual(actual[10, 10], 255)
        np.testing.assert_array_equal(actual[35:39, 40:44], image[35:39, 40:44])

    def test_incomplete_table_number_never_replaces_original(self):
        region = Region("table", (0, 0, 100, 100), ((0, 100), (0, 100)))
        image = np.zeros((100, 100), np.uint8)
        metrics = Metrics()
        try:
            with patch.object(P.T, "strip_grid_lines", side_effect=lambda value: value), \
                    patch.object(P, "_cell_ocr_best", return_value={"text": "۱۲۳۴۵"}), \
                    patch.object(P, "_glyph_digit_ocr", return_value={"text": "۱۲?۴۵"}), \
                    patch.object(P.T, "build_table_model") as build:
                P._ocr_table(region, image, [], metrics, 1, image.shape)
            self.assertEqual(build.call_args.args[2][(0, 0)], "۱۲۳۴۵")
            self.assertTrue(build.call_args.args[5][(0, 0)])
        finally:
            metrics.stop()


if __name__ == "__main__":
    unittest.main()
