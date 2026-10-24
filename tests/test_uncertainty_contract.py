import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from ocr_pipeline import engines as E
from ocr_pipeline import pipeline as P
from ocr_pipeline import precision, review
from ocr_pipeline import tables as T
from ocr_pipeline.layout import Region
from ocr_pipeline.metrics import Metrics


class UncertaintyContractTests(unittest.TestCase):
    def test_zero_confidence_is_not_missing(self):
        self.assertEqual(T.cell_confidence([{"confidence": 0}, {"confidence": 1}]), 0.5)
        self.assertEqual(T.cell_confidence([{"confidence": 0}, {"confidence": 0}]), 0)

    def test_low_or_missing_independent_confidence_never_confirms(self):
        for confidence in (None, 0, 0.01):
            readings = [
                {"engine": engine, "text": "0012", "confidence": confidence}
                for engine in ("tesseract", "paddleocr")
            ]
            self.assertTrue(precision.reconcile(readings, "0012")["needs_review"])

    def test_high_confidence_disagreement_reaches_page_json_and_review(self):
        with tempfile.TemporaryDirectory() as directory:
            master = SimpleNamespace(
                read_full=lambda: np.full((160, 300), 255, np.uint8),
                effective_dpi=500,
                requested_dpi=500,
                width=300,
                height=160,
                pdf_w_pt=43.2,
                pdf_h_pt=23.04,
                approx_mem_mb=0.05,
                render_seconds=0,
                path=Path(directory) / "master.tiff",
            )

            def readings(_engines, images, **kwargs):
                return [
                    {
                        "text": text,
                        "engine": "synthetic",
                        "confidence": 0.99,
                        "words": [
                            {
                                "text": text,
                                "confidence": 0.99,
                                "bbox": (30, 30, 100, 60),
                            }
                        ],
                    }
                    for text in (["۱۲۳۴", "۱۲۸۴"] * len(images))[: len(images)]
                ]

            with (
                patch.object(
                    P.L,
                    "analyze_layout",
                    return_value=[Region("text", (0, 0, 300, 160))],
                ),
                patch.object(E, "ocr_many", side_effect=readings),
                patch.object(P, "_repair_suspicious_words"),
                patch.object(P, "_recover_header_numbers"),
                patch.object(P, "_verify_text_numbers"),
            ):
                page = P.ocr_page(
                    master,
                    [SimpleNamespace(name="synthetic")],
                    Metrics(),
                    export_tables=False,
                )
            self.assertTrue(page["words"][0]["needs_review"])
            self.assertIn(
                "reading_disagreement", page["words"][0]["uncertainty_reasons"]
            )
            texts = {candidate["text"] for candidate in page["words"][0]["candidates"]}
            self.assertEqual(texts, {"۱۲۳۴", "۱۲۸۴"})
            state = review.create_document(Path(directory) / "out", {"pages": [page]})
            unit = state["pages"][0]["units"][0]
            self.assertTrue(unit["needs_review"])
            self.assertFalse(unit["confirmed"])
            self.assertEqual(len(unit["candidates"]), 2)
            self.assertEqual(
                review.read_document(Path(directory) / "out")["pages"][0]["units"][0][
                    "needs_review"
                ],
                True,
            )

    def test_overlap_preserves_conflicting_observation(self):
        a = {"text": "1234", "bbox": (0, 0, 100, 30), "confidence": 0.99}
        b = dict(a, text="1284")
        result = E.merge_word_lists([[a], [b]])
        self.assertTrue(result[0]["needs_review"])
        self.assertEqual({c["text"] for c in result[0]["candidates"]}, {"1234", "1284"})
