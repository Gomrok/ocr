import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pymupdf

from ocr_pipeline import pipeline as P
from ocr_pipeline import render as R
from ocr_pipeline.pdf_text import inspect_page


class PDFTextTests(unittest.TestCase):
    def make_doc(self):
        doc = pymupdf.open()
        page = doc.new_page(width=300, height=200)
        page.insert_text((20, 40), "Document Number: INV-001/02", fontsize=12)
        page.insert_text((20, 65), "Net Weight: 12.50", fontsize=12)
        return doc

    def test_visible_latin_text_candidate_and_invisible_or_mixed_fallback(self):
        with self.make_doc() as doc:
            self.assertTrue(inspect_page(doc[0])["accepted"])
            doc[0].insert_text((20, 90), "HIDDEN VALUE 999999", render_mode=3)
            self.assertFalse(inspect_page(doc[0])["accepted"])
        with self.make_doc() as doc:
            doc[0].set_rotation(90)
            self.assertFalse(inspect_page(doc[0])["accepted"])
        with self.make_doc() as doc:
            pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 10, 10), False)
            pix.clear_with(255)
            doc[0].insert_image(
                pymupdf.Rect(10, 100, 30, 120), stream=pix.tobytes("png")
            )
            assessment = inspect_page(doc[0])
            self.assertEqual(assessment["classification"], "mixed")
            self.assertFalse(assessment["accepted"])

    def test_chunked_render_matches_single_including_tile_seams(self):
        with self.make_doc() as doc, tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(R, "RENDER_TILE_EDGE", 10000):
                direct = R.render_master(doc[0], 1, path, dpi=144).read_full()
            with patch.object(R, "RENDER_TILE_EDGE", 128):
                tiled = R.render_master(doc[0], 2, path, dpi=144)
            self.assertEqual(tiled.render_method, "tiled")
            np.testing.assert_array_equal(tiled.read_full(), direct)

    def test_opt_in_pipeline_schema_fields_and_default_raster_contract(self):
        with self.make_doc() as doc, tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "synthetic.pdf"
            doc.save(source)
            with (
                patch.dict(
                    os.environ,
                    {
                        "OCR_PDF_TEXT_FIRST": "1",
                        "OCR_SCHEMA_V2": "1",
                        "OCR_DOCUMENT_SCHEMA": "common_labeled_v1",
                    },
                ),
                patch.object(
                    P.R,
                    "render_master",
                    side_effect=AssertionError(
                        "native candidate should avoid full raster"
                    ),
                ),
            ):
                result = P.process_pdf(source, path / "native", dpi=144, debug=False)
            self.assertEqual(result["performance"]["failed_pages"], 0)
            self.assertEqual(result["pages"][0]["engines"], ["native_pdf"])
            fields = {f["field_id"]: f for f in result["schema_v2"]["fields"]}
            self.assertEqual(fields["document_number"]["canonical_value"], "INV-001/02")
            self.assertEqual(fields["net_weight"]["review_status"], "uncertain")
            self.assertTrue((path / "native" / "synthetic.txt").is_file())
            self.assertFalse(result["schema_v2"]["commercial_registration_allowed"])
            with (
                patch.dict(
                    os.environ, {"OCR_PDF_TEXT_FIRST": "0", "OCR_SCHEMA_V2": "0"}
                ),
                patch.object(
                    P,
                    "ocr_page",
                    return_value={
                        "page": 1,
                        "text": "synthetic",
                        "tables": [],
                        "metrics": {},
                        "words": [],
                    },
                ) as ocr,
            ):
                legacy = P.process_pdf(source, path / "legacy", dpi=72, debug=False)
            ocr.assert_called_once()
            self.assertNotIn("schema_v2", legacy)

    def test_pure_scan_keeps_full_image_resampling_phase(self):
        import cv2

        image = np.full((120, 180), 255, np.uint8)
        cv2.putText(image, "INV-001/02", (5, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.65, 0, 1)
        ok, png = cv2.imencode(".png", image)
        self.assertTrue(ok)
        with pymupdf.open() as doc, tempfile.TemporaryDirectory() as directory:
            page = doc.new_page(width=180, height=120)
            page.insert_image(page.rect, stream=png.tobytes())
            pix = page.get_pixmap(dpi=144, colorspace=pymupdf.csGRAY, alpha=False)
            direct = (
                np.frombuffer(pix.samples_mv, np.uint8)
                .reshape(pix.height, pix.width)
                .copy()
            )
            with patch.object(R, "RENDER_TILE_EDGE", 128):
                master = R.render_master(page, 1, Path(directory), dpi=144)
            self.assertEqual(master.render_method, "single_scan")
            np.testing.assert_array_equal(master.read_full(), direct)

    def test_pure_scan_white_surface_uses_tiled_fallback(self):
        import cv2

        image = np.full((120, 180), 255, np.uint8)
        image[30:60, 40:90] = 0
        _, png = cv2.imencode(".png", image)
        with pymupdf.open() as doc, tempfile.TemporaryDirectory() as directory:
            page = doc.new_page(width=180, height=120)
            page.insert_image(page.rect, stream=png.tobytes())
            original = page.get_pixmap

            def render(**kwargs):
                pix = original(**kwargs)
                if "dpi" in kwargs and "clip" not in kwargs:
                    pix.clear_with(255)
                return pix

            with (
                patch.object(page, "get_pixmap", side_effect=render),
                patch.object(R, "RENDER_TILE_EDGE", 128),
            ):
                master = R.render_master(page, 1, Path(directory), dpi=144)
            self.assertEqual(master.render_method, "tiled_fallback")
            self.assertLess(int(master.read_full().min()), 255)

    def test_scan_with_wide_blank_margins_keeps_same_policy(self):
        import cv2

        image = np.full((80, 160), 255, np.uint8)
        image[20:30, 40:90] = 0
        _, png = cv2.imencode(".png", image)
        with pymupdf.open() as doc, tempfile.TemporaryDirectory() as directory:
            page = doc.new_page(width=300, height=500)
            page.insert_image(pymupdf.Rect(0, 175, 300, 325), stream=png.tobytes())
            self.assertTrue(R._is_pure_scan(page))
            with patch.object(R, "RENDER_TILE_EDGE", 128):
                master = R.render_master(page, 1, Path(directory), dpi=144)
            self.assertEqual(master.render_method, "single_scan")
