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
import re
from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from ..logger import logger
from .db import InstanceNotFound, add_instance, get_db, get_instance
from .models import Transaction, TradingOrder
from .portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP, FRACTIONAL_PATH_WHOLE,
    AllocationPlan, MarginInfo, PositionFetchFailed, PositionState, apply_order_impacts,
    decide_symbol_action, plan_quantity_attempts, split_delta_fifo,
)
from .TransactionHelper import TransactionHelper
from .types import OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus


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


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

OUTCOME_SUBMITTED = "submitted"
OUTCOME_PARTIAL = "partially_filled"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_WASHTRADE_LOCKED = "washtrade_locked"

#: Statuses that mean the broker took the order and is NOT going to work it any
#: further. The adapter returning an OBJECT is not the same as the broker
#: accepting the order -- ``_handle_order_submit_error`` sets ERROR on the row and
#: some adapters hand it straight back -- and an outcome table that reads
#: "submitted" for a REJECTED order is the run lying about what happened.
_DEAD_ON_ARRIVAL_STATUSES = frozenset({
    OrderStatus.REJECTED, OrderStatus.ERROR, OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
})

#: Comment stamped on every order an allocation run creates.
#: It MUST NOT contain the substring "closing" in any case: close_transaction
#: (AccountInterface.py:1567+) re-detects an existing close order with
#: ``order_type == MARKET and 'closing' in order.comment.lower()``, and allocation
#: orders are MARKET orders. A comment containing it would make every future
#: close on that symbol believe a close order already exists.
RUN_COMMENT_FMT = "Portfolio allocation run {run_tag} - {side} {symbol}"

#: ...and ``run_tag`` is caller-supplied, so the format being clean is not enough.
_CLOSING_RE = re.compile("closing", re.IGNORECASE)
_CLOSING_REPLACEMENT = "clos-ing"

#: Said when the broker refused an order and left no words anywhere. Never a
#: silent empty string: "failed" with no reason is unactionable.
_REJECTION_FALLBACK = "broker rejected the order"


def _run_comment(run_tag: Any, side: Any, symbol: str) -> str:
    """The order comment for one allocation order, with "closing" made impossible.

    Scrubbed rather than refused: ``run_tag`` is the allocation run's own id in
    every current caller, so a raise here would be a money-path exception over a
    cosmetic field -- while letting the substring through silently disables the
    duplicate-close guard for that symbol forever.
    """
    comment = RUN_COMMENT_FMT.format(
        run_tag=run_tag,
        side=side.value if hasattr(side, 'value') else side,
        symbol=symbol,
    )
    scrubbed = _CLOSING_RE.sub(_CLOSING_REPLACEMENT, comment)
    if scrubbed != comment:
        logger.warning(
            f"Allocation run tag {run_tag!r} contains 'closing'; scrubbed out of the "
            f"order comment because close_transaction uses that substring to detect an "
            f"existing close order"
        )
    return scrubbed


@dataclass
class RowOutcome:
    """What actually happened to one dry-run row at submission time.

    ``quantity`` is what was SENT; ``filled_quantity`` is what the broker says
    actually filled, and it is ``None`` when the broker said nothing -- never
    0.0, which would read as "nothing filled" for an order still working.
    """
    symbol: str
    action: str
    status: str
    quantity: float = 0.0
    filled_quantity: Optional[float] = None
    path: str = ""
    order_ids: List[int] = field(default_factory=list)
    transaction_ids: List[int] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "action": self.action, "status": self.status,
            "quantity": self.quantity, "filled_quantity": self.filled_quantity,
            "path": self.path,
            "order_ids": list(self.order_ids),
            "transaction_ids": list(self.transaction_ids),
            "message": self.message,
        }


def submit_plan(account, plan: AllocationPlan, current: Dict[str, PositionState],
                *, run_tag: str, allow_fractional: bool) -> List[RowOutcome]:
    """Submit a plan: every SELL first, then the BUYs by descending value.

    Decision 13 (sells before buys) and the "buying_power shrinks as buys fill"
    risk: descending value means a shortfall truncates the SMALLEST positions.

    A sell that FAILS does not abandon the buys. ``_apply_bp_scaling`` sized the
    buys against the buying power the account had BEFORE any sell, so a refused
    close cannot make them overspend, and stopping half way would leave the
    account further from target than doing nothing while the outcome table said
    nothing about the rows never attempted. Partial failure is normal: each row
    reports its own outcome and nothing is rolled back.

    Raises:
        ValueError: when ``allow_fractional`` disagrees with
            ``plan.allow_fractional``. That is the setting the DRY RUN was solved
            with; submitting under a different one sends quantities the user
            never reviewed (2.0 shares where the table said 2.5). Checked BEFORE
            the first order, so nothing at all goes out.
    """
    if bool(allow_fractional) != bool(plan.allow_fractional):
        raise ValueError(
            f"Allocation submission refused: allow_fractional={allow_fractional} but the "
            f"plan was solved with allow_fractional={plan.allow_fractional}. The dry run the "
            f"user approved would not be what gets sent. Nothing was submitted."
        )

    outcomes: List[RowOutcome] = []

    for row in plan.sell_rows:
        outcomes.append(_submit_row(account, row, current.get(row.symbol),
                                    run_tag=run_tag, allow_fractional=allow_fractional))
    for row in plan.buy_rows:
        outcomes.append(_submit_row(account, row, current.get(row.symbol),
                                    run_tag=run_tag, allow_fractional=allow_fractional))

    traded = {o.symbol for o in outcomes}
    for row in plan.rows:
        if row.symbol not in traded:
            outcomes.append(RowOutcome(
                symbol=row.symbol, action=ACTION_SKIP, status=OUTCOME_SKIPPED,
                message="; ".join(row.reasons) or "no delta",
            ))
    return outcomes


def _submit_row(account, row, state, *, run_tag: str, allow_fractional: bool) -> RowOutcome:
    action = decide_symbol_action(row, state)
    try:
        if action == ACTION_SKIP:
            return RowOutcome(symbol=row.symbol, action=ACTION_SKIP, status=OUTCOME_SKIPPED,
                              message="; ".join(row.reasons) or "nothing to do")
        if action == ACTION_CLOSE:
            return _close_symbol(account, row, state)
        if action == ACTION_ADJUST:
            return _adjust_symbol(account, row, state)
        return _open_symbol(account, row, run_tag=run_tag, allow_fractional=allow_fractional)
    except Exception as e:
        # Backstop only -- every branch below catches its own IO per unit of work,
        # so that one dead transaction does not abandon the rest of the symbol.
        logger.error(f"Allocation submission failed for {row.symbol}: {e}", exc_info=True)
        return RowOutcome(symbol=row.symbol, action=action, status=OUTCOME_FAILED,
                          message=str(e) or e.__class__.__name__)


def _close_symbol(account, row, state) -> RowOutcome:
    """Target 0 on a held symbol -> close every open transaction for it.

    Each transaction is closed independently: one refusal or one exception must
    not leave the others open with the run reporting a single failure and nothing
    about the rest.
    """
    messages: List[str] = []
    closed: List[int] = []
    ok = True
    for txn_id in state.transaction_ids:
        try:
            result = account.close_transaction(txn_id)
        except Exception as e:
            ok = False
            messages.append(f"txn {txn_id}: {e or e.__class__.__name__}")
            logger.error(f"Allocation close of transaction {txn_id} ({row.symbol}) "
                         f"failed: {e}", exc_info=True)
            continue
        if result and result.get('success'):
            closed.append(txn_id)
        else:
            ok = False
            messages.append(f"txn {txn_id}: {(result or {}).get('message', 'close failed')}")
    return RowOutcome(
        symbol=row.symbol, action=ACTION_CLOSE,
        status=OUTCOME_SUBMITTED if ok else OUTCOME_FAILED,
        quantity=abs(row.delta_quantity), transaction_ids=closed,
        message="; ".join(messages),
    )


def _adjust_symbol(account, row, state) -> RowOutcome:
    """Held, target > 0 -> resize the existing transaction(s), FIFO."""
    quantities: List[Tuple[int, float]] = []
    for txn_id in state.transaction_ids:
        try:
            txn = get_instance(Transaction, txn_id)
        except InstanceNotFound:
            logger.warning(f"Allocation: transaction {txn_id} vanished before adjustment")
            continue
        quantities.append((txn_id, float(txn.quantity or 0.0)))

    splits = split_delta_fifo(row.delta_quantity, quantities)
    if not splits:
        return RowOutcome(symbol=row.symbol, action=ACTION_ADJUST, status=OUTCOME_SKIPPED,
                          message="no open transaction quantity to adjust")

    order_ids: List[int] = []
    touched: List[int] = []
    messages: List[str] = []
    ok = True
    for txn_id, qty_change in splits:
        try:
            txn = get_instance(Transaction, txn_id)
        except InstanceNotFound:
            ok = False
            messages.append(f"txn {txn_id}: vanished")
            continue
        try:
            result = TransactionHelper.adjust_quantity_with_tpsl(account, txn, qty_change)
        except Exception as e:
            ok = False
            messages.append(f"txn {txn_id}: {e or e.__class__.__name__}")
            logger.error(f"Allocation adjustment of transaction {txn_id} ({row.symbol}) "
                         f"failed: {e}", exc_info=True)
            continue
        touched.append(txn_id)
        order_ids.extend((result or {}).get('orders_created') or [])
        if not (result or {}).get('success'):
            ok = False
            messages.append(f"txn {txn_id}: {(result or {}).get('message')}")

    # A HALF-applied trim is not a success: reporting it as SUBMITTED would tell
    # the user the position is at target when it is not.
    return RowOutcome(
        symbol=row.symbol, action=ACTION_ADJUST,
        status=OUTCOME_SUBMITTED if ok else OUTCOME_FAILED,
        quantity=abs(row.delta_quantity), order_ids=order_ids,
        transaction_ids=touched, message="; ".join(messages),
    )


def _open_symbol(account, row, *, run_tag: str, allow_fractional: bool) -> RowOutcome:
    """Not held, target > 0 -> a brand new MARKET order, with the fractional fallback.

    A fractional equity quantity is legal on a MARKET order and on nothing else
    (TastyTrade ``fractional_market_orders_only``; Alpaca refuses one on any
    non-market type and on any TIF but DAY), so every attempt goes in as MARKET /
    good_for='day'. On rejection the order is retried ONCE at floor(qty); a floor
    of 0 is SKIPPED, not a failure. The row reports which path succeeded.

    The retry is abandoned the moment anything FILLED. A broker that cancels an
    order after filling part of it has already moved the position, and topping
    that up with the whole-share retry would overshoot the target the user
    approved -- an overshoot created by the recovery path itself.
    """
    attempts = plan_quantity_attempts(
        row.delta_quantity,
        allow_fractional=allow_fractional,
        fractionable=bool(row.fractional),
    )
    if not attempts:
        return RowOutcome(
            symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_SKIPPED,
            quantity=abs(row.delta_quantity),
            message="below one whole share and fractional is off - nothing submitted",
        )

    # Every attempt persists its own TradingOrder row, and the rejected one is
    # left behind marked ERROR by the adapter. The outcome carries BOTH ids: the
    # run audit has to be able to find the row the broker refused, not just the
    # one that worked.
    created: List[int] = []
    last = None
    for path, quantity in attempts:
        last = _submit_new_order(account, row, quantity, run_tag=run_tag, path=path)
        for order_id in last.order_ids:
            if order_id not in created:
                created.append(order_id)
        last.order_ids = list(created)
        if last.status != OUTCOME_FAILED:
            return last
        if last.filled_quantity:
            logger.warning(
                f"Allocation: {row.symbol} failed at qty={quantity} ({path}) AFTER "
                f"filling {last.filled_quantity}; not retrying - a top-up would "
                f"overshoot the approved target"
            )
            return last
        logger.warning(
            f"Allocation: {row.symbol} rejected at qty={quantity} ({path}); "
            f"{'retrying at whole shares' if path != FRACTIONAL_PATH_WHOLE else 'no retry left'}"
        )
    return last


def _fresh_comment(order_id: int) -> str:
    """The comment as PERSISTED, which is where the adapters leave the real reason."""
    try:
        return get_instance(TradingOrder, order_id).comment or ""
    except Exception as e:  # noqa: BLE001 -- a missing row must not mask the rejection
        logger.warning(f"Allocation: could not re-read order {order_id} for its "
                       f"rejection reason: {e}")
        return ""


def _rejection_reason(order, order_id: int, stamped: str) -> str:
    """The broker's OWN words for a refusal, from wherever the adapter left them.

    ``AccountInterface._handle_order_submit_error`` appends the reason to
    ``fresh_order`` -- the row re-read from the database -- and returns None; the
    caller's ``order`` object is detached and never sees it. Other paths mutate
    the passed object instead. Both are read, and our own run stamp is stripped
    off each so the run does not report its own comment back as if it were the
    broker's complaint.
    """
    parts: List[str] = []
    for comment in (_fresh_comment(order_id), getattr(order, "comment", None)):
        text = (comment or "").strip()
        if text.startswith(stamped):
            text = text[len(stamped):].lstrip(" |").strip()
        if text and text not in parts:
            parts.append(text)
    return " | ".join(parts) or _REJECTION_FALLBACK


def _submit_new_order(account, row, quantity: float, *, run_tag: str, path: str) -> RowOutcome:
    """Persist one TradingOrder and put it through the PUBLIC submit_order seam.

    Public, not ``_submit_order_impl``: that is what runs order validation,
    creates the Transaction for a bare MARKET order and applies the wash-trade
    gate. ``is_closing_order`` is passed EXPLICITLY -- an allocation buy always
    OPENS, and a close mispriced as a short open is commit 1d099e8.

    Never raises: a broker adapter that throws (TastyTrade refuses a fractional
    priced order locally with a ValueError naming ``fractional_market_orders_only``)
    comes back as a FAILED outcome, so the whole-share fallback can act on it
    instead of a traceback reaching the user.
    """
    stamped = _run_comment(run_tag, row.side, row.symbol)
    order = TradingOrder(
        account_id=account.id,
        symbol=row.symbol,
        quantity=quantity,
        side=row.side,
        order_type=OrderType.MARKET,
        # A fractional quantity is legal ONLY on a MARKET order, and Alpaca
        # additionally refuses one on anything but DAY (AlpacaAccount.py:1039-1064,
        # TastyTrade `fractional_market_orders_only`). Both are pinned here rather
        # than left to a default: AlpacaAccount defaults an unset good_for to GTC.
        good_for='day',
        status=OrderStatus.PENDING,
        open_type=OrderOpenType.MANUAL,
        expert_recommendation_id=None,
        comment=stamped,
    )
    order_id = add_instance(order, expunge_after_flush=True)
    if not order_id:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path,
                          message="could not persist the TradingOrder")

    try:
        result = account.submit_order(order, is_closing_order=False)
    except Exception as e:
        logger.error(f"Allocation: submit_order raised for {row.symbol} at qty={quantity} "
                     f"({path}): {e}", exc_info=True)
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path, order_ids=[order_id],
                          message=str(e) or e.__class__.__name__)

    # submit_order returns a TRUTHY order with status WASHTRADE_LOCKED when the
    # gate fires, and None on hard failure with the reason on .comment. Inspect
    # .status, never truthiness.
    if result is None:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path, order_ids=[order_id],
                          message=_rejection_reason(order, order_id, stamped))

    status = getattr(result, 'status', None)
    filled = getattr(result, 'filled_qty', None)
    if status == OrderStatus.WASHTRADE_LOCKED:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW,
                          status=OUTCOME_WASHTRADE_LOCKED, quantity=quantity, path=path,
                          order_ids=[order_id], message="wash-trade gate locked this symbol")
    if status in _DEAD_ON_ARRIVAL_STATUSES:
        return RowOutcome(
            symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
            quantity=quantity, filled_quantity=filled, path=path, order_ids=[order_id],
            message=f"broker returned the order {status.value}: "
                    f"{_rejection_reason(result, order_id, stamped)}")
    if status == OrderStatus.PARTIALLY_FILLED:
        return RowOutcome(
            symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_PARTIAL,
            quantity=quantity, filled_quantity=filled, path=path, order_ids=[order_id],
            message=f"partially filled: {filled} of {quantity}")
    return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_SUBMITTED,
                      quantity=quantity, filled_quantity=filled, path=path,
                      order_ids=[order_id])


# ---------------------------------------------------------------------------
# Income ledger (deposits + dividends), synced on page load and Refresh only.
# ---------------------------------------------------------------------------

#: How far back the page syncs and displays the ledger.
INCOME_WINDOW_DAYS = 30


def _today() -> Date:
    """The ONE clock read behind the income window.

    Not inlined as ``Date.today()`` at each use: the sync's window and the
    panel's display cutoff have to be the same day or an event can be written by
    one and hidden by the other across a midnight boundary. Having a single
    named read also lets the tests freeze the date instead of racing it.
    """
    return Date.today()


def sync_income_events(account, *, days: int = INCOME_WINDOW_DAYS) -> int:
    """Upsert the broker's cash movements into ``portfolio_income_event``.

    Only ``CashTransfer.is_income`` rows (POSITIVE deposits and dividends) are
    persisted -- a withdrawal is not income, and neither is a reversed deposit,
    which arrives as a NEGATIVE ``DEPOSIT``. The DB write is
    ``portfolio_allocation_store.upsert_income_event``, keyed on
    ``(account_id, external_id)``, so re-syncing the same window RESTATES each
    event rather than duplicating or accumulating it -- the page presents the
    whole window again on every load, so summing would inflate the ledger every
    single time. ``consumed_amount`` is never touched: money already spent stays
    spent.

    Never runs on a timer: the caller invokes it on page load and on explicit
    Refresh, so the page issues no background broker calls.

    Returns:
        int: how many NEW events were inserted (a restatement is not counted; a
        broker failure is logged and returns 0 rather than looking like "no
        income").
    """
    from .portfolio_allocation_store import get_income_events_since, upsert_income_event

    end_date = _today()
    start_date = end_date - timedelta(days=days)
    try:
        transfers = account.get_cash_transfers(start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.error(f"get_cash_transfers failed for account {account.id}: {e}", exc_info=True)
        return 0

    # ``or []`` is correct HERE and nowhere near get_positions(): this seam is
    # documented not to distinguish failure from emptiness
    # (ReadOnlyAccountInterface.get_cash_transfers -- "an implementation that
    # fails must log the error and return []"), so None means "nothing", not
    # "the fetch failed".
    income = [t for t in (transfers or []) if t.is_income]
    if not income:
        logger.info(f"Income sync for account {account.id}: 0 new event(s)")
        return 0

    # The known-set cutoff is the OLDEST event about to be written, not the
    # window start: a broker is free to ignore our date bounds, and an event we
    # persist but cannot see here would be re-counted as newly discovered on
    # every page load. Read against the WHOLE ledger, open or not -- a deposit a
    # run has already spent in full is still a known event.
    cutoff = min([start_date] + [t.event_date for t in income])
    known = {row.external_id for row in get_income_events_since(account.id, cutoff)}

    inserted = 0
    for transfer in income:
        is_new = transfer.external_id not in known
        upsert_income_event(account.id, transfer.external_id, transfer.event_date,
                            transfer.event_type, transfer.amount, symbol=transfer.symbol)
        if is_new:
            inserted += 1
            known.add(transfer.external_id)

    logger.info(f"Income sync for account {account.id}: {inserted} new event(s)")
    return inserted


def get_recent_income_events(account_id: int, *,
                             days: int = INCOME_WINDOW_DAYS) -> List[Dict[str, Any]]:
    """The last ``days`` of income events, newest first, as display dicts.

    ``open_amount`` is the model's clamped property, so it is never negative even
    where ``consumed_amount`` exceeds ``amount`` -- reachable when a DIVNRA tax
    leg restates a dividend below what a run already spent of it. Do NOT render
    ``consumed / amount`` as a fraction from these two figures: that ratio goes
    past 100% in exactly that case.
    """
    from .portfolio_allocation_store import get_income_events_since

    cutoff = _today() - timedelta(days=days)
    return [{
        "id": row.id,
        "external_id": row.external_id,
        "event_date": row.event_date,
        "event_type": row.event_type,
        "symbol": row.symbol,
        "amount": row.amount,
        "open_amount": row.open_amount,
    } for row in get_income_events_since(account_id, cutoff)]


def get_open_income_total(account_id: int) -> float:
    """Total un-consumed income for this account, across the WHOLE ledger.

    Read-only. Spending the ledger is not reachable from here on purpose: it
    happens inside ``portfolio_allocation_store.finalise_allocation_run``, keyed
    on the run, so it cannot be replayed.
    """
    from .portfolio_allocation_store import get_open_income_total as _store_total
    return _store_total(account_id)
