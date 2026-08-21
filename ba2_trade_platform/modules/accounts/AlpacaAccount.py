from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, LimitOrderRequest, StopOrderRequest, StopLimitOrderRequest, ReplaceOrderRequest, TakeProfitRequest, StopLossRequest, OptionLegRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass, PositionIntent
from alpaca.common.exceptions import APIError
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import math
import time
import threading
import functools

from ...logger import logger
from ...core.models import TradingOrder, Position, Transaction
from ...core.types import OrderDirection, OrderStatus, OrderOpenType, OrderType as CoreOrderType, TransactionStatus
from ...core.interfaces import AccountInterface
from ...core.account_types import (
    AccountSnapshot, CashTransfer, MarginInfo,
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_WITHDRAWAL, CASH_TRANSFER_DIVIDEND,
    MARGIN_SOURCE_ASSET,
)
from ...core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ...core.db import get_db, get_instance, update_instance, add_instance
from sqlmodel import Session, select

# Namespace of the external_id get_cash_transfers() mints for dividends, whether
# from the broker's own DIV activity id or from the symbol/date fallback. Keeps the
# dividend key space disjoint from the verbatim CSD/CSW broker ids that share the
# same upsert column.
_DIVIDEND_KEY_PREFIX = "DIV:"


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



class AlpacaAccount(AccountInterface, OptionsAccountInterface):
    """
    A class that implements the AccountInterface for interacting with Alpaca trading accounts.
    This class provides methods for managing orders, positions, and account information through
    the Alpaca trading API.

    Also implements OptionsAccountInterface (option chain/quote/ATM-IV market data;
    positions / order submission / close / IV-rank land in later tasks).
    """

    # Lifetime of one _margin_info_cache entry. A CLASS attribute so that a bare
    # instance built with object.__new__ (the test idiom) still has it.
    _MARGIN_INFO_CACHE_TTL = 24 * 60 * 60

    # Lifetime of the _account_snapshot_cache. Deliberately FIVE SECONDS, not the
    # margin cache's 24 hours: that one holds STATIC ASSET FACTS, this one holds
    # MONEY (equity, buying power, cash), and a stale equity used for a risk check
    # is its own bug.
    #
    # 5.0 because it is the SAME NUMBER as _BALANCE_CACHE_TTL and the same
    # underlying endpoint (client.get_account()) publishing the same fields. Two
    # different staleness windows over one source would let get_balance() and
    # get_account_snapshot().equity disagree in the same pass for no reason.
    #
    # It is short enough to stay honest -- the position-size cap is a PERCENTAGE of
    # equity (typically 5-20%), and intraday equity cannot move enough in 5 s to
    # flip such a comparison -- and long enough to collapse the real burst, which is
    # an allocation / rebalance pass validating and submitting a whole basket
    # back-to-back inside one second. The events that DO move buying power
    # discontinuously are OUR OWN submissions, and those are handled by explicit
    # invalidation (invalidate_balance_cache), not by waiting out a TTL.
    _ACCOUNT_SNAPSHOT_CACHE_TTL = 5.0

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

        # Per-symbol margin metadata cache, {symbol: (fetched_at, MarginInfo)}.
        # Alpaca has NO bulk asset endpoint, so get_symbol_margin_info() costs one
        # get_asset() HTTP call per symbol not already cached, and the allocation page
        # asks for the same basket on every refresh.
        #
        # The entries EXPIRE after _MARGIN_INFO_CACHE_TTL. The ASSET facts do not change
        # intraday, but get_account_instance_from_id() hands out the same account object
        # for the whole PROCESS lifetime, and on a server up for weeks Alpaca does revoke
        # marginability / fractionability on individual names -- a frozen marginable=True
        # understates bp_cost. clear_margin_info_cache() drops them on demand for an
        # explicit user Refresh. bp_factor is re-derived on every hit (the multiplier
        # moves between 1/2/4 far more often than the asset facts do).
        self._margin_info_cache: Dict[str, Tuple[float, MarginInfo]] = {}

        # Account-level snapshot cache, (fetched_at, AccountSnapshot) or None.
        #
        # get_account_snapshot() costs TWO REST round trips on Alpaca -- get_account()
        # for the money plus get_account_configurations() for the fractional flag --
        # and it is on the LIVE order path: _validate_position_size_limits reads
        # equity through it on every market-order validation, so a basket paid for
        # two calls per order. See _ACCOUNT_SNAPSHOT_CACHE_TTL for why the window is
        # 5 s and not the margin cache's 24 h. Cleared explicitly by
        # clear_account_snapshot_cache(), and by invalidate_balance_cache() because a
        # submitted order changes the buying power this snapshot carries.
        self._account_snapshot_cache: Optional[Tuple[float, AccountSnapshot]] = None

        # Balance cache (5s TTL; serves stale value on fetch failure)
        self._balance_cache: Optional[float] = None
        self._balance_cache_time: float = 0.0
        self._balance_cache_lock = threading.Lock()
        self._BALANCE_CACHE_TTL = 5.0

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
            "paper_account": {"type": 'bool', "required": True, "description": "Is this a paper trading account?"},
            "data_feed": {
                "type": "str",
                "required": False,
                "default": "delayed_sip",
                "description": "Alpaca market data feed",
                "valid_values": ["delayed_sip", "sip", "iex", "otc"],
                "tooltip": "delayed_sip = 15-min delayed (free tier). sip = real-time consolidated (paid). iex = real-time IEX (paid). otc = OTC data.",
            },
            "options_feed": {"type": "str", "required": False, "default": "indicative",
                             "valid_values": ["indicative", "opra"],
                             "description": "Options market data feed (indicative = free fallback; opra requires subscription)"},
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
            logger.warning(f"_round_price_for_alpaca received None price" + (f" for {symbol}" if symbol else ""))
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

    @staticmethod
    def _sanitize_enum_field(value, enum_class, field_name, nullable=True, default_value=None):
        """
        Sanitize enum values from Alpaca API with proper logging and error handling.

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

    def _update_existing_oco_legs(self, parent_order: TradingOrder) -> int:
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
                    #logger.debug(f"OCO leg {leg_broker_id} already exists in database as order {existing_leg.id}, skipping insertion")
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
    
    @staticmethod
    def _map_order_type(alpaca_type, side: OrderDirection) -> "CoreOrderType":
        """Map an Alpaca order type to our directional OrderType.

        Alpaca's ``type`` is non-directional (market/limit/stop/stop_limit/
        trailing_stop); our OrderType is directional for limit/stop variants, so
        we combine it with the order side. Falls back to MARKET for unknown types.
        """
        if alpaca_type is None:
            return CoreOrderType.MARKET
        t = str(getattr(alpaca_type, "value", alpaca_type)).lower()
        is_buy = side == OrderDirection.BUY
        if t == "market":
            return CoreOrderType.MARKET
        if t == "limit":
            return CoreOrderType.BUY_LIMIT if is_buy else CoreOrderType.SELL_LIMIT
        if t == "stop":
            return CoreOrderType.BUY_STOP if is_buy else CoreOrderType.SELL_STOP
        if t == "stop_limit":
            return CoreOrderType.BUY_STOP_LIMIT if is_buy else CoreOrderType.SELL_STOP_LIMIT
        if t == "trailing_stop":
            return CoreOrderType.TRAILING_STOP
        return CoreOrderType.MARKET

    def alpaca_order_to_tradingorder(self, order):
        """
        Convert an Alpaca order object to a TradingOrder object.
        """
        # Classify order_class up front: OCO and MLEG (multi-leg option) orders
        # both legitimately have NO top-level side in Alpaca's response (the side
        # lives on each leg), so side must be nullable for them.
        order_class_val = getattr(order, "order_class", None)
        order_class_str = str(order_class_val).lower() if order_class_val else ""
        is_oco = "oco" in order_class_str
        is_mleg = "mleg" in order_class_str

        # Sanitize enum fields
        side = self._sanitize_enum_field(
            getattr(order, "side", None),
            OrderDirection,
            "side",
            nullable=is_mleg
        )

        # Alpaca's order type is non-directional (market/limit/stop/stop_limit);
        # combine it with side to get our directional OrderType. (A plain
        # _sanitize_enum_field would collapse limit/stop/stop_limit to MARKET
        # because our enum values are directional.)
        order_type = self._map_order_type(getattr(order, "type", None), side)

        status = self._sanitize_enum_field(
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

        if is_oco:
            final_order_type = CoreOrderType.OCO

        # OCO and MLEG orders both carry their child legs in the `legs` array;
        # capture the broker leg ids for upstream leg tracking.
        if (is_oco or is_mleg) and getattr(order, "legs", None):
            legs_broker_ids = [str(leg.id) for leg in order.legs
                               if getattr(leg, "id", None)]

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
            comment=None,
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
    def _fetch_raw_alpaca_orders(self, status: Optional[OrderStatus] = OrderStatus.ALL, fetch_all: bool = False) -> list:
        """
        Fetch raw Alpaca order objects from the API.

        Returns raw Alpaca SDK order objects (not converted to TradingOrder).
        Used internally by get_orders() and refresh_orders().

        Args:
            status: Filter by order status. Defaults to ALL.
            fetch_all: If True, fetches ALL orders using date-based pagination.
                       If False, returns first 500 orders.

        Returns:
            list: Raw Alpaca order objects. Empty list if authentication fails or error occurs.
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

            return alpaca_orders

        except Exception as e:
            logger.error(f"Error fetching Alpaca orders: {e}", exc_info=True)
            return []

    def get_orders(self, status: Optional[OrderStatus] = OrderStatus.ALL, fetch_all: bool = False):
        """
        Retrieve a list of orders based on the provided filter.

        Args:
            status: Filter by order status. Defaults to ALL.
            fetch_all: If True, fetches ALL orders using date-based pagination. If False, returns first 500 orders.

        Returns:
            list: A list of TradingOrder objects representing the orders.
            Returns empty list if an error occurs.
        """
        raw_orders = self._fetch_raw_alpaca_orders(status=status, fetch_all=fetch_all)
        orders = [self.alpaca_order_to_tradingorder(order) for order in raw_orders]
        logger.debug(f"Converted {len(raw_orders)} raw orders to {len(orders)} TradingOrder objects")
        return orders

    def _classify_order_error(self, exc: Exception) -> "BrokerOrderErrorReason":
        """Map an Alpaca ``APIError`` onto the shared broker-agnostic taxonomy.

        Alpaca's numeric ``code`` is NOT enough on its own — 42210000 (422 Unprocessable) has
        been observed for both a breached stop price ("stop price must be less than current
        price") AND an unrelated invalid-symbol rejection ("invalid underlying symbols"), and
        40310000 (403) covers both "insufficient buying power" and a wash-trade rejection — so
        classification also inspects ``message``. Non-APIError exceptions (network errors,
        etc.) fall through to UNKNOWN.
        """
        from ...core.types import BrokerOrderErrorReason

        if not isinstance(exc, APIError):
            return BrokerOrderErrorReason.UNKNOWN
        try:
            code = exc.code
            message = (exc.message or "").lower()
        except Exception:  # noqa: BLE001 — malformed error body, treat as unknown
            return BrokerOrderErrorReason.UNKNOWN

        if code == 42210000 and "stop price" in message and "current price" in message:
            return BrokerOrderErrorReason.STOP_THROUGH_MARKET
        if code == 42210000 and "invalid underlying symbols" in message:
            return BrokerOrderErrorReason.INVALID_SYMBOL
        if code == 40310000 and "insufficient buying power" in message:
            return BrokerOrderErrorReason.INSUFFICIENT_FUNDS
        if code == 40310000 and "wash trade" in message:
            return BrokerOrderErrorReason.WASH_TRADE
        if code == 40310000 and ("insufficient qty" in message or "insufficient quantity" in message):
            return BrokerOrderErrorReason.INSUFFICIENT_QTY
        return BrokerOrderErrorReason.UNKNOWN

    def _record_fractional_adjustment(self, trading_order: TradingOrder,
                                      quantity: Optional[float], reason: str) -> None:
        """Persist a submission-time fractional-quantity adjustment onto the order row.

        ``quantity`` is the whole-share quantity actually being sent, or ``None`` when
        nothing is being sent at all (the SKIP case: flooring left 0 shares). A skip is
        marked CANCELED — terminal, but deliberately NOT ERROR: no broker rejected
        anything and the account is healthy, there was simply no whole share left to
        trade, so it must not show up as a broker failure in the UI or the logs.

        The reason is appended to ``comment`` (same convention as
        ``_handle_order_submit_error``) so it is legible in the Pending Orders UI and not
        only in the log.
        """
        fresh_order = get_instance(TradingOrder, trading_order.id)
        if not fresh_order:
            logger.error(
                f"Could not find order {trading_order.id} to record fractional adjustment: {reason}")
            return
        if quantity is None:
            fresh_order.status = OrderStatus.CANCELED
        else:
            fresh_order.quantity = quantity
        fresh_order.comment = (
            f"{fresh_order.comment} | {reason}" if fresh_order.comment else reason)[:500]
        update_instance(fresh_order)

    @alpaca_api_retry
    def _submit_order_impl(self, trading_order: TradingOrder, tp_price: Optional[float] = None, sl_price: Optional[float] = None, is_closing_order: bool = False, use_complex_order: bool = False) -> TradingOrder:
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

        # Idempotency guard: an order that already carries a broker_order_id was already
        # sent to the broker. Never re-submit it — this protects against the race where a
        # concurrent refresh picks the same dependent order before its status is committed.
        if trading_order.broker_order_id:
            logger.warning(
                f"Order {trading_order.id} already has broker_order_id "
                f"{trading_order.broker_order_id} — skipping re-submission"
            )
            return trading_order

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
            
            # CRITICAL CHECK: For entry orders of new transactions, verify no opposite-direction positions exist
            # This prevents accidentally closing existing positions when trying to open new ones
            # Skip this check for close orders (they intentionally sell/buy against existing positions)
            if trading_order.transaction_id and not trading_order.depends_on_order and not is_closing_order:
                from ...core.models import Transaction
                transaction = get_instance(Transaction, trading_order.transaction_id)
                
                # Only check for entry orders (first order of new transaction)
                if transaction and transaction.status == TransactionStatus.WAITING:
                    try:
                        positions = self.client.get_all_positions()
                        broker_position = next((pos for pos in positions if pos.symbol == trading_order.symbol), None)
                        
                        if broker_position:
                            position_qty = float(broker_position.qty)
                            order_side = trading_order.side
                            
                            # Check for conflicting position direction
                            # LONG position (qty > 0) conflicts with BUY entry order (would add to position instead of opening new)
                            # SHORT position (qty < 0) conflicts with SELL entry order (would add to short instead of opening new)
                            if position_qty > 0 and order_side == OrderDirection.SELL:
                                logger.error(f"Cannot open SHORT position for {trading_order.symbol}: LONG position of {position_qty} shares already exists at broker")
                                raise ValueError(f"Cannot open SHORT position for {trading_order.symbol}: Existing LONG position of {position_qty} shares would be closed instead. Close all existing positions first or enable hedging.")
                            elif position_qty < 0 and order_side == OrderDirection.BUY:
                                logger.error(f"Cannot open LONG position for {trading_order.symbol}: SHORT position of {position_qty} shares already exists at broker")
                                raise ValueError(f"Cannot open LONG position for {trading_order.symbol}: Existing SHORT position of {position_qty} shares would be closed instead. Close all existing positions first or enable hedging.")
                            
                            logger.debug(f"Entry order check passed: {order_side.value} order compatible with existing position qty={position_qty}")
                    except Exception as e:
                        if "Cannot open" in str(e) or "already exists" in str(e):
                            raise  # Re-raise position conflict errors
                        logger.warning(f"Failed to check broker positions for entry order {trading_order.id}: {e}")
                        # Continue anyway - let broker handle validation
            
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

            # ---- Fractional quantities -----------------------------------------------
            # Alpaca accepts a fractional quantity ONLY on a DAY MARKET order. Two traps:
            #
            #  1. tif_map above resolves an unknown/absent good_for to GTC, and the
            #     allocation actions build their orders with no good_for at all — so a
            #     fractional order would go out GTC and be refused by the broker. Force
            #     DAY rather than trusting every future caller to remember.
            #  2. Every non-MARKET type (limit / stop / stop-limit / OCO) refuses
            #     fractional outright — including a protective TP/SL leg whose quantity
            #     was inherited from a fractional position. Those are pre-floored to
            #     floor(qty) whole shares and submitted ONCE (this is NOT a retry —
            #     nothing fractional is ever put on the wire): it is a guaranteed
            #     rejection otherwise, and flooring under-fills rather than overspending
            #     the target. The floored quantity is written back so the ledger matches
            #     what the broker got.
            #     `use_complex_order` (the wash-trade escape) belongs on this branch even
            #     for a MARKET order: it re-classes the request as BRACKET/OTO, and
            #     Alpaca refuses a fractional quantity on those too.
            #
            # A floor of 0 leaves nothing to send. That is a SKIP, not a failure —
            # nothing was rejected and nothing is wrong with the account — so the row is
            # marked CANCELED with the reason, never ERROR.
            #
            # Sizing itself is NOT re-derived here: the allocation engine already decided
            # fractional-vs-whole (opt-in per run, gated on the broker's own per-symbol
            # `fractionable` flag) and did the rounding. This is submission-time
            # enforcement of a broker constraint only.
            quantity_value = float(trading_order.quantity or 0.0)
            if quantity_value != int(quantity_value):
                is_plain_market = (order_type_value == CoreOrderType.MARKET.value.lower()
                                   and not use_complex_order)
                if is_plain_market:
                    if time_in_force != TimeInForce.DAY:
                        logger.info(
                            f"Order {trading_order.id} ({trading_order.symbol}) has fractional "
                            f"qty={quantity_value}; forcing time_in_force DAY (was "
                            f"{time_in_force.value}) — Alpaca rejects fractional on any other TIF"
                        )
                        time_in_force = TimeInForce.DAY
                else:
                    whole_shares = float(math.floor(quantity_value))
                    as_complex = (" order submitted as a complex (BRACKET/OTO) order"
                                  if use_complex_order else " order")
                    reason = (f"fractional qty {quantity_value} is not accepted by Alpaca on a "
                              f"{order_type_value}{as_complex}")
                    if whole_shares <= 0:
                        logger.warning(
                            f"Order {trading_order.id} ({trading_order.symbol}) skipped: "
                            f"{reason}, and flooring leaves 0 whole shares — nothing submitted"
                        )
                        self._record_fractional_adjustment(
                            trading_order, None,
                            f"skipped: {reason}; flooring leaves 0 whole shares")
                        return None
                    # Name the CONSEQUENCE, not just the arithmetic: a protective leg
                    # floored off a fractional parent covers less than the position, and
                    # this line is the only place that ever gets said.
                    logger.warning(
                        f"Order {trading_order.id} ({trading_order.symbol}): {reason}; "
                        f"submitting {whole_shares} whole shares instead of {quantity_value}; "
                        f"{quantity_value - whole_shares:g} shares of the position are "
                        f"left uncovered"
                    )
                    trading_order.quantity = whole_shares
                    self._record_fractional_adjustment(
                        trading_order, whole_shares,
                        f"{reason}; floored to {whole_shares} whole shares")

            # Note: We do NOT create bracket orders. TP/SL will be handled separately
            # as STOP_LIMIT orders after the entry order fills.
            # The parent AccountInterface.submit_order() will create pending_trigger orders.
            
            # Create the appropriate order request based on order type
            if order_type_value == CoreOrderType.MARKET.value.lower():
                # Validate order has ID before submission
                if not trading_order.id:
                    raise ValueError(f"Order must be saved to database before broker submission (missing ID)")
                
                order_request = MarketOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    client_order_id=str(trading_order.id)
                )
            elif order_type_value in [CoreOrderType.BUY_LIMIT.value.lower(), 
                                      CoreOrderType.SELL_LIMIT.value.lower()]:
                if not trading_order.limit_price:
                    raise ValueError("Limit price is required for limit orders")
                
                # Validate order has ID before submission
                if not trading_order.id:
                    raise ValueError(f"Order must be saved to database before broker submission (missing ID)")
                
                # Round limit price using Alpaca pricing rules
                rounded_limit_price = self._round_price(trading_order.limit_price, trading_order.symbol)
                    
                order_request = LimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    limit_price=rounded_limit_price,
                    client_order_id=str(trading_order.id)
                )
            elif order_type_value in [CoreOrderType.BUY_STOP.value.lower(), 
                                      CoreOrderType.SELL_STOP.value.lower()]:
                if not trading_order.stop_price:
                    raise ValueError("Stop price is required for stop orders")
                
                # Validate order has ID before submission
                if not trading_order.id:
                    raise ValueError(f"Order must be saved to database before broker submission (missing ID)")
                
                # Round stop price using Alpaca pricing rules
                rounded_stop_price = self._round_price(trading_order.stop_price, trading_order.symbol)
                    
                order_request = StopOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    stop_price=rounded_stop_price,
                    client_order_id=str(trading_order.id)
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
                    client_order_id=str(trading_order.id)
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
                
                # Validate order has ID before submission
                if not trading_order.id:
                    raise ValueError(f"Order must be saved to database before broker submission (missing ID)")
                
                # OCO order: MarketOrderRequest (no limit_price) with take_profit and stop_loss legs
                order_request = LimitOrderRequest(
                    symbol=trading_order.symbol,
                    qty=trading_order.quantity,
                    side=side,
                    time_in_force=time_in_force,
                    order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=rounded_tp_price),
                    stop_loss=StopLossRequest(stop_price=rounded_sl_stop_price, limit_price=rounded_sl_limit_price),
                    client_order_id=str(trading_order.id)
                )
                logger.info(f"Submitting OCO order: TP=${rounded_tp_price:.4f}, SL stop=${rounded_sl_stop_price:.4f} limit=${rounded_sl_limit_price:.4f}")
                
            else:
                raise ValueError(f"Unsupported order type: {trading_order.order_type} (value: {order_type_value})")

            # Wash-trade escape: attach the protective legs to THIS order so it goes to Alpaca as
            # a complex order. Alpaca exempts BRACKET/OTO/OCO from the wash-trade check that
            # rejects plain orders with 40310000 when an opposite-side market/stop order is
            # working (verified against the paper API 2026-08-05 — see docs/WASHTRADE-LOCK.md).
            # submit_order() sets this flag only on the blocked branch, and guarantees at least
            # one of tp_price/sl_price is present. The order type is unchanged; only order_class
            # gains legs, so an unblocked order is untouched by this.
            if use_complex_order:
                if trading_order.order_type == CoreOrderType.OCO:
                    # Already a complex order — it carries its own legs and is exempt as-is.
                    logger.debug(f"Order {trading_order.id} is already OCO; no extra legs needed")
                else:
                    legs = {}
                    if tp_price:
                        legs['take_profit'] = TakeProfitRequest(
                            limit_price=self._round_price(tp_price, trading_order.symbol))
                    if sl_price:
                        rounded_sl = self._round_price(sl_price, trading_order.symbol)
                        legs['stop_loss'] = StopLossRequest(stop_price=rounded_sl)
                    if not legs:
                        raise ValueError(
                            f"use_complex_order set for order {trading_order.id} but neither "
                            f"tp_price nor sl_price was provided"
                        )
                    order_request.order_class = (
                        OrderClass.BRACKET if len(legs) == 2 else OrderClass.OTO)
                    for leg_name, leg in legs.items():
                        setattr(order_request, leg_name, leg)
                    logger.info(
                        f"Order {trading_order.id} ({trading_order.symbol}) submitted as "
                        f"{order_request.order_class.value.upper()} to bypass the wash-trade block "
                        f"(tp={tp_price}, sl={sl_price})"
                    )

            logger.debug(f"Submitting Alpaca order: {order_request} (client_order_id={trading_order.id})")
            alpaca_order = self.client.submit_order(order_request)
            logger.info(f"Successfully submitted order to Alpaca: broker_order_id={alpaca_order.id}")

            # Invalidate balance cache — a submitted order immediately changes buying power
            self.invalidate_balance_cache()

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
                submitted_class = getattr(alpaca_order, 'order_class', None)
                if fresh_order.order_type == CoreOrderType.OCO and submitted_class == OrderClass.OCO:
                    logger.info(f"Order {fresh_order.id} is OCO, inserting legs...")
                    self._insert_oco_order_legs(alpaca_order, fresh_order, trading_order.transaction_id)
                elif submitted_class in (OrderClass.BRACKET, OrderClass.OTO):
                    # Wash-trade escape path: this entry went out as a complex order, so ALPACA
                    # created the protective legs, not us. Adopt them into our own TradingOrder
                    # rows now — submit_order() skipped its adjust_tp/adjust_sl block precisely
                    # so we would not place a duplicate set. The leg reader is generic over the
                    # legs array (it classifies TP vs SL by which prices are present), so the
                    # same helper works for BRACKET/OTO as for OCO.
                    logger.info(
                        f"Order {fresh_order.id} submitted as {submitted_class.value.upper()}, "
                        f"adopting broker-created protective legs..."
                    )
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
                                self.adjust_tp_sl(transaction, tp_price, sl_price, source="initial_setup")
                            elif tp_price:
                                logger.debug(f"Creating TP order for transaction {transaction.id} via adjust_tp")
                                self.adjust_tp(transaction, tp_price, source="initial_setup")
                            elif sl_price:
                                logger.debug(f"Creating SL order for transaction {transaction.id} via adjust_sl")
                                self.adjust_sl(transaction, sl_price, source="initial_setup")
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
            
            # Log activity with actual error details
            try:
                from ...core.db import log_activity
                from ...core.types import ActivityLogSeverity, ActivityLogType
                
                error_message = str(e)
                activity_data = {
                    "order_id": trading_order.id,
                    "symbol": trading_order.symbol,
                    "side": trading_order.side.value if hasattr(trading_order.side, 'value') else str(trading_order.side),
                    "quantity": trading_order.quantity,
                    "order_type": trading_order.order_type.value if hasattr(trading_order.order_type, 'value') else str(trading_order.order_type),
                    "error": error_message
                }
                
                if trading_order.transaction_id:
                    activity_data["transaction_id"] = trading_order.transaction_id
                
                # Get expert_id from transaction if available
                expert_id = None
                if trading_order.transaction_id:
                    from ...core.models import Transaction
                    transaction = get_instance(Transaction, trading_order.transaction_id)
                    if transaction:
                        expert_id = transaction.expert_id
                
                log_activity(
                    severity=ActivityLogSeverity.FAILURE,
                    activity_type=ActivityLogType.ORDER_SUBMITTED,
                    description=f"Failed to submit {trading_order.side.value if hasattr(trading_order.side, 'value') else str(trading_order.side)} order for {trading_order.symbol}: {error_message[:100]}",
                    data=activity_data,
                    source_account_id=self.id,
                    source_expert_id=expert_id
                )
            except Exception as log_error:
                logger.warning(f"Failed to log order submission error to activity log: {log_error}")
            
            # Step 4: broker-agnostic error handling (classify + retry-as-market on a
            # breached stop + mark ERROR with the reason recorded in comment). Centralized
            # in AccountInterface so the stop-breach retry isn't Alpaca-specific.
            if trading_order.id:
                return self._handle_order_submit_error(trading_order, e)
            logger.warning("Cannot mark order as ERROR - order has no ID")
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
            
            # Validate order has ID
            if not trading_order.id:
                raise ValueError(f"Order must be saved to database before modification (missing ID)")
            
            # Create ReplaceOrderRequest using order ID (already unique, no need to regenerate)
            replace_request = ReplaceOrderRequest(
                qty=trading_order.quantity,
                time_in_force=time_in_force,
                limit_price=limit_price,
                stop_price=stop_price,
                client_order_id=str(trading_order.id)
            )
            
            # Alpaca uses replace_order_by_id() method
            order = self.client.replace_order_by_id(
                order_id=order_id,
                order_data=replace_request
            )
            
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
            
            # Mark the order PENDING_CANCEL — NOT optimistically CANCELED. The cancel
            # has only been *requested*; the account refresh promotes it to CANCELED
            # once the broker confirms (and the qty is actually released). This keeps a
            # dependent replacement (e.g. a trailing-stop OCO swap) waiting until the
            # cancellation is real, instead of firing early and getting rejected.
            if db_order_id:
                db_order = get_instance(TradingOrder, db_order_id)
                if db_order:
                    db_order.status = OrderStatus.PENDING_CANCEL
                    update_instance(db_order)

            return True
        except APIError as e:
            if "pending cancel" in str(e).lower():
                logger.debug(f"Order {order_id} already pending cancel at Alpaca — treating as success")
                return True
            logger.error(f"Error cancelling Alpaca order {order_id}: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Error cancelling Alpaca order {order_id}: {e}", exc_info=True)
            return False

    @alpaca_api_retry
    def get_positions(self):
        """
        Retrieve all current positions in the Alpaca account.

        Returns:
            list: A list of position objects if the fetch succeeded (empty list is a REAL flat
            account). None if the fetch itself failed (network/DNS/auth/API error) — distinct
            from a real empty list so callers that gate auto-close logic on "we actually
            confirmed the broker's book" (e.g. ReadOnlyAccountInterface.
            reconcile_externally_closed_transactions, overview._compare_positions_with_broker)
            can skip rather than mistake a fetch failure for a fully-flat account and mass-close
            real open positions. See the 2026-07-03 incident: a transient local DNS outage
            (`getaddrinfo failed` resolving api.alpaca.markets) made this swallow to `[]`, which
            reconcile read as "broker holds nothing" and closed 8 real open transactions in the
            DB while they remained open at the broker the whole time.
        """
        try:
            positions = self.client.get_all_positions()
            logger.debug(f"Listed {len(positions)} Alpaca positions.")
            return [self.alpaca_position_to_position(position) for position in positions]
        except Exception as e:
            logger.error(f"Error listing Alpaca positions: {e}", exc_info=True)
            return None

    def get_available_position_quantity(self, symbol: str) -> float:
        """Broker-side AVAILABLE (not held-for-orders) quantity for ``symbol``.

        Used to confirm a prior order has actually released its qty before a
        cancel-and-replace order is submitted, so the replacement isn't rejected
        with 40310000 "insufficient qty available" and hard-ERRORed — which leaves
        the position with no protective stop.

        This is a FAST override of the interface default: ``get_open_position``
        is ONE targeted call, where the base has to pull the whole book via
        ``get_positions()``. It honours the same contract (see
        ``ReadOnlyAccountInterface.get_available_position_quantity``) and in
        particular NEVER RETURNS ``None``: ``None`` is the caller's "unknown → do
        NOT block" value, so every case where we cannot get a real answer —
        including the 404 Alpaca raises when the account holds nothing in the
        symbol, and any network failure — is reported as ``0.0``. That DEFERS the
        replacement (it stays WAITING_TRIGGER and the next refresh retries)
        instead of submitting it blind into a certain rejection.

        Returns the absolute quantity (a short reports negative; the buy-to-cover
        replacement needs the magnitude).
        """
        try:
            position = self.client.get_open_position(symbol)
        except Exception as e:
            # Includes the 404 "position does not exist" — the broker holds
            # nothing, so nothing is available — and any transport failure.
            logger.debug(
                f"[Account {self.id}] get_available_position_quantity({symbol}): "
                f"get_open_position failed ({e}); reporting 0 available "
                f"(defer, retry next refresh)"
            )
            return 0.0
        # qty_available is the number Alpaca actually enforces; qty is the fallback
        # for the (unexpected) case where it is absent — the broker has confirmed
        # it HOLDS the shares and only the encumbrance is unknown.
        for field in ("qty_available", "qty"):
            raw = getattr(position, field, None)
            if raw is None:
                continue
            try:
                return abs(float(raw))
            except (TypeError, ValueError):
                logger.warning(
                    f"[Account {self.id}] position {symbol}.{field}={raw!r} is not numeric"
                )
        logger.warning(
            f"[Account {self.id}] position {symbol} publishes no usable quantity; "
            f"reporting 0 available (defer, retry next refresh)"
        )
        return 0.0

    def invalidate_balance_cache(self) -> None:
        """Invalidate the balance cache so the next call fetches a fresh value."""
        with self._balance_cache_lock:
            self._balance_cache_time = 0.0
        # The snapshot carries the SAME buying_power / cash / equity off the SAME
        # get_account() call, so anything that stales the balance stales it too --
        # otherwise the next TradeActions.increase_instrument_share would size
        # against pre-trade buying power for up to a full TTL.
        self.clear_account_snapshot_cache()
        logger.debug("Balance cache invalidated")

    def get_balance(self) -> Optional[float]:
        """
        Get the current account balance/equity from Alpaca.

        Caches the result for 5 seconds. On fetch failure, waits 10 seconds and
        retries once. If the retry also fails, returns the last cached value (if
        any) and logs a warning.

        Returns:
            Optional[float]: The current account equity if available, None if error occurred
        """
        # Serve from cache if still fresh
        with self._balance_cache_lock:
            if self._balance_cache is not None and (time.time() - self._balance_cache_time) < self._BALANCE_CACHE_TTL:
                logger.debug(f"Alpaca account balance (cached): ${self._balance_cache}")
                return self._balance_cache

        def _fetch() -> Optional[float]:
            account = self.client.get_account()
            if account and hasattr(account, 'equity'):
                return float(account.equity)
            logger.warning("No equity field found in Alpaca account info")
            return None

        try:
            balance = _fetch()
            if balance is not None:
                with self._balance_cache_lock:
                    self._balance_cache = balance
                    self._balance_cache_time = time.time()
                logger.debug(f"Alpaca account balance: ${balance}")
                return balance
        except Exception as e:
            logger.warning(f"get_balance first attempt failed: {e}. Waiting 10s before retry.")

        # First attempt failed — wait 10 s then retry once
        time.sleep(10.0)
        try:
            balance = _fetch()
            if balance is not None:
                with self._balance_cache_lock:
                    self._balance_cache = balance
                    self._balance_cache_time = time.time()
                logger.info(f"Alpaca account balance (after retry): ${balance}")
                return balance
        except Exception as e:
            logger.warning(f"get_balance retry also failed: {e}. Returning cached value if available.", exc_info=True)

        # Both attempts failed — return stale cache if we have one
        with self._balance_cache_lock:
            cached = self._balance_cache
        if cached is not None:
            logger.warning(f"Returning stale cached balance ${cached} after fetch failures")
        return cached

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

    def get_account_snapshot(self) -> AccountSnapshot:
        """Broker-agnostic cash / equity / buying-power view of this Alpaca account.

        Overrides the tolerant base probe because Alpaca needs a SECOND endpoint
        (get_account_configurations) for the fractional-trading capability, which
        the base must not call. Every money field on the pydantic TradeAccount is
        Optional[str] -- including multiplier ("1"/"2"/"4") -- so everything goes
        through float().

        Returns an ALL-None AccountSnapshot (never None) when get_account_info()
        returns None on auth failure: the type stays stable and the caller must
        refuse to plan rather than substitute zeros.

        CACHED for _ACCOUNT_SNAPSHOT_CACHE_TTL (5 s). Those two endpoints are the
        cost of reading equity, and _validate_position_size_limits reads equity on
        every market-order validation, so a basket used to pay two REST round trips
        per order. The window is deliberately tiny because this is money -- see
        _ACCOUNT_SNAPSHOT_CACHE_TTL -- and it is dropped outright by
        invalidate_balance_cache() after every submission, so the one event that
        moves these numbers discontinuously never waits it out.

        A FAILED read is NOT cached: caching the all-None snapshot would keep a
        recovered account looking dead for the rest of the window.
        """
        cached_entry = getattr(self, '_account_snapshot_cache', None)
        if cached_entry is not None:
            fetched_at, cached = cached_entry
            if (time.time() - fetched_at) < self._ACCOUNT_SNAPSHOT_CACHE_TTL:
                logger.debug(f"[Account {self.id}] Account snapshot served from cache")
                return cached
            self._account_snapshot_cache = None

        info = self.get_account_info()
        if info is None:
            logger.error(f"[Account {self.id}] get_account_info() returned None -- empty snapshot")
            return AccountSnapshot()

        def _f(name: str) -> Optional[float]:
            val = getattr(info, name, None)
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                logger.warning(f"[Account {self.id}] TradeAccount.{name}={val!r} is not numeric")
                return None

        multiplier = _f('multiplier')
        equity = _f('equity')

        # TradeAccount carries no fractional flag; AccountConfiguration does.
        # A failure here must not lose the balances we already have.
        #
        # This flag stays BOOLEAN, not tri-state, on purpose. "Couldn't ask" and "known
        # not fractional" are collapsed to False because the only defensible handling of
        # an unknown is the same as False -- and the failure is one-directional: a
        # degraded read can only SUPPRESS fractional sizing (a smaller, always-legal
        # whole-share order), never wrongly enable it. Fractional also requires the
        # per-symbol `fractionable` flag from get_symbol_margin_info(), an independent
        # read from a different endpoint, so this is not a single point of truth. What
        # a tri-state would really have bought is visibility, and that is what the
        # WARNING below gives -- a silent debug line was the actual defect.
        supports_fractional = False
        try:
            supports_fractional = bool(getattr(self.client.get_account_configurations(),
                                               'fractional_trading', False))
        except Exception as e:
            logger.warning(
                f"[Account {self.id}] Could not read account configurations ({e}) — "
                f"reporting supports_fractional=False; this run will size whole shares "
                f"even if the account is fractional-capable"
            )

        snapshot = AccountSnapshot(
            cash=_f('cash'),
            equity=equity,
            net_liquidation=equity,
            buying_power=_f('buying_power'),
            non_marginable_buying_power=_f('non_marginable_buying_power'),
            margin_multiplier=multiplier,
            is_margin_account=bool(multiplier is not None and multiplier > 1.0),
            long_market_value=_f('long_market_value'),
            short_market_value=_f('short_market_value'),
            pending_transfer_in=_f('pending_transfer_in'),
            supports_fractional=supports_fractional,
            raw={'account_number': getattr(info, 'account_number', None),
                 'status': str(getattr(info, 'status', None))},
        )
        self._account_snapshot_cache = (time.time(), snapshot)
        return snapshot

    def clear_account_snapshot_cache(self) -> None:
        """Drop the cached account snapshot so the next call refetches.

        The companion to _ACCOUNT_SNAPSHOT_CACHE_TTL, mirroring
        clear_margin_info_cache(). The TTL alone is not enough: the account object
        lives for the whole PROCESS, and the moment these numbers change
        discontinuously is a submission of our own, which must not be allowed to
        read pre-trade buying power for the rest of the window. Called by
        invalidate_balance_cache() (i.e. after every submit) and available for an
        explicit user Refresh.
        """
        self._account_snapshot_cache = None
        logger.debug(f"[Account {self.id}] Cleared cached account snapshot")

    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        """Per-symbol margin / fractionability metadata for buying-power sizing.

        Alpaca has NO bulk asset endpoint -- TradingClient.get_asset() takes one
        symbol -- so this is one HTTP call per symbol NOT already cached. Callers
        should therefore pass the whole basket once and reuse the result.

        Alpaca's Asset exposes no INITIAL margin field, only
        maintenance_margin_requirement (a percentage, e.g. 30.0), so the initial
        rate is DERIVED from three facts:

          * marginable -> 0.5 (Reg-T), otherwise 1.0;
          * but only where the ACCOUNT can borrow: at multiplier 1 (cash /
            limited-margin, or a margin account under $2,000 equity)
            buying_power == cash, so the rate is 1.0 for every symbol;
          * floored by maintenance_margin_requirement / 100 -- an initial
            requirement below the maintenance requirement is not a thing.

        The maintenance number is also reported separately as
        maintenance_margin_rate. Then bp_factor = initial_rate * account
        multiplier -- 1.0 for an ordinary marginable symbol and 2.0 for a
        non-marginable one in a 2:1 account, and 1.0 for everything at 1x.

        The multiplier is read from get_account_info() (ONE get_account() call),
        not from get_account_snapshot(), which would add a get_account_configurations()
        round-trip for a fractional flag this method does not use. It is read
        fresh on every call and re-applied to cached entries, because Alpaca
        moves an account between 1/2/4 and this process is long-lived; only the
        Asset facts, which do not change intraday, are actually cached.

        A symbol the broker cannot describe is OMITTED, never defaulted here --
        the caller falls back to the conservative bp_factor = account multiplier.
        One symbol's failure never aborts the batch.
        """
        if not self._check_authentication():
            return {}

        info = self.get_account_info()
        multiplier = self._safe_float(getattr(info, 'multiplier', None)) if info is not None else None
        if multiplier is None:
            logger.warning(f"[Account {self.id}] No account multiplier -- cannot size bp_factor")
            return {}

        cache = self._margin_info_cache
        now = time.time()
        out: Dict[str, MarginInfo] = {}
        for raw_symbol in symbols:
            symbol = (raw_symbol or '').strip().upper()
            if not symbol:
                continue

            entry = cache.get(symbol)
            if entry is not None and (now - entry[0]) >= self._MARGIN_INFO_CACHE_TTL:
                logger.debug(f"[Account {self.id}] Margin info for {symbol} expired; refetching")
                entry = None
            if entry is not None:
                fetched_at, cached = entry
                # Only the Asset facts are cached; bp_factor is re-derived from the
                # multiplier read on THIS call. The None guard keeps an entry from a
                # future non-asset source (a precheck, whose bp_factor the broker
                # measured and which carries no initial rate to multiply) out of the
                # REPRICING -- the entry is still returned as-is.
                rate = cached.initial_margin_rate
                if rate is not None and cached.bp_factor != rate * multiplier:
                    cached = replace(cached, bp_factor=rate * multiplier)
                    cache[symbol] = (fetched_at, cached)
                out[symbol] = cached
                continue

            try:
                asset = self.client.get_asset(symbol)
            except Exception as e:
                logger.warning(f"[Account {self.id}] get_asset({symbol}) failed: {e}")
                continue
            if asset is None:
                logger.warning(f"[Account {self.id}] get_asset({symbol}) returned nothing")
                continue

            marginable = bool(getattr(asset, 'marginable', False))
            maint = self._safe_float(getattr(asset, 'maintenance_margin_requirement', None))
            # `Asset.marginable` describes the SECURITY, not the ACCOUNT. Alpaca reports
            # multiplier="1" for cash and limited-margin accounts (and drops a margin
            # account to 1 while its equity is under $2,000); there buying_power == cash
            # and nothing is lent, so Reg-T's 50% does not apply to ANY symbol and the
            # effective initial requirement is 100%. Without the multiplier gate the
            # asset-sourced answer was HALF the conservative fallback that is used when
            # the lookup fails -- more information made the plan bigger, which is the
            # wrong direction for a feasibility guard.
            initial_rate = 0.5 if (marginable and multiplier > 1.0) else 1.0
            # An initial requirement below the MAINTENANCE requirement is not a thing:
            # Alpaca publishes 30/50/75/100 per name and still flags hard-to-margin
            # names marginable, so the derived Reg-T rate is floored by it.
            if maint is not None:
                initial_rate = max(initial_rate, maint / 100.0)
            margin_info = MarginInfo(
                symbol=symbol,
                bp_factor=initial_rate * multiplier,
                marginable=marginable,
                fractionable=bool(getattr(asset, 'fractionable', False)),
                min_order_size=self._safe_float(getattr(asset, 'min_order_size', None)),
                min_trade_increment=self._safe_float(getattr(asset, 'min_trade_increment', None)),
                initial_margin_rate=initial_rate,
                maintenance_margin_rate=(maint / 100.0) if maint is not None else None,
                source=MARGIN_SOURCE_ASSET,
            )
            cache[symbol] = (now, margin_info)
            out[symbol] = margin_info

        return out

    def clear_margin_info_cache(self) -> None:
        """Drop every cached per-symbol margin fact so the next call refetches.

        The cache expires on its own after _MARGIN_INFO_CACHE_TTL; this is the
        EXPLICIT path, for a user who hits Refresh because they know the broker
        changed something (Alpaca revokes marginability / fractionability on
        individual names, and this account object lives for the whole process).
        """
        count = len(self._margin_info_cache)
        self._margin_info_cache = {}
        logger.debug(f"[Account {self.id}] Cleared {count} cached symbol margin entries")

    def _safe_float(self, value: Any) -> Optional[float]:
        """Coerce a broker-supplied number to a plain float, None when not numeric.

        Alpaca types these Optional[str] or Optional[float] depending on the
        field (TradeAccount.multiplier is a string, Asset.min_order_size a float),
        and the value dataclasses only carry floats.
        """
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.warning(f"[Account {self.id}] Non-numeric broker value {value!r}")
            return None

    def _fetch_activities(self, act_type: str,
                          params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Raw non-trade activities of one type, always a list, [] on failure.

        TradingClient exposes no get_account_activities, so every caller here
        (get_cash_transfers, get_balance_history) goes through the raw REST get()
        and has to normalise the single-object / None responses the same way.
        One activity type failing never aborts the others.
        """
        try:
            raw = self.client.get(f"/account/activities/{act_type}", params or None)
        except Exception as e:
            # Loud: both callers silently degrade (an empty ledger window, a
            # transfer-blind P/L) rather than surfacing this to the user.
            logger.error(f"[Account {self.id}] Could not fetch {act_type} activities: {e}",
                         exc_info=True)
            return []
        if isinstance(raw, list):
            return raw
        return [raw] if raw else []

    def get_cash_transfers(self, start_date=None, end_date=None) -> List[CashTransfer]:
        """Deposits, withdrawals and dividends over a window, from the activities API.

        Reuses the request idiom of get_balance_history and get_dividends
        (raw REST get() on /account/activities/<TYPE> with after/until), shared
        through _fetch_activities().

        external_id is the (account_id, external_id) idempotency key of
        portfolio_income_event, so it must be stable across re-syncs AND unique
        per event. CSD/CSW activities carry the broker's own id and it is passed
        through verbatim. Dividends carry one too -- Alpaca stamps an id on every
        non-trade activity -- so they are keyed DIV:<activity id>, namespaced so
        a dividend key can never be mistaken for, or collide with, a CSD/CSW id.
        Only when an activity arrives with no id does the synthetic
        DIV:<symbol>:<YYYY-MM-DD> fallback apply; that form cannot separate two
        DIV activities for one payer on one pay date (a special dividend
        alongside the regular one, a correction) and would upsert them into a
        single ledger row.

        event_date is the ACTIVITY date, not the T+1 settled date that
        get_balance_history shifts to. That shift exists to line a transfer up
        with the equity-curve day it moved equity (a P/L attribution concern);
        the ledger instead answers "what money arrived", and the shift target
        depends on which portfolio-history days happen to be in the window, so
        it would make event_date wobble between syncs.

        Signs are only normalised where that cannot destroy information: a CSW
        is forced negative (a WITHDRAWAL is never income, so its sign carries
        nothing), but a CSD keeps the broker's sign, because a NEGATIVE deposit
        is a clawed-back ACH and CashTransfer.is_income's ``amount > 0`` guard
        exists precisely to reject it.

        Returns [] and logs on failure -- this seam does NOT distinguish a broker
        outage from a genuinely empty window.
        """
        if not self._check_authentication():
            return []

        params: Dict[str, Any] = {}
        if start_date:
            params["after"] = start_date.isoformat()
        if end_date:
            params["until"] = end_date.isoformat()

        def _as_date(raw):
            try:
                return datetime.fromisoformat(str(raw)[:10]).date()
            except (ValueError, TypeError):
                return None

        transfers: List[CashTransfer] = []

        for act_type, event_type in (("CSD", CASH_TRANSFER_DEPOSIT),
                                     ("CSW", CASH_TRANSFER_WITHDRAWAL)):
            for act in self._fetch_activities(act_type, params):
                event_date = _as_date(act.get('date') or act.get('transaction_time'))
                if event_date is None:
                    logger.warning(f"[Account {self.id}] Skipping {act_type} activity "
                                   f"with no usable date: {act}")
                    continue
                amount = self._safe_float(act.get('net_amount'))
                if amount is None:
                    logger.warning(f"[Account {self.id}] Skipping {act_type} activity "
                                   f"with no usable amount: {act}")
                    continue
                external_id = str(act.get('id')
                                  or f"{act_type}:{event_date.isoformat()}:{amount}")
                if external_id.startswith(_DIVIDEND_KEY_PREFIX):
                    # Unreachable with real Alpaca ids (<17 digits>::<uuid>), but
                    # external_id is an UPSERT key: a broker id that shadowed a
                    # synthetic dividend key would silently merge two different
                    # events into one ledger row. Namespace it instead of hoping.
                    external_id = f"{act_type}:{external_id}"
                transfers.append(CashTransfer(
                    external_id=external_id,
                    event_date=event_date,
                    event_type=event_type,
                    amount=amount if event_type == CASH_TRANSFER_DEPOSIT else -abs(amount),
                    symbol=None,
                    description=act.get('description'),
                ))

        for div in self.get_dividends(start_date=start_date, end_date=end_date):
            event_date = _as_date(div.get('date'))
            if event_date is None:
                logger.warning(f"[Account {self.id}] Skipping dividend with no usable date: {div}")
                continue
            symbol = div.get('symbol')
            if not symbol:
                # Dropped even when the broker id below could key it: a dividend that
                # names no payer cannot be attributed to an instrument, and the
                # fallback key DIV:None:<date> would be a fabricated identity on an
                # UPSERT column. Loud, never guessed.
                logger.warning(f"[Account {self.id}] Skipping dividend with no payer "
                               f"symbol: {div}")
                continue
            amount = self._safe_float(div.get('amount'))
            if amount is None:
                logger.warning(f"[Account {self.id}] Skipping dividend with no usable "
                               f"amount: {div}")
                continue
            # Prefer the broker's own DIV activity id -- it is the only thing that
            # separates two dividends from one payer on one pay date. The
            # symbol/date form stays as the fallback for an activity that somehow
            # arrives without one.
            div_id = div.get('id')
            external_id = (f"{_DIVIDEND_KEY_PREFIX}{div_id}" if div_id
                           else f"{_DIVIDEND_KEY_PREFIX}{symbol}:{event_date.isoformat()}")
            transfers.append(CashTransfer(
                external_id=external_id,
                event_date=event_date,
                event_type=CASH_TRANSFER_DIVIDEND,
                amount=amount,
                symbol=symbol,
                description=None,
            ))

        logger.debug(f"[Account {self.id}] Retrieved {len(transfers)} cash transfers")
        return transfers

    @alpaca_api_retry
    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        """
        Check if multiple symbols exist and are tradeable on Alpaca.
        
        Uses Alpaca's get_asset API to verify each symbol exists and is tradeable.
        Symbols must be in the exact format Alpaca expects (e.g., BRK.B not BRK/B).
        
        Args:
            symbols (List[str]): List of stock symbols to check
        
        Returns:
            Dict[str, bool]: Dictionary mapping each symbol to True if tradeable, False otherwise
        """
        if not self._check_authentication():
            return {symbol: False for symbol in symbols}
        
        results = {}
        
        for symbol in symbols:
            try:
                # Use exact symbol - don't normalize, as the symbol format matters for trading
                # If caller provides BRK/B but Alpaca needs BRK.B, we should return False
                symbol_upper = symbol.upper()
                
                # Fetch asset info from Alpaca
                asset = self.client.get_asset(symbol_upper)
                
                # Check if asset exists and is tradeable
                if asset and hasattr(asset, 'tradable') and asset.tradable:
                    results[symbol] = True
                    logger.debug(f"Symbol {symbol} is tradeable on Alpaca")
                else:
                    results[symbol] = False
                    status = getattr(asset, 'status', 'unknown') if asset else 'not found'
                    logger.debug(f"Symbol {symbol} exists but not tradeable (status: {status})")
                    
            except Exception as e:
                # Symbol doesn't exist or API error
                results[symbol] = False
                error_msg = str(e)
                # Don't log full exception for expected "not found" errors
                if '404' in error_msg or 'not found' in error_msg.lower() or 'invalid symbol' in error_msg.lower():
                    logger.debug(f"Symbol {symbol} not found/invalid on Alpaca: {error_msg}")
                else:
                    logger.warning(f"Error checking symbol {symbol} on Alpaca: {e}")
        
        return results

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
            from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
            from alpaca.data.enums import DataFeed
            # Create data client for market data
            data_client = StockHistoricalDataClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"]
            )

            # Normalize input to list for uniform processing
            is_single_symbol = isinstance(symbol_or_symbols, str)
            symbols_list = [symbol_or_symbols] if is_single_symbol else symbol_or_symbols

            feed_setting = (self.settings.get("data_feed") or "delayed_sip").lower()
            feed_map = {
                "delayed_sip": DataFeed.DELAYED_SIP,
                "sip": DataFeed.SIP,
                "iex": DataFeed.IEX,
                "otc": DataFeed.OTC,
            }
            feed = feed_map.get(feed_setting, DataFeed.DELAYED_SIP)

            # Fetch last trade prices — these match what the Alpaca broker displays and are
            # reliable even for thinly traded stocks where bid/ask can be stale.
            trade_request = StockLatestTradeRequest(symbol_or_symbols=symbols_list, feed=feed)
            trades = data_client.get_stock_latest_trade(trade_request)

            # Fetch quotes as fallback for symbols with no trade data
            quote_request = StockLatestQuoteRequest(symbol_or_symbols=symbols_list, feed=feed)
            quotes = data_client.get_stock_latest_quote(quote_request)

            def get_trade_price(symbol):
                """Return last trade price for symbol, or None if unavailable."""
                if symbol in trades:
                    trade = trades[symbol]
                    price = float(trade.price) if trade.price else None
                    return price if price and price > 0 else None
                return None

            def get_quote_price(symbol):
                """Return quote-based price for symbol using price_type, or None if unavailable."""
                if symbol not in quotes:
                    return None
                quote = quotes[symbol]
                bid_price = float(quote.bid_price) if quote.bid_price else None
                ask_price = float(quote.ask_price) if quote.ask_price else None
                if price_type == 'bid':
                    return bid_price if bid_price else ask_price
                elif price_type == 'ask':
                    return ask_price if ask_price else bid_price
                elif price_type in ('avg', 'mid'):
                    if bid_price and ask_price:
                        return (bid_price + ask_price) / 2
                    return bid_price or ask_price
                else:
                    logger.warning(f"Invalid price_type '{price_type}', defaulting to trade/bid")
                    return bid_price if bid_price else ask_price

            def resolve_price(symbol):
                """Use last trade price primarily; fall back to quote if trade unavailable."""
                trade_price = get_trade_price(symbol)
                if trade_price is not None:
                    return trade_price
                quote_price = get_quote_price(symbol)
                if quote_price is not None:
                    logger.debug(f"No trade data for {symbol}, using quote price {quote_price}")
                return quote_price

            # Handle single symbol case (backward compatibility)
            if is_single_symbol:
                symbol = symbol_or_symbols
                current_price = resolve_price(symbol)
                if current_price is not None:
                    logger.debug(f"Current price for {symbol}: {current_price}")
                else:
                    logger.warning(f"No price data found for symbol {symbol}")
                return current_price

            # Handle multiple symbols case
            else:
                result = {}
                for symbol in symbols_list:
                    current_price = resolve_price(symbol)
                    result[symbol] = current_price
                    if current_price is not None:
                        logger.debug(f"Bulk fetch - Current price for {symbol}: {current_price}")
                    else:
                        logger.warning(f"Bulk fetch - No price data found for symbol {symbol}")

                logger.info(f"Bulk fetched prices for {len(symbols_list)} symbols in single API call")
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
            if positions is None:
                logger.error("Error refreshing positions from Alpaca: fetch failed")
                return False
            logger.info(f"Successfully refreshed {len(positions)} positions from Alpaca")
            return True
        except Exception as e:
            logger.error(f"Error refreshing positions from Alpaca: {e}", exc_info=True)
            return False

    def refresh_orders(self, heuristic_mapping: bool = False, fetch_all: bool = True) -> bool:
        """
        Refresh/synchronize account orders from Alpaca broker.
        This method updates database records with current order states from the broker.

        Uses raw Alpaca orders directly to access client_order_id (which is our database
        order ID) as the primary matching mechanism.

        Args:
            heuristic_mapping (bool): If True, pre-loads all database orders into memory for
                                      faster broker_order_id lookups (performance optimization
                                      for large order sets).
            fetch_all (bool): If True, fetches all orders from Alpaca using pagination.
                              If False, fetches only first 500 orders (faster but incomplete).
                              Defaults to True for complete synchronization.

        Returns:
            bool: True if refresh was successful, False otherwise
        """
        try:
            # Get raw Alpaca orders (not converted to TradingOrder)
            raw_alpaca_orders = self._fetch_raw_alpaca_orders(OrderStatus.ALL, fetch_all=fetch_all)

            if not raw_alpaca_orders:
                logger.warning("No orders returned from Alpaca during refresh")
                return True

            updated_count = 0
            mapped_count = 0

            # Pre-load broker_order_id map for faster lookups when heuristic_mapping is enabled
            broker_id_map = {}
            if heuristic_mapping:
                with Session(get_db().bind) as session:
                    db_orders = session.exec(
                        select(TradingOrder).where(TradingOrder.account_id == self.id)
                    ).all()
                    broker_id_map = {order.broker_order_id: order for order in db_orders if order.broker_order_id}

            # Process each raw Alpaca order
            for raw_order in raw_alpaca_orders:
                broker_order_id = str(raw_order.id) if raw_order.id else None
                if not broker_order_id:
                    continue

                db_order = None

                # Step 1: Try client_order_id first (primary match)
                # client_order_id is set to str(trading_order.id) during submission
                client_order_id = getattr(raw_order, 'client_order_id', None)
                if client_order_id:
                    try:
                        order_id = int(client_order_id)
                    except (ValueError, TypeError):
                        # client_order_id is not a valid int (e.g., OCO legs set by Alpaca)
                        order_id = None
                    if order_id is not None:
                        # Use session.get (returns None on miss) instead of get_instance
                        # (which raises). When a broker still reports a client_order_id
                        # for a row that no longer exists locally (e.g. after a DB wipe),
                        # we just skip and let Step 2 try to map by broker_order_id.
                        with Session(get_db().bind) as session:
                            candidate = session.get(TradingOrder, order_id)
                            if candidate and candidate.account_id == self.id:
                                db_order = candidate
                                # Backfill broker_order_id if missing
                                if not db_order.broker_order_id:
                                    db_order.broker_order_id = broker_order_id
                                    update_instance(db_order)
                                    mapped_count += 1
                                    logger.info(f"Mapped database order {db_order.id} to broker order {broker_order_id} via client_order_id")
                            elif candidate is None:
                                logger.debug(
                                    f"Broker reports client_order_id={order_id} for broker_order_id={broker_order_id}, "
                                    f"but local TradingOrder {order_id} no longer exists — falling back to broker_order_id match"
                                )

                # Step 2: Fallback to broker_order_id lookup
                if not db_order:
                    if heuristic_mapping and broker_order_id in broker_id_map:
                        db_order = broker_id_map[broker_order_id]
                    else:
                        with Session(get_db().bind) as session:
                            statement = select(TradingOrder).where(
                                TradingOrder.broker_order_id == broker_order_id,
                                TradingOrder.account_id == self.id
                            )
                            result = session.exec(statement).first()
                            if result:
                                db_order = get_instance(TradingOrder, result.id)

                # Step 3: Update order state if we found a match
                if db_order:
                    has_changes = False

                    # Convert Alpaca status to our OrderStatus enum
                    alpaca_status = self._sanitize_enum_field(
                        getattr(raw_order, 'status', None),
                        OrderStatus,
                        'status',
                        nullable=False,
                        default_value=OrderStatus.UNKNOWN
                    )
                    alpaca_filled_qty = getattr(raw_order, 'filled_qty', None)
                    alpaca_open_price = getattr(raw_order, 'filled_avg_price', None)

                    # PENDING_CANCEL: we've requested a cancel and are waiting for the broker
                    # to confirm it (a dependent replacement triggers on the real CANCELED, so
                    # it must not fire until the broker actually releases the qty). Advance only
                    # once the order reaches a FINAL broker state — CANCELED (confirmed) or a
                    # completion the cancel raced (FILLED/EXPIRED/REJECTED/...); otherwise stay
                    # PENDING_CANCEL and keep waiting.
                    if db_order.status == OrderStatus.PENDING_CANCEL:
                        resolved = OrderStatus.resolve_pending_cancel(alpaca_status)
                        if resolved is not None and resolved != db_order.status:
                            logger.info(f"Order {db_order.id} PENDING_CANCEL -> {resolved.value} (broker reported {alpaca_status})")
                            db_order.status = resolved
                            has_changes = True
                        else:
                            logger.debug(f"Order {db_order.id} in PENDING_CANCEL - broker {alpaca_status}, still waiting for confirmation")
                    # Normal status update for non-PENDING_CANCEL orders
                    elif db_order.status != alpaca_status:
                        logger.debug(f"Order {db_order.id} status changed: {db_order.status} -> {alpaca_status}")
                        db_order.status = alpaca_status
                        has_changes = True

                    if alpaca_filled_qty is not None and (db_order.filled_qty is None or float(db_order.filled_qty) != float(alpaca_filled_qty)):
                        logger.debug(f"Order {db_order.id} filled_qty changed: {db_order.filled_qty} -> {alpaca_filled_qty}")
                        db_order.filled_qty = alpaca_filled_qty
                        has_changes = True

                    # Update open_price if it changed (use broker's filled_avg_price)
                    if alpaca_open_price and (db_order.open_price is None or float(db_order.open_price) != float(alpaca_open_price)):
                        logger.debug(f"Order {db_order.id} open_price changed: {db_order.open_price} -> {alpaca_open_price}")
                        db_order.open_price = alpaca_open_price
                        has_changes = True

                    # Update broker_order_id if it wasn't set before
                    if not db_order.broker_order_id:
                        logger.debug(f"Order {db_order.id} broker_order_id set to: {broker_order_id}")
                        db_order.broker_order_id = broker_order_id
                        has_changes = True

                    # Use thread-safe update if there were changes
                    if has_changes:
                        update_instance(db_order)
                        updated_count += 1
                        logger.debug(f"Updated database order {db_order.id} with changes from Alpaca order {broker_order_id}")

                        # A cancel that raced a live fill leaves the order CANCELED with
                        # filled_qty > 0: those shares really traded at the broker, so fold
                        # them back into the transaction (otherwise the book over-counts the
                        # position -> "Quantity Mismatch" warning). Idempotent no-op otherwise.
                        if db_order.status == OrderStatus.CANCELED and (db_order.filled_qty or 0) > 0:
                            from ba2_trade_platform.core.TransactionHelper import TransactionHelper
                            TransactionHelper.reconcile_canceled_partial_fill(db_order)

                    # Step 3a: Update or insert OCO order legs if this is an OCO order
                    if db_order.order_type == CoreOrderType.OCO:
                        legs_inserted = 0
                        legs_updated = 0

                        # Extract legs_broker_ids from raw order's legs array
                        raw_legs = getattr(raw_order, 'legs', None)
                        legs_broker_ids = None
                        if raw_legs:
                            legs_broker_ids = [str(leg.id) for leg in raw_legs if hasattr(leg, 'id') and leg.id]

                        if legs_broker_ids:
                            # Update existing OCO legs in database
                            legs_updated = self._update_existing_oco_legs(db_order)
                            # Insert any legs that don't exist yet (has duplicate check by broker_order_id)
                            legs_inserted = self._insert_oco_legs_from_broker_ids(db_order, legs_broker_ids)
                        elif raw_legs:
                            # Fallback: full leg objects but no broker IDs extracted — use raw leg insertion
                            self._insert_oco_order_legs(raw_order, db_order, db_order.transaction_id)
                        else:
                            # OCO order but no legs in response -- update existing legs from individual API calls
                            legs_updated = self._update_existing_oco_legs(db_order)

                        if legs_inserted > 0 or legs_updated > 0:
                            logger.info(f"Order {db_order.id}: Updated {legs_updated} OCO legs, Inserted {legs_inserted} OCO leg orders")

            # Step 4: Mark database orders with broker_order_ids that don't exist in Alpaca as CANCELED
            # This catches orders that were canceled in Alpaca but status wasn't updated in database
            canceled_count = 0
            alpaca_broker_ids = {str(order.id) for order in raw_alpaca_orders if order.id}

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

                        # CRITICAL: Before marking as CANCELED, try to verify order status at broker
                        # Alpaca's get_orders() API has retention limits and may not return old orders
                        # We should only mark as CANCELED if we can explicitly confirm the order is canceled
                        broker_order = None
                        verification_failed = False
                        try:
                            broker_order = self.get_order(db_order.broker_order_id)
                        except Exception as check_err:
                            logger.debug(f"Could not verify order {db_order.broker_order_id} at broker: {check_err}")
                            verification_failed = True

                        # If we successfully retrieved the order from broker, update to match broker status
                        if broker_order:
                            if broker_order.status == OrderStatus.FILLED:
                                logger.info(
                                    f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                                    f"is FILLED at broker but not in get_orders() list (old order) - updating status to FILLED"
                                )
                                fresh_order = get_instance(TradingOrder, db_order.id)
                                if fresh_order:
                                    fresh_order.status = OrderStatus.FILLED
                                    fresh_order.filled_qty = broker_order.filled_qty
                                    if broker_order.open_price:
                                        fresh_order.open_price = broker_order.open_price
                                    update_instance(fresh_order)
                                    updated_count += 1
                                continue
                            elif broker_order.status == OrderStatus.CANCELED:
                                # Explicitly confirmed as CANCELED at broker
                                logger.info(
                                    f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                                    f"confirmed as CANCELED at broker"
                                )
                                fresh_order = get_instance(TradingOrder, db_order.id)
                                if fresh_order:
                                    fresh_order.status = OrderStatus.CANCELED
                                    # Copy any partial execution that happened before the cancel
                                    if broker_order.filled_qty:
                                        fresh_order.filled_qty = float(broker_order.filled_qty)
                                    if broker_order.open_price:
                                        fresh_order.open_price = broker_order.open_price
                                    update_instance(fresh_order)
                                    canceled_count += 1
                                    # Fold a cancel-raced partial fill back into the transaction
                                    if (fresh_order.filled_qty or 0) > 0:
                                        from ba2_trade_platform.core.TransactionHelper import TransactionHelper
                                        TransactionHelper.reconcile_canceled_partial_fill(fresh_order)
                                continue
                            else:
                                # Order exists at broker with other status - update only if changed
                                fresh_order = get_instance(TradingOrder, db_order.id)
                                if fresh_order:
                                    status_changed = fresh_order.status != broker_order.status

                                    # Compare filled_qty with type conversion (broker may return string "0" vs float 0.0)
                                    broker_filled = float(broker_order.filled_qty) if broker_order.filled_qty else 0.0
                                    db_filled = float(fresh_order.filled_qty) if fresh_order.filled_qty else 0.0
                                    filled_qty_changed = broker_filled != db_filled

                                    if status_changed or filled_qty_changed:
                                        logger.info(
                                            f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                                            f"has status {broker_order.status} at broker (was {fresh_order.status}) - updating"
                                        )
                                        fresh_order.status = broker_order.status
                                        if broker_order.filled_qty:
                                            fresh_order.filled_qty = float(broker_order.filled_qty)
                                        update_instance(fresh_order)
                                        updated_count += 1
                                continue

                        # If verification failed or order not found at broker:
                        # DON'T automatically mark as CANCELED - this could be an old order beyond retention period
                        # Only mark as CANCELED if it's recent (less than 30 days old)
                        if verification_failed or broker_order is None:
                            if db_order.created_at:
                                created_at = db_order.created_at
                                if created_at.tzinfo is None:
                                    created_at = created_at.replace(tzinfo=timezone.utc)

                                order_age_days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
                                if order_age_days > 30:
                                    # Old order - probably beyond retention period, leave it alone
                                    logger.debug(
                                        f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                                        f"not found at broker but is {order_age_days:.1f} days old (likely beyond retention period) - skipping"
                                    )
                                    continue

                            # Recent order not found - mark as CANCELED
                            logger.warning(
                                f"Order {db_order.id} (broker_order_id={db_order.broker_order_id}) "
                                f"not found in Alpaca (recent order), marking as CANCELED"
                            )
                            fresh_order = get_instance(TradingOrder, db_order.id)
                            if fresh_order:
                                fresh_order.status = OrderStatus.CANCELED
                                update_instance(fresh_order)
                                canceled_count += 1

            # Step 5: Check for dependent orders that can now be submitted
            triggered_count = self._check_and_submit_dependent_orders()

            if mapped_count > 0:
                logger.info(f"Successfully refreshed orders from Alpaca: {updated_count} updated, {mapped_count} mapped via client_order_id, {canceled_count} marked as canceled, {triggered_count} dependent orders triggered")
            else:
                logger.info(f"Successfully refreshed orders from Alpaca: {updated_count} updated, {canceled_count} marked as canceled, {triggered_count} dependent orders triggered")
            return True

        except Exception as e:
            logger.error(f"Error refreshing orders from Alpaca: {e}", exc_info=True)
            return False
    
    def _check_and_submit_dependent_orders(self) -> int:
        """
        Check for PENDING orders with depends_on_order and submit them if dependency is met.

        Division of responsibility (do not confuse the two dependent-order paths):
        - THIS method handles **PENDING** dependents that have **no explicit**
          depends_order_status_trigger and fire on *any terminal status* of the parent.
          It is the live handler for the OCO cancel-replace flow, where a replacement
          TP/SL order is created with status=PENDING + depends_on_order = the old order,
          waiting for that old order to reach CANCELED (see _replace_broker_*_order).
        - **WAITING_TRIGGER** dependents (the common entry-fill TP/SL case, exact-match
          trigger) are handled by TradeManager._check_all_waiting_trigger_orders, NOT here.

        Called from refresh_orders().

        This handles the workflow where:
        1. Order A is PENDING_CANCEL waiting for cancellation
        2. Order A transitions to CANCELED
        3. Order B (depends_on_order=A, status=PENDING) should now be submitted

        Returns:
            int: Number of dependent orders submitted
        """
        try:
            from sqlmodel import Session, select
            
            # Phase 1: Collect orders to submit (within session, read-only)
            # We collect IDs and necessary data, then close session before submitting
            orders_to_submit = []  # List of (order_id, parent_id) tuples
            orders_to_error = []   # List of (order_id, reason) tuples for invalid orders
            
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
                            logger.warning(f"Cannot submit dependent order {order.id}: parent order {parent_order.id} has invalid quantity {parent_order.quantity}. Will mark as ERROR.")
                            orders_to_error.append((order.id, f"parent order {parent_order.id} has invalid quantity {parent_order.quantity}"))
                            continue
                        
                        # Queue for submission (will submit outside session)
                        orders_to_submit.append((order.id, parent_order.id))
            
            # Session is now closed - safe to perform database operations
            
            # Phase 2: Mark invalid orders as ERROR
            for order_id, reason in orders_to_error:
                try:
                    order = get_instance(TradingOrder, order_id)
                    if order:
                        order.status = OrderStatus.ERROR
                        update_instance(order)
                except Exception as e:
                    logger.error(f"Failed to mark order {order_id} as ERROR: {e}", exc_info=True)
            
            # Phase 3: Submit dependent orders (outside the session to avoid locks)
            triggered_count = 0
            for order_id, parent_id in orders_to_submit:
                logger.info(f"Submitting dependent order {order_id} (depends on {parent_id})")
                try:
                    # Re-fetch the order fresh for submission
                    order = get_instance(TradingOrder, order_id)
                    if order and order.status == OrderStatus.PENDING:
                        self.submit_order(order)
                        triggered_count += 1
                    else:
                        logger.warning(f"Order {order_id} no longer PENDING (status={order.status if order else 'NOT FOUND'}), skipping submission")
                except Exception as e:
                    logger.error(f"Failed to submit dependent order {order_id}: {e}", exc_info=True)
                    # Mark order as ERROR status
                    try:
                        order = get_instance(TradingOrder, order_id)
                        if order:
                            order.status = OrderStatus.ERROR
                            update_instance(order)
                    except Exception as e2:
                        logger.error(f"Failed to mark order {order_id} as ERROR after submission failure: {e2}", exc_info=True)
            
            if triggered_count > 0:
                logger.info(f"Triggered {triggered_count} dependent orders")
            
            return triggered_count
            
        except Exception as e:
            logger.error(f"Error checking dependent orders: {e}", exc_info=True)
            return 0



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
                # good_for, NOT time_in_force: TradingOrder has never had a
                # time_in_force field, so this both dropped the kwarg AND raised
                # AttributeError on the READ. Matches _update_broker_tp_order.
                good_for=sl_order.good_for,
                stop_price=new_sl_price,  # New price
                limit_price=sl_order.limit_price,
                status=sl_order.status,
                comment=sl_order.comment,
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
                good_for=sl_order.good_for,
                stop_price=new_sl_price,
                limit_price=sl_order.limit_price,
                status=replacement_order.status,  # Status from broker (typically NEW or ACCEPTED)
                comment=replacement_order.comment,  # Tracking comment from broker
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
    
    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """
        Adjust take profit for a transaction.

        Args:
            transaction: Transaction to adjust TP for
            new_tp_price: New take profit price
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        return self.adjust_tp_sl(transaction, new_tp_price=new_tp_price, new_sl_price=None, source=source)

    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """
        Adjust stop loss for a transaction.

        Args:
            transaction: Transaction to adjust SL for
            new_sl_price: New stop loss price
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        return self.adjust_tp_sl(transaction, new_tp_price=None, new_sl_price=new_sl_price, source=source)

    def adjust_tp_sl(
        self,
        transaction: Transaction,
        new_tp_price: float | None = None,
        new_sl_price: float | None = None,
        source: str = ""
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
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(
            transaction=transaction,
            new_tp_price=new_tp_price,
            new_sl_price=new_sl_price,
            source=source
        )

    def _adjust_tpsl_internal(
        self,
        transaction: Transaction,
        new_tp_price: float | None = None,
        new_sl_price: float | None = None,
        source: str = ""
    ) -> bool:
        """
        Internal unified helper for adjusting TP and/or SL.

        This consolidates all the duplicated logic from adjust_tp(), adjust_sl(), and adjust_tp_sl().

        Args:
            transaction: Transaction to adjust
            new_tp_price: New take profit price (None = don't adjust TP)
            new_sl_price: New stop loss price (None = don't adjust SL)
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        try:
            # Capture old values before any modification for activity logging
            old_tp = transaction.take_profit
            old_sl = transaction.stop_loss

            adjustment_type = []
            if new_tp_price is not None:
                adjustment_type.append(f"TP=${new_tp_price:.2f}")
            if new_sl_price is not None:
                adjustment_type.append(f"SL=${new_sl_price:.2f}")
            source_label = f" (source: {source})" if source else ""
            logger.info(f"Adjusting {', '.join(adjustment_type)} for transaction {transaction.id}{source_label}")
            
            # Use a single session context to avoid SQLAlchemy session conflicts
            from sqlmodel import Session, select
            with Session(get_db().bind) as session:
                # 1. Get fresh transaction in this session (avoids session attachment conflicts)
                transaction_in_session = session.get(Transaction, transaction.id)
                if not transaction_in_session:
                    logger.error(f"Transaction {transaction.id} not found in database")
                    return False

                # Manual-override lock enforcement: if the user has locked a side,
                # drop any non-manual update to that side BEFORE any work happens.
                # Manual updates always pass through and also set/keep the lock.
                # NOTE: a call with BOTH prices None from the start is a RECONCILE
                # request — it falls through so the standing exit order gets rebuilt
                # (or cancelled) to match the transaction's current TP/SL values.
                requested_any = new_tp_price is not None or new_sl_price is not None
                if source == "manual":
                    if new_tp_price is not None:
                        transaction_in_session.tp_manual_override = True
                    if new_sl_price is not None:
                        transaction_in_session.sl_manual_override = True
                else:
                    if new_tp_price is not None and transaction_in_session.tp_manual_override:
                        logger.info(
                            f"Transaction {transaction.id}: ignoring {source or 'auto'} TP adjustment "
                            f"to ${new_tp_price:.2f} — TP is manually locked (current ${transaction_in_session.take_profit})"
                        )
                        new_tp_price = None
                    if new_sl_price is not None and transaction_in_session.sl_manual_override:
                        logger.info(
                            f"Transaction {transaction.id}: ignoring {source or 'auto'} SL adjustment "
                            f"to ${new_sl_price:.2f} — SL is manually locked (current ${transaction_in_session.stop_loss})"
                        )
                        new_sl_price = None
                    if requested_any and new_tp_price is None and new_sl_price is None:
                        logger.debug(
                            f"Transaction {transaction.id}: all sides of {source or 'auto'} TP/SL "
                            f"adjustment are manually locked — nothing to do"
                        )
                        return True  # Not a failure — caller asked for changes that are blocked by policy

                # 2. Get entry order (first market/limit order for this transaction, not TP/SL)
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

                # 3. Early skip check: if values unchanged and the existing exit orders already
                # have the right STRUCTURE (OCO vs plain limit vs plain stop) and prices, skip.
                tp_unchanged = (new_tp_price is None or
                               (transaction_in_session.take_profit is not None and
                                abs(transaction_in_session.take_profit - new_tp_price) < 0.01))
                sl_unchanged = (new_sl_price is None or
                               (transaction_in_session.stop_loss is not None and
                                abs(transaction_in_session.stop_loss - new_sl_price) < 0.01))

                if tp_unchanged and sl_unchanged:
                    target_spec = self._target_exit_spec(transaction_in_session, entry_order)
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

                    if target_spec is None:
                        if not valid_tpsl_orders:
                            logger.info(f"Skipping TP/SL adjustment for transaction {transaction.id}: "
                                       f"no TP/SL set and no exit orders exist")
                            return True
                        # else: fall through to cancel the leftover exit orders
                    elif valid_tpsl_orders:
                        target_type, want_tp, want_sl, _ = target_spec
                        orders_match = True
                        for order in valid_tpsl_orders:
                            if order.order_type != target_type:
                                logger.debug(f"Exit order {order.id} is {order.order_type}, target structure is {target_type}")
                                orders_match = False
                                break
                            if want_tp is not None and (order.limit_price is None or abs(order.limit_price - want_tp) >= 0.01):
                                logger.debug(f"Exit order {order.id} has TP={order.limit_price}, expected ${want_tp:.2f}")
                                orders_match = False
                                break
                            if want_sl is not None and (order.stop_price is None or abs(order.stop_price - want_sl) >= 0.01):
                                logger.debug(f"Exit order {order.id} has SL={order.stop_price}, expected ${want_sl:.2f}")
                                orders_match = False
                                break

                        if orders_match:
                            logger.info(f"Skipping TP/SL adjustment for transaction {transaction.id}: "
                                       f"values unchanged and {len(valid_tpsl_orders)} valid order(s) already exist with correct structure and prices")
                            return True
                        else:
                            logger.info(f"Proceeding with TP/SL adjustment for transaction {transaction.id}: "
                                       f"existing orders have wrong structure or prices")

                # 4. Update transaction (source of truth)
                if new_tp_price is not None:
                    transaction_in_session.take_profit = new_tp_price
                if new_sl_price is not None:
                    transaction_in_session.stop_loss = new_sl_price
                session.add(transaction_in_session)
                session.commit()

                # 5. Find existing exit orders (everything non-terminal that isn't the entry)
                all_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == transaction.id,
                        TradingOrder.status.notin_(OrderStatus.get_terminal_statuses()),
                        TradingOrder.id != entry_order.id
                    )
                ).all()

                # 6. Decide target structure from what's actually set (see _target_exit_spec):
                # both -> OCO, TP-only -> plain limit, SL-only -> plain stop, neither -> no exit order.
                spec = self._target_exit_spec(transaction_in_session, entry_order)

                spec_desc = f"{spec[0].value} TP={spec[1]} SL={spec[2]}" if spec else "none (cancel exits)"
                logger.debug(f"Entry order {entry_order.id} status: {entry_order.status}, "
                           f"existing exit orders: {[o.id for o in all_orders]}, "
                           f"target exit structure: {spec_desc}")

                # 7. Determine action based on entry order state
                result = False
                if entry_order.status in (OrderStatus.get_unsent_statuses() | OrderStatus.get_unfilled_statuses()):
                    # Entry not filled yet - maintain a WAITING_TRIGGER exit order that
                    # fires when the entry fills
                    result = self._handle_unfilled_entry_exit(
                        session, transaction_in_session, entry_order, spec, all_orders
                    )

                elif entry_order.status in OrderStatus.get_executed_statuses():
                    # Entry filled - work with broker
                    result = self._handle_filled_entry_exit(
                        session, transaction_in_session, entry_order, spec, all_orders
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
                            old_tp_str = f"${old_tp:.2f}" if old_tp else "none"
                            adjustment_desc.append(f"TP {old_tp_str} → ${new_tp_price:.2f}")
                        if new_sl_price is not None:
                            old_sl_str = f"${old_sl:.2f}" if old_sl else "none"
                            adjustment_desc.append(f"SL {old_sl_str} → ${new_sl_price:.2f}")

                        source_suffix = f" (source: {source})" if source else ""
                        log_activity(
                            severity=ActivityLogSeverity.SUCCESS,
                            activity_type=ActivityLogType.TP_SL_ADJUSTED,
                            description=f"Adjusted {' and '.join(adjustment_desc)} for {transaction_in_session.symbol}{source_suffix}",
                            data={
                                "transaction_id": transaction_in_session.id,
                                "symbol": transaction_in_session.symbol,
                                "old_tp": old_tp,
                                "new_tp": new_tp_price,
                                "old_sl": old_sl,
                                "new_sl": new_sl_price,
                                "source": source,
                                "entry_order_status": entry_order.status.value
                            },
                            source_account_id=self.id,
                            source_expert_id=transaction_in_session.expert_id
                        )
                    except Exception as log_error:
                        logger.warning(f"Failed to log TP/SL adjustment activity: {log_error}")

                else:
                    # result=False without exception means broker rejected the order
                    try:
                        from ...core.db import log_activity
                        from ...core.types import ActivityLogSeverity, ActivityLogType

                        adjustment_desc = []
                        if new_tp_price is not None:
                            old_tp_str = f"${old_tp:.2f}" if old_tp else "none"
                            adjustment_desc.append(f"TP {old_tp_str} → ${new_tp_price:.2f}")
                        if new_sl_price is not None:
                            old_sl_str = f"${old_sl:.2f}" if old_sl else "none"
                            adjustment_desc.append(f"SL {old_sl_str} → ${new_sl_price:.2f}")

                        source_suffix = f" (source: {source})" if source else ""
                        log_activity(
                            severity=ActivityLogSeverity.FAILURE,
                            activity_type=ActivityLogType.TP_SL_ADJUSTED,
                            description=f"Failed to adjust {' and '.join(adjustment_desc)} for {transaction_in_session.symbol}{source_suffix}: broker rejected order",
                            data={
                                "transaction_id": transaction_in_session.id,
                                "symbol": transaction_in_session.symbol,
                                "old_tp": old_tp,
                                "new_tp": new_tp_price,
                                "old_sl": old_sl,
                                "new_sl": new_sl_price,
                                "source": source,
                                "error": "broker rejected order"
                            },
                            source_account_id=self.id,
                            source_expert_id=transaction_in_session.expert_id
                        )
                    except Exception as log_error:
                        logger.warning(f"Failed to log TP/SL adjustment failure activity: {log_error}")

                return result
                    
        except Exception as e:
            logger.error(f"Error adjusting TP/SL for transaction {transaction.id}: {e}", exc_info=True)
            
            # Log activity for failed TP/SL adjustment
            try:
                from ...core.db import log_activity
                from ...core.types import ActivityLogSeverity, ActivityLogType

                adjustment_desc = []
                if new_tp_price is not None:
                    old_tp_str = f"${old_tp:.2f}" if old_tp else "none"
                    adjustment_desc.append(f"TP {old_tp_str} → ${new_tp_price:.2f}")
                if new_sl_price is not None:
                    old_sl_str = f"${old_sl:.2f}" if old_sl else "none"
                    adjustment_desc.append(f"SL {old_sl_str} → ${new_sl_price:.2f}")

                source_suffix = f" (source: {source})" if source else ""
                log_activity(
                    severity=ActivityLogSeverity.FAILURE,
                    activity_type=ActivityLogType.TP_SL_ADJUSTED,
                    description=f"Failed to adjust {' and '.join(adjustment_desc)} for {transaction.symbol}{source_suffix}: {str(e)}",
                    data={
                        "transaction_id": transaction.id,
                        "symbol": transaction.symbol,
                        "old_tp": old_tp,
                        "new_tp": new_tp_price,
                        "old_sl": old_sl,
                        "new_sl": new_sl_price,
                        "source": source,
                        "error": str(e)
                    },
                    source_account_id=self.id,
                    source_expert_id=transaction.expert_id
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
                    status=OrderStatus.WAITING_TRIGGER,  # Dependent order should wait for trigger
                    depends_on_order=entry_order.id,
                    depends_order_status_trigger=OrderStatus.FILLED,
                    open_type=OrderOpenType.AUTOMATIC,
                    comment=oco_comment,
                    data={
                        "tp_percent_target": self._calculate_tp_percent(entry_order, transaction.take_profit) if transaction.take_profit else 0,
                        "sl_percent_target": self._calculate_sl_percent(entry_order, transaction.stop_loss) if transaction.stop_loss else 0,
                        "tpsl_reference_price": self._tpsl_reference_price(entry_order)
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
                        status=OrderStatus.WAITING_TRIGGER,  # Dependent order should wait for trigger
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
                        status=OrderStatus.WAITING_TRIGGER,  # Dependent order should wait for trigger
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
                status=OrderStatus.WAITING_TRIGGER,
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
                    status=OrderStatus.WAITING_TRIGGER,
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
                    status=OrderStatus.WAITING_TRIGGER,
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
    
    # ==================== NEW OCO-ONLY HANDLERS ====================
    # These simplified handlers ALWAYS use OCO orders with default TP/SL values
    # This eliminates the complexity of managing separate limit/stop orders
    
    def _handle_unfilled_entry_exit(
        self,
        session: Session,
        transaction: Transaction,
        entry_order: TradingOrder,
        spec: tuple | None,
        all_orders: list
    ) -> bool:
        """Handle TP/SL adjustment while the entry order is not filled yet
        (unsent OR submitted-but-unfilled).

        Maintains at most ONE exit order in WAITING_TRIGGER state that fires when
        the entry fills, shaped by the target spec (OCO / plain limit / plain stop).
        spec=None means no TP and no SL -> no exit order at all.
        """
        spec_desc = f"{spec[0].value} TP={spec[1]} SL={spec[2]}" if spec else "none"
        logger.info(f"[Exit] Maintaining exit order for unfilled entry {entry_order.id} "
                    f"(transaction {transaction.id}): target {spec_desc}")

        # Keep one existing DB-only WAITING_TRIGGER order if it already has the
        # target type (update prices in place); cancel everything else.
        keep: TradingOrder | None = None
        for order in all_orders:
            if (spec and keep is None
                    and order.order_type == spec[0]
                    and order.status == OrderStatus.WAITING_TRIGGER
                    and not order.broker_order_id
                    and order.depends_on_order == entry_order.id):
                keep = order
                continue
            if order.broker_order_id:
                try:
                    self.cancel_order(order.id)
                    logger.info(f"Cancelled broker order {order.id}")
                except Exception as e:
                    logger.warning(f"Failed to cancel broker order {order.id}: {e}")
            else:
                order.status = OrderStatus.CANCELED
                session.add(order)
        session.commit()

        if spec is None:
            logger.info(f"No TP/SL set for transaction {transaction.id} — no exit order created")
            return True

        order_type, tp_price, sl_price, label = spec
        if keep:
            keep.limit_price = tp_price
            keep.stop_price = sl_price
            session.add(keep)
            session.commit()
            logger.info(f"Updated pending {order_type.value} exit order {keep.id}: TP={tp_price}, SL={sl_price}")
        else:
            exit_order = self._build_exit_order(
                transaction, entry_order, spec,
                quantity=entry_order.quantity,
                depends_on=entry_order.id,
                trigger_status=OrderStatus.FILLED
            )
            session.add(exit_order)
            session.commit()
            logger.info(f"Created pending {order_type.value} exit order {exit_order.id} "
                        f"waiting for entry {entry_order.id} to fill: TP={tp_price}, SL={sl_price}")

        return True

    def _handle_filled_entry_exit(
        self,
        session: Session,
        transaction: Transaction,
        entry_order: TradingOrder,
        spec: tuple | None,
        all_orders: list
    ) -> bool:
        """Handle TP/SL adjustment when entry is filled - maintain the target exit
        structure at the broker (OCO / plain limit / plain stop / none).

        When an existing live broker order needs to be replaced, we don't submit
        the new exit order inline (Alpaca would reject with 'insufficient qty /
        held_for_orders' until the cancel fully clears). Instead we create the
        new order in WAITING_TRIGGER state linked to the cancellation of the
        outgoing order — TradeManager will submit it on the next refresh once
        the parent reaches CANCELED.
        """
        spec_desc = f"{spec[0].value} TP={spec[1]} SL={spec[2]}" if spec else "none"
        logger.info(f"[Exit] Adjusting broker exit order for filled entry {entry_order.id} "
                    f"(transaction {transaction.id}): target {spec_desc}")

        # Split existing exit orders into "live at broker" vs "DB-only / already terminal"
        terminal_statuses = OrderStatus.get_terminal_statuses()
        existing_set = [o for o in all_orders if o is not entry_order]
        live_broker_orders = [
            o for o in existing_set
            if o.broker_order_id and o.status not in terminal_statuses
        ]
        db_only_orders = [o for o in existing_set if o not in live_broker_orders]

        # 1. DB-only / dead orders: just mark cancelled inline
        for order in db_only_orders:
            if order.status not in terminal_statuses:
                order.status = OrderStatus.CANCELED
                session.add(order)
        if db_only_orders:
            session.commit()

        # 2. No TP and no SL -> just cancel whatever is live; no standing exit order.
        #    (A leftover placeholder SELL would wash-trade-block new BUYs on the symbol.)
        if spec is None:
            for order in live_broker_orders:
                try:
                    self.cancel_order(order.id)
                    logger.info(f"Cancelled broker exit order {order.id} — transaction has no TP/SL")
                except Exception as e:
                    logger.warning(f"Failed to cancel broker order {order.id}: {e}")
            return True

        order_type, tp_price, sl_price, label = spec

        # 3. No live broker order — straight path, submit immediately
        if not live_broker_orders:
            if order_type == CoreOrderType.OCO:
                return self._create_broker_oco_order(session, transaction, entry_order, tp_price, sl_price)
            elif tp_price:
                return self._create_broker_tp_order(session, transaction, entry_order, tp_price)
            else:
                return self._create_broker_sl_order(session, transaction, entry_order, sl_price)

        # 4. Live broker order(s) — chain the new exit order behind the cancellation
        #    of the most recent (highest-id) live order, then cancel all live
        #    orders. TradeManager's _check_all_waiting_trigger_orders will submit
        #    the new order once that parent reaches CANCELED.
        live_broker_orders.sort(key=lambda o: o.id, reverse=True)
        parent_for_trigger = live_broker_orders[0]

        order_quantity = transaction.quantity
        if not order_quantity or order_quantity <= 0:
            logger.error(f"Cannot stage replacement exit order for transaction {transaction.id}: invalid quantity {order_quantity}")
            return False

        waiting_exit = self._build_exit_order(
            transaction, entry_order, spec,
            quantity=order_quantity,
            depends_on=parent_for_trigger.id,
            trigger_status=OrderStatus.CANCELED,
            comment_suffix=f" (chained on cancel of {parent_for_trigger.id})"
        )
        session.add(waiting_exit)
        session.commit()
        logger.info(
            f"Staged replacement {order_type.value} exit order {waiting_exit.id} (WAITING_TRIGGER) for "
            f"transaction {transaction.id} — will submit when order {parent_for_trigger.id} "
            f"reaches CANCELED at broker"
        )

        # Submit cancel to broker for ALL live orders
        for order in live_broker_orders:
            try:
                self.cancel_order(order.id)
                logger.info(f"Submitted cancel for broker order {order.id} (broker_id={order.broker_order_id})")
            except Exception as e:
                logger.warning(f"Failed to cancel broker order {order.id}: {e}")

        return True

    # ==================== END EXIT-ORDER HANDLERS ====================
    
    def adjust_tp(self, transaction: Transaction, new_tp_price: float, source: str = "") -> bool:
        """
        Adjust take profit for a transaction.

        Args:
            transaction: Transaction to adjust TP for
            new_tp_price: New take profit price
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(transaction, new_tp_price=new_tp_price, new_sl_price=None, source=source)

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
                # submit_order handles broker errors internally (sets status=ERROR, logs FAILURE)
                # without raising exceptions, so we must re-check the actual order status.
                session.refresh(tp_order)
                if tp_order.status == OrderStatus.ERROR:
                    logger.error(f"TP order {tp_order.id} was rejected by broker (status=ERROR) for transaction {transaction.id}")
                    return False
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
    
    def adjust_sl(self, transaction: Transaction, new_sl_price: float, source: str = "") -> bool:
        """
        Adjust stop loss for a transaction.

        Args:
            transaction: Transaction to adjust SL for
            new_sl_price: New stop loss price
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(transaction, new_tp_price=None, new_sl_price=new_sl_price, source=source)

    def _calculate_sl_percent(self, entry_order: TradingOrder, sl_price: float) -> float:
        """Calculate SL percent from entry price"""
        if not entry_order.open_price or entry_order.open_price == 0:
            return 0.0
        return ((entry_order.open_price - sl_price) / entry_order.open_price) * 100

    def _tpsl_reference_price(self, entry_order: TradingOrder):
        """Best pre-fill anchor the TP/SL were computed against, so a pending OCO can be
        re-based to the entry's ACTUAL fill on trigger.

        Prefers the realized fill if present, else the limit price, else the originating
        recommendation's snapshot price (what the rule's order_open_price/current-price
        reference resolved to for a not-yet-filled market order), else the live price.
        """
        if entry_order.open_price:
            return entry_order.open_price
        if entry_order.limit_price:
            return entry_order.limit_price
        rec_id = getattr(entry_order, "expert_recommendation_id", None)
        if rec_id:
            try:
                from ...core.db import get_instance
                from ...core.models import ExpertRecommendation
                rec = get_instance(ExpertRecommendation, rec_id)
                if rec and getattr(rec, "price_at_date", None):
                    return rec.price_at_date
            except Exception:
                pass
        try:
            return self.get_instrument_current_price(entry_order.symbol)
        except Exception:
            return None

    def _target_exit_spec(self, transaction: Transaction, entry_order: TradingOrder):
        """Decide which exit-order structure a transaction needs at the broker.

        Exactly one standing exit order per position, shaped by what is actually set:
          - TP and SL set  -> OCO (limit leg = TP, stop leg = SL)
          - TP only        -> plain limit order (no stop leg)
          - SL only        -> plain stop order (no limit leg)
          - neither        -> None (no standing exit order at all)

        Alpaca's OCO requires BOTH legs, and a placeholder leg is not an option:
        besides price-reasonability rejections on far-away limits, any standing
        SELL blocks new BUY orders on the same symbol with a wash-trade rejection
        (code 40310000) — so a position without TP/SL must have no resting exit
        order at the broker.

        Returns (order_type, limit_price, stop_price, comment_label) or None.
        """
        has_tp = transaction.take_profit is not None and transaction.take_profit > 0
        has_sl = transaction.stop_loss is not None and transaction.stop_loss > 0
        exit_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
        if has_tp and has_sl:
            return (CoreOrderType.OCO, transaction.take_profit, transaction.stop_loss, "TPSL")
        if has_tp:
            order_type = CoreOrderType.SELL_LIMIT if exit_side == OrderDirection.SELL else CoreOrderType.BUY_LIMIT
            return (order_type, transaction.take_profit, None, "TP")
        if has_sl:
            order_type = CoreOrderType.SELL_STOP if exit_side == OrderDirection.SELL else CoreOrderType.BUY_STOP
            return (order_type, None, transaction.stop_loss, "SL")
        return None

    def _build_exit_order(
        self,
        transaction: Transaction,
        entry_order: TradingOrder,
        spec: tuple,
        quantity: float,
        depends_on: int,
        trigger_status: OrderStatus,
        comment_suffix: str = ""
    ) -> TradingOrder:
        """Build (not persist) a WAITING_TRIGGER exit order for the given spec."""
        order_type, tp_price, sl_price, label = spec
        exit_side = OrderDirection.SELL if entry_order.side == OrderDirection.BUY else OrderDirection.BUY
        comment = self._generate_tpsl_comment(label, self.id, transaction.id, entry_order.id) + comment_suffix
        data = {}
        if tp_price:
            data["tp_percent_target"] = self._calculate_tp_percent(entry_order, tp_price)
        if sl_price:
            data["sl_percent_target"] = self._calculate_sl_percent(entry_order, sl_price)
        ref_price = self._tpsl_reference_price(entry_order)
        if ref_price:
            data["tpsl_reference_price"] = ref_price
        return TradingOrder(
            account_id=self.id,
            symbol=entry_order.symbol,
            quantity=quantity,
            side=exit_side,
            order_type=order_type,
            limit_price=tp_price,
            stop_price=sl_price,
            transaction_id=transaction.id,
            status=OrderStatus.WAITING_TRIGGER,
            depends_on_order=depends_on,
            depends_order_status_trigger=trigger_status,
            open_type=OrderOpenType.AUTOMATIC,
            comment=comment,
            data=data,
            created_at=datetime.now(timezone.utc)
        )

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
                    "sl_percent_target": self._calculate_sl_percent(entry_order, sl_price),
                    "tpsl_reference_price": self._tpsl_reference_price(entry_order)
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
                # submit_order handles broker errors internally (sets status=ERROR, logs FAILURE)
                # without raising exceptions, so we must re-check the actual order status.
                session.refresh(oco_order)
                if oco_order.status == OrderStatus.ERROR:
                    logger.error(f"OCO order {oco_order.id} was rejected by broker (status=ERROR) for transaction {transaction.id}")
                    return False
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
                # submit_order handles broker errors internally (sets status=ERROR, logs FAILURE)
                # without raising exceptions, so we must re-check the actual order status.
                session.refresh(sl_order)
                if sl_order.status == OrderStatus.ERROR:
                    logger.error(f"SL order {sl_order.id} was rejected by broker (status=ERROR) for transaction {transaction.id}")
                    return False
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
    
    def adjust_tp_sl(self, transaction: Transaction, new_tp_price: float, new_sl_price: float, source: str = "") -> bool:
        """
        Adjust both take profit and stop loss for a transaction.

        Args:
            transaction: Transaction to adjust TP/SL for
            new_tp_price: New take profit price
            new_sl_price: New stop loss price
            source: Origin of adjustment (e.g. "manual", "ruleset", "smart_risk_manager")

        Returns:
            bool: True if adjustment succeeded
        """
        return self._adjust_tpsl_internal(transaction, new_tp_price=new_tp_price, new_sl_price=new_sl_price, source=source)
    
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

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        """Get dividend history from Alpaca activities API."""
        if not self._check_authentication():
            return []

        try:
            params = {}
            if start_date:
                params["after"] = start_date.isoformat() if hasattr(start_date, 'isoformat') else str(start_date)
            if end_date:
                params["until"] = end_date.isoformat() if hasattr(end_date, 'isoformat') else str(end_date)

            # TradingClient doesn't expose get_account_activities; use REST get() directly
            raw_activities = self.client.get("/account/activities/DIV", params or None)

            if not isinstance(raw_activities, list):
                raw_activities = [raw_activities] if raw_activities else []

            # Fetch DIVNRA (non-resident alien tax withholding) activities
            try:
                raw_nra = self.client.get("/account/activities/DIVNRA", params or None)
                if not isinstance(raw_nra, list):
                    raw_nra = [raw_nra] if raw_nra else []
            except Exception:
                raw_nra = []

            # Build map of tax withholding by (symbol, date)
            nra_map = {}
            for nra in raw_nra:
                nra_symbol = nra.get('symbol')
                nra_date = nra.get('date') or nra.get('transaction_time')
                if nra_symbol and nra_date:
                    nra_date_str = str(nra_date)[:10]  # Extract YYYY-MM-DD
                    key = (nra_symbol, nra_date_str)
                    nra_map[key] = nra_map.get(key, 0) + abs(float(nra.get('net_amount', 0) or 0))

            dividends = []
            for activity in raw_activities:
                act_symbol = activity.get('symbol')
                if symbol and act_symbol != symbol:
                    continue

                date_str = activity.get('date') or activity.get('transaction_time')
                try:
                    date_val = datetime.fromisoformat(str(date_str)) if date_str else None
                except (ValueError, TypeError):
                    date_val = date_str

                # Look up matching tax withholding
                date_key = str(date_str)[:10] if date_str else None
                tax_withheld = nra_map.get((act_symbol, date_key), 0.0) if date_key else 0.0

                gross = float(activity.get('net_amount', 0) or 0)
                div_record = {
                    # Alpaca stamps an id on every non-trade activity. It is the only
                    # thing that tells two DIV activities for one payer on one date
                    # apart (a special dividend alongside the regular one, a
                    # correction), so get_cash_transfers() keys the income ledger on
                    # it. Netting DIVNRA withholding into `amount` below is a separate
                    # concern and does not make the id any less the broker's.
                    'id': activity.get('id'),
                    'symbol': act_symbol,
                    'amount': round(gross - tax_withheld, 2),   # NET dividend (income kept)
                    'gross_amount': gross,
                    'date': date_val,
                    'drip_quantity': None,
                    'drip_price': None,
                    'tax_withheld': tax_withheld,
                }
                dividends.append(div_record)

            logger.debug(f"[Account {self.id}] Retrieved {len(dividends)} dividend activities")
            return dividends

        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching dividends: {e}", exc_info=True)
            return []

    def get_balance_history(self, start_date=None, end_date=None):
        """Get portfolio history from Alpaca portfolio history API."""
        if not self._check_authentication():
            return []

        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            params = {"timeframe": "1D", "period": "1A"}
            if start_date:
                params["start"] = start_date.strftime("%Y-%m-%d")
                params.pop("period", None)  # start overrides period
            if end_date:
                params["end"] = end_date.strftime("%Y-%m-%d")

            history = self.client.get_portfolio_history(GetPortfolioHistoryRequest(**params))

            # Fetch cash transfers (deposits/withdrawals) to compute actual daily P/L.
            # Alpaca's profit_loss from portfolio history does NOT exclude withdrawals,
            # so we need to subtract transfer amounts from equity changes ourselves.
            transfer_by_date = {}
            for act_type in ['CSD', 'CSW']:
                for act in self._fetch_activities(act_type):
                    act_date = str(act.get('date', ''))[:10]
                    amount = self._safe_float(act.get('net_amount'))
                    if amount is None:
                        continue
                    transfer_by_date[act_date] = transfer_by_date.get(act_date, 0.0) + amount

            snapshots = []
            timestamps = getattr(history, 'timestamp', []) or []
            equity_values = getattr(history, 'equity', []) or []

            # Build list of history dates for T+1 settlement shifting.
            # Transfers settle T+1: activity on Oct 29 → equity change on Oct 30.
            all_history_dates = []
            for ts in timestamps:
                d_obj = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else ts
                all_history_dates.append(d_obj.strftime('%Y-%m-%d'))

            shifted_transfers = {}
            for act_date, amount in transfer_by_date.items():
                shifted = False
                for hd in all_history_dates:
                    if hd > act_date:
                        shifted_transfers[hd] = shifted_transfers.get(hd, 0.0) + amount
                        shifted = True
                        break
                if not shifted:
                    shifted_transfers[act_date] = shifted_transfers.get(act_date, 0.0) + amount
            transfer_by_date = shifted_transfers

            prev_equity = None
            for i, ts in enumerate(timestamps):
                equity = float(equity_values[i]) if i < len(equity_values) and equity_values[i] is not None else 0.0
                date_obj = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else ts
                date_str = date_obj.strftime('%Y-%m-%d')

                # Compute actual daily market P/L by subtracting transfers
                if prev_equity is not None:
                    equity_change = equity - prev_equity
                    transfer = transfer_by_date.get(date_str, 0.0)
                    daily_pl = equity_change - transfer
                else:
                    daily_pl = 0.0

                snapshots.append({
                    'date': date_obj,
                    'net_liquidating_value': equity,
                    'cash_balance': 0.0,
                    'equity_value': equity,
                    'profit_loss': daily_pl,
                })
                prev_equity = equity

            logger.debug(f"[Account {self.id}] Retrieved {len(snapshots)} balance history snapshots")
            return snapshots

        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching balance history: {e}", exc_info=True)
            return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        """Get filled trade history from Alpaca closed orders."""
        if not self._check_authentication():
            return []

        try:
            raw_orders = self._fetch_raw_alpaca_orders(status=OrderStatus.CLOSED, fetch_all=True)
            trades = []
            for order in raw_orders:
                filled_qty = float(getattr(order, 'filled_qty', 0) or 0)
                if filled_qty <= 0:
                    continue

                order_symbol = getattr(order, 'symbol', None)
                if symbol and order_symbol != symbol:
                    continue

                filled_at = getattr(order, 'filled_at', None) or getattr(order, 'created_at', None)
                if filled_at:
                    if start_date and filled_at < start_date:
                        continue
                    if end_date and filled_at > end_date:
                        continue

                # Normalize side from Alpaca enum
                side_raw = getattr(order, 'side', None)
                side_str = str(side_raw).lower() if side_raw else ''
                if 'buy' in side_str:
                    side = 'BUY'
                else:
                    side = 'SELL'

                filled_price = float(getattr(order, 'filled_avg_price', 0) or 0)

                trades.append({
                    'symbol': order_symbol,
                    'qty': filled_qty,
                    'side': side,
                    'date': filled_at,
                    'price': filled_price,
                })

            logger.debug(f"[Account {self.id}] Retrieved {len(trades)} filled trades")
            return trades

        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching filled trades: {e}", exc_info=True)
            return []

    # ======================================================================
    # OptionsAccountInterface — market data (chain / quote / ATM-IV)
    # ======================================================================
    def _get_option_data_client(self):
        """Lazily create & cache an OptionHistoricalDataClient.

        Uses getattr so that a pre-set/monkeypatched ``self._option_data_client``
        (e.g. in tests) is honored instead of being overwritten.
        """
        client = getattr(self, "_option_data_client", None)
        if client is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            client = OptionHistoricalDataClient(
                api_key=self.settings["api_key"],
                secret_key=self.settings["api_secret"],
            )
            self._option_data_client = client
        return client

    def _options_feed(self):
        """Return the OptionsFeed to use. Defaults to INDICATIVE unless an OPRA
        subscription is configured via the optional ``options_feed`` setting."""
        from alpaca.data.enums import OptionsFeed
        feed_setting = (self.settings.get("options_feed") or "indicative").lower()
        if feed_setting == "opra":
            return OptionsFeed.OPRA
        return OptionsFeed.INDICATIVE

    @staticmethod
    def _option_right_to_contract_type(option_type):
        """Map our OptionRight -> alpaca ContractType (or None for both sides)."""
        if option_type is None:
            return None
        from alpaca.trading.enums import ContractType
        from ...core.types import OptionRight
        if option_type == OptionRight.CALL:
            return ContractType.CALL
        if option_type == OptionRight.PUT:
            return ContractType.PUT
        return None

    @alpaca_api_retry
    def _get_option_contracts_meta(self, underlying: str, expiry_min, expiry_max,
                                   option_type=None, strike_min=None, strike_max=None) -> Dict[str, Any]:
        """Fetch option contract metadata (strike / expiry / type / open interest)
        for the underlying within the given filters, paging through all results.

        Returns a dict keyed by OCC contract symbol -> alpaca OptionContract.
        Strike filters must be passed to Alpaca as STRINGS on this request.
        """
        from alpaca.trading.requests import GetOptionContractsRequest

        contract_type = self._option_right_to_contract_type(option_type)
        meta: Dict[str, Any] = {}
        page_token = None

        while True:
            request = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                expiration_date_gte=expiry_min,
                expiration_date_lte=expiry_max,
                type=contract_type,
                strike_price_gte=(str(strike_min) if strike_min is not None else None),
                strike_price_lte=(str(strike_max) if strike_max is not None else None),
                limit=10000,
                page_token=page_token,
            )
            response = self.client.get_option_contracts(request)
            contracts = getattr(response, "option_contracts", None) or []
            for contract in contracts:
                symbol = getattr(contract, "symbol", None)
                if symbol:
                    meta[symbol] = contract

            page_token = getattr(response, "next_page_token", None)
            if not page_token:
                break

        return meta

    @alpaca_api_retry
    def get_option_chain(self, underlying: str, expiry_min, expiry_max,
                         option_type=None, strike_min=None, strike_max=None) -> List[Any]:
        """Return option-chain rows (quote + Greeks + liquidity) for the underlying
        within the given expiry / strike / type filters.

        Joins live snapshot data (OptionHistoricalDataClient.get_option_chain) with
        contract metadata (TradingClient.get_option_contracts) on the OCC symbol.
        """
        from alpaca.data.requests import OptionChainRequest
        from ...core.option_types import OptionContract
        from ...core.types import OptionRight

        contract_type = self._option_right_to_contract_type(option_type)

        request = OptionChainRequest(
            underlying_symbol=underlying,
            feed=self._options_feed(),
            type=contract_type,
            strike_price_gte=strike_min,
            strike_price_lte=strike_max,
            expiration_date_gte=expiry_min,
            expiration_date_lte=expiry_max,
        )

        snapshots = self._get_option_data_client().get_option_chain(request) or {}
        meta_by_symbol = self._get_option_contracts_meta(
            underlying, expiry_min, expiry_max,
            option_type=option_type, strike_min=strike_min, strike_max=strike_max,
        )

        chain: List[OptionContract] = []
        for occ_symbol, snapshot in snapshots.items():
            meta = meta_by_symbol.get(occ_symbol)
            if meta is None:
                # No contract metadata (strike/expiry/type) — skip; we cannot build a row.
                continue

            # --- Contract metadata (guard every field) ---
            meta_type = getattr(meta, "type", None)
            type_value = str(getattr(meta_type, "value", meta_type) or "").lower()
            if type_value == OptionRight.CALL.value:
                row_type = OptionRight.CALL
            elif type_value == OptionRight.PUT.value:
                row_type = OptionRight.PUT
            else:
                row_type = None

            strike = getattr(meta, "strike_price", None)
            expiry = getattr(meta, "expiration_date", None)

            # Defensive filtering (the API already filters, but enforce anyway).
            if option_type is not None and row_type is not None and row_type != option_type:
                continue
            if strike is not None:
                if strike_min is not None and strike < strike_min:
                    continue
                if strike_max is not None and strike > strike_max:
                    continue
            if expiry is not None:
                if expiry_min is not None and expiry < expiry_min:
                    continue
                if expiry_max is not None and expiry > expiry_max:
                    continue

            open_interest = None
            oi_raw = getattr(meta, "open_interest", None)
            if oi_raw is not None:
                try:
                    open_interest = int(float(oi_raw))
                except (TypeError, ValueError):
                    open_interest = None

            # --- Snapshot quote / trade / greeks (guard every field) ---
            quote = getattr(snapshot, "latest_quote", None)
            bid = getattr(quote, "bid_price", None) if quote is not None else None
            ask = getattr(quote, "ask_price", None) if quote is not None else None

            trade = getattr(snapshot, "latest_trade", None)
            last = getattr(trade, "price", None) if trade is not None else None

            iv = getattr(snapshot, "implied_volatility", None)

            greeks = getattr(snapshot, "greeks", None)
            delta = getattr(greeks, "delta", None) if greeks is not None else None
            gamma = getattr(greeks, "gamma", None) if greeks is not None else None
            theta = getattr(greeks, "theta", None) if greeks is not None else None
            vega = getattr(greeks, "vega", None) if greeks is not None else None

            chain.append(OptionContract(
                symbol=occ_symbol,
                underlying=getattr(meta, "underlying_symbol", None) or underlying,
                option_type=row_type,
                strike=strike,
                expiry=expiry,
                bid=bid,
                ask=ask,
                last=last,
                implied_volatility=iv,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                open_interest=open_interest,
            ))

        return chain

    @alpaca_api_retry
    def get_option_quote(self, contract_symbol: str) -> Optional[Any]:
        """Return the latest quote + Greeks for a single OCC option contract, or
        None if no snapshot is available."""
        from alpaca.data.requests import OptionSnapshotRequest
        from ...core.option_types import OptionQuote

        request = OptionSnapshotRequest(
            symbol_or_symbols=contract_symbol,
            feed=self._options_feed(),
        )
        snapshots = self._get_option_data_client().get_option_snapshot(request) or {}
        snapshot = snapshots.get(contract_symbol)
        if snapshot is None:
            return None

        quote = getattr(snapshot, "latest_quote", None)
        bid = getattr(quote, "bid_price", None) if quote is not None else None
        ask = getattr(quote, "ask_price", None) if quote is not None else None
        timestamp = getattr(quote, "timestamp", None) if quote is not None else None

        trade = getattr(snapshot, "latest_trade", None)
        last = getattr(trade, "price", None) if trade is not None else None

        iv = getattr(snapshot, "implied_volatility", None)

        greeks = getattr(snapshot, "greeks", None)
        delta = getattr(greeks, "delta", None) if greeks is not None else None
        gamma = getattr(greeks, "gamma", None) if greeks is not None else None
        theta = getattr(greeks, "theta", None) if greeks is not None else None
        vega = getattr(greeks, "vega", None) if greeks is not None else None

        return OptionQuote(
            symbol=contract_symbol,
            bid=bid,
            ask=ask,
            last=last,
            implied_volatility=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            timestamp=timestamp,
        )

    def get_atm_implied_volatility(self, underlying: str) -> Optional[float]:
        """Return the near-ATM implied volatility for the underlying (0-1), picking
        the contract whose strike is nearest the current spot price from a
        near-dated chain. Returns None if no spot or empty chain."""
        spot = self.get_instrument_current_price(underlying, 'mid')
        if spot is None:
            logger.warning(f"get_atm_implied_volatility: no spot price for {underlying}")
            return None

        today = datetime.now(timezone.utc).date()
        expiry_min = today + timedelta(days=20)
        expiry_max = today + timedelta(days=45)

        chain = self.get_option_chain(underlying, expiry_min, expiry_max) or []
        candidates = [c for c in chain
                      if getattr(c, "strike", None) is not None
                      and getattr(c, "implied_volatility", None) is not None]
        if not candidates:
            logger.warning(f"get_atm_implied_volatility: empty/IV-less chain for {underlying}")
            return None

        nearest = min(candidates, key=lambda c: abs(c.strike - spot))
        return nearest.implied_volatility

    # ======================================================================
    # OptionsAccountInterface — TEMPORARY stubs (replaced in later tasks)
    # ======================================================================
    @staticmethod
    def _parse_occ_symbol(occ: str):
        """Parse an OCC option symbol into (underlying, expiry, right, strike).

        OCC format: <ROOT><YYMMDD><C|P><STRIKE*1000 as 8 digits>. The root is
        variable length (1-6 chars), so parse from the right:
          - last 8 chars  = strike x 1000
          - char at -9    = 'C' (call) / 'P' (put)
          - chars -15:-9  = YYMMDD expiry
          - everything before -15 = root / underlying

        Example: "AAPL260116C00150000" -> ("AAPL", date(2026,1,16), CALL, 150.0)
        """
        from datetime import date
        from ...core.types import OptionRight

        strike = int(occ[-8:]) / 1000.0
        right_char = occ[-9].upper()
        if right_char == "C":
            right = OptionRight.CALL
        elif right_char == "P":
            right = OptionRight.PUT
        else:
            raise ValueError(f"invalid OCC right char {right_char!r} in {occ!r}")
        yymmdd = occ[-15:-9]
        # OCC YY is 2000-based (00-99 -> 2000-2099)
        expiry = date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        root = occ[:-15]
        return root, expiry, right, strike

    @staticmethod
    def _to_float_or_none(value):
        """Coerce an optional broker field to float, preserving None."""
        return float(value) if value is not None else None

    @alpaca_api_retry
    def get_option_positions(self) -> List["OptionPosition"]:
        """Return all held option positions as broker-agnostic OptionPosition
        objects. Equity (and any non-option) positions are filtered out.

        Malformed OCC symbols are logged and skipped rather than crashing the
        whole call.
        """
        from ...core.option_types import OptionPosition
        from ...core.types import OrderDirection

        raw_positions = self.client.get_all_positions() or []
        positions: List[OptionPosition] = []

        for pos in raw_positions:
            asset_class = str(getattr(pos, "asset_class", "")).lower()
            if "option" not in asset_class:
                continue

            # The ENTIRE per-position mapping (OCC parse + required numeric
            # coercions + optional floats + construction) lives inside ONE
            # try/except so a single malformed broker row is skipped+warned
            # rather than aborting the whole call. avg_entry_price stays STRICT:
            # a missing required entry price skips that row (never defaulted).
            try:
                underlying, expiry, right, strike = self._parse_occ_symbol(pos.symbol)

                qty = float(pos.qty)
                side_str = str(getattr(pos, "side", "")).lower()
                if "short" in side_str or qty < 0:
                    side = OrderDirection.SELL
                else:
                    side = OrderDirection.BUY

                positions.append(OptionPosition(
                    contract_symbol=pos.symbol,
                    underlying=underlying,
                    option_type=right,
                    strike=strike,
                    expiry=expiry,
                    side=side,
                    quantity=abs(qty),
                    avg_entry_price=float(pos.avg_entry_price),
                    current_price=self._to_float_or_none(getattr(pos, "current_price", None)),
                    market_value=self._to_float_or_none(getattr(pos, "market_value", None)),
                    unrealized_pl=self._to_float_or_none(getattr(pos, "unrealized_pl", None)),
                    multiplier=100,
                ))
            except Exception as e:
                logger.warning(f"Skipping option position {getattr(pos, 'symbol', '?')}: {e}")
                continue

        return positions

    def _build_option_order_request(self, trading_order, legs):
        """Build an Alpaca option order request (PURE - no network, no DB).

        single leg -> Market/LimitOrderRequest with symbol=<OCC>, side, qty.
        2-4 legs   -> Market/LimitOrderRequest with order_class=MLEG, legs=[...],
                      qty=<overall>, and NO top-level symbol.

        Options must use TimeInForce.DAY. For MLEG limit orders, a positive
        limit_price is a net debit and a negative one is a net credit.
        """
        def _to_side(direction):
            return OrderSide.BUY if direction == OrderDirection.BUY else OrderSide.SELL

        def _to_intent(intent):
            if not intent:
                return None
            return PositionIntent(str(intent).lower())

        is_market = trading_order.order_type == CoreOrderType.MARKET
        tif = TimeInForce.DAY
        coid = str(trading_order.id)

        if len(legs) == 1:
            leg = legs[0]
            req_kwargs = dict(
                symbol=leg.contract_symbol,
                qty=trading_order.quantity,
                side=_to_side(leg.side),
                time_in_force=tif,
                client_order_id=coid,
            )
            intent = _to_intent(leg.position_intent)
            if intent is not None:
                req_kwargs["position_intent"] = intent
            if is_market:
                return MarketOrderRequest(**req_kwargs)
            return LimitOrderRequest(limit_price=trading_order.limit_price, **req_kwargs)

        # Multi-leg (2-4 legs) -> MLEG
        leg_requests = [
            OptionLegRequest(
                symbol=lg.contract_symbol,
                ratio_qty=lg.ratio_qty,
                side=_to_side(lg.side),
                position_intent=_to_intent(lg.position_intent),
            )
            for lg in legs
        ]
        req_kwargs = dict(
            qty=trading_order.quantity,
            order_class=OrderClass.MLEG,
            legs=leg_requests,
            time_in_force=tif,
            client_order_id=coid,
        )
        if is_market:
            return MarketOrderRequest(**req_kwargs)
        return LimitOrderRequest(limit_price=trading_order.limit_price, **req_kwargs)

    @alpaca_api_retry
    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        """Submit an option order to Alpaca and write the broker response back
        onto the already-persisted parent (and, for multi-leg, the leg children).

        The OptionsAccountInterface.submit_option_order wrapper has already
        persisted the parent TradingOrder (and leg children for multi-leg) and
        created a Transaction, and wraps this call in a try/except that marks
        the parent ERROR on exception. So here we build the request, submit it,
        write the broker ids/status back, persist, and return the parent.
        Genuine errors are allowed to propagate.
        """
        req = self._build_option_order_request(trading_order, legs)
        alpaca_order = self.client.submit_order(req)
        logger.info(
            f"Successfully submitted option order to Alpaca: "
            f"broker_order_id={alpaca_order.id}, legs={len(legs)}"
        )

        # Invalidate balance cache - a submitted order changes buying power.
        self.invalidate_balance_cache()

        # Reuse the equity path's status mapping so we don't hand-roll a partial map.
        result_order = self.alpaca_order_to_tradingorder(alpaca_order)

        trading_order.broker_order_id = str(alpaca_order.id) if alpaca_order.id else None
        if result_order.status:
            trading_order.status = result_order.status
        if result_order.filled_qty is not None:
            trading_order.filled_qty = result_order.filled_qty

        broker_legs = getattr(alpaca_order, "legs", None)
        if broker_legs:
            trading_order.legs_broker_ids = [str(l.id) for l in broker_legs]

        update_instance(trading_order)
        logger.info(
            f"Updated option order {trading_order.id} in database: "
            f"broker_order_id={trading_order.broker_order_id}, status={trading_order.status}"
        )

        # Multi-leg: match each persisted child to its broker leg and write back.
        if leg_orders and broker_legs:
            remaining = list(broker_legs)
            for idx, child in enumerate(leg_orders):
                matched = None
                # Prefer matching by contract symbol.
                for bl in remaining:
                    if getattr(bl, "symbol", None) == child.contract_symbol:
                        matched = bl
                        break
                # Fall back to positional matching.
                if matched is None and idx < len(remaining):
                    matched = remaining[idx]
                if matched is None:
                    logger.warning(
                        f"Could not match child option leg {child.id} "
                        f"({child.contract_symbol}) to a broker leg"
                    )
                    continue
                remaining.remove(matched)
                child.broker_order_id = str(matched.id) if matched.id else None
                child_result = self.alpaca_order_to_tradingorder(matched)
                if child_result.status:
                    child.status = child_result.status
                if child_result.filled_qty is not None:
                    child.filled_qty = child_result.filled_qty
                update_instance(child)
                logger.debug(
                    f"Updated option leg child {child.id}: "
                    f"broker_order_id={child.broker_order_id}, status={child.status}"
                )

        return trading_order

    def close_option_position(self, position, order_type="limit", limit_price=None):
        from ba2_trade_platform.core.option_types import OptionLeg
        from ba2_trade_platform.core.types import OrderDirection
        close_side = OrderDirection.SELL if position.side == OrderDirection.BUY else OrderDirection.BUY
        intent = "sell_to_close" if position.side == OrderDirection.BUY else "buy_to_close"
        leg = OptionLeg(
            contract_symbol=position.contract_symbol, side=close_side, position_intent=intent,
            option_type=position.option_type, strike=position.strike, expiry=position.expiry,
            underlying=position.underlying,
        )
        return self.submit_option_order(
            [leg], int(position.quantity), order_type, limit_price, option_strategy="close")

    # ------------------------------------------------------------------
    # Option assignment / exercise / expiry reconciliation (Phase C2)
    # ------------------------------------------------------------------
    @alpaca_api_retry
    def get_option_activities(self, after: "datetime | None" = None,
                              activity_types=("OPASN", "OPEXC", "OPEXP", "OPCSH")) -> List[dict]:
        """Fetch raw broker option lifecycle activities (assignment/exercise/
        expiry/cash-settle) from the Trading API ``/v2/account/activities``.

        The alpaca-py ``TradingClient`` does NOT wrap the multi-type activities
        endpoint, so we use its inherited REST ``get(path, data)`` helper
        directly (same mechanism already used by ``get_dividends`` /
        ``get_balance_history``). Returns a list of raw activity dicts. Network
        failures are logged and an empty list returned (never raises).

        NOT unit-tested against the network; ``reconcile_option_assignments`` is
        the tested core that consumes these dicts.
        """
        if not self._check_authentication():
            return []

        try:
            params: Dict[str, Any] = {
                "activity_types": ",".join(activity_types),
            }
            if after is not None:
                params["after"] = after.isoformat() if hasattr(after, "isoformat") else str(after)

            raw = self.client.get("/account/activities", params)
            if raw is None:
                return []
            if not isinstance(raw, list):
                raw = [raw]
            logger.debug(f"[Account {self.id}] Retrieved {len(raw)} option activities")
            return raw
        except Exception as e:
            logger.error(f"[Account {self.id}] Error fetching option activities: {e}", exc_info=True)
            return []

    def _find_option_order_for_contract(self, occ: str) -> "Optional[TradingOrder]":
        """Most recent filled/open OPTION TradingOrder for this contract on this
        account, used to attribute the activity to its originating expert."""
        from ...core.types import AssetClass
        with get_db() as session:
            stmt = (
                select(TradingOrder)
                .where(TradingOrder.account_id == self.id)
                .where(TradingOrder.asset_class == AssetClass.OPTION)
                .where(TradingOrder.contract_symbol == occ)
                .order_by(TradingOrder.id.desc())
            )
            return session.exec(stmt).first()

    def reconcile_option_assignments(self, activities: List[dict]) -> List[dict]:
        """Idempotently reconcile broker option lifecycle activities against the
        local Transaction ledger.

        For each activity dict (keys: id, activity_type, symbol [OCC], qty,
        price, ...):

        - IDEMPOTENCY: skip if an ``OptionActivity`` row already exists for
          (account_id, activity_id). Each handled activity records exactly one
          ``OptionActivity`` audit row, so re-running the same batch is a no-op.
        - The originating option ``TradingOrder`` (asset_class OPTION, matching
          contract_symbol on this account) provides attribution to the expert
          via its transaction's ``expert_id``.

        Handled activity types (documented semantics):
        - OPASN on a SHORT PUT  (order side SELL, right PUT): cash-secured put
          assigned -> open an equity LONG Transaction (qty = 100 * contracts,
          open_price = strike, expert-attributed, meta_data.origin=
          "csp_assignment"); close the short-put option Transaction
          (close_reason="assigned").
        - OPASN on a SHORT CALL (order side SELL, right CALL): shares called away
          -> close the expert's OPENED equity long for the underlying
          (close_reason="called_away", close_price=strike); close the
          short-call option Transaction (close_reason="assigned"). If no equity
          long is found, record result "called_away_no_long" (still closes the
          option leg).
        - OPEXP (expiry): close the option Transaction (close_reason="expired",
          close_price=0.0).
        - OPEXC (exercise): close the option Transaction (close_reason=
          "exercised"). Equity-leg reconciliation is best-effort/logged for now.
        - Anything else (e.g. OPCSH) or any malformed/unmappable activity:
          recorded with result "unhandled: ..." and skipped (never crashes the
          batch).

        Returns a list of per-activity result dicts.
        """
        from ...core.models import OptionActivity
        from ...core.types import AssetClass, OptionRight, OrderDirection, TransactionStatus

        results: List[dict] = []

        for activity in activities:
            activity_id = activity.get("id")
            activity_type = activity.get("activity_type")
            symbol = activity.get("symbol")

            # Skip activities without an id - we cannot dedup them safely.
            if not activity_id:
                logger.warning(
                    f"[Account {self.id}] Option activity missing id; skipping: {activity}")
                results.append({"activity_id": None, "result": "skipped_no_id"})
                continue

            try:
                qty = float(activity.get("qty")) if activity.get("qty") is not None else None
            except (TypeError, ValueError):
                qty = None
            try:
                price = float(activity.get("price")) if activity.get("price") is not None else None
            except (TypeError, ValueError):
                price = None

            # IDEMPOTENCY: skip if already processed for this account.
            try:
                with get_db() as session:
                    existing = session.exec(
                        select(OptionActivity)
                        .where(OptionActivity.account_id == self.id)
                        .where(OptionActivity.activity_id == str(activity_id))
                    ).first()
                if existing is not None:
                    logger.debug(
                        f"[Account {self.id}] Option activity {activity_id} already "
                        f"processed; skipping (idempotent).")
                    results.append({"activity_id": activity_id, "result": "already_processed"})
                    continue
            except Exception as e:
                logger.error(
                    f"[Account {self.id}] Idempotency check failed for {activity_id}: {e}",
                    exc_info=True)
                # Fail closed: do not apply effects we can't dedup.
                results.append({"activity_id": activity_id, "result": f"unhandled: idempotency_check_failed: {e}"})
                continue

            result_str = "unhandled: unknown"
            try:
                result_str = self._apply_option_activity(
                    activity_id=str(activity_id),
                    activity_type=activity_type,
                    symbol=symbol,
                    qty=qty,
                    price=price,
                )
            except Exception as e:
                # Never crash the batch on a single bad activity.
                logger.error(
                    f"[Account {self.id}] Failed to reconcile option activity "
                    f"{activity_id} ({activity_type} {symbol}): {e}", exc_info=True)
                result_str = f"unhandled: exception: {e}"

            # Always record an audit/idempotency row, even for unhandled ones.
            try:
                add_instance(OptionActivity(
                    account_id=self.id,
                    activity_id=str(activity_id),
                    activity_type=str(activity_type) if activity_type is not None else "",
                    symbol=symbol,
                    qty=qty,
                    price=price,
                    result=result_str,
                ))
            except Exception as e:
                logger.error(
                    f"[Account {self.id}] Failed to persist OptionActivity audit row "
                    f"for {activity_id}: {e}", exc_info=True)

            results.append({"activity_id": activity_id, "result": result_str})

        return results

    def _apply_option_activity(self, activity_id: str, activity_type, symbol,
                               qty, price) -> str:
        """Apply the effect of a single (not-yet-processed) option activity to
        the Transaction ledger. Returns a result string. Raises on truly
        unexpected errors (caught by the caller). Returns an "unhandled: ..."
        string for expected-but-unmappable inputs (malformed symbol, etc.)."""
        from ...core.types import AssetClass, OptionRight, OrderDirection, TransactionStatus

        contracts = qty if qty is not None else 0.0

        # Parse OCC symbol; malformed -> unhandled (no crash).
        if not symbol:
            return "unhandled: missing symbol"
        try:
            underlying, expiry, right, strike = self._parse_occ_symbol(symbol)
        except Exception as e:
            return f"unhandled: bad OCC symbol {symbol!r}: {e}"

        # Locate the originating option order + its transaction (attribution).
        opt_order = self._find_option_order_for_contract(symbol)
        opt_txn = None
        expert_id = None
        order_side = None
        if opt_order is not None:
            order_side = opt_order.side
            if opt_order.transaction_id is not None:
                opt_txn = get_instance(Transaction, opt_order.transaction_id)
                if opt_txn is not None:
                    expert_id = opt_txn.expert_id

        atype = str(activity_type).upper() if activity_type is not None else ""

        # --- OPASN: assignment ---
        if atype == "OPASN":
            # Short option assigned. Determine PUT vs CALL from the contract.
            is_short = (order_side == OrderDirection.SELL)
            if right == OptionRight.PUT and is_short:
                # Cash-secured put assigned -> we BUY 100*contracts shares @ strike.
                share_qty = 100.0 * contracts
                equity_txn = Transaction(
                    symbol=underlying,
                    quantity=share_qty,
                    side=OrderDirection.BUY,
                    open_price=strike,
                    status=TransactionStatus.OPENED,
                    open_date=datetime.now(timezone.utc),
                    expert_id=expert_id,
                    close_reason=None,
                    meta_data={"origin": "csp_assignment", "activity_id": activity_id,
                               "contract": symbol},
                )
                add_instance(equity_txn)
                self._close_txn(opt_txn, close_reason="assigned")
                return (f"csp_assignment: opened equity long {underlying} "
                        f"{share_qty}@{strike}; closed short put txn")

            if right == OptionRight.CALL and is_short:
                # Short call assigned -> shares called away. Close held equity long.
                held = self._find_open_equity_long(underlying, expert_id)
                self._close_txn(opt_txn, close_reason="assigned")
                if held is not None:
                    self._close_txn(held, close_reason="called_away", close_price=strike)
                    return (f"called_away: closed equity long {underlying} @ {strike}; "
                            f"closed short call txn")
                logger.warning(
                    f"[Account {self.id}] Short CALL {symbol} assigned but no OPENED "
                    f"equity long found for {underlying} (expert {expert_id}).")
                return "called_away_no_long: closed short call txn, no equity long to close"

            # Long option assigned is not a normal flow; record + log.
            logger.warning(
                f"[Account {self.id}] OPASN on non-short option {symbol} "
                f"(order_side={order_side}, right={right}); recording without ledger change.")
            return f"unhandled: OPASN on non-short option (side={order_side}, right={right})"

        # --- OPEXP: expiry ---
        if atype == "OPEXP":
            if opt_txn is not None:
                self._close_txn(opt_txn, close_reason="expired", close_price=0.0)
                return "expired: closed option txn"
            return "unhandled: expiry with no matching option txn"

        # --- OPEXC: exercise ---
        if atype == "OPEXC":
            if opt_txn is not None:
                self._close_txn(opt_txn, close_reason="exercised")
                # Equity-leg handling for exercise is best-effort/minimal for now.
                return "exercised: closed option txn (equity leg not reconciled)"
            return "unhandled: exercise with no matching option txn"

        # --- OPCSH / anything else: no specific handler ---
        return f"unhandled: no handler for activity_type {atype!r}"

    def _find_open_equity_long(self, underlying: str, expert_id):
        """Find an OPENED equity LONG (side BUY) Transaction for the underlying.

        When ``expert_id`` is known (the usual case - the short call was written
        by an expert) the search is RESTRICTED to that expert's transactions:
        ``Transaction`` has no ``account_id`` column, so expert attribution is
        the only scoping that prevents closing another account's/expert's long.
        Only when the option was unattributed (``expert_id`` is None) do we fall
        back to the most recent unattributed open long. Returns None if none.
        """
        from ...core.types import OrderDirection, TransactionStatus
        with get_db() as session:
            stmt = (
                select(Transaction)
                .where(Transaction.symbol == underlying)
                .where(Transaction.side == OrderDirection.BUY)
                .where(Transaction.status == TransactionStatus.OPENED)
                .order_by(Transaction.id.desc())
            )
            if expert_id is not None:
                stmt = stmt.where(Transaction.expert_id == expert_id)
            else:
                stmt = stmt.where(Transaction.expert_id.is_(None))
            return session.exec(stmt).first()

    def _close_txn(self, txn, close_reason: str, close_price=None) -> None:
        """Close a Transaction (set CLOSED + reason + optional close_price + date)
        and persist. No-op if txn is None."""
        from ...core.types import TransactionStatus
        if txn is None:
            return
        txn.status = TransactionStatus.CLOSED
        txn.close_reason = close_reason
        if close_price is not None:
            txn.close_price = close_price
        if not txn.close_date:
            txn.close_date = datetime.now(timezone.utc)
        update_instance(txn)

