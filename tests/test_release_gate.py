import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "release_gate", Path(__file__).resolve().parents[1] / "scripts/release_gate.py",
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ReleaseGateTests(unittest.TestCase):
    def test_compatibility_accepts_multiline_results_only_without_skipped_tests(self):
        now = datetime.now(timezone.utc)
        identity = {"commit": "a" * 40}
        manifest = {"run_id": "1" * 64, "started_at": now.isoformat(), "finished_at": now.isoformat()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            log = root / "unittest.log"
            checks = root / "checks.json"
            proof = root / "proof.json"
            gate.atomic_json(checks, {"unit_exit_code": 0, "powershell_exit_code": 0, "negative_gate_exit_code": 1,
                                     "table": {"cells_expected": 20, "candidate_cell_accuracy": .8,
                                               "baseline_cell_accuracy": .8}})
            prefix = "test_example ... ok\n\n----------------------------------------------------------------------\nRan 1 test in 0.001s\n\n"
            for summary, expected in (("OK", gate.PASS), ("OK (skipped=1)", gate.FAIL)):
                with self.subTest(summary=summary):
                    log.write_text(prefix + summary + "\n", encoding="utf-8")
                    gate.atomic_json(proof, {"identity": identity, "run_id": manifest["run_id"],
                                             "finished_at": now.isoformat(), "checks": gate.descriptor(root, checks),
                                             "unit_log": gate.descriptor(root, log)})
                    manifest["compatibility"] = gate.descriptor(root, proof)
                    self.assertEqual(gate.runtime_gates(root, manifest, identity)["G5"], expected)

    def test_preflight_rejects_a_report_for_a_different_tree_or_configuration(self):
        now = datetime.now(timezone.utc).isoformat()
        identity = {"commit":"a"*40,"tree":"b"*40,"runtime":{"settings":{"OCR_PIPELINE_MODE":"legacy"}}}
        candidate = {"schema":1,"run_id":"1"*64,"started_at":now,"finished_at":now,"identity":identity}
        for changed in (identity | {"tree":"c"*40},
                        identity | {"runtime":{"settings":{"OCR_PIPELINE_MODE":"candidate"}}}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/"bundle.json").write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "does not match"):
                    gate.preflight(root, changed)

    def test_old_evidence_cannot_release_even_with_correct_identity(self):
        identity = {"commit":"a"*40, "tree":"b"*40}
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"bundle.json").write_text(json.dumps({
                "schema":1,"run_id":"1"*64,"started_at":old,"finished_at":old,"identity":identity,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence window"):
                gate.preflight(root, identity)

    def test_all_absent_artifacts_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                gate.preflight(Path(directory), {"commit":"a"*40, "tree":"b"*40})

    def test_a_verified_label_and_zero_engine_calls_cannot_issue_an_ocr_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.pdf"
            source.write_bytes(b"generated non-private fixture")
            record = root / "receipt.json"
            prediction = root / "export.json"
            source_sha = gate.file_sha256(source)
            prediction.write_text(json.dumps({"document":{"source_sha256":source_sha},
                "pages":[{"page":1}], "performance":{"failed_pages":0,"checkpoint_hits":0,
                "total_seconds":1,"peak_tree_rss_mb":123,"ocr_processes":0,"ocr_native_calls":0}}),encoding="utf-8")
            now = datetime.now(timezone.utc)
            identity = {"commit":"a"*40}
            record.write_text(json.dumps({
                "source_sha256":source_sha,"source":gate.descriptor(root,source),
                "json":gate.descriptor(root,prediction),"identity":identity,"run_id":"1"*64,
                "started_at":now.isoformat(),"finished_at":now.isoformat(),
            }),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"no fresh complete engine execution"):
                gate.check_receipt(root, gate.descriptor(root,record), identity, "1"*64,
                                   now - timedelta(minutes=1), now + timedelta(minutes=1))
