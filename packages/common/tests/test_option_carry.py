"""Carry-separated put-call-parity spot (option basis guard; user decision 2026-09-26, review
round 2 the same day).

The guard exists to catch SPLIT-BASIS errors: a constant multiple that scales every expiry of a
session alike. Borrow grows with time to expiry and fades as it nears; a cash dividend lowers
every expiry after its ex-date. ``carry_free_spot`` (TERM) adds declared dividends back and
extrapolates the two nearest expiries to T = 0; ``short_dated_spot`` (TIME) reads one 2-7 day
expiry. What must never be cleared as carry is a VETO: a special >= 10% of spot (OCC adjusts
strikes: ACI 2022-10-21 $6.85 on $27.50), a row of unknown declaration or amount, no calendar.
"""
from datetime import date, timedelta

import pytest

from ba2_common.core.option_carry import (
    MAX_LEVERAGE, CarryFreeSpot, DividendRow, carry_free_spot, short_dated_spot,
)


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


def _pts(spot, days_ratio, s=D):
    return [(s + timedelta(days=t), spot * r) for t, r in days_ratio]


def _div(ex, amount, declared=date(2000, 1, 1)):
    return DividendRow(ex, amount, declared)


def test_amc_borrow_extrapolates_back_to_the_spot():
    res = carry_free_spot(D, _pts(4.44, [(1, 0.9865), (8, 0.9178), (15, 0.8266)]), [], spot=4.44)
    assert isinstance(res, CarryFreeSpot) and res.veto is None
    assert res.spot / 4.44 == pytest.approx(0.9963, abs=0.001)


def test_stla_declared_dividend_is_added_back():
    s = date(2022, 4, 12)
    pts = [(s + timedelta(days=38), 14.90 * 0.9329), (s + timedelta(days=66), 14.90 * 0.9279)]
    divs = [_div(date(2022, 4, 19), 1.1221, date(2022, 2, 25))]           # 7.5%: still carry
    assert carry_free_spot(s, pts, divs, spot=14.90).spot / 14.90 == pytest.approx(1.0, abs=0.02)
    assert carry_free_spot(s, pts, [], spot=14.90).spot / 14.90 < 0.95


def test_a_dividend_declared_after_the_session_is_not_added_back():
    s = date(2022, 4, 12)
    pts = [(s + timedelta(days=38), 14.90 * 0.9329), (s + timedelta(days=66), 14.90 * 0.9279)]
    res = carry_free_spot(s, pts, [_div(date(2022, 4, 19), 1.1221, date(2022, 4, 13))], spot=14.90)
    assert res.spot / 14.90 < 0.95 and "not yet declared" in res.reason


def test_a_dividend_of_unknown_declaration_is_a_veto():
    s = date(2025, 12, 1)
    res = carry_free_spot(s, _pts(100.0, [(10, 0.97), (40, 0.96)], s),
                          [DividendRow(date(2025, 12, 5), 2.0, None)], spot=100.0)
    assert res.spot is None and "declaration date unknown" in res.veto


def test_a_row_without_an_adjusted_amount_is_a_veto():
    s = date(2025, 12, 1)
    res = carry_free_spot(s, _pts(100.0, [(10, 0.97), (40, 0.96)], s),
                          [DividendRow(date(2025, 12, 5), None, date(2025, 11, 1))], spot=100.0)
    assert "no split-adjusted amount" in res.veto


# ---- specials and garbage rows are never carry (review C1) ---------------------------------
@pytest.mark.parametrize("amount,spot,label", [
    (6.85, 27.50, "ACI 2022-10-21 special, 25% of spot"),
    (12.0, 28.42, "GEN 2020-02-03 special, 42%"),
    (0.875, 5.31, "HL 2023-06-14, a preferred dividend on the common"),
    (21.67, 20.86, "AMX 2023-03-16 garbage row, 104%"),
    (2.29, 22.9, "QGEN 2026-01-08 synthetic repurchase: a 0.95 split + 2.29 cash, 10%"),
])
def test_a_dividend_of_ten_percent_or_more_is_a_veto(amount, spot, label):
    s = date(2022, 10, 3)
    pts = _pts(spot, [(46, 1.0), (74, 0.99)], s)
    res = carry_free_spot(s, pts, [_div(s + timedelta(days=18), amount)], spot=spot)
    assert res.spot is None and ">= 10%" in res.veto, label
    res = short_dated_spot(s, (s + timedelta(days=5), spot), [_div(s + timedelta(days=3), amount)],
                           spot=spot)
    assert res.spot is None and ">= 10%" in res.veto, label


def test_the_aci_like_special_cannot_explain_away_a_x125_error():
    """Chain: the special is OCC-adjusted, so parity past the ex-date is NOT lowered; the run's
    spot is 1.25x. Adding $6.85 back would land at 27.5 + 6.85 = 34.35 ~ 1.25 x 27.5."""
    s = date(2022, 10, 3)
    pts = _pts(27.5, [(46, 1.0 - 0.0002 * 46), (74, 1.0 - 0.0002 * 74)], s)
    res = carry_free_spot(s, pts, [_div(date(2022, 10, 28), 6.85 * 1.25)], spot=27.5 * 1.25)
    assert res.spot is None and res.veto


# ---- leverage and slope (review I3) --------------------------------------------------------
@pytest.mark.parametrize("t1,t2", [(30, 37), (63, 91), (38, 55)])
def test_a_high_leverage_pair_is_not_extrapolated(t1, t2):
    s = date(2022, 3, 18)
    res = carry_free_spot(s, [(s + timedelta(days=t1), 102.5), (s + timedelta(days=t2), 97.5)],
                          [], spot=100.0)
    assert res.spot is None and str(MAX_LEVERAGE) in res.reason


def test_a_further_expiry_is_used_when_the_next_one_is_too_close():
    s = date(2022, 3, 18)
    res = carry_free_spot(s, [(s + timedelta(days=30), 99.0), (s + timedelta(days=37), 98.9),
                              (s + timedelta(days=65), 98.0)], [], spot=100.0)
    assert res.expiries_used == 2 and "30d and 65d" in res.reason
    assert res.spot == pytest.approx(99.0 + 30 * 1.0 / 35)


def test_a_term_structure_sloping_up_is_not_carry():
    s = date(2022, 3, 18)
    res = carry_free_spot(s, [(s + timedelta(days=5), 95.0), (s + timedelta(days=40), 97.0)],
                          [], spot=100.0)
    assert res.spot is None and "slopes up" in res.reason


def test_the_worst_case_noise_is_bounded_by_the_leverage_cap():
    s = date(2022, 3, 18)
    for t1 in range(2, 60):
        for t2 in range(t1 + 1, 200):
            res = carry_free_spot(s, [(s + timedelta(days=t1), 102.5),
                                      (s + timedelta(days=t2), 97.5)], [], spot=100.0)
            if res.spot is not None:
                assert res.spot <= 102.5 + MAX_LEVERAGE * 5.0 + 1e-9


# ---- basics --------------------------------------------------------------------------------
@pytest.mark.parametrize("basis", [2.0, 0.5, 1.25, 0.8])
@pytest.mark.parametrize("with_dividend", [False, True])
def test_a_basis_error_survives_the_carry_separation(basis, with_dividend):
    s = date(2022, 4, 12)
    true_spot = 14.90
    divs = [_div(date(2022, 4, 19), 1.1221)] if with_dividend else []
    dv = 1.1221 if with_dividend else 0.0
    pts = [(s + timedelta(days=t), true_spot - dv - 0.002 * t) for t in (38, 66)]
    res = carry_free_spot(s, pts, divs, spot=true_spot * basis)
    # refused outright (at x0.5 the $1.12 dividend is 15% of the run's spot: a veto) or still off
    assert res.veto or abs(res.spot / (true_spot * basis) - 1.0) > 0.15


def test_one_usable_expiry_cannot_separate_carry_and_says_so():
    res = carry_free_spot(D, _pts(4.44, [(8, 0.9178)]), [], spot=4.44)
    assert res.spot is None and "2 expiries" in res.reason and res.veto is None


def test_no_calendar_is_a_veto():
    for res in (carry_free_spot(D, _pts(4.44, [(1, 0.99), (8, 0.92)]), None, spot=4.44),
                short_dated_spot(D, (D + timedelta(days=3), 4.4), None, spot=4.44)):
        assert res.spot is None and "no dividend calendar" in res.veto


def test_the_time_measure_reads_only_a_2_to_7_day_expiry():
    s = date(2022, 3, 15)
    assert short_dated_spot(s, (s + timedelta(days=3), 12.42), [], spot=12.49).spot == 12.42
    assert short_dated_spot(s, (s + timedelta(days=8), 12.42), [], spot=12.49).spot is None
    assert short_dated_spot(s, (s + timedelta(days=1), 12.42), [], spot=12.49).spot is None
    res = short_dated_spot(s, (s + timedelta(days=5), 12.0), [_div(s + timedelta(days=2), 0.4)],
                           spot=12.49)
    assert res.spot == pytest.approx(12.4)
