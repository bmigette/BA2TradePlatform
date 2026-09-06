"""``fmp_history_disk_cached(retain=False)`` must take the payload without pinning it.

WHY THIS EXISTS. The in-process memo behind ``fmp_history_disk_cached`` holds the fully decoded
payload for every ``(namespace, symbol)`` a backtest touches, and on the backtest (TTL-frozen)
path entries never expire. That is the right trade for a caller that re-reads a history on every
bar; it is pure cost for one that reads it once and projects it down.

Measured 2026-09-05 with tracemalloc on a single FMPSenateTraderWeight trial: ``json/decoder.py``
held 2,465 MB in 44.6M live objects and was still climbing linearly, past the flat 2,145 MB bar
cache — decoded ``historical_price_full`` payloads for the ~1,800 tickers the Senate feed
discloses, each already reduced by its caller to a ``{date: open}`` map.
"""
import pytest

from ba2_providers import fmp_common


@pytest.fixture(autouse=True)
def _frozen_and_clean(monkeypatch, tmp_path):
    """The BACKTEST path (TTL frozen) with an empty memo and an empty disk cache.

    Unfrozen, ``fmp_history_disk_cached`` is a straight passthrough and none of this applies.
    """
    fmp_common._HISTORY_MEM_CACHE._store.clear()
    monkeypatch.setattr(fmp_common, "_is_ttl_frozen", lambda: True)
    monkeypatch.setattr(fmp_common, "_fmp_history_cache_dir", lambda: str(tmp_path))
    yield
    fmp_common._HISTORY_MEM_CACHE._store.clear()


def _payload(n=3):
    return [{"date": f"2020-01-{i + 1:02d}", "open": float(i)} for i in range(n)]


def test_the_default_still_memoizes_so_a_per_bar_reader_parses_once():
    """The behaviour every other caller depends on: the second call does not re-read."""
    calls = []

    def _fetch():
        calls.append(1)
        return _payload()

    first = fmp_common.fmp_history_disk_cached("ns", "AAPL", _fetch)
    second = fmp_common.fmp_history_disk_cached("ns", "AAPL", _fetch)

    assert first is second                       # the SAME object, not an equal copy
    assert len(calls) == 1


def test_retain_false_does_not_pin_the_payload():
    fmp_common.fmp_history_disk_cached("ns", "AAPL", lambda: _payload(), retain=False)

    assert fmp_common._HISTORY_MEM_CACHE._store == {}


def test_retain_false_still_returns_the_payload():
    got = fmp_common.fmp_history_disk_cached("ns", "AAPL", lambda: _payload(), retain=False)

    assert got == _payload()


def test_retain_false_reuses_an_entry_SOMEONE_ELSE_memoized():
    """It declines to ADD, it never bypasses a hit — so two callers of one key can never end up
    holding two different objects for it."""
    retained = fmp_common.fmp_history_disk_cached("ns", "AAPL", lambda: _payload())

    def _must_not_run():
        raise AssertionError("re-read a key that was already memoized")

    assert fmp_common.fmp_history_disk_cached(
        "ns", "AAPL", _must_not_run, retain=False) is retained


def test_a_cached_None_is_a_HIT_not_a_miss():
    """``get_or_call`` caches ``None`` deliberately (a symbol the provider has nothing for), so
    ``peek`` must distinguish it from absence — hence the ``_MISSING`` sentinel."""
    fmp_common.fmp_history_disk_cached("ns", "GONE", lambda: None)
    calls = []

    def _fetch():
        calls.append(1)
        return _payload()

    assert fmp_common.fmp_history_disk_cached("ns", "GONE", _fetch, retain=False) is None
    assert calls == []


def test_retain_false_re_reads_every_time_when_nothing_is_memoized():
    """The cost of not retaining, stated: a caller that does NOT keep its own projection would
    pay the parse on every call. That is why the Senate expert memoizes the projection."""
    calls = []

    def _fetch():
        calls.append(1)
        return _payload()

    fmp_common.fmp_history_disk_cached("ns", "AAPL", _fetch, retain=False)
    fmp_common.fmp_history_disk_cached("ns", "AAPL", _fetch, retain=False)

    # Both calls went to disk-or-fetch; the first one persisted the file, so the second is
    # served from DISK (one fetch) but still re-parsed — the memo is what it skips.
    assert len(calls) == 1
    assert fmp_common._HISTORY_MEM_CACHE._store == {}


def test_peek_never_calls_the_loader_and_never_stores():
    assert fmp_common._HISTORY_MEM_CACHE.peek("ns__NOPE") is fmp_common._MISSING
    assert fmp_common._HISTORY_MEM_CACHE._store == {}
