"""Engine-side regressions for the 2026-09-07 portfolio-allocator audit (PA-02, PA-05).

The audit shipped ``reports/portfolio_allocator/portfolio_allocator_audit_probe.py``, which
asserts the FAULTY behaviour on purpose -- it is evidence that the defects were real, not a
guard that they stay fixed. These are the guard, and they assert the opposite.

PA-02 -- rounding recovery discarded the broker's prechecked cost. ``apply_order_impacts``
adopted ``impact.bp_cost`` but left ``bp_factor`` at the pre-broker estimate, and every later
sizing pass recomputes cost as ``value x bp_factor``. Two orders the broker had priced at $200
each were restored by ``_reclaim_rounding_slack`` at $100 each, and the plan reported "$200
required" against $200 available while the real commitment was $400.

PA-05 -- a holding with no local transaction behind it was sized, its sale planned, and its
proceeds credited as buying power that funded other buys. Submission then refused the sale
(there is nothing to close) and sent the buys anyway. The refusal is correct and stays; spending
money it was never going to produce is the defect.
"""
import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import (
    MarginInfo, OrderImpact, PositionState, is_untracked_holding,
)


def _labels(*pairs):
    return [pa.LabelTarget("L", 100, [pa.SymbolTarget(sym, pct) for sym, pct in pairs])]


def _solve(base_notional, buying_power, labels, current, margin, **kw):
    return pa.compute_allocation(
        base_notional, buying_power, labels, current, margin,
        allow_fractional=False, default_bp_factor=1,
        valuation_mode=pa.VALUATION_MODE_MARKET, **kw)


class TestIsUntrackedHolding:
    """The discriminator. Three states look alike from outside and mean different things."""

    def test_shares_at_the_broker_with_no_transactions_at_all(self):
        assert is_untracked_holding(PositionState("AAA", quantity=10)) is True

    def test_a_tracked_holding_is_not_untracked(self):
        assert is_untracked_holding(
            PositionState("AAA", quantity=10, transaction_ids=[1])) is False

    def test_an_option_filtered_holding_is_not_untracked(self):
        """Real transactions exist and are deliberately held back -- that case already
        had its own loud outcome, and must keep it."""
        assert is_untracked_holding(
            PositionState("AAA", quantity=10, unactionable_transaction_ids=[41])) is False

    def test_holding_nothing_is_not_untracked(self):
        assert is_untracked_holding(PositionState("AAA", quantity=0)) is False
        assert is_untracked_holding(None) is False


class TestAnUnsellableHoldingFundsNothing:
    """PA-05. The money is the defect; the visible intent is not."""

    def _plan(self, **kw):
        current = {"AAA": PositionState("AAA", quantity=10, cost_basis=1000, price=100),
                   "BBB": PositionState("BBB", price=100)}
        return _solve(1000, 0, _labels(("AAA", 0), ("BBB", 100)), current, {}, **kw)

    def test_the_sale_releases_no_buying_power(self):
        plan = self._plan(unsellable_symbols={"AAA"})
        aaa = next(r for r in plan.rows if r.symbol == "AAA")
        assert aaa.bp_released == 0.0

    def test_the_row_is_flagged_and_says_why(self):
        plan = self._plan(unsellable_symbols={"AAA"})
        aaa = next(r for r in plan.rows if r.symbol == "AAA")
        assert aaa.untracked_unsellable is True
        assert pa.REASON_UNTRACKED_NO_SELL in aaa.reasons

    def test_the_reduction_stays_VISIBLE(self):
        """Deliberately not suppressed. The operator asked to exit; the size of what
        cannot happen is the useful part, and hiding it would make an impossible
        reduction look like a symbol already at target."""
        plan = self._plan(unsellable_symbols={"AAA"})
        aaa = next(r for r in plan.rows if r.symbol == "AAA")
        assert aaa.delta_quantity == -10.0

    def test_the_buy_is_not_funded_by_the_phantom_sale(self):
        """THE DEFECT, in one number: with $0 of real buying power the buy side must
        not be sized off proceeds that cannot arrive."""
        funded = self._plan()                                   # old behaviour
        starved = self._plan(unsellable_symbols={"AAA"})        # fixed
        bbb_funded = next(r for r in funded.rows if r.symbol == "BBB")
        bbb_starved = next(r for r in starved.rows if r.symbol == "BBB")
        assert bbb_funded.delta_quantity > 0, "premise: the phantom sale used to fund it"
        assert bbb_starved.delta_quantity == 0

    def test_a_TRACKED_sale_still_funds_normally(self):
        """The inverse, and the one that matters for every healthy account: an ordinary
        holding must keep releasing its buying power."""
        plan = self._plan()
        aaa = next(r for r in plan.rows if r.symbol == "AAA")
        assert aaa.bp_released > 0
        assert aaa.untracked_unsellable is False

    def test_default_is_exactly_todays_behaviour(self):
        """``unsellable_symbols`` defaults to None so no existing caller changes."""
        a = self._plan()
        b = self._plan(unsellable_symbols=set())
        assert [(r.symbol, r.delta_quantity, r.bp_released) for r in a.rows] == \
               [(r.symbol, r.delta_quantity, r.bp_released) for r in b.rows]


class TestAnUnsellableRowIsLoudAtSubmission:
    """PA-05's reporting half: not a skip, because a skip is what a symbol already at
    its target gets, and a skip lets the run be reported SUCCESS."""

    def _row(self, **kw):
        return pa.AllocationRow(symbol="AAA", price=100.0, delta_quantity=-10.0,
                                side=pa.OrderDirection.SELL, target_quantity=0.0, **kw)

    def test_a_flagged_row_is_unactionable(self):
        state = PositionState("AAA", quantity=10)
        assert pa.decide_symbol_action(self._row(untracked_unsellable=True),
                                       state) == pa.ACTION_UNACTIONABLE

    def test_an_unflagged_untracked_state_keeps_its_pre_existing_skip(self):
        """Pinned by test_decide_symbol_action_an_untracked_broker_position_is_still_a
        _plain_skip. The decision is keyed on the PLAN's flag, never inferred from the
        state, because an empty transaction_ids means 'untracked' only when the caller
        populates ids at all."""
        state = PositionState("AAA", quantity=10)
        assert pa.decide_symbol_action(self._row(), state) == pa.ACTION_SKIP

    def test_a_flagged_BUY_is_untouched(self):
        """Only the sell side has nowhere to go; topping a holding up is a new
        transaction and always was fine."""
        row = pa.AllocationRow(symbol="AAA", price=100.0, delta_quantity=+10.0,
                               side=pa.OrderDirection.BUY, target_quantity=10.0,
                               untracked_unsellable=True)
        assert pa.decide_symbol_action(row, PositionState("AAA", quantity=10)) == pa.ACTION_NEW


class TestThePrecheckedCostSurvivesRounding:
    """PA-02. The broker's answer must outlive the pass that adopted it."""

    def _checked(self):
        margin = {s: MarginInfo(symbol=s, bp_factor=1, fractionable=False)
                  for s in ("AAA", "BBB")}
        plan = _solve(200, 200, _labels(("AAA", 50), ("BBB", 50)),
                      {s: PositionState(s, price=100) for s in margin}, margin)
        return pa.apply_order_impacts(
            plan, {s: OrderImpact(s, change_in_buying_power=-200) for s in margin},
            available_buying_power=200, margin=margin), margin

    def test_the_brokers_rate_is_carried_not_just_its_total(self):
        checked, _ = self._checked()
        for row in checked.buy_rows:
            assert row.bp_factor == pytest.approx(2.0), (
                "the broker charged 2x the estimate; every later sizing pass must use "
                "that rate, not the pre-broker 1.0")

    def test_the_plan_no_longer_understates_what_it_commits(self):
        """THE DEFECT: two $200-prechecked orders were reported as needing $200 total."""
        checked, _ = self._checked()
        true_cost = sum(abs(r.delta_quantity) * 100 * 2.0 for r in checked.buy_rows)
        assert checked.required_buying_power == pytest.approx(true_cost)

    def test_it_does_not_silently_fit_a_budget_it_cannot(self):
        checked, _ = self._checked()
        committed = sum(r.bp_cost for r in checked.buy_rows)
        assert committed <= 200 + pa.MONEY_EPSILON, (
            "either the orders fit the $200 that was available, or they were dropped -- "
            "what must not happen is reporting a fit that the broker priced at $400")
