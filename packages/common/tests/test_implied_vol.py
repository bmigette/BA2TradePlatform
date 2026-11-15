"""Pure implied-volatility maths for the ``iv-v1`` market-condition profile.

Pinned here: Black-76 pricing/inversion, the put-call-parity forward, the ATM interpolation in
strike, the total-variance interpolation to constant 30/60-day maturities, every refusal that
makes a value MISSING (never 0, never a neighbour's value), and equality with an independent
re-implementation of the study script's maths (``ivrv_signal.py``, 2026-11-15) on a noisy chain.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from ba2_common.core import implied_vol as iv

SESSION = date(2024, 3, 1)


def _b76(F, K, T, s, is_call):
    sq = s * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    n = lambda x: 0.5 * math.erfc(-x / math.sqrt(2.0))  # noqa: E731
    c = F * n(d1) - K * n(d2)
    return c if is_call else c - F + K


def _chain(session, expiries_dte, F, vol_of, strikes_of, spread=0.02, extra=()):
    """A synthetic chain priced by Black-76 at ``vol_of(dte, K)``; bid/ask = price -/+ spread/2."""
    rows = []
    for dte in expiries_dte:
        T = dte / 365.0
        for K in strikes_of(dte):
            for is_call in (True, False):
                p = _b76(F, K, T, vol_of(dte, K), is_call)
                rows.append((session + timedelta(days=dte), is_call, K, max(p - spread / 2, 0.0),
                             p + spread / 2))
    rows.extend(extra)
    exp, call, k, bid, ask = zip(*rows)
    return iv.ChainQuotes.from_columns(list(exp), list(call), list(k), list(bid), list(ask))


# ------------------------------------------------------------------------- Black-76 primitives
def test_norm_cdf_matches_erf_identity_and_symmetry():
    for x in (-3.0, -1.0, 0.0, 0.5, 2.5):
        assert iv.norm_cdf(x) == pytest.approx(0.5 * (1 + math.erf(x / math.sqrt(2))), abs=1e-15)
        assert iv.norm_cdf(x) + iv.norm_cdf(-x) == pytest.approx(1.0, abs=1e-15)


def test_black76_put_call_parity_undiscounted():
    F, K, T, s = 100.0, 95.0, 0.25, 0.3
    c = iv.black76_price(F, K, T, s, True)
    p = iv.black76_price(F, K, T, s, False)
    assert c - p == pytest.approx(F - K, abs=1e-12)


@pytest.mark.parametrize("K,is_call,sigma", [
    (80.0, False, 0.3), (95.0, True, 0.3), (100.0, True, 0.3), (100.0, False, 0.3),
    (110.0, True, 0.3), (130.0, False, 0.3), (97.0, True, 0.08), (103.0, False, 0.08),
    (100.0, True, 0.08), (60.0, False, 1.2), (150.0, True, 1.2), (100.0, False, 1.2)])
def test_inversion_recovers_the_volatility(K, is_call, sigma):
    F, T = 100.0, 45 / 365.0
    price = iv.black76_price(F, K, T, sigma, is_call)
    got = iv.implied_vol_black76(F, K, T, price, is_call)
    assert got is not None
    assert got == pytest.approx(sigma, abs=1e-9)


def test_inversion_refuses_prices_outside_the_no_arbitrage_bounds():
    F, K, T = 100.0, 90.0, 0.1
    assert iv.implied_vol_black76(F, K, T, 10.0, True) is None          # == intrinsic
    assert iv.implied_vol_black76(F, K, T, 100.0, True) is None         # >= F (call upper bound)
    assert iv.implied_vol_black76(F, 110.0, T, 110.0, False) is None    # >= K (put upper bound)
    assert iv.implied_vol_black76(F, K, 0.0, 12.0, True) is None        # no time value possible


def test_inversion_pinned_at_a_bracket_bound_is_no_value():
    # a price that needs sigma < 0.011 (deep ITM with a hair of time value) is not an inversion
    F, K, T = 100.0, 80.0, 30 / 365.0
    p = iv.black76_price(F, K, T, 0.005, True)
    assert iv.implied_vol_black76(F, K, T, p, True) is None


# ------------------------------------------------------------------------- quote filters
def test_quote_iv_applies_the_study_filters():
    F, K, T = 100.0, 100.0, 30 / 365.0
    p = iv.black76_price(F, K, T, 0.3, True)
    assert iv.quote_iv(F, K, T, p - 0.05, p + 0.05, True) == pytest.approx(0.3, abs=1e-3)
    assert iv.quote_iv(F, K, T, 0.0, p, True) is None                  # zero bid: no two-sided quote
    assert iv.quote_iv(F, K, T, p + 0.1, p - 0.1, True) is None        # crossed
    assert iv.quote_iv(F, K, T, p * 0.4, p * 1.4, True) is None        # spread > 50% of mid
    assert iv.quote_iv(F, 40.0, T, 1.0, 1.1, False) is None            # K/F < 0.5
    assert iv.quote_iv(F, 160.0, T, 1.0, 1.1, True) is None            # K/F > 1.5
    assert iv.quote_iv(F, K, T, float("nan"), p, True) is None         # non-finite


def test_parity_forward_picks_the_strike_where_call_and_put_mids_meet():
    F, T = 101.3, 30 / 365.0
    strikes = np.array([95.0, 100.0, 105.0])
    c = np.array([iv.black76_price(F, k, T, 0.25, True) for k in strikes])
    p = np.array([iv.black76_price(F, k, T, 0.25, False) for k in strikes])
    got = iv.parity_forward(strikes, c, p)
    assert got == pytest.approx(F, abs=1e-12)
    assert iv.parity_forward(np.array([]), np.array([]), np.array([])) is None


def test_parity_forward_tie_breaks_to_the_lowest_strike():
    # two strikes with the same |C - P|: the lower one is K*
    strikes = np.array([100.0, 110.0])
    c = np.array([7.0, 2.0])
    p = np.array([2.0, 7.0])
    assert iv.parity_forward(strikes, c, p) == 105.0   # 100 + (7 - 2)


# ------------------------------------------------------------------------- ATM per expiry
def test_atm_iv_interpolates_linearly_in_strike_between_the_bracketing_strikes():
    F, dte = 102.0, 30
    vol = lambda d, K: 0.20 + 0.001 * (K - 100.0)   # noqa: E731 -- a smile slope
    ch = _chain(SESSION, [dte], F, vol, lambda d: [95.0, 100.0, 105.0, 110.0], spread=0.0002)
    pt = iv.expiry_point(ch, SESSION, SESSION + timedelta(days=dte))
    assert pt.forward == pytest.approx(F, abs=1e-6)
    want = 0.20 + 0.001 * (F - 100.0)               # linear in K between 100 and 105
    assert pt.atm_iv == pytest.approx(want, abs=2e-4)
    assert pt.how == "bracket"


def test_atm_iv_too_wide_a_bracket_falls_back_to_the_nearest_strike_within_2_5_pct():
    F, dte = 100.5, 30
    vol = lambda d, K: 0.30   # noqa: E731
    ch = _chain(SESSION, [dte], F, vol, lambda d: [80.0, 100.0, 125.0], spread=0.0002)
    pt = iv.expiry_point(ch, SESSION, SESSION + timedelta(days=dte))
    assert pt.how == "nearest" and pt.atm_iv == pytest.approx(0.30, abs=1e-4)


def test_atm_iv_with_no_strike_near_the_forward_is_missing():
    F, dte = 100.0, 30
    ch = _chain(SESSION, [dte], F, lambda d, K: 0.3, lambda d: [60.0, 140.0], spread=0.0002)
    pt = iv.expiry_point(ch, SESSION, SESSION + timedelta(days=dte))
    assert pt.atm_iv is None and pt.how == "far"


def test_ambiguous_duplicate_quotes_are_dropped_identical_ones_kept():
    exp = SESSION + timedelta(days=30)
    base = _chain(SESSION, [30], 100.0, lambda d, K: 0.3, lambda d: [95.0, 100.0, 105.0], spread=0.01)
    # an exact duplicate row changes nothing
    dup = iv.ChainQuotes.concat([base, iv.ChainQuotes.from_columns(
        [exp], [True], [100.0], [float(base.bid[(base.strike == 100.0) & base.is_call][0])],
        [float(base.ask[(base.strike == 100.0) & base.is_call][0])])])
    assert iv.expiry_point(dup, SESSION, exp) == iv.expiry_point(base, SESSION, exp)
    assert dup.digest() == base.digest()
    # a CONFLICTING duplicate makes that (type, strike) unusable, never "first row wins"
    bad = iv.ChainQuotes.concat([base, iv.ChainQuotes.from_columns([exp], [True], [100.0], [50.0], [51.0])])
    assert bad.digest() != base.digest()
    pt = iv.expiry_point(bad, SESSION, exp)
    assert pt.forward is not None   # still found from the other strikes


# ------------------------------------------------------------------------- term interpolation
def test_total_variance_interpolation():
    pts = [(20, 0.30), (45, 0.36)]
    v, br = iv.interp_total_variance(pts, 30, 7, 75)
    w1, w2 = 0.30 ** 2 * 20, 0.36 ** 2 * 45
    assert br == (20, 45)
    assert v == pytest.approx(math.sqrt((w1 + (w2 - w1) * (30 - 20) / (45 - 20)) / 30), abs=1e-15)


def test_total_variance_needs_both_sides():
    assert iv.interp_total_variance([(20, 0.3)], 30, 7, 75) == (None, None)
    assert iv.interp_total_variance([(45, 0.3), (50, 0.3)], 30, 7, 75) == (None, None)
    # an expiry exactly at the target counts on the LOW side
    v, br = iv.interp_total_variance([(30, 0.25), (40, 0.4)], 30, 7, 75)
    assert br == (30, 40) and v == pytest.approx(0.25, abs=1e-15)


def test_interpolated_total_variance_lies_between_the_endpoints():
    v, br = iv.interp_total_variance([(29, 0.9), (31, 0.05)], 30, 7, 75)
    assert br == (29, 31)
    w = v * v * 30
    assert 0.05 ** 2 * 31 <= w <= 0.9 ** 2 * 29
    # the NEAREST valid expiry on each side is used, not any other
    v2, br2 = iv.interp_total_variance([(10, 0.5), (28, 0.3), (40, 0.3), (70, 0.9)], 30, 7, 75)
    assert br2 == (28, 40)


# ------------------------------------------------------------------------- the session term
def _flat_term_chain(F=100.0, vol=lambda d, K: 0.20 + 0.001 * d, dtes=(9, 16, 23, 37, 44, 58, 72, 100)):
    return _chain(SESSION, dtes, F, vol, lambda d: [90.0, 95.0, 100.0, 105.0, 110.0], spread=0.0002)


def test_iv30_and_iv60_from_a_clean_chain():
    vol = lambda d, K: 0.20 + 0.001 * d   # noqa: E731 -- upward term structure, flat smile
    term = iv.atm_iv_term(_flat_term_chain(vol=vol), SESSION)
    # 30d: brackets 23 and 37; 60d: brackets 58 and 72
    def tv(t1, t2, target):
        w1, w2 = vol(t1, 0) ** 2 * t1, vol(t2, 0) ** 2 * t2
        return math.sqrt((w1 + (w2 - w1) * (target - t1) / (t2 - t1)) / target)
    assert term.iv30 == pytest.approx(tv(23, 37, 30), abs=2e-5)
    assert term.iv60 == pytest.approx(tv(58, 72, 60), abs=2e-5)
    assert term.iv30_bracket == (23, 37) and term.iv60_bracket == (58, 72)
    assert term.iv30_reason == "" and term.iv60_reason == ""


def test_a_failed_nearest_expiry_falls_back_to_the_next_one_on_the_same_side():
    # the 23-day expiry is listed but every quote is crossed -> the 16-day one brackets instead
    good = _flat_term_chain()
    hit = good.expiry_days == iv.date_to_days(SESSION + timedelta(days=23))
    assert hit.any()
    bid = np.where(hit, good.ask, good.bid)
    ask = np.where(hit, good.bid, good.ask)
    broken = iv.ChainQuotes(good.expiry_days, good.is_call, good.strike, bid, ask)
    term = iv.atm_iv_term(broken, SESSION)
    assert term.iv30_bracket == (16, 37)
    assert term.iv30 is not None


def test_no_bracketing_expiry_is_missing_with_a_reason():
    ch = _chain(SESSION, (5, 40, 70, 100), 100.0, lambda d, K: 0.3,
                lambda d: [90.0, 95.0, 100.0, 105.0, 110.0], spread=0.0002)
    term = iv.atm_iv_term(ch, SESSION)
    assert term.iv30 is None and "7..30" in term.iv30_reason
    assert term.iv60 is not None   # 40 and 70 bracket 60


def test_an_outlier_forward_expiry_is_excluded():
    # one expiry quoted on another basis (a 2:1 split-adjusted root): its forward is ~50
    base = _flat_term_chain()
    adj = _chain(SESSION, (23,), 50.0, lambda d, K: 0.9, lambda d: [45.0, 50.0, 55.0], spread=0.0002)
    # replace the 23-day expiry with the off-basis one
    keep = base.expiry_days != iv.date_to_days(SESSION + timedelta(days=23))
    mixed = iv.ChainQuotes.concat([base.select(keep), adj])
    term = iv.atm_iv_term(mixed, SESSION)
    assert term.iv30_bracket == (16, 37)
    assert term.outliers == 1


def test_empty_chain_and_out_of_range_expiries():
    term = iv.atm_iv_term(iv.ChainQuotes.empty(), SESSION)
    assert term.iv30 is None and term.iv60 is None and term.empty
    late = _chain(SESSION, (200, 300), 100.0, lambda d, K: 0.3, lambda d: [100.0])
    t2 = iv.atm_iv_term(late, SESSION)
    assert not t2.empty and t2.iv30 is None and "no expiry" in t2.iv30_reason


def test_crossed_and_zero_quotes_everywhere_is_missing_not_zero():
    exps, calls, ks, bids, asks = [], [], [], [], []
    for d in (9, 23, 37, 58, 72):
        for K in (95.0, 100.0, 105.0):
            for c in (True, False):
                exps.append(SESSION + timedelta(days=d)); calls.append(c); ks.append(K)
                bids.append(3.0); asks.append(2.0)          # crossed
    ch = iv.ChainQuotes.from_columns(exps, calls, ks, bids, asks)
    term = iv.atm_iv_term(ch, SESSION)
    assert term.iv30 is None and term.iv60 is None


def test_rows_of_other_sessions_and_expired_contracts_do_not_leak_in():
    """The function is handed ONE session's chain; an expiry on/before the session (dte <= 0) or
    below 7 days is not a candidate, so a chain that also carries such rows answers the same."""
    base = _flat_term_chain()
    junk = iv.ChainQuotes.from_columns([SESSION, SESSION - timedelta(days=3), SESSION + timedelta(days=3)],
                                       [True, False, True], [100.0, 100.0, 100.0], [1.0, 1.0, 1.0],
                                       [1.1, 1.1, 1.1])
    assert iv.atm_iv_term(iv.ChainQuotes.concat([base, junk]), SESSION) == iv.atm_iv_term(base, SESSION)


def test_the_result_does_not_depend_on_row_order():
    base = _flat_term_chain(vol=lambda d, K: 0.25 + 0.002 * abs(K - 100) + 0.0005 * d)
    rng = np.random.default_rng(3)
    perm = rng.permutation(len(base))
    shuffled = base.select(perm)
    assert iv.atm_iv_term(shuffled, SESSION) == iv.atm_iv_term(base, SESSION)
    assert shuffled.digest() == base.digest()


# ------------------------------------------------------------------------- study oracle
def _study_atm(rows, F, T):
    """The study's _add_civ + _atm_iv_one, re-written plainly (math.erfc for ndtr)."""
    civ_by_k = {}
    for K, is_call, bid, ask in rows:
        mid = (bid + ask) / 2.0
        ok = bid > 0 and ask >= bid and (ask - bid) <= 0.5 * mid and 0.5 * F <= K <= 1.5 * F
        intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
        upper = F if is_call else K
        ok = ok and (mid > intrinsic + 1e-6) and (mid < upper)
        if not ok:
            continue
        lo, hi = 0.01, 5.0
        for _ in range(45):
            m = 0.5 * (lo + hi)
            if _b76(F, K, T, m, is_call) > mid:
                hi = m
            else:
                lo = m
        v = 0.5 * (lo + hi)
        if v < 0.011 or v > 4.99:
            continue
        civ_by_k.setdefault(K, []).append(v)
    if not civ_by_k:
        return None
    ks = sorted(civ_by_k)
    vs = [sum(civ_by_k[k]) / len(civ_by_k[k]) for k in ks]
    j = int(np.searchsorted(ks, F))
    if 0 < j < len(ks):
        k0, k1 = ks[j - 1], ks[j]
        if (k1 - k0) / F <= 0.10:
            w = (F - k0) / (k1 - k0)
            return vs[j - 1] * (1 - w) + vs[j] * w
    i = int(np.argmin(np.abs(np.array(ks) - F)))
    if abs(ks[i] - F) / F <= 0.025:
        return vs[i]
    return None


def test_atm_iv_equals_the_study_maths_on_a_noisy_chain():
    rng = np.random.default_rng(11)
    F, dte = 187.3, 33
    T = dte / 365.0
    rows = []
    exp = SESSION + timedelta(days=dte)
    for K in np.arange(120.0, 260.0, 2.5):
        for is_call in (True, False):
            s = 0.28 + 0.0008 * abs(K - F) + rng.normal(0, 0.01)
            p = _b76(F, K, T, s, is_call)
            half = max(0.01, abs(rng.normal(0.05, 0.05)))
            bid, ask = p - half, p + half
            r = rng.random()
            if r < 0.05:
                bid, ask = ask, bid            # crossed
            elif r < 0.10:
                bid = 0.0                      # no bid
            rows.append((float(K), is_call, max(bid, 0.0), ask))
    ch = iv.ChainQuotes.from_columns([exp] * len(rows), [r[1] for r in rows], [r[0] for r in rows],
                                     [r[2] for r in rows], [r[3] for r in rows])
    pt = iv.expiry_point(ch, SESSION, exp)
    want = _study_atm(rows, pt.forward, T)
    assert want is not None
    assert pt.atm_iv == pytest.approx(want, abs=1e-12)
