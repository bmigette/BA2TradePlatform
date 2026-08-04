"""Compute-provider tests with canned stub providers (documented dict contracts)."""
from datetime import datetime
import pandas as pd
import pytest


def _ohlcv_df(closes, start="2024-01-01"):
    """Stub frame using the REAL OHLCV contract: capitalized columns
    (Date, Open, High, Low, Close, Volume) — see MarketDataProviderInterface."""
    idx = pd.date_range(start, periods=len(closes), freq="B")
    return pd.DataFrame({
        "Date": idx,
        "Open": closes,
        "High": closes,
        "Low": closes,
        "Close": closes,
        "Volume": [1000] * len(closes),
    })


class _StubOHLCV:
    def __init__(self, closes_by_symbol):
        self._closes = closes_by_symbol
        self.calls = []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d"):
        self.calls.append((symbol, start_date, end_date, interval))
        return _ohlcv_df(self._closes[symbol])


class _StubOverview:
    def get_fundamentals_overview(self, symbol, as_of_date, format_type="markdown"):
        return {"symbol": symbol, "company_name": "Stub Co",
                "as_of_date": "2026-01-01", "data_date": "2025-12-31",
                "metrics": {"beta": 1.2}}


class _StubDetails:
    def get_cashflow_statement(self, symbol, frequency, end_date,
                               lookback_periods=None, format_type="markdown"):
        return {"statement_count": 3, "statements": [
            {"fiscal_date_ending": "2025-12-31", "free_cash_flow": 121.0},
            {"fiscal_date_ending": "2024-12-31", "free_cash_flow": 110.0},
            {"fiscal_date_ending": "2023-12-31", "free_cash_flow": 100.0},
        ]}

    def get_income_statement(self, symbol, frequency, end_date,
                             lookback_periods=None, format_type="markdown"):
        return {"statement_count": 1, "statements": [
            {"fiscal_date_ending": "2025-12-31",
             "weighted_average_shares_outstanding": 100.0,
             "weighted_average_shares_diluted": 100.0}]}

    def get_balance_sheet(self, symbol, frequency, end_date,
                          lookback_periods=None, format_type="markdown"):
        return {"statement_count": 1, "statements": [
            {"fiscal_date_ending": "2025-12-31",
             "cash_and_cash_equivalents": 50.0,
             "short_term_debt": 50.0,
             "long_term_debt": 100.0}]}


AS_OF = datetime(2026, 1, 15)


def test_risk_stats_markdown_and_dict(tmp_path):
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    ohlcv = _StubOHLCV({
        "AAA": [100 + i * 0.1 for i in range(60)],
        "SPY": [400 + i * 0.05 for i in range(60)],
    })
    p = FinanceCalcRiskStatsProvider(ohlcv, benchmark_symbol="SPY")
    md = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="markdown")
    assert "Risk statistics" in md and "Realized vol" in md and "Beta" in md and "VaR" in md
    both = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="both")
    assert set(both) == {"text", "data"}
    assert both["data"]["symbol"] == "AAA"
    assert both["data"]["descriptive"]["n"] > 0
    d = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="dict")
    assert d["benchmark"] == "SPY"


def test_risk_stats_point_in_time():
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    ohlcv = _StubOHLCV({"AAA": [100.0] * 10, "SPY": [400.0] * 10})
    p = FinanceCalcRiskStatsProvider(ohlcv)
    p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="dict")
    # The OHLCV fetch must be bounded at the analysis date — nothing after it.
    for (_sym, _start, end, _iv) in ohlcv.calls:
        assert pd.Timestamp(end) <= pd.Timestamp(AS_OF)


def test_risk_stats_benchmark_misalignment_does_not_raise():
    """IPO/halt-day case: asset history shorter than the benchmark's. The
    benchmark block must be aligned (or skipped), never a ValueError from
    zip(strict=True) inside performance()."""
    from ba2_providers.riskstats import FinanceCalcRiskStatsProvider
    ohlcv = _StubOHLCV({
        "AAA": [100 + i * 0.1 for i in range(30)],
        "SPY": [400 + i * 0.05 for i in range(60)],
    })
    p = FinanceCalcRiskStatsProvider(ohlcv, benchmark_symbol="SPY")
    d = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="dict")
    assert d["computable"] is True
    assert d["performance"]["n_obs"] == 29
    assert "beta" in d["performance"]            # benchmark block present, aligned
    md = p.get_risk_stats("AAA", AS_OF, lookback_days=90, format_type="markdown")
    assert "Risk statistics" in md


def test_valuation_snapshot_contains_assumptions_and_value():
    from ba2_providers.valuation import FinanceCalcValuationProvider
    p = FinanceCalcValuationProvider(_StubOverview(), _StubDetails(),
                                     risk_free_rate=0.04, equity_risk_premium=0.05)
    md = p.get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    # Every default assumption is printed verbatim — nothing hidden.
    for marker in ("risk-free", "4.0%", "5.0%", "terminal growth", "2.5%", "beta"):
        assert marker in md.lower()
    assert "Intrinsic value" in md and "DEFAULT assumptions" in md
    d = p.get_valuation_snapshot("AAA", AS_OF, format_type="dict")
    assert d["assumptions"]["risk_free_rate"] == 0.04
    assert d["wacc"] == pytest.approx(0.10)          # 0.04 + 1.2*0.05
    assert d["fcf_cagr"] == pytest.approx(0.10, rel=1e-3)  # 100->110->121
    assert d["dcf"]["intrinsic_per_share"] is not None


def test_valuation_snapshot_not_computable_when_no_fcf():
    from ba2_providers.valuation import FinanceCalcValuationProvider

    class _NoFCF(_StubDetails):
        def get_cashflow_statement(self, *a, **k):
            return {"statement_count": 0, "statements": []}

    p = FinanceCalcValuationProvider(_StubOverview(), _NoFCF())
    md = p.get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    assert "not computable" in md.lower()
    d = p.get_valuation_snapshot("AAA", AS_OF, format_type="dict")
    assert d["computable"] is False
    # Never an exception, never a fabricated number.


def test_valuation_snapshot_not_computable_when_negative_fcf():
    """Negative-only FCF: a Gordon-growth DCF on a cash-burning base is
    meaningless — the snapshot must say 'not computable', never a number."""
    from ba2_providers.valuation import FinanceCalcValuationProvider

    class _NegFCF(_StubDetails):
        def get_cashflow_statement(self, *a, **k):
            return {"statement_count": 3, "statements": [
                {"fiscal_date_ending": "2025-12-31", "free_cash_flow": -80.0},
                {"fiscal_date_ending": "2024-12-31", "free_cash_flow": -100.0},
                {"fiscal_date_ending": "2023-12-31", "free_cash_flow": -120.0},
            ]}

    p = FinanceCalcValuationProvider(_StubOverview(), _NegFCF())
    md = p.get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    assert "not computable" in md.lower()
    assert "negative free cash flow" in md
    d = p.get_valuation_snapshot("AAA", AS_OF, format_type="dict")
    assert d["computable"] is False


def test_valuation_snapshot_flags_net_debt_assumed_zero():
    """When the balance-sheet section is missing, net debt silently defaults
    to 0 — the report must say so explicitly."""
    from ba2_providers.valuation import FinanceCalcValuationProvider

    class _NoBS(_StubDetails):
        def get_balance_sheet(self, *a, **k):
            return {"statement_count": 0, "statements": []}

    p = FinanceCalcValuationProvider(_StubOverview(), _NoBS())
    md = p.get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    assert "net debt assumed 0" in md
    d = p.get_valuation_snapshot("AAA", AS_OF, format_type="dict")
    assert d["computable"] is True
    assert d["assumptions"]["net_debt"] == 0.0
    # With a real balance sheet the disclaimer is absent.
    md2 = FinanceCalcValuationProvider(_StubOverview(), _StubDetails()) \
        .get_valuation_snapshot("AAA", AS_OF, format_type="markdown")
    assert "net debt assumed 0" not in md2
