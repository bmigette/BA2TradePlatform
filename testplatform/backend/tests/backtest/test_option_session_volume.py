"""BT/live option parity, Part B: a backtest chain's VOLUME is the data session's, nothing older.

Plan: ``docs/plans/2026-09-22-bt-live-option-parity.md`` Tasks B2 + B4.

A backtest bar D decides with data through D's close, so its data session is D
(``decision_data_session(backtest_decision_label(D)) == D``). Before this change both option
stores built a chain row from the contract's LATEST row on or before D and carried THAT row's
volume, so a contract that last traded on D-3 reported D-3's volume as if it were D's
liquidity -- and ``option_min_volume`` (25 on every grid run) passed it. Live reads the
session's own bar. The rule both paths now share is ``ba2_common.core.option_session
.session_volume``: the bar dated exactly the data session, else 0.

What deliberately did NOT change (user decision 2026-09-22): bid/ask/last and the greeks still
come from the latest row on or before D. How often that row is older than the data session is
MEASURED instead (``chain_staleness``), so the cost of that choice is visible per run.

Also here: ``rho`` (B4), on the parquet store's Black-Scholes greeks, in the convention the
other greeks use (per 1 percentage point of rate, like vega per vol point).

Run from testplatform/backend:
    python -m pytest tests/backtest/test_option_session_volume.py -q
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
from ba2_common.core.types import OptionRight

from app.services.backtest.option_greeks import compute_iv_and_greeks, greeks
from app.services.backtest.options_cache import OptionsHistoryCache
from app.services.backtest.options_provider import (
    HistoricalOptionsProvider,
    clear_worker_options_cache,
)
from app.services.backtest.parquet_options_provider import (
    ParquetOptionsProvider,
    clear_worker_parquet_options_cache,
)

_UNDER = "ZZ"
_EXP = date(2023, 2, 17)
#: The data session under test (a Tuesday) and a session three sessions earlier.
_D = date(2023, 1, 10)
_D_MINUS_3 = date(2023, 1, 5)
_STALE = "ZZ230217C00100000"      # bars on D-3 only
_FRESH = "ZZ230217C00105000"      # bars on D-3 and on D
_NAN_VOL = "ZZ230217C00110000"    # a bar on D whose volume is absent (NaN in the parquet)
_RATE = 0.045


def _spot_source(underlying, on):
    return {_D_MINUS_3: 100.0, _D: 104.0}.get(on, 100.0)


@pytest.fixture
def parquet_provider(tmp_path):
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    root = str(tmp_path / "ThetaDataOptionsProvider")
    OptionHistoryParquetStore(root=root).write_partition(
        _UNDER, _EXP,
        [
            OptionEodBar(occ_symbol=_STALE, bar_date=_D_MINUS_3, open=5.0, high=5.4, low=4.9,
                         close=5.2, volume=70, open_interest=900),
            OptionEodBar(occ_symbol=_FRESH, bar_date=_D_MINUS_3, open=3.0, high=3.3, low=2.9,
                         close=3.1, volume=999, open_interest=500),
            OptionEodBar(occ_symbol=_FRESH, bar_date=_D, open=3.4, high=3.6, low=3.3,
                         close=3.5, volume=45, open_interest=510),
            OptionEodBar(occ_symbol=_NAN_VOL, bar_date=_D, open=None, high=None, low=None,
                         close=None, volume=None, bid=1.9, ask=2.1, open_interest=300),
        ],
        start=date(2023, 1, 1), end=date(2023, 3, 31))
    clear_worker_parquet_options_cache()
    yield ParquetOptionsProvider(root, spot_source=_spot_source, risk_free_rate=_RATE,
                                 spot_scope="test")
    clear_worker_parquet_options_cache()


def _chain(p, as_of=_D, data_session=_D):
    return {c.symbol: c for c in p.get_chain(
        _UNDER, as_of, expiry_min=date(2023, 1, 1), expiry_max=date(2023, 12, 31),
        data_session=data_session)}


# --------------------------------------------------------------------------- #
# Parquet store
# --------------------------------------------------------------------------- #
def test_parquet_contract_that_last_traded_three_sessions_ago_has_zero_volume(parquet_provider):
    """THE regression. Before: volume 70 (D-3's), which clears option_min_volume=25."""
    row = _chain(parquet_provider)[_STALE]
    assert row.volume == 0
    # Prices are still the latest row's (user decision): D-3's close.
    assert row.last == pytest.approx(5.2)


def test_parquet_contract_with_a_bar_on_the_session_reports_that_bar_volume(parquet_provider):
    row = _chain(parquet_provider)[_FRESH]
    assert row.volume == 45 and isinstance(row.volume, int)
    assert row.last == pytest.approx(3.5)


def test_parquet_present_bar_with_nan_volume_is_a_known_zero(parquet_provider):
    """The ThetaData convention: a stored row with no volume is a no-trade day (0), converted
    BEFORE session_volume, which refuses NaN."""
    row = _chain(parquet_provider)[_NAN_VOL]
    assert row.volume == 0
    assert (row.bid, row.ask) == (pytest.approx(1.9), pytest.approx(2.1))


def test_parquet_stale_counter_counts_the_rows_priced_from_an_older_session(parquet_provider):
    assert parquet_provider.chain_staleness() == {
        "chain_rows": 0, "stale_rows": 0,
        "stale_age_days": {"1-3": 0, "4-7": 0, "8-30": 0, "31+": 0}}
    _chain(parquet_provider)
    s = parquet_provider.chain_staleness()
    assert s["chain_rows"] == 3
    assert s["stale_rows"] == 1                       # _STALE only
    assert s["stale_age_days"] == {"1-3": 0, "4-7": 1, "8-30": 0, "31+": 0}  # 5 calendar days
    _chain(parquet_provider)                          # accumulates per run (per provider)
    assert parquet_provider.chain_staleness()["chain_rows"] == 6
    assert parquet_provider.chain_staleness()["stale_rows"] == 2


def test_parquet_data_session_before_the_clock_reads_that_sessions_row(parquet_provider):
    """The branch a backtest never takes (it always has data_session == as_of) but the seam
    allows: a clock AFTER the data session. Prices stay the clamped row's; VOLUME is the
    session's own row, found by a second lookup when the clamped row is newer than it."""
    rows = _chain(parquet_provider, as_of=_D, data_session=_D_MINUS_3)
    assert rows[_FRESH].volume == 999            # D-3's row, although the price row is D's
    assert rows[_FRESH].last == pytest.approx(3.5)
    assert rows[_STALE].volume == 70             # the clamped row IS the session's row
    assert rows[_NAN_VOL].volume == 0            # its only row is after the session
    # No price row is older than the session here, so nothing is stale.
    assert parquet_provider.chain_staleness()["stale_rows"] == 0
    q = parquet_provider.get_quote(_FRESH, _D, data_session=_D_MINUS_3)
    assert q.volume == 999 and q.last == pytest.approx(3.5)
    assert parquet_provider.get_quote(_NAN_VOL, _D, data_session=_D_MINUS_3).volume == 0


def test_parquet_data_session_after_the_clock_is_refused(parquet_provider):
    """A data session later than the as-of clamp would read a bar the clock has not reached."""
    with pytest.raises(ValueError, match="after the as-of"):
        _chain(parquet_provider, as_of=_D_MINUS_3, data_session=_D)


def test_parquet_data_session_is_required(parquet_provider):
    """No silent default: a caller that does not say which session it reads is a bug."""
    with pytest.raises(TypeError):
        parquet_provider.get_chain(_UNDER, _D, expiry_min=date(2023, 1, 1),
                                   expiry_max=date(2023, 12, 31))


def test_parquet_quote_carries_the_session_volume_and_rho(parquet_provider):
    q = parquet_provider.get_quote(_FRESH, _D, data_session=_D)
    assert q.volume == 45
    row = _chain(parquet_provider)[_FRESH]
    assert q.rho is not None and q.rho == row.rho
    q_nan = parquet_provider.get_quote(_NAN_VOL, _D, data_session=_D)
    assert q_nan.volume == 0


def test_parquet_chain_rho_is_bs_rho_per_rate_point_of_the_row_iv(parquet_provider):
    """rho uses the SAME inputs as the other greeks: the row's inverted iv, its date's spot,
    the run's risk_free_rate, calendar-day T."""
    row = _chain(parquet_provider)[_FRESH]
    expected = compute_iv_and_greeks(3.5, 104.0, 105.0, (_EXP - _D).days / 365.0, _RATE,
                                     OptionRight.CALL)
    assert row.implied_volatility == pytest.approx(expected["iv"])
    assert row.rho == pytest.approx(expected["rho"])
    assert row.rho > 0  # a call gains value when rates rise


# --------------------------------------------------------------------------- #
# rho itself: textbook Black-Scholes
# --------------------------------------------------------------------------- #
def test_parquet_fast_volume_path_equals_the_shared_rule_on_every_row(parquet_provider):
    """The inlined hot path (``ds_ord`` given) answers exactly what the generic
    ``session_volume`` path answers, for every row of the store and every session."""
    u = parquet_provider._u(_UNDER)
    for j in list(range(u.n_rows)) + [-1]:
        for ds in (_D_MINUS_3, _D):
            fast = u.session_volume_of_row(j, ds, ds.toordinal())
            generic = u.session_volume_of_row(j, ds)
            assert type(fast) is int and fast == generic, (j, ds, fast, generic)


@pytest.mark.parametrize("bad", [2.5, -3.0, float("inf")])
def test_parquet_fast_volume_path_keeps_the_loud_refusals(parquet_provider, bad):
    u = parquet_provider._u(_UNDER)
    j = next(k for k in range(u.n_rows) if u.bar_ord_l[k] == _D.toordinal()
             and u.volume[k] == u.volume[k])
    old = u.volume                          # the store's read-only mapped column
    u.volume = old.copy()
    u.volume[j] = bad
    try:
        with pytest.raises(ValueError):
            u.session_volume_of_row(j, _D, _D.toordinal())
        with pytest.raises(ValueError):
            u.session_volume_of_row(j, _D)
    finally:
        u.volume = old


def _n(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def test_rho_matches_hull_textbook_example():
    """Hull, Options Futures and Other Derivatives (the delta-hedging example): S=49, K=50,
    r=5%, sigma=20%, T=20 weeks. Call rho = 8.91 per unit rate change, i.e. 0.0891 per 1
    percentage point -- the convention here, matching vega per vol point."""
    g = greeks(49.0, 50.0, 20 / 52, 0.05, 0.20, OptionRight.CALL)
    assert g["rho"] == pytest.approx(0.0891, abs=5e-5)


@pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
def test_rho_matches_the_closed_form(right):
    s, k, t, r, sig = 100.0, 100.0, 0.5, 0.05, 0.2
    d2 = (math.log(s / k) + (r - 0.5 * sig * sig) * t) / (sig * math.sqrt(t))
    if right == OptionRight.CALL:
        raw = k * t * math.exp(-r * t) * _n(d2)          # 26.4424
    else:
        raw = -k * t * math.exp(-r * t) * _n(-d2)        # -22.3231
    g = greeks(s, k, t, r, sig, right)
    assert g["rho"] == pytest.approx(raw / 100.0, abs=1e-6)


def test_compute_iv_and_greeks_reports_rho_none_when_nothing_computed():
    assert compute_iv_and_greeks(None, 100.0, 100.0, 0.5, 0.05, OptionRight.CALL)["rho"] is None


# --------------------------------------------------------------------------- #
# Sqlite store: the same rule
# --------------------------------------------------------------------------- #
def _bar(occ, d, close, volume):
    return {"occ_symbol": occ, "date": d.isoformat(), "open": close, "high": close,
            "low": close, "close": close, "volume": volume, "underlying": _UNDER,
            "option_type": "call", "strike": float(occ[-8:]) / 1000, "expiry": _EXP.isoformat(),
            "iv": 0.3, "delta": 0.5, "gamma": 0.01, "theta": -0.02, "vega": 0.1}


@pytest.fixture
def sqlite_provider(tmp_path):
    clear_worker_options_cache()
    db = str(tmp_path / "opt.sqlite")
    c = OptionsHistoryCache(db)
    c.write_chain_rows(_UNDER, "2023-01-03", [
        {"occ_symbol": occ, "option_type": "call", "strike": float(occ[-8:]) / 1000,
         "expiry": _EXP.isoformat(), "bid": 1.0, "ask": 1.0, "last": 1.0,
         "open_interest": None, "volume": 500}
        for occ in (_STALE, _FRESH)])
    c.write_bar_rows([_bar(_STALE, _D_MINUS_3, 5.2, 70),
                      _bar(_FRESH, _D_MINUS_3, 3.1, 999),
                      _bar(_FRESH, _D, 3.5, 45)])
    yield HistoricalOptionsProvider(db)
    clear_worker_options_cache()


def test_sqlite_contract_that_last_traded_three_sessions_ago_has_zero_volume(sqlite_provider):
    rows = _chain(sqlite_provider)
    assert rows[_STALE].volume == 0          # not D-3's 70, and not the chain column's 500
    assert rows[_STALE].last == pytest.approx(5.2)
    assert rows[_FRESH].volume == 45


def test_sqlite_stale_counter(sqlite_provider):
    _chain(sqlite_provider)
    s = sqlite_provider.chain_staleness()
    assert (s["chain_rows"], s["stale_rows"]) == (2, 1)
    assert s["stale_age_days"]["4-7"] == 1


def test_sqlite_present_bar_without_volume_is_refused(tmp_path, sqlite_provider):
    """Every sqlite bar carries a volume (19,484,995 of 19,484,995 measured); a missing one on
    the session's bar is a build bug, not a zero."""
    OptionsHistoryCache(sqlite_provider.db_path).write_bar_rows([_bar(_STALE, _D, 5.0, None)])
    clear_worker_options_cache()
    with pytest.raises(ValueError, match="carries no volume"):
        _chain(sqlite_provider)


def test_sqlite_quote_carries_the_session_volume(sqlite_provider):
    q = sqlite_provider.get_quote(_FRESH, _D, data_session=_D)
    assert q.volume == 45
    assert q.rho is None  # the sqlite store never stored rho: unknown, never 0
