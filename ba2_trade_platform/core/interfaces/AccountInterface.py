from abc import abstractmethod
from typing import Any, Dict, Optional, List
from unittest import result
from datetime import datetime, timezone
from threading import Lock
import time
from ba2_trade_platform.logger import logger
from ...core.models import AccountSetting, TradingOrder, Transaction, ExpertRecommendation, ExpertInstance
from ...core.types import OrderOpenType, OrderDirection, OrderType, OrderStatus, TransactionStatus
from .ExtendableSettingsInterface import ExtendableSettingsInterface
from ...core.db import add_instance, get_instance, update_instance


class AccountInterface(ExtendableSettingsInterface):
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"
    
    # Class-level price cache shared across all instances
    # Structure: {account_id: {symbol: {'price': float, 'timestamp': datetime, 'fetching': bool}}}
    _GLOBAL_PRICE_CACHE: Dict[int, Dict[str, Dict[str, Any]]] = {}
    _CACHE_LOCK = Lock()  # Thread-safe access to cache structure
    
    # Per-symbol locks to prevent duplicate API calls for the same symbol
    # Structure: {(account_id, symbol): Lock}
    _SYMBOL_LOCKS: Dict[tuple, Lock] = {}
    _SYMBOL_LOCKS_LOCK = Lock()  # Lock for managing the locks dict itself
    
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
        # Initialize settings cache to None (will be loaded on first access)
        self._settings_cache = None
        # Ensure this account has an entry in the global cache
        with self._CACHE_LOCK:
            if self.id not in self._GLOBAL_PRICE_CACHE:
                self._GLOBAL_PRICE_CACHE[self.id] = {}




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
    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        """
        Check if multiple symbols exist and are tradeable on this account's broker.
        
        This method checks if the given symbols are valid and tradeable on the broker.
        It should be implemented by each account provider to validate symbols
        against the broker's available instruments.
        
        Args:
            symbols (List[str]): List of stock symbols to check (e.g., ['AAPL', 'MSFT', 'BRK.B'])
        
        Returns:
            Dict[str, bool]: Dictionary mapping each symbol to True if it exists and is tradeable,
                           False otherwise. Example: {'AAPL': True, 'BRK.B': False, 'MSFT': True}
        """
        pass

    def filter_supported_symbols(self, symbols: List[str], log_prefix: str = "") -> List[str]:
        """
        Filter a list of symbols to only include those supported by the broker.
        
        Convenience method that uses symbols_exist() to check which symbols are tradeable
        and returns only the supported ones. Logs a warning for any unsupported symbols.
        
        Args:
            symbols (List[str]): List of stock symbols to filter
            log_prefix (str): Optional prefix for log messages (e.g., expert name)
        
        Returns:
            List[str]: List of symbols that are supported/tradeable on this broker
        """
        if not symbols:
            return []
        
        # Check all symbols at once
        existence_map = self.symbols_exist(symbols)
        
        # Separate supported and unsupported
        supported = [s for s in symbols if existence_map.get(s, False)]
        unsupported = [s for s in symbols if not existence_map.get(s, False)]
        
        # Log warning for unsupported symbols
        if unsupported:
            prefix = f"[{log_prefix}] " if log_prefix else ""
            logger.warning(f"{prefix}Filtered out {len(unsupported)} unsupported symbols: {unsupported}")
        
        if supported:
            logger.debug(f"Keeping {len(supported)} supported symbols: {supported}")
        
        return supported

    @abstractmethod
    def _submit_order_impl(self, trading_order, tp_price: Optional[float] = None, sl_price: Optional[float] = None) -> Any:
        """
        Internal implementation of order submission. This method should be implemented by child classes.
        The public submit_order method will call this after validation.

        Args:
            trading_order: A validated TradingOrder object containing all order details.
            tp_price: Optional take profit price for bracket orders (broker-specific support).
            sl_price: Optional stop loss price for bracket orders (broker-specific support).

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

    def submit_order(self, trading_order: TradingOrder, tp_price: Optional[float] = None, sl_price: Optional[float] = None, is_closing_order: bool = False) -> Any:
        """
        Submit a new order to the account with validation and transaction handling.

        For market orders without transaction_id: automatically creates a new Transaction
        For all other order types: requires existing transaction_id or raises exception

        Args:
            trading_order: A TradingOrder object containing all order details.
            tp_price: Optional take profit price. If provided, TP order will be set after successful submission.
            sl_price: Optional stop loss price. If provided, SL order will be set after successful submission.
            is_closing_order: If True, skip position size validation (for closing existing positions).

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        # Validate the trading order before submission
        validation_result = self._validate_trading_order(trading_order, is_closing_order=is_closing_order)
        if not validation_result['is_valid']:
            error_msg = f"Order validation failed: {', '.join(validation_result['errors'])}"
            logger.error(f"Order validation failed for order: {error_msg}")
            raise ValueError(error_msg)
        
        # Track if this order is being added to an existing transaction (for quantity recalculation)
        was_existing_transaction = (hasattr(trading_order, 'transaction_id') and 
                                    trading_order.transaction_id is not None)
        
        # Handle transaction requirements based on order type
        self._handle_transaction_requirements(trading_order)
        
        # Sync quantity with parent order ONLY for TP/SL orders whose parent is also a limit/stop order
        # TP/SL orders should match their entry order's quantity to close the full position
        # BUT: TP/SL orders triggered by close orders (MARKET) should keep their own quantity
        # Example: Partial close of 4 shares → new TP/SL for remaining 1 share (don't sync with close qty)
        if (trading_order.depends_on_order is not None and 
            trading_order.order_type in [OrderType.SELL_LIMIT, OrderType.BUY_LIMIT, OrderType.SELL_STOP, OrderType.BUY_STOP]):
            try:
                parent_order = get_instance(TradingOrder, trading_order.depends_on_order)
                # Only sync if parent is NOT a market order (market orders are typically close orders)
                if (parent_order and parent_order.quantity and 
                    parent_order.order_type != OrderType.MARKET):
                    if trading_order.quantity != parent_order.quantity:
                        old_qty = trading_order.quantity
                        trading_order.quantity = parent_order.quantity
                        logger.info(
                            f"Synced TP/SL order quantity with parent entry order: "
                            f"order {trading_order.id or 'new'} qty {old_qty} → {parent_order.quantity} "
                            f"(parent order {parent_order.id}, type {parent_order.order_type})"
                        )
                elif parent_order and parent_order.order_type == OrderType.MARKET:
                    logger.debug(
                        f"TP/SL order {trading_order.id or 'new'} parent is MARKET order "
                        f"(likely close order) - keeping independent quantity {trading_order.quantity}"
                    )
                else:
                    logger.warning(
                        f"Parent order {trading_order.depends_on_order} not found or has no quantity "
                        f"for TP/SL order {trading_order.id or 'new'}"
                    )
            except Exception as e:
                logger.error(f"Error syncing TP/SL quantity with parent order: {e}", exc_info=True)
        
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
        from ..db import add_instance, update_instance
        
        if not trading_order.id:
            # Save to database - object will be expunged and can be used like a normal Pydantic object
            order_id = add_instance(trading_order, expunge_after_flush=True)
            logger.debug(f"Created order {order_id} in database before broker submission")
        else:
            # Order already exists - update it to persist transaction_id and other changes
            update_instance(trading_order)
            logger.debug(f"Updated existing order {trading_order.id} in database with transaction_id={trading_order.transaction_id}")
        
        # Log successful validation (using captured values to avoid any potential issues)
        logger.info(f"Order validation passed for {symbol} - {side.value} {quantity} @ {order_type.value}")
        
        # Call the child class implementation (this will update the order with broker_order_id)
        # Pass tp_price and sl_price for brokers that support bracket orders
        # The trading_order object is now detached but all attributes are accessible
        result = self._submit_order_impl(trading_order, tp_price=tp_price, sl_price=sl_price)
        
        # Set TP and/or SL if provided and order was successfully submitted
        # Use adjust methods which create OCO/OTO orders (avoids code duplication)
        # The skip logic in adjust_tp_sl will prevent redundant calls if caller calls again
        if result and result.transaction_id:
            transaction = get_instance(Transaction, result.transaction_id)
            if transaction:
                if tp_price and sl_price:
                    # Both TP and SL provided - use adjust_tp_sl for OCO order
                    logger.debug(f"Creating TP/SL orders for transaction {transaction.id} via adjust_tp_sl")
                    try:
                        self.adjust_tp_sl(transaction, tp_price, sl_price)
                    except NotImplementedError:
                        logger.warning(f"Broker {self.__class__.__name__} does not implement adjust_tp_sl - TP/SL not set")
                elif tp_price:
                    # Only TP provided - use adjust_tp for OTO order
                    logger.debug(f"Creating TP order for transaction {transaction.id} via adjust_tp")
                    try:
                        self.adjust_tp(transaction, tp_price)
                    except NotImplementedError:
                        logger.warning(f"Broker {self.__class__.__name__} does not implement adjust_tp - TP not set")
                elif sl_price:
                    # Only SL provided - use adjust_sl for OTO order
                    logger.debug(f"Creating SL order for transaction {transaction.id} via adjust_sl")
                    try:
                        self.adjust_sl(transaction, sl_price)
                    except NotImplementedError:
                        logger.warning(f"Broker {self.__class__.__name__} does not implement adjust_sl - SL not set")
        
        # Recalculate transaction quantity if order was added to existing transaction
        # This ensures transaction.quantity reflects sum of ALL market entry orders
        if result and result.transaction_id and was_existing_transaction:
            # Only recalculate for market entry orders (not TP/SL dependent orders)
            if not trading_order.depends_on_order:
                self._recalculate_transaction_quantity(result.transaction_id)
        
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
                from ..models import ExpertRecommendation
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
            
            # Log activity using centralized helper
            from ..utils import log_transaction_created_activity
            log_transaction_created_activity(
                trading_order=trading_order,
                account_id=self.id,
                transaction_id=transaction_id,
                expert_id=expert_id,
                current_price=current_price,
                success=True
            )
            
        except Exception as e:
            logger.error(f"Error creating transaction for order: {e}", exc_info=True)
            
            # Log activity for transaction creation failure using centralized helper
            from ..utils import log_transaction_created_activity
            log_transaction_created_activity(
                trading_order=trading_order,
                account_id=self.id,
                expert_id=expert_id if 'expert_id' in locals() else None,
                success=False,
                error_message=str(e)
            )
            
            raise ValueError(f"Failed to create transaction for order: {e}")
    
    def _recalculate_transaction_quantity(self, transaction_id: int) -> None:
        """
        Recalculate and update transaction quantity from all its market entry orders.
        
        This is called after adding orders to existing transactions to ensure
        transaction.quantity reflects the sum of all linked market entry orders.
        
        For BUY transactions: sum only BUY orders (not canceled/rejected/expired)
        For SELL transactions: sum only SELL orders (not canceled/rejected/expired)
        
        Args:
            transaction_id: The ID of the transaction to recalculate
        """
        try:
            from sqlmodel import select
            from ..db import get_db, update_instance, get_instance
            from ..models import Transaction
            
            transaction = get_instance(Transaction, transaction_id)
            if not transaction:
                logger.warning(f"Transaction {transaction_id} not found for quantity recalculation")
                return
            
            # Determine transaction direction from current quantity sign
            # Positive = BUY transaction, Negative = SELL transaction
            is_buy_transaction = (transaction.quantity or 0) >= 0
            target_side = OrderDirection.BUY if is_buy_transaction else OrderDirection.SELL
            
            # Statuses to exclude from quantity calculation
            excluded_statuses = [
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
                OrderStatus.ERROR
            ]
            
            with get_db() as session:
                # Get all market entry orders (no depends_on_order) matching transaction direction
                statement = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.account_id == self.id,
                    TradingOrder.depends_on_order.is_(None),  # Market entry orders only
                    TradingOrder.side == target_side  # Only orders matching transaction direction
                )
                market_entry_orders = session.exec(statement).all()
                
                if not market_entry_orders:
                    logger.debug(f"No market entry orders found for transaction {transaction_id}")
                    return
                
                # Calculate total quantity from non-canceled market entry orders
                total_quantity = 0.0
                valid_count = 0
                for order in market_entry_orders:
                    # Skip orders with excluded statuses
                    if order.status in excluded_statuses:
                        continue
                    qty = float(order.quantity) if order.quantity else 0.0
                    total_quantity += qty
                    valid_count += 1
                
                # For SELL transactions, quantity is stored as negative
                if not is_buy_transaction:
                    total_quantity = -total_quantity
                
                # Only update if quantity has changed
                if transaction.quantity != total_quantity:
                    old_qty = transaction.quantity
                    transaction.quantity = total_quantity
                    update_instance(transaction)
                    logger.info(
                        f"Transaction {transaction_id} quantity recalculated: {old_qty} -> {total_quantity} "
                        f"(from {valid_count} valid market entry orders)"
                    )
                else:
                    logger.debug(f"Transaction {transaction_id} quantity unchanged: {total_quantity}")
                    
        except Exception as e:
            logger.error(f"Error recalculating transaction quantity for {transaction_id}: {e}", exc_info=True)
    
    def _ensure_tp_sl_percent_stored(self, tp_or_sl_order: TradingOrder, parent_order: TradingOrder) -> None:
        """
        Ensure that TP/SL percent is stored in the order.data field.
        If not already stored, calculate it from the limit_price/stop_price and parent's open_price.
        This provides a fallback mechanism if the percent wasn't stored during action evaluation.
        
        Args:
            tp_or_sl_order: The WAITING_TRIGGER TP or SL order
            parent_order: The parent order to calculate percent from
        """
        try:
            from ..types import OrderType
            from ..db import update_instance
            
            # Skip if no data field or already has tp_percent/sl_percent
            if not tp_or_sl_order.data:
                tp_or_sl_order.data = {}
            
            # Ensure "TP_SL" key exists for TP/SL data
            if "TP_SL" not in tp_or_sl_order.data:
                tp_or_sl_order.data["TP_SL"] = {}
            
            # Check if this is a TP order (BUY_LIMIT or SELL_LIMIT)
            if tp_or_sl_order.order_type in [OrderType.BUY_LIMIT, OrderType.SELL_LIMIT]:
                if "tp_percent" not in tp_or_sl_order.data["TP_SL"] or tp_or_sl_order.data["TP_SL"].get("tp_percent") is None:
                    # Calculate TP percent from current limit_price and parent's open_price
                    if parent_order.open_price and parent_order.open_price > 0 and tp_or_sl_order.limit_price:
                        tp_percent = ((tp_or_sl_order.limit_price - parent_order.open_price) / parent_order.open_price) * 100
                        tp_or_sl_order.data["TP_SL"]["tp_percent"] = round(tp_percent, 2)
                        tp_or_sl_order.data["TP_SL"]["parent_filled_price"] = parent_order.open_price
                        tp_or_sl_order.data["TP_SL"]["type"] = "tp"
                        update_instance(tp_or_sl_order)
                        logger.info(
                            f"Calculated and stored TP percent for order {tp_or_sl_order.id}: "
                            f"{tp_percent:.2f}% (parent filled ${parent_order.open_price:.2f} → TP target ${tp_or_sl_order.limit_price:.2f}) - FALLBACK calculation"
                        )
                    else:
                        logger.warning(
                            f"Cannot calculate TP percent for order {tp_or_sl_order.id}: "
                            f"parent open_price=${parent_order.open_price}, tp limit_price=${tp_or_sl_order.limit_price}"
                        )
            
            # Check if this is an SL order (BUY_STOP or SELL_STOP)
            elif tp_or_sl_order.order_type in [OrderType.BUY_STOP, OrderType.SELL_STOP]:
                if "sl_percent" not in tp_or_sl_order.data["TP_SL"] or tp_or_sl_order.data["TP_SL"].get("sl_percent") is None:
                    # Calculate SL percent from current stop_price and parent's open_price
                    if parent_order.open_price and parent_order.open_price > 0 and tp_or_sl_order.stop_price:
                        sl_percent = ((tp_or_sl_order.stop_price - parent_order.open_price) / parent_order.open_price) * 100
                        tp_or_sl_order.data["TP_SL"]["sl_percent"] = round(sl_percent, 2)
                        tp_or_sl_order.data["TP_SL"]["parent_filled_price"] = parent_order.open_price
                        tp_or_sl_order.data["TP_SL"]["type"] = "sl"
                        update_instance(tp_or_sl_order)
                        logger.info(
                            f"Calculated and stored SL percent for order {tp_or_sl_order.id}: "
                            f"{sl_percent:.2f}% (parent filled ${parent_order.open_price:.2f} → SL target ${tp_or_sl_order.stop_price:.2f}) - FALLBACK calculation"
                        )
                    else:
                        logger.warning(
                            f"Cannot calculate SL percent for order {tp_or_sl_order.id}: "
                            f"parent open_price=${parent_order.open_price}, sl stop_price=${tp_or_sl_order.stop_price}"
                        )
        
        except Exception as e:
            logger.warning(f"Error ensuring TP/SL percent stored for order {tp_or_sl_order.id}: {e}")
    
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
                    # Ensure TP percent is stored in the order data (fallback calculation if not already set)
                    # This will be used when the WAITING_TRIGGER order is triggered
                    self._ensure_tp_sl_percent_stored(trading_order, trading_order)  # Pass same order since we're calculating from current state
                    
                    # Call the implementation method directly to create TP order at broker
                    # Skip the check for PENDING status since order is now submitted
                    from ..types import OrderStatus
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
                    # Ensure SL percent is stored in the order data (fallback calculation if not already set)
                    # This will be used when the WAITING_TRIGGER order is triggered
                    self._ensure_tp_sl_percent_stored(trading_order, trading_order)  # Pass same order since we're calculating from current state
                    
                    # Call the implementation method if it exists
                    if hasattr(self, '_set_order_sl_impl'):
                        from ..types import OrderStatus
                        original_status = trading_order.status
                        trading_order.status = OrderStatus.SUBMITTED
                        self._set_order_sl_impl(trading_order, transaction.stop_loss)
                        trading_order.status = original_status
                        logger.info(f"Successfully submitted SL order to broker")
                except Exception as sl_error:
                    logger.error(f"Failed to submit SL order to broker: {sl_error}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"Error submitting pending TP/SL orders: {e}", exc_info=True)

    def _validate_trading_order(self, trading_order: TradingOrder, is_closing_order: bool = False) -> Dict[str, Any]:
        """
        Validate a trading order before submission.
        
        Args:
            trading_order: The TradingOrder object to validate
            is_closing_order: If True, skip position size validation
            
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
        
        # Validate position size limits for market orders with expert_id
        # This provides defense-in-depth validation at the account interface level
        # Skip validation for closing orders (exiting existing positions)
        if (not is_closing_order and
            hasattr(trading_order, 'order_type') and 
            trading_order.order_type == OrderType.MARKET and
            hasattr(trading_order, 'transaction_id') and trading_order.transaction_id):
            
            position_size_errors = self._validate_position_size_limits(trading_order)
            if position_size_errors:
                errors.extend(position_size_errors)
                
        return {
            'is_valid': len(errors) == 0,
            'errors': errors
        }

    def _validate_position_size_limits(self, trading_order: TradingOrder) -> List[str]:
        """
        Validate that the order respects expert position size limits (defense-in-depth).
        
        This provides a safety check at the account interface level to prevent any code path
        from bypassing position size limits set in expert settings.
        
        Args:
            trading_order: The TradingOrder object to validate
            
        Returns:
            List[str]: List of validation error messages (empty if valid)
        """
        errors = []
        
        try:
            # Get the transaction to find the expert_id
            from ..db import get_instance
            from ..models import Transaction, ExpertInstance, ExpertSetting
            
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction or not transaction.expert_id:
                # No expert associated - skip expert-specific validation
                return errors
            
            # Get the expert instance
            expert_instance = get_instance(ExpertInstance, transaction.expert_id)
            if not expert_instance:
                logger.warning(f"Expert instance {transaction.expert_id} not found for transaction {transaction.id}")
                return errors
            
            # Get expert settings
            # Load expert class to access settings
            expert_module_name = f"ba2_trade_platform.modules.experts.{expert_instance.expert}"
            try:
                import importlib
                expert_module = importlib.import_module(expert_module_name)
                expert_class = getattr(expert_module, expert_instance.expert)
                
                # Get settings using the ExtendableSettingsInterface pattern
                # Note: We can't use self.get_settings() because self is the AccountInterface
                # We need to instantiate a temporary helper to access expert settings
                from ..models import ExpertSetting
                from sqlmodel import Session, select
                from ..db import get_db
                
                # Manually load expert settings from database
                with get_db() as session:
                    expert_settings_rows = session.exec(
                        select(ExpertSetting).where(ExpertSetting.instance_id == expert_instance.id)
                    ).all()
                    
                    # Build settings dict
                    settings = {}
                    for setting_row in expert_settings_rows:
                        if setting_row.value_float is not None:
                            settings[setting_row.key] = setting_row.value_float
                        elif setting_row.value_str is not None:
                            settings[setting_row.key] = setting_row.value_str
                        elif setting_row.value_json:
                            settings[setting_row.key] = setting_row.value_json
                
            except (ImportError, AttributeError) as e:
                logger.warning(f"Could not load expert {expert_instance.expert} for position size validation: {e}")
                return errors
            
            # Get position size limit setting
            max_position_pct = settings.get("max_virtual_equity_per_instrument_percent")
            if max_position_pct is None:
                # Setting not defined - skip validation
                return errors
            
            # Calculate expert's virtual equity
            account_info = self.get_account_info()
            if not account_info:
                logger.warning("Could not get account info for position size validation")
                return errors
            
            account_equity = float(account_info.equity)
            virtual_equity_pct = expert_instance.virtual_equity_pct
            virtual_equity = account_equity * (virtual_equity_pct / 100.0)
            
            # Calculate max position value
            max_position_value = virtual_equity * (max_position_pct / 100.0)
            
            # Calculate order position value
            current_price = self.get_instrument_current_price(trading_order.symbol)
            if current_price is None:
                logger.warning(f"Could not get current price for {trading_order.symbol} in position size validation")
                return errors
            
            position_value = current_price * trading_order.quantity
            
            # Check if position exceeds limit
            if position_value > max_position_value:
                errors.append(
                    f"Position size ${position_value:.2f} exceeds expert's max allowed ${max_position_value:.2f} "
                    f"({max_position_pct:.1f}% of virtual equity ${virtual_equity:.2f}). "
                    f"Reduce quantity to {int(max_position_value / current_price)} or less."
                )
                logger.error(
                    f"POSITION SIZE LIMIT EXCEEDED: Order for {trading_order.quantity} shares of {trading_order.symbol} "
                    f"(${position_value:.2f}) exceeds expert {expert_instance.id} limit of ${max_position_value:.2f}"
                )
                
        except Exception as e:
            # Don't fail the entire validation if position size check has an error
            # Just log it and continue
            logger.warning(f"Error during position size validation: {e}")
        
        return errors

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
    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        """
        Internal implementation of price fetching. This method should be implemented by child classes.
        This is called by get_instrument_current_price() when cache is stale or missing.
        
        Args:
            symbol_or_symbols (Union[str, List[str]]): Single symbol or list of symbols to fetch prices for
            price_type (str): Type of price to fetch - 'bid', 'ask', or 'mid' (default: 'bid')
        
        Returns:
            Union[Optional[float], Dict[str, Optional[float]]]: 
                - If symbol_or_symbols is str: Returns Optional[float] (single price or None)
                - If symbol_or_symbols is List[str]: Returns Dict[str, Optional[float]] (symbol -> price mapping)
        """
        pass
    
    def _get_symbol_lock(self, symbol: str) -> Lock:
        """
        Get or create a lock for a specific symbol to prevent duplicate API calls.
        
        Args:
            symbol (str): The asset symbol
            
        Returns:
            Lock: A lock specific to this account and symbol combination
        """
        lock_key = (self.id, symbol)
        
        with self._SYMBOL_LOCKS_LOCK:
            if lock_key not in self._SYMBOL_LOCKS:
                self._SYMBOL_LOCKS[lock_key] = Lock()
            return self._SYMBOL_LOCKS[lock_key]
    
    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        """
        Get the current market price for instrument(s) with caching. Supports both single and bulk fetching.
        
        This method implements a thread-safe global cache with configurable TTL (PRICE_CACHE_TIME in config.py).
        The cache is shared across all instances of the same account and persists between instance creations.
        
        **Price Type Support:**
        Cache keys include price_type to prevent mixing bid/ask/mid prices.
        
        **Duplicate API Call Prevention:**
        Uses per-symbol locks to ensure only ONE thread fetches a price when multiple threads
        request the same uncached symbol simultaneously. Other threads wait for the first fetch
        to complete and then use the cached value.
        
        **Bulk Fetching:**
        When a list of symbols is provided, this method:
        1. Checks cache for all symbols and identifies which need fetching
        2. Fetches all uncached symbols in a SINGLE API call (if broker supports it)
        3. Updates cache for all newly fetched prices
        4. Returns a dictionary mapping symbols to prices
        
        Args:
            symbol_or_symbols (Union[str, List[str]]): Single symbol or list of symbols to get prices for
            price_type (str): Price type to fetch - 'bid', 'ask', or 'mid' (default: 'bid')
        
        Returns:
            Union[Optional[float], Dict[str, Optional[float]]]:
                - If single symbol (str): Returns Optional[float] (price or None)
                - If list of symbols: Returns Dict[str, Optional[float]] (symbol -> price mapping)
        """
        from ... import config
        
        # Handle backward compatibility - single symbol case
        if isinstance(symbol_or_symbols, str):
            symbol = symbol_or_symbols
            # Create cache key that includes price type to avoid mixing bid/ask/mid
            cache_key = f"{symbol}:{price_type}"
            
            # Quick cache check (no symbol lock needed for cache hits)
            with self._CACHE_LOCK:
                account_cache = self._GLOBAL_PRICE_CACHE.get(self.id, {})
                
                if cache_key in account_cache:
                    cached_data = account_cache[cache_key]
                    cached_time = cached_data['timestamp']
                    current_time = datetime.now(timezone.utc)
                    time_diff = (current_time - cached_time).total_seconds()
                    
                    # If cache is still valid, return immediately
                    if time_diff < config.PRICE_CACHE_TIME:
                        logger.debug(f"[Account {self.id}] Returning cached {price_type} price for {symbol}: ${cached_data['price']} (age: {time_diff:.1f}s)")
                        return cached_data['price']
                    else:
                        logger.debug(f"[Account {self.id}] Cache expired for {cache_key} (age: {time_diff:.1f}s > {config.PRICE_CACHE_TIME}s)")
            
            # Cache miss or expired - need to fetch
            # Use per-symbol lock to prevent duplicate API calls (lock on cache_key to include price_type)
            symbol_lock = self._get_symbol_lock(cache_key)
            
            with symbol_lock:
                # Double-check cache after acquiring lock (another thread may have just fetched it)
                with self._CACHE_LOCK:
                    account_cache = self._GLOBAL_PRICE_CACHE.get(self.id, {})
                    
                    if cache_key in account_cache:
                        cached_data = account_cache[cache_key]
                        cached_time = cached_data['timestamp']
                        current_time = datetime.now(timezone.utc)
                        time_diff = (current_time - cached_time).total_seconds()
                        
                        if time_diff < config.PRICE_CACHE_TIME:
                            logger.debug(f"[Account {self.id}] Another thread cached {cache_key} while waiting: ${cached_data['price']} (age: {time_diff:.1f}s)")
                            return cached_data['price']
                
                # Still need to fetch - we hold the symbol lock, so only this thread will fetch
                logger.debug(f"[Account {self.id}] Fetching fresh {price_type} price for {symbol} (holding symbol lock)")
                price = self._get_instrument_current_price_impl(symbol, price_type=price_type)
                
                # Update cache if we got a valid price
                if price is not None:
                    with self._CACHE_LOCK:
                        if self.id not in self._GLOBAL_PRICE_CACHE:
                            self._GLOBAL_PRICE_CACHE[self.id] = {}
                        self._GLOBAL_PRICE_CACHE[self.id][cache_key] = {
                            'price': price,
                            'timestamp': datetime.now(timezone.utc)
                        }
                        logger.debug(f"[Account {self.id}] Cached new price for {symbol}: ${price}")
                else:
                    logger.warning(f"[Account {self.id}] Failed to fetch price for {symbol}")
                
                return price
        
        # Handle list of symbols - bulk fetching
        elif isinstance(symbol_or_symbols, list):
            symbols = symbol_or_symbols
            current_time = datetime.now(timezone.utc)
            result = {}
            symbols_to_fetch = []
            
            # Check cache for all symbols (with price_type in cache key)
            with self._CACHE_LOCK:
                account_cache = self._GLOBAL_PRICE_CACHE.get(self.id, {})
                
                for symbol in symbols:
                    cache_key = f"{symbol}:{price_type}"
                    if cache_key in account_cache:
                        cached_data = account_cache[cache_key]
                        cached_time = cached_data['timestamp']
                        time_diff = (current_time - cached_time).total_seconds()
                        
                        if time_diff < config.PRICE_CACHE_TIME:
                            # Use cached price
                            result[symbol] = cached_data['price']
                            logger.debug(f"[Account {self.id}] Returning cached {price_type} price for {symbol}: ${cached_data['price']} (age: {time_diff:.1f}s)")
                        else:
                            # Cache expired
                            symbols_to_fetch.append(symbol)
                            logger.debug(f"[Account {self.id}] Cache expired for {cache_key} (age: {time_diff:.1f}s > {config.PRICE_CACHE_TIME}s)")
                    else:
                        # Not in cache
                        symbols_to_fetch.append(symbol)
            
            # Fetch uncached symbols in bulk if any
            if symbols_to_fetch:
                logger.debug(f"[Account {self.id}] Fetching {len(symbols_to_fetch)} symbols in bulk: {symbols_to_fetch}")
                
                # Call implementation with list of symbols (broker-specific bulk fetch)
                fetched_prices = self._get_instrument_current_price_impl(symbols_to_fetch, price_type=price_type)
                
                # Update cache and result with fetched prices
                if fetched_prices:
                    with self._CACHE_LOCK:
                        if self.id not in self._GLOBAL_PRICE_CACHE:
                            self._GLOBAL_PRICE_CACHE[self.id] = {}
                        
                        for symbol, price in fetched_prices.items():
                            cache_key = f"{symbol}:{price_type}"
                            if price is not None:
                                self._GLOBAL_PRICE_CACHE[self.id][cache_key] = {
                                    'price': price,
                                    'timestamp': current_time
                                }
                                result[symbol] = price
                                logger.debug(f"[Account {self.id}] Cached bulk-fetched {price_type} price for {symbol}: ${price}")
                            else:
                                result[symbol] = None
                                logger.warning(f"[Account {self.id}] Failed to fetch {price_type} price for {symbol} in bulk")
                else:
                    # Bulk fetch failed, set all to None
                    for symbol in symbols_to_fetch:
                        result[symbol] = None
                        logger.warning(f"[Account {self.id}] Bulk fetch failed for {symbol}")
            
            return result
        
        else:
            raise TypeError(f"symbol_or_symbols must be str or List[str], got {type(symbol_or_symbols)}")

    @abstractmethod
    def _set_order_tp_impl(self, trading_order: TradingOrder, tp_price: float) -> Any:
        """
        Broker-specific implementation hook for take profit order setting.
        
        This method is called AFTER the base class has:
        - Enforced minimum TP percent
        - Created/updated the WAITING_TRIGGER TP order in the database
        - Updated the transaction's take_profit value
        
        For most brokers, this method can be a no-op (just pass). Override only if your broker
        needs special handling beyond database order creation.
        
        Args:
            trading_order: The original TradingOrder object (for reference/context)
            tp_price: The validated and enforced TP price
            
        Returns:
            Any: Any broker-specific result (optional). Base class will not use this value.
        """
        pass

    def _update_broker_tp_order(self, tp_order: TradingOrder, new_tp_price: float) -> Any:
        """
        Update an already-submitted broker TP order with a new price.
        
        Called when a TP order is already OPEN at the broker (has broker_order_id) and 
        needs to be updated to a new price. Override to implement broker-specific logic
        like cancel+replace or direct order modification.
        
        Default implementation raises NotImplementedError - brokers must override if they
        support updating live orders.
        
        Args:
            tp_order: The TP order TradingOrder object (with broker_order_id set)
            new_tp_price: The new take profit price
            
        Returns:
            Any: Any broker-specific result (optional)
            
        Raises:
            NotImplementedError: If broker doesn't support updating live orders
        """
        raise NotImplementedError(
            f"Broker {self.__class__.__name__} does not support updating live TP orders. "
            f"Must implement _update_broker_tp_order() to support manual TP/SL updates."
        )

    def _update_broker_sl_order(self, sl_order: TradingOrder, new_sl_price: float) -> Any:
        """
        Update an already-submitted broker SL order with a new price.
        
        Called when a SL order is already OPEN at the broker (has broker_order_id) and 
        needs to be updated to a new price. Override to implement broker-specific logic
        like cancel+replace or direct order modification.
        
        Default implementation raises NotImplementedError - brokers must override if they
        support updating live orders.
        
        Args:
            sl_order: The SL order TradingOrder object (with broker_order_id set)
            new_sl_price: The new stop loss price
            
        Returns:
            Any: Any broker-specific result (optional)
            
        Raises:
            NotImplementedError: If broker doesn't support updating live orders
        """
        raise NotImplementedError(
            f"Broker {self.__class__.__name__} does not support updating live SL orders. "
            f"Must implement _update_broker_sl_order() to support manual TP/SL updates."
        )
    
    def adjust_tp(self, transaction: Transaction, new_tp_price: float) -> bool:
        """
        Adjust take profit for a transaction (generic stub - provider implements logic).
        
        This is a stateless operation that should work regardless of current transaction state.
        The provider implementation determines what action to take based on:
        - Current transaction status (WAITING, OPENED, etc.)
        - Existing TP/SL order states (PENDING, ACCEPTED, FILLED, etc.)
        - Broker capabilities (OCO/OTO support, order modification, etc.)
        
        Source of truth: transaction.take_profit is updated first, then orders are adjusted.
        
        Args:
            transaction: Transaction object to adjust TP for
            new_tp_price: New take profit price
            
        Returns:
            bool: True if adjustment succeeded, False otherwise
            
        Raises:
            NotImplementedError: Provider must implement this method
        """
        raise NotImplementedError(
            f"Broker {self.__class__.__name__} must implement adjust_tp() for TP/SL management."
        )
    
    def adjust_sl(self, transaction: Transaction, new_sl_price: float) -> bool:
        """
        Adjust stop loss for a transaction (generic stub - provider implements logic).
        
        This is a stateless operation that should work regardless of current transaction state.
        The provider implementation determines what action to take based on:
        - Current transaction status (WAITING, OPENED, etc.)
        - Existing TP/SL order states (PENDING, ACCEPTED, FILLED, etc.)
        - Broker capabilities (OCO/OTO support, order modification, etc.)
        
        Source of truth: transaction.stop_loss is updated first, then orders are adjusted.
        
        Args:
            transaction: Transaction object to adjust SL for
            new_sl_price: New stop loss price
            
        Returns:
            bool: True if adjustment succeeded, False otherwise
            
        Raises:
            NotImplementedError: Provider must implement this method
        """
        raise NotImplementedError(
            f"Broker {self.__class__.__name__} must implement adjust_sl() for TP/SL management."
        )
    
    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: float, new_sl_price: float) -> bool:
        """
        Adjust both take profit and stop loss for a transaction (generic stub - provider implements logic).
        
        This is a stateless operation that should work regardless of current transaction state.
        The provider implementation determines what action to take based on:
        - Current transaction status (WAITING, OPENED, etc.)
        - Existing TP/SL order states (PENDING, ACCEPTED, FILLED, etc.)
        - Broker capabilities (OCO/OTO support, order modification, etc.)
        
        Source of truth: transaction.take_profit and transaction.stop_loss are updated first,
        then orders are adjusted (using OCO if both defined, OTO if only one).
        
        Args:
            transaction: Transaction object to adjust TP/SL for
            new_tp_price: New take profit price
            new_sl_price: New stop loss price
            
        Returns:
            bool: True if adjustment succeeded, False otherwise
            
        Raises:
            NotImplementedError: Provider must implement this method
        """
        raise NotImplementedError(
            f"Broker {self.__class__.__name__} must implement adjust_tp_sl() for TP/SL management."
        )

    def set_order_tp(self, trading_order: TradingOrder, tp_price: float) -> TradingOrder:
        """
        Set take profit for an existing order.
        
        This method:
        1. Enforces minimum TP percent to protect profitability
        2. Creates or updates a WAITING_TRIGGER TP order in the database
        3. Updates the linked transaction's take_profit value
        4. Calls broker-specific implementation (if needed)
        
        Args:
            trading_order: The original TradingOrder object
            tp_price: The take profit price (may be adjusted upward if below minimum)
            
        Returns:
            TradingOrder: The created/updated take profit order object
        """
        try:
            from sqlmodel import Session
            from ..db import get_db
            
            # Validate inputs
            if not trading_order:
                raise ValueError("trading_order cannot be None")
            if not isinstance(tp_price, (int, float)) or tp_price <= 0:
                raise ValueError("tp_price must be a positive number")
            
            # Enforce minimum TP percent based on open price
            # This is a safety check in case market price slipped after TradeAction was issued
            if trading_order.open_price:
                from ...config import get_min_tp_sl_percent
                min_tp_percent = get_min_tp_sl_percent()
                
                open_price = float(trading_order.open_price)
                is_long = (trading_order.side.upper() == "BUY")
                
                original_tp = tp_price
                
                if is_long:
                    # For LONG: TP should be above open, profit percent = (TP - Open) / Open * 100
                    actual_percent = ((tp_price - open_price) / open_price) * 100
                    
                    if actual_percent < min_tp_percent:
                        # Enforce minimum by adjusting TP upward
                        tp_price = open_price * (1 + min_tp_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] TP enforcement (LONG): Profit {actual_percent:.2f}% below minimum {min_tp_percent}%. "
                            f"Adjusting TP from ${original_tp:.2f} to ${tp_price:.2f} (open: ${open_price:.2f})"
                        )
                else:
                    # For SHORT: TP should be below open, profit percent = (Open - TP) / Open * 100
                    actual_percent = ((open_price - tp_price) / open_price) * 100
                    
                    if actual_percent < min_tp_percent:
                        # Enforce minimum by adjusting TP downward
                        tp_price = open_price * (1 - min_tp_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] TP enforcement (SHORT): Profit {actual_percent:.2f}% below minimum {min_tp_percent}%. "
                            f"Adjusting TP from ${original_tp:.2f} to ${tp_price:.2f} (open: ${open_price:.2f})"
                        )
            
            # Get the linked transaction
            if not trading_order.transaction_id:
                raise ValueError("Order must have a linked transaction to set take profit")
            
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction:
                raise ValueError(f"Transaction {trading_order.transaction_id} not found")
            
            # Store original transaction take_profit for rollback if broker update fails
            original_transaction_tp = transaction.take_profit
            
            # Update transaction's take_profit value
            transaction.take_profit = tp_price
            update_instance(transaction)
            
            logger.info(f"Updated transaction {transaction.id} take_profit to ${tp_price}")
            
            # Create or update the TP order in the database
            with Session(get_db().bind) as session:
                # Calculate TP percent from open price (for storage in order.data)
                tp_percent = None
                if trading_order.open_price and trading_order.open_price > 0:
                    tp_percent = ((tp_price - trading_order.open_price) / trading_order.open_price) * 100
                    logger.debug(f"Calculated TP percent: {tp_percent:.2f}% for {trading_order.symbol}")
                
                # Check if there's already a take profit order for this transaction
                # Look for any TP order in the transaction that is still active (not terminal)
                from sqlmodel import select
                existing_tp_order = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == trading_order.transaction_id,
                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                        TradingOrder.limit_price.isnot(None),
                        TradingOrder.stop_price.is_(None)  # Ensure it's a limit order (TP), not a stop order (SL)
                    )
                ).first()
                
                if existing_tp_order:
                    # Store original values for rollback if broker update fails
                    original_limit_price = existing_tp_order.limit_price
                    original_data = existing_tp_order.data.copy() if existing_tp_order.data else None
                    
                    # Update existing TP order
                    logger.info(f"Updating existing TP order {existing_tp_order.id} to price ${tp_price}")
                    existing_tp_order.limit_price = tp_price
                    
                    if tp_percent is not None:
                        if not existing_tp_order.data:
                            existing_tp_order.data = {}
                        existing_tp_order.data['tp_percent_target'] = round(tp_percent, 2)
                        existing_tp_order.data['tp_reference_price'] = round(trading_order.open_price, 2)
                    
                    # Check if parent order is now EXECUTED (FILLED/PARTIALLY_FILLED) and TP order is still WAITING_TRIGGER
                    # If so, submit the TP order immediately to broker
                    should_submit_immediately = (
                        trading_order.status in OrderStatus.get_executed_statuses() and
                        existing_tp_order.status == OrderStatus.WAITING_TRIGGER
                    )
                    
                    if should_submit_immediately:
                        logger.info(f"Parent order {trading_order.id} is now EXECUTED (status: {trading_order.status.value}), submitting TP order {existing_tp_order.id} to broker")
                        
                        # CRITICAL: Validate parent order has valid quantity before updating TP order quantity
                        # This prevents submitting TP orders with qty=0 when entry order has qty=0
                        if not trading_order.quantity or trading_order.quantity <= 0:
                            logger.error(f"Cannot submit TP order {existing_tp_order.id}: parent order {trading_order.id} has invalid quantity {trading_order.quantity}. Skipping TP submission.")
                            # Mark TP order as ERROR instead of submitting
                            existing_tp_order.status = OrderStatus.ERROR
                            session.add(existing_tp_order)
                            session.commit()
                            raise ValueError(f"Parent order {trading_order.id} has invalid quantity {trading_order.quantity}, cannot create TP order")
                        
                        existing_tp_order.quantity = trading_order.quantity
                        # Don't set to PENDING - submit directly and let submit_order set the status
                    
                    session.add(existing_tp_order)
                    session.commit()
                    session.refresh(existing_tp_order)
                    tp_order = existing_tp_order
                    
                    # Check if the TP order is already submitted at broker (has broker_order_id and not in terminal state)
                    # If so, we need to update the actual broker order, not just the database record
                    if existing_tp_order.broker_order_id and existing_tp_order.status not in OrderStatus.get_terminal_statuses():
                        logger.info(f"TP order {existing_tp_order.id} is active at broker with ID {existing_tp_order.broker_order_id} (status: {existing_tp_order.status.value}), calling broker update")
                        # Call the broker-specific update method
                        # Note: This may need to be implemented in broker-specific classes to cancel/replace
                        try:
                            self._update_broker_tp_order(existing_tp_order, tp_price)
                        except NotImplementedError:
                            logger.warning(f"Broker {self.__class__.__name__} does not support updating live TP orders - may need to cancel and replace")
                        except Exception as e:
                            logger.error(f"Error updating broker TP order: {e}", exc_info=True)
                            # ROLLBACK: Restore original values in database
                            logger.warning(f"Rolling back TP order {existing_tp_order.id} to original limit_price ${original_limit_price}")
                            existing_tp_order.limit_price = original_limit_price
                            existing_tp_order.data = original_data
                            session.add(existing_tp_order)
                            session.commit()
                            
                            # ROLLBACK: Restore transaction take_profit
                            logger.warning(f"Rolling back transaction {transaction.id} take_profit to ${original_transaction_tp}")
                            transaction.take_profit = original_transaction_tp
                            update_instance(transaction)
                            raise
                    elif should_submit_immediately:
                        # Parent order is active and TP was WAITING_TRIGGER, submit it now
                        logger.info(f"Submitting TP order {existing_tp_order.id} to broker now that parent order {trading_order.id} is active")
                        session.close()
                        try:
                            result = self.submit_order(existing_tp_order)
                            logger.info(f"Successfully submitted TP order {existing_tp_order.id} to broker (broker_order_id: {existing_tp_order.broker_order_id})")
                        except Exception as submit_error:
                            logger.error(f"Error submitting TP order {existing_tp_order.id} to broker: {submit_error}", exc_info=True)
                            raise
                        # Reopen session for return
                        session = Session(get_db().bind)
                    
                    logger.info(f"Successfully updated TP order {tp_order.id} to ${tp_price}")
                    
                    # Log activity for TP change using centralized helper
                    from ..utils import log_tp_sl_adjustment_activity
                    log_tp_sl_adjustment_activity(
                        trading_order=trading_order,
                        account_id=self.id,
                        adjustment_type="tp",
                        new_price=tp_price,
                        percent=tp_percent,
                        order_id=existing_tp_order.id,
                        success=True
                    )
                else:
                    # Create new TP order
                    if trading_order.side.upper() == "BUY":
                        tp_side = OrderDirection.SELL
                        from ..types import OrderType as CoreOrderType
                        tp_order_type = CoreOrderType.SELL_LIMIT
                    else:
                        tp_side = OrderDirection.BUY
                        from ..types import OrderType as CoreOrderType
                        tp_order_type = CoreOrderType.BUY_LIMIT
                    
                    # Build order data with TP metadata
                    order_data = {}
                    if tp_percent is not None:
                        order_data = {
                            'tp_percent_target': round(tp_percent, 2),
                            'tp_reference_price': round(trading_order.open_price, 2)
                        }
                    
                    # Only submit TP immediately if parent order is already FILLED
                    # If parent is still PENDING/ACCEPTED, use WAITING_TRIGGER to avoid wash trade errors
                    should_submit_immediately = trading_order.status == OrderStatus.FILLED
                    
                    tp_order = TradingOrder(
                        account_id=self.id,
                        symbol=trading_order.symbol,
                        quantity=trading_order.quantity if should_submit_immediately else 0,  # Set quantity only if submitting
                        side=tp_side,
                        order_type=tp_order_type,
                        limit_price=tp_price,
                        transaction_id=trading_order.transaction_id,
                        status=OrderStatus.PENDING if should_submit_immediately else OrderStatus.WAITING_TRIGGER,
                        depends_on_order=trading_order.id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        expert_recommendation_id=trading_order.expert_recommendation_id,
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=f"TP for order {trading_order.id}",
                        data=order_data if order_data else None,
                        created_at=datetime.now(timezone.utc)
                    )
                    
                    session.add(tp_order)
                    session.commit()
                    session.refresh(tp_order)
                    
                    if should_submit_immediately:
                        logger.info(f"Created PENDING TP order {tp_order.id} at ${tp_price} - parent order {trading_order.id} already FILLED, will submit to broker now")
                        # Submit immediately to broker
                        session.close()
                        try:
                            result = self.submit_order(tp_order)
                            logger.info(f"Successfully submitted TP order {tp_order.id} to broker (broker_order_id: {tp_order.broker_order_id})")
                        except Exception as submit_error:
                            logger.error(f"Error submitting TP order {tp_order.id} to broker: {submit_error}", exc_info=True)
                            raise
                        # Reopen session for return
                        session = Session(get_db().bind)
                    else:
                        logger.info(f"Created WAITING_TRIGGER TP order {tp_order.id} at ${tp_price} (will submit when order {trading_order.id} is FILLED)")
            
            # Call broker-specific implementation (may be no-op for most brokers)
            self._set_order_tp_impl(trading_order, tp_price)
            
            return tp_order
            
        except Exception as e:
            logger.error(f"Error setting take profit for order {trading_order.id if trading_order else 'None'}: {e}", exc_info=True)
            
            # Log activity for TP adjustment failure using centralized helper
            from ..utils import log_tp_sl_adjustment_activity
            log_tp_sl_adjustment_activity(
                trading_order=trading_order,
                account_id=self.id,
                adjustment_type="tp",
                success=False,
                error_message=str(e)
            )
            
            raise

    def set_order_sl(self, trading_order: TradingOrder, sl_price: float) -> TradingOrder:
        """
        Set stop loss for an existing order.
        
        This method:
        1. Enforces minimum SL percent to protect profitability
        2. Creates or updates a WAITING_TRIGGER SL order in the database
        3. Updates the linked transaction's stop_loss value
        4. Calls broker-specific implementation (if needed)
        
        Args:
            trading_order: The original TradingOrder object
            sl_price: The stop loss price (may be adjusted away from entry if below minimum)
            
        Returns:
            TradingOrder: The created/updated stop loss order object
        """
        try:
            from sqlmodel import Session
            from ..db import get_db
            
            # Validate inputs
            if not trading_order:
                raise ValueError("trading_order cannot be None")
            if not isinstance(sl_price, (int, float)) or sl_price <= 0:
                raise ValueError("sl_price must be a positive number")
            
            # Enforce minimum SL percent based on open price
            # This is a safety check in case market price slipped after TradeAction was issued
            if trading_order.open_price:
                from ...config import get_min_tp_sl_percent
                min_tp_sl_percent = get_min_tp_sl_percent()
                
                open_price = float(trading_order.open_price)
                is_long = (trading_order.side.upper() == "BUY")
                
                original_sl = sl_price
                
                if is_long:
                    # For LONG: SL should be below open, loss percent = (Open - SL) / Open * 100
                    actual_percent = ((open_price - sl_price) / open_price) * 100
                    
                    if actual_percent < min_tp_sl_percent:
                        # Enforce minimum by adjusting SL downward (larger loss)
                        sl_price = open_price * (1 - min_tp_sl_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] SL enforcement (LONG): Risk {actual_percent:.2f}% below minimum {min_tp_sl_percent}%. "
                            f"Adjusting SL from ${original_sl:.2f} to ${sl_price:.2f} (open: ${open_price:.2f})"
                        )
                else:
                    # For SHORT: SL should be above open, loss percent = (SL - Open) / Open * 100
                    actual_percent = ((sl_price - open_price) / open_price) * 100
                    
                    if actual_percent < min_tp_sl_percent:
                        # Enforce minimum by adjusting SL upward (larger loss)
                        sl_price = open_price * (1 + min_tp_sl_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] SL enforcement (SHORT): Risk {actual_percent:.2f}% below minimum {min_tp_sl_percent}%. "
                            f"Adjusting SL from ${original_sl:.2f} to ${sl_price:.2f} (open: ${open_price:.2f})"
                        )
            
            # Get the linked transaction
            if not trading_order.transaction_id:
                raise ValueError("Order must have a linked transaction to set stop loss")
            
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction:
                raise ValueError(f"Transaction {trading_order.transaction_id} not found")
            
            # Store original transaction stop_loss for rollback if broker update fails
            original_transaction_sl = transaction.stop_loss
            
            # Update transaction's stop_loss value
            transaction.stop_loss = sl_price
            update_instance(transaction)
            
            logger.info(f"Updated transaction {transaction.id} stop_loss to ${sl_price}")
            
            # Create or update the SL order in the database
            with Session(get_db().bind) as session:
                # Calculate SL percent from open price (for storage in order.data)
                sl_percent = None
                if trading_order.open_price and trading_order.open_price > 0:
                    sl_percent = ((sl_price - trading_order.open_price) / trading_order.open_price) * 100
                    logger.debug(f"Calculated SL percent: {sl_percent:.2f}% for {trading_order.symbol}")
                
                # Check if there's already a stop loss order for this transaction
                # Look for any SL order in the transaction that is still active (not terminal)
                from sqlmodel import select
                existing_sl_order = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == trading_order.transaction_id,
                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                        TradingOrder.stop_price.isnot(None),
                        TradingOrder.limit_price.is_(None)  # Ensure it's a stop order (SL), not a limit order (TP)
                    )
                ).first()
                
                if existing_sl_order:
                    # Store original values for rollback if broker update fails
                    original_stop_price = existing_sl_order.stop_price
                    original_data = existing_sl_order.data.copy() if existing_sl_order.data else None
                    
                    # Update existing SL order
                    logger.info(f"Updating existing SL order {existing_sl_order.id} to price ${sl_price}")
                    existing_sl_order.stop_price = sl_price
                    
                    if sl_percent is not None:
                        if not existing_sl_order.data:
                            existing_sl_order.data = {}
                        existing_sl_order.data['sl_percent_target'] = round(sl_percent, 2)
                        existing_sl_order.data['sl_reference_price'] = round(trading_order.open_price, 2)
                    
                    # Check if parent order is now EXECUTED (FILLED/PARTIALLY_FILLED) and SL order is still WAITING_TRIGGER
                    # If so, submit the SL order immediately to broker
                    should_submit_immediately = (
                        trading_order.status in OrderStatus.get_executed_statuses() and
                        existing_sl_order.status == OrderStatus.WAITING_TRIGGER
                    )
                    
                    if should_submit_immediately:
                        logger.info(f"Parent order {trading_order.id} is now EXECUTED (status: {trading_order.status.value}), submitting SL order {existing_sl_order.id} to broker")
                        
                        # CRITICAL: Validate parent order has valid quantity before updating SL order quantity
                        # This prevents submitting SL orders with qty=0 when entry order has qty=0
                        if not trading_order.quantity or trading_order.quantity <= 0:
                            logger.error(f"Cannot submit SL order {existing_sl_order.id}: parent order {trading_order.id} has invalid quantity {trading_order.quantity}. Skipping SL submission.")
                            # Mark SL order as ERROR instead of submitting
                            existing_sl_order.status = OrderStatus.ERROR
                            session.add(existing_sl_order)
                            session.commit()
                            raise ValueError(f"Parent order {trading_order.id} has invalid quantity {trading_order.quantity}, cannot create SL order")
                        
                        existing_sl_order.quantity = trading_order.quantity
                        # Don't set to PENDING - submit directly and let submit_order set the status
                    
                    session.add(existing_sl_order)
                    session.commit()
                    session.refresh(existing_sl_order)
                    sl_order = existing_sl_order
                    
                    # Check if the SL order is already submitted at broker (has broker_order_id and not in terminal state)
                    # If so, we need to update the actual broker order, not just the database record
                    if existing_sl_order.broker_order_id and existing_sl_order.status not in OrderStatus.get_terminal_statuses():
                        logger.info(f"SL order {existing_sl_order.id} is active at broker with ID {existing_sl_order.broker_order_id} (status: {existing_sl_order.status.value}), calling broker update")
                        # Call the broker-specific update method
                        # Note: This may need to be implemented in broker-specific classes to cancel/replace
                        try:
                            self._update_broker_sl_order(existing_sl_order, sl_price)
                        except NotImplementedError:
                            logger.warning(f"Broker {self.__class__.__name__} does not support updating live SL orders - may need to cancel and replace")
                        except Exception as e:
                            logger.error(f"Error updating broker SL order: {e}", exc_info=True)
                            # ROLLBACK: Restore original values in database
                            logger.warning(f"Rolling back SL order {existing_sl_order.id} to original stop_price ${original_stop_price}")
                            existing_sl_order.stop_price = original_stop_price
                            existing_sl_order.data = original_data
                            session.add(existing_sl_order)
                            session.commit()
                            
                            # ROLLBACK: Restore transaction stop_loss
                            logger.warning(f"Rolling back transaction {transaction.id} stop_loss to ${original_transaction_sl}")
                            transaction.stop_loss = original_transaction_sl
                            update_instance(transaction)
                            raise
                    elif should_submit_immediately:
                        # Parent order is active and SL was WAITING_TRIGGER, submit it now
                        logger.info(f"Submitting SL order {existing_sl_order.id} to broker now that parent order {trading_order.id} is active")
                        session.close()
                        try:
                            result = self.submit_order(existing_sl_order)
                            logger.info(f"Successfully submitted SL order {existing_sl_order.id} to broker (broker_order_id: {existing_sl_order.broker_order_id})")
                        except Exception as submit_error:
                            logger.error(f"Error submitting SL order {existing_sl_order.id} to broker: {submit_error}", exc_info=True)
                            raise
                        # Reopen session for return
                        session = Session(get_db().bind)
                    
                    logger.info(f"Successfully updated SL order {sl_order.id} to ${sl_price}")
                    
                    # Log activity for SL change using centralized helper
                    from ..utils import log_tp_sl_adjustment_activity
                    log_tp_sl_adjustment_activity(
                        trading_order=trading_order,
                        account_id=self.id,
                        adjustment_type="sl",
                        new_price=sl_price,
                        percent=sl_percent,
                        order_id=existing_sl_order.id,
                        success=True
                    )
                else:
                    # Create new SL order
                    if trading_order.side.upper() == "BUY":
                        sl_side = OrderDirection.SELL
                        from ..types import OrderType as CoreOrderType
                        sl_order_type = CoreOrderType.SELL_STOP
                    else:
                        sl_side = OrderDirection.BUY
                        from ..types import OrderType as CoreOrderType
                        sl_order_type = CoreOrderType.BUY_STOP
                    
                    # Build order data with SL metadata
                    order_data = {}
                    if sl_percent is not None:
                        order_data = {
                            'sl_percent_target': round(sl_percent, 2),
                            'sl_reference_price': round(trading_order.open_price, 2)
                        }
                    
                    # Only submit SL immediately if parent order is already FILLED
                    # If parent is still PENDING/ACCEPTED, use WAITING_TRIGGER to avoid wash trade errors
                    should_submit_immediately = trading_order.status == OrderStatus.FILLED
                    
                    sl_order = TradingOrder(
                        account_id=self.id,
                        symbol=trading_order.symbol,
                        quantity=trading_order.quantity if should_submit_immediately else 0,  # Set quantity only if submitting
                        side=sl_side,
                        order_type=sl_order_type,
                        stop_price=sl_price,
                        transaction_id=trading_order.transaction_id,
                        status=OrderStatus.PENDING if should_submit_immediately else OrderStatus.WAITING_TRIGGER,
                        depends_on_order=trading_order.id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        expert_recommendation_id=trading_order.expert_recommendation_id,
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=f"SL for order {trading_order.id}",
                        data=order_data if order_data else None,
                        created_at=datetime.now(timezone.utc)
                    )
                    
                    session.add(sl_order)
                    session.commit()
                    session.refresh(sl_order)
                    
                    if should_submit_immediately:
                        logger.info(f"Created PENDING SL order {sl_order.id} at ${sl_price} - parent order {trading_order.id} already FILLED, will submit to broker now")
                        # Submit immediately to broker
                        session.close()
                        try:
                            result = self.submit_order(sl_order)
                            logger.info(f"Successfully submitted SL order {sl_order.id} to broker (broker_order_id: {sl_order.broker_order_id})")
                        except Exception as submit_error:
                            logger.error(f"Error submitting SL order {sl_order.id} to broker: {submit_error}", exc_info=True)
                            raise
                        # Reopen session for return
                        session = Session(get_db().bind)
                    else:
                        logger.info(f"Created WAITING_TRIGGER SL order {sl_order.id} at ${sl_price} (will submit when order {trading_order.id} is FILLED)")
            
            # Call broker-specific implementation (may be no-op for most brokers)
            self._set_order_sl_impl(trading_order, sl_price)
            
            return sl_order
            
        except Exception as e:
            logger.error(f"Error setting stop loss for order {trading_order.id if trading_order else 'None'}: {e}", exc_info=True)
            
            # Log activity for SL adjustment failure using centralized helper
            from ..utils import log_tp_sl_adjustment_activity
            log_tp_sl_adjustment_activity(
                trading_order=trading_order,
                account_id=self.id,
                adjustment_type="sl",
                success=False,
                error_message=str(e)
            )
            
            raise

    @abstractmethod
    def _set_order_sl_impl(self, trading_order: TradingOrder, sl_price: float) -> Any:
        """
        Broker-specific implementation hook for stop loss order setting.
        
        This method is called AFTER the base class has:
        - Enforced minimum SL percent
        - Created/updated the WAITING_TRIGGER SL order in the database
        - Updated the transaction's stop_loss value
        
        For most brokers, this method can be a no-op (just pass). Override only if your broker
        needs special handling beyond database order creation.
        
        Args:
            trading_order: The original TradingOrder object (for reference/context)
            sl_price: The validated and enforced SL price
            
        Returns:
            Any: Any broker-specific result (optional). Base class will not use this value.
        """
        pass

    @abstractmethod
    def _set_order_tp_sl_impl(self, trading_order: TradingOrder, tp_price: float, sl_price: float) -> Any:
        """
        Broker-specific implementation hook for setting both TP and SL simultaneously.
        
        This method is called AFTER the base class has:
        - Enforced minimum TP/SL percent
        - Updated the transaction's take_profit and stop_loss values
        
        For brokers that support STOP_LIMIT orders (e.g., Alpaca), override this method
        to submit a single STOP_LIMIT order that includes both stop price (SL) and limit price (TP).
        
        For brokers that don't support combined TP/SL orders, this method can be a no-op (just pass),
        and the base class will fall back to creating separate TP and SL orders.
        
        Args:
            trading_order: The original TradingOrder object (for reference/context)
            tp_price: The validated and enforced TP price
            sl_price: The validated and enforced SL price
            
        Returns:
            Any: Any broker-specific result (optional). Base class will not use this value.
        """
        pass

    def set_order_tp_sl(self, trading_order: TradingOrder, tp_price: float, sl_price: float) -> tuple[TradingOrder, TradingOrder]:
        """
        Set both take profit and stop loss for an existing order simultaneously.
        
        This method:
        1. Enforces minimum TP and SL percent to protect profitability
        2. Updates the linked transaction's take_profit and stop_loss values
        3. Finds and updates/replaces existing TP/SL orders:
           - For broker-submitted orders: Uses replace_order API
           - For WAITING_TRIGGER orders: Updates database directly
        4. Creates new combined STOP_LIMIT order if none exist
        
        Args:
            trading_order: The original TradingOrder object
            tp_price: The take profit price (may be adjusted if below minimum)
            sl_price: The stop loss price (may be adjusted if above minimum)
            
        Returns:
            tuple[TradingOrder, TradingOrder]: A tuple of (tp_order, sl_order) representing the created/updated orders
        """
        try:
            from sqlmodel import Session, select
            from ..db import get_db
            
            # Validate inputs
            if not trading_order:
                raise ValueError("trading_order cannot be None")
            if not isinstance(tp_price, (int, float)) or tp_price <= 0:
                raise ValueError("tp_price must be a positive number")
            if not isinstance(sl_price, (int, float)) or sl_price <= 0:
                raise ValueError("sl_price must be a positive number")
            
            # Get the linked transaction
            if not trading_order.transaction_id:
                raise ValueError("Order must have a linked transaction to set take profit and stop loss")
            
            transaction = get_instance(Transaction, trading_order.transaction_id)
            if not transaction:
                raise ValueError(f"Transaction {trading_order.transaction_id} not found")
            
            # Enforce minimum TP percent based on open price
            if trading_order.open_price:
                from ...config import get_min_tp_sl_percent
                min_tp_sl_percent = get_min_tp_sl_percent()
                
                open_price = float(trading_order.open_price)
                is_long = (trading_order.side.upper() == "BUY")
                
                original_tp = tp_price
                original_sl = sl_price
                
                if is_long:
                    # For LONG: TP should be above open, SL should be below open
                    tp_percent = ((tp_price - open_price) / open_price) * 100
                    sl_percent = ((open_price - sl_price) / open_price) * 100
                    
                    if tp_percent < min_tp_sl_percent:
                        tp_price = open_price * (1 + min_tp_sl_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] TP enforcement (LONG): Profit {tp_percent:.2f}% below minimum {min_tp_sl_percent}%. "
                            f"Adjusting TP from ${original_tp:.2f} to ${tp_price:.2f}"
                        )
                    
                    if sl_percent < min_tp_sl_percent:
                        sl_price = open_price * (1 - min_tp_sl_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] SL enforcement (LONG): Protection {sl_percent:.2f}% below minimum {min_tp_sl_percent}%. "
                            f"Adjusting SL from ${original_sl:.2f} to ${sl_price:.2f}"
                        )
                else:
                    # For SHORT: TP should be below open, SL should be above open
                    tp_percent = ((open_price - tp_price) / open_price) * 100
                    sl_percent = ((sl_price - open_price) / open_price) * 100
                    
                    if tp_percent < min_tp_sl_percent:
                        tp_price = open_price * (1 - min_tp_sl_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] TP enforcement (SHORT): Profit {tp_percent:.2f}% below minimum {min_tp_sl_percent}%. "
                            f"Adjusting TP from ${original_tp:.2f} to ${tp_price:.2f}"
                        )
                    
                    if sl_percent < min_tp_sl_percent:
                        sl_price = open_price * (1 + min_tp_sl_percent / 100)
                        logger.warning(
                            f"[Account {self.id}] SL enforcement (SHORT): Protection {sl_percent:.2f}% below minimum {min_tp_sl_percent}%. "
                            f"Adjusting SL from ${original_sl:.2f} to ${sl_price:.2f}"
                        )
            
            # Update transaction's take_profit and stop_loss values
            transaction.take_profit = tp_price
            transaction.stop_loss = sl_price
            update_instance(transaction)
            
            logger.info(f"Updated transaction {transaction.id} with TP=${tp_price:.2f} and SL=${sl_price:.2f}")
            
            # Find existing TP/SL orders for this transaction
            with Session(get_db().bind) as session:
                existing_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == trading_order.transaction_id,
                        TradingOrder.id != trading_order.id,  # Exclude entry order
                        TradingOrder.status.not_in(OrderStatus.get_terminal_statuses())
                    )
                ).all()
                
                existing_tp_sl = None  # Could be TP, SL, or STOP_LIMIT order
                
                # Identify existing TP/SL order (only one can exist due to Alpaca constraint)
                for order in existing_orders:
                    # Any order on opposite side of entry is our TP/SL order
                    if order.side != trading_order.side:
                        existing_tp_sl = order
                        break
                
                # Determine what order type we need for TP+SL combination
                if trading_order.side == OrderDirection.BUY:
                    target_order_type = OrderType.SELL_STOP_LIMIT
                    target_side = OrderDirection.SELL
                else:
                    target_order_type = OrderType.BUY_STOP_LIMIT
                    target_side = OrderDirection.BUY
                
                # Handle existing order or create new one
                if existing_tp_sl:
                    # We have an existing order - need to update it to STOP_LIMIT with both prices
                    if existing_tp_sl.broker_order_id and existing_tp_sl.status not in [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]:
                        # Order already at broker - use replace API
                        logger.info(f"Replacing existing order {existing_tp_sl.id} at broker with STOP_LIMIT (TP=${tp_price}, SL=${sl_price})")
                        
                        # Use broker-specific replace that handles STOP_LIMIT creation
                        result_order = self._replace_order_with_stop_limit(
                            existing_order=existing_tp_sl,
                            tp_price=tp_price,
                            sl_price=sl_price
                        )
                        
                        # Return same order for both TP and SL (it's a combined order)
                        return (result_order, result_order)
                        
                    elif existing_tp_sl.status == OrderStatus.WAITING_TRIGGER:
                        # Order is WAITING_TRIGGER - update in database directly
                        logger.info(f"Updating WAITING_TRIGGER order {existing_tp_sl.id} to STOP_LIMIT (TP=${tp_price}, SL=${sl_price})")
                        
                        existing_tp_sl.order_type = target_order_type
                        existing_tp_sl.stop_price = sl_price
                        existing_tp_sl.limit_price = tp_price
                        
                        session.add(existing_tp_sl)
                        session.commit()
                        session.refresh(existing_tp_sl)
                        
                        # Return same order for both TP and SL
                        return (existing_tp_sl, existing_tp_sl)
                    else:
                        logger.warning(f"Order {existing_tp_sl.id} in unexpected state {existing_tp_sl.status}, will create new")
                
                # No existing order or update failed - create new STOP_LIMIT order
                logger.info(f"Creating new STOP_LIMIT order for transaction {trading_order.transaction_id} (TP=${tp_price}, SL=${sl_price})")
                
                # Only submit immediately if parent order is FILLED
                should_submit_immediately = trading_order.status == OrderStatus.FILLED
                
                stop_limit_order = TradingOrder(
                    account_id=self.id,
                    symbol=trading_order.symbol,
                    quantity=trading_order.quantity if should_submit_immediately else 0,
                    side=target_side,
                    order_type=target_order_type,
                    stop_price=sl_price,  # Trigger price (stop loss)
                    limit_price=tp_price,  # Execution price (take profit)
                    transaction_id=trading_order.transaction_id,
                    status=OrderStatus.PENDING if should_submit_immediately else OrderStatus.WAITING_TRIGGER,
                    depends_on_order=trading_order.id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    expert_recommendation_id=trading_order.expert_recommendation_id,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=f"TP/SL for order {trading_order.id}",
                    created_at=datetime.now(timezone.utc)
                )
                
                session.add(stop_limit_order)
                session.commit()
                session.refresh(stop_limit_order)
                
                if should_submit_immediately:
                    logger.info(f"Parent order {trading_order.id} is FILLED, submitting STOP_LIMIT order {stop_limit_order.id} to broker")
                    session.close()
                    try:
                        self.submit_order(stop_limit_order)
                        logger.info(f"Successfully submitted STOP_LIMIT order {stop_limit_order.id} to broker")
                    except Exception as submit_error:
                        logger.error(f"Error submitting STOP_LIMIT order {stop_limit_order.id}: {submit_error}", exc_info=True)
                        raise
                else:
                    logger.info(f"Created WAITING_TRIGGER STOP_LIMIT order {stop_limit_order.id} (will submit when order {trading_order.id} is FILLED)")
                
                # Return same order for both TP and SL (it's a combined order)
                return (stop_limit_order, stop_limit_order)
            
            return (tp_order, sl_order)
            
        except Exception as e:
            logger.error(f"Error setting TP/SL for order {trading_order.id}: {e}", exc_info=True)
            raise
    
    def _replace_tp_order(self, existing_tp: TradingOrder, new_tp_price: float) -> TradingOrder:
        """
        Replace an existing TP order at the broker with a new price.
        Default implementation - can be overridden by broker-specific logic.
        
        Args:
            existing_tp: The existing TP order to replace
            new_tp_price: The new take profit price
            
        Returns:
            TradingOrder: The updated or new TP order
        """
        logger.warning(f"Broker {self.__class__.__name__} does not implement _replace_tp_order, using set_order_tp instead")
        return self.set_order_tp(existing_tp, new_tp_price)
    
    def _replace_sl_order(self, existing_sl: TradingOrder, new_sl_price: float) -> TradingOrder:
        """
        Replace an existing SL order at the broker with a new price.
        Default implementation - can be overridden by broker-specific logic.
        
        Args:
            existing_sl: The existing SL order to replace
            new_sl_price: The new stop loss price
            
        Returns:
            TradingOrder: The updated or new SL order
        """
        logger.warning(f"Broker {self.__class__.__name__} does not implement _replace_sl_order, using set_order_sl instead")
        return self.set_order_sl(existing_sl, new_sl_price)
    
    def _replace_order_with_stop_limit(self, existing_order: TradingOrder, tp_price: float, sl_price: float) -> TradingOrder:
        """
        Replace an existing TP or SL order with a STOP_LIMIT order containing both TP and SL.
        
        This is the critical method for Alpaca's constraint of allowing only one opposite-direction order.
        When setting both TP and SL together, or when adding TP to existing SL (or vice versa),
        we need to replace the single existing order with a STOP_LIMIT that has both prices.
        
        Args:
            existing_order: The existing TP or SL order to replace
            tp_price: The take profit (limit) price
            sl_price: The stop loss (trigger) price
            
        Returns:
            The new STOP_LIMIT order with both TP and SL
        """
        logger.warning(f"Broker {self.__class__.__name__} does not implement _replace_order_with_stop_limit, using cancel and recreate")
        
        # Fallback: cancel old order and create new one
        try:
            if existing_order.broker_order_id:
                self.cancel_order(existing_order.broker_order_id)
        except Exception as e:
            logger.error(f"Error canceling old order {existing_order.id}: {e}")
        
        # Create new STOP_LIMIT order
        from ba2_trade_platform.core.db import get_instance
        transaction = get_instance(Transaction, existing_order.transaction_id)
        entry_order = get_instance(TradingOrder, transaction.entry_order_id)
        
        # Validate entry order has valid quantity
        if not entry_order or not entry_order.quantity or entry_order.quantity <= 0:
            raise ValueError(f"Cannot create STOP_LIMIT order: entry order has invalid quantity {entry_order.quantity if entry_order else 'None'}")
        
        # Determine correct side and type
        if entry_order.side == OrderDirection.BUY:
            order_type = OrderType.SELL_STOP_LIMIT
            side = OrderDirection.SELL
        else:
            order_type = OrderType.BUY_STOP_LIMIT
            side = OrderDirection.BUY
        
        # Create new order
        stop_limit_order = TradingOrder(
            account_id=self.id,
            symbol=entry_order.symbol,
            quantity=entry_order.quantity,
            side=side,
            order_type=order_type,
            stop_price=sl_price,
            limit_price=tp_price,
            transaction_id=transaction.id,
            status=OrderStatus.PENDING,
            depends_on_order=entry_order.id,
            depends_order_status_trigger=OrderStatus.FILLED,
            expert_recommendation_id=entry_order.expert_recommendation_id,
            open_type=OrderOpenType.AUTOMATIC,
            comment=f"TP/SL replacement for order {entry_order.id}",
            created_at=datetime.now(timezone.utc)
        )
        
        add_instance(stop_limit_order)
        self.submit_order(stop_limit_order)
        
        return stop_limit_order


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
            from ..db import get_db
            
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
                    
                    # Check if ALL orders are in terminal states (canceled, rejected, error, expired, or closed)
                    # Use get_terminal_statuses() to ensure consistency across the codebase
                    terminal_statuses = OrderStatus.get_terminal_statuses()
                    all_orders_terminal = (
                        len(orders) > 0 and
                        all(order.status in terminal_statuses for order in orders)
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
                    
                    # Sum ALL filled buy and sell orders (including limit orders, market orders, TP/SL, etc.)
                    # If quantities match, the position is balanced and transaction should be closed
                    total_filled_buy = 0.0
                    total_filled_sell = 0.0
                    for order in orders:
                        if order.status in executed_statuses:
                            qty = order.filled_qty if order.filled_qty else order.quantity
                            if qty:
                                if order.side == OrderDirection.BUY:
                                    total_filled_buy += float(qty)
                                elif order.side == OrderDirection.SELL:
                                    total_filled_sell += float(qty)
                    
                    # If buy and sell orders match (within a small tolerance for floating point), position is closed
                    position_balanced = abs(total_filled_buy - total_filled_sell) < 0.0001
                    
                    # Update transaction quantity if different
                    if calculated_quantity != 0 and transaction.quantity != calculated_quantity:
                        transaction.quantity = calculated_quantity
                        has_changes = True
                        logger.debug(f"Transaction {transaction.id} quantity updated to {calculated_quantity}")
                    
                    # Update transaction status based on order states
                    new_status = None
                    
                    # Update open_price from the oldest filled market entry order (always update to ensure accuracy)
                    filled_entry_orders = [
                        order for order in market_entry_orders 
                        if order.status in executed_statuses and order.open_price
                    ]
                    if filled_entry_orders:
                        # Sort by created_at to get the oldest filled order
                        oldest_order = min(filled_entry_orders, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
                        if transaction.open_price != oldest_order.open_price:
                            transaction.open_price = oldest_order.open_price
                            has_changes = True
                            logger.debug(f"Transaction {transaction.id} open_price updated to {oldest_order.open_price} from oldest filled order {oldest_order.id}")
                    
                    # WAITING -> OPENED: If any market entry order is FILLED
                    if has_filled_entry_order and transaction.status == TransactionStatus.WAITING:
                        new_status = TransactionStatus.OPENED
                        if not transaction.open_date:
                            transaction.open_date = datetime.now(timezone.utc)
                            has_changes = True
                        
                        logger.debug(f"Transaction {transaction.id} has filled market entry order, marking as OPENED")
                        has_changes = True
                    
                    # Update close_price from filled closing orders (always update to ensure accuracy)
                    if filled_closing_orders:
                        closing_order = filled_closing_orders[0]  # Use first filled closing order
                        if closing_order.open_price and transaction.close_price != closing_order.open_price:
                            transaction.close_price = closing_order.open_price
                            has_changes = True
                            logger.debug(f"Transaction {transaction.id} close_price updated to {closing_order.open_price} from filled closing order {closing_order.id}")
                    
                    # OPENED -> CLOSED: If at least one OCO leg is filled
                    # OCO legs are dependent orders on the parent entry order with order_type = OCO or specific leg markers
                    oco_leg_filled = False
                    for dep_order in dependent_orders:
                        # Check if this is an OCO leg order (identified by depends_on_order and comment)
                        if (dep_order.status == OrderStatus.FILLED and 
                            ("OCO-" in (dep_order.comment or "") or dep_order.order_type == OrderType.OCO)):
                            oco_leg_filled = True
                            logger.debug(f"Transaction {transaction.id} has filled OCO leg: {dep_order.id} ({dep_order.comment})")
                            break
                    
                    if oco_leg_filled and transaction.status == TransactionStatus.OPENED:
                        # Update close_price from the filled OCO leg
                        filled_oco_legs = [
                            o for o in dependent_orders 
                            if (o.status == OrderStatus.FILLED and 
                                ("OCO-" in (o.comment or "") or o.order_type == OrderType.OCO) and
                                o.open_price)
                        ]
                        if filled_oco_legs:
                            oco_leg = filled_oco_legs[0]
                            if transaction.close_price != oco_leg.open_price:
                                transaction.close_price = oco_leg.open_price
                                has_changes = True
                                logger.debug(f"Transaction {transaction.id} close_price updated to {oco_leg.open_price} from filled OCO leg {oco_leg.id}")
                        
                        from ..utils import close_transaction_with_logging
                        close_transaction_with_logging(
                            transaction=transaction,
                            account_id=self.id,
                            close_reason="oco_leg_filled",
                            session=session
                        )
                        new_status = TransactionStatus.CLOSED
                        has_changes = True
                    
                    # OPENED -> CLOSED: If we have a filled closing order (TP/SL)
                    elif filled_closing_orders and transaction.status == TransactionStatus.OPENED:
                        from ..utils import close_transaction_with_logging
                        close_transaction_with_logging(
                            transaction=transaction,
                            account_id=self.id,
                            close_reason="tp_sl_filled",
                            session=session
                        )
                        new_status = TransactionStatus.CLOSED
                        has_changes = True
                    
                    # ANY STATUS -> CLOSED: If all orders are in terminal state (canceled, rejected, error, expired, or closed)
                    elif all_orders_terminal and transaction.status != TransactionStatus.CLOSED:
                        from ..utils import close_transaction_with_logging
                        close_transaction_with_logging(
                            transaction=transaction,
                            account_id=self.id,
                            close_reason="all_orders_terminal",
                            session=session
                        )
                        new_status = TransactionStatus.CLOSED
                        has_changes = True
                    
                    # OPENED -> CLOSED: If filled buy and sell orders sum to match quantity (position balanced)
                    elif position_balanced and transaction.status != TransactionStatus.CLOSED and (total_filled_buy > 0 or total_filled_sell > 0):
                        # Update close_price from the last filled order that closed the position (always update)
                        # Find the last filled order chronologically
                        filled_orders = [o for o in orders if o.status in executed_statuses and o.open_price]
                        if filled_orders:
                            # Sort by created_at to get the last one
                            filled_orders.sort(key=lambda x: x.created_at if x.created_at else datetime.min)
                            last_order = filled_orders[-1]
                            if transaction.close_price != last_order.open_price:
                                transaction.close_price = last_order.open_price
                                has_changes = True
                                logger.debug(f"Transaction {transaction.id} close_price updated to {last_order.open_price} from last filled order {last_order.id}")
                        
                        from ..utils import close_transaction_with_logging
                        close_transaction_with_logging(
                            transaction=transaction,
                            account_id=self.id,
                            close_reason="position_balanced",
                            session=session,
                            additional_data={
                                "total_filled_buy": total_filled_buy,
                                "total_filled_sell": total_filled_sell
                            }
                        )
                        new_status = TransactionStatus.CLOSED
                        has_changes = True
                    
                    # WAITING -> CLOSED: If all market entry orders are in terminal state without execution
                    # This handles canceled/rejected/error orders before the transaction ever opened
                    elif all_entry_orders_terminal and transaction.status == TransactionStatus.WAITING and not has_filled_entry_order:
                        from ..utils import close_transaction_with_logging
                        close_transaction_with_logging(
                            transaction=transaction,
                            account_id=self.id,
                            close_reason="entry_orders_terminal_no_execution",
                            session=session
                        )
                        new_status = TransactionStatus.CLOSED
                        has_changes = True
                    
                    # OPENED -> CLOSED: If all market entry orders are in terminal state after opening
                    # This handles cases where the position was closed via broker or manual intervention
                    elif all_entry_orders_terminal and transaction.status == TransactionStatus.OPENED and not filled_closing_orders:
                        # Only close if we don't have any active TP/SL orders waiting
                        active_dependent_orders = [o for o in dependent_orders if o.status not in terminal_statuses]
                        if not active_dependent_orders:
                            from ..utils import close_transaction_with_logging
                            close_transaction_with_logging(
                                transaction=transaction,
                                account_id=self.id,
                                close_reason="entry_orders_terminal_after_opening",
                                session=session
                            )
                            new_status = TransactionStatus.CLOSED
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
    
    async def close_transaction_async(self, transaction_id: int) -> dict:
        """
        Close a transaction asynchronously by:
        1. For unfilled orders: Cancel them at broker and delete WAITING_TRIGGER orders from DB
        2. For filled positions: Check if there's already a pending close order
           - If close order exists and is in ERROR state: Retry submitting it
           - If close order exists and is not in ERROR: Do nothing (log it)
           - If no close order exists: Create and submit a new closing order
        3. Refresh orders from broker
        4. Refresh transactions to update status
        
        This method handles both initial close and retry close operations.
        This async version prevents UI blocking during broker operations.
        
        Args:
            transaction_id: The transaction ID to close
            
        Returns:
            dict: Result containing:
                - success: bool
                - message: str (user-friendly message)
                - canceled_count: int (orders canceled)
                - deleted_count: int (orders deleted)
                - close_order_id: int (closing order ID if created/retried)
        """
        import asyncio
        
        # Get transaction details for logging
        transaction = get_instance(Transaction, transaction_id)
        if transaction:
            open_date_str = transaction.open_date.strftime('%Y-%m-%d %H:%M:%S') if transaction.open_date else 'N/A'
            logger.info(f"Closing transaction {transaction_id} - Account: {self.id}, Symbol: {transaction.symbol}, Opened: {open_date_str}")
        else:
            logger.warning(f"Closing transaction {transaction_id} - Account: {self.id}, transaction not found in database")
        
        # Run the synchronous close_transaction in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.close_transaction, transaction_id)
        
        # After closing, refresh orders from broker to get latest status
        if result['success']:
            logger.info(f"Refreshing orders from broker after close transaction {transaction_id}")
            await loop.run_in_executor(None, self.refresh_orders)
            
            # Then refresh transactions to update transaction status
            logger.info(f"Refreshing transactions after close transaction {transaction_id}")
            await loop.run_in_executor(None, self.refresh_transactions)
        
        return result
    
    def close_transaction(self, transaction_id: int) -> dict:
        """
        Close a transaction by:
        1. For unfilled orders: Cancel them at broker and delete WAITING_TRIGGER orders from DB
        2. For filled positions: Check if there's already a pending close order
           - If close order exists and is in ERROR state: Retry submitting it
           - If close order exists and is not in ERROR: Do nothing (log it)
           - If no close order exists: Create and submit a new closing order
        
        This method handles both initial close and retry close operations.
        For async version with automatic refresh, use close_transaction_async().
        
        Args:
            transaction_id: The transaction ID to close
            
        Returns:
            dict: Result containing:
                - success: bool
                - message: str (user-friendly message)
                - canceled_count: int (orders canceled)
                - deleted_count: int (orders deleted)
                - close_order_id: int (closing order ID if created/retried)
        """
        from sqlmodel import select, Session
        from ..db import get_db, delete_instance
        from ..types import OrderDirection, OrderType, TransactionStatus, OrderStatus
        
        result = {
            'success': False,
            'message': '',
            'canceled_count': 0,
            'deleted_count': 0,
            'close_order_id': None
        }
        
        try:
            # Get transaction
            transaction = get_instance(Transaction, transaction_id)
            if not transaction:
                result['message'] = 'Transaction not found'
                return result
            
            # Check if transaction is already being closed
            if transaction.status == TransactionStatus.CLOSING:
                logger.info(f"Transaction {transaction_id} is already in CLOSING status")
                # Continue anyway - this could be a retry
            
            # Set transaction status to CLOSING to prevent duplicate close attempts
            if transaction.status != TransactionStatus.CLOSING:
                transaction.status = TransactionStatus.CLOSING
                update_instance(transaction)
                logger.info(f"Set transaction {transaction_id} status to CLOSING")
            
            # Query for ALL orders associated with this transaction
            with Session(get_db().bind) as session:
                all_orders_statement = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.account_id == self.id
                ).order_by(TradingOrder.created_at)
                all_orders = list(session.exec(all_orders_statement).all())
                
                if not all_orders:
                    result['message'] = 'No orders found for this transaction'
                    return result
                
                # Process orders
                unfilled_statuses = OrderStatus.get_unfilled_statuses()
                executed_statuses = OrderStatus.get_executed_statuses()
                unsent_statuses = OrderStatus.get_unsent_statuses()
                has_filled = False
                existing_close_order = None
                
                for order in all_orders:
                    # Check if this is a filled entry order
                    if order.status in executed_statuses and not order.depends_on_order:
                        has_filled = True
                    
                    # Check if this is a closing order (market order to close position)
                    # Closing orders are typically MARKET orders with opposite side to position
                    # and have a comment indicating they're closing orders
                    is_closing_order = (
                        order.order_type == OrderType.MARKET and
                        order.comment and 
                        'closing' in order.comment.lower()
                    )
                    
                    if is_closing_order:
                        existing_close_order = order
                        logger.info(f"Found existing closing order {order.id} with status {order.status}")
                        continue
                    
                    # Handle unsent orders (PENDING, WAITING_TRIGGER) - just mark as CLOSED
                    if order.status in unsent_statuses:
                        try:
                            order.status = OrderStatus.CLOSED
                            session.add(order)  # Use the existing session
                            result['deleted_count'] += 1
                            logger.info(f"Marked unsent order {order.id} as CLOSED for transaction {transaction_id}")
                        except Exception as e:
                            logger.error(f"Error marking unsent order {order.id} as CLOSED: {e}")
                        continue
                    
                    # Cancel unfilled orders at broker (only if they were sent to broker)
                    if order.status in unfilled_statuses and not is_closing_order:
                        try:
                            if hasattr(self, 'cancel_order') and order.broker_order_id:
                                self.cancel_order(order.broker_order_id)
                                result['canceled_count'] += 1
                                logger.info(f"Canceled unfilled order {order.id} (broker: {order.broker_order_id})")
                        except Exception as e:
                            logger.error(f"Error canceling order {order.id}: {e}")
                
                # Handle filled positions
                if has_filled:
                    # Check if there's an existing close order
                    if existing_close_order:
                        if existing_close_order.status == OrderStatus.ERROR:
                            # Before retrying, check if position still exists at broker
                            # If position is gone, just mark transaction as CLOSED (it was already closed externally)
                            logger.info(f"Retrying close order {existing_close_order.id} which is in ERROR state")
                            try:
                                # Check if position still exists at broker
                                broker_positions = None
                                try:
                                    broker_positions = self.get_positions()
                                    position_exists = any(
                                        pos.get('symbol') == transaction.symbol if isinstance(pos, dict) 
                                        else getattr(pos, 'symbol', None) == transaction.symbol
                                        for pos in (broker_positions or [])
                                    )
                                    
                                    if not position_exists:
                                        logger.info(
                                            f"Position {transaction.symbol} no longer exists at broker - "
                                            f"marking transaction {transaction_id} as CLOSED without retry"
                                        )
                                        # Mark the ERROR order as CANCELED (not needed anymore)
                                        existing_close_order.status = OrderStatus.CANCELED
                                        session.add(existing_close_order)
                                        
                                        # Mark transaction as CLOSED with logging
                                        from ..utils import close_transaction_with_logging
                                        close_transaction_with_logging(
                                            transaction=transaction,
                                            account_id=self.id,
                                            close_reason="position_not_at_broker",
                                            session=session
                                        )
                                        session.add(transaction)
                                        session.commit()
                                        
                                        result['success'] = True
                                        result['message'] = f'Transaction closed (position no longer at broker)'
                                        logger.info(f"Transaction {transaction_id} marked as CLOSED - position already closed externally")
                                        
                                        # Skip the retry - position is already closed
                                        if result['canceled_count'] > 0 or result['deleted_count'] > 0:
                                            result['message'] += f' ({result["canceled_count"]} orders canceled, {result["deleted_count"]} waiting orders deleted)'
                                        
                                        # Continue to next transaction (don't retry order)
                                        return result
                                        
                                except Exception as pos_check_err:
                                    logger.warning(
                                        f"Could not verify if position {transaction.symbol} exists at broker: {pos_check_err}. "
                                        f"Proceeding with close order retry."
                                    )
                                    # If we can't check, proceed with retry (safer than assuming position is gone)
                                
                                # Resubmit the order
                                submitted_order = self.submit_order(existing_close_order)
                                if submitted_order:
                                    result['success'] = True
                                    result['close_order_id'] = existing_close_order.id
                                    result['message'] = f'Retried close order for {transaction.symbol}'
                                    if result['canceled_count'] > 0:
                                        result['message'] += f' ({result["canceled_count"]} orders canceled)'
                                    if result['deleted_count'] > 0:
                                        result['message'] += f' ({result["deleted_count"]} waiting orders deleted)'
                                    
                                    # Log successful retry
                                    from ..utils import log_close_order_activity
                                    log_close_order_activity(
                                        transaction=transaction,
                                        account_id=self.id,
                                        success=True,
                                        close_order_id=result['close_order_id'],
                                        canceled_count=result['canceled_count'],
                                        deleted_count=result['deleted_count'],
                                        is_retry=True
                                    )
                                else:
                                    result['message'] = 'Failed to retry closing order'
                                    
                                    # Log failed retry
                                    from ..utils import log_close_order_activity
                                    log_close_order_activity(
                                        transaction=transaction,
                                        account_id=self.id,
                                        success=False,
                                        error_message="Order retry returned None",
                                        canceled_count=result['canceled_count'],
                                        deleted_count=result['deleted_count'],
                                        is_retry=True
                                    )
                            except Exception as e:
                                logger.error(f"Error retrying close order: {e}", exc_info=True)
                                result['message'] = f'Error retrying close order: {str(e)}'
                                
                                # Log retry exception
                                from ..utils import log_close_order_activity
                                log_close_order_activity(
                                    transaction=transaction,
                                    account_id=self.id,
                                    success=False,
                                    error_message=str(e),
                                    canceled_count=result['canceled_count'],
                                    deleted_count=result['deleted_count'],
                                    is_retry=True
                                )
                        else:
                            # Close order exists but not in error - do nothing
                            logger.info(f"Close order {existing_close_order.id} exists with status {existing_close_order.status}, no action needed")
                            result['success'] = True
                            result['message'] = f'Close order already exists with status {existing_close_order.status.value}'
                            if result['canceled_count'] > 0:
                                result['message'] += f' ({result["canceled_count"]} orders canceled)'
                            if result['deleted_count'] > 0:
                                result['message'] += f' ({result["deleted_count"]} waiting orders deleted)'
                    else:
                        # No existing close order - create a new one
                        logger.info(f"Creating new closing order for transaction {transaction_id}")
                        # Determine closing side (opposite of position)
                        close_side = OrderDirection.SELL if transaction.quantity > 0 else OrderDirection.BUY
                        
                        close_order = TradingOrder(
                            account_id=self.id,
                            symbol=transaction.symbol,
                            quantity=abs(transaction.quantity),
                            side=close_side,
                            order_type=OrderType.MARKET,
                            transaction_id=transaction.id,
                            comment=f'Closing position for transaction {transaction.id}'
                        )
                        
                        # Submit the closing order (skip position size validation)
                        submitted_order = self.submit_order(close_order, is_closing_order=True)
                        
                        if submitted_order:
                            result['success'] = True
                            result['close_order_id'] = submitted_order.id if hasattr(submitted_order, 'id') else None
                            result['message'] = f'Closing order submitted for {transaction.symbol}'
                            if result['canceled_count'] > 0:
                                result['message'] += f' ({result["canceled_count"]} orders canceled)'
                            if result['deleted_count'] > 0:
                                result['message'] += f' ({result["deleted_count"]} waiting orders deleted)'
                            
                            # Log successful close order submission
                            from ..utils import log_close_order_activity
                            log_close_order_activity(
                                transaction=transaction,
                                account_id=self.id,
                                success=True,
                                close_order_id=result['close_order_id'],
                                quantity=abs(transaction.quantity),
                                side=close_side,
                                canceled_count=result['canceled_count'],
                                deleted_count=result['deleted_count']
                            )
                        else:
                            result['message'] = 'Failed to submit closing order'
                            
                            # Log failed close order submission
                            from ..utils import log_close_order_activity
                            log_close_order_activity(
                                transaction=transaction,
                                account_id=self.id,
                                success=False,
                                error_message="Order submission returned None",
                                canceled_count=result['canceled_count'],
                                deleted_count=result['deleted_count']
                            )
                else:
                    # No filled position, just report cleanup
                    result['success'] = True
                    result['message'] = 'Transaction cleanup completed'
                    if result['canceled_count'] > 0:
                        result['message'] += f': {result["canceled_count"]} orders canceled'
                    if result['deleted_count'] > 0:
                        result['message'] += f', {result["deleted_count"]} waiting orders deleted'
                
                # Check if all orders are now in terminal statuses and close transaction if so
                terminal_statuses = OrderStatus.get_terminal_statuses()
                all_orders_terminal = all(order.status in terminal_statuses for order in all_orders)
                
                if all_orders_terminal and transaction.status != TransactionStatus.CLOSED:
                    from ..utils import close_transaction_with_logging
                    close_transaction_with_logging(
                        transaction=transaction,
                        account_id=self.id,
                        close_reason="manual_close",
                        session=session,
                        additional_data={
                            "open_date": transaction.open_date.isoformat() if transaction.open_date else None,
                            "close_date": transaction.close_date.isoformat() if transaction.close_date else None
                        }
                    )
                    session.add(transaction)
                    result['message'] += ' (transaction closed)'
                
                session.commit()
            
            return result
            
        except Exception as e:
            logger.error(f"Error closing transaction {transaction_id}: {e}", exc_info=True)
            result['message'] = f'Error: {str(e)}'
            
            # Log activity for transaction close failure
            try:
                from ..db import log_activity
                from ..types import ActivityLogSeverity, ActivityLogType
                
                transaction = get_instance(Transaction, transaction_id)
                
                log_activity(
                    severity=ActivityLogSeverity.FAILURE,
                    activity_type=ActivityLogType.TRANSACTION_CLOSED,
                    description=f"Failed to close transaction #{transaction_id}" + 
                               (f" ({transaction.symbol})" if transaction else "") + 
                               f": {str(e)}",
                    data={
                        "transaction_id": transaction_id,
                        "symbol": transaction.symbol if transaction else None,
                        "error": str(e)
                    },
                    source_expert_id=transaction.expert_id if transaction else None,
                    source_account_id=self.id
                )
            except Exception as log_error:
                logger.warning(f"Failed to log transaction close failure activity: {log_error}")
            
            return result
