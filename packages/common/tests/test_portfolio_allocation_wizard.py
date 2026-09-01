"""Pure unit tests for the Portfolio Allocation wizard arithmetic.

Everything here runs with no DB, no broker and no NiceGUI. Invoke by explicit
path -- pytest.ini has `testpaths = tests`, so this directory is not collected
by a bare `pytest`.
"""
import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.account_types import AccountSnapshot, MarginInfo
from ba2_common.core.portfolio_allocation import (
    ACTION_ADJUST,
    ACTION_CLOSE,
    ACTION_NEW,
    ACTION_SKIP,
    ACTION_UNACTIONABLE,
    FRACTIONAL_PATH_FRACTIONAL,
    FRACTIONAL_PATH_WHOLE,
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
    plan_quantity_attempts,
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

    # COST explicitly: this position carries no price, so it is the cost basis that
    # makes 1,500 a number at all. The market-mode split has its own test below.
    base = build_base_snapshot(snap, current, ["AAPL"],
                               valuation_mode=VALUATION_MODE_COST)

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
    base = build_base_snapshot(snap, {}, [], valuation_mode=VALUATION_MODE_MARKET)
    assert base.default_bp_factor == pytest.approx(1.0)
    assert WARNING_NO_MULTIPLIER in base.warnings


def test_build_base_snapshot_missing_buying_power_raises():
    snap = AccountSnapshot(cash=1_000.0, buying_power=None)
    with pytest.raises(ValueError):
        build_base_snapshot(snap, {}, [], valuation_mode=VALUATION_MODE_MARKET)


def test_build_base_snapshot_with_no_snapshot_at_all_raises():
    with pytest.raises(ValueError):
        build_base_snapshot(None, {}, [], valuation_mode=VALUATION_MODE_MARKET)


# ---------------------------------------------------------------------------
# W1: VALUE (market) is the default valuation, and a HELD symbol with no quote
# blocks a market-mode submission.
# ---------------------------------------------------------------------------


def test_base_snapshot_defaults_to_market_valuation():
    """The shipped default is what the account gets when nobody touches the toggle,
    and the requirement is "allocate by VALUE". Cost mode understates the base by
    the whole unrealised P&L, so it tops winners up instead of trimming them."""
    base = BaseSnapshot(available_buying_power=1.0, managed_value=0.0,
                        base_notional=1.0, default_bp_factor=1.0)
    assert base.valuation_mode == VALUATION_MODE_MARKET


def test_build_base_snapshot_requires_an_explicit_valuation_mode():
    """No Python default, exactly like the three solvers. A silent default here and
    a different one at the solver is how the base and the deltas end up measured
    with two different definitions of "current value"."""
    snap = AccountSnapshot(buying_power=1_000.0, margin_multiplier=1.0)
    with pytest.raises(TypeError):
        build_base_snapshot(snap, {}, [])


def test_a_held_symbol_with_no_price_is_reported_by_the_base_snapshot():
    """In market mode a held position with no quote contributes 0 to the base, which
    silently shrinks every other label's target. Measured: a 5,000 position with a
    failed quote takes the base from 10,000 to 5,000 and halves every target."""
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=1.0)
    current = {"DARK": PositionState(symbol="DARK", quantity=100.0, cost_basis=5_000.0,
                                     price=None)}

    base = build_base_snapshot(snap, current, ["DARK"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.unpriced_held_symbols == ["DARK"]
    assert base.managed_value == pytest.approx(0.0)


def test_an_unheld_symbol_with_no_price_is_not_reported():
    """Already handled correctly: the engine skips it with REASON_NO_PRICE and counts
    its share in unallocatable_pct. It corrupts no denominator, so it is a non-event."""
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=1.0)
    current = {"NEW": PositionState(symbol="NEW", quantity=0.0, cost_basis=0.0, price=None)}

    base = build_base_snapshot(snap, current, ["NEW"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.unpriced_held_symbols == []


def test_cost_mode_reports_no_unpriced_held_symbols():
    """Cost mode reads the cost basis and never looks at the price, so an absent quote
    changes no number at all. Flagging it there would be a warning with no defect."""
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=1.0)
    current = {"DARK": PositionState(symbol="DARK", quantity=100.0, cost_basis=5_000.0,
                                     price=None)}

    base = build_base_snapshot(snap, current, ["DARK"],
                               valuation_mode=VALUATION_MODE_COST)

    assert base.unpriced_held_symbols == []
    assert base.managed_value == pytest.approx(5_000.0)


def test_a_zero_or_negative_price_counts_as_no_price():
    """``current_value`` treats ``price <= 0`` exactly like ``None`` (it returns 0.0),
    so the guard has to agree with it or a 0.0 quote slips through valued at nothing."""
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=1.0)
    current = {"ZERO": PositionState(symbol="ZERO", quantity=100.0, cost_basis=5_000.0,
                                     price=0.0)}

    base = build_base_snapshot(snap, current, ["ZERO"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.unpriced_held_symbols == ["ZERO"]


def test_a_short_position_with_no_price_is_reported_too():
    """A short carries a NEGATIVE quantity and a negative value, so it moves the base
    just as much as a long does. Testing ``quantity > 0`` would miss it."""
    snap = AccountSnapshot(buying_power=5_000.0, margin_multiplier=1.0)
    current = {"SHRT": PositionState(symbol="SHRT", quantity=-50.0, cost_basis=-2_000.0,
                                     price=None)}

    base = build_base_snapshot(snap, current, ["SHRT"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.unpriced_held_symbols == ["SHRT"]


def test_held_no_price_block_names_every_symbol_and_says_what_it_costs():
    from ba2_common.core.portfolio_allocation import held_no_price_block

    message = held_no_price_block(["DARK", "OTHER"])

    assert message is not None
    assert "DARK" in message and "OTHER" in message
    assert "cost basis" in message      # the escape hatch, named


def test_held_no_price_block_is_none_when_every_held_symbol_has_a_quote():
    from ba2_common.core.portfolio_allocation import held_no_price_block

    assert held_no_price_block([]) is None
    assert held_no_price_block(None) is None


def test_the_held_no_price_block_is_a_BLOCKING_message():
    """It must not fall through ``ADVISORY_MESSAGE_FRAGMENTS``: the whole point is
    that it stops a submission whose base is quietly wrong."""
    from ba2_common.core.portfolio_allocation import (
        held_no_price_block, is_blocking_message,
    )

    assert is_blocking_message(held_no_price_block(["DARK"])) is True


def test_unpriced_held_symbols_keeps_the_managed_symbol_order_and_de_duplicates():
    snap = AccountSnapshot(buying_power=1.0, margin_multiplier=1.0)
    current = {
        "BBB": PositionState(symbol="BBB", quantity=1.0, cost_basis=1.0, price=None),
        "AAA": PositionState(symbol="AAA", quantity=1.0, cost_basis=1.0, price=None),
    }

    base = build_base_snapshot(snap, current, ["BBB", "AAA", "BBB"],
                               valuation_mode=VALUATION_MODE_MARKET)

    assert base.unpriced_held_symbols == ["BBB", "AAA"]


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
    # SIGNED, and over the budget the footer divides. Negative because a buy
    # CONSUMES buying power: this fixture's plan carries no released_buying_power,
    # so the denominator is still the published 10,000.
    assert nvda["bp_usage_pct"] == pytest.approx(-72.0)  # -7200 of 10000
    assert nvda["bp_effect"] == pytest.approx(-7200.0)
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
    # $1.20, not $1.95: at the 2x bound one whole $3 share is 154% of a $1.95 target
    # and now FILLS, so that target no longer produces the suppressed row this test
    # is about. $1.20 puts one share at 250%, still outside the bound.
    plan = _one_symbol_plan(price=3.0, target_notional=1.20, margin=margin,
                            allow_fractional=True)

    rows = dry_run_rows(plan)
    assert [r["symbol"] for r in rows] == ["PENNY"]
    row = rows[0]
    assert row["suppressed"] is True
    assert row["quantity"] == 0.0
    assert row["side"] == ""            # no order, so no side
    # D1 owns this case now and prices it: no fraction can be sent under the $5
    # floor, and one whole share of a $3 stock is 250% of a $1.20 target -- outside
    # the 2x bump guard.
    assert "$5 fractional minimum" in row["reasons"]
    assert "250% of target" in row["reasons"]


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
    plan = _one_symbol_plan(price=3.0, target_notional=1.20, margin=margin,
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


# ---------------------------------------------------------------------------
# W2: "Load last" -- one generation of what the user allocated with before.
# Pure carriers on the two dataclasses; the solvers must ignore them.
# ---------------------------------------------------------------------------


def test_a_label_target_carries_no_previous_target_by_default():
    """NULL, not 0.0: "there is no last" and "last time this got nothing" are
    different answers, and only the first disables the button."""
    assert LabelTarget("A", 10.0).previous_target_pct is None
    assert SymbolTarget("AAA", 10.0).previous_weight_pct is None


def test_the_solvers_ignore_the_previous_fields_entirely():
    """Pure carriers. If a previous target could reach the arithmetic, a Load-last
    button would change a plan just by being available."""
    current = {"AAA": PositionState(symbol="AAA", quantity=0.0, cost_basis=0.0,
                                    price=10.0)}
    margin = {"AAA": MarginInfo(symbol="AAA", fractionable=False, bp_factor=1.0)}

    def _plan(previous):
        labels = [LabelTarget("A", 100.0,
                              [SymbolTarget("AAA", 100.0, previous_weight_pct=previous)],
                              )]
        labels[0].previous_target_pct = previous
        return compute_allocation(1_000.0, 1_000.0, labels, current, margin,
                                  allow_fractional=False, default_bp_factor=1.0,
                                  valuation_mode=VALUATION_MODE_MARKET)

    assert _plan(None).to_dict() == _plan(3.0).to_dict()


def test_load_previous_targets_restores_the_percentages_of_the_last_run():
    labels = [LabelTarget("A", 70.0, previous_target_pct=60.0),
              LabelTarget("B", 30.0, previous_target_pct=40.0)]

    out = pa.load_previous_targets(labels)

    assert [t.target_pct for t in out] == [60.0, 40.0]
    # The originals are not mutated -- the dialog can still cancel.
    assert [t.target_pct for t in labels] == [70.0, 30.0]


def test_load_previous_targets_leaves_a_label_with_no_history_where_it_is():
    """A partial history is the normal state -- a label added yesterday has none.
    Zeroing it would silently unallocate a real basket."""
    labels = [LabelTarget("A", 70.0, previous_target_pct=60.0),
              LabelTarget("B", 30.0)]

    out = pa.load_previous_targets(labels)

    assert [t.target_pct for t in out] == [60.0, 30.0]


def test_load_previous_targets_restores_a_previous_zero():
    """0.0 is a real prior state, so it must survive the ``is not None`` test that
    a truthiness check would swallow."""
    out = pa.load_previous_targets([LabelTarget("A", 70.0, previous_target_pct=0.0)])
    assert out[0].target_pct == 0.0


def test_load_previous_targets_keeps_the_history_so_the_button_stays_usable():
    out = pa.load_previous_targets([LabelTarget("A", 70.0, previous_target_pct=60.0)])
    assert out[0].previous_target_pct == 60.0


def test_load_previous_targets_gives_each_copy_its_own_symbol_list():
    labels = [LabelTarget("A", 70.0, [SymbolTarget("AAA", 100.0)],
                          previous_target_pct=60.0)]
    out = pa.load_previous_targets(labels)
    out[0].symbols.append(SymbolTarget("BBB", 0.0))
    assert [st.symbol for st in labels[0].symbols] == ["AAA"]


def test_load_previous_targets_of_nothing_is_empty():
    assert pa.load_previous_targets([]) == []
    assert pa.load_previous_targets(None) == []


def test_has_previous_targets_is_the_buttons_enabled_state():
    assert pa.has_previous_targets([LabelTarget("A", 10.0)]) is False
    assert pa.has_previous_targets([LabelTarget("A", 10.0, previous_target_pct=0.0)]) is True
    assert pa.has_previous_targets([]) is False
    assert pa.has_previous_targets(None) is False


def test_load_previous_symbol_weights_restores_one_labels_weights():
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 80.0, previous_weight_pct=30.0),
        SymbolTarget("BBB", 20.0, previous_weight_pct=70.0)])

    out = pa.load_previous_symbol_weights(label)

    assert [(st.symbol, st.weight_pct) for st in out.symbols] == [("AAA", 30.0),
                                                                  ("BBB", 70.0)]
    # The original label object the dialog is editing is untouched.
    assert [st.weight_pct for st in label.symbols] == [80.0, 20.0]


def test_load_previous_symbol_weights_leaves_a_symbol_with_no_history_alone():
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 80.0, previous_weight_pct=30.0),
        SymbolTarget("BBB", 20.0)])

    out = pa.load_previous_symbol_weights(label)

    assert [st.weight_pct for st in out.symbols] == [30.0, 20.0]


def test_load_previous_symbol_weights_keeps_the_labels_own_target():
    label = LabelTarget("A", 42.0, [SymbolTarget("AAA", 80.0, previous_weight_pct=30.0)],
                        previous_target_pct=11.0)
    out = pa.load_previous_symbol_weights(label)
    assert out.target_pct == 42.0
    assert out.previous_target_pct == 11.0


def test_load_previous_symbol_weights_of_an_empty_label_is_an_empty_label():
    out = pa.load_previous_symbol_weights(LabelTarget("A", 100.0))
    assert out.symbols == []


def test_has_previous_symbol_weights_is_the_per_label_buttons_enabled_state():
    assert pa.has_previous_symbol_weights(LabelTarget("A", 100.0)) is False
    assert pa.has_previous_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 80.0)])) is False
    assert pa.has_previous_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 80.0, previous_weight_pct=0.0)])) is True


# ---------------------------------------------------------------------------
# The per-label "Even split" for SYMBOLS -- step 2's pair to ``even_split_targets``.
# ---------------------------------------------------------------------------


def test_even_split_symbol_weights_gives_every_symbol_an_equal_share():
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 80.0), SymbolTarget("BBB", 20.0)])

    out = pa.even_split_symbol_weights(label)

    assert [(st.symbol, st.weight_pct) for st in out.symbols] == [("AAA", 50.0),
                                                                  ("BBB", 50.0)]
    # The original label object the dialog is editing is untouched, exactly as
    # ``load_previous_symbol_weights`` leaves it -- Cancel must still mean cancel.
    assert [st.weight_pct for st in label.symbols] == [80.0, 20.0]


def test_even_split_symbol_weights_delegates_to_even_split_pct_at_six():
    """The anti-divergence pin, and SIX is the count that catches it.

    A hand-rolled ``round(100 / n, 2)`` agrees with ``even_split_pct`` at n=2, 3
    and 5, so a test at those counts proves nothing. At n=6 they part company:
    ``even_split_pct`` floors to 16.66 and puts 16.70 on the last slot, the
    hand-roll rounds to 16.67 and leaves 16.65 (or, flat, totals 100.02 and fails
    the validator outright). Sharing ONE splitter with
    ``build_symbol_targets`` is what stops the wizard's button and the stored
    default disagreeing about the same six symbols.
    """
    symbols = [f"S{i}" for i in range(6)]
    label = LabelTarget("A", 100.0, [SymbolTarget(s, 0.0) for s in symbols])

    out = pa.even_split_symbol_weights(label)
    weights = [st.weight_pct for st in out.symbols]

    assert weights == pa.even_split_pct(6)
    assert weights == [16.66, 16.66, 16.66, 16.66, 16.66, 16.7]
    assert sum(weights) == 100.0


def test_even_split_symbol_weights_is_byte_identical_to_the_stored_default():
    """The button must land on the numbers ``build_symbol_targets`` would have
    produced for the same symbols, at EVERY count -- otherwise pressing it on an
    untouched label silently rewrites the weights it is showing."""
    for count in range(1, 16):
        symbols = [f"S{i}" for i in range(count)]
        label = LabelTarget("A", 100.0, [SymbolTarget(s, 1.0) for s in symbols])

        pressed = [st.weight_pct for st in pa.even_split_symbol_weights(label).symbols]
        default = [st.weight_pct for st in pa.build_symbol_targets(symbols)]

        assert pressed == default, count


def test_even_split_symbol_weights_always_totals_exactly_one_hundred():
    """The rule ``validate_symbol_weights`` enforces, at every count the button
    can be pressed at -- an even split is exactly where a naive round misses it."""
    for count in range(1, 16):
        label = LabelTarget("A", 100.0,
                            [SymbolTarget(f"S{i}", 0.0) for i in range(count)])
        assert pa.validate_symbol_weights(pa.even_split_symbol_weights(label)) == [], count


def test_even_split_symbol_weights_keeps_the_labels_own_target():
    """Step 2 is about shares WITHIN a label; the label itself must not move."""
    label = LabelTarget("A", 42.0, [SymbolTarget("AAA", 80.0), SymbolTarget("BBB", 20.0)],
                        previous_target_pct=11.0)

    out = pa.even_split_symbol_weights(label)

    assert out.target_pct == 42.0
    assert out.previous_target_pct == 11.0
    assert out.label == "A"


def test_even_split_symbol_weights_keeps_the_history_and_the_comments():
    """``previous_weight_pct`` is what step 2's Load last reads. An even split that
    dropped it would disable the button beside it as a side effect."""
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 80.0, "core holding", 30.0),
        SymbolTarget("BBB", 20.0, "hedge", 70.0)])

    out = pa.even_split_symbol_weights(label)

    assert [st.previous_weight_pct for st in out.symbols] == [30.0, 70.0]
    assert [st.comment for st in out.symbols] == ["core holding", "hedge"]
    assert pa.has_previous_symbol_weights(out) is True


def test_even_split_symbol_weights_of_an_empty_label_is_an_empty_label():
    """``even_split_pct(0)`` is [], not a ZeroDivisionError, and this inherits it."""
    out = pa.even_split_symbol_weights(LabelTarget("A", 100.0))
    assert out.symbols == []
    assert out.label == "A"


def test_can_even_split_symbols_is_the_per_label_buttons_enabled_state():
    """Two or more. A label with none has nothing to split, and a label with one
    already owns the whole 100 by construction -- both are drawn DISABLED rather
    than hidden, so the feature is discoverable where it does not apply."""
    assert pa.can_even_split_symbols(None) is False
    assert pa.can_even_split_symbols(LabelTarget("A", 100.0)) is False
    assert pa.can_even_split_symbols(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 100.0)])) is False
    assert pa.can_even_split_symbols(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0),
                                 SymbolTarget("BBB", 40.0)])) is True


def test_even_split_symbol_weights_is_exported():
    """The wizard imports both by name from the package's public surface."""
    assert "even_split_symbol_weights" in pa.__all__
    assert "can_even_split_symbols" in pa.__all__


# ---------------------------------------------------------------------------
# ``split_pct_across`` -- the one splitter both "Even split" and "Fill rest" use.
# ---------------------------------------------------------------------------


def test_split_pct_across_is_even_split_pct_generalised_to_any_total():
    """``even_split_pct(n)`` IS ``split_pct_across(100, n)``, at every count.

    The point of hoisting the primitive rather than writing a second one: "fill
    what is left" and "split the whole 100" are the same arithmetic over a
    different total, and two implementations of it would eventually disagree about
    the same symbols. Checked to 60 because the divergence counts are sparse --
    6, 7, 15 and friends -- and a spot check at 2, 3 and 5 proves nothing.
    """
    for count in range(0, 61):
        assert pa.split_pct_across(100.0, count) == pa.even_split_pct(count), count


def test_split_pct_across_puts_the_residual_on_the_last_slot():
    """The awkward totals, pinned by value. 40 across 3 is 13.33 / 13.33 / 13.34 --
    NOT 13.33 x 3 (totals 39.99) and not 13.34 x 3 (totals 40.02)."""
    assert pa.split_pct_across(40.0, 3) == [13.33, 13.33, 13.34]
    assert pa.split_pct_across(100.0, 6) == [16.66, 16.66, 16.66, 16.66, 16.66, 16.7]
    assert pa.split_pct_across(0.01, 3) == [0.0, 0.0, 0.01]
    assert pa.split_pct_across(37.5, 1) == [37.5]
    assert pa.split_pct_across(10.0, 0) == []
    assert pa.split_pct_across(0.0, 3) == [0.0, 0.0, 0.0]


def test_split_pct_across_totals_its_input_exactly_in_decimal():
    """Exact in DECIMAL, which is the arithmetic the two-decimal weights actually
    live in. Binary float addition of the parts drifts by ~1e-14, which is 12
    orders of magnitude inside ``LABEL_TOTAL_TOLERANCE_PCT``; the discipline being
    proved here is that no CENT goes missing."""
    from decimal import Decimal

    for count in range(1, 21):
        for hundredths in range(1, 10_001, 137):
            total = hundredths / 100.0
            parts = pa.split_pct_across(total, count)
            assert sum(Decimal(str(p)) for p in parts) == Decimal(str(total)), \
                (total, count)


# ---------------------------------------------------------------------------
# The per-label "Fill rest evenly" -- spread what is left over the empty slots.
# ---------------------------------------------------------------------------


def test_fill_remaining_symbol_weights_spreads_the_remainder_over_the_zeros():
    """Type a few by hand, press the button, and the rest share what is left.

    60 is spoken for, so AAA / BBB / CCC divide 40: 13.33 / 13.33 / 13.34.
    """
    label = LabelTarget("A", 100.0, [
        SymbolTarget("MANUAL", 60.0),
        SymbolTarget("AAA", 0.0), SymbolTarget("BBB", 0.0), SymbolTarget("CCC", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert [(st.symbol, st.weight_pct) for st in out.symbols] == [
        ("MANUAL", 60.0), ("AAA", 13.33), ("BBB", 13.33), ("CCC", 13.34)]
    # The label the dialog is editing is untouched -- Cancel must still mean
    # cancel, exactly as ``even_split_symbol_weights`` leaves it.
    assert [st.weight_pct for st in label.symbols] == [60.0, 0.0, 0.0, 0.0]


def test_fill_remaining_symbol_weights_leaves_every_non_zero_weight_exactly_as_typed():
    """The whole promise of the button: what the user typed is what survives. Not
    re-normalised, not nudged onto the 2dp grid -- 12.3456 comes back 12.3456."""
    label = LabelTarget("A", 100.0, [
        SymbolTarget("KEEP1", 12.3456), SymbolTarget("FILL1", 0.0),
        SymbolTarget("KEEP2", 7.5), SymbolTarget("FILL2", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert out.symbols[0].weight_pct == 12.3456
    assert out.symbols[2].weight_pct == 7.5
    # 100 - 19.8456 = 80.1544, rounded to the cent grid = 80.15, halved.
    assert [out.symbols[1].weight_pct, out.symbols[3].weight_pct] == [40.07, 40.08]
    assert pa.validate_symbol_weights(out) == []


def test_filling_a_completely_empty_label_is_the_even_split():
    """The cheapest correctness check there is: with nothing spoken for, "fill what
    is left across the empty slots" IS "split the 100 evenly", so the two buttons
    must land on identical numbers at EVERY count. They share ``split_pct_across``
    precisely so this cannot come apart."""
    for count in range(1, 21):
        symbols = [f"S{i}" for i in range(count)]
        empty = LabelTarget("A", 100.0, [SymbolTarget(s, 0.0) for s in symbols])

        filled = [st.weight_pct for st in pa.fill_remaining_symbol_weights(empty).symbols]
        split = [st.weight_pct for st in pa.even_split_symbol_weights(empty).symbols]

        assert filled == split, count
        assert filled == pa.even_split_pct(count), count


def test_fill_remaining_symbol_weights_treats_an_unset_weight_as_empty():
    """``SymbolTarget.weight_pct`` is typed ``float``, but the wizard's own setter
    coerces a cleared ``ui.number`` through ``float(value or 0.0)`` -- so "unset"
    and 0 are the same fact here, and a ``None`` that reaches the engine from
    anywhere else must not crash it or count as spoken for."""
    label = LabelTarget("A", 100.0, [
        SymbolTarget("MANUAL", 50.0),
        SymbolTarget("NONE1", None), SymbolTarget("ZERO1", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert [st.weight_pct for st in out.symbols] == [50.0, 25.0, 25.0]


def test_fill_remaining_symbol_weights_gives_a_single_empty_slot_the_whole_remainder():
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 30.0), SymbolTarget("BBB", 45.5), SymbolTarget("CCC", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert out.symbols[2].weight_pct == 24.5
    assert pa.validate_symbol_weights(out) == []


def test_fill_remaining_symbol_weights_output_always_passes_the_validator():
    """Across manual totals and slot counts: the filled set totals 100 to the
    validator's satisfaction every time. A naive ``round(remainder / k, 2)`` misses
    this the moment k does not divide the remainder cleanly."""
    for manual_hundredths in range(0, 10_000, 311):
        manual = manual_hundredths / 100.0
        for count in range(1, 12):
            label = LabelTarget("A", 100.0,
                                [SymbolTarget("MANUAL", manual)]
                                + [SymbolTarget(f"S{i}", 0.0) for i in range(count)])
            out = pa.fill_remaining_symbol_weights(label)
            assert pa.validate_symbol_weights(out) == [], (manual, count)


def test_fill_remaining_symbol_weights_never_writes_a_negative_when_over_allocated():
    """The manual weights already total 120. There is nothing to give away, so the
    button writes NOTHING -- it does not hand out -10 each, and it does not quietly
    rescale the numbers the user typed. ``can_fill_remaining_symbol_weights`` is
    False here, so the UI never reaches this; the engine is defensive anyway
    because a pure function that only behaves when its own predicate agrees is a
    trap for the next caller."""
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 70.0), SymbolTarget("BBB", 50.0),
        SymbolTarget("CCC", 0.0), SymbolTarget("DDD", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert [st.weight_pct for st in out.symbols] == [70.0, 50.0, 0.0, 0.0]
    assert pa.can_fill_remaining_symbol_weights(label) is False


def test_fill_remaining_symbol_weights_with_nothing_left_writes_zeros_not_negatives():
    """Manual weights total EXACTLY 100. The empty slots are already right at 0 and
    stay there -- the set is valid before and after."""
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0), SymbolTarget("CCC", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert [st.weight_pct for st in out.symbols] == [60.0, 40.0, 0.0]
    assert pa.validate_symbol_weights(out) == []


def test_fill_remaining_symbol_weights_with_no_empty_slot_changes_nothing():
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 30.0), SymbolTarget("BBB", 20.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert [st.weight_pct for st in out.symbols] == [30.0, 20.0]


def test_fill_remaining_symbol_weights_leaves_a_negative_weight_for_the_validator():
    """A negative is not "empty" -- it is a value, and the button's contract is that
    values are left alone. Rewriting it would be the button silently repairing an
    error the user needs to SEE; ``validate_symbol_weights`` owns that message."""
    label = LabelTarget("A", 100.0, [SymbolTarget("BAD", -10.0), SymbolTarget("AAA", 0.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert out.symbols[0].weight_pct == -10.0
    assert out.symbols[1].weight_pct == 110.0
    assert pa.validate_symbol_weights(out) != []


def test_fill_remaining_symbol_weights_keeps_the_labels_own_target():
    """Step 2 is about shares WITHIN a label; the label itself must not move."""
    label = LabelTarget("A", 42.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 0.0)],
                        comment="growth", previous_target_pct=11.0)

    out = pa.fill_remaining_symbol_weights(label)

    assert (out.label, out.target_pct, out.comment, out.previous_target_pct) == \
        ("A", 42.0, "growth", 11.0)


def test_fill_remaining_symbol_weights_keeps_the_history_and_the_comments():
    """``previous_weight_pct`` is what the Load-last button beside it reads. Dropping
    it would disable that button as a side effect of pressing this one."""
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 60.0, "core holding", 30.0),
        SymbolTarget("BBB", 0.0, "hedge", 70.0)])

    out = pa.fill_remaining_symbol_weights(label)

    assert [st.previous_weight_pct for st in out.symbols] == [30.0, 70.0]
    assert [st.comment for st in out.symbols] == ["core holding", "hedge"]
    assert pa.has_previous_symbol_weights(out) is True


def test_fill_remaining_symbol_weights_of_an_empty_label_is_an_empty_label():
    out = pa.fill_remaining_symbol_weights(LabelTarget("A", 100.0))
    assert out.symbols == []
    assert out.label == "A"


def test_can_fill_remaining_symbol_weights_is_the_buttons_enabled_state():
    """Two conditions, and BOTH have to hold: something to fill, and something left
    to fill it with.

    Over-allocated is DISABLED rather than a no-op click, on the Even-split
    button's terms: a control that does nothing when pressed is indistinguishable
    from a broken one, and the validator below already says why (the label totals
    120%). The user is never stuck -- Wipe is enabled in exactly that case.
    """
    assert pa.can_fill_remaining_symbol_weights(None) is False
    # No symbols at all.
    assert pa.can_fill_remaining_symbol_weights(LabelTarget("A", 100.0)) is False
    # Nothing empty to fill.
    assert pa.can_fill_remaining_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 40.0),
                                 SymbolTarget("BBB", 60.0)])) is False
    # Empty slots and 40 left over.
    assert pa.can_fill_remaining_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0),
                                 SymbolTarget("BBB", 0.0)])) is True
    # A completely empty label: the whole 100 is up for grabs.
    assert pa.can_fill_remaining_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 0.0),
                                 SymbolTarget("BBB", None)])) is True
    # Empty slot, but the manual weights already spend the whole 100.
    assert pa.can_fill_remaining_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0),
                                 SymbolTarget("CCC", 0.0)])) is False
    # Empty slot, and the manual weights are already OVER 100.
    assert pa.can_fill_remaining_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 130.0),
                                 SymbolTarget("BBB", 0.0)])) is False


def test_can_fill_remaining_symbol_weights_ignores_float_dust_in_the_manual_total():
    """7.64 + 83.57 + 8.79 is exactly 100 in decimal and 99.99999999999999 in binary,
    so the raw remainder is 1.4e-14 -- positive, and therefore "something left to
    fill" to anything that does not round first.

    The predicate rounds to the CENT before asking. Without that, a fully-spent
    label offers an enabled Fill rest that can only write 0.00 into the empty box:
    a button that does nothing when pressed, which is the exact failure the
    disabled-not-hidden rule exists to avoid. Pinned with a set that genuinely
    drifts -- 33.33 x 2 + 33.34 sums to 100.0 on the nose and proves nothing.
    """
    assert sum([7.64, 83.57, 8.79]) != 100.0                       # the premise
    label = LabelTarget("A", 100.0, [
        SymbolTarget("AAA", 7.64), SymbolTarget("BBB", 83.57),
        SymbolTarget("CCC", 8.79), SymbolTarget("DDD", 0.0)])

    assert pa.can_fill_remaining_symbol_weights(label) is False
    # And the raw, unrounded remainder really would have said otherwise.
    assert 100.0 - sum(st.weight_pct for st in label.symbols) > 0


# ---------------------------------------------------------------------------
# The per-label "Wipe" -- start this label's weights over.
# ---------------------------------------------------------------------------


def test_wipe_symbol_weights_clears_every_weight_in_the_label():
    label = LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", 40.0)])

    out = pa.wipe_symbol_weights(label)

    assert [st.weight_pct for st in out.symbols] == [0.0, 0.0]
    # A copy, like every other step-2 control: Cancel must still mean cancel.
    assert [st.weight_pct for st in label.symbols] == [60.0, 40.0]


def test_wipe_symbol_weights_writes_zero_rather_than_none():
    """``SymbolTarget.weight_pct`` is declared ``float`` and every solver does
    arithmetic on it. The wizard's own setter already turns a cleared box into 0.0,
    so 0.0 IS "empty" in this model -- writing ``None`` would buy a slightly emptier
    looking box at the cost of the field's type being a lie."""
    out = pa.wipe_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0), SymbolTarget("BBB", None)]))

    assert [st.weight_pct for st in out.symbols] == [0.0, 0.0]
    assert all(isinstance(st.weight_pct, float) for st in out.symbols)


def test_wipe_then_fill_rest_is_the_even_split():
    """The workflow the button exists for, end to end: wipe, type a couple, fill the
    rest. Wiping and filling with nothing typed lands exactly on Even split."""
    label = LabelTarget("A", 100.0, [SymbolTarget(f"S{i}", 100.0 / 6.0) for i in range(6)])

    out = pa.fill_remaining_symbol_weights(pa.wipe_symbol_weights(label))

    assert [st.weight_pct for st in out.symbols] == pa.even_split_pct(6)
    assert pa.validate_symbol_weights(out) == []


def test_wipe_symbol_weights_keeps_the_history_and_the_comments():
    """A wipe that dropped ``previous_weight_pct`` would disable Load last -- the one
    control that undoes it. That is what makes the wipe recoverable, and it is why
    it does not need a confirmation dialog."""
    label = LabelTarget("A", 42.0, [
        SymbolTarget("AAA", 60.0, "core holding", 30.0),
        SymbolTarget("BBB", 40.0, "hedge", 70.0)], previous_target_pct=11.0)

    out = pa.wipe_symbol_weights(label)

    assert [st.previous_weight_pct for st in out.symbols] == [30.0, 70.0]
    assert [st.comment for st in out.symbols] == ["core holding", "hedge"]
    assert pa.has_previous_symbol_weights(out) is True
    # And the label itself does not move: step 2 is about shares WITHIN a label.
    assert (out.target_pct, out.previous_target_pct) == (42.0, 11.0)


def test_wipe_symbol_weights_of_an_empty_label_is_an_empty_label():
    out = pa.wipe_symbol_weights(LabelTarget("A", 100.0))
    assert out.symbols == []
    assert out.label == "A"


def test_can_wipe_symbol_weights_is_the_buttons_enabled_state():
    """Enabled when there is something to destroy. A label already at all-zero has
    nothing to clear, and an empty label has no boxes at all -- DISABLED in both,
    never hidden, on the same terms as the three controls beside it."""
    assert pa.can_wipe_symbol_weights(None) is False
    assert pa.can_wipe_symbol_weights(LabelTarget("A", 100.0)) is False
    assert pa.can_wipe_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 0.0),
                                 SymbolTarget("BBB", None)])) is False
    assert pa.can_wipe_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 0.0),
                                 SymbolTarget("BBB", 100.0)])) is True
    # Over-allocated: Fill rest is disabled here, so Wipe MUST be enabled or the
    # user has no way out of a 120% label except retyping every box.
    assert pa.can_wipe_symbol_weights(
        LabelTarget("A", 100.0, [SymbolTarget("AAA", 70.0), SymbolTarget("BBB", 50.0),
                                 SymbolTarget("CCC", 0.0)])) is True


def test_an_over_allocated_label_always_offers_a_way_out():
    """The pair's escape-hatch invariant, stated directly: whenever Fill rest is
    disabled because the label is over-allocated, Wipe is enabled."""
    for weights in ([70.0, 50.0, 0.0], [120.0, 0.0], [50.0, 50.0, 0.01, 0.0]):
        label = LabelTarget("A", 100.0,
                            [SymbolTarget(f"S{i}", w) for i, w in enumerate(weights)])
        assert pa.can_fill_remaining_symbol_weights(label) is False, weights
        assert pa.can_wipe_symbol_weights(label) is True, weights


def test_fill_rest_and_wipe_are_exported():
    """The wizard imports all four by name from the package's public surface."""
    assert "fill_remaining_symbol_weights" in pa.__all__
    assert "can_fill_remaining_symbol_weights" in pa.__all__
    assert "wipe_symbol_weights" in pa.__all__
    assert "can_wipe_symbol_weights" in pa.__all__
    assert "split_pct_across" in pa.__all__


def test_load_previous_targets_output_still_passes_the_validator():
    """Whatever was last SAVED was last allocated with, so restoring it must not
    produce a set the wizard then refuses."""
    labels = [LabelTarget("A", 10.0, [SymbolTarget("AAA", 100.0)], previous_target_pct=60.0),
              LabelTarget("B", 10.0, [SymbolTarget("BBB", 100.0)], previous_target_pct=40.0)]
    assert steps_validation_messages(pa.load_previous_targets(labels)) == []


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


def test_steps_validation_includes_the_label_target_messages_too():
    """Step 1's rule (labels total EXACTLY 100) and step 2's (weights total exactly
    100 inside each label) are reported together, so Submit is blocked on either --
    and BOTH sides of step 1's total block, the under case included."""
    over = steps_validation_messages(
        [LabelTarget("A", 70.0, [SymbolTarget("AAA", 100.0)]),
         LabelTarget("B", 48.0, [SymbolTarget("BBB", 100.0)])])
    assert any("over 100%" in m for m in over)
    assert blocking_messages(over) == over

    under = steps_validation_messages([LabelTarget("A", 55.0, [SymbolTarget("AAA", 100.0)])])
    assert any("under 100%" in m for m in under)
    assert blocking_messages(under) == under

    bad_weights = steps_validation_messages(
        [LabelTarget("A", 100.0, [SymbolTarget("AAA", 60.0)])])
    assert any("must total 100%" in m for m in bad_weights)
    assert blocking_messages(bad_weights) == bad_weights


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
    """55% is no longer one of them -- see the test above -- so this uses a real
    error: a non-zero label with no symbols can absorb nothing."""
    labels = [LabelTarget("A", 100.0, [])]
    messages = steps_validation_messages(labels)
    assert messages and blocking_messages(messages) == messages


def test_a_reserve_out_of_range_blocks_alongside_the_label_messages():
    """The reserve is checked by the SAME seam the wizard already gates on, so a bad
    reserve and a bad label total surface together rather than one at a time."""
    messages = steps_validation_messages(
        [LabelTarget("A", 55.0, [SymbolTarget("AAA", 100.0)])], unallocated_pct=-5.0)

    assert len(messages) == 2
    assert any("under 100%" in m for m in messages)
    assert any("outside 0-100%" in m for m in messages)
    assert blocking_messages(messages) == messages


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
# ACTION_UNACTIONABLE: held, and every transaction behind it is one this planner
# will not act on. "Skipped / nothing to do" is what a symbol ALREADY at target
# says, so an exit that CANNOT happen must not borrow those words.
# ---------------------------------------------------------------------------

def test_decide_symbol_action_a_close_of_an_option_only_holding_is_unactionable():
    """The shares are at the broker and the user asked to exit them, but every open
    transaction for the symbol was filtered out of ``transaction_ids`` -- an assigned
    wheel is exactly this shape. ACTION_SKIP here reports "nothing to do" for a
    position the run cannot touch."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-100.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    state = PositionState(symbol="AAPL", quantity=100.0, transaction_ids=[],
                          unactionable_transaction_ids=[41, 42])
    assert decide_symbol_action(row, state) == ACTION_UNACTIONABLE


def test_decide_symbol_action_a_TRIM_of_an_option_only_holding_is_unactionable_too():
    """Same gate, other path. A partial reduction (target still > 0) leaves ``held``
    False in exactly the same way, so the trim went just as quiet as the close."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-40.0,
                        side=OrderDirection.SELL, target_quantity=60.0)
    state = PositionState(symbol="AAPL", quantity=100.0, transaction_ids=[],
                          unactionable_transaction_ids=[41])
    assert decide_symbol_action(row, state) == ACTION_UNACTIONABLE


def test_decide_symbol_action_an_untracked_broker_position_is_still_a_plain_skip():
    """DISCRIMINATOR for the ``unactionable_transaction_ids`` condition. Shares at
    the broker with no transactions of ours AT ALL is the pre-existing long-only
    refusal, not a filtered holding: there is nothing to name and nothing to look
    at, so it keeps the words it had."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-100.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    state = PositionState(symbol="AAPL", quantity=100.0, transaction_ids=[],
                          unactionable_transaction_ids=[])
    assert decide_symbol_action(row, state) == ACTION_SKIP


def test_decide_symbol_action_a_filtered_transaction_with_no_shares_held_is_a_skip():
    """DISCRIMINATOR for the ``quantity > 0`` condition, and a real position: a
    cash-secured put on a stock the account does not own has an open option
    transaction and zero shares. There is no holding to be unable to sell."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-100.0,
                        side=OrderDirection.SELL, target_quantity=0.0)
    state = PositionState(symbol="AAPL", quantity=0.0, transaction_ids=[],
                          unactionable_transaction_ids=[41])
    assert decide_symbol_action(row, state) == ACTION_SKIP


def test_decide_symbol_action_a_BUY_into_an_option_only_holding_still_opens():
    """DISCRIMINATOR for the SELL condition. The BUY side was never broken: it
    submits ``delta_quantity`` through a fresh equity transaction, which tops the
    holding up correctly and cannot double-buy. Turning it into a refusal would
    break a working path in the name of reporting."""
    row = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=25.0,
                        side=OrderDirection.BUY, target_quantity=125.0)
    state = PositionState(symbol="AAPL", quantity=100.0, transaction_ids=[],
                          unactionable_transaction_ids=[41])
    assert decide_symbol_action(row, state) == ACTION_NEW


def test_decide_symbol_action_one_equity_transaction_beside_the_options_still_trades():
    """DISCRIMINATOR for precedence: ``held`` must win. A covered call has BOTH an
    equity transaction and an option one, and the equity leg is perfectly
    actionable -- routing it to the refusal would stop a rebalance that works."""
    close = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-100.0,
                          side=OrderDirection.SELL, target_quantity=0.0)
    trim = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=-40.0,
                         side=OrderDirection.SELL, target_quantity=60.0)
    state = PositionState(symbol="AAPL", quantity=100.0, transaction_ids=[7],
                          unactionable_transaction_ids=[41])
    assert decide_symbol_action(close, state) == ACTION_CLOSE
    assert decide_symbol_action(trim, state) == ACTION_ADJUST


def test_decide_symbol_action_an_already_skipped_row_keeps_its_own_reason():
    """DISCRIMINATOR for the early return. A row the engine skipped for a REAL
    reason -- no price, precheck refusal, a suppressed sub-minimum trim -- already
    has something to say, and the refusal must not overwrite it with a story about
    option transactions."""
    state = PositionState(symbol="AAPL", quantity=100.0, transaction_ids=[],
                          unactionable_transaction_ids=[41])
    unpriced = AllocationRow(symbol="AAPL", price=None, delta_quantity=-100.0,
                             side=OrderDirection.SELL, target_quantity=0.0,
                             skipped=True, reasons=["no price - skipped"])
    zero_delta = AllocationRow(symbol="AAPL", price=160.0, delta_quantity=0.0,
                               side=OrderDirection.SELL, target_quantity=100.0)
    assert decide_symbol_action(unpriced, state) == ACTION_SKIP
    assert decide_symbol_action(zero_delta, state) == ACTION_SKIP


def test_the_four_submission_actions_are_distinct_strings():
    """They are dispatched on by value in the live service and stored in the run's
    activity JSON. Two of them collapsing onto one string would route a refusal
    into a submission path."""
    assert len({ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP,
                ACTION_UNACTIONABLE}) == 5


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


# ---------------------------------------------------------------------------
# Task 73: plan_quantity_attempts -- fractional first, one whole-share retry
# ---------------------------------------------------------------------------

def test_plan_quantity_attempts_fractional_first_then_whole_shares():
    attempts = plan_quantity_attempts(2.5, allow_fractional=True, fractionable=True)
    assert attempts == [(FRACTIONAL_PATH_FRACTIONAL, 2.5), (FRACTIONAL_PATH_WHOLE, 2.0)]


def test_plan_quantity_attempts_sub_one_share_has_no_whole_share_fallback():
    attempts = plan_quantity_attempts(0.4, allow_fractional=True, fractionable=True)
    assert attempts == [(FRACTIONAL_PATH_FRACTIONAL, 0.4)]


def test_plan_quantity_attempts_sub_one_share_without_fractional_is_skipped():
    # floor(0.4) == 0 -> nothing to attempt, which the caller reports as SKIPPED.
    assert plan_quantity_attempts(0.4, allow_fractional=False, fractionable=True) == []


def test_plan_quantity_attempts_already_whole_needs_no_fractional_attempt():
    attempts = plan_quantity_attempts(3.0, allow_fractional=True, fractionable=True)
    assert attempts == [(FRACTIONAL_PATH_WHOLE, 3.0)]


def test_plan_quantity_attempts_non_fractionable_symbol_goes_straight_to_whole():
    attempts = plan_quantity_attempts(2.5, allow_fractional=True, fractionable=False)
    assert attempts == [(FRACTIONAL_PATH_WHOLE, 2.0)]


def test_plan_quantity_attempts_uses_the_magnitude_of_a_signed_delta():
    attempts = plan_quantity_attempts(-4.0, allow_fractional=False, fractionable=False)
    assert attempts == [(FRACTIONAL_PATH_WHOLE, 4.0)]


@pytest.mark.parametrize("quantity", [2.9999999999, 3.0000000001])
def test_plan_quantity_attempts_does_not_floor_away_a_share_of_float_noise(quantity):
    """A quantity off a fractional grid lands at 2.9999999999 as readily as at
    3.0000000001, and both ARE three shares (`_is_fractional_quantity` tests
    QUANTITY_EPSILON from both sides). Flooring the low one would send two shares
    where the dry run showed three."""
    assert plan_quantity_attempts(quantity, allow_fractional=True,
                                  fractionable=True) == [(FRACTIONAL_PATH_WHOLE, 3.0)]


@pytest.mark.parametrize("quantity", [0.4, 1.0, 2.5, 7.125, 99.99999, 1234.0])
def test_plan_quantity_attempts_never_puts_a_fraction_on_the_whole_share_path(quantity):
    """L1: a fractional equity order is MARKET-only at both brokers, so the retry
    exists precisely to stop being fractional. A whole-share attempt that is not a
    whole number would be refused for the same reason as the attempt it replaced."""
    for path, attempt in plan_quantity_attempts(quantity, allow_fractional=True,
                                                fractionable=True):
        if path == FRACTIONAL_PATH_WHOLE:
            assert attempt == float(int(attempt))
            assert attempt > 0


def test_plan_quantity_attempts_offers_at_most_one_retry():
    """ONE retry. A loop that kept shrinking would walk a rejected order down to
    a single share and buy something nobody asked for."""
    assert len(plan_quantity_attempts(9.75, allow_fractional=True,
                                      fractionable=True)) == 2


def test_plan_quantity_attempts_of_zero_has_nothing_to_attempt():
    assert plan_quantity_attempts(0.0, allow_fractional=True, fractionable=True) == []


# ---------------------------------------------------------------------------
# Mixed fractional eligibility, bumps and redistribution: what the dry run shows
# ---------------------------------------------------------------------------
from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import (
    bump_notice,
    fractional_summary,
    no_order_notice,
    no_order_rows,
    redistribution_notice,
    whole_share_notice,
)


def _mixed_eligibility_plan():
    """One fractional buy, one whole-share buy, one bumped row, one refused row."""
    return AllocationPlan(
        rows=[
            AllocationRow(symbol="AAPL", price=160.0, current_quantity=0.0,
                          target_notional=1_600.0, target_quantity=10.0,
                          delta_quantity=10.0, side=OrderDirection.BUY,
                          estimated_value=1_600.0, bp_cost=1_600.0, bp_factor=1.0,
                          fractional=True, redistributed=True,
                          reasons=["fractional",
                                   "weight adjusted +9.0000 -> +10.0000 shares to "
                                   "keep label 'A' on target"]),
            AllocationRow(symbol="MSFT", price=400.0, current_quantity=0.0,
                          target_notional=1_000.0, target_quantity=2.0,
                          delta_quantity=2.0, side=OrderDirection.BUY,
                          estimated_value=800.0, bp_cost=800.0, bp_factor=1.0,
                          fractional=False, reasons=["rounded down to whole shares"]),
            AllocationRow(symbol="BUMPY", price=300.0, current_quantity=0.0,
                          target_notional=200.0, target_quantity=1.0,
                          delta_quantity=1.0, side=OrderDirection.BUY,
                          estimated_value=300.0, bp_cost=300.0, bp_factor=1.0,
                          fractional=False,
                          sizing_outcome=pa.SIZING_OUTCOME_BUMPED,
                          reasons=["target 200.00 buys 0.6667 shares at 300.00 - "
                                   "BUMPED UP to 1 share(s), 150% of target"]),
            AllocationRow(symbol="BRKA", price=650_000.0, current_quantity=0.0,
                          target_notional=260_000.0, target_quantity=0.0,
                          delta_quantity=0.0, side=None, unmet_notional=260_000.0,
                          fractional=False,
                          sizing_outcome=pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE,
                          reasons=["target 260,000.00 buys 0.4000 shares at "
                                   "650,000.00 - no order; the smallest tradeable "
                                   "order is 1 share(s), 250% of target, over the "
                                   "150% bump limit"]),
        ],
        base_notional=262_800.0,
        available_buying_power=300_000.0,
        allow_fractional=True,
        valuation_mode=VALUATION_MODE_MARKET,
    )


def test_fractional_summary_counts_fractional_and_whole_share_rows():
    summary = fractional_summary(_mixed_eligibility_plan())
    assert summary["total_rows"] == 4
    assert summary["fractional_rows"] == 1
    assert summary["whole_share_rows"] == 3
    assert summary["whole_share_symbols"] == ["BRKA", "BUMPY", "MSFT"]
    assert summary["whole_share_pct"] == pytest.approx(75.0)


def test_fractional_summary_residual_is_the_money_the_plan_is_off_target():
    """MSFT wants 1,000 and gets 800; BRKA wants 260,000 and gets nothing; BUMPY
    wants 200 and gets 300. The residual is SIGNED and nets them."""
    summary = fractional_summary(_mixed_eligibility_plan())
    assert summary["target_notional"] == pytest.approx(262_800.0)
    assert summary["projected_notional"] == pytest.approx(2_700.0)
    assert summary["residual_notional"] == pytest.approx(260_100.0)


def test_fractional_summary_counts_the_bumps_and_prices_the_over_allocation():
    summary = fractional_summary(_mixed_eligibility_plan())
    assert summary["bumped_rows"] == 1
    assert summary["bumped_notional"] == pytest.approx(100.0)
    assert summary["skipped_too_large_rows"] == 1


def test_fractional_summary_counts_the_rows_redistribution_moved():
    assert fractional_summary(_mixed_eligibility_plan())["redistributed_rows"] == 1


def test_fractional_summary_counts_the_rows_with_no_order_and_their_money():
    summary = fractional_summary(_mixed_eligibility_plan())
    assert summary["no_order_rows"] == 1
    assert summary["no_order_notional"] == pytest.approx(260_000.0)


def test_fractional_summary_counts_unknown_eligibility_separately():
    plan = _mixed_eligibility_plan()
    plan.rows[1].reasons = [pa.REASON_FRACTIONAL_UNKNOWN]
    assert fractional_summary(plan)["unknown_rows"] == 1


def test_fractional_summary_ignores_a_row_with_no_price():
    """Its whole target is already reported through unallocatable_pct; counting it
    here too would double the money the dry run calls unallocated."""
    plan = _mixed_eligibility_plan()
    plan.rows.append(AllocationRow(symbol="NOPRICE", price=None, skipped=True,
                                   target_notional=5_000.0))
    summary = fractional_summary(plan)
    assert summary["total_rows"] == 4
    assert summary["target_notional"] == pytest.approx(262_800.0)


def test_whole_share_notice_names_the_count_and_the_residual():
    notice = whole_share_notice(fractional_summary(_mixed_eligibility_plan()))
    assert "3 of 4" in notice
    assert "260,100.00" in notice


def test_whole_share_notice_is_none_when_every_row_is_fractional():
    plan = _mixed_eligibility_plan()
    plan.rows = [plan.rows[0]]
    assert whole_share_notice(fractional_summary(plan)) is None


def test_whole_share_notice_says_fractional_is_off_when_the_toggle_is_off():
    plan = _mixed_eligibility_plan()
    plan.allow_fractional = False
    for row in plan.rows:
        row.fractional = False
    notice = whole_share_notice(fractional_summary(plan))
    assert notice.startswith("Fractional shares are OFF")


def test_bump_notice_names_the_over_allocation_it_is_asking_permission_for():
    notice = bump_notice(fractional_summary(_mixed_eligibility_plan()))
    assert "1 symbol" in notice
    assert "100.00" in notice
    assert "over-allocat" in notice


def test_bump_notice_is_none_when_nothing_was_bumped():
    plan = _mixed_eligibility_plan()
    plan.rows[2].sizing_outcome = pa.SIZING_OUTCOME_NORMAL
    assert bump_notice(fractional_summary(plan)) is None


def test_no_order_notice_names_the_count_the_money_and_the_bump_limit():
    notice = no_order_notice(fractional_summary(_mixed_eligibility_plan()))
    assert "1 symbol" in notice
    assert "260,000.00" in notice
    assert "200%" in notice


def test_redistribution_notice_tells_the_user_their_weights_moved():
    notice = redistribution_notice(fractional_summary(_mixed_eligibility_plan()))
    assert "1 symbol" in notice
    assert "Weight" in notice


def test_redistribution_notice_is_none_when_nothing_moved():
    plan = _mixed_eligibility_plan()
    plan.rows[0].redistributed = False
    assert redistribution_notice(fractional_summary(plan)) is None


def test_no_order_rows_carries_the_money_the_main_table_cannot_show():
    """The main table lists what will be SENT; a refused row has no quantity, no
    side and no value to put in those columns. ``no_order_rows`` is the detail
    view: what was wanted, what will be held, and how much never left the cash.
    Selected by ``unmet_notional``, so no reason string is ever pattern-matched."""
    plan = _mixed_eligibility_plan()
    dropped = no_order_rows(plan)
    assert [r["symbol"] for r in dropped] == ["BRKA"]
    assert dropped[0]["unmet_notional"] == pytest.approx(260_000.0)
    assert dropped[0]["outcome"] == "skipped-too-large"
    assert dropped[0]["target_notional"] == pytest.approx(260_000.0)
    assert "over the 150% bump limit" in dropped[0]["reasons"]
    # And it is NOT lost from the review screen either: the table still lists it,
    # marked as carrying no order.
    table = {r["symbol"]: r for r in dry_run_rows(plan)}
    assert table["BRKA"]["suppressed"] is True
    assert table["BRKA"]["side"] == ""


def test_no_order_rows_are_biggest_first():
    plan = _mixed_eligibility_plan()
    plan.rows[1].delta_quantity = 0.0
    plan.rows[1].side = None
    plan.rows[1].unmet_notional = 1_000.0
    assert [r["symbol"] for r in no_order_rows(plan)] == ["BRKA", "MSFT"]


def test_dry_run_rows_report_the_sizing_mode_and_the_outcome_per_symbol():
    rows = {r["symbol"]: r for r in dry_run_rows(_mixed_eligibility_plan())}
    assert rows["AAPL"]["sizing"] == "fractional"
    assert rows["MSFT"]["sizing"] == "whole"
    assert rows["MSFT"]["outcome"] == "normal"
    assert rows["BUMPY"]["outcome"] == "bumped-to-1"
    assert rows["AAPL"]["redistributed"] is True


def test_dry_run_rows_carry_the_target_the_projection_and_the_residual():
    rows = {r["symbol"]: r for r in dry_run_rows(_mixed_eligibility_plan())}
    assert rows["MSFT"]["target_notional"] == pytest.approx(1_000.0)
    assert rows["MSFT"]["projected_notional"] == pytest.approx(800.0)
    assert rows["MSFT"]["residual_notional"] == pytest.approx(200.0)
    # A bump is an OVER-allocation, so its residual is negative. Visible, not hidden.
    assert rows["BUMPY"]["residual_notional"] == pytest.approx(-100.0)


def test_dry_run_rows_show_the_weight_the_plan_will_really_use():
    """D2 rewrites quantities, so the typed weight and the resulting weight are two
    different numbers and BOTH have to be on screen."""
    rows = {r["symbol"]: r for r in dry_run_rows(_mixed_eligibility_plan())}
    # Rounded to 3dp for display, like every other percentage in this table.
    assert rows["MSFT"]["weight_pct"] == pytest.approx(1_000.0 / 262_800.0 * 100.0,
                                                       abs=5e-4)
    assert rows["MSFT"]["projected_weight_pct"] == pytest.approx(
        800.0 / 262_800.0 * 100.0, abs=5e-4)
    assert rows["BUMPY"]["projected_weight_pct"] > rows["BUMPY"]["weight_pct"]


def test_dry_run_rows_weights_are_zero_when_there_is_no_base_to_divide_by():
    plan = _mixed_eligibility_plan()
    plan.base_notional = 0.0
    rows = {r["symbol"]: r for r in dry_run_rows(plan)}
    assert rows["MSFT"]["weight_pct"] == 0.0
    assert rows["MSFT"]["projected_weight_pct"] == 0.0


def test_dry_run_rows_keep_every_key_the_landed_table_already_drew():
    """The new columns are ADDITIVE. Dropping one of these silently blanks a column
    the wizard is already rendering."""
    row = dry_run_rows(_mixed_eligibility_plan())[0]
    for key in ("symbol", "side", "quantity", "price", "estimated_value", "bp_cost",
                "bp_usage_pct", "reasons", "fractional", "sized_fractional",
                "suppressed", "skipped"):
        assert key in row, key


def test_filter_plan_rows_keeps_the_valuation_mode_and_the_allocation_basis():
    """A cost-mode plan silently became market-mode when a row was un-ticked, which
    reinterprets every projected number in the footer."""
    plan = _mixed_eligibility_plan()
    plan.valuation_mode = VALUATION_MODE_COST
    plan.allocation_basis = pa.ALLOCATION_BASIS_BUDGET
    filtered = filter_plan_rows(plan, ["AAPL"])
    assert filtered.valuation_mode == VALUATION_MODE_COST
    assert filtered.allocation_basis == pa.ALLOCATION_BASIS_BUDGET


def test_a_plan_that_ends_OVER_target_reports_a_NEGATIVE_residual():
    """The residual is SIGNED. A plan whose bumps outweigh its rounding shortfalls
    is over target, and reporting the magnitude would tell the user they still have
    money to deploy when in fact they have over-committed."""
    plan = _mixed_eligibility_plan()
    plan.rows = [r for r in plan.rows if r.symbol == "BUMPY"]
    plan.base_notional = 200.0
    summary = fractional_summary(plan)
    assert summary["residual_notional"] == pytest.approx(-100.0)
    assert summary["residual_pct"] == pytest.approx(-50.0)
    assert "-100.00" in whole_share_notice(summary)


# ---------------------------------------------------------------------------
# W7 + W6: the dry run shows COST and VALUE per row, and flags leverage.
#
# The predicate is the whole point of this section, so it is spelled out once
# here. ``bp_factor = initial_margin_rate x account_multiplier`` = the dollars of
# BUYING POWER one dollar of NOTIONAL consumes, so the neutral point is 1.0 on
# EVERY live account shape:
#
#   ordinary marginable stock, Reg-T 2:1   0.500 x 2 = 1.000   dollar for dollar
#   cash account (multiplier 1)            1.000 x 1 = 1.000   dollar for dollar
#   leveraged ETF, 75% maintenance         0.750 x 2 = 1.500   BP-PENALISED
#   LAZR (hard to borrow)                  0.989 x 2 = 1.978   BP-PENALISED
#   non-marginable in a margin account     1.000 x 2 = 2.000   BP-PENALISED
#
# A HIGHER factor therefore means LESS leverage, not more. "Leveraged" in the
# user's sense -- consumes LESS buying power than its notional -- is < 1.0, and
# that is the direction ``LEVERAGE_LEVERAGED`` names.
# ---------------------------------------------------------------------------


def _margin(**kw):
    """A MarginInfo with the adapter defaults, overridden per test."""
    base = dict(symbol="X", bp_factor=1.0, marginable=True, fractionable=True)
    base.update(kw)
    return MarginInfo(**base)


def _one_symbol_labels(symbol="LAZR"):
    return [LabelTarget(label="A", target_pct=100.0,
                        symbols=[SymbolTarget(symbol=symbol, weight_pct=100.0)])]


def test_compute_allocation_carries_the_margin_facts_the_dry_run_must_show():
    """``marginable`` / ``initial_margin_rate`` / ``source`` were read at the
    solver boundary and thrown away, so the dry run could not tell a BROKER-STATED
    margin rate from the conservative account-multiplier fallback -- which is the
    difference between a real leverage flag and a red badge on every new buy."""
    margin = {"LAZR": _margin(symbol="LAZR", bp_factor=1.978, marginable=False,
                              initial_margin_rate=0.989,
                              source=pa.MARGIN_SOURCE_POSITION)}
    plan = compute_allocation(10_000.0, 20_000.0, _one_symbol_labels(),
                              {"LAZR": PositionState(symbol="LAZR", quantity=0.0,
                                                     cost_basis=0.0, price=10.0)},
                              margin, allow_fractional=False,
                              default_bp_factor=2.0,
                              valuation_mode=VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.marginable is False
    assert row.initial_margin_rate == pytest.approx(0.989)
    assert row.margin_source == pa.MARGIN_SOURCE_POSITION


def test_compute_allocation_without_margin_info_reports_the_rate_as_unpublished():
    """No MarginInfo is not "no leverage": it is "the broker did not say", and the
    row must carry that so the table can render ``?`` instead of a number."""
    plan = compute_allocation(10_000.0, 20_000.0, _one_symbol_labels(),
                              {"LAZR": PositionState(symbol="LAZR", quantity=0.0,
                                                     cost_basis=0.0, price=10.0)},
                              {}, allow_fractional=False, default_bp_factor=2.0,
                              valuation_mode=VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.initial_margin_rate is None
    assert row.margin_source == pa.MARGIN_SOURCE_DEFAULT
    assert row.marginable is True


def test_compute_label_investment_carries_the_same_margin_facts():
    """The INVEST_LABEL solver builds its rows in its own loop; widening only
    ``compute_allocation`` would leave every INVEST dry run unable to flag
    anything."""
    label = LabelTarget(label="A", target_pct=100.0,
                        symbols=[SymbolTarget(symbol="LAZR", weight_pct=100.0)])
    plan = pa.compute_label_investment(
        label, 1_000.0,
        {"LAZR": PositionState(symbol="LAZR", quantity=0.0, cost_basis=0.0,
                               price=10.0)},
        {"LAZR": _margin(symbol="LAZR", bp_factor=1.978, marginable=False,
                         initial_margin_rate=0.989,
                         source=pa.MARGIN_SOURCE_POSITION)},
        available_buying_power=20_000.0,
        allow_fractional=False, default_bp_factor=2.0,
        valuation_mode=VALUATION_MODE_MARKET)
    row = plan.rows[0]
    assert row.marginable is False
    assert row.initial_margin_rate == pytest.approx(0.989)
    assert row.margin_source == pa.MARGIN_SOURCE_POSITION


def test_allocation_row_to_dict_records_the_margin_facts_in_plan_json():
    """``plan_json`` is the only record of what a run was told about an
    instrument; a leverage badge nobody can reconstruct six months later is not
    an audit trail."""
    row = AllocationRow(symbol="LAZR", marginable=False, initial_margin_rate=0.989,
                        margin_source=pa.MARGIN_SOURCE_POSITION)
    d = row.to_dict()
    assert d["marginable"] is False
    assert d["initial_margin_rate"] == pytest.approx(0.989)
    assert d["margin_source"] == pa.MARGIN_SOURCE_POSITION


def _held_plan(**row_kw):
    """One BUY row on top of an existing holding: 10 shares at 160, basis 1,200."""
    kw = dict(symbol="AAPL", price=160.0, current_quantity=10.0,
              current_cost_basis=1_200.0, target_notional=3_200.0,
              target_quantity=20.0, delta_quantity=10.0,
              side=OrderDirection.BUY, estimated_value=1_600.0, bp_cost=1_600.0,
              bp_factor=1.0, initial_margin_rate=0.5,
              margin_source=pa.MARGIN_SOURCE_ASSET)
    kw.update(row_kw)
    return AllocationPlan(rows=[AllocationRow(**kw)], base_notional=10_000.0,
                          available_buying_power=10_000.0,
                          valuation_mode=VALUATION_MODE_MARKET)


def test_dry_run_rows_show_the_held_quantity_its_cost_and_its_value():
    """The basis the user is trading against. Without it Cost and Value have no
    denominator on screen and ``Projected`` is the only holding figure in the
    table."""
    row = dry_run_rows(_held_plan())[0]
    assert row["current_quantity"] == pytest.approx(10.0)
    assert row["current_cost_basis"] == pytest.approx(1_200.0)
    assert row["current_value"] == pytest.approx(1_600.0)


def test_dry_run_rows_report_no_current_value_rather_than_zero_when_there_is_no_price():
    """0.0 would say "this holding is worthless", which is a live-data fallback and
    a lie. None says "not measurable", which is the truth."""
    row = dry_run_rows(_held_plan(price=None))[0]
    assert row["current_value"] is None
    # The cost basis is a recorded fact and survives a missing quote.
    assert row["current_cost_basis"] == pytest.approx(1_200.0)


def test_dry_run_rows_report_both_the_projected_cost_and_the_projected_market_value():
    """``projected_notional`` silently means post-trade COST BASIS in cost mode and
    ``target_quantity x price`` in market mode -- one basis at a time, when the
    requirement is to see cost AND value side by side."""
    row = dry_run_rows(_held_plan())[0]
    # 20 shares x 160
    assert row["projected_market"] == pytest.approx(3_200.0)
    # basis 1,200 + 10 bought at 160
    assert row["projected_cost"] == pytest.approx(2_800.0)


def test_dry_run_rows_carry_the_brokers_own_fee_estimate():
    """Fees are literally cost, and the precheck's figure was captured onto the row
    and then dropped at the display boundary."""
    row = dry_run_rows(_held_plan(estimated_fees=1.37))[0]
    assert row["estimated_fees"] == pytest.approx(1.37)
    assert dry_run_rows(_held_plan())[0]["estimated_fees"] is None


# ---- the leverage predicate ------------------------------------------------


def test_an_ordinary_marginable_stock_is_not_flagged_as_leveraged():
    """0.5 x 2 = 1.0. A dollar of stock costs a dollar of buying power, which is
    the NEUTRAL case on every live account shape -- not a leverage story."""
    row = dry_run_rows(_held_plan())[0]
    assert row["bp_ratio"] == pytest.approx(1.0)
    assert row["leverage"] == pa.LEVERAGE_NONE


def test_a_bp_penalised_instrument_is_flagged_from_a_broker_published_rate():
    """LAZR: initial margin 98.9% on a 2:1 account = x1.978 buying power. It
    consumes nearly TWICE its notional, so it is the opposite of leveraged, and the
    table has to say which direction it is off neutral."""
    row = dry_run_rows(_held_plan(symbol="LAZR", bp_cost=3_164.8, bp_factor=1.978,
                                  initial_margin_rate=0.989,
                                  margin_source=pa.MARGIN_SOURCE_POSITION))[0]
    assert row["bp_ratio"] == pytest.approx(1.978)
    assert row["leverage"] == pa.LEVERAGE_PENALISED


def test_a_broker_sourced_ratio_below_one_is_the_only_real_leverage():
    """"Leveraged" in the user's sense is "consumes LESS buying power than its
    notional" -- ratio < 1.0. Unreachable on today's two adapters (Alpaca's
    marginable branch bottoms out at exactly 1.0), reachable under portfolio
    margin, and the only predicate that means what the word means."""
    row = dry_run_rows(_held_plan(bp_cost=800.0, bp_factor=0.5,
                                  initial_margin_rate=0.25,
                                  margin_source=pa.MARGIN_SOURCE_POSITION))[0]
    assert row["bp_ratio"] == pytest.approx(0.5)
    assert row["leverage"] == pa.LEVERAGE_LEVERAGED


def test_an_unheld_tastytrade_buy_is_not_flagged_as_leveraged():
    """THE GUARD. TastyTrade publishes no per-symbol margin rate for a symbol the
    account does not already hold, so its adapter returns
    ``bp_factor = multiplier = 2.0``, ``initial_margin_rate = None``,
    ``source = MARGIN_SOURCE_DEFAULT`` -- its own docstring calls this "assume no
    leverage". A first-time buy is unheld BY DEFINITION, so an unguarded
    ``ratio > 1.0`` paints every new TastyTrade position. The ratio is still
    reported; the VERDICT is withheld."""
    row = dry_run_rows(_held_plan(current_quantity=0.0, current_cost_basis=0.0,
                                  bp_cost=3_200.0, bp_factor=2.0,
                                  initial_margin_rate=None,
                                  margin_source=pa.MARGIN_SOURCE_DEFAULT))[0]
    assert row["bp_ratio"] == pytest.approx(2.0)
    assert row["leverage"] == pa.LEVERAGE_UNKNOWN
    assert row["leverage"] != pa.LEVERAGE_PENALISED


def test_a_row_with_no_published_rate_is_unknown_whatever_the_source_says():
    """First half of the guard, on its own. Alpaca stamps
    ``source = MARGIN_SOURCE_ASSET`` from asset metadata; if the rate itself is
    missing there is still no measured number to judge."""
    row = dry_run_rows(_held_plan(bp_cost=3_200.0, bp_factor=2.0,
                                  initial_margin_rate=None,
                                  margin_source=pa.MARGIN_SOURCE_ASSET))[0]
    assert row["leverage"] == pa.LEVERAGE_UNKNOWN


def test_a_row_that_fell_back_to_the_account_multiplier_is_unknown_even_with_a_rate():
    """Second half of the guard, on its own. On today's two adapters
    ``source == MARGIN_SOURCE_DEFAULT`` and ``initial_margin_rate is None`` happen
    to coincide, so testing only the pair would let a future adapter that stamps a
    fallback rate alongside the fallback source walk straight through. The SOURCE is
    the authority on whether a number was measured; the number's presence is not."""
    row = dry_run_rows(_held_plan(bp_cost=3_164.8, bp_factor=1.978,
                                  initial_margin_rate=0.989,
                                  margin_source=pa.MARGIN_SOURCE_DEFAULT))[0]
    assert row["bp_ratio"] == pytest.approx(1.978)
    assert row["leverage"] == pa.LEVERAGE_UNKNOWN


def test_a_sell_states_no_leverage_at_all():
    """``bp_cost`` is 0.0 for a sell BY CONSTRUCTION -- sells free buying power --
    so 0/notional would render every sell as infinitely leveraged."""
    row = dry_run_rows(_held_plan(delta_quantity=-5.0, side=OrderDirection.SELL,
                                  target_quantity=5.0, estimated_value=800.0,
                                  bp_cost=0.0))[0]
    assert row["bp_ratio"] is None
    assert row["leverage"] == pa.LEVERAGE_NOT_APPLICABLE


def test_the_leverage_ratio_uses_the_brokers_measured_cost_not_the_stale_factor():
    """``apply_order_impacts`` overwrites ``bp_cost`` with the broker's own figure
    and never touches ``bp_factor``, so after a precheck the factor is stale and
    only ``bp_cost / estimated_value`` is true."""
    row = dry_run_rows(_held_plan(bp_cost=3_200.0, bp_factor=1.0,
                                  initial_margin_rate=0.989,
                                  margin_source=pa.MARGIN_SOURCE_POSITION))[0]
    assert row["bp_ratio"] == pytest.approx(2.0)
    assert row["leverage"] == pa.LEVERAGE_PENALISED


def test_the_leverage_ratio_falls_back_to_the_factor_when_nothing_will_be_traded():
    """A suppressed row has no trade value to divide by, but the INSTRUMENT's
    factor is still a fact worth showing next to the reason it was suppressed."""
    plan = _held_plan(bp_factor=1.978, initial_margin_rate=0.989,
                      margin_source=pa.MARGIN_SOURCE_POSITION,
                      delta_quantity=0.0, side=None, skipped=True,
                      estimated_value=0.0, bp_cost=0.0,
                      reasons=["broker refused the order"])
    row = dry_run_rows(plan)[0]
    assert row["suppressed"] is True
    assert row["bp_ratio"] == pytest.approx(1.978)
    assert row["leverage"] == pa.LEVERAGE_PENALISED


def test_dry_run_rows_carry_the_margin_facts_the_tooltip_needs():
    """"The broker lends against this" is ``initial_margin_rate < 1.0``, which is a
    DIFFERENT statement from ``bp_factor > 1.0``: under it LAZR at rate 0.989 is
    nearly cash-collateralised, the opposite of what a bare red x1.98 suggests. The
    table cannot say so unless the rate and its provenance reach it."""
    row = dry_run_rows(_held_plan(symbol="LAZR", marginable=False, bp_cost=3_164.8,
                                  bp_factor=1.978, initial_margin_rate=0.989,
                                  margin_source=pa.MARGIN_SOURCE_POSITION))[0]
    assert row["marginable"] is False
    assert row["initial_margin_rate"] == pytest.approx(0.989)
    assert row["margin_source"] == pa.MARGIN_SOURCE_POSITION
    assert row["bp_factor"] == pytest.approx(1.978)


def test_the_leverage_verdicts_are_the_five_the_table_can_draw():
    """A sixth verdict would render as a blank cell rather than fail."""
    assert {pa.LEVERAGE_NONE, pa.LEVERAGE_LEVERAGED, pa.LEVERAGE_PENALISED,
            pa.LEVERAGE_UNKNOWN, pa.LEVERAGE_NOT_APPLICABLE} == set(
                pa.LEVERAGE_VERDICTS)
    assert len(pa.LEVERAGE_VERDICTS) == 5


def test_dry_run_rows_keep_every_key_the_landed_table_already_drew_after_widening():
    """Re-pinned with the new keys: the widening is ADDITIVE and nothing that was
    already rendered may disappear."""
    row = dry_run_rows(_mixed_eligibility_plan())[0]
    for key in ("symbol", "side", "quantity", "price", "estimated_value", "bp_cost",
                "bp_usage_pct", "reasons", "fractional", "sized_fractional",
                "suppressed", "skipped", "current_quantity", "current_cost_basis",
                "current_value", "projected_cost", "projected_market",
                "estimated_fees", "bp_factor", "bp_ratio", "leverage",
                "marginable", "initial_margin_rate", "margin_source"):
        assert key in row, key


def _reserved_eligibility_plan():
    """The same plan under a 25% reserve: base 262,800, investable 197,100.

    A fixture where the two candidate denominators are far apart, because with no
    reserve they are the SAME NUMBER -- which is why the Weight column's divisor
    was never pinned in the first place.
    """
    plan = _mixed_eligibility_plan()
    plan.reserved_pct = 25.0
    plan.reserved_notional = 65_700.0
    return plan


def test_dry_run_weights_divide_the_GROSS_base_even_under_a_reserve():
    """The Weight column is a share of ``base_notional``, not of the remainder.

    Its neighbour ``projected_weight_pct`` is a realised post-trade HOLDING and has
    no relative-weight reading at all, so the pair only means "asked -> actual" if
    both divide the same thing; and the reserve chip, ``bp_usage_pct``,
    ``residual_pct``, ``reserved_pct`` and ``unallocatable_pct`` on the same screen
    all divide the base. Switching this to the investable remainder inflates every
    weight by 1/(1-r) -- a third of the way again at a 25% reserve -- and left both
    suites green because no test drove it with a reserve at all.
    """
    rows = {r["symbol"]: r for r in dry_run_rows(_reserved_eligibility_plan())}

    assert rows["MSFT"]["weight_pct"] == pytest.approx(1_000.0 / 262_800.0 * 100.0,
                                                       abs=5e-4)
    assert rows["MSFT"]["projected_weight_pct"] == pytest.approx(
        800.0 / 262_800.0 * 100.0, abs=5e-4)
    # ...and emphatically NOT 1,000 / 197,100, which is where the remainder lands.
    assert rows["MSFT"]["weight_pct"] != pytest.approx(1_000.0 / 197_100.0 * 100.0,
                                                       abs=5e-4)


def test_no_order_rows_weights_divide_the_same_base_as_the_main_table():
    """The detail view sits under the same column heading and must not answer a
    different question from the row it is explaining."""
    dropped = {r["symbol"]: r for r in no_order_rows(_reserved_eligibility_plan())}

    assert dropped["BRKA"]["weight_pct"] == pytest.approx(
        260_000.0 / 262_800.0 * 100.0, abs=5e-4)
    assert dropped["BRKA"]["weight_pct"] != pytest.approx(
        260_000.0 / 197_100.0 * 100.0, abs=5e-4)


# ---------------------------------------------------------------------------
# Unrealised P&L (money and percent), for the wizard's step 1 / step 2 captions.
#
# The arithmetic is deliberately NOT parameterised by ``valuation_mode``: a P&L
# measured on the COST basis is identically zero, so a mode-aware "current value"
# is the one input this may never take.
# ---------------------------------------------------------------------------


def _held(symbol, quantity, cost_basis, price, market_value=None):
    return PositionState(symbol=symbol, quantity=quantity, cost_basis=cost_basis,
                         price=price, market_value=market_value)


def test_unrealised_pnl_takes_no_valuation_mode_at_all():
    """THE structural guarantee behind "the same P&L in both modes".

    In cost mode ``current_value`` IS the cost basis, so a P&L derived from it is
    0.00 on every row -- silently useless, and useless in the direction that looks
    like a fact. The only way that cannot happen is for the function not to be able
    to see the mode.
    """
    import inspect as _inspect

    assert 'valuation_mode' not in _inspect.signature(pa.unrealised_pnl).parameters


def test_unrealised_pnl_is_live_market_value_less_cost():
    pnl = pa.unrealised_pnl([_held("AAPL", 10.0, 1_500.0, 160.0)])

    assert pnl.market_value == pytest.approx(1_600.0)
    assert pnl.cost_basis == pytest.approx(1_500.0)
    assert pnl.amount == pytest.approx(100.0)
    assert pnl.pct == pytest.approx(100.0 / 1_500.0 * 100.0)
    assert (pnl.priced, pnl.unpriced) == (1, 0)


def test_unrealised_pnl_ignores_the_brokers_own_market_value_figure():
    """``PositionState.market_value`` can be stamped at a different price from
    ``price`` (a previous close, a delayed quote). ``current_value`` refuses it for
    exactly that reason, and this figure sits on the same line as that one."""
    pnl = pa.unrealised_pnl([_held("AAPL", 10.0, 1_500.0, 160.0, market_value=9_999.0)])

    assert pnl.market_value == pytest.approx(1_600.0)
    assert pnl.amount == pytest.approx(100.0)


def test_a_profitable_short_reports_a_POSITIVE_return():
    """Shorts are stored signed negative. ``market_value - cost_basis`` is already
    the right signed money; it is the DENOMINATOR that has to be absolute, or a
    short that made 300 dollars renders as -20%."""
    # Sold 10 at 150 (cost basis -1,500), now worth 120 -> market value -1,200.
    pnl = pa.unrealised_pnl([_held("TSLA", -10.0, -1_500.0, 120.0)])

    assert pnl.amount == pytest.approx(300.0)
    assert pnl.pct == pytest.approx(20.0)
    assert pnl.pct > 0


def test_a_losing_short_reports_a_negative_return():
    pnl = pa.unrealised_pnl([_held("TSLA", -10.0, -1_500.0, 180.0)])

    assert pnl.amount == pytest.approx(-300.0)
    assert pnl.pct == pytest.approx(-20.0)


def test_an_unpriced_holding_is_EXCLUDED_and_counted_never_summed_as_zero():
    """A failed quote is not a flat position and not a break-even one. Summing it
    at 0 would report the whole of its cost as a loss."""
    pnl = pa.unrealised_pnl([
        _held("AAPL", 10.0, 1_500.0, 160.0),
        _held("DARK", 5.0, 5_000.0, None),
    ])

    assert pnl.amount == pytest.approx(100.0)
    assert pnl.cost_basis == pytest.approx(1_500.0)      # DARK's 5,000 is not in here
    assert (pnl.priced, pnl.unpriced) == (1, 1)


def test_a_holding_with_no_price_has_NO_measurable_pnl():
    pnl = pa.unrealised_pnl([_held("DARK", 5.0, 5_000.0, None)])

    assert pnl.amount is None
    assert pnl.pct is None
    assert (pnl.priced, pnl.unpriced) == (0, 1)


def test_a_price_of_zero_is_no_price():
    """``current_value`` guards ``price <= 0`` for the same reason: a broker that
    answers 0.00 has not quoted the instrument."""
    pnl = pa.unrealised_pnl([_held("DARK", 5.0, 5_000.0, 0.0)])

    assert pnl.amount is None
    assert (pnl.priced, pnl.unpriced) == (0, 1)


def test_a_zero_cost_basis_leaves_the_percentage_undefined_rather_than_dividing():
    pnl = pa.unrealised_pnl([_held("GIFT", 10.0, 0.0, 5.0)])

    assert pnl.amount == pytest.approx(50.0)
    assert pnl.pct is None


def test_a_flat_or_absent_symbol_is_neither_priced_nor_unpriced():
    """``build_position_states`` returns a FLAT state for every managed symbol the
    account does not hold. Counting those as "unpriced" would put a permanent
    "3 unpriced excluded" on a perfectly healthy label."""
    pnl = pa.unrealised_pnl([None, _held("FLAT", 0.0, 0.0, None)])

    assert pnl.amount is None
    assert (pnl.priced, pnl.unpriced) == (0, 0)


def test_a_labels_percentage_is_on_SUMMED_MONEY_not_an_average_of_its_symbols():
    """A doubled 1,000 next to a flat 9,000 is +10% of the label, not +50%.

    Averaging the per-symbol percentages weights a tiny holding exactly as heavily
    as the position that dominates the label.
    """
    pnl = pa.unrealised_pnl([
        _held("WIN", 10.0, 1_000.0, 200.0),      # 2,000 now: +100%
        _held("FLATTISH", 90.0, 9_000.0, 100.0),  # 9,000 now: +0%
    ])

    assert pnl.amount == pytest.approx(1_000.0)
    assert pnl.pct == pytest.approx(10.0)
    assert pnl.pct != pytest.approx(50.0)


def test_a_hedged_label_divides_GROSS_cost_so_the_percentage_cannot_explode():
    """Long 10,000 against short 10,000 nets to a cost basis of ZERO. Dividing by
    ``abs(sum(cost))`` there is a division by zero on a label that made real money;
    ``sum(abs(cost))`` is the capital actually deployed and is what every
    single-position case already reduces to."""
    pnl = pa.unrealised_pnl([
        _held("LONG", 100.0, 10_000.0, 106.0),     # 10,600 -> +600
        _held("SHORT", -100.0, -10_000.0, 98.0),   # -9,800 -> +200
    ])

    assert pnl.cost_basis == pytest.approx(0.0)
    assert pnl.abs_cost_basis == pytest.approx(20_000.0)
    assert pnl.amount == pytest.approx(800.0)
    assert pnl.pct == pytest.approx(4.0)


def test_format_unrealised_pnl_signs_both_numbers():
    text = pa.format_unrealised_pnl(pa.unrealised_pnl([_held("A", 10.0, 1_000.0, 120.0)]))

    assert text == '+200.00 (+20.00%)'


def test_format_unrealised_pnl_signs_a_loss():
    text = pa.format_unrealised_pnl(pa.unrealised_pnl([_held("A", 10.0, 1_000.0, 80.0)]))

    assert text == '-200.00 (-20.00%)'


def test_format_unrealised_pnl_of_nothing_held_is_a_dash_never_a_zero():
    assert pa.format_unrealised_pnl(pa.unrealised_pnl([])) == pa.PNL_UNMEASURABLE_MARK
    assert '0.00' not in pa.format_unrealised_pnl(pa.unrealised_pnl([]))


def test_format_unrealised_pnl_of_an_unpriced_holding_names_the_missing_price():
    text = pa.format_unrealised_pnl(pa.unrealised_pnl([_held("DARK", 5.0, 5_000.0, None)]))

    assert text == pa.PNL_NO_PRICE_MARK
    assert '0.00' not in text


def test_format_unrealised_pnl_says_how_many_rows_it_left_out():
    text = pa.format_unrealised_pnl(pa.unrealised_pnl([
        _held("A", 10.0, 1_000.0, 120.0),
        _held("DARK", 5.0, 5_000.0, None),
    ]))

    assert text == '+200.00 (+20.00%, 1 unpriced excluded)'


def test_format_unrealised_pnl_of_a_zero_cost_holding_shows_money_and_no_percent():
    text = pa.format_unrealised_pnl(pa.unrealised_pnl([_held("GIFT", 10.0, 0.0, 5.0)]))

    assert text == '+50.00 (no cost basis)'


# ---------------------------------------------------------------------------
# ``scale_pct_to_total`` -- the PROPORTIONAL sibling of ``split_pct_across``.
#
# "Fill 100%" on the allocation page has three cases and two of them scale an
# existing set rather than splitting a fresh total across empty slots. That is a
# different apportionment, not a different ROUNDING RULE: both floor every part to
# the cent and hand the whole residual to ONE slot, so the parts sum to the target
# exactly in decimal. Writing a second rounding rule in the UI layer is what these
# tests exist to prevent.
# ---------------------------------------------------------------------------


def test_scale_pct_to_total_is_split_pct_across_when_every_share_is_equal():
    """The two primitives MUST agree wherever they overlap.

    Equal inputs make "scale proportionally to 100" and "split 100 evenly" the same
    question, and a second rounding rule is exactly the thing that answers it
    differently at n=6. Checked across the same sparse range as the splitter.
    """
    for count in range(1, 61):
        assert (pa.scale_pct_to_total([7.0] * count, 100.0)
                == pa.split_pct_across(100.0, count)), count


def test_scale_pct_to_total_scales_down_an_over_allocated_set():
    """150 / 50 is 3:1, so 100 splits 75 / 25 -- and nothing is invented."""
    assert pa.scale_pct_to_total([150.0, 50.0], 100.0) == [75.0, 25.0]


def test_scale_pct_to_total_scales_up_an_under_allocated_set():
    assert pa.scale_pct_to_total([10.0, 20.0], 100.0) == [33.33, 66.67]


def test_scale_pct_to_total_gives_the_residual_to_the_LARGEST_part():
    """NOT to the last slot, which is where ``split_pct_across`` puts it.

    The difference is load-bearing here and only here: a slot sitting at 0 is a
    symbol the user asked to hold NONE of, and last-slot residual would hand it a
    cent -- turning "sell this out" into "hold $12 of it" every time the last row
    happened to be the empty one. The cent lands where it is least significant
    instead.
    """
    assert pa.scale_pct_to_total([100.0, 50.0, 0.0], 100.0) == [66.67, 33.33, 0.0]
    # ...and a tie still goes to the LAST of the tied slots, so the equal case
    # reproduces ``split_pct_across`` exactly (the test above depends on it).
    assert pa.scale_pct_to_total([1.0, 1.0, 1.0], 100.0) == [33.33, 33.33, 33.34]


def test_scale_pct_to_total_never_promotes_a_zero_slot():
    """A 0 stays 0 at every count. Scaling is proportional: 0 x anything is 0."""
    for count in range(2, 20):
        parts = pa.scale_pct_to_total([0.0] + [3.0] * (count - 1), 100.0)
        assert parts[0] == 0.0, count
        assert round(sum(parts), 2) == 100.0, count


def test_scale_pct_to_total_totals_its_target_exactly_in_decimal():
    """The whole point: 99.99 is a set ``validate_symbol_weights`` refuses."""
    from decimal import Decimal
    for values in ([1.0, 1.0, 1.0], [10.0, 20.0, 30.0], [7.0] * 6, [0.01, 99.0],
                   [33.33, 33.33, 33.34], [5.0] * 7, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]):
        parts = pa.scale_pct_to_total(values, 100.0)
        assert sum(Decimal(str(p)) for p in parts) == Decimal('100')


def test_scale_pct_to_total_falls_back_to_the_EVEN_split_when_there_is_nothing_to_scale():
    """An all-zero set has no proportions, so "scale it to 100" is meaningless.

    It is answered with the even split rather than a ZeroDivisionError or a set of
    zeros, and it is the SAME function -- so "all symbols empty" reaches the same
    numbers whether the page routes it here or through ``split_pct_across``.
    """
    assert pa.scale_pct_to_total([0.0, 0.0, 0.0], 100.0) == pa.split_pct_across(100.0, 3)


def test_scale_pct_to_total_returns_an_empty_list_for_an_empty_set():
    assert pa.scale_pct_to_total([], 100.0) == []


def test_scale_pct_to_total_handles_a_single_slot():
    assert pa.scale_pct_to_total([37.5], 100.0) == [100.0]
    assert pa.scale_pct_to_total([0.0], 100.0) == [100.0]


def test_scale_pct_to_total_is_exported():
    assert "scale_pct_to_total" in pa.__all__


def test_scale_pct_to_total_reads_a_MISSING_share_as_zero_not_as_one():
    """Found by mutation: ``float(v or 0.0)`` weakened to ``float(v if v is not
    None else 1.0)``.

    ``SymbolTarget.weight_pct`` is declared float but a cleared ``ui.number`` yields
    ``None``, and ``_symbol_weight`` already says "'Unset' and 0 are one fact here".
    Reading an unset slot as 1% would give it a share of the rescale -- a symbol the
    user emptied quietly holding money again.
    """
    assert pa.scale_pct_to_total([None, 3.0, 3.0], 100.0) == \
        pa.scale_pct_to_total([0.0, 3.0, 3.0], 100.0)
    assert pa.scale_pct_to_total([None, 3.0, 3.0], 100.0)[0] == 0.0


def test_scale_pct_to_total_honours_a_total_OTHER_than_100():
    """Survivor: ``total_pct`` ignored in the body, twice -- every call site passes
    100, so the parameter was free to be a lie.

    It is not decoration: "scale this label's symbols into the 60% the label itself
    holds" is the same arithmetic, and the next caller will want it.
    """
    assert pa.scale_pct_to_total([1.0, 1.0], 50.0) == [25.0, 25.0]
    assert pa.scale_pct_to_total([3.0, 1.0], 40.0) == [30.0, 10.0]
    assert pa.scale_pct_to_total([1.0, 1.0, 1.0], 10.0) == [3.33, 3.33, 3.34]
    # ...including on the all-zero fallback, which routes through the splitter.
    assert pa.scale_pct_to_total([0.0, 0.0], 50.0) == pa.split_pct_across(50.0, 2)
