"""Carry-separated put-call-parity spot (option basis guard; user decision 2026-09-26, review
rounds 2 and 3 the same day).

The guard exists to catch SPLIT-BASIS errors: a constant multiple that scales every expiry of a
session alike. Borrow grows with time to expiry. ``carry_free_spot`` adds declared dividends
back and extrapolates the two nearest usable expiries to T = 0. What must never be cleared as
carry is a VETO: a total add-back above 2.5% of spot (half the tolerance: a dividend the chain
does not price -- a special OCC strike-adjusts -- would otherwise hide an error of its size), a
row of unknown declaration or amount, no calendar, a term structure sloping up.
"""
from datetime import date, timedelta

import pytest

from ba2_common.core.option_carry import (
    MAX_ADDBACK_FRACTION, MAX_LEVERAGE, CarryFreeSpot, DividendRow, carry_free_spot,
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
    assert MAX_ADDBACK_FRACTION == 0.025
    assert not hasattr(m, "short_dated_spot")          # the TIME measure is gone (review 3)


D = date(2023, 7, 13)


def _pts(spot, days_ratio, s=D):
    return [(s + timedelta(days=t), spot * r) for t, r in days_ratio]


def _div(ex, amount, declared=date(2000, 1, 1)):
    return DividendRow(ex, amount, declared)


def test_amc_borrow_extrapolates_back_to_the_spot():
    res = carry_free_spot(D, _pts(4.44, [(1, 0.9865), (8, 0.9178), (15, 0.8266)]), [], spot=4.44)
    assert isinstance(res, CarryFreeSpot) and res.veto is None
    assert res.spot / 4.44 == pytest.approx(0.9963, abs=0.001)


def test_a_small_declared_dividend_is_added_back():
    s = date(2022, 4, 12)
    pts = [(s + timedelta(days=38), 100.0 - 2.0 - 0.3), (s + timedelta(days=66), 100.0 - 2.0 - 0.5)]
    res = carry_free_spot(s, pts, [_div(date(2022, 4, 19), 2.0, date(2022, 2, 25))], spot=100.0)
    assert res.veto is None and res.spot == pytest.approx(100.0, abs=0.2)


def test_stla_s_7_5_percent_dividend_is_too_large_to_add_back_unverified():
    """STLA 2022-04 ($1.12 on $14.90): the chain did price it, but an unverified add-back that
    large could equally hide a basis error of its size -- refused by name."""
    s = date(2022, 4, 12)
    pts = [(s + timedelta(days=38), 14.90 * 0.9329), (s + timedelta(days=66), 14.90 * 0.9279)]
    res = carry_free_spot(s, pts, [_div(date(2022, 4, 19), 1.1221, date(2022, 2, 25))], spot=14.90)
    assert res.spot is None and "> 2.5%" in res.veto


def test_a_dividend_declared_after_the_session_is_not_added_back():
    s = date(2022, 4, 12)
    pts = [(s + timedelta(days=38), 98.0), (s + timedelta(days=66), 97.8)]
    res = carry_free_spot(s, pts, [_div(date(2022, 4, 19), 2.0, date(2022, 4, 13))], spot=100.0)
    assert res.spot / 100.0 < 0.99 and "not yet declared" in res.reason


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


# ---- an unverified add-back can never hide an error of its size (review C1 / N2) -----------
@pytest.mark.parametrize("amount,spot,label", [
    (6.85, 27.50, "ACI 2022-10-21 special, 25% of spot"),
    (12.0, 28.42, "GEN 2020-02-03 special, 42%"),
    (0.875, 5.31, "HL 2023-06-14, a preferred dividend on the common"),
    (21.67, 20.86, "AMX 2023-03-16 garbage row, 104%"),
    (2.29, 45.0, "QGEN 2026-01-08 capital repayment, 5%"),
])
def test_a_large_add_back_is_a_veto(amount, spot, label):
    s = date(2022, 10, 3)
    pts = _pts(spot, [(46, 1.0), (74, 0.99)], s)
    res = carry_free_spot(s, pts, [_div(s + timedelta(days=18), amount)], spot=spot)
    assert res.spot is None and "> 2.5%" in res.veto, label


@pytest.mark.parametrize("frac", [0.09, 0.07, 0.05])
def test_an_unpriced_special_under_ten_percent_cannot_hide_an_error_of_its_size(frac):
    """The reviewer's composites: the chain does NOT price the special (OCC adjusts strikes),
    the run's spot is off by 1 + frac. Adding the special back used to land on the wrong spot."""
    s = date(2022, 10, 3)
    true_spot = 100.0
    run_spot = true_spot * (1 + frac)
    pts = _pts(true_spot, [(46, 1.0 - 0.0002 * 46), (74, 1.0 - 0.0002 * 74)], s)
    res = carry_free_spot(s, pts, [_div(s + timedelta(days=25), run_spot * frac / (1 + frac))],
                          spot=run_spot)
    assert res.spot is None and "> 2.5%" in res.veto


def test_the_cap_is_on_the_total_added_back():
    s = date(2022, 10, 3)
    pts = _pts(100.0, [(46, 0.97), (74, 0.96)], s)
    divs = [_div(s + timedelta(days=10), 1.5), _div(s + timedelta(days=40), 1.5)]
    assert "> 2.5%" in carry_free_spot(s, pts, divs, spot=100.0).veto
    assert carry_free_spot(s, pts, divs[:1], spot=100.0).veto is None


# ---- leverage and slope (review I3, round 3) ------------------------------------------------
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


def test_a_term_structure_sloping_up_is_a_veto():
    s = date(2022, 3, 18)
    res = carry_free_spot(s, [(s + timedelta(days=5), 95.0), (s + timedelta(days=40), 97.0)],
                          [], spot=100.0)
    assert res.spot is None and "slopes up" in res.veto


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
    true_spot = 100.0
    divs = [_div(date(2022, 4, 19), 2.0)] if with_dividend else []
    dv = 2.0 if with_dividend else 0.0
    pts = [(s + timedelta(days=t), true_spot - dv - 0.02 * t) for t in (38, 66)]
    res = carry_free_spot(s, pts, divs, spot=true_spot * basis)
    assert res.veto or abs(res.spot / (true_spot * basis) - 1.0) > 0.15


def test_one_usable_expiry_cannot_separate_carry_and_says_so():
    res = carry_free_spot(D, _pts(4.44, [(8, 0.9178)]), [], spot=4.44)
    assert res.spot is None and "2 expiries" in res.reason and res.veto is None


def test_no_calendar_is_a_veto():
    res = carry_free_spot(D, _pts(4.44, [(1, 0.99), (8, 0.92)]), None, spot=4.44)
    assert res.spot is None and "no dividend calendar" in res.veto


# ---- the unexplained-drop veto (review of 5178e625..90980f18, item 3) --------------------------
_PAYER = [DividendRow(date(2024, 8, 15), 0.9, date(2024, 7, 25))]      # after both expiries


def test_a_payers_drop_the_calendar_does_not_explain_is_vetoed():
    s = date(2024, 4, 1)
    pts = [(date(2024, 4, 19), 100.0), (date(2024, 5, 17), 92.0)]      # an 8% step between them
    r = carry_free_spot(s, pts, _PAYER, spot=100.0)
    assert r.spot is None and r.veto and "not explained by the dividend calendar" in r.veto


def test_the_same_drop_explained_by_a_calendar_dividend_is_carry():
    s = date(2024, 4, 1)
    pts = [(date(2024, 4, 19), 100.0), (date(2024, 5, 17), 92.0)]
    divs = _PAYER + [DividendRow(date(2024, 5, 1), 2.0, date(2024, 3, 20))]   # 2% explained
    r = carry_free_spot(s, pts, divs, spot=100.0)
    assert r.veto and "(2.0%)" in r.veto                                     # 6% left: still vetoed
    small = [(date(2024, 4, 19), 100.0), (date(2024, 5, 17), 96.0)]         # 4% - 2% = 2%
    assert carry_free_spot(s, small, divs, spot=100.0).veto is None


def test_no_dividend_in_the_horizon_is_read_as_borrow():
    s = date(2024, 4, 1)
    pts = [(date(2024, 4, 19), 100.0), (date(2024, 5, 17), 92.0)]
    r = carry_free_spot(s, pts, [], spot=100.0)
    assert r.veto is None and r.spot is not None and r.spot > 100.0


# ---- non-payers: a third expiry must confirm the line (review r6, decision D) ------------------
def test_a_non_payer_session_is_confirmed_by_a_third_expiry_on_the_line():
    s = date(2024, 4, 1)
    pts = [(date(2024, 4, 5), 99.0), (date(2024, 4, 12), 98.0), (date(2024, 4, 19), 97.1)]
    r = carry_free_spot(s, pts, [], spot=100.0)
    assert r.spot is not None and r.linear_confirmed is True and r.expiries_used == 3


def test_a_non_payer_without_a_third_expiry_is_not_confirmed():
    s = date(2024, 4, 1)
    r = carry_free_spot(s, [(date(2024, 4, 19), 100.0), (date(2024, 5, 17), 92.0)], [], spot=100.0)
    assert r.linear_confirmed is False and "third" in r.reason


def test_a_step_between_the_first_two_expiries_is_not_on_the_line():
    """An uncalendared dividend between e1 and e2 is a STEP: the third expiry stays at e2's level
    instead of continuing the slope (a borrow fee keeps accruing)."""
    s = date(2024, 4, 1)
    pts = [(date(2024, 4, 5), 100.0), (date(2024, 4, 12), 97.0), (date(2024, 4, 19), 97.0)]
    r = carry_free_spot(s, pts, [], spot=100.0)
    assert r.linear_confirmed is False and "line" in r.reason


def test_the_third_expiry_must_lie_at_least_as_far_again():
    """A third expiry 1 day after e2 would hide a step of up to 4x the tolerance: it must lie at
    least (t2 - t1) beyond e2."""
    s = date(2024, 4, 1)
    pts = [(date(2024, 4, 5), 100.0), (date(2024, 5, 3), 92.0), (date(2024, 5, 6), 92.0)]
    r = carry_free_spot(s, pts, [], spot=100.0)
    assert r.linear_confirmed is False


def test_a_payer_is_not_asked_for_a_third_expiry():
    s = date(2024, 4, 1)
    r = carry_free_spot(s, [(date(2024, 4, 19), 100.0), (date(2024, 5, 17), 99.0)], _PAYER, spot=100.0)
    assert r.linear_confirmed is None and r.spot is not None


# ---- VERIFIED add-back: a large dividend is accepted when the same chain shows it priced ------
S0 = date(2022, 4, 12)
EX = date(2022, 4, 19)
LONG_AGO = date(2022, 2, 25)


def _chain(spot, ratios):
    """[(dte, parity/spot)] -> points; dte relative to S0."""
    return [(S0 + timedelta(days=t), spot * r) for t, r in ratios]


def test_a_priced_stla_shaped_dividend_is_verified_and_added_back():
    # 7.5% dividend: pre-ex expiry (day 5) at 1.0, post-ex expiries at 0.925
    pts = _chain(100.0, [(5, 1.0), (33, 0.925), (61, 0.925)])
    res = carry_free_spot(S0, pts, [_div(EX, 7.5, LONG_AGO)], spot=100.0)
    assert res.veto is None and res.spot is not None
    assert res.spot / 100.0 == pytest.approx(1.0, abs=0.01)


def test_the_real_stla_march_chain_is_verified_with_the_chain_s_own_step():
    """Measured 2022-03-01: pre-ex 17d 1.0018, post-ex 108d 0.9572 (step 0.67 of the $1.1221):
    the add-back follows the chain (min(1, step / D)), else it overshoots into 'slopes up'."""
    s = date(2022, 3, 1)
    pts = [(s + timedelta(days=17), 16.82 * 1.0018), (s + timedelta(days=108), 16.82 * 0.9572),
           (s + timedelta(days=199), 16.82 * 0.9438)]
    res = carry_free_spot(s, pts, [_div(EX, 1.1221, LONG_AGO)], spot=16.82)
    assert res.veto is None and res.spot / 16.82 == pytest.approx(1.0018, abs=0.01)


def test_no_pre_ex_expiry_keeps_the_veto_and_says_why():
    pts = _chain(100.0, [(10, 0.925), (33, 0.925), (61, 0.925)])        # every expiry after ex
    res = carry_free_spot(S0, pts, [_div(EX, 7.5, LONG_AGO)], spot=100.0)
    assert res.spot is None and "> 2.5%" in res.veto and "no usable expiry before" in res.veto


def test_an_unpriced_special_is_a_veto_the_step_does_not_match():
    """ACI-shaped, 25%: OCC adjusts the strikes, so pre-ex and post-ex expiries both sit at 1.0."""
    pts = _chain(27.5, [(5, 1.0), (33, 1.0), (61, 0.999)])
    res = carry_free_spot(S0, pts, [_div(EX, 6.85, LONG_AGO)], spot=27.5)
    assert res.spot is None and "> 2.5%" in res.veto and "not priced" in res.veto


@pytest.mark.parametrize("frac", [0.026, 0.03, 0.05, 0.075, 0.25])
def test_an_unpriced_special_just_above_the_cap_is_still_a_veto(frac):
    pts = _chain(100.0, [(5, 1.0), (33, 1.0), (61, 1.0)])
    res = carry_free_spot(S0, pts, [_div(EX, 100.0 * frac, LONG_AGO)], spot=100.0)
    assert res.spot is None and "not priced" in res.veto


def test_a_basis_error_plus_a_priced_dividend_keeps_the_basis_error_visible():
    pts = _chain(100.0, [(5, 0.93), (33, 0.93 - 0.07), (61, 0.93 - 0.07)])
    res = carry_free_spot(S0, pts, [_div(EX, 7.0, LONG_AGO)], spot=100.0)
    assert res.veto is None                                    # the step matches: verified
    assert res.spot / 100.0 == pytest.approx(0.93, abs=0.01)  # ... and the multiple survives
    from ba2_common.core.option_carry import MAX_ADDBACK_FRACTION as cap
    assert res.spot / 100.0 < 1.0 - (0.05 - cap)               # below the guard's CARRY_BAND_LOW


def test_a_basis_error_hiding_behind_an_unpriced_special_is_a_veto():
    pts = _chain(100.0, [(5, 0.93), (33, 0.93), (61, 0.93)])
    res = carry_free_spot(S0, pts, [_div(EX, 7.0, LONG_AGO)], spot=100.0)
    assert res.spot is None and "not priced" in res.veto


def test_a_verified_large_and_a_small_dividend_are_both_added_back():
    ex2 = S0 + timedelta(days=45)
    # day 5 pre; large 7% at ex (day 7) -> day 33 at 0.93; small 1% at day 45 -> day 61 at 0.92
    pts = _chain(100.0, [(5, 1.0), (33, 0.93), (61, 0.92)])
    divs = [_div(EX, 7.0, LONG_AGO), _div(ex2, 1.0, LONG_AGO)]
    res = carry_free_spot(S0, pts, divs, spot=100.0)
    assert res.veto is None and res.spot / 100.0 == pytest.approx(1.0, abs=0.01)


def test_a_large_dividend_declared_after_the_session_is_neither_added_back_nor_verified():
    pts = _chain(100.0, [(5, 1.0), (33, 0.925), (61, 0.925)])
    res = carry_free_spot(S0, pts, [_div(EX, 7.5, S0 + timedelta(days=1))], spot=100.0)
    # not added back (causal), and not verified either: on a payer the unexplained drop is a veto
    assert res.spot is None and "not explained by the dividend calendar (0.0%)" in res.veto


def test_an_undeclared_large_dividend_does_not_verify_a_small_declared_one():
    """The gap's expected step counts DECLARED rows only, so the not-yet-declared 7% cannot
    stand in for a priced step: the small row is judged on its own."""
    pts = _chain(100.0, [(5, 1.0), (33, 0.93), (61, 0.93)])
    divs = [_div(EX, 7.0, S0 + timedelta(days=1)), _div(EX, 3.0, LONG_AGO)]
    res = carry_free_spot(S0, pts, divs, spot=100.0)
    assert res.veto is not None and "not priced" in res.veto


def test_a_step_far_above_the_calendar_is_not_verified():
    pts = _chain(100.0, [(5, 1.0), (33, 0.80), (61, 0.80)])             # 20% step for a 7% row
    res = carry_free_spot(S0, pts, [_div(EX, 7.0, LONG_AGO)], spot=100.0)
    assert res.spot is None and "not priced" in res.veto
