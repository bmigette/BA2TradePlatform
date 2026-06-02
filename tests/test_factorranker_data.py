from datetime import datetime, timezone
from unittest.mock import patch

from ba2_trade_platform.modules.experts.FactorRanker import data as fr_data
from ba2_trade_platform.modules.experts.FactorRanker.data import (
    enterprise_value,
    return_on_equity,
    accruals_ratio,
    days_since,
    estimate_std_from_range,
    build_value_inputs,
    build_quality_inputs,
)


def test_enterprise_value():
    assert enterprise_value(market_cap=100.0, total_debt=20.0, cash=5.0) == 115.0


def test_return_on_equity():
    assert return_on_equity(net_income=25.0, equity=100.0) == 0.25
    assert return_on_equity(net_income=25.0, equity=0.0) is None      # no div by zero
    assert return_on_equity(net_income=None, equity=100.0) is None


def test_accruals_ratio_sloan_proxy():
    # (net_income - operating_cash_flow) / total_assets
    assert round(accruals_ratio(net_income=10.0, operating_cash_flow=8.0, total_assets=100.0), 4) == 0.02
    assert accruals_ratio(net_income=10.0, operating_cash_flow=8.0, total_assets=0.0) is None


def test_days_since():
    assert days_since(datetime(2026, 1, 1), as_of=datetime(2026, 1, 6)) == 5
    assert days_since(None, as_of=datetime(2026, 1, 6)) is None


def test_estimate_std_from_range():
    # range/4 proxy for the cross-analyst dispersion
    assert round(estimate_std_from_range(high=1.4, low=0.6), 6) == 0.2
    assert estimate_std_from_range(high=None, low=0.6) is None
    assert estimate_std_from_range(high=1.0, low=1.0) is None  # zero range -> no usable std


def test_build_value_inputs():
    out = build_value_inputs(eps_ttm=5.0, price=50.0, fcf_ttm=10.0,
                             market_cap=80.0, total_debt=30.0, cash=10.0)
    assert out == {"eps_ttm": 5.0, "price": 50.0, "fcf_ttm": 10.0, "enterprise_value": 100.0}


def test_build_quality_inputs():
    out = build_quality_inputs(net_income=10.0, equity=100.0, gross_profit=50.0,
                               total_assets=100.0, operating_cash_flow=8.0)
    assert out["roe"] == 0.10
    assert out["gross_profit"] == 50.0
    assert out["total_assets"] == 100.0
    assert round(out["accruals_ratio"], 4) == 0.02


# --------------------------------------------------------------------------- #
# Symbol-dropping behaviour: the ranker DISCARDS symbols with data-gathering
# issues (error string / dict-with-error / unexpected shape / empty critical
# data) and KEEPS GOING with the rest — no failsafe defaults, no propagated
# exceptions. We patch the FMP provider classes used inside data.py.
# --------------------------------------------------------------------------- #

AS_OF = datetime(2026, 6, 2, tzinfo=timezone.utc)

OVERVIEW_MOD = "ba2_trade_platform.modules.dataproviders.fundamentals.overview.FMPCompanyOverviewProvider.FMPCompanyOverviewProvider"
DETAILS_MOD = "ba2_trade_platform.modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider.FMPCompanyDetailsProvider"


def _income(eps, net_income, gross_profit):
    return {"statements": [{
        "eps": eps, "net_income": net_income, "gross_profit": gross_profit,
    }]}


def _balance(equity, total_assets, cash=0.0, st_debt=0.0, lt_debt=0.0):
    return {"statements": [{
        "total_shareholder_equity": equity,
        "total_assets": total_assets,
        "cash_and_cash_equivalents": cash,
        "short_term_debt": st_debt,
        "long_term_debt": lt_debt,
    }]}


def _cashflow(fcf, ocf):
    return {"statements": [{"free_cash_flow": fcf, "operating_cash_flow": ocf}]}


class _FakeOverview:
    """get_fundamentals_overview: GOOD -> valid metrics; BAD -> error string."""
    def __init__(self, *a, **k):
        pass

    def get_fundamentals_overview(self, symbol, as_of_date=None, format_type="dict"):
        if symbol == "GOOD":
            return {"metrics": {"price": 100.0, "market_cap": 1_000.0}}
        return "Error fetching company overview: Limit Reach."


class _FakeDetails:
    """Statement/earnings methods: GOOD -> valid dicts; BAD -> error str / error dict."""
    def __init__(self, *a, **k):
        pass

    def get_income_statement(self, symbol, freq, end, **k):
        if symbol == "GOOD":
            return _income(eps=5.0, net_income=10.0, gross_profit=50.0)
        return "Error fetching income statement: Limit Reach."

    def get_balance_sheet(self, symbol, freq, end, **k):
        if symbol == "GOOD":
            return _balance(equity=100.0, total_assets=200.0, cash=10.0)
        return "Error fetching balance sheet: Limit Reach."

    def get_cashflow_statement(self, symbol, freq, end, **k):
        if symbol == "GOOD":
            return _cashflow(fcf=20.0, ocf=15.0)
        return "Error fetching cash flow statement: Limit Reach."

    def get_past_earnings(self, symbol, freq, end, **k):
        if symbol == "GOOD":
            return {"earnings": [{
                "reported_eps": 1.2, "estimated_eps": 1.0, "report_date": "2026-04-01",
            }]}
        return {"error": "Limit Reach.", "symbol": symbol}

    def get_earnings_estimates(self, symbol, freq, as_of, **k):
        if symbol == "GOOD":
            return {"estimates": [{
                "estimated_eps_high": 1.4, "estimated_eps_low": 0.6,
            }]}
        return {"error": "Limit Reach.", "symbol": symbol}


def test_fetch_value_inputs_drops_bad_symbol():
    with patch(f"{OVERVIEW_MOD}.__new__", lambda cls: _FakeOverview()), \
         patch(f"{DETAILS_MOD}.__new__", lambda cls: _FakeDetails()):
        out = fr_data.fetch_value_inputs(["GOOD", "BAD"], as_of=AS_OF)
    assert "GOOD" in out
    assert "BAD" not in out
    assert out["GOOD"]["price"] == 100.0


def test_fetch_quality_inputs_drops_bad_symbol():
    with patch(f"{DETAILS_MOD}.__new__", lambda cls: _FakeDetails()):
        out = fr_data.fetch_quality_inputs(["GOOD", "BAD"], as_of=AS_OF)
    assert "GOOD" in out
    assert "BAD" not in out
    assert out["GOOD"]["total_assets"] == 200.0


def test_fetch_pead_inputs_drops_bad_symbol():
    with patch(f"{DETAILS_MOD}.__new__", lambda cls: _FakeDetails()):
        out = fr_data.fetch_pead_inputs(["GOOD", "BAD"], as_of=AS_OF)
    assert "GOOD" in out
    assert "BAD" not in out
    assert out["GOOD"]["actual"] == 1.2


def test_fetch_value_inputs_drops_when_price_missing():
    """A symbol whose overview lacks a positive price is dropped (no failsafe default)."""
    class _NoPriceOverview(_FakeOverview):
        def get_fundamentals_overview(self, symbol, as_of_date=None, format_type="dict"):
            return {"metrics": {"price": None, "market_cap": 1_000.0}}

    with patch(f"{OVERVIEW_MOD}.__new__", lambda cls: _NoPriceOverview()), \
         patch(f"{DETAILS_MOD}.__new__", lambda cls: _FakeDetails()):
        out = fr_data.fetch_value_inputs(["GOOD"], as_of=AS_OF)
    assert out == {}


def test_fetch_quality_inputs_drops_when_no_quality_signal():
    """A symbol with no ROE and no gross_profit/total_assets pair is dropped."""
    class _ThinDetails(_FakeDetails):
        def get_income_statement(self, symbol, freq, end, **k):
            return _income(eps=5.0, net_income=None, gross_profit=None)

        def get_balance_sheet(self, symbol, freq, end, **k):
            return _balance(equity=0.0, total_assets=None)

    with patch(f"{DETAILS_MOD}.__new__", lambda cls: _ThinDetails()):
        out = fr_data.fetch_quality_inputs(["GOOD"], as_of=AS_OF)
    assert out == {}


def test_no_exception_propagates_when_all_bad():
    """Even if every symbol fails, the fetchers return {} rather than raising."""
    with patch(f"{OVERVIEW_MOD}.__new__", lambda cls: _FakeOverview()), \
         patch(f"{DETAILS_MOD}.__new__", lambda cls: _FakeDetails()):
        assert fr_data.fetch_value_inputs(["BAD"], as_of=AS_OF) == {}
        assert fr_data.fetch_quality_inputs(["BAD"], as_of=AS_OF) == {}
        assert fr_data.fetch_pead_inputs(["BAD"], as_of=AS_OF) == {}
