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
its own backtest never exercised. ``PennyMomentumTrader._exit_position`` had the same defect
with a blunter symptom: it found no open transactions in a backtest, so exits never fired.

THE SHAPE
---------
``TradeRepository`` is the interface. Two implementations own the storage detail:

    InMemoryTradeRepository  -> the backtest store's dicts
    SqlTradeRepository       -> SQLModel / SQLite (live, and file-backed backtests)

``get_trade_repository()`` picks one. Tests (and any seam substituting a fake) call
``set_trade_repository()`` instead of reaching into the storage layer, so nothing above this
module mentions a backend.

Only four operations are backend-specific (``transactions``, ``transaction``,
``_orders_for_transactions``, ``_orders_by_recommendation``); everything else is shared logic on
the base class so it cannot drift between backends. Each backend-specific method must push its
filters DOWN to the backend — the live tables are unbounded, so "fetch all then filter in
Python" is only acceptable where the row set is already scoped (one transaction, one expert).

Joins are expressed as "transactions -> their orders" rather than SQL joins, because the
in-memory side has no join and a SQL-only formulation would force two call-site shapes.
"""
from abc import ABC, abstractmethod
from typing import Any, Iterable, List, Optional

from ba2_common.core.models import ExpertRecommendation, Transaction, TradingOrder
from ba2_common.core.types import AssetClass, OrderStatus, TransactionStatus
from ba2_common.core.utils import calculate_transaction_pnl

# Why ``last_closed_transaction_with_reason`` found (or did not find) a close.
#
# "NEVER CLOSED" AND "COULD NOT DETERMINE" ARE DIFFERENT ANSWERS, and collapsing them
# into a bare ``None`` is what let DaysSinceLastCloseCondition fabricate its 1e9
# "infinitely long ago" sentinel for a close it simply could not classify — turning a
# re-entry cooldown into a gate that never fires. The sentinel is defensible for
# LAST_CLOSE_NONE (there is genuinely nothing to wait for) and is a made-up
# measurement for LAST_CLOSE_UNCLASSIFIABLE.
LAST_CLOSE_FOUND = "found"
LAST_CLOSE_NONE = "no_qualifying_close"       # knowable: no close qualifies
LAST_CLOSE_UNCLASSIFIABLE = "unclassifiable"  # NOT knowable: a close exists, unreadable


class TradeRepository(ABC):
    """Read access to trade rows, independent of where they are stored."""

    # -- backend-specific -------------------------------------------------
    @abstractmethod
    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        """One Transaction by id, or None if it does not exist."""

    @abstractmethod
    def transactions(self, *, expert_id: int, statuses: Iterable[Any],
                     symbol: Optional[str] = None) -> List[Transaction]:
        """This expert's transactions in any of ``statuses``, optionally one symbol."""

    @abstractmethod
    def _orders_for_transactions(self, transaction_ids: List[Any]) -> List[TradingOrder]:
        """Every TradingOrder attached to any of ``transaction_ids``."""

    @abstractmethod
    def _orders_by_recommendation(self, *, expert_id: int,
                                  symbol: Optional[str] = None) -> List[TradingOrder]:
        """Orders linked to this expert through their ``ExpertRecommendation``.

        A DIFFERENT linkage from ``_orders_for_transactions``: some experts attribute an order
        via ``expert_recommendation_id`` rather than through a transaction.
        """

    # -- shared logic, identical on every backend -------------------------
    def open_transactions(self, *, expert_id: int, symbol: Optional[str] = None,
                          side: Optional[Any] = None,
                          include_waiting: bool = False) -> List[Transaction]:
        """OPENED transactions, optionally narrowed by symbol and/or side.

        ``include_waiting`` also returns WAITING ones: a transaction stays WAITING until the
        account's ``refresh_transactions()`` cycle promotes it, which can lag the broker fill
        (separate refresh cadences). Callers that re-derive a target book from holdings need
        them, or a re-trigger between fill and promotion sees an empty book and re-buys from
        scratch (the 2026-07-14 incident where 3 rapid re-triggers 3x'd a live position).
        """
        statuses = ([TransactionStatus.OPENED, TransactionStatus.WAITING] if include_waiting
                    else [TransactionStatus.OPENED])
        txns = self.transactions(expert_id=expert_id, statuses=statuses, symbol=symbol)
        if side is not None:
            txns = [t for t in txns if t.side == side]
        return txns

    def closed_transactions(self, *, expert_id: int,
                            symbol: Optional[str] = None) -> List[Transaction]:
        """CLOSED transactions carrying a ``close_date``, NEWEST CLOSE FIRST.

        Ordered here rather than in SQL so both backends return the same sequence — callers
        rely on "newest first" meaning "most recent close".
        """
        return self._newest_close_first(
            self.transactions(expert_id=expert_id, statuses=[TransactionStatus.CLOSED],
                              symbol=symbol))

    def last_closed_transaction(self, *, expert_id: int, symbol: str,
                                profit_sign: int = 0) -> Optional[Transaction]:
        """Most recent qualifying close, or None.

        ``profit_sign``: 0 = any close, +1 = only profitable, -1 = only losing.

        A bare ``None`` cannot say WHY nothing was found. Callers that turn "nothing found"
        into a decision (a cooldown, a sentinel) must use
        ``last_closed_transaction_with_reason`` instead.
        """
        return self.last_closed_transaction_with_reason(
            expert_id=expert_id, symbol=symbol, profit_sign=profit_sign)[0]

    def last_closed_transaction_with_reason(self, *, expert_id: int, symbol: str,
                                            profit_sign: int = 0):
        """``(transaction, reason)`` — the most recent qualifying close and WHY.

        ``reason`` is one of ``LAST_CLOSE_FOUND`` / ``LAST_CLOSE_NONE`` /
        ``LAST_CLOSE_UNCLASSIFIABLE``. The last one is the point of this method: it means
        a close EXISTS but this repository cannot show whether it qualifies, so "nothing
        found" must NOT be read as "never closed".

        Two ways a close becomes unclassifiable:

          * ``profit_sign != 0`` and ``calculate_transaction_pnl`` returns None (an
            open/close price was never recorded), so its profit sign is unknown;
          * the row carries no ``close_date``, so it cannot be DATED at all — which makes
            every "days since" answer unknowable regardless of the sign.

        Ordering matters. A close whose sign is unknown and that is NEWER than an otherwise
        qualifying one HIDES it: "days since the last profitable close" is then either of
        two numbers, and picking the older is a fabricated measurement. An unclassifiable
        close OLDER than a match is irrelevant — the match already answers the question —
        so the scan returns as soon as it matches with nothing unreadable above it.
        """
        rows = self.transactions(expert_id=expert_id,
                                 statuses=[TransactionStatus.CLOSED], symbol=symbol)
        # A CLOSED row with no close_date is dropped by _newest_close_first. Note it here
        # rather than letting it vanish into "never closed".
        undated = any(t.close_date is None for t in rows)

        blind = False
        for txn in self._newest_close_first(rows):
            if profit_sign != 0:
                pnl = calculate_transaction_pnl(txn)
                if pnl is None:
                    blind = True
                    continue
                if profit_sign > 0 and pnl <= 0:
                    continue
                if profit_sign < 0 and pnl >= 0:
                    continue
            if blind or undated:
                return None, LAST_CLOSE_UNCLASSIFIABLE
            return txn, LAST_CLOSE_FOUND
        if blind or undated:
            return None, LAST_CLOSE_UNCLASSIFIABLE
        return None, LAST_CLOSE_NONE

    def orders_for_transaction(self, transaction_id: Any, *, side: Optional[Any] = None,
                               statuses: Optional[Iterable[Any]] = None,
                               entry_only: bool = False,
                               newest_first: bool = False) -> List[TradingOrder]:
        """This transaction's orders. Filtering is in Python because the row set is one
        transaction's legs — a handful — on either backend.

        ``entry_only`` keeps only orders with no ``depends_on_order`` (i.e. not a TP/SL leg).
        """
        if transaction_id is None:
            return []
        sset = set(statuses) if statuses is not None else None
        out = []
        for o in self._orders_for_transactions([transaction_id]):
            if side is not None and o.side != side:
                continue
            if sset is not None and o.status not in sset:
                continue
            if entry_only and o.depends_on_order is not None:
                continue
            out.append(o)
        if newest_first:
            out.sort(key=lambda o: (o.created_at is not None, o.created_at), reverse=True)
        return out

    def orders_by_recommendation(self, *, expert_id: int, symbol: Optional[str] = None,
                                 statuses: Optional[Iterable[Any]] = None) -> List[TradingOrder]:
        """Orders attributed to this expert via their recommendation."""
        orders = self._orders_by_recommendation(expert_id=expert_id, symbol=symbol)
        if statuses is None:
            return orders
        sset = set(statuses)
        return [o for o in orders if o.status in sset]

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

    def held_covered_calls(self, *, expert_id: int, underlying: str,
                           option_strategy: str = "covered_call") -> List[TradingOrder]:
        """The short CALL contracts this expert STILL HOLDS on ``underlying``, one row each.

        THE ONE RESOLVER for "which option did the covered-call overlay write", and it exists
        because the ordinary answer does not work for these keys. Every other option rule
        reads the option off ``existing_order.transaction_id`` — but on an equity-entry
        overlay key (``O_CC``, and ``O_WHEEL`` once its put has been assigned) the manage pass
        is anchored to the STOCK: ``daily_engine._manage_open_positions`` and the live
        ``TradeManager.process_open_positions_recommendations`` both evaluate once per SYMBOL
        with the OLDEST entry order, and ``SellCoveredCallAction`` puts the written call on
        its own transaction. A transaction-anchored option condition is therefore not merely
        wrong there, it is INERT — measured, both runtimes. So the written call is resolved
        the way ``HasCoveredCallCondition`` already resolves its existence: through this
        repository, keyed on (expert, underlying).

        NETTED OVER EXECUTED ROWS, which the existence check does not do and this must: a
        call that has been bought back still has its ``sell_to_open`` row on an open
        transaction, so a resolver that merely filtered ``side=SELL`` would keep reporting a
        position that is gone — and a DTE rule reading it would re-submit a close every bar.
        A contract is HELD when its signed executed quantity is still short.

        The returned row is the SELL that opened each still-held contract (it carries the
        strike, expiry, multiplier and fill price a close needs). Ordered by contract symbol
        so two callers on one book see the same list in the same order.
        """
        from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus as _OS

        rows = self.open_option_orders(expert_id=expert_id, underlying=underlying,
                                       option_type=OptionRight.CALL)
        executed = _OS.get_executed_statuses()
        net: dict = {}
        opener: dict = {}
        for o in rows:
            if o.status not in executed or not o.contract_symbol:
                continue
            qty = abs(float(o.filled_qty or o.quantity or 0.0))
            if qty <= 0:
                continue
            sign = 1.0 if o.side == OrderDirection.BUY else -1.0
            net[o.contract_symbol] = net.get(o.contract_symbol, 0.0) + sign * qty
            # The opening SELL, and only one tagged as the overlay: a short call that is the
            # leg of a SPREAD is not a covered call, and closing it as one would break the
            # spread. The tag is what ``submit_option_order`` stamped at entry.
            if (sign < 0 and (o.option_strategy or "").strip().lower() == option_strategy
                    and o.contract_symbol not in opener):
                opener[o.contract_symbol] = o
        return [opener[c] for c in sorted(opener)
                if net.get(c, 0.0) < -1e-9]

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

    def transactions(self, *, expert_id: int, statuses: Iterable[Any],
                     symbol: Optional[str] = None) -> List[Transaction]:
        from ba2_common.core.trade_store import store_all
        sset = set(statuses)
        return [
            t for t in store_all(Transaction)
            if t.expert_id == expert_id
            and t.status in sset
            and (symbol is None or t.symbol == symbol)
        ]

    def _orders_for_transactions(self, transaction_ids: List[Any]) -> List[TradingOrder]:
        from ba2_common.core.trade_store import store_all
        wanted = set(transaction_ids)
        return [o for o in store_all(TradingOrder) if o.transaction_id in wanted]

    def _orders_by_recommendation(self, *, expert_id: int,
                                  symbol: Optional[str] = None) -> List[TradingOrder]:
        from ba2_common.core.trade_store import store_all
        rec_ids = {r.id for r in store_all(ExpertRecommendation) if r.instance_id == expert_id}
        if not rec_ids:
            return []
        return [o for o in store_all(TradingOrder)
                if o.expert_recommendation_id in rec_ids
                and (symbol is None or o.symbol == symbol)]


class SqlTradeRepository(TradeRepository):
    """Backed by SQLModel/SQLite — live, and file-backed backtests."""

    def transaction(self, transaction_id: Any) -> Optional[Transaction]:
        if transaction_id is None:
            return None
        from ba2_common.core.db import get_db
        with get_db() as session:
            return session.get(Transaction, transaction_id)

    def transactions(self, *, expert_id: int, statuses: Iterable[Any],
                     symbol: Optional[str] = None) -> List[Transaction]:
        from sqlmodel import select
        from ba2_common.core.db import get_db
        stmt = select(Transaction).where(
            Transaction.expert_id == expert_id,
            Transaction.status.in_(list(statuses)),
        )
        if symbol is not None:
            stmt = stmt.where(Transaction.symbol == symbol)
        with get_db() as session:
            return list(session.exec(stmt).all())

    def _orders_for_transactions(self, transaction_ids: List[Any]) -> List[TradingOrder]:
        from sqlmodel import select
        from ba2_common.core.db import get_db
        stmt = select(TradingOrder).where(TradingOrder.transaction_id.in_(list(transaction_ids)))
        with get_db() as session:
            return list(session.exec(stmt).all())

    def _orders_by_recommendation(self, *, expert_id: int,
                                  symbol: Optional[str] = None) -> List[TradingOrder]:
        from sqlmodel import select
        from ba2_common.core.db import get_db
        stmt = (
            select(TradingOrder)
            .join(ExpertRecommendation,
                  TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
            .where(ExpertRecommendation.instance_id == expert_id)
        )
        if symbol is not None:
            stmt = stmt.where(TradingOrder.symbol == symbol)
        with get_db() as session:
            return list(session.exec(stmt).all())


_IN_MEMORY = InMemoryTradeRepository()
_SQL = SqlTradeRepository()
_override: Optional[TradeRepository] = None


def get_trade_repository() -> TradeRepository:
    """The repository for the CURRENT execution context.

    The single place in the codebase mapping "how was this process started" onto a storage
    backend. Resolved per call, not cached, because one process runs live code and backtest
    trials at different moments (and the backtest override is thread-local).
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
