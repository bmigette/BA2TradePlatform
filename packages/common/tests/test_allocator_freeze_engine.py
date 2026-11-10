"""Allocator FREEZE: a frozen symbol is left exactly as it is and its CURRENT value is taken out
of the money the other members share (pure engine; no DB, no broker, no UI)."""
import pytest

from ba2_common.core import portfolio_allocation as pa
from ba2_common.core.portfolio_allocation import LabelTarget, PositionState, SymbolTarget

MARKET = pa.VALUATION_MODE_MARKET


def _pos(symbol, price, quantity=0.0, cost_basis=0.0):
    return PositionState(symbol=symbol, quantity=quantity, cost_basis=cost_basis, price=price,
                         transaction_ids=[1] if quantity else [])


def _labels():
    # ONE label, weights 50/30/20 of the whole base.
    return [LabelTarget("core", 100.0, [SymbolTarget("A", 50.0), SymbolTarget("B", 30.0),
                                        SymbolTarget("C", 20.0)])]


def _solve(current, frozen=None, base=100_000.0, labels=None, mode=MARKET, fractional=True):
    return pa.compute_allocation(
        base, 1_000_000.0, labels or _labels(), current, {}, allow_fractional=fractional,
        default_bp_factor=1.0, valuation_mode=mode, frozen_symbols=frozen)


def _row(plan, symbol):
    return next(r for r in plan.rows if r.symbol == symbol)


def test_worked_example_a_and_b_share_70k_as_50_to_30():
    current = {"C": _pos("C", 100.0, quantity=300.0)}  # worth 30k
    plan = _solve(current, frozen={"C"})
    assert _row(plan, "A").target_notional == pytest.approx(43_750.0)
    assert _row(plan, "B").target_notional == pytest.approx(26_250.0)
    c = _row(plan, "C")
    assert c.frozen is True
    assert c.delta_quantity == 0.0 and c.side is None and c.estimated_value == 0.0
    assert c.target_quantity == pytest.approx(300.0)
    assert c.target_notional == pytest.approx(30_000.0)
    assert plan.frozen_notional == pytest.approx(30_000.0)
    # nothing is idle: 43.75k + 26.25k + 30k == 100k
    assert sum(r.target_notional for r in plan.rows) == pytest.approx(100_000.0)


def test_frozen_without_position_contributes_zero_and_is_not_bought():
    plan = _solve({}, frozen={"C"})
    assert _row(plan, "A").target_notional == pytest.approx(100_000.0 * 50 / 80)
    assert _row(plan, "B").target_notional == pytest.approx(100_000.0 * 30 / 80)
    c = _row(plan, "C")
    assert c.frozen and c.delta_quantity == 0.0 and c.side is None
    assert plan.frozen_notional == 0.0


def test_all_members_frozen_places_no_order_at_all():
    current = {"A": _pos("A", 10.0, 100.0), "B": _pos("B", 10.0, 5.0)}
    plan = _solve(current, frozen={"A", "B", "C"})
    assert all(r.side is None and r.delta_quantity == 0.0 for r in plan.rows)
    assert plan.total_buy_value == 0.0 and plan.total_sell_value == 0.0
    assert all(r.frozen for r in plan.rows)


def test_frozen_value_above_capital_clamps_unfrozen_to_zero_and_says_so():
    current = {"C": _pos("C", 100.0, quantity=1_500.0),   # 150k > 100k
               "A": _pos("A", 100.0, quantity=100.0)}      # held, closed (target 0)
    plan = _solve(current, frozen={"C"})
    assert _row(plan, "A").target_notional == 0.0
    assert _row(plan, "B").target_notional == 0.0
    assert all(r.target_notional >= 0 for r in plan.rows)
    assert any("frozen" in w.lower() and "exceed" in w.lower() for w in plan.warnings)
    assert plan.frozen_notional == pytest.approx(150_000.0)


def test_unfreeze_restores_the_original_targets():
    current = {"C": _pos("C", 100.0, quantity=300.0)}
    normal = _solve(current)
    again = _solve(current, frozen=set())
    assert [r.target_notional for r in again.rows] == [r.target_notional for r in normal.rows]
    assert _row(normal, "C").target_notional == pytest.approx(20_000.0)
    assert not any(r.frozen for r in again.rows)


def test_frozen_symbol_keeps_its_holding_while_others_rebalance():
    current = {"C": _pos("C", 100.0, quantity=500.0),      # 50k, far above its 20% target
               "A": _pos("A", 100.0, quantity=600.0),      # 60k, above its new target
               "B": _pos("B", 100.0)}
    plan = _solve(current, frozen={"C"})
    assert _row(plan, "C").delta_quantity == 0.0 and _row(plan, "C").side is None
    a = _row(plan, "A")   # remaining capital 50k -> A 31.25k: sells
    assert a.target_notional == pytest.approx(50_000.0 * 50 / 80)
    assert a.delta_quantity < 0
    assert _row(plan, "B").delta_quantity > 0


def test_nothing_frozen_is_identical_to_the_old_call():
    current = {"A": _pos("A", 50.0, 700.0), "C": _pos("C", 100.0, 300.0)}
    old = pa.compute_allocation(100_000.0, 1_000_000.0, _labels(), current, {},
                                allow_fractional=False, default_bp_factor=1.0,
                                valuation_mode=MARKET)
    for frozen in (None, set(), {"NOTMANAGED"}):
        new = pa.compute_allocation(100_000.0, 1_000_000.0, _labels(), current, {},
                                    allow_fractional=False, default_bp_factor=1.0,
                                    valuation_mode=MARKET, frozen_symbols=frozen)
        assert new.to_dict() == old.to_dict()
    assert all(r.frozen is False for r in old.rows)


def test_symbol_match_is_case_insensitive():
    current = {"C": _pos("C", 100.0, quantity=300.0)}
    plan = _solve(current, frozen={"c"})
    assert _row(plan, "C").frozen is True


def test_cost_mode_takes_the_frozen_cost_basis_out_of_the_money():
    current = {"C": _pos("C", 100.0, quantity=300.0, cost_basis=20_000.0)}
    plan = _solve(current, frozen={"C"}, mode=pa.VALUATION_MODE_COST)
    assert plan.frozen_notional == pytest.approx(20_000.0)
    assert _row(plan, "A").target_notional == pytest.approx(80_000.0 * 50 / 80)


def test_frozen_held_without_a_price_is_skipped_and_warned_not_invented():
    current = {"C": PositionState(symbol="C", quantity=10.0, price=None),
               "A": _pos("A", 10.0), "B": _pos("B", 10.0)}
    plan = _solve(current, frozen={"C"})
    c = _row(plan, "C")
    assert c.skipped and c.delta_quantity == 0.0
    assert any("no price" in w.lower() and "C" in w for w in plan.warnings)
    assert plan.unallocatable_pct == 0.0  # a frozen symbol's share is not 'unallocatable'


def test_multi_label_frozen_symbol_is_taken_out_of_every_label():
    labels = [LabelTarget("one", 50.0, [SymbolTarget("A", 50.0), SymbolTarget("C", 50.0)]),
              LabelTarget("two", 50.0, [SymbolTarget("B", 50.0), SymbolTarget("C", 50.0)])]
    current = {"C": _pos("C", 100.0, quantity=200.0)}  # 20k
    plan = _solve(current, frozen={"C"}, labels=labels)
    # C's own target was 25k + 25k; the others share 100k - 20k = 80k as 25:25
    assert _row(plan, "A").target_notional == pytest.approx(40_000.0)
    assert _row(plan, "B").target_notional == pytest.approx(40_000.0)
    assert _row(plan, "C").delta_quantity == 0.0


# ---------------------------------------------------------------- INVEST_LABEL (compute_label_investment)

def _invest(current, amount=10_000.0, frozen=None, labels=None):
    return pa.compute_label_investment(
        (labels or _labels())[0], amount, current, {}, available_buying_power=1_000_000.0,
        allow_fractional=True, default_bp_factor=1.0, valuation_mode=MARKET, frozen_symbols=frozen)


def _priced():
    return {s: _pos(s, 100.0) for s in "ABC"}


def test_invest_label_shares_the_amount_among_the_unfrozen_members():
    plan = _invest(_priced(), frozen={"C"})
    assert _row(plan, "A").target_notional == pytest.approx(6_250.0)
    assert _row(plan, "B").target_notional == pytest.approx(3_750.0)
    # orders are whole shares here (no margin info says fractionable): 62.5 -> 63, 37.5 -> 37
    assert _row(plan, "A").delta_quantity > 0 and _row(plan, "B").delta_quantity > 0
    c =_row(plan, "C")
    assert c.frozen and c.side is None and c.delta_quantity == 0.0 and pa.REASON_FROZEN in c.reasons
    assert plan.total_buy_value == pytest.approx(10_000.0, abs=100.0)


def test_invest_label_with_every_member_frozen_buys_nothing_and_says_so():
    plan = _invest(_priced(), frozen={"A", "B", "C"})
    assert all(r.side is None and r.delta_quantity == 0.0 and r.frozen for r in plan.rows)
    assert plan.total_buy_value == 0.0
    assert any("every symbol of label 'core' is frozen" in w for w in plan.warnings)


def test_invest_label_nothing_frozen_is_identical_to_before():
    old = pa.compute_label_investment(_labels()[0], 10_000.0, _priced(), {},
                                      available_buying_power=1_000_000.0, allow_fractional=True,
                                      default_bp_factor=1.0, valuation_mode=MARKET)
    for frozen in (None, set(), {"NOTINLABEL"}):
        assert _invest(_priced(), frozen=frozen).to_dict() == old.to_dict()
    assert _row(old, "C").target_notional == pytest.approx(2_000.0)


def test_frozen_notional_survives_precheck_resolve_and_row_filtering():
    plan = _solve({"C": _pos("C", 100.0, quantity=300.0), "A": _pos("A", 100.0), "B": _pos("B", 100.0)},
                  frozen={"C"})
    assert pa.apply_order_impacts(plan, {}, available_buying_power=1_000_000.0).frozen_notional == 30_000.0
    assert pa.filter_plan_rows(plan, ["A", "B"]).frozen_notional == 30_000.0
