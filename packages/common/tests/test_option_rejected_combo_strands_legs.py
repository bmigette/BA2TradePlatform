"""OPT-S1 — a REJECTED combo must not strand its leg children PENDING forever.

``submit_option_order`` persists a parent order, N leg children and a Transaction, and only
THEN calls the broker. When that call raises — an Alpaca ``APIError`` on rejection (approval
tier, insufficient buying power on an ungated debit combo) or any transient network error,
since ``alpaca_api_retry`` re-raises every non-429 ``APIError`` — the except block set only
the PARENT to ERROR. The N children kept ``status=PENDING`` and ``broker_order_id=None``.

Nothing self-heals that state:

* ``refresh_orders`` sweeps only orders that HAVE a broker id;
* ``refresh_transactions``' ``never_opened`` cleanup requires ALL of the transaction's
  orders to be terminal, and PENDING is not;
* ``_fail_unsent_entry`` is equity-only;
* ``clean_pending_orders`` is a manual UI button.

``open_option_orders_book_wide`` keeps every non-terminal option order whose transaction is
not CLOSED/FAILED, so a stranded SHORT PUT child is counted as live assignment exposure for
ever, and ``_refuse_if_cannot_take_delivery`` then blocks bear put spread, bull put spread,
cash-secured put, short straddle, short strangle, iron condor, jade lizard and put ratio
spread — account-wide, across every expert, until someone intervenes by hand.

THE ONE CASE THAT MUST *NOT* BE TERMINALISED is a raise AFTER the broker accepted the order
(a write-back failure). Those contracts are genuinely live; marking them terminal would hide
a real short put from the very gate above. The two cases are told apart by the parent's
persisted ``broker_order_id``.
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_instance, update_instance
from ba2_common.core.interfaces import OptionsAccountInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, TransactionStatus,
)

SEP = date(2026, 9, 18)


def _occ(strike: float, right: OptionRight, expiry: date = SEP) -> str:
    return (f"ACN{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
            f"{int(strike * 1000):08d}")


def _leg(strike, right=OptionRight.PUT, side=OrderDirection.SELL, intent="sell_to_open"):
    return OptionLeg(contract_symbol=_occ(strike, right), side=side, position_intent=intent,
                     option_type=right, strike=strike, expiry=SEP, underlying="ACN")


class _RejectingAccount(OptionsAccountInterface):
    """Option-capable double whose broker call FAILS the way Alpaca fails.

    ``reached_broker`` reproduces the other window: the order WAS accepted (its id is
    persisted, exactly as the live adapter now persists it the instant ``submit_order``
    returns) and the failure happened afterwards, writing the response back.
    """

    def __init__(self, account_id: int = 1, *, reached_broker: bool = False):
        self.id = account_id
        self.reached_broker = reached_broker
        self.calls = 0

    def get_option_chain(self, *a, **k):
        raise AssertionError("no chain fetch in these tests")

    def get_option_quote(self, contract_symbol):
        raise AssertionError("no quote fetch in these tests")

    def get_atm_implied_volatility(self, underlying):
        raise AssertionError("no IV fetch in these tests")

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        raise AssertionError("these tests never close")

    def get_balance(self):
        return 100_000.0

    def _create_transaction_for_order(self, trading_order):
        trading_order.transaction_id = add_instance(Transaction(
            symbol=trading_order.symbol, quantity=trading_order.quantity,
            side=trading_order.side, multiplier=100, expert_id=None,
            status=TransactionStatus.WAITING))

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        self.calls += 1
        if self.reached_broker:
            # The live adapter persists the broker id the instant submit_order returns,
            # BEFORE any response mapping — so this window is visible to the wrapper.
            trading_order.broker_order_id = "alpaca-accepted-1"
            update_instance(trading_order)
            raise ValueError("write-back of the broker response failed")
        raise ValueError("APIError: account is not approved for this option level")


@pytest.fixture
def store():
    with ts.inmem_trades() as s:
        yield s


def _bull_put_spread(account):
    """Short 120 put / long 110 put — the short leg is what poisons the delivery gate."""
    legs = [_leg(120.0), _leg(110.0, side=OrderDirection.BUY, intent="buy_to_open")]
    return account.submit_option_order(legs, quantity=2, order_type="limit", limit_price=-1.5,
                                       option_strategy="bull_put_spread")


def _children(parent_id):
    return [o for o in ts.store_all(TradingOrder) if o.parent_order_id == parent_id]


def _the_parent():
    return [o for o in ts.store_all(TradingOrder)
            if o.asset_class == AssetClass.OPTION and o.parent_order_id is None][0]


# ---------------------------------------------------------------------------
# The rejection: nothing exists at the broker.
# ---------------------------------------------------------------------------

def test_a_rejected_combo_terminalises_every_leg_child(store):
    acct = _RejectingAccount()
    assert _bull_put_spread(acct) is None          # the seam swallows and returns None

    parent = _the_parent()
    assert parent.status == OrderStatus.ERROR
    terminal = OrderStatus.get_terminal_statuses()
    kids = _children(parent.id)
    assert len(kids) == 2
    stranded = [(k.id, k.contract_symbol, str(k.status)) for k in kids
                if k.status not in terminal]
    assert stranded == [], (
        f"these leg children of a REJECTED combo are still non-terminal and will never be "
        f"swept — nothing at the broker carries them: {stranded}")


def test_a_rejected_short_put_leaves_no_phantom_assignment_exposure(store):
    """The concrete harm: a stranded short put blocks 7 structures, permanently."""
    acct = _RejectingAccount()
    _bull_put_spread(acct)

    exposure = acct.short_put_assignment_exposure()
    assert exposure.cost == 0.0, (
        f"a combo the broker REJECTED is still reported as ${exposure.cost} of short-put "
        f"delivery obligation; every put-selling structure is refused until it is cleared "
        f"by hand (unmeasurable: {exposure.unmeasurable})")
    assert acct.open_option_orders_book_wide() == [], (
        "the rejected combo's orders are still counted as an open position")


def test_a_rejected_combo_records_why_on_every_row(store):
    """An operator reading the leg row must see the rejection, not a bare status."""
    acct = _RejectingAccount()
    _bull_put_spread(acct)
    for k in _children(_the_parent().id):
        assert k.comment and "not approved" in k.comment, (
            f"leg {k.id} was terminalised with no reason recorded: {k.comment!r}")


def test_the_stub_transaction_becomes_collectable(store):
    """Terminalising the children is what re-arms the EXISTING self-heal.

    ``refresh_transactions``' ``never_opened`` cleanup requires every order on the
    transaction to be terminal. With the children left PENDING it could never fire, which
    is why the stub survived every refresh cycle.
    """
    acct = _RejectingAccount()
    _bull_put_spread(acct)
    orders = [o for o in ts.store_all(TradingOrder)]
    terminal = OrderStatus.get_terminal_statuses()
    assert all(o.status in terminal for o in orders), (
        f"not all orders terminal -> never_opened cannot fire: "
        f"{[(o.id, str(o.status)) for o in orders if o.status not in terminal]}")
    txn = get_instance(Transaction, orders[0].transaction_id)
    assert txn.open_date is None and txn.status != TransactionStatus.OPENED


# ---------------------------------------------------------------------------
# The other window: the broker DID accept. Nothing may be terminalised.
# ---------------------------------------------------------------------------

def test_a_write_back_failure_after_acceptance_keeps_the_position_visible(store):
    """The contracts are LIVE. Terminalising them would hide a real short put.

    This is the asymmetry that makes the fix safe: a rejected combo must vanish from the
    book, an accepted one must stay in it even though the same ``except`` caught both.
    """
    acct = _RejectingAccount(reached_broker=True)
    _bull_put_spread(acct)

    parent = _the_parent()
    assert parent.broker_order_id == "alpaca-accepted-1"
    terminal = OrderStatus.get_terminal_statuses()
    assert parent.status not in terminal, (
        f"an order the broker ACCEPTED was marked {parent.status} — it leaves the book "
        f"entirely while the contracts are live at the broker")
    assert all(k.status not in terminal for k in _children(parent.id))
    exposure = acct.short_put_assignment_exposure()
    assert exposure.cost and exposure.cost > 0, (
        "a live short put stopped counting against assignment capacity")
