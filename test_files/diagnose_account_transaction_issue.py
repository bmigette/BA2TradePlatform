#!/usr/bin/env python3
"""
Diagnostic script to check for transactions that might cause the 
"Cannot find account for transaction" error.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.utils import get_account_instance_from_transaction
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def main():
    """Check for problematic transactions."""
    print("=== Diagnosing Transaction-Account Relationship Issues ===\n")
    
    session = get_db()
    
    try:
        # Get all transactions
        transactions_stmt = select(Transaction)
        transactions = session.exec(transactions_stmt).all()
        
        print(f"Found {len(transactions)} transactions in database\n")
        
        problematic_transactions = []
        
        for txn in transactions:
            print(f"Checking Transaction {txn.id} ({txn.symbol}, {txn.status.value})...")
            
            # Get orders for this transaction
            orders_stmt = select(TradingOrder).where(TradingOrder.transaction_id == txn.id)
            orders = session.exec(orders_stmt).all()
            
            if not orders:
                print(f"  ❌ NO ORDERS FOUND - This will cause 'Cannot find account' error")
                problematic_transactions.append((txn.id, "no_orders"))
                continue
            
            print(f"  Found {len(orders)} orders")
            
            # Check first order
            first_order = orders[0]
            if not first_order.account_id:
                print(f"  ❌ FIRST ORDER HAS NO ACCOUNT_ID - Order {first_order.id} missing account_id")
                problematic_transactions.append((txn.id, "no_account_id"))
                continue
            
            print(f"  ✓ First order {first_order.id} has account_id: {first_order.account_id}")
            
            # Test our helper function
            try:
                account = get_account_instance_from_transaction(txn.id, session=session)
                if account:
                    print(f"  ✓ Successfully got account instance: {account.__class__.__name__}")
                else:
                    print(f"  ❌ Helper function returned None")
                    problematic_transactions.append((txn.id, "helper_failed"))
            except Exception as e:
                print(f"  ❌ Helper function failed with error: {e}")
                problematic_transactions.append((txn.id, f"helper_error: {e}"))
            
            print()
        
        # Summary
        print("=== SUMMARY ===")
        if problematic_transactions:
            print(f"❌ Found {len(problematic_transactions)} problematic transactions:")
            for txn_id, issue in problematic_transactions:
                print(f"  - Transaction {txn_id}: {issue}")
            print("\nThese transactions will cause 'Cannot find account for transaction' errors.")
        else:
            print("✓ All transactions have proper account relationships!")
    
    except Exception as e:
        logger.error(f"Error during diagnosis: {e}", exc_info=True)
        print(f"Error during diagnosis: {e}")

if __name__ == "__main__":
    main()