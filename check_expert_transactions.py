"""
Diagnostic script to check transactions and orders for a specific expert.
"""

import argparse
import sys
import os

# Parse command-line arguments
parser = argparse.ArgumentParser(description="Check transactions for a specific expert")
parser.add_argument("--expert-id", type=int, required=True, help="Expert instance ID to check")
parser.add_argument("--db-file", type=str, help="Path to custom database file")
args = parser.parse_args()

# Set custom database path if provided
if args.db_file:
    if not os.path.exists(args.db_file):
        print(f"❌ Database file not found: {args.db_file}")
        sys.exit(1)
    import ba2_trade_platform.config as config
    config.DB_FILE = args.db_file
    print(f"Using custom database: {args.db_file}")

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, ExpertInstance
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus
from sqlmodel import select

def check_expert_transactions(expert_id: int):
    """Check all transactions for the given expert."""
    
    session = get_db()
    
    # Get expert info
    expert = session.get(ExpertInstance, expert_id)
    if not expert:
        print(f"❌ Expert {expert_id} not found!")
        return
    
    expert_name = f"{expert.alias or expert.expert}-{expert.id}"
    print(f"\n{'='*80}")
    print(f"Expert: {expert_name}")
    print(f"{'='*80}\n")
    
    # Get all transactions for this expert
    transactions = session.exec(
        select(Transaction)
        .where(Transaction.expert_id == expert_id)
    ).all()
    
    print(f"Total transactions: {len(transactions)}")
    
    for trans in transactions:
        print(f"\n{'─'*80}")
        print(f"Transaction #{trans.id}")
        print(f"  Symbol: {trans.symbol}")
        print(f"  Quantity: {trans.quantity}")
        print(f"  Side: {trans.side}")
        print(f"  Status: {trans.status.value}")
        print(f"  Open Price: {trans.open_price}")
        print(f"  Close Price: {trans.close_price}")
        print(f"  Created: {trans.created_at}")
        
        # Get orders for this transaction
        orders = session.exec(
            select(TradingOrder)
            .where(TradingOrder.transaction_id == trans.id)
        ).all()
        
        print(f"\n  Orders ({len(orders)}):")
        
        total_buy_qty = 0.0
        total_sell_qty = 0.0
        filled_orders = []
        
        for order in orders:
            status_icon = "✓" if order.status in OrderStatus.get_executed_statuses() else "○"
            print(f"    {status_icon} Order #{order.id}")
            print(f"       Side: {order.side}, Type: {order.order_type.value}")
            print(f"       Quantity: {order.quantity}, Filled: {order.filled_qty}")
            print(f"       Status: {order.status.value}")
            print(f"       Open Price: {order.open_price}")
            
            # Track filled quantities
            if order.status in OrderStatus.get_executed_statuses() and order.filled_qty and order.filled_qty > 0:
                filled_orders.append(order)
                if order.side.value == "BUY":
                    total_buy_qty += order.filled_qty
                elif order.side.value == "SELL":
                    total_sell_qty += order.filled_qty
        
        # Calculate net position
        net_filled_qty = total_buy_qty - total_sell_qty
        
        print(f"\n  Position Summary:")
        print(f"    Total Buy Qty: {total_buy_qty:.2f}")
        print(f"    Total Sell Qty: {total_sell_qty:.2f}")
        print(f"    Net Filled Qty: {net_filled_qty:.2f}")
        print(f"    Transaction Qty: {trans.quantity:.2f}")
        print(f"    Difference: {abs(net_filled_qty) - trans.quantity:.2f}")
        
        if abs(net_filled_qty) < 0.01:
            print(f"    ⚠️  NO NET POSITION - Widget will skip this!")
        
        if trans.quantity < 0:
            print(f"    ⚠️  NEGATIVE QUANTITY - Needs migration!")
    
    session.close()

if __name__ == "__main__":
    try:
        check_expert_transactions(args.expert_id)
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
