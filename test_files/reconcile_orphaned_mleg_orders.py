#!/usr/bin/env python
"""One-time reconciliation for MLEG (multi-leg option) spread orders that were
orphaned by the `side=None` mapper bug (fixed in alpaca_order_to_tradingorder).

These spreads were ACCEPTED at Alpaca but the post-submit writeback raised
before linking the broker leg ids, leaving:
  - parent: status flipped back to ACCEPTED by refresh, but legs_broker_ids=None
            and a stale "option submit error" comment
  - children: PENDING with no broker_order_id

This re-fetches each parent live, matches each child leg to its broker leg by
contract symbol (same logic as AlpacaAccount._submit_option_order_impl), and
backfills broker_order_id / status / filled_qty + parent.legs_broker_ids, then
clears the stale error comment.

Run:
    .venv/Scripts/python.exe test_files/reconcile_orphaned_mleg_orders.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import select

from ba2_trade_platform.core.db import get_db, update_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.utils import get_account_instance_from_id

ACCOUNT_ID = 5
PARENT_IDS = [1397, 1400]


def reconcile():
    acct = get_account_instance_from_id(ACCOUNT_ID)
    for pid in PARENT_IDS:
        parent = get_instance(TradingOrder, pid)
        if parent is None:
            print(f"parent {pid}: NOT FOUND, skipping")
            continue
        if not parent.broker_order_id:
            print(f"parent {pid}: no broker_order_id, skipping")
            continue

        order = acct.client.get_order_by_id(parent.broker_order_id)
        broker_legs = list(getattr(order, "legs", None) or [])
        print(f"\nparent {pid} ({parent.symbol}) broker={parent.broker_order_id} "
              f"broker_status={getattr(order, 'status', None)} legs={len(broker_legs)}")

        # Parent: status from broker, leg ids, clear the stale error comment.
        mapped = acct.alpaca_order_to_tradingorder(order)
        if mapped.status:
            parent.status = mapped.status
        parent.legs_broker_ids = [str(l.id) for l in broker_legs if getattr(l, "id", None)]
        if parent.comment and "option submit error" in parent.comment:
            parent.comment = None
        update_instance(parent)
        print(f"  parent -> status={parent.status} legs_broker_ids={parent.legs_broker_ids} comment_cleared")

        # Children: match each to its broker leg by contract symbol.
        with get_db() as session:
            children = session.exec(
                select(TradingOrder).where(TradingOrder.parent_order_id == pid)
            ).all()
            child_ids = [c.id for c in children]

        remaining = list(broker_legs)
        for cid in child_ids:
            child = get_instance(TradingOrder, cid)
            matched = next((bl for bl in remaining
                            if getattr(bl, "symbol", None) == child.contract_symbol), None)
            if matched is None:
                print(f"  child {cid} ({child.contract_symbol}): NO broker-leg match")
                continue
            remaining.remove(matched)
            child.broker_order_id = str(matched.id) if matched.id else None
            child_mapped = acct.alpaca_order_to_tradingorder(matched)
            if child_mapped.status:
                child.status = child_mapped.status
            if child_mapped.filled_qty is not None:
                child.filled_qty = child_mapped.filled_qty
            update_instance(child)
            print(f"  child {cid} ({child.contract_symbol}) -> broker={child.broker_order_id} "
                  f"status={child.status} filled={child.filled_qty}")

    print("\nDone.")


if __name__ == "__main__":
    reconcile()
