#!/usr/bin/env python3
"""
Utility to clean up orphaned transactions that have no orders.
These transactions cause "Cannot find account for transaction" errors.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.core.db import get_db, get_instance, update_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.types import TransactionStatus
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def main():
    """Clean up orphaned transactions."""
    print("=== Cleaning Up Orphaned Transactions ===\n")
    
    session = get_db()
    
    try:
        # Find all transactions with no orders
        print("Finding orphaned transactions...")
        
        orphaned_transactions = []
        transactions_stmt = select(Transaction)
        transactions = session.exec(transactions_stmt).all()
        
        for txn in transactions:
            # Check if transaction has any orders
            orders_stmt = select(TradingOrder).where(TradingOrder.transaction_id == txn.id)
            orders = session.exec(orders_stmt).all()
            
            if not orders:
                orphaned_transactions.append(txn)
        
        print(f"Found {len(orphaned_transactions)} orphaned transactions\n")
        
        if not orphaned_transactions:
            print("✓ No orphaned transactions found!")
            return
        
        # Group by status for reporting
        status_counts = {}
        for txn in orphaned_transactions:
            status = txn.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        
        print("Orphaned transactions by status:")
        for status, count in status_counts.items():
            print(f"  {status}: {count}")
        print()
        
        # Ask for confirmation before cleanup
        print("These transactions will be marked as FAILED since they have no orders.")
        print("This will prevent the 'Cannot find account for transaction' error.")
        response = input("Do you want to proceed with cleanup? (y/N): ").strip().lower()
        
        if response != 'y':
            print("Cleanup cancelled.")
            return
        
        # Clean up orphaned transactions
        print("\nCleaning up orphaned transactions...")
        
        cleaned_count = 0
        for txn in orphaned_transactions:
            try:
                # Mark as FAILED to indicate the transaction couldn't be processed
                original_status = txn.status
                txn.status = TransactionStatus.FAILED
                
                # Update in database
                update_instance(txn)
                
                print(f"  ✓ Transaction {txn.id} ({txn.symbol}) - {original_status.value} → FAILED")
                cleaned_count += 1
                
            except Exception as e:
                print(f"  ❌ Failed to update Transaction {txn.id}: {e}")
        
        print(f"\n✓ Successfully cleaned up {cleaned_count} orphaned transactions")
        print("These transactions will no longer cause 'Cannot find account' errors.")
        
    except Exception as e:
        logger.error(f"Error during cleanup: {e}", exc_info=True)
        print(f"Error during cleanup: {e}")

if __name__ == "__main__":
    main()