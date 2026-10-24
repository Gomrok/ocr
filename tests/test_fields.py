import unittest

from ocr_pipeline.contracts import build_contract
from ocr_pipeline.fields import REGISTRY, SCHEMA, extract


class FieldTests(unittest.TestCase):
    def observations(self, lines):
        page = {
            "page": 1,
            "width": 500,
            "height": 500,
            "words": [
                {"text": text, "bbox": [0, n * 30, 300, n * 30 + 20]}
                for n, text in enumerate(lines)
            ],
        }
        return build_contract(
            "synthetic",
            [page],
            {"source_digest": "a" * 64, "model_hashes": {"fas": "b" * 64}},
        )["observations"]

    def test_explicit_schema_preserves_labels_zero_and_geometry(self):
        observations = self.observations(
            [
                "Document Number: INV-001/02",
                "Net Weight: 12.50",
                "Gross Weight: 21.50",
                "Container: CSQU3054383",
            ]
        )
        result = extract(
            observations,
            document_type=SCHEMA,
            context={
                "iso6346_applicable": True,
                "decimal_separator": ".",
                "unit": "kg",
            },
        )
        fields = {field["field_id"]: field for field in result["fields"]}
        self.assertEqual(fields["document_number"]["canonical_value"], "INV-001/02")
        self.assertEqual(fields["net_weight"]["canonical_value"], "12.50")
        self.assertEqual(fields["gross_weight"]["canonical_value"], "21.50")
        self.assertEqual(fields["net_weight"]["source_bbox"], [0, 30, 300, 50])
        self.assertEqual(fields["date"]["review_status"], "missing")
        for field in fields.values():
            self.assertNotEqual(field["review_status"], "human_confirmed")
            if field["canonical_value"] is not None:
                self.assertIsNotNone(field["source_bbox"])
                self.assertIn("raw_sha256", field["provenance"])

    def test_duplicate_label_never_selects_one_silently(self):
        result = extract(
            self.observations(["Amount: 12.00", "Amount: 21.00"]), document_type=SCHEMA
        )
        amount = next(f for f in result["fields"] if f["field_id"] == "amount")
        self.assertIsNone(amount["canonical_value"])
        self.assertEqual(amount["review_status"], "uncertain")
        self.assertEqual(len(amount["candidates"]), 2)

    def test_families_are_unsupported_not_regex_classified(self):
        for family in (
            "invoice",
            "packing_list",
            "cmr",
            "customs",
            "origin",
            "shipping",
            "bank",
            "legal",
        ):
            self.assertEqual(
                extract([], document_type=family)["status"],
                "UNSUPPORTED_DOCUMENT_SCHEMA",
            )
        self.assertEqual(len(REGISTRY), 8)
