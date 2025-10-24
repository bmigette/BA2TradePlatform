"""
Recovery script to resubmit canceled orders and update their database records.

This script:
1. Finds recently canceled orders in the database
2. Resubmits them to Alpaca broker
3. Updates the existing database records with new broker_order_id and status

Run with: .venv\Scripts\python.exe test_files\recover_canceled_orders.py
"""

import sys
import os
from datetime import datetime, timezone, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderType, OrderStatus
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.db import get_instance, update_instance, get_db
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def recover_canceled_orders():
    """Find and resubmit recently canceled orders."""
    
    print("="*80)
    print("  Order Recovery Script")
    print("="*80)
    print()
    
    # Connect to account
    try:
        account = AlpacaAccount(1)
        print(f"✓ Connected to Alpaca account {account.id}")
        print()
    except Exception as e:
        print(f"✗ Failed to connect to Alpaca account: {e}")
        return
    
    # Find recently canceled or error orders (last 24 hours)
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
    
    with Session(get_db().bind) as session:
        canceled_orders = session.exec(
            select(TradingOrder).where(
                TradingOrder.account_id == account.id,
                TradingOrder.status.in_([OrderStatus.CANCELED, OrderStatus.ERROR]),
                TradingOrder.created_at >= cutoff_time
            ).order_by(TradingOrder.id)
        ).all()
        
        if not canceled_orders:
            print("✓ No recently canceled orders found")
            return
        
        print(f"Found {len(canceled_orders)} recently canceled orders:")
        print()
        
        # Group by transaction for better organization
        transactions = {}
        for order in canceled_orders:
            if order.transaction_id not in transactions:
                transactions[order.transaction_id] = []
            transactions[order.transaction_id].append(order)
        
        # Display orders grouped by transaction
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
        response = input(f"Resubmit these {len(canceled_orders)} orders? (yes/no): ").strip().lower()
        
        if response != 'yes':
            print("✗ Recovery canceled by user")
            return
        
        print()
        print("="*80)
        print("  Resubmitting Orders")
        print("="*80)
        print()
        
        success_count = 0
        error_count = 0
        
        for order in canceled_orders:
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
        print(f"Total orders processed: {len(canceled_orders)}")
        print(f"Successfully resubmitted: {success_count}")
        print(f"Errors: {error_count}")
        print()
        
        if success_count > 0:
            print("✓ Orders have been resubmitted and database records updated")
            print("  Run account.refresh_orders() to sync latest statuses from broker")

if __name__ == "__main__":
    try:
        recover_canceled_orders()
    except KeyboardInterrupt:
        print("\n✗ Recovery interrupted by user")
    except Exception as e:
        print(f"\n✗ Recovery failed: {e}")
        logger.error("Recovery script failed", exc_info=True)
