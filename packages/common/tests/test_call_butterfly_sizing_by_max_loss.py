"""A debit structure is SIZED by what it can lose, not by what it cost (``option_payoff.sizing_risk``).

A call butterfly whose UPPER wing is wider than its lower one is worth ``(k2-k1) - (k3-k2)`` per
share above its top strike -- a liability -- so it can lose MORE than its net debit.
``OpenCallButterflyAction`` used to size by the debit alone (and ``call_butterfly`` reserves
nothing by name), under-sizing the risk of exactly those flies. Live and backtest share the
builder and this function, so the two cannot disagree.

A BALANCED fly (and every other debit builder, whose worst case IS the debit) sizes exactly as
before and records no reserve.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_common.core.option_payoff import (
    MEASURED, UNBOUNDED, PayoffLeg, SIZING_RISK_TOLERANCE, sizing_risk,
)
from ba2_common.core.option_types import OptionContract
from ba2_common.core.TradeActions import create_action
from ba2_common.core.types import ExpertActionType, OrderDirection
from tests.test_option_butterfly_lower_wing import (  # the same fake broker the fly tests use
    AS_OF, NEAR, _REC, _own_db, _TwoExpiryAccount,
)

BUY, SELL = OrderDirection.BUY, OrderDirection.SELL
BUDGET = 1_000_000.0 * 0.50          # the double's balance x sizing=50


class _Acct(_TwoExpiryAccount):
    """The same chain restricted to a chosen strike set, so the wings can be made unequal."""

    def __init__(self, strikes, bp_ok=True):
        super().__init__()
        self._strikes = set(strikes)
        self._bp_ok = bp_ok
        self.bp_asked = []

    def get_option_chain(self, *a, **k):
        return [c for c in super().get_option_chain(*a, **k) if c.strike in self._strikes]

    def check_option_buying_power(self, required):
        self.bp_asked.append(required)
        return self._bp_ok


def _run(strikes, **acct_kw):
    acct = _Acct(strikes, **acct_kw)
    act = create_action(ExpertActionType.OPEN_CALL_BUTTERFLY, "AAPL", acct, SimpleNamespace(), None,
                        _REC, strike_method="percent_otm", strike_param=0.0, dte_min=10,
                        dte_max=40, sizing=50.0, wing_width_pct=10.0)
    act.submit_to_broker = True
    res = act.execute()
    return acct, res


def test_a_balanced_fly_sizes_exactly_by_its_debit_and_reserves_nothing():
    acct, res = _run({90.0, 100.0, 110.0})
    assert res["success"] is True, res["message"]
    sub = acct.submitted[-1]
    cost = sub["limit_price"] * 100.0
    assert sub["quantity"] == int(BUDGET // cost)               # floor(budget / debit), as before
    assert "option_reserve" not in (res.get("data") or {})
    assert acct.bp_asked == []                                  # no buying-power reserve asked


def test_an_unbalanced_fly_is_sized_by_its_true_max_loss_and_reserves_the_excess():
    """90/100/115: above 115 the fly is worth 10 - 15 = -5 a share, so its true max loss is
    its debit plus 5 a share."""
    acct, res = _run({90.0, 100.0, 115.0})
    assert res["success"] is True, res["message"]
    sub = acct.submitted[-1]
    debit = sub["limit_price"]
    max_loss = (debit + 5.0) * 100.0
    assert sub["quantity"] == int(BUDGET // max_loss)           # fewer contracts than by the debit
    assert sub["quantity"] < int(BUDGET // (debit * 100.0))
    excess = (max_loss - debit * 100.0) * sub["quantity"]       # what the fill does not pay
    assert acct.bp_asked == [pytest.approx(excess)]
    assert res["data"]["option_reserve"] == pytest.approx(excess)


def test_the_loss_beyond_the_debit_is_refused_when_buying_power_cannot_cover_it():
    acct, res = _run({90.0, 100.0, 115.0}, bp_ok=False)
    assert res["success"] is False and "Insufficient BP" in res["message"]
    assert acct.submitted == []


def test_an_unbounded_premium_sized_entry_is_refused_and_logged(monkeypatch):
    import ba2_common.core.TradeActions as TA
    a = TA.BuyCallAction.__new__(TA.BuyCallAction)
    a.instrument_name = "AAPL"
    a._result = lambda ok, msg, data=None: {"success": ok, "message": msg}
    warned = []
    monkeypatch.setattr(TA.logger, "warning", lambda m, *x, **k: warned.append(m))
    resolved = SimpleNamespace(
        cost_per_contract=100.0, sizing_basis="premium", option_strategy="ratio_x", legs=[],
        limit_price=1.0, budget_refusal_message=None,
        payoff_legs=[PayoffLeg("call", BUY, 10.0, 100.0, 1), PayoffLeg("call", SELL, 5.0, 110.0, 2)])
    res = a._size_and_submit(resolved)
    assert res["success"] is False and "unbounded" in res["message"]
    assert warned and "unbounded" in warned[0]


# --------------------------------------------------------------------------- the shared function
def _c(k, side, prem, ratio=1):
    return PayoffLeg("call", side, prem, k, ratio)


def test_sizing_risk_leaves_every_balanced_debit_structure_unchanged():
    fly = [_c(100, BUY, 12.0), _c(110, SELL, 5.5, 2), _c(120, BUY, 2.5)]           # debit 3.5
    r = sizing_risk(fly, 350.0)
    assert (r.state, r.risk_per_contract, r.extra_beyond_outlay) == (MEASURED, 350.0, 0.0)
    r = sizing_risk([_c(100, BUY, 5.0)], 500.0)                                     # a long call
    assert (r.risk_per_contract, r.extra_beyond_outlay) == (500.0, 0.0)
    r = sizing_risk([_c(100, BUY, 6.0), _c(110, SELL, 2.5)], 350.0)                 # debit spread
    assert (r.risk_per_contract, r.extra_beyond_outlay) == (350.0, 0.0)


def test_sizing_risk_tolerates_the_four_decimal_rounding_of_a_quoted_debit():
    fly = [_c(100, BUY, 12.0), _c(110, SELL, 5.5, 2), _c(120, BUY, 2.5)]
    r = sizing_risk(fly, 350.0 - SIZING_RISK_TOLERANCE / 2)       # outlay a hair below the max loss
    assert r.risk_per_contract == 350.0 - SIZING_RISK_TOLERANCE / 2 and r.extra_beyond_outlay == 0.0


def test_sizing_risk_of_an_unbalanced_fly_is_the_true_max_loss():
    fly = [_c(100, BUY, 12.0), _c(110, SELL, 5.5, 2), _c(130, BUY, 1.5)]           # debit 2.5
    r = sizing_risk(fly, 250.0)
    assert r.state == MEASURED
    assert r.risk_per_contract == pytest.approx(1250.0)
    assert r.extra_beyond_outlay == pytest.approx(1000.0)


def test_sizing_risk_of_an_unbounded_structure_is_no_number():
    ratio = [_c(100, BUY, 10.0), _c(110, SELL, 5.0, 2)]
    r = sizing_risk(ratio, 0.0)
    assert r.state == UNBOUNDED and r.risk_per_contract is None


# --------------------------------------------------------------------------- the audit
def test_every_reserve_sized_builder_reserves_at_least_its_true_max_loss():
    """The other multi-leg builders size by ``option_reserve_required``. Audited against the
    payoff evaluator's max loss: none under-reserves (iron condor with UNEQUAL wings included,
    which takes the wider wing)."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface as O
    from ba2_common.core.option_payoff import max_loss

    def put(k, side, prem, ratio=1):
        return PayoffLeg("put", side, prem, k, ratio)

    # iron condor 80/90 puts, 110/125 calls: wings 10 and 15, credit 2.0
    ic = [put(80, BUY, 0.5), put(90, SELL, 1.5), _c(110, SELL, 1.5), _c(125, BUY, 0.5)]
    assert O.option_reserve_required("iron_condor", 1, spread_width=15.0, net_credit=2.0) \
        >= max_loss(ic).amount - 1e-9
    # bull put / bear call credit verticals
    assert O.option_reserve_required("bull_put_spread", 1, spread_width=10.0, net_credit=3.0) \
        == pytest.approx(max_loss([put(100, SELL, 5.0), put(90, BUY, 2.0)]).amount)
    assert O.option_reserve_required("bear_call_spread", 1, spread_width=10.0, net_credit=3.0) \
        == pytest.approx(max_loss([_c(100, SELL, 5.0), _c(110, BUY, 2.0)]).amount)
    # jade lizard: naked put at 90 + call spread 110/117.5, credit 3.04 (broker margin >= max loss)
    jl = [put(90, SELL, 1.5), _c(110, SELL, 2.0), _c(117.5, BUY, 0.46)]
    assert O.option_reserve_required("jade_lizard", 1, strike=90.0, spread_width=7.5,
                                     net_credit=3.04) >= max_loss(jl).amount
