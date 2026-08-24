from abc import abstractmethod
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone
from ba2_common.logger import logger
from ba2_common.core.models import TradingOrder, Transaction, ExpertRecommendation, ExpertInstance
from ba2_common.core.types import (
    OrderOpenType, OrderDirection, OrderType, OrderStatus, TransactionStatus, BrokerOrderErrorReason,
)
from ba2_common.core.account_types import OrderImpact
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_common.core.db import add_instance, get_db, get_instance, update_instance


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

    #: Whether this broker can attach a protective TP/SL leg to a position at all,
    #: i.e. whether ``adjust_tp`` / ``adjust_sl`` / ``adjust_tp_sl`` really work.
    #:
    #: Set it False on a broker whose adjust_* methods raise NotImplementedError.
    #: ``submit_order`` then REFUSES a request that carries a tp_price/sl_price
    #: instead of opening the position and merely logging that the stop could not be
    #: placed -- which is what it used to do, handing the caller a live, UNPROTECTED
    #: position as the success value. Once the entry is filled there is no honest
    #: answer left (returning the order claims protection that does not exist;
    #: returning None strands a real broker position the caller thinks never opened),
    #: so the only safe place to fail is BEFORE the broker call.
    supports_protective_legs = True


    @abstractmethod
    def _submit_order_impl(self, trading_order, tp_price: Optional[float] = None, sl_price: Optional[float] = None, is_closing_order: bool = False, use_complex_order: bool = False) -> Any:
        """
        Internal implementation of order submission. This method should be implemented by child classes.
        The public submit_order method will call this after validation.

        Args:
            trading_order: A validated TradingOrder object containing all order details.
            tp_price: Optional take profit price for bracket orders (broker-specific support).
            sl_price: Optional stop loss price for bracket orders (broker-specific support).
            is_closing_order: If True, this order closes an existing position (skip hedging checks).
            use_complex_order: If True, submit as a native complex order (bracket/OTO) with the
                given tp_price/sl_price as attached legs, instead of a plain order plus separate
                protective legs. Set by ``submit_order`` when an opposing order is working at the
                broker, because complex orders are exempt from the wash-trade check. Brokers that
                do not support complex orders may ignore it; at least one of tp_price/sl_price is
                guaranteed non-None when it is True.

        Returns:
            Any: The created order object if successful. Returns None or raises an exception if failed.
        """
        pass

    def preview_order_impact(self, trading_order: TradingOrder,
                             is_closing_order: bool = False) -> Optional[OrderImpact]:
        """Broker-side dry-run of ONE order: what it would cost in buying power.

        CONCRETE, returns ``None`` by default. ``None`` means "this broker has no
        precheck", NOT "the order is free" -- a caller that treats ``None`` as a
        zero impact will over-commit. Alpaca has no order-preview endpoint and
        keeps the base ``None``, so it relies on ``get_symbol_margin_info()``.
        TastyTrade implements it with
        ``Account.place_order(session, order, dry_run=True)``.

        MUST NOT send a live order.
        ``tastytrade.account.Account.place_order``'s ``dry_run`` parameter
        DEFAULTS TO ``True`` (site-packages/tastytrade/account.py:877-879) --
        pass it explicitly here anyway, and never rely on that default at a real
        submission call site.

        Args:
            trading_order: a candidate TradingOrder (saved or unsaved) describing
                the order. This method must NOT mutate or persist it, and must
                not set ``broker_order_id``.
            is_closing_order: True when the order would CLOSE an existing position.
                It must be forwarded exactly as ``submit_order`` forwards it to
                ``_submit_order_impl``, because on brokers that encode open/close in
                the order itself (TastyTrade's SELL_TO_CLOSE vs SELL_TO_OPEN) the two
                price completely differently: a close FREES buying power, while a
                short open CONSUMES margin and needs short approval. Dropping it made
                every closing preview come back rejected on a cash account, so the
                allocation engine skipped legitimate sells -- while this docstring
                claimed the preview prices exactly what would be sent.

        Returns:
            Optional[OrderImpact]: ``None`` when the broker does not support
            prechecks OR when the preview call itself failed -- log the failure
            (``logger.error(..., exc_info=True)`` inside the except block); do not
            fabricate a zero impact.
        """
        return None

    def _classify_order_error(self, exc: Exception) -> BrokerOrderErrorReason:
        """Map this broker's NATIVE submission error onto the shared, broker-agnostic
        ``BrokerOrderErrorReason`` taxonomy. Default: UNKNOWN (the broker's raw message is
        still preserved verbatim in the order comment). Override in a broker subclass to
        recognize its own error codes/messages — a single numeric/string code is often
        AMBIGUOUS on its own (e.g. Alpaca's 42210000 covers both "stop price must be less
        than current price" and "invalid underlying symbols"), so classification must
        inspect the full error, not just a code.
        """
        return BrokerOrderErrorReason.UNKNOWN

    # Stop-type orders where a STOP_THROUGH_MARKET rejection can be safely resubmitted as an
    # equivalent MARKET order (same side/qty) — the stop had already been breached by the time
    # the broker processed it, so a market order fulfills the same intent immediately.
    _STOP_ORDER_TYPES = {
        OrderType.BUY_STOP, OrderType.SELL_STOP,
        OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT,
    }

    def _handle_order_submit_error(self, trading_order: TradingOrder, exc: Exception) -> Optional[TradingOrder]:
        """Broker-agnostic order-submission failure handling: classify the error via
        ``_classify_order_error``, retry ONCE as a MARKET order when a stop was already
        breached (STOP_THROUGH_MARKET), and otherwise mark the order ERROR with the typed
        reason + broker message recorded in ``comment`` (so it's visible in the Pending
        Orders UI, not just the log).

        Every ``_submit_order_impl`` should call this from its except block instead of each
        broker duplicating this bookkeeping — that's what makes the stop-breach retry and the
        comment reason work identically for Alpaca, IBKR, or any future broker.

        Returns the resubmitted order on a successful retry, else None (matching
        ``_submit_order_impl``'s existing "None on failure" contract).
        """
        reason = self._classify_order_error(exc)
        fresh_order = get_instance(TradingOrder, trading_order.id)
        if not fresh_order:
            logger.error(f"Could not find order {trading_order.id} to record submit error")
            return None

        error_msg = f"[{reason.value}] {str(exc)[:180]}"

        if reason == BrokerOrderErrorReason.STOP_THROUGH_MARKET and fresh_order.order_type in self._STOP_ORDER_TYPES:
            logger.warning(
                f"Order {fresh_order.id} ({fresh_order.symbol}) stop already through market; "
                f"resubmitting as MARKET order"
            )
            fresh_order.order_type = OrderType.MARKET
            fresh_order.stop_price = None
            fresh_order.limit_price = None
            note = f"{error_msg} — auto-converted to MARKET (stop already breached)"
            fresh_order.comment = f"{fresh_order.comment} | {note}" if fresh_order.comment else note
            fresh_order.comment = fresh_order.comment[:500]
            update_instance(fresh_order)
            try:
                # The converted order is a reducing/closing action for the stop's own
                # position, not a new entry — exempt it from position-size validation.
                return self._submit_order_impl(fresh_order, is_closing_order=True)
            except Exception as retry_exc:  # noqa: BLE001 — fall through to ERROR below
                logger.error(
                    f"Market-order retry failed for order {fresh_order.id}: {retry_exc}", exc_info=True
                )
                error_msg = f"{error_msg} | market retry also failed: {str(retry_exc)[:150]}"
                fresh_order = get_instance(TradingOrder, trading_order.id) or fresh_order

        fresh_order.status = OrderStatus.ERROR
        fresh_order.comment = (
            f"{fresh_order.comment} | {error_msg}" if fresh_order.comment else error_msg
        )[:500]
        update_instance(fresh_order)
        logger.info(f"Marked order {fresh_order.id} as ERROR in database ({reason.value})")
        return None

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

        Raises:
            ValueError: when a tp_price/sl_price is requested from a broker that cannot
                place protective legs (``supports_protective_legs`` is False). NOTHING is
                submitted in that case -- see the guard below.
        """
        # PROTECTIVE-LEG CAPABILITY GATE -- runs before ANY database write or broker call.
        #
        # The TP/SL block further down submits the entry FIRST and only then calls
        # adjust_tp/adjust_sl, catching NotImplementedError with a mere logger.warning.
        # The entry order is still returned as the success value, so all four sl_price=
        # call sites (TradeManager x2, FactorRanker.portfolio, SmartRiskManagerToolkit)
        # book a filled position and believe it is protected. That is a NAKED POSITION
        # REPORTED AS SUCCESS, and no caller can tell it apart from a protected one.
        #
        # There is no honest recovery after the fill, so the request is refused before
        # anything is opened: either the stop exists, or nothing was opened.
        if (tp_price is not None or sl_price is not None) and not self.supports_protective_legs:
            raise ValueError(
                f"Broker {self.__class__.__name__} cannot place protective TP/SL legs "
                f"(supports_protective_legs=False), so the requested protection for "
                f"{trading_order.symbol} (tp={tp_price}, sl={sl_price}) cannot be honoured. "
                f"Refusing to submit the entry: opening an unprotected position and "
                f"reporting it as success is not an acceptable outcome."
            )

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
        
        # Sync quantity with the parent order for dependent TP/SL legs.
        #
        # A protective leg must cover the position its parent creates, so it inherits the parent's
        # quantity. The one case where it must NOT is a PARTIAL CLOSE: closing 4 of 5 shares creates
        # a MARKET sell, and the new TP/SL for the remaining 1 share must keep its own quantity
        # rather than be resized to 4.
        #
        # THIS IS A REGRESSION, not an original defect. Until 1077e2c (2025-12-25, a commit titled
        # "Refactor UI: LazyTable component, async rendering fixes, modern dark theme") the sync was
        # unconditional -- `if parent_order and parent_order.quantity:` -- and entry-attached TPs
        # worked. That commit added `and parent_order.order_type != OrderType.MARKET` to stop a
        # partial close from resizing the leg for the REMAINING shares. The intent was right; the
        # test was not, because an ENTRY is a MARKET order too, so it silently took out every
        # entry-attached TP as collateral. Neither version was correct on its own:
        #
        #   pre-1077e2c : entry TP synced (correct)   | partial close wrongly resized (bug)
        #   1077e2c..   : entry TP left at 0 (bug)    | partial close keeps own qty (correct)
        #   this        : both correct, discriminated by SIDE
        #
        # Measured on prod 2026-08-08: every FMPEarningsDrift
        # take-profit (WKC/GNTX/CSTL) was created as a SELL_LIMIT with a real price but quantity 0,
        # hit this branch because its parent was the MARKET entry, kept the 0, and was cancelled by
        # the broker. Three live positions ran with a stop and NO upside exit, silently -- no error,
        # just a cancelled order. Dev has 12 more of the same rows; it only looked healthy there
        # because its other positions use OCO, which carries both legs in one correctly-sized order.
        #
        # The real discriminator is SIDE, not order type:
        #   * entry BUY  -> protective SELL leg : OPPOSITE sides -> the parent is the entry, SYNC.
        #   * close SELL -> new protective SELL : SAME side      -> partial close, KEEP own qty.
        # That is exactly the case the original comment describes, expressed in terms of what
        # actually distinguishes the two.
        if (trading_order.depends_on_order is not None and
            trading_order.order_type in [OrderType.SELL_LIMIT, OrderType.BUY_LIMIT, OrderType.SELL_STOP, OrderType.BUY_STOP]):
            try:
                parent_order = get_instance(TradingOrder, trading_order.depends_on_order)
                is_partial_close_parent = (
                    parent_order is not None
                    and parent_order.order_type == OrderType.MARKET
                    and parent_order.side == trading_order.side
                )
                if parent_order and parent_order.quantity and not is_partial_close_parent:
                    if trading_order.quantity != parent_order.quantity:
                        old_qty = trading_order.quantity
                        trading_order.quantity = parent_order.quantity
                        logger.info(
                            f"Synced TP/SL order quantity with parent entry order: "
                            f"order {trading_order.id or 'new'} qty {old_qty} → {parent_order.quantity} "
                            f"(parent order {parent_order.id}, type {parent_order.order_type})"
                        )
                elif is_partial_close_parent:
                    logger.debug(
                        f"TP/SL order {trading_order.id or 'new'} parent {parent_order.id} is a same-side "
                        f"MARKET close - keeping independent quantity {trading_order.quantity}"
                    )
                else:
                    logger.warning(
                        f"Parent order {trading_order.depends_on_order} not found or has no quantity "
                        f"for TP/SL order {trading_order.id or 'new'}"
                    )
            except Exception as e:
                logger.error(f"Error syncing TP/SL quantity with parent order: {e}", exc_info=True)

            # A protective leg with no quantity protects nothing. Submitting it anyway is what made
            # the prod TP loss invisible: the broker cancels it and the position looks "covered" in
            # the order list. Refuse loudly instead of sending a doomed order.
            if not trading_order.quantity or float(trading_order.quantity) <= 0:
                raise ValueError(
                    f"Refusing to submit protective {trading_order.order_type} leg for "
                    f"{trading_order.symbol} with quantity {trading_order.quantity!r}: a zero-quantity "
                    f"TP/SL is cancelled by the broker and leaves the position unprotected. "
                    f"(parent order {trading_order.depends_on_order})"
                )

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

        # Wash-trade gate (broker-agnostic): an opposite-side order already working at the
        # broker for this symbol makes most brokers (e.g. Alpaca, code 40310000) reject a
        # plain market/stop/limit order as a wash trade.
        #
        # Brokers exempt COMPLEX orders (bracket/OTO/OCO) from that check — Alpaca's own
        # rejection says "use complex orders", and this was verified against the live paper
        # API on 2026-08-05 (see docs/WASHTRADE-LOCK.md for the probe table). So when a
        # blocker exists and we have at least one protective price to attach, submit the
        # order as a complex order and let it through rather than locking it.
        #
        # Locking is the fallback for orders that cannot form a complex order (no TP and no
        # SL). It is only safe as a fallback: a lock waits for the blocker to clear, and a
        # protective stop guarding an open position never does. TradeManager expires locks
        # that outlive their signal.
        use_complex_order = False
        if self._is_washtrade_lock_candidate(trading_order):
            # A dependent leg's own parent is a genuine bracket pair the broker accepts, so it
            # must not lock its own leg; any OTHER opposing order still does.
            blocker = self._find_opposing_working_order(
                symbol, side,
                exclude_order_id=getattr(trading_order, 'depends_on_order', None),
            )
            if blocker is not None:
                blocker_status = blocker.status.value if hasattr(blocker.status, 'value') else blocker.status
                if tp_price or sl_price:
                    use_complex_order = True
                    logger.info(
                        f"Order {trading_order.id} ({symbol} {side.value}) is blocked by "
                        f"opposite-side order {blocker.id} ({blocker.side.value}, {blocker_status}) "
                        f"— submitting as a complex order (tp={tp_price}, sl={sl_price}) instead "
                        f"of locking; complex orders are exempt from the wash-trade check"
                    )
                else:
                    trading_order.status = OrderStatus.WASHTRADE_LOCKED
                    update_instance(trading_order)
                    logger.info(
                        f"Order {trading_order.id} ({symbol} {side.value}) set WASHTRADE_LOCKED: "
                        f"opposite-side order {blocker.id} ({blocker.side.value}, {blocker_status}) "
                        f"is working at the broker and no TP/SL is available to form a complex "
                        f"order; will retry on next refresh"
                    )
                    return trading_order

        # Call the child class implementation (this will update the order with broker_order_id)
        # Pass tp_price and sl_price for brokers that support bracket orders
        # The trading_order object is now detached but all attributes are accessible
        result = self._submit_order_impl(trading_order, tp_price=tp_price, sl_price=sl_price,
                                         is_closing_order=is_closing_order,
                                         use_complex_order=use_complex_order)
        
        # Set TP and/or SL if provided and order was successfully submitted
        # Use adjust methods which create OCO/OTO orders (avoids code duplication)
        # The skip logic in adjust_tp_sl will prevent redundant calls if caller calls again
        #
        # Skipped entirely for a complex submission: the broker built the protective legs as
        # part of the order itself, so calling adjust_* here would place a SECOND, duplicate
        # set of legs against the same position.
        if use_complex_order:
            logger.debug(
                f"Order {trading_order.id}: protective legs came from the complex order itself "
                f"— skipping the adjust_tp/adjust_sl bracket block"
            )
        elif result and result.transaction_id:
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
    
    # Order types that open or close a primary position (vs dependent TP/SL legs).
    _PRIMARY_ORDER_TYPES = {
        OrderType.MARKET, OrderType.BUY_LIMIT, OrderType.SELL_LIMIT,
        OrderType.BUY_STOP, OrderType.SELL_STOP,
        OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT,
    }

    # Order types that actually trigger a broker wash-trade rejection on the
    # opposite side. Alpaca (code 40310000) rejects only against opposing
    # MARKET/STOP orders — LIMIT and STOP_LIMIT orders (e.g. protective TP/SL
    # legs working at the broker) do NOT cause a wash trade, so they must not
    # lock a new order.
    _WASHTRADE_BLOCKING_ORDER_TYPES = {
        OrderType.MARKET, OrderType.BUY_STOP, OrderType.SELL_STOP,
    }

    def _is_washtrade_lock_candidate(self, trading_order: TradingOrder) -> bool:
        """Primary open/close orders AND dependent protective legs are subject to the lock.

        A dependent leg (TP/SL) used to be exempt outright, on the reasoning that it is
        "inherently opposite-side and brokers accept it as a complex order". That holds only
        against its OWN parent — Alpaca does accept that bracket pair. It does NOT hold when an
        UNRELATED order is working on the same symbol, which happens constantly here because
        several experts hold the same ticker in separate transactions while the broker nets them
        into one position.

        Measured 2026-08-03: 8 protective SELL_STOP legs were rejected 40310000
        ("opposite side market/stop order exists") by a DIFFERENT transaction's BUY. Because the
        exemption skipped the lock, they went straight to the broker and were marked ERROR —
        terminal, never retried. Order 587 died that way and left tx 273 (UBER, 21 shares) with
        NO stop at the broker at all. 7 of the 8 survived only by timing luck.

        Locking them instead is safe: a working order blocks only until it FILLS (the blocking
        BUY was a MARKET order, filled seconds later), not until its position closes — so the
        retry in ``_check_all_washtrade_locked_orders`` clears it promptly rather than parking a
        naked position indefinitely.
        """
        return trading_order.order_type in self._PRIMARY_ORDER_TYPES

    def _find_opposing_working_order(self, symbol: str, side: OrderDirection,
                                     exclude_order_id: Optional[int] = None) -> Optional[TradingOrder]:
        """Return the first order on this account for ``symbol`` on the side opposite
        to ``side`` that is working at the broker (unfilled or partially filled).

        Orders that are not live at the broker — WASHTRADE_LOCKED, WAITING_TRIGGER,
        terminal — do not count, so two opposing locked orders cannot deadlock.

        ``exclude_order_id`` skips one specific order: a dependent leg's OWN parent. That pair is
        a genuine bracket, which the broker accepts as a complex order, so the parent must not
        lock its own protective leg (that would deadlock every TP/SL behind an unfilled entry).
        Any OTHER opposing order still blocks.
        """
        from sqlmodel import select
        working = OrderStatus.get_unfilled_statuses() | {OrderStatus.PARTIALLY_FILLED}
        with get_db() as session:
            statement = select(TradingOrder).where(
                TradingOrder.account_id == self.id,
                TradingOrder.symbol == symbol,
                TradingOrder.side != side,
                TradingOrder.status.in_(working),
                TradingOrder.order_type.in_(self._WASHTRADE_BLOCKING_ORDER_TYPES),
            )
            if exclude_order_id is not None:
                statement = statement.where(TradingOrder.id != exclude_order_id)
            return session.exec(statement).first()

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
    
    def _estimate_transaction_open_price(self, trading_order: TradingOrder) -> Optional[float]:
        """Best-effort entry-price ESTIMATE for a just-created Transaction (superseded by the
        order's own real fill data for anything that reads the ORDER directly, e.g. round-trip
        trade reporting -- this is only what gets stamped on the Transaction row itself).

        For an OPTION order, ``trading_order.symbol`` is the UNDERLYING ticker (see
        ``OptionsAccountInterface.submit_option_order``: ``symbol=(first.underlying or
        first.contract_symbol)``), so ``get_instrument_current_price(symbol)`` would return
        the underlying's STOCK price -- wrong by roughly the option's leverage ratio. Confirmed
        live: a real backtest transaction showed ``open_price=$497.37`` for a META call whose
        actual premium was a few dollars -- that corrupted value then fed
        ``MarketExpertInterface._calculate_used_balance`` (``open_price * quantity``, no
        multiplier), overstating that ONE position's "used" capital by ~100x and tripping the
        entry equity-gate into rejecting nearly every other candidate for the rest of the run.

        Priced instead off the option's own premium: single-leg via ``get_option_quote``
        (mid of bid/ask, else last, else whichever side is available); a multi-leg parent (no
        ``contract_symbol``) via its own ``limit_price`` -- the net debit/credit
        ``submit_option_order`` already computed for the combo. Equity orders are unaffected
        (unchanged ``get_instrument_current_price`` call)."""
        from ba2_common.core.types import AssetClass
        if getattr(trading_order, "asset_class", None) != AssetClass.OPTION:
            return self.get_instrument_current_price(trading_order.symbol)

        if trading_order.contract_symbol and hasattr(self, "get_option_quote"):
            try:
                quote = self.get_option_quote(trading_order.contract_symbol)
            except Exception as e:
                logger.debug(f"_estimate_transaction_open_price: quote lookup failed for "
                             f"{trading_order.contract_symbol}: {e}")
                quote = None
            if quote is not None:
                if quote.bid is not None and quote.ask is not None:
                    return (quote.bid + quote.ask) / 2.0
                for px in (quote.last, quote.bid, quote.ask):
                    if px is not None:
                        return px
        # Multi-leg parent (no single contract to quote) or quote unavailable: fall back to
        # the order's own limit_price, the net premium already computed at submission time.
        return abs(trading_order.limit_price) if trading_order.limit_price else None

    def _create_transaction_for_order(self, trading_order: TradingOrder) -> None:
        """
        Create a new Transaction for the given trading order.

        Args:
            trading_order: The TradingOrder object to create a transaction for
        """
        try:
            # Get current price for the symbol (this will be the open_price estimate)
            current_price = self._estimate_transaction_open_price(trading_order)

            # Get expert_id from the expert_recommendation if available
            expert_id = None
            if trading_order.expert_recommendation_id:
                from ba2_common.core.models import ExpertRecommendation
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
                expert_id=expert_id,  # Link to expert instance
                # Carry the contract multiplier (100 for options) so P&L/value math
                # scales the per-share premium correctly; null for equity.
                multiplier=getattr(trading_order, "multiplier", None),
            )
            
            # Save transaction to database
            transaction_id = add_instance(transaction)
            trading_order.transaction_id = transaction_id
            
            logger.info(f"Created transaction {transaction_id} for order: {trading_order.symbol} {trading_order.side.value} {trading_order.quantity} (expert_id={expert_id})")
            
            # Log activity using centralized helper
            from ba2_common.core.utils import log_transaction_created_activity
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
            from ba2_common.core.utils import log_transaction_created_activity
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
            from ba2_common.core.models import Transaction
            
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
            
            # orders_where is the dual-path equivalent of the raw select() this replaced
            # (review 2026-07-18, M2): a raw select(TradingOrder) silently finds nothing
            # when the backtest in-mem store is active (TradingOrder is an in-mem model,
            # see trade_store.IN_MEM_MODELS). orders_where has no `side` filter, so that
            # check is applied in Python after the routed fetch.
            from ba2_common.core.trade_store import orders_where
            candidate_orders = orders_where(
                transaction_id=transaction_id, account_id=self.id, depends_on_order=None,
            )
            market_entry_orders = [o for o in candidate_orders if o.side == target_side]

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
                from ba2_common.core.trade_store import inmem_trades_active
                if inmem_trades_active():
                    # Backtest in-mem store: `transaction` IS the stored object (same
                    # identity), so this mutation is already reflected -- update_instance
                    # is a safe, tested no-op persist here (see db.update_instance's
                    # _inmem_route branch).
                    update_instance(transaction)
                else:
                    # LIVE (SQLite): do NOT call update_instance(transaction) here. That
                    # helper copies EVERY attribute from this in-memory `transaction` object
                    # onto the DB row, so if some OTHER field (e.g. take_profit/stop_loss)
                    # was set by a different session after THIS object was fetched -- which
                    # is exactly what happens here: this runs right after the entry order is
                    # submitted, moments after the ruleset's TP/SL adjustment committed in
                    # its own session -- that write gets silently reverted (found 2026-07-22:
                    # a live goal6 TP was set by the ruleset, then wiped back to None by this
                    # exact call). Fetch fresh and set only `.quantity` so the commit only
                    # ever touches that one column.
                    with get_db() as session:
                        db_txn = session.get(Transaction, transaction_id)
                        if db_txn:
                            db_txn.quantity = total_quantity
                            session.commit()
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
            from ba2_common.core.types import OrderType

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
            from ba2_common.core.models import ExpertSetting
            from sqlmodel import select
            from ba2_common.core.db import get_db

            # Expert settings are loaded directly from the database below; the
            # concrete expert class is resolved by the live host (InstanceResolver)
            # rather than imported here, so ba2_common never depends on the expert
            # package layout.

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
    
    def _get_transaction_entry_order(self, transaction_id) -> Optional[TradingOrder]:
        """Return the first (entry) order for a transaction, loaded within a session.

        Avoids lazy-loading ``transaction.trading_orders`` on a detached Transaction
        (which raises 'not bound to a Session' and silently skips validation).
        """
        if transaction_id is None:
            return None
        # orders_where is the dual-path equivalent of the raw select() this replaced
        # (review 2026-07-18, M2): a raw select(TradingOrder) silently finds nothing when
        # the backtest in-mem store is active. No ORDER BY/LIMIT support on the routed
        # helper, so take the lowest-id order in Python instead (small per-transaction set).
        from ba2_common.core.trade_store import orders_where
        candidates = orders_where(transaction_id=transaction_id)
        if not candidates:
            return None
        return min(candidates, key=lambda o: o.id)

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
        entry_order = self._get_transaction_entry_order(transaction.id)
        if entry_order and entry_order.side == trading_order.side:
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
            from ba2_common.core.instance_resolver import get_instance_resolver

            expert_interface = get_instance_resolver().get_expert_instance(transaction.expert_id)
            if not expert_interface:
                return errors
            
            # Check if adding to existing position
            is_adding_to_position = False
            entry_order = self._get_transaction_entry_order(transaction.id)
            if entry_order and entry_order.side == trading_order.side:
                is_adding_to_position = True

            if not is_adding_to_position:
                # New position - exclude this order's own WAITING transaction from used
                # balance, since it was already persisted before validation runs and
                # would otherwise be double-counted against itself.
                available_balance = expert_interface.get_available_balance(exclude_transaction_id=transaction.id)
                if available_balance is None:
                    # AN UNREADABLE BALANCE IS NOT A PASS. This used to ``return errors``
                    # with the list still empty, which every caller reads as "validated,
                    # no problems". ``TastyTradeAccount.get_balance()`` returns None on ANY
                    # exception, so one broker hiccup silently opened the gate. Same rule
                    # as the equity branch in _validate_position_size_limits below.
                    logger.error(
                        f"EXPERT BALANCE VALIDATION CANNOT RUN for {trading_order.symbol}: "
                        f"expert {expert_instance.id} published no available balance "
                        f"(get_available_balance() returned None). Rejecting the order "
                        f"rather than treating an unrun risk check as passed."
                    )
                    errors.append(
                        f"Cannot validate the expert's available balance: it is unavailable "
                        f"for expert {expert_instance.id}. Refusing the order rather than "
                        f"skipping the check."
                    )
                    return errors

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
                available_balance = expert_interface.get_available_balance()
                if available_balance is None:
                    # Same rule as the new-position branch above: the add-to-position path
                    # has its own balance read and its own bare ``return errors``, so
                    # fixing only one of them leaves the gate open for every top-up.
                    logger.error(
                        f"EXPERT BALANCE VALIDATION CANNOT RUN for {trading_order.symbol} "
                        f"(adding to an existing position): expert {expert_instance.id} "
                        f"published no available balance. Rejecting the order rather than "
                        f"treating an unrun risk check as passed."
                    )
                    errors.append(
                        f"Cannot validate the expert's available balance before adding to "
                        f"the {trading_order.symbol} position: it is unavailable for expert "
                        f"{expert_instance.id}. Refusing the order rather than skipping "
                        f"the check."
                    )
                    return errors

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
            # Same rule as _validate_position_size_limits: an exception inside a risk
            # control must not be reported to the caller as "validation passed".
            logger.error(
                f"EXPERT BALANCE VALIDATION FAILED TO RUN for {trading_order.symbol} "
                f"on account {self.id}: {balance_error}",
                exc_info=True,
            )
            errors.append(
                f"Expert available-balance validation could not be completed "
                f"({type(balance_error).__name__}: {balance_error}). Refusing the order "
                f"rather than treating an unrun risk check as passed."
            )

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
            from ba2_common.core.db import get_instance
            from ba2_common.core.models import Transaction, ExpertInstance
            
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
            
            # Calculate expert's virtual equity.
            #
            # READ EQUITY THROUGH THE TYPED SEAM, never off get_account_info(). That
            # return value is BROKER-SHAPED: a pydantic TradeAccount on Alpaca, a plain
            # dict on IBKR/TastyTrade, {} on auth failure. `float(account_info.equity)`
            # therefore raised AttributeError for every dict-shaped broker, the broad
            # `except Exception` below swallowed it, and BOTH guards -- the
            # per-instrument cap AND the expert virtual-balance check below -- silently
            # returned "no problems". get_account_snapshot() is the broker-agnostic
            # seam that exists for exactly this (same fix as TradeActions.py Task 34).
            snapshot = self.get_account_snapshot()
            account_equity = snapshot.equity
            if account_equity is None:
                # REFUSE TO VALIDATE, do not pass. `None` means the broker published no
                # equity at all -- there is no denominator, so the cap cannot be
                # checked. Reporting that as success is what made this bug invisible.
                logger.error(
                    f"POSITION SIZE VALIDATION CANNOT RUN for {trading_order.symbol}: "
                    f"account {self.id} published no equity "
                    f"({self.__class__.__name__}.get_account_snapshot().equity is None). "
                    f"Rejecting the order rather than treating an unrun risk check as passed."
                )
                errors.append(
                    f"Cannot validate position size limits: account equity is unavailable "
                    f"from {self.__class__.__name__}. Refusing the order rather than "
                    f"skipping the check."
                )
                return errors

            account_equity = float(account_equity)
            virtual_equity_pct = expert_instance.virtual_equity_pct
            virtual_equity = account_equity * (virtual_equity_pct / 100.0)
            
            # Get current price
            current_price = self.get_instrument_current_price(trading_order.symbol)
            if current_price is None:
                # NO PRICE MEANS NEITHER GATE RAN. Both the per-instrument cap and the
                # expert available-balance check below are priced off this quote, so a
                # bare ``return errors`` here skipped BOTH of them and reported the order
                # as validated -- on a MARKET order that already carries a transaction_id,
                # i.e. one that is about to be sent. Same rule as the equity branch above.
                logger.error(
                    f"POSITION SIZE VALIDATION CANNOT RUN for {trading_order.symbol} on "
                    f"account {self.id}: no current price is available "
                    f"({self.__class__.__name__}.get_instrument_current_price returned None), "
                    f"so neither the per-instrument cap nor the expert balance check could be "
                    f"priced. Rejecting the order rather than treating an unrun risk check "
                    f"as passed."
                )
                errors.append(
                    f"position-size validation could not run: no price for "
                    f"{trading_order.symbol}. Refusing the order rather than skipping "
                    f"the check."
                )
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
            # A CRASH IN A RISK CONTROL IS NOT A PASS. This used to log a warning and
            # return an empty list, which every caller reads as "no problems found" --
            # so an AttributeError here was indistinguishable from a clean validation
            # and disabled the position-size cap for a whole broker without a single
            # failing test. An unrun check is reported as a failure to validate.
            logger.error(
                f"POSITION SIZE VALIDATION FAILED TO RUN for {trading_order.symbol} "
                f"on account {self.id}: {e}",
                exc_info=True,
            )
            errors.append(
                f"Position size validation could not be completed "
                f"({type(e).__name__}: {e}). Refusing the order rather than treating "
                f"an unrun risk check as passed."
            )

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
        from ba2_common.core.db import get_instance
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
    
    def submit_close_order_for_transaction(
        self,
        transaction: "Transaction",
        last_broker_canceled_order_id: Optional[int] = None,
    ) -> dict:
        """
        Create and submit (or defer) a market close order for a transaction.

        If ``last_broker_canceled_order_id`` is provided, the close order is
        created as PENDING with a dependency on that order reaching CANCELED
        status.  This prevents "insufficient qty" errors that occur when Alpaca
        still holds shares against a just-canceled TP/SL OCO order.

        If no dependency is needed the order is submitted to the broker
        immediately (``is_closing_order=True`` bypasses hedging checks).

        Returns a dict with keys: success, message, close_order_id.
        """
        from ba2_common.core.db import add_instance
        from ba2_common.core.types import OrderDirection, OrderType

        close_side = OrderDirection.SELL if transaction.side == OrderDirection.BUY else OrderDirection.BUY

        # SIZE THE CLOSE OFF WHAT WE MEASURED, AND NOTHING ELSE.
        #
        # This was ``abs(get_current_open_qty()) or transaction.quantity``. A net of zero
        # is a MEASURED answer -- "the book is flat" -- but ``or`` cannot tell that from
        # "unknown", so it fell through to ``transaction.quantity``: the quantity that was
        # ORDERED, which after a partial exit / external close / assignment no longer
        # describes anything the account holds. The close then acted on a number it never
        # measured. On a long-only cash account Alpaca rejects it (40310000) instead of
        # opening a short, which is luck, not a safeguard.
        net = abs(transaction.get_current_open_qty())
        if net <= 0:
            logger.info(
                f"submit_close_order_for_transaction: transaction {transaction.id} "
                f"({transaction.symbol}) has a net open quantity of {net}; already flat, "
                f"nothing to close. NOT falling back to the ordered quantity "
                f"({transaction.quantity})."
            )
            return {
                "success": True,
                "message": (
                    f"{transaction.symbol} transaction {transaction.id} is already flat "
                    f"(net open quantity 0) — nothing to close"
                ),
                "close_order_id": None,
            }
        current_qty = net

        close_order = TradingOrder(
            account_id=self.id,
            symbol=transaction.symbol,
            quantity=current_qty,
            side=close_side,
            order_type=OrderType.MARKET,
            transaction_id=transaction.id,
            comment=f'Closing position for transaction {transaction.id}',
        )

        if last_broker_canceled_order_id:
            close_order.status = OrderStatus.PENDING
            close_order.depends_on_order = last_broker_canceled_order_id
            close_order.depends_order_status_trigger = OrderStatus.CANCELED
            order_id = add_instance(close_order, expunge_after_flush=True)
            logger.info(
                f"submit_close_order_for_transaction: created deferred close order {order_id} "
                f"for {transaction.symbol} — depends on order {last_broker_canceled_order_id} "
                f"reaching CANCELED"
            )
            from ba2_common.core.utils import log_close_order_activity
            log_close_order_activity(
                transaction=transaction,
                account_id=self.id,
                success=True,
                close_order_id=order_id,
                quantity=current_qty,
                side=close_side,
            )
            return {
                "success": True,
                "message": (
                    f"Close order queued for {transaction.symbol} — "
                    f"will submit once TP/SL cancellation is confirmed"
                ),
                "close_order_id": order_id,
            }
        else:
            submitted = self.submit_order(close_order, is_closing_order=True)
            if submitted:
                close_order_id = submitted.id if hasattr(submitted, 'id') else None
                logger.info(
                    f"submit_close_order_for_transaction: submitted close order {close_order_id} "
                    f"for {transaction.symbol}"
                )
                from ba2_common.core.utils import log_close_order_activity
                log_close_order_activity(
                    transaction=transaction,
                    account_id=self.id,
                    success=True,
                    close_order_id=close_order_id,
                    quantity=current_qty,
                    side=close_side,
                )
                return {"success": True, "message": f"Closing order submitted for {transaction.symbol}", "close_order_id": close_order_id}
            else:
                logger.error(f"submit_close_order_for_transaction: submission failed for {transaction.symbol}")
                from ba2_common.core.utils import log_close_order_activity
                log_close_order_activity(
                    transaction=transaction,
                    account_id=self.id,
                    success=False,
                    error_message="Order submission returned None",
                )
                return {"success": False, "message": "Failed to submit closing order", "close_order_id": None}

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
        from contextlib import nullcontext
        from datetime import datetime, timezone
        from sqlmodel import select, Session
        from ba2_common.core.db import get_db, delete_instance, update_instance
        from ba2_common.core.types import OrderDirection, OrderType, TransactionStatus, OrderStatus
        from ba2_common.core import trade_store as _ts

        # BT sql-less store (flag-on): read/persist orders+txn through the in-memory store, no SQLite
        # session for them. LIVE (flag-off) keeps the exact session + query + batched-commit path.
        inmem = _ts.inmem_trades_active()

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
            session_cm = nullcontext(None) if inmem else Session(get_db().bind)
            with session_cm as session:
                all_orders = _ts.orders_where(
                    account_id=self.id, transaction_id=transaction_id, session=session)
                all_orders = sorted(
                    all_orders,
                    key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))

                if not all_orders:
                    result['message'] = 'No orders found for this transaction'
                    return result
                
                # Process orders
                unfilled_statuses = OrderStatus.get_unfilled_statuses()
                executed_statuses = OrderStatus.get_executed_statuses()
                unsent_statuses = OrderStatus.get_unsent_statuses()
                has_filled = False
                existing_close_order = None
                last_broker_canceled_order_id = None  # Track for deferred close trigger

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
                            if inmem:
                                update_instance(order)
                            else:
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
                                last_broker_canceled_order_id = order.id
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
                                    # get_positions() is TRI-STATE: a list on success, [] when the
                                    # account is genuinely flat, and None when the FETCH ITSELF
                                    # FAILED (network/DNS/auth). Collapsing None into [] via
                                    # `broker_positions or []` made a transient broker outage read
                                    # as "the position is gone" and FORCE-CLOSE a real, still-open
                                    # transaction as position_not_at_broker (the 2026-07-03 DNS
                                    # incident, in the shape AlpacaAccount.get_positions' docstring
                                    # warns about). An unverified book is NOT an empty book: abort
                                    # the close decision and fall through to the retry, exactly as
                                    # the `except` branch below does for an exception and as
                                    # ReadOnlyAccountInterface.reconcile_externally_closed_transactions
                                    # does with `if positions is None: return 0`.
                                    if broker_positions is None:
                                        logger.error(
                                            f"Cannot verify whether position {transaction.symbol} still exists "
                                            f"at broker for transaction {transaction_id}: "
                                            f"{self.__class__.__name__}.get_positions() returned None "
                                            f"(FETCH FAILURE, not a flat account). Refusing to conclude the "
                                            f"position is gone; proceeding with close order retry."
                                        )
                                    else:
                                        position_exists = any(
                                            pos.get('symbol') == transaction.symbol if isinstance(pos, dict)
                                            else getattr(pos, 'symbol', None) == transaction.symbol
                                            for pos in broker_positions
                                        )

                                        if not position_exists:
                                            logger.info(
                                                f"Position {transaction.symbol} no longer exists at broker - "
                                                f"marking transaction {transaction_id} as CLOSED without retry"
                                            )
                                            # Mark the ERROR order as CANCELED (not needed anymore)
                                            existing_close_order.status = OrderStatus.CANCELED
                                            # Mark transaction as CLOSED with logging
                                            from ba2_common.core.utils import close_transaction_with_logging
                                            close_transaction_with_logging(
                                                transaction=transaction,
                                                account_id=self.id,
                                                close_reason="position_not_at_broker",
                                                session=session
                                            )
                                            if inmem:
                                                update_instance(existing_close_order)
                                                update_instance(transaction)
                                            else:
                                                session.add(existing_close_order)
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
                                
                                # Mark the errored order as CANCELED and create a fresh one
                                # via the helper (which handles TP/SL deferred submission)
                                existing_close_order.status = OrderStatus.CANCELED
                                if inmem:
                                    update_instance(existing_close_order)
                                else:
                                    session.add(existing_close_order)
                                    session.commit()

                                close_result = self.submit_close_order_for_transaction(
                                    transaction, last_broker_canceled_order_id
                                )
                                result['success'] = close_result['success']
                                result['close_order_id'] = close_result['close_order_id']
                                result['message'] = close_result['message']
                                if result['canceled_count'] > 0:
                                    result['message'] += f' ({result["canceled_count"]} orders canceled)'
                                if result['deleted_count'] > 0:
                                    result['message'] += f' ({result["deleted_count"]} waiting orders deleted)'
                            except Exception as e:
                                logger.error(f"Error retrying close order: {e}", exc_info=True)
                                result['message'] = f'Error retrying close order: {str(e)}'
                                from ba2_common.core.utils import log_close_order_activity
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
                        # No existing close order - create a new one (deferred if TP/SL were canceled)
                        logger.info(f"Creating new closing order for transaction {transaction_id}")
                        close_result = self.submit_close_order_for_transaction(
                            transaction, last_broker_canceled_order_id
                        )
                        result['success'] = close_result['success']
                        result['close_order_id'] = close_result['close_order_id']
                        result['message'] = close_result['message']
                        if result['canceled_count'] > 0:
                            result['message'] += f' ({result["canceled_count"]} orders canceled)'
                        if result['deleted_count'] > 0:
                            result['message'] += f' ({result["deleted_count"]} waiting orders deleted)'
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
                    from ba2_common.core.utils import close_transaction_with_logging
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
                    if inmem:
                        update_instance(transaction)
                    else:
                        session.add(transaction)
                    result['message'] += ' (transaction closed)'

                if not inmem:
                    session.commit()

            return result
            
        except Exception as e:
            logger.error(f"Error closing transaction {transaction_id}: {e}", exc_info=True)
            result['message'] = f'Error: {str(e)}'
            
            # Log activity for transaction close failure
            try:
                from ba2_common.core.db import log_activity
                from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                
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
