# -*- coding: utf-8 -*-
"""بنچمارک دقت: CER/WER/Numeric/Table (بخش ۲۶)."""
from __future__ import annotations

import re


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    ref_n = re.sub(r"\s+", "", ref)
    hyp_n = re.sub(r"\s+", "", hyp)
    if not ref_n:
        return 0.0 if not hyp_n else 1.0
    return _levenshtein(ref_n, hyp_n) / len(ref_n)


def wer(ref: str, hyp: str) -> float:
    r, h = ref.split(), hyp.split()
    if not r:
        return 0.0 if not h else 1.0
    return _levenshtein(" ".join(r), " ".join(h)) / max(len(r), 1)


_NUM_RE = re.compile(r"[\d۰-۹٠-٩][\d۰-۹٠-٩,./×\-]*")


def numeric_tokens(text: str) -> list[str]:
    return _NUM_RE.findall(text)


def numeric_accuracy(ref: str, hyp: str) -> float:
    """دقت اعداد: تطبیق ترتیبی توکن‌های عددی پس از نرمال‌سازی ارقام."""
    fa2en = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    r = [t.translate(fa2en).replace(",", "").replace("٬", "")
         for t in numeric_tokens(ref)]
    h = [t.translate(fa2en).replace(",", "").replace("٬", "")
         for t in numeric_tokens(hyp)]
    if not r:
        return 1.0
    match = sum(1 for x in r if x in h)
    return match / len(r)


def table_accuracy(ref_rows: list[list[str]], got_rows: list[list[str]]) -> dict:
    """دقت سلول/سطر/ستون/ساختار (بخش ۲۶)."""
    def norm(s):
        return re.sub(r"\s+", " ", str(s)).strip()

    n_ref = len(ref_rows) * (len(ref_rows[0]) if ref_rows else 0)
    n_got = len(got_rows) * (len(got_rows[0]) if got_rows else 0)
    cell_ok = 0
    total = max(n_ref, n_got, 1)
    for i, rrow in enumerate(ref_rows):
        grow = got_rows[i] if i < len(got_rows) else []
        for j, rv in enumerate(rrow):
            gv = grow[j] if j < len(grow) else ""
            if norm(rv) == norm(gv):
                cell_ok += 1
    row_ok = sum(1 for i, rrow in enumerate(ref_rows)
                 if i < len(got_rows)
                 and [norm(c) for c in rrow] == [norm(c) for c in got_rows[i]])
    col_ok = 0
    for j in range(len(ref_rows[0]) if ref_rows else 0):
        rc = [norm(r[j]) for r in ref_rows if j < len(r)]
        gc = [norm(g[j]) for g in got_rows if j < len(g)]
        col_ok += rc == gc
    struct_ok = (len(ref_rows) == len(got_rows)
                 and (len(ref_rows[0]) if ref_rows else 0)
                 == (len(got_rows[0]) if got_rows else 0))
    return {
        "cell_accuracy": round(cell_ok / total, 4),
        "row_accuracy": round(row_ok / max(len(ref_rows), 1), 4),
        "column_accuracy": round(col_ok / max(len(ref_rows[0]) if ref_rows else 0, 1), 4),
        "structure_accuracy": 1.0 if struct_ok else 0.0,
        "cells_total": total,
        "cells_matched": cell_ok,
    }
