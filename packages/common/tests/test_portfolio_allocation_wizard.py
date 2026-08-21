"""Pure unit tests for the Portfolio Allocation wizard arithmetic.

Everything here runs with no DB, no broker and no NiceGUI. Invoke by explicit
path -- pytest.ini has `testpaths = tests`, so this directory is not collected
by a bare `pytest`.
"""
import pytest

from ba2_common.core.account_types import AccountSnapshot, MarginInfo
from ba2_common.core.portfolio_allocation import (
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
    WARNING_NO_MULTIPLIER,
    build_base_snapshot,
    compute_allocation,
    compute_base_notional,
    dry_run_rows,
    filter_plan_rows,
    summarise_plan,
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
