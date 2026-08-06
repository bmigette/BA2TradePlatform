"""Tests for the survivorship-free universe builder (ba2_providers.screener.universe).

Pure list/replay logic — all FMP fetches are mocked. These tests prove:
  - broad_universe honours the [ipoDate, delistedDate] lifecycle window (a name that
    traded on the scan date but delisted later is PRESENT; gone after delistedDate),
  - index_universe inverts post-as_of add/remove change-log events to reconstruct
    dated membership,
  - fetch_lifecycle_map merges active (no delistedDate) + delisted lists and paginates.
"""
from datetime import datetime, timezone

import ba2_providers.screener.universe as U


def D(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class _R:
    """Minimal stand-in for a requests.Response with a .json()."""

    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


# --------------------------------------------------------------------------- #
# broad_universe — lifecycle window                                            #
# --------------------------------------------------------------------------- #


def test_broad_universe_lifecycle_window():
    lifecycle = {
        "ALIVE":  (D("2010-01-01"), None),             # active, IPO'd long ago
        "DEAD":   (D("2015-01-01"), D("2021-06-30")),  # delisted AFTER the scan date
        "NEWBIE": (D("2024-01-01"), None),             # IPO'd after the scan date
    }
    on = U.broad_universe(D("2020-06-30"), lifecycle=lifecycle)
    assert "ALIVE" in on
    assert "DEAD" in on        # survivorship: traded on 2020-06-30, delisted 2021 -> present
    assert "NEWBIE" not in on  # not yet public on the scan date


def test_broad_universe_excludes_delisted_after_death():
    lifecycle = {"DEAD": (D("2015-01-01"), D("2021-06-30"))}
    after = U.broad_universe(D("2022-01-03"), lifecycle=lifecycle)
    assert "DEAD" not in after  # gone after its delistedDate


def test_broad_universe_missing_ipo_is_always_eligible():
    # An active row whose ipoDate was absent (None) must not be excluded.
    lifecycle = {"NOIPO": (None, None)}
    assert "NOIPO" in U.broad_universe(D("1990-01-01"), lifecycle=lifecycle)


def test_broad_universe_boundary_dates_inclusive():
    # On the exact ipoDate and the exact delistedDate the symbol is tradable.
    lifecycle = {"EDGE": (D("2020-01-01"), D("2020-12-31"))}
    assert "EDGE" in U.broad_universe(D("2020-01-01"), lifecycle=lifecycle)  # == ipoDate
    assert "EDGE" in U.broad_universe(D("2020-12-31"), lifecycle=lifecycle)  # == delistedDate
    assert "EDGE" not in U.broad_universe(D("2019-12-31"), lifecycle=lifecycle)  # before IPO
    assert "EDGE" not in U.broad_universe(D("2021-01-01"), lifecycle=lifecycle)  # after delist


def test_broad_universe_is_sorted():
    lifecycle = {"ZZZ": (None, None), "AAA": (None, None), "MMM": (None, None)}
    out = U.broad_universe(D("2020-01-01"), lifecycle=lifecycle)
    assert out == sorted(out)


# --------------------------------------------------------------------------- #
# fetch_lifecycle_map — merge active + delisted, pagination                    #
# --------------------------------------------------------------------------- #


def test_fetch_lifecycle_map_merges_and_paginates(monkeypatch):
    calls = {"delisted_pages": []}

    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("/available-traded/list"):
            return _R([
                {"symbol": "alive", "ipoDate": "2010-05-01"},  # lowercase -> upper()
                {"symbol": "DUP", "ipoDate": "2011-01-01"},    # also in delisted (delisted wins)
                {"not_a": "symbol"},                           # skipped (no symbol)
            ])
        if url.endswith("/delisted-companies"):
            page = (params or {}).get("page")
            calls["delisted_pages"].append(page)
            if page == 0:
                # full page (100 rows) -> a second page must be fetched
                rows = [
                    {"symbol": f"D{i}", "ipoDate": "2012-01-01", "delistedDate": "2019-01-01"}
                    for i in range(99)
                ]
                rows.append({"symbol": "DUP", "ipoDate": "2011-01-01", "delistedDate": "2018-06-30"})
                return _R(rows)  # len == 100 == page size
            # short page -> last page
            return _R([{"symbol": "LAST", "ipoDate": "2013-01-01", "delistedDate": "2020-01-01"}])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")

    m = U.fetch_lifecycle_map()
    assert calls["delisted_pages"] == [0, 1]          # paginated until the short page
    assert m["ALIVE"] == (D("2010-05-01"), None)       # active -> delisted_date None
    assert m["DUP"] == (D("2011-01-01"), D("2018-06-30"))  # delisted list overrides active
    assert m["LAST"] == (D("2013-01-01"), D("2020-01-01"))
    assert "D0" in m and m["D0"][1] == D("2019-01-01")


# --------------------------------------------------------------------------- #
# index_universe — change-log replay                                          #
# --------------------------------------------------------------------------- #


def test_index_universe_replays_changelog(monkeypatch):
    # current members = {AAA, BBB, CCC}; after as_of, CCC was added and DDD removed.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("_constituent") and "historical" not in url:
            return _R([{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}])
        return _R([
            {"date": "2023-03-01", "symbol": "CCC", "removedTicker": ""},  # CCC added 2023 (after as_of)
            {"date": "2023-03-01", "symbol": "", "removedTicker": "DDD"},  # DDD removed 2023
        ])

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("sp500", D("2022-01-03"))
    assert "CCC" not in members  # added after as_of -> not a member then
    assert "DDD" in members      # removed after as_of -> a member then
    assert "AAA" in members and "BBB" in members


def test_index_universe_ignores_events_on_or_before_as_of(monkeypatch):
    # An add/remove that happened BEFORE as_of is already reflected in membership.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("_constituent") and "historical" not in url:
            return _R([{"symbol": "AAA"}, {"symbol": "BBB"}])
        return _R([
            {"date": "2019-01-01", "symbol": "AAA", "removedTicker": ""},  # before as_of -> ignored
            {"date": "2019-01-01", "symbol": "", "removedTicker": "ZZZ"},  # before as_of -> ignored
        ])

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("nasdaq", D("2022-01-03"))
    assert members == ["AAA", "BBB"]  # pre-as_of events do not perturb membership


def test_index_universe_added_security_alias(monkeypatch):
    # FMP change-log rows sometimes carry the added ticker under 'addedSecurity'.
    def fake_get(url, params=None, endpoint=None, timeout=None):
        if url.endswith("_constituent") and "historical" not in url:
            return _R([{"symbol": "AAA"}, {"symbol": "EEE"}])
        return _R([
            {"dateAdded": "2024-01-01", "addedSecurity": "EEE", "removedTicker": ""},
        ])

    monkeypatch.setattr(U, "fmp_http_get", fake_get)
    monkeypatch.setattr(U, "_api_key", lambda: "x")
    members = U.index_universe("sp500", D("2022-01-03"))
    assert "EEE" not in members  # added (via addedSecurity/dateAdded) after as_of -> removed
    assert "AAA" in members


def test_index_universe_rejects_unknown_index():
    import pytest
    with pytest.raises(ValueError):
        U.index_universe("russell2000", D("2022-01-03"))


# --- live-only short-TTL cache (2026-08-06) ---------------------------------------------------
# 16 live screener-based instances each pulled the whole market per cycle (available-traded +
# PAGINATED delisted-companies, then quote/OHLCV for the universe). That exhausted the FMP daily
# quota. The payloads are settings-independent, so they can be shared.

def test_lifecycle_map_is_fetched_once_and_shared_live(monkeypatch):
    import ba2_providers.fmp_common as fc
    import ba2_providers.screener.universe as U
    monkeypatch.setattr(fc, "_LIVE_BULK_CACHES", {})          # fresh cache per test
    monkeypatch.setattr(fc, "_is_ttl_frozen", lambda: False)   # live path

    calls = {"n": 0}
    def _fake():
        calls["n"] += 1
        return {"AAPL": (None, None)}
    monkeypatch.setattr(U, "_fetch_lifecycle_map_uncached", _fake)

    a, b, c = U.fetch_lifecycle_map(), U.fetch_lifecycle_map(), U.fetch_lifecycle_map()

    assert calls["n"] == 1, "the whole-market listing must be fetched once, not once per caller"
    assert a == b == c == {"AAPL": (None, None)}


def test_lifecycle_map_is_NOT_cached_in_a_frozen_backtest(monkeypatch):
    """A frozen TTLCache never expires, so caching a live-shaped payload would pin it for the
    whole run. Backtests must keep taking their own (hermetic disk) path."""
    import ba2_providers.fmp_common as fc
    import ba2_providers.screener.universe as U
    monkeypatch.setattr(fc, "_LIVE_BULK_CACHES", {})
    monkeypatch.setattr(fc, "_is_ttl_frozen", lambda: True)    # backtest path

    calls = {"n": 0}
    def _fake():
        calls["n"] += 1
        return {}
    monkeypatch.setattr(U, "_fetch_lifecycle_map_uncached", _fake)

    U.fetch_lifecycle_map(); U.fetch_lifecycle_map()

    assert calls["n"] == 2, "frozen/backtest runs must pass through, not memoize"


def test_live_cache_keys_do_not_collide_across_windows(monkeypatch):
    """The OHLCV key carries symbols AND the from/to window, so a different as_of or lookback
    never serves the wrong bars."""
    import ba2_providers.fmp_common as fc
    monkeypatch.setattr(fc, "_LIVE_BULK_CACHES", {})
    monkeypatch.setattr(fc, "_is_ttl_frozen", lambda: False)

    seen = []
    def make(tag):
        def _f():
            seen.append(tag); return tag
        return _f

    assert fc.fmp_live_cached("screener:ohlcv:AAPL:2026-01-01:2026-06-01", make("w1")) == "w1"
    assert fc.fmp_live_cached("screener:ohlcv:AAPL:2026-01-01:2026-06-27", make("w2")) == "w2"
    assert fc.fmp_live_cached("screener:ohlcv:AAPL:2026-01-01:2026-06-01", make("w1b")) == "w1"
    assert seen == ["w1", "w2"], "same window reuses; different window refetches"


def test_each_ttl_gets_its_own_cache(monkeypatch):
    """REGRESSION: TTLCache fixes its expiry at CONSTRUCTION, so one shared instance would give
    every caller whichever TTL constructed it first. The 6h lifecycle map is fetched before the
    quotes in a screen, so a shared cache pinned LIVE QUOTES for six hours.

    The clock is injected into the TTLCache instances rather than monkeypatched onto `time.time`:
    TTLCache takes `clock=time.time` as a DEFAULT ARGUMENT, bound at class-definition time, so
    patching the module attribute afterwards does nothing.
    """
    import ba2_providers.fmp_common as fc
    monkeypatch.setattr(fc, "_is_ttl_frozen", lambda: False)
    clock = {"t": 1000.0}
    now = lambda: clock["t"]
    monkeypatch.setattr(fc, "_LIVE_BULK_CACHES", {
        6 * 3600.0: fc.TTLCache(6 * 3600.0, clock=now),
        900.0: fc.TTLCache(900.0, clock=now),
    })

    fc.fmp_live_cached("bulk", lambda: "bulk-v1", ttl_seconds=6 * 3600.0)
    fc.fmp_live_cached("quote", lambda: "quote-v1", ttl_seconds=900.0)

    clock["t"] += 1800.0          # 30 min: past the quote TTL, well inside the bulk TTL

    assert fc.fmp_live_cached("quote", lambda: "quote-v2", ttl_seconds=900.0) == "quote-v2",         "a 15-min quote must expire at 15 min even though a 6h entry exists"
    assert fc.fmp_live_cached("bulk", lambda: "bulk-v2", ttl_seconds=6 * 3600.0) == "bulk-v1",         "the 6h entry must still be served"
