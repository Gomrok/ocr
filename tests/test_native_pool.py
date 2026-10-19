import os
import struct
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ocr_pipeline import engines as E, native_pool as N, pipeline as P


class NativePoolTests(unittest.TestCase):
    png = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 12, 8)

    def fake_pool(self):
        context = Mock()
        with patch.object(N.MP, 'get_context', return_value=context), \
                patch.object(N.os, 'cpu_count', return_value=24):
            pool = N.NativePool(Path('trusted'), Path('models'))
        context.Pool.assert_called_once_with(processes=4)
        return pool, context.Pool.return_value

    def test_pool_closes_only_owned_processes_and_is_idempotent(self):
        pool, process = self.fake_pool()
        process.apply_async.return_value.get.return_value = ('text', 'hocr')
        self.assertEqual(pool.recognize(self.png, 7, None, 'fas'), ('text', 'hocr'))
        pool.close()
        pool.close()
        process.terminate.assert_called_once()
        process.join.assert_called_once()
        with self.assertRaises(RuntimeError):
            pool.recognize(self.png, 7, None, 'fas')
        self.assertEqual(process.apply_async.call_count, 1)

    def test_worker_failure_and_deadline_are_normalized_for_cli_fallback(self):
        for failure in (EOFError('worker exited'), N.MP.TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                pool, process = self.fake_pool()
                process.apply_async.return_value.get.side_effect = failure
                with patch.object(N.time, 'monotonic', side_effect=[0, 0, 91]), \
                        self.assertRaisesRegex(RuntimeError, 'worker unavailable'):
                    pool.recognize(self.png, 7, None, 'fas')
                process.terminate.assert_called_once()
                process.join.assert_called_once()

    def test_pending_recognition_exits_when_another_call_closes_pool(self):
        pool, process = self.fake_pool()

        def close_while_waiting(**_kwargs):
            pool.close()
            raise N.MP.TimeoutError()

        process.apply_async.return_value.get.side_effect = close_while_waiting
        with self.assertRaises(RuntimeError):
            pool.recognize(self.png, 7, None, 'fas')
        self.assertEqual(process.apply_async.return_value.get.call_count, 1)

    def test_invalid_input_is_rejected_before_native_decoder(self):
        pool, process = self.fake_pool()
        giant = self.png[:16] + struct.pack('>II', 5000, 5000)
        cases = [(b'not png', 7, None, 'fas'), (giant, 7, None, 'fas'),
                 (self.png, 14, None, 'fas'), (self.png, 7, '1\n2', 'fas'),
                 (self.png, 7, None, '../model'), (self.png, 7, '1' * 65, 'fas')]
        try:
            for args in cases:
                with self.subTest(args=args[1:]), self.assertRaises(ValueError):
                    pool.recognize(*args)
            process.apply_async.assert_not_called()
        finally:
            pool.close()

    def test_engine_disables_failed_pool_for_entire_document(self):
        engine = E.TesseractEngine(reuse_model=True)
        engine.executable = 'trusted-tesseract.exe'
        with patch.object(E.NativePool, 'supported', return_value=True), \
                patch.object(E, 'NativePool') as pool_class, \
                patch.object(engine, 'native_ok', return_value=True), \
                patch.object(engine, '_ocr_subprocess', return_value=('fallback', '')) as cli:
            pool_class.supported.return_value = True
            pool = pool_class.return_value
            pool.workers = 4
            pool.recognize.side_effect = RuntimeError('worker unavailable')
            for image in (b'first', b'second'):
                self.assertEqual(engine.ocr_image(image)['text'], 'fallback')
            self.assertEqual(pool_class.call_count, 1)
            self.assertEqual(pool.recognize.call_count, 1)
            self.assertEqual(cli.call_count, 2)
        engine.close()
        self.assertFalse(engine._cache)
        self.assertEqual(engine._cache_bytes, 0)

    def test_disabled_backend_never_starts_workers(self):
        with patch.object(E, 'NativePool') as pool:
            self.assertIsNone(E.TesseractEngine()._pooled_read(b'image', 7, None, 'fas'))
            pool.assert_not_called()

    def test_pipeline_closes_engine_and_document_when_processing_raises(self):
        with patch.object(P.E, 'TesseractEngine') as engine, \
                patch.object(P.pymupdf, 'open') as document, \
                patch.object(P, '_process_document', side_effect=ValueError('failed')), \
                patch.dict(os.environ, {'OCR_PERSISTENT_ENGINE': '0'}):
            with self.assertRaises(ValueError):
                P.process_pdf(Path('input.pdf'), Path('outputs'))
            self.assertFalse(engine.call_args.kwargs['reuse_model'])
            engine.return_value.__exit__.assert_called_once()
            document.return_value.__exit__.assert_called_once()

    def test_real_native_model_reuse_matches_cli_and_resets_whitelist(self):
        original = E.TesseractEngine()
        if not original.native_ok() or not N.NativePool.supported(original.executable):
            self.skipTest('Installed native Tesseract C API is unavailable')
        fixture = (Path(__file__).parent / 'fixtures/persian_digits.png').read_bytes()
        # One worker forces successive calls through the same model handle.
        with patch.object(N.os, 'cpu_count', return_value=2):
            pool = N.NativePool(Path(original.executable).parent, original.tessdata_dir.resolve())
        try:
            for psm, whitelist, lang in [(7, None, 'fas'), (10, '۰۱۲۳۴۵۶۷۸۹0123456789', 'fas'),
                                         (3, None, 'eng'), (13, None, 'fas'), (7, None, 'fas')]:
                text, hocr = pool.recognize(fixture, psm, whitelist, lang)
                reference = original.ocr_image(fixture, psm=psm, whitelist=whitelist, langs=lang)
                self.assertEqual(text, reference['text'])
                self.assertEqual(original._parse_hocr(hocr), reference['words'])
        finally:
            pool.close()
            original.close()


if __name__ == '__main__':
    unittest.main()
