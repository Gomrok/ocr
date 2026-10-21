import io
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
import web_app as web


def pdf_bytes(pages=1):
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page(width=250, height=180)
        return document.tobytes()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gomrok_ocr_test_")
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.output = self.root / "output"
        self.uploads.mkdir()
        self.output.mkdir()
        self.patches = [patch.object(web, "DATA_DIR", self.root),
                        patch.object(web, "UPLOAD_DIR", self.uploads),
                        patch.object(web, "OUT_DIR", self.output),
                        patch.object(web, "QUEUE_SLOTS", threading.BoundedSemaphore(2)),
                        patch.object(web.threading, "Thread")]
        for item in self.patches:
            item.start()
        web.JOBS.clear()
        web.UPLOAD_TIMES.clear()
        web.PENDING_OWNERS.clear()
        self.client = web.app.test_client()
        self.page = self.client.get("/")
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', self.page.text)[1]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def upload(self, payload=None, name="سند.pdf", **extra):
        data = {"pdfs": (io.BytesIO(payload if payload is not None else pdf_bytes()), name)}
        data.update(extra)
        return self.client.post("/upload", data=data, headers={"X-CSRF-Token": self.csrf})

    def test_formal_interface_and_local_font(self):
        self.assertEqual(self.page.status_code, 200)
        self.assertIn('lang="fa" dir="rtl"', self.page.text)
        self.assertIn("Vazir-Bold.woff2", self.page.text)
        self.assertIn("Vazirmatn.woff2", self.page.text)
        self.assertIn('id="previewDocument"', self.page.text)
        self.assertIn('id="downloadList"', self.page.text)
        self.assertIn("اصلاح متن: پیشنهادی", self.page.text)
        self.assertNotIn("AIza", self.page.text)
        self.assertIsNone(re.search(r"[\U0001f300-\U0001faff\u2600-\u27bf]", self.page.text))
        with self.client.get("/static/fonts/Vazir-Bold.woff2") as font:
            self.assertEqual(font.data[:4], b"wOF2")

    def test_brand_palette_and_six_step_resolution_control(self):
        self.assertIn("فضای کار استخراج متن", self.page.text)
        with self.client.get("/static/dashboard.css") as stylesheet:
            self.assertEqual(stylesheet.status_code, 200)
            self.assertIn("--navy:#132a46", stylesheet.text)
            self.assertIn("--teal:#078d86", stylesheet.text)
            self.assertIn("--blue:#3675db", stylesheet.text)
            self.assertIn("@media(max-width:760px)", stylesheet.text)
        self.assertRegex(self.page.text, r'id="dpiControl" type="range" min="0" max="5" step="1" value="5"')
        self.assertIn('aria-describedby="qualityHint qualityScale"', self.page.text)
        stops = re.findall(r'<span data-dpi="(\d+)">', self.page.text)
        self.assertEqual(stops, ["100", "250", "500", "1000", "1500", "2000"])
        self.assertLess(self.page.text.index('id="qualityHeading"'), self.page.text.index('id="results"'))

    def test_csp_and_secure_headers(self):
        nonce = re.search(r'<script defer nonce="([^"]+)"', self.page.text)[1]
        self.assertIn("'nonce-" + nonce + "'", self.page.headers["Content-Security-Policy"])
        self.assertEqual(self.page.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.page.headers["Cache-Control"], "no-store")
        self.assertIn("script-src 'self'", self.page.headers["Content-Security-Policy"])
        self.assertIn("HttpOnly", self.page.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", self.page.headers["Set-Cookie"])

    def test_csrf_is_required(self):
        self.assertEqual(self.client.post("/upload").status_code, 403)
        self.assertEqual(self.client.post("/upload", headers={"X-CSRF-Token": "é"}).status_code, 403)

    def test_valid_pdf_uses_safe_storage_names(self):
        response = self.upload(name="../../خصوصی.pdf")
        self.assertEqual(response.status_code, 200)
        job_id = response.json["job_id"]
        self.assertEqual(len(job_id), 32)
        self.assertTrue((self.uploads / (job_id + "_001.pdf")).is_file())
        status = self.client.get("/status/" + job_id)
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("owner_id", status.json)
        self.assertNotIn("documents", status.json)

    def test_cross_session_status_and_download_are_denied(self):
        job = self.upload().json["job_id"]
        path = self.output / "private.txt"
        path.write_text("private test output", encoding="utf-8")
        web.JOBS[job]["files"] = ["private.txt"]
        outsider = web.app.test_client()
        self.assertEqual(outsider.get("/status/" + job).status_code, 404)
        self.assertEqual(outsider.get("/download/" + job + "/private.txt").status_code, 404)
        with self.client.get("/download/" + job + "/private.txt") as download:
            self.assertEqual(download.status_code, 200)
        self.assertEqual(self.client.get("/download/not-a-job/private.txt").status_code, 404)
        self.assertEqual(self.client.get("/download/" + job + "/../private.txt").status_code, 404)

    def test_preview_is_owned_and_bounded_to_document_pages(self):
        job = self.upload(pdf_bytes(2)).json["job_id"]
        image = self.client.get(f"/preview/{job}/1/1")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, "image/png")
        self.assertLessEqual(max(pymupdf.Pixmap(image.data).width, pymupdf.Pixmap(image.data).height), 1400)
        outsider = web.app.test_client()
        self.assertEqual(outsider.get(f"/preview/{job}/1/1").status_code, 404)
        self.assertEqual(outsider.get(f"/preview-text/{job}/1/1").status_code, 404)
        for index, page in ((2, 1), (1, 3), (0, 1), (1, 0)):
            with self.subTest(index=index, page=page):
                self.assertEqual(self.client.get(f"/preview/{job}/{index}/{page}").status_code, 404)
                self.assertEqual(self.client.get(f"/preview-text/{job}/{index}/{page}").status_code, 404)

    def test_text_preview_uses_only_the_requested_page_and_preserves_digits(self):
        job = self.upload(pdf_bytes(2)).json["job_id"]
        pending = self.client.get(f"/preview-text/{job}/1/1")
        self.assertEqual(pending.json["state"], "pending")
        document_id = web.JOBS[job]["file_statuses"][0]["id"]
        target = self.output / document_id
        target.mkdir()
        (target / f"{document_id}.txt").write_text(
            "==================== PAGE ۱ ====================\nنامه ۰۰۴۶\n"
            "==================== PAGE ۲ ====================\nمبلغ ۴۶\n", encoding="utf-8")
        web.JOBS[job]["file_statuses"][0]["state"] = "done"
        first = self.client.get(f"/preview-text/{job}/1/1").json
        second = self.client.get(f"/preview-text/{job}/1/2").json
        self.assertEqual(first["text"], "نامه ۰۰۴۶")
        self.assertEqual(second["text"], "مبلغ ۴۶")
        self.assertFalse(first["truncated"])

    def test_status_has_stable_total_and_per_file_state(self):
        job = self.upload(pdf_bytes(2)).json["job_id"]
        state = self.client.get(f"/status/{job}").json
        self.assertEqual(state["pages_total"], 2)
        self.assertEqual(state["pages_done"], 0)
        self.assertEqual(state["file_statuses"][0]["page_count"], 2)
        self.assertEqual(state["file_statuses"][0]["state"], "waiting")
        self.assertIn("elapsed_seconds", state)
        self.assertNotIn("started_monotonic", state)

    def test_page_callbacks_keep_batch_denominator_and_partial_file_visible(self):
        job = self.upload(pdf_bytes(2)).json["job_id"]
        callback = web._make_cb(job, "سند.pdf", 1)
        callback({"type": "start", "pages": 2, "dpi": 2000})
        callback({"type": "page_ok", "page": 1, "words": 4, "tables": 0,
                  "source": "tesseract", "needs_review": True})
        callback({"type": "page_error", "page": 2})
        web._finish_document(job, 1, "done", ["sample/sample.txt"])
        status = self.client.get(f"/status/{job}").json
        self.assertEqual((status["pages_total"], status["pages_done"]), (2, 2))
        self.assertEqual(status["failed_pages"], 1)
        self.assertEqual(status["review_pages"], 1)
        self.assertEqual(status["file_statuses"][0]["state"], "partial")
        self.assertEqual(status["file_statuses"][0]["outputs"], ["sample/sample.txt"])

    def test_preview_rendering_is_concurrency_bounded(self):
        job = self.upload().json["job_id"]
        web.PREVIEW_SLOTS.acquire()
        web.PREVIEW_SLOTS.acquire()
        try:
            response = self.client.get(f"/preview/{job}/1/1")
            self.assertEqual(response.status_code, 503)
        finally:
            web.PREVIEW_SLOTS.release()
            web.PREVIEW_SLOTS.release()

    def test_spoofed_and_corrupt_pdfs_are_rejected(self):
        self.assertEqual(self.upload(b"not a pdf").status_code, 400)
        self.assertEqual(self.upload(b"%PDF-1.4\ninvalid").status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(web.PENDING_OWNERS, set())

    def test_all_six_dpi_levels_reach_the_worker_and_status(self):
        for dpi in (100, 250, 500, 1000, 1500, 2000):
            with self.subTest(dpi=dpi):
                response = self.upload(dpi=str(dpi))
                self.assertEqual(response.status_code, 200)
                job_id = response.json["job_id"]
                self.assertEqual(response.json["dpi"], dpi)
                call = web.threading.Thread.call_args
                self.assertEqual(call.kwargs["target"], web._run_job)
                self.assertEqual(call.kwargs["args"][2], dpi)
                self.assertEqual(self.client.get("/status/" + job_id).json["dpi"], dpi)
                web.JOBS[job_id]["state"] = "done"
                web.QUEUE_SLOTS.release()  # The mocked worker cannot release its slot.

    def test_omitted_dpi_preserves_2000_default(self):
        response = self.upload()
        self.assertEqual(response.json["dpi"], 2000)
        self.assertEqual(web.threading.Thread.call_args.kwargs["args"][2], 2000)

    def test_invalid_and_duplicate_dpi_values_are_rejected_without_files(self):
        for dpi in ("999999", "-1", "invalid", "0", "200", "300", "2000.0", "1e3", "",
                    " 100", "۱۰۰", "0100", "NaN", ["100", "2000"], ["500", "500"]):
            with self.subTest(dpi=dpi):
                web.UPLOAD_TIMES.clear()
                self.assertEqual(self.upload(dpi=dpi).status_code, 400)
                self.assertEqual(list(self.uploads.iterdir()), [])
                self.assertEqual(web.JOBS, {})
                self.assertEqual(web.PENDING_OWNERS, set())
        web.threading.Thread.assert_not_called()

    def test_selected_dpi_is_forwarded_to_every_document_in_batch(self):
        with patch.object(web, "process_pdf", return_value={
                "document": {"pages": 1}, "performance": {"failed_pages": 0}}) as process:
            response = self.upload(dpi="250")
            job_id = response.json["job_id"]
            paths = [self.uploads / f"{job_id}_001.pdf", self.uploads / f"{job_id}_002.pdf"]
            web.JOBS[job_id]["documents"][paths[1].name] = "دوم.pdf"
            with patch.object(web, "_collect_outputs", return_value=[]):
                web._process_job(job_id, paths, 250)
        self.assertEqual(process.call_count, 2)
        self.assertTrue(all(call.kwargs["dpi"] == 250 for call in process.call_args_list))

    def test_100_pdf_batch_contract(self):
        content = pdf_bytes()
        data = {"pdfs": [(io.BytesIO(content), f"document_{i}.pdf") for i in range(100)]}
        response = self.client.post("/upload", data=data, headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 100)

    def test_empty_recognition_is_not_advertised_as_successful_downloads(self):
        response=self.upload()
        job=response.json["job_id"]
        path=self.uploads/f"{job}_001.pdf"
        result={"document":{"pages":1},"performance":{"failed_pages":0},
                "pages":[{"page":1,"status":"blank","has_text":False,"text":"","tables":[]}]}
        with patch.object(web,"process_pdf",return_value=result), patch.object(web,"_collect_outputs") as outputs:
            web._process_job(job,[path],2000)
        outputs.assert_not_called()
        state=self.client.get(f"/status/{job}").json
        self.assertEqual(state["state"],"error")
        self.assertEqual(state["files"],[])
        self.assertIsNone(state["zip"])
        self.assertEqual(state["file_statuses"][0]["state"],"error")

    def test_duplicate_owner_job_is_rejected(self):
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.upload().status_code, 409)

    def test_page_limit_and_queue_are_bounded(self):
        with patch.object(web, "MAX_BATCH_PAGES", 1):
            self.assertEqual(self.upload(pdf_bytes(2)).status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        web.QUEUE_SLOTS.acquire()
        web.QUEUE_SLOTS.acquire()
        self.assertEqual(self.upload().status_code, 503)

    def test_health_reports_engine_availability(self):
        with patch.object(web.LOCAL_ENGINE, "available", return_value=True):
            self.assertEqual(self.client.get("/healthz").status_code, 200)
        with patch.object(web.LOCAL_ENGINE, "available", return_value=False):
            self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_rate_limit_rejects_excessive_requests(self):
        for _ in range(10):
            self.assertEqual(self.upload(b"invalid").status_code, 400)
        self.assertEqual(self.upload(b"invalid").status_code, 429)


if __name__ == "__main__":
    unittest.main()
