"""
TransactionHelper provides business logic for managing transactions and their related orders.

This class handles:
- Adjusting transaction quantity and syncing all related orders
- Managing TP/SL order creation and updates
- Ensuring entry orders and dependent orders stay in sync
- Partial close and add-to-position with proper TP/SL order sequencing

Separates business logic from data models following clean architecture principles.
"""

from typing import Optional, List, Dict, Any, TYPE_CHECKING
from datetime import datetime, timezone
from sqlmodel import Session, select

from .models import Transaction, TradingOrder
from .types import OrderStatus, OrderDirection, OrderType, TransactionStatus, OrderOpenType
from .db import get_db, update_instance, add_instance, get_instance
from ..logger import logger

if TYPE_CHECKING:
    from .interfaces.AccountInterface import AccountInterface


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

    @staticmethod
    def adjust_quantity_with_tpsl(
        account: "AccountInterface",
        transaction: Transaction,
        qty_change: float,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        expert_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Adjust transaction quantity with proper TP/SL order sequencing.
        
        This method handles Alpaca's "held_for_orders" constraint by creating triggered
        orders in the correct sequence:
        
        **Decreasing qty (partial close)**:
        1. Create pending SELL order (triggered by TP/SL cancellation)
        2. Cancel existing TP/SL orders
        3. Create new TP/SL orders (triggered by close order fill)
        
        **Increasing qty (add-to-position)**:
        1. Create add-to-position BUY order
        2. Cancel existing TP/SL orders  
        3. Create new TP/SL orders (triggered by TP/SL cancellation)
        
        Args:
            account: The AccountInterface instance to use for order operations
            transaction: The transaction to adjust
            qty_change: Quantity change (positive to add, negative to reduce)
            tp_price: Optional new take-profit price
            sl_price: Optional new stop-loss price
            expert_id: Optional expert ID for logging
        
        Returns:
            Dict with keys:
                - success: bool
                - message: str
                - orders_created: List of order IDs created
                - orders_canceled: List of order IDs canceled
        """
        result = {
            "success": False,
            "message": "",
            "orders_created": [],
            "orders_canceled": []
        }
        
        try:
            # Validate inputs
            if qty_change == 0:
                result["message"] = "Quantity change is zero, nothing to adjust"
                result["success"] = True
                return result
            
            # Get current transaction state
            current_qty = transaction.quantity or 0
            new_qty = current_qty + qty_change
            
            if new_qty < 0:
                result["message"] = f"Cannot reduce quantity by {abs(qty_change)}, current quantity is {current_qty}"
                return result
            
            # Get the instrument symbol
            symbol = transaction.symbol
            if not symbol:
                result["message"] = "Transaction has no symbol"
                return result
            
            # Determine direction based on transaction's entry direction
            with Session(get_db().bind) as session:
                # Get existing TP/SL orders
                statement = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction.id,
                    TradingOrder.open_type.in_([OrderOpenType.TAKE_PROFIT, OrderOpenType.STOP_LOSS]),
                    TradingOrder.status.notin_(OrderStatus.get_terminal_statuses())
                )
                existing_tpsl_orders = list(session.exec(statement).all())
                
                # Get entry order to determine direction
                entry_order = TransactionHelper.get_entry_order(transaction, session)
                if entry_order:
                    entry_direction = entry_order.direction
                else:
                    # Try to infer from existing orders
                    if existing_tpsl_orders:
                        # TP/SL orders have opposite direction to position
                        entry_direction = (OrderDirection.BUY 
                                         if existing_tpsl_orders[0].direction == OrderDirection.SELL 
                                         else OrderDirection.SELL)
                    else:
                        result["message"] = "Cannot determine position direction"
                        return result
            
            # Get existing TP/SL prices if not provided
            with Session(get_db().bind) as session:
                statement = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction.id,
                    TradingOrder.open_type.in_([OrderOpenType.TAKE_PROFIT, OrderOpenType.STOP_LOSS]),
                    TradingOrder.status.notin_(OrderStatus.get_terminal_statuses())
                )
                existing_tpsl = list(session.exec(statement).all())
                
                for order in existing_tpsl:
                    if order.open_type == OrderOpenType.TAKE_PROFIT and tp_price is None:
                        tp_price = order.limit_price
                    elif order.open_type == OrderOpenType.STOP_LOSS and sl_price is None:
                        sl_price = order.stop_price
            
            # Determine close direction (opposite of position)
            close_direction = OrderDirection.SELL if entry_direction == OrderDirection.BUY else OrderDirection.BUY
            
            if qty_change < 0:
                # ========== DECREASING QUANTITY (partial close) ==========
                close_qty = abs(qty_change)
                
                if close_qty >= current_qty:
                    result["message"] = f"Cannot close {close_qty} shares, only have {current_qty}"
                    return result
                
                # Step 1: Create the partial close order (triggered by TP/SL cancellation)
                # This order will execute AFTER we cancel the TP/SL orders
                trigger_order_id = None
                if existing_tpsl_orders:
                    # Use first TP/SL order as trigger
                    trigger_order_id = existing_tpsl_orders[0].id
                
                close_order = TradingOrder(
                    transaction_id=transaction.id,
                    account_id=transaction.account_id,
                    symbol=symbol,
                    direction=close_direction,
                    quantity=close_qty,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.WAITING_TRIGGER if trigger_order_id else OrderStatus.PENDING,
                    open_type=OrderOpenType.CLOSE,
                    depends_on_order=trigger_order_id,
                    depends_order_status_trigger=OrderStatus.CANCELED if trigger_order_id else None,
                    created_at=datetime.now(timezone.utc)
                )
                
                close_order_id = add_instance(close_order)
                result["orders_created"].append(close_order_id)
                logger.info(f"Created partial close order {close_order_id} for {close_qty} shares, "
                           f"triggered by order {trigger_order_id} cancellation")
                
                # Step 2: Cancel existing TP/SL orders
                for tpsl_order in existing_tpsl_orders:
                    try:
                        cancel_result = account.cancel_order(tpsl_order.id)
                        if cancel_result:
                            result["orders_canceled"].append(tpsl_order.id)
                            logger.info(f"Canceled TP/SL order {tpsl_order.id}")
                        else:
                            logger.warning(f"Failed to cancel TP/SL order {tpsl_order.id}")
                    except Exception as e:
                        logger.error(f"Error canceling TP/SL order {tpsl_order.id}: {e}", exc_info=True)
                
                # Step 3: Create new TP/SL orders (triggered by close order fill)
                remaining_qty = new_qty
                
                if tp_price and remaining_qty > 0:
                    tp_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=remaining_qty,
                        order_type=OrderType.LIMIT,
                        limit_price=tp_price,
                        status=OrderStatus.WAITING_TRIGGER,
                        open_type=OrderOpenType.TAKE_PROFIT,
                        depends_on_order=close_order_id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        created_at=datetime.now(timezone.utc)
                    )
                    tp_id = add_instance(tp_order)
                    result["orders_created"].append(tp_id)
                    logger.info(f"Created new TP order {tp_id} with qty={remaining_qty}, price={tp_price}")
                
                if sl_price and remaining_qty > 0:
                    sl_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=remaining_qty,
                        order_type=OrderType.STOP,
                        stop_price=sl_price,
                        status=OrderStatus.WAITING_TRIGGER,
                        open_type=OrderOpenType.STOP_LOSS,
                        depends_on_order=close_order_id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        created_at=datetime.now(timezone.utc)
                    )
                    sl_id = add_instance(sl_order)
                    result["orders_created"].append(sl_id)
                    logger.info(f"Created new SL order {sl_id} with qty={remaining_qty}, price={sl_price}")
                
                # Update transaction quantity
                transaction.quantity = new_qty
                update_instance(transaction)
                
                result["success"] = True
                result["message"] = (f"Created partial close order for {close_qty} shares. "
                                   f"New position size will be {remaining_qty} shares.")
                
            else:
                # ========== INCREASING QUANTITY (add-to-position) ==========
                add_qty = qty_change
                
                # Step 1: Create the add-to-position order
                add_order = TradingOrder(
                    transaction_id=transaction.id,
                    account_id=transaction.account_id,
                    symbol=symbol,
                    direction=entry_direction,
                    quantity=add_qty,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.PENDING,
                    open_type=OrderOpenType.ADD,
                    created_at=datetime.now(timezone.utc)
                )
                
                add_order_id = add_instance(add_order)
                result["orders_created"].append(add_order_id)
                logger.info(f"Created add-to-position order {add_order_id} for {add_qty} shares")
                
                # Step 2: Cancel existing TP/SL orders and get one for triggering
                trigger_order_id = None
                for tpsl_order in existing_tpsl_orders:
                    try:
                        if trigger_order_id is None:
                            trigger_order_id = tpsl_order.id  # Use first one as trigger
                        cancel_result = account.cancel_order(tpsl_order.id)
                        if cancel_result:
                            result["orders_canceled"].append(tpsl_order.id)
                            logger.info(f"Canceled TP/SL order {tpsl_order.id}")
                        else:
                            logger.warning(f"Failed to cancel TP/SL order {tpsl_order.id}")
                    except Exception as e:
                        logger.error(f"Error canceling TP/SL order {tpsl_order.id}: {e}", exc_info=True)
                
                # Step 3: Create new TP/SL orders (triggered by old TP/SL cancellation)
                if tp_price and new_qty > 0:
                    tp_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=new_qty,
                        order_type=OrderType.LIMIT,
                        limit_price=tp_price,
                        status=OrderStatus.WAITING_TRIGGER if trigger_order_id else OrderStatus.PENDING,
                        open_type=OrderOpenType.TAKE_PROFIT,
                        depends_on_order=trigger_order_id,
                        depends_order_status_trigger=OrderStatus.CANCELED if trigger_order_id else None,
                        created_at=datetime.now(timezone.utc)
                    )
                    tp_id = add_instance(tp_order)
                    result["orders_created"].append(tp_id)
                    logger.info(f"Created new TP order {tp_id} with qty={new_qty}, price={tp_price}")
                
                if sl_price and new_qty > 0:
                    sl_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=new_qty,
                        order_type=OrderType.STOP,
                        stop_price=sl_price,
                        status=OrderStatus.WAITING_TRIGGER if trigger_order_id else OrderStatus.PENDING,
                        open_type=OrderOpenType.STOP_LOSS,
                        depends_on_order=trigger_order_id,
                        depends_order_status_trigger=OrderStatus.CANCELED if trigger_order_id else None,
                        created_at=datetime.now(timezone.utc)
                    )
                    sl_id = add_instance(sl_order)
                    result["orders_created"].append(sl_id)
                    logger.info(f"Created new SL order {sl_id} with qty={new_qty}, price={sl_price}")
                
                # Update transaction quantity
                transaction.quantity = new_qty
                update_instance(transaction)
                
                result["success"] = True
                result["message"] = (f"Created add-to-position order for {add_qty} shares. "
                                   f"New position size will be {new_qty} shares.")
            
            return result
            
        except Exception as e:
            logger.error(f"Error adjusting transaction {transaction.id} quantity: {e}", exc_info=True)
            result["message"] = f"Error: {str(e)}"
            return result
