from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, LimitOrderRequest, StopOrderRequest, StopLimitOrderRequest, ReplaceOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass
from alpaca.common.exceptions import APIError
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
import time
import functools

from ...logger import logger
from ...core.models import TradingOrder, Position, Transaction
from ...core.types import OrderDirection, OrderStatus, OrderOpenType, OrderType as CoreOrderType
from ...core.interfaces import AccountInterface
from ...core.db import get_db, get_instance, update_instance, add_instance
from sqlmodel import Session, select

def alpaca_api_retry(func):
    """
    Decorator to retry Alpaca API calls with exponential backoff on rate limit errors.
    
    Retries on "too many requests" errors with delays: 1s, 3s, 10s, then fails.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        delays = [1.0, 3.0, 10.0]  # Exponential backoff: 1s, 3s, 10s
        last_exception = None
        
        for attempt in range(len(delays) + 1):  # 4 total attempts (initial + 3 retries)
            try:
                return func(*args, **kwargs)
            except APIError as e:
                last_exception = e
                error_message = str(e).lower()
                
                # Check if this is a rate limit error
                if "too many requests" in error_message or "429" in error_message:
                    if attempt < len(delays):  # Still have retries left
                        delay = delays[attempt]
                        logger.warning(f"Alpaca API rate limit hit in {func.__name__}, retrying in {delay}s (attempt {attempt + 1}/{len(delays) + 1})")
                        time.sleep(delay)
                        continue
                    else:
                        logger.error(f"Alpaca API rate limit exceeded after {len(delays) + 1} attempts in {func.__name__}")
                        raise
                else:
                    # Not a rate limit error, don't retry
                    raise
            except Exception as e:
                # Non-API errors, don't retry
                raise
        
        # This should never be reached, but just in case
        raise last_exception
    
    return wrapper

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
        
        # Initialize client as None first
        self.client = None
        self._authentication_error = None

        try:
            # Check if we have the required settings
            required_settings = ["api_key", "api_secret", "paper_account"]
            missing_settings = [key for key in required_settings if key not in self.settings or self.settings[key] is None]
            
            if missing_settings:
                error_msg = f"Missing required settings: {', '.join(missing_settings)}"
                self._authentication_error = error_msg
                logger.error(f"AlpacaAccount {id}: {error_msg}")
                raise ValueError(error_msg)
         
            self.client = TradingClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"],
                paper=self.settings["paper_account"], # True if "paper" in APCA_API_BASE_URL else False
            )
            logger.info(f"Alpaca TradingClient initialized for account {id}.")
        except Exception as e:
            self._authentication_error = str(e)
            logger.error(f"Failed to initialize Alpaca TradingClient for account {id}: {e}", exc_info=True)
            raise
    
    def _check_authentication(self) -> bool:
        """
        Check if the account is properly authenticated.
        
        Returns:
            bool: True if authenticated, False otherwise
        """
        if self.client is None:
            logger.error(f"AlpacaAccount {self.id}: Not authenticated - {self._authentication_error}")
            return False
        return True
        
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
    
    @staticmethod
    def _generate_tpsl_comment(order_type: str, account_id: int, transaction_id: int, parent_order_id: int) -> str:
        """
        Generate unique timestamp-based comment for TP/SL orders.
        
        Format: timestamp-TYPE-[ACC:XX/TR:YY/PORD:ZZZ]
        where TYPE can be TP, SL, or TPSL
        
        Args:
            order_type: "TP", "SL", or "TPSL"
            account_id: Account ID
            transaction_id: Transaction ID
            parent_order_id: Parent order ID (entry order)
            
        Returns:
            str: Formatted comment string
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{timestamp}-{order_type}-[ACC:{account_id}/TR:{transaction_id}/PORD:{parent_order_id}]"
    
    def _update_existing_oco_legs(self, parent_order: TradingOrder, alpaca_parent_order) -> int:
        """
        Update status and other fields of existing OCO leg orders in the database.
        
        CRITICAL FIX: OCO leg orders are NOT returned by Alpaca's get_orders() API as separate items.
        They are only returned as metadata on the parent OCO order. However, they exist in our database
        as separate TradingOrder records. During refresh_orders(), we must explicitly update these legs
        because they won't be processed by the main loop.
        
        This method:
        1. Finds all leg orders linked to this parent OCO order in the database
        2. For each leg, fetches its current status from Alpaca (via get_order API)
        3. Updates the database record if the status or filled_qty changed
        
        Args:
            parent_order: The parent OCO TradingOrder record
            alpaca_parent_order: The Alpaca order object for the parent OCO
            
        Returns:
            int: Number of leg orders updated
        """
        try:
            from sqlmodel import Session, select
            
            updated_count = 0
            
            # Find all leg orders linked to this parent OCO
            with Session(get_db().bind) as session:
                leg_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.parent_order_id == parent_order.id,
                        TradingOrder.account_id == self.id
                    )
                ).all()
            
            if not leg_orders:
                logger.debug(f"Parent OCO order {parent_order.id} has no linked leg orders in database")
                return 0
            
            #logger.debug(f"Found {len(leg_orders)} leg orders to update for parent OCO {parent_order.id}")
            
            for leg_order in leg_orders:
                if not leg_order.broker_order_id:
                    logger.warning(f"Leg order {leg_order.id} has no broker_order_id, cannot fetch from Alpaca")
                    continue
                
                try:
                    # Fetch the current status of this leg from Alpaca
                    alpaca_leg_order = self.get_order(leg_order.broker_order_id)
                    if not alpaca_leg_order:
                        logger.warning(f"Could not fetch OCO leg order {leg_order.broker_order_id} (order {leg_order.id}) from Alpaca")
                        continue
                    
                    # Check if any fields have changed
                    has_changes = False
                    
                    # Check status
                    if leg_order.status != alpaca_leg_order.status:
                        logger.debug(f"OCO leg order {leg_order.id} status changed: {leg_order.status} -> {alpaca_leg_order.status}")
                        leg_order.status = alpaca_leg_order.status
                        has_changes = True
                    
                    # Check filled_qty
                    if (leg_order.filled_qty is None or 
                        float(leg_order.filled_qty) != float(alpaca_leg_order.filled_qty)):
                        logger.debug(f"OCO leg order {leg_order.id} filled_qty changed: {leg_order.filled_qty} -> {alpaca_leg_order.filled_qty}")
                        leg_order.filled_qty = alpaca_leg_order.filled_qty
                        has_changes = True
                    
                    # Check open_price
                    if (leg_order.open_price is None and alpaca_leg_order.open_price is not None) or \
                       (leg_order.open_price is not None and 
                        (alpaca_leg_order.open_price is None or 
                         float(leg_order.open_price) != float(alpaca_leg_order.open_price))):
                        logger.debug(f"OCO leg order {leg_order.id} open_price changed: {leg_order.open_price} -> {alpaca_leg_order.open_price}")
                        leg_order.open_price = alpaca_leg_order.open_price
                        has_changes = True
                    
                    # Persist changes if any
                    if has_changes:
                        update_instance(leg_order)
                        updated_count += 1
                        logger.info(f"Updated OCO leg order {leg_order.id}: status={leg_order.status}, filled_qty={leg_order.filled_qty}")
                    
                except Exception as e:
                    logger.error(f"Error updating OCO leg order {leg_order.id}: {e}", exc_info=True)
                    continue
            
            return updated_count
            
        except Exception as e:
            logger.error(f"Error in _update_existing_oco_legs for parent order {parent_order.id}: {e}", exc_info=True)
            return 0
    
    def _insert_oco_legs_from_broker_ids(self, parent_order: TradingOrder, legs_broker_ids: list[str]) -> int:
        """
        Insert OCO leg orders by fetching them from Alpaca using their broker IDs.
        
        Used during account refresh when we have the leg broker IDs but not the full leg objects.
        Fetches each leg from Alpaca and creates database records linked to the parent OCO order.
        
        Args:
            parent_order: The parent OCO TradingOrder record
            legs_broker_ids: List of broker order IDs for the OCO legs (from Alpaca submit response)
            
        Returns:
            int: Number of leg orders successfully inserted
        """
        if not legs_broker_ids:
            return 0
        
        inserted_count = 0
        
        for leg_broker_id in legs_broker_ids:
            try:
                # Check if leg already exists in database to prevent duplicates
                with Session(get_db().bind) as session:
                    existing_leg = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.broker_order_id == leg_broker_id,
                            TradingOrder.account_id == self.id
                        )
                    ).first()
                
                if existing_leg:
                    logger.debug(f"OCO leg {leg_broker_id} already exists in database as order {existing_leg.id}, skipping insertion")
                    continue
                
                # Fetch the leg order from Alpaca
                alpaca_leg_order = self.get_order(leg_broker_id)
                if not alpaca_leg_order:
                    logger.warning(f"Could not fetch OCO leg order {leg_broker_id} from Alpaca")
                    continue
                
                # Determine leg type from the leg order's properties
                is_tp_leg = alpaca_leg_order.order_type in [CoreOrderType.BUY_LIMIT, CoreOrderType.SELL_LIMIT] and alpaca_leg_order.limit_price and not alpaca_leg_order.stop_price
                is_sl_leg = alpaca_leg_order.stop_price
                
                leg_type_label = "TP" if is_tp_leg else ("SL" if is_sl_leg else "LEG")
                
                # Create leg order record with proper linkage to parent OCO order
                leg_order = TradingOrder(
                    account_id=self.id,
                    symbol=parent_order.symbol,
                    quantity=parent_order.quantity,
                    side=alpaca_leg_order.side,
                    order_type=alpaca_leg_order.order_type,
                    broker_order_id=leg_broker_id,
                    limit_price=alpaca_leg_order.limit_price,
                    stop_price=alpaca_leg_order.stop_price,
                    good_for=alpaca_leg_order.good_for,
                    status=alpaca_leg_order.status,
                    filled_qty=alpaca_leg_order.filled_qty,
                    open_price=alpaca_leg_order.open_price,
                    comment=f"{int(datetime.now(timezone.utc).timestamp())}-OCO-{leg_type_label}-[PARENT:{parent_order.id}/BROKER:{parent_order.broker_order_id}]",
                    transaction_id=parent_order.transaction_id,
                    parent_order_id=parent_order.id,  # Link to parent OCO order
                    created_at=alpaca_leg_order.created_at
                )
                
                # Insert into database
                leg_order_id = add_instance(leg_order)
                logger.info(f"Inserted OCO {leg_type_label} leg order {leg_order_id} from broker_id {leg_broker_id}")
                inserted_count += 1
                
            except Exception as e:
                logger.error(f"Error inserting OCO leg {leg_broker_id}: {e}", exc_info=True)
                continue
        
        return inserted_count
    
    def _insert_oco_order_legs(self, alpaca_oco_order, parent_order: TradingOrder, transaction_id: int | None) -> None:
        """
        Extract and insert OCO order legs (TP/SL orders) from Alpaca response into database.
        
        When an OCO order is submitted to Alpaca, the response includes leg orders for TP and SL.
        Per Alpaca API docs: https://docs.alpaca.markets/reference/postorder
        The response includes a 'legs' array where each leg is an order object with:
        - id: broker order ID for the leg
        - side: OrderSide (SELL for take-profit/stop-loss on long entry)
        - type: order type (limit, stop, etc.)
        - limit_price: limit price (for take-profit or stop-loss limit)
        - stop_price: stop price (for stop-loss)
        - status: order status
        - filled_qty: quantity filled
        - filled_avg_price: average fill price
        
        Args:
            alpaca_oco_order: Alpaca order response object (order_class=OCO with legs array)
            parent_order: The parent OCO TradingOrder record
            transaction_id: Transaction ID to link the leg orders to
        """
        try:
            # Check if this is an OCO order with legs array
            if not hasattr(alpaca_oco_order, 'legs'):
                logger.debug(f"OCO order {alpaca_oco_order.id} has no legs attribute")
                return
            
            legs = getattr(alpaca_oco_order, 'legs', None)
            if not legs:
                logger.debug(f"OCO order {alpaca_oco_order.id} legs list is empty")
                return
            
            logger.info(f"Processing {len(legs)} OCO order legs for parent order {parent_order.id} (broker_order_id={alpaca_oco_order.id})")
            
            for leg_index, leg in enumerate(legs):
                try:
                    # Extract leg information from Alpaca response
                    # Each leg is an Order object from alpaca-py library
                    leg_broker_id = str(leg.id) if hasattr(leg, 'id') and leg.id else None
                    
                    if not leg_broker_id:
                        logger.warning(f"OCO leg {leg_index} missing broker ID, skipping")
                        continue
                    
                    # Extract core leg attributes from Alpaca response
                    # Alpaca returns OrderSide enum (lowercase 'buy'/'sell'), convert to OrderDirection enum
                    leg_side_raw = leg.side if hasattr(leg, 'side') else None
                    if leg_side_raw:
                        leg_side_str = str(leg_side_raw).lower()
                        leg_side = OrderDirection.BUY if 'buy' in leg_side_str else OrderDirection.SELL
                    else:
                        leg_side = None
                    
                    leg_status = leg.status if hasattr(leg, 'status') else OrderStatus.UNKNOWN
                    leg_filled_qty = leg.filled_qty if hasattr(leg, 'filled_qty') else None
                    leg_filled_avg_price = leg.filled_avg_price if hasattr(leg, 'filled_avg_price') else None
                    leg_time_in_force = leg.time_in_force if hasattr(leg, 'time_in_force') else None
                    
                    # Extract price information based on leg type
                    # Take-profit leg: has limit_price (SELL at this price), no stop_price
                    # Stop-loss leg: has stop_price and optional limit_price (SELL at limit if stopped)
                    leg_limit_price = leg.limit_price if hasattr(leg, 'limit_price') else None
                    leg_stop_price = leg.stop_price if hasattr(leg, 'stop_price') else None
                    
                    # Determine leg type label for identification in comment
                    if leg_limit_price and not leg_stop_price:
                        leg_type_label = "TP"  # Take profit leg (limit order only)
                        # TP legs are SELL_LIMIT for short/SELL positions, BUY_LIMIT for long/BUY
                        leg_order_type = CoreOrderType.SELL_LIMIT if leg_side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
                    elif leg_stop_price:
                        leg_type_label = "SL"  # Stop loss leg
                        # SL legs are SELL_STOP_LIMIT for short/SELL, BUY_STOP_LIMIT for long/BUY
                        if leg_limit_price:
                            leg_order_type = CoreOrderType.SELL_STOP_LIMIT if leg_side == OrderDirection.SELL else CoreOrderType.BUY_STOP_LIMIT
                        else:
                            leg_order_type = CoreOrderType.SELL_STOP if leg_side == OrderDirection.SELL else CoreOrderType.BUY_STOP
                    else:
                        leg_type_label = "LEG"
                        leg_order_type = CoreOrderType.SELL_LIMIT  # Default to sell limit
                    
                    logger.debug(f"Processing OCO {leg_type_label} leg: id={leg_broker_id}, "
                               f"limit_price={leg_limit_price}, stop_price={leg_stop_price}, "
                               f"status={leg_status}, filled_qty={leg_filled_qty}")
                    
                    # Create leg order record
                    leg_order = TradingOrder(
                        account_id=self.id,
                        symbol=parent_order.symbol,
                        quantity=parent_order.quantity,
                        side=leg_side,
                        order_type=leg_order_type,
                        broker_order_id=leg_broker_id,
                        limit_price=leg_limit_price,
                        stop_price=leg_stop_price,
                        good_for=leg_time_in_force,
                        status=leg_status,
                        filled_qty=leg_filled_qty,
                        open_price=leg_filled_avg_price,  # Average fill price
                        comment=f"{int(datetime.now(timezone.utc).timestamp())}-OCO-{leg_type_label}-[PARENT:{parent_order.id}/BROKER:{alpaca_oco_order.id}]",
                        transaction_id=transaction_id,
                        parent_order_id=parent_order.id,  # Link to parent OCO order
                        created_at=datetime.now(timezone.utc)
                    )
                    
                    # Insert into database
                    leg_order_id = add_instance(leg_order)
                    logger.info(f"Created OCO {leg_type_label} leg order {leg_order_id}: "
                              f"broker_id={leg_broker_id}, status={leg_status}, "
                              f"limit=${leg_limit_price}, stop=${leg_stop_price}, "
                              f"filled_qty={leg_filled_qty}")
                    
                except Exception as leg_error:
                    logger.error(f"Error processing OCO leg {leg_index}: {leg_error}", exc_info=True)
                    continue
            
        except Exception as e:
            logger.error(f"Error inserting OCO order legs for order {parent_order.id}: {e}", exc_info=True)
    
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
        
        # Determine order type: Check for OCO order_class first, then fall back to type field
        # OCO orders have order_class="oco" in Alpaca response, not a type-based designation
        final_order_type = order_type
        legs_broker_ids = None
        
        # Check if order_class contains 'oco' (handles both string and enum representations)
        order_class_val = getattr(order, 'order_class', None)
        is_oco = False
        if order_class_val:
            order_class_str = str(order_class_val).lower()
            is_oco = 'oco' in order_class_str
        
        if is_oco:
            final_order_type = CoreOrderType.OCO
            #logger.debug(f"Order {getattr(order, 'id', 'unknown')} detected as OCO based on order_class field")
            
            # Extract OCO leg broker IDs from the legs array (if present in response)
            if hasattr(order, 'legs') and order.legs:
                legs_broker_ids = [str(leg.id) for leg in order.legs if hasattr(leg, 'id') and leg.id]
                logger.debug(f"OCO order {getattr(order, 'id', 'unknown')} has {len(legs_broker_ids)} legs: {legs_broker_ids}")
        else:
            pass
            #logger.debug(f"Order {getattr(order, 'id', 'unknown')}: order_class check - has attr: {hasattr(order, 'order_class')}, value: {order_class_val}, str: {str(order_class_val).lower() if order_class_val else 'none'}, is oco: {is_oco}")
        
        return TradingOrder(
            broker_order_id=str(getattr(order, "id", None)) if getattr(order, "id", None) else None,  # Set Alpaca order ID as broker_order_id
            symbol=getattr(order, "symbol", None),
            quantity=getattr(order, "qty", None),
            side=side,
            order_type=final_order_type,
            good_for=getattr(order, "time_in_force", None),
            limit_price=getattr(order, "limit_price", None),
            stop_price=getattr(order, "stop_price", None),
            status=status,
            filled_qty=getattr(order, "filled_qty", None),
            open_price=getattr(order, "filled_avg_price", None),  # Use broker's filled_avg_price as open_price
            comment=getattr(order, "client_order_id", None),
            created_at=getattr(order, "created_at", None),
            legs_broker_ids=legs_broker_ids,  # Store OCO leg broker IDs for upstream processing
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
        
    @alpaca_api_retry
    def get_orders(self, status: Optional[OrderStatus] = OrderStatus.ALL, fetch_all: bool = False): # TODO: Add filter handling
        """
        Retrieve a list of orders based on the provided filter.
        
        Args:
            status: Filter by order status. Defaults to ALL.
            fetch_all: If True, fetches ALL orders using date-based pagination. If False, returns first 500 orders.
            
        Returns:
            list: A list of TradingOrder objects representing the orders.
            Returns empty list if an error occurs.
        """
        if not self._check_authentication():
            return []
            
        try:
            limit = 500  # Always use 500 as limit per Alpaca's maximum
            all_orders_dict = {}  # Use dict to deduplicate by broker_order_id
            
            if fetch_all:
                # Paginate through all orders using date-based pagination
                until_date = None  # Start with no date filter (gets most recent orders)
                page = 0
                
                while True:
                    # Build filter with optional until parameter
                    filter_params = {
                        "status": status,
                        "limit": limit
                    }
                    if until_date:
                        filter_params["until"] = until_date
                    
                    filter = GetOrdersRequest(**filter_params)
                    filter.nested = True  # Get nested orders (for OCO)
                    alpaca_orders = self.client.get_orders(filter)
                    
                    # If no orders returned, we've fetched everything
                    if not alpaca_orders:
                        logger.debug(f"No more orders to fetch at page {page + 1}")
                        break
                    
                    # Add orders to dict (deduplicates by broker_order_id)
                    new_order_count = 0
                    oldest_order_date = None
                    
                    for order in alpaca_orders:
                        if order.id:  # Alpaca's order.id is the broker_order_id
                            if order.id not in all_orders_dict:
                                all_orders_dict[order.id] = order
                                new_order_count += 1
                            
                            # Track the oldest order date in this batch
                            if order.created_at:
                                if oldest_order_date is None or order.created_at < oldest_order_date:
                                    oldest_order_date = order.created_at
                    
                    logger.debug(
                        f"Fetched page {page + 1}: {len(alpaca_orders)} orders returned, "
                        f"{new_order_count} new unique orders (total unique: {len(all_orders_dict)})"
                    )
                    
                    # If we got fewer than limit, we've reached the end
                    if len(alpaca_orders) < limit:
                        logger.debug(f"Received fewer than {limit} orders, pagination complete")
                        break
                    
                    # If no new unique orders were added, we're seeing duplicates - stop
                    if new_order_count == 0:
                        logger.debug(f"No new unique orders in this batch, pagination complete")
                        break
                    
                    # Set until_date to oldest order's date - 1 day for next iteration
                    # The 'until' parameter means "fetch orders created BEFORE this date"
                    # So we go backwards in time to get older orders
                    if oldest_order_date:
                        # Subtract 1 day to fetch older orders in next iteration
                        until_date = oldest_order_date - timedelta(days=1)
                        logger.debug(f"Next pagination until date (going backwards): {until_date}")
                    else:
                        # No date found, can't continue pagination
                        logger.warning("No created_at date found in orders, stopping pagination")
                        break
                    
                    page += 1
                    
                    # Safety limit: stop after 100 pages to prevent infinite loops
                    if page >= 100:
                        logger.warning(f"Reached maximum pagination limit of 100 pages, stopping")
                        break
                
                # Convert dict values to list
                alpaca_orders = list(all_orders_dict.values())
                logger.info(f"Fetched {len(alpaca_orders)} unique orders across {page + 1} page(s)")
                
            else:
                # Just fetch first batch (up to 500 orders)
                filter = GetOrdersRequest(
                    status=status,
                    limit=limit
                )
                filter.nested = True  # Get nested orders (for OCO)
                alpaca_orders = self.client.get_orders(filter)
                logger.debug(f"Fetched {len(alpaca_orders)} orders (single page, no pagination)")
            
            # Convert to TradingOrder objects
            orders = [self.alpaca_order_to_tradingorder(order) for order in alpaca_orders]
            logger.debug(f"Converted to {len(orders)} TradingOrder objects")
            return orders
            
        except Exception as e:
            logger.error(f"Error listing Alpaca orders: {e}", exc_info=True)
            return []

    @alpaca_api_retry
    def _submit_order_impl(self, trading_order: TradingOrder, tp_price: Optional[float] = None, sl_price: Optional[float] = None) -> TradingOrder:
        """
        Submit a new order to Alpaca.
        
        Logic:
        1. If order.id is None, create new database record with status PENDING
        2. Submit order to broker (with bracket orders if tp_price and sl_price provided)
        3. Update database record with broker response (broker_order_id, status)
        4. If error occurs, mark order as ERROR in database
        
        Args:
            trading_order: A TradingOrder object containing all order details
            tp_price: Optional take profit price for bracket orders
            sl_price: Optional stop loss price for bracket orders
            
        Returns:
            TradingOrder: The database order record (updated with broker info), or None if failed
        """
        if not self._check_authentication():
            return None
            
        from sqlmodel import Session
        from ...core.db import add_instance
        
        try:
            # Step 1: Create database record if it doesn't exist yet
            if trading_order.id is None:
                logger.debug(f"Order has no ID, creating new database record")
                
                # Set initial status to PENDING
                trading_order.status = OrderStatus.PENDING
                
                # Round prices before saving
                if trading_order.limit_price is not None:
                    trading_order.limit_price = self._round_price(trading_order.limit_price, trading_order.symbol)
                if trading_order.stop_price is not None:
                    trading_order.stop_price = self._round_price(trading_order.stop_price, trading_order.symbol)
                
                # Insert into database
                order_id = add_instance(trading_order)
                trading_order.id = order_id
                logger.info(f"Created new order {order_id} in database with status PENDING")
            else:
                logger.debug(f"Order {trading_order.id} already exists in database")
            
            # Log dependency information if provided
            if trading_order.depends_on_order is not None:
                logger.info(f"Submitting order with dependency: depends on order {trading_order.depends_on_order} reaching status {trading_order.depends_order_status_trigger}")
            
            # Step 2: Submit order to Alpaca broker
            
            # Convert side to Alpaca enum
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
            
            # Note: We do NOT create bracket orders. TP/SL will be handled separately
            # as STOP_LIMIT orders after the entry order fills.
            # The parent AccountInterface.submit_order() will create pending_trigger orders.
            
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
                    raise ValueError("Limit price is required for limit orders")
                
                # Round limit price using Alpaca pricing rules
                rounded_limit_price = self._round_price(trading_order.limit_price, trading_order.symbol)
                    
                order_request = LimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    limit_price=rounded_limit_price,
                    client_order_id=trading_order.comment
                )
            elif order_type_value in [CoreOrderType.BUY_STOP.value.lower(), 
                                      CoreOrderType.SELL_STOP.value.lower()]:
                if not trading_order.stop_price:
                    raise ValueError("Stop price is required for stop orders")
                
                # Round stop price using Alpaca pricing rules
                rounded_stop_price = self._round_price(trading_order.stop_price, trading_order.symbol)
                    
                order_request = StopOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    stop_price=rounded_stop_price,
                    client_order_id=trading_order.comment
                )
            elif order_type_value in [CoreOrderType.BUY_STOP_LIMIT.value.lower(), 
                                      CoreOrderType.SELL_STOP_LIMIT.value.lower()]:
                if not trading_order.stop_price:
                    raise ValueError("Stop price is required for stop-limit orders")
                if not trading_order.limit_price:
                    raise ValueError("Limit price is required for stop-limit orders")
                
                # Round prices using Alpaca pricing rules
                rounded_stop_price = self._round_price(trading_order.stop_price, trading_order.symbol)
                rounded_limit_price = self._round_price(trading_order.limit_price, trading_order.symbol)
                    
                order_request = StopLimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    stop_price=rounded_stop_price,
                    limit_price=rounded_limit_price,
                    client_order_id=trading_order.comment
                )
            elif order_type_value == CoreOrderType.OCO.value.lower():
                # OCO (One-Cancels-Other): Both TP and SL in one submission
                # Per Alpaca API: OCO orders don't have limit_price on main order, only in take_profit/stop_loss legs
                if not trading_order.limit_price or trading_order.limit_price <= 0:
                    logger.error(f"Invalid take profit price for OCO order {trading_order.id}: {trading_order.limit_price}")
                    raise ValueError("Limit price (take profit) is required for OCO orders")
                if not trading_order.stop_price or trading_order.stop_price <= 0:
                    logger.error(f"Invalid stop loss price for OCO order {trading_order.id}: {trading_order.stop_price}")
                    raise ValueError("Stop price (stop loss) is required for OCO orders")
                
                # Round prices using Alpaca pricing rules
                rounded_tp_price = self._round_price(trading_order.limit_price, trading_order.symbol)
                rounded_sl_stop_price = self._round_price(trading_order.stop_price, trading_order.symbol)
                # Stop-loss limit price should be slightly worse than stop price to ensure execution
                rounded_sl_limit_price = self._round_price(
                    rounded_sl_stop_price * 0.995 if side == OrderSide.SELL else rounded_sl_stop_price * 1.005,
                    trading_order.symbol
                )
                
                # OCO order: MarketOrderRequest (no limit_price) with take_profit and stop_loss legs
                order_request = LimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=rounded_tp_price),
                    stop_loss=StopLossRequest(stop_price=rounded_sl_stop_price, limit_price=rounded_sl_limit_price),
                    client_order_id=trading_order.comment
                )
                logger.info(f"Submitting OCO order: TP=${rounded_tp_price:.4f}, SL stop=${rounded_sl_stop_price:.4f} limit=${rounded_sl_limit_price:.4f}")
                
            else:
                raise ValueError(f"Unsupported order type: {trading_order.order_type} (value: {order_type_value})")
            
            logger.debug(f"Submitting Alpaca order: {order_request} (client_order_id={trading_order.comment})")
            alpaca_order = self.client.submit_order(order_request)
            logger.info(f"Successfully submitted order to Alpaca: broker_order_id={alpaca_order.id}")

            # Step 3: Update database record with broker response using thread-safe function
            fresh_order = get_instance(TradingOrder, trading_order.id)
            if fresh_order:
                # Update with broker order ID (only if not already set)
                new_broker_order_id = str(alpaca_order.id) if alpaca_order.id else None
                if fresh_order.broker_order_id and fresh_order.broker_order_id != new_broker_order_id:
                    logger.warning(
                        f"Order {fresh_order.id} already has broker_order_id={fresh_order.broker_order_id}, "
                        f"not overwriting with new value: {new_broker_order_id}"
                    )
                else:
                    fresh_order.broker_order_id = new_broker_order_id
                
                # Update status from broker response
                result_order = self.alpaca_order_to_tradingorder(alpaca_order)
                if result_order.status:
                    fresh_order.status = result_order.status
                
                # Use thread-safe update function with retry logic
                update_instance(fresh_order)
                
                logger.info(f"Updated order {fresh_order.id} in database: broker_order_id={fresh_order.broker_order_id}, status={fresh_order.status}")
                
                # Step 4a: Handle OCO order legs - extract leg order IDs from broker response
                logger.debug(f"Checking for OCO legs: fresh_order.order_type={fresh_order.order_type}, is OCO: {fresh_order.order_type == CoreOrderType.OCO}")
                logger.debug(f"Alpaca order: has order_class={hasattr(alpaca_order, 'order_class')}, value={getattr(alpaca_order, 'order_class', None)}")
                if fresh_order.order_type == CoreOrderType.OCO and alpaca_order.order_class == OrderClass.OCO:
                    logger.info(f"Order {fresh_order.id} is OCO, inserting legs...")
                    self._insert_oco_order_legs(alpaca_order, fresh_order, trading_order.transaction_id)
                else:
                    logger.debug(f"Skipping OCO leg insertion for order {fresh_order.id}")
                
                # Step 4b: Handle TP/SL if provided (delegate to adjust methods which handle pending triggers)
                if tp_price or sl_price:
                    # Get the transaction for this order
                    if fresh_order.transaction_id:
                        from ...core.models import Transaction
                        transaction = get_instance(Transaction, fresh_order.transaction_id)
                        if transaction:
                            # Update transaction TP/SL values if provided
                            if tp_price:
                                transaction.take_profit = tp_price
                            if sl_price:
                                transaction.stop_loss = sl_price
                            update_instance(transaction)
                            
                            # Create TP/SL orders using adjust_tp_sl (avoids code duplication)
                            # The skip logic in adjust_tp_sl will prevent redundant calls if caller calls again
                            if tp_price and sl_price:
                                logger.debug(f"Creating TP/SL orders for transaction {transaction.id} via adjust_tp_sl")
                                self.adjust_tp_sl(transaction, tp_price, sl_price)
                            elif tp_price:
                                logger.debug(f"Creating TP order for transaction {transaction.id} via adjust_tp")
                                self.adjust_tp(transaction, tp_price)
                            elif sl_price:
                                logger.debug(f"Creating SL order for transaction {transaction.id} via adjust_sl")
                                self.adjust_sl(transaction, sl_price)
                        else:
                            logger.warning(f"Transaction {fresh_order.transaction_id} not found for setting TP/SL")
                    else:
                        logger.warning(f"Order {fresh_order.id} has no transaction_id, cannot set TP/SL")
                
                return fresh_order
            else:
                logger.error(f"Could not find order {trading_order.id} in database to update")
                return None
                    
        except Exception as e:
            logger.error(f"Error submitting order {trading_order} to Alpaca: {e}", exc_info=True)
            
            # Step 4: Mark order as ERROR in database using thread-safe function
            try:
                if trading_order.id:
                    fresh_order = get_instance(TradingOrder, trading_order.id)
                    if fresh_order:
                        fresh_order.status = OrderStatus.ERROR
                        
                        # Store error details in comment field (append to existing comment)
                        error_msg = f"Error: {str(e)[:200]}"
                        if not fresh_order.comment:
                            fresh_order.comment = error_msg
                        else:
                            # Append error to existing comment, truncate if too long
                            fresh_order.comment = f"{fresh_order.comment} | {error_msg}"[:500]
                        
                        # Use thread-safe update function with retry logic
                        update_instance(fresh_order)
                        logger.info(f"Marked order {trading_order.id} as ERROR in database")
                    else:
                        logger.warning(f"Could not find order {trading_order.id} to mark as ERROR")
                else:
                    logger.warning(f"Cannot mark order as ERROR - order has no ID")
            except Exception as update_error:
                logger.error(f"Failed to update order status to ERROR: {update_error}")
            
            return None

    @alpaca_api_retry
    def modify_order(self, order_id: str, trading_order: TradingOrder):
        """
        Modify an existing order in Alpaca.
        
        Note: Alpaca's replace_order_by_id() replaces an order by canceling the existing one
        and creating a new one with the updated parameters.
        
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
            
            # Map good_for to TimeInForce enum if provided
            time_in_force = None
            if trading_order.good_for:
                good_for_value = trading_order.good_for.lower()
                tif_map = {
                    'day': TimeInForce.DAY,
                    'gtc': TimeInForce.GTC,
                    'opg': TimeInForce.OPG,
                    'ioc': TimeInForce.IOC,
                    'fok': TimeInForce.FOK,
                    'cls': TimeInForce.CLS,
                }
                time_in_force = tif_map.get(good_for_value, TimeInForce.GTC)
            
            # Regenerate tracking comment with new epoch time to ensure unique client_order_id
            # This prevents Alpaca's "client_order_id must be unique" error when modifying orders
            new_tracking_comment = self._generate_tracking_comment(trading_order)
            
            # Create ReplaceOrderRequest
            replace_request = ReplaceOrderRequest(
                qty=trading_order.quantity,
                time_in_force=time_in_force,
                limit_price=limit_price,
                stop_price=stop_price,
                client_order_id=new_tracking_comment
            )
            
            # Alpaca uses replace_order_by_id() method
            order = self.client.replace_order_by_id(
                order_id=order_id,
                order_data=replace_request
            )
            
            # Update the trading_order comment with the new tracking comment
            trading_order.comment = new_tracking_comment
            
            logger.info(f"Modified Alpaca order: {order.id}")
            return self.alpaca_order_to_tradingorder(order)
        except Exception as e:
            logger.error(f"Error modifying Alpaca order {order_id}: {e}", exc_info=True)
            return None

    @alpaca_api_retry
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
            #logger.debug(f"Fetched Alpaca order: {order.id}")
            return self.alpaca_order_to_tradingorder(order)
        except Exception as e:
            logger.error(f"Error fetching Alpaca order {order_id}: {e}", exc_info=True)
            return None

    @alpaca_api_retry
    def cancel_order(self, order_id: str):
        """
        Cancel an existing order.
        
        Args:
            order_id (str): Either our database order ID or broker_order_id (UUID).
                           If it's a UUID (contains dashes), it's treated as broker_order_id.
                           Otherwise, it's treated as database order ID.
            
        Returns:
            bool: True if cancellation was successful, False otherwise.
        """
        try:
            # Determine if order_id is a broker_order_id (UUID) or database ID
            if '-' in str(order_id):
                # It's a broker_order_id (UUID format)
                broker_order_id = str(order_id)
                # Look up database order by broker_order_id
                with Session(get_db().bind) as session:
                    statement = select(TradingOrder).where(TradingOrder.broker_order_id == broker_order_id)
                    db_order = session.exec(statement).first()
                    if db_order:
                        db_order_id = db_order.id
                    else:
                        logger.warning(f"Order with broker_order_id {broker_order_id} not found in database, attempting cancellation anyway")
                        db_order_id = None
            else:
                # It's a database order ID
                db_order_id = int(order_id)
                db_order = get_instance(TradingOrder, db_order_id)
                if not db_order:
                    logger.error(f"Order {order_id} not found in database")
                    return False
                
                if not db_order.broker_order_id:
                    logger.error(f"Order {order_id} has no broker_order_id")
                    return False
                
                broker_order_id = db_order.broker_order_id
            
            # Cancel using the broker's order ID (UUID)
            self.client.cancel_order_by_id(broker_order_id)
            logger.info(f"Cancelled Alpaca order: broker_order_id={broker_order_id}" + 
                       (f", database_id={db_order_id}" if db_order_id else ""))
            
            # Update the order status in our database if we found it
            if db_order_id:
                db_order = get_instance(TradingOrder, db_order_id)
                if db_order:
                    db_order.status = OrderStatus.CANCELED
                    update_instance(db_order)
            
            return True
        except Exception as e:
            logger.error(f"Error cancelling Alpaca order {order_id}: {e}", exc_info=True)
            return False

    @alpaca_api_retry
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

    @alpaca_api_retry
    def get_account_info(self):
        """
        Retrieve current account information from Alpaca.
        
        Returns:
            object: Account information if successful, None if an error occurs.
        """
        if not self._check_authentication():
            return None
            
        try:
            account = self.client.get_account()
            logger.debug("Fetched Alpaca account info.")
            return account
        except Exception as e:
            logger.error(f"Error fetching Alpaca account info: {e}", exc_info=True)
            return None

    @alpaca_api_retry
    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        """
        Internal implementation of price fetching for Alpaca. Supports both single and bulk fetching.
        This is called by the base class get_instrument_current_price() when cache is stale.
        
        Alpaca's API natively supports fetching multiple symbols in a single request via symbol_or_symbols parameter.
        
        Args:
            symbol_or_symbols (Union[str, List[str]]): Single symbol or list of symbols to fetch prices for
            price_type (str): Type of price to return - 'bid', 'ask', or 'avg' (default: 'bid')
                             - 'bid': Use bid price divided by bid size
                             - 'ask': Use ask price divided by ask size
                             - 'avg': Average of bid and ask prices (both adjusted by their sizes)
        
        Returns:
            Union[Optional[float], Dict[str, Optional[float]]]:
                - If symbol_or_symbols is str: Returns Optional[float] (single price or None)
                - If symbol_or_symbols is List[str]: Returns Dict[str, Optional[float]] (symbol -> price mapping)
        """
        if not self._check_authentication():
            # Return appropriate type based on input
            if isinstance(symbol_or_symbols, str):
                return None
            else:
                return {symbol: None for symbol in symbol_or_symbols}
            
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            from alpaca.data.enums import DataFeed
            # Create data client for market data
            data_client = StockHistoricalDataClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"]
            )
            
            # Normalize input to list for uniform processing
            is_single_symbol = isinstance(symbol_or_symbols, str)
            symbols_list = [symbol_or_symbols] if is_single_symbol else symbol_or_symbols
            
            # Get latest quotes - Alpaca natively supports bulk fetching
            request = StockLatestQuoteRequest(symbol_or_symbols=symbols_list, feed=DataFeed.DELAYED_SIP)
            quotes = data_client.get_stock_latest_quote(request)
            
            # Process quotes and calculate prices
            def calculate_price(quote):
                """
                Calculate price from quote using bid/ask prices.
                
                Args:
                    quote: Alpaca quote object with bid_price, ask_price
                    
                Returns:
                    Optional[float]: Calculated price based on price_type, or None if unavailable
                """
                bid_price = float(quote.bid_price) if quote.bid_price else None
                ask_price = float(quote.ask_price) if quote.ask_price else None
                
                if price_type == 'bid':
                    # Return bid price, fallback to ask if bid not available
                    return bid_price if bid_price else ask_price
                elif price_type == 'ask':
                    # Return ask price, fallback to bid if ask not available
                    return ask_price if ask_price else bid_price
                elif price_type in ('avg', 'mid'):
                    # Return average of both prices (support both 'avg' and 'mid')
                    if bid_price and ask_price:
                        return (bid_price + ask_price) / 2
                    elif bid_price:
                        return bid_price
                    elif ask_price:
                        return ask_price
                    else:
                        return None
                else:
                    # Invalid price_type, default to bid behavior
                    logger.warning(f"Invalid price_type '{price_type}', defaulting to 'bid'")
                    return bid_price if bid_price else ask_price
            
            # Handle single symbol case (backward compatibility)
            if is_single_symbol:
                symbol = symbol_or_symbols
                if symbol in quotes:
                    quote = quotes[symbol]
                    current_price = calculate_price(quote)
                    logger.debug(f"Current {price_type} price for {symbol}: {current_price}")
                    return current_price
                else:
                    logger.warning(f"No quote data found for symbol {symbol}")
                    return None
            
            # Handle multiple symbols case
            else:
                result = {}
                for symbol in symbols_list:
                    if symbol in quotes:
                        quote = quotes[symbol]
                        current_price = calculate_price(quote)
                        result[symbol] = current_price
                        logger.debug(f"Bulk fetch - Current {price_type} price for {symbol}: {current_price}")
                    else:
                        result[symbol] = None
                        logger.warning(f"Bulk fetch - No quote data found for symbol {symbol}")
                
                logger.info(f"Bulk fetched {price_type} prices for {len(symbols_list)} symbols in single API call")
                return result
                
        except Exception as e:
            logger.error(f"Error getting current price for {symbol_or_symbols}: {e}", exc_info=True)
            # Return appropriate type based on input
            if isinstance(symbol_or_symbols, str):
                return None
            else:
                return {symbol: None for symbol in symbol_or_symbols}
        
        
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

    def refresh_orders(self, heuristic_mapping: bool = False, fetch_all: bool = True) -> bool:
        """
        Refresh/synchronize account orders from Alpaca broker.
        This method updates database records with current order states from the broker.
        
        Args:
            heuristic_mapping (bool): If True, attempt to map orders by comment field when broker_order_id is missing.
                                      Useful for recovering from errors where broker_order_id wasn't saved.
                                      Assumes comment field is unique for this account's orders.
            fetch_all (bool): If True, fetches all orders from Alpaca using pagination. 
                              If False, fetches only first 500 orders (faster but incomplete).
                              Defaults to True for complete synchronization.
        
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:
            # Get all orders from Alpaca (with pagination if fetch_all=True)
            alpaca_orders = self.get_orders(OrderStatus.ALL, fetch_all=fetch_all)
            
            if not alpaca_orders:
                logger.warning("No orders returned from Alpaca during refresh")
                return True
            
            updated_count = 0
            mapped_count = 0
            
            # Get all database orders for this account (for heuristic mapping)
            if heuristic_mapping:
                with Session(get_db().bind) as session:
                    db_orders = session.exec(
                        select(TradingOrder).where(TradingOrder.account_id == self.id)
                    ).all()
                    # Create lookup maps: comment -> db_order and broker_order_id -> db_order
                    comment_map = {order.comment: order for order in db_orders if order.comment}
                    broker_id_map = {order.broker_order_id: order for order in db_orders if order.broker_order_id}
            
            # Process each Alpaca order
            for alpaca_order in alpaca_orders:
                if not alpaca_order.broker_order_id:
                    continue
                
                db_order = None
                
                # Step 1: Try to find by broker_order_id first
                if heuristic_mapping and alpaca_order.broker_order_id in broker_id_map:
                    db_order = broker_id_map[alpaca_order.broker_order_id]
                else:
                    # Standard lookup by broker_order_id
                    with Session(get_db().bind) as session:
                        statement = select(TradingOrder).where(
                            TradingOrder.broker_order_id == alpaca_order.broker_order_id,
                            TradingOrder.account_id == self.id
                        )
                        result = session.exec(statement).first()
                        if result:
                            db_order = get_instance(TradingOrder, result.id)
                
                # Step 2: If not found and heuristic_mapping enabled, try comment matching
                if not db_order and heuristic_mapping and alpaca_order.comment:
                    if alpaca_order.comment in comment_map:
                        candidate = comment_map[alpaca_order.comment]
                        # Only map if broker_order_id is empty (avoid overwriting valid mappings)
                        if not candidate.broker_order_id:
                            db_order = get_instance(TradingOrder, candidate.id)
                            db_order.broker_order_id = alpaca_order.broker_order_id
                            update_instance(db_order)
                            mapped_count += 1
                            logger.info(f"Heuristic mapping: Linked database order {db_order.id} to broker order {alpaca_order.broker_order_id} via comment '{alpaca_order.comment}'")
                
                # Step 3: Update order state if we found a match
                if db_order:
                    has_changes = False
                    #logger.debug(f"Processing order {db_order.id}: DB status={db_order.status}, Alpaca status={alpaca_order.status}, alpaca_broker_id={alpaca_order.broker_order_id}")
                    
                    # Special handling for PENDING_CANCEL orders: Can only transition to CANCELLED
                    # PENDING_CANCEL means we're waiting for cancellation before replacing the order
                    # Ignore all broker state updates except transition to CANCELLED
                    if db_order.status == OrderStatus.PENDING_CANCEL:
                        if alpaca_order.status == OrderStatus.CANCELED:
                            logger.info(f"Order {db_order.id} transitioned from PENDING_CANCEL to CANCELED as expected")
                            db_order.status = OrderStatus.CANCELED
                            has_changes = True
                        else:
                            # Ignore broker state - order is waiting for cancellation
                            logger.debug(f"Order {db_order.id} in PENDING_CANCEL - ignoring broker state {alpaca_order.status}, waiting for CANCELED")
                    # Normal status update for non-PENDING_CANCEL orders
                    elif db_order.status != alpaca_order.status:
                        logger.debug(f"Order {db_order.id} status changed: {db_order.status} -> {alpaca_order.status}")
                        db_order.status = alpaca_order.status
                        has_changes = True
                    
                    if db_order.filled_qty is None or float(db_order.filled_qty) != float(alpaca_order.filled_qty):
                        logger.debug(f"Order {db_order.id} filled_qty changed: {db_order.filled_qty} -> {alpaca_order.filled_qty}")
                        db_order.filled_qty = alpaca_order.filled_qty
                        has_changes = True
                    
                    # Update open_price if it changed (use broker's filled_avg_price)
                    if alpaca_order.open_price and (db_order.open_price is None or float(db_order.open_price) != float(alpaca_order.open_price)):
                        logger.debug(f"Order {db_order.id} open_price changed: {db_order.open_price} -> {alpaca_order.open_price}")
                        db_order.open_price = alpaca_order.open_price
                        has_changes = True
                    
                    # Update broker_order_id if it wasn't set before (non-heuristic path)
                    if not db_order.broker_order_id:
                        logger.debug(f"Order {db_order.id} broker_order_id set to: {alpaca_order.broker_order_id}")
                        db_order.broker_order_id = alpaca_order.broker_order_id
                        has_changes = True
                    
                    # Use thread-safe update if there were changes
                    if has_changes:
                        update_instance(db_order)
                        updated_count += 1
                        logger.debug(f"Updated database order {db_order.id} with changes from Alpaca order {alpaca_order.broker_order_id}")
                    
                    # Step 3a: Update or insert OCO order legs if this is an OCO order and we have a matched DB order
                    if db_order.order_type == CoreOrderType.OCO:
                        legs_inserted = 0
                        legs_updated = 0
                        
                        # Try to update existing legs from the converted order's legs_broker_ids field
                        # This field is populated by alpaca_order_to_tradingorder() when Alpaca returns legs
                        if alpaca_order.legs_broker_ids:
                            logger.debug(f"Order {db_order.id} has {len(alpaca_order.legs_broker_ids)} OCO leg broker IDs: {alpaca_order.legs_broker_ids}")
                            # CRITICAL FIX: Update existing OCO legs that are already in database
                            # OCO legs are NOT returned by get_orders() as separate items, so we must update them
                            # based on the parent order's status and information
                            legs_updated = self._update_existing_oco_legs(db_order, alpaca_order)
                            # Then insert any legs that don't exist yet
                            legs_inserted = self._insert_oco_legs_from_broker_ids(db_order, alpaca_order.legs_broker_ids)
                        
                        # Also try insert legs from the raw Alpaca order if it has them
                        # (Alpaca submit_order response includes full leg objects, but get_orders response doesn't)
                        if (hasattr(alpaca_order, 'order_class') and 
                            alpaca_order.order_class == OrderClass.OCO and
                            hasattr(alpaca_order, 'legs') and alpaca_order.legs):
                            self._insert_oco_order_legs(alpaca_order, db_order, db_order.transaction_id)
                        
                        if legs_inserted > 0 or legs_updated > 0:
                            logger.info(f"Order {db_order.id}: Updated {legs_updated} OCO legs, Inserted {legs_inserted} OCO leg orders")
            
            # Step 4: Mark database orders with broker_order_ids that don't exist in Alpaca as CANCELED
            # This catches orders that were canceled in Alpaca but status wasn't updated in database
            canceled_count = 0
            alpaca_broker_ids = {order.broker_order_id for order in alpaca_orders if order.broker_order_id}
            
            # CRITICAL: Add OCO leg broker IDs to safe set
            # OCO legs are not returned by get_orders() as separate items, but they exist in our database
            # We must include their broker IDs in the safe set so they don't get incorrectly marked as CANCELED
            oco_leg_broker_ids = set()
            with Session(get_db().bind) as session:
                oco_legs = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == self.id,
                        TradingOrder.parent_order_id.is_not(None),  # Has a parent = is an OCO leg
                        TradingOrder.broker_order_id.is_not(None)
                    )
                ).all()
                oco_leg_broker_ids = {leg.broker_order_id for leg in oco_legs}
            
            # Combine both sets: parent orders + OCO legs
            alpaca_broker_ids = alpaca_broker_ids.union(oco_leg_broker_ids)
            logger.debug(f"Total broker IDs to check (parents + OCO legs): {len(alpaca_broker_ids)} (parents: {len(alpaca_broker_ids) - len(oco_leg_broker_ids)}, legs: {len(oco_leg_broker_ids)})")
            
            with Session(get_db().bind) as session:
                # Get all database orders for this account with broker_order_id and non-terminal status
                db_active_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == self.id,
                        TradingOrder.broker_order_id.is_not(None),
                        TradingOrder.status.not_in([
                            OrderStatus.FILLED, OrderStatus.CANCELED, 
                            OrderStatus.EXPIRED, OrderStatus.REPLACED, OrderStatus.REJECTED
                        ])
                    )
                ).all()
                
                for db_order in db_active_orders:
                    # If this broker_order_id doesn't exist in Alpaca anymore, check if we should mark as CANCELED
                    if db_order.broker_order_id not in alpaca_broker_ids:
                        # Safety check: Don't mark as CANCELED if order was created very recently
                        # This prevents race conditions where order was just submitted but not yet in Alpaca's response
                        if db_order.created_at:
                            # Ensure both datetimes have the same timezone awareness
                            created_at = db_order.created_at
                            if created_at.tzinfo is None:
                                # created_at is offset-naive, make it aware in UTC
                                created_at = created_at.replace(tzinfo=timezone.utc)
                            
                            order_age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60
                            if order_age_minutes < 5:
                                logger.debug(
                                    f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                                    f"not found in Alpaca but is only {order_age_minutes:.1f} minutes old - skipping cancellation"
                                )
                                continue
                        
                        logger.warning(
                            f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                            f"not found in Alpaca, marking as CANCELED"
                        )
                        fresh_order = get_instance(TradingOrder, db_order.id)
                        if fresh_order:
                            fresh_order.status = OrderStatus.CANCELED
                            update_instance(fresh_order)
                            canceled_count += 1
            
            # Step 5: Check for dependent orders that can now be submitted
            triggered_count = self._check_and_submit_dependent_orders()
            
            if heuristic_mapping and mapped_count > 0:
                logger.info(f"Successfully refreshed orders from Alpaca: {updated_count} updated, {mapped_count} mapped via comment heuristic, {canceled_count} marked as canceled, {triggered_count} dependent orders triggered")
            else:
                logger.info(f"Successfully refreshed orders from Alpaca: {updated_count} updated, {canceled_count} marked as canceled, {triggered_count} dependent orders triggered")
            return True
            
        except Exception as e:
            logger.error(f"Error refreshing orders from Alpaca: {e}", exc_info=True)
            return False
    
    def _check_and_submit_dependent_orders(self) -> int:
        """
        Check for PENDING orders with depends_on_order and submit them if dependency is met.
        
        This handles the workflow where:
        1. Order A is PENDING_CANCEL waiting for cancellation
        2. Order A transitions to CANCELED
        3. Order B (depends_on_order=A, status=PENDING) should now be submitted
        
        Also handles:
        - TP/SL orders waiting for entry order to fill
        - Any order waiting for its parent to reach a specific status
        
        Returns:
            int: Number of dependent orders submitted
        """
        try:
            from sqlmodel import Session, select
            
            triggered_count = 0
            
            with Session(get_db().bind) as session:
                # Find all PENDING orders with dependencies
                dependent_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.account_id == self.id,
                        TradingOrder.status == OrderStatus.PENDING,
                        TradingOrder.depends_on_order.is_not(None)
                    )
                ).all()
                
                for order in dependent_orders:
                    # Get the parent order
                    parent_order = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.id == order.depends_on_order
                        )
                    ).first()
                    
                    if not parent_order:
                        logger.warning(f"Order {order.id} depends on non-existent order {order.depends_on_order}")
                        continue
                    
                    # Check if dependency is met
                    dependency_met = False
                    if order.depends_order_status_trigger:
                        # Specific status trigger (e.g., wait for FILLED)
                        if parent_order.status == order.depends_order_status_trigger:
                            dependency_met = True
                            logger.info(f"Order {order.id} dependency met: parent order {parent_order.id} reached status {parent_order.status}")
                    else:
                        # Default: wait for any terminal status (FILLED, CANCELED, etc.)
                        if parent_order.status in OrderStatus.get_terminal_statuses():
                            dependency_met = True
                            logger.info(f"Order {order.id} dependency met: parent order {parent_order.id} reached terminal status {parent_order.status}")
                    
                    if dependency_met:
                        # Verify parent order has valid quantity before submitting dependent order
                        if not parent_order.quantity or parent_order.quantity <= 0:
                            logger.warning(f"Cannot submit dependent order {order.id}: parent order {parent_order.id} has invalid quantity {parent_order.quantity}. Marking as ERROR.")
                            order.status = OrderStatus.ERROR
                            session.add(order)
                            session.commit()
                            continue
                        
                        # Submit the dependent order
                        logger.info(f"Submitting dependent order {order.id} (depends on {parent_order.id})")
                        try:
                            self.submit_order(order)
                            triggered_count += 1
                        except Exception as e:
                            logger.error(f"Failed to submit dependent order {order.id}: {e}", exc_info=True)
                            # Mark order as ERROR status
                            order.status = OrderStatus.ERROR
                            session.add(order)
                            session.commit()
            
            if triggered_count > 0:
                logger.info(f"Triggered {triggered_count} dependent orders")
            
            return triggered_count
            
        except Exception as e:
            logger.error(f"Error checking dependent orders: {e}", exc_info=True)
            return 0

    def _set_order_tp_impl(self, trading_order: TradingOrder, tp_price: float) -> None:
        """
        Broker-specific implementation for Alpaca take profit orders.
        
        The base class AccountInterface.set_order_tp() handles:
        - Enforcing minimum TP percent
        - Creating/updating WAITING_TRIGGER order in database
        - Updating transaction's take_profit value
        
        This method is a no-op for Alpaca since we manage TP/SL orders through
        the database-level WAITING_TRIGGER mechanism, not through Alpaca API orders.
        """
        # No broker-specific operations needed - base class handles all TP logic
        pass

    def _set_order_sl_impl(self, trading_order: TradingOrder, sl_price: float) -> None:
        """
        Broker-specific implementation for Alpaca stop loss orders.
        
        The base class AccountInterface.set_order_sl() handles:
        - Enforcing minimum SL percent
        - Creating/updating WAITING_TRIGGER order in database
        - Updating transaction's stop_loss value
        
        This method is a no-op for Alpaca since we manage TP/SL orders through
        the database-level WAITING_TRIGGER mechanism, not through Alpaca API orders.
        """
        # No broker-specific operations needed - base class handles all SL logic
        pass

    def _set_order_tp_sl_impl(self, trading_order: TradingOrder, tp_price: float, sl_price: float) -> None:
        """
        Set both TP and SL for an order using STOP_LIMIT orders.
        
        After the entry order is filled, we can submit STOP_LIMIT orders for both TP and SL.
        If existing TP/SL orders exist, we replace them with new STOP_LIMIT orders.
        
        STOP_LIMIT orders have:
        - stop_price: The trigger price
        - limit_price: The execution price (set to same as stop_price for simplicity)
        
        Args:
            trading_order: The entry order (already submitted/filled)
            tp_price: Take profit price
            sl_price: Stop loss price
        """
        try:
            from ...core.db import get_db, add_instance, update_instance
            from sqlmodel import Session, select
            from ...core.types import OrderType as CoreOrderType, OrderDirection
            from alpaca.trading.requests import StopLimitOrderRequest, ReplaceOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            
            logger.info(
                f"Setting combined TP/SL for order {trading_order.id}: "
                f"TP=${tp_price:.2f}, SL=${sl_price:.2f}"
            )
            
            # Find existing TP/SL orders for this transaction
            with Session(get_db().bind) as session:
                existing_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == trading_order.transaction_id,
                        TradingOrder.id != trading_order.id,  # Exclude entry order
                        TradingOrder.status.not_in([OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REPLACED])
                    )
                ).all()
                
                existing_tp = None
                existing_sl = None
                
                for order in existing_orders:
                    # Check if it's a TP order (opposite side of entry, higher price for longs)
                    if order.side != trading_order.side:
                        if order.limit_price and order.limit_price > trading_order.open_price:
                            existing_tp = order
                        elif order.stop_price and order.stop_price < trading_order.open_price:
                            existing_sl = order
                
                # Handle TP: Replace existing or create new STOP_LIMIT order
                if existing_tp and existing_tp.broker_order_id:
                    logger.info(f"Replacing existing TP order {existing_tp.id} with STOP_LIMIT at ${tp_price:.2f}")
                    
                    replace_request = ReplaceOrderRequest(
                        qty=existing_tp.quantity,
                        limit_price=tp_price,
                        stop_price=tp_price  # STOP_LIMIT: trigger and execute at same price
                    )
                    
                    try:
                        replaced_order = self.client.replace_order_by_id(
                            order_id=existing_tp.broker_order_id,
                            order_data=replace_request
                        )
                        
                        # Create new database record
                        new_tp = TradingOrder(
                            account_id=existing_tp.account_id,
                            symbol=existing_tp.symbol,
                            quantity=existing_tp.quantity,
                            side=existing_tp.side,
                            order_type=CoreOrderType.SELL_STOP_LIMIT if existing_tp.side == OrderDirection.SELL else CoreOrderType.BUY_STOP_LIMIT,
                            limit_price=tp_price,
                            stop_price=tp_price,
                            transaction_id=existing_tp.transaction_id,
                            broker_order_id=replaced_order.id,
                            status=OrderStatus.PENDING_NEW,
                            comment=f"TP STOP_LIMIT (replaced {existing_tp.id})"
                        )
                        new_tp_id = add_instance(new_tp)
                        
                        # Mark old order as REPLACED
                        existing_tp.status = OrderStatus.REPLACED
                        update_instance(existing_tp)
                        
                        logger.info(f"Successfully replaced TP: old={existing_tp.id}, new={new_tp_id}, broker_id={replaced_order.id}")
                        
                    except Exception as e:
                        logger.warning(f"Replace failed, canceling and will create new: {e}")
                        try:
                            self.cancel_order(existing_tp.broker_order_id)
                            existing_tp = None  # Will trigger creation below
                        except Exception as cancel_error:
                            logger.error(f"Failed to cancel existing TP order: {cancel_error}")
                
                # If no existing TP or replace failed, create new STOP_LIMIT order
                if not existing_tp or not existing_tp.broker_order_id:
                    logger.info(f"Creating new TP STOP_LIMIT order at ${tp_price:.2f}")
                    
                    # Determine side (opposite of entry)
                    tp_side = OrderSide.SELL if trading_order.side == OrderDirection.BUY else OrderSide.BUY
                    
                    stop_limit_request = StopLimitOrderRequest(
                        symbol=trading_order.symbol,
                        qty=trading_order.quantity,
                        side=tp_side,
                        time_in_force=TimeInForce.GTC,
                        stop_price=tp_price,
                        limit_price=tp_price  # Execute at trigger price
                    )
                    
                    try:
                        alpaca_order = self.client.submit_order(stop_limit_request)
                        
                        # Create database record
                        tp_order = TradingOrder(
                            account_id=trading_order.account_id,
                            symbol=trading_order.symbol,
                            quantity=trading_order.quantity,
                            side=OrderDirection.SELL if tp_side == OrderSide.SELL else OrderDirection.BUY,
                            order_type=CoreOrderType.SELL_STOP_LIMIT if tp_side == OrderSide.SELL else CoreOrderType.BUY_STOP_LIMIT,
                            limit_price=tp_price,
                            stop_price=tp_price,
                            transaction_id=trading_order.transaction_id,
                            broker_order_id=str(alpaca_order.id),  # Convert UUID to string
                            status=OrderStatus.PENDING_NEW,
                            comment=f"TP STOP_LIMIT for order {trading_order.id}"
                        )
                        tp_order_id = add_instance(tp_order)
                        logger.info(f"Created TP order {tp_order_id} (broker_id={alpaca_order.id})")
                        
                    except Exception as e:
                        logger.error(f"Failed to create TP STOP_LIMIT order: {e}")
                
                # Handle SL: Replace existing or create new STOP_LIMIT order
                if existing_sl and existing_sl.broker_order_id:
                    logger.info(f"Replacing existing SL order {existing_sl.id} with STOP_LIMIT at ${sl_price:.2f}")
                    
                    replace_request = ReplaceOrderRequest(
                        qty=existing_sl.quantity,
                        limit_price=sl_price,
                        stop_price=sl_price
                    )
                    
                    try:
                        replaced_order = self.client.replace_order_by_id(
                            order_id=existing_sl.broker_order_id,
                            order_data=replace_request
                        )
                        
                        # Create new database record
                        new_sl = TradingOrder(
                            account_id=existing_sl.account_id,
                            symbol=existing_sl.symbol,
                            quantity=existing_sl.quantity,
                            side=existing_sl.side,
                            order_type=CoreOrderType.SELL_STOP_LIMIT if existing_sl.side == OrderDirection.SELL else CoreOrderType.BUY_STOP_LIMIT,
                            limit_price=sl_price,
                            stop_price=sl_price,
                            transaction_id=existing_sl.transaction_id,
                            broker_order_id=replaced_order.id,
                            status=OrderStatus.PENDING_NEW,
                            comment=f"SL STOP_LIMIT (replaced {existing_sl.id})"
                        )
                        new_sl_id = add_instance(new_sl)
                        
                        # Mark old order as REPLACED
                        existing_sl.status = OrderStatus.REPLACED
                        update_instance(existing_sl)
                        
                        logger.info(f"Successfully replaced SL: old={existing_sl.id}, new={new_sl_id}, broker_id={replaced_order.id}")
                        
                    except Exception as e:
                        logger.warning(f"Replace failed, canceling and will create new: {e}")
                        try:
                            self.cancel_order(existing_sl.broker_order_id)
                            existing_sl = None
                        except Exception as cancel_error:
                            logger.error(f"Failed to cancel existing SL order: {cancel_error}")
                
                # If no existing SL or replace failed, create new STOP_LIMIT order
                if not existing_sl or not existing_sl.broker_order_id:
                    logger.info(f"Creating new SL STOP_LIMIT order at ${sl_price:.2f}")
                    
                    sl_side = OrderSide.SELL if trading_order.side == OrderDirection.BUY else OrderSide.BUY
                    
                    stop_limit_request = StopLimitOrderRequest(
                        symbol=trading_order.symbol,
                        qty=trading_order.quantity,
                        side=sl_side,
                        time_in_force=TimeInForce.GTC,
                        stop_price=sl_price,
                        limit_price=sl_price
                    )
                    
                    try:
                        alpaca_order = self.client.submit_order(stop_limit_request)
                        
                        # Create database record
                        sl_order = TradingOrder(
                            account_id=trading_order.account_id,
                            symbol=trading_order.symbol,
                            quantity=trading_order.quantity,
                            side=OrderDirection.SELL if sl_side == OrderSide.SELL else OrderDirection.BUY,
                            order_type=CoreOrderType.SELL_STOP_LIMIT if sl_side == OrderSide.SELL else CoreOrderType.BUY_STOP_LIMIT,
                            limit_price=sl_price,
                            stop_price=sl_price,
                            transaction_id=trading_order.transaction_id,
                            broker_order_id=str(alpaca_order.id),  # Convert UUID to string
                            status=OrderStatus.PENDING_NEW,
                            comment=f"SL STOP_LIMIT for order {trading_order.id}"
                        )
                        sl_order_id = add_instance(sl_order)
                        logger.info(f"Created SL order {sl_order_id} (broker_id={alpaca_order.id})")
                        
                    except Exception as e:
                        logger.error(f"Failed to create SL STOP_LIMIT order: {e}")
            
            logger.info(f"Successfully set TP/SL using STOP_LIMIT orders for transaction {trading_order.transaction_id}")
                
        except Exception as e:
            logger.error(
                f"Error setting TP/SL for transaction {trading_order.transaction_id}: {e}",
                exc_info=True
            )
            raise

    def _update_broker_tp_order(self, tp_order: TradingOrder, new_tp_price: float) -> None:
        """
        Update an already-submitted Alpaca TP order with a new price.
        
        IMPORTANT: Alpaca's replace_order only works on orders in specific states:
        - Can replace: new, pending_new, held
        - Cannot replace: accepted, filled, cancelled, expired, rejected
        
        Our testing shows "accepted" orders CANNOT be replaced (error 42210000).
        This means we can only replace orders that haven't been accepted by the broker yet.
        
        Alpaca's replace_order API creates a NEW replacement order and marks the original as REPLACED.
        This method:
        1. Sends replace request to Alpaca (creates NEW order, marks old as REPLACED)
        2. Creates NEW database TradingOrder record with new broker_order_id
        3. Calls refresh_orders() to sync the old order status (REPLACED)
        
        Args:
            tp_order: The TP order TradingOrder object (with broker_order_id set)
            new_tp_price: The new take profit price
        """
        try:
            from ...core.db import add_instance
            from datetime import datetime, timezone
            
            if not self.client:
                raise ValueError("Alpaca client not initialized")
            
            if not tp_order.broker_order_id:
                logger.warning(f"TP order {tp_order.id} has no broker_order_id, cannot update at Alpaca")
                return
            
            old_broker_order_id = tp_order.broker_order_id
            old_order_id = tp_order.id
            
            # Log replacement operation
            logger.info(
                f"Replacing Alpaca TP order {old_broker_order_id} (database ID: {old_order_id}) "
                f"from ${tp_order.limit_price:.2f} to ${new_tp_price:.2f}"
            )
            
            # Clone the tp_order for the replacement request
            temp_order = TradingOrder(
                account_id=tp_order.account_id,
                symbol=tp_order.symbol,
                quantity=tp_order.quantity,
                side=tp_order.side,
                order_type=tp_order.order_type,
                limit_price=new_tp_price,  # New price
                stop_price=tp_order.stop_price,
                transaction_id=tp_order.transaction_id,
                status=tp_order.status,
                good_for=tp_order.good_for,
                comment=tp_order.comment
            )
            
            # Send replace request to Alpaca - this creates a NEW order and marks old one as REPLACED
            replacement_order = self.modify_order(old_broker_order_id, temp_order)
            
            if not replacement_order:
                raise Exception(f"Failed to replace Alpaca TP order {old_broker_order_id} - modify_order returned None")
            
            # Create NEW database order record with the replacement order details
            new_broker_order_id = replacement_order.broker_order_id
            new_tp_order = TradingOrder(
                account_id=tp_order.account_id,
                symbol=tp_order.symbol,
                quantity=tp_order.quantity,
                side=tp_order.side,
                order_type=tp_order.order_type,
                limit_price=new_tp_price,
                stop_price=tp_order.stop_price,
                transaction_id=tp_order.transaction_id,
                status=replacement_order.status,  # Use status from Alpaca
                broker_order_id=new_broker_order_id,
                depends_on_order=tp_order.depends_on_order,
                depends_order_status_trigger=tp_order.depends_order_status_trigger,
                expert_recommendation_id=tp_order.expert_recommendation_id,
                open_type=tp_order.open_type,
                comment=replacement_order.comment,  # Use new tracking comment from replacement
                data=tp_order.data,  # Preserve TP/SL metadata
                good_for=tp_order.good_for,
                created_at=datetime.now(timezone.utc)
            )
            
            new_order_id = add_instance(new_tp_order, expunge_after_flush=True)
            
            logger.info(
                f"Successfully replaced Alpaca TP order: "
                f"Old database_id={old_order_id}, broker_order_id={old_broker_order_id} → "
                f"New database_id={new_order_id}, broker_order_id={new_broker_order_id}, "
                f"New price=${new_tp_price:.2f}"
            )
            
            # Refresh orders from broker to sync the old order status (should now be REPLACED)
            logger.debug(f"Refreshing orders from broker to sync old TP order {old_broker_order_id} status to REPLACED")
            self.refresh_orders()
            
        except Exception as e:
            logger.error(
                f"Error replacing broker TP order {tp_order.broker_order_id}: {e}",
                exc_info=True
            )
            raise

    def _update_broker_sl_order(self, sl_order: TradingOrder, new_sl_price: float) -> None:
        """
        Replace an already-submitted Alpaca SL order with a new price.
        
        IMPORTANT: Alpaca's replace_order only works on orders in specific states:
        - Can replace: new, pending_new, held
        - Cannot replace: accepted, filled, cancelled, expired, rejected
        
        Our testing shows "accepted" orders CANNOT be replaced (error 42210000).
        This means we can only replace orders that haven't been accepted by the broker yet.
        
        Alpaca's replace_order API creates a NEW replacement order and marks the original as REPLACED.
        This method creates a NEW database record for the replacement order to preserve order history.
        
        Process:
        1. Store old order details (database ID and broker_order_id)
        2. Create temporary order with new stop_price for API call
        3. Submit replacement request to Alpaca (creates NEW order at broker)
        4. Create NEW TradingOrder database record with replacement broker_order_id
        5. Call refresh_orders() to mark old order as REPLACED
        
        After this method:
        - Old database order: status=REPLACED (synced by refresh_orders)
        - New database order: status=NEW/ACCEPTED with new broker_order_id
        
        Args:
            sl_order: The SL order TradingOrder object (with broker_order_id set)
            new_sl_price: The new stop loss price
        """
        try:
            if not self.client:
                raise ValueError("Alpaca client not initialized")
            
            if not sl_order.broker_order_id:
                logger.warning(f"SL order {sl_order.id} has no broker_order_id, cannot update at Alpaca")
                return
            
            # Store old order identifiers for logging
            old_order_id = sl_order.id
            old_broker_order_id = sl_order.broker_order_id
            
            logger.info(
                f"Replacing Alpaca SL order {old_broker_order_id} (database ID: {old_order_id}) "
                f"from ${sl_order.stop_price:.2f} to ${new_sl_price:.2f}"
            )
            
            # Create temporary order for API call with new price
            # We don't modify sl_order itself since we'll create a NEW database record
            temp_order = TradingOrder(
                transaction_id=sl_order.transaction_id,
                broker_order_id=old_broker_order_id,  # For the API call
                symbol=sl_order.symbol,
                quantity=sl_order.quantity,
                side=sl_order.side,
                order_type=sl_order.order_type,
                time_in_force=sl_order.time_in_force,
                stop_price=new_sl_price,  # New price
                limit_price=sl_order.limit_price,
                status=sl_order.status,
                comment=sl_order.comment,
                submitted_at=sl_order.submitted_at
            )
            
            # Send replace request to Alpaca - this creates a NEW order and marks old one as REPLACED
            replacement_order = self.modify_order(old_broker_order_id, temp_order)
            
            if not replacement_order:
                raise Exception(f"Failed to replace Alpaca SL order {old_broker_order_id} - modify_order returned None")
            
            # Create NEW database record for the replacement order
            new_tp_order = TradingOrder(
                transaction_id=sl_order.transaction_id,
                broker_order_id=replacement_order.broker_order_id,  # NEW broker order ID
                symbol=sl_order.symbol,
                quantity=sl_order.quantity,
                side=sl_order.side,
                order_type=sl_order.order_type,
                time_in_force=sl_order.time_in_force,
                stop_price=new_sl_price,
                limit_price=sl_order.limit_price,
                status=replacement_order.status,  # Status from broker (typically NEW or ACCEPTED)
                comment=replacement_order.comment,  # Tracking comment from broker
                submitted_at=replacement_order.submitted_at
            )
            
            # Add NEW order to database
            new_order_id = add_instance(new_tp_order, expunge_after_flush=True)
            
            logger.info(
                f"Successfully replaced Alpaca SL order - created NEW database record: "
                f"Old database_id={old_order_id}, Old broker_order_id={old_broker_order_id} → "
                f"New database_id={new_order_id}, New broker_order_id={replacement_order.broker_order_id}, "
                f"New price=${new_sl_price:.2f}"
            )
            
            # Refresh orders from broker to sync the old order status (will be marked REPLACED)
            logger.debug(
                f"Refreshing orders from broker to sync old SL order {old_broker_order_id} "
                f"(database ID: {old_order_id}) status to REPLACED"
            )
            self.refresh_orders()
            
        except Exception as e:
            logger.error(
                f"Error replacing broker SL order {sl_order.broker_order_id}: {e}",
                exc_info=True
            )
            raise
    
    def _replace_tp_order(self, existing_tp: TradingOrder, new_tp_price: float) -> TradingOrder:
        """
        Replace an existing TP order at Alpaca with a new price using replace_order API.
        
        Args:
            existing_tp: The existing TP order to replace
            new_tp_price: The new take profit price
            
        Returns:
            TradingOrder: The new TP order (old one marked as REPLACED)
        """
        try:
            from alpaca.trading.requests import ReplaceOrderRequest
            from ...core.db import add_instance, update_instance, get_db
            from sqlmodel import Session
            from ...core.types import OrderType as CoreOrderType
            
            logger.info(f"Replacing TP order {existing_tp.id} (broker_id={existing_tp.broker_order_id}) with new price ${new_tp_price:.2f}")
            
            # Build replace request - use STOP_LIMIT for both TP and SL
            replace_request = ReplaceOrderRequest(
                qty=existing_tp.quantity,
                limit_price=new_tp_price,
                stop_price=new_tp_price  # STOP_LIMIT: trigger and execute at same price
            )
            
            # Send replace request to Alpaca
            replaced_order = self.client.replace_order_by_id(
                order_id=existing_tp.broker_order_id,
                order_data=replace_request
            )
            
            # Create new database record for the replacement order
            with Session(get_db().bind) as session:
                new_tp = TradingOrder(
                    account_id=existing_tp.account_id,
                    symbol=existing_tp.symbol,
                    quantity=existing_tp.quantity,
                    side=existing_tp.side,
                    order_type=CoreOrderType.SELL_STOP_LIMIT if existing_tp.side == OrderDirection.SELL else CoreOrderType.BUY_STOP_LIMIT,
                    limit_price=new_tp_price,
                    stop_price=new_tp_price,
                    transaction_id=existing_tp.transaction_id,
                    broker_order_id=str(replaced_order.id),
                    status=OrderStatus.PENDING_NEW,
                    depends_on_order=existing_tp.depends_on_order,
                    depends_order_status_trigger=existing_tp.depends_order_status_trigger,
                    expert_recommendation_id=existing_tp.expert_recommendation_id,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=f"TP STOP_LIMIT (replaced {existing_tp.id})",
                    created_at=datetime.now(timezone.utc)
                )
                session.add(new_tp)
                session.commit()
                session.refresh(new_tp)
                new_tp_id = new_tp.id
            
            # Mark old order as REPLACED
            existing_tp.status = OrderStatus.REPLACED
            update_instance(existing_tp)
            
            logger.info(f"Successfully replaced TP: old={existing_tp.id}, new={new_tp_id}, broker_id={replaced_order.id}")
            
            # Refresh to sync broker state
            self.refresh_orders()
            
            # Return the new order
            return get_instance(TradingOrder, new_tp_id)
            
        except Exception as e:
            logger.error(f"Error replacing TP order {existing_tp.id}: {e}", exc_info=True)
            raise
    
    def _replace_sl_order(self, existing_sl: TradingOrder, new_sl_price: float) -> TradingOrder:
        """
        Replace an existing SL order at Alpaca with a new price using replace_order API.
        
        Args:
            existing_sl: The existing SL order to replace
            new_sl_price: The new stop loss price
            
        Returns:
            TradingOrder: The new SL order (old one marked as REPLACED)
        """
        try:
            from alpaca.trading.requests import ReplaceOrderRequest
            from ...core.db import add_instance, update_instance, get_db
            from sqlmodel import Session
            from ...core.types import OrderType as CoreOrderType
            
            logger.info(f"Replacing SL order {existing_sl.id} (broker_id={existing_sl.broker_order_id}) with new price ${new_sl_price:.2f}")
            
            # Build replace request - use STOP_LIMIT for both TP and SL
            replace_request = ReplaceOrderRequest(
                qty=existing_sl.quantity,
                limit_price=new_sl_price,
                stop_price=new_sl_price  # STOP_LIMIT: trigger and execute at same price
            )
            
            # Send replace request to Alpaca
            replaced_order = self.client.replace_order_by_id(
                order_id=existing_sl.broker_order_id,
                order_data=replace_request
            )
            
            # Create new database record for the replacement order
            with Session(get_db().bind) as session:
                new_sl = TradingOrder(
                    account_id=existing_sl.account_id,
                    symbol=existing_sl.symbol,
                    quantity=existing_sl.quantity,
                    side=existing_sl.side,
                    order_type=CoreOrderType.SELL_STOP_LIMIT if existing_sl.side == OrderDirection.SELL else CoreOrderType.BUY_STOP_LIMIT,
                    limit_price=new_sl_price,
                    stop_price=new_sl_price,
                    transaction_id=existing_sl.transaction_id,
                    broker_order_id=str(replaced_order.id),
                    status=OrderStatus.PENDING_NEW,
                    depends_on_order=existing_sl.depends_on_order,
                    depends_order_status_trigger=existing_sl.depends_order_status_trigger,
                    expert_recommendation_id=existing_sl.expert_recommendation_id,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=f"SL STOP_LIMIT (replaced {existing_sl.id})",
                    created_at=datetime.now(timezone.utc)
                )
                session.add(new_sl)
                session.commit()
                session.refresh(new_sl)
                new_sl_id = new_sl.id
            
            # Mark old order as REPLACED
            existing_sl.status = OrderStatus.REPLACED
            update_instance(existing_sl)
            
            logger.info(f"Successfully replaced SL: old={existing_sl.id}, new={new_sl_id}, broker_id={replaced_order.id}")
            
            # Refresh to sync broker state
            self.refresh_orders()
            
            # Return the new order
            return get_instance(TradingOrder, new_sl_id)
            
        except Exception as e:
            logger.error(f"Error replacing SL order {existing_sl.id}: {e}", exc_info=True)
            raise
    
    def _replace_order_with_stop_limit(self, existing_order: TradingOrder, tp_price: float, sl_price: float) -> TradingOrder:
        """
        Replace an existing TP or SL order with a STOP_LIMIT order containing both TP and SL.
        
        This is the critical method for Alpaca's constraint - they only allow ONE opposite-direction order.
        When setting both TP and SL together, or adding TP to existing SL (or vice versa),
        we replace the single existing order with a STOP_LIMIT that has both prices.
        
        Args:
            existing_order: The existing TP or SL order to replace
            tp_price: The take profit (limit) price
            sl_price: The stop loss (trigger) price
            
        Returns:
            TradingOrder: The new STOP_LIMIT order with both TP and SL (old one marked as REPLACED)
        """
        try:
            from alpaca.trading.requests import ReplaceOrderRequest
            from ...core.db import add_instance, update_instance, get_db, get_instance
            from sqlmodel import Session
            from ...core.types import OrderType as CoreOrderType
            
            logger.info(f"Replacing order {existing_order.id} (broker_id={existing_order.broker_order_id}) with STOP_LIMIT (TP=${tp_price:.2f}, SL=${sl_price:.2f})")
            
            # Build replace request - STOP_LIMIT with both prices
            replace_request = ReplaceOrderRequest(
                qty=existing_order.quantity,
                limit_price=tp_price,  # Take profit execution price
                stop_price=sl_price    # Stop loss trigger price
            )
            
            # Send replace request to Alpaca
            replaced_order = self.client.replace_order_by_id(
                order_id=existing_order.broker_order_id,
                order_data=replace_request
            )
            
            # Determine correct order type based on side
            if existing_order.side == OrderDirection.SELL:
                order_type = CoreOrderType.SELL_STOP_LIMIT
            else:
                order_type = CoreOrderType.BUY_STOP_LIMIT
            
            # Create new database record for the replacement order
            with Session(get_db().bind) as session:
                new_order = TradingOrder(
                    account_id=existing_order.account_id,
                    symbol=existing_order.symbol,
                    quantity=existing_order.quantity,
                    side=existing_order.side,
                    order_type=order_type,
                    limit_price=tp_price,
                    stop_price=sl_price,
                    transaction_id=existing_order.transaction_id,
                    broker_order_id=str(replaced_order.id),
                    status=OrderStatus.PENDING_NEW,
                    depends_on_order=existing_order.depends_on_order,
                    depends_order_status_trigger=existing_order.depends_order_status_trigger,
                    expert_recommendation_id=existing_order.expert_recommendation_id,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=f"TP/SL STOP_LIMIT (replaced {existing_order.id})",
                    created_at=datetime.now(timezone.utc)
                )
                session.add(new_order)
                session.commit()
                session.refresh(new_order)
                new_order_id = new_order.id
            
            # Mark old order as REPLACED
            existing_order.status = OrderStatus.REPLACED
            update_instance(existing_order)
            
            logger.info(f"Successfully replaced with STOP_LIMIT: old={existing_order.id}, new={new_order_id}, broker_id={replaced_order.id}")
            
            # Refresh to sync broker state
            self.refresh_orders()
            
            # Return the new order
            return get_instance(TradingOrder, new_order_id)
            
        except Exception as e:
            logger.error(f"Error replacing order {existing_order.id} with STOP_LIMIT: {e}", exc_info=True)
            raise
    
    def _is_tp_order(self, order: TradingOrder, entry_order: TradingOrder) -> bool:
        """
        Determine if an order is a take profit order based on its characteristics.
        
        TP orders are identified by:
        - Having limit_price set (exit at better price than entry)
        - Side opposite to entry order
        - For BUY entry: TP is SELL with limit_price > entry price
        - For SELL entry: TP is BUY with limit_price < entry price
        
        Supports legacy order types: SELL_LIMIT, BUY_LIMIT, STOP_LIMIT, OTO, OCO
        """
        if not order.limit_price:
            return False
        
        # TP is opposite side of entry
        if entry_order.side == OrderDirection.BUY:
            return order.side == OrderDirection.SELL and order.limit_price > (entry_order.open_price or 0)
        else:
            return order.side == OrderDirection.BUY and order.limit_price < (entry_order.open_price or 0)
    
    def _is_sl_order(self, order: TradingOrder, entry_order: TradingOrder) -> bool:
        """
        Determine if an order is a stop loss order based on its characteristics.
        
        SL orders are identified by:
        - Having stop_price set (exit at worse price than entry)
        - Side opposite to entry order
        - For BUY entry: SL is SELL with stop_price < entry price
        - For SELL entry: SL is BUY with stop_price > entry price
        
        Supports legacy order types: SELL_STOP, BUY_STOP, STOP_LIMIT, OTO, OCO
        """
        if not order.stop_price:
            return False
        
        # SL is opposite side of entry
        if entry_order.side == OrderDirection.BUY:
            return order.side == OrderDirection.SELL and order.stop_price < (entry_order.open_price or 0)
        else:
            return order.side == OrderDirection.BUY and order.stop_price > (entry_order.open_price or 0)
    
    def adjust_tp(self, transaction: Transaction, new_tp_price: float) -> bool:
        """
        Adjust take profit for a transaction.
        
        Args:
            transaction: Transaction to adjust TP for
            new_tp_price: New take profit price
            
        Returns:
            bool: True if adjustment succeeded
        """
        return self.adjust_tp_sl(transaction, new_tp_price=new_tp_price, new_sl_price=None)
    
    def adjust_sl(self, transaction: Transaction, new_sl_price: float) -> bool:
        """
        Adjust stop loss for a transaction.
        
        Args:
            transaction: Transaction to adjust SL for
            new_sl_price: New stop loss price
            
        Returns:
            bool: True if adjustment succeeded
        """
        return self.adjust_tp_sl(transaction, new_tp_price=None, new_sl_price=new_sl_price)
    
    def adjust_tp_sl(
        self, 
        transaction: Transaction, 
        new_tp_price: float | None = None, 
        new_sl_price: float | None = None
    ) -> bool:
        """
        Adjust take profit and/or stop loss for a transaction.
        
        This is the main implementation for transaction-level TP/SL management. It handles:
        - Entry order in PENDING state (creates/updates WAITING_TRIGGER orders)
        - Entry order FILLED (creates/updates broker orders)
        - Mixed states (OCO vs separate TP/SL orders)
        
        Args:
            transaction: Transaction to adjust
            new_tp_price: New take profit price (None = don't adjust TP)
            new_sl_price: New stop loss price (None = don't adjust SL)
            
        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(
            transaction=transaction,
            new_tp_price=new_tp_price,
            new_sl_price=new_sl_price
        )

    def _adjust_tpsl_internal(
        self, 
        transaction: Transaction, 
        new_tp_price: float | None = None, 
        new_sl_price: float | None = None
    ) -> bool:
        """
        Internal unified helper for adjusting TP and/or SL.
        
        This consolidates all the duplicated logic from adjust_tp(), adjust_sl(), and adjust_tp_sl().
        
        Args:
            transaction: Transaction to adjust
            new_tp_price: New take profit price (None = don't adjust TP)
            new_sl_price: New stop loss price (None = don't adjust SL)
            
        Returns:
            bool: True if adjustment succeeded
        """
        try:
            adjustment_type = []
            if new_tp_price is not None:
                adjustment_type.append(f"TP=${new_tp_price:.2f}")
            if new_sl_price is not None:
                adjustment_type.append(f"SL=${new_sl_price:.2f}")
            logger.info(f"Adjusting {', '.join(adjustment_type)} for transaction {transaction.id}")
            
            # Use a single session context to avoid SQLAlchemy session conflicts
            from sqlmodel import Session, select
            with Session(get_db().bind) as session:
                # 1. Get fresh transaction in this session (avoids session attachment conflicts)
                transaction_in_session = session.get(Transaction, transaction.id)
                if not transaction_in_session:
                    logger.error(f"Transaction {transaction.id} not found in database")
                    return False
                
                # 2. Early skip check: if values unchanged and valid orders exist WITH CORRECT PRICES, skip adjustment
                tp_unchanged = (new_tp_price is None or 
                               (transaction_in_session.take_profit is not None and 
                                abs(transaction_in_session.take_profit - new_tp_price) < 0.01))
                sl_unchanged = (new_sl_price is None or 
                               (transaction_in_session.stop_loss is not None and 
                                abs(transaction_in_session.stop_loss - new_sl_price) < 0.01))
                
                if tp_unchanged and sl_unchanged:
                    # Check if we have valid (non-error/non-canceled) TP/SL orders WITH MATCHING PRICES
                    valid_tpsl_orders = session.exec(
                        select(TradingOrder).where(
                            TradingOrder.transaction_id == transaction.id,
                            TradingOrder.order_type.in_([
                                CoreOrderType.OCO,
                                CoreOrderType.SELL_LIMIT, CoreOrderType.BUY_LIMIT,
                                CoreOrderType.SELL_STOP, CoreOrderType.BUY_STOP
                            ]),
                            TradingOrder.status.notin_([
                                OrderStatus.CANCELED, OrderStatus.EXPIRED, 
                                OrderStatus.ERROR, OrderStatus.REJECTED
                            ])
                        )
                    ).all()
                    
                    if valid_tpsl_orders:
                        # Verify that existing orders have correct prices
                        orders_have_correct_prices = True
                        for order in valid_tpsl_orders:
                            if order.order_type == CoreOrderType.OCO:
                                # OCO order has both TP (limit_price) and SL (stop_price)
                                if new_tp_price is not None and order.limit_price is not None:
                                    if abs(order.limit_price - new_tp_price) >= 0.01:
                                        logger.debug(f"OCO order {order.id} has TP=${order.limit_price:.2f}, expected ${new_tp_price:.2f}")
                                        orders_have_correct_prices = False
                                        break
                                if new_sl_price is not None and order.stop_price is not None:
                                    if abs(order.stop_price - new_sl_price) >= 0.01:
                                        logger.debug(f"OCO order {order.id} has SL=${order.stop_price:.2f}, expected ${new_sl_price:.2f}")
                                        orders_have_correct_prices = False
                                        break
                            elif order.order_type in [CoreOrderType.SELL_LIMIT, CoreOrderType.BUY_LIMIT]:
                                # Limit order is TP
                                if new_tp_price is not None and order.limit_price is not None:
                                    if abs(order.limit_price - new_tp_price) >= 0.01:
                                        logger.debug(f"TP order {order.id} has price=${order.limit_price:.2f}, expected ${new_tp_price:.2f}")
                                        orders_have_correct_prices = False
                                        break
                            elif order.order_type in [CoreOrderType.SELL_STOP, CoreOrderType.BUY_STOP]:
                                # Stop order is SL
                                if new_sl_price is not None and order.stop_price is not None:
                                    if abs(order.stop_price - new_sl_price) >= 0.01:
                                        logger.debug(f"SL order {order.id} has price=${order.stop_price:.2f}, expected ${new_sl_price:.2f}")
                                        orders_have_correct_prices = False
                                        break
                        
                        if orders_have_correct_prices:
                            logger.info(f"Skipping TP/SL adjustment for transaction {transaction.id}: "
                                       f"values unchanged and {len(valid_tpsl_orders)} valid order(s) already exist with correct prices")
                            return True
                        else:
                            logger.info(f"Proceeding with TP/SL adjustment for transaction {transaction.id}: "
                                       f"existing orders have incorrect prices")
                
                # 2. Update transaction (source of truth)
                if new_tp_price is not None:
                    transaction_in_session.take_profit = new_tp_price
                if new_sl_price is not None:
                    transaction_in_session.stop_loss = new_sl_price
                session.add(transaction_in_session)
                session.commit()
                
                # 3. Get entry order (first market/limit order for this transaction, not TP/SL)
                entry_order = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == transaction.id,
                        TradingOrder.order_type.in_([
                            CoreOrderType.MARKET, 
                            CoreOrderType.BUY_LIMIT, 
                            CoreOrderType.SELL_LIMIT
                        ])
                    ).order_by(TradingOrder.created_at)
                ).first()
                
                if not entry_order:
                    logger.error(f"No entry order found for transaction {transaction.id}")
                    return False
                
                # 3. Find existing TP/SL/OCO orders
                all_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == transaction.id,
                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                        TradingOrder.id != entry_order.id
                    )
                ).all()
                
                existing_tp = None
                existing_sl = None
                existing_oco = None
                
                for order in all_orders:
                    if order.order_type == CoreOrderType.OCO and order.limit_price and order.stop_price:
                        existing_oco = order
                    elif self._is_tp_order(order, entry_order):
                        existing_tp = order
                    elif self._is_sl_order(order, entry_order):
                        existing_sl = order
                
                # Determine if we need OCO (both TP and SL defined)
                has_tp = transaction_in_session.take_profit is not None and transaction_in_session.take_profit > 0
                has_sl = transaction_in_session.stop_loss is not None and transaction_in_session.stop_loss > 0
                need_oco = has_tp and has_sl
                
                logger.debug(f"Entry order {entry_order.id} status: {entry_order.status}, "
                           f"existing_tp: {existing_tp.id if existing_tp else None}, "
                           f"existing_sl: {existing_sl.id if existing_sl else None}, "
                           f"existing_oco: {existing_oco.id if existing_oco else None}, "
                           f"need_oco: {need_oco}")
                
                # 4. Determine action based on entry order state
                result = False
                if entry_order.status in OrderStatus.get_unsent_statuses():
                    # Entry not sent to broker yet - create/update pending orders
                    result = self._handle_pending_entry_tpsl(
                        session, transaction_in_session, entry_order, 
                        new_tp_price, new_sl_price,
                        existing_tp, existing_sl, existing_oco,
                        need_oco
                    )
                
                elif entry_order.status in OrderStatus.get_unfilled_statuses():
                    # Entry sent to broker but not filled yet - create triggered orders (OTO/OCO)
                    result = self._handle_submitted_entry_tpsl(
                        session, transaction_in_session, entry_order,
                        new_tp_price, new_sl_price,
                        existing_tp, existing_sl, existing_oco,
                        all_orders, need_oco
                    )
                
                elif entry_order.status in OrderStatus.get_executed_statuses():
                    # Entry filled - work with broker
                    result = self._handle_filled_entry_tpsl(
                        session, transaction_in_session, entry_order,
                        new_tp_price, new_sl_price,
                        existing_tp, existing_sl, existing_oco,
                        all_orders, need_oco
                    )
                
                else:
                    logger.warning(f"Entry order {entry_order.id} in unexpected state: {entry_order.status.value}")
                    result = False
                
                # Log activity for TP/SL adjustment
                if result:
                    try:
                        from ...core.db import log_activity
                        from ...core.types import ActivityLogSeverity, ActivityLogType
                        
                        adjustment_desc = []
                        if new_tp_price is not None:
                            adjustment_desc.append(f"TP to ${new_tp_price:.2f}")
                        if new_sl_price is not None:
                            adjustment_desc.append(f"SL to ${new_sl_price:.2f}")
                        
                        log_activity(
                            severity=ActivityLogSeverity.SUCCESS,
                            activity_type=ActivityLogType.TP_SL_ADJUSTED,
                            description=f"Adjusted {' and '.join(adjustment_desc)} for transaction {transaction_in_session.id} ({transaction_in_session.symbol})",
                            data={
                                "transaction_id": transaction_in_session.id,
                                "symbol": transaction_in_session.symbol,
                                "new_tp_price": new_tp_price,
                                "new_sl_price": new_sl_price,
                                "entry_order_status": entry_order.status.value
                            },
                            source_account_id=self.id
                        )
                    except Exception as log_error:
                        logger.warning(f"Failed to log TP/SL adjustment activity: {log_error}")
                
                return result
                    
        except Exception as e:
            logger.error(f"Error adjusting TP/SL for transaction {transaction.id}: {e}", exc_info=True)
            
            # Log activity for failed TP/SL adjustment
            try:
                from ...core.db import log_activity
                from ...core.types import ActivityLogSeverity, ActivityLogType
                
                adjustment_desc = []
                if new_tp_price is not None:
                    adjustment_desc.append(f"TP to ${new_tp_price:.2f}")
                if new_sl_price is not None:
                    adjustment_desc.append(f"SL to ${new_sl_price:.2f}")
                
                log_activity(
                    severity=ActivityLogSeverity.FAILURE,
                    activity_type=ActivityLogType.TP_SL_ADJUSTED,
                    description=f"Failed to adjust {' and '.join(adjustment_desc)} for transaction {transaction.id} ({transaction.symbol}): {str(e)}",
                    data={
                        "transaction_id": transaction.id,
                        "symbol": transaction.symbol,
                        "new_tp_price": new_tp_price,
                        "new_sl_price": new_sl_price,
                        "error": str(e)
                    },
                    source_account_id=self.id
                )
            except Exception as log_error:
                logger.warning(f"Failed to log TP/SL adjustment failure activity: {log_error}")
            
            return False
    
    def _handle_pending_entry_tpsl(
        self,
        session: Session,
        transaction: Transaction,
        entry_order: TradingOrder,
        new_tp_price: float | None,
        new_sl_price: float | None,
        existing_tp: TradingOrder | None,
        existing_sl: TradingOrder | None,
        existing_oco: TradingOrder | None,
        need_oco: bool
    ) -> bool:
        """Handle TP/SL adjustment when entry order is still pending (not sent to broker).
        
        Note: Entry orders may have quantity=0 at this stage because quantity is calculated
        later by the risk management system. TP/SL orders created here will have their
        quantity synced from the parent order when it's submitted to the broker.
        """
        
        # Skip quantity validation for dependent orders (orders with parent_order_id)
        # Dependent orders (TP/SL) get their quantity from the parent entry order
        if entry_order.depends_on_order is not None:
            logger.debug(
                f"Skipping quantity validation for dependent order {entry_order.id} "
                f"(parent order: {entry_order.depends_on_order})"
            )
        elif not entry_order.quantity or entry_order.quantity <= 0:
            # Entry order without parent has quantity=0 - this is normal for PENDING orders
            # The quantity will be calculated by risk management and synced to dependent orders
            logger.debug(
                f"Entry order {entry_order.id} has quantity {entry_order.quantity} - "
                f"TP/SL orders will be created with quantity=0 and synced later"
            )
        
        if need_oco:
            # Need OCO order with both TP and SL
            if existing_oco:
                # Update existing OCO
                if new_tp_price is not None:
                    existing_oco.limit_price = new_tp_price
                if new_sl_price is not None:
                    existing_oco.stop_price = new_sl_price
                session.add(existing_oco)
                session.commit()
                logger.info(f"Updated pending OCO order {existing_oco.id}")
            else:
                # Cancel separate TP/SL orders if they exist
                if existing_tp:
                    existing_tp.status = OrderStatus.CANCELED
                    session.add(existing_tp)
                if existing_sl:
                    existing_sl.status = OrderStatus.CANCELED
                    session.add(existing_sl)
                
                # Create new OCO order
                oco_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                oco_comment = self._generate_tpsl_comment("TPSL", self.id, transaction.id, entry_order.id)
                
                # Double-check TP/SL values before creating OCO order
                if not transaction.take_profit or transaction.take_profit <= 0:
                    logger.error(f"Cannot create OCO order for transaction {transaction.id}: invalid take_profit {transaction.take_profit}")
                    return False
                if not transaction.stop_loss or transaction.stop_loss <= 0:
                    logger.error(f"Cannot create OCO order for transaction {transaction.id}: invalid stop_loss {transaction.stop_loss}")
                    return False
                
                oco_order = TradingOrder(
                    account_id=self.id,
                    symbol=entry_order.symbol,
                    quantity=entry_order.quantity,
                    side=oco_side,
                    order_type=CoreOrderType.OCO,
                    limit_price=transaction.take_profit,
                    stop_price=transaction.stop_loss,
                    transaction_id=transaction.id,
                    status=OrderStatus.PENDING,
                    depends_on_order=entry_order.id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=oco_comment,
                    data={
                        "tp_percent_target": self._calculate_tp_percent(entry_order, transaction.take_profit) if transaction.take_profit else 0,
                        "sl_percent_target": self._calculate_sl_percent(entry_order, transaction.stop_loss) if transaction.stop_loss else 0
                    },
                    created_at=datetime.now(timezone.utc)
                )
                session.add(oco_order)
                session.commit()
                logger.info(f"Created pending OCO order {oco_order.id}")
        else:
            # Need separate TP and/or SL orders
            if new_tp_price is not None:
                if existing_oco:
                    # Had OCO, now only need TP - cancel OCO and create TP
                    existing_oco.status = OrderStatus.CANCELED
                    session.add(existing_oco)
                if existing_tp:
                    # Update existing TP
                    existing_tp.limit_price = new_tp_price
                    # Upgrade legacy order types
                    order_type_value = existing_tp.order_type.value if hasattr(existing_tp.order_type, 'value') else str(existing_tp.order_type)
                    if order_type_value not in ["sell_limit", "buy_limit"]:
                        tp_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                        existing_tp.order_type = CoreOrderType.SELL_LIMIT if tp_side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
                    session.add(existing_tp)
                    session.commit()
                    logger.info(f"Updated pending TP order {existing_tp.id}")
                else:
                    # Create new TP order
                    tp_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                    tp_comment = self._generate_tpsl_comment("TP", self.id, transaction.id, entry_order.id)
                    order_type = CoreOrderType.SELL_LIMIT if tp_side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
                    tp_order = TradingOrder(
                        account_id=self.id,
                        symbol=entry_order.symbol,
                        quantity=entry_order.quantity,
                        side=tp_side,
                        order_type=order_type,
                        limit_price=new_tp_price,
                        transaction_id=transaction.id,
                        status=OrderStatus.PENDING,
                        depends_on_order=entry_order.id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=tp_comment,
                        data={"tp_percent_target": self._calculate_tp_percent(entry_order, new_tp_price)},
                        created_at=datetime.now(timezone.utc)
                    )
                    session.add(tp_order)
                    session.commit()
                    logger.info(f"Created pending TP order {tp_order.id}")
            
            if new_sl_price is not None:
                if existing_oco:
                    # Had OCO, now only need SL - cancel OCO and create SL
                    existing_oco.status = OrderStatus.CANCELED
                    session.add(existing_oco)
                if existing_sl:
                    # Update existing SL
                    existing_sl.stop_price = new_sl_price
                    # Upgrade legacy order types
                    order_type_value = existing_sl.order_type.value if hasattr(existing_sl.order_type, 'value') else str(existing_sl.order_type)
                    if order_type_value not in ["sell_stop", "buy_stop"]:
                        sl_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                        existing_sl.order_type = CoreOrderType.SELL_STOP if sl_side == OrderDirection.SELL else CoreOrderType.BUY_STOP
                    session.add(existing_sl)
                    session.commit()
                    logger.info(f"Updated pending SL order {existing_sl.id}")
                else:
                    # Create new SL order
                    sl_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                    sl_comment = self._generate_tpsl_comment("SL", self.id, transaction.id, entry_order.id)
                    order_type = CoreOrderType.SELL_STOP if sl_side == OrderDirection.SELL else CoreOrderType.BUY_STOP
                    sl_order = TradingOrder(
                        account_id=self.id,
                        symbol=entry_order.symbol,
                        quantity=entry_order.quantity,
                        side=sl_side,
                        order_type=order_type,
                        stop_price=new_sl_price,
                        transaction_id=transaction.id,
                        status=OrderStatus.PENDING,
                        depends_on_order=entry_order.id,
                        depends_order_status_trigger=OrderStatus.FILLED,
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=sl_comment,
                        data={"sl_percent_target": self._calculate_sl_percent(entry_order, new_sl_price)},
                        created_at=datetime.now(timezone.utc)
                    )
                    session.add(sl_order)
                    session.commit()
                    logger.info(f"Created pending SL order {sl_order.id}")
        
        return True
    
    def _handle_filled_entry_tpsl(
        self,
        session: Session,
        transaction: Transaction,
        entry_order: TradingOrder,
        new_tp_price: float | None,
        new_sl_price: float | None,
        existing_tp: TradingOrder | None,
        existing_sl: TradingOrder | None,
        existing_oco: TradingOrder | None,
        all_orders: list,
        need_oco: bool
    ) -> bool:
        """Handle TP/SL adjustment when entry order is filled (at broker)."""
        
        # Cancel ALL existing TP/SL/OCO orders before creating new ones
        orders_to_cancel = []
        if existing_tp:
            orders_to_cancel.append(existing_tp)
        if existing_sl:
            orders_to_cancel.append(existing_sl)
        if existing_oco:
            orders_to_cancel.append(existing_oco)
        
        # Also check for any other non-terminal TP/SL orders we might have missed
        for order in all_orders:
            if order not in orders_to_cancel and order.id not in [
                existing_tp.id if existing_tp else None,
                existing_sl.id if existing_sl else None,
                existing_oco.id if existing_oco else None
            ]:
                orders_to_cancel.append(order)
        
        if orders_to_cancel:
            logger.info(f"Cancelling {len(orders_to_cancel)} existing TP/SL/OCO orders before creating new ones")
            for order in orders_to_cancel:
                if order.broker_order_id:
                    # Order at broker - cancel via API
                    try:
                        self.cancel_order(order.id)
                        logger.info(f"Cancelled broker order {order.id} (broker_id={order.broker_order_id})")
                    except Exception as e:
                        logger.warning(f"Failed to cancel broker order {order.id}: {e}")
                else:
                    # Pending order - just mark as cancelled in DB
                    order.status = OrderStatus.CANCELED
                    session.add(order)
                    logger.info(f"Cancelled pending order {order.id}")
            session.commit()
        
        # Create new order(s) at broker
        if need_oco:
            # Create OCO order with both TP and SL
            return self._create_broker_oco_order(session, transaction, entry_order, transaction.take_profit, transaction.stop_loss)
        else:
            # Create separate TP and/or SL orders
            success = True
            if new_tp_price is not None:
                success = success and self._create_broker_tp_order(session, transaction, entry_order, new_tp_price)
            if new_sl_price is not None:
                success = success and self._create_broker_sl_order(session, transaction, entry_order, new_sl_price)
            return success
    
    def _handle_submitted_entry_tpsl(
        self,
        session: Session,
        transaction: Transaction,
        entry_order: TradingOrder,
        new_tp_price: float | None,
        new_sl_price: float | None,
        existing_tp: TradingOrder | None,
        existing_sl: TradingOrder | None,
        existing_oco: TradingOrder | None,
        all_orders: list,
        need_oco: bool
    ) -> bool:
        """Handle TP/SL adjustment when entry order is submitted but not yet filled (PENDING_NEW, OPEN, etc.)."""
        
        logger.info(f"Creating triggered TP/SL orders for submitted entry order {entry_order.id} in state {entry_order.status}")
        
        # Cancel ALL existing TP/SL/OCO orders before creating triggered ones
        orders_to_cancel = []
        if existing_tp:
            orders_to_cancel.append(existing_tp)
        if existing_sl:
            orders_to_cancel.append(existing_sl)
        if existing_oco:
            orders_to_cancel.append(existing_oco)
        
        # Also check for any other non-terminal TP/SL orders we might have missed
        for order in all_orders:
            if order not in orders_to_cancel and order.id not in [
                existing_tp.id if existing_tp else None,
                existing_sl.id if existing_sl else None,
                existing_oco.id if existing_oco else None
            ]:
                orders_to_cancel.append(order)
        
        if orders_to_cancel:
            logger.info(f"Cancelling {len(orders_to_cancel)} existing TP/SL/OCO orders before creating triggered ones")
            for order in orders_to_cancel:
                if order.broker_order_id:
                    # Order at broker - cancel via API
                    try:
                        self.cancel_order(order.id)
                        logger.info(f"Cancelled broker order {order.id} (broker_id={order.broker_order_id})")
                    except Exception as e:
                        logger.warning(f"Failed to cancel broker order {order.id}: {e}")
                else:
                    # Pending order - just mark as cancelled in DB
                    order.status = OrderStatus.CANCELED
                    session.add(order)
                    logger.info(f"Cancelled pending order {order.id}")
            session.commit()
        
        # Create triggered order(s) in database only - they'll be submitted when entry order fills
        if need_oco:
            # Validate TP/SL prices before creating OCO order
            if not new_tp_price or new_tp_price <= 0:
                logger.error(f"Cannot create triggered OCO order for transaction {transaction.id}: invalid take_profit {new_tp_price}")
                return False
            if not new_sl_price or new_sl_price <= 0:
                logger.error(f"Cannot create triggered OCO order for transaction {transaction.id}: invalid stop_loss {new_sl_price}")
                return False
                
            # Create triggered OCO order with both TP and SL
            oco_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
            oco_comment = self._generate_tpsl_comment("TPSL", self.id, transaction.id, entry_order.id)
            oco_order = TradingOrder(
                account_id=self.id,
                symbol=entry_order.symbol,
                quantity=entry_order.quantity,
                side=oco_side,
                order_type=CoreOrderType.OCO,
                limit_price=new_tp_price,
                stop_price=new_sl_price,
                transaction_id=transaction.id,
                status=OrderStatus.PENDING,
                depends_on_order=entry_order.id,
                depends_order_status_trigger=OrderStatus.FILLED,
                open_type=OrderOpenType.AUTOMATIC,
                comment=oco_comment,
                data={
                    "tp_percent_target": self._calculate_tp_percent(entry_order, new_tp_price) if new_tp_price else 0,
                    "sl_percent_target": self._calculate_sl_percent(entry_order, new_sl_price) if new_sl_price else 0
                },
                created_at=datetime.now(timezone.utc)
            )
            session.add(oco_order)
            session.commit()
            logger.info(f"Created triggered OCO order {oco_order.id} waiting for entry order {entry_order.id} to fill")
        else:
            # Create separate triggered TP and/or SL orders
            success = True
            
            if new_tp_price is not None:
                tp_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                tp_order_type = CoreOrderType.SELL_LIMIT if tp_side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
                tp_comment = self._generate_tpsl_comment("TP", self.id, transaction.id, entry_order.id)
                tp_order = TradingOrder(
                    account_id=self.id,
                    symbol=entry_order.symbol,
                    quantity=entry_order.quantity,
                    side=tp_side,
                    order_type=tp_order_type,
                    limit_price=new_tp_price,
                    transaction_id=transaction.id,
                    status=OrderStatus.PENDING,
                    depends_on_order=entry_order.id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=tp_comment,
                    data={"tp_percent_target": self._calculate_tp_percent(entry_order, new_tp_price)},
                    created_at=datetime.now(timezone.utc)
                )
                session.add(tp_order)
                logger.info(f"Created triggered TP order {tp_order.id} waiting for entry order {entry_order.id} to fill")
                
            if new_sl_price is not None:
                sl_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
                sl_order_type = CoreOrderType.SELL_STOP if sl_side == OrderDirection.SELL else CoreOrderType.BUY_STOP
                sl_comment = self._generate_tpsl_comment("SL", self.id, transaction.id, entry_order.id)
                sl_order = TradingOrder(
                    account_id=self.id,
                    symbol=entry_order.symbol,
                    quantity=entry_order.quantity,
                    side=sl_side,
                    order_type=sl_order_type,
                    stop_price=new_sl_price,
                    transaction_id=transaction.id,
                    status=OrderStatus.PENDING,
                    depends_on_order=entry_order.id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=sl_comment,
                    data={"sl_percent_target": self._calculate_sl_percent(entry_order, new_sl_price)},
                    created_at=datetime.now(timezone.utc)
                )
                session.add(sl_order)
                logger.info(f"Created triggered SL order {sl_order.id} waiting for entry order {entry_order.id} to fill")
                
            session.commit()
            
        logger.info(f"Successfully created triggered TP/SL orders for submitted entry order {entry_order.id}")
        return True
    
    def adjust_tp(self, transaction: Transaction, new_tp_price: float) -> bool:
        """
        Adjust take profit for a transaction.
        
        Args:
            transaction: Transaction to adjust TP for
            new_tp_price: New take profit price
            
        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(transaction, new_tp_price=new_tp_price, new_sl_price=None)
    
    def _calculate_tp_percent(self, entry_order: TradingOrder, tp_price: float) -> float:
        """Calculate TP percent from entry price"""
        if not entry_order.open_price or entry_order.open_price == 0:
            return 0.0
        return ((tp_price - entry_order.open_price) / entry_order.open_price) * 100
    
    def _create_broker_tp_order(self, session: Session, transaction: Transaction, entry_order: TradingOrder, tp_price: float) -> bool:
        """Create new TP order at broker using OCO (both TP+SL) or simple limit order (TP only)"""
        try:
            # Use transaction.quantity as source of truth (handles partial closes)
            order_quantity = transaction.quantity
            if not order_quantity or order_quantity <= 0:
                logger.error(f"Cannot create TP order for transaction {transaction.id}: transaction has invalid quantity {order_quantity}")
                return False
            
            logger.info(f"Creating TP order at broker for transaction {transaction.id} with qty={order_quantity}")
            
            # Determine if we need OCO (both TP and SL) or simple limit order (only TP)
            has_sl = transaction.stop_loss is not None and transaction.stop_loss > 0
            
            # Validate TP price
            if not tp_price or tp_price <= 0:
                logger.error(f"Cannot create TP order for transaction {transaction.id}: invalid take_profit {tp_price}")
                return False
            
            # Validate SL price if creating OCO
            if has_sl:
                if not transaction.stop_loss or transaction.stop_loss <= 0:
                    logger.error(f"Cannot create OCO TP order for transaction {transaction.id}: invalid stop_loss {transaction.stop_loss}")
                    return False
            
            # Create TP order
            tp_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
            
            # Determine order type based on whether we have SL
            if has_sl:
                order_type = CoreOrderType.OCO
            else:
                # Use direction-specific limit type
                order_type = CoreOrderType.SELL_LIMIT if tp_side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
            
            tp_comment = self._generate_tpsl_comment(
                "TPSL" if has_sl else "TP",
                self.id,
                transaction.id,
                entry_order.id
            )
            tp_order = TradingOrder(
                account_id=self.id,
                symbol=entry_order.symbol,
                quantity=order_quantity,
                side=tp_side,
                order_type=order_type,
                limit_price=tp_price,
                stop_price=transaction.stop_loss if has_sl else None,  # Include SL if OCO
                transaction_id=transaction.id,
                status=OrderStatus.PENDING,
                open_type=OrderOpenType.AUTOMATIC,
                comment=tp_comment,
                data={"tp_percent_target": self._calculate_tp_percent(entry_order, tp_price)},
                created_at=datetime.now(timezone.utc)
            )
            session.add(tp_order)
            session.commit()
            session.refresh(tp_order)
            
            # Submit to broker
            logger.info(f"Submitting {order_type.value} TP order {tp_order.id} to broker")
            try:
                self.submit_order(tp_order)
                logger.info(f"Successfully submitted {order_type.value} TP order {tp_order.id} for transaction {transaction.id}")
                return True
            except Exception as e:
                logger.error(f"Failed to submit TP order to broker: {e}", exc_info=True)
                return False
            
        except Exception as e:
            logger.error(f"Error creating broker TP order: {e}", exc_info=True)
            return False
    
    def _replace_broker_tp_order(self, session: Session, existing_tp: TradingOrder, new_tp_price: float) -> bool:
        """
        Replace existing TP order at broker.
        
        Handles SELL_LIMIT/BUY_LIMIT ↔ OCO transitions based on current transaction state.
        """
        try:
            logger.info(f"Attempting to replace TP order {existing_tp.id} at broker")
            
            # Get transaction to check if we need limit or OCO
            transaction = get_instance(Transaction, existing_tp.transaction_id)
            has_sl = transaction.stop_loss is not None and transaction.stop_loss > 0
            
            # Validate TP price
            if not new_tp_price or new_tp_price <= 0:
                logger.error(f"Cannot replace TP order {existing_tp.id}: invalid take_profit {new_tp_price}")
                return False
            
            # Validate SL price if creating OCO
            if has_sl:
                if not transaction.stop_loss or transaction.stop_loss <= 0:
                    logger.error(f"Cannot replace TP order {existing_tp.id} with OCO: invalid stop_loss {transaction.stop_loss}")
                    return False
            
            # Determine correct order type
            if has_sl:
                new_order_type = CoreOrderType.OCO
            else:
                # Use direction-specific limit type
                new_order_type = CoreOrderType.SELL_LIMIT if existing_tp.side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
            
            # Check if order type needs to change (SELL_LIMIT/BUY_LIMIT ↔ OCO transition)
            old_order_type = existing_tp.order_type.value if hasattr(existing_tp.order_type, 'value') else str(existing_tp.order_type)
            
            # Check if we need to change order type (between limit types and OCO, or legacy OTO)
            type_change_needed = False
            if has_sl and old_order_type not in ["oco"]:
                type_change_needed = True
            elif not has_sl and old_order_type not in ["sell_limit", "buy_limit"]:
                type_change_needed = True
            
            if type_change_needed:
                logger.info(f"Order type transition detected: {old_order_type} → {new_order_type.value}. Canceling old order and creating new one.")
                # Can't replace when order type changes - must cancel and create new
                existing_tp.status = OrderStatus.PENDING_CANCEL
                session.add(existing_tp)
                
                # Create new pending order with correct type
                new_tp = TradingOrder(
                    account_id=existing_tp.account_id,
                    symbol=existing_tp.symbol,
                    quantity=existing_tp.quantity,
                    side=existing_tp.side,
                    order_type=new_order_type,
                    limit_price=new_tp_price,
                    stop_price=transaction.stop_loss if has_sl else None,  # Include SL if OCO
                    transaction_id=existing_tp.transaction_id,
                    status=OrderStatus.PENDING,
                    depends_on_order=existing_tp.id,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=f"TP order (type change: {old_order_type}→{new_order_type.value}, pending cancel of {existing_tp.id})",
                    data=existing_tp.data.copy() if existing_tp.data else {},
                    created_at=datetime.now(timezone.utc)
                )
                session.add(new_tp)
                session.commit()
                
                # Cancel the old order
                self.cancel_order(existing_tp.id)
                
                logger.info(f"Created new pending {new_order_type.value} order {new_tp.id} to replace {existing_tp.id}")
                return True
            
            # Try to use replace_order API (same order type, just price change)
            try:
                self._update_broker_tp_order(existing_tp, new_tp_price)
                logger.info(f"Successfully replaced TP order {existing_tp.id}")
                return True
            except APIError as e:
                error_msg = str(e).lower()
                if "cannot replace order" in error_msg or "42210000" in error_msg:
                    # Replace failed - create PENDING_CANCEL order
                    logger.warning(f"Cannot replace TP order {existing_tp.id} (error: {e}), creating PENDING_CANCEL order")
                    
                    # Mark existing order as PENDING_CANCEL
                    existing_tp.status = OrderStatus.PENDING_CANCEL
                    session.add(existing_tp)
                    
                    # Create new pending TP order to replace it
                    new_tp = TradingOrder(
                        account_id=existing_tp.account_id,
                        symbol=existing_tp.symbol,
                        quantity=existing_tp.quantity,
                        side=existing_tp.side,
                        order_type=new_order_type,  # Use determined order type
                        limit_price=new_tp_price,
                        stop_price=transaction.stop_loss if has_sl else None,
                        transaction_id=existing_tp.transaction_id,
                        status=OrderStatus.PENDING,
                        depends_on_order=existing_tp.id,  # Depends on old order being cancelled
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=f"TP order (pending cancel of {existing_tp.id})",
                        data=existing_tp.data.copy() if existing_tp.data else {},
                        created_at=datetime.now(timezone.utc)
                    )
                    session.add(new_tp)
                    session.commit()
                    
                    # Cancel the old order
                    self.cancel_order(existing_tp.id)
                    
                    logger.info(f"Created new pending TP order {new_tp.id} to replace {existing_tp.id}")
                    return True
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Error replacing broker TP order: {e}", exc_info=True)
            return False
    
    def adjust_sl(self, transaction: Transaction, new_sl_price: float) -> bool:
        """
        Adjust stop loss for a transaction.
        
        Args:
            transaction: Transaction to adjust SL for
            new_sl_price: New stop loss price
            
        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(transaction, new_tp_price=None, new_sl_price=new_sl_price)
    
    def _calculate_sl_percent(self, entry_order: TradingOrder, sl_price: float) -> float:
        """Calculate SL percent from entry price"""
        if not entry_order.open_price or entry_order.open_price == 0:
            return 0.0
        return ((entry_order.open_price - sl_price) / entry_order.open_price) * 100
    
    def _create_broker_oco_order(self, session: Session, transaction: Transaction, entry_order: TradingOrder, tp_price: float, sl_price: float) -> bool:
        """Create new OCO order at broker with both TP and SL."""
        try:
            # Validate TP/SL prices before creating OCO order
            if not tp_price or tp_price <= 0:
                logger.error(f"Cannot create broker OCO order for transaction {transaction.id}: invalid take_profit {tp_price}")
                return False
            if not sl_price or sl_price <= 0:
                logger.error(f"Cannot create broker OCO order for transaction {transaction.id}: invalid stop_loss {sl_price}")
                return False
            
            # Use transaction.quantity as source of truth (handles partial closes)
            # This is the current position size that needs TP/SL protection
            order_quantity = transaction.quantity
            if not order_quantity or order_quantity <= 0:
                logger.error(f"Cannot create OCO order for transaction {transaction.id}: transaction has invalid quantity {order_quantity}")
                return False
            
            logger.info(f"Creating OCO order at broker for transaction {transaction.id} with TP=${tp_price:.2f}, SL=${sl_price:.2f}, qty={order_quantity}")
            
            oco_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
            oco_comment = self._generate_tpsl_comment("TPSL", self.id, transaction.id, entry_order.id)
            
            oco_order = TradingOrder(
                account_id=self.id,
                symbol=entry_order.symbol,
                quantity=order_quantity,
                side=oco_side,
                order_type=CoreOrderType.OCO,
                limit_price=tp_price,
                stop_price=sl_price,
                transaction_id=transaction.id,
                status=OrderStatus.PENDING,
                open_type=OrderOpenType.AUTOMATIC,
                comment=oco_comment,
                data={
                    "tp_percent_target": self._calculate_tp_percent(entry_order, tp_price),
                    "sl_percent_target": self._calculate_sl_percent(entry_order, sl_price)
                },
                created_at=datetime.now(timezone.utc)
            )
            session.add(oco_order)
            session.commit()
            session.refresh(oco_order)
            
            # Submit to broker
            logger.info(f"Submitting OCO order {oco_order.id} to broker")
            try:
                self.submit_order(oco_order)
                logger.info(f"Successfully submitted OCO order {oco_order.id} for transaction {transaction.id}")
                return True
            except Exception as e:
                logger.error(f"Failed to submit OCO order to broker: {e}", exc_info=True)
                return False
            
        except Exception as e:
            logger.error(f"Error creating broker OCO order: {e}", exc_info=True)
            return False
    
    def _create_broker_sl_order(self, session: Session, transaction: Transaction, entry_order: TradingOrder, sl_price: float) -> bool:
        """Create new SL order at broker using OCO (both TP+SL) or simple stop order (SL only)"""
        try:
            # Use transaction.quantity as source of truth (handles partial closes)
            order_quantity = transaction.quantity
            if not order_quantity or order_quantity <= 0:
                logger.error(f"Cannot create SL order for transaction {transaction.id}: transaction has invalid quantity {order_quantity}")
                return False
            
            logger.info(f"Creating SL order at broker for transaction {transaction.id} with qty={order_quantity}")
            
            has_tp = transaction.take_profit is not None and transaction.take_profit > 0
            
            # Validate SL price
            if not sl_price or sl_price <= 0:
                logger.error(f"Cannot create SL order for transaction {transaction.id}: invalid stop_loss {sl_price}")
                return False
            
            # Validate TP price if creating OCO
            if has_tp:
                if not transaction.take_profit or transaction.take_profit <= 0:
                    logger.error(f"Cannot create OCO SL order for transaction {transaction.id}: invalid take_profit {transaction.take_profit}")
                    return False
            
            sl_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
            
            # Determine order type based on whether we have TP
            if has_tp:
                order_type = CoreOrderType.OCO
            else:
                # Use direction-specific stop type
                order_type = CoreOrderType.SELL_STOP if sl_side == OrderDirection.SELL else CoreOrderType.BUY_STOP
            
            sl_comment = self._generate_tpsl_comment(
                "TPSL" if has_tp else "SL",
                self.id,
                transaction.id,
                entry_order.id
            )
            sl_order = TradingOrder(
                account_id=self.id,
                symbol=entry_order.symbol,
                quantity=order_quantity,
                side=sl_side,
                order_type=order_type,
                stop_price=sl_price,
                limit_price=transaction.take_profit if has_tp else None,  # Include TP if OCO
                transaction_id=transaction.id,
                status=OrderStatus.PENDING,
                open_type=OrderOpenType.AUTOMATIC,
                comment=sl_comment,
                data={"sl_percent_target": self._calculate_sl_percent(entry_order, sl_price)},
                created_at=datetime.now(timezone.utc)
            )
            session.add(sl_order)
            session.commit()
            session.refresh(sl_order)
            
            # Submit to broker
            logger.info(f"Submitting {order_type.value} SL order {sl_order.id} to broker")
            try:
                self.submit_order(sl_order)
                logger.info(f"Successfully submitted {order_type.value} SL order {sl_order.id} for transaction {transaction.id}")
                return True
            except Exception as e:
                logger.error(f"Failed to submit SL order to broker: {e}", exc_info=True)
                return False
            
        except Exception as e:
            logger.error(f"Error creating broker SL order: {e}", exc_info=True)
            return False
    
    def _replace_broker_sl_order(self, session: Session, existing_sl: TradingOrder, new_sl_price: float) -> bool:
        """
        Replace existing SL order at broker.
        
        Handles SELL_STOP/BUY_STOP ↔ OCO transitions based on current transaction state.
        """
        try:
            logger.info(f"Attempting to replace SL order {existing_sl.id} at broker")
            
            # Get transaction to check if we need stop or OCO
            transaction = get_instance(Transaction, existing_sl.transaction_id)
            has_tp = transaction.take_profit is not None and transaction.take_profit > 0
            
            # Validate SL price
            if not new_sl_price or new_sl_price <= 0:
                logger.error(f"Cannot replace SL order {existing_sl.id}: invalid stop_loss {new_sl_price}")
                return False
            
            # Validate TP price if creating OCO
            if has_tp:
                if not transaction.take_profit or transaction.take_profit <= 0:
                    logger.error(f"Cannot replace SL order {existing_sl.id} with OCO: invalid take_profit {transaction.take_profit}")
                    return False
            
            # Determine correct order type
            if has_tp:
                new_order_type = CoreOrderType.OCO
            else:
                # Use direction-specific stop type
                new_order_type = CoreOrderType.SELL_STOP if existing_sl.side == OrderDirection.SELL else CoreOrderType.BUY_STOP
            
            # Check if order type needs to change (SELL_STOP/BUY_STOP ↔ OCO transition)
            old_order_type = existing_sl.order_type.value if hasattr(existing_sl.order_type, 'value') else str(existing_sl.order_type)
            
            # Check if we need to change order type (between stop types and OCO, or legacy OTO)
            type_change_needed = False
            if has_tp and old_order_type not in ["oco"]:
                type_change_needed = True
            elif not has_tp and old_order_type not in ["sell_stop", "buy_stop"]:
                type_change_needed = True
            
            if type_change_needed:
                logger.info(f"Order type transition detected: {old_order_type} → {new_order_type.value}. Canceling old order and creating new one.")
                # Can't replace when order type changes - must cancel and create new
                existing_sl.status = OrderStatus.PENDING_CANCEL
                session.add(existing_sl)
                
                # Create new pending order with correct type
                new_sl = TradingOrder(
                    account_id=existing_sl.account_id,
                    symbol=existing_sl.symbol,
                    quantity=existing_sl.quantity,
                    side=existing_sl.side,
                    order_type=new_order_type,
                    limit_price=transaction.take_profit if has_tp else None,  # Include TP if OCO
                    stop_price=new_sl_price,
                    transaction_id=existing_sl.transaction_id,
                    status=OrderStatus.PENDING,
                    depends_on_order=existing_sl.id,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=f"SL order (type change: {old_order_type}→{new_order_type.value}, pending cancel of {existing_sl.id})",
                    data=existing_sl.data.copy() if existing_sl.data else {},
                    created_at=datetime.now(timezone.utc)
                )
                session.add(new_sl)
                session.commit()
                
                # Cancel the old order
                self.cancel_order(existing_sl.id)
                
                logger.info(f"Created new pending {new_order_type.value} order {new_sl.id} to replace {existing_sl.id}")
                return True
            
            # Try to use replace_order API (same order type, just price change)
            try:
                self._update_broker_sl_order(existing_sl, new_sl_price)
                logger.info(f"Successfully replaced SL order {existing_sl.id}")
                return True
            except APIError as e:
                error_msg = str(e).lower()
                if "cannot replace order" in error_msg or "42210000" in error_msg:
                    logger.warning(f"Cannot replace SL order {existing_sl.id} (error: {e}), creating PENDING_CANCEL order")
                    
                    existing_sl.status = OrderStatus.PENDING_CANCEL
                    session.add(existing_sl)
                    
                    new_sl = TradingOrder(
                        account_id=existing_sl.account_id,
                        symbol=existing_sl.symbol,
                        quantity=existing_sl.quantity,
                        side=existing_sl.side,
                        order_type=new_order_type,  # Use determined order type
                        limit_price=transaction.take_profit if has_tp else None,
                        stop_price=new_sl_price,
                        transaction_id=existing_sl.transaction_id,
                        status=OrderStatus.PENDING,
                        depends_on_order=existing_sl.id,
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=f"SL order (pending cancel of {existing_sl.id})",
                        data=existing_sl.data.copy() if existing_sl.data else {},
                        created_at=datetime.now(timezone.utc)
                    )
                    session.add(new_sl)
                    session.commit()
                    
                    self.cancel_order(existing_sl.id)
                    
                    logger.info(f"Created new pending SL order {new_sl.id} to replace {existing_sl.id}")
                    return True
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Error replacing broker SL order: {e}", exc_info=True)
            return False
    
    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: float, new_sl_price: float) -> bool:
        """
        Adjust both take profit and stop loss for a transaction.
        
        Args:
            transaction: Transaction to adjust TP/SL for
            new_tp_price: New take profit price
            new_sl_price: New stop loss price
            
        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(transaction, new_tp_price=new_tp_price, new_sl_price=new_sl_price)
    
    def _replace_broker_oco_order(self, session: Session, existing_oco: TradingOrder, new_tp_price: float, new_sl_price: float) -> bool:
        """
        Replace existing OCO order at broker with new TP/SL prices.
        
        Attempts replace first, falls back to PENDING_CANCEL + new order on failure.
        """
        try:
            logger.info(f"Attempting to replace OCO order {existing_oco.id} at broker")
            
            # Try to use replace_order API
            try:
                from alpaca.trading.requests import ReplaceOrderRequest
                
                replace_request = ReplaceOrderRequest(
                    qty=existing_oco.quantity,
                    limit_price=new_tp_price,
                    stop_price=new_sl_price
                )
                
                replaced_order = self.client.replace_order_by_id(
                    order_id=existing_oco.broker_order_id,
                    order_data=replace_request
                )
                
                # Update existing order record with new prices and broker ID
                existing_oco.limit_price = new_tp_price
                existing_oco.stop_price = new_sl_price
                existing_oco.broker_order_id = str(replaced_order.id)
                existing_oco.data = {
                    "tp_percent_target": existing_oco.data.get("tp_percent_target", 0) if existing_oco.data else 0,
                    "sl_percent_target": existing_oco.data.get("sl_percent_target", 0) if existing_oco.data else 0
                }
                session.add(existing_oco)
                session.commit()
                
                logger.info(f"Successfully replaced OCO order {existing_oco.id} with new broker ID {replaced_order.id}")
                return True
                
            except APIError as e:
                error_msg = str(e).lower()
                if "cannot replace order" in error_msg or "42210000" in error_msg:
                    # Replace failed - create PENDING_CANCEL order
                    logger.warning(f"Cannot replace OCO order {existing_oco.id} (error: {e}), creating PENDING_CANCEL order")
                    
                    # Mark existing order as PENDING_CANCEL
                    existing_oco.status = OrderStatus.PENDING_CANCEL
                    session.add(existing_oco)
                    
                    # Validate TP/SL prices before creating replacement OCO order
                    if not new_tp_price or new_tp_price <= 0:
                        logger.error(f"Cannot create replacement OCO order: invalid take_profit {new_tp_price}")
                        raise ValueError(f"Invalid take_profit price for OCO replacement: {new_tp_price}")
                    if not new_sl_price or new_sl_price <= 0:
                        logger.error(f"Cannot create replacement OCO order: invalid stop_loss {new_sl_price}")
                        raise ValueError(f"Invalid stop_loss price for OCO replacement: {new_sl_price}")
                        
                    # Create new pending OCO order to replace it
                    new_oco = TradingOrder(
                        account_id=existing_oco.account_id,
                        symbol=existing_oco.symbol,
                        quantity=existing_oco.quantity,
                        side=existing_oco.side,
                        order_type=CoreOrderType.OCO,
                        limit_price=new_tp_price,
                        stop_price=new_sl_price,
                        transaction_id=existing_oco.transaction_id,
                        status=OrderStatus.PENDING,
                        depends_on_order=existing_oco.id,  # Depends on old order being cancelled
                        open_type=OrderOpenType.AUTOMATIC,
                        comment=f"OCO order (TP+SL) (pending cancel of {existing_oco.id})",
                        data={
                            "tp_percent_target": existing_oco.data.get("tp_percent_target", 0) if existing_oco.data else 0,
                            "sl_percent_target": existing_oco.data.get("sl_percent_target", 0) if existing_oco.data else 0
                        },
                        created_at=datetime.now(timezone.utc)
                    )
                    session.add(new_oco)
                    session.commit()
                    
                    # Cancel the old order
                    self.cancel_order(existing_oco.id)
                    
                    logger.info(f"Created new pending OCO order {new_oco.id} to replace {existing_oco.id}")
                    return True
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Error replacing broker OCO order: {e}", exc_info=True)
            return False




