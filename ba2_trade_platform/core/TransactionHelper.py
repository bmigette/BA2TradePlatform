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
from .types import OrderStatus, OrderDirection, OrderType, TransactionStatus
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
    def is_tpsl_order(order: TradingOrder) -> bool:
        """
        Check if an order is a TP/SL order.
        
        TP/SL orders are identified by:
        1. Having depends_on_order set (depends on entry order)
        2. OR having order_type = OCO/OTO
        3. OR having "OCO-" in the comment
        
        Args:
            order: The order to check
        
        Returns:
            bool: True if this is a TP/SL order
        """
        if order.depends_on_order is not None:
            return True
        
        if order.order_type in [OrderType.OCO, OrderType.OTO]:
            return True
        
        if order.comment and "OCO-" in order.comment:
            return True
        
        return False
    
    @staticmethod
    def is_tp_order(order: TradingOrder, entry_direction: Optional[OrderDirection] = None) -> bool:
        """
        Check if an order is a Take Profit order.
        
        TP orders are identified by:
        - Being a TP/SL order (depends on entry)
        - Having a limit_price set (TP uses limit orders to take profit)
        - OR having 'tp' or 'take_profit' in the comment
        
        For OCO orders, checks if limit_price is set.
        
        Args:
            order: The order to check
            entry_direction: Optional entry direction to validate TP direction
        
        Returns:
            bool: True if this is a Take Profit order
        """
        if not TransactionHelper.is_tpsl_order(order):
            return False
        
        # Check comment for explicit TP indicator
        comment_lower = (order.comment or '').lower()
        if 'tp' in comment_lower or 'take_profit' in comment_lower or 'take profit' in comment_lower:
            return True
        
        # OCO orders have both TP (limit_price) and SL (stop_price)
        if order.order_type == OrderType.OCO:
            # For OCO, check if it has a limit_price (TP component)
            return order.limit_price is not None
        
        # Individual TP orders are LIMIT type orders with limit_price
        order_type_value = order.order_type.value.lower() if hasattr(order.order_type, 'value') else str(order.order_type).lower()
        if 'limit' in order_type_value and 'stop' not in order_type_value:
            return order.limit_price is not None
        
        # SELL_LIMIT or BUY_LIMIT types
        if order_type_value in ['sell_limit', 'buy_limit']:
            return order.limit_price is not None
        
        return False
    
    @staticmethod
    def is_sl_order(order: TradingOrder, entry_direction: Optional[OrderDirection] = None) -> bool:
        """
        Check if an order is a Stop Loss order.
        
        SL orders are identified by:
        - Being a TP/SL order (depends on entry)
        - Having a stop_price set (SL uses stop orders)
        - OR having 'sl' or 'stop_loss' in the comment
        
        For OCO orders, checks if stop_price is set.
        
        Args:
            order: The order to check
            entry_direction: Optional entry direction to validate SL direction
        
        Returns:
            bool: True if this is a Stop Loss order
        """
        if not TransactionHelper.is_tpsl_order(order):
            return False
        
        # Check comment for explicit SL indicator
        comment_lower = (order.comment or '').lower()
        if 'sl' in comment_lower or 'stop_loss' in comment_lower or 'stop loss' in comment_lower:
            return True
        
        # OCO orders have both TP (limit_price) and SL (stop_price)
        if order.order_type == OrderType.OCO:
            # For OCO, check if it has a stop_price (SL component)
            return order.stop_price is not None
        
        # Individual SL orders are STOP type orders with stop_price
        order_type_value = order.order_type.value.lower() if hasattr(order.order_type, 'value') else str(order.order_type).lower()
        if 'stop' in order_type_value and 'limit' not in order_type_value:
            return order.stop_price is not None
        
        # SELL_STOP or BUY_STOP types
        if order_type_value in ['sell_stop', 'buy_stop']:
            return order.stop_price is not None
        
        return False
    
    @staticmethod
    def get_tpsl_orders(
        transaction: Transaction,
        session: Optional[Session] = None,
        include_terminal: bool = False
    ) -> Dict[str, List[TradingOrder]]:
        """
        Get all TP/SL orders for a transaction, categorized by type.
        
        Args:
            transaction: The transaction to find TP/SL orders for
            session: Optional database session
            include_terminal: If True, include orders in terminal states (CANCELED, etc.)
        
        Returns:
            Dict with keys:
            - 'tp': List of Take Profit orders
            - 'sl': List of Stop Loss orders
            - 'oco': List of OCO orders (contain both TP and SL)
            - 'all': All TP/SL orders combined
        """
        close_session = False
        if session is None:
            session = Session(get_db().bind)
            close_session = True
        
        try:
            # Get all orders for transaction
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == transaction.id
            )
            if not include_terminal:
                statement = statement.where(
                    TradingOrder.status.notin_(OrderStatus.get_terminal_statuses())
                )
            
            all_orders = list(session.exec(statement).all())
            
            result = {
                'tp': [],
                'sl': [],
                'oco': [],
                'all': []
            }
            
            for order in all_orders:
                if not TransactionHelper.is_tpsl_order(order):
                    continue
                
                result['all'].append(order)
                
                # OCO orders contain both TP and SL
                if order.order_type == OrderType.OCO:
                    result['oco'].append(order)
                    # Also add to tp/sl if they have the respective prices
                    if order.limit_price is not None:
                        result['tp'].append(order)
                    if order.stop_price is not None:
                        result['sl'].append(order)
                else:
                    # Individual orders
                    if TransactionHelper.is_tp_order(order):
                        result['tp'].append(order)
                    elif TransactionHelper.is_sl_order(order):
                        result['sl'].append(order)
            
            return result
        finally:
            if close_session:
                session.close()
    
    @staticmethod
    def get_active_tpsl_orders(
        transaction: Transaction,
        session: Optional[Session] = None
    ) -> List[TradingOrder]:
        """
        Get all active (non-terminal) TP/SL orders for a transaction.
        
        This is a convenience method that returns all active TP/SL orders
        regardless of whether they are TP, SL, or OCO.
        
        Args:
            transaction: The transaction to find TP/SL orders for
            session: Optional database session
        
        Returns:
            List[TradingOrder]: List of active TP/SL orders
        """
        result = TransactionHelper.get_tpsl_orders(
            transaction, session, include_terminal=False
        )
        return result['all']
    
    @staticmethod
    def get_tpsl_prices(
        transaction: Transaction,
        session: Optional[Session] = None
    ) -> Dict[str, Optional[float]]:
        """
        Get the current TP and SL prices from active orders for a transaction.
        
        If multiple orders exist, returns the first found price for each.
        
        Args:
            transaction: The transaction to get TP/SL prices for
            session: Optional database session
        
        Returns:
            Dict with 'tp_price' and 'sl_price' keys (None if not found)
        """
        tpsl_orders = TransactionHelper.get_tpsl_orders(transaction, session, include_terminal=False)
        
        tp_price = None
        sl_price = None
        
        # Check OCO orders first (they have both)
        for order in tpsl_orders['oco']:
            if tp_price is None and order.limit_price:
                tp_price = order.limit_price
            if sl_price is None and order.stop_price:
                sl_price = order.stop_price
        
        # Check individual TP orders
        if tp_price is None:
            for order in tpsl_orders['tp']:
                if order.limit_price:
                    tp_price = order.limit_price
                    break
        
        # Check individual SL orders
        if sl_price is None:
            for order in tpsl_orders['sl']:
                if order.stop_price:
                    sl_price = order.stop_price
                    break
        
        return {'tp_price': tp_price, 'sl_price': sl_price}
    
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
        Adjust transaction quantity with proper triggered order sequencing.
        
        This method handles Alpaca's "held_for_orders" constraint by creating triggered
        orders that execute in sequence based on status changes. The TradeManager
        processes WAITING_TRIGGER orders when their parent reaches the trigger status.
        
        **DECREASING QTY (partial close)**:
        1. Create WAITING_TRIGGER SELL order (triggered when TP/SL status == CANCELED)
        2. Cancel existing TP/SL orders (this triggers the SELL order to submit)
        3. Create WAITING_TRIGGER TP/SL orders (triggered when SELL order status == FILLED)
        
        **INCREASING QTY (add-to-position)**:
        1. Submit the add-to-position BUY order immediately
        2. Cancel existing TP/SL orders
        3. Create WAITING_TRIGGER TP/SL orders (triggered when old TP/SL status == CANCELED)
        
        The TradeManager's _check_all_waiting_trigger_orders() method monitors these
        orders and submits them when their trigger conditions are met.
        
        Args:
            account: The AccountInterface instance to use for order operations
            transaction: The transaction to adjust
            qty_change: Quantity change (positive to add, negative to reduce)
            tp_price: Optional new take-profit price (uses existing if not provided)
            sl_price: Optional new stop-loss price (uses existing if not provided)
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
            
            # Get existing TP/SL orders using helper
            existing_tpsl_orders = TransactionHelper.get_active_tpsl_orders(transaction)
            
            # Determine direction based on transaction's entry direction
            with Session(get_db().bind) as session:
                # Get entry order to determine direction
                entry_order = TransactionHelper.get_entry_order(transaction, session)
                if entry_order:
                    entry_direction = entry_order.side
                else:
                    # Try to infer from existing orders
                    if existing_tpsl_orders:
                        # TP/SL orders have opposite direction to position
                        entry_direction = (OrderDirection.BUY 
                                         if existing_tpsl_orders[0].side == OrderDirection.SELL 
                                         else OrderDirection.SELL)
                    else:
                        result["message"] = "Cannot determine position direction"
                        return result
            
            # Get existing TP/SL prices if not provided
            if tp_price is None:
                tp_price = transaction.take_profit
            if sl_price is None:
                sl_price = transaction.stop_loss
            
            # Determine close direction (opposite of position)
            close_direction = OrderDirection.SELL if entry_direction == OrderDirection.BUY else OrderDirection.BUY
            
            if qty_change < 0:
                # ========== DECREASING QUANTITY (partial close) ==========
                # Flow: Create triggered SELL -> Cancel TP/SL (triggers SELL) -> Create triggered new TP/SL
                close_qty = abs(qty_change)
                
                if close_qty >= current_qty:
                    result["message"] = f"Cannot close {close_qty} shares, only have {current_qty}"
                    return result
                
                remaining_qty = new_qty
                
                # Step 1: Create WAITING_TRIGGER partial close order
                # This order will be triggered when the first TP/SL order is CANCELED
                trigger_order_id = None
                if existing_tpsl_orders:
                    trigger_order_id = existing_tpsl_orders[0].id
                
                close_order = TradingOrder(
                    transaction_id=transaction.id,
                    account_id=transaction.account_id,
                    symbol=symbol,
                    direction=close_direction,
                    quantity=close_qty,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.WAITING_TRIGGER if trigger_order_id else OrderStatus.PENDING,
                    depends_on_order=trigger_order_id,
                    depends_order_status_trigger=OrderStatus.CANCELED if trigger_order_id else None,
                    comment="Partial close order (triggered by TP/SL cancel)",
                    created_at=datetime.now(timezone.utc)
                )
                
                close_order_id = add_instance(close_order)
                result["orders_created"].append(close_order_id)
                logger.info(
                    f"Created WAITING_TRIGGER partial close order {close_order_id} for {close_qty} shares, "
                    f"will trigger when order {trigger_order_id} is CANCELED"
                )
                
                # Step 2: Cancel existing TP/SL orders (this will trigger the close order)
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
                
                # Step 3: Create WAITING_TRIGGER TP/SL orders
                # These will be triggered when the close order is FILLED
                if tp_price and remaining_qty > 0:
                    tp_order_type = OrderType.SELL_LIMIT if entry_direction == OrderDirection.BUY else OrderType.BUY_LIMIT
                    tp_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=remaining_qty,
                        order_type=tp_order_type,
                        limit_price=tp_price,
                        status=OrderStatus.WAITING_TRIGGER,
                        depends_on_order=close_order_id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        comment="Take Profit order (triggered by close fill)",
                        created_at=datetime.now(timezone.utc)
                    )
                    tp_id = add_instance(tp_order)
                    result["orders_created"].append(tp_id)
                    logger.info(
                        f"Created WAITING_TRIGGER TP order {tp_id} at ${tp_price}, qty={remaining_qty}, "
                        f"will trigger when order {close_order_id} is FILLED"
                    )
                
                if sl_price and remaining_qty > 0:
                    sl_order_type = OrderType.SELL_STOP if entry_direction == OrderDirection.BUY else OrderType.BUY_STOP
                    sl_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=remaining_qty,
                        order_type=sl_order_type,
                        stop_price=sl_price,
                        status=OrderStatus.WAITING_TRIGGER,
                        depends_on_order=close_order_id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        comment="Stop Loss order (triggered by close fill)",
                        created_at=datetime.now(timezone.utc)
                    )
                    sl_id = add_instance(sl_order)
                    result["orders_created"].append(sl_id)
                    logger.info(
                        f"Created WAITING_TRIGGER SL order {sl_id} at ${sl_price}, qty={remaining_qty}, "
                        f"will trigger when order {close_order_id} is FILLED"
                    )
                
                # Update transaction
                transaction.quantity = remaining_qty
                if tp_price:
                    transaction.take_profit = tp_price
                if sl_price:
                    transaction.stop_loss = sl_price
                update_instance(transaction)
                
                result["success"] = True
                result["message"] = (
                    f"Created triggered order chain for partial close of {close_qty} shares. "
                    f"New position size will be {remaining_qty} shares after close order fills."
                )
                
            else:
                # ========== INCREASING QUANTITY (add-to-position) ==========
                # Flow: Submit BUY immediately -> Cancel TP/SL -> Create triggered new TP/SL
                add_qty = qty_change
                
                # Step 1: Submit add-to-position order immediately
                add_order = TradingOrder(
                    transaction_id=transaction.id,
                    account_id=transaction.account_id,
                    symbol=symbol,
                    direction=entry_direction,
                    quantity=add_qty,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.PENDING,
                    comment="Add-to-position order",
                    created_at=datetime.now(timezone.utc)
                )
                
                try:
                    submitted_order = account.submit_order(add_order)
                    if submitted_order and hasattr(submitted_order, 'id'):
                        result["orders_created"].append(submitted_order.id)
                        logger.info(f"Submitted add-to-position order {submitted_order.id} for {add_qty} shares")
                    elif add_order.id:
                        result["orders_created"].append(add_order.id)
                        logger.info(f"Submitted add-to-position order {add_order.id} for {add_qty} shares")
                except Exception as e:
                    logger.error(f"Error submitting add-to-position order: {e}", exc_info=True)
                    result["message"] = f"Failed to submit add-to-position order: {str(e)}"
                    return result
                
                # Step 2: Get trigger order ID before canceling, then cancel existing TP/SL orders
                trigger_order_id = None
                if existing_tpsl_orders:
                    trigger_order_id = existing_tpsl_orders[0].id
                
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
                
                # Step 3: Create WAITING_TRIGGER TP/SL orders
                # These will be triggered when the old TP/SL order is CANCELED
                if tp_price and new_qty > 0:
                    tp_order_type = OrderType.SELL_LIMIT if entry_direction == OrderDirection.BUY else OrderType.BUY_LIMIT
                    tp_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=new_qty,
                        order_type=tp_order_type,
                        limit_price=tp_price,
                        status=OrderStatus.WAITING_TRIGGER if trigger_order_id else OrderStatus.PENDING,
                        depends_on_order=trigger_order_id,
                        depends_order_status_trigger=OrderStatus.CANCELED if trigger_order_id else None,
                        comment="Take Profit order (triggered by old TP/SL cancel)",
                        created_at=datetime.now(timezone.utc)
                    )
                    tp_id = add_instance(tp_order)
                    result["orders_created"].append(tp_id)
                    logger.info(
                        f"Created WAITING_TRIGGER TP order {tp_id} at ${tp_price}, qty={new_qty}, "
                        f"will trigger when order {trigger_order_id} is CANCELED"
                    )
                
                if sl_price and new_qty > 0:
                    sl_order_type = OrderType.SELL_STOP if entry_direction == OrderDirection.BUY else OrderType.BUY_STOP
                    sl_order = TradingOrder(
                        transaction_id=transaction.id,
                        account_id=transaction.account_id,
                        symbol=symbol,
                        direction=close_direction,
                        quantity=new_qty,
                        order_type=sl_order_type,
                        stop_price=sl_price,
                        status=OrderStatus.WAITING_TRIGGER if trigger_order_id else OrderStatus.PENDING,
                        depends_on_order=trigger_order_id,
                        depends_order_status_trigger=OrderStatus.CANCELED if trigger_order_id else None,
                        comment="Stop Loss order (triggered by old TP/SL cancel)",
                        created_at=datetime.now(timezone.utc)
                    )
                    sl_id = add_instance(sl_order)
                    result["orders_created"].append(sl_id)
                    logger.info(
                        f"Created WAITING_TRIGGER SL order {sl_id} at ${sl_price}, qty={new_qty}, "
                        f"will trigger when order {trigger_order_id} is CANCELED"
                    )
                
                # Update transaction
                transaction.quantity = new_qty
                if tp_price:
                    transaction.take_profit = tp_price
                if sl_price:
                    transaction.stop_loss = sl_price
                update_instance(transaction)
                
                result["success"] = True
                result["message"] = (
                    f"Submitted add-to-position order for {add_qty} shares. "
                    f"New TP/SL orders will activate when old orders are canceled. "
                    f"New position size: {new_qty} shares."
                )
            
            return result
            
        except Exception as e:
            logger.error(f"Error adjusting transaction {transaction.id} quantity: {e}", exc_info=True)
            result["message"] = f"Error: {str(e)}"
            return result
