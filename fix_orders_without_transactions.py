"""
Database migration script to create transactions for existing orders that don't have one.

This script:
1. Finds all orders without a transaction_id
2. Groups orders by symbol and side to create logical transactions
3. Creates Transaction records and links the orders to them
"""

from ba2_trade_platform.core.db import get_db, add_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import TransactionStatus, OrderDirection
from ba2_trade_platform.logger import logger
from sqlmodel import select, Session
from datetime import timezone, datetime


def fix_orders_without_transactions():
    """Find and fix all orders that don't have a transaction_id."""
    
    logger.info("Starting database migration: creating transactions for orders without transaction_id")
    
    with Session(get_db().bind) as session:
        # Find all orders without a transaction_id
        statement = select(TradingOrder).where(TradingOrder.transaction_id.is_(None))
        orders_without_transaction = session.exec(statement).all()
        
        logger.info(f"Found {len(orders_without_transaction)} orders without transaction_id")
        
        if not orders_without_transaction:
            logger.info("No orders need fixing. Migration complete.")
            return
        
        # Group orders by symbol to create logical transactions
        # Each transaction will contain orders for the same symbol
        orders_by_symbol = {}
        for order in orders_without_transaction:
            key = (order.symbol, order.account_id)
            if key not in orders_by_symbol:
                orders_by_symbol[key] = []
            orders_by_symbol[key].append(order)
        
        created_transactions = 0
        linked_orders = 0
        
        # Create transactions for each group
        for (symbol, account_id), orders in orders_by_symbol.items():
            logger.info(f"Processing {len(orders)} orders for symbol {symbol} on account {account_id}")
            
            # Determine transaction details from the first order
            first_order = orders[0]
            
            # Calculate total quantity (positive for buys, negative for sells)
            total_quantity = sum(
                order.quantity if order.side == OrderDirection.BUY else -order.quantity
                for order in orders
            )
            
            # Determine transaction status based on order statuses
            from ba2_trade_platform.core.types import OrderStatus
            all_filled = all(order.status == OrderStatus.FILLED for order in orders)
            any_filled = any(order.status in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED] for order in orders)
            
            if all_filled:
                transaction_status = TransactionStatus.CLOSED
            elif any_filled:
                transaction_status = TransactionStatus.OPENED
            else:
                transaction_status = TransactionStatus.WAITING
            
            # Create the transaction
            transaction = Transaction(
                symbol=symbol,
                quantity=total_quantity,
                open_price=None,  # We don't have historical price data
                close_price=None,
                stop_loss=None,
                take_profit=None,
                open_date=first_order.created_at if any_filled else None,
                close_date=datetime.now(timezone.utc) if all_filled else None,
                status=transaction_status,
                created_at=first_order.created_at or datetime.now(timezone.utc),
                expert_id=None  # We don't know which expert created these old orders
            )
            
            # Add transaction to session
            session.add(transaction)
            session.flush()  # Flush to get the transaction ID
            
            created_transactions += 1
            logger.info(f"Created transaction {transaction.id} for {symbol} with status {transaction_status.value}")
            
            # Link all orders to this transaction
            for order in orders:
                order.transaction_id = transaction.id
                session.add(order)
                linked_orders += 1
                logger.debug(f"Linked order {order.id} to transaction {transaction.id}")
        
        # Commit all changes
        session.commit()
        
        logger.info(f"Migration complete: Created {created_transactions} transactions and linked {linked_orders} orders")
        logger.info(f"Summary:")
        logger.info(f"  - {created_transactions} new transactions created")
        logger.info(f"  - {linked_orders} orders now have transaction_id")
        logger.info(f"  - {len(orders_by_symbol)} symbol groups processed")


if __name__ == "__main__":
    try:
        fix_orders_without_transactions()
        print("\n✅ Migration completed successfully!")
        print("All orders now have transaction_id assigned.")
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        print(f"\n❌ Migration failed: {e}")
        print("Check the logs for more details.")
