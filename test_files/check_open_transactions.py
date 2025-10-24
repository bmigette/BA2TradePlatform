"""
Check open transactions for canceled TP/SL orders.

This script finds all open transactions and checks if they have any canceled TP or SL orders.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderType, OrderStatus
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.logger import logger
from sqlmodel import Session, select

def check_open_transactions():
    """Check all open transactions for canceled TP/SL orders."""
    
    print("="*80)
    print("  Open Transactions with Canceled TP/SL Orders")
    print("="*80)
    print()
    
    with Session(get_db().bind) as session:
        # Find all open transactions
        open_transactions = session.exec(
            select(Transaction).where(
                Transaction.close_date == None
            ).order_by(Transaction.id)
        ).all()
        
        if not open_transactions:
            print("✓ No open transactions found")
            return
        
        print(f"Found {len(open_transactions)} open transactions")
        print()
        
        transactions_with_canceled_orders = []
        
        for transaction in open_transactions:
            # Get all orders for this transaction
            orders = session.exec(
                select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction.id
                ).order_by(TradingOrder.id)
            ).all()
            
            # Check for canceled TP/SL orders
            canceled_tp_orders = []
            canceled_sl_orders = []
            active_tp_orders = []
            active_sl_orders = []
            
            for order in orders:
                # Identify TP orders (SELL_LIMIT or limit_price set)
                is_tp = order.order_type in [OrderType.SELL_LIMIT, OrderType.BUY_LIMIT] or order.limit_price is not None
                # Identify SL orders (SELL_STOP, BUY_STOP or stop_price set)
                is_sl = order.order_type in [OrderType.SELL_STOP, OrderType.BUY_STOP] or (order.stop_price is not None and order.limit_price is None)
                
                if order.status == OrderStatus.CANCELED:
                    if is_tp and not is_sl:
                        canceled_tp_orders.append(order)
                    elif is_sl and not is_tp:
                        canceled_sl_orders.append(order)
                elif order.status not in [OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.ERROR]:
                    if is_tp and not is_sl:
                        active_tp_orders.append(order)
                    elif is_sl and not is_tp:
                        active_sl_orders.append(order)
            
            if canceled_tp_orders or canceled_sl_orders:
                transactions_with_canceled_orders.append({
                    'transaction': transaction,
                    'canceled_tp': canceled_tp_orders,
                    'canceled_sl': canceled_sl_orders,
                    'active_tp': active_tp_orders,
                    'active_sl': active_sl_orders,
                    'all_orders': orders
                })
        
        if not transactions_with_canceled_orders:
            print("✓ No open transactions with canceled TP/SL orders")
            return
        
        print(f"Found {len(transactions_with_canceled_orders)} transactions with canceled orders:")
        print()
        
        for item in transactions_with_canceled_orders:
            transaction = item['transaction']
            print(f"Transaction {transaction.id}: {transaction.symbol}")
            print(f"  Open date: {transaction.open_date}")
            print(f"  Take profit: ${transaction.take_profit:.2f}" if transaction.take_profit else "  Take profit: None")
            print(f"  Stop loss: ${transaction.stop_loss:.2f}" if transaction.stop_loss else "  Stop loss: None")
            
            if item['canceled_tp']:
                print(f"  ❌ Canceled TP orders: {len(item['canceled_tp'])}")
                for order in item['canceled_tp']:
                    print(f"     - Order {order.id}: {order.order_type.value} @ ${order.limit_price:.2f if order.limit_price else 0:.2f}")
            
            if item['canceled_sl']:
                print(f"  ❌ Canceled SL orders: {len(item['canceled_sl'])}")
                for order in item['canceled_sl']:
                    print(f"     - Order {order.id}: {order.order_type.value} @ ${order.stop_price:.2f if order.stop_price else 0:.2f}")
            
            if item['active_tp']:
                print(f"  ✓ Active TP orders: {len(item['active_tp'])}")
                for order in item['active_tp']:
                    print(f"     - Order {order.id}: {order.order_type.value} @ ${order.limit_price:.2f if order.limit_price else 0:.2f} (status: {order.status.value})")
            
            if item['active_sl']:
                print(f"  ✓ Active SL orders: {len(item['active_sl'])}")
                for order in item['active_sl']:
                    print(f"     - Order {order.id}: {order.order_type.value} @ ${order.stop_price:.2f if order.stop_price else 0:.2f} (status: {order.status.value})")
            
            print(f"  Total orders: {len(item['all_orders'])}")
            print()

if __name__ == "__main__":
    try:
        check_open_transactions()
    except KeyboardInterrupt:
        print("\n✗ Check interrupted by user")
    except Exception as e:
        print(f"\n✗ Check failed: {e}")
        logger.error("Check open transactions failed", exc_info=True)
