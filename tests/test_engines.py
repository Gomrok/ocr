import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ocr_pipeline.engines import GoogleVisionEngine, TesseractEngine, ocr_any, pick_engine


class EngineTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
