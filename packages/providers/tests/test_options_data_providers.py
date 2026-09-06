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


def _eod_df(rows):
    """An option_history_eod response: the WIDE endpoint's 20-column shape. Note the date
    column is ``created`` (greeks_eod calls it ``timestamp``) and there is NO implied_vol."""
    cols = ["symbol", "expiration", "strike", "right", "created", "open", "high", "low",
           "close", "volume", "bid", "ask"]
    return pd.DataFrame(rows, columns=cols)


class _FakeClient:
    def __init__(self, greeks=None, oi=None, expirations=None, strikes=None,
                 list_dates=None, eod=None):
        self._greeks = greeks if greeks is not None else _greeks_df([])
        self._oi = oi if oi is not None else _oi_df([])
        self._eod = eod
        self._expirations = expirations
        self._strikes = strikes or {}
        #: None -> the start-clamp probe finds nothing and the caller's own start stands,
        #: which is what every test written before that optimisation expects.
        self._list_dates = list_dates
        self.calls = []

    def option_list_dates(self, **kw):
        self.calls.append(("list_dates", kw))
        if self._list_dates is None:
            raise _FakeNoDataError("no dates")
        return self._list_dates

    def option_history_greeks_eod(self, **kw):
        self.calls.append(("greeks_eod", kw))
        return self._greeks

    def option_history_eod(self, **kw):
        """The WIDE shape's bars endpoint (expiration="*"). Separate from greeks_eod: it is
        the only one that accepts a multi-day range with expiration="*", and it carries no
        implied_vol -- see fetch_underlying_eod_bars."""
        self.calls.append(("history_eod", kw))
        return self._eod if self._eod is not None else _eod_df([])

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
    """A zero ASK means 'no quote': passing it through as a real price would look like a
    $0.00 contract to the arb guard — exactly the class of artifact that produced fabricated
    option profits before.

    Contrast test_a_zero_bid_with_a_real_ask_is_kept_as_a_real_quote: a zero BID alongside a
    real ask is the opposite case (a genuine 'nobody is bidding' quote) and IS kept. The two
    zeros mean opposite things; see thetadata._quote."""
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

    # list_dates is the first-traded-date probe (an optimisation, not a data call) -- the
    # claim under test is about the DATA calls: one per (underlying, expiry), not per contract.
    kinds = [c[0] for c in client.calls if c[0] != "list_dates"]
    assert kinds == ["greeks_eod", "open_interest"]
    _, greeks_kwargs = next(c for c in client.calls if c[0] == "greeks_eod")
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
    assert [c[0] for c in client.calls if c[0] != "list_dates"] == ["greeks_eod"]


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
    assert [c[0] for c in client.calls if c[0] != "list_dates"] == ["greeks_eod"]


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

    # Monthly windows (see _REQUEST_WINDOW_DAYS) -> many chunks, the 2020 ones all raising.
    assert any(d.year == 2020 for d in calls_seen), "the raising chunks must be requested"
    assert any(d.year == 2021 for d in calls_seen), "later chunks must still be requested"
    assert set(b.close for b in bars) == {33.0}, "the chunk that HAS data must still land"


# --------------------------------------------------------------------------- #
# ThetaData: 365-day-per-request cap (found live 2026-09-02, launching the
# 2020-2026 backfill: "Too many days between start and end date; max 365 days allowed")
# --------------------------------------------------------------------------- #
def test_chunk_window_leaves_a_narrow_window_untouched():
    assert _chunk_window(date(2024, 10, 1), date(2024, 10, 14)) == \
        [(date(2024, 10, 1), date(2024, 10, 14))]


def test_chunk_window_splits_at_exactly_365_days():
    """The SERVER cap, asked for explicitly. The default request size is deliberately far
    narrower (see _REQUEST_WINDOW_DAYS) -- this pins the ceiling that must never be crossed,
    not the size we normally ask for."""
    from ba2_providers.options.thetadata import _MAX_WINDOW_DAYS
    start = date(2020, 1, 1)
    chunks = _chunk_window(start, date(2020, 1, 1) + timedelta(days=365),
                           max_days=_MAX_WINDOW_DAYS)
    assert len(chunks) == 1, "exactly 365 days apart must still fit in ONE request"


def test_the_requested_window_is_a_month_not_the_full_server_cap():
    """ThetaData support 2026-09-03: an EOD request SCANS the range asked for, so a 1-year
    window costs far more than the rows it returns; one month per request is their guidance.
    Requests are not rate limited (only concurrency is), so more, narrower requests is a
    straight win -- and this is the constant that decides it."""
    from ba2_providers.options.thetadata import _MAX_WINDOW_DAYS, _REQUEST_WINDOW_DAYS
    assert _REQUEST_WINDOW_DAYS <= 31
    assert _REQUEST_WINDOW_DAYS < _MAX_WINDOW_DAYS
    chunks = _chunk_window(date(2024, 1, 1), date(2024, 12, 31))
    assert len(chunks) >= 12, "a year must be split into monthly requests by default"
    for c_start, c_end in chunks:
        assert (c_end - c_start).days <= _REQUEST_WINDOW_DAYS


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
            # ONE bar in the whole span per year, each inside its own chunk's window, so the
            # assertion below proves both years' chunks were fetched and merged rather than
            # counting duplicates of a frame the fake hands back for every chunk.
            if kw["start_date"] <= date(2020, 6, 1) <= kw["end_date"]:
                return _greeks_df([dict(
                    symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                    timestamp="2020-06-01", open=1, high=1, low=1, close=11.0,
                    volume=1, bid=None, ask=None, implied_vol=None)])
            if kw["start_date"] <= date(2021, 6, 1) <= kw["end_date"]:
                return _greeks_df([dict(
                    symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
                    timestamp="2021-06-01", open=1, high=1, low=1, close=22.0,
                    volume=1, bid=None, ask=None, implied_vol=None)])
            return _greeks_df([])

        def option_history_open_interest(self, **kw):
            calls_seen.append(("open_interest", kw["start_date"], kw["end_date"]))
            return _oi_df([])

    client = _ChunkedFakeClient()
    p = _wired(client)
    # ~730 days -> many monthly requests (was 2 back when the window was the 365-day cap).
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2020, 1, 1), end=date(2021, 12, 31)))

    from ba2_providers.options.thetadata import _REQUEST_WINDOW_DAYS
    greeks_calls = [c for c in calls_seen if c[0] == "greeks_eod"]
    assert len(greeks_calls) >= 2, "a wide window must be split into separate requests"
    for _, c_start, c_end in greeks_calls:
        assert (c_end - c_start).days <= _REQUEST_WINDOW_DAYS

    closes = sorted(b.close for b in bars)
    assert closes == [11.0, 22.0], "bars from BOTH chunks must be yielded, not just the first"


def test_the_window_starts_at_the_expirys_FIRST_TRADED_date_not_the_runs_start():
    """A contract does not exist before it is listed. AAPL's 2024-06-21 expiry first traded
    2022-03-11 (measured live), so a 2020-01-01 run start scans years it could not have
    traded in. option_list_dates answers that in one small call."""
    import pandas as pd
    seen = []

    class _ClampFake(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            seen.append((kw["start_date"], kw["end_date"]))
            return _greeks_df([])

        def option_history_open_interest(self, **kw):
            return _oi_df([])

    client = _ClampFake(list_dates=pd.DataFrame([{"date": "2024-12-02"}, {"date": "2024-12-03"}]))
    p = _wired(client)
    list(p.fetch_eod_bars(_CONTRACTS, start=date(2020, 1, 1), end=date(2025, 1, 17)))

    assert seen, "the fetch must still issue requests"
    assert min(s for s, _ in seen) == date(2024, 12, 2), \
        "the first request must start at the first TRADED date, not the run's start"


def test_a_failing_first_traded_probe_leaves_the_runs_own_start_untouched():
    """The clamp is an optimisation: if option_list_dates errors or returns nothing, the
    fetch must still run over the caller's original window rather than fail or skip."""
    seen = []

    class _NoDatesFake(_FakeClient):
        def option_history_greeks_eod(self, **kw):
            seen.append((kw["start_date"], kw["end_date"]))
            return _greeks_df([])

        def option_history_open_interest(self, **kw):
            return _oi_df([])

    client = _NoDatesFake(list_dates=None)  # -> the probe raises
    p = _wired(client)
    list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 12, 1), end=date(2024, 12, 20)))

    assert seen and min(s for s, _ in seen) == date(2024, 12, 1)


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


# --------------------------------------------------------------------------- #
# No-trade days and zero bids (2026-09-03).
#
# ThetaData's EOD OHLC is a TRADE statistic: on a day the contract did not trade it reports
# 0.0 for open/high/low/close. Measured across 4 underlyings x 4 years (4,944 rows, zero
# exceptions) close > 0 iff volume > 0. Storing that 0.0 as a price marks a contract that
# merely did not trade as WORTHLESS -- 44.9% of a liquid chain's rows, 28.3% of which carry a
# real two-sided quote at a median $60.75 mid.
#
# Separately, a 0.00 BID is a real quote ("nobody is bidding"), not a missing one: verified
# live, such rows still carry a bid_exchange stamp and a real ask with size. Coercing it to
# None erases the difference between "we know nobody bids" and "we do not know".
# --------------------------------------------------------------------------- #
def _no_trade_client(**overrides):
    row = dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
               timestamp="2024-10-01", open=0.0, high=0.0, low=0.0, close=0.0,
               volume=0, bid=55.10, ask=56.20, implied_vol=0.31)
    row.update(overrides)
    return _FakeClient(greeks=_greeks_df([row]))


def test_a_no_trade_day_stores_no_ohlc_rather_than_a_zero_price():
    """close/open/high/low must be None, NOT 0.0 -- 0.0 reads downstream as a free option."""
    bars = list(_wired(_no_trade_client()).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1, "a no-trade day still has a quote, so the bar must be KEPT"
    b = bars[0]
    assert b.close is None and b.open is None and b.high is None and b.low is None
    assert b.close != 0.0, "0.0 would mark a $55-bid contract worthless"
    # ...and the day's real mark survives, which is the whole point of keeping the row.
    assert (b.bid, b.ask) == (55.10, 56.20)


def test_a_traded_day_keeps_its_real_ohlc():
    """The no-trade rule must not swallow genuine prices."""
    bars = list(_wired(_no_trade_client(open=10.0, high=11.0, low=9.5, close=10.5,
                                        volume=1234)).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1
    b = bars[0]
    assert (b.open, b.high, b.low, b.close) == (10.0, 11.0, 9.5, 10.5)


def test_a_zero_bid_with_a_real_ask_is_kept_as_a_real_quote():
    """bid == 0.0 alongside a real ask means 'nobody is bidding' -- information a liquidity
    gate needs, and 16.6% of a liquid chain. Distinct from a zero ASK, which means no quote
    at all (test_zero_bid_ask_becomes_none_not_a_free_option)."""
    bars = list(_wired(_no_trade_client(bid=0.0, ask=0.01)).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1
    b = bars[0]
    assert b.bid == 0.0, "a 0.00 bid must not be coerced to None"
    assert b.bid is not None
    assert b.ask == 0.01


def test_a_zero_ask_drops_the_quote_even_on_a_no_trade_day():
    """No trade AND no quote (ask == 0) is no information -- the row must not reach the
    store, or a price-less chain row silently skips the selector's penny gate."""
    bars = list(_wired(_no_trade_client(bid=0.0, ask=0.0)).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert bars == []


def test_a_row_with_neither_a_trade_nor_a_quote_is_dropped():
    """A price-less chain row SKIPS the selector's penny gate instead of being rejected by
    it, so it must never reach the store."""
    bars = list(_wired(_no_trade_client(bid=None, ask=None)).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert bars == [], "no trade AND no quote is no information at all"


# --------------------------------------------------------------------------- #
# The WIDE shape (expiration="*") -- fetch_underlying_eod_bars.
#
# CURRENT-DAY GUARD. ThetaData rejects a wide window that touches TODAY with
# "Cannot fetch current-day data without specifying an expiration" (INVALID_ARGUMENT). A
# backfill's window ends at "today" by default, so without a clamp the LAST chunk of EVERY
# symbol raises -- and it raises at the END, after the full multi-year fetch has been paid
# for, with a PERMANENT error the retry loop does not rescue. Observed live 2026-09-03: ABEV
# died after 941 s having written nothing, and every other symbol would have died the same
# way, producing a run of exactly zero partitions.
# --------------------------------------------------------------------------- #
def _wide_client():
    today = date.today()
    return _FakeClient(eod=_eod_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
             created=(today - timedelta(days=5)).isoformat(),
             open=10.0, high=11.0, low=9.5, close=10.5, volume=1234, bid=10.4, ask=10.6),
    ]))


def _requested_windows(client):
    """(start_date, end_date) of every wide bars request the provider issued."""
    return [(kw["start_date"], kw["end_date"])
            for name, kw in client.calls if name == "history_eod"]


def test_the_wide_shape_never_requests_the_current_day():
    """A window touching today is rejected outright by the server, so it must be clamped."""
    client = _wide_client()
    today = date.today()
    list(_wired(client).fetch_underlying_eod_bars(
        "AAPL", start=today - timedelta(days=40), end=today))

    windows = _requested_windows(client)
    assert windows, "the fetch must still happen -- clamping is not skipping"
    for w_start, w_end in windows:
        assert w_end < today, f"requested {w_start}..{w_end}, which includes today"


def test_the_wide_shape_asks_for_nothing_when_the_window_is_only_today():
    """Clamping to yesterday can empty the window; that must be a quiet no-op, not a request
    the server will refuse."""
    client = _wide_client()
    today = date.today()
    bars = list(_wired(client).fetch_underlying_eod_bars("AAPL", start=today, end=today))

    assert bars == []
    assert _requested_windows(client) == [], "no request may be issued at all"


def test_the_wide_shape_still_fetches_a_historical_window_untouched():
    """The clamp must not narrow a window that never reached today in the first place."""
    client = _wide_client()
    list(_wired(client).fetch_underlying_eod_bars(
        "AAPL", start=date(2024, 5, 1), end=date(2024, 5, 31)))

    windows = _requested_windows(client)
    assert windows[0][0] == date(2024, 5, 1)
    assert max(w_end for _s, w_end in windows) == date(2024, 5, 31)


def test_the_wide_shape_yields_in_non_decreasing_date_order():
    """A load-bearing guarantee, not a nicety.

    tools/warm_options_history.py writes an expiry's partition as soon as a bar dated after
    that expiry arrives. If an earlier-dated bar could follow a later one, it would land on an
    already-written partition and be DROPPED -- silently truncating it. The server returns
    rows contract-major, not date-major, so the provider must sort.
    """
    client = _FakeClient(eod=_eod_df([
        # deliberately out of date order, and interleaved across two expiries -- the shape a
        # contract-major response actually has
        dict(symbol="AAPL", expiration="2024-05-17", strike=200.0, right="CALL",
             created="2024-05-16", open=1.0, high=1.0, low=1.0, close=1.0,
             volume=1, bid=0.9, ask=1.1),
        dict(symbol="AAPL", expiration="2024-06-21", strike=200.0, right="CALL",
             created="2024-05-02", open=2.0, high=2.0, low=2.0, close=2.0,
             volume=1, bid=1.9, ask=2.1),
        dict(symbol="AAPL", expiration="2024-05-17", strike=200.0, right="CALL",
             created="2024-05-10", open=3.0, high=3.0, low=3.0, close=3.0,
             volume=1, bid=2.9, ask=3.1),
    ]))
    bars = list(_wired(client).fetch_underlying_eod_bars(
        "AAPL", start=date(2024, 5, 1), end=date(2024, 5, 31)))

    dates = [b.bar_date for b in bars]
    assert dates == sorted(dates), f"bars must be non-decreasing by date, got {dates}"


def test_the_current_day_clamp_uses_EXCHANGE_time_not_machine_time(monkeypatch):
    """A CET machine at 01:00 is still on the PREVIOUS trading day in New York.

    Measured live 2026-09-04: the clamp used date.today() and worked all evening, then began
    failing again the moment local midnight passed -- 01:01 CET on the 4th is 19:01 ET on the
    3rd, so "local yesterday" was still the server's CURRENT day and it refused the request.
    Every symbol processed after local midnight lost its final windows.
    """
    import ba2_providers.options.thetadata as mod

    # Exchange says the 3rd; a CET machine would say the 4th.
    monkeypatch.setattr(mod, "_exchange_today", lambda: date(2026, 9, 3))
    client = _wide_client()
    list(_wired(client).fetch_underlying_eod_bars(
        "AAPL", start=date(2026, 8, 1), end=date(2026, 9, 4)))

    latest = max(w_end for _s, w_end in _requested_windows(client))
    assert latest == date(2026, 9, 2), (
        f"must stop at the day before the EXCHANGE's today (2026-09-02), got {latest}")


def test_exchange_today_is_a_real_date_and_not_ahead_of_local():
    """Sanity: the helper resolves (tzdata present) and cannot be in the future relative to
    the machine -- ET is never ahead of UTC-or-later machine clocks by a whole day."""
    import ba2_providers.options.thetadata as mod

    et = mod._exchange_today()
    assert isinstance(et, date)
    assert et <= date.today(), "ET can lag the local date, never lead it by a day"


# --------------------------------------------------------------------------- #
# Vendor IV sentinels (found by auditing the live backfill, 2026-09-04).
#
# ThetaData signals "could not invert" with an INT32_MAX sentinel rather than a null, at more
# than one scale factor: 2147483646/10000 = 214748.3646 and /100000 = 21474.8365 both appear,
# plus a tail of 10^2-10^3 values -- together 0.4% of rows. IV is a DECIMAL here (0.2841 ==
# 28.41%), so those are 2-21 MILLION percent. Stored as-is they look like measurements.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sentinel", [214748.3646, 21474.8365, 2147483646.0, 150.0])
def test_an_implausible_vendor_iv_is_dropped_not_stored(sentinel):
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
             timestamp="2024-10-01", open=1.0, high=1.2, low=0.9, close=1.1,
             volume=50, bid=1.0, ask=1.2, implied_vol=sentinel),
    ]))
    bars = list(_wired(client).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))

    assert len(bars) == 1, "the BAR is still good -- only its iv is unusable"
    assert bars[0].iv is None, f"{sentinel} is not a volatility measurement"
    assert bars[0].close == 1.1, "dropping a bad iv must not discard the price"


@pytest.mark.parametrize("iv", [0.0625, 0.4147, 1.5, 5.0, 99.9])
def test_a_plausible_vendor_iv_is_kept(iv):
    """The cutoff must not eat real values. 98.07% of live rows are 0 < iv <= 5."""
    client = _FakeClient(greeks=_greeks_df([
        dict(symbol="AAPL", expiration="2025-01-17", strike=200.0, right="CALL",
             timestamp="2024-10-01", open=1.0, high=1.2, low=0.9, close=1.1,
             volume=50, bid=1.0, ask=1.2, implied_vol=iv),
    ]))
    bars = list(_wired(client).fetch_eod_bars(
        _CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))
    assert bars[0].iv == iv


def test_both_fetch_paths_apply_the_same_iv_rule():
    """The wide and per-expiry shapes write into the SAME store, so an iv whose meaning
    depended on which path fetched it would be worse than no iv at all."""
    import ba2_providers.options.thetadata as mod

    for bad in (0.0, 214748.3646, 21474.8365):
        assert mod._clean_iv(bad) is None, bad
    for good in (0.0625, 0.4147, 2.0):
        assert mod._clean_iv(good) == good, good
