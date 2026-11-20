"""Quality disclosure is not a handwriting accuracy claim."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pymupdf

from ocr_pipeline import pipeline
from ocr_pipeline.quality import page_quality, unknown_quality


class QualityTests(unittest.TestCase):
    def test_high_confidence_handwriting_still_requires_human_review(self):
        page = {"words": [{"text": "۰۰۴۶", "confidence": .99}], "text": "۰۰۴۶"}
        before = copy.deepcopy(page)
        result = page_quality(page, handwriting=True)
        self.assertTrue(result["review_required"])
        self.assertFalse(result["human_verified"])
        self.assertFalse(result["confidence_is_accuracy"])
        self.assertEqual(result["flagged_items"], 0)
        self.assertEqual(page, before)
        self.assertEqual(page_quality(page)["status"], "unverified")

    def test_unknown_invalid_and_disagreeing_readings_remain_visible(self):
        page = {"words": [{"text": str(index), "confidence": confidence}
                           for index, confidence in enumerate([None, -1, float("nan"), 1.1, .4, .99])],
                "low_confidence_words": 8}
        page["words"][-1]["needs_review"] = True
        quality = page_quality(page)
        self.assertEqual(quality["flagged_words"], 6)
        self.assertEqual(quality["flagged_items"], 8)
        self.assertTrue(quality["review_required"])
        self.assertIsNone(unknown_quality()["flagged_items"])

    def test_blank_and_failed_pages_are_not_certified(self):
        self.assertEqual(page_quality({"status": "blank"}, handwriting=True)["status"], "blank")
        self.assertEqual(page_quality({"error": "nonblank_page_no_text"})["status"], "failed")
        self.assertFalse(page_quality({"status": "blank"})["human_verified"])

    def test_exports_keep_raw_content_and_record_only_system_annotations(self):
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as document:
            document.new_page()
            root = Path(directory)
            source = root / "synthetic.pdf"
            document.save(source)
            source_before = source.read_bytes()
            original = {"page": 1, "status": "extracted", "text": "۰۰۴۶ NA ...",
                        "raw_text": "0046 NA ...", "words": [{"text": "۰۰۴۶", "raw_ocr_text": "0046", "confidence": .99}],
                        "tables": [], "metrics": {}, "engines": ["tesseract"], "low_confidence_words": 0}
            stats = {"ocr_processes": 0, "ocr_cache_hits": 0, "ocr_native_calls": 0}
            engine = SimpleNamespace(name="tesseract", cache_enabled=True, statistics=lambda: stats.copy())
            events = []
            with patch.object(pipeline.E, "pick_engine", return_value=engine), \
                    patch.object(pipeline, "ocr_page", return_value=copy.deepcopy(original)):
                result = pipeline._process_document(document, engine, source, root/"exports", 300,
                                                    None, True, 0, False, events.append, handwriting=True)
            stored = json.loads((root/"exports/synthetic.json").read_text("utf-8"))
            self.assertEqual(result, stored)
            page = stored["pages"][0]
            self.assertEqual(page["raw_text"], original["raw_text"])
            self.assertEqual(page["text"], original["text"])
            self.assertEqual(page["words"], original["words"])
            self.assertFalse(stored["document"]["human_verified"])
            self.assertTrue(next(event for event in events if event["type"] == "page_ok")["needs_review"])
            notice = page["export_annotations"][0]["text"]
            literal = (root/"exports/synthetic.txt").read_text("utf-8")
            self.assertEqual(literal.count(notice), 1)
            self.assertIn(original["text"], literal)
            metadata = json.loads((root/"exports/synthetic.quality.json").read_text("utf-8"))
            self.assertEqual(metadata["pages"][0]["quality"], page["quality"])
            self.assertEqual(source.read_bytes(), source_before)
