import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf

import web_app as web
from ocr_pipeline import review as R
from ocr_pipeline import tables as T
from ocr_pipeline.precision import merge_detections, reconcile, source_box


def document():
    table = T.build_table_model(
        1, ((60, 90), (5, 95)), {(0, 0): "۱۲۳"}, {(0, 0): 0.7}, "test"
    )
    table["markdown"] = T.markdown_table(table)
    return {
        "document": {"filename": "original.pdf", "pages": 1},
        "pages": [
            {
                "page": 1,
                "width": 100,
                "height": 100,
                "text": "متن ۱۲",
                "raw_text": "متن 12",
                "normalized_text": "متن ۱۲",
                "tables": [table],
                "words": [
                    {
                        "text": "متن",
                        "raw_ocr_text": "متن",
                        "bbox": [50, 10, 90, 30],
                        "line_id": "a",
                        "confidence": 0.9,
                    },
                    {
                        "text": "۱۲",
                        "raw_ocr_text": "12",
                        "bbox": [5, 10, 40, 30],
                        "line_id": "a",
                        "needs_review": True,
                        "confidence": 0.5,
                    },
                ],
            }
        ],
        "performance": {"failed_pages": 0},
    }


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / ("a" * 32 + "_001")
        self.out.mkdir()
        capacity = patch.object(
            R.shutil, "disk_usage", return_value=SimpleNamespace(free=8 * 1024**3)
        )
        capacity.start()
        self.addCleanup(capacity.stop)
        self.original = document()
        R.create_document(self.out, self.original)

    def tearDown(self):
        self.temp.cleanup()

    def edit(self, revision=0):
        data = R.read_document(self.out)
        return {
            "revision": revision,
            "edits": [
                {
                    "id": data["pages"][0]["units"][0]["id"],
                    "text": "اصلاح 0042",
                    "confirmed": True,
                },
                {
                    "id": data["pages"][0]["units"][1]["id"],
                    "text": '=HYPERLINK("x")',
                    "confirmed": False,
                },
            ],
        }

    def test_original_is_immutable_and_all_revision_exports_agree(self):
        initial = (self.out / "review-base.json").read_bytes()
        result = R.save_document(self.out, self.edit())
        self.assertEqual(result["revision"], 1)
        self.assertEqual(initial, (self.out / "review-base.json").read_bytes())
        self.assertEqual(self.original, document())
        paths = [self.root / name for name in result["files"]]
        output = next(
            p for p in paths if p.suffix == ".json" and "tables" not in p.parts
        )
        revised = json.loads(output.read_text("utf-8"))
        self.assertEqual(revised["pages"][0]["text"], "اصلاح ۰۰۴۲")
        self.assertEqual(revised["pages"][0]["raw_text"], "متن 12")
        self.assertEqual(
            revised["pages"][0]["words"], self.original["pages"][0]["words"]
        )
        self.assertEqual(
            revised["pages"][0]["tables"][0]["cells"][0]["raw_text"], "۱۲۳"
        )
        txt = next(p for p in paths if p.suffix == ".txt").read_text("utf-8")
        self.assertIn("اصلاح ۰۰۴۲", txt)
        self.assertIn('=HYPERLINK("x")', txt)
        csv = next(p for p in paths if p.suffix == ".csv").read_text("utf-8")
        self.assertIn("'=HYPERLINK", csv)
        with zipfile.ZipFile(next(p for p in paths if p.suffix == ".zip")) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), len(paths) - 1)

    def test_stale_revision_cannot_overwrite_user_work(self):
        R.save_document(self.out, self.edit())
        with self.assertRaises(R.ReviewConflict):
            R.save_document(self.out, self.edit(revision=0))
        self.assertEqual(R.read_document(self.out)["revision"], 1)

    def test_revisions_survive_fresh_reads_and_keep_previous_exports(self):
        first = R.save_document(self.out, self.edit())
        second = R.save_document(self.out, self.edit(1))
        self.assertEqual(second["revision"], 2)
        self.assertTrue(all((self.root / name).is_file() for name in first["files"]))

    def test_validation_rejects_unknown_duplicate_units_controls_and_large_text(self):
        for change in (
            {"id": "../private", "text": "x", "confirmed": True},
            {"id": "p0001-u00001", "text": "\x00", "confirmed": True},
            {"id": "p0001-u00001", "text": "\x01", "confirmed": True},
            {"id": "p0001-u00001", "text": "x" * 8193, "confirmed": True},
            {"id": "p0001-u00001", "text": "x", "confirmed": "yes"},
        ):
            with self.subTest(change=change["id"]), self.assertRaises(ValueError):
                R.save_document(self.out, {"revision": 0, "edits": [change]})
        with self.assertRaises(ValueError):
            R.save_document(
                self.out, {"revision": 0, "edits": [self.edit()["edits"][0]] * 2}
            )
        self.assertEqual(R.read_document(self.out)["revision"], 0)

    def test_partial_export_failure_does_not_publish_revision(self):
        with (
            patch.object(T, "write_table_outputs", side_effect=OSError("full")),
            self.assertRaises(OSError),
        ):
            R.save_document(self.out, self.edit())
        self.assertEqual(R.read_document(self.out)["revision"], 0)
        self.assertEqual(list((self.out / "reviews").iterdir()), [])

    def test_metadata_budget_rejects_oversized_data_without_changing_original(self):
        initial = (self.out / "review-base.json").read_bytes()
        with (
            patch.object(R, "MAX_BASE_BYTES", 32),
            self.assertRaises(R.ReviewLimitError),
        ):
            R.create_document(self.out, self.original)
        self.assertEqual((self.out / "review-base.json").read_bytes(), initial)
        with self.assertRaises(R.ReviewLimitError):
            R.read_json(self.out / "review-base.json", limit=32)
        self.assertEqual(R.read_document(self.out)["revision"], 0)

    def test_two_concurrent_saves_publish_only_one_revision(self):
        body = self.edit()
        ready = threading.Barrier(2)

        def save():
            ready.wait(timeout=5)
            try:
                return R.save_document(self.out, body)["revision"]
            except R.ReviewConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save(), range(2)))
        self.assertCountEqual(results, [1, "conflict"])
        self.assertEqual(R.read_document(self.out)["revision"], 1)

    def test_another_service_process_cannot_overwrite_locked_review(self):
        code = (
            "from pathlib import Path; import sys; from ocr_pipeline.review import document_lock\n"
            "with document_lock(Path(sys.argv[1])):\n"
            " print('locked', flush=True)\n"
            " sys.stdin.readline()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(self.out)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self.assertEqual(
                child.stdout.readline(),
                b"locked\r\n" if sys.platform == "win32" else b"locked\n",
            )
            with self.assertRaises(R.ReviewConflict):
                R.save_document(self.out, self.edit())
        finally:
            child.communicate(b"\n", timeout=10)
        self.assertEqual(R.save_document(self.out, self.edit())["revision"], 1)

    def test_storage_exhaustion_during_export_removes_only_new_attempt(self):
        first = R.save_document(self.out, self.edit())
        with (
            patch.object(
                R.shutil,
                "disk_usage",
                side_effect=[
                    SimpleNamespace(free=8 * 1024**3),
                    SimpleNamespace(free=1),
                ],
            ),
            self.assertRaises(R.ReviewLimitError),
        ):
            R.save_document(self.out, self.edit(1))
        self.assertEqual(R.read_document(self.out)["revision"], 1)
        self.assertEqual(len(list((self.out / "reviews").iterdir())), 1)
        self.assertTrue(all((self.root / name).is_file() for name in first["files"]))

    def test_second_engine_failure_is_visible_on_every_fallback_unit(self):
        original = document()
        original["pages"][0]["precision_warnings"] = ["second_engine_failed"]
        fresh = self.root / ("f" * 32 + "_001")
        fresh.mkdir()
        R.create_document(fresh, original)
        restored = R.read_document(fresh)
        self.assertTrue(all(u["needs_review"] for u in restored["pages"][0]["units"]))
        self.assertEqual(restored["pages"][0]["warnings"], ["second_engine_failed"])

    def test_disk_reserve_and_revision_limits_preserve_current_version(self):
        with (
            patch.object(R.shutil, "disk_usage", return_value=SimpleNamespace(free=10)),
            self.assertRaises(R.ReviewLimitError),
        ):
            R.save_document(self.out, self.edit())
        with patch.object(R, "MAX_REVISIONS", 0), self.assertRaises(R.ReviewLimitError):
            R.save_document(self.out, self.edit())
        self.assertEqual(R.read_document(self.out)["revision"], 0)

    def test_job_ownership_survives_restart_and_interrupted_jobs_are_explicit(self):
        R.save_job(
            self.root, "a" * 32, {"owner_id": "owner", "state": "running", "log": []}
        )
        self.assertIsNone(R.load_job(self.root, "a" * 32, "other"))
        self.assertIsNone(R.load_job(self.root, "../escape", "owner"))
        self.assertEqual(R.load_job(self.root, "a" * 32, "owner")["state"], "error")

    def test_same_engine_votes_do_not_remove_uncertainty(self):
        result = reconcile(
            [{"engine": "tesseract", "text": "۱۲۳", "confidence": 0.99}] * 3
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["reason"], "engine_disagreement")

    def test_independent_readings_preserve_leading_zeros_and_persian_digits(self):
        result = reconcile(
            [
                {"engine": "tesseract", "text": "0012/04", "confidence":.99},
                {"engine": "paddleocr", "text": "۰۰۱۲/۰۴", "confidence":.99},
            ]
        )
        self.assertEqual(result["text"], "۰۰۱۲/۰۴")
        self.assertFalse(result["needs_review"])

    def test_disagreeing_numbers_are_not_claimed_verified(self):
        result = reconcile(
            [
                {"engine": "tesseract", "text": "۱۶۰", "confidence": 0.99},
                {"engine": "paddleocr", "text": "۱۵۰", "confidence": 0.99},
            ],
            "۱۶۰",
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["text"], "۱۶۰")

    def test_tile_dedup_preserves_repeated_text_at_separate_locations(self):
        def reading(x, y):
            return {
                "text": "تکرار",
                "polygon": [[x, y], [x + 100, y], [x + 100, y + 20], [x, y + 20]],
            }

        groups = merge_detections([reading(0, 0), reading(5, 1), reading(0, 80)])
        self.assertEqual(len(groups), 2)

    def test_source_box_undoes_deskew(self):
        p = {
            "width": 1000,
            "height": 1000,
            "analysis": {"skew_angle": 3},
            "preprocess_ops": ["deskew(3)"],
        }
        box = source_box([100, 100, 200, 130], p)
        self.assertNotEqual(box, [100, 100, 200, 130])
        self.assertTrue(all(0 <= v <= 1000 for v in box))


class ReviewRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.patches = [
            patch.object(web, "DATA_DIR", self.root),
            patch.object(web, "UPLOAD_DIR", self.uploads),
            patch.object(
                web.shutil, "disk_usage", return_value=SimpleNamespace(free=8 * 1024**3)
            ),
            patch.object(web, "OUT_DIR", self.output),
        ]
        for p in self.patches:
            p.start()
        web.JOBS.clear()
        self.client = web.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.owner = session["owner_id"]
            self.csrf = session["csrf_token"]
        self.job = "b" * 32
        self.doc = self.job + "_001"
        out = self.output / self.doc
        out.mkdir()
        R.create_document(out, document())
        with pymupdf.open() as source:
            source.new_page(width=100, height=100)
            source.save(self.uploads / (self.doc + ".pdf"))
        R.save_job(
            self.root,
            self.job,
            {
                "owner_id": self.owner,
                "state": "done",
                "log": [],
                "files": [],
                "zip": None,
                "documents": {self.doc + ".pdf": "نمونه.pdf"},
                "review_documents": [
                    {
                        "id": self.doc,
                        "name": "نمونه.pdf",
                        "revision": 0,
                        "unresolved": 1,
                    }
                ],
            },
        )
        self.url = "/review/" + self.job + "/" + self.doc

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_cross_session_cannot_read_edit_or_preview(self):
        outsider = web.app.test_client()
        outsider.get("/")
        with outsider.session_transaction() as session:
            csrf = session["csrf_token"]
        self.assertEqual(outsider.get(self.url).status_code, 404)
        self.assertEqual(
            outsider.post(
                self.url, json={}, headers={"X-CSRF-Token": csrf}
            ).status_code,
            404,
        )
        self.assertEqual(
            outsider.get(f"/preview/{self.job}/{self.doc}/1").status_code, 404
        )

    def test_restored_owner_can_review_and_download_bounded_preview(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        response = self.client.get(f"/preview/{self.job}/{self.doc}/1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\x89PNG"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            self.client.get(f"/preview/{self.job}/{self.doc}/99").status_code, 404
        )

    def test_csrf_revision_validation_and_authorized_revision_download(self):
        self.assertEqual(self.client.post(self.url, json={}).status_code, 403)
        body = {
            "revision": 0,
            "edits": [{"id": "p0001-u00001", "text": "اصلاح 12", "confirmed": True}],
        }
        response = self.client.post(
            self.url, json=body, headers={"X-CSRF-Token": self.csrf}
        )
        self.assertEqual(response.status_code, 200)
        path = next(p for p in response.json["files"] if p.endswith(".txt"))
        with self.client.get("/download/" + self.job + "/" + path) as downloaded:
            self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(
            self.client.post(
                self.url, json=body, headers={"X-CSRF-Token": self.csrf}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.get(
                "/download/" + self.job + "/" + self.doc + "/review-base.json"
            ).status_code,
            404,
        )

    def test_other_document_id_and_traversal_are_rejected(self):
        self.assertEqual(
            self.client.get(
                "/review/" + self.job + "/" + ("c" * 32) + "_001"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/review/" + self.job + "/..").status_code, 404
        )

    def test_preview_failure_is_safe_and_releases_capacity(self):
        url = f"/preview/{self.job}/{self.doc}/1"
        with patch.object(
            web, "render_preview", side_effect=RuntimeError("private path")
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"private path", response.data)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_complex_pdf_preview_times_out_without_changing_source(self):
        from ocr_pipeline import preview

        source_path = self.uploads / (self.doc + ".pdf")
        with pymupdf.open() as doc:
            page = doc.new_page(width=100, height=100)
            prior = None
            for _ in range(13):
                xref = doc.get_new_xref()
                resources = f"/XObject << /F {prior} 0 R >>" if prior else ""
                doc.update_object(
                    xref,
                    f"<< /Type /XObject /Subtype /Form "
                    f"/BBox [0 0 100 100] /Resources << {resources} >> >>",
                )
                doc.update_stream(
                    xref, b"0 0 10 10 re f" if prior is None else b"/F Do\n" * 4
                )
                prior = xref
            resource = doc.get_new_xref()
            doc.update_object(resource, f"<< /XObject << /F {prior} 0 R >> >>")
            doc.xref_set_key(page.xref, "Resources", f"{resource} 0 R")
            content = doc.get_new_xref()
            doc.update_object(content, "<< >>")
            doc.update_stream(content, b"/F Do")
            page.set_contents(content)
            doc.save(source_path)
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        with patch.object(preview, "PREVIEW_SECONDS", 2):
            response = self.client.get(f"/preview/{self.job}/{self.doc}/1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
