import copy
import unittest
from decimal import Decimal as D

from ocr_pipeline import validators as V


class ValidatorTests(unittest.TestCase):
    def test_container_examples_and_single_digit_mutations(self):
        value = "CSQU3054383"
        self.assertEqual(V.container(value, iso6346_applicable=True)["result"], "valid")
        for n in "012456789":
            self.assertEqual(
                V.container(value[:-1] + n, iso6346_applicable=True)["result"],
                "invalid",
            )
        self.assertEqual(V.container(value)["result"], "unverified_context")
        self.assertEqual(
            V.container("CSQA3054383", iso6346_applicable=True)["result"], "invalid"
        )
        self.assertEqual(value, "CSQU3054383")

    def test_hs_context_length_and_leading_zero_preserved(self):
        self.assertEqual(V.hs("010310")["result"], "unverified_context")
        args = {"country": "synthetic", "document_type": "synthetic", "edition": "test"}
        self.assertEqual(V.hs("010310", **args)["result"], "format_valid")
        self.assertEqual(V.hs("01031000", **args)["result"], "unverified_context")
        self.assertEqual(
            V.hs("01031000", national_length=8, **args)["result"], "format_valid"
        )
        self.assertEqual(V.hs("01O310", **args)["result"], "invalid")

    def test_explicit_calendar_and_leap_year(self):
        self.assertEqual(V.date("2024/02/29", calendar="gregorian")["result"], "valid")
        self.assertEqual(
            V.date("2023/02/29", calendar="gregorian")["result"], "invalid"
        )
        self.assertEqual(V.date("1405/01/01")["result"], "unverified_context")
        self.assertEqual(
            V.date("1405/01/01", calendar="jalali")["result"], "BLOCKED_DEPENDENCY"
        )

    def test_decimal_locale_and_adversarial_forms(self):
        self.assertIsNone(V.parse_decimal("1,234")[0])
        self.assertEqual(
            V.parse_decimal("۱٬۲۳۴٫۵۰", decimal_separator="٫", grouping_separator="٬")[
                0
            ],
            D("1234.50"),
        )
        self.assertEqual(
            V.parse_decimal("-001.20", decimal_separator=".")[0], D("-1.20")
        )
        for text in ("NaN", "1e3", "Infinity", "12,34.00", "1.2.3", "=1+2", "1" * 129):
            self.assertIsNone(
                V.parse_decimal(text, decimal_separator=".", grouping_separator=",")[0]
            )

    def test_weights_and_total_require_complete_contract(self):
        self.assertEqual(
            V.weights(D(20), D(10), net_unit="kg", gross_unit="kg")["result"], "invalid"
        )
        self.assertEqual(
            V.weights(D(10), D(20), net_unit="kg", gross_unit="lb")["result"],
            "unverified_context",
        )
        self.assertEqual(V.row_total(D(2), D(3), D(6))["result"], "not_computed")
        kwargs = {
            "discount": D(1),
            "charges": D("0.5"),
            "tax": D("0.5"),
            "tolerance": D("0.01"),
            "currency": "synthetic",
        }
        self.assertEqual(V.row_total(D(2), D(3), D(6), **kwargs)["result"], "valid")
        self.assertEqual(V.row_total(D(2), D(3), D(7), **kwargs)["result"], "invalid")
        self.assertEqual(
            V.row_total(2.0, D(3), D(6), **kwargs)["result"], "not_computed"
        )

    def test_cross_document_scope_and_no_mutation(self):
        fields = [
            {
                "case_id": "synthetic",
                "document_id": doc,
                "field_id": "container",
                "canonical_value": value,
            }
            for doc, value in [("a", "001"), ("b", "002")]
        ]
        before = copy.deepcopy(fields)
        self.assertEqual(V.cross_document(fields)["result"], "unverified_context")
        self.assertEqual(
            V.cross_document(fields, authorized_case_id="synthetic")["result"],
            "inconsistent",
        )
        self.assertEqual(fields, before)
        self.assertEqual(
            V.registered_identifier("anything")["result"], "unverified_context"
        )
