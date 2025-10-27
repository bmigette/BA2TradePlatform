"""Check transaction 248 details."""
import sys
sys.path.insert(0, 'c:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Transaction, TradingOrder
from sqlmodel import select

with get_db() as session:
    trans = session.exec(select(Transaction).where(Transaction.id == 248)).first()
    
    if trans:
        print(f"\nTransaction 248:")
        print(f"  Symbol: {trans.symbol}")
        print(f"  Status: {trans.status}")
        print(f"  Expert ID: {trans.expert_id}")
        print(f"  Open Price: {trans.open_price}")
        print(f"  Close Price: {trans.close_price}")
        print(f"  Created: {trans.created_at}")
        
        # Get current open quantity
        qty = trans.get_current_open_qty()
        print(f"  Current Open Qty: {qty}")
        
        # Check orders
        print(f"\n  Trading Orders ({len(trans.trading_orders)}):")
        for order in sorted(trans.trading_orders, key=lambda o: o.created_at):
            print(f"    Order {order.id}: {order.order_type} {order.side} quantity={order.quantity} filled={order.filled_qty} status={order.status}")
            print(f"      Created: {order.created_at}")
        
        # Check direction
        if trans.trading_orders:
            first_order = sorted(trans.trading_orders, key=lambda o: o.created_at)[0]
            direction = first_order.side
            print(f"\n  Direction (from first order): {direction}")
    else:
        print("Transaction 248 not found")
