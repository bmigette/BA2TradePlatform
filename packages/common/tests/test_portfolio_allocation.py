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


def test_an_order_below_min_order_size_is_suppressed_and_the_target_kept():
    """CONTRACT CHANGED (Task 25): min_order_size constrains the ORDER, not the TARGET.

    This used to assert that the min order size zeroed ``target_quantity`` -- i.e.
    that it rewrote what we want to HOLD. It does not: it suppresses the trade and
    leaves the position where it is, with a reason saying why. On a FLAT position
    the number is still 0.0, but it is now "held nothing, ordered nothing" rather
    than "target rewritten to nothing", which is what stopped a held position from
    being liquidated by a rounding rule.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_order_size=5.0)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.target_notional == 1_000.0            # the TARGET is untouched
    assert row.target_quantity == 0.0                # post-trade: flat stays flat
    assert pa.REASON_BELOW_MIN_ORDER_FMT.format(size=5.0) in row.reasons


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


def test_validate_label_targets_rejects_symbol_weights_totalling_one_fifty():
    """The page lets a user type symbol weights directly; 100+50 must not silently
    over-deploy the label."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0), SymbolTarget("BBB", 50.0)])]
    errors = pa.validate_label_targets(labels)
    assert errors == [pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=150.0)]
    assert errors == ["label 'A' symbol weights total 150.00% - must total 100%"]


def test_validate_label_targets_rejects_a_naive_two_dp_symbol_split():
    """3 x 33.33 = 99.99, one hair OUTSIDE the 0.01pp tolerance -- use even_split_pct."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget(s, 33.33) for s in ("AAA", "BBB", "CCC")])]
    assert pa.validate_label_targets(labels) == [
        pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=99.99)]


def test_validate_label_targets_rejects_symbol_weights_just_over_the_tolerance():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.01)])]
    assert pa.validate_label_targets(labels) == [
        pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=100.01)]


def test_validate_label_targets_accepts_an_even_split_pct_symbol_set():
    """The whole point of even_split_pct: its output always passes this check."""
    syms = [SymbolTarget(s, p) for s, p in zip(("AAA", "BBB", "CCC"), pa.even_split_pct(3))]
    assert pa.validate_label_targets([LabelTarget("A", 100.0, syms)]) == []


def test_validate_label_targets_rejects_a_negative_symbol_weight():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 120.0), SymbolTarget("BBB", -20.0)])]
    errors = pa.validate_label_targets(labels)
    assert pa.ERROR_SYMBOL_NEGATIVE_FMT.format(label="A", symbol="BBB", pct=-20.0) in errors
    assert any("A" in e and "BBB" in e and "negative" in e for e in errors)


def test_validate_label_targets_rejects_a_duplicate_symbol_inside_one_label():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 50.0), SymbolTarget("AAA", 50.0)])]
    errors = pa.validate_label_targets(labels)
    assert pa.ERROR_SYMBOL_DUPLICATE_FMT.format(label="A", symbol="AAA") in errors
    assert any("A" in e and "AAA" in e and "duplicate" in e for e in errors)


def test_validate_label_targets_allows_the_same_symbol_in_two_different_labels():
    """Decision 7: a symbol MAY sit in several labels and its targets sum. Only a
    repeat WITHIN one label is an error."""
    labels = [LabelTarget("A", 50.0, [SymbolTarget("XXX", 100.0)]),
              LabelTarget("B", 50.0, [SymbolTarget("XXX", 100.0)])]
    assert pa.validate_label_targets(labels) == []


def test_validate_label_targets_empty_label_with_a_zero_target_stays_valid():
    """An empty label at 0% is not an error -- and gets no symbol-total error either."""
    labels = [LabelTarget("FULL", 100.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 0.0, [])]
    assert pa.validate_label_targets(labels) == []


def test_label_investment_splits_the_amount_and_only_buys():
    label = LabelTarget("ARK26", 40.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0)])
    current = {"AAA": _pos("AAA", 100.0, quantity=7.0, cost_basis=700.0),
               "BBB": _pos("BBB", 50.0, quantity=1000.0, cost_basis=50_000.0)}
    plan = pa.compute_label_investment(label, 10_000.0, current, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].delta_quantity == 60.0
    assert by["AAA"].target_quantity == 67.0
    assert by["BBB"].delta_quantity == 80.0
    assert plan.total_sell_value == 0.0
    assert plan.net_buy_value == plan.total_buy_value == 10_000.0


def test_label_investment_scales_down_to_available_buying_power():
    label = LabelTarget("ARK26", 100.0, [SymbolTarget("AAA", 100.0)])
    plan = pa.compute_label_investment(label, 10_000.0, {"AAA": _pos("AAA", 100.0)}, {},
                                       available_buying_power=2_500.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert plan.scale_factor == pytest.approx(0.25)
    assert plan.rows[0].delta_quantity == 25.0


def test_label_investment_on_an_empty_label_allocates_nothing():
    plan = pa.compute_label_investment(LabelTarget("EMPTY", 100.0, []), 10_000.0, {}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows == []
    assert plan.unallocatable_pct == 100.0


def test_validate_symbol_weights_accepts_an_even_split():
    syms = [SymbolTarget(s, p) for s, p in zip(("AAA", "BBB", "CCC"), pa.even_split_pct(3))]
    assert pa.validate_symbol_weights(LabelTarget("A", 40.0, syms)) == []


def test_validate_symbol_weights_rejects_a_one_fifty_total():
    """The INVEST_LABEL gate: 150% would turn a 10k budget into 15k of buys."""
    label = LabelTarget("A", 40.0, [SymbolTarget("AAA", 100.0), SymbolTarget("BBB", 50.0)])
    assert pa.validate_symbol_weights(label) == [
        pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=150.0)]
    assert pa.validate_symbol_weights(label) == [
        "label 'A' symbol weights total 150.00% - must total 100%"]


def test_validate_symbol_weights_rejects_a_total_under_one_hundred():
    """Concern 3: weights totalling 60 would silently leave 40% of the budget as cash."""
    label = LabelTarget("A", 40.0, [SymbolTarget("AAA", 60.0)])
    assert pa.validate_symbol_weights(label) == [
        pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=60.0)]


def test_validate_symbol_weights_rejects_a_negative_weight():
    label = LabelTarget("A", 40.0, [SymbolTarget("AAA", 120.0), SymbolTarget("BBB", -20.0)])
    assert pa.validate_symbol_weights(label) == [
        pa.ERROR_SYMBOL_NEGATIVE_FMT.format(label="A", symbol="BBB", pct=-20.0)]


def test_validate_symbol_weights_rejects_a_duplicate_symbol():
    """The engine now COALESCES a duplicate, so validation is what reports it."""
    label = LabelTarget("A", 40.0, [SymbolTarget("AAA", 50.0), SymbolTarget("AAA", 50.0)])
    assert pa.validate_symbol_weights(label) == [
        pa.ERROR_SYMBOL_DUPLICATE_FMT.format(label="A", symbol="AAA")]


def test_validate_symbol_weights_on_an_empty_label_returns_no_errors():
    """An empty label cannot have bad weights; whether it may be INVESTED into is
    the caller's call, not this check's."""
    assert pa.validate_symbol_weights(LabelTarget("EMPTY", 40.0, [])) == []


def test_validate_symbol_weights_ignores_the_labels_own_target_pct():
    """The whole reason this is separate: an INVEST_LABEL run picks ONE label, whose
    target_pct is meaningless, so validate_label_targets' total-100 rule cannot apply."""
    assert pa.validate_symbol_weights(LabelTarget("A", 40.0, [SymbolTarget("AAA", 100.0)])) == []
    assert pa.validate_symbol_weights(LabelTarget("A", 0.0, [SymbolTarget("AAA", 100.0)])) == []


def test_validate_label_targets_reports_exactly_what_validate_symbol_weights_does():
    """One implementation behind two entry points -- the strings cannot drift."""
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 120.0), SymbolTarget("AAA", -20.0)])
    assert pa.validate_label_targets([label]) == pa.validate_symbol_weights(label)
    assert len(pa.validate_symbol_weights(label)) == 2


def test_validate_label_targets_forwards_its_tolerance_to_the_symbol_check():
    """Pre-existing behaviour: a widened tolerance loosens BOTH levels, not just labels."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 99.0)])]
    assert pa.validate_label_targets(labels) == [
        pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=99.0)]
    assert pa.validate_label_targets(labels, tolerance=5.0) == []
    assert pa.validate_symbol_weights(labels[0], tolerance=5.0) == []


def test_label_investment_coalesces_a_duplicated_symbol_into_one_row():
    """Two rows for one symbol means two independent orders for it in the same run --
    which is what compute_allocation's per-symbol dict already prevents."""
    label = LabelTarget("L", 100.0, [SymbolTarget("AAA", 50.0), SymbolTarget("AAA", 50.0)])
    plan = pa.compute_label_investment(label, 10_000.0, {"AAA": _pos("AAA", 100.0)}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert [r.symbol for r in plan.rows] == ["AAA"]
    assert plan.rows[0].delta_quantity == 100.0
    assert plan.total_buy_value == pytest.approx(10_000.0)


def test_label_investment_coalescing_preserves_first_appearance_order():
    label = LabelTarget("L", 100.0, [SymbolTarget("BBB", 25.0), SymbolTarget("AAA", 50.0),
                                     SymbolTarget("BBB", 25.0)])
    plan = pa.compute_label_investment(label, 10_000.0,
                                       {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 100.0)}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert [r.symbol for r in plan.rows] == ["BBB", "AAA"]
    assert [r.delta_quantity for r in plan.rows] == [50.0, 50.0]


def test_apply_order_impacts_replaces_the_estimated_bp_cost():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].bp_cost == 10_000.0
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-25_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=1_000_000.0)
    assert out.rows[0].bp_cost == 25_000.0
    assert "broker precheck disagreed on XXX - re-solved" in out.warnings
    assert plan.rows[0].bp_cost == 10_000.0


def test_apply_order_impacts_rescales_when_the_precheck_no_longer_fits():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 10_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-20_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=10_000.0)
    assert out.scale_factor == pytest.approx(0.5)
    assert out.rows[0].delta_quantity == 50.0
    assert out.rows[0].bp_cost == pytest.approx(10_000.0)


def test_apply_order_impacts_skips_a_rejected_order():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=0.0,
                                  accepted=False, errors=["symbol not tradeable"])}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=1_000_000.0)
    assert out.rows[0].skipped is True
    assert "symbol not tradeable" in out.rows[0].reasons
    assert out.total_buy_value == 0.0


def test_consume_income_events_takes_oldest_first_and_partially_consumes_the_last():
    out = pa.consume_income_events([(1, 100.0), (2, 250.0), (3, 500.0)], 300.0)
    assert out == [(1, 100.0), (2, 200.0)]


def test_consume_income_events_returns_nothing_for_a_sell_funded_run():
    assert pa.consume_income_events([(1, 100.0)], 0.0) == []
    assert pa.consume_income_events([(1, 100.0)], -50.0) == []


def test_consume_income_events_with_an_empty_ledger_returns_empty():
    assert pa.consume_income_events([], 500.0) == []


def test_consume_income_events_never_takes_more_than_the_ledger_holds():
    out = pa.consume_income_events([(1, 100.0), (2, 50.0)], 1_000.0)
    assert sum(a for _, a in out) == pytest.approx(150.0)


def test_current_value_in_cost_mode_is_the_cost_basis():
    state = _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)
    assert pa.current_value(state, pa.VALUATION_MODE_COST) == 900.0


def test_current_value_in_market_mode_is_quantity_times_price():
    state = _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)
    assert pa.current_value(state, pa.VALUATION_MODE_MARKET) == 2_000.0


def test_current_value_of_a_flat_symbol_is_zero_in_both_modes():
    assert pa.current_value(None, pa.VALUATION_MODE_COST) == 0.0
    assert pa.current_value(None, pa.VALUATION_MODE_MARKET) == 0.0


def test_current_value_in_market_mode_without_a_price_is_zero_not_a_guess():
    """The caller skips a no-price symbol anyway; this must not invent a value."""
    assert pa.current_value(_pos("AAA", None, quantity=10.0, cost_basis=900.0),
                            pa.VALUATION_MODE_MARKET) == 0.0


def test_current_value_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        pa.current_value(_pos("AAA", 10.0), "marketish")


def test_base_notional_in_market_mode_uses_quantity_times_price():
    current = {"AAA": _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA"],
                                    valuation_mode=pa.VALUATION_MODE_MARKET) == 7_000.0
    assert pa.compute_base_notional(5_000.0, current, ["AAA"],
                                    valuation_mode=pa.VALUATION_MODE_COST) == 5_900.0


def test_cost_mode_sizes_the_top_up_off_the_purchase_value_not_the_share_count():
    """Held 20 shares bought at 90 (cost basis 1800) now worth 100 each.

    market: target 100 shares, hold 20 -> buy 80.
    cost:   target notional 10000, cost basis 1800 -> spend 8200 -> buy 82.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=20.0, cost_basis=1_800.0)}
    market = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                   allow_fractional=False, default_bp_factor=1.0,
                                   valuation_mode=pa.VALUATION_MODE_MARKET)
    cost = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert market.rows[0].delta_quantity == 80.0
    assert cost.rows[0].delta_quantity == 82.0
    assert cost.rows[0].target_quantity == 102.0


def test_market_mode_trims_a_doubled_position_that_cost_mode_leaves_alone():
    """Bought 50 at 100 (cost basis 5000), now 200 each. Target notional 5000."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 200.0, quantity=50.0, cost_basis=5_000.0)}
    market = pa.compute_allocation(5_000.0, 1_000_000.0, labels, current, {},
                                   allow_fractional=False, default_bp_factor=1.0,
                                   valuation_mode=pa.VALUATION_MODE_MARKET)
    cost = pa.compute_allocation(5_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert market.rows[0].delta_quantity == -25.0     # 25 shares is now 5000
    assert market.rows[0].side == OrderDirection.SELL
    assert cost.rows[0].delta_quantity == 0.0          # already at its purchase weight
    assert cost.rows[0].side is None


def test_cost_mode_sizes_a_trim_off_the_average_cost_not_the_market_price():
    """CONTRACT FIXED: selling N shares removes N x AVERAGE COST from the basis.

    ``cost_basis`` is ``quantity x avg_entry_price``, so a basis gap converts to
    shares at the AVERAGE COST, never at the market price. Dividing by the price
    made the trim wrong by exactly the ratio between the two -- and when the price
    had HALVED it asked to sell twice the right number, which the hold-clamp then
    turned into a full liquidation with no reason string to show for it.

    Hold 100 at an average of 100 (basis 10,000); target basis 5,000 is a half
    trim, so 50 shares, whatever the market price happens to be today.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    for price in (50.0, 100.0, 200.0):
        current = {"XXX": _pos("XXX", price, quantity=100.0, cost_basis=10_000.0)}
        row = pa.compute_allocation(5_000.0, 1_000_000.0, labels, current, {},
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode=pa.VALUATION_MODE_COST).rows[0]
        assert row.delta_quantity == -50.0, price
        assert row.target_quantity == 50.0, price
        # ...and the trim CONVERGES: the basis left behind is the basis asked for.
        assert row.target_quantity * (10_000.0 / 100.0) == pytest.approx(5_000.0)


def test_cost_mode_trims_towards_the_target_basis_rounding_down():
    """Basis 20000 on 10 shares (average 2000) with a 5000 target: 7.5 shares of
    basis must go, and a whole-share account rounds the SELL down to 7 -- never up,
    so the trim under-shoots the target rather than overshooting it."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=20_000.0)}
    plan = pa.compute_allocation(5_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert plan.rows[0].delta_quantity == -7.0
    assert plan.rows[0].target_quantity == 3.0


def test_round_delta_quantity_clamps_a_sell_to_the_holding():
    """Defence in depth. With the average-cost divisor a basis gap can no longer
    exceed the basis, so compute_allocation cannot reach this -- but the helper is
    public and must still refuse to sell shares that are not there."""
    assert pa.round_delta_quantity(-1_000_000.0, 100.0, None, allow_fractional=False,
                                   current_quantity=10.0) == -10.0


def test_round_delta_quantity_never_extends_a_short():
    """A negative holding clamps a SELL to zero, not to the negative quantity."""
    assert pa.round_delta_quantity(-5_000.0, 100.0, None, allow_fractional=False,
                                   current_quantity=-50.0) == 0.0


def test_cost_mode_still_closes_a_position_on_a_zero_target():
    labels = [LabelTarget("EXIT", 0.0, [SymbolTarget("BBB", 100.0)])]
    current = {"BBB": _pos("BBB", 20.0, quantity=30.0, cost_basis=500.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_COST)
    assert plan.rows[0].delta_quantity == -30.0
    assert pa.REASON_CLOSE_TO_ZERO in plan.rows[0].reasons


def test_compute_allocation_rejects_an_unknown_valuation_mode():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    with pytest.raises(ValueError):
        pa.compute_allocation(10_000.0, 1_000.0, labels, {"XXX": _pos("XXX", 100.0)}, {},
                              allow_fractional=False, default_bp_factor=1.0,
                              valuation_mode="marketish")


def test_the_python_defaults_match_each_functions_pinned_behaviour():
    """compute_base_notional defaults to COST; compute_allocation defaults to MARKET.
    Live code always passes the mode explicitly and relies on neither."""
    current = {"AAA": _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)}
    assert pa.compute_base_notional(0.0, current, ["AAA"]) == 900.0
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    plan = pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].delta_quantity == 0.0    # market: 2000/200 = 10 shares, already held


def test_compute_label_investment_rejects_an_unknown_valuation_mode():
    """Accepted for call-site symmetry, so it must be validated like the others."""
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])
    with pytest.raises(ValueError):
        pa.compute_label_investment(label, 1_000.0, {"AAA": _pos("AAA", 100.0)}, {},
                                    available_buying_power=1_000_000.0,
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode="marketish")


def test_compute_label_investment_arithmetic_is_identical_in_both_modes():
    """INVEST_LABEL ADDS a budget, so the mode cannot change what it buys.

    Compares the ROWS and the money totals rather than the whole ``to_dict()``:
    the plan now RECORDS its valuation mode, so the two dicts differ by exactly
    that key -- which is the point of recording it, and is asserted below.
    """
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])
    current = {"AAA": _pos("AAA", 100.0, quantity=20.0, cost_basis=500.0)}
    plans = [pa.compute_label_investment(label, 1_000.0, current, {},
                                         available_buying_power=1_000_000.0,
                                         allow_fractional=False, default_bp_factor=1.0,
                                         valuation_mode=mode)
             for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET)]
    assert [r.to_dict() for r in plans[0].rows] == [r.to_dict() for r in plans[1].rows]
    assert plans[0].total_buy_value == plans[1].total_buy_value
    assert plans[0].required_buying_power == plans[1].required_buying_power
    assert plans[0].rows[0].delta_quantity == 10.0
    assert plans[0].valuation_mode == pa.VALUATION_MODE_COST
    assert plans[1].valuation_mode == pa.VALUATION_MODE_MARKET


def test_the_base_the_percentages_and_the_deltas_never_disagree():
    """The mode picks ONE meaning of "current value" for all three at once.

    A portfolio already sitting at its weights UNDER THE SELECTED MODE must produce
    no orders, and each row's target_notional must equal that same current value.
    AAA is 45% by cost but 80% by market, so a mode that leaked would show up as a
    non-zero delta on both symbols.
    """
    aaa = _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)    # cost 900, market 2000
    bbb = _pos("BBB", 100.0, quantity=5.0, cost_basis=1_100.0)   # cost 1100, market 500
    current = {"AAA": aaa, "BBB": bbb}
    for mode, weights, base in ((pa.VALUATION_MODE_COST, (45.0, 55.0), 2_000.0),
                                (pa.VALUATION_MODE_MARKET, (80.0, 20.0), 2_500.0)):
        computed_base = pa.compute_base_notional(0.0, current, ["AAA", "BBB"],
                                                 valuation_mode=mode)
        assert computed_base == pytest.approx(base)
        labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", weights[0]),
                                           SymbolTarget("BBB", weights[1])])]
        plan = pa.compute_allocation(computed_base, 1_000_000.0, labels, current, {},
                                     allow_fractional=False, default_bp_factor=1.0,
                                     valuation_mode=mode)
        by = {r.symbol: r for r in plan.rows}
        for sym in ("AAA", "BBB"):
            assert by[sym].target_notional == pytest.approx(
                pa.current_value(current[sym], mode)), (mode, sym)
            assert by[sym].delta_quantity == 0.0, (mode, sym)
            assert by[sym].side is None
        assert sum(r.target_notional for r in plan.rows) == pytest.approx(computed_base)


def test_a_min_order_size_never_liquidates_a_position_in_either_mode():
    """Holding exactly the 3.3333 shares a 1000 target buys at 300, min_order_size 5.

    Two separate defects used to conspire here: the close branch keyed on the
    ROUNDED quantity, and round_quantity applied min_order_size to the TARGET. In
    market mode that made the target 0 shares and sold the lot -- an unexplained
    full exit on a position that is exactly where it should be. Neither mode may
    trade anything here.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_order_size=5.0)}
    current = {"XXX": _pos("XXX", 300.0, quantity=3.3333, cost_basis=1_000.0)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        row = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                    allow_fractional=True, default_bp_factor=1.0,
                                    valuation_mode=mode).rows[0]
        assert row.delta_quantity == 0.0, mode
        assert row.side is None, mode
        assert pa.REASON_CLOSE_TO_ZERO not in row.reasons, mode
        assert row.target_quantity == pytest.approx(3.3333), mode


def test_an_order_below_min_order_size_leaves_the_position_untouched():
    """A REAL gap this time -- hold 10 at 100, target 1200, so 2 shares short. The
    broker will not take an order that small, so nothing trades and the row SAYS SO
    rather than going quiet."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        row = pa.compute_allocation(1_200.0, 1_000_000.0, labels, current, margin,
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode=mode).rows[0]
        assert row.delta_quantity == 0.0, mode
        assert row.target_quantity == 10.0, mode      # position HELD, not rewritten
        assert row.estimated_value == 0.0, mode
        assert row.bp_cost == 0.0, mode
        assert pa.REASON_BELOW_MIN_ORDER_FMT.format(size=5.0) in row.reasons, mode


def test_an_order_at_the_min_order_size_still_trades():
    """The boundary: 6 shares clears a minimum of 5 and is submitted normally."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    row = pa.compute_allocation(1_600.0, 1_000_000.0, labels, current, margin,
                                allow_fractional=False, default_bp_factor=1.0).rows[0]
    assert row.delta_quantity == 6.0
    assert row.side == OrderDirection.BUY
    assert row.target_quantity == 16.0
    assert not any(r.startswith("below") for r in row.reasons)


def test_a_sell_below_min_order_size_is_suppressed_too():
    """Suppression is on the MAGNITUDE: an unsendable trim is not sent either."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    row = pa.compute_allocation(800.0, 1_000_000.0, labels, current, margin,
                                allow_fractional=False, default_bp_factor=1.0).rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.target_quantity == 10.0
    assert pa.REASON_BELOW_MIN_ORDER_FMT.format(size=5.0) in row.reasons


def test_label_investment_suppresses_an_order_below_min_order_size():
    """The same rule on the INVEST_LABEL path, with the same reason string."""
    label = LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_order_size=5.0)}
    current = {"XXX": _pos("XXX", 300.0, quantity=2.0, cost_basis=600.0)}
    plan = pa.compute_label_investment(label, 1_000.0, current, margin,
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=True, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.target_quantity == 2.0
    assert pa.REASON_BELOW_MIN_ORDER_FMT.format(size=5.0) in row.reasons
    assert plan.total_buy_value == 0.0


def test_both_modes_report_target_quantity_as_the_post_trade_holding():
    """ONE meaning for target_quantity: what the account HOLDS if the row executes.

    Holding 10.5 with whole-share rounding and a 2000 target at 100, the ideal
    count is 20 but only 9 whole shares can be bought, so the honest answer is
    19.5 in both modes -- always current_quantity + delta_quantity.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.5, cost_basis=1_050.0)}
    rows = [pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                                  allow_fractional=False, default_bp_factor=1.0,
                                  valuation_mode=mode).rows[0]
            for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET)]
    for row in rows:
        assert row.delta_quantity == 9.0
        assert row.target_quantity == 19.5
        assert row.target_quantity == row.current_quantity + row.delta_quantity
    assert rows[0].target_quantity == rows[1].target_quantity


def test_a_negative_target_clamped_to_zero_still_liquidates():
    """The other direction: Task 21's clamp zeroes target_notional too, so the
    re-keyed branch still fires and still reports REASON_CLOSE_TO_ZERO."""
    labels = [LabelTarget("A", -20.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        plan = pa.compute_allocation(10_000.0, 0.0, labels, current, {},
                                     allow_fractional=False, default_bp_factor=1.0,
                                     valuation_mode=mode)
        row = plan.rows[0]
        assert row.target_notional == 0.0, mode
        assert row.target_quantity == 0.0, mode
        assert row.delta_quantity == -10.0, mode
        assert pa.REASON_NEGATIVE_CLAMPED in row.reasons
        assert pa.REASON_CLOSE_TO_ZERO in row.reasons


def test_both_kinds_of_skipped_row_serialise_a_null_side():
    """A no-price skip and a scaled-to-nothing buy must LOOK the same to a consumer
    reading the raw side field (Section G's dry-run table does exactly that)."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 50.0), SymbolTarget("BBB", 50.0)])]
    current = {"AAA": _pos("AAA", None), "BBB": _pos("BBB", 100.0)}
    plan = pa.compute_allocation(10_000.0, 10.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    by = {r.symbol: r for r in plan.rows}
    assert by["AAA"].skipped is True and pa.REASON_NO_PRICE in by["AAA"].reasons
    assert by["BBB"].skipped is True and any("scaled" in r for r in by["BBB"].reasons)
    assert by["AAA"].side is None
    assert by["BBB"].side is None
    blob = json.loads(json.dumps(plan.to_dict()))
    assert [r["side"] for r in blob["rows"]] == [None, None]


def test_apply_order_impacts_keeps_the_min_trade_increment_on_the_re_solve():
    """The precheck re-solve must round on the BROKER's grid, not the 4dp default."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_trade_increment=0.25)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    assert plan.rows[0].delta_quantity == pytest.approx(3.25)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-1_950.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=975.0,
                                 margin=margin)
    assert out.rows[0].delta_quantity == pytest.approx(1.5)   # 1.625 off-grid without it


def test_apply_order_impacts_keeps_the_min_order_size_on_the_re_solve():
    """Same forwarding, the other piece of metadata: a re-solve below the broker's
    minimum is skipped rather than emitted."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_order_size=2.0)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0)
    assert plan.rows[0].delta_quantity == pytest.approx(3.3333)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-2_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=500.0,
                                 margin=margin)
    assert out.rows[0].delta_quantity == 0.0
    assert out.rows[0].skipped is True


def test_apply_order_impacts_compounds_the_scale_factor_and_keeps_one_reason():
    """Scaled 0.6 by the first solve then 0.5 by the precheck is 0.3 against the
    ORIGINAL target -- 300 of the 1000 shares first asked for."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(100_000.0, 60_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.scale_factor == pytest.approx(0.6)
    assert plan.rows[0].delta_quantity == 600.0
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-120_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=60_000.0)
    assert out.scale_factor == pytest.approx(0.3)
    assert out.rows[0].delta_quantity == 300.0
    scaled = [r for r in out.rows[0].reasons if r.startswith("scaled ")]
    assert scaled == ["scaled ×0.30 to fit buying power"]


def test_apply_order_impacts_leaves_a_single_scale_reason_untouched():
    """Only the SECOND scaling compounds: a plan that fitted the first time keeps
    the precheck's own factor verbatim."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.scale_factor == 1.0
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-20_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=10_000.0)
    assert out.scale_factor == pytest.approx(0.5)
    scaled = [r for r in out.rows[0].reasons if r.startswith("scaled ")]
    assert scaled == ["scaled ×0.50 to fit buying power"]


# --- CRITICAL 2: a missing base must never be read as "hold nothing" -----------

def test_compute_allocation_refuses_a_none_base_notional():
    """`float(None or 0.0)` used to make this a zero base -- i.e. a target of 0 for
    every managed symbol, i.e. LIQUIDATE THE PORTFOLIO. That is the accident
    PositionFetchFailed exists to prevent, arriving through a different door."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    with pytest.raises(ValueError):
        pa.compute_allocation(None, 1_000_000.0, labels, current, {},
                              allow_fractional=False, default_bp_factor=1.0)


def test_compute_allocation_refuses_a_negative_base_notional():
    """A margin call can drive a computed base negative; flattening the whole
    managed portfolio is not the right response to arithmetic."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    with pytest.raises(ValueError):
        pa.compute_allocation(-1.0, 1_000_000.0, labels, {"XXX": _pos("XXX", 100.0)}, {},
                              allow_fractional=False, default_bp_factor=1.0)


def test_compute_allocation_refuses_a_none_buying_power():
    """No fallback values for balances -- the same rule compute_base_notional applies."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    with pytest.raises(ValueError):
        pa.compute_allocation(10_000.0, None, labels, {"XXX": _pos("XXX", 100.0)}, {},
                              allow_fractional=False, default_bp_factor=1.0)


def test_compute_allocation_accepts_a_zero_base():
    """Zero is a real answer (a flat, empty account); only None and negative are not."""
    plan = pa.compute_allocation(0.0, 0.0, [], {}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows == []


def test_compute_label_investment_refuses_a_none_amount_or_buying_power():
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])
    with pytest.raises(ValueError):
        pa.compute_label_investment(label, None, {}, {}, available_buying_power=1_000.0,
                                    allow_fractional=False, default_bp_factor=1.0)
    with pytest.raises(ValueError):
        pa.compute_label_investment(label, 1_000.0, {}, {}, available_buying_power=None,
                                    allow_fractional=False, default_bp_factor=1.0)


# --- 3: one shape for "no order", including a precheck rejection ---------------

def test_a_precheck_rejection_is_zeroed_like_every_other_no_order():
    """A refused order kept side=BUY, delta=100 and estimated_value=10,000 while
    claiming skipped=True, so Section G's dry-run would render it as a live BUY."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0, quantity=5.0, cost_basis=500.0)},
                                 {}, allow_fractional=False, default_bp_factor=1.0)
    assert plan.rows[0].delta_quantity == 95.0
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=0.0,
                                  accepted=False, errors=["symbol not tradeable"])}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=1_000_000.0)
    row = out.rows[0]
    assert row.skipped is True
    assert row.side is None
    assert row.delta_quantity == 0.0
    assert row.estimated_value == 0.0
    assert row.bp_cost == 0.0
    assert row.target_quantity == 5.0            # post-trade == held, nothing happens
    assert row.to_dict()["side"] is None
    assert "symbol not tradeable" in row.reasons


# --- 4: the delta is what the broker receives, so the delta is what must fit ---

def test_market_mode_rounds_the_delta_onto_the_brokers_increment():
    """An on-grid TARGET minus an off-grid HOLDING is off-grid. 5.0 - 3.3333 is
    1.6667, which a 0.25-increment broker cannot fill; both modes must say 1.5."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True,
                                min_trade_increment=0.25)}
    current = {"XXX": _pos("XXX", 100.0, quantity=3.3333, cost_basis=333.33)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        row = pa.compute_allocation(500.0, 1_000_000.0, labels, current, margin,
                                    allow_fractional=True, default_bp_factor=1.0,
                                    valuation_mode=mode).rows[0]
        assert row.delta_quantity == pytest.approx(1.5), mode
        assert row.delta_quantity / 0.25 == pytest.approx(round(row.delta_quantity / 0.25))


# --- 5: the plan records the mode every one of its numbers is measured in ------

def test_the_plan_records_its_valuation_mode():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, {},
                                     allow_fractional=False, default_bp_factor=1.0,
                                     valuation_mode=mode)
        assert plan.valuation_mode == mode
        assert json.loads(json.dumps(plan.to_dict()))["valuation_mode"] == mode
        out = pa.apply_order_impacts(plan, {}, available_buying_power=1_000_000.0)
        assert out.valuation_mode == mode          # survives the precheck re-solve


def test_an_invest_label_plan_records_the_budget_as_its_base_notional():
    """base_notional carries two meanings: the allocatable base in a REBALANCE and
    the BUDGET in an INVEST_LABEL run. Documented on AllocationPlan."""
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])
    plan = pa.compute_label_investment(label, 7_500.0, {"AAA": _pos("AAA", 100.0)}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0)
    assert plan.base_notional == 7_500.0


# --- 6 and 7: say the right thing about why nothing is being traded ------------

def test_a_buy_scaled_below_min_order_size_says_which_rule_stopped_it():
    """Scaling and the minimum order size are different causes with different
    fixes; the row used to report only the scaling."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    plan = pa.compute_allocation(10_000.0, 200.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, margin,
                                 allow_fractional=False, default_bp_factor=1.0)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.skipped is True
    assert pa.REASON_BELOW_MIN_ORDER_FMT.format(size=5.0) in row.reasons


def test_a_suppressed_row_never_claims_the_position_is_held_and_closed_at_once():
    """A close too small to send is honest about both facts without contradicting
    itself: we want out, and no order can be sent."""
    labels = [LabelTarget("EXIT", 0.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    row = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 100.0, quantity=3.0, cost_basis=300.0)},
                                margin, allow_fractional=False,
                                default_bp_factor=1.0).rows[0]
    assert row.delta_quantity == 0.0
    assert row.target_quantity == 3.0
    assert "held" not in " ".join(row.reasons)


# --- 8: the reason renders inside a label panel, so it must not name it --------

def test_the_multi_label_reason_does_not_name_the_panel_it_renders_in():
    labels = [LabelTarget("ARK26", 50.0, [SymbolTarget("XXX", 100.0)]),
              LabelTarget("HighRisk", 50.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert "⚠ in ARK26, HighRisk" in plan.rows[0].reasons
    assert not any("also in" in r for r in plan.rows[0].reasons)


# --- 10: the precheck's own advisory output reaches the row -------------------

def test_the_row_carries_the_prechecks_fees_and_warnings():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-10_000.0,
                                  estimated_fees=1.37,
                                  warnings=["extended hours pricing"])}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=1_000_000.0)
    assert out.rows[0].estimated_fees == 1.37
    assert "extended hours pricing" in out.rows[0].reasons
    assert out.rows[0].to_dict()["estimated_fees"] == 1.37


# --- 11, 12, 13: small correctness --------------------------------------------

def test_an_empty_label_at_zero_percent_warns_about_nothing():
    """A 0% empty label allocates nothing and there is nothing to tell the user."""
    labels = [LabelTarget("FULL", 100.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 0.0, [])]
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0)
    assert plan.warnings == []
    assert plan.unallocatable_pct == 0.0


def test_money_and_quantity_epsilons_are_separate_constants():
    """One constant for two units meant tightening a share tolerance would silently
    move a money one. A residual of 1e-7 dollars is not worth a ledger write."""
    assert pa.MONEY_EPSILON != pa.QUANTITY_EPSILON
    assert pa.consume_income_events([(1, 100.0)], 1e-7) == []
    assert pa.consume_income_events([(1, 100.0)], 50.0) == [(1, 50.0)]


def test_the_module_declares_its_public_api():
    assert "compute_allocation" in pa.__all__
    assert "current_value" in pa.__all__
    assert "PositionFetchFailed" in pa.__all__
    missing = [n for n in pa.__all__ if not hasattr(pa, n)]
    assert missing == []


# --- closed test gaps ---------------------------------------------------------

def test_compute_base_notional_guards_the_mode_on_an_empty_managed_list():
    """The guard is only OBSERVABLE here: with symbols, current_value catches it."""
    with pytest.raises(ValueError):
        pa.compute_base_notional(5_000.0, {}, [], valuation_mode="marketish")


def test_plan_sell_rows_sorted_descending_by_estimated_value():
    """Sells go out BEFORE buys and biggest first, so the cash lands early."""
    small = AllocationRow(symbol="S", side=OrderDirection.SELL, estimated_value=10.0)
    big = AllocationRow(symbol="B", side=OrderDirection.SELL, estimated_value=99.0)
    mid = AllocationRow(symbol="M", side=OrderDirection.SELL, estimated_value=50.0)
    assert [r.symbol for r in AllocationPlan(rows=[small, big, mid]).sell_rows] == \
        ["B", "M", "S"]


def test_decision_2_margin_changes_the_cost_of_a_target_not_its_size():
    """Targets are NOTIONAL, not buying power. A non-marginable symbol consumes
    twice the buying power for the same position, and gets the same share count."""
    labels = [LabelTarget("ONE", 100.0, [SymbolTarget("MARG", 50.0),
                                         SymbolTarget("NONM", 50.0)])]
    current = {"MARG": _pos("MARG", 100.0), "NONM": _pos("NONM", 100.0)}
    margin = {"MARG": MarginInfo(symbol="MARG", bp_factor=1.0, marginable=True),
              "NONM": MarginInfo(symbol="NONM", bp_factor=2.0, marginable=False)}
    plan = pa.compute_allocation(100_000.0, 10_000_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=2.0)
    by = {r.symbol: r for r in plan.rows}
    assert plan.scale_factor == 1.0
    assert by["MARG"].delta_quantity == by["NONM"].delta_quantity == 500.0
    assert by["NONM"].bp_cost == 2.0 * by["MARG"].bp_cost


def test_neither_mode_ever_sells_more_than_is_held():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=30.0, cost_basis=3_000.0)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        for base in (0.0, 1.0, 100.0, 1_000.0, 2_999.0):
            row = pa.compute_allocation(base, 1_000_000.0, labels, current, {},
                                        allow_fractional=False, default_bp_factor=1.0,
                                        valuation_mode=mode).rows[0]
            assert row.delta_quantity >= -30.0, (mode, base)
            assert row.target_quantity >= 0.0, (mode, base)


def test_a_pre_existing_short_is_bought_back_never_extended():
    """Targets are long-only. A short holding is covered towards the target, and a
    ZERO target buys it back to flat rather than selling more."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=-50.0, cost_basis=-5_000.0)}
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        row = pa.compute_allocation(5_000.0, 1_000_000.0, labels, current, {},
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode=mode).rows[0]
        assert row.delta_quantity == 100.0, mode
        assert row.target_quantity == 50.0, mode
        flat = pa.compute_allocation(0.0, 1_000_000.0, labels, current, {},
                                     allow_fractional=False, default_bp_factor=1.0,
                                     valuation_mode=mode).rows[0]
        assert flat.delta_quantity == 50.0, mode
        assert flat.target_quantity == 0.0, mode


def test_position_market_value_is_display_only_and_never_sizes_a_plan():
    """The engine measures market value as quantity x price so the base, the
    percentages and the deltas share one price. A broker market_value stamped at a
    stale price must not leak into any of them."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    sane = PositionState(symbol="XXX", quantity=10.0, cost_basis=900.0, price=200.0)
    absurd = PositionState(symbol="XXX", quantity=10.0, cost_basis=900.0, price=200.0,
                           market_value=999_999_999.0)
    for mode in (pa.VALUATION_MODE_COST, pa.VALUATION_MODE_MARKET):
        assert (pa.compute_base_notional(5_000.0, {"XXX": sane}, ["XXX"], valuation_mode=mode)
                == pa.compute_base_notional(5_000.0, {"XXX": absurd}, ["XXX"],
                                            valuation_mode=mode))
        a = pa.compute_allocation(4_000.0, 1_000_000.0, labels, {"XXX": sane}, {},
                                  allow_fractional=False, default_bp_factor=1.0,
                                  valuation_mode=mode)
        b = pa.compute_allocation(4_000.0, 1_000_000.0, labels, {"XXX": absurd}, {},
                                  allow_fractional=False, default_bp_factor=1.0,
                                  valuation_mode=mode)
        assert a.to_dict() == b.to_dict(), mode
