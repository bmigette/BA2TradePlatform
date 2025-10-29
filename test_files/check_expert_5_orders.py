"""Check what orders expert 5 has and why it shows $2000 pending."""

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import TradingOrder, Transaction, ExpertInstance
from ba2_trade_platform.core.types import OrderStatus, OrderType, TransactionStatus
from sqlmodel import select

def main():
    with get_db() as session:
        # Get expert instance 5
        expert = session.get(ExpertInstance, 5)
        if not expert:
            print("Expert instance 5 not found")
            return
        
        print(f"Expert 5: {expert.alias or expert.expert}")
        print(f"Enabled: {expert.enabled}")
        print()
        
        # Get all transactions for expert 5
        transactions = session.exec(
            select(Transaction)
            .where(Transaction.expert_id == 5)
            .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
        ).all()
        
        print(f"Found {len(transactions)} active transactions for expert 5:")
        print()
        
        total_pending = 0.0
        total_filled = 0.0
        
        for transaction in transactions:
            print(f"Transaction {transaction.id}: {transaction.symbol}")
            print(f"  Status: {transaction.status}")
            
            # Get all orders for this transaction
            orders = session.exec(
                select(TradingOrder)
                .where(TradingOrder.transaction_id == transaction.id)
            ).all()
            
            print(f"  Orders ({len(orders)}):")
            for order in orders:
                print(f"    - Order {order.id}:")
                print(f"      Type: {order.order_type}")
                print(f"      Status: {order.status}")
                print(f"      Quantity: {order.quantity}")
                print(f"      Price: {order.limit_price}")
                print(f"      Value: ${(order.limit_price or 0) * order.quantity:.2f}")
                
                # Count towards pending or filled
                if order.status in [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]:
                    if order.order_type == OrderType.MARKET:
                        total_pending += (order.limit_price or 0) * order.quantity
                        print(f"      >>> COUNTED AS PENDING: ${(order.limit_price or 0) * order.quantity:.2f}")
                    else:
                        print(f"      >>> EXCLUDED (not MARKET type)")
                elif order.status in [OrderStatus.FILLED, OrderStatus.NEW, OrderStatus.OPEN, OrderStatus.ACCEPTED]:
                    if order.order_type == OrderType.MARKET:
                        total_filled += (order.limit_price or 0) * order.quantity
                        print(f"      >>> COUNTED AS FILLED: ${(order.limit_price or 0) * order.quantity:.2f}")
                    else:
                        print(f"      >>> EXCLUDED (not MARKET type)")
            
            # Calculate equity from transaction methods
            try:
                from ba2_trade_platform.core.utils import get_account_instance_from_id
                first_order = orders[0] if orders else None
                account_interface = None
                if first_order and first_order.account_id:
                    account_interface = get_account_instance_from_id(first_order.account_id, session=session)
                
                pending_equity = transaction.get_pending_open_equity(account_interface)
                filled_equity = transaction.get_current_open_equity(account_interface)
                
                print(f"  Transaction equity calculation:")
                print(f"    Pending equity: ${pending_equity:.2f}")
                print(f"    Filled equity: ${filled_equity:.2f}")
            except Exception as e:
                print(f"  Error calculating equity: {e}")
            
            print()
        
        print("="*80)
        print(f"TOTALS:")
        print(f"  Total pending (from MARKET orders): ${total_pending:.2f}")
        print(f"  Total filled (from MARKET orders): ${total_filled:.2f}")
        print(f"  Grand total: ${total_pending + total_filled:.2f}")

if __name__ == "__main__":
    main()
