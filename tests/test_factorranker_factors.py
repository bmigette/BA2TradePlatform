import pandas as pd
from ba2_trade_platform.modules.experts.FactorRanker.factors import (
    momentum_12_1,
    earnings_surprise,
    value_score,
    quality_score,
)


def test_momentum_12_1_basic():
    # 260 trading days; AAA doubled over the 12->1 month window, BBB flat
    idx = pd.RangeIndex(260)
    aaa = pd.Series([100.0] * 8 + list(range(100, 352)), index=idx)[:260]  # rising
    bbb = pd.Series([100.0] * 260, index=idx)
    out = momentum_12_1({"AAA": aaa, "BBB": bbb})
    assert out["BBB"] == 0.0
    assert out["AAA"] > 0.0  # positive 12-1 momentum
    assert set(out) == {"AAA", "BBB"}


def test_momentum_skips_recent_month():
    # A spike only in the last 21 days must NOT count (12-1 skips last month)
    idx = pd.RangeIndex(260)
    flat_then_spike = pd.Series([100.0] * 239 + [200.0] * 21, index=idx)
    out = momentum_12_1({"X": flat_then_spike})
    assert out["X"] == 0.0


def test_earnings_surprise_within_window():
    data = {
        "A": {"actual": 1.2, "estimate": 1.0, "estimate_std": 0.1, "days_since": 5},
        "B": {"actual": 0.9, "estimate": 1.0, "estimate_std": 0.1, "days_since": 5},
        "C": {"actual": 1.5, "estimate": 1.0, "estimate_std": 0.1, "days_since": 90},  # stale
    }
    out = earnings_surprise(data, drift_window_days=60)
    assert round(out["A"], 1) == 2.0   # (1.2-1.0)/0.1
    assert round(out["B"], 1) == -1.0
    assert out["C"] == 0.0             # outside drift window -> no signal


def test_value_score_cheaper_is_higher():
    data = {
        "CHEAP": {"eps_ttm": 5.0, "price": 50.0, "fcf_ttm": 10.0, "enterprise_value": 100.0},
        "RICH":  {"eps_ttm": 1.0, "price": 100.0, "fcf_ttm": 1.0, "enterprise_value": 500.0},
    }
    out = value_score(data)
    assert out["CHEAP"] > out["RICH"]


def test_quality_score_higher_for_profitable_low_accruals():
    data = {
        "GOOD": {"roe": 0.25, "gross_profit": 50.0, "total_assets": 100.0, "accruals_ratio": 0.02},
        "BAD":  {"roe": 0.02, "gross_profit": 5.0,  "total_assets": 100.0, "accruals_ratio": 0.20},
    }
    out = quality_score(data)
    assert out["GOOD"] > out["BAD"]
