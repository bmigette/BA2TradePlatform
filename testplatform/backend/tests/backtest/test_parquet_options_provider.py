"""The PARQUET option-store backend: contract parity, as-of clamp, caching, wiring.

Two layers:

  * a SYNTHETIC parquet fixture (written through the real ``OptionHistoryParquetStore``, so
    the layout under test is the one ``tools/warm_options_history.py`` produces) — runs
    everywhere, including CI;
  * a GATED read of the REAL local tree (``CACHE_FOLDER/TastyTradeOptionsProvider``), skipped
    when it is absent. It is the only place a claim about the actual 2023 data can be checked.

The FIRST test is the important one: the two backends must present the SAME callable surface,
because ``BacktestAccount`` holds one of them behind a bare attribute and never asks which.

Run:
    ./venv/bin/python -m pytest tests/backtest/test_parquet_options_provider.py -q
"""
from __future__ import annotations

import inspect
import os
from datetime import date

import pytest

from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
from ba2_common.core.types import OptionRight

import app.services.backtest.parquet_options_provider as pq
from app.services.backtest.option_greeks import compute_iv_and_greeks
from app.services.backtest.options_provider import HistoricalOptionsProvider
from app.services.backtest.parquet_options_provider import (
    ParquetOptionsProvider,
    clear_worker_parquet_options_cache,
)

# --------------------------------------------------------------------------- #
# Synthetic store: ONE underlying (ZZ), two expiries, calls + puts.
#
# Bar coverage is deliberately RAGGED so the as-of clamp has something to clamp:
#   ZZ230120C00100000  bars on 01-03, 01-05, 01-10   (a 5-day gap before 01-10)
#   ZZ230120P00100000  bars on 01-03 only
#   ZZ230217C00110000  bars on 01-10 only            (does not exist before 01-10)
# --------------------------------------------------------------------------- #
_UNDER = "ZZ"
_C100 = "ZZ230120C00100000"
_P100 = "ZZ230120P00100000"
_C110 = "ZZ230217C00110000"
_EXP1 = date(2023, 1, 20)
_EXP2 = date(2023, 2, 17)

_BARS = {
    _EXP1: [
        # occ, bar_date, o, h, l, c, volume, oi, iv
        (_C100, date(2023, 1, 3), 5.0, 5.4, 4.9, 5.2, 110, 900, 0.31),
        (_C100, date(2023, 1, 5), 6.0, 6.4, 5.9, 6.2, 120, 950, 0.33),
        (_C100, date(2023, 1, 10), 7.0, 7.4, 6.9, 7.2, 130, 980, 0.35),
        (_P100, date(2023, 1, 3), 4.0, 4.4, 3.9, 4.2, 40, 500, 0.29),
    ],
    _EXP2: [
        (_C110, date(2023, 1, 10), 3.0, 3.4, 2.9, 3.2, 55, 700, 0.28),
    ],
}

#: Underlying closes the greeks are inverted against. Deliberately NOT flat, so a greek
#: computed on the wrong bar's spot is a different number.
_SPOT = {
    date(2023, 1, 3): 100.0,
    date(2023, 1, 5): 103.0,
    date(2023, 1, 10): 106.0,
}
_RATE = 0.045


def _spot_source(underlying: str, on: date):
    """Last known close at or before ``on`` — the shape ``price_source_spot`` provides."""
    keys = [d for d in sorted(_SPOT) if d <= on]
    return _SPOT[keys[-1]] if keys else None


@pytest.fixture
def store_root(tmp_path):
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    root = str(tmp_path / "TastyTradeOptionsProvider")
    store = OptionHistoryParquetStore(root=root)
    for expiry, rows in _BARS.items():
        store.write_partition(
            _UNDER, expiry,
            [OptionEodBar(occ_symbol=occ, bar_date=d, open=o, high=h, low=lo, close=c,
                          volume=v, open_interest=oi, iv=iv)
             for (occ, d, o, h, lo, c, v, oi, iv) in rows],
            start=date(2023, 1, 1), end=date(2023, 3, 31))
    clear_worker_parquet_options_cache()
    yield root
    clear_worker_parquet_options_cache()


@pytest.fixture
def provider(store_root):
    return ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")


def _wide(p, as_of):
    return p.get_chain(_UNDER, as_of, expiry_min=date(2023, 1, 1), expiry_max=date(2023, 12, 31))


# --------------------------------------------------------------------------- #
# 1. ONE INTERFACE, TWO BACKENDS
# --------------------------------------------------------------------------- #
_SEAM_METHODS = ("get_chain", "get_quote", "get_bar", "get_atm_iv")


@pytest.mark.parametrize("name", _SEAM_METHODS)
def test_signature_matches_the_sqlite_backend(name):
    """The engine holds ONE reader behind a bare attribute and never asks which it is, so a
    drifted signature is a runtime TypeError deep inside a run rather than an import error."""
    a = inspect.signature(getattr(HistoricalOptionsProvider, name))
    b = inspect.signature(getattr(ParquetOptionsProvider, name))
    assert str(a) == str(b), f"{name}: sqlite {a} != parquet {b}"


def test_backtest_account_only_calls_the_four_seam_methods(provider):
    """Every attribute BacktestAccount reaches for on ``self._options`` exists here."""
    for name in _SEAM_METHODS:
        assert callable(getattr(provider, name))


# --------------------------------------------------------------------------- #
# 2. AS-OF DISCIPLINE
# --------------------------------------------------------------------------- #
def test_chain_row_is_the_latest_bar_on_or_before_never_a_later_one(provider):
    """On 01-06 the C100 row must be the 01-05 bar (6.2), NOT the 01-10 bar (7.2)."""
    row = {c.symbol: c for c in _wide(provider, date(2023, 1, 6))}[_C100]
    assert row.last == pytest.approx(6.2)


def test_chain_omits_a_contract_whose_first_bar_is_after_the_asof(provider):
    """C110's first bar is 01-10; on 01-09 it is not in the chain at all (no lookahead on
    contract EXISTENCE, which the sqlite snapshot cannot express)."""
    assert _C110 not in {c.symbol for c in _wide(provider, date(2023, 1, 9))}
    assert _C110 in {c.symbol for c in _wide(provider, date(2023, 1, 10))}


def test_chain_is_empty_before_any_bar(provider):
    assert _wide(provider, date(2022, 12, 31)) == []


def test_get_bar_refuses_a_bar_dated_after_the_asof(provider):
    """THE lookahead guard. C100's next bar after 01-05 is 01-10; asking on 01-06..01-09 must
    return None, not the 01-10 bar. A reader that returned a contract's whole life would
    silently invalidate every result."""
    assert provider.get_bar(_C100, date(2023, 1, 5))["close"] == pytest.approx(6.2)
    for d in (date(2023, 1, 6), date(2023, 1, 7), date(2023, 1, 8), date(2023, 1, 9)):
        assert provider.get_bar(_C100, d) is None, f"leaked a future bar at {d}"
    assert provider.get_bar(_C100, date(2023, 1, 10))["close"] == pytest.approx(7.2)


def test_get_bar_matches_the_sqlite_backend_exact_date_semantics(provider, tmp_path):
    """``get_bar`` is EXACT-date on both backends (the fill engine relies on it: no bar on the
    fill day means no fill)."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    db = str(tmp_path / "opt.sqlite")
    c = OptionsHistoryCache(db)
    c.write_bar_rows([{"occ_symbol": _C100, "date": "2023-01-05", "open": 6.0, "high": 6.4,
                       "low": 5.9, "close": 6.2, "volume": 120, "underlying": _UNDER,
                       "option_type": "call", "strike": 100.0, "expiry": "2023-01-20"}])
    sq = HistoricalOptionsProvider(db)
    assert (sq.get_bar(_C100, date(2023, 1, 6)) is None
            and provider.get_bar(_C100, date(2023, 1, 6)) is None)


def test_get_quote_is_exact_date_and_never_leaks_forward(provider):
    assert provider.get_quote(_C100, date(2023, 1, 6)) is None
    q = provider.get_quote(_C100, date(2023, 1, 5))
    assert (q.bid, q.ask, q.last) == pytest.approx((6.2, 6.2, 6.2))


def test_atm_iv_uses_only_clamped_bars(provider):
    """C110 (exp 02-17, 38 DTE from 01-10) is the only in-window call. On 01-09 it has no bar
    yet, so the answer is None rather than its 01-10 volatility."""
    assert provider.get_atm_iv(_UNDER, date(2023, 1, 9)) is None
    assert provider.get_atm_iv(_UNDER, date(2023, 1, 10)) is not None


# --------------------------------------------------------------------------- #
# 3. WHAT THE ROW SAYS
# --------------------------------------------------------------------------- #
def test_greeks_come_from_compute_iv_and_greeks_on_that_bars_own_close(provider):
    """Byte-for-byte the ONE greeks path, on the clamped bar's close and THAT date's spot."""
    row = {c.symbol: c for c in _wide(provider, date(2023, 1, 6))}[_C100]
    expected = compute_iv_and_greeks(
        6.2, _SPOT[date(2023, 1, 5)], 100.0,
        (_EXP1 - date(2023, 1, 5)).days / 365.0, _RATE, OptionRight.CALL)
    assert row.implied_volatility == expected["iv"]
    assert row.delta == expected["delta"]
    assert row.gamma == expected["gamma"]
    assert row.theta == expected["theta"]
    assert row.vega == expected["vega"]


def test_greeks_track_the_clamped_bar_not_the_latest_one(provider):
    """The same contract on two dates must NOT share a delta — otherwise the overlay is
    stuck on one bar and the as-of clamp is cosmetic."""
    d5 = {c.symbol: c for c in _wide(provider, date(2023, 1, 5))}[_C100].delta
    d10 = {c.symbol: c for c in _wide(provider, date(2023, 1, 10))}[_C100].delta
    assert d5 is not None and d10 is not None and d5 != d10


def test_open_interest_is_surfaced(provider):
    """The parquet's whole point over the sqlite (whose open_interest is NULL on every row):
    ``option_selector``'s min_open_interest gate becomes answerable."""
    row = {c.symbol: c for c in _wide(provider, date(2023, 1, 6))}[_C100]
    assert row.open_interest == 950


def test_quotes_are_the_zero_spread_close_proxy(provider):
    """bid == ask == last == the clamped close, exactly what the sqlite store literally holds
    (bid == ask on all of its rows). Never None: the entry action needs an ``ask`` to size."""
    row = {c.symbol: c for c in _wide(provider, date(2023, 1, 6))}[_C100]
    assert row.bid == row.ask == row.last == pytest.approx(6.2)


def test_bar_dict_carries_the_columns_the_engine_reads(provider):
    bar = provider.get_bar(_C100, date(2023, 1, 10))
    for k in ("open", "high", "low", "close", "volume", "underlying", "option_type",
              "strike", "expiry", "date", "iv", "delta", "gamma", "theta", "vega"):
        assert k in bar, k
    assert bar["option_type"] == OptionRight.CALL      # str-enum compare, as the engine does
    assert bar["strike"] == 100.0 and bar["expiry"] == "2023-01-20"
    assert bar["date"] == "2023-01-10" and bar["volume"] == 130
    # Vendor IV is preserved but is NOT what selection reads (see the module docstring).
    assert bar["vendor_iv"] == pytest.approx(0.35)
    assert bar["iv"] != bar["vendor_iv"]


def test_chain_filters(provider):
    d = date(2023, 1, 10)
    calls = provider.get_chain(_UNDER, d, expiry_min=date(2023, 1, 1),
                               expiry_max=date(2023, 12, 31), option_type=OptionRight.CALL)
    assert {c.symbol for c in calls} == {_C100, _C110}
    near = provider.get_chain(_UNDER, d, expiry_min=date(2023, 1, 1), expiry_max=date(2023, 1, 31))
    assert {c.symbol for c in near} == {_C100, _P100}
    banded = provider.get_chain(_UNDER, d, expiry_min=date(2023, 1, 1),
                                expiry_max=date(2023, 12, 31), strike_min=105.0)
    assert {c.symbol for c in banded} == {_C110}


def test_unknown_underlying_is_an_empty_chain_not_a_crash(provider):
    assert provider.get_chain("NOPE", date(2023, 1, 10), expiry_min=date(2023, 1, 1),
                              expiry_max=date(2023, 12, 31)) == []
    assert provider.get_atm_iv("NOPE", date(2023, 1, 10)) is None
    assert provider.get_bar("NOPE230120C00100000", date(2023, 1, 10)) is None


def test_absent_store_root_fails_loud(tmp_path):
    from app.services.backtest.options_cache import OptionsCacheMiss

    with pytest.raises(OptionsCacheMiss):
        ParquetOptionsProvider(str(tmp_path / "not-there"), spot_source=_spot_source,
                               risk_free_rate=_RATE, spot_scope="test")


# --------------------------------------------------------------------------- #
# 4. CACHING — the GA rebuilds the provider once per trial from the same store
# --------------------------------------------------------------------------- #
def test_second_provider_reuses_the_worker_cache_no_reload(monkeypatch, store_root):
    real = pq._load_underlying
    calls = {"n": 0}

    def counting(root, underlying, rate, spot_source):
        calls["n"] += 1
        return real(root, underlying, rate, spot_source)

    monkeypatch.setattr(pq, "_load_underlying", counting)

    p1 = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")
    _wide(p1, date(2023, 1, 10))
    assert calls["n"] == 1

    p2 = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")
    _wide(p2, date(2023, 1, 10))
    p2.get_bar(_C100, date(2023, 1, 10))
    p2.get_atm_iv(_UNDER, date(2023, 1, 10))
    assert calls["n"] == 1, "a fresh provider must not re-read the parquet"


def test_clear_worker_options_cache_also_clears_the_parquet_backend(monkeypatch, store_root):
    """One reset entry point for both readers — every existing caller means "forget
    everything the option readers cached"."""
    from app.services.backtest.options_provider import clear_worker_options_cache

    real = pq._load_underlying
    calls = {"n": 0}

    def counting(root, underlying, rate, spot_source):
        calls["n"] += 1
        return real(root, underlying, rate, spot_source)

    monkeypatch.setattr(pq, "_load_underlying", counting)
    p = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")
    _wide(p, date(2023, 1, 10))
    assert calls["n"] == 1
    clear_worker_options_cache()
    _wide(p, date(2023, 1, 10))
    assert calls["n"] == 2


def test_underlying_cache_is_lru_bounded(monkeypatch, store_root):
    monkeypatch.setattr(pq, "_UNDERLYING_CACHE_MAX", 1)
    p = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")
    _wide(p, date(2023, 1, 10))
    p.get_chain("OTHER", date(2023, 1, 10), expiry_min=date(2023, 1, 1),
                expiry_max=date(2023, 12, 31))
    assert len(pq._WORKER_UNDERLYING_CACHE) <= 1


def test_atm_iv_result_is_memoised(monkeypatch, store_root):
    p = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")
    first = p.get_atm_iv(_UNDER, date(2023, 1, 10))

    def boom(*a, **k):
        raise AssertionError("get_atm_iv recomputed instead of hitting the memo")

    monkeypatch.setattr(ParquetOptionsProvider, "_compute_atm_iv", boom)
    assert p.get_atm_iv(_UNDER, date(2023, 1, 10)) == first


def test_atm_iv_memo_caches_none_too(monkeypatch, store_root):
    """None is a VALID result ("not measurable today") and re-deriving it is the worst case —
    a symbol absent from the store is scanned in full before returning None."""
    p = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")
    assert p.get_atm_iv(_UNDER, date(2023, 1, 9)) is None

    def boom(*a, **k):
        raise AssertionError("a cached None was treated as a miss")

    monkeypatch.setattr(ParquetOptionsProvider, "_compute_atm_iv", boom)
    assert p.get_atm_iv(_UNDER, date(2023, 1, 9)) is None


# --------------------------------------------------------------------------- #
# 5. OCC -> underlying routing (the one place the two backends differ in shape)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("occ,expected", [
    ("ZZ230120C00100000", "ZZ"),
    ("GOOG230317P00090000", "GOOG"),
    ("SPXW240119C04800000", "SPXW"),
])
def test_underlying_of_occ(occ, expected):
    assert pq._underlying_of(occ) == expected


# --------------------------------------------------------------------------- #
# 6. THE REAL LOCAL STORE (gated)
# --------------------------------------------------------------------------- #
def _real_root():
    from app.services.backtest.options_store import default_options_parquet_root
    root = default_options_parquet_root()
    if os.path.isdir(os.path.join(root, "GOOG")):
        return root
    legacy = os.path.expanduser(
        "~/Documents/ba2_trade_platform/cache/TastyTradeOptionsProvider")
    return legacy if os.path.isdir(os.path.join(legacy, "GOOG")) else None


@pytest.mark.skipif(_real_root() is None, reason="no local TastyTrade parquet tree")
def test_real_store_serves_a_plausible_2023_chain():
    """The window the sqlite cannot reach at all: GOOG, 2023-01-17."""
    root = _real_root()
    clear_worker_parquet_options_cache()
    try:
        p = ParquetOptionsProvider(root, spot_source=lambda s, d: 90.0,
                                   risk_free_rate=_RATE, spot_scope="real-store-probe")
        chain = p.get_chain("GOOG", date(2023, 1, 17), expiry_min=date(2023, 2, 1),
                            expiry_max=date(2023, 3, 31))
        assert len(chain) > 50
        assert len({c.expiry for c in chain}) >= 4
        assert all(date(2023, 2, 1) <= c.expiry <= date(2023, 3, 31) for c in chain)
        assert all(c.last is not None and c.last > 0 for c in chain)
        # Both rights present, greeks computed, and open interest is actually there.
        assert {c.option_type for c in chain} == {OptionRight.CALL, OptionRight.PUT}
        assert sum(1 for c in chain if c.delta is not None) > len(chain) // 2
        assert sum(1 for c in chain if c.open_interest is not None) > len(chain) // 2
    finally:
        clear_worker_parquet_options_cache()


# --------------------------------------------------------------------------- #
# 7. SPOT SCOPE — the greeks overlay is cached, so it must not cross runs whose
#    price sources answer differently for the same (symbol, date).
# --------------------------------------------------------------------------- #
def test_a_different_spot_scope_does_not_reuse_another_runs_greeks(store_root):
    """Two providers over the SAME store with DIFFERENT spot sources must not share the
    cached greeks. In a long-lived pool worker this is a run whose price source was preloaded
    over a narrower window forward-filling a stale close into the next run's inversion."""
    a = ParquetOptionsProvider(store_root, spot_source=lambda s, d: 100.0,
                               risk_free_rate=_RATE, spot_scope="run-A")
    b = ParquetOptionsProvider(store_root, spot_source=lambda s, d: 104.0,
                               risk_free_rate=_RATE, spot_scope="run-B")
    da = {c.symbol: c for c in _wide(a, date(2023, 1, 10))}[_C100].delta
    db = {c.symbol: c for c in _wide(b, date(2023, 1, 10))}[_C100].delta
    assert da is not None and db is not None
    assert da != db, "run B reused run A's greeks — the spot scope is not in the cache key"


def test_the_same_spot_scope_does_reuse(monkeypatch, store_root):
    """The other half: a GA's trials share a scope (same universe + window), which is what
    makes the greeks affordable at all."""
    real = pq._load_underlying
    calls = {"n": 0}

    def counting(root, underlying, rate, spot_source):
        calls["n"] += 1
        return real(root, underlying, rate, spot_source)

    monkeypatch.setattr(pq, "_load_underlying", counting)
    for _ in range(3):
        p = ParquetOptionsProvider(store_root, spot_source=_spot_source,
                                   risk_free_rate=_RATE, spot_scope="one-job")
        _wide(p, date(2023, 1, 10))
    assert calls["n"] == 1


def test_spot_scope_tracks_the_ohlcv_memo_eviction_key():
    """``options_store.spot_scope`` must move with exactly the inputs that change what
    ``AsOfPriceSource.close_asof`` answers — the same tuple the OHLCV memo is evicted on."""
    from app.services.backtest.options_store import spot_scope

    base = {"enabled_instruments": ["AAPL", "MSFT"], "execution_interval": "1d",
            "start_date": "2023-01-01", "end_date": "2023-03-31", "warmup_days": 30}
    assert spot_scope(base) == spot_scope({**base, "enabled_instruments": ["MSFT", "AAPL"]})
    for k, v in (("enabled_instruments", ["AAPL"]), ("execution_interval", "1h"),
                 ("start_date", "2023-01-02"), ("end_date", "2023-04-30"),
                 ("warmup_days", 60)):
        assert spot_scope({**base, k: v}) != spot_scope(base), k


# --------------------------------------------------------------------------- #
# 8. COVERAGE IS STATED, because the vendor floor cannot state it
# --------------------------------------------------------------------------- #
def test_loading_an_underlying_logs_its_actual_bar_coverage(caplog, provider):
    """The floor bounds what COULD have been downloaded; only the tree knows what WAS. A run
    outside the downloaded window otherwise reads an empty store and reports the zero-trade
    result as a result."""
    import logging

    clear_worker_parquet_options_cache()
    with caplog.at_level(logging.INFO, logger=pq.__name__):
        _wide(provider, date(2023, 1, 10))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("2023-01-03..2023-01-10" in m and _UNDER in m for m in msgs), msgs


def test_an_underlying_with_no_partitions_warns(caplog, provider):
    import logging

    clear_worker_parquet_options_cache()
    with caplog.at_level(logging.WARNING, logger=pq.__name__):
        provider.get_chain("NOPE", date(2023, 1, 10), expiry_min=date(2023, 1, 1),
                           expiry_max=date(2023, 12, 31))
    assert any("NO partitions for NOPE" in r.getMessage() for r in caplog.records)
