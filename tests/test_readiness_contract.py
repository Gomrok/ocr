import os
import unittest
from unittest.mock import patch

import test_web

import web_app as web


class ReadinessContractTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp
    tearDown = test_web.WebTests.tearDown

    def test_required_exporter_blocks_admission_without_saving_upload(self):
        with (
            patch.dict(os.environ, {"OCR_REQUIRE_XLSX": "1"}),
            patch.object(web, "xlsx_available", return_value=False),
        ):
            response = test_web.WebTests.upload(self)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(list(self.uploads.iterdir()))
        self.assertFalse(web.JOBS)
        self.assertFalse(web.PENDING_OWNERS)
        self.assertTrue(web.QUEUE_SLOTS.acquire(blocking=False))
        web.QUEUE_SLOTS.release()

    def test_required_exporter_readiness_is_explicit(self):
        with (
            patch.dict(
                os.environ, {"OCR_REQUIRE_XLSX": "1", "OCR_ACCURACY_MODE": "legacy"}
            ),
            patch.object(web, "xlsx_available", return_value=False),
            patch.object(web.LOCAL_ENGINE, "available", return_value=True),
        ):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["exports"]["xlsx"]["status"], "unavailable")
        self.assertEqual(
            response.json["exports"]["xlsx"]["reason"], "BLOCKED_DEPENDENCY"
        )
