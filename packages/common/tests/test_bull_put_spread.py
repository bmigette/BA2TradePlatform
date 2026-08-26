"""``open_bull_put_spread`` — the put CREDIT vertical, as a first-class entry action.

THE GAP THIS CLOSES. After the option grid's defined-risk filter removes the short
straddle/strangle and the capital filter strips ``O_CSP``/``O_JL``/``O_RS`` (a CSP
reserves ~$28,800 at spot 320 against a $10,000 default capital), the searched credit
residue was ``O_IC`` and ``O_BEARCS`` only — one neutral, one bearish. **The sell arm had
no bullish defined-risk credit expression at all.** The put credit spread is the
canonical defined-risk income structure and it was absent repo-wide.

THE STRUCTURE. SELL the higher-strike put, BUY the lower-strike put, same expiry. Net
CREDIT (``short.bid - long.ask``), so the limit price is NEGATIVE (Alpaca MLEG
convention). Max loss = ``(width - credit) x 100 x contracts``, which is what is
reserved. Bullish/neutral: it pays as long as the underlying stays above the short
strike.

WHY IT IS *NOT* ``PremiumSeller.structures.build_put_credit_spread``. That helper
bypasses ``TradeActions`` entirely, so it gets no reserve, no assignment-capacity gate,
and — because it is absent from ``BacktestAccount.DEFINED_RISK_SHORT_STRATEGIES`` — no
MTM clamp and no unit combo-expiry settlement. It is not a drop-in, which is why this is
a port rather than a re-export.

WHAT THE LONG WING DOES *NOT* DO. It does not reduce the assignment bill. The short leg
can be assigned tonight; exercising our own lower-strike put is a choice we make later,
after the shares have already been paid for. That is pinned here and in
``test_option_assignment_capacity_wiring.py``.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeActions as TA
from ba2_common.core.TradeActions import create_action
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import (
    ExpertActionType, OptionRight, OrderDirection,
    get_option_action_values, is_option_action,
)

#: Frozen clock. Never "today": a selection window that drifts with the wall clock
#: passes and fails for reasons that have nothing to do with the structure.
TODAY = date(2024, 6, 1)
EXPIRY = date(2024, 6, 21)


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows. Sibling DB-seam tests repoint the
    global DB seam at their own temp sqlite without restoring it, so re-point to a
    fresh, fully-initialized sqlite for each test here (order-independence)."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "bull_put_spread.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Minimal options account capturing ``submit_option_order`` calls.

    Chain: strikes 80..120 step 5, both rights, one expiry. Premium = intrinsic + a time
    value that decays with OTM distance, so the farther-OTM long wing is always cheaper
    than the nearer short leg — which is what makes a vertical a genuine net credit.
    """

    def __init__(self, spot=100.0, balance=100_000.0):
        self.id = 1
        self._spot = spot
        self._balance = balance
        self.submitted = []

    def _as_of_date(self):
        return TODAY

    def get_balance(self):
        return self._balance

    def get_account_snapshot(self):
        """The double's balance IS its CASH — the intent this file has always tested.

        Completed for OPT-L5: ``cash_available_for_delivery`` reads
        ``AccountSnapshot.cash`` and must never fall back to total equity, so a double
        that published only ``get_balance()`` left the delivery gate unmeasurable.
        """
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self._balance, equity=self._balance, net_liquidation=self._balance)

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    def get_current_price(self, symbol=None):
        return self._spot

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        out = []
        for s in range(80, 121, 5):
            if option_type == OptionRight.CALL:
                otm_dist = max(float(s) - self._spot, 0.0)
                intrinsic = max(self._spot - float(s), 0.0)
            else:
                otm_dist = max(self._spot - float(s), 0.0)
                intrinsic = max(float(s) - self._spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm_dist) + intrinsic
            out.append(OptionContract(
                symbol=f"{underlying}{s}{'C' if option_type == OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(s),
                expiry=EXPIRY, bid=round(bid, 4), ask=round(bid + 0.2, 4),
                last=round(bid, 4), open_interest=1000, delta=None,
                implied_volatility=None))
        return out

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity,
                                   limit_price=limit_price, strategy=option_strategy,
                                   order_type=order_type))
        return SimpleNamespace(id=len(self.submitted), data={})

    # --- unused abstract bits
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


#: Sized so more than one contract is bought on the $100k account: a bug that charges or
#: reserves a flat single contract is then discriminated.
BPS = dict(strike_method="percent_otm", strike_param=[5.0, 10.0], dte_min=10, dte_max=40,
           sizing=5.0)


def act(acct, action_type=ExpertActionType.OPEN_BULL_PUT_SPREAD.value, **kw):
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    a = create_action(ExpertActionType(action_type), "XYZ", acct,
                      SimpleNamespace(), None, rec, **kw)
    a.submit_to_broker = True
    return a


def _legs(sub):
    short = [l for l in sub["legs"] if l.side == OrderDirection.SELL]
    long_ = [l for l in sub["legs"] if l.side == OrderDirection.BUY]
    return short, long_


# ==========================================================================
# the enum + the registries it must appear in
# ==========================================================================
def test_the_action_type_exists_with_the_canonical_value():
    assert ExpertActionType.OPEN_BULL_PUT_SPREAD.value == "open_bull_put_spread"


def test_it_is_classified_as_an_option_action():
    """``is_option_action`` is what routes a rule down the option path in
    ``rule_builders.action_from_rule``, ``ruleset_meta``, the live settings UI and the
    backtest engine's ``_entry_is_option`` flag. Miss this one registry and the action
    silently becomes an unknown equity action rather than erroring."""
    assert "open_bull_put_spread" in get_option_action_values()
    assert is_option_action("open_bull_put_spread")


def test_create_action_dispatches_to_the_builder():
    a = act(FakeAccount(), **BPS)
    assert type(a).__name__ == "OpenBullPutSpreadAction"


def test_the_evaluator_can_name_the_action_class_it_built():
    """``TradeActionEvaluator._get_action_type_from_action`` maps by CLASS NAME; an
    unmapped class returns None and the action sorts last with no action type recorded."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    a = act(FakeAccount(), **BPS)
    assert ev._get_action_type_from_action(a) == ExpertActionType.OPEN_BULL_PUT_SPREAD


def test_it_is_documented_so_it_is_discoverable():
    from ba2_common.core.rules_documentation import get_action_type_documentation

    doc = get_action_type_documentation()["open_bull_put_spread"]
    assert doc["name"]
    assert "credit" in doc["description"].lower()
    assert doc["use_cases"] and doc["parameters"] and doc["example"]


# ==========================================================================
# leg construction
# ==========================================================================
def test_it_sells_the_higher_strike_put_and_buys_the_lower_one():
    """The whole structure in one assertion: two PUTs, one expiry, SHORT above LONG."""
    acct = FakeAccount()
    res = act(acct, **BPS).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    assert sub["strategy"] == "bull_put_spread"
    short, long_ = _legs(sub)
    assert len(sub["legs"]) == 2 and len(short) == 1 and len(long_) == 1
    assert all(l.option_type == OptionRight.PUT for l in sub["legs"])
    assert short[0].strike > long_[0].strike
    assert short[0].expiry == long_[0].expiry
    assert short[0].position_intent == "sell_to_open"
    assert long_[0].position_intent == "buy_to_open"


def test_the_short_leg_comes_first_so_the_broker_reads_it_as_a_credit():
    """Mirrors ``bear_call_spread``: the SHORT leg leads the MLEG order."""
    acct = FakeAccount()
    act(acct, **BPS).execute()
    assert acct.submitted[-1]["legs"][0].side == OrderDirection.SELL


def test_the_net_price_is_a_credit_and_equals_short_bid_minus_long_ask():
    """Negative = net credit (Alpaca MLEG). Derived from the chain, not hardcoded, so a
    sign inversion or a bid/ask swap is caught rather than re-encoded."""
    acct = FakeAccount()
    res = act(acct, **BPS).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    short, long_ = _legs(sub)
    chain = {c.strike: c for c in acct.get_option_chain(
        "XYZ", None, None, OptionRight.PUT)}
    expected = round(chain[short[0].strike].bid - chain[long_[0].strike].ask, 4)
    assert expected > 0                                   # a credit, in positive terms
    assert sub["limit_price"] == pytest.approx(-expected)
    assert sub["limit_price"] < 0


def test_the_width_the_caller_asked_for_is_respected():
    """The two ``strike_param`` entries pick the two legs; widening the pair must widen
    the spread rather than silently re-selecting the same two strikes."""
    narrow = FakeAccount()
    act(narrow, **dict(BPS, strike_param=[5.0, 10.0])).execute()
    wide = FakeAccount()
    act(wide, **dict(BPS, strike_param=[5.0, 20.0])).execute()

    def width(acct):
        short, long_ = _legs(acct.submitted[-1])
        return short[0].strike - long_[0].strike

    assert width(narrow) == pytest.approx(5.0)
    assert width(wide) == pytest.approx(15.0)


def test_both_legs_are_out_of_the_money_puts_below_spot():
    """A bull put spread is sold BELOW the market — that is what "stays above the short
    strike" means. Selecting an ITM short put would be a different trade."""
    acct = FakeAccount(spot=100.0)
    act(acct, **BPS).execute()
    short, long_ = _legs(acct.submitted[-1])
    assert short[0].strike < 100.0 and long_[0].strike < 100.0


# ==========================================================================
# max loss and the reserve
# ==========================================================================
def test_max_loss_is_width_minus_credit_and_the_reserve_matches_it():
    acct = FakeAccount()
    res = act(acct, **BPS).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    short, long_ = _legs(sub)
    width = short[0].strike - long_[0].strike
    credit = -sub["limit_price"]
    max_loss = (width - credit) * 100.0 * sub["quantity"]
    assert max_loss > 0
    assert res["data"]["option_reserve"] == pytest.approx(max_loss)
    assert OptionsAccountInterface.option_reserve_required(
        "bull_put_spread", sub["quantity"], spread_width=round(width, 4),
        net_credit=round(credit, 4)) == pytest.approx(max_loss)


def test_the_reserve_branch_exists_rather_than_raising():
    """``option_reserve_required`` RAISES for an unknown strategy (a zero reserve passes
    every buying-power gate), so a missing branch is a hard failure at submit time."""
    assert "bull_put_spread" in OptionsAccountInterface.RESERVING_STRATEGIES
    assert "bull_put_spread" not in OptionsAccountInterface.ZERO_RESERVE_STRATEGIES
    assert OptionsAccountInterface.option_reserve_required(
        "bull_put_spread", 3, spread_width=5.0, net_credit=1.5) == pytest.approx(1_050.0)


@pytest.mark.parametrize("kw,missing", [
    (dict(spread_width=None, net_credit=1.0), "spread_width"),
    (dict(spread_width=5.0, net_credit=None), "net_credit"),
])
def test_a_missing_sizing_input_raises_and_names_the_field(kw, missing):
    """Unknown is not zero one layer in either: a priced branch whose input is absent
    would reserve nothing and be affordable at any size."""
    with pytest.raises(ValueError, match="option_reserve_required") as e:
        OptionsAccountInterface.option_reserve_required("bull_put_spread", 3, **kw)
    assert missing in str(e.value)


def test_a_credit_at_least_as_wide_as_the_spread_has_no_max_loss_to_reserve():
    """A genuinely COMPUTED zero (arithmetic), not an absent field.

    The credit EXCEEDING the width is pinned too (FOUND BY MUTATION D06): without the
    ``max(0.0, ...)`` floor that returns a NEGATIVE reserve, and a negative reserve is
    worse than a zero one — it does not merely pass the buying-power gate, it *adds* to
    the pool and makes the next structure look affordable as well.
    """
    assert OptionsAccountInterface.option_reserve_required(
        "bull_put_spread", 1, spread_width=5.0, net_credit=5.0) == 0.0
    assert OptionsAccountInterface.option_reserve_required(
        "bull_put_spread", 3, spread_width=5.0, net_credit=9.0) == 0.0


def test_it_is_sized_off_the_max_loss_not_off_the_credit():
    """A credit structure cannot be sized off its premium (the premium comes IN). Sizing
    off max loss is what makes the count track the budget.

    Derived rather than asserted as a literal: read the legs the builder produced and
    require ``floor(equity x sizing% / per-spread max loss)``. Sized off the CREDIT
    (0.20/share = $20) instead, a 1% budget would buy 50 spreads rather than 2.
    """
    import math

    def sized(pct):
        acct = FakeAccount(balance=100_000.0)
        res = act(acct, **dict(BPS, sizing=pct)).execute()
        assert res["success"], res["message"]
        sub = acct.submitted[-1]
        short, long_ = _legs(sub)
        per = (short[0].strike - long_[0].strike - (-sub["limit_price"])) * 100.0
        return sub["quantity"], per

    q1, per = sized(1.0)
    q4, _ = sized(4.0)
    assert q1 == math.floor(100_000.0 * 0.01 / per) >= 1
    assert q4 == math.floor(100_000.0 * 0.04 / per) > q1
    # ...and NOT the count a credit-based budget would have bought.
    assert q1 < math.floor(100_000.0 * 0.01 / (0.20 * 100.0))


def test_it_refuses_when_buying_power_cannot_cover_the_reserve():
    class Poor(FakeAccount):
        def check_option_buying_power(self, required):
            return False

        def available_option_buying_power(self):
            return 0.0

    acct = Poor()
    res = acct and act(acct, **BPS).execute()
    assert not res["success"]
    assert "buying power" in res["message"].lower()
    assert acct.submitted == []


# ==========================================================================
# an INVERTED pair is a bear put DEBIT spread, not this
# ==========================================================================
def test_an_inverted_pair_is_rejected_rather_than_opened_as_a_debit_spread(monkeypatch):
    """Long ABOVE short is ``bear_put_spread``: a DEBIT structure with a different max
    loss, a different reserve (zero) and the opposite directional thesis. Opening it
    under the ``bull_put_spread`` label would reserve a max loss that does not apply and
    charge assignment capacity against the wrong leg.

    The selector cannot produce this today (it sorts), so the guard is driven by
    monkeypatching the selection: this is the mutation ("swap the two legs") made
    executable rather than a shape the chain happens to rule out.
    """
    acct = FakeAccount()
    real = TA.select_vertical_spread

    def inverted(*a, **kw):
        pair = real(*a, **kw)
        return None if pair is None else (pair[1], pair[0])   # (hi, lo) -> (lo, hi)

    monkeypatch.setattr(TA, "select_vertical_spread", inverted)
    res = act(acct, **BPS).execute()
    assert not res["success"], res["message"]
    assert "invert" in res["message"].lower() or "below" in res["message"].lower()
    assert acct.submitted == []


def test_a_zero_width_pair_is_rejected():
    """Both legs on one strike is not a spread; it reserves nothing and nets to zero."""
    class OneStrike(FakeAccount):
        def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                             strike_min=None, strike_max=None):
            return [c for c in super().get_option_chain(
                underlying, expiry_min, expiry_max, option_type) if c.strike == 95.0]

    acct = OneStrike()
    res = act(acct, **BPS).execute()
    assert not res["success"]
    assert acct.submitted == []


def test_a_non_positive_credit_is_refused():
    """Inverted quotes (the long wing costs more than the short leg pays) make this a
    DEBIT, which is not the structure being opened."""
    class NoCredit(FakeAccount):
        def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                             strike_min=None, strike_max=None):
            out = super().get_option_chain(underlying, expiry_min, expiry_max,
                                           option_type, strike_min, strike_max)
            for c in out:                     # flat premium curve -> ask > bid always
                c.bid, c.ask = 1.0, 1.5
            return out

    acct = NoCredit()
    res = act(acct, **BPS).execute()
    assert not res["success"]
    assert "credit" in res["message"].lower()
    assert acct.submitted == []


# ==========================================================================
# the entry never reaches the broker when the chain cannot answer
# ==========================================================================
def test_an_empty_chain_says_the_chain_is_EMPTY_not_that_it_is_thin():
    """FOUND BY MUTATION A44. Delete the empty-chain guard and the selector refuses one
    step later with "No liquid bull put spread" — the message that means "the chain is
    thin", which sends an operator to loosen liquidity gates that were never the problem.
    """
    class Empty(FakeAccount):
        def get_option_chain(self, *a, **kw):
            return []

    acct = Empty()
    res = act(acct, **BPS).execute()
    assert not res["success"]
    assert "Empty option chain" in res["message"], res["message"]
    assert acct.submitted == []


# ==========================================================================
# EACH GUARD NAMES ITS OWN PROBLEM
#
# The refusals below are layered, so removing any ONE of them still ends in a refusal —
# from the NEXT guard down, wearing the wrong diagnosis. "Non-positive spread width" and
# "Non-positive max-loss reserve" and "the short leg must be ABOVE the long leg" send an
# operator to three different places, and a chain of guards that all collapse into
# whichever one happens to be left is not defence in depth, it is one guard with spares.
#
# Driven through a forced selector result because the real selector cannot produce these
# shapes: they are the mutations made executable.
# ==========================================================================
def _contract(strike, bid, ask):
    return OptionContract(symbol=f"XYZ_{strike}_P", underlying="XYZ",
                          option_type=OptionRight.PUT, strike=strike, expiry=EXPIRY,
                          bid=bid, ask=ask, last=bid, open_interest=1000, delta=None,
                          implied_volatility=None)


def _force_pair(monkeypatch, hi, lo):
    """Pin the selector's return. For a PUT chain it hands back (higher, lower)."""
    monkeypatch.setattr(TA, "select_vertical_spread", lambda *a, **k: (hi, lo))


@pytest.mark.parametrize("hi,lo,expect,mutation", [
    # A03 — the two strikes are EQUAL. Diagnosed as the ordering problem it is, not as a
    # mysterious zero width two guards later.
    (_contract(95.0, 4.6, 4.8), _contract(95.0, 4.6, 4.8), "ABOVE", "A03"),
    # A10 — distinct strikes that round to a zero width (sub-tick gap).
    (_contract(95.00001, 4.6, 4.8), _contract(95.0, 4.2, 4.4), "width", "A10"),
    # A14/A66 — a credit WIDER than the spread (bad data / a stale quote): the max loss is
    # negative, which is not a free trade, and abs() would turn it into a real reserve.
    (_contract(95.0, 20.0, 20.2), _contract(90.0, 0.8, 1.0), "max-loss", "A14/A66"),
    # A08 — a credit of EXACTLY zero: all of the risk, none of the premium.
    (_contract(95.0, 4.4, 4.6), _contract(90.0, 4.2, 4.4), "credit", "A08"),
    # A46 — a leg with no strike at all.
    (_contract(None, 4.6, 4.8), _contract(90.0, 4.2, 4.4), "strike", "A46"),
    # A47 — a leg with no quote.
    (_contract(95.0, None, 4.8), _contract(90.0, 4.2, 4.4), "quote", "A47"),
])
def test_each_refusal_names_the_input_that_is_wrong(monkeypatch, hi, lo, expect, mutation):
    acct = FakeAccount()
    _force_pair(monkeypatch, hi, lo)
    res = act(acct, **BPS).execute()
    assert not res["success"], f"{mutation}: {res['message']}"
    assert expect in res["message"], f"{mutation}: {res['message']}"
    # ...and it is a REFUSAL, not an exception that happened to be swallowed.
    assert "Error executing" not in res["message"], f"{mutation}: {res['message']}"
    assert acct.submitted == [], mutation


# ==========================================================================
# the remaining survivors
# ==========================================================================
def test_each_leg_names_the_CONTRACT_it_was_selected_from():
    """FOUND BY MUTATION A33. Strike and side were pinned; the contract SYMBOL was not,
    so a leg could carry the right strike and the wrong instrument — which is the field
    the broker actually routes on."""
    acct = FakeAccount()
    act(acct, **BPS).execute()
    sub = acct.submitted[-1]
    by_strike = {c.strike: c for c in acct.get_option_chain(
        "XYZ", None, None, OptionRight.PUT)}
    for leg in sub["legs"]:
        assert leg.contract_symbol == by_strike[leg.strike].symbol, leg


def test_the_recorded_action_type_is_this_action_not_a_siblings():
    """FOUND BY MUTATION A38. ``_action_type_value`` is what lands on the persisted
    ``TradeActionResult`` and in every log line this action writes; returning a sibling's
    value makes the trade unattributable after the fact."""
    acct = FakeAccount()
    res = act(acct, **BPS).execute()
    assert res["action_type"] == "open_bull_put_spread"


def test_the_buying_power_gate_is_asked_about_the_REAL_reserve():
    """FOUND BY MUTATION A55. Every other BP test used an account that refused
    everything, so a gate asking "can I afford $0?" was indistinguishable from one asking
    the true figure. This account affords $100 and nothing more."""
    class Tight(FakeAccount):
        def check_option_buying_power(self, required):
            return float(required) <= 100.0

        def available_option_buying_power(self):
            return 100.0

    acct = Tight()
    res = act(acct, **BPS).execute()
    assert not res["success"], res["message"]
    assert "buying power" in res["message"].lower()
    assert acct.submitted == []


def test_a_structure_the_budget_cannot_afford_opens_NOTHING():
    """FOUND BY MUTATION A62. ``quantity < 1`` must refuse, not floor to one contract:
    "the smallest tradeable size" is not the same fact as "the size the budget allows",
    and rounding it up spends money the sizing rule said was not there."""
    acct = FakeAccount(balance=100_000.0)
    res = act(acct, **dict(BPS, sizing=0.1)).execute()   # $100 of budget vs a ~$480 spread
    assert not res["success"], res["message"]
    assert "Insufficient budget" in res["message"]
    assert acct.submitted == []


def test_the_sizing_helper_buys_NOTHING_off_an_unpriceable_per_contract_reserve():
    """FOUND BY MUTATION V01. This builder guards ``per_spread_reserve <= 0`` before it
    ever calls ``_size_by_reserve``, so the helper's own zero-guard was unreachable from
    here and untested anywhere — and it is the last line of defence shared by all eight
    credit builders. Returning 1 there would buy a contract against a risk nobody could
    price."""
    a = act(FakeAccount(), **BPS)
    assert a._size_by_reserve(0.0, 5.0) == 0
    assert a._size_by_reserve(-480.0, 5.0) == 0
    assert a._size_by_reserve(None, 5.0) == 0
    assert a._size_by_reserve(480.0, 0.0) == 0


def test_the_entry_action_sorts_AHEAD_of_the_adjustment_actions():
    """FOUND BY MUTATION E03. ``_sort_actions_by_priority`` puts order-CREATING actions
    first and anything unmapped last (99), so an entry missing from the priority map runs
    AFTER the TP/SL adjustments that are supposed to attach to the order it creates."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    acct = FakeAccount()
    rec = SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                          expected_profit_percent=None, recommended_action=None)
    entry = act(acct, **BPS)
    adjust = create_action(ExpertActionType.ADJUST_TAKE_PROFIT, "XYZ", acct,
                           SimpleNamespace(), None, rec, percent=10.0,
                           reference_value="current_price")
    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ordered = ev._sort_actions_by_priority([adjust, entry])
    assert ordered[0] is entry, [type(a).__name__ for a in ordered]
