"""OPT-L4 — the coverage sizer over-counted on the SELL side, and fail-open at that.

``_OptionEntryAction._held_equity_shares`` is what SIZES a covered call and a protective
put (``floor(held / 100)``). It walked the expert's OPENED transactions' orders with::

    if o.status not in get_executed_statuses(): continue
    qty = o.filled_qty
    if not qty: continue

Two defects, both on the SELL side, both fail-OPEN:

1. **A CANCELED sell that already filled is skipped.** A cancel-and-replace that races a
   fill leaves the SELL ``CANCELED`` with ``filled_qty=100``. Those shares genuinely left
   the account. ``reconcile_canceled_partial_fill`` repairs ``Transaction.quantity`` but
   writes no compensating order row, so this loop still counts the full 200 and writes 2
   contracts against 100 held — one of them naked. The codebase already compensates for
   exactly this elsewhere (``ReadOnlyAccountInterface`` recalculation:
   ``if order.status in executed_statuses or filled_qty > 0``); the sizer was the one
   place that did not.

2. **``filled_qty is None`` on an EXECUTED order is read as nothing.** ``if not qty`` folds
   NULL (the broker said it filled and never said how much — UNMEASURABLE) into 0.0 (a
   measurement). On a SELL that means shares that may be gone are counted as still held.
   ``models.py``'s ``get_current_open_qty`` already refuses to make that collapse and says
   so loudly; the sizer must too.

**Live-only.** The backtest cannot produce either state: every FILLED path sets
``filled_qty`` in the same breath as the status, and every CANCELED path either zeroes the
quantity or cancels an untouched resting order. Both callers of
``reconcile_canceled_partial_fill`` are in ``AlpacaAccount``. A live-money defect with no
backtest signal, which is why no grid run would surface it.
"""
import math

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.TradeActions import BuyProtectivePutAction, SellCoveredCallAction
from ba2_common.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)

EXPERT = 42


def _txn(symbol="AAPL", status=TransactionStatus.OPENED):
    return Transaction(symbol=symbol, quantity=200.0, side=OrderDirection.BUY,
                       status=status, expert_id=EXPERT)


def _order(txn_id, *, side=OrderDirection.BUY, status=OrderStatus.FILLED,
           quantity=200.0, filled_qty=200.0, asset_class=AssetClass.EQUITY):
    return TradingOrder(
        account_id=1, symbol="AAPL", quantity=quantity, filled_qty=filled_qty, side=side,
        order_type=(OrderType.MARKET if side == OrderDirection.BUY else OrderType.SELL_LIMIT),
        status=status, transaction_id=txn_id, asset_class=asset_class)


def _action(cls=SellCoveredCallAction):
    action = cls.__new__(cls)
    action.instrument_name = "AAPL"
    action.expert_recommendation = type("Rec", (), {"instance_id": EXPERT})()
    action.existing_order = None
    return action


# ---------------------------------------------------------------------------
# 1. the cancel that raced a fill
# ---------------------------------------------------------------------------

def test_a_canceled_sell_that_already_filled_still_removes_its_shares():
    """200 bought, 100 sold on a CANCELED order = 100 held, not 200."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL,
                            status=OrderStatus.CANCELED, quantity=200.0, filled_qty=100.0))

        held = _action()._held_equity_shares()
        assert held == 100.0, (
            f"the sizer reports {held} shares; 100 of the 200 left the account on a "
            f"CANCELED sell that had already filled, and writing floor({held}/100) "
            f"contracts against them leaves one naked")


def test_the_covered_call_written_against_a_raced_cancel_is_one_lot_not_two():
    """The consequence, in contracts — the number that reaches the broker."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL,
                            status=OrderStatus.CANCELED, quantity=200.0, filled_qty=100.0))

        held = _action()._held_equity_shares()
        assert int(math.floor(held / 100.0)) == 1


def test_a_canceled_sell_that_filled_NOTHING_removes_nothing():
    """The other side of the same rule: a cancelled resting order did not trade."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL,
                            status=OrderStatus.CANCELED, quantity=200.0, filled_qty=0.0))
        assert _action()._held_equity_shares() == 200.0


def test_a_canceled_BUY_that_already_filled_counts_its_shares_too():
    """Symmetric: a cancel does not un-trade a contract on either side."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY,
                            status=OrderStatus.CANCELED, quantity=200.0, filled_qty=100.0))
        assert _action()._held_equity_shares() == 100.0


# ---------------------------------------------------------------------------
# 2. UNMEASURABLE is not zero
# ---------------------------------------------------------------------------

def test_an_executed_order_with_no_filled_qty_makes_the_count_unmeasurable():
    """NULL filled_qty on a FILLED order is "we do not know", not "nothing"."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL, status=OrderStatus.FILLED,
                            quantity=100.0, filled_qty=None))

        held = _action()._held_equity_shares()
        assert held is None, (
            f"the sizer answered {held}: a SELL that filled an unknown amount was read as "
            f"having sold nothing, so shares that may be gone still count as cover")


def test_an_unmeasurable_count_refuses_the_covered_call_and_says_why():
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL, status=OrderStatus.FILLED,
                            quantity=100.0, filled_qty=None))

        action = _action()
        action._result = lambda ok, msg, data=None: (ok, msg)
        ok, msg = action._build_and_submit()
        assert ok is False
        assert "unmeasurable" in msg.lower() or "cannot be measured" in msg.lower(), msg


def test_an_unmeasurable_count_refuses_the_protective_put_too():
    """The sizer's other caller. One accessor, one refusal."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL, status=OrderStatus.FILLED,
                            quantity=100.0, filled_qty=None))

        action = _action(BuyProtectivePutAction)
        action._result = lambda ok, msg, data=None: (ok, msg)
        ok, msg = action._build_and_submit()
        assert ok is False
        assert "unmeasurable" in msg.lower() or "cannot be measured" in msg.lower(), msg


# ---------------------------------------------------------------------------
# what must NOT change
# ---------------------------------------------------------------------------

def test_an_unfilled_stock_buy_still_contributes_nothing():
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, status=OrderStatus.NEW,
                            quantity=200.0, filled_qty=None))
        assert _action()._held_equity_shares() == 0.0


def test_a_partial_buy_still_rounds_down_to_no_contract():
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY,
                            status=OrderStatus.PARTIALLY_FILLED,
                            quantity=100.0, filled_qty=60.0))
        held = _action()._held_equity_shares()
        assert held == 60.0 and int(math.floor(held / 100.0)) == 0


def test_option_orders_are_still_skipped():
    """A short call must never be able to count as its own cover."""
    with ts.inmem_trades():
        txn_id = add_instance(_txn())
        add_instance(_order(txn_id, side=OrderDirection.BUY, filled_qty=200.0))
        add_instance(_order(txn_id, side=OrderDirection.SELL, status=OrderStatus.CANCELED,
                            quantity=2.0, filled_qty=2.0, asset_class=AssetClass.OPTION))
        assert _action()._held_equity_shares() == 200.0


def test_no_expert_instance_is_still_a_hard_zero():
    with ts.inmem_trades():
        action = _action()
        action.expert_recommendation = None
        assert action._held_equity_shares() == 0.0
