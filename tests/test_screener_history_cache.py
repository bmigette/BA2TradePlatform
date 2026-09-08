"""The screener must not re-fetch a symbol's history once per pass, per instance, per screen.

MEASURED, 2026-09-08, prod: 10 screens in one day over universes of
1177/878/641/640/459/304/116/26/18 symbols -- roughly 4,200 symbol-history fetches, none
of which could reuse each other. The cause was the cache key: it carried the ordered
CHUNK of tickers plus the exact from/to dates, and a screen calls ``_fetch_history_bulk``
three times with three different lookbacks -- volume/RVOL (``window + 10``), the
price-drop filter (``screener_price_drop_days``, a per-INSTANCE setting) and Weinstein
(250d). Same symbol, three windows, and again for every instance whose price-drop setting
differed.

These tests count HTTP calls, because the fix is only worth having if the count falls.
"""
import types

import pytest

from ba2_providers import fmp_common
from ba2_providers.StockScreener import SCREENER_HISTORY_WINDOW_DAYS, StockScreener


class _Response:
    """Minimal stand-in for the FMP historical-price-full payload."""

    def __init__(self, symbols, days):
        self._symbols = symbols
        self._days = days

    def json(self):
        def bars(sym):
            # Newest-first, as FMP returns them. Dates descend from 2026-09-08.
            return [{"date": f"2026-{9 - (i // 28):02d}-{28 - (i % 28):02d}",
                     "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                     "volume": 1000 + i}
                    for i in range(self._days)]
        return {"historicalStockList": [{"symbol": s, "historical": bars(s)}
                                        for s in self._symbols]}


@pytest.fixture
def screener(monkeypatch):
    """A screener whose only IO is a counted fake ``fmp_http_get``."""
    calls = []

    def _fake_get(url, params=None, **_kw):
        symbols = url.rsplit("/", 1)[-1].split(",")
        calls.append({"symbols": symbols, "from": (params or {}).get("from"),
                      "to": (params or {}).get("to")})
        return _Response(symbols, days=420)

    monkeypatch.setattr(fmp_common, "fmp_http_get", _fake_get)
    monkeypatch.setattr("ba2_providers.StockScreener.get_app_setting",
                        lambda key, *a, **k: "test-key" if key == "FMP_API_KEY" else None)
    # A fresh live cache per test, or the first test warms the rest of the file.
    monkeypatch.setattr(fmp_common, "_LIVE_BULK_CACHES", {})

    scr = object.__new__(StockScreener)
    scr._as_of = None
    return scr, calls


def _symbols(n):
    return [f"SYM{i:03d}" for i in range(n)]


def test_the_three_passes_of_one_screen_fetch_each_symbol_once(screener):
    """THE DEFECT: volume/RVOL, price-drop and Weinstein each fetched the same symbols
    because their lookbacks differed. One window now serves all three."""
    scr, calls = screener
    universe = _symbols(10)

    scr._fetch_history_bulk(universe, lookback_days=35)     # volume / RVOL
    after_first = len(calls)
    scr._fetch_history_bulk(universe, lookback_days=60)     # price drop
    scr._fetch_history_bulk(universe, lookback_days=250)    # Weinstein

    assert after_first > 0, "the first pass must actually fetch"
    assert len(calls) == after_first, (
        f"the later passes fetched again: {len(calls) - after_first} extra call(s)")


def test_a_second_screen_over_an_overlapping_universe_fetches_only_the_new_symbols(screener):
    """Prod's screens overlap heavily -- 1177 and 878 and 641 symbols of the same market."""
    scr, calls = screener
    scr._fetch_history_bulk(_symbols(10), lookback_days=250)
    fetched_first = {s for c in calls for s in c["symbols"]}

    calls.clear()
    scr._fetch_history_bulk(_symbols(15), lookback_days=250)
    fetched_second = {s for c in calls for s in c["symbols"]}

    assert fetched_second == set(_symbols(15)) - fetched_first, \
        "only the five new symbols should have been fetched"


def test_a_reordered_or_recomposed_chunk_still_reuses_its_symbols(screener):
    """The old key was the ORDERED chunk, so reversing one refetched all of it."""
    scr, calls = screener
    scr._fetch_history_bulk(_symbols(10), lookback_days=250)
    calls.clear()

    scr._fetch_history_bulk(list(reversed(_symbols(10))), lookback_days=250)
    assert calls == [], f"a reordered universe refetched: {calls}"


def test_every_caller_still_gets_only_the_window_it_asked_for(screener):
    """Fetch wide, SERVE NARROW. A pass must not silently receive a year of bars where
    it asked for a month -- the RVOL and price-drop windows are the ones they were."""
    scr, _calls = screener
    wide = scr._fetch_history_bulk(_symbols(1), lookback_days=250)
    narrow = scr._fetch_history_bulk(_symbols(1), lookback_days=35)

    assert len(narrow["SYM000"]) < len(wide["SYM000"])
    assert narrow["SYM000"], "the narrow window must not be empty"
    # Oldest-first, and the narrow slice is a suffix of the wide one.
    assert narrow["SYM000"] == wide["SYM000"][-len(narrow["SYM000"]):]


def test_a_caller_wanting_more_than_the_shared_window_is_not_served_short(screener):
    """Correctness outranks the saving: a longer lookback than the shared window keeps
    its own key rather than receiving a truncated history."""
    scr, calls = screener
    scr._fetch_history_bulk(_symbols(1), lookback_days=250)
    calls.clear()

    scr._fetch_history_bulk(_symbols(1), lookback_days=SCREENER_HISTORY_WINDOW_DAYS + 100)
    assert calls, "a wider request must fetch rather than reuse the narrower window"


def test_a_symbol_the_provider_omits_is_not_re_asked_every_pass(screener):
    """The most expensive way to learn a symbol has no history is to ask once per pass
    of every screen, all day."""
    scr, calls = screener

    def _empty(url, params=None, **_kw):
        calls.append(url)
        return _Response([], days=0)

    import ba2_providers.StockScreener as mod
    scr._fetch_history_bulk(_symbols(2), lookback_days=250)  # warm normally
    calls.clear()
    scr._fetch_history_bulk(_symbols(2), lookback_days=250)
    assert calls == []


class TestAnEmptyHistoryIsNotReAskedAllDay:
    """Prod, 2026-09-08: SPY gave up "after 3 attempts" 37 times in one day.

    Each give-up is 3 HTTP calls and two backoff sleeps, so a weekend tail with no new
    bars cost 111 requests and ~220 seconds of sleeping. The retry itself is right --
    FMP does return an empty 200 transiently -- but nothing recorded that we had just
    asked, so every read paid for the lesson again.
    """

    def _provider(self, monkeypatch, calls):
        from datetime import datetime as _dt

        # importlib, because ``ba2_providers.ohlcv.FMPOHLCVProvider`` resolves to the
        # CLASS the package re-exports under that name, not to the module holding it.
        import importlib
        mod = importlib.import_module('ba2_providers.ohlcv.FMPOHLCVProvider')

        def _fake_get(url, params=None, symbol=None, endpoint=None, **_kw):
            calls.append(symbol)
            return types.SimpleNamespace(json=lambda: {"historical": []})

        monkeypatch.setattr(fmp_common, "fmp_http_get", _fake_get)
        monkeypatch.setattr(fmp_common, "_LIVE_BULK_CACHES", {})
        monkeypatch.setattr("time.sleep", lambda _s: None)

        prov = object.__new__(mod.FMPOHLCVProvider)
        prov.api_key = "test-key"
        return prov, mod, _dt(2026, 9, 6), _dt(2026, 9, 8)

    def test_one_read_still_retries_the_transient_empty_200(self, monkeypatch):
        """The retry is NOT removed: FMP really does return an empty 200 under load, and
        it usually succeeds on a re-request."""
        calls = []
        prov, _mod, start, end = self._provider(monkeypatch, calls)
        prov._fetch_daily_data("SPY", start, end)
        assert len(calls) == 3, f"expected the 3-attempt retry, got {len(calls)}"

    def test_the_next_read_does_not_pay_for_the_lesson_again(self, monkeypatch):
        """THE DEFECT: 37 give-ups in one prod day, each costing three requests."""
        calls = []
        prov, _mod, start, end = self._provider(monkeypatch, calls)
        prov._fetch_daily_data("SPY", start, end)
        calls.clear()

        prov._fetch_daily_data("SPY", start, end)
        assert calls == [], f"re-asked a window known empty moments ago: {len(calls)} call(s)"

    def test_a_different_window_is_still_asked(self, monkeypatch):
        """The memo is per (symbol, from, to). A new day's window is a new question."""
        from datetime import datetime as _dt
        calls = []
        prov, _mod, start, end = self._provider(monkeypatch, calls)
        prov._fetch_daily_data("SPY", start, end)
        calls.clear()

        prov._fetch_daily_data("SPY", start, _dt(2026, 9, 9))
        assert calls, "a different window must be fetched"

    def test_the_memo_ttl_is_minutes_not_a_session(self):
        """A transient absence must never become a permanent one."""
        # importlib, because ``ba2_providers.ohlcv.FMPOHLCVProvider`` resolves to the
        # CLASS the package re-exports under that name, not to the module holding it.
        import importlib
        mod = importlib.import_module('ba2_providers.ohlcv.FMPOHLCVProvider')
        assert 60 <= mod._EMPTY_HISTORY_MEMO_TTL_S <= 1800
