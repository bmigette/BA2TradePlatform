"""
Floating P/L Per Expert Widget
Displays unrealized profit/loss for open positions grouped by expert.
"""
from typing import Dict, List, Tuple
from sqlmodel import select, Session
from ...logger import logger
from ...core.models import Transaction, ExpertInstance, TradingOrder
from .FloatingPLPerAccountWidget import _FloatingPLWidgetBase


class FloatingPLPerExpertWidget(_FloatingPLWidgetBase):
    """Widget component showing floating profit/loss per expert."""

    _title = '📊 Floating P/L Per Expert'

    def _get_extra_filters(self) -> list:
        """Only include transactions that have an expert attribution."""
        return [Transaction.expert_id.isnot(None)]

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        logger.debug(f"FloatingPLPerExpertWidget: Found {len(transactions)} open/waiting transactions with expert_id")

        account_transactions: Dict[int, List[Tuple[Transaction, str]]] = {}

        for trans in transactions:
            try:
                # Get expert info
                expert = session.get(ExpertInstance, trans.expert_id)
                if not expert:
                    logger.warning(f"Transaction {trans.id} (symbol={trans.symbol}, expert_id={trans.expert_id}) expert not found in database")
                    continue

                expert_name = f"{expert.alias or expert.expert}-{expert.id}"
                logger.debug(f"Processing transaction {trans.id} (symbol={trans.symbol}, status={trans.status.value}, expert={expert_name})")

                # Get account ID from first order
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()

                if not first_order or not first_order.account_id:
                    logger.warning(f"Transaction {trans.id} (symbol={trans.symbol}, expert={expert_name}) has no orders or account_id")
                    continue

                account_id = first_order.account_id

                if account_id not in account_transactions:
                    account_transactions[account_id] = []
                account_transactions[account_id].append((trans, expert_name))

            except Exception as e:
                logger.error(f"Error grouping transaction {trans.id}: {e}", exc_info=True)
                continue

        return account_transactions
