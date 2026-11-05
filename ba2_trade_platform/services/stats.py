"""Overview-page figures, UI-free: trade performance by period, order counts, profit per expert, position
distribution. The Overview widgets and ``GET /stats/overview`` call these same functions (one function, two callers);
``tests/golden_overview/test_stats_golden.py`` holds them to what the inline widget code computed before the extraction.

Scoping rule (see ``services/account_scope.py``): ``account_id is None`` is "All accounts" and ONLY that widens a query."""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlmodel import func, select

from ba2_common.core.utils import calculate_transaction_pnl

from ..core.db import get_db
from ..core.models import AccountDefinition, ExpertInstance, Instrument, TradingOrder, Transaction
from ..core.types import OrderStatus, OrderType, TransactionStatus
from ..logger import logger
from ._common import require_row
from .account_scope import scope_transactions_to_account
from .errors import Invalid


def expert_display_name(expert: ExpertInstance) -> str:
    """The name every per-expert Overview figure uses: alias (or expert type) plus the id."""
    return f"{expert.alias or expert.expert}-{expert.id}"


def expert_ids_for_account(account_id: Optional[int]) -> Optional[List[int]]:
    """``None`` for "All"; otherwise the account's expert ids (``[]`` for an account with no experts: that means NO
    rows, never "widen")."""
    if account_id is None:
        return None
    with get_db() as session:
        return list(session.exec(select(ExpertInstance.id).where(ExpertInstance.account_id == account_id)).all())


def _pnl_stats(transactions) -> Dict[str, Any]:
    """Total, wins, losses of the transactions whose P&L can be computed; ``unmeasured`` counts the rest (they are
    left out of the total, never added as 0)."""
    pnls = []
    unmeasured = 0
    for txn in transactions:
        pnl = calculate_transaction_pnl(txn)
        if pnl is None:
            unmeasured += 1
        else:
            pnls.append(pnl)
    return {"total": sum(pnls), "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p < 0), "unmeasured": unmeasured, "measured": len(pnls)}


def trade_performance(account_id: Optional[int], now: Optional[datetime] = None, *,
                      strict: bool = False) -> Dict[str, Any]:
    """The Trade Performance widget: open/closed counts, win/loss and P&L of the last 7 / 30 days against the
    previous 7 / 30. Scoped by ACCOUNT (a transaction's account is its orders' account), not by expert.

    TIME: ``now`` and the windows are SERVER-LOCAL naive time, exactly as the Overview page computes them (the same
    convention ``closed_pnl`` uses). ``strict=True`` (the API) reports a window's P&L as ``None`` when it has closed
    trades but none could be measured; the page keeps the number 0 it always drew."""
    now = datetime.now() if now is None else now
    d7, d14, d30, d60 = (now - timedelta(days=n) for n in (7, 14, 30, 60))
    with get_db() as session:
        def scoped(query):
            return scope_transactions_to_account(query, account_id)

        total_count = session.exec(scoped(select(func.count(Transaction.id)))).first() or 0
        open_count = session.exec(scoped(
            select(func.count(Transaction.id)).where(Transaction.status == TransactionStatus.OPENED))).first() or 0

        def closed_in_range(start, end):
            q = scoped(select(Transaction)).where(
                Transaction.status == TransactionStatus.CLOSED, Transaction.close_date.isnot(None),
                Transaction.close_date >= start, Transaction.close_date < end)
            return session.exec(q).all()

        w7, wp7 = _pnl_stats(closed_in_range(d7, now)), _pnl_stats(closed_in_range(d14, d7))
        closed_30d = closed_in_range(d30, now)
        w30, wp30 = _pnl_stats(closed_30d), _pnl_stats(closed_in_range(d60, d30))
    if strict:
        for w in (w7, wp7, w30, wp30):
            if w["unmeasured"] and w["measured"] == 0:
                w["total"] = None
    return {
        "total_count": total_count, "open_count": open_count, "closed_count": len(closed_30d),
        "pnl_7d": w7["total"], "wins_7d": w7["wins"], "losses_7d": w7["losses"], "unmeasured_7d": w7["unmeasured"],
        "pnl_prev_7d": wp7["total"],
        "pnl_30d": w30["total"], "wins_30d": w30["wins"], "losses_30d": w30["losses"],
        "unmeasured_30d": w30["unmeasured"], "pnl_prev_30d": wp30["total"],
    }


def order_counts(account_id: Optional[int]) -> List[Dict[str, Any]]:
    """The Orders-by-Account widget: MARKET order counts per account (open = FILLED/NEW/OPEN/ACCEPTED, pending =
    PENDING/WAITING_TRIGGER, error)."""
    with get_db() as session:
        accounts = [a for a in session.exec(select(AccountDefinition)).all()
                    if account_id is None or a.id == account_id]

        def count(account, statuses):
            return session.exec(
                select(func.count(TradingOrder.id)).where(TradingOrder.account_id == account.id)
                .where(TradingOrder.status.in_(statuses)).where(TradingOrder.order_type == OrderType.MARKET)
            ).first() or 0
        return [{"account_id": a.id, "account": a.name,
                 "open": count(a, [OrderStatus.FILLED, OrderStatus.NEW, OrderStatus.OPEN, OrderStatus.ACCEPTED]),
                 "pending": count(a, [OrderStatus.PENDING, OrderStatus.WAITING_TRIGGER]),
                 "error": count(a, [OrderStatus.ERROR])} for a in accounts]


def profit_per_expert(account_id: Optional[int], skipped: Optional[List[Dict[str, Any]]] = None) -> Dict[str, float]:
    """The Profit Per Expert chart: realized P&L of the closed, expert-attributed transactions, per expert,
    highest first. A closed transaction whose P&L cannot be computed is skipped (it has no figure to add), and
    one that raises on bad data is skipped too; both are appended to ``skipped`` (``{transaction, reason}``) when the
    caller passes a list."""
    expert_ids = expert_ids_for_account(account_id)
    if expert_ids is not None and not expert_ids:
        return {}
    profits: Dict[str, float] = {}
    with get_db() as session:
        query = (select(Transaction).where(Transaction.status == TransactionStatus.CLOSED)
                 .where(Transaction.expert_id.isnot(None)).where(Transaction.open_price.isnot(None))
                 .where(Transaction.close_price.isnot(None)))
        if expert_ids is not None:
            query = query.where(Transaction.expert_id.in_(expert_ids))
        for transaction in session.exec(query).all():
            try:
                expert = session.get(ExpertInstance, transaction.expert_id)
                if not expert:
                    logger.warning(f"Expert instance {transaction.expert_id} not found for transaction {transaction.id}")
                    if skipped is not None:
                        skipped.append({"transaction": transaction.id, "reason": "expert instance not found"})
                    continue
                profit = calculate_transaction_pnl(transaction)
                if profit is None:
                    if skipped is not None:
                        skipped.append({"transaction": transaction.id, "reason": "pnl not computable"})
                    continue
                name = expert_display_name(expert)
                profits[name] = profits.get(name, 0.0) + profit
            except Exception as e:  # noqa: BLE001 -- the page's own behaviour (75431922): ONE bad row never blanks the chart
                logger.warning(f"Skipping transaction {transaction.id} in profit per expert: {type(e).__name__}: {e}")
                if skipped is not None:
                    skipped.append({"transaction": transaction.id, "reason": type(e).__name__})
    return dict(sorted(profits.items(), key=lambda x: x[1], reverse=True))


def position_distribution(positions: Iterable[Dict[str, Any]], grouping_field: str,
                          skipped: Optional[List[Dict[str, Any]]] = None) -> Dict[str, float]:
    """Market value (absolute) of ``positions`` grouped by the instrument's first label or category.
    ``grouping_field`` is ``'labels'`` or ``'categories'``; the other list, then the instrument type, is the fallback;
    a symbol with no Instrument row is 'Uncategorized'. A position with no symbol, an unparseable or missing market
    value, or any other unreadable field is SKIPPED and the rest still count (one bad row never blanks the chart); the
    skipped ones are appended to ``skipped`` (``{symbol, reason}``) when the caller passes a list. Highest value
    first."""
    positions = list(positions or [])
    if not positions:
        return {}
    distribution: Dict[str, float] = defaultdict(float)
    with get_db() as session:
        for position in positions:
            symbol = position.get('symbol', '') if isinstance(position, dict) else ''
            if not symbol:
                if skipped is not None:
                    skipped.append({"symbol": None, "reason": "no symbol"})
                continue
            try:
                raw = position.get('market_value', '0')
                market_value = float(raw) if isinstance(raw, str) else raw
                instrument = session.exec(select(Instrument).where(Instrument.name == symbol)).first()
                if instrument:
                    first, second = ((instrument.labels, instrument.categories) if grouping_field == 'labels'
                                     else (instrument.categories, instrument.labels))
                    if first and len(first) > 0:
                        category = first[0]
                    elif second and len(second) > 0:
                        category = second[0]
                    else:
                        category = instrument.instrument_type.value if instrument.instrument_type else 'Unknown'
                else:
                    category = 'Uncategorized'
                distribution[category] += abs(market_value)
            except Exception as e:  # noqa: BLE001 -- the page's own behaviour (75431922): ONE bad row never blanks the chart
                logger.warning(f"Skipping position {symbol} in the distribution: {type(e).__name__}: {e}")
                if skipped is not None:
                    skipped.append({"symbol": symbol, "reason": type(e).__name__})
    return dict(sorted(distribution.items(), key=lambda x: x[1], reverse=True))


#: group_by values of ``closed_pnl``.
CLOSED_PNL_GROUPS = ("day", "month", "expert", "symbol")
#: Longest window a closed-P&L request may cover (the rows are read into memory to be grouped).
CLOSED_PNL_MAX_DAYS = 365 * 5
CLOSED_PNL_DEFAULT_DAYS = 90
NO_EXPERT = "(no expert)"


def closed_pnl(account_id: int, from_: Optional[datetime], to: Optional[datetime], group_by: str,
               *, limit: int, offset: int) -> Dict[str, Any]:
    """Realized P&L of the account's closed transactions with ``close_date`` in ``[from_, to]`` (``to`` inclusive,
    like ``/transactions``), grouped by day / month / expert / symbol. The P&L of one transaction is
    ``calculate_transaction_pnl`` -- the function the Overview's Trade Performance and Profit-Per-Expert figures use.
    A closed transaction whose P&L cannot be computed is COUNTED in ``unmeasured`` (per group and in the total) and
    left out of the sums; it is never added as 0, and a group (or the total) in which EVERY trade is unmeasured has
    ``pnl: null``. Groups are paged (newest / largest first).

    TIME: server-local naive time, the Overview page's own convention (``trade_performance`` uses the same). A
    timezone-aware ``from``/``to`` is converted to server-local; a naive one is taken as server-local already; the
    default ``to`` is now (local)."""
    if group_by not in CLOSED_PNL_GROUPS:
        raise Invalid(f"group_by={group_by!r} is not one of {list(CLOSED_PNL_GROUPS)}", {"field": "group_by"})
    def local(moment: datetime) -> datetime:
        return moment.astimezone().replace(tzinfo=None) if moment.tzinfo is not None else moment
    to_n = local(to) if to is not None else datetime.now()
    from_n = local(from_) if from_ is not None else to_n - timedelta(days=CLOSED_PNL_DEFAULT_DAYS)
    if from_n > to_n:
        raise Invalid("from is after to", {"from": from_n.isoformat(), "to": to_n.isoformat()})
    if (to_n - from_n).days > CLOSED_PNL_MAX_DAYS:
        raise Invalid(f"The window is longer than {CLOSED_PNL_MAX_DAYS} days", {"max_days": CLOSED_PNL_MAX_DAYS})
    groups: Dict[str, List[Transaction]] = defaultdict(list)
    with get_db() as session:
        require_row(session, AccountDefinition, account_id, "account")
        query = scope_transactions_to_account(select(Transaction), account_id).where(
            Transaction.status == TransactionStatus.CLOSED, Transaction.close_date.isnot(None),
            Transaction.close_date >= from_n, Transaction.close_date <= to_n)
        experts = {e.id: expert_display_name(e) for e in session.exec(select(ExpertInstance)).all()}
        for tx in session.exec(query).all():
            if group_by == "day":
                key = tx.close_date.strftime("%Y-%m-%d")
            elif group_by == "month":
                key = tx.close_date.strftime("%Y-%m")
            elif group_by == "symbol":
                key = tx.symbol
            else:
                key = experts[tx.expert_id] if tx.expert_id in experts else (NO_EXPERT if tx.expert_id is None
                                                                             else f"expert-{tx.expert_id} (deleted)")
            groups[key].append(tx)
    rows = []
    for key, txs in groups.items():
        st = _pnl_stats(txs)
        rows.append({"group": key, "pnl": st["total"] if st["measured"] or not st["unmeasured"] else None, "trades": len(txs), "wins": st["wins"], "losses": st["losses"],
                     "unmeasured": st["unmeasured"]})
    if group_by in ("day", "month"):
        rows.sort(key=lambda r: r["group"], reverse=True)
    else:
        rows.sort(key=lambda r: (r["pnl"] is None, -(r["pnl"] if r["pnl"] is not None else 0.0), r["group"]))
    every = [tx for txs in groups.values() for tx in txs]
    total = _pnl_stats(every)
    return {"account_id": account_id, "group_by": group_by, "from": from_n.isoformat(), "to": to_n.isoformat(),
            "timezone": "server-local naive time, as the Overview page",
            "total": {"pnl": total["total"] if total["measured"] or not total["unmeasured"] else None, "trades": len(every), "wins": total["wins"], "losses": total["losses"],
                      "unmeasured": total["unmeasured"]},
            "items": rows[offset:offset + limit], "groups": len(rows), "limit": limit, "offset": offset}
