"""
Validate Canceled Orders for Opened Transactions

This script checks all orders marked as CANCELED in the database for currently OPENED transactions
and verifies if they're actually canceled at the broker, or if they have a different status
(like FILLED) that should update the transaction.
"""
import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.db import get_db, update_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus
from ba2_trade_platform.modules.accounts import AlpacaAccount
from sqlmodel import Session, select
from collections import defaultdict

print("=" * 100)
print("VALIDATING CANCELED ORDERS FOR OPENED TRANSACTIONS")
print("=" * 100)
print()

# Get all opened transactions with their canceled orders
with Session(get_db().bind) as session:
    opened_transactions = session.exec(
        select(Transaction).where(Transaction.status == TransactionStatus.OPENED)
    ).all()
    
    print(f"Found {len(opened_transactions)} OPENED transactions")
    print()
    
    # Group by account
    account_orders = defaultdict(list)
    
    for txn in opened_transactions:
        # Get all CANCELED orders for this transaction
        canceled_orders = session.exec(
            select(TradingOrder).where(
                TradingOrder.transaction_id == txn.id,
                TradingOrder.status == OrderStatus.CANCELED,
                TradingOrder.broker_order_id.is_not(None)
            )
        ).all()
        
        if canceled_orders:
            for order in canceled_orders:
                account_orders[order.account_id].append({
                    'transaction_id': txn.id,
                    'transaction_symbol': txn.symbol,
                    'order_id': order.id,
                    'broker_order_id': order.broker_order_id,
                    'order_type': order.order_type,
                    'side': order.side,
                    'quantity': order.quantity,
                    'comment': order.comment
                })

if not account_orders:
    print("✓ No CANCELED orders found for OPENED transactions")
    sys.exit(0)

print(f"Found CANCELED orders across {len(account_orders)} account(s)")
print()

# Check each account's orders
total_checked = 0
total_mismatches = 0
total_errors = 0
updates_needed = []

for account_id, orders in account_orders.items():
    print("=" * 100)
    print(f"ACCOUNT {account_id}: Checking {len(orders)} canceled orders")
    print("=" * 100)
    print()
    
    try:
        account = AlpacaAccount(account_id)
    except Exception as e:
        print(f"✗ ERROR: Could not initialize account {account_id}: {e}")
        total_errors += len(orders)
        continue
    
    for order_info in orders:
        total_checked += 1
        broker_order_id = order_info['broker_order_id']
        db_order_id = order_info['order_id']
        
        print(f"Order {db_order_id} (Transaction {order_info['transaction_id']} - {order_info['transaction_symbol']})")
        print(f"  DB Status: CANCELED")
        print(f"  Broker Order ID: {broker_order_id}")
        print(f"  Type: {order_info['order_type']}, Side: {order_info['side']}, Qty: {order_info['quantity']}")
        
        try:
            # Check broker status
            broker_order = account.get_order(broker_order_id)
            
            if not broker_order:
                print(f"  ✓ Broker Status: NOT FOUND (truly canceled/expired)")
                print()
                continue
            
            broker_status = broker_order.status
            print(f"  Broker Status: {broker_status}")
            
            # Check if statuses match
            if broker_status == OrderStatus.CANCELED:
                print(f"  ✓ Status matches - order is truly CANCELED")
            else:
                print(f"  ✗ MISMATCH! Broker says {broker_status}, DB says CANCELED")
                total_mismatches += 1
                
                # Collect update information
                update_info = {
                    'order_id': db_order_id,
                    'transaction_id': order_info['transaction_id'],
                    'symbol': order_info['transaction_symbol'],
                    'broker_order_id': broker_order_id,
                    'db_status': 'CANCELED',
                    'broker_status': broker_status,
                    'filled_qty': broker_order.filled_qty if hasattr(broker_order, 'filled_qty') else None,
                    'open_price': broker_order.open_price if hasattr(broker_order, 'open_price') else None
                }
                updates_needed.append(update_info)
                
                # Show additional details for mismatches
                if broker_status == OrderStatus.FILLED:
                    print(f"    → Filled Qty: {broker_order.filled_qty}")
                    if hasattr(broker_order, 'open_price') and broker_order.open_price:
                        print(f"    → Fill Price: {broker_order.open_price}")
                    print(f"    ⚠ This order should trigger transaction closure!")
                elif broker_status in [OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED, OrderStatus.NEW]:
                    print(f"    → Order is still active/pending at broker")
                elif broker_status == OrderStatus.PARTIALLY_FILLED:
                    print(f"    → Partially filled: {broker_order.filled_qty}")
                    
        except Exception as e:
            print(f"  ✗ ERROR checking broker: {e}")
            total_errors += 1
        
        print()

# Summary
print("=" * 100)
print("VALIDATION SUMMARY")
print("=" * 100)
print(f"Total orders checked: {total_checked}")
print(f"Mismatches found: {total_mismatches}")
print(f"Errors encountered: {total_errors}")
print(f"Correct (matched): {total_checked - total_mismatches - total_errors}")
print()

if updates_needed:
    print("=" * 100)
    print("ORDERS REQUIRING UPDATE")
    print("=" * 100)
    print()
    
    for update in updates_needed:
        print(f"Order {update['order_id']} (Transaction {update['transaction_id']} - {update['symbol']})")
        print(f"  Broker Order ID: {update['broker_order_id']}")
        print(f"  DB Status: {update['db_status']} → Should be: {update['broker_status']}")
        if update['filled_qty']:
            print(f"  Filled Qty: {update['filled_qty']}")
        if update['open_price']:
            print(f"  Fill Price: {update['open_price']}")
        print()
    
    print("=" * 100)
    print("RECOMMENDATION")
    print("=" * 100)
    print("Run account.refresh_orders() to sync these orders with broker status.")
    print("The fix implemented should now correctly update these orders instead of marking them as CANCELED.")
    print()
    
    # Ask if user wants to update
    response = input("Would you like to update these orders now? (yes/no): ").strip().lower()
    
    if response in ['yes', 'y']:
        print()
        print("Updating orders...")
        updated_count = 0
        
        for update in updates_needed:
            try:
                order = get_instance(TradingOrder, update['order_id'])
                if order:
                    order.status = update['broker_status']
                    if update['filled_qty'] is not None:
                        order.filled_qty = update['filled_qty']
                    if update['open_price'] is not None:
                        order.open_price = update['open_price']
                    update_instance(order)
                    updated_count += 1
                    print(f"  ✓ Updated order {update['order_id']} to {update['broker_status']}")
            except Exception as e:
                print(f"  ✗ Error updating order {update['order_id']}: {e}")
        
        print()
        print(f"Updated {updated_count} of {len(updates_needed)} orders")
        print("⚠ You may need to run status_update_loop for affected transactions to close them properly.")
else:
    print("✓ All canceled orders are correctly synchronized with broker status!")
