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
        counts=report["micro"]["tuning"]["after"]["CER_CONTENT"]
        self.assertEqual((counts["N"],counts["D"]),(8,8))
        self.assertFalse(report["accuracy_acceptance_allowed"])
