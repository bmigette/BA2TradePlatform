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


def test_held_above_target_produces_a_sell_of_the_difference():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    plan = pa.compute_allocation(5_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == 50.0
    assert row.delta_quantity == -50.0
    assert row.side == OrderDirection.SELL
    assert row.estimated_value == 5_000.0
    assert row.bp_cost == 0.0
    assert plan.total_sell_value == 5_000.0


def test_held_below_target_produces_a_top_up_buy():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=20.0, cost_basis=1_800.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == 100.0
    assert row.delta_quantity == 80.0
    assert row.side == OrderDirection.BUY
    assert row.estimated_value == 8_000.0


def test_zero_target_on_a_held_symbol_closes_the_position():
    labels = [
        LabelTarget("KEEP", 100.0, [SymbolTarget("AAA", 100.0)]),
        LabelTarget("EXIT", 0.0, [SymbolTarget("BBB", 100.0)]),
    ]
    current = {"AAA": _pos("AAA", 100.0),
               "BBB": _pos("BBB", 20.0, quantity=30.0, cost_basis=500.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["BBB"].target_quantity == 0.0
    assert by["BBB"].delta_quantity == -30.0
    assert by["BBB"].side == OrderDirection.SELL
    assert pa.REASON_CLOSE_TO_ZERO in by["BBB"].reasons


def test_already_on_target_produces_no_order():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert plan.buy_rows == []
    assert plan.sell_rows == []


def test_whole_share_mode_never_emits_a_fractional_delta():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.5, cost_basis=1_050.0)}
    plan = pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].delta_quantity == 9.0


def test_fractional_on_without_increment_rounds_to_four_decimals():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == pytest.approx(3.3333)
    assert row.fractional is True
    assert pa.REASON_FRACTIONAL in row.reasons


def test_fractional_on_rounds_down_to_the_brokers_min_trade_increment():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_trade_increment=0.01)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    assert plan.rows[0].target_quantity == pytest.approx(3.33)


def test_fractional_requested_on_a_non_fractionable_symbol_falls_back_to_whole_shares():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_quantity == 3.0
    assert row.fractional is False
    assert pa.REASON_WHOLE_SHARE_FLOOR in row.reasons


def test_quantity_below_min_order_size_is_dropped_to_zero():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_order_size=5.0)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    assert plan.rows[0].target_quantity == 0.0
    assert plan.rows[0].delta_quantity == 0.0


def test_buys_scale_pro_rata_when_they_exceed_buying_power():
    labels = [LabelTarget("ONE", 100.0, [SymbolTarget("MARG", 50.0), SymbolTarget("NONM", 50.0)])]
    current = {"MARG": _pos("MARG", 100.0), "NONM": _pos("NONM", 100.0)}
    margin = {
        "MARG": MarginInfo(symbol="MARG", bp_factor=1.0, marginable=True),
        "NONM": MarginInfo(symbol="NONM", bp_factor=2.0, marginable=False),
    }
    plan = pa.compute_allocation(100_000.0, 60_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=2.0)
    by = {r.symbol: r for r in plan.rows}
    assert plan.scale_factor == pytest.approx(0.4)
    assert by["MARG"].delta_quantity == 200.0
    assert by["NONM"].delta_quantity == 200.0
    assert by["MARG"].bp_cost == pytest.approx(20_000.0)
    assert by["NONM"].bp_cost == pytest.approx(40_000.0)
    assert plan.required_buying_power == pytest.approx(60_000.0)
    assert plan.bp_usage_pct == pytest.approx(100.0)
    assert pa.REASON_NOT_MARGINABLE in by["NONM"].reasons
    assert "scaled ×0.40 to fit buying power" in by["MARG"].reasons


def test_sells_are_never_scaled_down():
    labels = [LabelTarget("ONE", 100.0, [SymbolTarget("BUYME", 50.0), SymbolTarget("SELLME", 50.0)])]
    current = {"BUYME": _pos("BUYME", 100.0),
               "SELLME": _pos("SELLME", 100.0, quantity=1000.0, cost_basis=100_000.0)}
    plan = pa.compute_allocation(100_000.0, 1_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["SELLME"].delta_quantity == -500.0
    assert by["SELLME"].estimated_value == 50_000.0
    assert not any("scaled" in r for r in by["SELLME"].reasons)
    assert by["BUYME"].delta_quantity == 10.0
    assert plan.total_sell_value == 50_000.0


def test_zero_buying_power_skips_every_buy():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 0.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].skipped is True
    assert plan.total_buy_value == 0.0


def test_symbol_without_a_price_is_skipped_not_guessed():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0)])]
    current = {"AAA": _pos("AAA", None), "BBB": _pos("BBB", 50.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].skipped is True
    assert by["AAA"].delta_quantity == 0.0
    assert pa.REASON_NO_PRICE in by["AAA"].reasons
    assert plan.unallocatable_pct == pytest.approx(60.0)
    assert by["BBB"].target_quantity == 80.0


def test_symbol_with_a_zero_price_is_skipped():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 0.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].skipped is True
    assert pa.REASON_NO_PRICE in plan.rows[0].reasons


def test_empty_managed_label_contributes_to_unallocatable_pct():
    labels = [LabelTarget("FULL", 70.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 30.0, [])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert [r.symbol for r in plan.rows] == ["AAA"]
    assert plan.rows[0].target_quantity == 70.0
    assert plan.unallocatable_pct == pytest.approx(30.0)
    assert "label 'EMPTY' has no symbols - 30.00% unallocated" in plan.warnings


def test_negative_label_target_is_clamped_to_zero():
    labels = [LabelTarget("A", -20.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    plan = pa.compute_allocation(10_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.target_notional == 0.0
    assert pa.REASON_NEGATIVE_CLAMPED in row.reasons
    assert row.delta_quantity == -10.0


def test_even_split_of_three_totals_exactly_one_hundred():
    assert pa.even_split_pct(3) == [33.33, 33.33, 33.34]
    assert sum(pa.even_split_pct(3)) == 100.0


def test_even_split_of_seven_still_totals_exactly_one_hundred():
    parts = pa.even_split_pct(7)
    assert len(parts) == 7
    assert parts[0] == pytest.approx(14.28)
    assert sum(parts) == pytest.approx(100.0)


def test_even_split_of_zero_symbols_is_empty():
    assert pa.even_split_pct(0) == []
    assert pa.even_split_pct(-4) == []


def test_build_symbol_targets_defaults_to_even_when_nothing_stored():
    out = pa.build_symbol_targets(["A", "B", "C", "D"])
    assert [t.weight_pct for t in out] == [25.0, 25.0, 25.0, 25.0]
    assert [t.symbol for t in out] == ["A", "B", "C", "D"]


def test_build_symbol_targets_shares_the_remainder_among_unstored_symbols():
    out = pa.build_symbol_targets(["A", "B", "C"], {"A": 50.0})
    assert {t.symbol: t.weight_pct for t in out} == {"A": 50.0, "B": 25.0, "C": 25.0}


def test_validate_label_targets_accepts_a_valid_hundred_percent_set():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 40.0, [SymbolTarget("BBB", 100.0)])]
    assert pa.validate_label_targets(labels) == []


def test_validate_label_targets_rejects_a_total_below_one_hundred():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)])]
    errors = pa.validate_label_targets(labels)
    assert errors == [pa.ERROR_LABEL_TOTAL_FMT.format(total=60.0)]
    assert errors == ["label targets total 60.00% - must total 100%"]


def test_validate_label_targets_accepts_a_total_inside_the_tolerance():
    """LABEL_TOTAL_TOLERANCE_PCT is 0.01 PERCENTAGE POINTS: 33.33+33.33+33.34 passes."""
    labels = [LabelTarget("A", 33.33, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 33.33, [SymbolTarget("BBB", 100.0)]),
              LabelTarget("C", 33.34, [SymbolTarget("CCC", 100.0)])]
    assert pa.validate_label_targets(labels) == []


def test_validate_label_targets_rejects_a_non_zero_label_with_no_symbols():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 40.0, [])]
    errors = pa.validate_label_targets(labels)
    assert pa.ERROR_LABEL_NO_SYMBOLS_FMT.format(label="B", pct=40.0) in errors
    assert any("B" in e and "no symbols" in e for e in errors)


def test_validate_label_targets_rejects_duplicates():
    labels = [LabelTarget("A", 50.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("A", 50.0, [SymbolTarget("BBB", 100.0)])]
    assert pa.ERROR_LABEL_DUPLICATE_FMT.format(label="A") in pa.validate_label_targets(labels)


def test_validate_label_targets_rejects_a_negative_target():
    labels = [LabelTarget("A", 120.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", -20.0, [SymbolTarget("BBB", 100.0)])]
    errors = pa.validate_label_targets(labels)
    assert any("negative" in e for e in errors)


def test_compute_base_notional_adds_managed_cost_basis_to_buying_power():
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=900.0),
               "ZZZ": _pos("ZZZ", 100.0, quantity=99.0, cost_basis=9_900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA"]) == 5_900.0


def test_compute_base_notional_managed_symbol_with_no_position_contributes_zero():
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=1500.0)}
    assert pa.compute_base_notional(10_000.0, current, ["AAA", "NVDA"]) == 11_500.0


def test_compute_base_notional_counts_a_repeated_symbol_once():
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA", "AAA"]) == 5_900.0


def test_compute_base_notional_raises_when_buying_power_is_none():
    with pytest.raises(ValueError):
        pa.compute_base_notional(None, {}, ["AAA"])
