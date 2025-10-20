"""
Test the updated widget calculation to verify it matches broker P/L.
This simulates exactly what the widget does now.
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

def test_widget_calculation_with_broker_prices():
    """Test widget calculation using broker's current_price (same as updated widget)."""
    
    print("="*80)
    print("TESTING UPDATED WIDGET CALCULATION (Using Broker Prices)")
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
        account_transactions = {}
        
        for trans in transactions:
            try:
                expert = session.get(ExpertInstance, trans.expert_id)
                if not expert:
                    continue
                
                expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                
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
        
        # Fetch prices from broker positions (NEW APPROACH - same as updated widget)
        for account_id, trans_list in account_transactions.items():
            try:
                account = get_account_instance_from_id(account_id, session=session)
                if not account:
                    continue
                
                # Get broker positions to use their current_price
                print(f"Getting broker positions for account {account_id}...")
                broker_positions = account.get_positions()
                prices = {}
                if broker_positions:
                    for pos in broker_positions:
                        pos_dict = pos if isinstance(pos, dict) else dict(pos)
                        prices[pos_dict['symbol']] = float(pos_dict['current_price'])
                
                print(f"Fetched prices for {len(prices)} symbols from broker positions\n")
                
                # Calculate P/L for each transaction using broker prices
                for trans, expert_name in trans_list:
                    current_price = prices.get(trans.symbol)
                    
                    if not current_price:
                        continue
                    
                    # Get all orders
                    all_orders = session.exec(
                        select(TradingOrder)
                        .where(TradingOrder.transaction_id == trans.id)
                    ).all()
                    
                    if not all_orders:
                        continue
                    
                    # Determine position direction
                    first_order = min(all_orders, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
                    position_direction = first_order.side
                    
                    # Filter for market entry orders
                    market_entry_orders = [
                        order for order in all_orders
                        if order.side == position_direction
                        and order.order_type in [OrderType.MARKET, OrderType.BUY_STOP, OrderType.SELL_STOP]
                    ]
                    
                    # Calculate from filled orders
                    filled_entry_orders = [o for o in market_entry_orders if o.status in OrderStatus.get_executed_statuses()]
                    
                    total_cost = 0.0
                    filled_qty = 0.0
                    
                    for order in filled_entry_orders:
                        if not order.open_price or not order.filled_qty:
                            continue
                        total_cost += order.filled_qty * order.open_price
                        filled_qty += order.filled_qty
                    
                    if abs(filled_qty) < 0.01:
                        continue
                    
                    # Calculate weighted average price
                    avg_price = total_cost / filled_qty
                    
                    # Calculate P/L
                    pl = (current_price - avg_price) * filled_qty
                    if position_direction == OrderDirection.SELL:
                        pl = -pl
                    
                    if expert_name not in expert_pl:
                        expert_pl[expert_name] = 0.0
                    expert_pl[expert_name] += pl
                    
            except Exception as e:
                print(f"Error calculating P/L for account {account_id}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
    finally:
        session.close()
    
    print(f"\n{'='*80}")
    print("FINAL EXPERT P/L TOTALS (Using Broker Prices)")
    print(f"{'='*80}")
    
    if expert_pl:
        for expert_name, pl in sorted(expert_pl.items(), key=lambda x: x[1], reverse=True):
            pl_sign = "+" if pl >= 0 else ""
            print(f"  {expert_name}: {pl_sign}${pl:.2f}")
        
        total = sum(expert_pl.values())
        print(f"\n  TOTAL: ${total:.2f}")
        
        # Compare with broker
        session = get_db()
        try:
            account = get_account_instance_from_id(1, session=session)
            broker_positions = account.get_positions()
            broker_total = sum(float(pos.unrealized_pl if hasattr(pos, 'unrealized_pl') else pos['unrealized_pl']) for pos in broker_positions)
            print(f"  BROKER TOTAL: ${broker_total:.2f}")
            print(f"  MATCH: {'✅ YES' if abs(total - broker_total) < 0.5 else '❌ NO'}")
        finally:
            session.close()
    else:
        print("  No P/L calculated")
    
    return expert_pl

if __name__ == "__main__":
    test_widget_calculation_with_broker_prices()
