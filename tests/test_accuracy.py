import unittest
import csv
import io
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, Mock

import cv2
import numpy as np

from ocr_pipeline import engines as E, pipeline as P, tables as T
from ocr_pipeline.metrics import Metrics


class AccuracyTests(unittest.TestCase):
    def test_persian_output_preserves_values_separators_and_raw_reading(self):
        raw = 'INV-2026-004291 ۱۲۳,٤٥٦ 250.75 ۱۴۰۵/07/۰۴'
        expected = 'INV-۲۰۲۶-۰۰۴۲۹۱ ۱۲۳,۴۵۶ ۲۵۰.۷۵ ۱۴۰۵/۰۷/۰۴'
        self.assertEqual(E.display_text(raw), expected)
        self.assertEqual(E.en_digits_only(expected), 'INV-2026-004291 123,456 250.75 1405/07/04')
        table = T.build_table_model(1, ((0, 50), (0, 200)), {(0, 0): raw}, {}, 'test')
        self.assertEqual(table['cells'][0]['raw_text'], raw)
        self.assertEqual(T.rows_of(table), [[expected]])
        self.assertIn(expected.replace(',', ''), T.to_csv(table).replace(',', ''))
        self.assertIn(expected, T.to_html(table))
        self.assertIn(expected, T.markdown_table(table))

    def test_hocr_entities_nested_markup_quotes_and_zero_confidence(self):
        words = E.TesseractEngine._parse_hocr('''<div>
            <span title="bbox 1 2 30 40; x_wconf 0" class="ocrx_word">A&amp;B</span>
            <span class='ocrx_word' title='bbox 40 2 70 40; x_wconf 93'><strong>۱۲۳</strong></span>
            <span class='ocrx_word' title='bbox 80 2 100 40; x_wconf 99'>&lt;script&gt;</span>
            </div>''')
        self.assertEqual([w['text'] for w in words], ['A&B', '۱۲۳', '<script>'])
        self.assertEqual(words[0]['confidence'], 0.0)
        self.assertAlmostEqual(E.result_confidence({'words': words}), .64)

    def test_exports_do_not_execute_ocr_markup_or_spreadsheet_formulas(self):
        values = ['=1+1', '\t@SUM(A1)', '<script>alert(1)</script>', '-123']
        table = T.build_table_model(1, ((0, 50), (0, 50, 100, 150, 200)),
                                    {(0, i): value for i, value in enumerate(values)}, {}, 'test')
        row = next(csv.reader(io.StringIO(T.to_csv(table))))
        self.assertTrue(row[0].startswith("'="))
        self.assertTrue(row[1].startswith("'@"))
        self.assertEqual(row[3], '-۱۲۳')
        self.assertNotIn('<script>', T.to_html(table))
        self.assertIn('&lt;script&gt;', T.to_html(table))
        workbook = Mock()
        cells = [Mock() for _ in values]
        workbook.active.cell.side_effect = cells
        with patch.dict(sys.modules, {'openpyxl': SimpleNamespace(Workbook=lambda: workbook)}):
            T.to_xlsx(table, Path('unused-test-output.xlsx'))
        self.assertTrue(all(cell.data_type == 's' for cell in cells))

    def test_consensus_tie_uses_confidence_and_does_not_hide_disagreement(self):
        poor = {'text': 'تاریخ: ۱۳/۴', 'confidence': .5}
        good = {'text': 'تاریخ: ۱۴۰۵/۰۷/۰۴', 'confidence': .91}
        self.assertEqual(E.select_consensus([poor, good])['text'], good['text'])
        self.assertEqual(E.select_consensus([poor, poor, good])['text'], good['text'])
        self.assertTrue(E.select_consensus([poor, good])['needs_review'])
        result = E.select_consensus([{'text': '120', 'confidence': .9},
                                     {'text': '۱۲۰', 'confidence': .91}])
        self.assertEqual(result['votes'], 2)
        self.assertNotIn('needs_review', result)

    def test_english_line_recovery_does_not_replace_confident_persian_prose(self):
        secondary = {'text': 'WEIGHT 25,450 KG', 'words': [
            {'text': text, 'confidence': .96} for text in ('WEIGHT', '25,450', 'KG')]}
        self.assertTrue(E.prefer_latin_region({'confidence': .6}, secondary))
        self.assertFalse(E.prefer_latin_region({'words': [
            {'text': 'شرکت', 'confidence': .95}, {'text': '۷۷۴', 'confidence': .3}]}, secondary))

    def test_targeted_star_repair_requires_agreement_and_preserves_audit(self):
        word = {'text': '۱۲*۴۵', 'confidence': .2, 'bbox': (10, 10, 90, 40)}
        reading = {'text': '۱۲۳۴۵', 'confidence': .95}
        with patch.object(P, '_confirmed_crop_read', return_value=reading):
            P._repair_suspicious_words([word], np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(word['text'], '۱۲۳۴۵')
        self.assertEqual(word['raw_ocr_text'], '۱۲*۴۵')
        self.assertTrue(word['recognition_repaired'])
        self.assertTrue(word['needs_review'])

    def test_literal_masks_uncertain_and_incompatible_repairs_are_not_erased(self):
        for original, confidence, result in [
                ('***', .1, {'text': '123', 'confidence': .99}),
                ('*', .95, {'text': '2', 'confidence': .99}),
                ('۱۲*۴۵', .2, None),
                ('۱۲*۴۵', .2, {'text': '۹۹۹۹۹', 'confidence': .99})]:
            with self.subTest(original=original, result=result):
                word = {'text': original, 'confidence': confidence, 'bbox': (10, 10, 90, 40)}
                with patch.object(P, '_confirmed_crop_read', return_value=result), \
                        patch.object(P, '_glyph_digit_ocr', return_value={'text': ''}):
                    P._repair_suspicious_words([word], np.full((60, 100), 255, np.uint8), [], None)
                self.assertEqual(word['text'], original)
                self.assertTrue(word['needs_review'])

    def test_crop_repair_rejects_single_disagreeing_or_low_confidence_pass(self):
        crop = np.full((90, 250), 255, np.uint8)
        cv2.putText(crop, '12345', (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, 0, 2)
        metrics = Metrics()
        try:
            good = {'text': '12345', 'confidence': .95}
            for readings in ([good, None], [good, {'text': '12346', 'confidence': .99}],
                             [good, {'text': '12345', 'confidence': .4}]):
                with patch.object(E, 'ocr_many', return_value=readings):
                    self.assertIsNone(P._confirmed_crop_read([], crop, metrics))
            with patch.object(E, 'ocr_many', return_value=[good, good]):
                self.assertEqual(P._confirmed_crop_read([], crop, metrics)['text'], '12345')
        finally:
            metrics.stop()

    def test_suspicious_word_recovery_is_bounded(self):
        words = [{'text': '۱*۳', 'confidence': .2, 'bbox': (10, 10, 90, 40)} for _ in range(30)]
        with patch.object(P, '_confirmed_crop_read', return_value=None) as repair, \
                patch.object(P, '_glyph_digit_ocr', return_value={'text': ''}):
            P._repair_suspicious_words(words, np.full((60, 100), 255, np.uint8), [], None)
        self.assertEqual(repair.call_count, 16)

    def test_real_pixels_recover_a_rejected_digit_without_changing_other_digits(self):
        engine = E.TesseractEngine()
        if not engine.native_ok():
            self.skipTest('Native Tesseract is not installed')
        fixture = Path(__file__).parent / 'fixtures' / 'persian_digits.png'
        image = cv2.imread(str(fixture), cv2.IMREAD_GRAYSCALE)
        height, width = image.shape
        word = {'text': '۱۲*۴۵', 'confidence': .2, 'bbox': (0, 0, width, height)}
        metrics = Metrics()
        try:
            P._repair_suspicious_words([word], image, [engine], metrics)
        finally:
            metrics.stop()
        self.assertEqual(word['text'], '۱۲۳۴۵')
        self.assertEqual(word['raw_ocr_text'], '۱۲*۴۵')
        self.assertTrue(word['recognition_repaired'])

    def test_single_glyph_is_scaled_from_actual_ink_height(self):
        crop = np.full((600, 400), 255, np.uint8)
        cv2.rectangle(crop, (100, 100), (180, 400), 0, -1)
        metrics = Metrics()
        try:
            with patch.object(E, 'ocr_many', return_value=[{'text': '۱'}, {'text': '۱'}]) as read:
                self.assertEqual(P._glyph_digit_ocr([], crop, metrics)['text'], '۱')
            for encoded, expected in zip(read.call_args.args[1], [80, 44]):
                image = cv2.imdecode(np.frombuffer(encoded, np.uint8), 0)
                rows = np.where((image < 128).any(axis=1))[0]
                self.assertLessEqual(abs(len(rows) - expected), 2)
        finally:
            metrics.stop()

    def test_repeated_small_zeros_are_not_fives(self):
        crop = np.full((400, 600), 255, np.uint8)
        cv2.rectangle(crop, (30, 50), (100, 300), 0, -1)
        cv2.circle(crop, (230, 245), 55, 0, 12)
        cv2.circle(crop, (400, 245), 55, 0, 12)
        metrics = Metrics()
        try:
            with patch.object(E, 'ocr_many', return_value=[{'text': '۲'}, {'text': '۲'}]):
                self.assertEqual(E.en_digits_only(P._glyph_digit_ocr([], crop, metrics)['text']), '200')
        finally:
            metrics.stop()

    def test_extremely_narrow_regions_fail_safely_instead_of_zero_sized_resize(self):
        crop = np.zeros((120000, 1), np.uint8)
        metrics = Metrics()
        try:
            with patch.object(E, 'ocr_many', return_value=[]) as read:
                self.assertIsNone(P._confirmed_crop_read([], crop, metrics))
                read.assert_not_called()
            result = P._glyph_digit_ocr([], crop, metrics)
            self.assertTrue(result.get('needs_review'))
        finally:
            metrics.stop()

    def test_identical_tie_breaker_is_not_counted_as_independent_evidence(self):
        crop = np.full((40, 40), 255, np.uint8)
        cv2.rectangle(crop, (10, 10), (18, 25), 0, -1)
        metrics = Metrics()
        try:
            with patch.object(E, 'ocr_many', return_value=[{'text': '۲'}, {'text': '۳'}]) as read:
                result = P._glyph_digit_ocr([], crop, metrics, dpi=100)
            self.assertEqual(read.call_count, 1)
            self.assertTrue(result.get('needs_review'))
        finally:
            metrics.stop()


if __name__ == '__main__':
    unittest.main()
