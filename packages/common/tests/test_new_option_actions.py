# packages/common/tests/test_new_option_actions.py
from datetime import date
from types import SimpleNamespace
import pytest

from ba2_common.core.TradeActions import create_action
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows. Sibling DB-seam tests
    (test_db_seam / test_threadlocal_db) repoint the global DB seam at their own
    temp sqlite without restoring it, so by the time this module runs the
    session DB may lack the trade_action_result table. Re-point to a fresh,
    fully-initialized sqlite for each test here so it is order-independent."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "actions.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Minimal options account capturing submit_option_order calls."""
    def __init__(self, spot=100.0):
        self.id = 1
        self._spot = spot
        self.submitted = []
    # capability + clock
    def _as_of_date(self):
        return date(2024, 6, 1)
    def get_balance(self):
        return 100_000.0

    def get_account_snapshot(self):
        """The double's balance IS its CASH — the intent this file has always tested.

        Completed for OPT-L5: ``cash_available_for_delivery`` reads
        ``AccountSnapshot.cash`` and must never fall back to total equity, so a double
        that published only ``get_balance()`` left the delivery gate unmeasurable.
        """
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=100_000.0, equity=100_000.0, net_liquidation=100_000.0)
    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot
    def get_current_price(self, symbol=None):
        return self._spot
    # chain: strikes around spot for both rights, 30 DTE
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        # Premium = intrinsic value + time value. Time value decays with OTM
        # distance from spot so farther-OTM wings are cheaper than the shorts
        # (required for a defined-risk net credit, e.g. iron condor); ATM stays
        # richest in time value (short straddle credit). Intrinsic value
        # (max(spot-strike,0) for calls, max(strike-spot,0) for puts) is added
        # for ITM contracts. This makes the premium curve CONVEX in strike, which
        # is what makes a long butterfly cost a small net debit
        # (lower.ask + upper.ask > 2*body.bid). Without intrinsic the model is a
        # symmetric V around spot and a butterfly comes out as a credit, which is
        # unrealistic. The OTM-only credit structures are unaffected because none
        # of their legs are ITM (intrinsic == 0 there).
        out = []
        for s in range(80, 121, 5):
            if option_type == OptionRight.CALL:
                otm_dist = max(float(s) - self._spot, 0.0)
                intrinsic = max(self._spot - float(s), 0.0)
            else:
                otm_dist = max(self._spot - float(s), 0.0)
                intrinsic = max(float(s) - self._spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm_dist) + intrinsic
            ask = round(bid + 0.2, 4)
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type==OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=date(2024, 6, 21), bid=round(bid, 4), ask=ask, last=round(bid, 4),
                open_interest=1000, delta=None, implied_volatility=None))
        return out
    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        order = SimpleNamespace(id=len(self.submitted) + 1, data={})
        self.submitted.append(dict(legs=legs, quantity=quantity,
                                   limit_price=limit_price, strategy=option_strategy))
        return order
    # unused abstract bits
    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order
    def get_option_quote(self, contract_symbol):
        return None
    def get_atm_implied_volatility(self, underlying):
        return 0.3
    def get_option_positions(self):
        return []
    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None
    def check_option_buying_power(self, required):
        return True
    def available_option_buying_power(self):
        return 100_000.0


def _mk(action_type, **kw):
    acct = FakeAccount()
    rec = SimpleNamespace(id=1, instance_id=None)
    act = create_action(ExpertActionType(action_type), "AAPL", acct,
                        SimpleNamespace(), None, rec, **kw)
    act.submit_to_broker = True
    return acct, act


def test_short_strangle_two_short_legs_credit():
    # SIZING REDUCED 20% -> 10% when the assignment-capacity gate went ENFORCING.
    # At 20% this sized 20 contracts off $1,000/contract of Reg-T naked margin — and
    # 20 short 90-strike puts is $180,000 of delivery on a $100,000 account, which is
    # now refused (pinned in test_option_assignment_capacity_wiring.py). The leg
    # construction this test exists to pin is unchanged; only the size is.
    acct, act = _mk("open_short_strangle", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=10.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "short_strangle"
    assert len(sub["legs"]) == 2
    assert all(l.side == OrderDirection.SELL for l in sub["legs"])
    assert sub["limit_price"] < 0   # credit
    assert sub["quantity"] >= 1


def test_short_straddle_same_strike_both_short():
    acct, act = _mk("open_short_straddle", strike_method="percent_otm",
                    strike_param=0.0, dte_min=20, dte_max=40, sizing=20.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "short_straddle"
    legs = sub["legs"]
    assert len(legs) == 2 and legs[0].strike == legs[1].strike
    assert all(l.side == OrderDirection.SELL for l in legs)
    assert sub["limit_price"] < 0


def test_iron_condor_four_legs_credit_defined_risk():
    # SIZING REDUCED 20% -> 5% when the assignment-capacity gate went ENFORCING.
    # A condor is sized off its WING WIDTH ($460/contract here), so 20% bought 43 of
    # them — $387,000 of short-put delivery on a $100,000 account. The wing does not
    # pay for the shares: the short put is assigned tonight, our long put is a choice
    # we make later. Refusal pinned in test_option_assignment_capacity_wiring.py.
    acct, act = _mk("open_iron_condor", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=5.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "iron_condor"
    legs = sub["legs"]
    assert len(legs) == 4
    sells = [l for l in legs if l.side == OrderDirection.SELL]
    buys = [l for l in legs if l.side == OrderDirection.BUY]
    assert len(sells) == 2 and len(buys) == 2
    assert sub["limit_price"] < 0  # net credit


def test_jade_lizard_three_legs_credit():
    acct, act = _mk("open_jade_lizard", strike_method="percent_otm",
                    strike_param=10.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "jade_lizard"
    legs = sub["legs"]
    assert len(legs) == 3
    assert sum(1 for l in legs if l.side == OrderDirection.SELL) == 2
    assert sum(1 for l in legs if l.side == OrderDirection.BUY) == 1
    assert sub["limit_price"] < 0


def test_call_butterfly_three_strikes_ratio_121_debit():
    acct, act = _mk("open_call_butterfly", strike_method="percent_otm",
                    strike_param=0.0, dte_min=20, dte_max=40, sizing=10.0,
                    wing_width_pct=10.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "call_butterfly"
    legs = sub["legs"]
    assert len(legs) == 3
    body = [l for l in legs if l.side == OrderDirection.SELL]
    wings = [l for l in legs if l.side == OrderDirection.BUY]
    assert len(body) == 1 and body[0].ratio_qty == 2
    assert len(wings) == 2 and all(w.ratio_qty == 1 for w in wings)
    assert sub["limit_price"] > 0  # net debit


def test_put_ratio_spread_buy1_sell2():
    acct, act = _mk("open_put_ratio_spread", strike_method="percent_otm",
                    strike_param=5.0, dte_min=20, dte_max=40, sizing=20.0,
                    wing_width_pct=5.0)
    res = act.execute()
    assert res["success"], res["message"]
    sub = acct.submitted[0]
    assert sub["strategy"] == "put_ratio_spread"
    legs = sub["legs"]
    buys = [l for l in legs if l.side == OrderDirection.BUY]
    sells = [l for l in legs if l.side == OrderDirection.SELL]
    assert len(buys) == 1 and buys[0].ratio_qty == 1
    assert len(sells) == 1 and sells[0].ratio_qty == 2
    assert buys[0].strike > sells[0].strike


# --------------------------------------------------------------------------- #
# Evaluator wiring: the REAL backtest/live path runs option actions through
# TradeActionEvaluator, NOT create_action directly. Three hardcoded action-type
# lists in the evaluator (the execute() routing list, the _create_trade_action
# option branch, and the _get_action_type_from_action class map) silently DROPPED
# the 6 new action types — so they never submitted, producing zero fills (-1e9
# sentinel) in the optimizer even though the action classes themselves work.
# These tests lock the wiring so a new option action can never be added to the
# action classes without also being routed by the evaluator.
# --------------------------------------------------------------------------- #
_NEW_OPTION_ACTION_CLASSES = {
    "OpenShortStraddleAction": ExpertActionType.OPEN_SHORT_STRADDLE,
    "OpenShortStrangleAction": ExpertActionType.OPEN_SHORT_STRANGLE,
    "OpenIronCondorAction": ExpertActionType.OPEN_IRON_CONDOR,
    "OpenJadeLizardAction": ExpertActionType.OPEN_JADE_LIZARD,
    "OpenCallButterflyAction": ExpertActionType.OPEN_CALL_BUTTERFLY,
    "OpenPutRatioSpreadAction": ExpertActionType.OPEN_PUT_RATIO_SPREAD,
}


@pytest.mark.parametrize("class_name,expected_type", list(_NEW_OPTION_ACTION_CLASSES.items()))
def test_evaluator_maps_new_option_action_classes(class_name, expected_type):
    """_get_action_type_from_action must resolve every new option action class to its
    ExpertActionType. If it returns None, execute() routes the action to the
    'unknown type' branch and silently drops it (the root cause of the zero-fill bug)."""
    import ba2_common.core.TradeActions as TA
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    action_cls = getattr(TA, class_name)
    inst = action_cls.__new__(action_cls)  # don't need a fully-constructed action
    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    resolved = ev._get_action_type_from_action(inst)
    assert resolved == expected_type, (
        f"{class_name} resolved to {resolved}, expected {expected_type}; the evaluator "
        f"would drop this action as 'unknown type' and it would never submit/fill"
    )


def test_evaluator_option_type_lists_cover_all_option_actions():
    """Every option ExpertActionType in the canonical get_option_action_values() must be
    routed by the evaluator's execute() order-creating list (so the action submits)."""
    from ba2_common.core.types import get_option_action_values
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_ACTION_TYPES

    canonical = {ExpertActionType(v) for v in get_option_action_values()}
    # CLOSE_OPTION is routed too but is not an "entry"; entry list = canonical - {CLOSE_OPTION}
    expected_entries = canonical - {ExpertActionType.CLOSE_OPTION}
    assert expected_entries.issubset(_OPTION_ENTRY_ACTION_TYPES), (
        f"missing from evaluator entry list: {expected_entries - _OPTION_ENTRY_ACTION_TYPES}"
    )


def test_evaluator_forwards_wing_width_pct():
    """_create_trade_action must forward wing_width_pct to the option action ctor so
    iron condor / jade lizard / butterfly / ratio-spread get their configured wing."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    assert "wing_width_pct" in _OPTION_ENTRY_PARAM_KEYS


# --------------------------------------------------------------------------- #
# Naked short-premium reserve = Reg-T margin, NOT full strike*100 cash. The old
# full-cash proxy ($22k for one AAPL contract) made short straddle/strangle, jade
# lizard, and put ratio spread impossible to size on a realistic account, so they
# never opened. The margin model (~20% of notional) reserves what a broker actually
# does for a naked short, making the structures sizeable.
# --------------------------------------------------------------------------- #
def test_naked_margin_far_below_full_cash():
    """Reg-T naked margin per contract must be a fraction (not the full strike*100)."""
    acct = FakeAccount(spot=250.0)
    full_cash = 225.0 * 100.0
    # Spot 250 / strike 225 PUT is 25 OTM: max(0.20*250-25, 0.10*250)*100 = max(25,25)*100 = 2500
    margin = acct.naked_margin_per_contract(225.0, option_type=OptionRight.PUT, spot=250.0)
    assert 0 < margin < full_cash
    assert margin == pytest.approx(2500.0)
    # Without a spot, falls back to 0.20*strike*100 = 4500 (still << full 22500 cash).
    assert acct.naked_margin_per_contract(225.0, option_type=OptionRight.PUT) == pytest.approx(4500.0)


def test_naked_margin_reg_t_direction_aware():
    """Reg-T: the OTM deduction is DIRECTION-aware and NEVER negative for an ITM short.

    Reg-T naked-short margin = premium + max(20%*underlying - OTM_amount, 10%*underlying)
    with OTM_amount = max(strike-spot, 0) for a CALL and max(spot-strike, 0) for a PUT.
    The old abs(spot-strike) wrongly REDUCED margin for ITM shorts (spot 100, short put
    strike 150 gave 1000 instead of 2000)."""
    acct = FakeAccount(spot=100.0)
    # ITM short PUT (spot 100, strike 150): OTM amount 0 -> full 20%*spot*100 = 2000
    # (the premium term is handled in cash, not by this bracket helper).
    assert acct.naked_margin_per_contract(150.0, option_type=OptionRight.PUT, spot=100.0) == pytest.approx(2000.0)
    # ITM short CALL (spot 100, strike 50): OTM amount 0 -> 2000 as well.
    assert acct.naked_margin_per_contract(50.0, option_type=OptionRight.CALL, spot=100.0) == pytest.approx(2000.0)
    # Far-OTM PUT (strike 50, spot 100): deduction exceeds the 20% bracket -> 10% floor = 1000.
    assert acct.naked_margin_per_contract(50.0, option_type=OptionRight.PUT, spot=100.0) == pytest.approx(1000.0)
    # Mildly OTM CALL (strike 115, spot 100): max(20-15, 10)*100 = 1000.
    assert acct.naked_margin_per_contract(115.0, option_type=OptionRight.CALL, spot=100.0) == pytest.approx(1000.0)


def test_naked_reserve_uses_margin_not_full_cash():
    """option_reserve_required for GENUINELY Reg-T-netted naked structures must use the
    margin model, not full cash. put_ratio_spread is deliberately EXCLUDED from this group
    (see test_jade_lizard_and_put_ratio_spread_reserve_match_alpaca below) -- Alpaca's real
    MLEG engine does NOT extend this discount to it."""
    acct = FakeAccount(spot=250.0)
    full_cash = 225.0 * 100.0 * 2
    # short_straddle shorts BOTH rights at one strike -> worst case over both is reserved
    # (no option_type needed); the single-sided strategies require the leg's right.
    for strat in ("short_straddle", "short_strangle", "naked_put"):
        kw = {} if strat == "short_straddle" else {"option_type": OptionRight.PUT}
        r = acct.option_reserve_required(strat, 2, strike=225.0, spot=250.0, **kw)
        assert 0 < r < full_cash, f"{strat} reserve {r} should be margin (< full cash {full_cash})"
    # A naked strategy without option_type fails LOUDLY (no silent direction default)...
    with pytest.raises(ValueError):
        acct.option_reserve_required("naked_put", 1, strike=225.0, spot=250.0)
    # ...except short_straddle, which reserves the worst case over both rights:
    # max(call: OTM=0 -> 0.20*250, put: OTM=25 -> max(50-25,25)) *100*2 = 5000*2.
    assert acct.option_reserve_required(
        "short_straddle", 2, strike=225.0, spot=250.0) == pytest.approx(10000.0)
    # cash-secured put stays fully secured (full strike*100 by design).
    assert acct.option_reserve_required("cash_secured_put", 1, strike=225.0) == pytest.approx(22500.0)


def test_jade_lizard_and_put_ratio_spread_reserve_match_alpaca():
    """Regression (2026-07-24): jade_lizard and put_ratio_spread previously sized/reserved off
    plain Reg-T naked_margin_per_contract, underestimating Alpaca's real MLEG margin requirement
    by ~9-10x (empirically confirmed via real paper-account submissions on account 3 -- Alpaca's
    risk engine does not extend Reg-T naked-margin netting to these non-simple combo shapes,
    unlike the 2-leg vertical / 4-leg iron condor cases above, which DO match Reg-T/max-loss
    exactly). Both now reserve at full notional for the naked component, netted by the credit
    collected -- verified exact against the real cost_basis Alpaca reported."""
    acct = OptionsAccountInterface

    # jade_lizard: real order (put strike 290, call wing width 17.5, net_credit 3.04) reported
    # Alpaca cost_basis=$30,446 for quantity=1.
    r = acct.option_reserve_required(
        "jade_lizard", 1, strike=290.0, spread_width=17.5, net_credit=3.04)
    assert r == pytest.approx(30446.0)
    # Previous (wrong) naked_margin_per_contract-only formula would have given ~$3,289 here --
    # assert the fix is materially more conservative, not just numerically different.
    assert r > 9 * 3289.0

    # put_ratio_spread: real order (short strike 290, net_credit 3.65) reported Alpaca
    # cost_basis=$859,050 for quantity=30 -> $28,635/contract.
    r = acct.option_reserve_required("put_ratio_spread", 30, strike=290.0, net_credit=3.65)
    assert r == pytest.approx(859_050.0)
    assert r / 30 == pytest.approx(28_635.0)

    # A net-debit put ratio spread (no credit to net against) reserves the full strike notional,
    # not a fraction -- no Reg-T discount applies here at all.
    r = acct.option_reserve_required("put_ratio_spread", 1, strike=200.0, net_credit=0.0)
    assert r == pytest.approx(20_000.0)


# ---------------------------------------------------------------------------
# option_reserve_required must not fail open on a strategy it does not know
# ---------------------------------------------------------------------------
def _submitted_option_strategies():
    """Every option_strategy literal TradeActions can hand to the broker.

    Read out of the SOURCE with ``ast`` rather than hard-coded, so the day someone
    adds an eighth credit builder the coverage test below fails until its capital
    requirement is priced. A hard-coded list would have to be edited by the same
    person who forgot the reserve branch, which is no guard at all."""
    import ast
    import inspect

    from ba2_common.core import TradeActions

    tree = ast.parse(inspect.getsource(TradeActions))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "_submit_option_order":
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    assert found, "the ast scan found no strategies — the scan itself is broken"
    return found


def test_an_unknown_strategy_raises_rather_than_reserving_nothing():
    """A strategy whose capital requirement is UNKNOWN must not be priced at zero.

    The function used to end in a bare ``return 0.0``, so any strategy outside its
    seven known branches reserved nothing and sailed through every buying-power
    gate — the newest structure on the platform was always the one the risk check
    could not see. Unknown is not free."""
    acct = OptionsAccountInterface
    for strategy in ("", "iron_butterfly", "broken_wing_condor", "SHORT_STRANGLE",
                     "short strangle", "cash_secured_put "):
        with pytest.raises(ValueError, match="option_reserve_required"):
            acct.option_reserve_required(strategy, 1, strike=100.0, spread_width=5.0,
                                         net_credit=1.0, spot=100.0,
                                         option_type=OptionRight.PUT)


def test_an_unknown_strategy_raises_even_for_a_zero_quantity():
    """The quantity short-circuit must not swallow the unknown-strategy error: an
    unrecognised name is a CODE defect and stays loud whatever the size is."""
    with pytest.raises(ValueError, match="option_reserve_required"):
        OptionsAccountInterface.option_reserve_required("iron_butterfly", 0, strike=100.0)


def test_the_genuinely_zero_reserve_strategies_are_named_and_still_reserve_zero():
    """The long/debit book legitimately reserves nothing — its max loss was paid at
    entry — and covered_call is covered by the 100 shares. Those are now an EXPLICIT
    list rather than the tail of a blanket default, so the answer 'zero' is a decision
    somebody made per strategy instead of the answer everything unknown gets."""
    acct = OptionsAccountInterface
    for strategy in ("long_call", "long_put", "bull_call_spread", "bear_put_spread",
                     "straddle", "strangle", "covered_call", "protective_put"):
        assert acct.option_reserve_required(strategy, 5, strike=150.0, spot=150.0,
                                            spread_width=5.0, net_credit=1.0,
                                            option_type=OptionRight.CALL) == 0.0


def test_every_strategy_the_platform_can_submit_has_a_priced_reserve():
    """No submittable structure may fall through to the unknown-strategy error.

    This is the other half of the raise: making the default loud is only safe if
    every strategy the platform actually opens is priced. Scanned out of
    TradeActions' source, so a new builder is caught here rather than in production."""
    acct = OptionsAccountInterface
    for strategy in sorted(_submitted_option_strategies()):
        acct.option_reserve_required(strategy, 1, strike=100.0, spread_width=5.0,
                                     net_credit=1.0, spot=100.0,
                                     option_type=OptionRight.PUT)


def test_the_eight_reserving_builders_still_price_the_same_dollars():
    """The raise must not disturb a single number the credit builders already reserve."""
    acct = OptionsAccountInterface
    assert acct.option_reserve_required("cash_secured_put", 2, strike=150.0) == 30_000.0
    assert acct.option_reserve_required("bear_call_spread", 1, spread_width=5.0,
                                        net_credit=1.5) == 350.0
    # The put mirror of the bear call spread: same defined-risk vertical arithmetic, and
    # sharing the branch is what keeps the two from drifting apart.
    assert acct.option_reserve_required("bull_put_spread", 1, spread_width=5.0,
                                        net_credit=1.5) == 350.0
    assert acct.option_reserve_required("credit_spread", 1, spread_width=5.0,
                                        net_credit=1.5) == 350.0
    assert acct.option_reserve_required("short_straddle", 2, strike=225.0,
                                        spot=250.0) == pytest.approx(10_000.0)
    assert acct.option_reserve_required("short_strangle", 1, strike=225.0, spot=250.0,
                                        option_type=OptionRight.PUT) == pytest.approx(2_500.0)
    assert acct.option_reserve_required("naked_put", 1, strike=225.0, spot=250.0,
                                        option_type=OptionRight.PUT) == pytest.approx(2_500.0)
    assert acct.option_reserve_required("iron_condor", 1, spread_width=5.0,
                                        net_credit=1.0) == 400.0
    assert acct.option_reserve_required("jade_lizard", 1, strike=290.0, spread_width=17.5,
                                        net_credit=3.04) == pytest.approx(30_446.0)
    assert acct.option_reserve_required("put_ratio_spread", 1, strike=200.0,
                                        net_credit=0.0) == 20_000.0


def test_the_two_strategy_lists_match_the_branches():
    """The zero list and the priced list must be disjoint, and every priced name must
    really reach a branch — otherwise a name in RESERVING_STRATEGIES with no branch
    would fall through, which is the exact hole the raise closed."""
    acct = OptionsAccountInterface
    assert not (acct.ZERO_RESERVE_STRATEGIES & acct.RESERVING_STRATEGIES)
    for strategy in sorted(acct.RESERVING_STRATEGIES):
        r = acct.option_reserve_required(strategy, 1, strike=100.0, spread_width=5.0,
                                         net_credit=1.0, spot=100.0,
                                         option_type=OptionRight.PUT)
        assert r > 0.0, f"{strategy} is listed as reserving but priced {r}"


# ---------------------------------------------------------------------------
# ...and the SAME hole, six branches further in: a KNOWN strategy whose SIZING
# INPUT is missing. `3d30442` closed the unknown-strategy fall-through; these six
# `return 0.0` guards were the identical fail-open one layer down. A reserve of
# zero passes `check_option_buying_power` unconditionally, so a cash_secured_put
# that arrived with no strike was affordable at any size.
# ---------------------------------------------------------------------------
MISSING_SIZING_INPUT = [
    ("cash_secured_put", dict(strike=None), "strike"),
    ("bear_call_spread", dict(spread_width=None, net_credit=1.0), "spread_width"),
    ("bear_call_spread", dict(spread_width=5.0, net_credit=None), "net_credit"),
    ("bull_put_spread", dict(spread_width=None, net_credit=1.0), "spread_width"),
    ("bull_put_spread", dict(spread_width=5.0, net_credit=None), "net_credit"),
    ("credit_spread", dict(spread_width=None, net_credit=1.0), "spread_width"),
    ("credit_spread", dict(spread_width=5.0, net_credit=None), "net_credit"),
    ("short_straddle", dict(strike=None), "strike"),
    ("short_strangle", dict(strike=None, option_type=OptionRight.PUT), "strike"),
    ("naked_put", dict(strike=None, option_type=OptionRight.PUT), "strike"),
    ("put_ratio_spread", dict(strike=None, net_credit=1.0), "strike"),
    ("jade_lizard", dict(strike=None, spread_width=5.0, net_credit=1.0), "strike"),
    ("jade_lizard", dict(strike=100.0, spread_width=None, net_credit=1.0), "spread_width"),
    ("iron_condor", dict(spread_width=None, net_credit=1.0), "spread_width"),
    # ``call_butterfly`` was here. It moved to ZERO_RESERVE_STRATEGIES (OPT-L6): it is a
    # DEBIT structure whose max loss is the premium already paid, so it answers 0.0 by
    # name and no longer has a priced branch that could be missing a sizing input. Its
    # reserve behaviour is pinned instead by test_option_reserve_lockstep.py, which checks
    # the list against what the BUILDER persists — the relationship that was broken.
    ("debit_spread", dict(spread_width=None, net_credit=1.0), "spread_width"),
]


@pytest.mark.parametrize("strategy,kw,missing", MISSING_SIZING_INPUT)
def test_a_known_strategy_with_a_missing_sizing_input_raises(strategy, kw, missing):
    """Unknown is not zero here either — and the message names the field."""
    with pytest.raises(ValueError, match="option_reserve_required") as e:
        OptionsAccountInterface.option_reserve_required(strategy, 3, **kw)
    assert missing in str(e.value)


@pytest.mark.parametrize("strategy,kw", [
    ("cash_secured_put", dict(strike=0.0)),
    ("cash_secured_put", dict(strike=-150.0)),
    ("short_straddle", dict(strike=0.0)),
    ("short_strangle", dict(strike=0.0, option_type=OptionRight.PUT)),
    ("naked_put", dict(strike=-1.0, option_type=OptionRight.PUT)),
    ("put_ratio_spread", dict(strike=0.0, net_credit=1.0)),
    ("jade_lizard", dict(strike=0.0, spread_width=5.0, net_credit=1.0)),
])
def test_a_non_positive_strike_is_a_missing_field_not_a_free_option(strategy, kw):
    """No listed equity option has a strike of zero, so a zero strike is an unpopulated
    field — and it prices delivery, and the reserve, at exactly nothing. The same rule
    ``option_lifecycle.put_assignment_cost`` already applies ("a strike of 0 is NOT a
    free put")."""
    with pytest.raises(ValueError, match="option_reserve_required"):
        OptionsAccountInterface.option_reserve_required(strategy, 3, **kw)


@pytest.mark.parametrize("strike", [None, 0.0, -225.0])
def test_naked_margin_per_contract_refuses_a_strike_it_cannot_use(strike):
    """The sibling guard. ``0.0`` here is a margin requirement of nothing on a naked
    short — the most fail-open answer in the file."""
    with pytest.raises(ValueError, match="naked_margin_per_contract"):
        OptionsAccountInterface.naked_margin_per_contract(
            strike, option_type=OptionRight.PUT, spot=250.0)


def test_the_only_zeros_left_are_the_two_legitimate_ones():
    """``quantity <= 0`` and ``ZERO_RESERVE_STRATEGIES`` by name are the only ways to
    get a zero out of this function — plus a genuinely computed one (a credit at least
    as wide as the spread has no max loss to reserve), which is arithmetic, not a
    missing field."""
    acct = OptionsAccountInterface
    assert acct.option_reserve_required("cash_secured_put", 0, strike=150.0) == 0.0
    assert acct.option_reserve_required("cash_secured_put", -3, strike=150.0) == 0.0
    assert acct.option_reserve_required("long_call", 5, strike=150.0) == 0.0
    assert acct.option_reserve_required("bear_call_spread", 1, spread_width=5.0,
                                        net_credit=5.0) == 0.0
    assert acct.option_reserve_required("iron_condor", 1, spread_width=5.0,
                                        net_credit=9.0) == 0.0


def test_a_missing_sizing_input_can_no_longer_pass_the_buying_power_gate():
    """The whole point, stated as the gate sees it. Before: reserve 0.0 ->
    ``check_option_buying_power(0.0)`` -> True, for any size, on any account."""
    acct = FakeAccount()
    assert acct.check_option_buying_power(0.0) is True      # unchanged, and correct
    with pytest.raises(ValueError):
        acct.check_option_buying_power(
            acct.option_reserve_required("cash_secured_put", 1_000, strike=None))


def test_a_reserve_refusal_stops_the_entry_and_submits_nothing(monkeypatch):
    """THE BLAST RADIUS of failing closed, pinned rather than assumed.

    ``_OptionEntryAction.execute`` catches broadly but routes through
    ``absorb_if_benign(e, InstanceNotFound)``, and ``ValueError`` is deliberately NOT
    benign (``failure_modes._BENIGN_DEFAULT`` is ``(OSError,)``), so the refusal
    PROPAGATES instead of becoming a quiet failed result. It keeps propagating through
    ``TradeActionEvaluator``'s phase-1 handler — also ``absorb_if_benign`` — and is
    caught per-candidate by ``TradeManager`` (``except Exception as submit_error`` at
    the funded-entry loop), which logs it with a traceback and moves to the next symbol.

    So the consequence of every guard in this file is: THIS symbol's entry is refused,
    loudly, and the run continues. Nothing reaches the broker. Identical to the blast
    radius ``3d30442`` established for the unknown-strategy raise."""
    acct = FakeAccount()

    def _missing_input(*a, **k):
        raise ValueError("option_reserve_required('cash_secured_put'): strike is missing")

    monkeypatch.setattr(acct, "option_reserve_required", _missing_input)
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    action = create_action(ExpertActionType("sell_cash_secured_put"), "AAPL", acct,
                           SimpleNamespace(), None, rec,
                           strike_method="percent_otm", strike_param=10.0,
                           dte_min=20, dte_max=40, sizing=20.0)
    action.submit_to_broker = True
    with pytest.raises(ValueError, match="option_reserve_required"):
        action.execute()
    assert acct.submitted == []
