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
proceeds credited as buying power that funded other buys. Submission then refused the sale and
sent the buys anyway.

RESOLVED THE RIGHT WAY ROUND. Refusing the sale was correct given the constraint, but the
constraint was the defect: a ``Transaction`` links quantity to an EXPERT, and a manually-traded
account has none, so requiring one in order to sell a broker holding was the wrong shape. The
sale is now ROUTED as a plain closing order (``ACTION_SELL_UNTRACKED``) -- so its proceeds are
real again and correctly fund the plan. ``unsellable_symbols`` survives for a sale that
genuinely has no route, and is tested here as the mechanism it now is.
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


class TestACallerDeclaredUnroutableSaleFundsNothing:
    """The ``unsellable_symbols`` mechanism itself.

    No longer what an untracked broker holding is -- those are sold directly now -- but
    still the right answer for a sale a caller KNOWS cannot be submitted: it is planned
    and shown, and it funds nothing.
    """

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

    def test_a_buy_is_not_funded_by_a_sale_that_cannot_be_submitted(self):
        """With $0 of real buying power, a buy must not be sized off proceeds that
        cannot arrive."""
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


class TestHowAnUntrackedSaleIsRouted:
    """``decide_symbol_action`` is where the holding stops being a dead end."""

    def _row(self, **kw):
        return pa.AllocationRow(symbol="AAA", price=100.0, delta_quantity=-10.0,
                                side=pa.OrderDirection.SELL, target_quantity=0.0, **kw)

    def test_an_untracked_broker_holding_is_sold_directly(self):
        """THE CHANGE: shares at the broker with no transaction behind them used to be
        an ACTION_SKIP -- the same answer a symbol already at its target gets."""
        state = PositionState("AAA", quantity=10)
        assert pa.decide_symbol_action(self._row(), state) == pa.ACTION_SELL_UNTRACKED

    def test_a_TRACKED_holding_still_closes_through_its_transaction(self):
        """Untouched: a position this platform opened is closed the way it always was."""
        state = PositionState("AAA", quantity=10, transaction_ids=[7])
        assert pa.decide_symbol_action(self._row(), state) == pa.ACTION_CLOSE

    def test_an_option_filtered_holding_keeps_its_loud_refusal(self):
        """Real transactions exist and are deliberately held back; a different situation
        with a different remedy, so it keeps its own outcome."""
        state = PositionState("AAA", quantity=10, unactionable_transaction_ids=[41])
        assert pa.decide_symbol_action(self._row(), state) == pa.ACTION_UNACTIONABLE

    def test_a_caller_declared_unroutable_sale_is_still_loud(self):
        state = PositionState("AAA", quantity=10)
        assert pa.decide_symbol_action(self._row(untracked_unsellable=True),
                                       state) == pa.ACTION_UNACTIONABLE

    def test_nothing_held_is_still_a_skip(self):
        """No shares at the broker means there is no holding to sell, tracked or not."""
        assert (pa.decide_symbol_action(self._row(), PositionState("AAA", quantity=0))
                == pa.ACTION_SKIP)

    def test_a_BUY_is_untouched(self):
        row = pa.AllocationRow(symbol="AAA", price=100.0, delta_quantity=+10.0,
                               side=pa.OrderDirection.BUY, target_quantity=10.0)
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


class TestTheWeightColumnHasANowInIt:
    """``current_weight_pct``, added 2026-09-07.

    The dry-run's Weight column showed ``weight_pct -> projected_weight_pct``: the
    share ASKED for, and the share the plan will ACHIEVE. Both are forward-looking, so
    a symbol the account holds NOTHING of still opened with a non-zero weight and read
    as a position it did not have -- which is exactly how it was reported.

    All three divide the same ``base_notional`` on purpose: three percentages with
    different denominators cannot be compared by eye, which is the whole reason the
    column exists.
    """

    def _rows(self):
        current = {"HELD": PositionState("HELD", quantity=10, cost_basis=800, price=100),
                   "NEW": PositionState("NEW", price=100)}
        # HELD is deliberately OFF target (holds 1000, asked 500) -- dry_run_rows
        # excludes a row already at its target, so an on-target fixture would have
        # nothing to assert about.
        plan = _solve(2000, 2000, _labels(("HELD", 25), ("NEW", 75)), current, {})
        return {r["symbol"]: r for r in pa.dry_run_rows(plan)}

    def test_an_unheld_symbol_reports_zero_percent_NOW(self):
        """THE DEFECT: this was silently the TARGET share, so it was never 0."""
        assert self._rows()["NEW"]["current_weight_pct"] == 0.0

    def test_a_held_symbol_reports_what_it_actually_holds(self):
        """10 shares at 100 = 1000 of a 2000 base = 50%."""
        assert self._rows()["HELD"]["current_weight_pct"] == pytest.approx(50.0, abs=0.01)

    def test_the_asked_share_is_still_there_and_unchanged(self):
        """The pair is now NOW -> ASKED; the ask itself must not have moved."""
        rows = self._rows()
        assert rows["NEW"]["weight_pct"] > 0
        assert "projected_weight_pct" in rows["NEW"], "the achieved share moved to the tooltip, not away"

    def test_all_three_shares_divide_the_same_base(self):
        rows = self._rows()["HELD"]
        for key in ("current_weight_pct", "weight_pct", "projected_weight_pct"):
            assert 0.0 <= rows[key] <= 100.0


class TestCapitalRequiredIsALevelNotADelta:
    """``capital_required``, added 2026-09-07.

    The dry run had one buying-power column and it held the TRADE's effect
    (``order value x factor``). Renaming it "Cap req" made the name wrong: the
    capital a position ties up is ``projected market value x initial margin rate``,
    which is a LEVEL and answers a different question. Both now exist.
    """

    def _plan(self, factor, multiplier=2.0, rate=None):
        margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=factor, fractionable=False,
                                    initial_margin_rate=rate)}
        current = {"AAA": PositionState("AAA", price=100)}
        return pa.compute_allocation(
            1000, 10_000, _labels(("AAA", 100)), current, margin,
            allow_fractional=False, default_bp_factor=multiplier,
            valuation_mode=pa.VALUATION_MODE_MARKET)

    def _row(self, **kw):
        return {r["symbol"]: r for r in pa.dry_run_rows(self._plan(**kw))}["AAA"]

    def test_a_marginable_holding_ties_up_its_MEASURED_share_of_its_value(self):
        """A rate the broker published is the rate that is used -- 10 shares at 100
        projected = 1000 of stock holding 500 of capital at a measured 0.5."""
        row = self._row(factor=1.0, rate=0.5)
        assert row["projected_notional"] == pytest.approx(1000.0)
        assert row["capital_required"] == pytest.approx(500.0)

    def test_an_unrated_holding_ties_up_the_WHOLE_value(self):
        """No rate, no leverage. The factor is NOT divided by the account multiplier
        to manufacture one: that is what printed a confident 0.5 on symbols nobody
        had rated."""
        row = self._row(factor=1.0)
        assert row["capital_required"] == pytest.approx(row["projected_notional"])

    def test_a_cash_account_ties_up_the_whole_value(self):
        """No leverage anywhere: nothing about multiplier 1 can discount this."""
        row = self._row(factor=1.0, multiplier=1.0)
        assert row["capital_required"] == pytest.approx(row["projected_notional"])

    def test_cap_req_is_a_LEVEL_and_bp_effect_is_the_DELTA(self):
        """The distinction the split exists for: they are different numbers on the
        same row, and only one of them is about the order."""
        row = self._row(factor=1.0, rate=0.5)
        assert row["capital_required"] == pytest.approx(500.0)     # holding the position
        assert row["bp_effect"] == pytest.approx(-1000.0)          # placing the order

    def test_the_rate_is_clamped_to_a_rate(self):
        """A rate over 1.0 would claim a position ties up more capital than it is
        worth."""
        row = self._row(factor=4.0, rate=2.0)
        assert row["capital_required"] <= row["projected_notional"] + 0.01

    def test_a_nonsense_rate_falls_back_to_the_full_value(self):
        """Zero is not a measurement of "free"; it is an unusable rate."""
        row = self._row(factor=1.0, rate=0.0)
        assert row["capital_required"] == pytest.approx(row["projected_notional"])


class TestAnAssumedCapitalRequirementSaysSo:
    """``capital_required_estimated``.

    A symbol nobody has rated used to get the neutral factor divided by the account
    multiplier, which on a 2:1 account manufactured a 0.5 rate out of a placeholder --
    it ASSUMED the symbol was ordinary marginable, and printed the result to the cent
    so it read as a measurement. Reported by the operator: "seems capreq assumes a
    default lever of 2 even if it is unknown", then settled: "we need to assume a 1x by
    default nothing else".

    So the assumption is now NO LEVERAGE, and it is still flagged: 1x is the
    conservative end rather than a fabricated discount, but it is an assumption either
    way and a dry run must not present one as a broker fact.
    """

    def _row(self, margin):
        current = {"AAA": PositionState("AAA", price=100)}
        plan = pa.compute_allocation(
            1000, 10_000, _labels(("AAA", 100)), current, margin,
            allow_fractional=False, default_bp_factor=2.0,
            valuation_mode=pa.VALUATION_MODE_MARKET)
        return {r["symbol"]: r for r in pa.dry_run_rows(plan)}["AAA"]

    def test_an_unrated_symbol_is_flagged_as_an_estimate(self):
        row = self._row({})
        assert row["capital_required_estimated"] is True
        # Still computed -- a blank column would be less useful, not more honest --
        # and computed at 1x, the only assumption that cannot understate the cost.
        assert row["capital_required"] == pytest.approx(row["projected_notional"])

    def test_a_broker_measured_symbol_is_NOT_flagged(self):
        """The inverse: a real rate must not be dressed up as a guess either."""
        from ba2_common.core.account_types import MARGIN_SOURCE_POSITION

        margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0718, fractionable=False,
                                    initial_margin_rate=0.5359,
                                    source=MARGIN_SOURCE_POSITION)}
        row = self._row(margin)
        assert row["capital_required_estimated"] is False
        assert row["capital_required"] == pytest.approx(1000.0 * 0.5359, abs=0.5)


class TestThePrecheckMeasuresTheCapitalRequirement:
    """The way an unrated symbol STOPS being unrated, added 2026-09-07.

    TastyTrade publishes a per-symbol margin rate only for a symbol the account
    already holds, so a first-time buy has none and its capital requirement is the
    conservative 1x assumption. But the order precheck -- the same dry run the
    wizard already runs on every solve -- returns
    ``isolated_order_margin_requirement`` for exactly that order: the broker
    pricing the holding it does not yet rate. The adapter collected it and
    ``apply_order_impacts`` dropped it, so a market-hours dry run showed an assumed
    requirement on a row the broker had just measured.

    Operator: "we should be able to test during market open time the missing capreq
    when clicking dry run button from portfolio page". Market hours is the
    precondition -- TastyTrade will not price an order out of session -- so this is
    the mechanism that has to be in place for that test to mean anything.
    """

    def _plan(self):
        margin = {"AAA": MarginInfo(symbol="AAA", bp_factor=1.0, fractionable=False)}
        current = {"AAA": PositionState("AAA", price=100)}
        return pa.compute_allocation(
            1000, 10_000, _labels(("AAA", 100)), current, margin,
            allow_fractional=False, default_bp_factor=2.0,
            valuation_mode=pa.VALUATION_MODE_MARKET), margin

    def _checked(self, **impact_kw):
        plan, margin = self._plan()
        impact = OrderImpact("AAA", change_in_buying_power=-1000.0, **impact_kw)
        return pa.apply_order_impacts(plan, {"AAA": impact},
                                      available_buying_power=10_000, margin=margin)

    def _row(self, **impact_kw):
        return {r["symbol"]: r
                for r in pa.dry_run_rows(self._checked(**impact_kw))}["AAA"]

    def test_the_brokers_isolated_requirement_becomes_the_capital_required(self):
        """10 shares at 100 = 1000 of stock the broker says needs 530 of capital."""
        row = self._row(margin_requirement=530.0)
        assert row["capital_required"] == pytest.approx(530.0)
        assert row["capital_required_estimated"] is False

    def test_without_it_the_row_still_says_ASSUMED(self):
        """THE INVERSE, and the reason the flag is keyed on the rate rather than on
        margin_source: the precheck marks EVERY accepted buy as prechecked, so a
        source-keyed flag called an impact carrying no requirement a measurement."""
        row = self._row()
        assert row["capital_required_estimated"] is True
        assert row["capital_required"] == pytest.approx(row["projected_notional"])

    def test_a_zero_requirement_is_not_a_measurement_of_free(self):
        row = self._row(margin_requirement=0.0)
        assert row["capital_required_estimated"] is True

    def test_it_is_stored_as_a_RATE_so_it_survives_a_resize(self):
        """The impact prices the pre-scaling quantity. A dollar figure would be wrong
        the moment ``_apply_bp_scaling`` cut the row back; a rate stays right."""
        checked = self._checked(margin_requirement=530.0)
        row = checked.rows[0]
        assert row.initial_margin_rate == pytest.approx(0.53)
        assert row.margin_source == pa.MARGIN_SOURCE_PRECHECK

    def test_the_requirement_cannot_exceed_the_value_it_prices(self):
        row = self._row(margin_requirement=99_999.0)
        assert row["capital_required"] <= row["projected_notional"] + 0.01
