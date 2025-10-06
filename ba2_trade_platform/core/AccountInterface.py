from abc import abstractmethod
from typing import Any, Dict, Optional
from unittest import result
from datetime import datetime, timezone
from ..logger import logger
from ..core.models import AccountSetting, TradingOrder, Transaction, ExpertRecommendation, ExpertInstance
from ..core.types import OrderOpenType, OrderDirection, OrderType, OrderStatus, TransactionStatus
from ..core.ExtendableSettingsInterface import ExtendableSettingsInterface
from ..core.db import add_instance, get_instance, update_instance


class AccountInterface(ExtendableSettingsInterface):
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"
    
    """
    Abstract base class for trading account interfaces.
    
    This class defines the required interface for all account provider implementations in the BA2 Trade Platform.
    Subclasses must implement all abstract methods to support account info retrieval, order management, position tracking,
    and broker synchronization. All account plugins should inherit from this class and provide concrete implementations
    for the required methods.
    """
    def __init__(self, id: int):
        """
        Initialize the account with a unique identifier.

        Args:
            id (int): The unique identifier for the account.
        """
        self.id = id




    @abstractmethod
    def get_balance(self) -> Optional[float]:
        """
        Get the current account balance/equity.
        
        Returns:
            Optional[float]: The current account balance if available, None if error occurred
        """
        pass

    @abstractmethod
    def get_account_info(self) -> Dict[str, Any]:
        """
        Retrieve account information such as balance, equity, buying power, etc.
        
        Returns:
            Dict[str, Any]: A dictionary containing account information with fields such as:
                - balance: Current account balance
                - equity: Total account value including positions
                - buying_power: Available funds for trading
                - etc.
        """
        pass

    @abstractmethod
    def get_positions(self) -> Any:
        """
        Retrieve all current open positions in the account.
        
        Returns:
            Any: A list or collection of position objects containing information such as:
                - symbol: The asset symbol
                - quantity: Position size
                - average_price: Average entry price
                - current_price: Current market price
                - unrealized_pl: Unrealized profit/loss
        """
        pass

    @abstractmethod
    def get_orders(self, status: Optional[str] = None) -> Any:
        """
        Retrieve orders, optionally filtered by status.
        
        Args:
            status (Optional[str]): The status to filter orders by (e.g., 'open', 'closed', 'canceled').
                                  If None, returns all orders.
        
        Returns:
            Any: A list or collection of order objects containing information such as:
                - id: Order identifier
                - symbol: The asset symbol
                - quantity: Order size
                - side: Buy/Sell
                - type: Market/Limit/Stop
                - status: Current order status
                - filled_quantity: Amount filled
                - created_at: Order creation timestamp
        """
        pass

    @abstractmethod
    def _submit_order_impl(self, trading_order) -> Any:
        """
        Internal implementation of order submission. This method should be implemented by child classes.
        The public submit_order method will call this after validation.

        Args:
            trading_order: A validated TradingOrder object containing all order details.

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        pass

    def _generate_tracking_comment(self, trading_order: TradingOrder) -> str:
        """
        Generate a tracking comment for the order with epoch time and metadata prefix.
        Format: {epoch}-[ACC:X/EXP:Y/TR:Z/ORD:W] original_comment
        
        Args:
            trading_order: The TradingOrder object
            
        Returns:
            str: The formatted comment with epoch time and tracking metadata, truncated to 128 characters if needed
        """
        import re
        import time
        
        # Get epoch time (microseconds precision) for uniqueness
        epoch = int(time.time() * 1000000)
        
        # Get account_id
        account_id = trading_order.account_id or self.id
        
        # Get expert_id if available through recommendation
        expert_id = None
        if trading_order.expert_recommendation_id:
            try:
                recommendation = get_instance(ExpertRecommendation, trading_order.expert_recommendation_id)
                if recommendation:
                    expert_instance = get_instance(ExpertInstance, recommendation.instance_id)
                    if expert_instance:
                        expert_id = expert_instance.id
            except Exception as e:
                logger.debug(f"Could not retrieve expert_id for order: {e}")
        
        # Get transaction_id
        transaction_id = trading_order.transaction_id or "NONE"
        
        # Get order_id (will be None for new orders)
        order_id = trading_order.id or "NEW"
        
        # Build tracking prefix with epoch time at the beginning
        exp_part = f"/EXP:{expert_id}" if expert_id else ""
        tracking_prefix = f"{epoch}-[ACC:{account_id}{exp_part}/TR:{transaction_id}/ORD:{order_id}]"
        
        # Get original comment and clean any existing automated comment prefix
        original_comment = trading_order.comment or ""
        # Remove any existing epoch-[ACC:...] prefix using regex
        original_comment = re.sub(r'^\d+-\[ACC:\d+(?:/EXP:\d+)?/TR:\w+/ORD:\w+\]\s*', '', original_comment).strip()
        
        # Combine prefix with original comment
        if original_comment:
            full_comment = f"{tracking_prefix} {original_comment}"
        else:
            full_comment = tracking_prefix
        
        # Truncate to 128 characters if needed
        max_length = 128
        if len(full_comment) > max_length:
            # Try to preserve the tracking prefix and truncate the original comment
            available_space = max_length - len(tracking_prefix) - 1  # -1 for space
            if available_space > 0:
                truncated_comment = original_comment[:available_space - 3] + "..."
                full_comment = f"{tracking_prefix} {truncated_comment}"
            else:
                # If tracking prefix itself is too long, just truncate everything
                full_comment = full_comment[:max_length]
        
        return full_comment

    def submit_order(self, trading_order: TradingOrder) -> Any:
        """
        Submit a new order to the account with validation and transaction handling.

        For market orders without transaction_id: automatically creates a new Transaction
        For all other order types: requires existing transaction_id or raises exception

        Args:
            trading_order: A TradingOrder object containing all order details.

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        # Validate the trading order before submission
        validation_result = self._validate_trading_order(trading_order)
        if not validation_result['is_valid']:
            error_msg = f"Order validation failed: {', '.join(validation_result['errors'])}"
            logger.error(f"Order validation failed for order: {error_msg}", exc_info=True)
            raise ValueError(error_msg)
        
        # Handle transaction requirements based on order type
        self._handle_transaction_requirements(trading_order)
        
        # Generate tracking comment and set account_id BEFORE saving to DB
        tracking_comment = self._generate_tracking_comment(trading_order)
        trading_order.comment = tracking_comment
        trading_order.account_id = self.id
        
        # Capture values for logging BEFORE saving (to avoid detached instance errors)
        symbol = trading_order.symbol
        side = trading_order.side
        quantity = trading_order.quantity
        order_type = trading_order.order_type
        
        # CRITICAL: Save order to database BEFORE broker submission
        # This ensures the order has an ID for error tracking
        # Use expunge_after_flush=True to allow normal attribute access after save
        from .db import add_instance
        
        if not trading_order.id:
            # Save to database - object will be expunged and can be used like a normal Pydantic object
            order_id = add_instance(trading_order, expunge_after_flush=True)
            logger.debug(f"Created order {order_id} in database before broker submission")
        
        # Log successful validation (using captured values to avoid any potential issues)
        logger.info(f"Order validation passed for {symbol} - {side.value} {quantity} @ {order_type.value}")
        
        # Call the child class implementation (this will update the order with broker_order_id)
        # The trading_order object is now detached but all attributes are accessible
        result = self._submit_order_impl(trading_order)
        
        return result
    
    def _handle_transaction_requirements(self, trading_order: TradingOrder) -> None:
        """
        Handle transaction creation/validation requirements based on order type.
        
        Args:
            trading_order: The TradingOrder object to process
            
        Raises:
            ValueError: If order requirements are not met
        """
        # Check if order type is market
        is_market_order = (hasattr(trading_order, 'order_type') and 
                          trading_order.order_type == OrderType.MARKET)
        
        # Check if transaction_id is provided
        has_transaction = (hasattr(trading_order, 'transaction_id') and 
                          trading_order.transaction_id is not None)
        
        if is_market_order and not has_transaction:
            # Automatically create Transaction for market orders without transaction_id
            self._create_transaction_for_order(trading_order)
            logger.info(f"Automatically created transaction {trading_order.transaction_id} for market order")
            
        elif not is_market_order and not has_transaction:
            # All non-market orders must have an existing transaction
            raise ValueError(f"Non-market orders ({trading_order.order_type.value if trading_order.order_type else 'unknown'}) must be attached to an existing transaction. No transaction_id provided.")
        
        elif has_transaction:
            # Validate that the transaction exists
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction:
                raise ValueError(f"Transaction {trading_order.transaction_id} not found")
            logger.debug(f"Order linked to existing transaction {trading_order.transaction_id}")
    
    def _create_transaction_for_order(self, trading_order: TradingOrder) -> None:
        """
        Create a new Transaction for the given trading order.
        
        Args:
            trading_order: The TradingOrder object to create a transaction for
        """
        try:
            # Get current price for the symbol (this will be the open_price estimate)
            current_price = self.get_instrument_current_price(trading_order.symbol)
            
            # Get expert_id from the expert_recommendation if available
            expert_id = None
            if trading_order.expert_recommendation_id:
                from .models import ExpertRecommendation
                recommendation = get_instance(ExpertRecommendation, trading_order.expert_recommendation_id)
                if recommendation:
                    expert_id = recommendation.instance_id
                    logger.debug(f"Found expert_id {expert_id} from recommendation {trading_order.expert_recommendation_id}")
                else:
                    logger.warning(f"Expert recommendation {trading_order.expert_recommendation_id} not found for order")
            else:
                logger.debug("Order has no expert_recommendation_id, transaction will have no expert_id")
            
            # Create new transaction
            transaction = Transaction(
                symbol=trading_order.symbol,
                quantity=trading_order.quantity if trading_order.side == OrderDirection.BUY else -trading_order.quantity,
                open_price=current_price,  # Estimated open price
                status=TransactionStatus.WAITING,
                created_at=datetime.now(timezone.utc),
                expert_id=expert_id  # Link to expert instance
            )
            
            # Save transaction to database
            transaction_id = add_instance(transaction)
            trading_order.transaction_id = transaction_id
            
            logger.info(f"Created transaction {transaction_id} for order: {trading_order.symbol} {trading_order.side.value} {trading_order.quantity} (expert_id={expert_id})")
            
        except Exception as e:
            logger.error(f"Error creating transaction for order: {e}", exc_info=True)
            raise ValueError(f"Failed to create transaction for order: {e}")
    
    def _submit_pending_tp_sl_orders(self, trading_order: TradingOrder) -> None:
        """
        Check if the order's transaction has pending TP/SL values and submit them to the broker.
        This is called after a PENDING order is successfully submitted to the broker.
        
        Args:
            trading_order: The order that was just submitted
        """
        try:
            if not trading_order.transaction_id:
                return
            
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction:
                return
            
            # Check if transaction has take_profit or stop_loss set
            if transaction.take_profit:
                logger.info(f"Submitting pending TP order (${transaction.take_profit}) to broker for order {trading_order.id}")
                try:
                    # Call the implementation method directly to create TP order at broker
                    # Skip the check for PENDING status since order is now submitted
                    from .types import OrderStatus
                    original_status = trading_order.status
                    trading_order.status = OrderStatus.SUBMITTED  # Temporarily mark as submitted
                    self._set_order_tp_impl(trading_order, transaction.take_profit)
                    trading_order.status = original_status  # Restore original status
                    logger.info(f"Successfully submitted TP order to broker")
                except Exception as tp_error:
                    logger.error(f"Failed to submit TP order to broker: {tp_error}", exc_info=True)
            
            if transaction.stop_loss:
                logger.info(f"Submitting pending SL order (${transaction.stop_loss}) to broker for order {trading_order.id}")
                try:
                    # Call the implementation method if it exists
                    if hasattr(self, '_set_order_sl_impl'):
                        from .types import OrderStatus
                        original_status = trading_order.status
                        trading_order.status = OrderStatus.SUBMITTED
                        self._set_order_sl_impl(trading_order, transaction.stop_loss)
                        trading_order.status = original_status
                        logger.info(f"Successfully submitted SL order to broker")
                except Exception as sl_error:
                    logger.error(f"Failed to submit SL order to broker: {sl_error}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"Error submitting pending TP/SL orders: {e}", exc_info=True)

    def _validate_trading_order(self, trading_order: TradingOrder) -> Dict[str, Any]:
        """
        Validate a trading order before submission.
        
        Args:
            trading_order: The TradingOrder object to validate
            
        Returns:
            Dict[str, Any]: Validation result with 'is_valid' (bool) and 'errors' (list) keys
        """
        errors = []
        
        # Check if trading_order exists
        if trading_order is None:
            errors.append("trading_order cannot be None")
            return {'is_valid': False, 'errors': errors}
        
        # Validate required fields
        if not hasattr(trading_order, 'symbol') or not trading_order.symbol:
            errors.append("symbol is required and cannot be empty")
        elif not isinstance(trading_order.symbol, str):
            errors.append("symbol must be a string")
        elif len(trading_order.symbol.strip()) == 0:
            errors.append("symbol cannot be empty or whitespace only")
            
        if not hasattr(trading_order, 'quantity') or trading_order.quantity is None:
            errors.append("quantity is required")
        elif not isinstance(trading_order.quantity, (int, float)):
            errors.append("quantity must be a number")
        elif trading_order.quantity <= 0:
            errors.append("quantity must be greater than 0")
            
        if not hasattr(trading_order, 'side') or trading_order.side is None:
            errors.append("side is required")
        elif not isinstance(trading_order.side, OrderDirection):
            errors.append(f"side must be an OrderDirection enum, got {type(trading_order.side)}")
            
        if not hasattr(trading_order, 'order_type') or trading_order.order_type is None:
            errors.append("order_type is required")
        elif not isinstance(trading_order.order_type, OrderType):
            errors.append(f"order_type must be an OrderType enum, got {type(trading_order.order_type)}")
            
        if not hasattr(trading_order, 'account_id') or trading_order.account_id is None:
            errors.append("account_id is required")
        elif not isinstance(trading_order.account_id, int):
            errors.append("account_id must be an integer")
        elif trading_order.account_id != self.id:
            errors.append(f"order account_id ({trading_order.account_id}) does not match this account ({self.id})")
            
        # Validate limit orders have limit_price
        if (hasattr(trading_order, 'order_type') and 
            trading_order.order_type in [OrderType.BUY_LIMIT, OrderType.SELL_LIMIT]):
            if not hasattr(trading_order, 'limit_price') or trading_order.limit_price is None:
                errors.append(f"limit_price is required for {trading_order.order_type.value} orders")
            elif not isinstance(trading_order.limit_price, (int, float)):
                errors.append("limit_price must be a number")
            elif trading_order.limit_price <= 0:
                errors.append("limit_price must be greater than 0")
                
        # Validate stop orders have stop_price
        if (hasattr(trading_order, 'order_type') and 
            trading_order.order_type in [OrderType.BUY_STOP, OrderType.SELL_STOP]):
            if not hasattr(trading_order, 'stop_price') or trading_order.stop_price is None:
                errors.append(f"stop_price is required for {trading_order.order_type.value} orders")
            elif not isinstance(trading_order.stop_price, (int, float)):
                errors.append("stop_price must be a number")
            elif trading_order.stop_price <= 0:
                errors.append("stop_price must be greater than 0")
                
        # Validate status if present
        if hasattr(trading_order, 'status') and trading_order.status is not None:
            if not isinstance(trading_order.status, OrderStatus):
                errors.append(f"status must be an OrderStatus enum, got {type(trading_order.status)}")
                
        # Validate open_type if present
        if hasattr(trading_order, 'open_type') and trading_order.open_type is not None:
            if not isinstance(trading_order.open_type, OrderOpenType):
                errors.append(f"open_type must be an OrderOpenType enum, got {type(trading_order.open_type)}")
                
        # Validate dependency fields
        if (hasattr(trading_order, 'depends_on_order') and trading_order.depends_on_order is not None):
            if not isinstance(trading_order.depends_on_order, int):
                errors.append("depends_on_order must be an integer")
            elif trading_order.depends_on_order <= 0:
                errors.append("depends_on_order must be a positive integer")
                
            # If depends_on_order is set, depends_order_status_trigger should also be set
            if (not hasattr(trading_order, 'depends_order_status_trigger') or 
                trading_order.depends_order_status_trigger is None):
                errors.append("depends_order_status_trigger is required when depends_on_order is set")
            elif not isinstance(trading_order.depends_order_status_trigger, OrderStatus):
                errors.append("depends_order_status_trigger must be an OrderStatus enum")
                
        # Validate string fields for length and content
        if hasattr(trading_order, 'comment') and trading_order.comment is not None:
            if not isinstance(trading_order.comment, str):
                errors.append("comment must be a string")
            elif len(trading_order.comment) > 1000:  # Reasonable limit
                errors.append("comment is too long (max 1000 characters)")
                
        if hasattr(trading_order, 'good_for') and trading_order.good_for is not None:
            if not isinstance(trading_order.good_for, str):
                errors.append("good_for must be a string")
            elif trading_order.good_for.lower() not in ['gtc', 'day', 'ioc', 'fok']:
                errors.append("good_for must be one of: 'gtc', 'day', 'ioc', 'fok'")
                
        return {
            'is_valid': len(errors) == 0,
            'errors': errors
        }

    @abstractmethod
    def cancel_order(self, order_id: str) -> Any:
        """
        Cancel an existing order by order ID.
        
        Args:
            order_id (str): The unique identifier of the order to cancel
        
        Returns:
            Any: True if cancellation was successful, False or raises exception if failed
        """
        pass
    
    @abstractmethod
    def modify_order(self, order_id: str) -> Any:
        """
        Modify an existing order by order ID.
        
        Args:
            order_id (str): The unique identifier of the order to cancel
        
        Returns:
            Any: True if modification was successful, False or raises exception if failed
        """
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Any:
        """
        Retrieve a specific order by order ID.
        
        Args:
            order_id (str): The unique identifier of the order to retrieve
        
        Returns:
            Any: The order object if found, None or raises exception if not found
                Order object typically contains:
                - id: Order identifier
                - symbol: The asset symbol
                - quantity: Order size
                - status: Current order status
                - filled_quantity: Amount filled
                - created_at: Order creation timestamp
        """
        pass


    @abstractmethod
    def get_instrument_current_price(self, symbol: str) -> Optional[float]:
        """
        Get the current market price for a given instrument/symbol.
        
        Args:
            symbol (str): The asset symbol to get the price for
        
        Returns:
            Optional[float]: The current price if available, None if not found or error occurred
        """
        pass

    @abstractmethod
    def _set_order_tp_impl(self, trading_order: TradingOrder, tp_price: float) -> Any:
        """
        Internal implementation of take profit order setting. This method should be implemented by child classes.
        The public set_order_tp method will call this after updating the transaction.

        Args:
            trading_order: The original TradingOrder object
            tp_price: The take profit price

        Returns:
            Any: The created/modified take profit order object if successful. Returns None or raises an exception if failed.
        """
        pass

    def set_order_tp(self, trading_order: TradingOrder, tp_price: float) -> Any:
        """
        Set take profit for an existing order.
        
        This method:
        1. Updates the linked transaction's take_profit value
        2. Calls the implementation to create/modify the take profit order at the broker
        
        Args:
            trading_order: The original TradingOrder object
            tp_price: The take profit price
            
        Returns:
            Any: The created/modified take profit order object if successful. Returns None or raises an exception if failed.
        """
        try:
            # Validate inputs
            if not trading_order:
                raise ValueError("trading_order cannot be None")
            if not isinstance(tp_price, (int, float)) or tp_price <= 0:
                raise ValueError("tp_price must be a positive number")
            
            # Get the linked transaction
            if not trading_order.transaction_id:
                raise ValueError("Order must have a linked transaction to set take profit")
            
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction:
                raise ValueError(f"Transaction {trading_order.transaction_id} not found")
            
            # Update transaction's take_profit value
            transaction.take_profit = tp_price
            update_instance(transaction)
            
            logger.info(f"Updated transaction {transaction.id} take_profit to ${tp_price}")
            
            # Call implementation to handle broker-side take profit order
            return self._set_order_tp_impl(trading_order, tp_price)
            
        except Exception as e:
            logger.error(f"Error setting take profit for order {trading_order.id if trading_order else 'None'}: {e}", exc_info=True)
            raise

    @abstractmethod
    def refresh_positions(self) -> bool:
        """
        Refresh/synchronize account positions from the broker.
        This method should update any cached position data with fresh data from the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        pass

    @abstractmethod
    def refresh_orders(self) -> bool:
        """
        Refresh/synchronize account orders from the broker.
        This method should update any cached order data and sync database records
        with the current state of orders at the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        pass

    def refresh_transactions(self) -> bool:
        """
        Refresh/synchronize transaction states based on linked order and position states.
        
        This method ensures transaction states are in sync with their linked orders:
        - If any market entry order (order without depends_on_order) is FILLED, transaction should be OPENED
        - If a closing order is FILLED, transaction should be CLOSED with close_price set
        - If all orders are canceled/rejected before execution, transaction should be CLOSED
        - Updates open_price and close_price based on filled orders
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:
            from sqlmodel import select, Session
            from .db import get_db
            
            # Get terminal and executed order states from OrderStatus
            terminal_statuses = OrderStatus.get_terminal_statuses()
            executed_statuses = OrderStatus.get_executed_statuses()
            
            updated_count = 0
            
            with Session(get_db().bind) as session:
                # Get all transactions for this account
                statement = select(Transaction).join(TradingOrder).where(
                    TradingOrder.account_id == self.id
                ).distinct()
                
                transactions = session.exec(statement).all()
                
                for transaction in transactions:
                    original_status = transaction.status
                    has_changes = False
                    
                    # Get all orders for this transaction
                    orders_statement = select(TradingOrder).where(
                        TradingOrder.transaction_id == transaction.id,
                        TradingOrder.account_id == self.id
                    )
                    orders = session.exec(orders_statement).all()
                    
                    if not orders:
                        continue
                    
                    # Separate orders into market entry orders and TP/SL orders
                    # Market entry orders are orders that open a position (no depends_on_order)
                    # TP/SL orders are dependent orders (have depends_on_order set)
                    market_entry_orders = [o for o in orders if not o.depends_on_order]
                    dependent_orders = [o for o in orders if o.depends_on_order]
                    
                    # Check if any market entry order is filled (to open transaction)
                    has_filled_entry_order = any(order.status in executed_statuses for order in market_entry_orders)
                    
                    # Check if all MARKET ENTRY orders are in terminal state (not TP/SL orders)
                    # This determines if the transaction should be closed
                    all_entry_orders_terminal = (
                        len(market_entry_orders) > 0 and
                        all(order.status in terminal_statuses for order in market_entry_orders)
                    )
                    
                    # Check if we have a filled closing order (dependent order that closes position)
                    filled_closing_orders = [o for o in dependent_orders if o.status == OrderStatus.FILLED]
                    
                    # Calculate transaction quantity from filled market entry orders
                    # Sum all filled quantities from market entry orders
                    calculated_quantity = 0.0
                    for order in market_entry_orders:
                        if order.status in executed_statuses:
                            # Use filled_qty if available, otherwise use order quantity
                            qty = order.filled_qty if order.filled_qty else order.quantity
                            if qty:
                                # Add for BUY orders, subtract for SELL orders (for short positions)
                                if order.side == OrderDirection.BUY:
                                    calculated_quantity += float(qty)
                                elif order.side == OrderDirection.SELL:
                                    calculated_quantity -= float(qty)
                    
                    # Update transaction quantity if different
                    if calculated_quantity != 0 and transaction.quantity != calculated_quantity:
                        transaction.quantity = calculated_quantity
                        has_changes = True
                        logger.debug(f"Transaction {transaction.id} quantity updated to {calculated_quantity}")
                    
                    # Update transaction status based on order states
                    new_status = None
                    
                    # WAITING -> OPENED: If any market entry order is FILLED
                    if has_filled_entry_order and transaction.status == TransactionStatus.WAITING:
                        new_status = TransactionStatus.OPENED
                        if not transaction.open_date:
                            transaction.open_date = datetime.now(timezone.utc)
                            has_changes = True
                        
                        # Set open_price from the first filled market entry order
                        if not transaction.open_price:
                            for order in market_entry_orders:
                                if order.status in executed_statuses and order.limit_price:
                                    transaction.open_price = order.limit_price
                                    has_changes = True
                                    break
                                # For market orders, try to get current price from broker
                                elif order.status in executed_statuses:
                                    # We don't have exact fill price, but we can try to get current price
                                    # This is a fallback - ideally filled_avg_price would be stored
                                    try:
                                        current_price = self.get_instrument_current_price(order.symbol)
                                        if current_price:
                                            transaction.open_price = current_price
                                            has_changes = True
                                            break
                                    except:
                                        pass
                        
                        logger.debug(f"Transaction {transaction.id} has filled market entry order, marking as OPENED")
                        has_changes = True
                    
                    # OPENED -> CLOSED: If we have a filled closing order (TP/SL)
                    if filled_closing_orders and transaction.status == TransactionStatus.OPENED:
                        new_status = TransactionStatus.CLOSED
                        transaction.close_date = datetime.now(timezone.utc)
                        
                        # Set close_price from the first filled closing order
                        if not transaction.close_price:
                            closing_order = filled_closing_orders[0]  # Use first filled closing order
                            if closing_order.limit_price:
                                transaction.close_price = closing_order.limit_price
                            else:
                                # Fallback to current price for market orders
                                try:
                                    current_price = self.get_instrument_current_price(closing_order.symbol)
                                    if current_price:
                                        transaction.close_price = current_price
                                except:
                                    pass
                        
                        logger.debug(f"Transaction {transaction.id} has filled closing order, marking as CLOSED")
                        has_changes = True
                    
                    # WAITING -> CLOSED: If all market entry orders are in terminal state without execution
                    # This handles canceled/rejected/error orders before the transaction ever opened
                    elif all_entry_orders_terminal and transaction.status == TransactionStatus.WAITING and not has_filled_entry_order:
                        new_status = TransactionStatus.CLOSED
                        logger.debug(f"Transaction {transaction.id} all market entry orders terminal without execution, marking as CLOSED")
                        has_changes = True
                    
                    # OPENED -> CLOSED: If all market entry orders are in terminal state after opening
                    # This handles cases where the position was closed via broker or manual intervention
                    elif all_entry_orders_terminal and transaction.status == TransactionStatus.OPENED and not filled_closing_orders:
                        # Only close if we don't have any active TP/SL orders waiting
                        active_dependent_orders = [o for o in dependent_orders if o.status not in terminal_statuses]
                        if not active_dependent_orders:
                            new_status = TransactionStatus.CLOSED
                            transaction.close_date = datetime.now(timezone.utc)
                            logger.debug(f"Transaction {transaction.id} all market entry orders terminal after opening, marking as CLOSED")
                            has_changes = True
                    
                    # Update transaction status if changed
                    if new_status and new_status != original_status:
                        transaction.status = new_status
                        session.add(transaction)
                        updated_count += 1
                        logger.info(f"Updated transaction {transaction.id} status: {original_status.value} -> {new_status.value}")
                    elif has_changes:
                        # Even if status didn't change, we may have updated prices
                        session.add(transaction)
                        updated_count += 1
                
                session.commit()
            
            logger.info(f"Successfully refreshed transactions for account {self.id}: {updated_count} transactions updated")
            return True
            
        except Exception as e:
            logger.error(f"Error refreshing transactions for account {self.id}: {e}", exc_info=True)
            return False
