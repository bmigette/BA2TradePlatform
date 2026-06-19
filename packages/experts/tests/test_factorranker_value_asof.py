"""No lookahead in the VALUE factor: FactorRanker must build value inputs (price +
market cap) from AS_OF-correct sources, never the current company-profile snapshot.

The bug: ``fetch_value_inputs`` used ``FMPCompanyOverviewProvider.get_fundamentals_overview``
(→ ``fmpsdk.company_profile``), which returns the CURRENT ``price`` and ``mktCap``
regardless of ``as_of``. Feeding those into earnings-yield (E/P) and FCF/EV at a PAST
``as_of`` contaminates the value rank with today's prices = lookahead.

The fix: price comes from the OHLCV provider's as_of close (the same close the rest of
the backtest uses for momentum/pricing), and market cap is reconstructed as
``as_of_close × shares_outstanding`` where shares come from the DATED income statement
(``weighted_average_shares_outstanding``, point-in-time filtered <= as_of). If shares
are unavailable for a symbol, the symbol is dropped (no guessing, no alternate lookahead).
"""
import sys
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FactorRanker import data

AS_OF = datetime(2020, 6, 15, tzinfo=timezone.utc)

# The current-snapshot profile price/market cap (what the buggy code used). These
# must NOT appear in the computed value inputs at a PAST as_of.
CURRENT_PRICE = 500.0
CURRENT_MKTCAP = 5_000_000_000.0

# The as_of-correct close (what the fix MUST use) and the dated shares outstanding.
ASOF_CLOSE = 100.0
SHARES_OUT = 10_000_000.0  # market cap at as_of = 100 * 10M = 1_000_000_000


def _details_module():
    import ba2_providers.fundamentals.details.FMPCompanyDetailsProvider  # noqa: F401
    return sys.modules["ba2_providers.fundamentals.details.FMPCompanyDetailsProvider"]


def _overview_module():
    import ba2_providers.fundamentals.overview.FMPCompanyOverviewProvider  # noqa: F401
    return sys.modules["ba2_providers.fundamentals.overview.FMPCompanyOverviewProvider"]


def _ohlcv_module():
    import ba2_providers.ohlcv.FMPOHLCVProvider  # noqa: F401
    return sys.modules["ba2_providers.ohlcv.FMPOHLCVProvider"]


class FakeOHLCV:
    """Returns an ascending-by-date close series whose LAST close is the as_of close."""

    def __init__(self, last_close=ASOF_CLOSE):
        self._last_close = last_close
        self.calls = []

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=400, interval="1d"):
        self.calls.append({"symbol": symbol, "end_date": end_date})
        # ascending: the most recent (as_of) bar is the last row
        return pd.DataFrame({"Close": [80.0, 90.0, self._last_close]})


class FakeOverview:
    """Profile snapshot carrying the CURRENT (post-as_of) price/market cap = the lookahead trap."""

    def get_fundamentals_overview(self, symbol, as_of_date=None, format_type="dict"):
        return {
            "symbol": symbol,
            "metrics": {"price": CURRENT_PRICE, "market_cap": CURRENT_MKTCAP},
        }


class FakeDetails:
    """Dated statements: income carries shares outstanding; balance carries debt/cash."""

    def __init__(self, shares=SHARES_OUT):
        self._shares = shares

    def get_income_statement(self, symbol, frequency, end_date, lookback_periods=None, format_type="dict"):
        return {
            "statements": [{
                "eps": 5.0,
                "weighted_average_shares_outstanding": self._shares,
            }]
        }

    def get_balance_sheet(self, symbol, frequency, end_date, lookback_periods=None, format_type="dict"):
        return {
            "statements": [{
                "short_term_debt": 0.0,
                "long_term_debt": 0.0,
                "cash_and_cash_equivalents": 0.0,
            }]
        }

    def get_cashflow_statement(self, symbol, frequency, end_date, lookback_periods=None, format_type="dict"):
        return {"statements": [{"free_cash_flow": 50_000_000.0}]}


def _patch_providers(monkeypatch, ohlcv=None, details=None):
    ohlcv = ohlcv or FakeOHLCV()
    details = details or FakeDetails()
    monkeypatch.setattr(_ohlcv_module(), "FMPOHLCVProvider", lambda: ohlcv)
    monkeypatch.setattr(_overview_module(), "FMPCompanyOverviewProvider", lambda: FakeOverview())
    monkeypatch.setattr(_details_module(), "FMPCompanyDetailsProvider", lambda: details)
    return ohlcv, details


def test_value_inputs_use_as_of_close_not_current_price(monkeypatch):
    """E/P must be computed against the AS_OF close, not the current-snapshot price."""
    _patch_providers(monkeypatch)
    out = data.fetch_value_inputs(["AAA"], as_of=AS_OF)
    assert "AAA" in out
    assert out["AAA"]["price"] == ASOF_CLOSE, (
        f"value price must be the as_of close ({ASOF_CLOSE}), not the current "
        f"profile price ({CURRENT_PRICE}) — that would be lookahead"
    )
    assert out["AAA"]["price"] != CURRENT_PRICE


def test_value_inputs_market_cap_reconstructed_from_as_of_close_and_shares(monkeypatch):
    """EV (hence FCF/EV) must use market cap = as_of_close × dated shares, not the
    current-snapshot mktCap."""
    _patch_providers(monkeypatch)
    out = data.fetch_value_inputs(["AAA"], as_of=AS_OF)
    ev = out["AAA"]["enterprise_value"]
    expected_mktcap = ASOF_CLOSE * SHARES_OUT  # 1_000_000_000, debt=cash=0 => EV == mktcap
    assert ev == expected_mktcap, (
        f"EV must be reconstructed from as_of market cap ({expected_mktcap}), "
        f"not the current-snapshot mktCap ({CURRENT_MKTCAP})"
    )
    assert ev != CURRENT_MKTCAP


def test_value_inputs_ohlcv_fetched_with_end_date_equal_as_of(monkeypatch):
    """The price source must be anchored at as_of (end_date == as_of)."""
    ohlcv, _ = _patch_providers(monkeypatch)
    data.fetch_value_inputs(["AAA"], as_of=AS_OF)
    assert ohlcv.calls, "OHLCV provider must be used for the as_of price"
    assert all(c["end_date"] == AS_OF for c in ohlcv.calls), ohlcv.calls


def test_value_inputs_drop_symbol_when_shares_unavailable(monkeypatch):
    """No shares outstanding in the dated statements => no as_of market cap can be
    reconstructed => drop the symbol (do NOT fall back to the current snapshot)."""
    _patch_providers(monkeypatch, details=FakeDetails(shares=None))
    out = data.fetch_value_inputs(["AAA"], as_of=AS_OF)
    assert "AAA" not in out, "symbol with no as_of shares must be dropped, not look-ahead-filled"


def test_value_inputs_drop_symbol_when_no_asof_close(monkeypatch):
    """No as_of close (e.g. empty OHLCV) => no as_of price => drop the symbol."""
    class EmptyOHLCV(FakeOHLCV):
        def get_ohlcv_data(self, symbol, end_date=None, lookback_days=400, interval="1d"):
            return pd.DataFrame({"Close": []})

    _patch_providers(monkeypatch, ohlcv=EmptyOHLCV())
    out = data.fetch_value_inputs(["AAA"], as_of=AS_OF)
    assert "AAA" not in out
