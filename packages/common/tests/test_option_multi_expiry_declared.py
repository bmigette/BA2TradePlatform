"""The single-expiry guard stays the DEFAULT; only a DECLARED strategy is let through.

``test_option_single_expiry_guard.py`` pins the refusal and stays exactly as it was — every
one of its cases is still red-lined here by construction, because none of the strategies it
submits is declared. This file pins the other half of the contract:

* a strategy in ``MULTI_EXPIRY_OPTION_STRATEGIES`` may submit two expiries, and its legs are
  persisted with their OWN dates on their own rows;
* the structure-level ``expiry`` — on the parent order AND on the Transaction — stays NULL
  for such a structure, because there is no single date that is true of the whole position.
  A denormalised summary of a set with two elements is exactly the "money record asserting a
  date half the position does not honour" the guard's comment was written about;
* **everything else still refuses.** The relaxation is opt-in, so an undeclared tag, a
  missing tag, and the phase-gated ``calendar_spread`` are all still ValueError.

Persistence here is the REAL ``submit_option_order`` code path — only the broker call and
the transaction factory are stubbed — because what is under test is which rows exist.
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import OptionsAccountInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus

# A real PMCC shape: a January LEAPS covered by a near-dated short call.
NEAR = date(2026, 9, 18)
FAR = date(2027, 1, 15)

#: The declared strategy. Its builder is plan Task 6; this task only opens the door.
PMCC = "pmcc"


def _leg(expiry, strike=150.0, side=OrderDirection.BUY, right=OptionRight.CALL,
         intent="buy_to_open"):
    occ = (f"ACN{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
           f"{int(strike * 1000):08d}") if expiry is not None else f"ACN_{strike}"
    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=right, strike=strike, expiry=expiry, underlying="ACN")


def _pmcc_legs():
    """Long LEAPS at 150, short overlay at 170 — short strike above the long, as admission
    will require."""
    return [_leg(expiry=FAR, strike=150.0),
            _leg(expiry=NEAR, strike=170.0, side=OrderDirection.SELL, intent="sell_to_open")]


class _OptionAccount(OptionsAccountInterface):
    """Concrete option-capable account double: no broker SDK, no network, no chain fetch."""

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.submitted: list = []

    def get_option_chain(self, *a, **k):
        raise AssertionError("these tests must never fetch a chain")

    def get_option_quote(self, contract_symbol):
        raise AssertionError("these tests must never fetch a quote")

    def get_atm_implied_volatility(self, underlying):
        raise AssertionError("these tests must never fetch IV")

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        raise AssertionError("these tests never close")

    def _create_transaction_for_order(self, trading_order):
        trading_order.transaction_id = add_instance(Transaction(
            symbol=trading_order.symbol, quantity=trading_order.quantity,
            side=trading_order.side, multiplier=100, expert_id=None))

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        self.submitted.append((trading_order, list(legs), list(leg_orders or [])))
        trading_order.status = OrderStatus.FILLED
        trading_order.broker_order_id = f"double-{trading_order.id}"
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = OrderStatus.FILLED
            update_instance(child)
        return trading_order


@pytest.fixture
def store():
    with ts.inmem_trades() as s:
        yield s


@pytest.fixture
def account():
    return _OptionAccount()


# ---------------------------------------------------------------------------
# the relaxation
# ---------------------------------------------------------------------------
def test_a_declared_multi_expiry_strategy_is_accepted(store, account):
    parent = account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                         limit_price=25.0, option_strategy=PMCC)

    assert parent is not None
    assert parent.status == OrderStatus.FILLED
    assert len(account.submitted) == 1, "the broker must be reached exactly once"


def test_each_leg_is_persisted_with_its_OWN_expiry(store, account):
    """The per-leg record. This is the storage the task was said to need — and it already
    existed: one TradingOrder child per leg, each carrying its own date."""
    parent = account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                         limit_price=25.0, option_strategy=PMCC)

    children = [o for o in store.all(TradingOrder) if o.parent_order_id == parent.id]
    assert len(children) == 2
    assert {c.expiry for c in children} == {NEAR, FAR}

    by_side = {c.side: c for c in children}
    assert by_side[OrderDirection.BUY].expiry == FAR, "the LEAPS is the long leg"
    assert by_side[OrderDirection.SELL].expiry == NEAR, "the overlay is the short leg"


def test_the_parent_order_records_NO_single_expiry_for_a_two_expiry_structure(store, account):
    """NULL is the honest value. Stamping either date would make the row the broker fills
    assert an expiry that half the position does not honour."""
    parent = account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                         limit_price=25.0, option_strategy=PMCC)

    assert parent.expiry is None
    assert parent.expiry not in (NEAR, FAR)


def test_the_transaction_records_NO_single_expiry_for_a_two_expiry_structure(store, account):
    """``Transaction.expiry`` KEEPS its single-value meaning: it is filled only when the
    structure genuinely has one. The legs are the record for everything else."""
    parent = account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                         limit_price=25.0, option_strategy=PMCC)

    txn = store.get(Transaction, parent.transaction_id)
    assert txn.expiry is None
    assert txn.option_strategy == PMCC, "the INTENT is still recorded"


def test_a_declared_strategy_on_ONE_expiry_still_stamps_that_expiry(store, account):
    """The relaxation permits disagreement; it does not stop recording agreement. A PMCC
    whose legs happen to share an expiry is an ordinary single-expiry structure."""
    legs = [_leg(expiry=FAR, strike=150.0),
            _leg(expiry=FAR, strike=170.0, side=OrderDirection.SELL, intent="sell_to_open")]

    parent = account.submit_option_order(legs, quantity=1, order_type="limit",
                                         limit_price=25.0, option_strategy=PMCC)

    assert parent.expiry == FAR
    assert store.get(Transaction, parent.transaction_id).expiry == FAR


def test_the_declared_tag_is_matched_case_insensitively(store, account):
    parent = account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                         limit_price=25.0, option_strategy="PMCC")
    assert parent is not None


# ---------------------------------------------------------------------------
# FAIL-CLOSED: everything undeclared still refuses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", [
    None,
    "",
    "calendar_spread",      # phase-2: O_CAL is still gated, so still refused
    "diagonal_spread",      # the shape PMCC is, under a tag nobody declared
    "bull_call_spread",
    "pmcc_v2",              # near-miss: membership is exact, never a prefix
    "spread",
])
def test_an_undeclared_two_expiry_submit_is_still_refused(store, account, strategy):
    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                    limit_price=25.0, option_strategy=strategy)

    assert account.submitted == [], "the broker must never see an undeclared multi-expiry"
    assert store.all(TradingOrder) == [], "a refused structure left order rows behind"
    assert store.all(Transaction) == [], "a refused structure left a Transaction behind"


def test_the_default_is_refusal_when_no_strategy_argument_is_passed(store, account):
    """Not merely "None refuses" — the parameter's own default must refuse too."""
    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order(_pmcc_legs(), quantity=1, order_type="limit",
                                    limit_price=25.0)
    assert store.all(TradingOrder) == [] and store.all(Transaction) == []


def test_a_three_expiry_structure_is_refused_even_when_declared(store, account):
    """The relaxation is for TWO-leg diagonals and calendars. Three distinct expiries is not
    a structure this platform's lifecycle can manage, and the named rules would have to pick
    among several legs per side with no stated basis."""
    legs = _pmcc_legs() + [_leg(expiry=date(2026, 10, 16), strike=180.0,
                                side=OrderDirection.SELL, intent="sell_to_open")]

    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=25.0,
                                    option_strategy=PMCC)

    assert store.all(TradingOrder) == [] and store.all(Transaction) == []
