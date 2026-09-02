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
from datetime import date

import pandas as pd
import pytest

from ba2_common.core.interfaces import (
    CandleBatch, OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)
from ba2_providers import OPTIONS_PROVIDERS, get_provider
from ba2_providers.options.thetadata import ThetaDataOptionsProvider, _occ_symbol
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


def _wired(client: _FakeClient) -> ThetaDataOptionsProvider:
    p = ThetaDataOptionsProvider(api_key="test-key")
    p._client = client  # bypass the lazy `from thetadata import ThetaClient`
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


def test_open_interest_is_skipped_when_greeks_eod_has_no_rows():
    """An expiry genuinely absent from the window (e.g. listed after `end`) must not spend a
    second call finding out its open interest is empty too."""
    client = _FakeClient()  # default greeks/oi are both empty
    p = _wired(client)
    bars = list(p.fetch_eod_bars(_CONTRACTS, start=date(2024, 10, 1), end=date(2024, 10, 1)))
    assert bars == []
    assert [c[0] for c in client.calls] == ["greeks_eod"]


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
