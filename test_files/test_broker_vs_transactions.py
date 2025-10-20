"""
Compare broker positions vs our expert transactions to understand the discrepancy.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder, AccountDefinition
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus, OrderDirection, OrderType
from ba2_trade_platform.core.utils import get_account_instance_from_id
from sqlmodel import select
from datetime import datetime, timezone
from collections import defaultdict

def compare_broker_vs_transactions():
    """Compare broker positions with our transaction records."""
    
    print("="*80)
    print("BROKER POSITIONS VS TRANSACTION RECORDS")
    print("="*80)
    
    session = get_db()
    try:
        # Get all accounts
        accounts = session.exec(select(AccountDefinition)).all()
        
        for account_def in accounts:
            print(f"\n{'='*80}")
            print(f"ACCOUNT: {account_def.name} (ID: {account_def.id})")
            print(f"{'='*80}")
            
            # Get account interface
            account = get_account_instance_from_id(account_def.id, session=session)
            if not account:
                print("  ⚠️ Could not get account interface")
                continue
            
            # Get broker positions
            broker_positions = account.get_positions()
            if not broker_positions:
                print("  No broker positions")
                continue
            
            print(f"\n  Broker has {len(broker_positions)} positions")
            
            # Get our transactions for this account
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            ).all()
            
            # Filter transactions by account (need to check first order)
            account_transactions = []
            for trans in transactions:
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()
                
                if first_order and first_order.account_id == account_def.id:
                    account_transactions.append(trans)
            
            print(f"  We have {len(account_transactions)} transactions")
            print(f"    - With expert: {sum(1 for t in account_transactions if t.expert_id)}")
            print(f"    - Without expert: {sum(1 for t in account_transactions if not t.expert_id)}")
            
            # Group by symbol
            broker_by_symbol = {}
            for pos in broker_positions:
                symbol = pos.symbol
                qty = float(pos.qty)
                avg_price = float(pos.avg_entry_price)
                current_price = float(pos.current_price)
                unrealized_pl = float(pos.unrealized_pl)
                
                broker_by_symbol[symbol] = {
                    'qty': qty,
                    'avg_price': avg_price,
                    'current_price': current_price,
                    'unrealized_pl': unrealized_pl
                }
            
            # Group our transactions by symbol
            our_by_symbol = defaultdict(list)
            for trans in account_transactions:
                our_by_symbol[trans.symbol].append(trans)
            
            # Compare
            print(f"\n  {'Symbol':<8} {'Broker Qty':>12} {'Our Trans':>12} {'Match':>8}")
            print(f"  {'-'*50}")
            
            all_symbols = sorted(set(list(broker_by_symbol.keys()) + list(our_by_symbol.keys())))
            
            for symbol in all_symbols:
                broker_qty = broker_by_symbol.get(symbol, {}).get('qty', 0)
                our_trans_count = len(our_by_symbol.get(symbol, []))
                our_total_qty = sum(t.quantity for t in our_by_symbol.get(symbol, []))
                
                match = "✓" if abs(broker_qty - our_total_qty) < 0.01 else "✗"
                
                print(f"  {symbol:<8} {broker_qty:>12.2f} {our_trans_count:>8} txn ({our_total_qty:>6.2f}) {match:>4}")
            
            # Summary
            print(f"\n  BROKER TOTAL P/L: ${sum(b['unrealized_pl'] for b in broker_by_symbol.values()):,.2f}")
            
            # Calculate our P/L from transactions with experts
            expert_pl = 0.0
            for trans in account_transactions:
                if not trans.expert_id:
                    continue
                
                # Get all orders
                all_orders = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                ).all()
                
                if not all_orders:
                    continue
                
                # Get current price
                current_price = broker_by_symbol.get(trans.symbol, {}).get('current_price')
                if not current_price:
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
                
                avg_price = total_cost / filled_qty
                pl = (current_price - avg_price) * filled_qty
                if position_direction == OrderDirection.SELL:
                    pl = -pl
                
                expert_pl += pl
            
            print(f"  OUR EXPERT P/L:   ${expert_pl:,.2f}")
            print(f"  DIFFERENCE:       ${broker_by_symbol and (sum(b['unrealized_pl'] for b in broker_by_symbol.values()) - expert_pl):,.2f}")
            
    finally:
        session.close()

if __name__ == "__main__":
    compare_broker_vs_transactions()
