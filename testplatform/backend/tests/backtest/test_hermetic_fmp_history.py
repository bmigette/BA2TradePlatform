"""Hermetic FMP-history contract — a backtest must run from pre-warmed caches only (0 fetch).

Mirrors the OHLCV hermetic contract: inside ``hermetic_fmp_history()`` a per-symbol history miss
raises ``FMPHistoryCacheMiss`` instead of silently network-fetching mid-run. Prewarm / live run
WITHOUT hermetic so they can populate the cache.
"""
import tempfile

import pytest

from ba2_providers import fmp_common as fc


@pytest.fixture()
def temp_cache(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(fc, "_fmp_history_cache_dir", lambda: d)
    # fresh in-process layer so disk behaviour is exercised, not a stale mem hit
    monkeypatch.setattr(fc, "_HISTORY_MEM_CACHE", fc.TTLCache(999999))
    return d


def test_hermetic_miss_raises_without_fetching(temp_cache):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [{"x": 1}]

    with fc.frozen_ttl_cache(), fc.hermetic_fmp_history():
        with pytest.raises(fc.FMPHistoryCacheMiss):
            fc.fmp_history_disk_cached("insider_v2", "NOPE", fetch)
    assert calls["n"] == 0  # 0 network fetches — the whole point


def test_prewarm_fetches_then_hermetic_serves(temp_cache, monkeypatch):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [{"x": 1}]

    # prewarm: frozen but NOT hermetic -> fetch + persist to disk
    with fc.frozen_ttl_cache():
        fc.fmp_history_disk_cached("insider_v2", "AAPL", fetch)
    assert calls["n"] == 1

    # new process -> clear the in-process layer so the disk file is what's served
    monkeypatch.setattr(fc, "_HISTORY_MEM_CACHE", fc.TTLCache(999999))
    calls["n"] = 0
    with fc.frozen_ttl_cache(), fc.hermetic_fmp_history():
        out = fc.fmp_history_disk_cached("insider_v2", "AAPL", fetch)
    assert out == [{"x": 1}] and calls["n"] == 0  # served from disk, 0 fetch


def test_empty_not_persisted_without_sentinel(temp_cache, monkeypatch):
    """Default: a genuinely-empty FMP payload is NOT cached (retried next run). The absent file
    then reads back as a fatal 'not pre-warmed' miss in hermetic mode."""
    with fc.frozen_ttl_cache():
        out = fc.fmp_history_disk_cached("past_earnings_quarterly", "BNH", lambda: [])
    assert out == []
    monkeypatch.setattr(fc, "_HISTORY_MEM_CACHE", fc.TTLCache(999999))
    with fc.frozen_ttl_cache(), fc.hermetic_fmp_history():
        with pytest.raises(fc.FMPHistoryCacheMiss):
            fc.fmp_history_disk_cached("past_earnings_quarterly", "BNH", lambda: [{"x": 1}])


def test_prewarm_persists_empty_as_sentinel(temp_cache, monkeypatch):
    """Under persist_empty_sentinel() (prewarm), a genuine empty is cached as ``[]`` so the next
    hermetic read serves it as 'checked, no data' (no signal) instead of raising 'not pre-warmed'.
    fmp_list_call RAISES on real FMP errors, so a falsy result here is a true no-data."""
    calls = {"n": 0}

    def empty_fetch():
        calls["n"] += 1
        return []

    with fc.frozen_ttl_cache(), fc.persist_empty_sentinel():
        out = fc.fmp_history_disk_cached("past_earnings_quarterly", "BNH", empty_fetch)
    assert out == [] and calls["n"] == 1

    # new process: the sentinel file is served, hermetic does NOT raise, 0 fetch
    monkeypatch.setattr(fc, "_HISTORY_MEM_CACHE", fc.TTLCache(999999))
    calls["n"] = 0
    with fc.frozen_ttl_cache(), fc.hermetic_fmp_history():
        out2 = fc.fmp_history_disk_cached("past_earnings_quarterly", "BNH", empty_fetch)
    assert out2 == [] and calls["n"] == 0


def test_live_path_is_passthrough(temp_cache):
    """Outside a frozen backtest the cache is bypassed entirely (live always pulls fresh)."""
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [{"x": 1}]

    for _ in range(3):
        fc.fmp_history_disk_cached("insider_v2", "MSFT", fetch)
    assert calls["n"] == 3  # no caching live
