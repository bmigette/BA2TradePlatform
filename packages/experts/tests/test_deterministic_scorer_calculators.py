"""Unit tests for DeterministicScorer PURE calculators (no providers, no DB).

Run: pytest packages/experts/tests/test_deterministic_scorer_calculators.py
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ba2_experts.DeterministicScorer import technical as T
from ba2_experts.DeterministicScorer import fundamental as F
from ba2_experts.DeterministicScorer import analyst as A
from ba2_experts.DeterministicScorer import macro as M
from ba2_experts.DeterministicScorer import combine as C


# ----------------------------------------------------------------- technical
def _trend_df(n=300, drift=0.001, seed=7):
    """Synthetic OHLCV with a steady uptrend."""
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"Date": dates, "Close": close, "High": high, "Low": low})


def test_momentum_12_1_skips_last_month():
    closes = pd.Series(np.linspace(100, 200, 252))
    mom = T.momentum_12_1(closes, lookback=252, skip=21)
    # P[t-21] / P[t-252] - 1 with a linear series
    expected = float(closes.iloc[-22]) / float(closes.iloc[0]) - 1.0
    assert mom == pytest.approx(expected, rel=1e-9)


def test_momentum_none_when_history_short():
    assert T.momentum_12_1(pd.Series([1.0] * 100)) is None


def test_rsi_extremes():
    up = pd.Series(np.linspace(100, 200, 60))
    down = pd.Series(np.linspace(200, 100, 60))
    assert T.rsi_wilder(up) > 90
    assert T.rsi_wilder(down) < 10


def test_atr_positive_and_donchian_bounds():
    df = _trend_df()
    atr = T.atr_wilder(df["High"], df["Low"], df["Close"])
    assert atr is not None and atr > 0
    don = T.donchian_state(df["High"], df["Low"], df["Close"])
    assert -1.0 <= don <= 1.0


def test_technical_score_uptrend_positive():
    df = _trend_df(n=300, drift=0.0015)
    out = T.technical_score(df, {})
    assert -1.0 <= out["score"] <= 1.0
    assert out["score"] > 0.2  # strong uptrend should score clearly positive
    assert out["n_signals"] >= 3


def test_technical_score_downtrend_negative():
    df = _trend_df(n=300, drift=-0.0015, seed=11)
    out = T.technical_score(df, {})
    assert out["score"] < -0.2


# --------------------------------------------------------------- fundamental
def test_piotroski_perfect_company_scores_high():
    cur = {"netIncome": 100, "totalAssets": 1000, "operatingCashFlow": 150,
           "longTermDebt": 100, "currentAssets": 300, "currentLiabilities": 150,
           "commonStock": 100, "grossProfit": 400, "revenue": 1000}
    prior = {"netIncome": 50, "totalAssets": 900, "longTermDebt": 150,
             "currentAssets": 250, "currentLiabilities": 160,
             "commonStock": 110, "grossProfit": 300, "revenue": 800}
    fs = F.piotroski_f_score(cur, prior)
    assert fs is not None and fs >= 7


def test_piotroski_weak_company_scores_low():
    cur = {"netIncome": -100, "totalAssets": 1000, "operatingCashFlow": -50,
           "longTermDebt": 300, "currentAssets": 100, "currentLiabilities": 200,
           "commonStock": 150, "grossProfit": 100, "revenue": 500}
    prior = {"netIncome": 50, "totalAssets": 900, "longTermDebt": 150,
             "currentAssets": 250, "currentLiabilities": 100,
             "commonStock": 100, "grossProfit": 300, "revenue": 800}
    fs = F.piotroski_f_score(cur, prior)
    assert fs is not None and fs <= 2


def test_altman_z_original_and_distress_veto():
    healthy = {"totalAssets": 1000, "totalCurrentAssets": 400,
               "totalCurrentLiabilities": 200, "retainedEarnings": 300,
               "ebit": 150, "revenue": 1200, "totalLiabilities": 400}
    z = F.altman_z(healthy, market_cap=3000, variant="original")
    assert z is not None and z > 2.99  # safe zone

    distressed = {"totalAssets": 1000, "totalCurrentAssets": 100,
                  "totalCurrentLiabilities": 500, "retainedEarnings": -400,
                  "ebit": -100, "revenue": 300, "totalLiabilities": 950}
    z2 = F.altman_z(distressed, market_cap=50, variant="original")
    assert z2 is not None and z2 < 1.8  # distress zone -> veto


def test_fundamental_score_veto_flag():
    snap = {"fscore": 8, "z": 1.2, "quality_norm": 0.5, "value_norm": 0.2}
    out = F.fundamental_score(snap, {})
    assert out["veto"] is True and out["veto_reason"] == "altman_z_distress"


def test_fundamental_score_renormalizes_missing_components():
    out = F.fundamental_score({"fscore": 9}, {})
    assert out["score"] > 0.9  # only piotroski available, F=9 -> +1
    assert out["n_signals"] == 1


# ------------------------------------------------------------------- analyst
def test_revision_momentum_no_lookahead():
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"date": "2026-05-15T00:00:00+00:00", "strongBuy": 3, "buy": 5, "hold": 1,
         "sell": 0, "strongSell": 0},
        # second bullish row keeps coverage above the attenuation knee
        {"date": "2026-05-28T00:00:00+00:00", "strongBuy": 2, "buy": 4, "hold": 0,
         "sell": 0, "strongSell": 0},
        # AFTER as_of -> must be ignored
        {"date": "2026-07-01T00:00:00+00:00", "strongBuy": 0, "buy": 0, "hold": 0,
         "sell": 10, "strongSell": 10},
    ]
    out = A.revision_momentum(rows, as_of, window_days=90, halflife_days=30)
    assert out is not None
    assert out["score"] > 0.85  # only the bullish rows count
    # the post-as_of bearish rows must NOT leak in
    assert out["weighted_downgrades"] == 0.0


def test_revision_momentum_empty_returns_none():
    assert A.revision_momentum([], datetime(2026, 6, 1, tzinfo=timezone.utc)) is None


# --------------------------------------------------------------------- macro
def test_vix_score_levels():
    assert M.vix_score(12.0) == pytest.approx(1.0)
    assert M.vix_score(32.0) == pytest.approx(-1.0)
    assert M.vix_score(22.5) == pytest.approx(0.0, abs=1e-9)
    assert M.vix_score(None) is None


def test_pmi_score():
    assert M.pmi_score(55) == pytest.approx(1.0)
    assert M.pmi_score(45) == pytest.approx(-1.0)
    assert M.pmi_score(50) == pytest.approx(0.0)


def test_trend_score_sign():
    up = pd.Series(np.linspace(100, 200, 250))
    down = pd.Series(np.linspace(200, 100, 250))
    assert M.trend_score(up) == 1.0
    assert M.trend_score(down) == -1.0


def test_regime_composite_renormalizes():
    out = M.regime_composite({"trend_index": 1.0, "vix": None, "pmi": 0.5},
                             {"trend_index": 0.3, "vix": 0.15, "pmi": 0.1})
    # only trend + pmi available: (0.3*1 + 0.1*0.5)/0.4 = 0.875
    assert out["score"] == pytest.approx(0.875)
    assert out["n_inputs"] == 2


def test_exposure_multiplier_bounds_and_riskoff():
    assert M.exposure_multiplier(1.0) == pytest.approx(1.0)
    # m_floor reachable only when hard_riskoff sits below -1
    assert M.exposure_multiplier(-1.0, m_floor=0.25, hard_riskoff=-1.01) == pytest.approx(0.25)
    assert M.exposure_multiplier(-0.9, hard_riskoff=-0.75) == 0.0


# ------------------------------------------------------------------- combine
def test_normalize_weights():
    w = C.normalize_weights({"a": 2, "b": 2, "c": 0})
    assert w == pytest.approx({"a": 0.5, "b": 0.5})


def test_final_score_pipeline_with_veto():
    settings = {"macro_mode": "multiply", "m_floor": 0.25}
    ok = C.final_score(0.9, 0.6, None, 1.0, settings, veto=False)
    vetoed = C.final_score(0.9, 0.6, None, 1.0, settings, veto=True)
    assert ok["final"] > 0
    assert vetoed["final"] <= C.DEF_VETO_CAP + 1e-9


def test_final_score_macro_off_vs_multiply():
    off = C.final_score(0.8, 0.4, None, -0.9, {"macro_mode": "off"}, veto=False)
    mult = C.final_score(0.8, 0.4, None, -0.9, {"macro_mode": "multiply", "m_floor": 0.25}, veto=False)
    assert abs(off["final"]) >= abs(mult["final"])  # stress regime attenuates


def test_schmitt_trigger_hysteresis():
    s = {"theta_buy": 0.5, "theta_sell": 0.2}
    assert C.schmitt_trigger(0.6, s) == "BUY"
    assert C.schmitt_trigger(-0.3, s) == "SELL"
    assert C.schmitt_trigger(0.1, s) == "HOLD"
    assert C.schmitt_trigger(0.0, s, prev_signal="BUY") == "HOLD"  # band = no churn


def test_atr_target_and_stop():
    s = {"k_target": 4.5, "k_stop": 2.5}
    tp = C.atr_target_price(100.0, 2.0, "BUY", s)
    sl = C.atr_stop_price(100.0, 2.0, "BUY", s)
    assert tp == pytest.approx(109.0)
    assert sl == pytest.approx(95.0)
    assert C.atr_target_price(100.0, None, "BUY", s) is None


def test_atr_target_from_score_stretches():
    s = {"k_target": 4.0, "target_from_score": True}
    lo = C.atr_target_price(100.0, 2.0, "BUY", s, final=0.0)
    hi = C.atr_target_price(100.0, 2.0, "BUY", s, final=1.0)
    assert hi > lo  # higher conviction -> wider target


def test_confidence_bounds():
    assert 5.0 <= C.confidence_from_score(0.01) <= 100.0
    assert C.confidence_from_score(1.0) == pytest.approx(100.0)
