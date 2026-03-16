"""
PennyTradeManager - Position sizing and trade execution for PennyMomentumTrader.

Manages:
1. Pre-calculating position sizes weighted by confidence score
2. Clamping quantities to available balance at execution time
3. Creating ExpertRecommendation + TradingOrder records for entries
4. Handling partial and full exits
"""

from typing import Dict, List, Optional

from sqlmodel import select

from ba2_trade_platform.core.db import get_db, get_instance, add_instance
from ba2_trade_platform.core.models import (
    ExpertInstance, ExpertRecommendation, TradingOrder, Transaction,
)
from ba2_trade_platform.core.types import (
    OrderDirection, OrderOpenType, OrderRecommendation, OrderStatus,
    OrderType, RiskLevel, TimeHorizon, TransactionStatus,
)
from ba2_trade_platform.logger import logger


class PennyTradeManager:
    """Position sizing and trade execution for PennyMomentumTrader."""

    def __init__(self, expert_instance_id: int):
        from ba2_trade_platform.core.utils import get_account_instance_from_id, get_expert_instance_from_id
        self.expert_instance_id = expert_instance_id
        self.expert = get_expert_instance_from_id(expert_instance_id)
        instance = get_instance(ExpertInstance, expert_instance_id)
        self.account = get_account_instance_from_id(instance.account_id)
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
    ) -> Optional[int]:
        """
        Execute a buy entry for *symbol*.

        Creates an ExpertRecommendation and a TradingOrder, then submits
        through the account interface. Clamps qty to available balance and
        per-instrument limit at execution time.

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

        # --- ExpertRecommendation ---
        time_horizon = (
            TimeHorizon.SHORT_TERM
            if strategy == "intraday"
            else TimeHorizon.MEDIUM_TERM
        )
        rec = ExpertRecommendation(
            instance_id=self.expert_instance_id,
            symbol=symbol,
            recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=0.0,
            price_at_date=current_price,
            confidence=confidence,
            risk_level=RiskLevel.HIGH,  # Penny stocks are always HIGH risk
            time_horizon=time_horizon,
            details=f"Catalyst: {catalyst}",
            market_analysis_id=market_analysis_id,
            data={
                "strategy": strategy,
                "catalyst": catalyst,
                "exit_conditions": exit_conditions,
            },
        )
        rec_id = add_instance(rec)

        # --- TradingOrder (submit_order handles persistence + broker call) ---
        order = TradingOrder(
            account_id=self.account_id,
            symbol=symbol,
            quantity=clamped_qty,
            side=OrderDirection.BUY,
            order_type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment=f"PennyMomentum entry: {catalyst}",
            expert_recommendation_id=rec_id,
        )

        try:
            submitted = self.account.submit_order(order)
            if submitted and submitted.id:
                logger.info(
                    f"PennyTradeManager: entry order {submitted.id} placed for "
                    f"{symbol} x{clamped_qty}"
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

            exit_qty = int(current_qty * exit_pct / 100.0)
            if exit_qty <= 0:
                exit_qty = current_qty  # At minimum close 1 share

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
            )

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
