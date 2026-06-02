from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.modules.experts.FactorRanker import FactorRanker
from tests.factories import (
    create_account_definition, create_expert_instance, create_market_analysis,
)


def _ramp(start, end, n=260):
    return pd.Series(np.linspace(start, end, n), index=pd.RangeIndex(n))


def _make_expert(inst_id, **overrides):
    expert = FactorRanker(inst_id)
    settings = {
        "factor_weight_momentum": 1.0,
        "factor_weight_value": 0.0,
        "factor_weight_quality": 0.0,
        "factor_weight_pead": 0.0,
        "top_n": 2,
        "weighting": "equal",
        "max_weight_per_name": 1.0,
        "gross_exposure": 1.0,
        "winsorize_pct": 0.0,
        "pead_drift_window_days": 60,
        "min_price": 0.0,
        "min_dollar_volume": 0.0,
        "sector_neutralize": False,
        "enabled_instruments": {"A": {}, "B": {}, "C": {}},
        "instrument_selection_method": "expert",
    }
    settings.update(overrides)
    expert._settings_cache = settings
    return expert


def test_run_analysis_ranks_and_rebalances():
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="FactorRanker")
    ma = create_market_analysis(symbol="EXPERT", expert_instance_id=inst.id)
    expert = _make_expert(inst.id)

    # Momentum ordering A > B > C (steeper ramp = higher 12-1 return).
    prices = {"A": _ramp(100, 300), "B": _ramp(100, 150), "C": _ramp(100, 100)}

    pm_instance = MagicMock()
    pm_instance.get_holdings.return_value = ({}, {})  # empty portfolio (first run)
    with patch("ba2_trade_platform.modules.experts.FactorRanker.data.fetch_close_prices", return_value=prices) as fetch_px, \
         patch("ba2_trade_platform.modules.experts.FactorRanker.FactorPortfolioManager", return_value=pm_instance) as PM:
        expert.run_analysis("EXPERT", ma)

    # Disabled factors must not be fetched.
    fetch_px.assert_called_once()

    # Rebalanced to the equal-weight top-2 by momentum.
    PM.assert_called_once_with(inst.id)
    (targets,), _ = pm_instance.rebalance.call_args
    assert set(targets) == {"A", "B"}
    assert round(targets["A"], 6) == round(targets["B"], 6) == 0.5

    # Status + ranked book persisted to state.
    assert ma.status == MarketAnalysisStatus.COMPLETED
    book = ma.state["factor_ranker"]
    assert [row["symbol"] for row in book["ranking"]] == ["A", "B", "C"]
    assert book["targets"] == targets

    # First run, empty portfolio -> top-N are BUY, the rest "—".
    actions = {row["symbol"]: row["action"] for row in book["ranking"]}
    assert actions == {"A": "BUY", "B": "BUY", "C": "—"}


def test_run_analysis_action_reflects_holdings():
    """action shows BUY (new), HOLD (kept), SELL (dropped) vs current holdings."""
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="FactorRanker")
    ma = create_market_analysis(symbol="EXPERT", expert_instance_id=inst.id)
    expert = _make_expert(inst.id)
    prices = {"A": _ramp(100, 300), "B": _ramp(100, 150), "C": _ramp(100, 100)}

    pm_instance = MagicMock()
    # Currently hold A (stays in top-2) and C (ranked last, drops out of top-2).
    pm_instance.get_holdings.return_value = ({"A": 10.0, "C": 20.0}, {})
    with patch("ba2_trade_platform.modules.experts.FactorRanker.data.fetch_close_prices", return_value=prices), \
         patch("ba2_trade_platform.modules.experts.FactorRanker.FactorPortfolioManager", return_value=pm_instance):
        expert.run_analysis("EXPERT", ma)

    actions = {row["symbol"]: row["action"] for row in ma.state["factor_ranker"]["ranking"]}
    assert actions["A"] == "HOLD"   # in target and already held
    assert actions["B"] == "BUY"    # in target, not held
    assert actions["C"] == "SELL"   # held but dropped from the top-N


def test_run_analysis_skips_when_universe_empty():
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="FactorRanker")
    ma = create_market_analysis(symbol="EXPERT", expert_instance_id=inst.id)
    expert = _make_expert(inst.id, enabled_instruments={})

    with patch("ba2_trade_platform.modules.experts.FactorRanker.FactorPortfolioManager") as PM:
        expert.run_analysis("EXPERT", ma)

    PM.assert_not_called()
    assert ma.status == MarketAnalysisStatus.SKIPPED
