from abc import abstractmethod
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta, date
from ba2_common.core.account_types import (
    MARKET_HOURS_SOURCE_UNAVAILABLE,
    AccountSnapshot,
    CashTransfer,
    MarginInfo,
    MarketHours,
)
from threading import Lock
from ba2_common.logger import logger
from ba2_common.core.models import AccountSetting
from ba2_common.core.interfaces.ExtendableSettingsInterface import ExtendableSettingsInterface


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

    _builtin_settings: Dict[str, Any] = {}

    @classmethod
    def _ensure_builtin_settings(cls):
        """Ensure builtin settings are initialized for account classes."""
        if not cls._builtin_settings:
            cls._builtin_settings = {
                "minimum_equity_threshold_percent": {
                    "type": "float",
                    "required": False,
                    "default": 5.0,
                    "description": "Minimum equity threshold (%)",
                    "tooltip": "Minimum percentage of account balance that must remain available across all experts before new positions are blocked. This is an account-wide safety net."
                },
                "manual_trading_enabled": {
                    "type": "bool",
                    "required": False,
                    "default": False,
                    "description": "Manually traded account",
                    "tooltip": "Enable the Portfolio Allocation page for this account. Only for accounts you trade by hand -- the page refuses to run when the account has any enabled expert."
                },
            }

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
        self._ensure_builtin_settings()
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

    def get_account_snapshot(self) -> AccountSnapshot:
        """Broker-agnostic view of this account's cash / equity / buying power.

        CONCRETE ON PURPOSE: an ``@abstractmethod`` here would break every
        existing subclass's instantiation (IBKRAccount, TastyTradeAccount).

        The base implementation reads ``get_account_info()`` TOLERANTLY, in the
        manner of ``MarketExpertInterface._get_actual_available_balance``
        (MarketExpertInterface.py:815): the return may be a pydantic object
        (Alpaca ``TradeAccount``), a dict (IBKR, TastyTrade) or ``None`` (Alpaca
        auth failure), so every field is probed with a
        ``obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)``
        helper and coerced with ``float()`` (Alpaca ships them as STRINGS).
        Alpaca and TastyTrade override this properly.

        NEVER fabricates a number: a field the broker did not supply is left as
        ``None`` and the caller must raise rather than substitute a default
        (platform rule: no fallback values for prices/balances/quantities).

        Two AccountSnapshot contract rules are enforced HERE so no adapter has to
        remember them (TastyTrade breaks both if left to itself):

          * ``equity`` and ``net_liquidation`` are mirrored when the broker
            publishes only one of them -- the contract says a broker with a
            single number MUST set both;
          * ``short_market_value`` is forced NEGATIVE, because a broker may
            publish a positive magnitude and gross exposure has to be one
            formula for every broker.

        Neither invents a value: both only act on what the broker did publish.

        Returns:
            AccountSnapshot: populated as far as the broker allows. An
            all-``None`` snapshot is a legitimate "the broker told us nothing"
            result, NOT an error -- it is the caller that must refuse to plan.
        """
        try:
            info = self.get_account_info()
        except Exception as e:
            logger.error(f"Account {self.id}: get_account_info() failed: {e}", exc_info=True)
            info = None

        if info is None:
            return AccountSnapshot()

        def _field(obj: Any, name: str) -> Optional[float]:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        def _first(*names: str) -> Optional[float]:
            """First of these field names the broker actually publishes."""
            for n in names:
                v = _field(info, n)
                if v is not None:
                    return v
            return None

        multiplier = _first("multiplier", "margin_multiplier")

        # AccountSnapshot's contract: a broker that publishes only ONE of
        # equity / net_liquidation MUST have BOTH set to it. The two chains do not
        # cover the same names (a broker publishing only `portfolio_value` matched
        # the equity chain and left net_liquidation None -- and net_liquidation is
        # the headline total value), so mirror whichever one was found. Only a hole
        # is filled: where the broker publishes both they legitimately diverge.
        equity = _first("equity", "net_liquidating_value", "portfolio_value")
        net_liquidation = _first("net_liquidation", "net_liquidating_value", "equity")
        if equity is None:
            equity = net_liquidation
        elif net_liquidation is None:
            net_liquidation = equity

        # short_market_value is NEGATIVE while shorts are held (the Alpaca
        # convention, so gross exposure is one formula everywhere). A broker that
        # publishes a positive MAGNITUDE instead -- TastyTrade's
        # `short-equity-value` -- is normalised here rather than in every adapter.
        short_market_value = _first("short_market_value")
        if short_market_value is not None and short_market_value > 0:
            short_market_value = -short_market_value

        return AccountSnapshot(
            cash=_first("cash", "cash_balance"),
            equity=equity,
            net_liquidation=net_liquidation,
            buying_power=_first("buying_power", "equity_buying_power", "derivative_buying_power"),
            non_marginable_buying_power=_first("non_marginable_buying_power",
                                               "cash_available_to_withdraw"),
            margin_multiplier=multiplier,
            is_margin_account=bool(multiplier is not None and multiplier > 1.0),
            long_market_value=_first("long_market_value"),
            short_market_value=short_market_value,
            pending_transfer_in=_first("pending_transfer_in"),
            supports_fractional=False,
            raw=dict(info) if isinstance(info, dict) else {},
        )

    def get_cash_transfers(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[CashTransfer]:
        """Cash movements (deposits, withdrawals, dividends) over a date window.

        CONCRETE, returns ``[]`` by default so no existing broker breaks. Alpaca
        overrides it from the ``CSD``/``CSW`` activity endpoint that
        ``get_balance_history`` already calls inline (AlpacaAccount.py:4376-4382)
        plus the existing ``get_dividends()``; TastyTrade from
        ``get_history(types=["Money Movement"], page_offset=None)``.

        ``CashTransfer.external_id`` MUST be the broker's own activity id: it is
        the ``(account_id, external_id)`` idempotency key of
        ``portfolio_income_event``, so re-syncing the same window upserts rather
        than duplicating -- exactly as ``OptionActivity`` does.

        Args:
            start_date: inclusive lower bound; ``None`` means "broker default".
                A ``datetime`` is accepted (``date`` is its supertype).
            end_date: inclusive upper bound; ``None`` means "up to now".

        Returns:
            List[CashTransfer]: empty when the broker has none OR when the broker
            does not implement this seam. Unlike ``get_positions()``, this seam
            does NOT distinguish failure from emptiness -- an implementation that
            fails must log the error and return ``[]``.
        """
        return []

    def get_symbol_margin_info(self, symbols: List[str]) -> Dict[str, MarginInfo]:
        """Per-symbol margin / fractionability metadata, for buying-power sizing.

        CONCRETE, returns ``{}`` by default. Alpaca derives each entry from
        ``Asset.marginable``, ``Asset.maintenance_margin_requirement``,
        ``Asset.fractionable``, ``Asset.min_order_size`` and
        ``Asset.min_trade_increment`` combined with ``TradeAccount.multiplier``.

        ``bp_factor = initial_margin_rate * account_multiplier`` -- the dollars of
        buying power one dollar of notional consumes. A fully marginable stock in
        a 2:1 account is ``0.5 * 2 = 1.0``; a non-marginable one is ``1.0 * 2 = 2.0``.

        MIND THE UNITS on the three minimums. ``min_order_size`` and
        ``min_trade_increment`` are SHARE quantities; ``min_fractional_notional``
        is DOLLARS and binds FRACTIONAL orders only. An adapter that publishes a
        money floor as a share count mis-sizes by the price of the symbol, in the
        direction of never trading, and nothing raises. Alpaca has only the share
        minimums; TastyTrade has only the money one (its $5 fractional floor).

        Args:
            symbols: symbols to describe, already normalised (.strip().upper()).

        Returns:
            Dict[str, MarginInfo]: keyed by symbol. A symbol the broker cannot
            describe is OMITTED, never defaulted here -- the caller falls back to
            the conservative ``bp_factor = account multiplier`` (assume no
            leverage), which under-deploys rather than over-committing.
        """
        return {}

    #: Upper bound on how stale a cached market-status answer may be, in seconds.
    #: A CLASS attribute so a bare instance built with object.__new__ (the test
    #: idiom) still has it. Short on purpose: this is a page-level de-duplicator --
    #: the wizard asks once per render -- not a day-level cache. The real
    #: invalidation is the session BOUNDARY below.
    _MARKET_HOURS_CACHE_TTL = 60

    #: ``(asked_at, answer)``. Declared on the CLASS so a bare instance has it, but
    #: only ever REBOUND on the instance -- never mutated in place -- so there is no
    #: shared mutable state between accounts (contrast the per-instance dict in
    #: AlpacaAccount._margin_info_cache, which is built in __init__).
    _market_hours_cache: Optional[Tuple[datetime, MarketHours]] = None

    def get_market_hours(self, *, now: Optional[datetime] = None) -> MarketHours:
        """Whether this account's market is trading, and when that next changes.

        CONCRETE and effectively FINAL: it owns argument validation and the cache.
        **DO NOT OVERRIDE THIS.** Override ``_get_market_hours_impl`` instead --
        the same template/impl split as ``submit_order`` / ``_submit_order_impl``
        (overriding the template with a different shape is what disabled
        IBKRAccount; see IBKRAccount.py:27-40). An adapter that overrides this
        method loses the boundary-expiry cache and, if it then calls
        ``super().get_market_hours()`` from its ``_get_market_hours_impl``,
        recurses forever.

        CACHING. An entry is reused only while BOTH hold: it is younger than
        ``_MARKET_HOURS_CACHE_TTL``, and ``now`` has not yet reached the answer's
        own ``next_transition`` -- i.e. it expires at
        ``min(now + TTL, next_transition)``. A plain elapsed-seconds TTL -- the
        shape AlpacaAccount._margin_info_cache uses for asset metadata -- is WRONG
        here: an entry taken at 15:59:30 would still claim the market is open at
        16:00:15. ``clear_market_hours_cache()`` is the explicit path.

        An answer whose ``source`` is ``MARKET_HOURS_SOURCE_UNAVAILABLE`` is
        **never cached**. A failure is not a fact: caching one would keep the
        wizard blocked for a full TTL after the broker recovered, and this seam is
        called once per page render, not in a loop, so there is no storm to fear.
        Broker and fallback answers ARE cached.

        FAILS CLOSED. Any exception out of the implementation is logged and
        becomes ``MarketHours(is_open=False, source=..._UNAVAILABLE, detail=...)``;
        use ``MarketHours.is_known`` to tell "shut" from "we could not find out".
        This method NEVER raises and never propagates.

        Args:
            now: the instant to describe; must be timezone-aware. Defaults to
                ``datetime.now(timezone.utc)``. Passing it explicitly is THE way
                tests freeze time -- preferred over monkeypatching an adapter's
                ``_utcnow``/``_now_eastern`` -- and it also guarantees the age
                check and the boundary check below read ONE clock and so cannot
                disagree.

        Returns:
            MarketHours: never ``None``.

        Raises:
            ValueError: when ``now`` is naive. Read as UTC instead of Eastern, a
                naive instant moves the boundary by four or five hours. This is
                the ONE thing this method raises, and it is a programming error,
                not a runtime condition.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError(
                "get_market_hours(now=...) requires a timezone-aware datetime; a "
                "naive one would silently shift the 09:30/16:00 ET boundaries")

        entry = self._market_hours_cache
        if entry is not None:
            asked_at, cached = entry
            age = now - asked_at
            transition = cached.next_transition
            if (timedelta(0) <= age < timedelta(seconds=self._MARKET_HOURS_CACHE_TTL)
                    and (transition is None or now < transition)):
                return cached
            self._market_hours_cache = None

        try:
            hours = self._get_market_hours_impl(now)
        except Exception as e:
            logger.error(
                f"[Account {self.id}] Could not determine market hours: {e}", exc_info=True)
            hours = MarketHours(
                is_open=False, source=MARKET_HOURS_SOURCE_UNAVAILABLE, as_of=now,
                detail=f"Market hours could not be determined: {e}")

        # A failure is retried on the very next call; a real answer is cached until
        # the earlier of the TTL and the session boundary.
        self._market_hours_cache = (now, hours) if hours.is_known else None
        return hours

    def _get_market_hours_impl(self, now: datetime) -> MarketHours:
        """Broker-specific market status. THIS IS THE OVERRIDE POINT.

        The default is the offline, holiday- and half-day-aware NYSE
        REGULAR-session calendar (``ba2_common.core.market_calendar``), so a
        broker that implements nothing still gets a correct answer for a US
        equities account. It reports closed during extended hours. If the calendar
        package itself is unusable this degrades to
        ``MARKET_HOURS_SOURCE_UNAVAILABLE`` -- NOT to an ``is_open=False``
        fallback, because fetch-failed is not the same as closed.

        RULES FOR AN OVERRIDE:
          * override THIS method and nothing else. Never override
            ``get_market_hours`` or ``is_market_open``.
          * USE the ``now`` you are given. Do not read the wall clock, ``_utcnow()``
            or ``self._now_eastern()`` inside here -- ``now=`` is the clock-freeze
            seam every test depends on.
          * do NOT cache. ``get_market_hours`` already does, with the boundary
            invariant. Do not add a TTL, a ``_market_hours_cache`` or a
            session-boundary helper to an adapter.
          * to fall back to the calendar, call
            ``dataclasses.replace(super()._get_market_hours_impl(now), detail=reason)``.
            **Never ``super().get_market_hours()``** -- that is the method that
            calls this one, so it recurses until the stack dies. And do not force
            ``source=FALLBACK`` on the result: if the calendar is dead too, the
            honest answer is UNAVAILABLE.
          * you MAY raise. ``get_market_hours`` logs it and fails closed.

        Alpaca overrides it from ``TradingClient.get_clock()`` ->
        ``Clock(timestamp, is_open, next_open, next_close)``
        (alpaca/trading/models.py:348), leaving ``open_at`` ``None`` while the
        session is live because ``Clock`` does not publish the current session's
        start, and stamping ``source=MARKET_HOURS_SOURCE_BROKER``. TastyTrade
        overrides it from the async ``get_market_sessions(session, exchanges)`` ->
        ``MarketSession(status, open_at, close_at, next_session, ...)``
        (tastytrade/market_sessions.py), bridged with ``_run_async``, carrying the
        raw broker word in ``status``.

        Args:
            now: the tz-aware instant to describe. A broker that only reports
                "right now" still stamps it into ``as_of``.

        Returns:
            MarketHours: with ``source`` set.
        """
        # Imported lazily: pandas_market_calendars parses the whole NYSE ruleset on
        # first use, and merely importing this interface must not pay for that.
        from ba2_common.core.market_calendar import (
            MarketCalendarUnavailable, nyse_market_hours)
        try:
            return nyse_market_hours(now)
        except MarketCalendarUnavailable as e:
            logger.error(
                f"[Account {self.id}] No offline NYSE calendar, so market hours are "
                f"UNKNOWN rather than closed: {e}", exc_info=True)
            return MarketHours(
                is_open=False, source=MARKET_HOURS_SOURCE_UNAVAILABLE, as_of=now,
                detail=f"No market-hours source available: {e}")

    def is_market_open(self, *, now: Optional[datetime] = None) -> bool:
        """Shorthand for ``get_market_hours(now=now).is_open``.

        CONCRETE. **DO NOT OVERRIDE** -- an adapter that overrides this can
        disagree with the banner, which reads ``get_market_hours()``.

        REGULAR session under the default implementation -- False during extended
        hours. The Portfolio Allocation wizard only SUBMITS while this is True.

        A failed lookup returns False (fail closed); call ``get_market_hours()``
        and read ``is_known`` when the caller needs to tell that apart from a
        genuine closure.
        """
        return self.get_market_hours(now=now).is_open

    def clear_market_hours_cache(self) -> None:
        """Drop the cached market-status answer so the next call refetches.

        The entry expires on its own at the earlier of the TTL and the next
        session boundary; this is the EXPLICIT path, for a user who hits Refresh
        -- the counterpart of ``AlpacaAccount.clear_margin_info_cache()``. It lives
        HERE, on the interface, so no adapter needs one of its own.
        """
        self._market_hours_cache = None
        logger.debug(f"[Account {self.id}] Cleared cached market hours")

    @abstractmethod
    def get_positions(self) -> Any:
        """
        Retrieve all current open positions in the account.

        TRI-STATE CONTRACT -- ``None`` IS NOT ``[]``. This is the ONE seam in this
        interface where a failure is reported as a distinct sentinel rather than
        an empty result, and it is load-bearing:

          * a non-empty list -- the positions the broker actually holds;
          * ``[]``           -- the fetch SUCCEEDED and the account is genuinely FLAT;
          * ``None``         -- the FETCH ITSELF FAILED (network/DNS/auth/API error).

        IMPLEMENTERS: catch your broker's errors and ``return None``. Returning
        ``[]`` on an exception is a CONTRACT VIOLATION -- it tells every caller
        "the broker holds nothing", which is the input that auto-close logic acts
        on. On 2026-07-03 a transient local DNS outage did exactly that and
        force-closed 8 real open transactions in the DB while the broker held them
        the entire time (see ``AlpacaAccount.get_positions``).

        CALLERS: never write ``for pos in (positions or [])`` in code that decides
        to CLOSE, CANCEL or SIZE something -- that idiom silently re-conflates the
        two states and reintroduces the incident. Test ``is None`` FIRST and abort
        the decision (``reconcile_externally_closed_transactions`` returns 0;
        ``close_transaction`` falls through to a retry). ``or []`` is only
        acceptable in read-only display code that must not crash.

        Returns:
            Any: A list or collection of position objects containing information such as:
                - symbol: The asset symbol
                - quantity: Position size
                - average_price: Average entry price
                - current_price: Current market price
                - unrealized_pl: Unrealized profit/loss
            or ``None`` if the fetch failed.
        """
        pass

    def get_available_position_quantity(self, symbol: str) -> float:
        """Broker-side AVAILABLE (not held-for-orders) quantity for ``symbol``.

        Used by ``TradeManager.replacement_blocked_by_qty`` to confirm a prior
        order has actually RELEASED its held quantity before a cancel-and-replace
        order (a trailing-stop raise / OCO swap) is submitted. Submitting too
        early is rejected by the broker -- Alpaca 40310000 "insufficient qty
        available" -- and the rejected order hard-ERRORs, silently leaving the
        position with NO protective stop.

        CONCRETE ON PURPOSE, and DERIVED from ``get_positions()`` -- the mandatory
        seam every broker already implements -- exactly as ``get_account_snapshot()``
        is derived from ``get_account_info()``. This method previously existed only
        on ``AlpacaAccount``, so the caller's ``except AttributeError`` branch set
        the qty to ``None`` on every other broker and the gate was a PERMANENT
        NO-OP there. A broker with a cheaper direct endpoint should override it
        (AlpacaAccount uses ``get_open_position(symbol).qty_available``, one
        targeted call instead of the whole book).

        NEVER RETURNS ``None``. ``None`` is the caller's "unknown -> do NOT block"
        value, and "we could not find out" is precisely the case that must NOT
        submit blind: an unverifiable answer is returned as ``0.0``, which DEFERS
        the replacement (it stays WAITING_TRIGGER and the next account refresh
        retries), so the degraded path is self-healing rather than a hard ERROR.

        The full table:

          * position found, broker publishes ``qty_available`` -> ``abs()`` of it
            (``abs`` because a short reports a negative quantity and the
            buy-to-cover replacement needs the magnitude);
          * position found, broker publishes NO ``qty_available`` (IBKR builds
            ``Position`` without it) -> ``abs(qty)``, the total held. The broker
            CONFIRMS it holds the shares; only the transient encumbrance is
            unknown. Reporting 0.0 here would block such a broker FOREVER, turning
            a seconds-long race into a permanent, silent absence of protection --
            strictly worse than the rejection this gate exists to avoid, which at
            least surfaces as ERROR. The gate still bites where it matters: a
            stale leg asking for more than the position's total size is deferred
            instead of submitted into a certain rejection;
          * symbol NOT in the book -> ``0.0``. A protective stop for shares the
            broker does not hold is a guaranteed rejection; deferring is right,
            and it is not a permanent hang (a position that never comes back gets
            its transaction reconciled closed, which cancels the staged leg);
          * ``get_positions()`` returned ``None`` (FETCH FAILED) or raised ->
            ``0.0``. The tri-state contract again: an unverified book is not an
            empty book and must not be acted on. Compare the 2026-07-03 incident.

        Args:
            symbol: the instrument to look up; matched case- and
                whitespace-insensitively against the broker's book.

        Returns:
            float: available quantity, always >= 0, never ``None``.
        """
        wanted = (symbol or '').strip().upper()
        try:
            positions = self.get_positions()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[Account {self.id}] get_available_position_quantity({symbol}): "
                f"position fetch raised ({e}); reporting 0 available (defer, retry next refresh)"
            )
            return 0.0
        if positions is None:
            logger.warning(
                f"[Account {self.id}] get_available_position_quantity({symbol}): "
                f"get_positions() returned None (FETCH FAILURE, not a flat account); "
                f"reporting 0 available (defer, retry next refresh)"
            )
            return 0.0

        for pos in positions:
            pos_symbol = (pos.get('symbol') if isinstance(pos, dict)
                          else getattr(pos, 'symbol', None))
            if (pos_symbol or '').strip().upper() != wanted:
                continue
            for field in ('qty_available', 'qty'):
                raw = (pos.get(field) if isinstance(pos, dict)
                       else getattr(pos, field, None))
                if raw is None:
                    continue
                try:
                    return abs(float(raw))
                except (TypeError, ValueError):
                    logger.warning(
                        f"[Account {self.id}] position {wanted}.{field}={raw!r} is not numeric"
                    )
            logger.warning(
                f"[Account {self.id}] position {wanted} publishes no usable quantity; "
                f"reporting 0 available (defer, retry next refresh)"
            )
            return 0.0

        # Broker holds nothing in this symbol. Logged rather than silent: this
        # DEFERS the replacement every refresh, and while that is self-limiting
        # (a position that never returns has its transaction reconciled closed,
        # which cancels the staged leg) a repeating line is what makes a genuine
        # hang visible instead of a stop that quietly never appears.
        logger.warning(
            f"[Account {self.id}] get_available_position_quantity({symbol}): broker's book "
            f"holds no position for this symbol; reporting 0 available (defer, retry next refresh)"
        )
        return 0.0

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
        from ba2_common import config

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
                        return cached_data['price']

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
                        else:
                            # Cache expired
                            symbols_to_fetch.append(symbol)
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
            from contextlib import nullcontext
            from sqlmodel import select, Session
            from ba2_common.core.db import get_db
            from ba2_common.core.models import TradingOrder, Transaction
            from ba2_common.core.types import OrderStatus, OrderDirection, OrderType, TransactionStatus, AssetClass
            from ba2_common.core.db import update_instance, delete_instance
            from ba2_common.core import trade_store as _ts

            # Get terminal and executed order states from OrderStatus
            terminal_statuses = OrderStatus.get_terminal_statuses()
            executed_statuses = OrderStatus.get_executed_statuses()

            updated_count = 0

            # BT sql-less "dict trades" (flag-on): read/write TradingOrder+Transaction through the
            # in-memory store; no SQLite session for them. LIVE (flag-off) keeps the exact session +
            # join + batched-commit path below, byte-for-byte unchanged.
            inmem = _ts.inmem_trades_active()
            session_cm = nullcontext(None) if inmem else Session(get_db().bind)
            with session_cm as session:
                # Get all NON-terminal transactions for this account. A CLOSED transaction is
                # terminal — all its orders are already terminal and its status/prices never
                # change again — so re-deriving its state every refresh is pure waste. Skipping
                # CLOSED ones is behaviour-preserving (no CLOSED->other transition exists) and,
                # in a backtest that refreshes every bar with hundreds of accumulated closed
                # transactions, removes the dominant per-bar O(transactions) re-query cost.
                if inmem:
                    transactions = _ts.transactions_with_orders(
                        lambda o: o.account_id == self.id,
                        lambda t: t.status != TransactionStatus.CLOSED)
                else:
                    statement = select(Transaction).join(TradingOrder).where(
                        TradingOrder.account_id == self.id,
                        Transaction.status != TransactionStatus.CLOSED,
                    ).distinct()
                    transactions = session.exec(statement).all()

                for transaction in transactions:
                    original_status = transaction.status
                    has_changes = False

                    # Get all orders for this transaction
                    orders = _ts.orders_where(
                        account_id=self.id, transaction_id=transaction.id, session=session)

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

                    # Sum ALL filled buy and sell orders to determine position quantity.
                    # A cancel-and-replace (TP/SL rebase) can race a live fill: the broker
                    # executes part of the order before honoring the cancel, leaving it
                    # CANCELED with filled_qty > 0. Those shares really traded, so count
                    # them even though CANCELED isn't in executed_statuses - otherwise this
                    # recalculation re-inflates the transaction back to the pre-fill
                    # quantity on every refresh, overwriting reconcile_canceled_partial_fill
                    # (and any manual correction) every cycle.
                    # A multi-leg option combo (e.g. call_butterfly) represents ONE
                    # "structures" quantity on BOTH its PARENT order (asset_class OPTION,
                    # no contract_symbol) and each of its CHILD legs (each leg's own
                    # quantity/filled_qty independently encodes that same structures count,
                    # scaled by that leg's ratio - e.g. 1x/2x/1x for a butterfly's
                    # wing/body/wing). Summing every order unconditionally counts a single
                    # combo fill event (1 parent + N legs) times instead of once, which
                    # compounds every bar as the resulting bogus quantity inflates
                    # mark-to-market equity -> next trade's position size -> quantity
                    # further. Only the parent's filled_qty is the transaction-level
                    # position size; its legs are execution mechanics and must be excluded.
                    multi_leg_parent_ids = {
                        o.id for o in orders
                        if getattr(o, "asset_class", None) == AssetClass.OPTION
                        and not getattr(o, "contract_symbol", None)
                        and getattr(o, "parent_order_id", None) is None
                    }

                    total_filled_buy = 0.0
                    total_filled_sell = 0.0
                    for order in orders:
                        if getattr(order, "parent_order_id", None) in multi_leg_parent_ids:
                            continue
                        filled_qty = order.filled_qty or 0
                        if order.status in executed_statuses or filled_qty > 0:
                            qty = filled_qty if filled_qty else order.quantity
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

                    # MULTI-LEG OPTION STRUCTURE: the transaction's real position is the
                    # PER-CONTRACT net over every executed option order on it (entry legs,
                    # MLEG-close legs, standalone single-leg closes, synthetic expiry
                    # closes) — NOT the parent-vs-closes quantity comparison above, which
                    # mixes units (structures vs contracts) and marked the transaction
                    # CLOSED as soon as ANY ONE leg was closed individually (e.g. a
                    # margin-liquidation buy-back of one strangle leg): the surviving legs
                    # then became invisible to get_option_positions / _apply_option_expiry /
                    # exit management and were recorded open_at_end instead of expiring
                    # (the B10 orphan-leg defect, run 760's BABA/C/MRVL puts). The
                    # structure-quantity rewrite is likewise skipped (mixed-unit garbage);
                    # the parent + the engine's own sync own the structures count.
                    if multi_leg_parent_ids:
                        contract_net: Dict[str, float] = {}
                        for order in orders:
                            if getattr(order, "asset_class", None) != AssetClass.OPTION:
                                continue
                            contract = getattr(order, "contract_symbol", None)
                            if not contract:
                                continue  # net-only parent, not a contract position
                            o_filled = order.filled_qty or 0
                            if order.status in executed_statuses or o_filled > 0:
                                qty = float(o_filled if o_filled else order.quantity or 0)
                                if qty:
                                    contract_net[contract] = contract_net.get(contract, 0.0) + (
                                        qty if order.side == OrderDirection.BUY else -qty)
                        position_balanced = bool(contract_net) and all(
                            abs(v) < 0.0001 for v in contract_net.values())
                        calculated_quantity = 0.0

                    # Update transaction quantity if different
                    if calculated_quantity != 0 and transaction.quantity != calculated_quantity:
                        if calculated_quantity < 0:
                            logger.error(
                                f"NEGATIVE calculated_quantity in sync_transaction_orders: {calculated_quantity} "
                                f"for transaction {transaction.id} ({transaction.symbol}, side={transaction.side}), "
                                f"total_filled_buy={total_filled_buy}, total_filled_sell={total_filled_sell}. "
                                f"Using abs() as safety measure."
                            )
                            calculated_quantity = abs(calculated_quantity)
                        transaction.quantity = calculated_quantity
                        has_changes = True
                        logger.debug(f"Transaction {transaction.id} quantity updated to {calculated_quantity}")

                    # NEVER-OPENED CLEANUP: If all orders are terminal AND nothing ever
                    # filled (no entry executed, no quantity, no open_date), the transaction
                    # is a stub from a rejected/canceled order chain. Delete it (cascading
                    # to its orders) instead of leaving a zero-qty CLOSED row that clutters
                    # the Live Trades view. Example causes: "asset not found" at broker,
                    # hedging block on conflicting side, insufficient buying power.
                    never_opened = (
                        all_orders_terminal
                        and not has_filled_entry_order
                        and transaction.open_date is None
                        and total_filled_buy == 0
                        and total_filled_sell == 0
                        and transaction.status != TransactionStatus.CLOSED
                    )
                    if never_opened:
                        from ba2_common.core.db import log_activity
                        from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                        order_errors = [
                            (o.id, o.symbol, o.side.value if o.side else None,
                             o.status.value if o.status else None, o.comment)
                            for o in orders
                        ]
                        log_activity(
                            severity=ActivityLogSeverity.INFO,
                            activity_type=ActivityLogType.TRANSACTION_CLOSED,
                            description=(
                                f"Deleted never-opened transaction {transaction.id} "
                                f"({transaction.symbol} {transaction.side.value if transaction.side else '?'}) "
                                f"- all orders terminal without execution"
                            ),
                            data={
                                "transaction_id": transaction.id,
                                "symbol": transaction.symbol,
                                "side": transaction.side.value if transaction.side else None,
                                "expert_id": transaction.expert_id,
                                "orders": order_errors,
                                "reason": "never_opened_cleanup",
                            },
                            source_account_id=self.id,
                        )
                        logger.info(
                            f"Deleting never-opened transaction {transaction.id} "
                            f"({transaction.symbol}): {len(orders)} terminal order(s), no fills"
                        )
                        if inmem:
                            delete_instance(transaction)
                        else:
                            session.delete(transaction)
                        updated_count += 1
                        continue

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

                    # ONE LEG SETTLING IS NOT THE STRUCTURE CLOSING. THIS ARM FIXES
                    # OPT-S8, THE BACKTEST DEFECT. It is NOT the OPT-S3 live fix, and the
                    # commit that introduced it (de4f0f0f) claimed both. Read the next
                    # paragraph before relying on this guard for anything live.
                    #
                    # WHERE THE LIVE DOOR ACTUALLY IS. A live assignment / exercise /
                    # expiry arrives as a broker ACTIVITY and is applied by
                    # ``AlpacaAccount._apply_option_activity``, which calls ``_close_txn``
                    # — and ``_close_txn`` sets ``Transaction.status = CLOSED`` on the row
                    # and persists it directly. It never passes through
                    # ``refresh_transactions``, so nothing on this line can see it, let
                    # alone hold it back. de4f0f0f does not touch ``AlpacaAccount.py`` at
                    # all. OPT-S3 is fixed at that door, in ``_apply_option_activity``,
                    # by refusing to close a structure while any of its contracts is
                    # still open.
                    #
                    # WHAT THIS ARM IS, THEN. The BACKTEST fix, and — for live —
                    # DEFENCE IN DEPTH. In the backtest engine the settlement really does
                    # arrive as a FILLED dependent OPTION order
                    # (``_record_option_expiry_close`` links its synthetic close to the
                    # entry via ``depends_on_order``, which is what makes it dependent),
                    # so this branch is the one that fired and this guard is the fix.
                    # Live, no code path writes a FILLED dependent OPTION order onto a
                    # multi-leg option transaction, so the guarded arm is currently
                    # UNREACHABLE for a live structure: it costs nothing, and it is
                    # already correct for the day a live path starts producing that shape.
                    #
                    # THE BRANCH AS IT STOOD. ``filled_closing_orders`` is "some DEPENDENT
                    # order on this transaction is FILLED", and on a multi-leg option
                    # structure exactly one leg can produce that on its own: an expiry
                    # settlement as above, or a ONE-LEG MARGIN LIQUIDATION. The branch
                    # below then closed the WHOLE transaction as "tp_sl_filled",
                    # pre-empting the per-contract ``position_balanced`` computed above,
                    # which is the only thing here that actually knows whether the
                    # STRUCTURE is flat.
                    #
                    # What that cost: the surviving legs — INCLUDING the protective long of
                    # a spread — disappear from ``get_option_positions`` and
                    # ``_option_transaction_for_contract``, both of which filter
                    # ``TransactionStatus.OPENED``, so nothing can see, manage, expire or
                    # close them again; in the backtest their ``_OptionLot`` stays in the
                    # ledger and keeps being charged maintenance margin. It is the same
                    # orphaning as the B10 defect, reached through the other door: B10 came
                    # in through the mixed-unit balance sums (fixed by the per-contract
                    # ``contract_net`` above), this one walks straight past that fix.
                    #
                    # NO NEW LINKAGE IS NEEDED to tell the cases apart. ``multi_leg_parent_ids``
                    # is non-empty exactly when this transaction carries an MLEG net-only
                    # PARENT (an OPTION order with no ``contract_symbol`` and no
                    # ``parent_order_id``) — i.e. when the legs are joined by
                    # ``parent_order_id`` at all — and ``position_balanced`` has already been
                    # recomputed per CONTRACT for precisely that case.
                    #
                    # A SINGLE-LEG option is NOT held back: it writes one contract-carrying
                    # order and no parent, so ``multi_leg_parent_ids`` is empty and it still
                    # closes on its own closing fill. It has no sibling to wait on, and
                    # stranding it OPENED would be the mirror of the bug. An EQUITY
                    # transaction has no OPTION orders at all and is untouched.
                    one_leg_of_many_settled = bool(multi_leg_parent_ids) and not position_balanced

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

                        from ba2_common.core.utils import close_transaction_with_logging
                        close_transaction_with_logging(
                            transaction=transaction,
                            account_id=self.id,
                            close_reason="oco_leg_filled",
                            session=session
                        )
                        new_status = TransactionStatus.CLOSED
                        has_changes = True

                    # OPENED -> CLOSED: If we have a filled closing order (TP/SL) — unless
                    # it is one leg of a multi-leg structure whose other legs are still
                    # open (see ``one_leg_of_many_settled`` above). When every contract IS
                    # flat this still fires, so a fully closed structure closes here as
                    # before.
                    elif (filled_closing_orders
                          and transaction.status == TransactionStatus.OPENED
                          and not one_leg_of_many_settled):
                        from ba2_common.core.utils import close_transaction_with_logging
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
                        from ba2_common.core.utils import close_transaction_with_logging
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

                        from ba2_common.core.utils import close_transaction_with_logging
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
                        from ba2_common.core.utils import close_transaction_with_logging
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
                            from ba2_common.core.utils import close_transaction_with_logging
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
                        if inmem:
                            update_instance(transaction)
                        else:
                            session.add(transaction)
                        updated_count += 1
                        logger.info(f"Updated transaction {transaction.id} status: {original_status.value} -> {new_status.value}")
                    elif has_changes:
                        if inmem:
                            update_instance(transaction)
                        else:
                            session.add(transaction)
                        updated_count += 1

                if not inmem:
                    session.commit()

            logger.info(f"Successfully refreshed transactions for account {self.id}: {updated_count} transactions updated")
            return True

        except Exception as e:
            logger.error(f"Error refreshing transactions for account {self.id}: {e}", exc_info=True)
            return False

    def has_pending_closing_order(self, transaction_id: int) -> bool:
        """True if a closing order for this transaction is already WORKING (submitted, not
        yet terminal).

        A transaction's market-entry-level orders (``depends_on_order IS NULL`` - this
        includes both the original entry and any subsequently-submitted closing order, whether
        single-leg or a multi-leg parent) sort oldest-first to newest: the oldest is the entry,
        so any LATER one still non-terminal is a close that hasn't resolved yet.

        Callers managing open positions (deciding whether to re-evaluate exit rules / submit a
        new closing order for a transaction) MUST check this first. Without it, a transaction
        stays visible as "still open, needs managing" for every cycle a submitted close takes to
        fill - and each cycle submits ANOTHER closing order for the same position, each one
        crediting cash/reducing exposure for contracts that may already be gone. This is a
        shared standard on the interface (not a backtest- or live-only concern) because both
        sides re-evaluate open positions from ``Transaction.status`` alone and neither
        previously guarded against a close already in flight (found investigating the
        2026-07-21 options-grid trillion-scale equity runaway, where the backtest engine's
        limit-order multi-leg closes take a full bar to fill).
        """
        from ba2_common.core.trade_store import orders_where
        from ba2_common.core.types import OrderStatus

        orders = orders_where(account_id=self.id, transaction_id=transaction_id, depends_on_order=None)
        if len(orders) <= 1:
            return False
        orders.sort(key=lambda o: (o.created_at or datetime.min.replace(tzinfo=timezone.utc), o.id or 0))
        # "Resolved" = genuinely terminal (rejected/canceled/expired/...) OR fully FILLED.
        # get_terminal_statuses() deliberately excludes FILLED (tracked separately as
        # "executed") — a FILLED close IS resolved and must not be read as still pending.
        # PARTIALLY_FILLED is intentionally left OUT of "resolved": the remainder is still
        # working, so the close hasn't finished doing its job yet.
        resolved = OrderStatus.get_terminal_statuses() | {OrderStatus.FILLED}
        return any(o.status not in resolved for o in orders[1:])

    def reconcile_externally_closed_transactions(self, grace_period_minutes: int = 5) -> int:
        """Close OPENED (or stuck-CLOSING) transactions whose symbol no longer has a position
        at the broker.

        ``refresh_transactions`` is order-driven: it closes a transaction only when the
        platform's OWN orders fill/balance/terminate. A position closed DIRECTLY at the
        broker (outside the platform) leaves the entry order FILLED (which is NOT a
        terminal status) and the protective SELL/OCO orders CANCELED, with no filled
        sell — so the order-driven logic can never close it and the trade shows "alive"
        forever. This reconciles against the broker's ACTUAL positions: if the broker
        holds no position for a symbol that still has OPENED transaction(s), close them
        as ``position_not_at_broker`` and cancel any still-resting orders.

        Safeguards:
          - If broker positions can't be fetched, do NOTHING (never close on an API error
            — an empty/failed fetch must not mass-close the book).
          - Only close when the broker has ZERO position for the symbol. A non-zero but
            smaller position (partial manual close) is a quantity mismatch handled by the
            Overview "Adjust Quantities" flow, not here.
          - Skip transactions opened within ``grace_period_minutes`` — a just-filled entry
            may not have settled into a reported broker position yet.

        Returns the number of transactions closed.
        """
        from sqlmodel import select, Session
        from ba2_common.core.db import get_db, update_instance
        from ba2_common.core.models import TradingOrder, Transaction
        from ba2_common.core.types import OrderStatus, TransactionStatus, AssetClass
        from ba2_common.core.utils import close_transaction_with_logging

        # 1) Fetch the broker's real positions. NEVER reconcile on a failure — that would
        #    risk closing the entire book if the broker API hiccups.
        try:
            positions = self.get_positions()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Account {self.id}] reconcile: could not fetch broker positions ({e}); skipping")
            return 0
        if positions is None:
            return 0

        broker_symbols = set()
        for pos in positions:
            pos_dict = pos if isinstance(pos, dict) else dict(pos)
            sym = pos_dict.get('symbol')
            # SENTINEL, not 0. ``pos_dict.get('qty', 0)`` defaulted a MISSING key to a
            # measured zero, which is the same "unknown reads as zero" the block below
            # exists to stop.
            qty = pos_dict.get('qty', None)
            # AN UNREADABLE QUANTITY IS NOT A FLAT POSITION.
            #
            # This was ``abs(float(qty or 0)) > 1e-6``. The ``or 0`` converted None into a
            # measured zero BEFORE the except clause below -- written for exactly this
            # case -- could ever fire, so the guard was dead code. The symbol then never
            # entered ``broker_symbols``, reconcile concluded the position was gone, and
            # FORCE-CLOSED the transaction, cancelling its protective stops on the way
            # out. A broker that reports a position but not its size is telling us the
            # position EXISTS; only a quantity we actually read as ~0 is a close signal.
            try:
                if qty is None:
                    raise TypeError("no quantity reported")
                has_qty = abs(float(qty)) > 1e-6
            except (TypeError, ValueError):
                logger.error(
                    f"[Account {self.id}] reconcile: broker position for {sym!r} reports an "
                    f"UNREADABLE quantity ({qty!r}). Treating the position as STILL HELD — "
                    f"an unmeasurable size must never be read as 'flat' and force-close the "
                    f"transaction."
                )
                has_qty = True
            if sym and has_qty:
                broker_symbols.add(sym)

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_period_minutes)
        terminal_statuses = OrderStatus.get_terminal_statuses()
        closed_count = 0

        with Session(get_db().bind) as session:
            # OPENED (and CLOSING) transactions belonging to THIS account (transactions link
            # to an account only through their orders), mirroring refresh_transactions' join.
            # CLOSING is included because a transaction can get stuck there indefinitely: the
            # platform's own close attempt fails/cancels repeatedly (flipping status to CLOSING)
            # while the position gets closed DIRECTLY at the broker in the meantime (manually,
            # outside the platform) -- that external fill never resolves the CLOSING state since
            # order-driven refresh_transactions only reacts to the platform's OWN order fills.
            # An OPENED-only filter here left such a transaction silently unreconciled forever
            # (see the 2026-07-14 ASC incident: stuck CLOSING since 2026-07-09, only surfaced via
            # a stale bracket-retry erroring 5 days later).
            statement = select(Transaction).join(TradingOrder).where(
                TradingOrder.account_id == self.id,
                Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSING]),
            ).distinct()
            open_txns = session.exec(statement).all()

            for txn in open_txns:
                if not txn.symbol or txn.symbol in broker_symbols:
                    continue
                # Grace period: skip a freshly-opened entry that may not have settled.
                open_date = txn.open_date
                if open_date is not None:
                    if open_date.tzinfo is None:
                        open_date = open_date.replace(tzinfo=timezone.utc)
                    if open_date > cutoff:
                        continue

                txn_orders = session.exec(
                    select(TradingOrder).where(
                        TradingOrder.transaction_id == txn.id,
                        TradingOrder.account_id == self.id,
                    )
                ).all()

                # OPTIONS GUARD: get_positions() reports EQUITY positions only, so an
                # option transaction's symbol would never match and we'd wrongly close it.
                # Option lifecycle (assignment/exercise/expiry) is reconciled separately
                # (see TradeManager._reconcile_account_option_activities). Skip options here.
                if any(o.asset_class == AssetClass.OPTION for o in txn_orders):
                    continue

                # Stamp a close price from the last known market price (best-effort, so P&L
                # is recorded). Leave it unset if unavailable rather than guessing.
                try:
                    price = self.get_instrument_current_price(txn.symbol)
                    if price:
                        txn.close_price = float(price)
                except Exception:  # noqa: BLE001
                    pass

                # Cancel any still-resting orders for this transaction (the broker has
                # already released the position, so these can never fill correctly).
                for o in txn_orders:
                    if o.status not in terminal_statuses:
                        o.status = OrderStatus.CANCELED
                        session.add(o)

                logger.info(
                    f"[Account {self.id}] reconcile: {txn.symbol} no longer held at broker — "
                    f"closing OPENED transaction {txn.id} (external close)"
                )
                close_transaction_with_logging(
                    transaction=txn,
                    account_id=self.id,
                    close_reason="position_not_at_broker",
                    session=session,
                )
                session.add(txn)
                closed_count += 1

            if closed_count:
                session.commit()

        return closed_count

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
                - amount (float): NET dividend (gross - tax) in account currency. This is the
                  income actually kept and the value consumers (income/charts) should use.
                - gross_amount (float): Gross dividend before tax withholding.
                - tax_withheld (float): Tax withheld on the dividend (positive number, e.g., 0.12). Defaults to 0.0.
                - date (datetime): Date the dividend was received
                - drip_quantity (float | None): Number of shares reinvested via DRIP, if applicable
                  (None for non-reinvested / cash dividends)
                - drip_price (float | None): Price per share for DRIP reinvestment, if applicable
        """
        pass

    @abstractmethod
    def get_filled_trades(self, symbol: Optional[str] = None, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> List[Dict]:
        """
        Get filled trade history for this account.

        Args:
            symbol: Optional symbol to filter by. If None, returns trades for all symbols.
            start_date: Optional start date filter.
            end_date: Optional end date filter.

        Returns:
            List[Dict]: Each containing:
                - symbol (str): The stock symbol
                - qty (float): Filled quantity (always positive)
                - side (str): 'BUY' or 'SELL'
                - date (datetime): Date the trade was filled
                - price (float): Fill price per share
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
