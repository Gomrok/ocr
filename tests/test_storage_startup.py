import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import serve


class StorageStartupTests(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory(prefix="ocr_startup_test_")
        self.root = Path(self.private.name).resolve()
        self.settings = {
            "OCR_DATA_DIR": str(self.root / "data"),
            "OCR_TEMP_DIR": str(self.root / "temp"),
            "OCR_LOG_DIR": str(self.root / "logs"),
        }
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.cached_temp = tempfile.tempdir
        self.bytecode = sys.dont_write_bytecode

    def tearDown(self):
        tempfile.tempdir = self.cached_temp
        sys.dont_write_bytecode = self.bytecode
        self.environment.stop()
        self.private.cleanup()

    def test_explicit_storage_replaces_cached_temp_and_reaches_child(self):
        tempfile.tempdir = str(self.root / "old-temp")
        with patch.object(
            serve.shutil, "disk_usage", return_value=SimpleNamespace(free=8 * 1024**3)
        ):
            log_dir = serve.configure_storage(
                self.settings, self.root / "settings.json"
            )
        self.assertEqual(log_dir, self.root / "logs")
        self.assertEqual(Path(tempfile.gettempdir()), self.root / "temp")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import json,tempfile; from pathlib import Path; "
                    "f=tempfile.NamedTemporaryFile(); "
                    "print(json.dumps({'temp':tempfile.gettempdir(),'file':str(Path(f.name).parent)})); f.close()"
                ),
            ],
            cwd=self.root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        child = json.loads(result.stdout)
        self.assertEqual(Path(child["temp"]), self.root / "temp")
        self.assertEqual(Path(child["file"]), self.root / "temp")

    def test_missing_relative_or_release_storage_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "explicit"):
            serve.configure_storage({}, self.root / "settings.json")
        for path in (
            "relative/data",
            str(Path(serve.__file__).resolve().parent / "uploads"),
        ):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                serve.configure_storage(
                    {**self.settings, "OCR_DATA_DIR": path}, self.root / "settings.json"
                )

    def test_low_space_in_temporary_volume_is_not_hidden_by_spacious_data_volume(self):
        def free(path):
            return SimpleNamespace(
                free=1 if Path(path) == self.root / "temp" else 8 * 1024**3
            )

        with (
            patch.object(serve.shutil, "disk_usage", side_effect=free),
            self.assertRaisesRegex(RuntimeError, "OCR_TEMP_DIR.*safety reserve"),
        ):
            serve.configure_storage(self.settings, self.root / "settings.json")

    def test_service_write_failure_is_not_swallowed(self):
        with (
            patch.object(
                serve.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=8 * 1024**3),
            ),
            patch.object(
                serve.tempfile, "TemporaryFile", side_effect=PermissionError("denied")
            ),
            self.assertRaises(PermissionError),
        ):
            serve.configure_storage(self.settings, self.root / "settings.json")
