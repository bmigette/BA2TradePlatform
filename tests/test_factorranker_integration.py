"""End-to-end FactorRanker: real run_analysis -> real FactorPortfolioManager ->
orders, across two rebalances. Only the data fetch and account/expert lookups are
mocked; the factor math, composite/rank, construction and rebalance are all real.
"""
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import (
    MarketAnalysisStatus, OrderDirection, OrderStatus, TransactionStatus,
)
from ba2_trade_platform.modules.experts.FactorRanker import FactorRanker
from tests.factories import (
    create_account_definition, create_expert_instance, create_market_analysis,
)


def _ramp(end, n=260):
    return pd.Series(np.linspace(100.0, end, n), index=pd.RangeIndex(n))


class _PipelineAccount:
    """Account double that fills orders and simulates the fill->transaction pipeline:
    opening fills flip the linked transaction to OPENED (so it shows as a holding
    on the next rebalance)."""

    def __init__(self, prices):
        self.prices = prices
        self.submitted = []

    def get_instrument_current_price(self, symbol):
        return self.prices.get(symbol)

    def submit_order(self, order, is_closing_order=False):
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.open_price = self.prices.get(order.symbol)
        add_instance(order, expunge_after_flush=True)
        self.submitted.append(order)
        if order.transaction_id and not is_closing_order:
            trans = get_instance(Transaction, order.transaction_id)
            if trans and trans.status != TransactionStatus.OPENED:
                trans.status = TransactionStatus.OPENED
                update_instance(trans)
        return order


def _settings():
    return {
        "factor_weight_momentum": 1.0,
        "factor_weight_value": 0.0,
        "factor_weight_quality": 0.0,
        "factor_weight_pead": 0.0,
        "top_n": 3,
        "weighting": "equal",
        "max_weight_per_name": 1.0,
        "gross_exposure": 1.0,
        "winsorize_pct": 0.0,
        "pead_drift_window_days": 60,
        "min_price": 0.0,
        "min_dollar_volume": 0.0,
        "sector_neutralize": False,
        "enabled_instruments": {s: {} for s in ["A", "B", "C", "D", "E"]},
        "instrument_selection_method": "expert",
    }


def _orders_by_symbol(orders):
    return {o.symbol: o.side for o in orders}


def test_two_rebalances_sell_dropped_buy_new():
    acct = create_account_definition()
    inst = create_expert_instance(account_id=acct.id, expert="FactorRanker", virtual_equity_pct=100.0)
    expert = FactorRanker(inst.id)
    expert._settings_cache = _settings()

    # Constant current prices for sizing (so a held name with an unchanged target
    # weight produces a zero delta -> no churn order).
    account = _PipelineAccount({s: 10.0 for s in ["A", "B", "C", "D", "E"]})
    expert_stub = MagicMock()
    expert_stub.get_virtual_balance.return_value = 100_000.0

    # Run 1 momentum: A > B > C > D > E  -> hold {A, B, C}
    run1 = {"A": _ramp(300), "B": _ramp(250), "C": _ramp(200), "D": _ramp(120), "E": _ramp(110)}
    # Run 2 momentum: D > E > C > A > B  -> hold {C, D, E} (drop A,B; keep C; add D,E)
    run2 = {"A": _ramp(105), "B": _ramp(103), "C": _ramp(200), "D": _ramp(320), "E": _ramp(300)}

    with patch("ba2_trade_platform.core.utils.get_account_instance_from_id", return_value=account), \
         patch("ba2_trade_platform.core.utils.get_expert_instance_from_id", return_value=expert_stub):

        ma1 = create_market_analysis(symbol="EXPERT", expert_instance_id=inst.id)
        with patch("ba2_trade_platform.modules.experts.FactorRanker.data.fetch_close_prices", return_value=run1):
            expert.run_analysis("EXPERT", ma1)
        run1_orders = _orders_by_symbol(account.submitted)
        account.submitted.clear()

        ma2 = create_market_analysis(symbol="EXPERT", expert_instance_id=inst.id)
        with patch("ba2_trade_platform.modules.experts.FactorRanker.data.fetch_close_prices", return_value=run2):
            expert.run_analysis("EXPERT", ma2)
        run2_orders = _orders_by_symbol(account.submitted)

    # Run 1: opened the top-3 by momentum.
    assert ma1.status == MarketAnalysisStatus.COMPLETED
    assert run1_orders == {"A": OrderDirection.BUY, "B": OrderDirection.BUY, "C": OrderDirection.BUY}

    # Run 2: sold the dropped names, bought the new entrants, left the kept name (C) alone.
    assert ma2.status == MarketAnalysisStatus.COMPLETED
    assert run2_orders == {
        "A": OrderDirection.SELL,   # dropped
        "B": OrderDirection.SELL,   # dropped
        "D": OrderDirection.BUY,    # new
        "E": OrderDirection.BUY,    # new
    }
    assert "C" not in run2_orders   # held with unchanged target weight -> no churn
