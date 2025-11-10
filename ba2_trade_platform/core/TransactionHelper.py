"""
TransactionHelper provides business logic for managing transactions and their related orders.

This class handles:
- Adjusting transaction quantity and syncing all related orders
- Managing TP/SL order creation and updates
- Ensuring entry orders and dependent orders stay in sync

Separates business logic from data models following clean architecture principles.
"""

from typing import Optional, List
from sqlmodel import Session, select

from .models import Transaction, TradingOrder
from .types import OrderStatus
from .db import get_db, update_instance
from ..logger import logger


class TransactionHelper:
    """Helper class for transaction-related business logic."""
    
    @staticmethod
    def adjust_qty(
        transaction: Transaction,
        new_quantity: float,
        session: Optional[Session] = None
    ) -> bool:
        """
        Adjust the quantity of a transaction and automatically update all related orders.
        
        This method handles:
        1. Updating the transaction's quantity
        2. Updating the entry order's quantity
        3. Updating all dependent TP/SL orders' quantities to match new entry quantity
        4. Ensuring WAITING_TRIGGER TP/SL orders don't get submitted with qty=0
        
        This ensures all orders stay in sync and prevents invalid TP/SL orders from being submitted.
        
        Args:
            transaction: The transaction to adjust
            new_quantity: New quantity for the transaction
            session: Optional database session (creates new one if not provided)
        
        Returns:
            bool: True if adjustment succeeded, False otherwise
        """
        close_session = False
        if session is None:
            session = Session(get_db().bind)
            close_session = True
        
        try:
            # Validate new quantity
            if new_quantity is None or new_quantity < 0:
                logger.error(f"Cannot adjust transaction {transaction.id} quantity: invalid value {new_quantity}")
                return False
            
            logger.info(f"Adjusting transaction {transaction.id} quantity from {transaction.quantity} to {new_quantity}")
            
            # Update transaction quantity
            transaction.quantity = new_quantity
            
            # Find all orders for this transaction
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction.id
            )
            orders = session.exec(statement).all()
            
            # Separate into entry orders and dependent orders
            entry_order = None
            dependent_orders = []
            
            for order in orders:
                if order.depends_on_order is None and order.status not in OrderStatus.get_terminal_statuses():
                    # This is an active entry order (no dependencies)
                    entry_order = order
                elif order.depends_on_order is not None:
                    # This is a dependent order (TP/SL)
                    dependent_orders.append(order)
            
            # Update entry order quantity
            if entry_order:
                old_qty = entry_order.quantity
                entry_order.quantity = new_quantity
                session.add(entry_order)
                logger.debug(f"Updated entry order {entry_order.id} quantity from {old_qty} to {new_quantity}")
            
            # Update all dependent orders (TP/SL)
            for dep_order in dependent_orders:
                old_qty = dep_order.quantity
                
                # For WAITING_TRIGGER orders, only set qty once entry order is filled
                if dep_order.status == OrderStatus.WAITING_TRIGGER:
                    if entry_order and entry_order.status in OrderStatus.get_executed_statuses():
                        # Entry is filled, now set qty and mark for submission
                        dep_order.quantity = new_quantity
                        logger.debug(
                            f"Updated dependent order {dep_order.id} (WAITING_TRIGGER) quantity from {old_qty} to {new_quantity}, "
                            f"ready for submission"
                        )
                    else:
                        # Entry still pending, keep qty at current value or 0
                        logger.debug(
                            f"Dependent order {dep_order.id} (WAITING_TRIGGER) keeping qty={dep_order.quantity} "
                            f"(entry not yet filled)"
                        )
                elif dep_order.status == OrderStatus.PENDING:
                    # PENDING dependent orders get the new quantity
                    dep_order.quantity = new_quantity
                    logger.debug(f"Updated dependent order {dep_order.id} (PENDING) quantity from {old_qty} to {new_quantity}")
                else:
                    # Already at broker or terminal - don't change
                    logger.debug(f"Skipped dependent order {dep_order.id} (status: {dep_order.status})")
                
                session.add(dep_order)
            
            # Commit all changes
            session.commit()
            logger.info(
                f"Successfully adjusted transaction {transaction.id}: qty={new_quantity}, "
                f"updated {1 if entry_order else 0} entry order, {len(dependent_orders)} dependent orders"
            )
            return True
            
        except Exception as e:
            logger.error(f"Error adjusting transaction {transaction.id} quantity: {e}", exc_info=True)
            session.rollback()
            return False
        finally:
            if close_session:
                session.close()
    
    @staticmethod
    def get_entry_order(transaction: Transaction, session: Optional[Session] = None) -> Optional[TradingOrder]:
        """
        Get the entry order (MARKET/LIMIT order without dependencies) for a transaction.
        
        Args:
            transaction: The transaction to find entry order for
            session: Optional database session
        
        Returns:
            TradingOrder: The entry order if found, None otherwise
        """
        close_session = False
        if session is None:
            session = Session(get_db().bind)
            close_session = True
        
        try:
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction.id,
                TradingOrder.depends_on_order.is_(None),
                TradingOrder.status.notin_(OrderStatus.get_terminal_statuses())
            )
            entry_order = session.exec(statement).first()
            return entry_order
        finally:
            if close_session:
                session.close()
    
    @staticmethod
    def get_dependent_orders(
        transaction: Transaction,
        session: Optional[Session] = None
    ) -> List[TradingOrder]:
        """
        Get all dependent orders (TP/SL) for a transaction.
        
        Args:
            transaction: The transaction to find dependent orders for
            session: Optional database session
        
        Returns:
            List[TradingOrder]: List of dependent orders
        """
        close_session = False
        if session is None:
            session = Session(get_db().bind)
            close_session = True
        
        try:
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction.id,
                TradingOrder.depends_on_order.isnot(None)
            )
            dependent_orders = session.exec(statement).all()
            return dependent_orders
        finally:
            if close_session:
                session.close()
    
    @staticmethod
    def validate_entry_order_qty(entry_order: Optional[TradingOrder]) -> bool:
        """
        Validate that an entry order has a valid (non-zero, positive) quantity.
        
        Args:
            entry_order: The entry order to validate
        
        Returns:
            bool: True if quantity is valid, False otherwise
        """
        if not entry_order:
            logger.warning("Cannot validate entry order: order is None")
            return False
        
        if entry_order.quantity is None or entry_order.quantity <= 0:
            logger.warning(
                f"Entry order {entry_order.id} has invalid quantity: {entry_order.quantity}"
            )
            return False
        
        return True
