"""Storage-agnostic access to trade rows.

Callers ask for trades. They do NOT know, and must not ask, whether those rows live in the
backtest's in-memory store or in SQLite — that is decided in exactly ONE place, the resolver at
the bottom of this module. A condition, a risk rule or an expert says "give me this expert's
open transactions on AAPL" and gets them, in a backtest and live alike.

WHY THIS EXISTS
---------------
``TradingOrder`` / ``Transaction`` / ``ExpertRecommendation`` are ``IN_MEM_MODELS``: during a
RAM-only backtest (``backtest_trading_db(..., in_memory=True)``) their rows live in
``trade_store``'s dicts and NEVER reach the run's SQLite tables. A raw ``select(Transaction)``
against that run therefore returns EMPTY instead of raising — the decision built on it silently
evaluates to "nothing found", and it looks like ordinary data rather than a bug.

MEASURED (2026-07-30): ``DaysSinceLastCloseCondition`` did exactly this. With a close 3 days old
and a ">15 day" cooldown it read the 1e9 "never closed" sentinel, so the gate was inert for a
WHOLE optimization and the GA spent its search tuning a gene that did nothing. The same genome
scored 103 trades / 17.55% annualised on the in-memory path and 169 / 0.20% on the SQLite one.
Live has no in-memory store, so live behaved like the second — a deployed config running gates
its own backtest never exercised.

THE SHAPE
---------
``TradeRepository`` is the interface. Two implementations own the storage detail:

    InMemoryTradeRepository  -> the backtest store's dicts
    SqlTradeRepository       -> SQLModel / SQLite (live, and file-backed backtests)

``get_trade_repository()`` picks one. Tests (and any seam that wants to substitute a fake) call
``set_trade_repository()`` instead of reaching into the storage layer, so nothing above this
module mentions a backend.

Joins are expressed as "open transactions -> their orders" rather than SQL joins, because the
in-memory side has no join and a SQL-only formulation would force two different call-site
shapes — the very split this module removes.
"""
from abc import ABC, abstractmethod
from typing import Any, List, Optional

from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.types import AssetClass, OrderStatus, TransactionStatus
from ba2_common.core.utils import calculate_transaction_pnl


class TradeRepository(ABC):
    """Read access to trade rows, independent of where they are stored."""

    # -- backend-specific ------------------------------------------------
    @abstractmethod
    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        """One Transaction by id, or None if it does not exist."""

    @abstractmethod
    def open_transactions(self, *, expert_id: int, symbol: Optional[str] = None,
                          side: Optional[Any] = None) -> List[Transaction]:
        """This expert's OPENED transactions, optionally narrowed by symbol and/or side."""

    @abstractmethod
    def closed_transactions(self, *, expert_id: int,
                            symbol: Optional[str] = None) -> List[Transaction]:
        """This expert's CLOSED transactions that carry a ``close_date``, NEWEST CLOSE FIRST.

        Implementations must apply the ordering themselves so both backends return the same
        sequence — callers rely on "newest first" meaning "most recent close".
        """

    @abstractmethod
    def _orders_for_transactions(self, transaction_ids: List[Any]) -> List[TradingOrder]:
        """Every TradingOrder attached to any of ``transaction_ids``."""

    # -- storage-independent logic, shared by every backend ---------------
    def last_closed_transaction(self, *, expert_id: int, symbol: str,
                                profit_sign: int = 0) -> Optional[Transaction]:
        """Most recent qualifying close, or None.

        ``profit_sign``: 0 = any close, +1 = only profitable, -1 = only losing. A transaction
        whose P&L cannot be computed is skipped when a sign is requested — it cannot be shown
        to qualify.
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

    def open_option_orders(self, *, expert_id: int, underlying: str,
                           option_type: Optional[Any] = None,
                           side: Optional[Any] = None,
                           option_strategy: Optional[str] = None) -> List[TradingOrder]:
        """Non-terminal OPTION orders on ``underlying`` under this expert's OPEN transactions."""
        open_ids = [t.id for t in self.open_transactions(expert_id=expert_id) if t.id is not None]
        if not open_ids:
            return []
        terminal = OrderStatus.get_terminal_statuses()
        out = []
        for o in self._orders_for_transactions(open_ids):
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

    @staticmethod
    def _newest_close_first(txns: List[Transaction]) -> List[Transaction]:
        out = [t for t in txns if t.close_date is not None]
        out.sort(key=lambda t: t.close_date, reverse=True)
        return out


class InMemoryTradeRepository(TradeRepository):
    """Backed by the backtest in-memory trade store."""

    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        if transaction_id is None:
            return None
        from ba2_common.core.trade_store import store_get
        return store_get(Transaction, transaction_id)

    def open_transactions(self, *, expert_id: int, symbol: Optional[str] = None,
                          side: Optional[Any] = None) -> List[Transaction]:
        from ba2_common.core.trade_store import store_all
        return [
            t for t in store_all(Transaction)
            if t.expert_id == expert_id
            and t.status == TransactionStatus.OPENED
            and (symbol is None or t.symbol == symbol)
            and (side is None or t.side == side)
        ]

    def closed_transactions(self, *, expert_id: int,
                            symbol: Optional[str] = None) -> List[Transaction]:
        from ba2_common.core.trade_store import store_all
        return self._newest_close_first([
            t for t in store_all(Transaction)
            if t.expert_id == expert_id
            and t.status == TransactionStatus.CLOSED
            and (symbol is None or t.symbol == symbol)
        ])

    def _orders_for_transactions(self, transaction_ids: List[Any]) -> List[TradingOrder]:
        from ba2_common.core.trade_store import store_all
        wanted = set(transaction_ids)
        return [o for o in store_all(TradingOrder) if o.transaction_id in wanted]


class SqlTradeRepository(TradeRepository):
    """Backed by SQLModel/SQLite — live, and file-backed backtests."""

    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        if transaction_id is None:
            return None
        from ba2_common.core.db import get_db
        with get_db() as session:
            return session.get(Transaction, transaction_id)

    def open_transactions(self, *, expert_id: int, symbol: Optional[str] = None,
                          side: Optional[Any] = None) -> List[Transaction]:
        from sqlmodel import select
        from ba2_common.core.db import get_db
        stmt = select(Transaction).where(
            Transaction.expert_id == expert_id,
            Transaction.status == TransactionStatus.OPENED,
        )
        if symbol is not None:
            stmt = stmt.where(Transaction.symbol == symbol)
        if side is not None:
            stmt = stmt.where(Transaction.side == side)
        with get_db() as session:
            return list(session.exec(stmt).all())

    def closed_transactions(self, *, expert_id: int,
                            symbol: Optional[str] = None) -> List[Transaction]:
        from sqlmodel import select
        from ba2_common.core.db import get_db
        stmt = select(Transaction).where(
            Transaction.expert_id == expert_id,
            Transaction.status == TransactionStatus.CLOSED,
            Transaction.close_date.is_not(None),
        )
        if symbol is not None:
            stmt = stmt.where(Transaction.symbol == symbol)
        with get_db() as session:
            rows = list(session.exec(stmt).all())
        # Sorted in Python, not SQL, so both backends order identically.
        return self._newest_close_first(rows)

    def _orders_for_transactions(self, transaction_ids: List[Any]) -> List[TradingOrder]:
        from sqlmodel import select
        from ba2_common.core.db import get_db
        stmt = select(TradingOrder).where(TradingOrder.transaction_id.in_(list(transaction_ids)))
        with get_db() as session:
            return list(session.exec(stmt).all())


_IN_MEMORY = InMemoryTradeRepository()
_SQL = SqlTradeRepository()
_override: Optional[TradeRepository] = None


def get_trade_repository() -> TradeRepository:
    """The repository for the CURRENT execution context.

    This is the single place in the codebase that maps "how was this process started" onto a
    storage backend. Resolved per call, not cached, because a process runs live code and
    backtest trials at different moments (and the backtest override is thread-local).
    """
    if _override is not None:
        return _override
    from ba2_common.core.trade_store import inmem_trades_active
    return _IN_MEMORY if inmem_trades_active() else _SQL


def set_trade_repository(repo: Optional[TradeRepository]) -> None:
    """Force a specific repository (tests, or a seam substituting a fake). None restores
    automatic resolution — always reset in a fixture teardown, since this is process-wide."""
    global _override
    _override = repo


def reset_trade_repository() -> None:
    set_trade_repository(None)
