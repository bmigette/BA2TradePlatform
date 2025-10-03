from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, LimitOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from typing import Any, Dict, Optional
from datetime import datetime, timezone

from ...logger import logger
from ...core.models import TradingOrder, Position, Transaction
from ...core.types import OrderDirection, OrderStatus, OrderOpenType
from ...core.AccountInterface import AccountInterface
from ...core.db import get_db, update_instance
from sqlmodel import Session, select

class AlpacaAccount(AccountInterface):
    """
    A class that implements the AccountInterface for interacting with Alpaca trading accounts.
    This class provides methods for managing orders, positions, and account information through
    the Alpaca trading API.
    """
    def __init__(self, id: int):
        """
        Initialize the AlpacaAccount with API credentials.
        Establishes connection with Alpaca trading API using credentials from config.

        Args:
            id (int): The unique identifier for the account.
        
        Raises:
            Exception: If initialization of Alpaca TradingClient fails.
        """
        super().__init__(id)

        try:
         
            self.client = TradingClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"],
                paper=self.settings["paper_account"], # True if "paper" in APCA_API_BASE_URL else False
            )
            logger.info("Alpaca TradingClient initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Alpaca TradingClient: {e}", exc_info=True)
            raise
        
    def get_settings_definitions() -> Dict[str, Any]:
        """
        Return the settings definitions required for AlpacaAccount.

        Returns:
            dict: Dictionary with setting names and their types.
        """
        return {
            "api_key": {"type": 'str', "required": True, "description": "Alpaca API Key ID"},
            "api_secret": {"type": 'str', "required": True, "description": "Alpaca API Secret Key"},
            "paper_account": {"type": 'bool', "required": True, "description": "Is this a paper trading account?"}
        }
    
    @staticmethod
    def _round_price(price: float, symbol: str = None) -> float:
        """
        Round price to comply with Alpaca's pricing requirements.
        
        Alpaca pricing rules:
        - Stocks >= $1: Round to 2 decimal places (penny increments only)
        - Stocks < $1: Round to 4 decimal places (sub-penny allowed)
        
        Args:
            price: The price to round
            symbol: Optional symbol for logging
            
        Returns:
            float: Rounded price
        """
        if price is None:
            return None
        
        # For stocks >= $1, round to 2 decimal places (penny increments)
        # For stocks < $1, round to 4 decimal places (sub-penny allowed)
        if price >= 1.0:
            rounded = round(price, 2)
        else:
            rounded = round(price, 4)
        
        if rounded != price:
            logger.debug(f"Rounded price from {price} to {rounded}" + (f" for {symbol}" if symbol else ""))
        
        return rounded
    
    def alpaca_order_to_tradingorder(self, order):
        """
        Convert an Alpaca order object to a TradingOrder object.
        """
        # Helper function to sanitize enum fields
        def sanitize_enum_field(value, enum_class, field_name, nullable=True, default_value=None):
            """
            Sanitize enum values with proper logging and error handling.
            
            Args:
                value: The value to sanitize
                enum_class: The enum class to validate against
                field_name: Name of the field for logging
                nullable: Whether the field can be None
                default_value: Default value if sanitization fails (only for non-nullable fields)
            
            Returns:
                Sanitized enum value or None/default_value
            
            Raises:
                ValueError: If field is not nullable and sanitization fails
            """
            if value is None:
                if nullable:
                    return None
                elif default_value is not None:
                    return default_value
                else:
                    raise ValueError(f"Required enum field '{field_name}' cannot be None")
            
            # Handle Alpaca enum objects - extract the .value attribute
            if hasattr(value, 'value'):
                str_value = str(value.value).lower()
            else:
                # Convert value to string for comparison
                str_value = str(value).lower()
            
            # Try to find matching enum value (case-insensitive)
            for enum_item in enum_class:
                if enum_item.value.lower() == str_value:
                    return enum_item
            
            # Special handling for OrderStatus
            if enum_class == OrderStatus:
                if str_value in ['unknown', 'invalid', '']:
                    return OrderStatus.UNKNOWN
                else:
                    logger.warning(f"Unknown Alpaca order status '{value}' for field '{field_name}', setting to UNKNOWN")
                    return OrderStatus.UNKNOWN
            
            # For other enums, log warning and handle based on nullability
            if nullable:
                logger.warning(f"Unknown value '{value}' for enum field '{field_name}', setting to None")
                return None
            elif default_value is not None:
                logger.warning(f"Unknown value '{value}' for required enum field '{field_name}', using default value")
                return default_value
            else:
                raise ValueError(f"Unknown value '{value}' for required enum field '{field_name}' and no default provided")
        
        # Sanitize enum fields
        side = sanitize_enum_field(
            getattr(order, "side", None), 
            OrderDirection, 
            "side", 
            nullable=False
        )
        
        order_type = sanitize_enum_field(
            getattr(order, "type", None), 
            OrderType, 
            "order_type", 
            nullable=False, 
            default_value=OrderType.MARKET
        )
        
        status = sanitize_enum_field(
            getattr(order, "status", None), 
            OrderStatus, 
            "status", 
            nullable=False, 
            default_value=OrderStatus.UNKNOWN
        )
        
        return TradingOrder(
            broker_order_id=str(getattr(order, "id", None)) if getattr(order, "id", None) else None,  # Set Alpaca order ID as broker_order_id
            symbol=getattr(order, "symbol", None),
            quantity=getattr(order, "qty", None),
            side=side,
            order_type=order_type,
            good_for=getattr(order, "time_in_force", None),
            limit_price=getattr(order, "limit_price", None),
            stop_price=getattr(order, "stop_price", None),
            status=status,
            filled_qty=getattr(order, "filled_qty", None),
            comment=getattr(order, "client_order_id", None),
            created_at=getattr(order, "created_at", None),
        )
    
    def alpaca_position_to_position(self, position):
        """
        Convert an Alpaca position object to a Position object.
        
        Args:
            position: An Alpaca position object containing position details.
            
        Returns:
            Position: A Position object containing the position information.
        """
        return Position(
            symbol=getattr(position, "symbol", None),
            qty=float(getattr(position, "qty")) if getattr(position, "qty") is not None else None,
            qty_available=float(getattr(position, "qty_available")) if getattr(position, "qty_available") is not None else None,
            avg_entry_price=float(getattr(position, "avg_entry_price")) if getattr(position, "avg_entry_price") is not None else None,
            avg_entry_swap_rate=float(getattr(position, "avg_entry_swap_rate")) if getattr(position, "avg_entry_swap_rate") is not None else None,
            current_price=float(getattr(position, "current_price")) if getattr(position, "current_price") is not None else None,
            lastday_price=float(getattr(position, "lastday_price")) if getattr(position, "lastday_price") is not None else None,
            change_today=float(getattr(position, "change_today")) if getattr(position, "change_today") is not None else None,
            unrealized_pl=float(getattr(position, "unrealized_pl")) if getattr(position, "unrealized_pl") is not None else None,
            unrealized_plpc=float(getattr(position, "unrealized_plpc")) if getattr(position, "unrealized_plpc") is not None else None,
            unrealized_intraday_pl=float(getattr(position, "unrealized_intraday_pl")) if getattr(position, "unrealized_intraday_pl") is not None else None,
            unrealized_intraday_plpc=float(getattr(position, "unrealized_intraday_plpc")) if getattr(position, "unrealized_intraday_plpc") is not None else None,
            market_value=float(getattr(position, "market_value")) if getattr(position, "market_value") is not None else None,
            cost_basis=float(getattr(position, "cost_basis")) if getattr(position, "cost_basis") is not None else None,
            side=OrderDirection.BUY if getattr(position, "side") == "long" or getattr(position, "side") == "buy" else OrderDirection.SELL,
            exchange=getattr(position, "exchange", None),
            asset_class=getattr(position, "asset_class", None),
            swap_rate=getattr(position, "swap_rate", None)
        )
        
    def get_orders(self, status: Optional[OrderStatus] = OrderStatus.ALL): # TODO: Add filter handling
        """
        Retrieve a list of orders based on the provided filter.
        
        Args:
            filter (dict, optional): Filter criteria for orders. Defaults to {}.
            
        Returns:
            list: A list of TradingOrder objects representing the orders.
            Returns empty list if an error occurs.
        """
        try:
            filter = GetOrdersRequest(
                status=status
            )
            alpaca_orders = self.client.get_orders(filter)
            orders = [self.alpaca_order_to_tradingorder(order) for order in alpaca_orders]
            logger.debug(f"Listed {len(orders)} Alpaca Orders.")
            return orders
        except Exception as e:
            logger.error(f"Error listing Alpaca orders: {e}", exc_info=True)
            return []

    def _submit_order_impl(self, trading_order: TradingOrder) -> TradingOrder:
        """
        Submit a new order to Alpaca.
        
        Args:
            trading_order: A TradingOrder object containing all order details
            depends_on_order_id (Optional[int]): ID of the order this order depends on (for conditional orders)
            depends_on_status (Optional[str]): Status that the depends_on_order must reach to trigger this order
            good_for (str): Time in force for the order (e.g., "gtc", "day").
            open_type (OrderOpenType): Specifies how the order should be opened (automatic/manual).
            
        Returns:
            TradingOrder: The created order if successful, None if an error occurs.
        """
        try:
            # Log dependency information if provided
            if trading_order.depends_on_order is not None:
                logger.info(f"Submitting order with dependency: depends on order {trading_order.depends_on_order} reaching status {trading_order.depends_order_status_trigger}")
            
            # Round all price fields to 4 decimal places to comply with Alpaca pricing requirements
            # This prevents sub-penny increment errors from floating-point arithmetic
            if trading_order.limit_price is not None:
                trading_order.limit_price = self._round_price(trading_order.limit_price, trading_order.symbol)
            if trading_order.stop_price is not None:
                trading_order.stop_price = self._round_price(trading_order.stop_price, trading_order.symbol)

            # Convert string values to enums
            side = OrderSide.BUY if trading_order.side == OrderDirection.BUY else OrderSide.SELL
            # Map good_for to TimeInForce enum
            good_for_value = (trading_order.good_for or '').lower()
            tif_map = {
                'day': TimeInForce.DAY,
                'gtc': TimeInForce.GTC,
                'opg': TimeInForce.OPG,
                'ioc': TimeInForce.IOC,
                'fok': TimeInForce.FOK,
                'cls': TimeInForce.CLS,
            }
            time_in_force = tif_map.get(good_for_value, TimeInForce.GTC)
            
            # Get order type value - handle both enum and string
            if hasattr(trading_order.order_type, 'value'):
                order_type_value = trading_order.order_type.value.lower()
            else:
                order_type_value = str(trading_order.order_type).lower()
            
            # Import OrderType enum from core.types to compare values
            from ...core.types import OrderType as CoreOrderType
            
            # Create the appropriate order request based on order type
            if order_type_value == CoreOrderType.MARKET.value.lower():
                order_request = MarketOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    client_order_id=trading_order.comment
                )
            elif order_type_value in [CoreOrderType.BUY_LIMIT.value.lower(), 
                                      CoreOrderType.SELL_LIMIT.value.lower()]:
                if not trading_order.limit_price:
                    logger.error(f"Limit price is required for limit orders")
                    return None
                    
                order_request = LimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    limit_price=trading_order.limit_price,
                    client_order_id=trading_order.comment
                )
            elif order_type_value in [CoreOrderType.BUY_STOP.value.lower(), 
                                      CoreOrderType.SELL_STOP.value.lower()]:
                if not trading_order.stop_price:
                    logger.error(f"Stop price is required for stop orders")
                    return None
                    
                order_request = StopOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    stop_price=trading_order.stop_price,
                    client_order_id=trading_order.comment
                )
            else:
                logger.error(f"Unsupported order type: {trading_order.order_type} (value: {order_type_value})")
                return None

            order = self.client.submit_order(order_request)
            logger.info(f"Submitted Alpaca order: {order.id}")

            # Convert to TradingOrder and set the broker_order_id
            result_order = self.alpaca_order_to_tradingorder(order)
            
            # Update the order in the database using a fresh session
            from sqlmodel import Session
            with Session(get_db().bind) as session:
                # Re-fetch the order from the database to get a fresh instance
                if trading_order.id:
                    fresh_order = session.get(TradingOrder, trading_order.id)
                    if fresh_order:
                        fresh_order.broker_order_id = str(order.id) if order.id else None
                        fresh_order.status = result_order.status if result_order.status else None
                        session.add(fresh_order)
                        session.commit()
                        session.refresh(fresh_order)
                        return fresh_order
                    else:
                        logger.warning(f"Could not find order {trading_order.id} to update")
                        return trading_order
                else:
                    logger.warning(f"Order has no ID to update in database")
                    return trading_order
        except Exception as e:
            logger.error(f"Error creating Alpaca order: {e}", exc_info=True)
            
            # Mark the order as ERROR in the database using a fresh session
            try:
                from sqlmodel import Session
                
                # Use a fresh session to avoid session conflicts
                with Session(get_db().bind) as session:
                    # Re-fetch the order from the database to get a fresh instance
                    if trading_order.id:
                        fresh_order = session.get(TradingOrder, trading_order.id)
                        if fresh_order:
                            fresh_order.status = OrderStatus.ERROR
                            # Store error details in comment field
                            if not fresh_order.comment:
                                fresh_order.comment = f"Error: {str(e)[:200]}"
                            else:
                                fresh_order.comment = f"{fresh_order.comment} | Error: {str(e)[:150]}"
                            session.add(fresh_order)
                            session.commit()
                            logger.info(f"Marked order {trading_order.id} as ERROR in database")
                        else:
                            logger.warning(f"Could not find order {trading_order.id} to mark as ERROR")
                    else:
                        logger.warning(f"Cannot mark order as ERROR - order has no ID yet")
            except Exception as update_error:
                logger.error(f"Failed to update order status to ERROR: {update_error}", exc_info=True)
            
            return None

    def modify_order(self, order_id: str, trading_order: TradingOrder):
        """
        Modify an existing order in Alpaca.
        
        Args:
            order_id (str): The ID of the order to modify.
            trading_order (TradingOrder): The new order details.
            
        Returns:
            TradingOrder: The modified order if successful, None if an error occurs.
        """
        try:
            # Round all price fields to 4 decimal places to comply with Alpaca pricing requirements
            limit_price = self._round_price(trading_order.limit_price, trading_order.symbol) if trading_order.limit_price is not None else None
            stop_price = self._round_price(trading_order.stop_price, trading_order.symbol) if trading_order.stop_price is not None else None
            
            order = self.client.replace_order(
                order_id=order_id,
                qty=trading_order.quantity,
                time_in_force=trading_order.good_for,
                limit_price=limit_price,
                stop_price=stop_price
            )
            logger.info(f"Modified Alpaca order: {order.id}")
            return self.alpaca_order_to_tradingorder(order)
        except Exception as e:
            logger.error(f"Error modifying Alpaca order {order_id}: {e}", exc_info=True)
            return None

    def get_order(self, order_id: str):
        """
        Retrieve a specific order by its ID.
        
        Args:
            order_id (str): The ID of the order to retrieve.
            
        Returns:
            TradingOrder: The requested order if found, None if an error occurs.
        """
        try:
            order = self.client.get_order_by_id(order_id)
            logger.debug(f"Fetched Alpaca order: {order.id}")
            return self.alpaca_order_to_tradingorder(order)
        except Exception as e:
            logger.error(f"Error fetching Alpaca order {order_id}: {e}", exc_info=True)
            return None
        
    def cancel_order(self, order_id: str):
        """
        Cancel an existing order.
        
        Args:
            order_id (str): The ID of the order to cancel.
            
        Returns:
            bool: True if cancellation was successful, False otherwise.
        """
        try:
            self.client.cancel_order(order_id)
            logger.info(f"Cancelled Alpaca order: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Error cancelling Alpaca order {order_id}: {e}", exc_info=True)
            return False

    def get_positions(self):
        """
        Retrieve all current positions in the Alpaca account.
        
        Returns:
            list: A list of position objects if successful, empty list if an error occurs.
        """
        try:
            positions = self.client.get_all_positions()
            logger.debug(f"Listed {len(positions)} Alpaca positions.")
            return [self.alpaca_position_to_position(position) for position in positions]
        except Exception as e:
            logger.error(f"Error listing Alpaca positions: {e}", exc_info=True)
            return []

    def get_balance(self) -> Optional[float]:
        """
        Get the current account balance/equity from Alpaca.
        
        Returns:
            Optional[float]: The current account equity if available, None if error occurred
        """
        try:
            account = self.client.get_account()
            if account and hasattr(account, 'equity'):
                balance = float(account.equity)
                logger.debug(f"Alpaca account balance: ${balance}")
                return balance
            else:
                logger.warning("No equity field found in Alpaca account info")
                return None
        except Exception as e:
            logger.error(f"Error getting Alpaca account balance: {e}", exc_info=True)
            return None

    def get_account_info(self):
        """
        Retrieve current account information from Alpaca.
        
        Returns:
            object: Account information if successful, None if an error occurs.
        """
        try:
            account = self.client.get_account()
            logger.debug("Fetched Alpaca account info.")
            return account
        except Exception as e:
            logger.error(f"Error fetching Alpaca account info: {e}", exc_info=True)
            return None

    def get_instrument_current_price(self, symbol: str) -> Optional[float]:
        """
        Get the current market price for a given instrument/symbol.
        
        Args:
            symbol (str): The asset symbol to get the price for
        
        Returns:
            Optional[float]: The current price if available, None if not found or error occurred
        """
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            
            # Create data client for market data
            data_client = StockHistoricalDataClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"]
            )
            
            # Get latest quote
            request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = data_client.get_stock_latest_quote(request)
            
            if symbol in quotes:
                quote = quotes[symbol]
                # Use bid-ask midpoint as current price
                if quote.bid_price and quote.ask_price:
                    current_price = (float(quote.bid_price) + float(quote.ask_price)) / 2
                elif quote.bid_price:
                    current_price = float(quote.bid_price)
                elif quote.ask_price:
                    current_price = float(quote.ask_price)
                else:
                    current_price = None
                
                logger.debug(f"Current price for {symbol}: {current_price}")
                return current_price
            else:
                logger.warning(f"No quote data found for symbol {symbol}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}", exc_info=True)
            return None
        
        
    def refresh_positions(self) -> bool:
        """
        Refresh/synchronize account positions from Alpaca broker.
        This method updates any cached position data with fresh data from the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:
            positions = self.get_positions()
            logger.info(f"Successfully refreshed {len(positions)} positions from Alpaca")
            return True
        except Exception as e:
            logger.error(f"Error refreshing positions from Alpaca: {e}", exc_info=True)
            return False

    def refresh_orders(self) -> bool:
        """
        Refresh/synchronize account orders from Alpaca broker.
        This method updates database records with current order states from the broker.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:

            
            # Get all orders from Alpaca
            alpaca_orders = self.get_orders(OrderStatus.ALL)
            
            if not alpaca_orders:
                logger.warning("No orders returned from Alpaca during refresh")
                return True
            
            updated_count = 0
            
            # Update database records with current Alpaca order states
            with Session(get_db().bind) as session:
                for alpaca_order in alpaca_orders:
                    if not alpaca_order.broker_order_id:
                        continue
                    # Find corresponding database record
                    statement = select(TradingOrder).where(
                        TradingOrder.broker_order_id == alpaca_order.broker_order_id
                    )
                    db_order = session.exec(statement).first()
                    if db_order:
                        # Track if any changes were made
                        has_changes = False
                        
                        # Update database order with current Alpaca state only if values differ
                        if db_order.status != alpaca_order.status:
                            logger.debug(f"Order {db_order.id} status changed: {db_order.status} -> {alpaca_order.status}")
                            db_order.status = alpaca_order.status
                            has_changes = True
                        
                        if db_order.filled_qty is None or float(db_order.filled_qty) != float(alpaca_order.filled_qty):
                            logger.debug(f"Order {db_order.id} filled_qty changed: {db_order.filled_qty} -> {alpaca_order.filled_qty}")
                            db_order.filled_qty = alpaca_order.filled_qty
                            has_changes = True
                        
                        # Update broker_order_id if it wasn't set before
                        if not db_order.broker_order_id:
                            logger.debug(f"Order {db_order.id} broker_order_id set to: {alpaca_order.broker_order_id}")
                            db_order.broker_order_id = alpaca_order.broker_order_id
                            has_changes = True
                        
                        # Only add to session and increment counter if there were actual changes
                        if has_changes:
                            session.add(db_order)
                            updated_count += 1
                            logger.debug(f"Updated database order {db_order.id} with changes from Alpaca order {alpaca_order.broker_order_id}")
                
                session.commit()
            
            logger.info(f"Successfully refreshed orders from Alpaca: {updated_count} database records updated")
            return True
            
        except Exception as e:
            logger.error(f"Error refreshing orders from Alpaca: {e}", exc_info=True)
            return False

    def _set_order_tp_impl(self, trading_order: TradingOrder, tp_price: float) -> TradingOrder:
        """
        Set take profit for an order by creating a WAITING_TRIGGER TP order.
        
        Logic:
        - Creates a TP order with WAITING_TRIGGER status
        - Order will be automatically submitted when parent order reaches FILLED status
        - Quantity will be set at trigger time based on parent order's filled quantity
        
        Args:
            trading_order: The original TradingOrder object
            tp_price: The take profit price
            
        Returns:
            TradingOrder: The created WAITING_TRIGGER take profit order
        """
        try:
            from ...core.db import add_instance, get_instance
            from ...core.types import OrderStatus
            
            # Round take profit price to 4 decimal places
            tp_price = self._round_price(tp_price, trading_order.symbol)
            
            # Check if there's already a take profit order for this transaction
            existing_tp_order = self._find_existing_tp_order(trading_order.transaction_id)
            
            if existing_tp_order:
                # Update existing take profit order price
                logger.info(f"Updating existing TP order {existing_tp_order.id} to price ${tp_price}")
                existing_tp_order.limit_price = tp_price
                from ...core.db import update_instance
                update_instance(existing_tp_order)
                logger.info(f"Successfully updated TP order to ${tp_price}")
                return existing_tp_order
            
            # Create new WAITING_TRIGGER take profit order
            tp_order = self._create_tp_order_object(trading_order, tp_price)
            
            # Save to database - it will be submitted when parent order is FILLED
            tp_order_id = add_instance(tp_order)
            tp_order.id = tp_order_id
            
            logger.info(f"Created WAITING_TRIGGER TP order {tp_order_id} at ${tp_price} (will submit when order {trading_order.id} is FILLED)")
            return tp_order
                
        except Exception as e:
            logger.error(f"Error setting take profit for order {trading_order.id}: {e}", exc_info=True)
            raise
    
    def _find_existing_tp_order(self, transaction_id: int) -> Optional[TradingOrder]:
        """
        Find existing take profit order for a transaction.
        
        Args:
            transaction_id: The transaction ID to search for
            
        Returns:
            Optional[TradingOrder]: Existing TP order if found, None otherwise
        """
        try:
            with Session(get_db().bind) as session:
                # Import OrderType enum from core.types
                from ...core.types import OrderType as CoreOrderType
                
                # Look for limit orders linked to the same transaction
                # Take profit orders are typically limit orders on the opposite side
                statement = select(TradingOrder).where(
                    TradingOrder.transaction_id == transaction_id,
                    TradingOrder.order_type.in_([CoreOrderType.BUY_LIMIT, CoreOrderType.SELL_LIMIT]),
                    TradingOrder.status.in_([OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER])
                )
                orders = session.exec(statement).all()
                
                # Return the first active limit order (assuming it's the TP order)
                for order in orders:
                    if order.limit_price is not None:
                        return order
                        
                return None
                
        except Exception as e:
            logger.error(f"Error finding existing TP order for transaction {transaction_id}: {e}", exc_info=True)
            return None
    
    def _create_tp_order_object(self, original_order: TradingOrder, tp_price: float) -> TradingOrder:
        """
        Create a take profit order object based on the original order.
        Creates a WAITING_TRIGGER order that will be submitted when the parent order is FILLED.
        Quantity will be set when trigger is hit based on the parent order's filled quantity.
        
        Args:
            original_order: The original trading order
            tp_price: The take profit price
            
        Returns:
            TradingOrder: The take profit order object
        """
        # Import OrderType enum from core.types
        from ...core.types import OrderType as CoreOrderType
        
        # Determine opposite side and appropriate order type for take profit
        if original_order.side == OrderDirection.BUY:
            tp_side = OrderDirection.SELL
            tp_order_type = CoreOrderType.SELL_LIMIT
        else:
            tp_side = OrderDirection.BUY
            tp_order_type = CoreOrderType.BUY_LIMIT
        
        # Create take profit order in WAITING_TRIGGER status
        # Quantity will be set when parent order is FILLED
        tp_order = TradingOrder(
            account_id=self.id,
            symbol=original_order.symbol,
            quantity=0,  # Will be set when trigger is hit
            side=tp_side,  # Opposite side
            order_type=tp_order_type,  # BUY_LIMIT or SELL_LIMIT based on side
            limit_price=tp_price,
            transaction_id=original_order.transaction_id,  # Link to same transaction
            status=OrderStatus.WAITING_TRIGGER,  # Wait for parent order to be FILLED
            depends_on_order=original_order.id,  # Trigger when this order changes
            depends_order_status_trigger=OrderStatus.FILLED,  # Trigger when parent is FILLED
            comment=f"TP for order {original_order.id}",
            created_at=datetime.now(timezone.utc)
        )
        
        return tp_order