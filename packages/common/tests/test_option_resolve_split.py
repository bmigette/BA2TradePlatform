"""The resolve/submit split must be BEHAVIOUR-NEUTRAL and independently callable.

Two properties, and they pull in opposite directions, which is why both are pinned:

  * `execute()` must do exactly what it did before -- same order, same quantity, same limit,
    same refusals, same persisted TradeActionResult rows. ~20 existing tests depend on it.
  * `_resolve()` must be callable ON ITS OWN and reach a priced structure without submitting
    anything, because that is the whole point: in Phase 3 a risk manager calls it.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeActions
from ba2_common.core.option_request import ResolvedStructure
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight, OrderRecommendation


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """These tests persist TradeActionResult rows (``_result`` is not pure).

    Same guard as ``test_new_option_actions.py``: sibling DB-seam tests repoint the global
    DB seam at their own temp sqlite without restoring it, so by the time this module runs
    the session DB may lack the trade_action_result table. Re-point to a fresh, fully
    initialized sqlite per test so this file is order-independent."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "resolve_split.sqlite"))
    db.init_db()
    yield


class _Acct:
    """Minimal options account double: serves a chain, a price, a balance, records submits."""

    def __init__(self, chain):
        self._chain = chain
        self.submitted = []

    def get_option_chain(self, symbol, expiry_min, expiry_max, option_type):
        return [c for c in self._chain if c.option_type == option_type]

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_balance(self):
        return 100_000.0

    def submit_option_order(self, **kw):
        self.submitted.append(kw)
        return type("O", (), {"id": len(self.submitted)})()


def _contract(strike, right, *, bid=1.00, ask=1.10, delta=0.30, dte=30):
    return OptionContract(
        symbol=f"X{int(strike*1000):08d}", underlying="X", option_type=right,
        strike=float(strike), expiry=date.today() + timedelta(days=dte),
        bid=bid, ask=ask, last=None, implied_volatility=0.25, delta=delta, volume=500)


CHAIN = ([_contract(s, OptionRight.CALL) for s in (95, 100, 105, 110)]
         + [_contract(s, OptionRight.PUT) for s in (90, 95, 100, 105)])


def _recommendation():
    """A recommendation stub the action can persist a TradeActionResult against.

    NOT optional: ``trade_action_result.expert_recommendation_id`` is NOT NULL, so an action
    with no recommendation cannot write the row every refusal and every submit writes -- the
    insert raises IntegrityError before the assertion under test is ever reached.
    ``instance_id=None`` keeps ``_virtual_equity`` at 100% of balance and leaves
    ``_max_equity_per_instrument_cap`` unresolvable (None), which is what the arithmetic in
    these tests assumes; the three ``_consensus_target`` fields are None so the target-based
    strike methods stay inert."""
    return SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                           expected_profit_percent=None,
                           recommended_action=OrderRecommendation.BUY)


def _action(cls, **kw):
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    acct = _Acct(CHAIN)
    acct.__class__ = type("_A", (_Acct, OptionsAccountInterface), {})
    params = dict(strike_method="delta", strike_param=0.30, dte_min=1, dte_max=60,
                  sizing=10.0)
    params.update(kw)
    a = cls("X", acct, OrderRecommendation.BUY, expert_recommendation=_recommendation(),
            **params)
    return a, acct


def test_resolve_returns_a_structure_and_submits_nothing():
    a, acct = _action(TradeActions.BuyCallAction)
    resolved = a._resolve()
    assert isinstance(resolved, ResolvedStructure)
    assert acct.submitted == []          # THE point of the split
    assert resolved.option_strategy == "long_call"
    assert resolved.sizing_basis == "premium"
    assert len(resolved.legs) == 1
    assert len(resolved.payoff_legs) == 1


def test_resolve_prices_cost_per_contract_as_the_premium_times_one_hundred():
    a, _ = _action(TradeActions.BuyCallAction)
    resolved = a._resolve()
    assert resolved.cost_per_contract == pytest.approx(resolved.limit_price * 100.0)


def test_resolve_computes_dte_which_no_builder_used_to_compute():
    a, _ = _action(TradeActions.BuyCallAction)
    assert a._resolve().dte == 30


def test_execute_still_submits_and_matches_the_pre_split_arithmetic():
    a, acct = _action(TradeActions.BuyCallAction)
    a.execute()
    assert len(acct.submitted) == 1
    sub = acct.submitted[0]
    # 100_000 * 10% = 10_000 budget; ask 1.10 -> 110/contract -> floor(10000/110) = 90
    assert sub["quantity"] == 90
    assert sub["limit_price"] == pytest.approx(1.10)
    assert sub["option_strategy"] == "long_call"


def test_a_refusal_from_resolve_short_circuits_execute_without_submitting():
    a, acct = _action(TradeActions.BuyCallAction, strike_method="delta", strike_param=0.30)
    a.account._chain = []                       # empty chain
    result = a.execute()
    assert acct.submitted == []
    assert result["success"] is False


def test_submit_to_broker_false_still_reaches_the_informational_result():
    a, acct = _action(TradeActions.BuyCallAction)
    a.submit_to_broker = False
    result = a.execute()
    assert acct.submitted == []
    assert result["success"] is True
    assert "not submitted" in result["message"]


# --- Task 3: the 7 premium-sized builders ------------------------------------------------

PREMIUM_SIZED = [
    ("BuyCallAction", "long_call", 1),
    ("BuyPutAction", "long_put", 1),
    ("OpenBullCallSpreadAction", "bull_call_spread", 2),
    ("OpenBearPutSpreadAction", "bear_put_spread", 2),
    ("OpenStraddleAction", "straddle", 2),
    ("OpenStrangleAction", "strangle", 2),
    ("OpenCallButterflyAction", "call_butterfly", 3),
]


@pytest.mark.parametrize("cls_name, strategy, n_legs", PREMIUM_SIZED)
def test_every_premium_sized_builder_resolves_without_submitting(cls_name, strategy, n_legs):
    a, acct = _action(getattr(TradeActions, cls_name), strike_param=0.30, wing_width_pct=5.0)
    resolved = a._resolve()
    if not isinstance(resolved, ResolvedStructure):
        pytest.skip(f"{cls_name} refused on this synthetic chain: {resolved['message']}")
    assert acct.submitted == []
    assert resolved.option_strategy == strategy
    assert len(resolved.legs) == n_legs
    assert len(resolved.payoff_legs) == n_legs
    assert resolved.sizing_basis == "premium"
    assert resolved.reserve_per_contract == 0.0
    assert resolved.cost_per_contract == pytest.approx(resolved.limit_price * 100.0)
    assert resolved.dte == 30


@pytest.mark.parametrize("cls_name, strategy, n_legs", PREMIUM_SIZED)
def test_payoff_legs_reprice_to_the_same_net_the_limit_price_carries(cls_name, strategy, n_legs):
    """The payoff legs and the limit price are two views of one structure. If they disagree,
    the risk manager's max-loss and the broker's fill price describe different trades."""
    from ba2_common.core.option_payoff import validate_legs
    from ba2_common.core.types import OrderDirection
    a, _ = _action(getattr(TradeActions, cls_name), strike_param=0.30, wing_width_pct=5.0)
    resolved = a._resolve()
    if not isinstance(resolved, ResolvedStructure):
        pytest.skip("refused on this synthetic chain")
    assert validate_legs(resolved.payoff_legs) is None
    # A payoff leg's `premium` is ALWAYS POSITIVE (the sign lives in `side`), so the net paid
    # is the signed sum -- which must be the very number sent to the broker as the limit.
    net_paid = sum((1 if leg.side == OrderDirection.BUY else -1) * leg.premium * leg.ratio
                   for leg in resolved.payoff_legs)
    assert net_paid == pytest.approx(resolved.limit_price, abs=1e-4)


@pytest.mark.parametrize("cls_name, strategy, n_legs", PREMIUM_SIZED)
def test_execute_still_submits_for_every_premium_sized_builder(cls_name, strategy, n_legs):
    a, acct = _action(getattr(TradeActions, cls_name), strike_param=0.30, wing_width_pct=5.0)
    result = a.execute()
    if not result["success"]:
        pytest.skip(f"{cls_name} refused on this synthetic chain: {result['message']}")
    assert len(acct.submitted) == 1
    assert acct.submitted[0]["option_strategy"] == strategy
    assert acct.submitted[0]["quantity"] >= 1


def test_the_butterfly_body_leg_carries_ratio_two():
    a, _ = _action(TradeActions.OpenCallButterflyAction, wing_width_pct=5.0)
    resolved = a._resolve()
    if not isinstance(resolved, ResolvedStructure):
        pytest.skip("refused on this synthetic chain")
    ratios = sorted(leg.ratio for leg in resolved.payoff_legs)
    assert ratios == [1, 1, 2]


# --- Task 5: the split changed no arithmetic ---------------------------------------------

def test_the_unified_sizer_reproduces_both_old_sizers_exactly():
    """`_size` and `_size_by_reserve` now both delegate to `_size_by_cost`. This pins that the
    delegation is arithmetic-preserving, over the whole grid of inputs that used to hit two
    separate implementations -- including the zero and negative cases each guarded differently.
    """
    import math
    a, _ = _action(TradeActions.BuyCallAction)
    equity = a._virtual_equity()
    for premium in (0.01, 0.10, 1.00, 1.10, 7.35, 250.0):
        for pct in (0.5, 1.0, 10.0, 100.0):
            budget = equity * (pct / 100.0)
            cap = a._max_equity_per_instrument_cap(equity)
            if cap is not None:
                budget = min(budget, cap)
            assert a._size(premium, pct) == int(math.floor(budget / (premium * 100.0)))
            assert a._size_by_reserve(premium * 100.0, pct) == a._size(premium, pct)


@pytest.mark.parametrize("bad", [0, -1.0, None])
def test_both_sizers_still_refuse_the_unsizeable_inputs_they_always_refused(bad):
    a, _ = _action(TradeActions.BuyCallAction)
    assert a._size(bad, 10.0) == 0
    assert a._size(1.0, bad) == 0
    assert a._size_by_reserve(bad, 10.0) == 0
    assert a._size_by_reserve(100.0, bad) == 0


def test_a_premium_structure_the_budget_cannot_afford_submits_NOTHING():
    """The shared tail's ``quantity < 1`` refusal, exercised through ``execute()``.

    Every other test in this file funds a size of 90, so a tail that refused only at
    ``quantity < 0`` would look identical to the real one: it would sail past the guard with
    ``quantity == 0`` and hand a ZERO-contract order to ``_submit_option_order``, which does
    not re-check the size. "The smallest tradeable size" is not the same fact as "the size the
    budget allows" -- the sibling credit builders pin this for their own inline tails
    (``test_bull_put_spread.py``, found by mutation A62); the shared tail now needs its own.
    """
    a, acct = _action(TradeActions.BuyCallAction, sizing=0.1)   # $100 budget vs $110/contract
    result = a.execute()
    assert result["success"] is False
    assert "Insufficient budget" in result["message"]
    assert acct.submitted == []


def test_the_per_instrument_cap_still_clamps_the_shared_tails_budget(monkeypatch):
    """``min(budget, cap)`` inside ``_size_by_cost`` -- the ONE place both sizing families now
    apply ``max_virtual_equity_per_instrument_percent``.

    Pinned here, and not only in ``test_option_entry_sizing_cap.py``, because that file calls
    ``_size`` directly: it cannot see whether ``execute()``'s new shared tail still routes
    through the capped sizer. Every other test in this module leaves ``instance_id`` None, so
    the cap resolves to None and deleting the clamp changes nothing they measure.
    """
    import ba2_common.core.instance_resolver as ir_mod
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertInstance

    inst_id = add_instance(ExpertInstance(account_id=1, expert="MockExpert",
                                          virtual_equity_pct=100.0))
    capped = SimpleNamespace(settings={"max_virtual_equity_per_instrument_percent": 1.0})
    monkeypatch.setattr(
        ir_mod, "get_instance_resolver",
        lambda: SimpleNamespace(get_expert_instance=lambda _id: capped))

    a, acct = _action(TradeActions.BuyCallAction)
    a.expert_recommendation.instance_id = inst_id
    a.execute()
    # sizing=10% of $100k funds $10,000 -> 90 contracts at $110. The 1% cap funds $1,000,
    # and the tighter of the two must win -> 9.
    assert len(acct.submitted) == 1
    assert acct.submitted[0]["quantity"] == 9


# --- refusal-message parity, added after the 2026-08-27 code-quality review -------------------
#
# The review found the shared tail had REWORDED five of the seven "insufficient budget" messages.
# They are persisted to TradeActionResult.message and rendered in the UI as the reason an entry
# did not fire, so a refactor advertised as behaviour-neutral had quietly changed the one thing a
# user actually reads. These strings are lifted verbatim from the pre-split source
# (commit 7a7a1bbc^) -- if the tail ever re-uniformises them, this fails.

HISTORICAL_BUDGET_REFUSALS = [
    ("BuyCallAction",            "Insufficient budget to size long_call for X (premium="),
    ("BuyPutAction",             "Insufficient budget to size long_put for X (premium="),
    ("OpenBullCallSpreadAction", "Insufficient budget to size bull_call_spread for X (net_debit="),
    ("OpenBearPutSpreadAction",  "Insufficient budget to size bear_put_spread for X (net_debit="),
    ("OpenStraddleAction",       "Insufficient budget to size straddle for X (net_debit="),
    ("OpenStrangleAction",       "Insufficient budget to size strangle for X (net_debit="),
    # No parenthetical, and it calls itself "butterfly" where its option_strategy is
    # "call_butterfly" -- which is exactly why the message is CARRIED and not derived.
    ("OpenCallButterflyAction",  "Insufficient budget to size butterfly for X"),
]


@pytest.mark.parametrize("cls_name, expected_prefix", HISTORICAL_BUDGET_REFUSALS)
def test_the_budget_refusal_message_is_the_one_this_structure_always_emitted(
        cls_name, expected_prefix):
    # sizing=0.001% makes the budget smaller than one contract for every structure, which is
    # the only path to the quantity < 1 branch without mocking the sizer.
    a, acct = _action(getattr(TradeActions, cls_name), sizing=0.001, wing_width_pct=5.0)
    result = a.execute()
    assert result["success"] is False
    assert acct.submitted == []
    assert result["message"].startswith(expected_prefix), (
        f"{cls_name} refusal drifted.\n  expected prefix: {expected_prefix!r}\n"
        f"  actual:          {result['message']!r}")


def test_a_structure_with_no_carried_message_still_refuses_intelligibly():
    """The fallback exists for 2b/2c builders that have not been converted yet."""
    from ba2_common.core.option_request import ResolvedStructure
    a, _ = _action(TradeActions.BuyCallAction, sizing=0.001)
    bare = ResolvedStructure(
        request=None, legs=[], payoff_legs=[], limit_price=1.0,
        option_strategy="some_future_structure", dte=30, reserve_per_contract=0.0,
        cost_per_contract=100.0, sizing_basis="premium", reserve_kwargs={})
    out = a._size_and_submit(bare)
    assert out["success"] is False
    assert "some_future_structure" in out["message"]
