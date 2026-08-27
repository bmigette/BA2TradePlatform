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
