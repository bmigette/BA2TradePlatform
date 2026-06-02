"""Rebalance math and execution for FactorRanker.

``rebalance_deltas`` is pure (target weights + holdings + prices -> signed share
deltas) and unit tested directly. ``FactorPortfolioManager`` (added later) wraps it
with DB/account access to actually submit the orders.
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlmodel import select

from ba2_trade_platform.core.db import add_instance, get_db, get_instance
from ba2_trade_platform.core.models import ExpertInstance, TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.logger import logger


def rebalance_deltas(target_weights: Dict[str, float], held_shares: Dict[str, float],
                     prices: Dict[str, float], equity: float) -> Dict[str, float]:
    """Signed whole-share deltas to move from current holdings to target weights.

    target_shares = floor(weight * equity / price); delta = target - held. Names
    held but absent from the target weight 0 (sold down). A held name we must exit
    but cannot price is still fully sold using its held quantity. Zero deltas are
    omitted from the result.
    """
    deltas: Dict[str, float] = {}
    symbols = set(target_weights) | set(held_shares)
    for s in symbols:
        price = prices.get(s)
        if price is None or price <= 0:
            # Can't price a held name we must exit -> still allow full sell using held qty
            if s in held_shares and target_weights.get(s, 0.0) == 0.0:
                deltas[s] = -float(held_shares[s])
            continue
        target_shares = math.floor((target_weights.get(s, 0.0) * equity) / price)
        delta = target_shares - float(held_shares.get(s, 0.0))
        if delta != 0.0:
            deltas[s] = float(delta)
    return deltas


class FactorPortfolioManager:
    """Diffs FactorRanker target weights against current holdings and submits the
    buy/sell deltas directly (no ExpertRecommendation, no SmartRiskManager).

    Expert attribution flows through ``Transaction.expert_id``: new positions
    pre-create a Transaction stamped with this expert's id (the same path the
    SmartRiskManager uses), so the buys are recognised as this expert's holdings
    on the next rebalance.
    """

    def __init__(self, expert_instance_id: int):
        from ba2_trade_platform.core.utils import (
            get_account_instance_from_id, get_expert_instance_from_id,
        )
        self.expert_instance_id = expert_instance_id
        self.expert = get_expert_instance_from_id(expert_instance_id)
        instance = get_instance(ExpertInstance, expert_instance_id)
        self.account_id = instance.account_id
        self.account = get_account_instance_from_id(instance.account_id)

    # ------------------------------------------------------------------
    # Holdings
    # ------------------------------------------------------------------

    def get_holdings(self):
        """Return (held_shares {symbol: qty}, transactions_by_symbol {symbol: [Transaction]})
        for this expert's OPENED transactions."""
        with get_db() as session:
            transactions = session.exec(
                select(Transaction)
                .where(Transaction.expert_id == self.expert_instance_id)
                .where(Transaction.status == TransactionStatus.OPENED)
            ).all()

        held: Dict[str, float] = {}
        by_symbol: Dict[str, list] = {}
        for trans in transactions:
            qty = trans.get_current_open_qty()
            if qty == 0:
                continue
            held[trans.symbol] = held.get(trans.symbol, 0.0) + qty
            by_symbol.setdefault(trans.symbol, []).append(trans)
        return held, by_symbol

    # ------------------------------------------------------------------
    # Rebalance
    # ------------------------------------------------------------------

    def rebalance(self, target_weights: Dict[str, float], equity: Optional[float] = None) -> List[TradingOrder]:
        """Submit the buy/sell orders needed to move current holdings to the targets.

        Returns the list of submitted orders.
        """
        held, by_symbol = self.get_holdings()
        symbols = set(target_weights) | set(held)
        prices = {s: self.account.get_instrument_current_price(s) for s in symbols}

        if equity is None:
            equity = self.expert.get_virtual_balance()
        if equity is None:
            raise ValueError("FactorRanker: virtual balance (equity) not available for rebalance")

        deltas = rebalance_deltas(target_weights, held, prices, equity)

        submitted: List[TradingOrder] = []
        for sym, delta in deltas.items():
            order = self._submit_delta(sym, int(delta), by_symbol.get(sym, []))
            if order is not None:
                submitted.append(order)
        logger.info(
            f"FactorRanker[{self.expert_instance_id}]: rebalance submitted {len(submitted)} orders "
            f"(equity={equity:.2f}, deltas={deltas})"
        )
        return submitted

    def _submit_delta(self, symbol: str, delta: int, transactions: list) -> Optional[TradingOrder]:
        if delta > 0:
            return self._submit_buy(symbol, delta, transactions)
        if delta < 0:
            return self._submit_sell(symbol, -delta, transactions)
        return None

    def _submit_buy(self, symbol: str, qty: int, transactions: list) -> Optional[TradingOrder]:
        if qty <= 0:
            return None
        if transactions:
            # Adding to an existing position — link to its OPENED transaction.
            transaction_id = transactions[0].id
        else:
            # New position — pre-create an expert-attributed transaction so the
            # holding is recognised on the next rebalance (attribution path 1).
            price = self.account.get_instrument_current_price(symbol)
            trans = Transaction(
                symbol=symbol, quantity=qty, side=OrderDirection.BUY,
                status=TransactionStatus.WAITING, open_price=price,
                open_date=datetime.now(timezone.utc), expert_id=self.expert_instance_id,
            )
            transaction_id = add_instance(trans)

        order = TradingOrder(
            account_id=self.account_id, symbol=symbol, quantity=qty,
            side=OrderDirection.BUY, order_type=OrderType.MARKET,
            transaction_id=transaction_id, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment="FactorRanker rebalance buy",
            # Deliberate rebalance sizing — never let transaction qty-sync resize it.
            data={"fixed_quantity": True},
        )
        return self.account.submit_order(order, is_closing_order=False)

    def _submit_sell(self, symbol: str, qty: int, transactions: list) -> Optional[TradingOrder]:
        if qty <= 0 or not transactions:
            return None
        # Reduce/exit — attach to the existing OPENED transaction.
        order = TradingOrder(
            account_id=self.account_id, symbol=symbol, quantity=qty,
            side=OrderDirection.SELL, order_type=OrderType.MARKET,
            transaction_id=transactions[0].id, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC,
            comment="FactorRanker rebalance sell",
            data={"fixed_quantity": True},
        )
        return self.account.submit_order(order, is_closing_order=True)
