"""LABEL-ONLY rebalance: one label's CURRENT money is re-split by its symbol shares; the label's
total and every other label are left alone (pure engine; no DB, no broker, no UI)."""
import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import LabelTarget, PositionState, SymbolTarget
from ba2_common.core.types import OrderDirection

MARKET = pa.VALUATION_MODE_MARKET
COST = pa.VALUATION_MODE_COST


def _pos(symbol, price, quantity=0.0, cost_basis=0.0):
    return PositionState(symbol=symbol, quantity=quantity, cost_basis=cost_basis, price=price,
                         transaction_ids=[1] if quantity else [])


def _label(weights, target_pct=30.0, name="core"):
    return LabelTarget(name, target_pct, [SymbolTarget(s, w) for s, w in weights.items()])


def _solve(label, current, *, buying_power=0.0, mode=MARKET, fractional=True, frozen=None):
    return pa.compute_label_rebalance(
        label, current, {}, available_buying_power=buying_power, allow_fractional=fractional,
        default_bp_factor=1.0, valuation_mode=mode, frozen_symbols=frozen)


def _row(plan, symbol):
    return next(r for r in plan.rows if r.symbol == symbol)


def _book():
    # A is worth 600, B 400: the label holds 1,000.
    return {"A": _pos("A", 10.0, quantity=60.0, cost_basis=300.0),
            "B": _pos("B", 20.0, quantity=20.0, cost_basis=500.0)}


def test_the_run_is_its_own_mode_with_a_stable_stored_name():
    assert pa.ALLOCATION_MODE_REBALANCE_LABEL == "REBALANCE_LABEL"
    assert "ALLOCATION_MODE_REBALANCE_LABEL" in pa.__all__
    assert "compute_label_rebalance" in pa.__all__ and "label_rebalance_block" in pa.__all__


def test_the_labels_current_money_is_split_by_its_symbol_shares():
    plan = _solve(_label({"A": 50.0, "B": 50.0}), _book())
    assert plan.base_notional == pytest.approx(1_000.0)
    a, b = _row(plan, "A"), _row(plan, "B")
    assert a.target_notional == pytest.approx(500.0) and b.target_notional == pytest.approx(500.0)
    assert a.side == OrderDirection.SELL and a.delta_quantity == pytest.approx(-10.0)
    assert b.side == OrderDirection.BUY and b.delta_quantity == pytest.approx(5.0)
    # the label's total does not move: what is sold is what is bought
    assert plan.total_sell_value == pytest.approx(100.0)
    assert plan.total_buy_value == pytest.approx(100.0)


def test_the_labels_portfolio_target_plays_no_part():
    small = _solve(_label({"A": 50.0, "B": 50.0}, target_pct=5.0), _book())
    large = _solve(_label({"A": 50.0, "B": 50.0}, target_pct=90.0), _book())
    assert [(r.symbol, r.delta_quantity) for r in small.rows] == \
           [(r.symbol, r.delta_quantity) for r in large.rows]
    # the plan records the label at 100% of ITS OWN money, so the target-total gate passes
    assert [lt.target_pct for lt in small.labels] == [100.0]
    assert pa.blocking_messages(pa.validate_label_targets(small.labels)) == []


def test_no_cash_reserve_is_taken_and_the_plan_is_a_position_plan():
    plan = _solve(_label({"A": 50.0, "B": 50.0}), _book())
    assert plan.reserved_pct == 0.0 and plan.reserved_notional == 0.0
    assert plan.allocation_basis == pa.ALLOCATION_BASIS_POSITION


def test_the_sells_fund_the_buys_so_no_buying_power_is_needed():
    plan = _solve(_label({"A": 50.0, "B": 50.0}), _book(), buying_power=0.0)
    assert plan.scale_factor == pytest.approx(1.0)
    assert _row(plan, "B").delta_quantity == pytest.approx(5.0)


def test_money_held_outside_the_label_is_not_counted_and_not_traded():
    current = dict(_book(), Z=_pos("Z", 50.0, quantity=100.0, cost_basis=5_000.0))
    plan = _solve(_label({"A": 50.0, "B": 50.0}), current, buying_power=9_999.0)
    assert plan.base_notional == pytest.approx(1_000.0)
    assert sorted(r.symbol for r in plan.rows) == ["A", "B"]


def test_cost_mode_measures_the_label_by_what_was_paid():
    # paid 300 for A and 500 for B: the label's money is 800 -> 400 each
    plan = _solve(_label({"A": 50.0, "B": 50.0}), _book(), mode=COST)
    assert plan.base_notional == pytest.approx(800.0)
    assert plan.valuation_mode == COST
    assert _row(plan, "A").target_notional == pytest.approx(400.0)
    assert _row(plan, "A").side == OrderDirection.BUY       # 300 paid, 400 wanted
    assert _row(plan, "B").side == OrderDirection.SELL      # 500 paid, 400 wanted


def test_a_symbol_at_zero_is_closed_and_its_money_goes_to_the_others():
    plan = _solve(_label({"A": 0.0, "B": 100.0}), _book())
    a, b = _row(plan, "A"), _row(plan, "B")
    assert a.delta_quantity == pytest.approx(-60.0) and pa.REASON_CLOSE_TO_ZERO in a.reasons
    assert b.target_notional == pytest.approx(1_000.0)
    assert b.delta_quantity == pytest.approx(30.0)          # 600 more of B at 20


def test_a_symbol_repeated_in_the_label_counts_its_money_once():
    label = LabelTarget("core", 30.0, [SymbolTarget("A", 25.0), SymbolTarget("A", 25.0),
                                       SymbolTarget("B", 50.0)])
    plan = _solve(label, _book())
    assert plan.base_notional == pytest.approx(1_000.0)


def test_a_frozen_member_is_left_alone_and_the_rest_share_what_is_left():
    current = dict(_book(), C=_pos("C", 10.0, quantity=100.0, cost_basis=900.0))   # +1,000
    plan = _solve(_label({"A": 25.0, "B": 25.0, "C": 50.0}), current, frozen={"C"})
    c = _row(plan, "C")
    assert c.frozen and c.side is None and c.delta_quantity == 0.0
    # 2,000 in the label, 1,000 of it frozen: A and B share the other 1,000 evenly
    assert _row(plan, "A").target_notional == pytest.approx(500.0)
    assert _row(plan, "B").target_notional == pytest.approx(500.0)


def test_a_label_that_holds_nothing_orders_nothing_and_says_why():
    plan = _solve(_label({"A": 50.0, "B": 50.0}),
                  {"A": _pos("A", 10.0), "B": _pos("B", 20.0)}, buying_power=5_000.0)
    assert plan.base_notional == 0.0
    assert all(r.side is None and r.delta_quantity == 0.0 for r in plan.rows)
    assert pa.WARNING_LABEL_HOLDS_NOTHING_FMT.format(label="core") in plan.warnings


def test_money_inputs_are_never_guessed():
    with pytest.raises(ValueError):
        _solve(_label({"A": 100.0}), _book(), buying_power=None)
    with pytest.raises(ValueError):
        _solve(_label({"A": 100.0}), _book(), mode="guess")
    with pytest.raises(ValueError):
        _solve(None, _book())


# -- a symbol that also belongs to another managed label ------------------------------------

def test_a_label_with_no_shared_symbol_may_be_rebalanced_alone():
    core = _label({"A": 50.0, "B": 50.0})
    other = _label({"Z": 100.0}, name="income")
    assert pa.label_rebalance_block(core, [core, other]) is None


def test_a_symbol_shared_with_another_label_refuses_the_label_only_run():
    core = _label({"A": 50.0, "B": 50.0})
    other = _label({"B": 40.0, "Z": 60.0}, name="income")
    reason = pa.label_rebalance_block(core, [core, other])
    assert reason == pa.LABEL_REBALANCE_SHARED_BLOCK_FMT.format(label="core", shared="B (income)")
    # the other way round too, and matched whatever the case of the ticker
    assert "B (core)" in pa.label_rebalance_block(other, [core, other])
    lower = _label({"b": 100.0}, name="lower")
    assert pa.label_rebalance_block(lower, [lower, other]) is not None


# -- the label's total is KEPT: nothing may leak to cash or be bought with account money -----

def test_shares_totalling_under_100_are_scaled_so_nothing_is_sold_into_cash():
    # 30/30 on a label holding 500 + 500: the full run would leave 40% as cash; here it is 50/50
    current = {"A": _pos("A", 10.0, quantity=50.0), "B": _pos("B", 20.0, quantity=25.0)}
    plan = _solve(_label({"A": 30.0, "B": 30.0}), current)
    assert plan.total_sell_value == 0.0 and plan.total_buy_value == 0.0
    assert _row(plan, "A").target_notional == pytest.approx(500.0)
    assert sum(st.weight_pct for st in plan.labels[0].symbols) == pytest.approx(100.0)
    assert pa.WARNING_LABEL_SHARES_SCALED_FMT.format(label="core", total=60.0) in plan.warnings


def test_scaled_shares_keep_their_proportions():
    plan = _solve(_label({"A": 60.0, "B": 20.0}), _book())      # 60/20 of 80 -> 75/25 of 1,000
    assert _row(plan, "A").target_notional == pytest.approx(750.0)
    assert _row(plan, "B").target_notional == pytest.approx(250.0)
    assert plan.total_sell_value == pytest.approx(plan.total_buy_value)


def test_shares_at_100_are_not_reported_as_scaled():
    plan = _solve(_label({"A": 50.0, "B": 50.0}), _book())
    assert not [w for w in plan.warnings if "scaled" in w]


def test_shares_over_100_are_left_for_the_target_gate_to_refuse():
    plan = _solve(_label({"A": 80.0, "B": 80.0}), _book())
    assert pa.blocking_messages(pa.validate_label_targets(plan.labels)) != []


def test_a_label_with_every_share_at_zero_is_refused_not_liquidated():
    label = _label({"A": 0.0, "B": 0.0})
    assert pa.label_rebalance_block(label, [label]) == \
        pa.LABEL_REBALANCE_NO_SHARE_BLOCK_FMT.format(label="core")
    with pytest.raises(ValueError, match="no symbol share above 0%"):
        _solve(label, _book())


def test_a_held_member_without_a_price_refuses_the_run():
    # cost mode: A's 800 would count as label money while A itself cannot trade, so B would be
    # bought up with the ACCOUNT's money and the label would grow
    current = {"A": _pos("A", None, quantity=60.0, cost_basis=800.0),
               "B": _pos("B", 20.0, quantity=5.0, cost_basis=100.0)}
    label = _label({"A": 50.0, "B": 50.0})
    assert pa.label_rebalance_block(label, [label], current) == \
        pa.LABEL_REBALANCE_NO_PRICE_BLOCK_FMT.format(label="core", symbols="A")
    with pytest.raises(ValueError, match="no price"):
        _solve(label, current, mode=COST)


def test_a_wanted_member_without_a_price_refuses_the_run():
    # A has a share but no price: its share would become cash and B would be SOLD down to 50%
    current = {"A": _pos("A", None), "B": _pos("B", 20.0, quantity=50.0)}
    label = _label({"A": 50.0, "B": 50.0})
    assert "A" in pa.label_rebalance_block(label, [label], current)
    with pytest.raises(ValueError, match="no price"):
        _solve(label, current)


def test_an_unheld_member_at_zero_without_a_price_is_no_obstacle():
    current = {"A": _pos("A", None), "B": _pos("B", 20.0, quantity=50.0)}
    label = _label({"A": 0.0, "B": 100.0})
    assert pa.label_rebalance_block(label, [label], current) is None
    assert _solve(label, current).total_sell_value == 0.0


def test_a_short_member_refuses_the_run():
    current = dict(_book(), A=_pos("A", 10.0, quantity=-60.0, cost_basis=-600.0))
    label = _label({"A": 50.0, "B": 50.0})
    assert pa.label_rebalance_block(label, [label], current) == \
        pa.LABEL_REBALANCE_SHORT_BLOCK_FMT.format(label="core", symbols="A")
    with pytest.raises(ValueError, match="short"):
        _solve(label, current)
