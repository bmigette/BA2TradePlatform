"""Portfolio Allocation service: the live wiring between the pure engine and reality.

This module is LIVE-ONLY (it touches the DB and a broker), so it belongs in-tree
rather than in ba2_common -- it is NOT a shim, and there is no
``ba2_common.core.portfolio_allocation_service``. Every *decision* it makes is
delegated to a pure function in ``ba2_common.core.portfolio_allocation`` or to the
persistence layer ``ba2_common.core.portfolio_allocation_store``; what lives here
is the IO: reading positions/prices/margin metadata, running the broker precheck,
creating TradingOrder rows, and driving the run audit.

Do not confuse it with ``ba2_trade_platform/core/portfolio_allocation.py``, which
IS a shim (for the pure engine).
"""
from typing import Any, Dict, List, Optional

from sqlmodel import select

from ..logger import logger
from .db import get_db
from .models import Transaction, TradingOrder
from .portfolio_allocation import (
    AllocationPlan, MarginInfo, PositionFetchFailed, PositionState, apply_order_impacts,
)
from .types import OrderStatus, OrderType, TransactionStatus


def _open_transaction_ids(account_id: int, symbols: List[str]) -> Dict[str, List[int]]:
    """``{symbol: [transaction_id]}`` for OPENED/CLOSING transactions, oldest first.

    Transaction has NO account_id column -- it links to an account only through
    ``TradingOrder.account_id``, hence the join. Ordering is by primary key,
    which is creation order, so submission can consume them FIFO.
    """
    if not symbols:
        return {}
    out: Dict[str, List[int]] = {}
    with get_db() as session:
        statement = select(Transaction).join(TradingOrder).where(
            TradingOrder.account_id == account_id,
            Transaction.symbol.in_(symbols),
            Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSING]),
        ).distinct()
        for txn in session.exec(statement).all():
            out.setdefault(txn.symbol, []).append(txn.id)
    return {symbol: sorted(ids) for symbol, ids in out.items()}


def build_position_states(account, symbols: List[str]) -> Dict[str, PositionState]:
    """Positions + live prices + open transaction ids for the managed symbols.

    A managed symbol with no position is returned FLAT (quantity 0) but priced,
    so the wizard can open a position in it. A symbol with no price keeps
    ``price=None`` and the engine will skip it with a reason.

    Raises:
        PositionFetchFailed: when ``get_positions()`` returned None. The class is
            defined in the pure engine, so the UI's view module raises the same one.
    """
    wanted = []
    for raw in symbols:
        if raw and raw.strip():
            normalised = raw.strip().upper()
            if normalised not in wanted:
                wanted.append(normalised)

    positions = account.get_positions()
    if positions is None:
        raise PositionFetchFailed(
            f"get_positions() returned None for account {account.id}: the broker fetch "
            f"failed. Refusing to treat it as a flat account."
        )

    held: Dict[str, Any] = {}
    for position in positions:
        symbol = (getattr(position, 'symbol', '') or '').strip().upper()
        if symbol in wanted:
            held[symbol] = position

    prices = account.get_instrument_current_price(wanted) if wanted else {}
    if not isinstance(prices, dict):
        prices = {}
    txn_ids = _open_transaction_ids(account.id, wanted)

    states: Dict[str, PositionState] = {}
    for symbol in wanted:
        position = held.get(symbol)
        states[symbol] = PositionState(
            symbol=symbol,
            quantity=float(getattr(position, 'qty', 0.0) or 0.0) if position else 0.0,
            cost_basis=float(getattr(position, 'cost_basis', 0.0) or 0.0) if position else 0.0,
            price=prices.get(symbol),
            market_value=float(getattr(position, 'market_value', 0.0) or 0.0) if position else 0.0,
            transaction_ids=list(txn_ids.get(symbol, [])),
        )
    return states


def fetch_margin_info(account, symbols: List[str]) -> Dict[str, MarginInfo]:
    """``{symbol: MarginInfo}`` from the broker, tolerating brokers without the seam.

    A symbol the broker cannot describe is OMITTED; the engine falls back to the
    conservative ``default_bp_factor``, which under-deploys rather than
    over-commits.
    """
    if not symbols:
        return {}
    try:
        info = account.get_symbol_margin_info(list(symbols))
    except Exception as e:
        logger.error(f"get_symbol_margin_info failed for account {account.id}: {e}", exc_info=True)
        return {}
    return info or {}


def precheck_plan(account, plan: AllocationPlan, *, available_buying_power: float,
                  margin: Optional[Dict[str, MarginInfo]]) -> AllocationPlan:
    """Re-solve the plan against broker order prechecks, when the broker has them.

    Solve once (the caller has already done that), build the candidate BUY
    orders, dry-run each through ``preview_order_impact``, and re-solve ONLY if
    at least one impact came back. Alpaca has no order-preview endpoint and
    returns None for every row, so its deterministic per-asset margin data
    stands and this returns the SAME plan object -- no second solve.

    The candidate orders are never persisted and never submitted.

    BUYS ONLY, deliberately. Sells free buying power and ``_apply_bp_scaling``
    never scales them, so a sell impact could not change the plan -- while
    ``apply_order_impacts`` ZEROES a row whose impact came back
    ``accepted=False``, so one flaky close preview would silently hold a
    position the user asked to exit. Under the long-only design an allocation
    buy is always an OPENING order, and ``is_closing_order=False`` is passed
    EXPLICITLY rather than left to the seam's default, because the preview must
    price exactly what submission would send (a close mispriced as a short open
    is commit 1d099e8; the same mistake the other way round would reject a
    legitimate buy). If sells are ever added here they are CLOSES and must pass
    True.

    ``margin`` is a REQUIRED keyword: pass the same dict the plan was solved
    with (``{}`` when the broker described nothing). Without it the re-solve
    rebuilds a bare ``MarginInfo`` per fractional row and rounds on the default
    4dp grid, losing ``min_trade_increment``, ``min_order_size`` and
    ``min_fractional_notional`` -- a broker rejection that looks like a correct
    plan right up to submission.
    """
    preview = getattr(account, "preview_order_impact", None)
    if preview is None:
        # ReadOnlyAccountInterface has no such method: "cannot preview", not an
        # AttributeError, and never a zero-valued OrderImpact.
        return plan

    impacts: Dict[str, Any] = {}
    for row in plan.buy_rows:
        candidate = TradingOrder(
            account_id=account.id,
            symbol=row.symbol,
            quantity=abs(row.delta_quantity),
            side=row.side,
            order_type=OrderType.MARKET,
            good_for='day',
            status=OrderStatus.PENDING,
        )
        try:
            impact = preview(candidate, is_closing_order=False)
        except Exception as e:
            logger.error(f"preview_order_impact failed for {row.symbol}: {e}", exc_info=True)
            impact = None
        # `is None`, never falsiness: OrderImpact.bp_cost is a real 0.0 for an
        # order that FREES buying power, and dropping those loses the headroom.
        if impact is not None:
            impacts[row.symbol] = impact

    if not impacts:
        return plan

    logger.info(f"Allocation precheck returned {len(impacts)} broker impact(s); re-solving")
    return apply_order_impacts(plan, impacts,
                               available_buying_power=available_buying_power,
                               margin=margin)
