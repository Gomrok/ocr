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
        self.assertNotIn("<svg", self.page.text)
        self.assertNotIn("AIza", self.page.text)
        self.assertIsNone(re.search(r"[\U0001f300-\U0001faff\u2600-\u27bf]", self.page.text))
        with self.client.get("/static/fonts/Vazir-Bold.woff2") as font:
            self.assertEqual(font.data[:4], b"wOF2")

    def test_csp_and_secure_headers(self):
        nonce = re.search(r'<script nonce="([^"]+)"', self.page.text)[1]
        self.assertIn("'nonce-" + nonce + "'", self.page.headers["Content-Security-Policy"])
        self.assertEqual(self.page.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.page.headers["Cache-Control"], "no-store")
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

    def test_spoofed_and_corrupt_pdfs_are_rejected(self):
        self.assertEqual(self.upload(b"not a pdf").status_code, 400)
        self.assertEqual(self.upload(b"%PDF-1.4\ninvalid").status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(web.PENDING_OWNERS, set())

    def test_quality_cannot_be_overridden(self):
        for dpi in ("999999", "-1", "invalid"):
            self.assertEqual(self.upload(dpi=dpi).status_code, 400)

    def test_100_pdf_batch_contract(self):
        content = pdf_bytes()
        data = {"pdfs": [(io.BytesIO(content), f"document_{i}.pdf") for i in range(100)]}
        response = self.client.post("/upload", data=data, headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 100)

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
