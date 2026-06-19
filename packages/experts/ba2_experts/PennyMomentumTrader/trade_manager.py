"""
PennyTradeManager - Position sizing and trade execution for PennyMomentumTrader.

Manages:
1. Pre-calculating position sizes weighted by confidence score
2. Clamping quantities to available balance at execution time
3. Creating expert-attributed Transaction + TradingOrder records for entries
4. Handling partial and full exits
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from sqlmodel import select

from ba2_common.core.db import get_db, get_instance, add_instance
from ba2_common.core.models import (
    ExpertInstance, TradingOrder, Transaction,
)
from ba2_common.core.types import (
    OrderDirection, OrderOpenType, OrderStatus,
    OrderType, TransactionStatus,
)
from ba2_common.logger import logger


# Back-off window before retrying an exit whose previous attempt failed with a
# transient broker error. Prevents a once-per-monitor-tick retry storm.
_EXIT_RETRY_COOLDOWN = timedelta(minutes=30)

# Substrings (lowercase) in a rejected exit order's comment that mean the asset is
# permanently non-tradable (e.g. delisted/halted). When seen, auto-exits stop
# entirely — retrying can never succeed and only floods the order table.
_NON_TRADABLE_ERROR_MARKERS = (
    "not active",
    "not tradable",
    "not currently tradable",
    "is not tradeable",
)


def find_open_entry_buy(session, transaction_id: int):
    """Return the entry BUY order for a transaction that is still working at the
    broker, or None.

    A SELL submitted while an opposing BUY is open is rejected by Alpaca as a wash
    trade. An entry buy counts as "open" when it is partially filled or in any
    unfilled status. Dependent legs (TP/SL, i.e. ``depends_on_order`` set) are never
    treated as the entry order.
    """
    open_statuses = list(OrderStatus.get_unfilled_statuses()) + [OrderStatus.PARTIALLY_FILLED]
    return session.exec(
        select(TradingOrder)
        .where(TradingOrder.transaction_id == transaction_id)
        .where(TradingOrder.side == OrderDirection.BUY)
        .where(TradingOrder.depends_on_order.is_(None))
        .where(TradingOrder.status.in_(open_statuses))
    ).first()


class PennyTradeManager:
    """Position sizing and trade execution for PennyMomentumTrader."""

    def __init__(self, expert_instance_id: int):
        from ba2_common.core.instance_resolver import get_instance_resolver
        resolver = get_instance_resolver()
        self.expert_instance_id = expert_instance_id
        self.expert = resolver.get_expert_instance(expert_instance_id)
        instance = get_instance(ExpertInstance, expert_instance_id)
        self.account = resolver.get_account_instance(instance.account_id)
        self.account_id = instance.account_id

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_position_sizes(
        self, candidates: list, available_balance: float
    ) -> Dict[str, dict]:
        """
        Weight position sizes by confidence score.

        Args:
            candidates: List of dicts with keys: symbol, confidence, price
            available_balance: Available balance to allocate

        Returns:
            Dict mapping symbol -> {qty, allocation, confidence, price}
        """
        total_confidence = sum(c["confidence"] for c in candidates)
        if total_confidence == 0:
            return {}

        max_per_instrument_pct = self.expert.get_setting_with_interface_default(
            "max_virtual_equity_per_instrument_percent", log_warning=False
        )
        virtual_balance = self.expert.get_virtual_balance()
        max_per_instrument = virtual_balance * max_per_instrument_pct / 100.0

        result: Dict[str, dict] = {}
        for c in candidates:
            weight = c["confidence"] / total_confidence
            raw_allocation = available_balance * weight
            allocation = min(raw_allocation, max_per_instrument)
            price = c["price"]
            if price is None or price <= 0:
                continue
            qty = int(allocation / price)  # Whole shares only
            if qty <= 0:
                continue
            result[c["symbol"]] = {
                "qty": qty,
                "allocation": round(allocation, 2),
                "confidence": c["confidence"],
                "price": price,
            }
        return result

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        symbol: str,
        qty: int,
        confidence: float,
        catalyst: str,
        strategy: str,
        exit_conditions: Optional[dict] = None,
        market_analysis_id: Optional[int] = None,
        limit_slippage_pct: float = 3.0,
    ) -> Optional[int]:
        """
        Execute a buy entry for *symbol*.

        Creates an expert-attributed Transaction and a TradingOrder linked to it,
        then submits through the account interface. Clamps qty to available balance
        and per-instrument limit at execution time. No ExpertRecommendation is
        created — attribution flows through Transaction.expert_id.

        Returns:
            The TradingOrder id on success, or None on failure.
        """
        # Re-check available balance at execution time
        available = self.expert.get_available_balance()
        current_price = self.account.get_instrument_current_price(symbol)

        if current_price is None or current_price <= 0:
            logger.error(f"Cannot execute entry for {symbol}: price unavailable")
            return None

        max_affordable = int(available / current_price)
        clamped_qty = min(qty, max_affordable)
        if clamped_qty <= 0:
            logger.warning(f"Cannot execute entry for {symbol}: insufficient balance")
            return None

        # Clamp to per-instrument limit
        max_per_pct = self.expert.get_setting_with_interface_default(
            "max_virtual_equity_per_instrument_percent", log_warning=False
        )
        virtual_balance = self.expert.get_virtual_balance()
        max_per_instrument = virtual_balance * max_per_pct / 100.0
        max_instrument_qty = int(max_per_instrument / current_price)
        clamped_qty = min(clamped_qty, max_instrument_qty)
        if clamped_qty <= 0:
            logger.warning(
                f"Cannot execute entry for {symbol}: exceeds per-instrument limit"
            )
            return None

        # --- Transaction (expert-attributed) ---
        # Pre-create the transaction stamped with expert_id so order/expert
        # attribution flows through Transaction.expert_id (the same path
        # FactorRanker and the SmartRiskManager use). No ExpertRecommendation is
        # created; the entry order links to this transaction instead.
        transaction = Transaction(
            symbol=symbol,
            quantity=clamped_qty,
            side=OrderDirection.BUY,
            open_price=current_price,
            status=TransactionStatus.WAITING,
            expert_id=self.expert_instance_id,
            meta_data={
                "strategy": strategy,
                "catalyst": catalyst,
                "exit_conditions": exit_conditions,
                "market_analysis_id": market_analysis_id,
            },
        )
        transaction_id = add_instance(transaction)

        # --- TradingOrder (submit_order handles persistence + broker call) ---
        if limit_slippage_pct > 0:
            order_type = OrderType.BUY_LIMIT
            limit_price = round(current_price * (1 + limit_slippage_pct / 100.0), 4)
        else:
            order_type = OrderType.MARKET
            limit_price = None

        order = TradingOrder(
            account_id=self.account_id,
            symbol=symbol,
            quantity=clamped_qty,
            side=OrderDirection.BUY,
            order_type=order_type,
            limit_price=limit_price,
            status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment=f"PennyMomentum entry: {catalyst}",
            transaction_id=transaction_id,
            good_for='day',  # Expire at EOD; re-evaluated each day by the monitor
        )

        try:
            submitted = self.account.submit_order(order)
            if submitted and submitted.id:
                order_type_label = f"LIMIT@${limit_price:.4f}" if limit_price else "MARKET"
                logger.info(
                    f"PennyTradeManager: entry order {submitted.id} placed for "
                    f"{symbol} x{clamped_qty} ({order_type_label})"
                )
                from ba2_common.core.db import log_activity
                from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.SUCCESS,
                    activity_type=ActivityLogType.ORDER_SUBMITTED,
                    description=(
                        f"PennyMomentumTrader entry order placed: BUY {symbol} "
                        f"x{clamped_qty} {order_type_label} (quote=${current_price:.2f}) "
                        f"| Catalyst: {catalyst}"
                    ),
                    data={
                        "symbol": symbol,
                        "qty": clamped_qty,
                        "price": current_price,
                        "catalyst": catalyst,
                        "strategy": strategy,
                        "order_id": submitted.id,
                    },
                    source_expert_id=self.expert_instance_id,
                    source_account_id=self.account_id,
                )
                return submitted.id
            logger.error(
                f"PennyTradeManager: submit_order returned no id for {symbol}"
            )
            return None
        except Exception as e:
            logger.error(
                f"PennyTradeManager: failed to place entry order for {symbol}: {e}",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Exit execution
    # ------------------------------------------------------------------

    def execute_exit(
        self, symbol: str, exit_pct: float = 100.0, reason: str = "exit condition met"
    ) -> bool:
        """
        Exit an open position for *symbol*.

        Finds all open transactions owned by this expert for the symbol
        and submits SELL market orders.

        Args:
            symbol: Ticker to exit.
            exit_pct: Percentage of position to close (100.0 = full exit).
            reason: Human-readable reason for the exit.

        Returns:
            True if at least one exit order was successfully submitted.
        """
        with get_db() as session:
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id == self.expert_instance_id)
                .where(Transaction.symbol == symbol)
                .where(Transaction.status == TransactionStatus.OPENED)
            ).all()

        if not transactions:
            logger.warning(f"No open transactions found for {symbol}")
            return False

        any_success = False
        for trans in transactions:
            current_qty = abs(trans.get_current_open_qty())
            if current_qty <= 0:
                continue

            # Guard: skip if a pending sell order already exists for this transaction.
            # This prevents duplicate exits when shares are already held_for_orders at the
            # broker (Alpaca code 40310000 "insufficient qty available").
            with get_db() as session:
                existing_sell = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .where(TradingOrder.side == OrderDirection.SELL)
                    .where(TradingOrder.status.in_(list(OrderStatus.get_unfilled_statuses())))
                ).first()
            if existing_sell:
                logger.info(
                    f"PennyTradeManager: exit for {symbol} skipped — "
                    f"pending sell order {existing_sell.id} (status={existing_sell.status}) already exists"
                )
                any_success = True
                continue

            # Guard against exit retry storms. Inspect the most recent SELL for this
            # transaction; a terminal ERROR means the previous exit was rejected by
            # the broker. Two cases:
            #   * permanently non-tradable asset (delisted/halted) -> stop forever
            #   * any other (transient) failure -> back off for a cooldown window
            # Without this, a stop-loss on a frozen symbol re-fires every monitor
            # tick and floods the order table (see SUUN: 296 ERROR sells in 5h).
            with get_db() as session:
                last_sell = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .where(TradingOrder.side == OrderDirection.SELL)
                    .order_by(TradingOrder.created_at.desc())
                ).first()
            if last_sell is not None and last_sell.status == OrderStatus.ERROR:
                comment = (last_sell.comment or "").lower()
                if any(marker in comment for marker in _NON_TRADABLE_ERROR_MARKERS):
                    logger.error(
                        f"PennyTradeManager: exit for {symbol} permanently blocked — "
                        f"broker rejects the asset as non-tradable (last exit order "
                        f"{last_sell.id}: {last_sell.comment}). No further auto-exit "
                        f"attempts; transaction {trans.id} needs manual resolution."
                    )
                    any_success = True
                    continue
                last_ts = last_sell.created_at
                if last_ts is not None:
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - last_ts
                    if age < _EXIT_RETRY_COOLDOWN:
                        mins = int(age.total_seconds() // 60)
                        cd = int(_EXIT_RETRY_COOLDOWN.total_seconds() // 60)
                        logger.warning(
                            f"PennyTradeManager: exit for {symbol} skipped — previous "
                            f"exit order {last_sell.id} failed {mins}m ago (< {cd}m "
                            f"cooldown). Last error: {last_sell.comment}"
                        )
                        any_success = True
                        continue

            exit_qty = int(current_qty * exit_pct / 100.0)
            if exit_qty <= 0:
                exit_qty = current_qty  # At minimum close 1 share

            # Detect an entry BUY still working at the broker. Submitting an opposing
            # SELL while it is open triggers Alpaca's wash-trade rejection, so stage
            # the exit to fire once the entry reaches FILLED.
            with get_db() as session:
                open_buy = find_open_entry_buy(session, trans.id)

            order = TradingOrder(
                account_id=self.account_id,
                symbol=symbol,
                quantity=exit_qty,
                side=OrderDirection.SELL,
                order_type=OrderType.MARKET,
                transaction_id=trans.id,
                status=OrderStatus.PENDING,
                open_type=OrderOpenType.AUTOMATIC,
                comment=f"PennyMomentum exit: {reason}",
                # Stepped exits are deliberately sized (often a partial of the
                # position); never let transaction qty-sync resize them.
                data={"fixed_quantity": True},
            )

            if open_buy is not None:
                # Stage as a triggered order; TradeManager submits it once the entry
                # buy reaches FILLED. Avoids the wash-trade rejection entirely.
                order.status = OrderStatus.WAITING_TRIGGER
                order.depends_on_order = open_buy.id
                order.depends_order_status_trigger = OrderStatus.FILLED
                try:
                    new_id = add_instance(order)
                    if new_id:
                        any_success = True
                        logger.info(
                            f"PennyTradeManager: exit for {symbol} staged as WAITING_TRIGGER "
                            f"(order {new_id} x{exit_qty}, depends on entry order "
                            f"{open_buy.id} reaching FILLED) — avoids wash trade"
                        )
                        from ba2_common.core.db import log_activity
                        from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                        log_activity(
                            severity=ActivityLogSeverity.INFO,
                            activity_type=ActivityLogType.ORDER_SUBMITTED,
                            description=(
                                f"PennyMomentumTrader exit staged (waiting for entry fill): "
                                f"SELL {symbol} x{exit_qty} ({exit_pct:.0f}%) | Reason: {reason}"
                            ),
                            data={
                                "symbol": symbol,
                                "qty": exit_qty,
                                "exit_pct": exit_pct,
                                "reason": reason,
                                "order_id": new_id,
                                "transaction_id": trans.id,
                                "depends_on_order": open_buy.id,
                            },
                            source_expert_id=self.expert_instance_id,
                            source_account_id=self.account_id,
                        )
                    else:
                        logger.error(
                            f"PennyTradeManager: failed to stage WAITING_TRIGGER exit "
                            f"for {symbol} — add_instance returned no id"
                        )
                except Exception as e:
                    logger.error(
                        f"PennyTradeManager: failed to stage exit for {symbol}: {e}",
                        exc_info=True,
                    )
                continue

            try:
                submitted = self.account.submit_order(
                    order, is_closing_order=True
                )
                if submitted and submitted.id:
                    logger.info(
                        f"PennyTradeManager: exit order {submitted.id} placed "
                        f"for {symbol} x{exit_qty}"
                    )
                    any_success = True
                    from ba2_common.core.db import log_activity
                    from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                    log_activity(
                        severity=ActivityLogSeverity.INFO,
                        activity_type=ActivityLogType.ORDER_SUBMITTED,
                        description=(
                            f"PennyMomentumTrader exit order placed: SELL {symbol} "
                            f"x{exit_qty} ({exit_pct:.0f}%) | Reason: {reason}"
                        ),
                        data={
                            "symbol": symbol,
                            "qty": exit_qty,
                            "exit_pct": exit_pct,
                            "reason": reason,
                            "order_id": submitted.id,
                            "transaction_id": trans.id,
                        },
                        source_expert_id=self.expert_instance_id,
                        source_account_id=self.account_id,
                    )
                else:
                    logger.error(
                        f"PennyTradeManager: submit_order returned no id "
                        f"for exit on {symbol}"
                    )
            except Exception as e:
                logger.error(
                    f"PennyTradeManager: failed to place exit for {symbol}: {e}",
                    exc_info=True,
                )

        return any_success

    # ------------------------------------------------------------------
    # Position queries
    # ------------------------------------------------------------------

    def get_open_positions(self) -> List[dict]:
        """
        Return all open positions for this expert instance.

        Each element is a dict with keys:
            transaction_id, symbol, qty, entry_price
        """
        with get_db() as session:
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id == self.expert_instance_id)
                .where(Transaction.status == TransactionStatus.OPENED)
            ).all()

        positions: List[dict] = []
        for trans in transactions:
            qty = abs(trans.get_current_open_qty())
            if qty <= 0:
                continue
            positions.append(
                {
                    "transaction_id": trans.id,
                    "symbol": trans.symbol,
                    "qty": qty,
                    "entry_price": trans.open_price,
                }
            )
        return positions
