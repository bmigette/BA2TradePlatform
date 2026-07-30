"""Storage-agnostic access to trade rows — ONE implementation for live and backtest.

WHY THIS EXISTS
---------------
``TradingOrder`` / ``Transaction`` / ``ExpertRecommendation`` live in DIFFERENT places
depending on how the process was started:

* **live** and file-backed backtests -> SQLite, reachable with ``select()`` via ``get_db()``
* **GA trials / RAM-only backtests**  -> ``trade_store``'s in-memory dicts
  (``backtest_trading_db(..., in_memory=True)`` activates it; see ``IN_MEM_MODELS``)

A raw ``select(Transaction)`` is blind to the second case: the rows never reach the run's
SQLite table, so the query returns EMPTY instead of raising. Any decision built on such a
query therefore silently evaluates to "nothing found" in every GA trial while working
correctly live — the failure is invisible because it looks like ordinary data.

That is not hypothetical. On 2026-07-30 ``DaysSinceLastCloseCondition`` was measured doing
exactly this: with a close 3 days old and a ">15 day" cooldown it returned the
"never closed" sentinel (1e9) under the in-memory store and the correct 3.0 under SQLite.
The cooldown gate was inert for the WHOLE optimization, so the GA spent its search tuning a
gene that did nothing, and the same genome then scored 103 trades / 17.55% annualised in a
GA trial versus 169 trades / 0.20% on the file-backed path. Live, which has no in-memory
store, behaves like the 0.20% arm — i.e. unlike its own backtest.

THE RULE
--------
**Never issue a raw ``select(Transaction)`` / ``select(TradingOrder)`` in decision code.**
Go through this repository. It delegates to ``trade_store``'s dual-mode helpers
(``transactions_where`` / ``orders_where`` / ``get_instance``), which pick the backend, so
call sites contain no ``inmem_trades_active()`` branch and both paths run the SAME code.

Joins are expressed as "fetch transactions, then fetch their orders by id" rather than a SQL
join, because ``trade_store.transactions_with_orders`` is in-mem-ONLY and would force call
sites back into two implementations. The id sets involved are the caller's own OPEN
transactions, so this stays small on both paths.
"""
from typing import Any, List, Optional

from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.types import OrderStatus, TransactionStatus
from ba2_common.core.utils import calculate_transaction_pnl


class TradeRepository:
    """Domain queries over trade rows, valid under BOTH storage backends."""

    # -- transactions ----------------------------------------------------
    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        """One Transaction by id, or None. ``get_instance`` already routes to the store."""
        if transaction_id is None:
            return None
        from ba2_common.core.db import get_instance
        return get_instance(Transaction, transaction_id)

    def open_transactions(self, *, expert_id: int, symbol: Optional[str] = None,
                          side: Optional[Any] = None) -> List[Transaction]:
        """This expert's OPENED transactions, optionally narrowed to a symbol and/or side.

        ``side`` is filtered in Python: ``transactions_where`` has no side filter, and doing it
        here keeps the single-code-path guarantee rather than reintroducing a raw query.
        """
        from ba2_common.core.trade_store import transactions_where
        txns = transactions_where(expert_id=expert_id, symbol=symbol,
                                  status=TransactionStatus.OPENED)
        if side is not None:
            txns = [t for t in txns if t.side == side]
        return list(txns)

    def closed_transactions(self, *, expert_id: int, symbol: Optional[str] = None,
                            ) -> List[Transaction]:
        """This expert's CLOSED transactions that carry a ``close_date``, NEWEST CLOSE FIRST.

        Ordering is done here (not in SQL) so both backends return the same sequence — callers
        rely on "newest first" to mean "most recent close".
        """
        from ba2_common.core.trade_store import transactions_where
        txns = [t for t in transactions_where(expert_id=expert_id, symbol=symbol,
                                              status=TransactionStatus.CLOSED)
                if t.close_date is not None]
        txns.sort(key=lambda t: t.close_date, reverse=True)
        return txns

    def last_closed_transaction(self, *, expert_id: int, symbol: str,
                                profit_sign: int = 0) -> Optional[Transaction]:
        """Most recent qualifying close, or None.

        ``profit_sign``: 0 = any close, +1 = only profitable, -1 = only losing. A transaction
        whose P&L cannot be computed is skipped when a sign is requested (it cannot be shown
        to qualify), matching the previous per-condition logic.
        """
        for txn in self.closed_transactions(expert_id=expert_id, symbol=symbol):
            if profit_sign != 0:
                pnl = calculate_transaction_pnl(txn)
                if pnl is None:
                    continue
                if profit_sign > 0 and pnl <= 0:
                    continue
                if profit_sign < 0 and pnl >= 0:
                    continue
            return txn
        return None

    # -- orders ----------------------------------------------------------
    def open_option_orders(self, *, expert_id: int, underlying: str,
                           option_type: Optional[Any] = None,
                           side: Optional[Any] = None,
                           option_strategy: Optional[str] = None) -> List[TradingOrder]:
        """Non-terminal OPTION orders on ``underlying`` belonging to this expert's OPEN
        transactions — the storage-agnostic form of the old
        ``select(TradingOrder).join(Transaction)...`` used by the option flag conditions.
        """
        from ba2_common.core.trade_store import orders_where
        open_ids = [t.id for t in self.open_transactions(expert_id=expert_id) if t.id is not None]
        if not open_ids:
            return []
        from ba2_common.core.types import AssetClass
        terminal = OrderStatus.get_terminal_statuses()
        out = []
        for o in orders_where(transaction_ids=open_ids):
            if o.status in terminal:
                continue
            if o.asset_class != AssetClass.OPTION:
                continue
            if o.underlying_symbol != underlying:
                continue
            if option_type is not None and o.option_type != option_type:
                continue
            if side is not None and o.side != side:
                continue
            if option_strategy is not None and o.option_strategy != option_strategy:
                continue
            out.append(o)
        return out


_REPOSITORY = TradeRepository()


def get_trade_repository() -> TradeRepository:
    """The process-wide repository. Stateless — the backend is chosen per call by
    ``trade_store``, so one instance is correct for live and for every concurrent trial."""
    return _REPOSITORY
