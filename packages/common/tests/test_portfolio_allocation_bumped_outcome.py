"""An OUTCOME describes the row's FINAL state, not an intermediate one.

THE DEFECT, as it reached the user. A dry-run row read ``SIDE -``, ``QTY 0``,
``ORDER no order``, ``EST. VALUE 0.00``, weight ``0.64% -> 0.00%`` -- and
``OUTCOME bumped-to-1``. Its reason told the whole story::

    rounded down to whole shares, target 34.48 buys 0.9170 shares at 37.60 -
    BUMPED UP to 1 share(s), 109% of target, scaled x0.30 to fit buying power

Rounded to 0, bumped up to 1, then the buying-power scaling took it back under
one share and it rounded to 0 again. ``sizing_outcome`` was stamped by the bump
and never revisited, so the column claimed an over-allocation on a row that
bought nothing. The notice above the table agreed it was wrong -- "1 symbol(s)
... were BUMPED UP to one ... that over-allocates them by 0.00 in total", 0.00
because the over-allocation never happened.

THE RULE, and it is a rule rather than a patch: a BUMP is a claim that the row
holds MORE than its weight asked for, so it cannot survive the row ending with no
order. ``_reconcile_sizing_outcomes`` is the ONE door -- it runs from
``_finalise_totals``, which every solver and ``apply_order_impacts`` already call,
so a future step that zeroes a row cannot leave the claim standing either. Two
paths reach it today (the buying-power scaler and a broker precheck refusal) and
both are pinned below.

The history is not thrown away. ``bumped-then-dropped`` names both halves -- the
plan wanted the position and could not have it -- and the reason string keeps the
full narrative. That is a materially different fact from
``skipped-too-large`` (one share would have overshot the target, so the bump was
never taken) and it must not be spelled the same way.
"""
from ba2_common.core.account_types import MARGIN_SOURCE_ASSET, OrderImpact
from ba2_common.core.portfolio_allocation import (
    BUMP_TO_ONE_SHARE_MAX_MULTIPLE,
    SIZING_OUTCOME_BUMPED,
    SIZING_OUTCOME_BUMPED_DROPPED,
    SIZING_OUTCOME_NORMAL,
    SIZING_OUTCOME_SKIPPED_TOO_LARGE,
    VALUATION_MODE_MARKET,
    AllocationPlan,
    AllocationRow,
    LabelTarget,
    MarginInfo,
    PositionState,
    SymbolTarget,
    apply_order_impacts,
    bump_notice,
    compute_allocation,
    dry_run_rows,
    fractional_summary,
    no_order_notice,
    no_order_rows,
)
from ba2_common.core.types import OrderDirection

NUKX_PRICE = 37.60
BIG_PRICE = 5.0


def _margin(symbol):
    return MarginInfo(symbol=symbol, bp_factor=1.0, marginable=True,
                      fractionable=False, min_trade_increment=1.0,
                      initial_margin_rate=0.5, source=MARGIN_SOURCE_ASSET)


def _bp_starved_plan(*, buying_power=50.0, base_notional=50.0):
    """NUKX's whole target is 0.917 shares -- bumped to 1 -- and there is not
    enough buying power left to keep it once the plan is scaled.

    ``base_notional`` is a SEPARATE knob from ``buying_power`` on purpose: raising
    the budget alone is what isolates the buying-power constraint, and raising
    both would move NUKX's target off the sub-share case entirely and test
    nothing.

    NO SELLS anywhere: the shortfall is real, not the artefact fixed in
    ``test_portfolio_allocation_sell_releases_bp.py``.
    """
    current = {
        'NUKX': PositionState(symbol='NUKX', quantity=0.0, cost_basis=0.0,
                              price=NUKX_PRICE),
        'BIG': PositionState(symbol='BIG', quantity=0.0, cost_basis=0.0,
                             price=BIG_PRICE),
    }
    margin = {s: _margin(s) for s in current}
    labels = [LabelTarget(label='Core', target_pct=100.0, symbols=[
        SymbolTarget(symbol='NUKX', weight_pct=68.96),
        SymbolTarget(symbol='BIG', weight_pct=31.04),
    ])]
    return compute_allocation(
        base_notional=base_notional, available_buying_power=buying_power,
        labels=labels, current=current, margin=margin, allow_fractional=False,
        default_bp_factor=1.0, valuation_mode=VALUATION_MODE_MARKET)


def _row(plan, symbol):
    return next(r for r in plan.rows if r.symbol == symbol)


# ---------------------------------------------------------------------------
# 1. The reported row
# ---------------------------------------------------------------------------

def test_the_starved_plan_really_does_bump_then_scale_the_row_away():
    """The fixture's own precondition, stated separately so a later change that
    stops reproducing the sequence fails HERE rather than making the real
    assertions below pass for the wrong reason."""
    plan = _bp_starved_plan()
    nukx = _row(plan, 'NUKX')

    assert plan.scale_factor < 1.0
    assert nukx.delta_quantity == 0.0
    assert nukx.side is None
    assert any('BUMPED UP to 1 share(s)' in reason for reason in nukx.reasons)
    assert any('to fit buying power' in reason for reason in nukx.reasons)


def test_a_row_that_produced_no_order_is_not_reported_as_bumped_to_1():
    """THE BUG. ``bumped-to-1`` says the row holds more than asked; it holds
    nothing."""
    assert _row(_bp_starved_plan(), 'NUKX').sizing_outcome != SIZING_OUTCOME_BUMPED


def test_the_row_says_the_position_was_wanted_and_could_not_be_afforded():
    """Both halves. Dropping the bump entirely would lose the one thing worth
    knowing: the plan asked for this position."""
    nukx = _row(_bp_starved_plan(), 'NUKX')

    assert nukx.sizing_outcome == SIZING_OUTCOME_BUMPED_DROPPED
    assert nukx.sizing_outcome != SIZING_OUTCOME_SKIPPED_TOO_LARGE
    assert nukx.unmet_notional > 0


def test_the_dry_run_table_and_the_not_traded_table_agree():
    """The row appears in both, and a reader must not find two different words
    for one state."""
    plan = _bp_starved_plan()
    table = {r['symbol']: r for r in dry_run_rows(plan)}
    dropped = {r['symbol']: r for r in no_order_rows(plan)}

    assert table['NUKX']['outcome'] == SIZING_OUTCOME_BUMPED_DROPPED
    assert dropped['NUKX']['outcome'] == SIZING_OUTCOME_BUMPED_DROPPED


# ---------------------------------------------------------------------------
# 2. ...and the notices that were counting it
# ---------------------------------------------------------------------------

def test_the_bump_notice_no_longer_announces_an_over_allocation_of_zero():
    """"were BUMPED UP to one ... that over-allocates them by 0.00 in total" --
    0.00 because no bump survived. A notice whose own number contradicts it is
    worse than no notice."""
    summary = fractional_summary(_bp_starved_plan())

    assert summary['bumped_rows'] == 0
    assert bump_notice(summary) is None


def test_the_summary_can_still_count_the_rows_that_were_dropped():
    summary = fractional_summary(_bp_starved_plan())

    assert summary['bumped_dropped_rows'] == 1


def test_the_no_order_notice_does_not_blame_the_bump_bound_for_a_starved_row():
    """The old sentence asserted ONE cause for every no-order row: "One whole
    share of each would be more than 150% of its target". For a row buying power
    took away that is simply false -- one share was 109% of target and perfectly
    acceptable."""
    text = no_order_notice(fractional_summary(_bp_starved_plan()))

    assert text is not None
    assert f'{BUMP_TO_ONE_SHARE_MAX_MULTIPLE * 100.0:.0f}%' not in text
    assert 'Not traded' in text


# ---------------------------------------------------------------------------
# 3. A BUMP THAT SURVIVED IS STILL A BUMP.
#
# The whole risk of this fix is that it downgrades every bump, which would hide
# the deliberate over-allocation the outcome exists to surface.
# ---------------------------------------------------------------------------

def test_a_bump_that_kept_its_order_is_still_reported_as_bumped_to_1():
    plan = _bp_starved_plan(buying_power=5_000.0)
    nukx = _row(plan, 'NUKX')

    assert plan.scale_factor == 1.0
    assert nukx.delta_quantity == 1.0
    assert nukx.sizing_outcome == SIZING_OUTCOME_BUMPED


def test_a_surviving_bump_is_still_counted_and_still_announced():
    summary = fractional_summary(_bp_starved_plan(buying_power=5_000.0))

    assert summary['bumped_rows'] == 1
    assert summary['bumped_dropped_rows'] == 0
    assert bump_notice(summary) is not None
    assert '0.00 in total' not in bump_notice(summary)


def test_a_row_the_bound_refused_keeps_its_own_word_for_it():
    """``skipped-too-large`` is a DIFFERENT fact: one share would have overshot
    the target, so no bump was ever taken. It must not be swept into the new
    outcome just because it also has no order."""
    current = {'TINY': PositionState(symbol='TINY', quantity=0.0, cost_basis=0.0,
                                     price=100.0)}
    labels = [LabelTarget(label='Core', target_pct=100.0,
                          symbols=[SymbolTarget(symbol='TINY', weight_pct=100.0)])]
    plan = compute_allocation(
        base_notional=10.0, available_buying_power=10_000.0, labels=labels,
        current=current, margin={'TINY': _margin('TINY')}, allow_fractional=False,
        default_bp_factor=1.0, valuation_mode=VALUATION_MODE_MARKET)

    assert _row(plan, 'TINY').sizing_outcome == SIZING_OUTCOME_SKIPPED_TOO_LARGE


def test_an_ordinary_row_with_no_order_is_left_as_normal():
    """``normal`` claims nothing, so there is nothing to invalidate. Only a false
    claim gets rewritten."""
    plan = _bp_starved_plan()

    assert _row(plan, 'BIG').sizing_outcome == SIZING_OUTCOME_NORMAL


# ---------------------------------------------------------------------------
# 4. THE SECOND PATH. A bump the BROKER refuses is invalidated the same way, by
#    the same door -- which is why the door is at the end of the solve and not
#    inside the buying-power scaler.
# ---------------------------------------------------------------------------

def test_a_precheck_refusal_invalidates_a_bump_too():
    plan = AllocationPlan(
        rows=[AllocationRow(symbol='NUKX', price=NUKX_PRICE, delta_quantity=1.0,
                            target_quantity=1.0, side=OrderDirection.BUY,
                            estimated_value=NUKX_PRICE, bp_cost=NUKX_PRICE,
                            bp_factor=1.0, target_notional=34.48,
                            sizing_outcome=SIZING_OUTCOME_BUMPED,
                            initial_margin_rate=0.5,
                            margin_source=MARGIN_SOURCE_ASSET)],
        base_notional=1_000.0, available_buying_power=1_000.0,
        valuation_mode=VALUATION_MODE_MARKET)
    refused = OrderImpact(symbol='NUKX', change_in_buying_power=0.0,
                          accepted=False, errors=['symbol not tradeable'])

    out = apply_order_impacts(plan, {'NUKX': refused},
                              available_buying_power=1_000.0, margin={})

    assert _row(out, 'NUKX').delta_quantity == 0.0
    assert _row(out, 'NUKX').sizing_outcome == SIZING_OUTCOME_BUMPED_DROPPED
    assert fractional_summary(out)['bumped_rows'] == 0


def test_a_precheck_that_accepts_the_bump_leaves_it_alone():
    plan = AllocationPlan(
        rows=[AllocationRow(symbol='NUKX', price=NUKX_PRICE, delta_quantity=1.0,
                            target_quantity=1.0, side=OrderDirection.BUY,
                            estimated_value=NUKX_PRICE, bp_cost=NUKX_PRICE,
                            bp_factor=1.0, target_notional=34.48,
                            sizing_outcome=SIZING_OUTCOME_BUMPED,
                            initial_margin_rate=0.5,
                            margin_source=MARGIN_SOURCE_ASSET)],
        base_notional=1_000.0, available_buying_power=1_000.0,
        valuation_mode=VALUATION_MODE_MARKET)
    accepted = OrderImpact(symbol='NUKX', change_in_buying_power=-NUKX_PRICE)

    out = apply_order_impacts(plan, {'NUKX': accepted},
                              available_buying_power=1_000.0, margin={})

    assert _row(out, 'NUKX').sizing_outcome == SIZING_OUTCOME_BUMPED
