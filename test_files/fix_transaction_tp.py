#!/usr/bin/env python3
"""
Fix transaction TP values to match their limit orders.

This script finds all transactions that have a SELL_LIMIT or BUY_LIMIT order
(take profit order) and syncs the transaction's take_profit field to match
the limit order's limit_price.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.db import get_db, get_instance, update_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.types import OrderType
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def fix_transaction_tp_values():
    """
    Find transactions with TP orders and sync their take_profit field.
    """
    db = get_db()
    with Session(db.bind) as session:
        try:
            # Find all transactions
            all_txns = session.exec(select(Transaction)).all()
            logger.info(f"Found {len(all_txns)} total transactions")
            
            fixed_count = 0
            mismatch_count = 0
            
            for txn in all_txns:
                if not txn.id:
                    continue
                
                # Find any SELL_LIMIT or BUY_LIMIT orders for this transaction
                statement = select(TradingOrder).where(
                    TradingOrder.transaction_id == txn.id,
                    TradingOrder.order_type.in_([OrderType.SELL_LIMIT, OrderType.BUY_LIMIT])
                )
                tp_orders = session.exec(statement).all()
                
                if not tp_orders:
                    continue
                
                # Check if any order's limit_price doesn't match txn.take_profit
                for order in tp_orders:
                    if not order.limit_price:
                        continue
                    
                    if txn.take_profit is None or abs(txn.take_profit - order.limit_price) > 0.01:
                        mismatch_count += 1
                        old_tp = txn.take_profit
                        
                        # Refresh to get latest version
                        fresh_txn = get_instance(Transaction, txn.id)
                        if fresh_txn:
                            fresh_txn.take_profit = order.limit_price
                            update_instance(fresh_txn)
                            fixed_count += 1
                            logger.info(
                                f"Fixed transaction {txn.id} ({txn.symbol}): "
                                f"TP was ${old_tp}, order limit_price is ${order.limit_price:.2f}, "
                                f"synced to ${order.limit_price:.2f}"
                            )
                        break  # Only use first TP order per transaction
            
            logger.info(f"\n=== SUMMARY ===")
            logger.info(f"Transactions checked: {len(all_txns)}")
            logger.info(f"Transactions with mismatched TP: {mismatch_count}")
            logger.info(f"Transactions fixed: {fixed_count}")
            
            return fixed_count
            
        except Exception as e:
            logger.error(f"Error fixing transaction TP values: {e}", exc_info=True)
            return 0

if __name__ == "__main__":
    print("=" * 60)
    print("Transaction TP Fix Script")
    print("=" * 60)
    print("\nThis script will:")
    print("1. Find all transactions with SELL_LIMIT or BUY_LIMIT orders")
    print("2. Sync the transaction's take_profit field to match the order's limit_price")
    print("\n")
    
    fixed = fix_transaction_tp_values()
    
    print("\n" + "=" * 60)
    print(f"✓ Fixed {fixed} transactions")
    print("=" * 60)
