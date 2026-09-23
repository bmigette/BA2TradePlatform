"""E1 (BT/live option parity plan, Part E): the adjusted -> as-traded split factor.

Option strikes are stored AS TRADED; the FMP daily cache is back-adjusted for every split up
to its fetch. ``as_traded_factor`` is the multiplier that brings an adjusted close back to the
basis a chain was struck in. The split calendars below are the real FMP ones (copied from the
local ``fmp_history/mc_stock_split__*.json`` cache) so the expected numbers are the market's.
"""
from datetime import date

import numpy as np
import pytest

from ba2_common.core.split_basis import (
    CalendarSplit, SplitBasisRefused, SymbolSplitBasis, as_traded_factor,
    resolve_symbol_split_basis,
)

NFLX = [CalendarSplit(date(2004, 2, 12), 2.0), CalendarSplit(date(2015, 7, 15), 7.0),
        CalendarSplit(date(2025, 11, 17), 10.0)]
NVDA = [CalendarSplit(date(2007, 9, 11), 1.5), CalendarSplit(date(2021, 7, 20), 4.0),
        CalendarSplit(date(2024, 6, 10), 10.0)]
#: GE's reverse 1-for-8 plus FMP's two spin-off "splits" (GEHC, GEV).
GE = [CalendarSplit(date(2021, 8, 2), 0.125), CalendarSplit(date(2023, 1, 4), 1.281),
      CalendarSplit(date(2024, 4, 2), 1.253)]
BASIS = date(2026, 9, 21)


def test_the_module_under_test_is_the_worktree_copy():
    import ba2_common.core.split_basis as m
    assert "BA2-optparity" in m.__file__, m.__file__


def test_nflx_2024_is_ten():
    assert as_traded_factor(NFLX, date(2024, 5, 1), basis_date=BASIS) == 10.0


def test_nvda_2020_is_forty():
    """The 2021-07-20 4:1 and the 2024-06-10 10:1; the 2007 split is already in a 2020 price."""
    assert as_traded_factor(NVDA, date(2020, 3, 2), basis_date=BASIS) == 40.0


def test_between_two_splits_only_the_later_one_counts():
    assert as_traded_factor(NVDA, date(2022, 1, 3), basis_date=BASIS) == 10.0


def test_on_the_ex_date_the_split_is_already_in_the_as_traded_price():
    assert as_traded_factor(NVDA, date(2024, 6, 10), basis_date=BASIS) == 1.0
    assert as_traded_factor(NVDA, date(2024, 6, 7), basis_date=BASIS) == 10.0


def test_after_the_last_split_the_factor_is_one():
    assert as_traded_factor(NFLX, date(2026, 1, 5), basis_date=BASIS) == 1.0


def test_a_split_after_the_basis_date_is_excluded():
    """The cache's adjustment stops at its basis: a split it has not seen is not in the
    adjusted close, so dividing it out would double count."""
    assert as_traded_factor(NFLX, date(2024, 5, 1), basis_date=date(2025, 11, 16)) == 1.0
    assert as_traded_factor(NFLX, date(2024, 5, 1), basis_date=date(2025, 11, 17)) == 10.0


def test_reverse_split_gives_a_factor_below_one():
    # 2021-07-01: before the 1-for-8 reverse and both spin-offs.
    assert as_traded_factor(GE, date(2021, 7, 1), basis_date=BASIS) == pytest.approx(
        0.125 * 1.281 * 1.253)
    assert as_traded_factor(GE[:1], date(2021, 7, 1), basis_date=BASIS) == 0.125


def test_an_empty_calendar_is_a_real_answer_but_a_missing_one_refuses():
    assert as_traded_factor([], date(2024, 5, 1), basis_date=BASIS) == 1.0
    with pytest.raises(SplitBasisRefused, match="no split calendar"):
        as_traded_factor(None, date(2024, 5, 1), basis_date=BASIS)


def test_a_missing_basis_date_refuses():
    with pytest.raises(SplitBasisRefused, match="basis date"):
        as_traded_factor(NFLX, date(2024, 5, 1), basis_date=None)


def test_an_unknown_ratio_inside_the_window_refuses_and_outside_it_does_not():
    cal = NFLX + [CalendarSplit(date(2020, 1, 2), float("nan"))]
    with pytest.raises(SplitBasisRefused, match="unusable ratio"):
        as_traded_factor(cal, date(2019, 6, 3), basis_date=BASIS)
    assert as_traded_factor(cal, date(2021, 6, 1), basis_date=BASIS) == 10.0


# ---- resolve_symbol_split_basis: the per-symbol basis from the cached series -------------------
def _series(first: date, last: date, split: date, factor: float, *, adjusted: bool):
    """A flat 100 series with a split on ``split``: adjusted -> no step, else a 1/factor step."""
    days = np.arange(np.datetime64(first), np.datetime64(last) + 1, dtype="datetime64[D]")
    days = days[np.is_busday(days)]
    px = np.full(len(days), 100.0)
    if not adjusted:
        px[days < np.datetime64(split)] = 100.0 * factor
    return days, px


def test_resolve_includes_a_split_the_file_is_adjusted_for():
    days, px = _series(date(2024, 1, 2), date(2026, 3, 2), date(2025, 11, 17), 10.0, adjusted=True)
    b = resolve_symbol_split_basis("NFLX", days, px, px, px, px, NFLX)
    assert isinstance(b, SymbolSplitBasis)
    assert b.basis_date == date(2026, 3, 2)
    assert b.factor(date(2024, 5, 1)) == 10.0
    assert b.factor(date(2025, 12, 1)) == 1.0


def test_resolve_excludes_a_split_after_the_last_cached_bar():
    days, px = _series(date(2024, 1, 2), date(2025, 10, 31), date(2025, 11, 17), 10.0, adjusted=True)
    b = resolve_symbol_split_basis("NFLX", days, px, px, px, px, NFLX)
    assert b.factor(date(2024, 5, 1)) == 1.0


def test_resolve_refuses_a_mixed_basis_file():
    """Appended across the split without a re-fetch: unadjusted before, adjusted after."""
    days, px = _series(date(2024, 1, 2), date(2026, 3, 2), date(2025, 11, 17), 10.0, adjusted=False)
    with pytest.raises(SplitBasisRefused, match="not verifiably on one split basis"):
        resolve_symbol_split_basis("NFLX", days, px, px, px, px, NFLX)


def test_resolve_refuses_a_missing_calendar():
    days, px = _series(date(2024, 1, 2), date(2024, 3, 1), date(2025, 11, 17), 10.0, adjusted=True)
    with pytest.raises(SplitBasisRefused, match="no split calendar"):
        resolve_symbol_split_basis("NFLX", days, px, px, px, px, None)


def test_resolve_refuses_an_undetectable_split_without_a_marker_and_accepts_it_with_one():
    cal = [CalendarSplit(date(2024, 4, 2), 1.253)]
    days, px = _series(date(2023, 1, 3), date(2025, 1, 2), date(2024, 4, 2), 1.253, adjusted=True)
    with pytest.raises(SplitBasisRefused):
        resolve_symbol_split_basis("GE", days, px, px, px, px, cal)
    b = resolve_symbol_split_basis("GE", days, px, px, px, px, cal,
                                   marker={"fetched_on_utc": "2025-01-03"})
    assert b.factor(date(2024, 1, 2)) == 1.253
