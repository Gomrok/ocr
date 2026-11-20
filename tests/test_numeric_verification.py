import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from ocr_pipeline.engines import TesseractEngine
from ocr_pipeline.metrics import Metrics
from ocr_pipeline.pipeline import _glyph_digit_ocr, _verify_text_numbers
from ocr_pipeline import pipeline as P, engines as E


class NumericVerificationTests(unittest.TestCase):
    def test_bundled_script_model_matches_pinned_upstream_asset(self):
        model=Path(__file__).resolve().parent.parent/'tessdata/Arabic.traineddata'
        self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(),
                         'a80454f72df35c6f52550551dd69b8521ea6a62d834396d1c7dce4c35493902e')

    def test_real_arabic_numeral_shape_is_read_without_date_or_letter_context(self):
        engine=E.TesseractEngine()
        if not engine.native_ok():
            self.skipTest('Native Tesseract is not installed')
        image=cv2.imread(str(Path(__file__).parent/'fixtures/arabic_four.png'),0)
        metric=Metrics()
        try:
            self.assertEqual(P._script_digit_read([engine],image,metric),'4')
        finally:
            metric.stop()
            engine.close()

    def test_date_recovery_requires_nearby_label_and_complete_pixel_read(self):
        value = {'text': '۱۳', 'confidence': .4, 'bbox': (100,100,340,140)}
        label = {'text': 'تاریخ:', 'confidence': .9, 'bbox': (370,100,430,150)}
        for result, expected in [({'text':'۱۴۰۴/۴/۱۲'}, '۱۴۰۴/۴/۱۲'),
                                 ({'text':'۱۴۰۴/۴/۱۲','needs_review':True}, '۱۳'),
                                 ({'text':'۱۴۰۴۴۱۲'}, '۱۳')]:
            word = dict(value)
            with patch.object(P, '_glyph_digit_ocr', return_value=result) as glyph:
                P._recover_header_numbers([word,label], np.full((1000,1000),255,np.uint8),[],None)
            self.assertEqual(word['text'], expected)
            self.assertTrue(word['needs_review'])
            self.assertTrue(glyph.call_args.kwargs['detect_separators'])
            if expected != '۱۳':
                self.assertEqual(word['raw_ocr_text'], '۱۳')
        with patch.object(P, '_glyph_digit_ocr') as glyph:
            P._recover_header_numbers([dict(value)], np.full((1000,1000),255,np.uint8),[],None)
            glyph.assert_not_called()

    def test_header_recovery_budget_and_confident_text_are_preserved(self):
        label = {'text':'شماره', 'bbox':(370,100,430,150)}
        values = [{'text':'هب','confidence':.2,'bbox':(100,100,340,140)} for _ in range(9)]
        confident = {'text':'۱۲۳/۴۵۶','confidence':.95,'bbox':(100,100,340,140)}
        with patch.object(P, '_glyph_digit_ocr', return_value={'text':'۱۲۰/۵۶۷'}) as glyph:
            P._recover_header_numbers([label,confident,*values],np.full((1000,1000),255,np.uint8),[],None)
        self.assertEqual(glyph.call_count,6)
        self.assertEqual(confident['text'],'۱۲۳/۴۵۶')
        self.assertEqual(values[-1]['text'],'هب')

    def test_years_without_two_or_three_are_verified_before_lower_priority_values(self):
        words = [{'text':'۱۲۳','bbox':(10,10,90,40)} for _ in range(16)]
        year = {'text':'۱۶۰۱','bbox':(10,10,90,40)}
        words.append(year)
        with patch.object(P,'_glyph_digit_ocr',return_value={'text':'۱۴۰۱','independent_positions':[1]}) as glyph:
            _verify_text_numbers(words,np.full((60,100),255,np.uint8),[],None)
        self.assertEqual(year['text'],'۱۴۰۱')
        self.assertEqual(glyph.call_args_list[0].kwargs["expected_text"], "۱۶۰۱")
        self.assertEqual(glyph.call_count,17)

    def test_confident_footer_number_is_not_overwritten_by_isolated_glyph(self):
        word={'text':'۴۳۱۱۱','confidence':.92,'bbox':(10,900,90,940)}
        with patch.object(P,'_glyph_digit_ocr',return_value={'text':'۴۳۱۱۹'}) as glyph:
            _verify_text_numbers([word],np.full((1000,100),255,np.uint8),[],None)
        self.assertEqual(word['text'],'۴۳۱۱۱')
        glyph.assert_called_once()
        self.assertTrue(word["needs_review"])

    def test_false_numeric_footer_read_next_to_latin_text_is_not_reinterpreted(self):
        word={'text':'۵۲۸۳','confidence':.5,'bbox':(100,900,200,940)}
        neighbour={'text':'Street','confidence':.95,'bbox':(220,900,350,940)}
        with patch.object(P,'_glyph_digit_ocr',return_value={'text':'۵۲۸۸'}) as glyph:
            _verify_text_numbers([word,neighbour],np.full((1000,500),255,np.uint8),[],None)
        self.assertEqual(word['text'],'۵۲۸۳')
        self.assertTrue(word['needs_review'])
        glyph.assert_called_once()
        self.assertEqual(word["text"], "۵۲۸۳")

    def test_slash_is_not_a_persian_one_or_a_curved_digit(self):
        slash=np.zeros((100,70),np.uint8)
        cv2.line(slash,(48,5),(18,95),255,5)
        self.assertTrue(P._is_slash(slash))
        one=np.zeros_like(slash)
        cv2.line(one,(18,5),(48,95),255,5)
        self.assertFalse(P._is_slash(one))
        cv2.circle(one,(32,25),20,255,5)
        self.assertFalse(P._is_slash(one))

    def test_repeated_low_confidence_glyph_cannot_verify_a_header_field(self):
        image = np.full((300,200),255,np.uint8)
        cv2.rectangle(image,(70,40),(115,240),0,-1)
        metric = Metrics()
        try:
            with patch.object(E,'ocr_many',return_value=[{'text':'۳','confidence':.3}]*2):
                result = _glyph_digit_ocr([],image,metric,detect_separators=True)
            self.assertTrue(result['needs_review'])
            self.assertFalse(result['confirmed_positions'])
        finally:
            metric.stop()

    def test_script_model_requires_independent_agreement_and_local_availability(self):
        engine=E.TesseractEngine()
        image=np.full((240,160),255,np.uint8)
        cv2.putText(image,'4',(20,180),cv2.FONT_HERSHEY_SIMPLEX,5,0,6)
        metric=Metrics()
        try:
            with patch.object(Path,'is_file',return_value=False), patch.object(E,'ocr_many') as read:
                self.assertIsNone(P._script_digit_read([engine],image,metric))
                read.assert_not_called()
            with patch.object(Path,'is_file',return_value=True):
                good={'text':'٤','confidence':.8}
                with patch.object(E,'ocr_many',return_value=[good,good]) as read:
                    self.assertEqual(P._script_digit_read([engine],image,metric),'4')
                    self.assertEqual(read.call_args.kwargs['langs_override'],'Arabic')
                    self.assertIn('٤',read.call_args.kwargs['whitelist'])
                with patch.object(E,'ocr_many',return_value=[good,{'text':'٦','confidence':.8}]):
                    self.assertIsNone(P._script_digit_read([engine],image,metric))
                with patch.object(E,'ocr_many',return_value=[{'text':'٤','confidence':.2}]*2):
                    self.assertIsNone(P._script_digit_read([engine],image,metric))
        finally:
            metric.stop()

    def test_confirmed_numeric_correction_preserves_original_for_review(self):
        words = [{"text": "۱۳۳۴۵", "bbox": (10, 10, 90, 40), "confidence": 0.92}]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr",
                   return_value={"text": "۱۲۳۴۵", "independent_positions": [1]}):
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(words[0]["text"], "۱۲۳۴۵")
        self.assertEqual(words[0]["raw_ocr_text"], "۱۳۳۴۵")
        self.assertTrue(words[0]["numeric_verified"])
        self.assertTrue(words[0]["needs_review"])

    def test_ambiguous_candidates_do_not_replace_the_source(self):
        for result in ({"text": "۱۲۳۴۵", "needs_review": True}, {"text": "۱۲۳"}, {"text": "?"}):
            words = [{"text": "۱۳۳۴۵", "bbox": (10, 10, 90, 40)}]
            with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value=result):
                _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
            self.assertEqual(words[0]["text"], "۱۳۳۴۵")
            self.assertTrue(words[0]["needs_review"])

    def test_prose_and_latin_identifiers_are_preserved(self):
        words = [{"text": text} for text in ("شرکت", "INV-2026-0042", "2026")]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr") as glyph:
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
            glyph.assert_not_called()

    def test_separator_positions_and_unconfirmed_digits_are_preserved(self):
        words = [{"text": "۱۳۳,۴۵۶", "bbox": (10, 10, 90, 40)}]
        reading = {"text": "۱۲۳,۴۵۲", "needs_review": True,
                   "confirmed_positions": [0, 1, 2, 3, 4, 5], "independent_positions": [1]}
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value=reading) as glyph:
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(glyph.call_args.kwargs['expected_text'], '۱۳۳,۴۵۶')
        self.assertEqual(words[0]['text'], '۱۲۳,۴۵۶')
        self.assertEqual(words[0]['raw_ocr_text'], '۱۳۳,۴۵۶')
        self.assertTrue(words[0]['needs_review'])
        self.assertFalse(words[0].get('numeric_verified', False))

    def test_numeric_recheck_never_drops_separators(self):
        words = [{"text": "۱۳۳,۴۵۶", "bbox": (10, 10, 90, 40)}]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value={'text': '۱۲۳۰۴۵۶'}):
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(words[0]['text'], '۱۳۳,۴۵۶')
        self.assertTrue(words[0]['needs_review'])

    def test_page_verification_work_is_bounded(self):
        value = "۱۲۳" * 8
        words = [{"text": value, "bbox": (10, 10, 90, 40)} for _ in range(150)]
        with patch("ocr_pipeline.pipeline._glyph_digit_ocr", return_value={"text": value}) as glyph:
            _verify_text_numbers(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(glyph.call_count, 640 // len(value))
        self.assertTrue(all(word["numeric_status"] == "deferred" and not word.get("numeric_verified")
                            for word in words[glyph.call_count:]))

    def test_digits_of_all_three_scripts_enter_the_same_verification_path(self):
        inputs = ["۶", "٤", "007", "-04.6", "۱۴:۰۵"]
        words = [{"text": value, "bbox": (10, 10, 90, 40)} for value in inputs]
        with patch.object(P, "_glyph_digit_ocr", side_effect=lambda *a, **kw: {"text": kw["expected_text"]}) as glyph:
            _verify_text_numbers(words, np.full((60,100),255,np.uint8), [], None)
        self.assertCountEqual([call.kwargs["expected_text"] for call in glyph.call_args_list], inputs)

    def test_real_isolated_persian_digits(self):
        engine = TesseractEngine("fas")
        if not engine.native_ok():
            self.skipTest("Native Tesseract is not installed")
        fixture = Path(__file__).parent / "fixtures" / "persian_digits.png"
        image = cv2.imread(str(fixture), cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(image)
        metrics = Metrics()
        try:
            result = _glyph_digit_ocr([engine], image, metrics)
        finally:
            metrics.stop()
        self.assertEqual(result["text"], "۱۲۳۴۵")
        self.assertFalse(result.get("needs_review", False))


if __name__ == "__main__":
    unittest.main()
