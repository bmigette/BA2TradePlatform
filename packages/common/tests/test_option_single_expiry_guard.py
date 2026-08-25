"""A structure whose legs span two expiries is refused at the submission boundary.

``Transaction.expiry`` (Task 1) holds ONE date and calls it "the structure's expiry". That is
honest only because every structure this platform supports — the four singles, the four
verticals, straddle/strangle and their short forms, iron condor, jade lizard, call butterfly,
put ratio spread — puts all of its legs on a single expiry. There are no calendars and no
diagonals.

The moment one is added, ``Transaction.expiry`` stops being *incomplete* and becomes *wrong*:
a money record silently asserting a date that half the position does not honour. These tests
pin the refusal at ``submit_option_order``, before any row exists, so the failure mode is a
loud ``ValueError`` rather than a plausible-looking lie in the book.
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

# Two real monthly expiries. Frozen constants, never "today" — a date that happens to equal the
# wall clock has let a mutation survive in this repo before.
AUG = date(2026, 8, 21)
SEP = date(2026, 9, 18)


def _leg(expiry, strike=150.0, side=OrderDirection.BUY, right=OptionRight.CALL,
         intent="buy_to_open"):
    """One OCC-shaped leg on ACN. The OCC string encodes the expiry, as a real one does."""
    occ = (f"ACN{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
           f"{int(strike * 1000):08d}") if expiry is not None else f"ACN_{strike}"
    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=right, strike=strike, expiry=expiry, underlying="ACN")


class _OptionAccount(OptionsAccountInterface):
    """Concrete option-capable account double: no broker SDK, no network, no chain fetch.

    Persistence is the REAL ``submit_option_order`` code path — only the broker call and the
    transaction factory are stubbed — because what is under test is exactly which rows exist
    when the refusal happens.
    """

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.submitted: list = []

    # --- abstract market-data surface: unreachable in these tests -----------
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

    # --- the two seams submit_option_order reaches into ---------------------
    def _create_transaction_for_order(self, trading_order):
        """Stand-in for AccountInterface's factory: writes a real Transaction row."""
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
    """A fresh in-RAM order/transaction store per test, so "nothing was written" is exact."""
    with ts.inmem_trades() as s:
        yield s


@pytest.fixture
def account():
    return _OptionAccount()


def test_submitting_legs_with_different_expiries_is_refused(store, account):
    """A calendar spread would make Transaction.expiry a lie. Refuse it at the boundary."""
    legs = [_leg(expiry=AUG), _leg(expiry=SEP)]
    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=1.5,
                                    option_strategy="calendar_spread")
    assert account.submitted == [], "the broker must never see a multi-expiry structure"


def test_the_refusal_names_the_offending_expiries_and_the_reason(store, account):
    """An error a reader cannot act on is a nuisance. Name both dates and say why."""
    legs = [_leg(expiry=AUG), _leg(expiry=SEP)]
    with pytest.raises(ValueError) as excinfo:
        account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=1.5)
    message = str(excinfo.value)
    assert "2026-08-21" in message and "2026-09-18" in message, message
    assert "Transaction.expiry" in message, message


def test_all_legs_on_one_expiry_is_accepted(store, account):
    """The normal path must be completely unaffected: parent, children and Transaction."""
    legs = [_leg(expiry=AUG, strike=150.0),
            _leg(expiry=AUG, strike=160.0, side=OrderDirection.SELL, intent="sell_to_open")]

    parent = account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=4.0,
                                         option_strategy="bull_call_spread")

    assert parent is not None
    assert parent.status == OrderStatus.FILLED
    assert parent.transaction_id is not None
    assert len(account.submitted) == 1, "the broker must be reached exactly once"
    children = [o for o in store.all(TradingOrder) if o.parent_order_id == parent.id]
    assert len(children) == 2
    assert {c.expiry for c in children} == {AUG}
    assert len(store.all(Transaction)) == 1


def test_a_single_leg_is_trivially_accepted(store, account):
    """One leg cannot span two expiries; the guard must not invent a problem."""
    parent = account.submit_option_order([_leg(expiry=AUG)], quantity=2, order_type="limit",
                                         limit_price=5.2, option_strategy="long_call")

    assert parent is not None
    assert parent.expiry == AUG
    assert len(store.all(TradingOrder)) == 1
    assert len(store.all(Transaction)) == 1


def test_a_refusal_writes_nothing(store, account):
    """No parent, no children, no Transaction. A half-written refusal is worse than the bug."""
    assert store.all(TradingOrder) == [] and store.all(Transaction) == []

    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order([_leg(expiry=AUG), _leg(expiry=SEP)], quantity=1,
                                    order_type="limit", limit_price=1.5)

    assert store.all(TradingOrder) == [], "a refused structure left order rows behind"
    assert store.all(Transaction) == [], "a refused structure left a Transaction behind"


def test_a_four_leg_condor_with_one_stray_expiry_is_refused(store, account):
    """The stray leg is LAST: checking only the first two legs is not checking anything."""
    legs = [
        _leg(expiry=AUG, strike=140.0, right=OptionRight.PUT, side=OrderDirection.SELL,
             intent="sell_to_open"),
        _leg(expiry=AUG, strike=130.0, right=OptionRight.PUT),
        _leg(expiry=AUG, strike=160.0, side=OrderDirection.SELL, intent="sell_to_open"),
        _leg(expiry=SEP, strike=170.0),                      # <- the diagonal wing
    ]
    with pytest.raises(ValueError, match="single expiry"):
        account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=-1.2,
                                    option_strategy="iron_condor")

    assert store.all(TradingOrder) == [] and store.all(Transaction) == []


def test_a_genuine_four_leg_iron_condor_is_accepted(store, account):
    """Four legs on one expiry is the platform's widest supported structure — never refuse it."""
    legs = [
        _leg(expiry=AUG, strike=140.0, right=OptionRight.PUT, side=OrderDirection.SELL,
             intent="sell_to_open"),
        _leg(expiry=AUG, strike=130.0, right=OptionRight.PUT),
        _leg(expiry=AUG, strike=160.0, side=OrderDirection.SELL, intent="sell_to_open"),
        _leg(expiry=AUG, strike=170.0),
    ]
    parent = account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=-1.2,
                                         option_strategy="iron_condor")

    assert parent is not None
    assert len([o for o in store.all(TradingOrder) if o.parent_order_id == parent.id]) == 4


def test_an_unrecorded_expiry_is_not_treated_as_a_second_expiry(store, account):
    """A leg with no expiry is UNKNOWN, not a calendar.

    The close paths rebuild legs from stored order rows -- ``PremiumSeller/portfolio.py``
    reads ``getattr(o, "expiry", None)`` -- so a row written before the option columns
    existed yields a leg with ``expiry=None``. Refusing on that would strand an OPEN
    position that cannot then be flattened, which is a far worse failure than an
    incomplete intent record. Only two KNOWN, DIFFERENT dates are a lie.
    """
    legs = [_leg(expiry=AUG, strike=150.0),
            _leg(expiry=None, strike=160.0, side=OrderDirection.SELL, intent="sell_to_close")]

    parent = account.submit_option_order(legs, quantity=1, order_type="market",
                                         option_strategy="close")

    assert parent is not None
    assert len([o for o in store.all(TradingOrder) if o.parent_order_id == parent.id]) == 2
