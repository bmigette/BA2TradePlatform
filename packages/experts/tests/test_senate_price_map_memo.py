"""``_get_price_at_date`` memoizes the ``{date: open}`` PROJECTION, never the payload.

The expert resolves execution prices for every ticker the Senate feed discloses (~1,800 of them
against the 498 actually traded), and each ``historical_price_full`` payload decodes to ~3.1 MB
of Python objects against ~275 KB for the projection built from it. Retaining the payload cost
2,465 MB and climbing in one measured trial (tracemalloc, 2026-09-05) for data each symbol needs
exactly once.
"""
import sys
from datetime import datetime

import pytest

mod = sys.modules.get('ba2_experts.FMPSenateTraderWeight')
if mod is None:
    import importlib
    mod = importlib.import_module('ba2_experts.FMPSenateTraderWeight')
FMPSenateTraderWeight = mod.FMPSenateTraderWeight


@pytest.fixture(autouse=True)
def _clean_memo():
    mod.clear_price_map_memo()
    yield
    mod.clear_price_map_memo()


@pytest.fixture()
def expert(monkeypatch):
    """A settings-free shell -- ``_get_price_at_date`` needs an api key and nothing else."""
    ex = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    ex._api_key = 'k'
    return ex


def _history(symbol='AAPL', n=3):
    return [{"date": f"2020-01-{i + 1:02d}", "open": 100.0 + i} for i in range(n)]


def _patch_history(monkeypatch, calls, payload=None):
    """Intercept the provider call, recording (symbol, retain) per invocation."""
    import ba2_providers.fmp_common as fc

    def _fake(namespace, symbol, fetch_fn, max_age_days=7.0, *, retain=True):
        calls.append((symbol, retain))
        return _history(symbol) if payload is None else payload

    monkeypatch.setattr(fc, 'fmp_history_disk_cached', _fake)
    return calls


def test_the_open_is_the_one_from_the_history(expert, monkeypatch):
    _patch_history(monkeypatch, [])

    assert expert._get_price_at_date('AAPL', datetime(2020, 1, 2)) == 101.0


def test_a_date_the_history_does_not_cover_is_None_not_a_guess(expert, monkeypatch):
    _patch_history(monkeypatch, [])

    assert expert._get_price_at_date('AAPL', datetime(2021, 6, 1)) is None


def test_the_payload_is_taken_WITHOUT_being_retained(expert, monkeypatch):
    calls = _patch_history(monkeypatch, [])

    expert._get_price_at_date('AAPL', datetime(2020, 1, 1))

    assert calls == [('AAPL', False)]


def test_the_history_is_read_ONCE_per_symbol_across_instances(expert, monkeypatch):
    """The memo is module-level on purpose: a pool child runs the whole GA population, and a
    per-instance map would re-parse every history once per trial."""
    calls = _patch_history(monkeypatch, [])

    expert._get_price_at_date('AAPL', datetime(2020, 1, 1))
    other = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    other._api_key = 'k'
    other._get_price_at_date('AAPL', datetime(2020, 1, 3))

    assert len(calls) == 1


def test_what_is_kept_is_the_projection_not_the_rows(expert, monkeypatch):
    _patch_history(monkeypatch, [])

    expert._get_price_at_date('AAPL', datetime(2020, 1, 1))

    assert mod._PRICE_MAP_MEM['AAPL'] == {'2020-01-01': 100.0, '2020-01-02': 101.0,
                                          '2020-01-03': 102.0}


def test_the_memo_is_BOUNDED(expert, monkeypatch):
    """A pool child outlives the job. Unbounded, this would accumulate every universe the box
    ever runs -- the failure it was introduced to fix, one structure further down."""
    _patch_history(monkeypatch, [])
    monkeypatch.setattr(mod, '_PRICE_MAP_MEM_MAX', 3)

    for i in range(6):
        expert._get_price_at_date(f'SYM{i}', datetime(2020, 1, 1))

    assert len(mod._PRICE_MAP_MEM) == 3
    assert list(mod._PRICE_MAP_MEM) == ['SYM3', 'SYM4', 'SYM5']   # oldest evicted


def test_a_reused_symbol_is_kept_over_an_untouched_one(expert, monkeypatch):
    """LRU, not FIFO: the symbols a run keeps asking about are the ones worth keeping."""
    _patch_history(monkeypatch, [])
    monkeypatch.setattr(mod, '_PRICE_MAP_MEM_MAX', 2)

    expert._get_price_at_date('OLD', datetime(2020, 1, 1))
    expert._get_price_at_date('NEW', datetime(2020, 1, 1))
    expert._get_price_at_date('OLD', datetime(2020, 1, 2))    # touch -> most recent
    expert._get_price_at_date('THIRD', datetime(2020, 1, 1))

    assert set(mod._PRICE_MAP_MEM) == {'OLD', 'THIRD'}


def test_an_empty_history_is_memoized_rather_than_re_read_every_bar(expert, monkeypatch):
    """A symbol the provider has nothing for is asked about on every bar it appears on. Not
    memoizing the empty would re-read the miss thousands of times."""
    calls = _patch_history(monkeypatch, [], payload=[])

    assert expert._get_price_at_date('GONE', datetime(2020, 1, 1)) is None
    assert expert._get_price_at_date('GONE', datetime(2020, 1, 2)) is None
    assert len(calls) == 1


def test_the_ticker_is_normalised_before_the_cache_key(expert, monkeypatch):
    """BRK/B from the disclosure feed and BRK-B in the price cache are one instrument; two keys
    would parse the same history twice and hold it twice."""
    calls = _patch_history(monkeypatch, [])

    expert._get_price_at_date('BRK/B', datetime(2020, 1, 1))
    expert._get_price_at_date('BRK-B', datetime(2020, 1, 1))

    assert len(calls) == 1
