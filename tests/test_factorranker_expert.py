from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

from ba2_trade_platform.core.types import MarketAnalysisStatus
from ba2_trade_platform.modules.experts.FactorRanker import FactorRanker
from tests.factories import (
    create_account_definition, create_expert_instance, create_market_analysis,
)

# _gather() eagerly builds a real OHLCV provider (providers.ohlcv()) at the top, before it
# knows whether any enabled factor will actually need it — see FactorRanker/__init__.py. The
# instance itself is never touched here (fetch_close_prices is mocked below, so nothing calls
# a method on it), so a bare stub avoids depending on a real FMP_API_KEY app setting.
_OHLCV_MOD = "ba2_providers.ohlcv.FMPOHLCVProvider.FMPOHLCVProvider"


class _StubOHLCV:
    def __init__(self, *a, **k):
        pass


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
         patch("ba2_trade_platform.modules.experts.FactorRanker.FactorPortfolioManager", return_value=pm_instance) as PM, \
         patch(f"{_OHLCV_MOD}.__new__", lambda cls: _StubOHLCV()):
        expert.run_analysis("EXPERT", ma)

    # Disabled factors must not be fetched — momentum (the only enabled factor here) is the
    # sole fetch_close_prices caller. The whole-universe "rebalance price snapshot" fetch that
    # used to make this 2 calls was dropped (it was never consumed downstream — the portfolio
    # reads live prices off the account instead; see FactorRanker/__init__.py's _gather).
    assert fetch_px.call_count == 1
    for call in fetch_px.call_args_list:
        assert call.args[0] == ["A", "B", "C"]

    # Rebalanced to the equal-weight top-2 by momentum. Phase 6: the package
    # FactorRanker constructs FactorPortfolioManager(self.id) twice — once in
    # _gather_holdings() to read current holdings, once in run_analysis to rebalance
    # — both keyed by the expert-instance id. (Pre-extraction in-tree code built it
    # once; this is an internal detail of the golden-verified _gather/_process split.)
    assert PM.call_count == 2
    for call in PM.call_args_list:
        assert call.args == (inst.id,)
    pm_instance.rebalance.assert_called_once()
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
         patch("ba2_trade_platform.modules.experts.FactorRanker.FactorPortfolioManager", return_value=pm_instance), \
         patch(f"{_OHLCV_MOD}.__new__", lambda cls: _StubOHLCV()):
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

    # Phase 6: the package _gather computes the momentum factor (which builds an
    # OHLCV provider) before the skip decision, so patch fetch_close_prices to keep
    # the empty-universe path off the network. The subject under test is the SKIP
    # decision (empty universe -> no portfolio manager, status SKIPPED).
    with patch("ba2_trade_platform.modules.experts.FactorRanker.data.fetch_close_prices", return_value={}), \
         patch("ba2_trade_platform.modules.experts.FactorRanker.FactorPortfolioManager") as PM, \
         patch(f"{_OHLCV_MOD}.__new__", lambda cls: _StubOHLCV()):
        expert.run_analysis("EXPERT", ma)

    PM.assert_not_called()
    assert ma.status == MarketAnalysisStatus.SKIPPED
