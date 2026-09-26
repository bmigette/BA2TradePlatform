"""Carry-separated put-call-parity spot (option basis guard, user decision 2026-09-26).

The guard exists to catch SPLIT-BASIS errors: a constant multiple (x0.5, x2, x1.25, a missed
spin-off) that scales every expiry of a session alike. Carry does not look like that:

* a hard-to-borrow fee lowers the parity spot by an amount that GROWS with time to expiry
  (AMC 2023-07-13: 1d 0.9865, 8d 0.9178, 15d 0.8266, 36d 0.7568 of the spot);
* a cash dividend lowers the parity spot of every expiry after its ex-date by the dividend
  (STLA 2022-04-12, ex 2022-04-19 of $1.12 on $14.90: 38d 0.9329, 66d 0.9279, 157d 0.9312).

``carry_free_spot`` adds the known dividends back to each expiry's parity spot and extrapolates
the two nearest expiries linearly to T = 0. A basis error survives both steps.
"""
from datetime import date, timedelta

import pytest

from ba2_common.core.option_carry import CarryFreeSpot, carry_free_spot


def _in_this_checkout(module_file) -> bool:
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[2]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False


def test_the_module_under_test_is_the_worktree_copy():
    import ba2_common.core.option_carry as m
    assert _in_this_checkout(m.__file__), m.__file__


D = date(2023, 7, 13)


def _pts(spot, days_ratio):
    return [(D + timedelta(days=t), spot * r) for t, r in days_ratio]


def test_amc_borrow_extrapolates_back_to_the_spot():
    # AMC 2023-07-13, as-traded spot 4.44 (real parity / spot ratios of the store)
    res = carry_free_spot(D, _pts(4.44, [(1, 0.9865), (8, 0.9178), (15, 0.8266)]), [])
    assert isinstance(res, CarryFreeSpot)
    assert res.spot / 4.44 == pytest.approx(0.9963, abs=0.001)
    assert res.expiries_used == 2


def test_stla_dividend_is_added_back():
    s = date(2022, 4, 12)
    pts = [(s + timedelta(days=38), 14.90 * 0.9329), (s + timedelta(days=66), 14.90 * 0.9279)]
    divs = [(date(2022, 4, 19), 1.1221)]
    assert carry_free_spot(s, pts, divs).spot / 14.90 == pytest.approx(1.0, abs=0.02)
    # without the dividend the extrapolation cannot remove a step that every expiry shares
    assert carry_free_spot(s, pts, []).spot / 14.90 < 0.95


def test_a_dividend_between_the_two_expiries_is_added_to_the_later_one_only():
    s = date(2024, 3, 1)
    pts = [(date(2024, 3, 8), 100.0), (date(2024, 3, 29), 97.0)]
    res = carry_free_spot(s, pts, [(date(2024, 3, 15), 3.0), (date(2023, 12, 1), 2.0)])
    assert res.spot == pytest.approx(100.0)


@pytest.mark.parametrize("basis", [2.0, 0.5, 1.25, 0.8])
@pytest.mark.parametrize("with_dividend", [False, True])
def test_a_basis_error_survives_the_carry_separation(basis, with_dividend):
    s = date(2022, 4, 12)
    true_spot = 14.90
    divs = [(date(2022, 4, 19), 1.1221)] if with_dividend else []
    dv = 1.1221 if with_dividend else 0.0
    # the chain is as traded; the run's spot is off by ``basis`` (a split missing from it)
    pts = [(s + timedelta(days=t), true_spot - dv - 0.002 * t) for t in (38, 66)]
    res = carry_free_spot(s, pts, divs)
    ratio = res.spot / (true_spot * basis)
    assert abs(ratio - 1.0) > 0.15


def test_one_usable_expiry_cannot_separate_carry_and_says_so():
    res = carry_free_spot(D, _pts(4.44, [(8, 0.9178)]), [])
    assert res.spot is None and "2 expiries" in res.reason


def test_unknown_dividends_are_named_in_the_reason():
    res = carry_free_spot(D, _pts(4.44, [(1, 0.9865), (8, 0.9178)]), None)
    assert res.spot is not None and "dividend calendar unavailable" in res.reason


def test_expiries_on_or_before_the_session_are_ignored():
    res = carry_free_spot(D, [(D, 1.0), (D + timedelta(days=7), 100.0),
                              (D + timedelta(days=14), 99.0)], [])
    assert res.spot == pytest.approx(101.0)
