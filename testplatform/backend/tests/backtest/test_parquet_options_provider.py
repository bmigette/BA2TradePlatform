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

import array
import dataclasses
import inspect
import logging
import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
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


@pytest.fixture(autouse=True)
def shared_arrays_enabled(monkeypatch):
    """Every test here runs with the host-shared derived array cache ON — the production
    default — so an operator's ambient ``BA2_SHARED_ARRAYS=0`` cannot quietly turn the
    caching assertions below into a different test. The tests that are ABOUT the escape
    hatch set "0" in their own body, which wins: it is the same monkeypatch instance."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")


@pytest.fixture
def provider(store_root):
    return ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=_RATE,
                                  spot_scope="test")


def _wide(p, as_of):
    return p.get_chain(_UNDER, as_of, expiry_min=date(2023, 1, 1), expiry_max=date(2023, 12, 31))


# --------------------------------------------------------------------------- #
# 1. ONE INTERFACE, TWO BACKENDS
# --------------------------------------------------------------------------- #
_SEAM_METHODS = ("get_chain", "get_quote", "get_bar", "get_atm_iv", "delta_at_entry")


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


def test_get_bar_returns_a_fresh_dict_the_caller_may_mutate(provider):
    """The bar dict is MEMOISED per row (it is rebuilt on every MTM/fill/expiry read and the
    row is immutable once cached), so ``get_bar`` must hand out a copy. Returning the memo
    itself would turn any caller's ``bar["close"] = ...`` into cross-call corruption of every
    later read of that bar."""
    a = provider.get_bar(_C100, date(2023, 1, 10))
    a["close"] = -999.0
    a["injected"] = True
    b = provider.get_bar(_C100, date(2023, 1, 10))
    assert b is not a
    assert b["close"] == pytest.approx(7.2)
    assert "injected" not in b


def test_the_bar_dict_memo_is_per_row_not_per_contract(provider):
    """MUTATION KILLER for the memo key: memoising on the CONTRACT instead of the ROW would
    serve 01-05's bar again on 01-10."""
    assert provider.get_bar(_C100, date(2023, 1, 5))["close"] == pytest.approx(6.2)
    assert provider.get_bar(_C100, date(2023, 1, 10))["close"] == pytest.approx(7.2)
    assert provider.get_bar(_C100, date(2023, 1, 5))["date"] == "2023-01-05"
    assert provider.get_bar(_C100, date(2023, 1, 10))["date"] == "2023-01-10"


def test_bar_dict_is_built_once_per_row(provider):
    """The greeks are the expensive part and they are already memoised; the DICT around them
    (two ISO conversions, seven NaN tests, a 17-key build — 5.4 us measured) was not."""
    u = provider._u(_UNDER)
    ci = u.c_index[_C100]
    i = u.exact_row(ci, date(2023, 1, 10).toordinal())
    first = u.bar_dict(i, ci, provider.spot_source)
    assert len(u._bar_memo) == 1
    u._bar_memo[i]["close"] = 1234.5   # poison the memo: a rebuild would not see it
    assert u.bar_dict(i, ci, provider.spot_source)["close"] == pytest.approx(1234.5)
    assert first["close"] == pytest.approx(7.2)


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
def _count_reads(monkeypatch):
    """Count PARQUET READS — ``OptionHistoryParquetStore.read_underlying``.

    Not overlay construction: the greeks overlay is cheap and scope-keyed by design, while
    re-reading and re-parsing the bytes is what a scope change used to cost.

    And not ``_load_raw_underlying``, which since the host-shared derived cache runs on every
    cold OPEN: a fresh worker still calls it and still gets a whole object, it just MAPS the
    arrays somebody already built instead of parsing the parquet again. The parquet read is
    the expensive thing and it is precisely what a warm derived set skips, so it is the thing
    worth counting.
    """
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    real = OptionHistoryParquetStore.read_underlying
    calls = {"n": 0}

    def counting(self, underlying, parts=None):
        calls["n"] += 1
        return real(self, underlying, parts)

    monkeypatch.setattr(OptionHistoryParquetStore, "read_underlying", counting)
    return calls


def _count_loads(monkeypatch):
    """Count ``_load_raw_underlying`` calls — i.e. WORKER-CACHE misses, whatever served them.

    The counterpart to ``_count_reads``: for the tests that are about the process-local cache
    being kept or dropped, a miss is the event, and the derived store deliberately makes a
    miss cheap rather than making it disappear.
    """
    real = pq._load_raw_underlying
    calls = {"n": 0}

    def counting(root, underlying):
        calls["n"] += 1
        return real(root, underlying)

    monkeypatch.setattr(pq, "_load_raw_underlying", counting)
    return calls


def test_second_provider_reuses_the_worker_cache_no_reload(monkeypatch, store_root):
    calls = _count_reads(monkeypatch)

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

    # _count_loads, not _count_reads: what is under test is that the WORKER cache was
    # dropped, and after the drop the host-shared derived set serves the reload with no
    # parquet read at all.
    calls = _count_loads(monkeypatch)
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
    assert len(pq._WORKER_RAW_CACHE) <= 1


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
# 4b. delta_at_entry — the intraday-drawdown refinement's seam (results.py)
# --------------------------------------------------------------------------- #
def test_delta_at_entry_is_the_last_bar_STRICTLY_BEFORE_entry(provider):
    """NOT on-or-before. A daily bar is dated at the CLOSE, so the entry day's own bar has
    already absorbed the whole session — and the refinement only asks about trades flagged
    BECAUSE the underlying moved, so that delta embeds the very move whose drawdown is being
    estimated. Calling it "delta at entry" is circular, and it feeds
    strategy_fitness.option_consistent_annual_return.

    The prior session's delta is stale, not wrong: it describes a real market state that
    preceded the entry. Staleness is a bounded approximation; lookahead is not."""
    d5 = {c.symbol: c for c in _wide(provider, date(2023, 1, 5))}[_C100].delta
    d10 = {c.symbol: c for c in _wide(provider, date(2023, 1, 10))}[_C100].delta
    assert d5 != d10
    # 01-07 has no bar of its own; the latest STRICTLY BEFORE it is 01-05's.
    assert provider.delta_at_entry(_UNDER, _C100, date(2023, 1, 7)) == d5
    # 01-10 HAS a bar, and that is exactly the one that must not be served: entering on 01-10
    # cannot see 01-10's close, so the answer stays 01-05's.
    assert provider.delta_at_entry(_UNDER, _C100, date(2023, 1, 10)) == d5


@pytest.mark.parametrize("when", [
    datetime(2023, 1, 5, 15, 45),          # what the refinement actually holds
    date(2023, 1, 5),
    "2023-01-05",
    "2023-01-05 15:45:00",
    "2023-01-05T15:45:00",
])
def test_delta_at_entry_accepts_every_shape_the_refinement_hands_it(provider, when):
    expected = provider.delta_at_entry(_UNDER, _C100, date(2023, 1, 5))
    assert expected is not None
    assert provider.delta_at_entry(_UNDER, _C100, when) == expected


def test_delta_at_entry_is_none_not_a_crash_for_anything_unknown(provider):
    assert provider.delta_at_entry(_UNDER, _C100, date(2022, 12, 31)) is None   # before any bar
    assert provider.delta_at_entry(_UNDER, "ZZ230120C09999000", date(2023, 1, 10)) is None
    assert provider.delta_at_entry("NOPE", _C100, date(2023, 1, 10)) is None
    assert provider.delta_at_entry(_UNDER, _C100, None) is None
    assert provider.delta_at_entry(_UNDER, _C100, "not-a-date") is None


def test_both_backends_answer_delta_at_entry(tmp_path, provider):
    """FINDING 3. ``results._build_refine_drawdown_fn`` used to bind ``options.cache.db_path``
    — sqlite-only — so the intraday refinement silently disabled itself on parquet and
    ``option_consistent_annual_return`` (which divides by max_drawdown) differed between the
    two stores for a reason nothing in the result could show."""
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import _WORKER_CHAIN_CACHE

    _WORKER_CHAIN_CACHE.clear()
    db = str(tmp_path / "opt.sqlite")
    OptionsHistoryCache(db).write_chain_rows(_UNDER, "2023-01-05", [
        {"occ_symbol": _C100, "option_type": "call", "strike": 100.0,
         "expiry": "2023-01-20", "bid": 6.1, "ask": 6.3, "last": 6.2, "iv": 0.33,
         "delta": 0.61}])
    try:
        sq = HistoricalOptionsProvider(db)
        # 01-06, not 01-05: both readers now serve the last snapshot STRICTLY BEFORE entry, so
        # the only snapshot (01-05) answers an entry on the 6th and NOT one on the 5th itself.
        assert sq.delta_at_entry(_UNDER, _C100, date(2023, 1, 6)) == pytest.approx(0.61)
        assert sq.delta_at_entry(_UNDER, _C100, date(2023, 1, 5)) is None
        assert sq.delta_at_entry(_UNDER, _C100, date(2022, 1, 1)) is None
        assert provider.delta_at_entry(_UNDER, _C100, date(2023, 1, 6)) is not None
    finally:
        _WORKER_CHAIN_CACHE.clear()


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
def test_real_store_serves_a_plausible_2023_chain(monkeypatch):
    """The window the sqlite cannot reach at all: GOOG, 2023-01-17."""
    # The escape hatch, deliberately: this is the one test that reads the operator's REAL
    # tree, and the shared path would publish a derived array set into their real cache as a
    # side effect of running the suite. What is under test here is the tree's CONTENT.
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
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
    calls = _count_reads(monkeypatch)
    for _ in range(3):
        p = ParquetOptionsProvider(store_root, spot_source=_spot_source,
                                   risk_free_rate=_RATE, spot_scope="one-job")
        _wide(p, date(2023, 1, 10))
    assert calls["n"] == 1


def test_a_new_spot_scope_reuses_the_PARQUET_BYTES_and_only_redoes_the_greeks(
        monkeypatch, store_root):
    """THE SPLIT. A scope change must cost a greeks overlay, not a re-read.

    ``_build_daily_trial_config`` sets ``enabled_instruments`` per INDIVIDUAL (to that trial's
    screener candidates) and ``spot_scope`` is derived from it, so in a screener GA the scope
    changes between trials of one job. Keyed as one object this re-read and re-parsed
    byte-identical parquet every time (measured on the real tree: GOOG 145 ms cold, 4.5 us
    warm, 58 ms on a new scope) and left two full copies in the LRU.
    """
    calls = _count_reads(monkeypatch)
    a = ParquetOptionsProvider(store_root, spot_source=lambda s, d: 100.0,
                               risk_free_rate=_RATE, spot_scope="run-A")
    _wide(a, date(2023, 1, 10))
    assert calls["n"] == 1

    b = ParquetOptionsProvider(store_root, spot_source=lambda s, d: 104.0,
                               risk_free_rate=_RATE, spot_scope="run-B")
    _wide(b, date(2023, 1, 10))
    assert calls["n"] == 1, "a new spot scope re-read the identical parquet bytes"
    # ONE copy of the bytes, TWO overlays — the whole point.
    assert len(pq._WORKER_RAW_CACHE) == 1
    assert len(pq._WORKER_UNDERLYING_CACHE) == 2
    # And they genuinely share the same underlying arrays, not two equal copies.
    (raw,) = list(pq._WORKER_RAW_CACHE.values())
    for overlay in pq._WORKER_UNDERLYING_CACHE.values():
        assert overlay.raw is raw


def test_a_new_risk_free_rate_also_only_redoes_the_greeks(monkeypatch, store_root):
    """The rate is a pricing assumption, not data: same bytes, different inversion."""
    calls = _count_reads(monkeypatch)
    a = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=0.01,
                               spot_scope="same")
    b = ParquetOptionsProvider(store_root, spot_source=_spot_source, risk_free_rate=0.09,
                               spot_scope="same")
    da = {c.symbol: c for c in _wide(a, date(2023, 1, 10))}[_C100].delta
    db = {c.symbol: c for c in _wide(b, date(2023, 1, 10))}[_C100].delta
    assert calls["n"] == 1
    assert da != db, "the rate is not in the greeks-overlay key"


def test_the_worker_cache_does_not_pin_the_runs_spot_source(store_root):
    """FINDING 4. ``price_source_spot`` closes over the run's ``AsOfPriceSource`` (and so over
    that run's whole OHLCV memo). Nothing on the run path clears these caches
    — ``clear_worker_parquet_options_cache`` has no production caller — so a cached object
    holding that closure pins the finished run's memory for the life of the pool worker.

    The spot source therefore lives on the PROVIDER and is threaded into the greeks call; what
    the overlay caches is the resulting float.
    """
    import gc
    import weakref

    class _Source:
        def __call__(self, underlying, on):
            return 100.0

    src = _Source()
    p = ParquetOptionsProvider(store_root, spot_source=src, risk_free_rate=_RATE,
                               spot_scope="pin-check")
    _wide(p, date(2023, 1, 10))
    p.get_bar(_C100, date(2023, 1, 10))
    ref = weakref.ref(src)

    del p, src
    gc.collect()
    assert ref() is None, (
        "the worker option cache is still holding the run's spot source "
        f"(referrers: {gc.get_referrers(ref())!r})")
    # The cached overlay is still there and still usable — only the closure went.
    assert pq._WORKER_UNDERLYING_CACHE
    q = ParquetOptionsProvider(store_root, spot_source=lambda s, d: 100.0,
                               risk_free_rate=_RATE, spot_scope="pin-check")
    assert q.get_bar(_C100, date(2023, 1, 10))["close"] == pytest.approx(7.2)


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


# --------------------------------------------------------------------------- #
# REAL QUOTES (2026-09-03). The TastyTrade tree predates the bid/ask columns and falls back
# to the zero-spread close proxy above; ThetaData partitions carry real NBBO, and where they
# do the reader must use it -- that is what makes max_spread_pct and the GA's w_spread do
# anything at all (with a constant 0.0 spread they gate nothing and rank everything alike).
#
# A no-trade day is the case that matters: close is NULL there (an option that did not trade
# has no trade price), and the row's mark is the quote mid. Storing 0.0 instead would price a
# genuinely $55-bid contract at zero.
# --------------------------------------------------------------------------- #
@pytest.fixture
def quoted_store_root(tmp_path):
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    root = str(tmp_path / "ThetaDataOptionsProvider")
    store = OptionHistoryParquetStore(root=root)
    store.write_partition(
        _UNDER, date(2023, 1, 20),
        [
            # traded day: real OHLC AND a real quote around it
            OptionEodBar(occ_symbol=_C100, bar_date=date(2023, 1, 3),
                         open=5.0, high=5.5, low=4.8, close=5.2, volume=100,
                         bid=5.10, ask=5.30, open_interest=900, iv=0.30),
            # NO-TRADE day: no OHLC at all, but a real two-sided quote
            OptionEodBar(occ_symbol=_C100, bar_date=date(2023, 1, 5),
                         open=None, high=None, low=None, close=None, volume=0,
                         bid=55.10, ask=56.20, open_interest=900, iv=0.31),
        ],
        start=date(2023, 1, 1), end=date(2023, 3, 31))
    clear_worker_parquet_options_cache()
    yield root
    clear_worker_parquet_options_cache()


def test_a_store_with_quotes_serves_the_real_bid_ask_not_the_close_proxy(quoted_store_root):
    p = ParquetOptionsProvider(quoted_store_root, spot_source=_spot_source,
                               risk_free_rate=_RATE, spot_scope="test")
    row = {c.symbol: c for c in p.get_chain(_UNDER, date(2023, 1, 3),
                                            expiry_min=date(2023, 1, 1),
                                            expiry_max=date(2023, 12, 31))}[_C100]
    assert row.bid == pytest.approx(5.10)
    assert row.ask == pytest.approx(5.30)
    assert row.bid != row.ask, "a real spread, not the zero-spread proxy"
    assert row.last == pytest.approx(5.2), "`last` stays the TRADE price"
    assert row.spread_pct is not None and row.spread_pct > 0, (
        "a real spread must make spread_pct non-degenerate -- a constant 0.0 ranks BEST and "
        "silently disables max_spread_pct and the GA's w_spread")


def test_a_no_trade_day_is_marked_at_the_quote_not_at_zero(quoted_store_root):
    p = ParquetOptionsProvider(quoted_store_root, spot_source=_spot_source,
                               risk_free_rate=_RATE, spot_scope="test")
    row = {c.symbol: c for c in p.get_chain(_UNDER, date(2023, 1, 5),
                                            expiry_min=date(2023, 1, 1),
                                            expiry_max=date(2023, 12, 31))}[_C100]
    assert row.bid == pytest.approx(55.10) and row.ask == pytest.approx(56.20)
    assert row.mid == pytest.approx(55.65)
    assert row.last is None, "there was no trade, so there is no last price"
    assert row.mid != 0.0, "0.0 here would mark a $55-bid contract worthless"

    bar = p.get_bar(_C100, date(2023, 1, 5))
    assert bar["close"] is None, "a no-trade day must not report a 0.0 close"

    q = p.get_quote(_C100, date(2023, 1, 5))
    assert (q.bid, q.ask) == (pytest.approx(55.10), pytest.approx(56.20)), (
        "get_quote and get_chain must price identically (options_provider bug B4)")



# --------------------------------------------------------------------------- #
# THE SHARED-ARRAY SEAM. A `_RawUnderlying` is two separable halves: numeric columns that
# depend on nothing but the parquet bytes (so several worker processes can memory-map ONE
# copy) and the python projections the hot paths index, which are per-process by
# construction. `arrays_from_frame` produces the first half as a plain dict of 1-D numeric
# arrays; `from_arrays` rebuilds the whole object from it. Nothing here changes what a reader
# answers -- it is the shape a per-host derived array store can plug into.
# --------------------------------------------------------------------------- #
def _assert_same_raw(a, b):
    """Every slot, compared by its kind.

    Driven off ``__slots__`` rather than a hand-kept name list, so a field added to
    ``_RawUnderlying`` later cannot quietly escape the round-trip check -- which is exactly
    how a shared store would start serving a half-built object.
    """
    for name in pq._RawUnderlying.__slots__:
        va, vb = getattr(a, name), getattr(b, name)
        if isinstance(va, np.ndarray) or isinstance(vb, np.ndarray):
            assert isinstance(va, np.ndarray) and isinstance(vb, np.ndarray), name
            np.testing.assert_array_equal(va, vb, err_msg=name)
            assert va.dtype == vb.dtype, name
        elif isinstance(va, array.array) or isinstance(vb, array.array):
            assert isinstance(va, array.array) and isinstance(vb, array.array), name
            assert va.typecode == vb.typecode, name
            assert list(va) == list(vb), name
        else:
            assert type(va) is type(vb), name
            assert va == vb, name


def _arrays_for(root):
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    df = OptionHistoryParquetStore(root=root).read_underlying(_UNDER)
    return df, pq._RawUnderlying.arrays_from_frame(df)


def test_raw_underlying_round_trips_through_its_array_dict(store_root):
    """The numeric arrays a _RawUnderlying is built from are a plain dict of 1-D numeric arrays,
    and building from that dict gives the identical object -- the seam the shared store plugs
    into. Only what a memory-mapped .npy can hold may appear in the dict: no object arrays."""
    df, arrays = _arrays_for(store_root)
    assert set(arrays) == set(pq._RawUnderlying.ARRAY_NAMES)
    for name, arr in arrays.items():
        assert isinstance(arr, np.ndarray), name
        assert arr.ndim == 1 and arr.dtype != object, name
        assert arr.flags.c_contiguous, name

    a = pq._RawUnderlying(_UNDER, df)
    b = pq._RawUnderlying.from_arrays(_UNDER, arrays)
    _assert_same_raw(a, b)

    # The as-of clamp bisects a C int buffer, not a list of boxed ints: half the private
    # bytes per process and no transient int-object spike while building it.
    assert isinstance(b.bar_ord_l, array.array) and b.bar_ord_l.typecode == "i"
    assert len(b.bar_ord_l) == b.n_rows
    assert list(b.bar_ord_l) == b.bar_ord.tolist()


def test_direct_arrays_all_name_real_slots():
    """`_DIRECT_ARRAYS` is set with setattr, so a name that is not a slot would raise at bind
    time -- on the first underlying of a run, not in any test that only reads the dict."""
    assert set(pq._RawUnderlying._DIRECT_ARRAYS) <= set(pq._RawUnderlying.__slots__)
    assert set(pq._RawUnderlying._DIRECT_ARRAYS) == (
        set(pq._RawUnderlying.ARRAY_NAMES) - {"c_occ_utf8", "has_quotes", "priceless_count"})


def test_empty_frame_arrays_have_the_documented_dtypes():
    """A store with no rows for an underlying still produces the full array set, so a derived
    store never has to special-case it -- and the empty arrays carry the SAME dtypes as the
    populated ones, or a memory-mapped rebuild would silently change an int column to float."""
    arrays = pq._RawUnderlying.arrays_from_frame(None)
    assert set(arrays) == set(pq._RawUnderlying.ARRAY_NAMES)
    expected = {
        "bar_ord": np.int32, "starts": np.int32, "stops": np.int32, "c_expiry_ord": np.int32,
        "open": np.float64, "high": np.float64, "low": np.float64, "close": np.float64,
        "volume": np.float64, "open_interest": np.float64, "vendor_iv": np.float64,
        "bid": np.float64, "ask": np.float64, "c_strike": np.float64,
        "c_is_call": np.bool_, "c_occ_utf8": np.uint8, "has_quotes": np.bool_,
        "priceless_count": np.int64,
    }
    sized = {"has_quotes": 1, "priceless_count": 1}
    for name, dt in expected.items():
        assert arrays[name].dtype == np.dtype(dt), f"{name}: {arrays[name].dtype}"
        assert arrays[name].size == sized.get(name, 0), name
    assert arrays["has_quotes"][0] == False  # noqa: E712 -- the value, not truthiness
    assert arrays["priceless_count"][0] == 0

    empty = pq._RawUnderlying.from_arrays(_UNDER, arrays)
    _assert_same_raw(pq._RawUnderlying(_UNDER, None), empty)
    assert empty.n_rows == 0 and empty.c_occ == [] and empty.has_quotes is False


def test_a_store_without_quotes_persists_no_nan_quote_columns(store_root):
    """The REAL TastyTrade tree predates the bid/ask columns and does not carry them at all
    (the fixture store writes today's column set, so the legacy frame is reproduced by
    dropping them). Two all-NaN float64 columns would be 16 bytes a row of stored nothing --
    61 MB for TSLA -- written to disk and mapped into every worker. Absence is the fact; the
    binder synthesises them per process, so every read path still finds a full-length array."""
    df, _ = _arrays_for(store_root)
    df = df.drop(columns=["bid", "ask"])
    arrays = pq._RawUnderlying.arrays_from_frame(df)
    assert arrays["has_quotes"].tolist() == [False]
    assert arrays["bid"].size == 0 and arrays["ask"].size == 0
    assert arrays["bid"].dtype == np.float64 and arrays["ask"].dtype == np.float64

    a = pq._RawUnderlying(_UNDER, df)
    b = pq._RawUnderlying.from_arrays(_UNDER, arrays)
    for raw in (a, b):
        assert raw.bid.shape == (raw.n_rows,) and raw.ask.shape == (raw.n_rows,)
        assert np.all(np.isnan(raw.bid)) and np.all(np.isnan(raw.ask))
    _assert_same_raw(a, b)


def test_quoted_store_round_trips_its_real_bid_ask_through_the_array_dict(quoted_store_root):
    """A ThetaData-shaped store carries real NBBO, so `has_quotes` and the bid/ask columns are
    part of what a shared store must ship -- a rebuild that lost them would silently revert the
    run to the zero-spread close proxy."""
    df, arrays = _arrays_for(quoted_store_root)
    assert arrays["has_quotes"].tolist() == [True]

    a = pq._RawUnderlying(_UNDER, df)
    b = pq._RawUnderlying.from_arrays(_UNDER, arrays)
    assert arrays["bid"].size == a.n_rows and arrays["ask"].size == a.n_rows
    assert a.has_quotes is True and b.has_quotes is True
    _assert_same_raw(a, b)
    assert not np.all(np.isnan(b.bid)) and not np.all(np.isnan(b.ask))


def test_priceless_rows_are_reported_once_per_process_not_once_per_host(store_root, caplog):
    """The invariant is COUNTED at build (a fact about the store) but must be SAID wherever
    the arrays are used: with a per-host cache the build happens once and every later process
    would otherwise open a malformed store in silence."""
    _df, arrays = _arrays_for(store_root)
    arrays = dict(arrays)
    arrays["priceless_count"] = np.array([3], dtype=np.int64)

    with caplog.at_level(logging.ERROR, logger=pq.__name__):
        raw = pq._RawUnderlying.from_arrays(_UNDER, arrays)

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(_UNDER in m and "3" in m and "NO price" in m for m in msgs), msgs
    assert raw.n_rows > 0, "the complaint does not stop the object being usable"


def test_a_decoded_symbol_count_that_disagrees_with_the_contracts_is_refused(store_root):
    """c_occ_utf8 is a newline-joined encoding, so a symbol containing a newline -- or a
    mapping paired with the wrong underlying's arrays -- silently shifts every contract
    index. Cheap to check once per bind, and unrecoverable if it is not."""
    _df, arrays = _arrays_for(store_root)
    arrays = dict(arrays)
    arrays["c_occ_utf8"] = np.frombuffer("A\nB\nC\nEXTRA".encode("utf-8"), dtype=np.uint8)

    with pytest.raises(ValueError) as e:
        pq._RawUnderlying.from_arrays(_UNDER, arrays)
    assert _UNDER in str(e.value) and "newline" in str(e.value)


def test_arrays_round_trip_through_read_only_memory_maps(store_root, tmp_path):
    """The point of the split: the arrays survive a .npy round trip and the binder reads them
    AS MAPPED. If _bind ever copied one privately the sharing would be gone and nothing else
    would notice, so the read-only flag is asserted on the bound object."""
    df, arrays = _arrays_for(store_root)
    mapped = {}
    for name, arr in arrays.items():
        path = tmp_path / f"{name}.npy"
        np.save(str(path), arr)
        mapped[name] = np.asarray(np.load(str(path), mmap_mode="r"))

    b = pq._RawUnderlying.from_arrays(_UNDER, mapped)
    _assert_same_raw(pq._RawUnderlying(_UNDER, df), b)
    assert b.bar_ord.flags.writeable is False, "_bind must not privately copy a mapping"
    assert b.close.flags.writeable is False


# --------------------------------------------------------------------------- #
# 10. THE HOST-SHARED DERIVED ARRAY CACHE (Task 4 of
#     docs/plans/2026-09-14-shared-arrays-across-workers.md)
#
# The seam above (arrays_from_frame / from_arrays) is now SOURCED from a per-host derived
# cache of memory-mapped .npy files: the first process to want an underlying parses the
# parquet once and publishes the arrays; every later process on that host maps them. What
# these tests pin is that the mapping is real (so the memory is actually shared), that it is
# invalidated by a rewritten partition, and — the whole point — that it answers IDENTICALLY
# to the private path it replaces.
# --------------------------------------------------------------------------- #
def _derived_key_dir(root, symbol=_UNDER):
    from ba2_common.core import shared_arrays as SA

    # The ``u_`` prefix is the reader's, not the store's: see _load_raw_underlying.
    return (Path(SA.derived_root_for(root))
            / f"u_{symbol.upper()}.v{pq._RawUnderlying.ARRAYS_VERSION}")


def _canon(arr):
    """An array as a comparable value, with NaN equal to NaN.

    ``[nan] == [nan]`` is False, and a no-trade row's OHLC (and a legacy tree's whole
    bid/ask) is exactly NaN, so a plain list comparison would report every fixture as
    "different" whatever the arrays hold. ``repr`` of the list renders NaN as the token
    ``nan``, which compares positionally like every other value.
    """
    return arr.dtype.str, repr(arr.tolist())


def _published_sigs(root, symbol=_UNDER):
    from ba2_common.core import shared_arrays as SA

    d = _derived_key_dir(root, symbol)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if (p / SA.DONE_MARKER).is_file())


def test_shared_store_is_built_then_opened_without_re_reading_parquet(store_root, monkeypatch):
    """Cold: parse once and publish. Warm: MAP, do not re-parse.

    The second load stands in for a second worker process — the worker caches are process
    globals, so clearing them is exactly what a fresh process starts with. Zero parquet reads
    there is the entire saving: on the 2020 ThetaData tree it is 177.8M rows of float64 that
    every worker used to build, and hold, privately.
    """
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    clear_worker_parquet_options_cache()

    raw = pq._load_raw_underlying(store_root, _UNDER)

    sigs = _published_sigs(store_root)
    assert len(sigs) == 1, [p.name for p in _derived_key_dir(store_root).iterdir()]
    assert (sigs[0] / "close.npy").is_file()
    # A plain ndarray VIEW over the mapping, not the np.memmap subclass (33-43% slower on
    # scalar reads) and not a private copy (which would share nothing at all).
    assert type(raw.close) is np.ndarray
    assert raw.close.base is not None
    assert raw.close.flags.writeable is False

    clear_worker_parquet_options_cache()
    calls = _count_reads(monkeypatch)
    raw2 = pq._load_raw_underlying(store_root, _UNDER)
    assert calls["n"] == 0, "a published derived set must be opened, not re-parsed"
    assert raw2.close.base is not None
    _assert_same_raw(raw, raw2)


@pytest.mark.parametrize("root_fixture", ["store_root", "quoted_store_root"])
def test_shared_and_private_paths_are_bit_identical(root_fixture, request, monkeypatch):
    """THE POINT. Same chain, same marks, same greeks, same bar, whichever path served the
    arrays — on the legacy no-quote tree AND on a quoted (ThetaData-shaped) one, because the
    two do not even store the same columns."""
    root = request.getfixturevalue(root_fixture)
    as_of = date(2023, 1, 5)          # a date BOTH fixtures carry a bar on
    results = {}
    raws = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("BA2_SHARED_ARRAYS", flag)
        clear_worker_parquet_options_cache()
        p = ParquetOptionsProvider(root, spot_source=_spot_source, risk_free_rate=_RATE,
                                   spot_scope="parity")
        chain = sorted(p.get_chain(_UNDER, as_of, expiry_min=date(2023, 1, 1),
                                   expiry_max=date(2023, 12, 31)),
                       key=lambda c: c.symbol)
        raw = pq._raw_underlying(root, _UNDER)
        raws[flag] = raw
        results[flag] = (
            [dataclasses.asdict(c) for c in chain],
            p.get_atm_iv(_UNDER, as_of),
            p.get_bar(_C100, as_of),
            {name: _canon(getattr(raw, name)) for name in pq._RawUnderlying._DIRECT_ARRAYS},
            (raw.has_quotes, raw.c_occ, raw.n_rows),
        )
    assert results["0"] == results["1"]
    # And every remaining slot, including the array('i') projection and the ordinal lookups.
    _assert_same_raw(raws["0"], raws["1"])
    assert raws["0"].close.flags.writeable is True, "the escape hatch keeps private arrays"
    assert raws["1"].close.flags.writeable is False, "the shared path maps them read-only"


def test_a_rewritten_partition_invalidates_the_derived_set(store_root, monkeypatch):
    """A re-warmed underlying must not be served from the arrays built off the OLD parquet.

    The signature is (path, size, mtime) per partition — deliberately not a content hash, on
    a tree that runs to hundreds of GB — so this rewrites a partition through the store's own
    writer AND advances its mtime, which is what a real re-warm does.
    """
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    clear_worker_parquet_options_cache()
    pq._load_raw_underlying(store_root, _UNDER)
    before = {p.name for p in _published_sigs(store_root)}
    assert len(before) == 1

    store = OptionHistoryParquetStore(root=store_root)
    store.write_partition(
        _UNDER, _EXP2,
        [OptionEodBar(occ_symbol=_C110, bar_date=date(2023, 1, 10), open=3.0, high=3.4,
                      low=2.9, close=9.9, volume=55, open_interest=700, iv=0.28)],
        start=date(2023, 1, 1), end=date(2023, 3, 31))
    # Belt and braces: a one-row rewrite need not change the file's SIZE, and a filesystem
    # timestamp granularity coarser than the rewrite would leave the mtime equal too.
    path = store.bars_path(_UNDER, _EXP2)
    stamp = os.stat(path).st_mtime + 100
    os.utime(path, (stamp, stamp))

    clear_worker_parquet_options_cache()
    raw = pq._load_raw_underlying(store_root, _UNDER)
    after = {p.name for p in _published_sigs(store_root)}
    assert after - before, f"no new signature was published: {after}"
    assert 9.9 in raw.close.tolist(), "the stale arrays were served for a rewritten partition"


def test_escape_hatch_reads_parquet_every_cold_load_and_writes_nothing(
        store_root, tmp_path, monkeypatch):
    """``BA2_SHARED_ARRAYS=0`` is the pre-2026-09-14 behaviour, exactly: private writable
    arrays, a parquet read per cold load, and NOTHING on disk to roll back."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    clear_worker_parquet_options_cache()
    calls = _count_reads(monkeypatch)

    a = pq._load_raw_underlying(store_root, _UNDER)
    clear_worker_parquet_options_cache()
    b = pq._load_raw_underlying(store_root, _UNDER)

    assert calls["n"] == 2, "the escape hatch must re-read, not open a derived set"
    assert not (tmp_path / "_derived").exists()
    assert a.close.flags.writeable and b.close.flags.writeable
    _assert_same_raw(a, b)


@pytest.mark.parametrize("symbol", ["PRN", "AUX"])
def test_a_reserved_device_name_ticker_is_still_readable(symbol, tmp_path, monkeypatch):
    """PRN and AUX are real tickers AND Windows device names.

    ``shared_arrays._safe_key`` REFUSES such a key outright (it will not silently mangle one),
    so a bare-symbol key raised ValueError out of ``_load_raw_underlying`` -- nothing catches
    it, so the trial dies -- and on Linux too, because the refusal is in the sanitiser rather
    than in the filesystem. The reader prefixes, which is what the sanitiser's own message
    asks a symbol-keyed caller to do. Reproduced before the fix; the test is the fix's
    receipt, so it must exercise the whole load, not just the key.
    """
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    root = str(tmp_path / "ThetaDataOptionsProvider")
    occ = f"{symbol}230120C00100000"
    OptionHistoryParquetStore(root=root).write_partition(
        symbol, _EXP1,
        [OptionEodBar(occ_symbol=occ, bar_date=date(2023, 1, 3), open=5.0, high=5.4, low=4.9,
                      close=5.2, volume=110, open_interest=900, iv=0.31)],
        start=date(2023, 1, 1), end=date(2023, 3, 31))
    clear_worker_parquet_options_cache()

    p = ParquetOptionsProvider(root, spot_source=_spot_source, risk_free_rate=_RATE,
                               spot_scope="reserved-name")
    chain = p.get_chain(symbol, date(2023, 1, 3), expiry_min=date(2023, 1, 1),
                        expiry_max=date(2023, 12, 31))
    assert [c.symbol for c in chain] == [occ]
    assert p.get_bar(occ, date(2023, 1, 3))["close"] == pytest.approx(5.2)

    sigs = _published_sigs(root, symbol)
    assert len(sigs) == 1, "the arrays must actually be shared, not skipped for this symbol"
    assert sigs[0].parent.name == f"u_{symbol}.v{pq._RawUnderlying.ARRAYS_VERSION}"
