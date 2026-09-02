"""OptionsDataProviderInterface + the Alpaca / ThetaData implementations.

The ThetaData transport (rewritten 2026-09-02 for the official cloud ``thetadata`` Python
library) is exercised against a MOCKED client object built from column shapes VERIFIED live
against the real API with a real key (see the provider module's docstring) — no HTTP/gRPC
actually happens in these tests. What IS covered: OCC reconstruction, the bars+open-interest
merge (ThetaData does not fold OI into the bars call the way TastyTrade's dxfeed does), the
0-means-no-quote convention, per-(underlying,expiry) call grouping, and the CandleBatch
contract ``fetch_bars_detailed`` must honour for ``tools/warm_options_history.py``'s
retry loop.
"""
import sys
import threading
import types
from datetime import date, timedelta

import pandas as pd
import pytest

from ba2_common.core.interfaces import (
    CandleBatch, OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)
from ba2_providers import OPTIONS_PROVIDERS, get_provider
from ba2_providers.options.thetadata import ThetaDataOptionsProvider, _chunk_window, _occ_symbol
from types import SimpleNamespace


# --------------------------------------------------------------------------- #
# registry / contract
# --------------------------------------------------------------------------- #
def test_every_provider_registered_under_the_options_category():
    """One seam, three vendors: "alpaca" (the incumbent, floored at 2024-01-18), "thetadata"
    (deeper — a cloud API key, verified live to 2012 for AAPL) and "tastytrade" (dxfeed — the
    only one whose bars carry imp_volatility and open_interest without a second call). Adding
    a fourth belongs here, not in a parallel mechanism. Every provider here MUST construct
    with zero required args — options_history_floor() asks a provider its floor without
    opening any connection, credentials included."""
    assert set(OPTIONS_PROVIDERS) == {"alpaca", "thetadata", "tastytrade"}
    for name in ("alpaca", "thetadata", "tastytrade"):
        assert isinstance(get_provider("options", name), OptionsDataProviderInterface)


def test_alpaca_history_floor_is_the_measured_date_not_the_documented_one():
    """Alpaca's docs claim options history to 2016; measured against the live API it is a
    hard 2024-01-18 for every symbol/expiry, and no subscription tier moves it."""
    assert get_provider("options", "alpaca").history_floor() == date(2024, 1, 18)


def test_thetadata_floor_is_tier_driven_and_deeper_than_alpaca():
    """The whole reason this vendor exists: reach further back than 2024-01-18."""
    four_year = ThetaDataOptionsProvider(history_years=4).history_floor()
    eight_year = ThetaDataOptionsProvider(history_years=8).history_floor()
    assert eight_year < four_year < date(2024, 1, 18)


# --------------------------------------------------------------------------- #
# OCC reconstruction — ThetaData returns the contract as separate columns
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("underlying,expiry,right,strike,expected", [
    ("AAPL", date(2025, 1, 17), "C", 200.0, "AAPL250117C00200000"),
    ("SPY", date(2024, 6, 21), "put", 545.5, "SPY240621P00545500"),
    ("BAC", date(2024, 5, 17), "c", 41.0, "BAC240517C00041000"),
])
def test_occ_symbol_reconstruction(underlying, expiry, right, strike, expected):
    assert _occ_symbol(underlying, expiry, right, strike) == expected


# --------------------------------------------------------------------------- #
# ThetaData: construction / credentials
# --------------------------------------------------------------------------- #
def test_thetadata_constructs_with_zero_required_args():
    """The registry (OPTIONS_PROVIDERS, options_history_floor) constructs every provider
    with no args just to ask its history_floor() — must not need a live key for that."""
    p = ThetaDataOptionsProvider()
    assert isinstance(p.history_floor(), date)


def test_thetadata_floor_is_tier_driven_and_deeper_than_alpaca():
    """The whole reason this vendor exists: reach further back than 2024-01-18."""
    four_year = ThetaDataOptionsProvider(history_years=4).history_floor()
    eight_year = ThetaDataOptionsProvider(history_years=8).history_floor()
    assert eight_year < four_year < date(2024, 1, 18)


def test_thetadata_raises_a_named_error_when_asked_to_talk_to_the_network_with_no_key():
    p = ThetaDataOptionsProvider()  # no api_key, THETADATA_API_KEY unset in this env
    import ba2_providers.options.thetadata as mod
    import os
    old = os.environ.pop("THETADATA_API_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="THETADATA_API_KEY"):
            p._get_client()
    finally:
        if old is not None:
            os.environ["THETADATA_API_KEY"] = old


# --------------------------------------------------------------------------- #
# ThetaData: shared-session threading (found live 2026-09-02: 8 separate PROCESSES each
# calling ThetaClient(api_key=...) independently invalidated each other's sessions --
# "Invalid session ID. This can occur if more than one terminal is running." The library's
# fix is existing_authorized_client: a second client sharing an already-authenticated one's
# session rather than opening a competing one -- confirmed live (4 threads, zero errors).
# These tests exercise _get_client's orchestration of that against a FAKE thetadata module
# (installed into sys.modules) so no real package or network is needed.
# --------------------------------------------------------------------------- #
def _install_fake_thetadata_module(monkeypatch, theta_client_cls):
    fake_errors = types.ModuleType("thetadata.errors")

    class _FakeNoDataFoundError(Exception):
        pass

    fake_errors.NoDataFoundError = _FakeNoDataFoundError
    fake_thetadata = types.ModuleType("thetadata")
    fake_thetadata.ThetaClient = theta_client_cls
    fake_thetadata.errors = fake_errors
    monkeypatch.setitem(sys.modules, "thetadata", fake_thetadata)
    monkeypatch.setitem(sys.modules, "thetadata.errors", fake_errors)
    return _FakeNoDataFoundError


def test_get_client_authenticates_the_session_once_and_reuses_it_on_later_calls(monkeypatch):
    constructed = []

    class _FakeThetaClient:
        def __init__(self, api_key=None, existing_authorized_client=None, dataframe_type="pandas"):
            self.api_key = api_key
            self.existing_authorized_client = existing_authorized_client
            constructed.append(self)

    _install_fake_thetadata_module(monkeypatch, _FakeThetaClient)

    p = ThetaDataOptionsProvider(api_key="k")
    c1 = p._get_client()
    c2 = p._get_client()  # same thread, called again

    assert len(constructed) == 2  # 1 authenticated session + 1 thread-local client wrapping it
    main_session, wrapper = constructed
    assert main_session.api_key == "k" and main_session.existing_authorized_client is None
    assert wrapper.existing_authorized_client is main_session
    assert c1 is c2 is wrapper, "the SAME thread must reuse its own client, not rebuild it"


def test_get_client_gives_each_thread_its_own_client_sharing_one_session(monkeypatch):
    constructed = []

    class _FakeThetaClient:
        def __init__(self, api_key=None, existing_authorized_client=None, dataframe_type="pandas"):
            self.existing_authorized_client = existing_authorized_client
            constructed.append(self)

    _install_fake_thetadata_module(monkeypatch, _FakeThetaClient)

    p = ThetaDataOptionsProvider(api_key="k")
    seen = {}

    def worker(name):
        seen[name] = p._get_client()

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 4
    assert len({id(c) for c in seen.values()}) == 4, "each thread must get its OWN client object"
    sessions = {c.existing_authorized_client for c in seen.values()}
    assert len(sessions) == 1, "every thread's client must share the SAME underlying session"


def test_get_client_authenticates_only_once_under_a_concurrent_first_call_race(monkeypatch):
    """The exact live failure this redesign fixes: several threads all calling _get_client()
    for the FIRST time simultaneously must not each race to open their own competing
    session."""
    session_count = {"n": 0}

    class _FakeThetaClient:
        def __init__(self, api_key=None, existing_authorized_client=None, dataframe_type="pandas"):
            if existing_authorized_client is None:
                session_count["n"] += 1
            self.existing_authorized_client = existing_authorized_client

    _install_fake_thetadata_module(monkeypatch, _FakeThetaClient)

    p = ThetaDataOptionsProvider(api_key="k")
    threads = [threading.Thread(target=p._get_client) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert session_count["n"] == 1, \
        "exactly one authenticated session must be created, however many threads race for it"


def test_a_directly_injected_client_is_returned_verbatim_never_wrapped(monkeypatch):
    """The existing test-injection path (_wired: p._client = fake) must keep working exactly
    as before -- wrapping the fake in a real ThetaClient(existing_authorized_client=...)
    would need the real package and silently defeat every other test's mocking."""
    sentinel = object()
    p = ThetaDataOptionsProvider(api_key="k")
    p._client = sentinel
    assert p._get_client() is sentinel


# --------------------------------------------------------------------------- #
# ThetaData: bars + open-interest merge (mocked client — no network)
# --------------------------------------------------------------------------- #
# Column shapes VERIFIED live against the real cloud API (2026-09-02, a real "td1_prod_..."
# key, AAPL): option_history_greeks_eod returns the full bars+quote+greeks block (including
# implied_vol — a superset of the plain option_history_eod, so that one is never called);
# open_interest is a SEPARATE call, one row per (contract, day).
def _greeks_df(rows):
    """rows: list of dicts with at least strike/right/timestamp/close; other bar/greek
    columns default to NaN like a real sparse response would."""
    cols = ["symbol", "expiration", "strike", "right", "timestamp", "open", "high", "low",
           "close", "volume", "bid", "ask", "implied_vol"]
    return pd.DataFrame(rows, columns=cols)


def _oi_df(rows):
    cols = ["symbol", "expiration", "strike", "right", "timestamp", "open_interest"]
    return pd.DataFrame(rows, columns=cols)


class _FakeClient:
    def __init__(self, greeks=None, oi=None, expirations=None, strikes=None):
        self._greeks = greeks if greeks is not None else _greeks_df([])
        self._oi = oi if oi is not None else _oi_df([])
        self._expirations = expirations
        self._strikes = strikes or {}
        self.calls = []

    def option_history_greeks_eod(self, **kw):
        self.calls.append(("greeks_eod", kw))
        return self._greeks

    def option_history_open_interest(self, **kw):
        self.calls.append(("open_interest", kw))
        return self._oi

    def option_list_expirations(self, symbol):
        self.calls.append(("list_expirations", symbol))
        return self._expirations

    def option_list_strikes(self, symbol, expiration):
        self.calls.append(("list_strikes", (symbol, expiration)))
        return self._strikes[expiration]


class _FakeNoDataError(Exception):
    """Stand-in for thetadata.errors.NoDataFoundError -- _wired bypasses _get_client (the only
    place that resolves the real class), so a test exercising that path raises/expects THIS."""


def _wired(client: _FakeClient) -> ThetaDataOptionsProvider:
    p = ThetaDataOptionsProvider(api_key="test-key")
    p._client = client  # bypass the lazy `from thetadata import ThetaClient`
    p._no_data_exc = _FakeNoDataError  # ditto for thetadata.errors.NoDataFoundError
    return p


_EXPIRY = date(2025, 1, 17)
_CONTRACTS = [
    OptionContractMeta(occ_symbol="AAPL250117C00200000", underlying="AAPL",
                       option_type="call", strike=200.0, expiry=_EXPIRY),
    OptionContractMeta(occ_symbol="AAPL250117C00210000", underlying="AAPL",
                       option_type="call", strike=210.0, expiry=_EXPIRY),
]


def test_fetch_eod_bars_merges_bars_and_open_interest_by_strike_right_and_date():
    client = _FakeClient(
        greeks=_greeks_df([
            dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                timestamp="2024-10-01", open=10.0, high=11.0, low=9.5, close=10.5,
                volume=1234, bid=10.4, ask=10.6, implied_vol=0.28),
        ]),
        oi=_oi_df([
            dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                timestamp="2024-10-01", open_interest=5000),
        ]),
    )
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1
    b = bars[0]
    assert b.occ_symbol == "AAPL250117C00200000"
    assert b.bar_date == date(2024, 10, 1)
    assert (b.open, b.high, b.low, b.close) == (10.0, 11.0, 9.5, 10.5)
    assert b.volume == 1234
    assert (b.bid, b.ask) == (10.4, 10.6)
    assert b.iv == 0.28
    assert b.open_interest == 5000, "must be joined in from the SEPARATE open_interest call"


def test_zero_bid_ask_becomes_none_not_a_free_option():
    """ThetaData reports 0 for 'no quote'. Passing that through as a real price would look
    like a $0.00 contract to the arb guard — exactly the class of artifact that produced
    fabricated option profits before."""
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=210.0, right="CALL",
            timestamp="2024-10-01", open=1.0, high=1.2, low=0.9, close=1.1,
            volume=50, bid=0.0, ask=0.0, implied_vol=None),
    ]))
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1
    b = bars[0]
    assert b.bid is None and b.ask is None
    assert b.close == 1.1, "a missing QUOTE must not discard the traded price"
    assert b.iv is None, "a NaN/None implied_vol must not become 0.0"


def test_a_row_with_no_close_is_not_a_usable_bar():
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
            timestamp="2024-10-01", open=10.0, high=11.0, low=9.5, close=None,
            volume=0, bid=None, ask=None, implied_vol=None),
    ]))
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))
    assert bars == []


def test_fetch_eod_bars_only_yields_contracts_actually_requested():
    """The greeks_eod call returns the WHOLE chain for the expiry (strike='*'); a row for a
    strike not in the requested contract set must be filtered out, not yielded."""
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=999.0, right="CALL",
            timestamp="2024-10-01", open=1, high=1, low=1, close=1.0,
            volume=1, bid=None, ask=None, implied_vol=None),
    ]))
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))
    assert bars == [], "strike 999 was never in the requested contract set"


def test_one_greeks_call_and_one_open_interest_call_per_underlying_expiry():
    """Two contracts, SAME (underlying, expiry) -> exactly one greeks_eod + one
    open_interest call, not one per contract (that is the whole efficiency point of the
    wildcard strike='*' request). A non-empty greeks response, so the open_interest call
    is actually reached (an empty greeks response short-circuits it -- see
    test_open_interest_is_skipped_when_greeks_eod_has_no_rows)."""
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
            timestamp="2024-10-01", open=1, high=1, low=1, close=1.0,
            volume=1, bid=None, ask=None, implied_vol=None),
    ]))
    p = _wired(client)
    list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    kinds = [c[0] for c in client.calls]
    assert kinds == ["greeks_eod", "open_interest"]
    _, greeks_kwargs = client.calls[0]
    assert greeks_kwargs["strike"] == "*" and greeks_kwargs["right"] == "both"
    assert greeks_kwargs["symbol"] == "AAPL" and greeks_kwargs["expiration"] == _EXPIRY


def test_the_query_window_is_capped_at_the_contracts_own_expiry_not_the_runs_global_end():
    """An option never trades past its own expiration -- querying years past it (e.g. a
    2025-01-17 expiry all the way to a 2030 run end) wastes real requests on chunks
    guaranteed to answer 'nothing here'. Confirmed to matter live: a 2020-01-17 expiry
    queried to a 2026-09 run end spent 6 of 7 chunk-pairs on NoDataFoundError, ~9.5 minutes
    for that ONE unit."""
    calls_seen = []

    class _TrackingClient(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            calls_seen.append((kw["start_date"], kw["end_date"]))
            return _greeks_df([])

    client = _TrackingClient()
    p = _wired(client)
    list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 1, 1), end=date(2030, 1, 1)))

    assert calls_seen, "at least one call must have been made"
    for _, c_end in calls_seen:
        assert c_end <= _EXPIRY, \
            f"queried past the contract's own expiry ({_EXPIRY}): end_date={c_end}"


def test_the_query_window_start_is_never_narrowed_only_the_end():
    """A LEAPS contract can legitimately have started trading long before the run's start
    date -- only the END may be capped at the expiry; narrowing the START risks losing real
    data the contract genuinely has."""
    calls_seen = []

    class _TrackingClient(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            calls_seen.append(kw["start_date"])
            return _greeks_df([])

    client = _TrackingClient()
    p = _wired(client)
    list(p.fetch_eod_bars(_CONTRACTS, start=date(2010, 1, 1), end=date(2030, 1, 1)))

    assert calls_seen[0] == date(2010, 1, 1), "the run's start date must survive untouched"


def test_an_expiry_entirely_before_the_window_is_skipped_without_a_call():
    client = _FakeClient()
    p = _wired(client)
    # The window starts AFTER the contract's own expiry -- nothing to fetch, no call at all.
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2025, 6, 1), end=date(2025, 12, 1)))
    assert bars == []
    assert client.calls == []


def test_open_interest_is_skipped_when_greeks_eod_has_no_rows():
    """An expiry genuinely absent from the window (e.g. listed after `end`) must not spend a
    second call finding out its open interest is empty too."""
    client = _FakeClient()  # default greeks/oi are both empty
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))
    assert bars == []
    assert [c[0] for c in client.calls] == ["greeks_eod"]


# --------------------------------------------------------------------------- #
# ThetaData: NoDataFoundError -- the library RAISES for "genuinely nothing here"
# instead of returning an empty dataframe (found live 2026-09-02, the SAME backfill
# relaunch that found the 365-day cap: BJ's low-volume weeklies raised on every request).
# --------------------------------------------------------------------------- #
def test_greeks_eod_raising_no_data_is_treated_as_an_empty_chunk_not_an_error():
    class _RaisingClient(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            self.calls.append(("greeks_eod", kw))
            raise _FakeNoDataError("No data found")

    client = _RaisingClient()
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert bars == []
    # open_interest must NOT be called either -- same short-circuit as an empty dataframe.
    assert [c[0] for c in client.calls] == ["greeks_eod"]


def test_open_interest_raising_no_data_still_keeps_the_bars():
    """The bars are the point; missing OI for one chunk must not throw the whole chunk away."""
    class _RaisingOiClient(_FakeClient):
        def option_history_open_interest(self, **kw):
            self.calls.append(("open_interest", kw))
            raise _FakeNoDataError("No data found")

    client = _RaisingOiClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
            timestamp="2024-10-01", open=10.0, high=11.0, low=9.5, close=10.5,
            volume=1234, bid=10.4, ask=10.6, implied_vol=0.28),
    ]))
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1
    assert bars[0].close == 10.5
    assert bars[0].open_interest is None, "no OI data -> None, not a fabricated 0"


def test_a_nodata_chunk_does_not_abort_the_other_chunks_of_a_wide_window():
    """The exact shape that broke the real backfill: most Fridays for a thin name raise
    NoDataFoundError, but the one that DOES have data, in a LATER chunk, must still land."""
    calls_seen = []

    class _MixedClient(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            calls_seen.append(kw["start_date"])
            if kw["start_date"].year == 2020:
                raise _FakeNoDataError("No data found")
            return _greeks_df([dict(
                symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                timestamp="2021-06-01", open=1, high=1, low=1, close=33.0,
                volume=1, bid=None, ask=None, implied_vol=None)])

    client = _MixedClient()
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2020, 1, 1), end=date(2021, 12, 31)))

    assert len(calls_seen) == 2, "both chunks must still be requested"
    assert [b.close for b in bars] == [33.0]


# --------------------------------------------------------------------------- #
# ThetaData: 365-day-per-request cap (found live 2026-09-02, launching the
# 2020-2026 backfill: "Too many days between start and end date; max 365 days allowed")
# --------------------------------------------------------------------------- #
def test_chunk_window_leaves_a_narrow_window_untouched():
    assert _chunk_window(date(2024, 10, 1), date(2024, 10, 14)) == \
        [(date(2024, 10, 1), date(2024, 10, 14))]


def test_chunk_window_splits_at_exactly_365_days():
    start = date(2020, 1, 1)
    chunks = _chunk_window(start, date(2020, 1, 1) + timedelta(days=365))
    assert len(chunks) == 1, "exactly 365 days apart must still fit in ONE request"


def test_chunk_window_splits_a_multi_year_span_into_contiguous_non_overlapping_pieces():
    start, end = date(2020, 1, 1), date(2026, 9, 2)
    chunks = _chunk_window(start, end)

    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for c_start, c_end in chunks:
        assert (c_end - c_start).days <= 365
    # contiguous: each chunk starts the day after the previous one ends
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert next_start == prev_end + timedelta(days=1)


def test_fetch_eod_bars_chunks_a_wide_window_into_separate_requests_and_merges_the_bars():
    """The bug that broke the real 2020-2026 launch: every request carried the FULL window
    and ThetaData rejected every single one. Two chunks here, with DIFFERENT bars in each,
    proves both actually get fetched and yielded -- not just that two calls happen."""
    calls_seen = []

    class _ChunkedFakeClient(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            calls_seen.append(("greeks_eod", kw["start_date"], kw["end_date"]))
            # A distinct bar per chunk, dated inside that chunk's own window.
            if kw["start_date"].year == 2020:
                return _greeks_df([dict(
                    symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                    timestamp="2020-06-01", open=1, high=1, low=1, close=11.0,
                    volume=1, bid=None, ask=None, implied_vol=None)])
            return _greeks_df([dict(
                symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                timestamp="2021-06-01", open=1, high=1, low=1, close=22.0,
                volume=1, bid=None, ask=None, implied_vol=None)])

        def option_history_open_interest(self, **kw):
            calls_seen.append(("open_interest", kw["start_date"], kw["end_date"]))
            return _oi_df([])

    client = _ChunkedFakeClient()
    p = _wired(client)
    # > 365 days -> must chunk (2020-01-01 .. 2021-12-31 is ~730 days).
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2020, 1, 1), end=date(2021, 12, 31)))

    greeks_calls = [c for c in calls_seen if c[0] == "greeks_eod"]
    assert len(greeks_calls) == 2, "a >365-day window must be split into 2 requests"
    for _, c_start, c_end in greeks_calls:
        assert (c_end - c_start).days <= 365

    closes = sorted(b.close for b in bars)
    assert closes == [11.0, 22.0], "bars from BOTH chunks must be yielded, not just the first"


def test_fetch_bars_detailed_reports_missing_contracts_as_empty_never_unresolved():
    """ThetaData's API is plain request/response, not streaming — a contract with no bars in
    the window IS empty (a durable fact), never unresolved (work still owed)."""
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
            timestamp="2024-10-01", open=10.0, high=11.0, low=9.5, close=10.5,
            volume=1, bid=None, ask=None, implied_vol=None),
    ]))
    p = _wired(client)
    batch = p.fetch_bars_detailed(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1))

    assert isinstance(batch, CandleBatch)
    assert {b.occ_symbol for b in batch.bars} == {"AAPL250117C00200000"}
    assert batch.empty == {"AAPL250117C00210000"}
    assert batch.unresolved == set()
    assert batch.ok  # ok means "nothing unresolved", not "nothing empty"


# --------------------------------------------------------------------------- #
# ThetaData: discovery (mocked client — --discovery rest path only; the default
# --discovery synthetic never calls this)
# --------------------------------------------------------------------------- #
def test_discover_contracts_uses_list_expirations_and_list_strikes_with_filters():
    exps = pd.DataFrame({"symbol": ["AAPL", "AAPL"],
                         "expiration": ["2025-01-17", "2026-01-16"]})
    strikes = {date(2025, 1, 17): pd.DataFrame({"strike": [200.0, 210.0]})}
    client = _FakeClient(expirations=exps, strikes=strikes)
    p = _wired(client)

    got = p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1), expiry_lte=date(2025, 2, 1),
                               strike_min=205.0)

    assert {c.strike for c in got} == {210.0}, "the 2026 expiry is outside the window"
    assert {c.occ_symbol for c in got} == {"AAPL250117C00210000", "AAPL250117P00210000"}, \
        "strike_min=205 must drop the 200 strike; both call+put survive for the 210 strike"


# --------------------------------------------------------------------------- #
# Alpaca discovery (mocked client)
# --------------------------------------------------------------------------- #
def test_alpaca_discovery_includes_expired_contracts_and_drops_adjusted_ones():
    """status=ACTIVE alone returns nothing for a historical window (all expired), so
    INACTIVE must be queried too — the easiest way to build a silently-empty cache.
    Corporate-action ADJUSTED roots ('1SPY...') are rejected by the bars endpoint."""
    from alpaca.trading.enums import AssetStatus
    from ba2_providers.options.alpaca import AlpacaOptionsProvider

    inactive = [SimpleNamespace(symbol="SPY240621C00545000", expiration_date=date(2024, 6, 21),
                                strike_price=545.0, type=SimpleNamespace(value="call")),
                SimpleNamespace(symbol="1SPY240621P00370010", expiration_date=date(2024, 6, 21),
                                strike_price=370.01, type=SimpleNamespace(value="put"))]
    active = [SimpleNamespace(symbol="SPY260821C00700000", expiration_date=date(2026, 8, 21),
                              strike_price=700.0, type=SimpleNamespace(value="call"))]
    seen_statuses = []

    class _TC:
        def get_option_contracts(self, req):
            seen_statuses.append(req.status)
            rows = inactive if req.status == AssetStatus.INACTIVE else active
            return SimpleNamespace(option_contracts=rows)

    p = AlpacaOptionsProvider(api_key="k", api_secret="s")
    p._tc, p._dc = _TC(), object()
    got = p.discover_contracts("SPY", expiry_gte=date(2024, 1, 1), expiry_lte=date(2026, 12, 31))

    assert AssetStatus.INACTIVE in seen_statuses, "expired contracts must be queried"
    syms = {c.occ_symbol for c in got}
    assert "SPY240621C00545000" in syms and "SPY260821C00700000" in syms
    assert "1SPY240621P00370010" not in syms, "ADJUSTED root must be dropped"
