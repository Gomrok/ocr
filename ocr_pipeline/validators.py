"""Pure checks. No correction, network, persistence, or human-confirmation state."""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation, localcontext

from .benchmark import normalize_evaluation


def result(rule, state, evidence=None, context=None):
    return {
        "rule_id": rule,
        "version": 1,
        "applicable_context": context or {},
        "result": state,
        "evidence": evidence or {},
        "limit": "validation_only_not_human_confirmation",
    }


def container(value: str, *, iso6346_applicable: bool | None = None) -> dict:
    rule = "iso6346_check_digit"
    if iso6346_applicable is not True:
        return result(rule, "unverified_context")
    text = normalize_evaluation(value)
    if not re.fullmatch(r"[A-Z]{3}[UJZ][0-9]{7}", text):
        return result(rule, "invalid", {"format_valid": False})
    letters = dict(
        zip(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            (
                10,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
                31,
                32,
                34,
                35,
                36,
                37,
                38,
            ),
        )
    )
    total = sum(
        (int(char) if char.isdigit() else letters[char]) * 2**i
        for i, char in enumerate(text[:10])
    )
    valid = (total % 11) % 10 == int(text[-1])
    return result(
        rule,
        "valid" if valid else "invalid",
        {
            "format_valid": True,
            "check_digit_valid": valid,
            "owner_registration_checked": False,
        },
        {"standard": "ISO 6346", "scope": "identification_only"},
    )


def hs(
    value: str, *, country=None, document_type=None, edition=None, national_length=None
) -> dict:
    text = normalize_evaluation(value)
    context = {"country": country, "document_type": document_type, "edition": edition}
    structure = bool(re.fullmatch(r"[0-9]{6,}", text))
    if not structure:
        return result("hs_structure", "invalid", {"base_format_valid": False}, context)
    if not all(context.values()) or len(text) > 6 and national_length is None:
        return result(
            "hs_structure", "unverified_context", {"base_format_valid": True}, context
        )
    valid = len(text) == (national_length or 6)
    # This is a shape check, never proof of membership in a tariff edition.
    return result(
        "hs_structure",
        "format_valid" if valid else "invalid",
        {"length_valid": valid, "nomenclature_membership": "NOT_MEASURED"},
        context,
    )


def date(value: str, *, calendar="unknown") -> dict:
    if calendar == "jalali":
        return result(
            "calendar_date", "BLOCKED_DEPENDENCY", context={"calendar": calendar}
        )
    if calendar != "gregorian":
        return result(
            "calendar_date", "unverified_context", context={"calendar": "unknown"}
        )
    text = normalize_evaluation(value)
    valid = False
    match = re.fullmatch(r"([0-9]{4})[-/]([0-9]{2})[-/]([0-9]{2})", text)
    if match:
        try:
            datetime.date(*(int(part) for part in match.groups()))
            valid = True
        except ValueError:
            pass
    return result(
        "calendar_date", "valid" if valid else "invalid", context={"calendar": calendar}
    )


def parse_decimal(value: str, *, decimal_separator=None, grouping_separator=None):
    """Return a derived Decimal only with an explicit separator contract."""
    text = normalize_evaluation(value)
    if len(text) > 128 or decimal_separator not in (".", ",", "٫"):
        return None, result(
            "decimal_parse", "uncertain", {"reason": "missing_or_invalid_locale"}
        )
    if (
        grouping_separator not in (None, ",", ".", "٬", " ")
        or grouping_separator == decimal_separator
    ):
        return None, result(
            "decimal_parse", "uncertain", {"reason": "ambiguous_separators"}
        )
    dec = re.escape(decimal_separator)
    integer = r"[0-9]+"
    if grouping_separator:
        group = re.escape(grouping_separator)
        integer = rf"(?:[0-9]+|[0-9]{{1,3}}(?:{group}[0-9]{{3}})+)"
    if not re.fullmatch(rf"[-+]?{integer}(?:{dec}[0-9]+)?", text):
        return None, result("decimal_parse", "invalid", {"reason": "format"})
    normalized = text.replace(grouping_separator, "") if grouping_separator else text
    try:
        number = Decimal(normalized.replace(decimal_separator, "."))
    except InvalidOperation:
        return None, result("decimal_parse", "invalid")
    return number, result(
        "decimal_parse",
        "format_valid",
        context={
            "decimal_separator": decimal_separator,
            "grouping_separator": grouping_separator,
        },
    )


def weights(net, gross, *, net_unit=None, gross_unit=None):
    if not all(
        isinstance(value, Decimal) and value.is_finite() for value in (net, gross)
    ):
        return result("net_gross", "not_computed", {"reason": "missing_or_non_decimal"})
    if not net_unit or net_unit != gross_unit:
        return result(
            "net_gross", "unverified_context", {"reason": "unit_mismatch_or_missing"}
        )
    return result(
        "net_gross",
        "valid" if Decimal(0) <= net <= gross else "invalid",
        context={"unit": net_unit},
    )


def row_total(
    quantity,
    unit_price,
    total,
    *,
    discount=None,
    charges=None,
    tax=None,
    tolerance=None,
    currency=None,
):
    values = (quantity, unit_price, total, discount, charges, tax, tolerance)
    if not currency or not all(
        isinstance(v, Decimal) and v.is_finite() for v in values
    ):
        return result(
            "row_total", "not_computed", {"reason": "incomplete_explicit_contract"}
        )
    if any(v < 0 for v in values):
        return result(
            "row_total", "invalid", {"reason": "negative_amount_or_tolerance"}
        )
    if any(
        len(v.as_tuple().digits) > 128 or abs(v.as_tuple().exponent) > 128
        for v in values
    ):
        return result(
            "row_total", "not_computed", {"reason": "precision_budget_exceeded"}
        )
    with localcontext() as arithmetic:
        arithmetic.prec = (
            sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent) for v in values)
            + 32
        )
        difference = abs(quantity * unit_price - discount + charges + tax - total)
    return result(
        "row_total",
        "valid" if difference <= tolerance else "invalid",
        {"within_tolerance": difference <= tolerance},
        {
            "currency": currency,
            "absolute_tolerance": str(tolerance),
            "discount_kind": "absolute",
        },
    )


def registered_identifier(value, *, schema=None, rules=None):
    # rules are caller-owned, local, reviewed schema contracts, never user regex input.
    rule = (rules or {}).get(schema)
    if not rule or not all(
        key in rule for key in ("rule_id", "version", "pattern", "reference")
    ):
        return result("registered_identifier", "unverified_context")
    checked = result(
        rule["rule_id"],
        "format_valid" if re.fullmatch(rule["pattern"], value) else "invalid",
        context={"schema": schema, "reference": rule["reference"]},
    )
    checked["version"] = rule["version"]
    return checked


def cross_document(fields, *, authorized_case_id=None):
    if not authorized_case_id or any(
        f.get("case_id") != authorized_case_id for f in fields
    ):
        return result(
            "cross_document",
            "unverified_context",
            {"reason": "case_scope_not_established"},
        )
    if len({f.get("document_id") for f in fields if f.get("document_id")}) < 2:
        return result(
            "cross_document", "not_computed", {"reason": "insufficient_documents"}
        )
    groups = {}
    for field in fields:
        if not field.get("field_id") or field.get("canonical_value") is None:
            return result("cross_document", "not_computed", {"reason": "missing_field"})
        key = (field["field_id"], field.get("item_id"))
        groups.setdefault(key, []).append(field)
    comparable = [
        group
        for group in groups.values()
        if len({f.get("document_id") for f in group}) >= 2
    ]
    if not comparable:
        return result("cross_document", "not_computed", {"reason": "no_shared_fields"})
    if any(
        len({f.get("unit") for f in group}) > 1
        or len({f.get("currency") for f in group}) > 1
        for group in comparable
    ):
        return result(
            "cross_document",
            "unverified_context",
            {"reason": "unit_or_currency_mismatch"},
        )
    conflicts = sum(
        len({str(f["canonical_value"]) for f in group}) > 1 for group in comparable
    )
    return result(
        "cross_document",
        "inconsistent" if conflicts else "consistent",
        {"conflicting_groups": conflicts, "compared_groups": len(comparable)},
        {"case_scoped": True},
    )
