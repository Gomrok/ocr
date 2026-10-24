import tempfile
import unittest
from pathlib import Path

from ocr_pipeline import benchmark as B


class BenchmarkContractTests(unittest.TestCase):
    def test_word_distance_and_space_sensitive_cer(self):
        self.assertEqual(B.wer("ABCD EFGH", "WXYZ EFGH"), 0.5)
        self.assertEqual(B.cer("AB CD", "ABCD"), 0.2)

    def test_normalizer_preserves_semantic_letters_and_separators(self):
        self.assertEqual(B.normalize_evaluation("ؤ أ ۰۰۱٫۲۰"), "ؤ أ 001٫20")

    def test_duplicate_order_and_extra_numbers(self):
        result = B.token_metrics(["12", "12"], ["12"])
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["recall"], 0.5)
        self.assertTrue(B.token_metrics(["12", "34"], ["34", "12"])["incorrect_order"])
        self.assertFalse(B.token_metrics(["001.20"], ["00120"])["exact_match"])
        self.assertEqual(B.token_metrics(["12"], ["12", "12"])["duplicated"], 1)

    def test_swapped_labels_or_locations_fail_field_match(self):
        base = {
            "document_id": "synthetic",
            "context": "invoice",
            "page": 1,
            "source_bbox": [0, 0, 100, 20],
        }
        truth = [
            dict(base, field_id="net", canonical_value="12"),
            dict(base, field_id="gross", canonical_value="34"),
        ]
        wrong = [
            dict(truth[0], canonical_value="34"),
            dict(truth[1], canonical_value="12"),
        ]
        self.assertEqual(B.field_accuracy(truth, wrong)["exact_match"], 0)
        self.assertEqual(B.field_accuracy(truth, truth)["exact_match"], 1)
        self.assertEqual(
            B.field_accuracy(truth, [dict(f, page=2) for f in truth])["matched"], 0
        )

    def test_ragged_extra_cell_and_empty_are_not_perfect(self):
        result = B.table_accuracy([["a"], ["b"]], [["a"], ["b", "extra"]])
        self.assertLess(result["cell_accuracy"], 1)
        self.assertEqual(result["structure_accuracy"], 0)
        self.assertIsNone(B.table_accuracy([], [])["structure_accuracy"])

    def test_missing_and_empty_output_never_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                B.evaluate_outputs({}, Path(directory))["status"], "EMPTY_DATASET"
            )
            result = B.evaluate_outputs(
                {"case": {"text": "synthetic"}}, Path(directory)
            )
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(result["cases"][0]["status"], "MISSING_OUTPUT")

    def test_numeric_sign_and_missing_row_position_are_errors(self):
        self.assertNotEqual(B.numeric_tokens("-001.20"), B.numeric_tokens("001.20"))
        result = B.table_accuracy([[], ["x"]], [["x"], []])
        self.assertEqual(result["column_accuracy"], 0)
        field = {"canonical_value": None}
        self.assertEqual(B.field_accuracy([field], [field])["matched"], 0)
