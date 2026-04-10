from abc import abstractmethod
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone
from ba2_trade_platform.logger import logger
from ...core.models import TradingOrder, Transaction, ExpertRecommendation, ExpertInstance
from ...core.types import OrderOpenType, OrderDirection, OrderType, OrderStatus, TransactionStatus
from .ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ...core.db import add_instance, get_instance, update_instance


class AccountInterface(ReadOnlyAccountInterface):
    """
    Abstract base class for trading account interfaces.

    Extends ReadOnlyAccountInterface with trading capabilities: order submission,
    cancellation, modification, and TP/SL management.

    Subclasses must implement all abstract methods to support order management,
    position tracking, and broker synchronization. All trading account plugins
    should inherit from this class.

    For read-only broker integrations, inherit from ReadOnlyAccountInterface instead.
    """

    # Trading accounts support trading operations
    supports_trading = True


    @abstractmethod
    def _submit_order_impl(self, trading_order, tp_price: Optional[float] = None, sl_price: Optional[float] = None, is_closing_order: bool = False) -> Any:
        """
        Internal implementation of order submission. This method should be implemented by child classes.
        The public submit_order method will call this after validation.

        Args:
            trading_order: A validated TradingOrder object containing all order details.
            tp_price: Optional take profit price for bracket orders (broker-specific support).
            sl_price: Optional stop loss price for bracket orders (broker-specific support).
            is_closing_order: If True, this order closes an existing position (skip hedging checks).

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        pass

    def _generate_tracking_comment(self, trading_order: TradingOrder) -> str:
        """
        Preserve the original comment without modification.
        No longer needs to generate unique tracking prefixes since we use order ID as client_order_id.
        
        Args:
            trading_order: The TradingOrder object
            
        Returns:
            str: The original comment as-is (no length limit, no epoch prepending)
        """
        # Simply return the original comment, or empty string if None
        return trading_order.comment or ""

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
        
        # Set account_id BEFORE saving to DB
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
        result = self._submit_order_impl(trading_order, tp_price=tp_price, sl_price=sl_price, is_closing_order=is_closing_order)
        
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
                        self.adjust_tp_sl(transaction, tp_price, sl_price, source="initial_setup")
                    except NotImplementedError:
                        logger.warning(f"Broker {self.__class__.__name__} does not implement adjust_tp_sl - TP/SL not set")
                elif tp_price:
                    # Only TP provided - use adjust_tp for OTO order
                    logger.debug(f"Creating TP order for transaction {transaction.id} via adjust_tp")
                    try:
                        self.adjust_tp(transaction, tp_price, source="initial_setup")
                    except NotImplementedError:
                        logger.warning(f"Broker {self.__class__.__name__} does not implement adjust_tp - TP not set")
                elif sl_price:
                    # Only SL provided - use adjust_sl for OTO order
                    logger.debug(f"Creating SL order for transaction {transaction.id} via adjust_sl")
                    try:
                        self.adjust_sl(transaction, sl_price, source="initial_setup")
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
        # Entry order types that can auto-create a transaction (opening a new position)
        entry_order_types = {OrderType.MARKET, OrderType.BUY_LIMIT, OrderType.SELL_LIMIT,
                             OrderType.BUY_STOP, OrderType.SELL_STOP,
                             OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT}
        is_entry_order = (hasattr(trading_order, 'order_type') and
                          trading_order.order_type in entry_order_types)

        # Check if transaction_id is provided
        has_transaction = (hasattr(trading_order, 'transaction_id') and
                          trading_order.transaction_id is not None)

        if is_entry_order and not has_transaction:
            # Automatically create Transaction for entry orders without transaction_id
            self._create_transaction_for_order(trading_order)
            logger.info(f"Automatically created transaction {trading_order.transaction_id} for {trading_order.order_type.value} order")

        elif not is_entry_order and not has_transaction:
            # Exit/close orders must be attached to an existing transaction
            raise ValueError(f"Non-entry orders ({trading_order.order_type.value if trading_order.order_type else 'unknown'}) must be attached to an existing transaction. No transaction_id provided.")
        
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
                quantity=trading_order.quantity,  # Always positive
                side=trading_order.side,  # BUY for LONG, SELL for SHORT
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
            
            # Get transaction side from side field
            # BUY = LONG transaction, SELL = SHORT transaction
            target_side = transaction.side
            
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
                
                # Quantity is always positive - direction field indicates LONG/SHORT
                # No need to negate for SELL transactions
                
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

    def _get_expert_settings_for_validation(self, expert_instance) -> Optional[Dict[str, Any]]:
        """
        Load expert settings from database for validation.
        
        Args:
            expert_instance: The ExpertInstance object
            
        Returns:
            Optional[Dict[str, Any]]: Settings dictionary or None if error
        """
        try:
            import importlib
            from ..models import ExpertSetting
            from sqlmodel import select
            from ..db import get_db
            
            expert_module_name = f"ba2_trade_platform.modules.experts.{expert_instance.expert}"
            expert_module = importlib.import_module(expert_module_name)
            expert_class = getattr(expert_module, expert_instance.expert)
            
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
                
                return settings
                
        except (ImportError, AttributeError) as e:
            logger.warning(f"Could not load expert {expert_instance.expert} for position size validation: {e}")
            return None
    
    def _validate_single_position_size(self, trading_order: TradingOrder, transaction, expert_instance,
                                       current_price: float, max_position_pct: float, 
                                       virtual_equity: float) -> List[str]:
        """
        Validate that position size doesn't exceed expert's per-instrument limit.
        
        Args:
            trading_order: The order to validate
            transaction: The transaction associated with the order
            expert_instance: The expert instance
            current_price: Current market price
            max_position_pct: Maximum position percentage setting
            virtual_equity: Expert's virtual equity
            
        Returns:
            List[str]: List of error messages (empty if valid)
        """
        errors = []
        max_position_value = virtual_equity * (max_position_pct / 100.0)
        
        # Get current position size from the transaction
        current_position_qty = abs(transaction.quantity or 0)
        current_position_value = current_position_qty * current_price
        
        # Check if this is adding to an existing position
        is_adding_to_position = False
        if transaction.trading_orders:
            entry_order = transaction.trading_orders[0]
            if entry_order.side == trading_order.side:
                is_adding_to_position = True
        
        if is_adding_to_position:
            # Calculate the new total position value after this order
            new_total_qty = current_position_qty + trading_order.quantity
            new_total_value = new_total_qty * current_price
            
            if new_total_value > max_position_value:
                max_additional_value = max_position_value - current_position_value
                max_additional_qty = int(max_additional_value / current_price) if max_additional_value > 0 else 0
                
                errors.append(
                    f"Adding {trading_order.quantity} shares would bring total position to ${new_total_value:.2f}, "
                    f"exceeding expert's max allowed ${max_position_value:.2f} "
                    f"({max_position_pct:.1f}% of virtual equity ${virtual_equity:.2f}). "
                    f"Current position: {current_position_qty} shares (${current_position_value:.2f}). "
                    f"Can add up to {max_additional_qty} more shares."
                )
                logger.error(
                    f"POSITION SIZE LIMIT EXCEEDED: Adding {trading_order.quantity} shares of {trading_order.symbol} "
                    f"to existing {current_position_qty} shares (new total ${new_total_value:.2f}) "
                    f"exceeds expert {expert_instance.id} limit of ${max_position_value:.2f}"
                )
        else:
            # This is a new position - validate the order quantity directly
            position_value = current_price * trading_order.quantity
            
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
        
        return errors
    
    def _validate_expert_available_balance(self, trading_order: TradingOrder, transaction,
                                           expert_instance, current_price: float) -> List[str]:
        """
        Validate that order doesn't exceed expert's available virtual balance (defense-in-depth).
        
        Args:
            trading_order: The order to validate
            transaction: The transaction associated with the order
            expert_instance: The expert instance
            current_price: Current market price
            
        Returns:
            List[str]: List of error messages (empty if valid)
        """
        errors = []
        
        try:
            from ..utils import get_expert_instance_from_id
            
            expert_interface = get_expert_instance_from_id(transaction.expert_id)
            if not expert_interface:
                return errors
            
            available_balance = expert_interface.get_available_balance()
            if available_balance is None:
                return errors
            
            # Check if adding to existing position
            is_adding_to_position = False
            if transaction.trading_orders:
                entry_order = transaction.trading_orders[0]
                if entry_order.side == trading_order.side:
                    is_adding_to_position = True
            
            if not is_adding_to_position:
                # New position - check if order value exceeds available balance
                order_value = current_price * trading_order.quantity
                if order_value > available_balance:
                    errors.append(
                        f"Order value ${order_value:.2f} exceeds expert's available balance ${available_balance:.2f}. "
                        f"Close existing positions or increase virtual equity percentage to allow this trade."
                    )
                    logger.error(
                        f"EXPERT BALANCE EXCEEDED: Order for {trading_order.quantity} shares of {trading_order.symbol} "
                        f"(${order_value:.2f}) exceeds expert {expert_instance.id} available balance ${available_balance:.2f}"
                    )
            else:
                # Adding to position - check if additional value exceeds available balance
                additional_value = trading_order.quantity * current_price
                if additional_value > available_balance:
                    errors.append(
                        f"Adding ${additional_value:.2f} exceeds expert's available balance ${available_balance:.2f}. "
                        f"Close existing positions or increase virtual equity percentage."
                    )
                    logger.error(
                        f"EXPERT BALANCE EXCEEDED: Adding {trading_order.quantity} shares of {trading_order.symbol} "
                        f"(${additional_value:.2f}) exceeds expert {expert_instance.id} available balance ${available_balance:.2f}"
                    )
        except Exception as balance_error:
            logger.warning(f"Error checking expert available balance: {balance_error}")
        
        return errors

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
            from ..models import Transaction, ExpertInstance
            
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
            settings = self._get_expert_settings_for_validation(expert_instance)
            if not settings:
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
            
            # Get current price
            current_price = self.get_instrument_current_price(trading_order.symbol)
            if current_price is None:
                logger.warning(f"Could not get current price for {trading_order.symbol} in position size validation")
                return errors
            
            # Validate position size limits
            position_size_errors = self._validate_single_position_size(
                trading_order, transaction, expert_instance,
                current_price, max_position_pct, virtual_equity
            )
            errors.extend(position_size_errors)
            
            # Validate expert available balance (defense-in-depth)
            balance_errors = self._validate_expert_available_balance(
                trading_order, transaction, expert_instance, current_price
            )
            errors.extend(balance_errors)
                
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
    
    @abstractmethod
    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """Adjust take profit for a transaction. Must be implemented by each broker."""
        pass
    
    @abstractmethod
    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """Adjust stop loss for a transaction. Must be implemented by each broker."""
        pass
    
    @abstractmethod
    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: float | None = None, new_sl_price: float | None = None, source: str = "") -> bool:
        """Adjust take profit and/or stop loss for a transaction. Must be implemented by each broker."""
        pass

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


    # refresh_positions, refresh_orders, and refresh_transactions are inherited from ReadOnlyAccountInterface

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
                        # Determine closing side (opposite of position side)
                        # BUY position closes with SELL, SELL position closes with BUY
                        close_side = OrderDirection.SELL if transaction.side == OrderDirection.BUY else OrderDirection.BUY
                        
                        close_order = TradingOrder(
                            account_id=self.id,
                            symbol=transaction.symbol,
                            quantity=transaction.quantity,  # Already positive
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
                                quantity=transaction.quantity,  # Already positive
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
