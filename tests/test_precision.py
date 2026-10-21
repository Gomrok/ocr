import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
import pymupdf

from ocr_pipeline import benchmark, engines, pipeline
from ocr_pipeline.evaluation import (content_from_segments, edit_counts, evaluate_text_layers,
                                     line_order_metrics, micro, numeric_field_metrics,
                                     text_metrics, validate_reference)
from ocr_pipeline.precision.engine import LocalRecognizer, parse_tsv
from ocr_pipeline.precision.extraction import (choose_observation, normalize_search,
                                              process_document, process_page, reading_order, _initial_regions)
from ocr_pipeline.precision.imaging import SourcePages, Variant, blank_evidence, rotate_expanded, variants
from ocr_pipeline.precision.profiles import Options
from ocr_pipeline.review import ReviewConflict, ReviewStore


def word(text="متن", box=(20, 20, 80, 40), confidence=90, order=0):
    return {"text": text, "bbox": list(box), "confidence_raw": confidence, "line_key": [1,1,1], "order": order}


def reading(text, *, confidence=90, words=None, attempt="base"):
    return {"engine_text_raw": text, "words": words if words is not None else [word(text, confidence=confidence)],
            "attempt_id": attempt}


class FakeRecognizer:
    manifest = {"engine": "test_double_not_accuracy_evidence"}
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or {"engine_text_raw": "", "words": []}
    def recognize(self, image, **settings):
        self.calls += 1
        return {**copy.deepcopy(self.result), "tsv_raw": "", **settings}


class MetricTests(unittest.TestCase):
    def test_cer_counts_and_unbounded_insertions(self):
        self.assertEqual(edit_counts("a", "abcd"), {"S":0,"D":0,"I":3,"N":1,"errors":3,"fraction":3.0,"percent":300.0,"status":"MEASURED"})
        result = edit_counts("abc", "ax")
        self.assertEqual((result["S"],result["D"],result["I"]), (1,1,0))
        self.assertIsNone(edit_counts("", "0046")["fraction"])
        self.assertEqual(edit_counts("", "0046")["I"], 4)

    def test_wer_counts_words_not_characters(self):
        self.assertEqual(benchmark.wer("short word", "completelydifferent word"), .5)
        self.assertEqual(text_metrics("a b", "ab")["CER"]["D"], 1)

    def test_micro_weights_reference_lengths(self):
        result = micro([edit_counts("a", "b"), edit_counts("b"*9, "b"*9)])
        self.assertEqual(result["fraction"], .1)
        self.assertNotEqual(result["fraction"], .5)

    def test_real_ellipsis_and_marker_like_text_are_content(self):
        text = "... [U001] سند … ۴۶"
        parts = [{"kind":"content","text":text},{"kind":"annotation","text":"[U002]"}]
        self.assertEqual(content_from_segments(parts), text)
        result = evaluate_text_layers(text, text, text, parts)
        self.assertEqual(result["CER_CONTENT"]["errors"], 0)
        self.assertEqual(result["ANNOTATION_CHAR_COUNT"], 6)
        self.assertEqual(result["CER_EXPORT_LITERAL"]["I"], 6)

    def test_field_locations_leading_zero_units_and_silent_errors(self):
        def field(name, value, **extra):
            return {"source_sha256":"a"*64,"page":1,"field_id":name,"text":value,"bbox":[1,2,20,30],**extra}
        reference = [field("id","۰۰۴۶"),field("weight","-۴٫۵ kg"),field("date","۱۴۰۴/۰۶/۰۴")]
        observed = [field("id","۴۶"),field("weight","-۶٫۵ kg",review_required=True),field("date","۱۴۰۴/۰۶/۰۴")]
        result = numeric_field_metrics(reference, observed)
        self.assertEqual(result["numeric_field_exact_match"], 1)
        self.assertEqual(result["digit_deletions"], 2)
        self.assertEqual(result["4_to_6_substitutions"], 1)
        self.assertEqual(result["silent_numeric_errors"], 1)
        self.assertEqual(result["leading_zero_preserved"], 0)
        self.assertEqual(numeric_field_metrics([], observed)["status"], "NOT_MEASURED")
        swapped = numeric_field_metrics([field("a","۴"),field("b","۶")], [field("a","۶"),field("b","۴")])
        self.assertEqual(swapped["numeric_field_exact_match"], 0)
        self.assertEqual(swapped["6_to_4_substitutions"], 1)

    def test_line_coverage_duplicates_and_order_are_separate(self):
        result = line_order_metrics(["a","b","c"], ["b","a","a"])
        self.assertEqual((result["covered_lines"],result["duplicates"],result["order_inversions"]),(2,1,2))

    def test_document_split_and_human_reference_are_not_inferred(self):
        document = {"source_sha256":"a"*64,"split":"tuning","pages":[{"regions":[{"readability":"readable","human_verified":False}]}]}
        self.assertEqual(validate_reference({"documents":[document]})["reference_status"], "PROVISIONAL")
        with self.assertRaises(ValueError):
            validate_reference({"documents":[document, {**document,"split":"evaluation"}]})


class RecognitionTests(unittest.TestCase):
    def test_tsv_preserves_single_letters_NA_and_leading_zeros(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        rows = ["4\t1\t1\t1\t1\t0\t0\t0\t10\t10\t-1\tignored"]
        for i, (value, confidence) in enumerate((("۰۰۴۶","0"),("NA","nan"),("و","-1"),("...","90"),("INV-0046","85"))):
            rows.append(f"5\t1\t1\t1\t1\t{i+1}\t{i*20}\t0\t15\t10\t{confidence}\t{value}")
        result = parse_tsv(header+"\n".join(rows))
        self.assertEqual([w["text"] for w in result], ["۰۰۴۶","NA","و","...","INV-0046"])
        self.assertEqual(result[0]["confidence_raw"], 0)
        self.assertIsNone(result[1]["confidence_raw"])
        self.assertIsNone(result[2]["confidence_raw"])

    def test_numeric_disagreement_never_replaces_base_value(self):
        base = reading("۰۰۶۶", confidence=42)
        chosen, reasons = choose_observation([base,reading("۰۰۴۶",confidence=99,attempt="alternative")])
        self.assertIs(chosen, base)
        self.assertIn("numeric_disagreement", reasons)

    def test_higher_confidence_and_agreement_do_not_prove_accuracy(self):
        base = reading("متن کامل",confidence=50,words=[word("متن"),word("کامل",(100,20,160,40))])
        short = reading("متن",confidence=99)
        chosen, reasons = choose_observation([base,short,short])
        self.assertIs(chosen, base)
        self.assertIn("reading_disagreement",reasons)

    def test_raw_and_search_normalization_are_separate(self):
        original = "يك ۰۰۴۶\nINV-0046 ۴٫۶%"
        self.assertEqual(normalize_search(original), "یک 0046\nINV-0046 4٫6%")
        self.assertIn("۰۰۴۶", original)
        self.assertNotEqual(normalize_search("۴"), normalize_search("۶"))

    def test_base_candidate_preserves_dots_and_faint_strokes(self):
        image = np.full((100,200),255,np.uint8)
        image[20,30] = 170; image[50:52,20:90] = 245
        before = image.copy()
        candidates = list(variants(image, [0,0,200,100],Options()))
        np.testing.assert_array_equal(candidates[0].image[12:-12,12:-12],before)
        np.testing.assert_array_equal(image,before)
        self.assertFalse(blank_evidence(image)["verified_uniform_white"])
        self.assertTrue(blank_evidence(np.full((10,10),255,np.uint8))["verified_uniform_white"])

    def test_crop_rotation_and_perspective_inverse_coordinates(self):
        image = np.full((200,300),255,np.uint8)
        _, matrix = rotate_expanded(image, 5)
        variant = Variant("rotated",image,matrix,[])
        pts = np.array([[[60.,50.],[100.,50.],[100.,80.],[60.,80.]]])
        mapped = cv2.perspectiveTransform(pts,matrix)[0]
        bbox = [mapped[:,0].min(),mapped[:,1].min(),mapped[:,0].max(),mapped[:,1].max()]
        restored = variant.reference_box(bbox,image.shape)
        self.assertLessEqual(restored[0],60); self.assertGreaterEqual(restored[2],100)
        self.assertLess(abs(restored[0]-60),5)
        options=Options(perspective_corners=((10,10),(280,15),(270,190),(15,180)))
        geometry=list(variants(image,[0,0,300,200],options,full_page=True))[1]
        np.testing.assert_allclose(np.linalg.inv(geometry.reference_to_image)@geometry.reference_to_image,np.eye(3),atol=1e-8)

    def test_table_row_order_and_rtl_columns(self):
        regions=[{"kind":"cell","table_id":1,"row":r,"column":c,"bbox":[c*100,r*40,c*100+80,r*40+30],"source_order":r*2+c}
                 for r in range(2) for c in range(2)]
        self.assertEqual([(r["row"],r["column"]) for r in reading_order(regions)],[(0,1),(0,0),(1,1),(1,0)])

    def test_excessive_grid_keeps_words_and_flags_structure_without_cell_expansion(self):
        grid=(tuple(range(100)),tuple(range(100)))
        with patch("ocr_pipeline.precision.extraction.detect_tables",return_value=[grid]), \
             patch("ocr_pipeline.precision.extraction._grid_clear",side_effect=AssertionError("must bound before grid work")):
            regions,tables=_initial_regions(np.full((100,100),255,np.uint8),[word("۰۰۴۶")])
        self.assertEqual(tables[0]["reason"],"table_structure_budget_exhausted")
        self.assertEqual(regions[0]["words"][0]["text"],"۰۰۴۶")
        self.assertEqual(tables[0]["cells"],[])

    def test_region_budget_counts_baseline_and_failed_attempts(self):
        image=np.full((120,300),255,np.uint8); image[20:40,20:80]=80
        fake=FakeRecognizer(reading("۰۰۴۶",confidence=30,words=[word("۰۰۴۶",(32,32,92,52),confidence=30)]))
        with tempfile.TemporaryDirectory() as directory:
            options=Options(max_attempts=2,max_extra_calls=1,max_regions=1)
            result=process_page(image,1,fake,options,Path(directory))
        self.assertLessEqual(fake.calls,2)
        self.assertTrue(all(len(r["attempts"])<=2 for r in result["regions"]))
        self.assertTrue(all(r["search_text"]=="" for r in result["regions"]))
        self.assertTrue(any("retry_not_run_region_budget" in r["reasons"] or "retry_budget_exhausted" in r["reasons"] for r in result["regions"]))

    def test_external_engine_is_disabled_even_when_given_a_key(self):
        with patch.object(socket.socket,"connect",side_effect=AssertionError("network forbidden")):
            cloud=engines.GoogleVisionEngine("synthetic")
            self.assertFalse(cloud.available)
            with self.assertRaises(RuntimeError): cloud.ocr_image(b"image")
            with self.assertRaises(ValueError): pipeline.process_pdf(Path("missing.pdf"),Path("output"),api_key="synthetic")

    def test_expired_document_budget_keeps_base_and_reports_unread_retries(self):
        image=np.full((120,300),255,np.uint8);image[20:40,20:80]=80
        fake=FakeRecognizer(reading("۰۰۴۶",confidence=30,words=[word("۰۰۴۶",(32,32,92,52),confidence=30)]))
        with tempfile.TemporaryDirectory() as directory:
            result=process_page(image,1,fake,Options(),Path(directory),deadline=0)
        self.assertEqual(fake.calls,1)
        self.assertTrue(any("document_time_budget_exhausted" in region["reasons"] for region in result["regions"]))
        self.assertIn("۰۰۴۶",result["content_text"])

    def test_explicit_temp_and_pinned_engine_required(self):
        with self.assertRaises(ValueError):
            with patch.dict(os.environ,{},clear=True):
                pipeline.process_pdf(Path("missing.pdf"),Path("output"),mode="candidate")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); executable=root/"bad.exe"; executable.write_bytes(b"not pinned")
            with self.assertRaisesRegex(RuntimeError,"differs"):
                LocalRecognizer(Options(),root,executable=executable,data_dir=root)
        for options in ({"max_attempts":7},{"min_free_bytes":0},{"dpi":2000},
                        {"max_snapshot_bytes":128*1024*1024},{"max_document_seconds":0}):
            with self.assertRaises(ValueError): Options(**options)


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.root=Path(self.temporary.name)
        self.source=self.root/"source.pdf"
        with pymupdf.open() as pdf:
            for _ in range(2):
                page=pdf.new_page(width=120,height=100)
                page.insert_text((10,30),"BROKEN TEXT LAYER 0046",fontsize=7)
            pdf.save(self.source)

    def tearDown(self): self.temporary.cleanup()

    def test_pdf_text_layer_is_rendered_and_source_is_unchanged(self):
        before=self.source.read_bytes()
        with SourcePages(self.source,Options()) as pages:
            image,metadata=pages.read(0)
            self.assertFalse(metadata["embedded_text_used"])
            self.assertEqual(image.shape,(417,500))
            self.assertLess(image.min(),255)
        self.assertEqual(before,self.source.read_bytes())

    def test_page_failure_preserves_other_page_and_immutable_snapshot(self):
        original=SourcePages.read
        def fail_first(pages,index):
            if index==0: raise RuntimeError("synthetic first page failure")
            return original(pages,index)
        fake=FakeRecognizer()
        with patch.object(SourcePages,"read",fail_first), patch.object(socket.socket,"connect",side_effect=AssertionError("network forbidden")):
            output=process_document(self.source,self.root/"out",temp_root=self.root,options=Options(max_extra_calls=0),recognizer=fake)
        self.assertEqual([p["status"] for p in output["pages"]],["failed","needs_review"])
        self.assertEqual(output["performance"]["failed_pages"],1)
        self.assertFalse(output["search"]["entries"])
        with patch.object(fake,"recognize",side_effect=AssertionError("must reuse immutable snapshot")):
            again=process_document(self.source,self.root/"out",temp_root=self.root,options=Options(max_extra_calls=0),recognizer=fake)
        self.assertEqual(output,again)

    def test_low_disk_space_is_blocked_before_recognition(self):
        from types import SimpleNamespace
        fake=FakeRecognizer()
        with patch("ocr_pipeline.precision.extraction.shutil.disk_usage",return_value=SimpleNamespace(free=1)),self.assertRaises(OSError):
            process_document(self.source,self.root/"out",temp_root=self.root,recognizer=fake)
        self.assertEqual(fake.calls,0)

    def test_time_budget_retains_completed_page_and_reports_remaining_page(self):
        page={"page":1,"status":"blank","regions":[],"tables":[],"words":[]}
        with patch("ocr_pipeline.precision.extraction.time.monotonic",side_effect=[0,0,2,2]), \
             patch("ocr_pipeline.precision.extraction.process_page",return_value=page) as process:
            result=process_document(self.source,self.root/"out",temp_root=self.root,
                                    options=Options(max_document_seconds=1),recognizer=FakeRecognizer())
        self.assertEqual(process.call_count,1)
        self.assertEqual(result["pages"][0]["status"],"blank")
        self.assertEqual(result["pages"][1]["reason"],"document_time_budget_exhausted")
        self.assertEqual(result["performance"]["failed_pages"],1)

    def test_oversized_page_metadata_is_reported_without_oversized_snapshot(self):
        page={"page":1,"status":"needs_review","regions":[],"tables":[],"words":[],"engine_text_raw":"x"*(600*1024)}
        with patch("ocr_pipeline.precision.extraction.process_page",side_effect=lambda *a,**kw: copy.deepcopy(page)):
            result=process_document(self.source,self.root/"out",temp_root=self.root,
                                    options=Options(max_snapshot_bytes=1024*1024),recognizer=FakeRecognizer())
        self.assertEqual(len(result["pages"]),2)
        self.assertTrue(all(p["reason"]=="document_metadata_budget_exhausted" for p in result["pages"]))
        artifact=self.root/"out"/"extractions"/result["extraction_revision"]/"extraction.json"
        self.assertLess(artifact.stat().st_size,1024*1024)

    @unittest.skipUnless(importlib.util.find_spec("PIL"),"Optional candidate image decoder is not installed")
    def test_multipage_tiff_and_transparent_background(self):
        from PIL import Image
        source=self.root/"frames.tiff"
        a=Image.new("RGBA",(30,30),(0,0,0,0)); b=Image.new("RGBA",(30,30),(200,200,200,255))
        a.save(source,save_all=True,append_images=[b])
        with SourcePages(source,Options()) as pages:
            self.assertEqual(pages.count,2)
            self.assertTrue(blank_evidence(pages.read(0)[0])["verified_uniform_white"])
            self.assertFalse(blank_evidence(pages.read(1)[0])["verified_uniform_white"])


def review_fixture(root, revision="b"*24):
    snapshot=root/revision; snapshot.mkdir()
    reference=snapshot/"reference";reference.mkdir()
    cv2.imwrite(str(reference/"p0001.png"),np.full((100,100),255,np.uint8))
    region={"span_id":"P0001-test","page":1,"bbox":[1,2,50,30],"kind":"line","content_text":"۰۰۶۶",
            "engine_text_raw":"۰۰۶۶","reasons":["numeric_disagreement"],"attempts":[{"engine_text_raw":"۰۰۶۶"},{"engine_text_raw":"۰۰۴۶"}]}
    payload={"document":{"source_sha256":"a"*64,"filename":"synthetic.pdf"},"extraction_revision":revision,
             "pages":[{"page":1,"width":100,"height":100,"status":"needs_review","reference_image":"reference/p0001.png","regions":[region],"handwriting_declared":True}]}
    (snapshot/"extraction.json").write_text(json.dumps(payload),encoding="utf-8")
    return snapshot,payload


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.snapshot,self.payload=review_fixture(self.root)
        self.owner="c"*32;self.document="a"*64
        self.store=ReviewStore(self.root/"reviews")
        self.store.register(self.owner,self.payload,self.snapshot)
    def tearDown(self):self.temporary.cleanup()
    def approve(self,version=1):
        return self.store.save(self.owner,self.document,version,[{"span_id":"P0001-test","status":"approved","text":"۰۰۴۶","handwriting":True}],reviewer="Synthetic test reviewer")

    def test_approval_never_changes_raw_and_restart_recovers_search(self):
        raw=(self.snapshot/"extraction.json").read_bytes()
        self.assertEqual(self.store.search_snapshot(self.owner,self.document)["entries"],[])
        result=self.approve()
        self.assertEqual(result["version"],2)
        fresh=ReviewStore(self.root/"reviews")
        entries=fresh.search_snapshot(self.owner,self.document)["entries"]
        self.assertEqual(entries[0]["text"],"0046")
        self.assertEqual(entries[0]["human_text"],"۰۰۴۶")
        self.assertEqual((self.snapshot/"extraction.json").read_bytes(),raw)

    def test_conflict_revoke_and_duplicate_processing(self):
        self.approve()
        with self.assertRaises(ReviewConflict):self.approve()
        self.store.register(self.owner,self.payload,self.snapshot)
        self.assertEqual(self.store.get(self.owner,self.document)["version"],2)
        self.assertEqual(len(self.store.search_snapshot(self.owner,self.document)["entries"]),1)
        self.store.save(self.owner,self.document,2,[{"span_id":"P0001-test","status":"unreadable","text":None}],reviewer="Synthetic test reviewer")
        self.assertEqual(self.store.search_snapshot(self.owner,self.document)["entries"],[])

    def test_owner_and_path_boundaries(self):
        with self.assertRaises(FileNotFoundError):self.store.get("d"*32,self.document)
        for owner,document in (("../",self.document),(self.owner,"../state")):
            with self.assertRaises(ValueError):self.store.get(owner,document)
        with self.assertRaises(ValueError):
            self.store.save(self.owner,self.document,1,[{"span_id":"P0001-test","status":"approved","text":"x"*10001}],reviewer="test")

    def test_revision_activation_and_rollback_preserve_approvals(self):
        self.approve()
        snapshot,payload=review_fixture(self.root,"e"*24)
        self.store.register(self.owner,payload,snapshot)
        self.assertEqual(self.store.get(self.owner,self.document)["extraction_revision"],"b"*24)
        self.store.save(self.owner,self.document,3,[],reviewer="test",activate_revision="e"*24)
        self.assertEqual(self.store.search_snapshot(self.owner,self.document)["entries"],[])
        self.store.save(self.owner,self.document,4,[],reviewer="test",activate_revision="b"*24)
        self.assertEqual(len(self.store.search_snapshot(self.owner,self.document)["entries"]),1)

    def test_declared_handwriting_is_preserved_when_edit_omits_optional_flag(self):
        result=self.store.save(self.owner,self.document,1,[{"span_id":"P0001-test","status":"approved","text":"۴"}],reviewer="Synthetic tester")
        self.assertTrue(result["units"][0]["handwriting"])


class IsolationTests(unittest.TestCase):
    def test_missing_web_storage_configuration_has_no_default(self):
        environment=os.environ.copy();environment.pop("OCR_DATA_DIR",None)
        result=subprocess.run([sys.executable,"-B","-c","import web_app"],capture_output=True,text=True,env=environment,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("explicit absolute OCR_DATA_DIR",result.stderr)

    @unittest.skipUnless(os.environ.get("OCR_TEST_ROOT"),"Run scripts/run_isolated_tests.py to prove write boundaries")
    def test_import_startup_and_child_write_guard(self):
        root=Path(os.environ["OCR_TEST_ROOT"]).resolve()
        code="""import os,json,tempfile,sqlite3
from pathlib import Path
import web_app
assert all(Path(p).resolve().is_relative_to(Path(os.environ['OCR_TEST_ROOT']).resolve()) for p in (web_app.UPLOAD_DIR,web_app.OUT_DIR,Path(tempfile.gettempdir())))
client=web_app.app.test_client()
assert client.get('/').status_code == 200
outside=Path(os.environ['OCR_TEST_ROOT']).parent/'forbidden-ocr-write-test.txt'
try:
    outside.write_text('denied')
    raise AssertionError('guard missing')
except PermissionError: pass
try:
    sqlite3.connect(':memory:')
    raise AssertionError('unexpected database')
except PermissionError: pass
print(json.dumps({'outside_write':'denied','database':'denied','startup':'isolated'}))
"""
        result=subprocess.run([sys.executable,"-B","-c",code],capture_output=True,text=True,env=os.environ.copy(),timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["outside_write"],"denied")
        self.assertFalse((root.parent/"forbidden-ocr-write-test.txt").exists())
