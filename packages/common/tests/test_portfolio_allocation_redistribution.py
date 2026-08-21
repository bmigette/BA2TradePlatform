"""D2: the label rounding residual is redistributed inside the label, both ways.

Pure-function tests. Several build an AllocationPlan by hand and call
``redistribute_label_residuals`` directly -- that is the only way to pin the
individual clamps (buying power, the broker minimum, the no-negative rule) without
constructing an elaborate portfolio for each one.
"""
import json

import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, LabelTarget, PositionState, SymbolTarget,
)
from ba2_common.core.account_types import MarginInfo
from ba2_common.core.types import OrderDirection


def _pos(symbol, price, quantity=0.0, cost_basis=0.0):
    return PositionState(symbol=symbol, quantity=quantity, cost_basis=cost_basis, price=price)


def _frac(symbol, increment=None):
    return MarginInfo(symbol=symbol, bp_factor=1.0, fractionable=True,
                      min_trade_increment=increment)


def _whole(symbol, min_order_size=None):
    return MarginInfo(symbol=symbol, bp_factor=1.0, fractionable=False,
                      min_order_size=min_order_size)


def _buy_row(symbol, price, delta, *, fractional=False, current_quantity=0.0,
             current_cost_basis=0.0, target_notional=0.0, labels=("A",)):
    side = (OrderDirection.BUY if delta > 0
            else OrderDirection.SELL if delta < 0 else None)
    return AllocationRow(
        symbol=symbol, labels=list(labels), price=price,
        current_quantity=current_quantity, current_cost_basis=current_cost_basis,
        target_notional=target_notional,
        target_quantity=current_quantity + delta, delta_quantity=delta, side=side,
        estimated_value=abs(delta) * price,
        bp_cost=(delta * price if delta > 0 else 0.0), bp_factor=1.0,
        fractional=fractional)


# ---------------------------------------------------------------------------
# The measurement the residual is computed from
# ---------------------------------------------------------------------------

def test_projected_value_in_market_mode_is_the_post_trade_holding_at_price():
    row = _buy_row("X", 400.0, 2.0)
    assert pa.projected_value(row, pa.VALUATION_MODE_MARKET) == pytest.approx(800.0)


def test_projected_value_in_cost_mode_adds_the_buy_at_price_to_the_basis():
    row = _buy_row("X", 100.0, 3.0, current_quantity=5.0, current_cost_basis=400.0)
    assert pa.projected_value(row, pa.VALUATION_MODE_COST) == pytest.approx(700.0)


def test_projected_value_in_cost_mode_removes_average_cost_on_a_sell():
    """Basis 400 over 5 shares = 80/share; selling 2 leaves 240, not 400 - 2*price."""
    row = _buy_row("X", 100.0, -2.0, current_quantity=5.0, current_cost_basis=400.0)
    assert pa.projected_value(row, pa.VALUATION_MODE_COST) == pytest.approx(240.0)


def test_projected_value_of_a_priceless_row_is_none_never_zero():
    assert pa.projected_value(AllocationRow(symbol="X", price=None),
                              pa.VALUATION_MODE_MARKET) is None


def test_projected_value_rejects_an_unknown_valuation_mode():
    with pytest.raises(ValueError):
        pa.projected_value(_buy_row("X", 100.0, 1.0), "nominal")


def test_allocated_value_in_the_budget_basis_is_the_money_deployed():
    """An INVEST_LABEL target is money to ADD, so the comparable figure is the money
    this row deploys -- NOT the post-trade holding, which includes what was already
    owned."""
    row = _buy_row("X", 100.0, 3.0, current_quantity=5.0, current_cost_basis=400.0)
    assert pa.allocated_value(row, pa.VALUATION_MODE_MARKET,
                              pa.ALLOCATION_BASIS_BUDGET) == pytest.approx(300.0)
    assert pa.allocated_value(row, pa.VALUATION_MODE_MARKET,
                              pa.ALLOCATION_BASIS_POSITION) == pytest.approx(800.0)


def test_allocated_value_rejects_an_unknown_basis():
    with pytest.raises(ValueError):
        pa.allocated_value(_buy_row("X", 100.0, 1.0), pa.VALUATION_MODE_MARKET, "vibes")


# ---------------------------------------------------------------------------
# Positive residual: the label is short, the shortfall is deployed
# ---------------------------------------------------------------------------

def test_a_whole_share_shortfall_is_absorbed_by_the_fractionable_sibling():
    """XXX floors to 1 share of a 500 target; FRAC picks up the missing 200 so the
    LABEL deploys its full 1,000."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 50.0),
                                       SymbolTarget("FRAC", 50.0)])]
    current = {"XXX": _pos("XXX", 300.0), "FRAC": _pos("FRAC", 100.0)}
    margin = {"XXX": _whole("XXX"), "FRAC": _frac("FRAC")}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert by["XXX"].delta_quantity == 1.0
    assert by["FRAC"].delta_quantity == pytest.approx(7.0)
    assert by["FRAC"].redistributed is True
    assert by["XXX"].redistributed is False
    assert sum(r.estimated_value for r in plan.rows) == pytest.approx(1_000.0)
    assert any("weight adjusted" in r for r in by["FRAC"].reasons)
    assert plan.warnings == []


def test_the_user_typed_weight_is_kept_next_to_the_redistributed_quantity():
    """target_notional is NEVER rewritten -- the dry run has to show both numbers."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 50.0),
                                       SymbolTarget("FRAC", 50.0)])]
    current = {"XXX": _pos("XXX", 300.0), "FRAC": _pos("FRAC", 100.0)}
    margin = {"XXX": _whole("XXX"), "FRAC": _frac("FRAC")}
    row = {r.symbol: r for r in pa.compute_allocation(
        1_000.0, 1_000_000.0, labels, current, margin, allow_fractional=True,
        default_bp_factor=1.0, valuation_mode=pa.VALUATION_MODE_MARKET).rows}["FRAC"]
    assert row.target_notional == pytest.approx(500.0)      # what the user typed
    assert row.estimated_value == pytest.approx(700.0)      # what will be traded


# ---------------------------------------------------------------------------
# Negative residual: D1's bumps pushed the label OVER, so weight comes back off
# ---------------------------------------------------------------------------

def test_a_bump_pushes_the_label_over_target_and_the_excess_is_taken_back_off():
    """BIG's 200 target bumps to one 300 share (150%, on the bound). The label is now
    100 over, so FRAC gives one share back. Redistribution is BIDIRECTIONAL."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("BIG", 20.0),
                                       SymbolTarget("FRAC", 80.0)])]
    current = {"BIG": _pos("BIG", 300.0), "FRAC": _pos("FRAC", 100.0)}
    margin = {"BIG": _whole("BIG"), "FRAC": _frac("FRAC")}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert by["BIG"].sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    assert by["BIG"].delta_quantity == 1.0
    assert by["FRAC"].delta_quantity == pytest.approx(7.0)   # 8 rounded, minus one
    assert sum(r.estimated_value for r in plan.rows) == pytest.approx(1_000.0)


def test_a_bumped_row_is_never_the_row_that_gives_the_weight_back():
    """The obvious absorber for a bump-induced overshoot is the bumped row itself,
    and taking it back to zero re-creates the floor-to-zero D1 exists to remove --
    in a loop. Bumps are one-way."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("BIG", 20.0),
                                       SymbolTarget("WHOLE", 80.0)])]
    current = {"BIG": _pos("BIG", 300.0), "WHOLE": _pos("WHOLE", 790.0)}
    margin = {"BIG": _whole("BIG"), "WHOLE": _whole("WHOLE")}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert by["BIG"].delta_quantity == 1.0        # still bumped
    assert by["BIG"].redistributed is False
    assert by["WHOLE"].delta_quantity == 1.0      # 800 target, one 790 share
    # 1,090 deployed against a 1,000 target: over, and nothing may give it back.
    assert sum(r.estimated_value for r in plan.rows) == pytest.approx(1_090.0)


# ---------------------------------------------------------------------------
# Whole-share absorbers move in lumps and never cross the target
# ---------------------------------------------------------------------------

def test_a_whole_share_sibling_absorbs_one_lump_and_stops_short_of_crossing():
    """575 each on a 100 grid is 5 shares each and 150 left over. One more share
    fits; the remaining 50 does not, and buying it would OVERSHOOT the label."""
    labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 50.0),
                                       SymbolTarget("BBB", 50.0)])]
    current = {"AAA": _pos("AAA", 100.0), "BBB": _pos("BBB", 100.0)}
    margin = {"AAA": _whole("AAA"), "BBB": _whole("BBB")}
    plan = pa.compute_allocation(1_150.0, 1_000_000.0, labels, current, margin,
                                 allow_fractional=False, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert sorted([by["AAA"].delta_quantity, by["BBB"].delta_quantity]) == [5.0, 6.0]
    assert sum(r.estimated_value for r in plan.rows) == pytest.approx(1_100.0)
    assert plan.warnings == []          # 50 left is under one share: arithmetic, not a fault


# ---------------------------------------------------------------------------
# The clamps
# ---------------------------------------------------------------------------

def test_redistribution_never_drives_a_position_negative_or_liquidates_it():
    """A 300 overshoot against a row holding 3 shares: it may give two back, never
    all three. Exiting a symbol is a position decision, not a rounding fix -- and
    the 100 it could not give back is reported instead of being taken anyway."""
    row = _buy_row("HELD", 100.0, 0.0, current_quantity=3.0, target_notional=0.0)
    plan = AllocationPlan(rows=[row], base_notional=300.0,
                          available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(plan, {"A": {"HELD": 0.0}},
                                           {"HELD": _whole("HELD")},
                                           allow_fractional=False)
    assert row.delta_quantity == -2.0
    assert row.target_quantity == 1.0
    assert left["A"] == pytest.approx(-100.0)
    assert any("can absorb the rest" in w for w in plan.warnings)


def test_redistribution_cannot_sell_a_symbol_that_is_not_held():
    row = _buy_row("FLAT", 100.0, 0.0, target_notional=0.0)
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    pa.redistribute_label_residuals(plan, {"A": {"FLAT": -500.0}},
                                    {"FLAT": _whole("FLAT")}, allow_fractional=False)
    assert row.delta_quantity == 0.0
    assert row.side is None


def test_redistribution_never_exceeds_the_available_buying_power():
    """Headroom is 50 of a 100 share, so nothing moves; at 150 exactly one does."""
    row = _buy_row("AAA", 100.0, 2.0, target_notional=200.0)
    plan = AllocationPlan(rows=[row], available_buying_power=250.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    pa.redistribute_label_residuals(plan, {"A": {"AAA": 1_200.0}},
                                    {"AAA": _whole("AAA")}, allow_fractional=False)
    assert row.delta_quantity == 2.0

    row2 = _buy_row("AAA", 100.0, 2.0, target_notional=200.0)
    plan2 = AllocationPlan(rows=[row2], available_buying_power=350.0,
                           valuation_mode=pa.VALUATION_MODE_MARKET)
    pa.redistribute_label_residuals(plan2, {"A": {"AAA": 1_200.0}},
                                    {"AAA": _whole("AAA")}, allow_fractional=False)
    assert row2.delta_quantity == 3.0
    assert row2.bp_cost == pytest.approx(300.0)


def test_redistribution_shrinks_an_order_but_never_deletes_it():
    """Removing the last unit would silently drop a row the user reviewed."""
    row = _buy_row("AAA", 100.0, 3.0, target_notional=300.0)
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    pa.redistribute_label_residuals(plan, {"A": {"AAA": 0.0}},
                                    {"AAA": _whole("AAA")}, allow_fractional=False)
    assert row.delta_quantity == 1.0
    assert row.side == OrderDirection.BUY
    assert row.estimated_value == pytest.approx(100.0)


def test_redistribution_respects_the_broker_minimum_order_size():
    row = _buy_row("AAA", 100.0, 0.0, target_notional=0.0)
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    pa.redistribute_label_residuals(plan, {"A": {"AAA": 300.0}},
                                    {"AAA": _whole("AAA", min_order_size=5.0)},
                                    allow_fractional=False)
    assert row.delta_quantity == 0.0


def test_a_row_the_grid_already_refused_is_not_resurrected_as_an_absorber():
    """It carries "no order" in its reasons and unmet money on its face; giving it an
    order anyway would make the two halves of the row contradict each other."""
    row = _buy_row("SKIP", 650_000.0, 0.0, target_notional=260_000.0)
    row.sizing_outcome = pa.SIZING_OUTCOME_SKIPPED_TOO_LARGE
    row.unmet_notional = 260_000.0
    plan = AllocationPlan(rows=[row], available_buying_power=10_000_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    pa.redistribute_label_residuals(plan, {"A": {"SKIP": 260_000.0}},
                                    {"SKIP": _whole("SKIP")}, allow_fractional=False)
    assert row.delta_quantity == 0.0
    assert row.redistributed is False


def test_a_symbol_in_two_labels_is_left_out_of_the_redistribution_entirely():
    """Its target is the SUM of two labels' shares, so no single label owns it and
    moving it would move both. Excluded from the total AND from the absorbers."""
    shared = _buy_row("XXX", 100.0, 5.0, target_notional=500.0, labels=("A", "B"))
    plan = AllocationPlan(rows=[shared], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(plan, {"A": {"XXX": 900.0}}, {},
                                           allow_fractional=False)
    assert shared.delta_quantity == 5.0
    assert left == {}
    assert plan.warnings == []


# ---------------------------------------------------------------------------
# Cost mode converts at the right rate in both directions
# ---------------------------------------------------------------------------

def test_a_cost_mode_reduction_removes_average_cost_not_the_market_price():
    """Basis 1,000 over 10 shares (avg 100) at a 200 price, target basis 900. One
    share off the basis is 100, not 200: divide by the price and nothing moves."""
    row = _buy_row("HELD", 200.0, 0.0, current_quantity=10.0,
                   current_cost_basis=1_000.0, target_notional=900.0)
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_COST)
    left = pa.redistribute_label_residuals(plan, {"A": {"HELD": 900.0}},
                                           {"HELD": _whole("HELD")},
                                           allow_fractional=False)
    assert row.delta_quantity == -1.0
    assert pa.projected_value(row, pa.VALUATION_MODE_COST) == pytest.approx(900.0)
    assert left["A"] == pytest.approx(0.0)


def test_cost_mode_converts_a_buying_row_at_price_even_when_the_label_is_OVER():
    """The rate belongs to the ROW BEING MOVED, not to the label's residual.

    HELD is down 50%: 10 shares carrying 2,000 of basis (200 average) at a 100
    price, and it is buying 8 more. Its projected BASIS is 2,000 + 800 = 2,800
    against a 2,400 target, so the label is 400 OVER and the give-back is a SELL
    direction on a BUYING row -- the two signs disagree.

    One share bought less removes the PRICE from the basis (100), never the average
    cost (200), so exactly 4 shares come off. Sizing that give-back at the average
    cost halves it: 2 shares, and the label finishes 100 over target with a warning
    on it. That is the cost-mode-divides-by-the-wrong-rate bug, one level up.
    """
    row = _buy_row("HELD", 100.0, 8.0, current_quantity=10.0,
                   current_cost_basis=2_000.0, target_notional=2_400.0)
    plan = AllocationPlan(rows=[row], available_buying_power=1_000_000.0,
                          valuation_mode=pa.VALUATION_MODE_COST)
    left = pa.redistribute_label_residuals(plan, {"A": {"HELD": 2_400.0}},
                                           {"HELD": _whole("HELD")},
                                           allow_fractional=False)
    assert row.delta_quantity == pytest.approx(4.0)
    assert pa.projected_value(row, pa.VALUATION_MODE_COST) == pytest.approx(2_400.0)
    assert left["A"] == pytest.approx(0.0)
    assert plan.warnings == []


def test_cost_mode_converts_a_selling_row_at_average_cost_even_when_the_label_is_SHORT():
    """The same rule with the signs disagreeing the other way round.

    HELD is up 100%: 10 shares carrying 500 of basis (50 average) at a 100 price,
    and it is selling 8. Its projected basis is 500 x 2/10 = 100 against a 300
    target, so the label is 200 SHORT and the top-up is a BUY direction on a
    SELLING row. Selling one share fewer puts the AVERAGE COST back (50), not the
    price (100), so 4 shares of the sell are cancelled, not 2.
    """
    row = _buy_row("HELD", 100.0, -8.0, current_quantity=10.0,
                   current_cost_basis=500.0, target_notional=300.0)
    plan = AllocationPlan(rows=[row], available_buying_power=1_000_000.0,
                          valuation_mode=pa.VALUATION_MODE_COST)
    left = pa.redistribute_label_residuals(plan, {"A": {"HELD": 300.0}},
                                           {"HELD": _whole("HELD")},
                                           allow_fractional=False)
    assert row.delta_quantity == pytest.approx(-4.0)
    assert pa.projected_value(row, pa.VALUATION_MODE_COST) == pytest.approx(300.0)
    assert left["A"] == pytest.approx(0.0)
    assert plan.warnings == []


def test_a_cost_mode_row_not_trading_yet_absorbs_a_shortfall_at_price():
    """The boundary of "which side is this row on": a row with NO order yet takes a
    shortfall by BUYING, so it converts at the price even though it is held at a 200
    average. The gap is exactly one share at the price and half a share at the
    average, so reading the zero as a sell floors the step to nothing and the label
    is left short with a warning on it.
    """
    row = _buy_row("HELD", 100.0, 0.0, current_quantity=10.0,
                   current_cost_basis=2_000.0, target_notional=2_100.0)
    plan = AllocationPlan(rows=[row], available_buying_power=1_000_000.0,
                          valuation_mode=pa.VALUATION_MODE_COST)
    left = pa.redistribute_label_residuals(plan, {"A": {"HELD": 2_100.0}},
                                           {"HELD": _whole("HELD")},
                                           allow_fractional=False)
    assert row.delta_quantity == pytest.approx(1.0)
    assert pa.projected_value(row, pa.VALUATION_MODE_COST) == pytest.approx(2_100.0)
    assert left["A"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Convergence and the bound
# ---------------------------------------------------------------------------

def test_redistribution_converges_and_leaves_no_warning():
    labels = [LabelTarget("A", 100.0, [SymbolTarget("XXX", 50.0),
                                       SymbolTarget("FRAC", 50.0)])]
    current = {"XXX": _pos("XXX", 300.0), "FRAC": _pos("FRAC", 100.0)}
    margin = {"XXX": _whole("XXX"), "FRAC": _frac("FRAC")}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.warnings == []
    total = sum(r.estimated_value for r in plan.rows)
    assert total == pytest.approx(1_000.0)


def test_the_pass_bound_stops_the_loop_and_the_plan_says_the_bound_stopped_it(monkeypatch):
    """Pinned by driving the bound to zero: the loop must be bounded by the constant,
    not by the argument that it converges."""
    monkeypatch.setattr(pa, "REDISTRIBUTION_MAX_PASSES", 0)
    row = _buy_row("AAA", 100.0, 5.0, target_notional=500.0)
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(plan, {"A": {"AAA": 900.0}},
                                           {"AAA": _whole("AAA")}, allow_fractional=False)
    assert row.delta_quantity == 5.0
    assert left["A"] == pytest.approx(400.0)
    assert any("redistribution passes" in w for w in plan.warnings)
    assert any("400.00" in w for w in plan.warnings)


def test_a_residual_nothing_may_absorb_is_reported_not_hidden():
    """Buying power blocks the only absorber. The money is real and the plan says so
    rather than pretending the label is on target."""
    row = _buy_row("AAA", 100.0, 2.0, target_notional=200.0)
    plan = AllocationPlan(rows=[row], available_buying_power=200.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(plan, {"A": {"AAA": 1_200.0}},
                                           {"AAA": _whole("AAA")}, allow_fractional=False)
    assert row.delta_quantity == 2.0
    assert left["A"] == pytest.approx(1_000.0)
    assert any("can absorb the rest" in w for w in plan.warnings)


def test_a_residual_under_one_share_is_not_a_warning():
    row = _buy_row("AAA", 100.0, 5.0, target_notional=500.0)
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(plan, {"A": {"AAA": 550.0}},
                                           {"AAA": _whole("AAA")}, allow_fractional=False)
    assert row.delta_quantity == 5.0
    assert left["A"] == pytest.approx(50.0)
    assert plan.warnings == []


def test_redistribution_is_deterministic():
    def _solve():
        labels = [LabelTarget("A", 100.0, [SymbolTarget("AAA", 34.0),
                                           SymbolTarget("BBB", 33.0),
                                           SymbolTarget("CCC", 33.0)])]
        current = {"AAA": _pos("AAA", 170.0), "BBB": _pos("BBB", 90.0),
                   "CCC": _pos("CCC", 55.0)}
        margin = {"AAA": _whole("AAA"), "BBB": _whole("BBB"), "CCC": _frac("CCC")}
        return pa.compute_allocation(3_333.0, 1_000_000.0, labels, current, margin,
                                     allow_fractional=True, default_bp_factor=1.0,
                                     valuation_mode=pa.VALUATION_MODE_MARKET)
    first, second = _solve(), _solve()
    assert [r.to_dict() for r in first.rows] == [r.to_dict() for r in second.rows]
    assert json.dumps(first.to_dict()) == json.dumps(second.to_dict())


# ---------------------------------------------------------------------------
# INVEST_LABEL: the budget basis
# ---------------------------------------------------------------------------

def test_an_invest_label_run_redistributes_against_the_budget_not_the_holding():
    """AAA already holds 1,000 of stock. The label is investing 1,000 MORE, so the
    residual is measured on the money deployed, never on the post-trade value."""
    label = LabelTarget("L", 100.0, [SymbolTarget("AAA", 50.0),
                                     SymbolTarget("FRAC", 50.0)])
    current = {"AAA": _pos("AAA", 300.0, quantity=10.0, cost_basis=1_000.0),
               "FRAC": _pos("FRAC", 100.0)}
    margin = {"AAA": _whole("AAA"), "FRAC": _frac("FRAC")}
    plan = pa.compute_label_investment(label, 1_000.0, current, margin,
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=True, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert plan.allocation_basis == pa.ALLOCATION_BASIS_BUDGET
    assert by["AAA"].delta_quantity == 1.0                     # 500 buys one 300 share
    assert by["FRAC"].delta_quantity == pytest.approx(7.0)     # 5 + the missing 200
    assert plan.total_buy_value == pytest.approx(1_000.0)


def test_a_budget_basis_residual_is_never_closed_by_opening_a_sell():
    """``compute_label_investment`` promises "no sells are ever produced", and
    redistribution is inside that promise.

    BUMP was bumped to one 300 share against a 250 budget, so the label is 50 OVER.
    The bumped row is one-way and cannot give it back, which leaves HELD -- flat in
    this run, but holding 10 shares of a 25 stock. Selling 2 of them would balance
    the arithmetic and would also be a trade nobody asked for, in a flow whose whole
    contract is "deploy this money": it realises a gain, it moves a position the
    user never mentioned, and it quietly shrinks ``net_buy_value``, which is what
    consumes the income ledger. The 50 is REPORTED instead.
    """
    bump = _buy_row("BUMP", 300.0, 1.0, target_notional=250.0, labels=("L",))
    bump.sizing_outcome = pa.SIZING_OUTCOME_BUMPED
    held = _buy_row("HELD", 25.0, 0.0, current_quantity=10.0,
                    current_cost_basis=250.0, target_notional=0.0, labels=("L",))
    plan = AllocationPlan(rows=[bump, held], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_COST,
                          allocation_basis=pa.ALLOCATION_BASIS_BUDGET)
    left = pa.redistribute_label_residuals(plan, {"L": {"BUMP": 250.0, "HELD": 0.0}},
                                           {"BUMP": _whole("BUMP"), "HELD": _whole("HELD")},
                                           allow_fractional=False)
    assert held.delta_quantity == 0.0
    assert held.side is None
    assert held.redistributed is False
    assert left["L"] == pytest.approx(-50.0)


def test_an_invest_label_run_reports_a_bumps_overshoot_rather_than_selling_it_off():
    """The same thing end to end, through the solver that makes the promise."""
    label = LabelTarget("L", 100.0, [SymbolTarget("BUMP", 100.0),
                                     SymbolTarget("HELD", 0.0)])
    current = {"BUMP": _pos("BUMP", 300.0),
               "HELD": _pos("HELD", 25.0, quantity=10.0, cost_basis=250.0)}
    margin = {"BUMP": _whole("BUMP"), "HELD": _whole("HELD")}
    plan = pa.compute_label_investment(label, 250.0, current, margin,
                                       available_buying_power=1_000_000.0,
                                       allow_fractional=False, default_bp_factor=1.0,
                                       valuation_mode=pa.VALUATION_MODE_COST)
    by = {r.symbol: r for r in plan.rows}
    assert by["BUMP"].sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    assert by["BUMP"].delta_quantity == 1.0
    assert by["HELD"].delta_quantity == 0.0
    assert by["HELD"].side is None
    assert plan.total_sell_value == 0.0
    assert plan.net_buy_value == plan.total_buy_value == pytest.approx(300.0)


def test_a_rebalance_plan_is_stamped_with_the_position_basis():
    plan = pa.compute_allocation(0.0, 0.0, [], {}, {}, allow_fractional=False,
                                 default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    assert plan.allocation_basis == pa.ALLOCATION_BASIS_POSITION
    assert json.loads(json.dumps(plan.to_dict()))["allocation_basis"] == "position"


def test_a_bumped_row_does_not_soak_up_a_siblings_shortfall_either():
    """The other half of "bumps are one-way", and the expensive half.

    BIG's 50 target bumps to one 65 share -- already 130% of what the user typed.
    PRICEY is 450 short and cannot take another 500 share. If the bumped row were
    an absorber it would buy SIX more, ending on 455 against a 50 target: a
    deliberate 30% over-allocation turned into a 910% one, silently.
    """
    labels = [LabelTarget("A", 100.0, [SymbolTarget("BIG", 5.0),
                                       SymbolTarget("PRICEY", 95.0)])]
    current = {"BIG": _pos("BIG", 65.0), "PRICEY": _pos("PRICEY", 500.0)}
    margin = {"BIG": _whole("BIG"), "PRICEY": _whole("PRICEY")}
    plan = pa.compute_allocation(1_000.0, 1_000_000.0, labels, current, margin,
                                 allow_fractional=True, default_bp_factor=1.0,
                                 valuation_mode=pa.VALUATION_MODE_MARKET)
    by = {r.symbol: r for r in plan.rows}
    assert by["BIG"].sizing_outcome == pa.SIZING_OUTCOME_BUMPED
    assert by["BIG"].delta_quantity == 1.0
    assert by["BIG"].redistributed is False
    assert by["PRICEY"].delta_quantity == 1.0
    assert sum(r.estimated_value for r in plan.rows) == pytest.approx(565.0)


def test_a_row_that_says_no_order_is_never_handed_one_by_redistribution():
    """``unmet_notional`` is the test, not the sizing outcome: a row zeroed by a
    broker minimum keeps ``SIZING_OUTCOME_NORMAL``, so excluding only bumped and
    too-large rows would let redistribution place the very order that was refused
    -- while the row still reads "no order" to the user."""
    row = _buy_row("SUPP", 100.0, 0.0, current_quantity=3.0, target_notional=600.0)
    row.unmet_notional = 300.0          # the 3-share top-up the broker refused
    plan = AllocationPlan(rows=[row], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(plan, {"A": {"SUPP": 600.0}},
                                           {"SUPP": _whole("SUPP")},
                                           allow_fractional=False)
    assert row.delta_quantity == 0.0
    assert row.side is None
    assert row.redistributed is False
    assert left["A"] == pytest.approx(300.0)


def test_the_fractionable_sibling_absorbs_first_so_fewer_typed_weights_move():
    """Order matters, and it is not cosmetic. A 250 shortfall is exactly absorbable
    by the fractionable row on its 4dp grid, leaving the whole-share row on the
    weight the user typed. Take the whole-share row first and it lumps in 2 shares,
    the fractionable one mops up the remaining 50, and TWO weights have moved where
    one would have done."""
    aaa = _buy_row("AAA", 100.0, 2.0, target_notional=200.0)
    frac = _buy_row("FRAC", 100.0, 2.0, target_notional=200.0, fractional=True)
    plan = AllocationPlan(rows=[aaa, frac], available_buying_power=100_000.0,
                          valuation_mode=pa.VALUATION_MODE_MARKET)
    left = pa.redistribute_label_residuals(
        plan, {"A": {"AAA": 200.0, "FRAC": 450.0}},
        {"AAA": _whole("AAA"), "FRAC": _frac("FRAC")}, allow_fractional=True)
    assert frac.delta_quantity == pytest.approx(4.5)
    assert frac.redistributed is True
    assert aaa.delta_quantity == 2.0
    assert aaa.redistributed is False
    assert left["A"] == pytest.approx(0.0)
