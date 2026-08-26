"""A SELL FREES buying power, and the plan is allowed to spend it.

THE DEFECT, as it reached the user. A live dry run sold 2,112.58, bought 307.78,
and reported ``Required BP: 359.26 / 394.87 (91.0%)`` with ``scaled x0.30 to fit
buying power`` on nearly every buy row. The plan was strongly NET RELEASING and
the solver still believed it was nearly out of room, because the buying-power
budget counted only what the broker had published BEFORE the sells and nothing
the sells gave back. Every SELL row showed ``BP COST 0.00`` -- the display half
of the same omission.

THAT A SELL FREES BUYING POWER IS NOT A NEW CONVENTION HERE. It is stated in four
places already: ``AllocationRow.bp_cost``'s own docstring ("sells free buying
power and never scale"), ``_apply_bp_scaling``'s ("SELLS NEVER SCALE -- they free
buying power"), the dry run's leverage tooltip, and ``OrderImpact.bp_cost``, which
reads the broker's SIGNED ``change_in_buying_power`` and returns 0.0 "when it frees
BP". Nothing anywhere -- not ``check_option_buying_power``, not any adapter --
distinguishes a cash account's unsettled proceeds from a margin account's
immediate release, so no such distinction is invented here either: there is ONE
convention and this credits it to the budget the same way ``bp_cost`` debits it.

HOW MUCH a sell frees is the exact mirror of what the same notional would cost to
buy: ``estimated_value * bp_factor``. Any other rate would make buying a position
and selling it back move the budget by different amounts.

THE SAFETY PROPERTY THAT CHANGED, said out loud. ``submit_plan`` sends every sell
before any buy, and its docstring used to note that the buys were sized against
the pre-sell buying power so "a refused close cannot make them overspend". That
was true and it cost the user the whole plan: a net-releasing rebalance shrank to
a third of itself for no reason. The buys are now sized against the post-sell
budget; a sell the broker refuses can therefore leave a buy short of room, which
the broker rejects and ``_submit_row`` reports on its own row. Untick the sell in
the dry run and the footer says so BEFORE anything is sent -- see
``test_unticking_the_funding_sell_puts_the_plan_back_over_budget``.
"""
import pytest

from ba2_common.core.account_types import MARGIN_SOURCE_ASSET, OrderImpact
from ba2_common.core.portfolio_allocation import (
    VALUATION_MODE_MARKET,
    AllocationPlan,
    AllocationRow,
    LabelTarget,
    MarginInfo,
    PositionState,
    REASON_SCALED_PREFIX,
    SymbolTarget,
    apply_order_impacts,
    compute_allocation,
    dry_run_rows,
    filter_plan_rows,
)
from ba2_common.core.types import OrderDirection


# ---------------------------------------------------------------------------
# The reported plan, rebuilt. Sells 2,112.58 against 394.87 of published buying
# power, and wants ~1,187 of buys -- so it is NET RELEASING by a wide margin and
# used to scale every buy by ~0.33 anyway.
# ---------------------------------------------------------------------------

BUYING_POWER = 394.87
HELD_PRICE = 21.1258
NUKX_PRICE = 37.60
BIG_PRICE = 50.0


def _margin(symbol, *, bp_factor=1.0):
    return MarginInfo(symbol=symbol, bp_factor=bp_factor, marginable=True,
                      fractionable=False, min_trade_increment=1.0,
                      initial_margin_rate=bp_factor / 2.0,
                      source=MARGIN_SOURCE_ASSET)


def _net_releasing_plan(*, allow_fractional=False):
    """Liquidate a 2,112.58 holding and redeploy into two other symbols."""
    current = {
        'HELD': PositionState(symbol='HELD', quantity=100.0, cost_basis=2_000.0,
                              price=HELD_PRICE),
        'NUKX': PositionState(symbol='NUKX', quantity=0.0, cost_basis=0.0,
                              price=NUKX_PRICE),
        'BIG': PositionState(symbol='BIG', quantity=0.0, cost_basis=0.0,
                             price=BIG_PRICE),
    }
    margin = {s: _margin(s) for s in current}
    labels = [LabelTarget(label='Core', target_pct=100.0, symbols=[
        SymbolTarget(symbol='HELD', weight_pct=0.0),
        SymbolTarget(symbol='NUKX', weight_pct=2.865),
        SymbolTarget(symbol='BIG', weight_pct=97.135),
    ])]
    base = BUYING_POWER + 100.0 * HELD_PRICE
    return compute_allocation(
        base_notional=base, available_buying_power=BUYING_POWER, labels=labels,
        current=current, margin=margin, allow_fractional=allow_fractional,
        default_bp_factor=1.0, valuation_mode=VALUATION_MODE_MARKET,
        unallocated_pct=52.0)


def _row(plan, symbol):
    return next(r for r in plan.rows if r.symbol == symbol)


# ---------------------------------------------------------------------------
# 1. The per-row fact
# ---------------------------------------------------------------------------

def test_a_sell_carries_the_buying_power_it_frees_not_a_bare_zero():
    """``BP COST 0.00`` on every sell row was the display half of the defect."""
    plan = _net_releasing_plan()
    sell = _row(plan, 'HELD')

    assert sell.side == OrderDirection.SELL
    assert sell.bp_cost == 0.0                       # a sell costs nothing...
    # ...it FREES, at the same rate the same notional would have cost to buy.
    assert sell.bp_released == pytest.approx(sell.estimated_value * sell.bp_factor)
    assert sell.bp_released > 2_100.0


def test_the_signed_bp_effect_points_the_way_the_buying_power_moves():
    """One number per row, negative when the row consumes and positive when it
    frees -- the sign convention the broker itself publishes
    (``OrderImpact.change_in_buying_power`` is NEGATIVE for a buy)."""
    plan = _net_releasing_plan()

    assert _row(plan, 'HELD').bp_effect > 0
    assert _row(plan, 'BIG').bp_effect == -_row(plan, 'BIG').bp_cost
    assert _row(plan, 'BIG').bp_effect < 0


def test_a_buy_frees_nothing():
    plan = _net_releasing_plan()

    assert _row(plan, 'BIG').bp_released == 0.0


# ---------------------------------------------------------------------------
# 2. The plan-level budget -- the arithmetic the user actually complained about
# ---------------------------------------------------------------------------

def test_the_plan_reports_the_buying_power_its_sells_free():
    plan = _net_releasing_plan()

    assert plan.released_buying_power == _row(plan, 'HELD').bp_released
    assert plan.total_buying_power == (plan.available_buying_power
                                       + plan.released_buying_power)


def test_a_NET_RELEASING_plan_is_not_scaled_down_at_all():
    """THE BUG. Selling 2,112.58 to buy ~1,187 is not a buying-power shortfall,
    and reporting one shrank every buy to a third of what the weights asked for."""
    plan = _net_releasing_plan()

    assert plan.total_sell_value > plan.total_buy_value
    assert plan.scale_factor == 1.0
    scaled = [r.symbol for r in plan.rows
              for reason in r.reasons if reason.startswith(REASON_SCALED_PREFIX)]
    assert scaled == []


def test_the_buys_the_scaling_used_to_shrink_are_sent_in_full():
    """The two rows from the screenshot: a sub-share target that the bump saves,
    and the big buy that used to lose two thirds of itself."""
    plan = _net_releasing_plan()

    assert _row(plan, 'NUKX').delta_quantity == 1.0        # bumped, and it SURVIVES
    assert _row(plan, 'BIG').delta_quantity == 23.0        # was 7 under the scaling
    assert plan.required_buying_power == 1_187.6


def test_bp_usage_is_measured_against_the_budget_the_plan_really_has():
    """91.0% of 394.87 was the headline the dry run showed for a plan that had
    2,507 to spend."""
    plan = _net_releasing_plan()

    assert plan.bp_usage_pct == (plan.required_buying_power
                                 / plan.total_buying_power * 100.0)
    assert plan.bp_usage_pct < 50.0


# ---------------------------------------------------------------------------
# 3. ...and the budget is still a BUDGET
# ---------------------------------------------------------------------------

#: Deliberately FIXED rather than derived from the holding, so the control below
#: aims at exactly the same buy target as the plan it is a control for.
OVER_BUDGET_BASE = 10_029.80


def _over_budget_plan(*, hold=100.0):
    """Wants 10,029.80 of BIG and cannot afford it even with the sell credited.

    ``hold`` is the only difference between this and its control: 100 shares of
    HELD to liquidate, or nothing to sell at all. The BUY TARGET is identical
    either way.
    """
    current = {
        'HELD': PositionState(symbol='HELD', quantity=hold,
                              cost_basis=20.0 * hold, price=HELD_PRICE),
        'BIG': PositionState(symbol='BIG', quantity=0.0, cost_basis=0.0,
                             price=BIG_PRICE),
    }
    margin = {s: _margin(s) for s in current}
    labels = [LabelTarget(label='Core', target_pct=100.0, symbols=[
        SymbolTarget(symbol='HELD', weight_pct=0.0),
        SymbolTarget(symbol='BIG', weight_pct=100.0),
    ])]
    return compute_allocation(
        base_notional=OVER_BUDGET_BASE, available_buying_power=BUYING_POWER,
        labels=labels, current=current, margin=margin, allow_fractional=False,
        default_bp_factor=1.0, valuation_mode=VALUATION_MODE_MARKET)


def test_a_plan_that_outruns_even_the_freed_buying_power_still_scales():
    """Crediting the sells is not the same as removing the constraint."""
    plan = _over_budget_plan()

    assert plan.scale_factor < 1.0
    assert plan.required_buying_power <= plan.total_buying_power + 0.005
    # ...and it scaled against the BIGGER budget, so it kept far more than the
    # pre-sell figure alone would have allowed.
    assert plan.required_buying_power > plan.available_buying_power


def test_the_freed_money_reaches_the_ORDERS_and_not_just_the_footer():
    """A control with nothing to sell, aimed at exactly the same buy target.

    This is the assertion the reported defect would fail: crediting the sells has
    to change how many shares are bought, not merely the percentage in the footer.
    """
    with_sell = _over_budget_plan()
    without_sell = _over_budget_plan(hold=0.0)

    assert without_sell.released_buying_power == 0.0
    assert without_sell.total_buying_power == BUYING_POWER
    assert (_row(with_sell, 'BIG').delta_quantity
            > _row(without_sell, 'BIG').delta_quantity * 5)


# ---------------------------------------------------------------------------
# 4. Un-ticking the sell takes its money back -- BEFORE anything is sent
# ---------------------------------------------------------------------------

def test_unticking_the_funding_sell_puts_the_plan_back_over_budget():
    """The dry run's own answer to "what if the close does not happen?". The
    filtered plan is what Submit consumes, so the footer has to re-measure."""
    plan = _net_releasing_plan()
    without_the_sell = filter_plan_rows(plan, ['NUKX', 'BIG'])

    assert without_the_sell.released_buying_power == 0.0
    assert without_the_sell.total_buying_power == BUYING_POWER
    assert without_the_sell.required_buying_power > BUYING_POWER


def test_keeping_the_sell_ticked_keeps_its_released_buying_power():
    plan = _net_releasing_plan()
    everything = filter_plan_rows(plan, ['HELD', 'NUKX', 'BIG'])

    assert everything.released_buying_power == plan.released_buying_power
    assert everything.bp_usage_pct == plan.bp_usage_pct


# ---------------------------------------------------------------------------
# 5. A precheck that KILLS the sell takes the release with it
# ---------------------------------------------------------------------------

def test_a_refused_sell_takes_its_release_back_out_of_the_budget():
    """``apply_order_impacts`` zeroes a row the broker refused. A refused SELL is
    not going to free anything, so the buys must be re-scaled without it."""
    plan = AllocationPlan(
        rows=[
            AllocationRow(symbol='HELD', price=20.0, current_quantity=100.0,
                          delta_quantity=-100.0, side=OrderDirection.SELL,
                          estimated_value=2_000.0, bp_released=2_000.0,
                          bp_factor=1.0, initial_margin_rate=0.5,
                          margin_source=MARGIN_SOURCE_ASSET),
            AllocationRow(symbol='BIG', price=50.0, delta_quantity=40.0,
                          target_quantity=40.0, side=OrderDirection.BUY,
                          estimated_value=2_000.0, bp_cost=2_000.0, bp_factor=1.0,
                          initial_margin_rate=0.5, margin_source=MARGIN_SOURCE_ASSET),
        ],
        base_notional=2_100.0, available_buying_power=100.0,
        released_buying_power=2_000.0, required_buying_power=2_000.0,
        valuation_mode=VALUATION_MODE_MARKET)
    refused = OrderImpact(symbol='HELD', change_in_buying_power=0.0,
                          accepted=False, errors=['position is not closeable'])

    out = apply_order_impacts(plan, {'HELD': refused},
                              available_buying_power=100.0, margin={})

    assert out.released_buying_power == 0.0
    assert out.total_buying_power == 100.0
    assert _row(out, 'HELD').bp_released == 0.0
    assert out.scale_factor < 1.0
    assert out.required_buying_power <= 100.0


# ---------------------------------------------------------------------------
# 6. What the dry-run table is handed
# ---------------------------------------------------------------------------

def test_the_dry_run_row_carries_the_release_and_the_signed_effect():
    rows = {r['symbol']: r for r in dry_run_rows(_net_releasing_plan())}

    assert rows['HELD']['bp_cost'] == 0.0
    assert rows['HELD']['bp_released'] > 2_100.0
    assert rows['HELD']['bp_effect'] == rows['HELD']['bp_released']
    assert rows['BIG']['bp_effect'] == -rows['BIG']['bp_cost']


def test_the_dry_runs_bp_percentage_is_signed_and_shares_the_footers_denominator():
    """A column reading 88.6% beside a footer reading 14.0% is two answers to one
    question. Both divide the budget the plan really has."""
    plan = _net_releasing_plan()
    rows = {r['symbol']: r for r in dry_run_rows(plan)}

    assert rows['HELD']['bp_usage_pct'] > 0
    assert rows['BIG']['bp_usage_pct'] < 0
    assert rows['BIG']['bp_usage_pct'] == round(
        _row(plan, 'BIG').bp_effect / plan.total_buying_power * 100.0, 2)


def test_a_stored_plan_can_say_what_its_sells_freed():
    """``plan_json`` is read by things with no engine to re-derive it with."""
    plan = _net_releasing_plan()
    data = plan.to_dict()

    assert data['released_buying_power'] == plan.released_buying_power
    assert data['total_buying_power'] == plan.total_buying_power
    sell = next(r for r in data['rows'] if r['symbol'] == 'HELD')
    assert sell['bp_released'] == _row(plan, 'HELD').bp_released
