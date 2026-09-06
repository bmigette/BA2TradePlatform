"""The PRE-SUBMIT validation pass: which of a plan's orders will the broker refuse?

Asked for from live use on 2026-09-05: "when sending the order to the broker after
the pf alloc dry run, are we testing if broker accept the order? like can we test
the plan? Upon failure, it should report the failing orders and allow user to
continue without those."

The honest answer has two halves, and the split is what these tests pin:

  * a broker with an order-preview endpoint (TastyTrade) answers for itself, and
    an ``OrderImpact`` with ``accepted=False`` is a refusal in its own words;
  * ALPACA HAS NO SUCH ENDPOINT -- there is no way to ask it "would you take
    this?" without sending it -- so on the live account everything rests on the
    locally knowable refusals: tradability, fractional eligibility, the share
    minimum, the fractional money floor and whether a price still exists.

The one failure mode that would make the whole pass worse than nothing is a FALSE
POSITIVE -- un-ticking a good order on a guess -- so "the broker did not say" is
never a refusal, and that has a test of its own.
"""
import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import AllocationPlan, AllocationRow
from ba2_common.core.account_types import MarginInfo, OrderImpact
from ba2_common.core.types import OrderDirection


def _sendable_row(symbol="AAA", *, quantity=10.0, price=100.0,
                  side=OrderDirection.BUY):
    return AllocationRow(symbol=symbol, price=price, delta_quantity=quantity,
                         side=side, estimated_value=abs(quantity) * price,
                         bp_cost=abs(quantity) * price, bp_factor=1.0)


# -- the clean case, and the false-positive guard ----------------------------

def test_validate_plan_rows_is_empty_on_a_clean_plan():
    plan = AllocationPlan(rows=[_sendable_row()])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, tradable=True,
                                fractionable=True)}
    assert pa.validate_plan_rows(plan, margin) == []


def test_validate_plan_rows_never_flags_a_symbol_the_broker_said_nothing_about():
    """``None`` is "the broker did not say", never "the broker said no". Covers
    the tri-state None, a symbol missing from the dict, and no dict at all."""
    plan = AllocationPlan(rows=[_sendable_row("AAA"), _sendable_row("BBB")])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, tradable=None,
                                fractionable=None)}

    assert pa.validate_plan_rows(plan, margin) == []
    assert pa.validate_plan_rows(plan, {}) == []
    assert pa.validate_plan_rows(plan, None) == []


def test_validate_plan_rows_skips_rows_that_carry_no_order():
    """A suppressed or skipped row is not tickable and is never submitted, so
    reporting it would flag an order nobody is sending."""
    suppressed = AllocationRow(symbol="AAA", price=100.0, delta_quantity=0.0,
                               side=None)
    skipped = _sendable_row("BBB")
    skipped.skipped = True
    plan = AllocationPlan(rows=[suppressed, skipped])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, tradable=False),
              "BBB": MarginInfo(symbol="BBB", bp_factor=1.0, tradable=False)}

    assert pa.validate_plan_rows(plan, margin) == []


# -- the locally knowable refusals -------------------------------------------

def test_validate_plan_rows_flags_a_symbol_the_broker_will_not_trade():
    """The gap this pass exists for: ``tradable`` is no part of sizing, so a
    halted or delisted symbol sizes perfectly and is refused every time."""
    plan = AllocationPlan(rows=[_sendable_row("DEAD")])
    margin = {"DEAD": MarginInfo(symbol="DEAD", bp_factor=1.0, tradable=False)}

    findings = pa.validate_plan_rows(plan, margin)

    assert findings == [("DEAD", pa.REFUSAL_NOT_TRADABLE_FMT.format(symbol="DEAD"))]


def test_validate_plan_rows_flags_a_fractional_order_on_a_whole_share_symbol():
    plan = AllocationPlan(rows=[_sendable_row("AAA", quantity=2.5)])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, fractionable=False)}

    findings = pa.validate_plan_rows(plan, margin)

    assert findings == [("AAA", pa.REFUSAL_FRACTIONAL_NOT_ELIGIBLE_FMT.format(
        symbol="AAA", quantity=2.5))]


def test_a_whole_share_order_on_a_whole_share_symbol_is_fine():
    plan = AllocationPlan(rows=[_sendable_row("AAA", quantity=3.0)])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, fractionable=False)}
    assert pa.validate_plan_rows(plan, margin) == []


def test_validate_plan_rows_flags_an_order_under_the_brokers_share_minimum():
    plan = AllocationPlan(rows=[_sendable_row("AAA", quantity=1.0)])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, min_order_size=5.0)}

    findings = pa.validate_plan_rows(plan, margin)

    assert findings == [("AAA", pa.REFUSAL_BELOW_MIN_ORDER_FMT.format(
        symbol="AAA", quantity=1.0, minimum=5.0))]


def test_validate_plan_rows_flags_a_fractional_order_under_the_money_floor():
    """The $5 rule, and only on a FRACTIONAL quantity -- a 1-share buy of a $3
    stock is legal and must not be flagged."""
    fractional = AllocationPlan(rows=[_sendable_row("AAA", quantity=0.5, price=3.0)])
    whole = AllocationPlan(rows=[_sendable_row("AAA", quantity=1.0, price=3.0)])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, fractionable=True,
                                min_fractional_notional=5.0)}

    assert pa.validate_plan_rows(fractional, margin) == [
        ("AAA", pa.REFUSAL_BELOW_MIN_NOTIONAL_FMT.format(
            symbol="AAA", value=1.5, minimum=5.0))]
    assert pa.validate_plan_rows(whole, margin) == []


def test_validate_plan_rows_flags_a_row_that_lost_its_price():
    """Re-read at COMMIT time: a quote that existed at solve time may not now."""
    row = _sendable_row("AAA")
    row.price = None
    findings = pa.validate_plan_rows(AllocationPlan(rows=[row]), {})
    assert findings == [("AAA", pa.REFUSAL_NO_PRICE_FMT.format(symbol="AAA"))]


def test_validate_plan_rows_reports_every_problem_a_symbol_has():
    """Two findings for one symbol, not one -- fixing the first would otherwise
    reveal the second only on the next attempt."""
    plan = AllocationPlan(rows=[_sendable_row("AAA", quantity=0.5)])
    margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, tradable=False,
                                fractionable=False)}

    findings = pa.validate_plan_rows(plan, margin)

    assert len(findings) == 2
    assert {symbol for symbol, _ in findings} == {"AAA"}


# -- the broker's own verdict ------------------------------------------------

def test_validate_plan_rows_reports_the_brokers_own_refusal_in_its_own_words():
    plan = AllocationPlan(rows=[_sendable_row("AAA")])
    impacts = {"AAA": OrderImpact(symbol="AAA", change_in_buying_power=-1000.0,
                                  accepted=False,
                                  errors=["insufficient buying power"])}

    findings = pa.validate_plan_rows(plan, {}, impacts)

    assert findings == [("AAA", pa.REFUSAL_PRECHECK_FMT.format(
        symbol="AAA", reason="insufficient buying power"))]


def test_a_refused_precheck_with_no_stated_reason_still_reads_as_a_refusal():
    plan = AllocationPlan(rows=[_sendable_row("AAA")])
    impacts = {"AAA": OrderImpact(symbol="AAA", change_in_buying_power=-1000.0,
                                  accepted=False)}

    findings = pa.validate_plan_rows(plan, {}, impacts)

    assert findings == [("AAA", pa.REFUSAL_PRECHECK_FMT.format(
        symbol="AAA", reason=pa.PRECHECK_REASON_UNKNOWN))]


def test_an_accepted_precheck_is_not_a_finding_however_many_warnings_it_carries():
    plan = AllocationPlan(rows=[_sendable_row("AAA")])
    impacts = {"AAA": OrderImpact(symbol="AAA", change_in_buying_power=-1000.0,
                                  accepted=True, warnings=["market is closed"])}
    assert pa.validate_plan_rows(plan, {}, impacts) == []


# -- the plan-level advisory -------------------------------------------------

def test_validate_plan_budget_is_none_when_the_plan_fits():
    plan = AllocationPlan(required_buying_power=1_000.0, available_buying_power=5_000.0)
    assert pa.validate_plan_budget(plan) is None


def test_validate_plan_budget_warns_when_the_ticked_buys_exceed_the_budget():
    """Advisory, not a refusal: the broker fills until the money runs out, and
    buys go out largest first, so the SMALLEST ones are what get refused."""
    plan = AllocationPlan(rows=[_sendable_row("AAA")],
                          required_buying_power=9_000.0,
                          available_buying_power=1_000.0)

    message = pa.validate_plan_budget(plan)

    assert message == pa.REFUSAL_OVER_BUDGET_FMT.format(required=9_000.0, budget=1_000.0)


def test_validate_plan_budget_counts_the_money_this_plans_own_sells_free():
    """``total_buying_power`` is published PLUS released -- the same budget the
    scaler measures against -- so a rebalance funded by its own sells is not
    reported as over budget."""
    plan = AllocationPlan(rows=[_sendable_row("AAA")],
                          required_buying_power=5_000.0,
                          available_buying_power=1_000.0,
                          released_buying_power=4_500.0)
    assert pa.validate_plan_budget(plan) is None


def test_margin_info_tradable_defaults_to_unknown():
    """Tri-state, defaulting to None on exactly the same terms as
    ``fractionable``: an adapter that never sets it must not be read as having
    said no."""
    assert MarginInfo(symbol="AAA", bp_factor=1.0).tradable is None
