import unittest
from pathlib import Path
import tempfile
import json
import importlib.util

spec=importlib.util.spec_from_file_location("evaluate_extractions",Path(__file__).resolve().parents[1]/"scripts/evaluate_extractions.py")
evaluator=importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class EvaluationRunnerTests(unittest.TestCase):
    def test_legacy_storage_order_is_not_misreported_as_content_order(self):
        page={"width":200,"height":50,"words":[
            {"text":"دوم","bbox":[10,10,60,30],"line_id":"one"},
            {"text":"اول","bbox":[100,10,160,30],"line_id":"one"}]}
        self.assertEqual(evaluator.scope_text(page,[0,0,1,1]),"اول دوم")
        self.assertIsNone(evaluator.scope_text(page,[0,0,1,1],raw=True))

    def test_missing_hard_page_is_counted_as_deletion(self):
        region={"id":"a","text":"۰۰۴۶","bbox_relative":[0,0,1,1],"readability":"readable","human_verified":False}
        reference={"documents":[{"source_sha256":"a"*64,"split":"tuning","pages":[{"page":1,"regions":[region]},{"page":2,"regions":[region]}]}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"empty.json";path.write_text(json.dumps({"pages":[]}),encoding="utf-8")
            report=evaluator.evaluate(reference,[{"source_sha256":"a"*64,"before":str(path),"after":str(path)}])
        counts=report["documents"][0]["after"]["CER_CONTENT"]
        self.assertEqual((counts["N"],counts["D"]),(8,8))
        self.assertEqual(report["input_status"], "FAIL")
        self.assertFalse(report["accuracy_acceptance_allowed"])

    def test_never_accepts_a_human_label_on_empty_pages_or_regions(self):
        document = {"source_sha256":"a"*64, "split":"evaluation", "source_group":"source"}
        for pages in ([], [{"page":1,"regions":[]}]):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                evaluator.validate_reference({"documents":[dict(document, pages=pages, human_verified=True)]})

    def test_missing_all_outputs_is_a_failure_and_all_numbers_stay_in_denominator(self):
        sha = "a" * 64
        field = {"source_sha256":sha,"page":1,"field_id":"amount","bbox":[.1,.2,.3,.4],
                 "text":"۰۴٫۵","type":"amount","readability":"readable"}
        region = {"id":"body","text":"۰۴٫۵","bbox_relative":[0,0,1,1],"readability":"readable"}
        document = {"source_sha256":sha,"split":"evaluation","source_group":"independent",
                    "pages":[{"page":1,"regions":[region],"numeric_fields":[field]}]}
        report = evaluator.evaluate({"documents":[document]}, [{"source_sha256":sha}])
        value = report["documents"][0]["after"]["numeric"]
        self.assertEqual(report["input_status"], "FAIL")
        self.assertEqual(value["canonical_character_errors"]["D"], 4)
        self.assertEqual(value["missing_numeric_fields"], 1)
        field["text"] = ""
        with self.assertRaises(ValueError):
            evaluator.validate_reference({"documents":[document]})

    def test_same_number_in_wrong_place_counts_as_insertion_and_deletion(self):
        sha = "b" * 64
        field = {"source_sha256":sha, "page":1, "field_id":"identifier", "bbox":[.1,.2,.3,.4], "text":"۰۰۴۶"}
        page = {"width":100,"height":100,
                "words":[{"text":"0046","bbox":[60,20,78,40]}]}
        prediction = evaluator.spatial_numeric_fields(page, [field], sha, 1)
        result = evaluator.numeric_field_metrics([field], prediction)
        self.assertEqual(result["field_exact_match"], 0)
        self.assertEqual(result["extra_numeric_fields"], 1)
        counts = result["canonical_character_errors"]
        self.assertEqual(tuple(counts[k] for k in ("S","D","I","N")), (0,4,4,4))
        page["words"][0]["bbox"] = [10,20,29,39]
        prediction = evaluator.spatial_numeric_fields(page, [field], sha, 1)
        result = evaluator.numeric_field_metrics([field], prediction)
        self.assertEqual(result["field_exact_match"], 1)
        self.assertEqual(result["digit_style_only_fields"], 1)
        self.assertEqual(result["raw_field_exact_match"], 0)
