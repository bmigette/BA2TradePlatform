"""
Debug script to trace exactly what the widget is calculating and why it differs from broker.
"""

import sys
import os
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, ExpertInstance
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus, OrderDirection, OrderType
from ba2_trade_platform.core.utils import get_account_instance_from_id
from sqlmodel import select

def debug_widget_calculation():
    """Debug the exact widget calculation to see what's different."""
    
    print("="*80)
    print("WIDGET CALCULATION DEBUG")
    print("="*80)
    
    expert_pl = {}
    
    session = get_db()
    try:
        # Get all open transactions with expert attribution (same as widget)
        transactions = session.exec(
            select(Transaction)
            .where(Transaction.expert_id.isnot(None))
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
        ).all()
        
        print(f"\nFound {len(transactions)} transactions with experts\n")
        
        # Group transactions by account to batch price fetching
        account_transactions = {}  # account_id -> [(transaction, expert_name), ...]
        
        for trans in transactions:
            try:
                # Get expert info
                expert = session.get(ExpertInstance, trans.expert_id)
                if not expert:
                    continue
                
                expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                
                # Get account ID from first order
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()
                
                if not first_order or not first_order.account_id:
                    continue
                
                account_id = first_order.account_id
                
                if account_id not in account_transactions:
                    account_transactions[account_id] = []
                account_transactions[account_id].append((trans, expert_name))
                
            except Exception as e:
                print(f"Error grouping transaction {trans.id}: {e}")
                continue
        
        # Fetch prices in bulk per account and calculate P/L
        for account_id, trans_list in account_transactions.items():
            try:
                # Get account interface once
                account = get_account_instance_from_id(account_id, session=session)
                if not account:
                    continue
                
                # Collect all unique symbols for this account
                symbols = list(set(trans.symbol for trans, _ in trans_list))
                
                # Fetch all prices at once (single API call)
                prices = account.get_instrument_current_price(symbols)
                
                # Calculate P/L for each transaction using fetched prices
                for trans, expert_name in trans_list:
                    print(f"\n{'='*60}")
                    print(f"Transaction {trans.id}: {trans.symbol} (Expert: {expert_name})")
                    print(f"  Transaction qty: {trans.quantity}")
                    print(f"  Transaction open_price: ${trans.open_price:.2f}")
                    
                    current_price = prices.get(trans.symbol) if prices else None
                    
                    if not current_price:
                        print(f"  ⚠️ No current price available")
                        continue
                    
                    print(f"  Current price: ${current_price:.2f}")
                    
                    # Get all orders for this transaction
                    all_orders = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                    ).all()
                    
                    if not all_orders:
                        print(f"  ⚠️ No orders found")
                        continue
                    
                    print(f"  Total orders: {len(all_orders)}")
                    
                    # Determine position direction from first order
                    first_order = min(all_orders, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
                    position_direction = first_order.side
                    print(f"  Position direction: {position_direction}")
                    
                    # Filter for market entry orders (exclude TP/SL limit orders)
                    market_entry_orders = [
                        order for order in all_orders
                        if order.side == position_direction
                        and order.order_type in [OrderType.MARKET, OrderType.BUY_STOP, OrderType.SELL_STOP]
                    ]
                    
                    print(f"  Market entry orders: {len(market_entry_orders)}")
                    
                    # Calculate P/L from FILLED market entry orders only
                    filled_entry_orders = [o for o in market_entry_orders if o.status in OrderStatus.get_executed_statuses()]
                    
                    print(f"  Filled entry orders: {len(filled_entry_orders)}")
                    
                    total_cost = 0.0
                    filled_qty = 0.0
                    
                    for order in filled_entry_orders:
                        if not order.open_price or not order.filled_qty:
                            print(f"    Order {order.id}: SKIPPED (missing price or qty)")
                            continue
                        
                        print(f"    Order {order.id}: {order.side} {order.filled_qty:.2f} @ ${order.open_price:.2f} = ${order.filled_qty * order.open_price:.2f}")
                        
                        # All entry orders are same direction, so just sum
                        total_cost += order.filled_qty * order.open_price
                        filled_qty += order.filled_qty
                    
                    if abs(filled_qty) < 0.01:
                        print(f"  ⚠️ No filled position yet")
                        continue
                    
                    # Calculate weighted average price
                    avg_price = total_cost / filled_qty
                    print(f"  Weighted avg price: ${avg_price:.2f} (cost ${total_cost:.2f} / qty {filled_qty:.2f})")
                    
                    # Calculate P/L: (current_price - avg_price) * filled_quantity
                    pl = (current_price - avg_price) * filled_qty
                    print(f"  P/L calculation: (${current_price:.2f} - ${avg_price:.2f}) × {filled_qty:.2f} = ${pl:.2f}")
                    
                    if position_direction == OrderDirection.SELL:
                        pl = -pl
                        print(f"  SHORT position - inverted P/L: ${pl:.2f}")
                    
                    if expert_name not in expert_pl:
                        expert_pl[expert_name] = 0.0
                    expert_pl[expert_name] += pl
                    
                    print(f"  ✅ Expert '{expert_name}' running total: ${expert_pl[expert_name]:.2f}")
                    
            except Exception as e:
                print(f"Error calculating P/L for account {account_id}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
    finally:
        session.close()
    
    print(f"\n{'='*80}")
    print("FINAL EXPERT P/L TOTALS")
    print(f"{'='*80}")
    
    if expert_pl:
        for expert_name, pl in sorted(expert_pl.items(), key=lambda x: x[1], reverse=True):
            pl_sign = "+" if pl >= 0 else ""
            print(f"  {expert_name}: {pl_sign}${pl:.2f}")
        
        total = sum(expert_pl.values())
        print(f"\n  TOTAL: ${total:.2f}")
    else:
        print("  No P/L calculated")
    
    return expert_pl

if __name__ == "__main__":
    debug_widget_calculation()
