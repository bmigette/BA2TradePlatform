"""Unit tests for the pure portfolio-allocation engine (no DB, no broker, no UI)."""
import json

import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, LabelTarget, PositionState, SymbolTarget,
)
from ba2_common.core.account_types import MarginInfo, OrderImpact
from ba2_common.core.types import OrderDirection


def _pos(symbol, price, quantity=0.0, cost_basis=0.0):
    return PositionState(symbol=symbol, quantity=quantity, cost_basis=cost_basis, price=price)


def test_allocation_row_side_buy_reports_is_buy_true():
    row = AllocationRow(symbol="AAA", side=OrderDirection.BUY)
    assert row.is_buy is True
    assert row.is_sell is False


def test_allocation_row_skipped_buy_reports_is_buy_false():
    row = AllocationRow(symbol="AAA", side=OrderDirection.BUY, skipped=True)
    assert row.is_buy is False


def test_plan_buy_rows_sorted_descending_by_estimated_value():
    small = AllocationRow(symbol="S", side=OrderDirection.BUY, estimated_value=10.0)
    big = AllocationRow(symbol="B", side=OrderDirection.BUY, estimated_value=99.0)
    plan = AllocationPlan(rows=[small, big])
    assert [r.symbol for r in plan.buy_rows] == ["B", "S"]


def test_plan_net_buy_value_is_buys_minus_sells_floored_at_zero():
    assert AllocationPlan(total_buy_value=100.0, total_sell_value=30.0).net_buy_value == 70.0
    assert AllocationPlan(total_buy_value=10.0, total_sell_value=30.0).net_buy_value == 0.0


def test_plan_to_dict_is_json_serialisable():
    plan = AllocationPlan(rows=[AllocationRow(symbol="AAA", side=OrderDirection.BUY)])
    blob = json.dumps(plan.to_dict())
    assert '"side": "BUY"' in blob


def test_compute_allocation_no_labels_returns_empty_plan():
    plan = pa.compute_allocation(0.0, 0.0, [], {}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows == []
    assert plan.total_buy_value == 0.0
    assert plan.scale_factor == 1.0
    assert plan.unallocatable_pct == 0.0


def test_position_fetch_failed_is_a_runtime_error_subclass():
    """Defined HERE, in the pure engine, so both the live service and the UI's
    view-model can raise/catch the same class without either importing the other."""
    assert issubclass(pa.PositionFetchFailed, RuntimeError)
