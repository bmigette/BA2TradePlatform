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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.total_buy_value == 9_000.0


def test_unknown_margin_uses_default_bp_factor():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=2.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows[0].bp_factor == 2.0
    assert plan.rows[0].bp_cost == 20_000.0


def test_held_symbol_with_no_managed_label_is_absent_from_the_plan():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    current = {"AAA": _pos("AAA", 100.0),
               "ZZZ": _pos("ZZZ", 100.0, quantity=50.0, cost_basis=5_000.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert [r.symbol for r in plan.rows] == ["AAA"]


def test_fractional_off_floors_the_quantity():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows[0].target_quantity == 3.0
    assert plan.rows[0].estimated_value == 900.0


def test_round_quantity_returns_zero_for_a_non_positive_price():
    assert pa.round_quantity(1_000.0, 0.0, None, allow_fractional=False) == 0.0


def test_held_above_target_produces_a_sell_of_the_difference():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    plan = pa.compute_allocation(5_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.target_quantity == 50.0
    assert row.delta_quantity == -50.0
    assert row.side == OrderDirection.SELL
    assert row.estimated_value == 5_000.0
    assert row.bp_cost == 0.0
    assert plan.total_sell_value == 5_000.0


def test_held_below_target_produces_a_top_up_buy():
    """MARKET, and it MATTERS: this is the mode half of the same fixture
    ``test_cost_mode_sizes_the_top_up_off_the_purchase_value_not_the_share_count``
    uses. Holding 20 bought at 90 with the price now 100, market targets 100 SHARES
    and buys 80; cost targets 10,000 of PURCHASE VALUE and buys 82. The 80 asserted
    below is the market answer, and it used to come from the Python default."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=20.0, cost_basis=1_800.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert by["BBB"].target_quantity == 0.0
    assert by["BBB"].delta_quantity == -30.0
    assert by["BBB"].side == OrderDirection.SELL
    assert pa.REASON_CLOSE_TO_ZERO in by["BBB"].reasons


def test_already_on_target_produces_no_order():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert plan.buy_rows == []
    assert plan.sell_rows == []


def test_whole_share_mode_never_emits_a_fractional_delta():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.5, cost_basis=1_050.0)}
    plan = pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows[0].delta_quantity == 9.0


def test_fractional_on_without_increment_rounds_to_four_decimals():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=True)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows[0].target_quantity == pytest.approx(3.33)


def test_fractional_requested_on_a_non_fractionable_symbol_falls_back_to_whole_shares():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 300.0)}, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=2.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert by["SELLME"].delta_quantity == -500.0
    assert by["SELLME"].estimated_value == 50_000.0
    assert not any("scaled" in r for r in by["SELLME"].reasons)
    assert plan.total_sell_value == 50_000.0
    # ...and what the sell FREES funds the buy. This used to read 10 shares: the
    # scaler measured the 50,000 buy against the 1,000 of published buying power
    # and cut it by 98%, on a plan whose own sell hands back 50,000 before the buy
    # is ever sent. See test_portfolio_allocation_sell_releases_bp.py.
    assert by["SELLME"].bp_released == 50_000.0
    assert by["BUYME"].delta_quantity == 500.0
    assert plan.scale_factor == 1.0
    assert plan.total_buying_power == 51_000.0


def test_zero_buying_power_skips_every_buy():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 0.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows[0].skipped is True
    assert plan.total_buy_value == 0.0


def test_symbol_without_a_price_is_skipped_not_guessed():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0)])]
    current = {"AAA": _pos("AAA", None), "BBB": _pos("BBB", 50.0)}
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows[0].skipped is True
    assert pa.REASON_NO_PRICE in plan.rows[0].reasons


def test_empty_managed_label_contributes_to_unallocatable_pct():
    labels = [LabelTarget("FULL", 70.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 30.0, [])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"AAA": _pos("AAA", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert [r.symbol for r in plan.rows] == ["AAA"]
    assert plan.rows[0].target_quantity == 70.0
    assert plan.unallocatable_pct == pytest.approx(30.0)
    # No reserve, so the weight and the share of base are the same 30 -- the case
    # that hid the two-denominator defect for as long as the reserve was always 0.
    assert ("label 'EMPTY' has no symbols - its 30.00% weight (30.00% of the base) "
            "can absorb nothing") in plan.warnings


def test_negative_label_target_is_clamped_to_zero():
    labels = [LabelTarget("A", -20.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    plan = pa.compute_allocation(10_000.0, 0.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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


def test_a_total_above_one_hundred_is_still_a_hard_ERROR():
    """The only rule left, and it has to stay hard: the plan cannot buy money the
    account does not have, and the buying-power scaler would silently shrink every
    row instead."""
    labels = [LabelTarget("A", 70.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 48.0, [SymbolTarget("BBB", 100.0)])]
    messages = pa.validate_label_targets(labels)

    assert messages == [pa.ERROR_LABEL_TOTAL_FMT.format(total=118.0, over=18.0)]
    assert messages == ["label targets total 118.00% - over 100% by 18.00%"]
    assert pa.blocking_messages(messages) == messages


def test_symbol_weights_inside_a_label_must_STILL_total_exactly_one_hundred():
    """The reserve is a LABEL-level idea only. A label whose symbol weights total 60
    leaves 40% of THAT label's money undeployed with nothing on the plan to record
    it -- ``compute_allocation`` multiplies the weights straight through -- so this
    rule does not relax with the other one."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0)])]
    messages = pa.validate_label_targets(labels)

    assert messages == [pa.ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=60.0)]
    assert pa.blocking_messages(messages) == messages


def test_an_under_allocated_set_still_reports_its_real_errors():
    """The total rule must not become an excuse to stop checking: a duplicate
    label and a non-zero label with no symbols are still blocking underneath it."""
    labels = [LabelTarget("A", 30.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("A", 20.0, [SymbolTarget("BBB", 100.0)]),
              LabelTarget("B", 10.0)]
    messages = pa.validate_label_targets(labels)

    blocking = pa.blocking_messages(messages)
    assert pa.ERROR_LABEL_UNDER_FMT.format(total=60.0, under=40.0) in blocking
    assert pa.ERROR_LABEL_DUPLICATE_FMT.format(label="A") in blocking
    assert pa.ERROR_LABEL_NO_SYMBOLS_FMT.format(label="B", pct=10.0) in blocking


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
    assert pa.compute_base_notional(5_000.0, current, ["AAA"],
                                    valuation_mode=pa.VALUATION_MODE_COST) == 5_900.0


def test_compute_base_notional_managed_symbol_with_no_position_contributes_zero():
    """COST mode: 10,000 buying power + AAA's 1,500 basis. NVDA is managed but not
    held, so it adds nothing (market mode would make AAA 1,000 and the total 11,000
    -- the arithmetic pinned here is the cost one)."""
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=1500.0)}
    assert pa.compute_base_notional(10_000.0, current, ["AAA", "NVDA"],
                                    valuation_mode=pa.VALUATION_MODE_COST) == 11_500.0


def test_compute_base_notional_counts_a_repeated_symbol_once():
    """COST again: 5,000 + one 900 basis, not two (market would be 5,000 + 1,000)."""
    current = {"AAA": _pos("AAA", 100.0, quantity=10.0, cost_basis=900.0)}
    assert pa.compute_base_notional(5_000.0, current, ["AAA", "AAA"],
                                    valuation_mode=pa.VALUATION_MODE_COST) == 5_900.0


def test_compute_base_notional_raises_when_buying_power_is_none():
    """A VALID mode is passed so the raise is unambiguously about the buying power:
    the None-balance check runs first, but an invalid mode would also raise ValueError
    and the test would pass for the wrong reason."""
    with pytest.raises(ValueError):
        pa.compute_base_notional(None, {}, ["AAA"], valuation_mode=pa.VALUATION_MODE_COST)


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
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.scale_factor == pytest.approx(0.25)
    assert plan.rows[0].delta_quantity == 25.0


def test_label_investment_on_an_empty_label_allocates_nothing():
    plan = pa.compute_label_investment(LabelTarget("EMPTY", 100.0, []), 10_000.0, {}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    assert [r.symbol for r in plan.rows] == ["AAA"]
    assert plan.rows[0].delta_quantity == 100.0
    assert plan.total_buy_value == pytest.approx(10_000.0)


def test_label_investment_coalescing_preserves_first_appearance_order():
    label = LabelTarget("L", 100.0, [SymbolTarget("BBB", 25.0), SymbolTarget("AAA", 50.0),
                                     SymbolTarget("BBB", 25.0)])
    plan = pa.compute_label_investment(label, 10_000.0,
                                       {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 100.0)}, {},
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    assert [r.symbol for r in plan.rows] == ["BBB", "AAA"]
    assert [r.delta_quantity for r in plan.rows] == [50.0, 50.0]


def test_apply_order_impacts_replaces_the_estimated_bp_cost():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-20_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=10_000.0)
    assert out.scale_factor == pytest.approx(0.5)
    assert out.rows[0].delta_quantity == 50.0
    assert out.rows[0].bp_cost == pytest.approx(10_000.0)


def test_apply_order_impacts_skips_a_rejected_order():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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


def test_consume_income_events_steps_over_an_event_with_nothing_left_in_it():
    """An event already spent to the last cent (or one the store handed over with a
    NULL open amount) contributes nothing and produces no entry -- taking from it
    would invent money, and writing a zero entry would list it in the run's
    ``income_consumed_events`` as though it had funded the run."""
    out = pa.consume_income_events([(1, 0.0), (2, None), (3, 250.0)], 300.0)
    assert out == [(3, 250.0)]


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


def test_all_three_entry_points_require_an_explicit_valuation_mode():
    """CONTRACT CHANGED: there is no Python default on ANY of the three.

    They used to have defaults that DISAGREED -- ``compute_base_notional`` fell back
    to cost (its pinned behaviour, "buying power + cost basis") and the two solvers
    to market (theirs, "shares vs shares") -- and this test pinned exactly that.
    But the mode picks the meaning of "current value" for the allocatable base, the
    displayed percentages and every delta AT ONCE, so a call site that forgot the
    keyword got a cost base and market deltas: no exception, no warning, just wrong
    money. Since no single default is right for every caller, none of them has one
    and the omission is a TypeError at the call site instead.
    """
    current = {"AAA": _pos("AAA", 200.0, quantity=10.0, cost_basis=900.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    with pytest.raises(TypeError):
        pa.compute_base_notional(0.0, current, ["AAA"])
    with pytest.raises(TypeError):
        pa.compute_allocation(2_000.0, 1_000_000.0, labels, current, {},
                              allow_fractional=False, default_bp_factor=1.0)
    with pytest.raises(TypeError):
        pa.compute_label_investment(labels[0], 1_000.0, current, {},
                                    available_buying_power=1_000_000.0,
                                    allow_fractional=False, default_bp_factor=1.0)


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
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
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
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
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
                                       allow_fractional=True, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.scale_factor == 1.0
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=-20_000.0)}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=10_000.0)
    assert out.scale_factor == pytest.approx(0.5)
    scaled = [r for r in out.rows[0].reasons if r.startswith("scaled ")]
    assert scaled == ["scaled ×0.50 to fit buying power"]


# --- CRITICAL 2: a missing base must never be read as "hold nothing" -----------
#
# The three guards below all pass a VALID valuation_mode on purpose. The mode check
# runs FIRST inside compute_allocation and also raises ValueError, so an omitted or
# typo'd mode would make every one of them pass without ever reaching the money
# guard they exist to pin.

def test_compute_allocation_refuses_a_none_base_notional():
    """`float(None or 0.0)` used to make this a zero base -- i.e. a target of 0 for
    every managed symbol, i.e. LIQUIDATE THE PORTFOLIO. That is the accident
    PositionFetchFailed exists to prevent, arriving through a different door."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    current = {"XXX": _pos("XXX", 100.0, quantity=100.0, cost_basis=10_000.0)}
    with pytest.raises(ValueError):
        pa.compute_allocation(None, 1_000_000.0, labels, current, {},
                              allow_fractional=False, default_bp_factor=1.0,
                              valuation_mode=pa.VALUATION_MODE_MARKET)


def test_compute_allocation_refuses_a_negative_base_notional():
    """A margin call can drive a computed base negative; flattening the whole
    managed portfolio is not the right response to arithmetic."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    with pytest.raises(ValueError):
        pa.compute_allocation(-1.0, 1_000_000.0, labels, {"XXX": _pos("XXX", 100.0)}, {},
                              allow_fractional=False, default_bp_factor=1.0,
                              valuation_mode=pa.VALUATION_MODE_MARKET)


def test_compute_allocation_refuses_a_none_buying_power():
    """No fallback values for balances -- the same rule compute_base_notional applies."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    with pytest.raises(ValueError):
        pa.compute_allocation(10_000.0, None, labels, {"XXX": _pos("XXX", 100.0)}, {},
                              allow_fractional=False, default_bp_factor=1.0,
                              valuation_mode=pa.VALUATION_MODE_MARKET)


def test_compute_allocation_accepts_a_zero_base():
    """Zero is a real answer (a flat, empty account); only None and negative are not."""
    plan = pa.compute_allocation(0.0, 0.0, [], {}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.rows == []


def test_compute_label_investment_refuses_a_none_amount_or_buying_power():
    """Valid mode again, for the same reason: the mode check runs before both money
    guards and raises the same exception type."""
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])
    with pytest.raises(ValueError):
        pa.compute_label_investment(label, None, {}, {}, available_buying_power=1_000.0,
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode=pa.VALUATION_MODE_MARKET)
    with pytest.raises(ValueError):
        pa.compute_label_investment(label, 1_000.0, {}, {}, available_buying_power=None,
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode=pa.VALUATION_MODE_MARKET)


# --- 3: one shape for "no order", including a precheck rejection ---------------

def test_a_precheck_rejection_is_zeroed_like_every_other_no_order():
    """A refused order kept side=BUY, delta=100 and estimated_value=10,000 while
    claiming skipped=True, so Section G's dry-run would render it as a live BUY."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0, quantity=5.0, cost_basis=500.0)},
                                 {}, allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.base_notional == 7_500.0


# --- 6 and 7: say the right thing about why nothing is being traded ------------

def test_a_buy_scaled_below_min_order_size_says_which_rule_stopped_it():
    """Scaling and the minimum order size are different causes with different
    fixes; the row used to report only the scaling."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    plan = pa.compute_allocation(10_000.0, 200.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.target_quantity == 3.0
    assert "held" not in " ".join(row.reasons)


# --- 8: the reason renders inside a label panel, so it must not name it --------

def test_the_multi_label_reason_does_not_name_the_panel_it_renders_in():
    labels = [LabelTarget("ARK26", 50.0, [SymbolTarget("XXX", 100.0)]),
              LabelTarget("HighRisk", 50.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert "⚠ in ARK26, HighRisk" in plan.rows[0].reasons
    assert not any("also in" in r for r in plan.rows[0].reasons)


# --- 10: the precheck's own advisory output reaches the row -------------------

def test_the_row_carries_the_prechecks_fees_and_warnings():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                 {"XXX": _pos("XXX", 100.0)}, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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
                                 allow_fractional=False, default_bp_factor=2.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
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


# ---------------------------------------------------------------------------
# MarginInfo.min_fractional_notional -- the broker's $5 fractional money floor.
#
# TastyTrade, live dry-run 2026-08-21, HTTP 422:
#     below_notional_value_minimum: Fractional equities orders cannot have a
#     notional value less than $5.
# The engine is PURE and never calls a broker, so the rule reaches it only as data
# on MarginInfo -- the same way `fractionable` and `min_trade_increment` do. Every
# number below is the real case: SCHD at ~$34 on a 5-decimal quantity grid.
# ---------------------------------------------------------------------------

def _schd_margin(min_fractional_notional=5.0):
    return {"SCHD": MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                               min_trade_increment=1e-5,
                               min_fractional_notional=min_fractional_notional)}


def test_a_fractional_buy_below_the_brokers_notional_floor_is_suppressed():
    """The exact order the broker refused: ~0.057 shares of SCHD, under $2.

    Still not sent -- but D1 now owns this case and prices it: nothing that small
    can be sent under the $5 floor, and the CHEAPEST order that clears it (0.14706
    shares, $5.00) is 256% of a $1.95 target, outside the 1.5x bump guard. The row
    quotes the cheapest legal order, not one whole share ($34, 1744%): the number
    the user is being refused has to be the one the engine actually considered.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    plan = pa.compute_allocation(1.95, 1_000_000.0, labels,
                                 {"SCHD": _pos("SCHD", 34.0)}, _schd_margin(),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.estimated_value == 0.0
    assert row.target_notional == 1.95          # the TARGET is never rewritten
    assert row.sizing_outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert row.unmet_notional == pytest.approx(1.95)
    reason = " ".join(row.reasons)
    assert "$5 fractional minimum" in reason
    assert "256% of target" in reason


def test_a_fractional_buy_above_the_notional_floor_still_trades():
    """The guard must be narrow: a normal fractional order is the common case on
    this account, which holds 18 of its 25 positions fractionally."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    plan = pa.compute_allocation(100.0, 1_000_000.0, labels,
                                 {"SCHD": _pos("SCHD", 34.0)}, _schd_margin(),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == pytest.approx(2.94117)
    assert row.side == OrderDirection.BUY
    assert not any("below the broker" in r for r in row.reasons)


def test_the_notional_floor_keys_on_the_quantity_not_on_the_symbol_flag():
    """The broker's rule is worded "FRACTIONAL equities orders ...". A WHOLE-share
    order is exempt even in a fractionable symbol, so a legal 1-share buy of a $3
    stock must not be refused for being worth less than $5."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    plan = pa.compute_allocation(3.0, 1_000_000.0, labels,
                                 {"SCHD": _pos("SCHD", 3.0)}, _schd_margin(),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 1.0
    assert row.side == OrderDirection.BUY
    assert row.estimated_value == 3.0


def test_a_fractional_trim_below_the_notional_floor_is_suppressed_too():
    """Suppression is on the MAGNITUDE, like min_order_size: the broker refuses an
    unsendable SELL for exactly the same reason it refuses an unsendable buy."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    current = {"SCHD": _pos("SCHD", 34.0, quantity=1.0, cost_basis=34.0)}
    plan = pa.compute_allocation(32.90, 1_000_000.0, labels, current, _schd_margin(),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.target_quantity == 1.0           # the position is HELD, not rewritten
    assert any("below the broker" in r for r in row.reasons)


def test_a_fractional_position_too_small_to_close_says_so_instead_of_going_quiet():
    """A 0.05715-share holding is worth under $2, so the broker will not take the
    closing order either. The row must carry BOTH "close position" and the reason
    that stopped it -- a close that silently does not happen is the worst shape."""
    labels = [LabelTarget("A", 0.0, [SymbolTarget("SCHD", 100.0)])]
    current = {"SCHD": _pos("SCHD", 34.0, quantity=0.05715, cost_basis=1.94)}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, _schd_margin(),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert pa.REASON_CLOSE_TO_ZERO in row.reasons
    assert any("below the broker" in r for r in row.reasons)


def test_no_notional_floor_is_applied_when_the_broker_published_none():
    """This is broker DATA, never a rule the engine invents. Alpaca publishes no
    such floor, and a hardcoded $5 would silently suppress legal Alpaca orders."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    plan = pa.compute_allocation(1.95, 1_000_000.0, labels,
                                 {"SCHD": _pos("SCHD", 34.0)},
                                 _schd_margin(min_fractional_notional=None),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == pytest.approx(0.05735)
    assert row.side == OrderDirection.BUY


def test_label_investment_applies_the_fractional_notional_floor_too():
    """The same rule on the INVEST_LABEL path, priced the same way by D1."""
    label = LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])
    plan = pa.compute_label_investment(label, 1.95, {"SCHD": _pos("SCHD", 34.0)},
                                       _schd_margin(),
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=True, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert plan.total_buy_value == 0.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert any("$5 fractional minimum" in r for r in row.reasons)


def test_the_notional_floor_reason_is_exported_so_the_ui_cannot_drift():
    assert "REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT" in pa.__all__


def test_a_buy_scaled_under_the_notional_floor_says_which_rule_stopped_it():
    """Scaling a buy down to fit buying power is exactly what pushes a fractional
    order under the $5 floor, and the two causes have different fixes (add buying
    power vs. nothing the user can do), so the row must name both."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    plan = pa.compute_allocation(100.0, 2.0, labels, {"SCHD": _pos("SCHD", 34.0)},
                                 _schd_margin(), allow_fractional=True,
                                 default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.skipped is True
    assert row.side is None
    assert any(r.startswith(pa.REASON_SCALED_PREFIX) for r in row.reasons)
    assert any("below the broker" in r for r in row.reasons)


# ---------------------------------------------------------------------------
# D1: a target under one tradeable unit is bumped to one unit inside the bound,
# and skipped-with-its-price outside it. Never silently dropped.
# ---------------------------------------------------------------------------

def test_one_share_inside_the_bound_is_bumped_and_says_so():
    """$200 of a $300 share is 0.6667 shares. One share is 150% of target, which is
    exactly the bound, so the symbol gets its position."""
    qty, outcome, reason = pa.size_sub_unit_target(
        200.0, 300.0, MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False),
        allow_fractional=True)
    assert qty == 1.0
    assert outcome == pa.SIZING_OUTCOME_BUMPED
    assert "BUMPED UP" in reason
    assert "150% of target" in reason


def test_the_bump_bound_is_inclusive_at_exactly_the_multiple():
    """The boundary itself bumps. 1.5 x 200 == 300 exactly in binary, so this is a
    real equality test, not a tolerance one."""
    margin = MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)
    at_bound = 200.0 * pa.BUMP_TO_ONE_SHARE_MAX_MULTIPLE
    assert pa.size_sub_unit_target(200.0, at_bound, margin,
                                   allow_fractional=True)[1] == pa.SIZING_OUTCOME_BUMPED
    just_over = at_bound + 1.0
    assert pa.size_sub_unit_target(200.0, just_over, margin,
                                   allow_fractional=True)[1] == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE


def test_one_share_outside_the_bound_is_refused_and_quotes_the_limit():
    """$50 of a $500 share is a 10x overspend. Skipped, with the arithmetic."""
    qty, outcome, reason = pa.size_sub_unit_target(
        50.0, 500.0, MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False),
        allow_fractional=True)
    assert qty == 0.0
    assert outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert "1000% of target" in reason
    assert "200% bump limit" in reason
    assert not reason.startswith("below")
    assert "held" not in reason


def test_a_bump_the_broker_minimum_order_size_forbids_is_not_attempted():
    """One share is inside the bound but under the broker's minimum ORDER size, so
    there is no order to place. That is neither a bump nor a bound problem."""
    qty, outcome, reason = pa.size_sub_unit_target(
        200.0, 300.0,
        MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False, min_order_size=5.0),
        allow_fractional=True)
    assert qty == 0.0
    assert outcome == pa.SIZING_OUTCOME_NORMAL
    assert "minimum order size 5" in reason


def test_the_tradeable_unit_is_one_share_unless_the_broker_publishes_a_step():
    assert pa.tradeable_unit(None, allow_fractional=True) == 1.0
    assert pa.tradeable_unit(MarginInfo(symbol="X", bp_factor=1.0, fractionable=False),
                             allow_fractional=True) == 1.0
    assert pa.tradeable_unit(MarginInfo(symbol="X", bp_factor=1.0, fractionable=True),
                             allow_fractional=True) == pytest.approx(0.0001)
    assert pa.tradeable_unit(MarginInfo(symbol="X", bp_factor=1.0, fractionable=True,
                                        min_trade_increment=0.25),
                             allow_fractional=True) == 0.25
    # Toggle OFF: the grid is whole shares no matter what the broker publishes.
    assert pa.tradeable_unit(MarginInfo(symbol="X", bp_factor=1.0, fractionable=True,
                                        min_trade_increment=0.25),
                             allow_fractional=False) == 1.0


def test_the_five_dollar_fractional_floor_escalates_the_bump_to_one_whole_share():
    """L2/L8b: the three thresholds are weighed together, never in sequence.

    A WHOLE share is exempt from the money floor, so when it is the CHEAPER of the
    two legal destinations it wins: one share of this $4 stock costs $4 against the
    $5.00 (1.25 shares) the smallest clearing fraction would cost."""
    margin = MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001, min_fractional_notional=5.0)
    qty, outcome, reason = pa.size_sub_unit_target(3.0, 4.0, margin,
                                                   allow_fractional=True)
    assert qty == 1.0
    assert outcome == pa.SIZING_OUTCOME_BUMPED
    assert "$5 fractional minimum" in reason


def test_the_money_floor_escalates_to_the_smallest_FRACTION_that_clears_it():
    """The $5 rule is a minimum NOTIONAL, not a ban on fractions: a BIGGER fraction
    clears it, and it is usually far cheaper than a whole share.

    A $4 slice of a $34 ETF on a 5-decimal grid: 0.14706 shares is $5.00004, legal
    and 125% of target -- inside the bump bound. Escalating straight to one whole
    share asks for $34, 850% of target, which the bound then refuses, so the symbol
    gets NOTHING when a legal order was sitting there.
    """
    margin = MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001, min_fractional_notional=5.0)
    qty, outcome, reason = pa.size_sub_unit_target(4.0, 34.0, margin,
                                                   allow_fractional=True)
    assert qty == pytest.approx(0.14706)
    assert qty * 34.0 >= 5.0                    # the broker will take this one
    assert outcome == pa.SIZING_OUTCOME_BUMPED
    assert "$5 fractional minimum" in reason
    assert "125% of target" in reason


def test_the_four_dollar_slice_reaches_the_plan_as_a_real_order():
    """The same case end to end -- the row that used to read "no order"."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    plan = pa.compute_allocation(4.0, 1_000_000.0, labels,
                                 {"SCHD": _pos("SCHD", 34.0)}, _schd_margin(),
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == pytest.approx(0.14706)
    assert row.side == OrderDirection.BUY
    assert row.estimated_value == pytest.approx(5.00004)
    assert row.unmet_notional == 0.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    # And it survives the suppression that refuses sub-$5 fractions.
    assert pa.REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX not in " ".join(row.reasons)


def test_a_thirty_four_dollar_share_under_the_fractional_floor_skips_with_the_real_reason():
    """A $2 slice of a $34 ETF. The cheapest order that clears the floor -- 0.14706
    shares for $5.00 -- is 250% of target, outside the 2x guard, so it SKIPS. It must
    say the FLOOR is why the small fraction was unavailable, not "rounds to zero".
    A larger target and this same case becomes a legal order (see the 125% test
    above), which is exactly why the quoted percentage has to come off the cheapest
    legal order and not off the $34 whole share (1700%).

    Was a $3 slice when the bound was 1.5x (167%, refused). At 2x that case now FILLS,
    which is the widening the rounding rule bought -- see the test below.
    """
    margin = MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001, min_fractional_notional=5.0)
    qty, outcome, reason = pa.size_sub_unit_target(2.0, 34.0, margin,
                                                   allow_fractional=True)
    assert qty == 0.0
    assert outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert "$5 fractional minimum" in reason
    assert "250% of target" in reason
    assert "rounds to 0" not in reason
    assert not reason.startswith("below")


def test_the_wider_bound_lets_a_three_dollar_slice_clear_the_fractional_floor():
    """The other side of the same move. At 1.5x a $3 target could not reach the $5
    broker minimum (167% > 150%) and the symbol got nothing; at 2x it does. The
    constant governs BOTH escalations -- one whole share, and the cheapest fraction
    that clears a notional floor -- and widening it widens both."""
    margin = MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001, min_fractional_notional=5.0)
    qty, outcome, reason = pa.size_sub_unit_target(3.0, 34.0, margin,
                                                   allow_fractional=True)

    assert qty == pytest.approx(0.14706)
    assert outcome == pa.SIZING_OUTCOME_BUMPED
    assert "167% of target" in reason


def test_the_engine_weighs_the_fractional_floor_before_it_suppresses_the_row():
    """L8b's ORDERING constraint, end to end: the $5 suppression must not get to
    the row first and leave the bump nothing to decide about."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])]
    margin = {"SCHD": MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                                 min_trade_increment=0.00001,
                                 min_fractional_notional=5.0)}
    # $2, not $3: at the 2x bound a $3 target CLEARS the $5 floor (167%) and this
    # test needs a row the bound still refuses, so that "the floor was weighed before
    # the suppression" is what it is actually measuring.
    row = pa.compute_allocation(2.0, 1_000_000.0, labels,
                                {"SCHD": _pos("SCHD", 34.0)}, margin,
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert row.unmet_notional == pytest.approx(2.0)
    reason = " ".join(row.reasons)
    assert "$5 fractional minimum" in reason
    assert pa.REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX not in reason


def test_a_sub_one_share_buy_inside_the_bound_ends_up_holding_one_share():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    row = pa.compute_allocation(200.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 300.0)}, margin,
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 1.0
    assert row.side == OrderDirection.BUY
    assert row.target_quantity == 1.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    assert row.unmet_notional == 0.0          # money MOVED -- more than asked, not less
    assert row.estimated_value == pytest.approx(300.0)


def test_a_sub_one_share_buy_outside_the_bound_records_the_unmet_target():
    """$260k of a $650k share is 0.4 shares and one share is 250% of target: no
    order, and the row carries the $260k it failed to deploy."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("BRKA", 100.0)])]
    margin = {"BRKA": MarginInfo(symbol="BRKA", bp_factor=1.0, fractionable=False)}
    row = pa.compute_allocation(260_000.0, 1_000_000.0, labels,
                                {"BRKA": _pos("BRKA", 650_000.0)}, margin,
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.sizing_outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert row.unmet_notional == pytest.approx(260_000.0)
    reason = " ".join(row.reasons)
    assert "buys 0.4000 shares" in reason
    assert "250% of target" in reason
    # Pinned by the landed suite: no "held", nothing starting with "below".
    assert "held" not in reason
    assert not any(r.startswith("below") for r in row.reasons)


def test_a_trim_that_rounds_to_zero_is_never_bumped():
    """Holding 10.4, want 10 -> -0.4 shares on a whole-share grid. The position
    already exists; bumping would sell a whole share nobody asked to sell."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    current = {"XXX": _pos("XXX", 100.0, quantity=10.4, cost_basis=1_040.0)}
    row = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_NORMAL
    assert row.unmet_notional == pytest.approx(40.0)
    assert any("rounds to 0" in r for r in row.reasons)


def test_a_row_already_on_target_records_no_unmet_notional():
    """Exactly on target: zero delta, zero unmet, and no new reason."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    row = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.unmet_notional == 0.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_NORMAL
    assert not any("rounds to 0" in r for r in row.reasons)


def test_cost_mode_bumps_a_sub_one_share_target_too():
    """The cost-basis branch converts through the same grid and takes the same
    decision: one share costs `price` of BASIS, so the bound reads the same."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    row = pa.compute_allocation(200.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 300.0)}, margin,
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_COST).rows[0]
    assert row.delta_quantity == 1.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_BUMPED


def test_cost_mode_refuses_the_bump_outside_the_bound():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("BRKA", 100.0)])]
    margin = {"BRKA": MarginInfo(symbol="BRKA", bp_factor=1.0, fractionable=False)}
    row = pa.compute_allocation(260_000.0, 1_000_000.0, labels,
                                {"BRKA": _pos("BRKA", 650_000.0)}, margin,
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_COST).rows[0]
    assert row.delta_quantity == 0.0
    assert row.unmet_notional == pytest.approx(260_000.0)


def test_an_order_suppressed_by_min_order_size_records_the_unmet_notional():
    """A real 3-share trim the broker will not accept is unmet money, not nothing."""
    labels = [LabelTarget("EXIT", 0.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    row = pa.compute_allocation(10_000.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 100.0, quantity=3.0, cost_basis=300.0)},
                                margin, allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.unmet_notional == pytest.approx(300.0)


def test_a_buy_scaled_away_by_buying_power_records_the_unmet_notional():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, min_order_size=5.0)}
    row = pa.compute_allocation(10_000.0, 200.0, labels,
                                {"XXX": _pos("XXX", 100.0)}, margin,
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.skipped is True
    assert row.unmet_notional == pytest.approx(10_000.0)


def test_a_precheck_rejection_records_the_unmet_notional():
    plan = AllocationPlan(
        rows=[AllocationRow(symbol="XXX", price=100.0, delta_quantity=10.0,
                            side=OrderDirection.BUY, estimated_value=1_000.0,
                            bp_cost=1_000.0, bp_factor=1.0)],
        available_buying_power=50_000.0)
    impacts = {"XXX": OrderImpact(symbol="XXX", change_in_buying_power=0.0,
                                  accepted=False, errors=["symbol not tradeable"])}
    out = pa.apply_order_impacts(plan, impacts, available_buying_power=50_000.0)
    assert out.rows[0].unmet_notional == pytest.approx(1_000.0)


def test_missing_margin_info_with_fractional_on_says_eligibility_is_unknown():
    """No MarginInfo row at all is a DATA GAP, not "the broker said no"."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    row = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 300.0)}, {},
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.fractional is False
    assert pa.REASON_FRACTIONAL_UNKNOWN in row.reasons
    assert pa.REASON_WHOLE_SHARE_FLOOR not in row.reasons


def test_a_none_fractionable_flag_is_unknown_and_never_collapses_to_false():
    """The broker answered, but not about fractionability. Tri-state, explicitly."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=None)}
    row = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 300.0)}, margin,
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.fractional is False
    assert pa.REASON_FRACTIONAL_UNKNOWN in row.reasons
    assert pa.REASON_WHOLE_SHARE_FLOOR not in row.reasons


def test_a_non_fractionable_symbol_says_whole_share_floor_not_unknown_eligibility():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    row = pa.compute_allocation(1_000.0, 1_000_000.0, labels,
                                {"XXX": _pos("XXX", 300.0)}, margin,
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert pa.REASON_WHOLE_SHARE_FLOOR in row.reasons
    assert pa.REASON_FRACTIONAL_UNKNOWN not in row.reasons


def test_the_new_row_fields_round_trip_through_to_dict():
    row = AllocationRow(symbol="XXX", unmet_notional=1_234.5,
                        sizing_outcome=pa.SIZING_OUTCOME_BUMPED)
    blob = json.loads(json.dumps(row.to_dict()))
    assert blob["unmet_notional"] == 1_234.5
    assert blob["sizing_outcome"] == "bumped-to-1"
    assert blob["redistributed"] is False


def test_label_investment_bumps_a_sub_one_share_budget_share():
    """The budget IS the intended allocation, so a sub-unit share of it bumps even
    when the symbol is already held -- deploying nothing is the wrong answer."""
    label = LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    plan = pa.compute_label_investment(label, 200.0,
                                       {"XXX": _pos("XXX", 300.0, quantity=4.0,
                                                    cost_basis=1_200.0)}, margin,
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=True, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 1.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    assert row.target_quantity == 5.0


def test_label_investment_outside_the_bound_records_the_unmet_target():
    label = LabelTarget("A", 100.0, [SymbolTarget("BRKA", 100.0)])
    margin = {"BRKA": MarginInfo(symbol="BRKA", bp_factor=1.0, fractionable=False)}
    plan = pa.compute_label_investment(label, 260_000.0,
                                       {"BRKA": _pos("BRKA", 650_000.0)}, margin,
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=True, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 0.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert row.unmet_notional == pytest.approx(260_000.0)
    assert any("buys 0.4000 shares" in r for r in row.reasons)


def test_label_investment_weighs_the_fractional_floor_before_suppressing():
    """L8b again, on the INVEST_LABEL path: the $5 floor must not zero the row
    before D1 has weighed one whole share against the bound."""
    label = LabelTarget("A", 100.0, [SymbolTarget("SCHD", 100.0)])
    margin = {"SCHD": MarginInfo(symbol="SCHD", bp_factor=1.0, fractionable=True,
                                 min_trade_increment=0.00001,
                                 min_fractional_notional=5.0)}
    plan = pa.compute_label_investment(label, 3.0, {"SCHD": _pos("SCHD", 4.0)}, margin,
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=True, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.delta_quantity == 1.0
    assert row.sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    assert any("$5 fractional minimum" in r for r in row.reasons)


def test_a_sub_unit_TOP_UP_to_an_existing_position_is_never_bumped():
    """The mirror of the trim case, and the dangerous one: holding 10 at 100 with
    a 1,050 target wants +0.5 shares. Bumping that to a whole share buys 100 of
    stock to close a 50 gap -- an unrequested trade twice the size of the miss.
    D1 fires ONLY when the sub-unit amount is the symbol's WHOLE position."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 100.0)])]
    margin = {"XXX": MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)}
    current = {"XXX": _pos("XXX", 100.0, quantity=10.0, cost_basis=1_000.0)}
    row = pa.compute_allocation(1_050.0, 1_000_000.0, labels, current, margin,
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.sizing_outcome == pa.SIZING_OUTCOME_NORMAL
    assert row.unmet_notional == pytest.approx(50.0)
    assert any("rounds to 0" in r for r in row.reasons)


def test_the_bump_bound_survives_the_float_noise_of_a_percentage_chain():
    """What makes the bound INCLUSIVE is MONEY_EPSILON, not the comparison
    operator: a target computed through two percentage multiplications lands a few
    ulps off, and without the tolerance a symbol exactly on the bound flips to
    SKIPPED depending on the arithmetic that produced its target."""
    margin = MarginInfo(symbol="XXX", bp_factor=1.0, fractionable=False)
    target = 924.1170589305282
    # Exactly DOUBLE the target -- but reached by a different arithmetic route (a
    # percentage chain, which is how the engine gets there), landing a few ulps ABOVE
    # ``target * 2.0``. Without the tolerance this share is refused while an identical
    # one written ``target * 2.0`` is bumped.
    price = target * 54.0 / 27.0
    assert price > target * 2.0                     # the noise is real, not imagined
    assert price - target * 2.0 < 1e-9
    assert pa.size_sub_unit_target(target, price, margin,
                                   allow_fractional=True)[1] == pa.SIZING_OUTCOME_BUMPED
    # A hair of slack is all it gets: a genuinely larger share still skips.
    assert pa.size_sub_unit_target(target, price * 1.001, margin,
                                   allow_fractional=True)[1] == pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE


def test_a_legal_whole_share_order_under_five_dollars_is_not_refused_by_the_floor():
    """The floor is worded "FRACTIONAL equities orders", so 2 whole shares of a $2
    stock is a legal $4.00 order. Reading the floor without the whole-share
    exemption would bump this to ONE share and under-deploy by half."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("CHEAP", 100.0)])]
    margin = {"CHEAP": MarginInfo(symbol="CHEAP", bp_factor=1.0, fractionable=True,
                                  min_trade_increment=0.00001,
                                  min_fractional_notional=5.0)}
    row = pa.compute_allocation(4.0, 1_000_000.0, labels,
                                {"CHEAP": _pos("CHEAP", 2.0)}, margin,
                                allow_fractional=True, default_bp_factor=1.0,
                                valuation_mode=pa.VALUATION_MODE_MARKET).rows[0]
    assert row.delta_quantity == pytest.approx(2.0)
    assert row.side == OrderDirection.BUY
    assert row.sizing_outcome == pa.SIZING_OUTCOME_NORMAL
    assert row.unmet_notional == 0.0


# ---------------------------------------------------------------------------
# measure_filled_values -- FILLED money, and whether the answer is final
# ---------------------------------------------------------------------------

from ba2_common.core.portfolio_allocation import (  # noqa: E402
    SETTLED_ORDER_STATUSES, UNEXECUTED_ORDER_STATUSES,
    FilledTotals, OrderFill, measure_filled_values,
)
from ba2_common.core.types import OrderStatus  # noqa: E402


def _fill(order_id, side, status, qty=0.0, price=None):
    return OrderFill(order_id=order_id, side=side, status=status,
                     filled_quantity=qty, fill_price=price)


def test_a_filled_buy_is_worth_its_filled_quantity_times_its_fill_price():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=10.0, price=161.25)])
    assert totals.buy_value == pytest.approx(1612.5)
    assert totals.sell_value == 0.0
    assert totals.settled is True
    assert totals.net_buy_value == pytest.approx(1612.5)


def test_a_rejected_order_is_worth_nothing_and_is_settled():
    """THE bug. A rejected order used to consume income at its planned value."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.REJECTED, qty=0.0, price=None)])
    assert totals.buy_value == 0.0
    assert totals.settled is True
    assert totals.working_order_ids == []


def test_a_canceled_order_that_never_filled_is_worth_nothing():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.CANCELED, qty=0.0, price=None)])
    assert totals.buy_value == 0.0
    assert totals.settled is True


def test_an_order_that_errored_before_reaching_the_broker_is_worth_nothing():
    """AccountInterface.py:148 stamps ERROR on a hard submit failure."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.ERROR)])
    assert totals.buy_value == 0.0
    assert totals.settled is True


def test_a_washtrade_locked_order_is_settled_and_worth_nothing():
    """Our own gate: never sent, so it is as final as an order gets."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.WASHTRADE_LOCKED)])
    assert totals.settled is True
    assert totals.buy_value == 0.0


def test_done_for_day_is_settled_per_decision_d5():
    """The broker sends no further update today, so an unfilled residue never
    fills; waiting would strand this run's income overnight."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.DONE_FOR_DAY, qty=6.0, price=100.0)])
    assert totals.settled is True
    assert totals.buy_value == pytest.approx(600.0)


def test_a_partial_fill_contributes_only_the_filled_part_and_blocks_settlement():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.PARTIALLY_FILLED, qty=3.0, price=100.0)])
    assert totals.buy_value == pytest.approx(300.0)
    assert totals.settled is False
    assert totals.working_order_ids == [1]


def test_a_partial_fill_that_was_then_canceled_is_settled_at_the_filled_part():
    """Cancelled after a partial: the 3 shares are real and nothing more is coming."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.CANCELED, qty=3.0, price=100.0)])
    assert totals.buy_value == pytest.approx(300.0)
    assert totals.settled is True


def test_a_still_working_order_blocks_settlement():
    for status in (OrderStatus.PENDING, OrderStatus.NEW, OrderStatus.ACCEPTED,
                   OrderStatus.HELD, OrderStatus.WAITING_TRIGGER, OrderStatus.UNKNOWN):
        totals = measure_filled_values([_fill(1, OrderDirection.BUY, status)])
        assert totals.settled is False, status
        assert totals.working_order_ids == [1], status


def test_sells_and_buys_are_kept_apart_and_net_is_clamped_at_zero():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=10.0, price=100.0),
        _fill(2, OrderDirection.SELL, OrderStatus.FILLED, qty=5.0, price=400.0),
    ])
    assert totals.buy_value == pytest.approx(1000.0)
    assert totals.sell_value == pytest.approx(2000.0)
    assert totals.net_buy_value == 0.0
    assert totals.settled is True


def test_a_negative_broker_quantity_is_normalised_by_side_not_by_sign():
    """abs() + `side` is the canonical signed-field normalisation at this boundary,
    the same rule OrderImpact.bp_cost applies to change_in_buying_power. A broker
    that reports a sell as filled_qty=-5 must still book 5 shares SOLD."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.SELL, OrderStatus.FILLED, qty=-5.0, price=400.0)])
    assert totals.sell_value == pytest.approx(2000.0)
    assert totals.buy_value == 0.0


def test_a_fill_with_no_price_is_unmeasurable_never_estimated():
    """No fallback to the plan's quote: a priceless fill must stall the ledger, not
    be guessed at. Guessing is how the platform spends money it did not spend."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=10.0, price=None)])
    assert totals.buy_value == 0.0
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [1]


def test_a_fill_with_a_zero_price_is_unmeasurable_too():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=10.0, price=0.0)])
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [1]


def test_a_fill_with_no_side_is_unmeasurable():
    totals = measure_filled_values([
        _fill(1, None, OrderStatus.FILLED, qty=10.0, price=100.0)])
    assert totals.buy_value == 0.0
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [1]


# -- a MISSING quantity is the sibling of a missing price -------------------
#
# The income double-spend. A NULL ``filled_qty`` used to arrive here as ``0.0``
# (``float(fill.filled_quantity or 0.0)``) and be read as a MEASURED fill of zero
# shares: the run settled, consumed nothing, and took its one-shot
# ``income_consumed_at`` stamp -- leaving the income open for the NEXT run to
# spend on shares this run had already bought. Nothing in the ledger can undo
# that second deployment.


def test_a_filled_order_with_no_reported_quantity_is_unmeasurable_never_a_zero_fill():
    """THE bug report. FILLED, priced, and nobody said how many shares.

    "We were not told" is not "it filled nothing". A quantity that never arrived
    is exactly as unmeasurable as a price that never arrived
    (test_a_fill_with_no_price_is_unmeasurable_never_estimated), and takes the
    same exit: unmeasurable, unsettled, income untouched.
    """
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=None, price=160.0)])
    assert totals.buy_value == 0.0
    assert totals.net_buy_value == 0.0
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [1]


def test_a_tastytrade_shaped_zero_none_fill_on_a_filled_order_is_unmeasurable():
    """The cleanest producer. ``TastyTradeAccount._fills_summary`` answers
    ``(0.0, None)`` when the legs carry no fill records, and its refresh then does
    ``if float(db.filled_qty or 0.0) != float(broker.filled_qty or 0.0)`` -- which
    is False for a NULL row against that 0.0 -- so the quantity is never written
    while the status IS advanced to FILLED. The row that reaches the ledger is
    FILLED with a NULL quantity and no price: a contradiction, not a zero.
    """
    totals = measure_filled_values([
        _fill(7, OrderDirection.BUY, OrderStatus.FILLED, qty=None, price=None)])
    assert totals.buy_value == 0.0
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [7]


def test_an_explicit_zero_quantity_on_a_rejected_order_is_still_a_plain_skip():
    """The INVERSE defect, and the more expensive one. A refused order really did
    fill zero shares; routing its explicit 0.0 into ``unmeasurable`` would leave
    every run that had one row refused permanently unsettled, and its income
    permanently unconsumed."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.REJECTED, qty=0.0, price=None)])
    assert totals.buy_value == 0.0
    assert totals.settled is True
    assert totals.unmeasurable_order_ids == []
    assert totals.working_order_ids == []


def test_an_explicit_zero_quantity_on_a_cancelled_order_is_still_a_plain_skip():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.CANCELED, qty=0.0, price=None)])
    assert totals.settled is True
    assert totals.unmeasurable_order_ids == []


def test_a_refused_order_with_no_quantity_at_all_is_worth_zero_not_unmeasurable():
    """A status that PROVES nothing executed measures itself.

    The allocation path persists its TradingOrder with ``filled_qty`` unset
    (portfolio_allocation_service._submit_row) and the broker refusal stamps only
    the status, so a rejected row reaches the ledger as REJECTED with a NULL
    quantity -- never an explicit 0.0. Reading that NULL as "unknown" would strand
    the income of every run that had a single row refused, which is the most
    ordinary outcome there is.
    """
    for status in (OrderStatus.REJECTED, OrderStatus.ERROR,
                   OrderStatus.WASHTRADE_LOCKED):
        totals = measure_filled_values([
            _fill(1, OrderDirection.BUY, status, qty=None, price=None)])
        assert totals.settled is True, status
        assert totals.buy_value == 0.0, status
        assert totals.unmeasurable_order_ids == [], status
        assert totals.working_order_ids == [], status


def test_a_null_quantity_on_a_settled_status_that_can_carry_a_fill_still_stalls():
    """The other side of that carve-out, and the reason it is three statuses and
    not ``SETTLED_ORDER_STATUSES``. Every one of these can be reached with shares
    already traded -- a cancel after a partial fill is the ordinary case (C3) -- so
    a missing quantity on one of them is UNKNOWN, and valuing it at zero would
    consume income for a purchase that really happened."""
    for status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.STOPPED,
                   OrderStatus.REPLACED, OrderStatus.CLOSED,
                   OrderStatus.DONE_FOR_DAY, OrderStatus.FILLED):
        totals = measure_filled_values([
            _fill(1, OrderDirection.BUY, status, qty=None, price=160.0)])
        assert totals.settled is False, status
        assert totals.unmeasurable_order_ids == [1], status
        assert totals.buy_value == 0.0, status


def test_the_statuses_that_measure_themselves_are_settled_and_never_filled():
    """``UNEXECUTED_ORDER_STATUSES`` may only ever contain statuses that are
    already settled (otherwise skipping the quantity would ALSO skip the stall)
    and that cannot possibly have traded a share."""
    assert UNEXECUTED_ORDER_STATUSES <= SETTLED_ORDER_STATUSES
    assert not (UNEXECUTED_ORDER_STATUSES & OrderStatus.get_executed_statuses())
    assert OrderStatus.FILLED not in UNEXECUTED_ORDER_STATUSES
    assert OrderStatus.CANCELED not in UNEXECUTED_ORDER_STATUSES
    assert OrderStatus.DONE_FOR_DAY not in UNEXECUTED_ORDER_STATUSES


def test_a_measurable_fill_beside_a_null_quantity_keeps_its_money_and_stalls():
    """A mixed batch. The 1,600 that really moved is still counted -- losing it
    would UNDER-consume the ledger -- and the run still refuses to settle, naming
    the order nobody could measure."""
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=10.0, price=160.0),
        _fill(2, OrderDirection.BUY, OrderStatus.FILLED, qty=None, price=200.0),
    ])
    assert totals.buy_value == pytest.approx(1600.0)
    assert totals.settled is False
    assert totals.unmeasurable_order_ids == [2]
    assert totals.working_order_ids == []


def test_a_missing_quantity_and_a_missing_price_are_answered_identically():
    """Whichever half of ``quantity * price`` is absent, the answer is the same
    one. There is no asymmetry between the two fields to remember."""
    missing_price = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=10.0, price=None)])
    missing_quantity = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=None, price=10.0)])
    assert missing_quantity.to_dict() == missing_price.to_dict()


def test_an_order_fill_cannot_be_edited_after_it_is_lifted_off_the_row():
    """``frozen=True``, so nothing between the collector and the ledger can quietly
    revise a fill. The whole point of lifting plain values off the ORM row is that
    the measurement is a fact, not a mutable draft."""
    fill = OrderFill(order_id=1, side=OrderDirection.BUY, status=OrderStatus.FILLED,
                     filled_quantity=None, fill_price=160.0)
    with pytest.raises(Exception):
        fill.filled_quantity = 10.0
    assert fill.filled_quantity is None


def test_order_fill_defaults_its_quantity_to_unknown_and_not_to_zero():
    """The default is the value every producer means when it omits the field:
    "nobody told us". ``0.0`` there would be a measurement of zero shares -- the
    exact confusion that spends the same income twice."""
    assert OrderFill(order_id=1).filled_quantity is None
    assert OrderFill(order_id=1).fill_price is None


def test_a_null_quantity_on_a_still_working_order_stalls_and_is_not_valued():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.PARTIALLY_FILLED,
              qty=None, price=160.0)])
    assert totals.buy_value == 0.0
    assert totals.settled is False
    assert totals.working_order_ids == [1]
    assert totals.unmeasurable_order_ids == [1]


def test_a_missing_order_row_reads_as_status_none_and_blocks_settlement():
    """The live collector emits this for an order id whose row has vanished. It is
    a real inconsistency; stalling is the only safe answer."""
    totals = measure_filled_values([_fill(1, None, None)])
    assert totals.settled is False
    assert totals.working_order_ids == [1]


def test_measuring_no_orders_at_all_is_settled_and_worth_nothing():
    """A run whose every row was skipped. Settled, zero, consumes nothing."""
    totals = measure_filled_values([])
    assert totals == FilledTotals()
    assert totals.settled is True
    assert totals.net_buy_value == 0.0


def test_two_filled_totals_do_not_share_their_lists():
    """field(default_factory=list), not `= []`. A bare list default is a dataclass
    ValueError at import; if it somehow were not, every run would append into one
    shared list and every run would look unsettled."""
    first, second = FilledTotals(), FilledTotals()
    first.working_order_ids.append(1)
    assert second.working_order_ids == []


def test_a_sub_epsilon_filled_quantity_is_not_a_fill():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=1e-12, price=100.0)])
    assert totals.buy_value == 0.0
    assert totals.settled is True
    assert totals.unmeasurable_order_ids == []


def test_settled_statuses_cover_every_terminal_status_plus_filled():
    assert OrderStatus.get_terminal_statuses() <= SETTLED_ORDER_STATUSES
    assert OrderStatus.FILLED in SETTLED_ORDER_STATUSES
    assert OrderStatus.DONE_FOR_DAY in SETTLED_ORDER_STATUSES
    assert OrderStatus.WASHTRADE_LOCKED in SETTLED_ORDER_STATUSES
    assert OrderStatus.PARTIALLY_FILLED not in SETTLED_ORDER_STATUSES
    assert OrderStatus.UNKNOWN not in SETTLED_ORDER_STATUSES


def test_filled_totals_to_dict_is_json_safe():
    totals = measure_filled_values([
        _fill(1, OrderDirection.BUY, OrderStatus.FILLED, qty=2.0, price=50.0),
        _fill(2, OrderDirection.SELL, OrderStatus.PENDING),
    ])
    assert json.loads(json.dumps(totals.to_dict())) == {
        "buy_value": 100.0, "sell_value": 0.0, "net_buy_value": 100.0,
        "settled": False, "working_order_ids": [2], "unmeasurable_order_ids": [],
    }


def test_filled_totals_to_dict_hands_back_copies_not_its_own_lists():
    """The dict goes into the activity log's JSON blob and into the panel's copy;
    handing out the live lists would let a caller edit the run's own measurement."""
    totals = measure_filled_values([_fill(2, OrderDirection.SELL, OrderStatus.PENDING)])
    blob = totals.to_dict()
    blob["working_order_ids"].append(999)
    assert totals.working_order_ids == [2]


# ---------------------------------------------------------------------------
# unconsumed_income_notice -- the income panel's deferral copy. Pure.
# ---------------------------------------------------------------------------

from ba2_common.core.portfolio_allocation import unconsumed_income_notice  # noqa: E402


def test_no_notice_when_nothing_is_outstanding():
    assert unconsumed_income_notice(0, 0) is None


def test_the_notice_counts_orders_and_runs_and_warns():
    """The two counts must land the right way round: "3 orders from 2 runs", not
    "2 orders from 3 runs". Asserting only that both digits appear cannot tell
    the difference, and the swapped sentence is a plausible typo."""
    text, severity = unconsumed_income_notice(2, 3)

    assert "3 order(s)" in text
    assert "2 allocation run(s)" in text
    assert "not consumed" in text.lower()
    assert severity == "warning"


def test_a_run_with_no_working_orders_still_gets_its_own_sentence():
    """A run that died mid-submit has an open stamp but no working order. Saying
    "0 orders still working" would be nonsense, and saying nothing would hide it."""
    text, severity = unconsumed_income_notice(1, 0)

    assert "0 order" not in text
    assert "1" in text
    assert severity == "warning"


def test_a_negative_count_is_treated_as_nothing_outstanding():
    """<= 0, not == 0: a caller that subtracted its way to -1 must not be told
    there is a problem it cannot name."""
    assert unconsumed_income_notice(-1, 4) is None


def test_the_severity_is_a_valid_nicegui_notify_type():
    """'error' is NOT one of 'positive' | 'negative' | 'warning' | 'info'
    (settings.py gets this wrong; do not copy it)."""
    _, severity = unconsumed_income_notice(1, 1)

    assert severity in {"positive", "negative", "warning", "info"}


# ---------------------------------------------------------------------------
# W3: the deliberate reserve, recorded separately from the accidental leftover.
# ---------------------------------------------------------------------------

def test_a_fully_allocated_plan_reserves_nothing():
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)

    assert plan.reserved_pct == 0.0
    assert plan.reserved_notional == 0.0


def test_the_reserve_survives_un_ticking_a_row():
    """``filter_plan_rows`` carries it across for the same reason it carries
    ``unallocatable_pct``: the reserve is a property of the TARGETS, which un-ticking
    a row does not change. Dropping it would report a plan as fully deployed when
    the user deliberately left 30% in cash."""
    plan = AllocationPlan(rows=[AllocationRow(symbol="AAA", side=OrderDirection.BUY,
                                              estimated_value=100.0)],
                          reserved_pct=30.0, reserved_notional=3_000.0)

    assert pa.filter_plan_rows(plan, []).reserved_pct == 30.0
    assert pa.filter_plan_rows(plan, []).reserved_notional == 3_000.0


def test_the_precheck_carries_the_reserve_across_too():
    plan = AllocationPlan(rows=[], reserved_pct=30.0, reserved_notional=3_000.0)
    out = pa.apply_order_impacts(plan, {}, available_buying_power=10_000.0)
    assert (out.reserved_pct, out.reserved_notional) == (30.0, 3_000.0)


def test_the_reserve_is_recorded_in_plan_json():
    blob = AllocationPlan(reserved_pct=30.0, reserved_notional=3_000.0).to_dict()
    assert blob["reserved_pct"] == 30.0
    assert blob["reserved_notional"] == 3_000.0


def test_summarise_plan_reports_the_reserve_beside_the_estimated_cash():
    """The footer's whole job here: "est. cash after 3,100" is only meaningful next
    to "you reserved 3,000"."""
    plan = AllocationPlan(total_buy_value=7_000.0, reserved_pct=30.0,
                          reserved_notional=3_000.0)
    totals = pa.summarise_plan(plan, cash=10_000.0)
    assert totals["reserved_notional"] == 3_000.0
    assert totals["reserved_pct"] == 30.0
    assert totals["estimated_cash_after"] == pytest.approx(3_000.0)


def test_a_plan_records_the_label_targets_it_ran_with():
    """``plan_json`` is write-only today, but a stored plan that cannot say what
    percentages produced it is not reproducible. NOT the source for "load last" --
    that is the previous_* columns; this is the audit trail."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 70.0, [SymbolTarget("AAA", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    blob = json.loads(json.dumps(plan.to_dict()))

    assert blob["labels"] == [
        {"label": "A", "target_pct": 70.0,
         "symbols": [{"symbol": "AAA", "weight_pct": 100.0}]}]


def test_investable_notional_is_the_base_less_the_reserve():
    """The ONE place the reserve arithmetic lives. 10% off a 10,000 base leaves
    9,000 to divide among labels that total 100."""
    assert pa.investable_notional(10_000.0, 10.0) == pytest.approx(9_000.0)
    assert pa.investable_notional(10_000.0, 0.0) == pytest.approx(10_000.0)
    assert pa.investable_notional(10_000.0, 100.0) == pytest.approx(0.0)


def test_investable_notional_clamps_a_nonsense_reserve_rather_than_inverting_the_base():
    """A negative reserve would INFLATE the base into money the account does not
    have, and a reserve above 100 would make it negative. The validator blocks both
    before a plan is ever solved; this is the arithmetic refusing to produce a
    monster anyway if one gets through."""
    assert pa.investable_notional(10_000.0, -25.0) == pytest.approx(10_000.0)
    assert pa.investable_notional(10_000.0, 175.0) == pytest.approx(0.0)


def test_reserved_notional_for_is_exactly_what_investable_left_behind():
    """Defined in terms of ``investable_notional`` so the two cannot drift: what is
    reserved plus what is investable IS the base, at every reserve."""
    for pct in (0.0, 10.0, 33.33, 99.9, 100.0):
        reserved = pa.reserved_notional_for(10_000.0, pct)
        assert reserved + pa.investable_notional(10_000.0, pct) == pytest.approx(10_000.0)
    assert pa.reserved_notional_for(10_000.0, 10.0) == pytest.approx(1_000.0)


def test_reserved_notional_for_clamps_a_nonsense_reserve_like_its_partner_does():
    """The same defence ``investable_notional`` carries, pinned on this side too.

    Written as a SUBTRACTION from ``investable_notional`` precisely so it inherits
    that clamp; spelled as its own ``base * pct / 100`` instead -- the obvious
    "simplification" -- a -20 reserve would report -2,000 of money held back, which
    is money the account does not have, and the identity above (reserved +
    investable == base) would still hold at every legal value.
    """
    assert pa.reserved_notional_for(10_000.0, -20.0) == pytest.approx(0.0)
    assert pa.reserved_notional_for(10_000.0, 175.0) == pytest.approx(10_000.0)


def test_effective_target_pct_restates_a_relative_weight_against_the_gross_base():
    """The two percentages this feature carries are DIFFERENT numbers with the same
    '%' sign, and this is the only conversion between them. 50% of what a 10%
    reserve left IS 45% of the base."""
    assert pa.effective_target_pct(50.0, 10.0) == pytest.approx(45.0)
    assert pa.effective_target_pct(100.0, 25.0) == pytest.approx(75.0)
    # No reserve: the two percentages coincide, which is why the defect was
    # invisible until a reserve was stored.
    assert pa.effective_target_pct(50.0, 0.0) == pytest.approx(50.0)
    # A full reserve targets nothing, whatever the weights say.
    assert pa.effective_target_pct(100.0, 100.0) == pytest.approx(0.0)


def test_effective_target_pct_clamps_the_reserve_like_the_rest_of_the_arithmetic():
    """Drawn live from a box the validator has not accepted yet, so a -20 must not
    print a target ABOVE the weight the user typed."""
    assert pa.effective_target_pct(50.0, -20.0) == pytest.approx(50.0)
    assert pa.effective_target_pct(50.0, 175.0) == pytest.approx(0.0)


def test_effective_target_pct_is_the_same_share_the_solver_actually_uses():
    """Not a second formula. What the engine gives a label is its weight applied to
    ``investable_notional``; this must be that money as a share of the gross base,
    or the caption and the plan disagree about the same row."""
    base, reserve, weight = 10_000.0, 10.0, 30.0
    money = pa.investable_notional(base, reserve) * weight / 100.0

    assert pa.effective_target_pct(weight, reserve) == pytest.approx(money / base * 100.0)


def test_the_reserve_scales_the_base_and_labels_still_total_one_hundred():
    """THE WORKED EXAMPLE. Base 10,000, reserve 10%, labels 50/30/20 -> 4,500 /
    2,700 / 1,800 of buys and 1,000 held as cash. Nothing the user typed was
    rewritten: the percentages are still 50/30/20."""
    current = {s: _pos(s, 1.0) for s in ("AAA", "BBB", "CCC")}
    margin = {s: MarginInfo(symbol=s, fractionable=False, bp_factor=1.0)
              for s in ("AAA", "BBB", "CCC")}
    labels = [LabelTarget("A", 50.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 30.0, [SymbolTarget("BBB", 100.0)]),
              LabelTarget("C", 20.0, [SymbolTarget("CCC", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=10.0)

    by_symbol = {r.symbol: r.target_notional for r in plan.rows}
    assert by_symbol == {"AAA": pytest.approx(4_500.0),
                         "BBB": pytest.approx(2_700.0),
                         "CCC": pytest.approx(1_800.0)}
    assert plan.total_buy_value == pytest.approx(9_000.0)
    assert plan.reserved_pct == pytest.approx(10.0)
    assert plan.reserved_notional == pytest.approx(1_000.0)
    assert plan.investable_notional == pytest.approx(9_000.0)
    # The GROSS base is what the plan reports: reserved + investable == base.
    assert plan.base_notional == pytest.approx(10_000.0)


def test_the_plan_reserve_comes_from_the_STORED_reserve_not_from_a_label_shortfall():
    """The reversal of 6fd532c. Labels that do not total 100 are now an ERROR, so
    ``100 - sum(target_pct)`` is no longer a reserve -- it is a bug the validator
    catches. Only ``unallocated_pct`` reserves money."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 70.0, [SymbolTarget("AAA", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)

    assert plan.reserved_pct == 0.0
    assert plan.reserved_notional == 0.0


def test_investable_notional_on_a_plan_is_the_base_minus_what_is_reserved():
    """A derived PROPERTY, not a stored field: a fourth money number that could
    disagree with the other three is exactly the drift this model exists to stop."""
    assert AllocationPlan(base_notional=10_000.0,
                          reserved_notional=1_000.0).investable_notional == 9_000.0


def test_the_reserve_is_still_kept_apart_from_unallocatable_pct():
    """``unallocatable_pct`` means "no label COULD absorb this" -- an empty label, a
    no-price symbol. The reserve is deliberate. Opposite problems, opposite fixes.

    SEPARATE, but measured the same way: the empty label's 20% weight buys 20% of
    the 7,500 the reserve left, which is 15% of the base. Both fields divide
    ``base_notional`` so that 25 + 15 + 60 accounts for the whole book."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 80.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 20.0, [])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=25.0)

    assert plan.reserved_pct == pytest.approx(25.0)
    assert plan.unallocatable_pct == pytest.approx(15.0)
    # 80% of the 7,500 investable remainder, NOT of the 10,000 base.
    assert plan.total_buy_value == pytest.approx(6_000.0)
    # 25 reserved + 15 unabsorbable + 60 deployed == the whole base.
    assert plan.reserved_pct + plan.unallocatable_pct == pytest.approx(40.0)


def test_unallocatable_pct_is_a_share_of_the_BASE_not_of_what_the_reserve_left():
    """The denominator that changed under 8ba9a33 without the field saying so.

    It is accumulated from raw label ``target_pct``, and those became shares of the
    INVESTABLE remainder rather than of the base. Base 10,000 with a 50% reserve,
    labels 50 (held) / 50 (EMPTY): the empty half can absorb 2,500, which is 25% of
    the base -- but the raw weight is 50, so the plan reported ``reserved_pct=50``
    AND ``unallocatable_pct=50``, summing to 100 and reading as "the whole book is
    cash" on a plan that invests 2,500. They are the two halves of the same
    question ("what is NOT going to work, and why") and they only add up if they
    divide the same thing.
    """
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("HELD", 50.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 50.0, [])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=50.0)

    assert plan.reserved_pct == pytest.approx(50.0)
    assert plan.unallocatable_pct == pytest.approx(25.0)
    # ...and the third quarter is the money that actually trades.
    assert plan.total_buy_value == pytest.approx(2_500.0)
    assert plan.reserved_pct + plan.unallocatable_pct < 100.0


def test_a_no_price_symbols_share_is_reported_against_the_base_too():
    """The second accumulation site. A skipped symbol's share arrives already
    multiplied out of the label weight, so it carries the same relative
    denominator and needs the same restatement."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0),
                                       SymbolTarget("BBB", 40.0)])]
    current = {"AAA": _pos("AAA", None), "BBB": _pos("BBB", 50.0)}

    plan = pa.compute_allocation(10_000.0, 1_000_000.0, labels, current, {},
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=20.0)

    # 60% of the 8,000 the reserve left is 4,800 -- 48% of the 10,000 base.
    assert plan.unallocatable_pct == pytest.approx(48.0)


def test_the_serialised_plan_reports_unallocatable_against_the_base():
    """``plan_json`` is read by things that have no engine to re-derive anything
    with, so the stored number has to be the one the docstring describes."""
    import json

    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("HELD", 60.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 40.0, [])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=25.0)
    blob = json.loads(json.dumps(plan.to_dict()))

    assert blob["unallocatable_pct"] == pytest.approx(30.0)   # 40% of the 75% left
    assert blob["reserved_pct"] == pytest.approx(25.0)
    # 30% + 25% of the base is idle; the remaining 45% is what the plan deploys.
    assert blob["total_buy_value"] == pytest.approx(4_500.0)


def test_the_empty_label_warning_names_both_the_weight_and_the_share_of_base():
    """The warning is what the user acts on, and under a reserve the two numbers
    are different. Naming only the share of base leaves them hunting for a box
    holding a percentage nobody typed; naming only the weight is the field's own
    defect restated in prose."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("HELD", 60.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("EMPTY", 40.0, [])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=25.0)

    warning = next(w for w in plan.warnings if "EMPTY" in w)
    assert "40.00%" in warning, warning        # the weight the user typed
    assert "30.00%" in warning, warning        # ...which is this much of the base
    assert warning.index("40.00%") < warning.index("30.00%"), warning


def test_a_hundred_percent_reserve_targets_nothing_and_sells_the_book():
    """The extreme, and it must be arithmetic rather than a special case: every
    target is 0, so a held book is closed out."""
    current = {"AAA": _pos("AAA", 10.0, quantity=100.0, cost_basis=1_000.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=100.0)

    assert plan.reserved_notional == pytest.approx(10_000.0)
    assert [(r.symbol, r.side, r.delta_quantity, r.target_quantity)
            for r in plan.rows] == [("AAA", OrderDirection.SELL, -100.0, 0.0)]


def test_raising_the_reserve_on_a_fully_invested_account_SELLS_to_free_the_cash():
    """Arithmetically correct and it must be obvious on the dry run rather than a
    surprise: a book worth its whole base has to shed 10% of itself to fund a 10%
    reserve."""
    current = {"AAA": _pos("AAA", 10.0, quantity=1_000.0, cost_basis=10_000.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    flat = pa.compute_allocation(10_000.0, 0.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    reserved = pa.compute_allocation(10_000.0, 0.0, labels, current, margin,
                                     allow_fractional=False, default_bp_factor=1.0,
                                     valuation_mode=pa.VALUATION_MODE_MARKET,
                                     unallocated_pct=10.0)

    # Already on target with no reserve: a row, but a ZERO-delta one -- no order.
    assert [(r.symbol, r.side, r.delta_quantity) for r in flat.rows] == [
        ("AAA", None, 0.0)]
    assert flat.total_sell_value == 0.0
    # With a 10% reserve the target drops to 9,000 and 100 shares have to go.
    assert [(r.symbol, r.side, r.delta_quantity, r.target_quantity)
            for r in reserved.rows] == [("AAA", OrderDirection.SELL, -100.0, 900.0)]
    assert reserved.total_sell_value == pytest.approx(1_000.0)
    assert reserved.reserved_notional == pytest.approx(1_000.0)


def test_the_reserve_is_recorded_in_plan_json_from_the_stored_value():
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=30.0)
    blob = json.loads(json.dumps(plan.to_dict()))

    assert blob["reserved_pct"] == pytest.approx(30.0)
    assert blob["reserved_notional"] == pytest.approx(3_000.0)
    assert blob["investable_notional"] == pytest.approx(7_000.0)


def test_an_invest_run_takes_no_reserve_because_the_amount_IS_the_budget():
    """Decided and pinned: an INVEST_LABEL run deploys a specific amount the user
    named. Skimming a portfolio-level reserve off it would silently spend less than
    they typed, and ``base_notional`` there is the budget, not the book."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    label = LabelTarget("A", 40.0, [SymbolTarget("AAA", 100.0)])

    plan = pa.compute_label_investment(label, 1_000.0, current, margin,
                                       available_buying_power=10_000.0,
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)

    assert plan.reserved_pct == 0.0
    assert plan.reserved_notional == 0.0
    assert plan.investable_notional == pytest.approx(1_000.0)
    assert plan.total_buy_value == pytest.approx(1_000.0)


def test_compute_label_investment_has_no_reserve_knob_at_all():
    """Not merely defaulted to zero -- absent. A caller cannot pass one by accident
    and quietly under-spend the amount the user named."""
    import inspect
    assert "unallocated_pct" not in inspect.signature(
        pa.compute_label_investment).parameters


# -- the rule: labels total EXACTLY 100 again --------------------------------

def test_a_label_total_below_one_hundred_is_a_hard_ERROR_again():
    """Reversal of 6fd532c. With a stored reserve, the reserve is the single
    unambiguous way to hold cash; leaving under-100 legal too would make "labels
    sum to 90" mean two different things."""
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 100.0)])]
    messages = pa.validate_label_targets(labels)

    assert messages == [pa.ERROR_LABEL_UNDER_FMT.format(total=60.0, under=40.0)]
    assert messages == ["label targets total 60.00% - under 100% by 40.00%. "
                        "Use the Unallocated box to hold money back."]
    assert pa.blocking_messages(messages) == messages


def test_nothing_in_the_advisory_fragments_lets_a_label_total_through_any_more():
    """The advisory fragment 6fd532c added is GONE, so both halves of the rule
    block by default -- the safe direction to be wrong in."""
    assert pa.ADVISORY_MESSAGE_FRAGMENTS == (pa._INVEST_EXCEEDS_BP_FRAGMENT,)
    assert not hasattr(pa, "ADVISORY_LABEL_UNDER_FMT")


def test_an_empty_label_set_is_an_error_not_an_advisory():
    messages = pa.validate_label_targets([])
    assert messages == [pa.ERROR_LABEL_UNDER_FMT.format(total=0.0, under=100.0)]
    assert pa.blocking_messages(messages) == messages


def test_the_label_total_rule_still_respects_the_tolerance_on_both_sides():
    assert pa.validate_label_targets(
        [LabelTarget("A", 99.995, [SymbolTarget("AAA", 100.0)])]) == []
    assert pa.validate_label_targets(
        [LabelTarget("A", 100.005, [SymbolTarget("AAA", 100.0)])]) == []


# -- the reserve's own validation --------------------------------------------

def test_a_reserve_between_zero_and_one_hundred_is_accepted():
    for pct in (0.0, 0.01, 10.0, 99.99, 100.0):
        assert pa.validate_unallocated_pct(pct) == []


def test_a_negative_reserve_is_refused():
    messages = pa.validate_unallocated_pct(-5.0)
    assert messages == [pa.ERROR_UNALLOCATED_RANGE_FMT.format(pct=-5.0)]
    assert pa.blocking_messages(messages) == messages


def test_a_reserve_above_one_hundred_is_refused():
    """Above 100 the investable base would go NEGATIVE, so ``compute_allocation``
    CLAMPS it to 100 and liquidates the book instead -- silently, and not the plan
    the user described. Caught here, where the user can see which box."""
    messages = pa.validate_unallocated_pct(120.0)
    assert messages == [pa.ERROR_UNALLOCATED_RANGE_FMT.format(pct=120.0)]
    assert pa.blocking_messages(messages) == messages


def test_the_range_error_does_not_ROUND_the_offending_value_into_range():
    """It quotes the number back at the user, so it must quote the real one.

    ``validate_unallocated_pct`` has no tolerance on purpose -- 100.005 is out of
    range -- and at ``{pct:.2f}`` the message read "unallocated 100.00% is outside
    0-100%", which is a sentence that refutes itself and leaves the user retyping
    the value they already have. The rejection and the evidence for it have to be
    the same number.
    """
    message = pa.validate_unallocated_pct(100.005)[0]

    assert "100.005" in message, message
    assert "100.00%" not in message, message


def test_the_range_error_still_reads_naturally_for_the_ordinary_typo():
    """The digits that ARE there, and no invented ones: 140, not 140.00 and not
    140.000000000001."""
    assert "140%" in pa.validate_unallocated_pct(140.0)[0]
    assert "-5%" in pa.validate_unallocated_pct(-5.0)[0]


def test_the_range_error_does_not_promise_cash_either():
    """Same correction as the reserve captions: the reserve is a share of a base
    that INCLUDES buying power, so what it holds back is undeployed money and not
    necessarily a cash balance."""
    assert "cash" not in pa.validate_unallocated_pct(120.0)[0].lower()


def test_the_wizard_gate_checks_the_reserve_alongside_the_labels():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    assert pa.steps_validation_messages(labels, unallocated_pct=10.0) == []
    assert pa.steps_validation_messages(labels, unallocated_pct=140.0) == [
        pa.ERROR_UNALLOCATED_RANGE_FMT.format(pct=140.0)]


def test_the_wizard_gate_defaults_to_no_reserve():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]
    assert pa.steps_validation_messages(labels) == []


# -- even split is about the LABELS, and always totals 100 --------------------

def test_even_split_always_totals_exactly_one_hundred():
    """The reserve is stored separately now, so there is nothing for Even split to
    preserve -- and a partial split would produce a label set the validator
    refuses."""
    out = pa.even_split_targets([LabelTarget(n, 0.0) for n in "ABC"])
    assert [t.target_pct for t in out] == [33.33, 33.33, 33.34]
    assert sum(t.target_pct for t in out) == pytest.approx(100.0)


def test_even_split_has_no_total_pct_knob_any_more():
    """Removed rather than left defaulted: 100 is the only value the validator
    accepts, so a knob here is a trap that produces an unsubmittable set."""
    import inspect
    assert "total_pct" not in inspect.signature(pa.even_split_targets).parameters


def test_an_even_split_passes_the_validator_whatever_the_reserve_is():
    labels = [LabelTarget(n, 0.0, [SymbolTarget(n * 3, 100.0)]) for n in "ABC"]
    for reserve in (0.0, 10.0, 90.0):
        assert pa.steps_validation_messages(pa.even_split_targets(labels),
                                            unallocated_pct=reserve) == []


# -- the reserve is not a buying-power shortfall ------------------------------

def test_the_reserve_and_the_buying_power_scaler_stay_separate_facts():
    """A reserve is a TARGET; a bp shortfall is a CONSTRAINT the plan hit. Reporting
    them as one number would tell a user to lower a reserve they never set."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    # Base 10,000 with a 10% reserve wants 9,000 of buys, but only 4,500 of buying
    # power exists: the scaler halves the buys and the reserve does not move.
    plan = pa.compute_allocation(10_000.0, 4_500.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=10.0)

    assert plan.scale_factor == pytest.approx(0.5)
    assert plan.reserved_pct == pytest.approx(10.0)
    assert plan.reserved_notional == pytest.approx(1_000.0)
    assert plan.total_buy_value == pytest.approx(4_500.0)
    assert any(any(pa.REASON_SCALED_PREFIX in reason for reason in r.reasons)
               for r in plan.rows)


def test_a_plan_that_fits_its_buying_power_never_scales_however_big_the_reserve():
    """The mirror: the reserve SHRINKS the requirement, so it can only ever move the
    scaler away from firing."""
    current = {"AAA": _pos("AAA", 10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])]

    plan = pa.compute_allocation(10_000.0, 9_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=10.0)

    assert plan.scale_factor == 1.0
    assert plan.required_buying_power == pytest.approx(9_000.0)


# -- redistribution against a scaled base -------------------------------------

def test_redistribution_still_converges_against_a_reserved_base():
    """``redistribute_label_residuals`` works on LABEL TOTALS, which are now shares
    of the investable remainder. Its termination argument -- a bounded pass count
    and a residual that only ever shrinks -- is untouched by which number the totals
    were derived from."""
    current = {"AAA": _pos("AAA", 300.0), "BBB": _pos("BBB", 100.0)}
    margin = {s: MarginInfo(symbol=s, fractionable=False, bp_factor=1.0)
              for s in ("AAA", "BBB")}
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 50.0),
                                       SymbolTarget("BBB", 50.0)])]

    # ``compute_allocation`` runs redistribution itself, against the label totals
    # it just derived from the SCALED base -- which is the only thing the reserve
    # changed.
    plan = pa.compute_allocation(10_000.0, 10_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET,
                                 unallocated_pct=10.0)

    # Whole shares of a 300 and a 100 name cannot land exactly on 4,500 each, so
    # the residual is real. It CONVERGED: no unconverged warning, and the label
    # ends up within one unit of the cheapest absorber of its 9,000 total.
    assert not any(pa.WARNING_RESIDUAL_UNCONVERGED_FMT.split("{", 1)[0] in w
                   for w in plan.warnings)
    assert plan.total_buy_value <= 9_000.0 + pa.MONEY_EPSILON
    assert plan.total_buy_value > 9_000.0 - 100.0     # inside one BBB share
    assert plan.reserved_notional == pytest.approx(1_000.0)
