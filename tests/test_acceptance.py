import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ocr_pipeline.provenance import source_digest
from tools.evaluate_accuracy import evaluate
from tools.release_gate import check_report


def evidence():
    reference = {"sources": {"sample": "a" * 64}, "pages": []}
    document = {
        "case": "sample",
        "sha256": "a" * 64,
        "document": {
            "quality_mode": "maximum_local",
            "evaluation_kind": "full_pipeline",
            "source_digest": "b" * 64,
            "model_manifest_sha256": "c" * 64,
        },
        "pages": [],
    }
    for page_number in range(1, 6):
        reference["pages"].append(
            {
                "case": "sample",
                "page": page_number,
                "unreadable": [],
                "reference_complete": True,
                "regions": [
                    {"name": "body", "bbox": [0, 0, 1, 1], "text": "متن ۰۰۴۲/۰۱"}
                ],
                "critical": ["۰۰۴۲/۰۱"],
            }
        )
        document["pages"].append(
            {
                "page": page_number,
                "width": 100,
                "height": 100,
                "review_units": [
                    {"kind": "line", "text": "متن ۰۰۴۲/۰۱", "bbox": [5, 5, 95, 25]}
                ],
            }
        )
    return reference, [document]


class AcceptanceTests(unittest.TestCase):
    def test_source_provenance_ignores_git_newlines_but_tracks_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in (
                "web_app.py",
                "serve.py",
                "requirements.txt",
                "requirements-paddle.lock",
                "deploy.ps1",
            ):
                (root / filename).write_bytes(b"one\nsecond\n")
            (root / "tessdata").mkdir()
            model = root / "tessdata" / "test.traineddata"
            model.write_bytes(b"model\r\n")
            before = source_digest(root)
            (root / "deploy.ps1").write_bytes(b"one\r\nsecond\r\n")
            self.assertEqual(source_digest(root), before)
            model.write_bytes(b"model\n")
            self.assertNotEqual(source_digest(root), before)

    def test_exact_automatic_reference_passes(self):
        report = evaluate(*evidence())
        self.assertTrue(report["passed"])
        self.assertEqual(report["cer"], 0)
        self.assertEqual(report["wer"], 0)
        self.assertEqual(report["line_coverage"], 1)

    def test_legacy_partial_or_failed_engine_cannot_pass_even_with_correct_text(self):
        for change in (
            "legacy",
            "diagnostic",
            "second_engine_failed",
            "line_review_budget_exceeded",
        ):
            reference, results = evidence()
            if change == "legacy":
                results[0]["document"]["quality_mode"] = "legacy"
            elif change == "diagnostic":
                results[0]["document"].pop("evaluation_kind")
            else:
                results[0]["pages"][0]["precision_warnings"] = [change]
            self.assertFalse(evaluate(reference, results)["passed"])

    def test_duplicate_page_or_manual_revision_is_rejected(self):
        reference, results = evidence()
        for ref, res in (
            ({**reference, "pages": [reference["pages"][0]] * 5}, results),
            (reference, results * 2),
            (reference, [{**results[0], "review": {"revision": 1}}]),
        ):
            with self.assertRaises(ValueError):
                evaluate(ref, res)

    def test_missing_leading_zero_and_duplicate_text_fail_acceptance(self):
        for text in ("متن ۰۴۲/۰۱", "متن ۰۰۴۲/۰۱\nمتن ۰۰۴۲/۰۱"):
            reference, results = evidence()
            results[0]["pages"][0]["review_units"][0]["text"] = text
            self.assertFalse(evaluate(reference, results)["passed"])

    def test_critical_identifier_cannot_be_assembled_from_separate_lines(self):
        reference, results = evidence()
        results[0]["pages"][0]["review_units"] = [
            {"kind": "line", "text": "متن ۰۰۴۲/", "bbox": [5, 5, 95, 25]},
            {"kind": "line", "text": "۰۱", "bbox": [5, 30, 95, 50]},
        ]
        report = evaluate(reference, results)
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["critical_failures"]), 1)

    def test_duplicate_table_or_cell_is_not_hidden_by_coordinate_lookup(self):
        reference, results = evidence()
        reference["pages"][0]["tables"] = [{"table_id": 1, "cells": [["۰۰۱۲"]]}]
        table = {
            "table_id": 1,
            "cells": [{"row": 0, "column": 0, "normalized_text": "۰۰۱۲"}],
        }
        results[0]["pages"][0]["tables"] = [table, table]
        self.assertFalse(evaluate(reference, results)["passed"])
        results[0]["pages"][0]["tables"] = [table]
        table["cells"] *= 2
        self.assertFalse(evaluate(reference, results)["passed"])

    def test_additional_or_reordered_table_cells_fail(self):
        reference, results = evidence()
        reference["pages"][0]["tables"] = [{"table_id": 1, "cells": [["الف", "۰۰۱۲"]]}]
        results[0]["pages"][0]["tables"] = [
            {
                "table_id": 1,
                "cells": [
                    {"row": 0, "column": 0, "normalized_text": "الف"},
                    {"row": 0, "column": 1, "normalized_text": "۰۰۱۲"},
                    {"row": 1, "column": 0, "normalized_text": "تکرار"},
                ],
            }
        ]
        self.assertFalse(evaluate(reference, results)["passed"])

    def test_release_gate_rejects_diagnostic_only_evidence(self):
        report = evaluate(*evidence())
        report["evaluation_kind"] = "diagnostic_only"
        with patch("tools.release_gate.verify_models"), self.assertRaises(ValueError):
            check_report(report, None)


if __name__ == "__main__":
    unittest.main()


class RemediationAcceptanceTests(unittest.TestCase):
    def test_absent_hash_provenance_cannot_pass(self):
        reference,results=evidence()
        results[0]['document'].pop('source_digest')
        report=evaluate(reference,results)
        self.assertFalse(report['passed'])
        self.assertFalse(report['provenance_valid'])

    def test_swapped_critical_labels_fail_even_with_near_perfect_text(self):
        reference,results=evidence()
        expected='NET 0012 GROSS 0034 '+' filler'*200
        actual='NET 0034 GROSS 0012 '+' filler'*200
        reference['pages'][0]['regions'][0]['text']=expected
        reference['pages'][0]['critical']=['0012','0034']
        results[0]['pages'][0]['review_units'][0]['text']=actual
        report=evaluate(reference,results)
        self.assertLess(report['cer'],.005)
        self.assertFalse(report['passed'])
        self.assertTrue(report['critical_failures'])
