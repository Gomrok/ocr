import copy
import unittest

from ocr_pipeline.benchmark import normalize_evaluation
from ocr_pipeline.contracts import build_contract


class DataContractTests(unittest.TestCase):
    def test_semantic_identity_and_raw_immutable(self):
        for value in ("INV-0012/03", "متن ۰۰۱٫۲۰ KG", "ؤ أ می\u200cرود", "-001.20"):
            page = {
                "page": 1,
                "width": 100,
                "height": 50,
                "words": [
                    {"text": value, "logical_text": value, "bbox": [1, 2, 40, 20]}
                ],
            }
            original = copy.deepcopy(page)
            item = build_contract(
                "synthetic",
                [page],
                {"source_digest": "a" * 64, "model_hashes": {"fas": "b" * 64}},
            )["observations"][0]
            self.assertEqual(item["raw_ocr_text"], value)
            self.assertEqual(item["logical_text"], value)
            self.assertEqual(item["canonical_value"], normalize_evaluation(value))
            self.assertEqual(item["source_bbox"], [1, 2, 40, 20])
            self.assertEqual(item["review_status"], "observed")
            self.assertIsNone(item["human_review_revision"])
            self.assertEqual(page, original)

    def test_missing_provenance_and_failed_page_are_explicit(self):
        page = {"page": 1, "words": [{"text": "001", "bbox": [0, 0, 10, 10]}]}
        result = build_contract(
            "synthetic", [page, {"page": 2, "error": "synthetic"}], {}
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["observations"][0]["review_status"], "uncertain")
        self.assertFalse(result["commercial_registration_allowed"])
