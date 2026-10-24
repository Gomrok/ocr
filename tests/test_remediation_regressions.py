import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

import numpy as np
from test_review import document

from ocr_pipeline import benchmark as B
from ocr_pipeline import precision
from ocr_pipeline import preprocess as P
from ocr_pipeline import review as R
from ocr_pipeline import tables as T
from ocr_pipeline import validators as V
from ocr_pipeline.contracts import build_contract
from ocr_pipeline.observations import carry_observations


class FinalRegressionTests(unittest.TestCase):
    def test_decimal_does_not_round_large_values_into_false_acceptance(self):
        kwargs = {
            "discount": D(0),
            "charges": D(0),
            "tax": D(0),
            "tolerance": D(0),
            "currency": "synthetic",
        }
        self.assertEqual(
            V.row_total(
                D(12345678901234567890123456789),
                D(1),
                D(12345678901234567890123456790),
                **kwargs,
            )["result"],
            "invalid",
        )
        self.assertEqual(
            V.row_total(D("1E9999"), D(1), D("1E9999"), **kwargs)["result"],
            "not_computed",
        )

    def test_unlike_units_and_no_shared_fields_are_not_consistent(self):
        fields = [
            {
                "case_id": "test",
                "document_id": "a",
                "field_id": "net",
                "canonical_value": "1",
                "unit": "kg",
            },
            {
                "case_id": "test",
                "document_id": "b",
                "field_id": "net",
                "canonical_value": "2",
                "unit": "lb",
            },
        ]
        self.assertEqual(
            V.cross_document(fields, authorized_case_id="test")["result"],
            "unverified_context",
        )
        fields[1]["field_id"] = "gross"
        self.assertEqual(
            V.cross_document(fields, authorized_case_id="test")["result"],
            "not_computed",
        )

    def test_transformation_and_uncertainty_survive_agreement(self):
        previous = {
            "text": "1234",
            "needs_review": True,
            "uncertainty_reasons": ["prior_disagreement"],
            "transformations": [
                {"stage": "first", "before": "1284", "selected": "1234"}
            ],
        }
        merged = carry_observations({"text": "1234"}, previous, stage="next")
        self.assertEqual(merged["transformations"], previous["transformations"])
        readings = [
            dict(previous, engine="tesseract", confidence=0.99),
            {"text": "1234", "engine": "paddleocr", "confidence": 0.99},
        ]
        final = precision.reconcile(readings)
        self.assertTrue(final["needs_review"])
        self.assertIn("prior_disagreement", final["uncertainty_reasons"])

    def test_selected_precision_units_supply_fields_not_original_words(self):
        page = {
            "page": 1,
            "width": 500,
            "height": 100,
            "words": [{"text": "Container: OLD1234", "bbox": [0, 0, 100, 20]}],
            "final_text_source": "review_units",
            "review_units": [
                {
                    "kind": "line",
                    "text": "Container: FINAL1234",
                    "bbox": [0, 0, 200, 20],
                    "needs_review": True,
                    "candidates": [{"text": "OLD1234"}],
                }
            ],
        }
        result = build_contract(
            "test",
            [page],
            {"source_digest": "a" * 64, "model_hashes": {"fas": "b" * 64}},
        )
        self.assertEqual(
            result["line_observations"][0]["logical_text"], "Container: FINAL1234"
        )
        self.assertEqual(result["line_observations"][0]["review_status"], "uncertain")
        self.assertEqual(
            result["observations"][0]["logical_text"], "Container: OLD1234"
        )

    def test_versioned_field_mapping_and_missing_table_are_explicit(self):
        field = {
            "document_id": "test",
            "context": "test",
            "page": 1,
            "source_bbox": [0, 0, 100, 20],
            "field_id": "net",
            "canonical_value": "001",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "case"
            case.mkdir()
            (case / "case.json").write_text(
                json.dumps(
                    {"pages": [{"text": "synthetic"}], "schema_v2": {"fields": [field]}}
                )
            )
            result = B.evaluate_outputs(
                {"case": {"text": "synthetic", "fields": [field]}}, root
            )
            self.assertEqual(result["cases"][0]["fields"]["exact_match"], 1)
            result = B.evaluate_outputs(
                {"case": {"text": "synthetic", "table_rows": [["1"]]}}, root
            )
            self.assertNotEqual(result["status"], "MEASURED")

    def test_review_export_manifest_matches_actual_degraded_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "a"
            out.mkdir()
            base = document()
            base["exports"] = {"xlsx": {"status": "available"}}
            base["pages"][0]["exports"] = {"xlsx": {"status": "available"}}
            base["schema_v2"] = {"schema_version": 2, "fields": []}
            R.create_document(out, base)
            unit = R.read_document(out)["pages"][0]["units"][0]
            with patch.object(
                T, "to_xlsx", side_effect=ImportError("synthetic missing exporter")
            ):
                result = R.save_document(
                    out,
                    {
                        "revision": 0,
                        "edits": [
                            {"id": unit["id"], "text": "synthetic", "confirmed": False}
                        ],
                    },
                )
            self.assertEqual(result["exports"]["xlsx"]["status"], "unavailable")
            version = next((out / "reviews").glob("v000001-*"))
            revised = json.loads((version / "a.json").read_text("utf-8"))
            self.assertEqual(
                revised["pages"][0]["exports"]["xlsx"]["status"], "unavailable"
            )
            self.assertEqual(revised["schema_v2"]["status"], "stale_after_human_review")
            self.assertFalse(list(version.rglob("*.xlsx")))

    def test_deskew_refuses_clipping_source_ink(self):
        image = np.full((100, 100), 255, np.uint8)
        image[0:5, 0:5] = 0
        result, applied = P.deskew_safe(image, 5)
        self.assertFalse(applied)
        np.testing.assert_array_equal(result, image)
