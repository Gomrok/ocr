import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ocr_pipeline.engines import GoogleVisionEngine, TesseractEngine, ocr_any, pick_engine


class EngineTests(unittest.TestCase):
    def test_sparse_retry_cannot_gain_confidence_by_omitting_difficult_prose(self):
        engine = TesseractEngine()
        def result(psm, confidence, letters):
            return {'psm':psm,'text':'ک'*letters,'words':[{'text':'کلمه'}]*25,
                    'confidence':confidence}
        with patch.object(engine,'ocr_image',side_effect=[result(3,.75,1000),
                result(11,.78,950),result(6,.73,1030)]):
            answer=ocr_any([engine],b'image',region_kind='text')
        self.assertEqual(answer['psm'],6)

    def test_long_low_confidence_retry_cannot_displace_readable_paragraph(self):
        engine=TesseractEngine()
        def result(psm,confidence,letters):
            return {'psm':psm,'text':'ک'*letters,'words':[{'text':'کلمه'}]*25,
                    'confidence':confidence}
        with patch.object(engine,'ocr_image',side_effect=[result(3,.8,1000),
                result(11,.81,1000),result(6,.5,2000)]):
            answer=ocr_any([engine],b'image',region_kind='text')
        self.assertEqual(answer['psm'],3)

    def test_native_engine_is_selectable_without_docker(self):
        engine = TesseractEngine()
        with patch.object(engine, "native_ok", return_value=True), patch.object(engine, "docker_ok") as docker:
            self.assertIs(pick_engine([engine]), engine)
            docker.assert_not_called()

    def test_docker_fallback_remains_available(self):
        engine = TesseractEngine()
        with patch.object(engine, "native_ok", return_value=False), patch.object(engine, "docker_ok", return_value=True):
            self.assertIs(pick_engine([engine]), engine)

    def test_disabled_cloud_engine_is_not_retried(self):
        cloud = GoogleVisionEngine("not-a-real-key")
        cloud.available = False
        local = TesseractEngine()
        result = {"text": "متن آزمایشی", "confidence": 0.99, "words": [], "engine": "tesseract"}
        with patch.object(cloud, "ocr_image") as request, patch.object(local, "ocr_image", return_value=result):
            self.assertEqual(ocr_any([cloud, local], b"image")["text"], result["text"])
            request.assert_not_called()

    def test_native_arguments_and_hocr_contract(self):
        engine = TesseractEngine()
        engine.executable = "trusted-tesseract.exe"
        def execute(command, **kwargs):
            self.assertEqual(command[0], "trusted-tesseract.exe")
            self.assertNotIn("docker", command)
            self.assertNotIn("shell", kwargs)
            Path(command[2] + ".txt").write_text("متن آزمایشی", encoding="utf-8")
            Path(command[2] + ".hocr").write_text(
                "<span class='ocrx_word' title='bbox 10 20 80 40; x_wconf 95'>متن</span>", encoding="utf-8")
            return Mock(returncode=0)
        with patch.object(engine, "native_ok", return_value=True), patch("ocr_pipeline.engines.subprocess.run", side_effect=execute):
            result = engine.ocr_image(b"test input", psm=7)
        self.assertEqual(result["text"], "متن آزمایشی")
        self.assertEqual(result["words"][0]["confidence"], 0.95)

    def test_native_persian_whitelist_uses_utf8_configuration(self):
        engine = TesseractEngine()
        engine.executable = "trusted-tesseract.exe"
        whitelist = "۰۱۲۳۴۵۶۷۸۹0123456789"
        def execute(command, **kwargs):
            self.assertNotIn(whitelist, " ".join(command))
            self.assertEqual(Path(command[-1]).read_text("utf-8"),
                             "tessedit_char_whitelist " + whitelist + "\n")
            Path(command[2] + ".txt").write_text("۱۲۳۴۵", encoding="utf-8")
            return Mock(returncode=0)
        with patch.object(engine, "native_ok", return_value=True), patch("ocr_pipeline.engines.subprocess.run", side_effect=execute):
            self.assertEqual(engine.ocr_image(b"test", whitelist=whitelist)["text"], "۱۲۳۴۵")

    def test_numeric_whitelist_and_language_survive_all_retry_passes(self):
        engine = TesseractEngine()
        with patch.object(engine, 'ocr_image', side_effect=lambda *_a, **_k:
                          {'text': '۱', 'confidence': .2, 'words': []}) as read:
            ocr_any([engine], b'original', region_kind='glyph', variants=[b'variant'],
                    whitelist='۰۱۲۳', langs_override='fas')
        self.assertEqual(read.call_count, 4)
        for call in read.call_args_list:
            self.assertEqual(call.kwargs['whitelist'], '۰۱۲۳')
            self.assertEqual(call.kwargs['langs'], 'fas')


if __name__ == "__main__":
    unittest.main()
