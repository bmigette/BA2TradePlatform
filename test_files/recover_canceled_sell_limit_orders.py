"""
Recovery script for SELL_LIMIT orders that were canceled in Alpaca but still show as NEW in database.

This script:
1. Finds orders showing as NEW/PENDING_NEW in database but CANCELED in Alpaca
2. Filters to only SELL_LIMIT orders that belong to OPEN transactions
3. Resubmits them to Alpaca broker
4. Updates the database records with new broker_order_id and status

Run with: .venv\Scripts\python.exe test_files\recover_canceled_sell_limit_orders.py
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderType, OrderStatus, TransactionStatus
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_instance, update_instance, get_db
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def find_mismatched_orders(account: AlpacaAccount):
    """Find orders that show as NEW in DB but CANCELED in Alpaca."""
    mismatches = []
    
    with Session(get_db().bind) as session:
        # Get all orders that are marked as NEW or PENDING_NEW in database
        statement = select(TradingOrder).where(
            TradingOrder.status.in_([OrderStatus.NEW, OrderStatus.PENDING_NEW])
        ).where(
            TradingOrder.broker_order_id.is_not(None)
        ).where(
            TradingOrder.account_id == account.id
        )
        
        orders = session.exec(statement).all()
        
        logger.info(f"Checking {len(orders)} orders with status NEW/PENDING_NEW and broker_order_id set")
        
        for order in orders:
            try:
                # Get order from Alpaca
                alpaca_order = account.client.get_order_by_id(order.broker_order_id)
                
                db_status = order.status.value if hasattr(order.status, 'value') else order.status
                alpaca_status = str(alpaca_order.status.value if hasattr(alpaca_order.status, 'value') else alpaca_order.status).upper()
                
                # Check if it's a mismatch
                if db_status != alpaca_status.lower() and alpaca_status == "CANCELED":
                    mismatches.append({
                        "order": order,
                        "alpaca_status": alpaca_status
                    })
                    
            except Exception as e:
                logger.error(f"Error checking order {order.id}: {e}")
                continue
    
    return mismatches


def filter_sell_limit_open_transactions(mismatches):
    """Filter mismatches to only include SELL_LIMIT orders for OPENED transactions."""
    filtered = []
    
    for mismatch in mismatches:
        order = mismatch["order"]
        
        # Check if it's a SELL_LIMIT order
        if order.order_type != OrderType.SELL_LIMIT:
            logger.debug(f"Skipping order {order.id} - not SELL_LIMIT (is {order.order_type})")
            continue
        
        # Check if it has a transaction
        if not order.transaction_id:
            logger.debug(f"Skipping order {order.id} - no transaction_id")
            continue
        
        # Get transaction and check if it's OPENED
        try:
            transaction = get_instance(Transaction, order.transaction_id)
            if not transaction:
                logger.warning(f"Skipping order {order.id} - transaction {order.transaction_id} not found")
                continue
            
            # Compare enum values properly
            transaction_status = transaction.status.value if hasattr(transaction.status, 'value') else transaction.status
            target_status = TransactionStatus.OPENED.value if hasattr(TransactionStatus.OPENED, 'value') else TransactionStatus.OPENED
            
            if transaction_status != target_status:
                logger.debug(f"Skipping order {order.id} - transaction {order.transaction_id} is {transaction_status}, not OPENED")
                continue
            
            # This order qualifies for recovery
            filtered.append(mismatch)
            
        except Exception as e:
            logger.error(f"Error checking transaction for order {order.id}: {e}", exc_info=True)
            continue
    
    return filtered


def recover_orders(account: AlpacaAccount, orders_to_recover):
    """Resubmit orders and update database records."""
    print()
    print("="*80)
    print("  Resubmitting Orders")
    print("="*80)
    print()
    
    success_count = 0
    error_count = 0
    
    for mismatch in orders_to_recover:
        order = mismatch["order"]
        
        try:
            print(f"Resubmitting order {order.id} ({order.symbol} {order.side.value} {order.quantity})...")
            
            # Get fresh instance to avoid stale data
            fresh_order = get_instance(TradingOrder, order.id)
            
            if not fresh_order:
                print(f"  ✗ Order {order.id} not found in database")
                error_count += 1
                continue
            
            # Reset order status to PENDING before resubmission
            fresh_order.status = OrderStatus.PENDING
            fresh_order.broker_order_id = None  # Clear old broker order ID
            
            # Generate completely new tracking comment to avoid exceeding 128 char limit
            import time
            epoch_time = int(time.time() * 1000000)  # Microseconds since epoch
            
            # Create short, clean comment
            if fresh_order.transaction_id:
                if fresh_order.limit_price:
                    fresh_order.comment = f"{epoch_time}-TR{fresh_order.transaction_id} TP"
                elif fresh_order.stop_price:
                    fresh_order.comment = f"{epoch_time}-TR{fresh_order.transaction_id} SL"
                else:
                    fresh_order.comment = f"{epoch_time}-TR{fresh_order.transaction_id}"
            else:
                fresh_order.comment = f"{epoch_time}-Order{fresh_order.id}"
            
            update_instance(fresh_order)
            
            # Resubmit to broker using the account's submit method
            # This will update broker_order_id and status automatically
            result = account._submit_order_impl(fresh_order)
            
            if result and result.broker_order_id:
                print(f"  ✓ Order {order.id} resubmitted successfully")
                print(f"    New broker_order_id: {result.broker_order_id}")
                print(f"    Status: {result.status.value}")
                success_count += 1
            else:
                print(f"  ✗ Order {order.id} resubmission failed - no broker order ID returned")
                error_count += 1
                
        except Exception as e:
            print(f"  ✗ Error resubmitting order {order.id}: {e}")
            logger.error(f"Error resubmitting order {order.id}", exc_info=True)
            error_count += 1
    
    print()
    print("="*80)
    print("  Recovery Summary")
    print("="*80)
    print(f"Total orders processed: {len(orders_to_recover)}")
    print(f"Successfully resubmitted: {success_count}")
    print(f"Errors: {error_count}")
    print()
    
    if success_count > 0:
        print("✓ Orders have been resubmitted and database records updated")
        print("  Run account.refresh_orders() to sync latest statuses from broker")
    
    return success_count, error_count


def main():
    """Main execution."""
    print("="*80)
    print("  SELL_LIMIT Order Recovery Script")
    print("  (For OPENED transactions with canceled orders)")
    print("="*80)
    print()
    
    # Connect to account
    try:
        account = AlpacaAccount(1)
        print(f"✓ Connected to Alpaca account {account.id}")
        print()
    except Exception as e:
        print(f"✗ Failed to connect to Alpaca account: {e}")
        logger.error("Failed to connect to Alpaca account", exc_info=True)
        return
    
    # Step 1: Find all mismatched orders
    print("Step 1: Finding orders with status mismatches...")
    print()
    mismatches = find_mismatched_orders(account)
    print(f"Found {len(mismatches)} orders with status mismatch (DB:NEW, Alpaca:CANCELED)")
    print()
    
    if not mismatches:
        print("✓ No mismatched orders found. Database is in sync with Alpaca.")
        return
    
    # Step 2: Filter to SELL_LIMIT orders for OPENED transactions
    print("Step 2: Filtering to SELL_LIMIT orders for OPENED transactions...")
    print()
    orders_to_recover = filter_sell_limit_open_transactions(mismatches)
    print(f"Found {len(orders_to_recover)} SELL_LIMIT orders for OPENED transactions")
    print()
    
    if not orders_to_recover:
        print("✓ No SELL_LIMIT orders for OPENED transactions need recovery")
        print(f"  ({len(mismatches)} total mismatches, but none meet recovery criteria)")
        return
    
    # Display orders to be recovered
    print("Orders to be recovered:")
    print()
    
    # Group by transaction for better organization
    transactions = {}
    for mismatch in orders_to_recover:
        order = mismatch["order"]
        if order.transaction_id not in transactions:
            transactions[order.transaction_id] = []
        transactions[order.transaction_id].append(order)
    
    for transaction_id, orders in transactions.items():
        print(f"Transaction {transaction_id}:")
        for order in orders:
            print(f"  - Order {order.id}: {order.symbol} {order.side.value} {order.quantity} @ {order.order_type.value}")
            if order.limit_price:
                print(f"    Limit price: ${order.limit_price:.2f}")
            if order.stop_price:
                print(f"    Stop price: ${order.stop_price:.2f}")
    print()
    
    # Ask for confirmation
    response = input(f"Resubmit these {len(orders_to_recover)} orders? (yes/no): ").strip().lower()
    
    if response != 'yes':
        print("✗ Recovery canceled by user")
        return
    
    # Step 3: Recover the orders
    success_count, error_count = recover_orders(account, orders_to_recover)
    
    # Final summary
    if len(mismatches) > len(orders_to_recover):
        skipped = len(mismatches) - len(orders_to_recover)
        print(f"Note: {skipped} mismatched orders were not recovered because they were not")
        print(f"      SELL_LIMIT orders for OPENED transactions")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n✗ Recovery interrupted by user")
    except Exception as e:
        print(f"\n✗ Recovery failed: {e}")
        logger.error("Recovery script failed", exc_info=True)
