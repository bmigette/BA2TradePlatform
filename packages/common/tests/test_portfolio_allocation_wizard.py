"""Pure unit tests for the Portfolio Allocation wizard arithmetic.

Everything here runs with no DB, no broker and no NiceGUI. Invoke by explicit
path -- pytest.ini has `testpaths = tests`, so this directory is not collected
by a bare `pytest`.
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot, MarginInfo
from ba2_common.core.portfolio_allocation import (
    ACTION_ADJUST,
    ACTION_CLOSE,
    ACTION_NEW,
    ACTION_SKIP,
    ERROR_INVEST_AMOUNT_FMT,
    ERROR_INVEST_LABEL_EMPTY_FMT,
    ERROR_INVEST_NO_LABEL,
    ERROR_SYMBOL_TOTAL_FMT,
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT,
    REASON_BELOW_MIN_ORDER_FMT,
    VALUATION_MODE_COST,
    VALUATION_MODE_MARKET,
    AllocationPlan,
    AllocationRow,
    BaseSnapshot,
    LabelTarget,
    PositionState,
    SymbolTarget,
    WARNING_INVEST_EXCEEDS_BP_FMT,
    WARNING_NO_MULTIPLIER,
    blocking_messages,
    build_base_snapshot,
    compute_allocation,
    compute_base_notional,
    decide_symbol_action,
    dry_run_rows,
    even_split_targets,
    filter_plan_rows,
    invest_validation_messages,
    split_delta_fifo,
    steps_validation_messages,
    summarise_plan,
    validate_invest_amount,
)
from ba2_common.core.types import OrderDirection


def test_base_notional_adds_cost_basis_of_managed_positions_only():
    current = {
        "AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0, price=160.0),
        "MSFT": PositionState(symbol="MSFT", quantity=5, cost_basis=2000.0, price=410.0),
        "TSLA": PositionState(symbol="TSLA", quantity=3, cost_basis=900.0, price=300.0),
    }
    # TSLA is held but NOT managed -> it must not inflate the base.
    # valuation_mode is REQUIRED (no default, see the Task 25 amendment) and COST is
    # what 1500 + 2000 is: at market those two positions are 1600 + 2050.
    base = compute_base_notional(10_000.0, current, ["AAPL", "MSFT"],
                                 valuation_mode=VALUATION_MODE_COST)
    assert base == pytest.approx(13_500.0)


def test_build_base_snapshot_splits_buying_power_from_managed_value():
    snap = AccountSnapshot(cash=2_000.0, buying_power=10_000.0, margin_multiplier=2.0,
                           is_margin_account=True, supports_fractional=True)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0)}

    base = build_base_snapshot(snap, current, ["AAPL"])

    assert isinstance(base, BaseSnapshot)
    assert base.available_buying_power == pytest.approx(10_000.0)
    assert base.managed_value == pytest.approx(1_500.0)
    assert base.base_notional == pytest.approx(11_500.0)
    assert base.default_bp_factor == pytest.approx(2.0)
    assert base.valuation_mode == VALUATION_MODE_COST
    assert base.warnings == []


def test_build_base_snapshot_in_market_mode_values_positions_at_the_live_price():
    snap = AccountSnapshot(buying_power=10_000.0, margin_multiplier=1.0)
    current = {"AAPL": PositionState(symbol="AAPL", quantity=10, cost_basis=1500.0,
                                     price=250.0)}

    base = build_base_snapshot(snap, current, ["AAPL"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.managed_value == pytest.approx(2_500.0)
    assert base.base_notional == pytest.approx(12_500.0)
    assert base.valuation_mode == VALUATION_MODE_MARKET


def test_build_base_snapshot_without_multiplier_assumes_cash_account():
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=None)
    base = build_base_snapshot(snap, {}, [])
    assert base.default_bp_factor == pytest.approx(1.0)
    assert WARNING_NO_MULTIPLIER in base.warnings


def test_build_base_snapshot_missing_buying_power_raises():
    snap = AccountSnapshot(cash=1_000.0, buying_power=None)
    with pytest.raises(ValueError):
        build_base_snapshot(snap, {}, [])


def test_build_base_snapshot_with_no_snapshot_at_all_raises():
    with pytest.raises(ValueError):
        build_base_snapshot(None, {}, [])


# ---------------------------------------------------------------------------
# Task 70: the dry-run table -- row and total arithmetic.
# ---------------------------------------------------------------------------


def _plan():
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", price=160.0, delta_quantity=10.0,
                          side=OrderDirection.BUY, estimated_value=1600.0,
                          bp_cost=1600.0, bp_factor=1.0),
            AllocationRow(symbol="NVDA", price=900.0, delta_quantity=4.0,
                          side=OrderDirection.BUY, estimated_value=3600.0,
                          bp_cost=7200.0, bp_factor=2.0,
                          reasons=["⚠ not marginable"]),
            AllocationRow(symbol="MSFT", price=400.0, delta_quantity=-5.0,
                          side=OrderDirection.SELL, estimated_value=2000.0,
                          bp_cost=0.0, bp_factor=1.0),
            AllocationRow(symbol="TSLA", price=None, delta_quantity=0.0,
                          side=None, skipped=True, reasons=["no price - skipped"]),
        ],
        base_notional=20_000.0,
        available_buying_power=10_000.0,
        required_buying_power=8_800.0,
        bp_usage_pct=88.0,
        total_buy_value=5_200.0,
        total_sell_value=2_000.0,
    )


def test_dry_run_rows_shows_one_row_per_non_zero_delta():
    rows = dry_run_rows(_plan())
    assert [r["symbol"] for r in rows] == ["AAPL", "NVDA", "MSFT"]
    assert rows[0]["side"] == "BUY"
    assert rows[2]["side"] == "SELL"
    assert rows[2]["quantity"] == pytest.approx(5.0)  # magnitude, never signed


def test_dry_run_rows_carries_bp_usage_pct_and_reason_strings():
    rows = dry_run_rows(_plan())
    nvda = next(r for r in rows if r["symbol"] == "NVDA")
    assert nvda["bp_cost"] == pytest.approx(7200.0)
    assert nvda["bp_usage_pct"] == pytest.approx(72.0)  # 7200 of 10000
    assert "not marginable" in nvda["reasons"]


def test_dry_run_rows_omits_a_no_price_skip():
    """TSLA has no order AND no target -- there is nothing to review, and it is
    already counted in ``plan.unallocatable_pct``. Contrast a SUPPRESSED row
    below, which is an order the user asked for that will not be sent."""
    assert "TSLA" not in [r["symbol"] for r in dry_run_rows(_plan())]


def test_dry_run_rows_with_no_buying_power_reports_zero_pct_not_a_zero_division():
    plan = _plan()
    plan.available_buying_power = 0.0
    assert all(r["bp_usage_pct"] == 0.0 for r in dry_run_rows(plan))


def test_filter_plan_rows_unticking_a_buy_drops_it_from_the_totals():
    filtered = filter_plan_rows(_plan(), ["AAPL", "MSFT"])
    assert [r.symbol for r in filtered.rows] == ["AAPL", "MSFT"]
    assert filtered.total_buy_value == pytest.approx(1600.0)
    assert filtered.total_sell_value == pytest.approx(2000.0)
    assert filtered.required_buying_power == pytest.approx(1600.0)
    assert filtered.bp_usage_pct == pytest.approx(16.0)


def test_filter_plan_rows_keeps_the_plan_level_context():
    filtered = filter_plan_rows(_plan(), ["AAPL"])
    assert filtered.base_notional == pytest.approx(20_000.0)
    assert filtered.available_buying_power == pytest.approx(10_000.0)


def test_filter_plan_rows_keeps_the_valuation_mode():
    """The filtered plan is what Submit persists into ``plan_json``. Losing the
    mode there would silently re-label a cost-basis plan as a market one."""
    plan = _plan()
    plan.valuation_mode = VALUATION_MODE_COST
    assert filter_plan_rows(plan, ["AAPL"]).valuation_mode == VALUATION_MODE_COST


def test_filter_plan_rows_keeps_the_unallocatable_share_of_the_base():
    """It is a property of the BASE (labels that could absorb nothing), not of the
    ticked rows. Zeroing it would report the filtered plan as fully deployed."""
    plan = _plan()
    plan.unallocatable_pct = 12.5
    assert filter_plan_rows(plan, ["AAPL"]).unallocatable_pct == pytest.approx(12.5)


def test_filter_plan_rows_does_not_mutate_the_original():
    plan = _plan()
    filter_plan_rows(plan, ["AAPL"])
    assert [r.symbol for r in plan.rows] == ["AAPL", "NVDA", "MSFT", "TSLA"]
    assert plan.total_buy_value == pytest.approx(5_200.0)


def test_summarise_plan_estimated_cash_after_nets_buys_against_sells():
    totals = summarise_plan(_plan(), cash=7_000.0)
    assert totals["total_buy_value"] == pytest.approx(5_200.0)
    assert totals["total_sell_value"] == pytest.approx(2_000.0)
    assert totals["net_buy_value"] == pytest.approx(3_200.0)
    assert totals["estimated_cash_after"] == pytest.approx(3_800.0)


def test_summarise_plan_with_no_cash_figure_raises():
    with pytest.raises(ValueError):
        summarise_plan(_plan(), cash=None)


def test_summarise_plan_treats_zero_cash_as_a_real_balance():
    """0.0 is falsy but it is a REAL balance; only ``None`` is 'unknown'."""
    totals = summarise_plan(_plan(), cash=0.0)
    assert totals["estimated_cash_after"] == pytest.approx(-3_200.0)


# -- truth-telling: fractional vs whole-share, and suppressed orders ---------


def _one_symbol_plan(*, price, target_notional, margin, allow_fractional,
                     buying_power=1_000_000.0):
    """Solve a single-symbol REBALANCE through the REAL engine.

    Hand-built rows cannot prove the dry-run reflects what the engine produces --
    the suppression that matters here happens INSIDE ``compute_allocation``.
    """
    return compute_allocation(
        base_notional=target_notional,
        available_buying_power=buying_power,
        labels=[LabelTarget(margin.symbol, 100.0, [SymbolTarget(margin.symbol, 100.0)])],
        current={margin.symbol: PositionState(symbol=margin.symbol, price=price)},
        margin={margin.symbol: margin},
        allow_fractional=allow_fractional,
        default_bp_factor=1.0,
        valuation_mode=VALUATION_MODE_MARKET,
    )


def test_dry_run_rows_flags_an_order_that_will_be_fractional():
    margin = MarginInfo(symbol="FRAC", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001)
    plan = _one_symbol_plan(price=300.0, target_notional=500.0, margin=margin,
                            allow_fractional=True)
    row = dry_run_rows(plan)[0]
    # EXACTLY what the broker's 1e-5 grid produced. Displaying 1.6667 (a 4dp
    # round, cf. DEFAULT_FRACTIONAL_DECIMALS) would misstate the order.
    assert row["quantity"] == 1.66666
    assert row["fractional"] is True
    assert row["sized_fractional"] is True


def test_dry_run_rows_does_not_call_a_whole_share_order_fractional():
    """Sized on the fractional grid but landing on exactly 5 shares. The ORDER is
    whole-share, so neither the market-only rule (L1) nor the $5 notional floor
    (L2) applies to it, and the table must not claim otherwise."""
    margin = MarginInfo(symbol="FRAC", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001)
    plan = _one_symbol_plan(price=100.0, target_notional=500.0, margin=margin,
                            allow_fractional=True)
    row = dry_run_rows(plan)[0]
    assert row["quantity"] == pytest.approx(5.0)
    assert row["fractional"] is False       # the ORDER
    assert row["sized_fractional"] is True  # the GRID it was sized on


def test_dry_run_rows_flags_a_whole_share_order_when_fractional_is_off():
    margin = MarginInfo(symbol="WHOLE", bp_factor=1.0, fractionable=False)
    plan = _one_symbol_plan(price=300.0, target_notional=500.0, margin=margin,
                            allow_fractional=False)
    row = dry_run_rows(plan)[0]
    assert row["quantity"] == pytest.approx(1.0)
    assert row["fractional"] is False
    assert row["sized_fractional"] is False


def test_dry_run_rows_shows_a_sub_minimum_fractional_target_as_suppressed():
    """L2: a $1.95 fractional target is NOT 'rounds to zero' -- fractional is
    simply unavailable below the broker's $5 floor, and the table must say so
    instead of dropping the symbol silently."""
    margin = MarginInfo(symbol="PENNY", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001, min_fractional_notional=5.0)
    plan = _one_symbol_plan(price=3.0, target_notional=1.95, margin=margin,
                            allow_fractional=True)

    rows = dry_run_rows(plan)
    assert [r["symbol"] for r in rows] == ["PENNY"]
    row = rows[0]
    assert row["suppressed"] is True
    assert row["quantity"] == 0.0
    assert row["side"] == ""            # no order, so no side
    assert REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
        value=1.95, minimum=5.0) in row["reasons"]


def test_dry_run_rows_shows_a_below_min_order_size_row_as_suppressed():
    margin = MarginInfo(symbol="BIGLOT", bp_factor=1.0, min_order_size=10.0)
    plan = _one_symbol_plan(price=100.0, target_notional=500.0, margin=margin,
                            allow_fractional=False)
    row = dry_run_rows(plan)[0]
    assert row["suppressed"] is True
    assert REASON_BELOW_MIN_ORDER_FMT.format(size=10.0) in row["reasons"]


def test_dry_run_rows_shows_a_buy_scaled_away_by_buying_power():
    """A buy the buying-power scaler wiped out is still an order the user asked
    for and will not get."""
    margin = MarginInfo(symbol="BIG", bp_factor=1.0)
    plan = _one_symbol_plan(price=1_000.0, target_notional=10_000.0, margin=margin,
                            allow_fractional=False, buying_power=500.0)
    rows = dry_run_rows(plan)
    assert [r["symbol"] for r in rows] == ["BIG"]
    assert rows[0]["suppressed"] is True
    assert rows[0]["quantity"] == 0.0


def test_dry_run_rows_leaves_an_already_on_target_symbol_out():
    """Zero delta with NO 'why not' reason is nothing to review."""
    margin = MarginInfo(symbol="ONTGT", bp_factor=1.0)
    plan = compute_allocation(
        base_notional=500.0, available_buying_power=10_000.0,
        labels=[LabelTarget("L", 100.0, [SymbolTarget("ONTGT", 100.0)])],
        current={"ONTGT": PositionState(symbol="ONTGT", quantity=5.0, price=100.0)},
        margin={"ONTGT": margin}, allow_fractional=False, default_bp_factor=1.0,
        valuation_mode=VALUATION_MODE_MARKET)
    assert plan.rows[0].delta_quantity == 0.0
    assert dry_run_rows(plan) == []


def test_dry_run_rows_of_a_suppressed_row_is_not_counted_by_filter_plan_rows():
    """Ticking a suppressed symbol must not add money to the totals."""
    margin = MarginInfo(symbol="PENNY", bp_factor=1.0, fractionable=True,
                        min_trade_increment=0.00001, min_fractional_notional=5.0)
    plan = _one_symbol_plan(price=3.0, target_notional=1.95, margin=margin,
                            allow_fractional=True)
    filtered = filter_plan_rows(plan, ["PENNY"])
    assert filtered.total_buy_value == pytest.approx(0.0)
    assert filtered.required_buying_power == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Task 71: wizard steps 1-3 -- label percentages, symbol weights, INVEST amount.
# ---------------------------------------------------------------------------


def test_even_split_targets_gives_each_label_an_equal_share_totalling_100():
    labels = [LabelTarget("A", 0.0, [SymbolTarget("AAA", 100.0)]),
              LabelTarget("B", 0.0, [SymbolTarget("BBB", 100.0)]),
              LabelTarget("C", 0.0, [SymbolTarget("CCC", 100.0)])]
    out = even_split_targets(labels)
    assert [t.target_pct for t in out] == [33.33, 33.33, 33.34]
    assert sum(t.target_pct for t in out) == pytest.approx(100.0)
    assert [t.label for t in out] == ["A", "B", "C"]
    # The originals are not mutated -- the dialog can still cancel.
    assert [t.target_pct for t in labels] == [0.0, 0.0, 0.0]


def test_even_split_targets_of_nothing_is_empty():
    assert even_split_targets([]) == []


def test_even_split_targets_gives_each_copy_its_own_symbol_list():
    labels = [LabelTarget("A", 0.0, [SymbolTarget("AAA", 100.0)])]
    out = even_split_targets(labels)
    out[0].symbols.append(SymbolTarget("BBB", 0.0))
    assert [st.symbol for st in labels[0].symbols] == ["AAA"]


def test_even_split_targets_passes_its_own_output_to_the_validator():
    """The 0.01pp tolerance is tight enough to reject a naive 3 x 33.33 = 99.99,
    which is exactly why defaults must come from here and never by hand."""
    labels = [LabelTarget(name, 0.0, [SymbolTarget(name * 3, 100.0)])
              for name in ("A", "B", "C")]
    assert steps_validation_messages(even_split_targets(labels)) == []


def test_steps_validation_reports_the_label_total_and_the_symbol_totals():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 40.0), SymbolTarget("BBB", 40.0)]),
              LabelTarget("B", 40.0, [SymbolTarget("CCC", 100.0)])]
    messages = steps_validation_messages(labels)
    assert any("A" in m and "80.00" in m for m in messages)


def test_steps_validation_does_not_report_a_symbol_total_twice():
    """``validate_label_targets`` already folds in ``validate_symbol_weights``
    (Task 22). A second loop here emitted the identical string a second time."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 40.0), SymbolTarget("BBB", 40.0)])]
    messages = steps_validation_messages(labels)
    assert messages.count(ERROR_SYMBOL_TOTAL_FMT.format(label="A", total=80.0)) == 1


def test_steps_validation_is_empty_for_a_fully_valid_set():
    labels = [LabelTarget("A", 60.0, [SymbolTarget("AAA", 50.0), SymbolTarget("BBB", 50.0)]),
              LabelTarget("B", 40.0, [SymbolTarget("CCC", 100.0)])]
    assert steps_validation_messages(labels) == []


def test_steps_validation_includes_the_label_target_errors_too():
    """Step 1's rule (labels total 100) and step 2's rule (weights total 100 inside
    each label) are reported together, so Submit is blocked on either."""
    labels = [LabelTarget("A", 55.0, [SymbolTarget("AAA", 100.0)])]
    messages = steps_validation_messages(labels)
    assert any("must total 100%" in m for m in messages)


def test_validate_invest_amount_accepts_a_positive_amount_within_buying_power():
    assert validate_invest_amount(500.0, available_buying_power=10_000.0) == []


def test_validate_invest_amount_rejects_zero_and_negative():
    assert validate_invest_amount(0.0, available_buying_power=10_000.0) == [
        ERROR_INVEST_AMOUNT_FMT.format(amount=0.0)]
    assert validate_invest_amount(-5.0, available_buying_power=10_000.0)


def test_validate_invest_amount_warns_when_it_exceeds_buying_power():
    messages = validate_invest_amount(20_000.0, available_buying_power=10_000.0)
    assert any("buying power" in m for m in messages)


# -- the INVEST_LABEL gate: the amount is not the only thing that can be wrong --


def test_invest_validation_blocks_a_symbol_weight_set_that_does_not_total_100():
    """``compute_label_investment`` multiplies the weights straight through, so a
    150% set turns a 10,000 budget into 15,000 of buys. ``validate_invest_amount``
    only looks at the amount; the weights need their own gate."""
    label = LabelTarget("Income", 40.0, [SymbolTarget("AAA", 100.0),
                                         SymbolTarget("BBB", 50.0)])
    messages = invest_validation_messages(label, 10_000.0,
                                          available_buying_power=50_000.0)
    assert messages == [ERROR_SYMBOL_TOTAL_FMT.format(label="Income", total=150.0)]


def test_invest_validation_ignores_the_labels_own_percentage():
    """A single label at 40% must not trip the labels-total-100 rule -- the amount
    is the whole budget, so ``target_pct`` is meaningless on this path."""
    label = LabelTarget("Income", 40.0, [SymbolTarget("AAA", 100.0)])
    assert invest_validation_messages(label, 1_000.0,
                                      available_buying_power=50_000.0) == []


def test_invest_validation_requires_a_label():
    assert invest_validation_messages(None, 1_000.0,
                                      available_buying_power=50_000.0) == [
        ERROR_INVEST_NO_LABEL]


def test_invest_validation_rejects_a_label_that_can_absorb_nothing():
    label = LabelTarget("Empty", 0.0, [])
    assert invest_validation_messages(label, 1_000.0,
                                      available_buying_power=50_000.0) == [
        ERROR_INVEST_LABEL_EMPTY_FMT.format(label="Empty")]


def test_invest_validation_reports_the_weights_and_the_amount_together():
    label = LabelTarget("Income", 40.0, [SymbolTarget("AAA", 90.0)])
    messages = invest_validation_messages(label, 0.0, available_buying_power=50_000.0)
    assert ERROR_SYMBOL_TOTAL_FMT.format(label="Income", total=90.0) in messages
    assert ERROR_INVEST_AMOUNT_FMT.format(amount=0.0) in messages


def test_blocking_messages_drops_the_buying_power_advisory():
    """Over-spending buying power EXPLAINS -- the engine scales the plan down and
    the dry-run shows the result, which beats refusing to compute it."""
    label = LabelTarget("Income", 40.0, [SymbolTarget("AAA", 100.0)])
    messages = invest_validation_messages(label, 20_000.0,
                                          available_buying_power=10_000.0)
    assert messages == [WARNING_INVEST_EXCEEDS_BP_FMT.format(
        amount=20_000.0, available=10_000.0)]
    assert blocking_messages(messages) == []


def test_blocking_messages_keeps_the_zero_amount_error():
    """It and the advisory both start 'amount ...', so a leading-prefix test
    would swallow a real error."""
    messages = validate_invest_amount(0.0, available_buying_power=10_000.0)
    assert blocking_messages(messages) == messages


def test_blocking_messages_keeps_every_rebalance_error():
    labels = [LabelTarget("A", 55.0, [SymbolTarget("AAA", 100.0)])]
    messages = steps_validation_messages(labels)
    assert messages and blocking_messages(messages) == messages


# ---------------------------------------------------------------------------
# Task 72: decide_symbol_action -- which of the three submission paths a row takes
# ---------------------------------------------------------------------------

def test_decide_symbol_action_not_held_with_a_buy_is_a_new_position():
    row = AllocationRow(symbol="NVDA", price=900.0, delta_quantity=4.0,
                        side=OrderDirection.BUY, target_quantity=4.0)
    assert decide_symbol_action(row, None) == ACTION_NEW


def test_decide_symbol_action_held_with_a_non_zero_target_is_an_adjustment():
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-3.0,
                        side=OrderDirection.SELL, target_quantity=7.0)
    state = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_ADJUST


def test_decide_symbol_action_held_with_a_zero_target_is_a_close():
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-10.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    state = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_CLOSE


def test_decide_symbol_action_skipped_row_is_never_traded():
    row = AllocationRow(symbol="TSLA", price=None, delta_quantity=5.0,
                        side=OrderDirection.BUY, skipped=True,
                        reasons=["no price - skipped"])
    assert decide_symbol_action(row, None) == ACTION_SKIP


def test_decide_symbol_action_sell_of_an_unheld_symbol_is_skipped():
    # Long-only: there is nothing to sell, so this must not become a short.
    row = AllocationRow(symbol="NVDA", price=900.0, delta_quantity=-2.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    assert decide_symbol_action(row, None) == ACTION_SKIP


def test_decide_symbol_action_topping_up_a_held_symbol_is_an_adjustment():
    """A BUY into a symbol we already hold must resize the EXISTING transaction,
    not open a second one alongside it (decision 14: one transaction per symbol)."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=5.0,
                        side=OrderDirection.BUY, target_quantity=15.0)
    state = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_ADJUST


def test_decide_symbol_action_a_broker_position_we_do_not_track_is_not_adjustable():
    """Held at the broker but with no open Transaction of ours: there is nothing
    to adjust, so a BUY opens a fresh transaction and a SELL is refused rather
    than trimming a position this platform does not own."""
    buy = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=5.0,
                        side=OrderDirection.BUY, target_quantity=15.0)
    sell = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-5.0,
                         side=OrderDirection.SELL, target_quantity=5.0)
    untracked = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[])
    assert decide_symbol_action(buy, untracked) == ACTION_NEW
    assert decide_symbol_action(sell, untracked) == ACTION_SKIP


def test_decide_symbol_action_a_suppressed_row_is_skipped_not_closed():
    """The $5 fractional floor zeroes the DELTA and leaves target_quantity at the
    CURRENT holding. A close is decided on the TARGET, so an unsendable trim of a
    position down to zero must not be promoted into a full liquidation."""
    row = AllocationRow(
        symbol="SCHD", price=25.0, delta_quantity=0.0, side=None,
        target_quantity=0.0,
        reasons=[REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(value=1.95, minimum=5)])
    state = PositionState(symbol="SCHD", quantity=3.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_SKIP


# ---------------------------------------------------------------------------
# Task 72: split_delta_fifo
# ---------------------------------------------------------------------------

def test_split_delta_fifo_sell_spans_two_transactions_oldest_first():
    assert split_delta_fifo(-30.0, [(11, 20.0), (12, 15.0)]) == [(11, -20.0), (12, -10.0)]


def test_split_delta_fifo_sell_larger_than_held_is_clamped_to_what_exists():
    assert split_delta_fifo(-50.0, [(11, 20.0), (12, 15.0)]) == [(11, -20.0), (12, -15.0)]


def test_split_delta_fifo_buy_lands_entirely_on_the_oldest_transaction():
    assert split_delta_fifo(7.0, [(11, 20.0), (12, 15.0)]) == [(11, 7.0)]


def test_split_delta_fifo_with_no_transactions_returns_empty():
    assert split_delta_fifo(-5.0, []) == []
    assert split_delta_fifo(0.0, [(11, 20.0)]) == []


def test_split_delta_fifo_stops_at_the_first_transaction_that_covers_the_trim():
    """A trim that the oldest transaction absorbs must not touch the newer one --
    every extra transaction touched is an extra broker order and an extra TP/SL
    rebuild."""
    assert split_delta_fifo(-5.0, [(11, 20.0), (12, 15.0)]) == [(11, -5.0)]


def test_split_delta_fifo_skips_an_empty_transaction():
    """A fully-consumed-but-still-open transaction has nothing to give; sending it
    a zero-quantity adjustment is an order the broker cancels."""
    assert split_delta_fifo(-5.0, [(11, 0.0), (12, 15.0)]) == [(12, -5.0)]


def test_decide_symbol_action_a_zero_delta_that_still_carries_a_side_is_skipped():
    """``side`` and ``delta_quantity`` are INDEPENDENT fields of ``to_dict()``, so a
    plan replayed from ``plan_json`` can present both. A zero-quantity order is one
    the broker cancels, so the delta -- not the side -- decides."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=0.0,
                        side=OrderDirection.BUY, target_quantity=10.0)
    assert decide_symbol_action(row, None) == ACTION_SKIP
    state = PositionState(symbol="AAPL", quantity=10.0, transaction_ids=[1])
    assert decide_symbol_action(row, state) == ACTION_SKIP
