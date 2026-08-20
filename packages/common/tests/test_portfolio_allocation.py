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


def test_even_split_two_symbols_targets_half_the_base_each():
    labels = [LabelTarget("ARK26", 100.0, [SymbolTarget("AAA", 50.0), SymbolTarget("BBB", 50.0)])]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 50.0)}
    plan = pa.compute_allocation(100_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].target_notional == 50_000.0
    assert by["AAA"].target_quantity == 500.0
    assert by["BBB"].target_quantity == 1000.0
    assert plan.total_buy_value == 100_000.0
    assert plan.scale_factor == 1.0
    assert plan.bp_usage_pct == pytest.approx(10.0)


def test_uneven_label_and_symbol_weights_multiply_through():
    labels = [
        LabelTarget("ARK26", 40.0, [SymbolTarget("AAA", 70.0), SymbolTarget("BBB", 30.0)]),
        LabelTarget("NDX", 60.0, [SymbolTarget("CCC", 100.0)]),
    ]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 20.0), "CCC": _pos("CCC", 250.0)}
    plan = pa.compute_allocation(100_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].target_quantity == 280.0
    assert by["BBB"].target_quantity == 600.0
    assert by["CCC"].target_quantity == 240.0


def test_symbol_in_two_labels_sums_targets_into_one_row():
    labels = [
        LabelTarget("ARK26", 50.0, [SymbolTarget("XXX", 100.0)]),
        LabelTarget("HighRisk", 50.0, [SymbolTarget("XXX", 100.0)]),
    ]
    plan = pa.compute_allocation(100_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert len(plan.rows) == 1
    row = plan.rows[0]
    assert row.target_notional == 100_000.0
    assert row.target_quantity == 1000.0
    assert row.labels == ["ARK26", "HighRisk"]
    assert pa.REASON_MULTI_LABEL_FMT.format(labels="ARK26, HighRisk") in row.reasons


def test_labels_totalling_ninety_percent_leave_ten_percent_undeployed():
    labels = [
        LabelTarget("A", 40.0, [SymbolTarget("AAA", 100.0)]),
        LabelTarget("B", 50.0, [SymbolTarget("BBB", 100.0)]),
    ]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 100.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.total_buy_value == 9_000.0


def test_unknown_margin_uses_default_bp_factor():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=2.0)
    assert plan.rows[0].bp_factor == 2.0
    assert plan.rows[0].bp_cost == 20_000.0


def test_held_symbol_with_no_managed_label_is_absent_from_the_plan():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    current = {"AAA": _pos("AAA", 100.0),
               "ZZZ": _pos("ZZZ", 100.0, quantity=50.0, cost_basis=5_000.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert [r.symbol for r in plan.rows] == ["AAA"]


def test_fractional_off_floors_the_quantity():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].target_quantity == 3.0
    assert plan.rows[0].estimated_value == 900.0


def test_round_quantity_returns_zero_for_a_non_positive_price():
    assert pa.round_quantity(1_000.0, 0.0, None, allow_fractional=False) == 0.0
