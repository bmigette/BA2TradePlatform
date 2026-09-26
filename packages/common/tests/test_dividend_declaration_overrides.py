"""Operator-maintained declaration dates for undated FMP dividend rows (2026-09-26).

QGEN's 2026-01-08 capital repayment ($2.29) has an empty FMP declarationDate; QIAGEN fixed the
amount in its 2025-12-18 announcement (SEC 6-K exhibit 99.1, press release). The entry fills
that date ONLY where FMP has none; a vendor date that differs, a missing row or a changed
amount refuses.
"""
from datetime import date

import pytest

from ba2_common.core import dividend_declaration_overrides as D


def _in_this_checkout(module_file) -> bool:
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[2]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False


QGEN_ROWS = [(date(2025, 7, 2), 0.25, date(2025, 5, 7)),
             (date(2026, 1, 8), 2.29, None),
             (date(2026, 7, 7), 0.35, date(2026, 5, 11))]


def test_the_module_under_test_is_the_worktree_copy():
    assert _in_this_checkout(D.__file__), D.__file__


def test_the_shipped_qgen_entry_is_the_sourced_one():
    [e] = [o for o in D.DECLARATION_OVERRIDES if o.symbol == "QGEN"]
    assert (e.ex_date, e.declared, e.amount) == (date(2026, 1, 8), date(2025, 12, 18), 2.29)
    assert "sec.gov" in e.evidence and "nasdaq.com" in e.evidence
    assert D.DECLARATION_OVERRIDES_VERSION >= "2026-09-26.1"


def test_it_fills_only_the_undated_row():
    assert D.apply_declaration_overrides("QGEN", QGEN_ROWS) == [
        QGEN_ROWS[0], (date(2026, 1, 8), 2.29, date(2025, 12, 18)), QGEN_ROWS[2]]


def test_other_symbols_are_untouched():
    rows = [(date(2026, 1, 8), 2.29, None)]
    assert D.apply_declaration_overrides("STLA", rows) == rows


def test_the_same_vendor_date_is_accepted_and_a_different_one_refuses():
    same = [(date(2026, 1, 8), 2.29, date(2025, 12, 18))]
    assert D.apply_declaration_overrides("QGEN", same) == same
    with pytest.raises(D.DeclarationOverrideConflict, match="never replaces a vendor date"):
        D.apply_declaration_overrides("QGEN", [(date(2026, 1, 8), 2.29, date(2025, 12, 19))])


def test_a_changed_amount_or_a_missing_row_refuses():
    with pytest.raises(D.DeclarationOverrideConflict, match="recorded against 2.29"):
        D.apply_declaration_overrides("QGEN", [(date(2026, 1, 8), 2.40, None)])
    with pytest.raises(D.DeclarationOverrideConflict, match="name no row"):
        D.apply_declaration_overrides("QGEN", [QGEN_ROWS[0]])


def test_duplicates_and_impossible_entries_are_refused():
    e = D.DeclarationOverride("X", date(2024, 6, 3), date(2024, 5, 1), 1.0, "t")
    with pytest.raises(ValueError, match="duplicate"):
        D.apply_declaration_overrides("X", [], overrides=(e, e))
    with pytest.raises(ValueError, match="after the ex-date"):
        D.DeclarationOverride("X", date(2024, 6, 3), date(2024, 6, 4), 1.0, "t")
