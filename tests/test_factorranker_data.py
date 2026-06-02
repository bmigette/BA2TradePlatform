from datetime import datetime

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
