"""Plan Part G2: the basis guard's moneyness gate.

A session whose parity pair is struck more than ``MAX_STRIKE_DISTANCE`` (10%) from the parity
spot it implies is UNEVALUABLE -- it neither passes nor refuses. The 2026-09-23 sweep's false
refusals were exactly such sessions: cheap ADRs (MUFG/SMFG/SAN in March 2020) whose only
two-sided pair at the nearest expiry was a far-OTM strike, whose "parity spot" is carry and
skew, not the underlying. The gate reads the pair's OWN parity spot, so it is basis-independent:
a real basis error (CRWD-like x4) still refuses.

The store stand-in below is the minimum ``parity_spot`` reads: one row per (contract, session).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest


def test_the_guard_under_test_is_the_worktree_copy():
    import app.services.backtest.option_basis_guard as g
    assert "BA2-optparity" in g.__file__
    assert g.WINDOW == 5 and g.TOLERANCE == 0.05 and g.MAX_STRIKE_DISTANCE == 0.10


class _Raw:
    def __init__(self, underlying, contracts, sessions):
        self.underlying = underlying
        self.c_occ = [f"{underlying}{i}" for i in range(len(contracts))]
        self.c_expiry_ord = np.array([e.toordinal() for e, _k, _c in contracts], dtype=np.int64)
        self.c_strike_f = np.array([k for _e, k, _c in contracts], dtype=np.float64)
        self.bar_ord = np.array(sorted(s.toordinal() for s in sessions), dtype=np.int64)
        self.n_rows = len(contracts) * len(sessions)
        self.date_of_ord = {s.toordinal(): s for s in sessions}


class _Store:
    """``u`` as ``parity_spot`` / ``BasisGuard`` read it. ``quotes[(session, contract)] =
    (bid, ask)``."""

    def __init__(self, underlying, contracts, quotes):
        sessions = sorted({s for s, _c in quotes})
        self.raw = _Raw(underlying, contracts, sessions)
        self.c_is_call = np.array([c for _e, _k, c in contracts], dtype=bool)
        self._row = {}
        bid, ask = [], []
        for (s, ci), (b, a) in quotes.items():
            self._row[(ci, s.toordinal())] = len(bid)
            bid.append(b)
            ask.append(a)
        self.bid = np.array(bid, dtype=np.float64)
        self.ask = np.array(ask, dtype=np.float64)
        self.close = np.full(len(bid), np.nan)

    def exact_row(self, ci, session_ord):
        return self._row.get((ci, session_ord), -1)


def _pair_store(underlying, rows):
    """``rows`` [(session, expiry, strike, parity_spot)]: one call/put pair per row, quoted so
    that K + C - P == parity_spot (mids; a 0.02-wide market)."""
    keys = sorted({(e, k) for _s, e, k, _p in rows})
    contracts = []
    for e, k in keys:
        contracts += [(e, k, True), (e, k, False)]
    quotes = {}
    for s, e, k, par in rows:
        i = 2 * keys.index((e, k))
        c = max(par - k, 0.0) + 0.05
        p = c - (par - k)
        quotes[(s, i)] = (c - 0.01, c + 0.01)
        quotes[(s, i + 1)] = (p - 0.01, p + 0.01)
    return _Store(underlying, contracts, quotes)


def _single_pair_store(underlying, expiry, rows):
    """``rows`` [(session, strike, parity_spot)] at one expiry."""
    return _pair_store(underlying, [(s, expiry, k, p) for s, k, p in rows])


@pytest.fixture(autouse=True)
def _clean():
    from app.services.backtest.option_basis_guard import clear_basis_guard_cache
    clear_basis_guard_cache()
    yield
    clear_basis_guard_cache()


def test_a_far_otm_single_pair_session_is_unevaluable():
    """No later expiry to hand over to: unevaluable."""
    import app.services.backtest.option_basis_guard as g

    s = date(2020, 3, 18)
    u = _single_pair_store("MUFG", date(2020, 5, 15), [(s, 5.0, 2.925)])
    assert g.parity_spot(u, s.toordinal()) is None
    guard = g.BasisGuard(lambda sym, d: 3.43)
    guard.check(u, "MUFG", s)                       # neither pass nor refuse
    assert guard.checks == 1 and guard.unevaluable == 1 and guard.outliers_passed == 0


def test_a_near_the_money_pair_still_answers():
    import app.services.backtest.option_basis_guard as g

    s = date(2020, 3, 2)                            # K=5 vs parity 4.575: 9.3% away
    u = _single_pair_store("MUFG", date(2020, 5, 15), [(s, 5.0, 4.575)])
    assert g.parity_spot(u, s.toordinal()) == pytest.approx(4.575)


def test_a_crwd_like_4x_offset_still_refuses():
    """Chain quoted as traded (spot ~694), the run's spot on the /4 basis: the pairs are at
    the money of their own parity spot, so the gate lets them through and the ratio refuses."""
    import app.services.backtest.option_basis_guard as g

    sessions = [date(2026, 6, d) for d in (8, 9, 10, 11, 12, 15)]
    rows = [(s, 690.0, 694.0 + i) for i, s in enumerate(sessions)]
    u = _single_pair_store("CRWD", date(2026, 7, 17), rows)
    guard = g.BasisGuard(lambda sym, d: 694.0 / 4)
    with pytest.raises(g.OptionSpotBasisMismatch, match="CRWD 2026-06-15"):
        guard.check(u, "CRWD", date(2026, 6, 15))


#: MUFG, March 2020, from the sweep (``sessions_ratio_all.csv``): (session, the single pair's
#: strike, its parity spot, the FMP close). No basis problem (factor 1) -- the refusals it
#: produced were the far-OTM pairs. The five February rows are SYNTHETIC at-the-money history
#: (parity == close), standing in for the earlier store sessions a real run's median reaches
#: back to; the March rows are the sweep's.
MUFG_2020_03 = [
    (date(2020, 2, 24), 5.0, 5.10, 5.10),
    (date(2020, 2, 25), 5.0, 5.05, 5.05),
    (date(2020, 2, 26), 5.0, 5.00, 5.00),
    (date(2020, 2, 27), 5.0, 4.95, 4.95),
    (date(2020, 2, 28), 5.0, 4.82, 4.82),
    (date(2020, 3, 2), 5.0, 4.575, 4.85),
    (date(2020, 3, 4), 5.0, 4.600, 4.74),
    (date(2020, 3, 6), 5.0, 4.450, 4.52),
    (date(2020, 3, 9), 5.0, 3.800, 4.05),
    (date(2020, 3, 10), 5.0, 4.225, 4.14),
    (date(2020, 3, 11), 5.0, 3.550, 4.11),
    (date(2020, 3, 16), 5.0, 3.300, 3.49),
    (date(2020, 3, 17), 5.0, 3.625, 3.69),
    (date(2020, 3, 18), 5.0, 2.925, 3.43),
    (date(2020, 3, 19), 5.0, 3.100, 3.42),
    (date(2020, 3, 23), 2.5, 3.125, 3.48),
    (date(2020, 3, 25), 5.0, 4.050, 3.93),
]


def _mufg_guard(monkeypatch):
    """Every session checked (SAMPLE_EVERY = 1): these tests are about the gates, not the
    sampling."""
    import app.services.backtest.option_basis_guard as g

    monkeypatch.setattr(g, "SAMPLE_EVERY", 1)

    u = _single_pair_store("MUFG", date(2020, 5, 15), [(s, k, p) for s, k, p, _c in MUFG_2020_03])
    closes = {s: c for s, _k, _p, c in MUFG_2020_03}
    return g, u, g.BasisGuard(lambda sym, d: closes.get(d))


def test_mufg_march_2020_no_longer_refuses(monkeypatch):
    g, u, guard = _mufg_guard(monkeypatch)
    for s, *_ in MUFG_2020_03:
        guard.check(u, "MUFG", s)
    # every session whose pair is > 10% from its own parity spot is unevaluable, the rest pass
    far = [s for s, k, p, _c in MUFG_2020_03 if abs(k / p - 1) > 0.10]
    assert guard.unevaluable == len(far) and len(far) == 10
    assert guard.checks == len(MUFG_2020_03)


def test_mufg_march_2020_refused_without_the_gate(monkeypatch):
    """The false refusal the gate removes (2020-03-19: median 0.906 over five sessions)."""
    g, u, guard = _mufg_guard(monkeypatch)
    monkeypatch.setattr(g, "MAX_STRIKE_DISTANCE", float("inf"))
    with pytest.raises(g.OptionSpotBasisMismatch, match="MUFG 2020-03-1"):
        for s, *_ in MUFG_2020_03:
            guard.check(u, "MUFG", s)


def test_a_far_otm_nearest_expiry_hands_over_to_the_next_one():
    import app.services.backtest.option_basis_guard as g

    s = date(2020, 3, 18)
    u = _pair_store("MUFG", [(s, date(2020, 3, 20), 5.0, 2.925),     # far OTM: skipped
                             (s, date(2020, 4, 17), 3.5, 3.40)])     # usable
    assert g.parity_spot(u, s.toordinal()) == pytest.approx(3.40)


def test_a_crwd_like_4x_offset_still_refuses_through_a_later_expiry():
    """Every nearest-expiry pair is far OTM, so every session is decided by the NEXT expiry
    -- which is at the money of the as-traded chain, and the /4 spot still refuses."""
    import app.services.backtest.option_basis_guard as g

    sessions = [date(2026, 6, d) for d in (8, 9, 10, 11, 12, 15)]
    rows = []
    for i, s in enumerate(sessions):
        rows.append((s, date(2026, 6, 19), 900.0, 694.0 + i))       # 30% OTM: skipped
        rows.append((s, date(2026, 7, 17), 690.0, 694.0 + i))
    u = _pair_store("CRWD", rows)
    assert g.parity_spot(u, sessions[-1].toordinal()) == pytest.approx(699.0)
    guard = g.BasisGuard(lambda sym, d: 694.0 / 4)
    with pytest.raises(g.OptionSpotBasisMismatch, match="CRWD 2026-06-15"):
        guard.check(u, "CRWD", date(2026, 6, 15))


def test_a_pair_quoted_wider_than_half_the_tolerance_is_never_used():
    """MUFG 2024-01-25 (real store rows): exp 2024-03-15 $10 call 0.10 x 1.30, put 0.65 x
    1.00 -> parity 9.875 against a 9.36 close (x1.055) -- the width of the market, not a basis
    error. Half-spreads 0.60 + 0.175 >> 2.5% of 9.875, so the pair is skipped."""
    import app.services.backtest.option_basis_guard as g

    s, e = date(2024, 1, 25), date(2024, 3, 15)
    contracts = [(e, 10.0, True), (e, 10.0, False)]
    u = _Store("MUFG", contracts, {(s, 0): (0.10, 1.30), (s, 1): (0.65, 1.00)})
    assert g.parity_spot(u, s.toordinal()) is None
    g.clear_basis_guard_cache()
    u2 = _Store("MUFG", contracts, {(s, 0): (0.65, 0.75), (s, 1): (1.15, 1.25)})
    assert g.parity_spot(u2, s.toordinal()) == pytest.approx(9.5)


def test_the_width_gate_prefers_a_tight_pair_over_a_wide_one_with_a_smaller_gap():
    import app.services.backtest.option_basis_guard as g

    s, e = date(2024, 1, 25), date(2024, 3, 15)
    contracts = [(e, 10.0, True), (e, 10.0, False), (e, 9.0, True), (e, 9.0, False)]
    quotes = {(s, 0): (0.10, 1.30), (s, 1): (0.65, 1.00),      # gap 0.125, but 0.10 x 1.30
              (s, 2): (0.60, 0.64), (s, 3): (0.24, 0.28)}      # gap 0.36, tight: par 9.36
    assert g.parity_spot(_Store("MUFG", contracts, quotes), s.toordinal()) == pytest.approx(9.36)


def test_a_crossed_quote_never_loosens_the_width_gate():
    """A crossed leg (ask < bid) counts as zero width, never negative: it cannot offset the
    other leg's spread. Here the put is 0.10 x 1.30 (half 0.60 > 2.5% of ~9.5); a crossed call
    reading -0.60 would cancel it and admit the pair."""
    import app.services.backtest.option_basis_guard as g

    s, e = date(2024, 1, 25), date(2024, 3, 15)
    u = _Store("MUFG", [(e, 10.0, True), (e, 10.0, False)],
               {(s, 0): (1.30, 0.10), (s, 1): (0.10, 1.30)})
    assert g._half_spread(u, 0) == 0.0 and g._half_spread(u, 1) == pytest.approx(0.60)
    assert g.parity_spot(u, s.toordinal()) is None
