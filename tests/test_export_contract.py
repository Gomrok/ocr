import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ocr_pipeline import tables as T


class ExportContractTests(unittest.TestCase):
    def test_optional_excel_preserves_other_exports_and_declares_unavailable(self):
        table = T.build_table_model(
            1, ((0, 50), (0, 100)), {(0, 0): "۰۰۱۲"}, {}, "synthetic"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                T, "to_xlsx", side_effect=ModuleNotFoundError("synthetic_dependency")
            ),
        ):
            paths = T.write_table_outputs(
                table, Path(directory), page_no=1, xlsx_required=False
            )
            self.assertEqual(
                {Path(p).suffix for p in paths}, {".json", ".csv", ".html"}
            )
            payload = json.loads(
                (Path(directory) / "page_001_table_001.json").read_text("utf-8")
            )
            self.assertEqual(payload["export_status"]["xlsx"]["status"], "unavailable")
            self.assertEqual(payload["cells"][0]["raw_text"], "۰۰۱۲")
            self.assertEqual(
                payload["export_status"]["xlsx"]["reason"], "BLOCKED_DEPENDENCY"
            )

    def test_required_excel_contract_remains_strict(self):
        table = T.build_table_model(
            1, ((0, 50), (0, 100)), {(0, 0): "۰۰۱۲"}, {}, "synthetic"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(T, "to_xlsx", side_effect=ImportError("synthetic")),
            self.assertRaises(ImportError),
        ):
            T.write_table_outputs(table, Path(directory), page_no=1)

    def test_optional_does_not_hide_disk_or_serialization_failure(self):
        table = T.build_table_model(
            1, ((0, 50), (0, 100)), {(0, 0): "text"}, {}, "synthetic"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(T, "to_xlsx", side_effect=OSError("synthetic")),
            self.assertRaises(OSError),
        ):
            T.write_table_outputs(
                table, Path(directory), page_no=1, xlsx_required=False
            )
