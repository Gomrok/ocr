import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import serve
from ocr_pipeline.engines import GoogleVisionEngine, TesseractEngine
from ocr_pipeline.local_models import verify_models
from ocr_pipeline.pipeline import os_key


class OfflineContractTests(unittest.TestCase):
    def test_cloud_cannot_be_enabled_with_key_env_or_attribute(self):
        with (
            patch.dict(
                os.environ,
                {"OCR_LOCAL_ONLY": "0", "GOOGLE_API_KEY": "synthetic-secret"},
            ),
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ) as network,
        ):
            engine = GoogleVisionEngine("synthetic-secret")
            engine.available = True
            with self.assertRaisesRegex(RuntimeError, "EXTERNAL_OCR_DISABLED"):
                engine.ocr_image(b"synthetic")
            self.assertIsNone(os_key())
            self.assertFalse(hasattr(engine, "api_key"))
            network.assert_not_called()

    def test_docker_subprocess_entrypoint_fails_before_writing_image(self):
        engine = TesseractEngine()
        with patch("ocr_pipeline.engines.subprocess.run") as run:
            self.assertFalse(engine.docker_ok())
            with self.assertRaisesRegex(RuntimeError, "DOCKER_OCR_DISABLED"):
                engine._ocr_subprocess(b"synthetic", 3, None, "fas", False)
            run.assert_not_called()

    def test_model_missing_tampered_and_path_traversal_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "LOCAL_MODEL_MISSING"):
                verify_models(root, "fas")
            (root / "fas.traineddata").write_bytes(b"synthetic invalid model")
            with self.assertRaisesRegex(RuntimeError, "HASH_MISMATCH"):
                verify_models(root, "fas")
            with self.assertRaisesRegex(RuntimeError, "UNAPPROVED"):
                verify_models(root, "../fas")

    def test_storage_rejects_release_and_relative_paths(self):
        for path in (".", str(Path(serve.__file__).resolve().parent / "data")):
            with self.assertRaises(RuntimeError):
                serve.configure_storage(
                    {"OCR_DATA_DIR": path}, Path("synthetic-settings.json")
                )

    def test_private_temp_initialized_before_import(self):
        import tempfile as temp_module

        old = temp_module.tempdir
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
                settings = {
                    "OCR_DATA_DIR": str(Path(directory) / "data"),
                    "OCR_LOG_DIR": str((Path(directory) / "logs").resolve()),
                }
                log = serve.configure_storage(
                    settings, Path(directory) / "synthetic.json"
                )
                self.assertEqual(log, (Path(directory) / "logs").resolve())
                self.assertEqual(
                    Path(temp_module.gettempdir()),
                    (Path(directory) / "data" / "temp").resolve(),
                )
        finally:
            temp_module.tempdir = old

    def test_readiness_rejects_tampered_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fas.traineddata").write_bytes(b"synthetic invalid")
            engine = TesseractEngine()
            engine.tessdata_dir = root
            with patch("ocr_pipeline.engines.subprocess.run") as run:
                self.assertFalse(engine.available())
                run.assert_not_called()
