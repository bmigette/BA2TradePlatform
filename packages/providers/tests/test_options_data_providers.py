"""OptionsDataProviderInterface + the Alpaca / ThetaData implementations.

The ThetaData transport is exercised against a MOCKED HTTP response built from the
documented v3 CSV column order, because ThetaData v3 serves from a locally-running Theta
Terminal (no cloud key), so there is nothing to hit in CI. What IS covered here is
everything that would otherwise be silently wrong: OCC reconstruction from the separate
symbol/expiration/strike/right columns, the 0-means-no-quote convention, window filtering
and the bulk-response memo.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.interfaces import (
    OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)
from ba2_providers import OPTIONS_PROVIDERS, get_provider
from ba2_providers.options.thetadata import ThetaDataOptionsProvider, _occ_symbol


# --------------------------------------------------------------------------- #
# registry / contract
# --------------------------------------------------------------------------- #
def test_every_provider_registered_under_the_options_category():
    """One seam, three vendors: "alpaca" (the incumbent, floored at 2024-01-18), "thetadata"
    (deeper, needs a paid local terminal) and "tastytrade" (dxfeed — the only one whose bars
    carry imp_volatility and open_interest). Adding a fourth belongs here, not in a parallel
    mechanism."""
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
# ThetaData EOD parsing (mocked transport)
# --------------------------------------------------------------------------- #
# Documented v3 /v3/option/history/eod column order.
_HEADER = ("symbol,expiration,strike,right,created,last_trade,open,high,low,close,volume,"
           "count,bid_size,bid_exchange,bid,bid_condition,ask_size,ask_exchange,ask,ask_condition")
_ROWS = [
    # AAPL 200C, two days, with a real two-sided quote
    "AAPL,2025-01-17,200.0,C,2024-10-01,x,10.0,11.0,9.5,10.5,1234,10,5,1,10.4,0,5,1,10.6,0",
    "AAPL,2025-01-17,200.0,C,2024-10-02,x,10.5,12.0,10.2,11.8,2000,12,5,1,11.7,0,5,1,11.9,0",
    # AAPL 210C — bid/ask reported as 0 (ThetaData's "no quote"), must NOT become $0.00
    "AAPL,2025-01-17,210.0,C,2024-10-01,x,1.0,1.2,0.9,1.1,50,3,0,1,0,0,0,1,0,0",
    # outside the requested bar window -> filtered out
    "AAPL,2025-01-17,200.0,C,2024-12-31,x,20.0,21.0,19.0,20.5,10,1,5,1,20.4,0,5,1,20.6,0",
]


def _provider_with(rows, calls=None):
    p = ThetaDataOptionsProvider(base_url="http://127.0.0.1:25503")

    def fake_get_csv(path, params):
        if calls is not None:
            calls.append((path, params))
        import csv, io
        return list(csv.DictReader(io.StringIO("\n".join([_HEADER, *rows]))))

    p._get_csv = fake_get_csv  # type: ignore[assignment]
    return p


def test_discover_contracts_derives_the_chain_from_real_eod_rows():
    p = _provider_with(_ROWS)
    got = p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1), expiry_lte=date(2025, 2, 1))
    assert {c.occ_symbol for c in got} == {"AAPL250117C00200000", "AAPL250117C00210000"}
    c = next(c for c in got if c.strike == 200.0)
    assert (c.underlying, c.option_type, c.expiry) == ("AAPL", "call", date(2025, 1, 17))


def test_discover_contracts_applies_strike_and_expiry_filters():
    p = _provider_with(_ROWS)
    got = p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1), expiry_lte=date(2025, 2, 1),
                               strike_min=205.0)
    assert [c.strike for c in got] == [210.0]
    # An expiry window excluding the only expiry yields nothing (not an error).
    assert p.discover_contracts("AAPL", expiry_gte=date(2026, 1, 1),
                                expiry_lte=date(2026, 2, 1)) == []


def test_fetch_eod_bars_parses_prices_and_honours_the_window():
    p = _provider_with(_ROWS)
    contracts = p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1),
                                     expiry_lte=date(2025, 2, 1))
    bars = list(p.fetch_eod_bars(contracts, start=date(2024, 10, 1), end=date(2024, 10, 2)))

    assert len(bars) == 3, "the 2024-12-31 row is outside the window and must be dropped"
    b = next(b for b in bars if b.occ_symbol == "AAPL250117C00200000"
             and b.bar_date == date(2024, 10, 1))
    assert (b.open, b.high, b.low, b.close) == (10.0, 11.0, 9.5, 10.5)
    assert b.volume == 1234
    assert (b.bid, b.ask) == (10.4, 10.6), "real two-sided quote must survive"


def test_zero_bid_ask_becomes_none_not_a_free_option():
    """ThetaData reports 0 for 'no quote'. Passing that through as a real price would look
    like a $0.00 contract to the arb guard — exactly the class of artifact that produced
    fabricated option profits before."""
    p = _provider_with(_ROWS)
    contracts = p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1),
                                     expiry_lte=date(2025, 2, 1))
    bars = list(p.fetch_eod_bars(contracts, start=date(2024, 10, 1), end=date(2024, 10, 2)))
    b = next(b for b in bars if b.occ_symbol == "AAPL250117C00210000")
    assert b.bid is None and b.ask is None
    assert b.close == 1.1, "a missing QUOTE must not discard the traded price"


def test_bulk_response_is_memoized_across_discover_then_fetch():
    """discover + fetch run over the same window in a build; the bulk EOD response is large,
    so it must be paid for once."""
    calls = []
    p = _provider_with(_ROWS, calls=calls)
    contracts = p.discover_contracts("AAPL", expiry_gte=date(2025, 1, 1),
                                     expiry_lte=date(2025, 2, 1))
    list(p.fetch_eod_bars(contracts, start=date(2025, 1, 1), end=date(2025, 2, 1)))
    assert len(calls) == 1, f"expected one bulk request, got {len(calls)}"
    path, params = calls[0]
    assert path == "/v3/option/history/eod"
    # Bulk form: whole chain in one request.
    assert params["expiration"] == "*" and params["strike"] == "*" and params["right"] == "both"
    assert params["symbol"] == "AAPL"


def test_unreachable_terminal_raises_an_actionable_error():
    """No credentials go in the request, so a connection refusal is easy to misdiagnose as
    an auth problem — the message must name the terminal."""
    import requests
    p = ThetaDataOptionsProvider(base_url="http://127.0.0.1:25503")

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    orig = requests.get
    requests.get = boom
    try:
        with pytest.raises(RuntimeError, match="Theta Terminal"):
            p._get_csv("/v3/option/history/eod", {})
    finally:
        requests.get = orig


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
