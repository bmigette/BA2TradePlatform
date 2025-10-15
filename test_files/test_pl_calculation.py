"""
P/L Calculation Test Script
Compares broker positions with our order-based calculations to identify discrepancies.
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db, get_all_instances
from ba2_trade_platform.core.models import AccountDefinition, Transaction, TradingOrder
from ba2_trade_platform.core.types import TransactionStatus, OrderStatus, OrderDirection, OrderType
from ba2_trade_platform.core.utils import get_account_instance_from_id
from sqlmodel import select
from datetime import datetime, timezone

def calculate_pl_from_orders(account_id, session):
    """Calculate P/L from our orders (same logic as widgets)."""
    positions = {}  # symbol -> {qty, cost_basis, avg_price, filled_qty}
    
    # Get open transactions
    open_transactions = session.exec(
        select(Transaction)
        .join(TradingOrder, Transaction.id == TradingOrder.transaction_id)
        .where(TradingOrder.account_id == account_id)
        .where(Transaction.status == TransactionStatus.OPENED)
        .distinct()
    ).all()
    
    print(f"\n  Found {len(open_transactions)} open transactions")
    
    for trans in open_transactions:
        # Get all orders for this transaction
        all_orders = session.exec(
            select(TradingOrder)
            .where(TradingOrder.transaction_id == trans.id)
        ).all()
        
        if not all_orders:
            continue
        
        # Determine position direction from first order
        first_order = min(all_orders, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
        position_direction = first_order.side
        
        print(f"\n  Transaction {trans.id}: {trans.symbol}")
        print(f"    Transaction qty: {trans.quantity}, open_price: ${trans.open_price or 0:.2f}")
        print(f"    Position direction: {position_direction.value}")
        
        # Filter for market entry orders
        market_entry_orders = [
            order for order in all_orders
            if order.side == position_direction
            and order.order_type in [OrderType.MARKET, OrderType.BUY_STOP, OrderType.SELL_STOP]
        ]
        
        print(f"    Market entry orders: {len(market_entry_orders)}")
        
        # Calculate from FILLED orders
        filled_entry_orders = [o for o in market_entry_orders if o.status in OrderStatus.get_executed_statuses()]
        
        symbol = trans.symbol
        if symbol not in positions:
            positions[symbol] = {
                'qty': 0.0,
                'cost_basis': 0.0,
                'avg_price': 0.0,
                'filled_qty': 0.0,
                'transaction_ids': set()
            }
        
        for order in filled_entry_orders:
            if not order.open_price or not order.filled_qty:
                print(f"      Order {order.id}: Skipping (no price or qty)")
                continue
            
            cost = order.filled_qty * order.open_price
            positions[symbol]['cost_basis'] += cost
            positions[symbol]['filled_qty'] += order.filled_qty
            positions[symbol]['transaction_ids'].add(trans.id)
            
            print(f"      Order {order.id}: {order.side.value} {order.filled_qty} @ ${order.open_price:.2f} = ${cost:.2f}")
        
        # Validate quantity
        pending_entry_orders = [o for o in market_entry_orders if o.status in [OrderStatus.PENDING, OrderStatus.OPEN]]
        total_order_qty = sum(o.filled_qty or 0 for o in filled_entry_orders) + sum(o.quantity or 0 for o in pending_entry_orders)
        
        if abs(total_order_qty - abs(trans.quantity)) > 0.01:
            print(f"    ⚠️  QUANTITY MISMATCH: transaction={trans.quantity}, orders={total_order_qty:.2f}")
    
    # Calculate averages and net quantities
    for symbol, data in positions.items():
        if abs(data['filled_qty']) > 0.01:
            data['avg_price'] = data['cost_basis'] / data['filled_qty']
            data['qty'] = data['filled_qty']  # For display
    
    return positions

def calculate_broker_pl(account, our_positions):
    """Get broker positions and calculate P/L."""
    broker_positions = account.get_positions()
    broker_data = {}
    
    print(f"\n  Broker has {len(broker_positions)} positions")
    
    for pos in broker_positions:
        pos_dict = pos if isinstance(pos, dict) else dict(pos)
        symbol = pos_dict.get('symbol')
        qty = float(pos_dict.get('qty', 0))
        avg_price = float(pos_dict.get('avg_entry_price', 0))
        current_price = float(pos_dict.get('current_price', 0))
        unrealized_pl = float(pos_dict.get('unrealized_pl', 0))
        
        broker_data[symbol] = {
            'qty': qty,
            'avg_price': avg_price,
            'current_price': current_price,
            'broker_pl': unrealized_pl,
            'cost_basis': qty * avg_price
        }
        
        print(f"\n  {symbol}:")
        print(f"    Broker: {qty:.2f} shares @ ${avg_price:.2f} avg, current ${current_price:.2f}")
        print(f"    Broker P/L: ${unrealized_pl:.2f}")
    
    return broker_data

def compare_and_calculate_pl():
    """Main comparison function."""
    session = get_db()
    
    try:
        accounts = get_all_instances(AccountDefinition)
        
        print("=" * 80)
        print("P/L CALCULATION COMPARISON TEST")
        print("=" * 80)
        
        total_broker_pl = 0.0
        total_calculated_pl = 0.0
        all_discrepancies = []
        
        for account_def in accounts:
            print(f"\n{'='*80}")
            print(f"ACCOUNT: {account_def.name} (ID: {account_def.id})")
            print(f"{'='*80}")
            
            # Get account interface
            account = get_account_instance_from_id(account_def.id, session=session)
            if not account:
                print(f"  ❌ Could not get account interface")
                continue
            
            # Calculate from our orders
            print("\n📊 CALCULATING FROM OUR ORDERS:")
            our_positions = calculate_pl_from_orders(account_def.id, session)
            
            # Get broker positions
            print("\n📈 GETTING BROKER POSITIONS:")
            broker_data = calculate_broker_pl(account, our_positions)
            
            # Compare and calculate P/L
            print("\n🔍 COMPARISON & P/L CALCULATION:")
            print("-" * 80)
            
            account_broker_pl = 0.0
            account_calculated_pl = 0.0
            
            # Check all symbols (union of both our positions and broker positions)
            all_symbols = set(our_positions.keys()) | set(broker_data.keys())
            
            for symbol in sorted(all_symbols):
                print(f"\n  {symbol}:")
                
                our_data = our_positions.get(symbol, {'qty': 0, 'avg_price': 0, 'cost_basis': 0, 'filled_qty': 0})
                broker_info = broker_data.get(symbol, {'qty': 0, 'avg_price': 0, 'current_price': 0, 'broker_pl': 0, 'cost_basis': 0})
                
                our_qty = our_data['filled_qty']
                our_avg = our_data['avg_price']
                our_cost = our_data['cost_basis']
                
                broker_qty = broker_info['qty']
                broker_avg = broker_info['avg_price']
                broker_cost = broker_info['cost_basis']
                current_price = broker_info['current_price']
                broker_pl = broker_info['broker_pl']
                
                print(f"    Our Data:")
                print(f"      Quantity: {our_qty:.2f}")
                print(f"      Avg Price: ${our_avg:.2f}")
                print(f"      Cost Basis: ${our_cost:.2f}")
                
                print(f"    Broker Data:")
                print(f"      Quantity: {broker_qty:.2f}")
                print(f"      Avg Price: ${broker_avg:.2f}")
                print(f"      Cost Basis: ${broker_cost:.2f}")
                print(f"      Current Price: ${current_price:.2f}")
                print(f"      Broker P/L: ${broker_pl:.2f}")
                
                # Calculate our P/L if we have data
                our_pl = 0.0
                if our_qty > 0.01 and current_price > 0:
                    our_pl = (current_price - our_avg) * our_qty
                    print(f"    Our Calculated P/L: ${our_pl:.2f}")
                    account_calculated_pl += our_pl
                
                account_broker_pl += broker_pl
                
                # Check for discrepancies
                qty_match = abs(our_qty - broker_qty) < 0.01
                cost_match = abs(our_cost - broker_cost) / max(broker_cost, 1) * 100 < 5 if broker_cost > 0 else our_cost < 0.01
                pl_match = abs(our_pl - broker_pl) / max(abs(broker_pl), 1) * 100 < 5 if abs(broker_pl) > 0.01 else abs(our_pl) < 0.01
                
                if not qty_match:
                    print(f"    ⚠️  QTY MISMATCH: {our_qty:.2f} vs {broker_qty:.2f}")
                    all_discrepancies.append(f"{account_def.name}/{symbol}: Qty mismatch")
                
                if not cost_match:
                    pct = abs(our_cost - broker_cost) / max(broker_cost, 1) * 100
                    print(f"    ⚠️  COST BASIS MISMATCH: ${our_cost:.2f} vs ${broker_cost:.2f} ({pct:.1f}%)")
                    all_discrepancies.append(f"{account_def.name}/{symbol}: Cost basis mismatch")
                
                if not pl_match and abs(broker_pl) > 0.01:
                    pct = abs(our_pl - broker_pl) / max(abs(broker_pl), 1) * 100
                    print(f"    ⚠️  P/L MISMATCH: ${our_pl:.2f} vs ${broker_pl:.2f} ({pct:.1f}%)")
                    all_discrepancies.append(f"{account_def.name}/{symbol}: P/L mismatch")
                
                if qty_match and cost_match and pl_match:
                    print(f"    ✅ MATCH (within 5% tolerance)")
            
            # Account summary
            print(f"\n  {'-'*76}")
            print(f"  ACCOUNT TOTALS:")
            print(f"    Broker Total P/L:     ${account_broker_pl:,.2f}")
            print(f"    Calculated Total P/L: ${account_calculated_pl:,.2f}")
            
            diff = account_calculated_pl - account_broker_pl
            diff_pct = abs(diff) / max(abs(account_broker_pl), 1) * 100 if abs(account_broker_pl) > 0.01 else 0
            
            if abs(diff) > 0.01:
                print(f"    Difference:           ${diff:,.2f} ({diff_pct:.1f}%)")
                if diff_pct > 5:
                    print(f"    ⚠️  DIFFERENCE EXCEEDS 5% TOLERANCE")
                else:
                    print(f"    ✅ Within 5% tolerance")
            else:
                print(f"    ✅ PERFECT MATCH")
            
            total_broker_pl += account_broker_pl
            total_calculated_pl += account_calculated_pl
        
        # Grand total summary
        print(f"\n{'='*80}")
        print("GRAND TOTALS (ALL ACCOUNTS)")
        print(f"{'='*80}")
        print(f"  Broker Total P/L:     ${total_broker_pl:,.2f}")
        print(f"  Calculated Total P/L: ${total_calculated_pl:,.2f}")
        
        grand_diff = total_calculated_pl - total_broker_pl
        grand_diff_pct = abs(grand_diff) / max(abs(total_broker_pl), 1) * 100 if abs(total_broker_pl) > 0.01 else 0
        
        if abs(grand_diff) > 0.01:
            print(f"  Difference:           ${grand_diff:,.2f} ({grand_diff_pct:.1f}%)")
            if grand_diff_pct > 5:
                print(f"  ⚠️  DIFFERENCE EXCEEDS 5% TOLERANCE")
            else:
                print(f"  ✅ Within 5% tolerance")
        else:
            print(f"  ✅ PERFECT MATCH")
        
        if all_discrepancies:
            print(f"\n{'='*80}")
            print(f"DISCREPANCIES FOUND ({len(all_discrepancies)}):")
            print(f"{'='*80}")
            for disc in all_discrepancies:
                print(f"  • {disc}")
        else:
            print(f"\n✅ NO DISCREPANCIES FOUND - ALL POSITIONS MATCH!")
        
    finally:
        session.close()

if __name__ == "__main__":
    compare_and_calculate_pl()
