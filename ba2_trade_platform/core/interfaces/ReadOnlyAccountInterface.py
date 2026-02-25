from abc import abstractmethod
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone
from threading import Lock
from ba2_trade_platform.logger import logger
from ...core.models import AccountSetting
from .ExtendableSettingsInterface import ExtendableSettingsInterface


class ReadOnlyAccountInterface(ExtendableSettingsInterface):
    """
    Abstract base class for read-only account interfaces.

    This class provides the read-only portion of the account interface:
    balance/position/order queries, price fetching with caching, and
    refresh operations. It does NOT include any trading (order submission,
    cancellation, modification, TP/SL management).

    Subclass this directly for read-only broker integrations (e.g., TastyTrade).
    For trading-capable accounts, subclass AccountInterface instead (which inherits this).
    """
    SETTING_MODEL = AccountSetting
    SETTING_LOOKUP_FIELD = "account_id"

    # Whether this account supports trading operations
    supports_trading = False

    # Class-level price cache shared across all instances
    # Structure: {account_id: {symbol: {'price': float, 'timestamp': datetime, 'fetching': bool}}}
    _GLOBAL_PRICE_CACHE: Dict[int, Dict[str, Dict[str, Any]]] = {}
    _CACHE_LOCK = Lock()  # Thread-safe access to cache structure

    # Per-symbol locks to prevent duplicate API calls for the same symbol
    # Structure: {(account_id, symbol): Lock}
    _SYMBOL_LOCKS: Dict[tuple, Lock] = {}
    _SYMBOL_LOCKS_LOCK = Lock()  # Lock for managing the locks dict itself

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
    def symbols_exist(self, symbols: List[str]) -> Dict[str, bool]:
        """
        Check if multiple symbols exist and are tradeable on this account's broker.

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
            from ..models import TradingOrder, Transaction
            from ..types import OrderStatus, OrderDirection, OrderType, TransactionStatus
            from ..db import update_instance

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
                    market_entry_orders = [o for o in orders if not o.depends_on_order]
                    dependent_orders = [o for o in orders if o.depends_on_order]

                    # Check if any market entry order is filled (to open transaction)
                    has_filled_entry_order = any(order.status in executed_statuses for order in market_entry_orders)

                    # Check if all MARKET ENTRY orders are in terminal state
                    all_entry_orders_terminal = (
                        len(market_entry_orders) > 0 and
                        all(order.status in terminal_statuses for order in market_entry_orders)
                    )

                    # Check if ALL orders are in terminal states
                    terminal_statuses = OrderStatus.get_terminal_statuses()
                    all_orders_terminal = (
                        len(orders) > 0 and
                        all(order.status in terminal_statuses for order in orders)
                    )

                    # Check if we have a filled closing order (dependent order that closes position)
                    filled_closing_orders = [o for o in dependent_orders if o.status == OrderStatus.FILLED]

                    # Sum ALL filled buy and sell orders to determine position quantity
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

                    # Calculate remaining quantity based on transaction side
                    if transaction.side == OrderDirection.SELL:
                        calculated_quantity = total_filled_sell - total_filled_buy
                    else:  # BUY (LONG)
                        calculated_quantity = total_filled_buy - total_filled_sell

                    # If buy and sell orders match, position is closed
                    position_balanced = abs(total_filled_buy - total_filled_sell) < 0.0001

                    # Update transaction quantity if different
                    if calculated_quantity != 0 and transaction.quantity != calculated_quantity:
                        if calculated_quantity < 0:
                            logger.error(
                                f"NEGATIVE calculated_quantity in sync_transaction_orders: {calculated_quantity} "
                                f"for transaction {transaction.id} ({transaction.symbol}, side={transaction.side}), "
                                f"total_filled_buy={total_filled_buy}, total_filled_sell={total_filled_sell}. "
                                f"Using abs() as safety measure.",
                                exc_info=True
                            )
                            calculated_quantity = abs(calculated_quantity)
                        transaction.quantity = calculated_quantity
                        has_changes = True
                        logger.debug(f"Transaction {transaction.id} quantity updated to {calculated_quantity}")

                    # Update transaction status based on order states
                    new_status = None

                    # Update open_price from the oldest filled market entry order
                    filled_entry_orders = [
                        order for order in market_entry_orders
                        if order.status in executed_statuses and order.open_price
                    ]
                    if filled_entry_orders:
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

                    # Update close_price from filled closing orders
                    if filled_closing_orders:
                        closing_order = filled_closing_orders[0]
                        if closing_order.open_price and transaction.close_price != closing_order.open_price:
                            transaction.close_price = closing_order.open_price
                            has_changes = True
                            logger.debug(f"Transaction {transaction.id} close_price updated to {closing_order.open_price} from filled closing order {closing_order.id}")

                    # OPENED -> CLOSED: If at least one OCO leg is filled
                    oco_leg_filled = False
                    for dep_order in dependent_orders:
                        if (dep_order.status == OrderStatus.FILLED and
                            ("OCO-" in (dep_order.comment or "") or dep_order.order_type == OrderType.OCO)):
                            oco_leg_filled = True
                            logger.debug(f"Transaction {transaction.id} has filled OCO leg: {dep_order.id} ({dep_order.comment})")
                            break

                    if oco_leg_filled and transaction.status == TransactionStatus.OPENED:
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

                    # ANY STATUS -> CLOSED: If all orders are in terminal state
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

                    # OPENED -> CLOSED: If filled buy and sell orders sum to match quantity
                    elif position_balanced and transaction.status != TransactionStatus.CLOSED and (total_filled_buy > 0 or total_filled_sell > 0):
                        filled_orders = [o for o in orders if o.status in executed_statuses and o.open_price]
                        if filled_orders:
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
                    elif all_entry_orders_terminal and transaction.status == TransactionStatus.OPENED and not filled_closing_orders:
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
                        session.add(transaction)
                        updated_count += 1

                session.commit()

            logger.info(f"Successfully refreshed transactions for account {self.id}: {updated_count} transactions updated")
            return True

        except Exception as e:
            logger.error(f"Error refreshing transactions for account {self.id}: {e}", exc_info=True)
            return False

    @abstractmethod
    def get_dividends(self, symbol: Optional[str] = None, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> List[Dict]:
        """
        Get dividend history for this account.

        Args:
            symbol: Optional symbol to filter by. If None, returns dividends for all symbols.
            start_date: Optional start date filter.
            end_date: Optional end date filter.

        Returns:
            List[Dict]: List of dividend records, each containing:
                - symbol (str): The stock symbol
                - amount (float): Dividend amount in account currency
                - date (datetime): Date the dividend was received
                - drip_quantity (float | None): Number of shares reinvested via DRIP, if applicable
                - drip_price (float | None): Price per share for DRIP reinvestment, if applicable
        """
        pass

    @abstractmethod
    def get_balance_history(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> List[Dict]:
        """
        Get historical balance/equity data for the account.

        Args:
            start_date: Optional start date filter.
            end_date: Optional end date filter.

        Returns:
            List[Dict]: List of balance snapshots, each containing:
                - date (datetime): Snapshot date
                - net_liquidating_value (float): Total account value
                - cash_balance (float): Cash portion
                - equity_value (float): Equity/positions portion
        """
        pass

    @abstractmethod
    def is_drip_enabled(self) -> bool:
        """
        Check if Dividend Reinvestment Plan (DRIP) is enabled for this account.

        Returns:
            bool: True if DRIP is enabled, False otherwise
        """
        pass
