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
from .db import InstanceNotFound, add_instance, get_db, get_instance, log_activity
from .models import Transaction, TradingOrder
from .portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP, FRACTIONAL_PATH_WHOLE,
    AllocationPlan, BaseSnapshot, MarginInfo, PositionFetchFailed, PositionState,
    apply_order_impacts, decide_symbol_action, plan_quantity_attempts, split_delta_fifo,
)
from .TransactionHelper import TransactionHelper
from .types import (
    ActivityLogSeverity, ActivityLogType, BrokerOrderErrorReason,
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)


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

#: Classified rejection reasons that can ONLY be produced by parsing a REPLY from
#: the broker -- ``_classify_order_error`` reaches them by recognising the
#: broker's own error body -- so each of them is positive proof that the order
#: was seen, refused and never worked.
#:
#: UNKNOWN is deliberately absent, and that is the whole point of the set. It is
#: the fall-through for every error nobody has characterised: BOTH live
#: classifiers return it for any non-APIError / non-auth exception, which is
#: exactly where a socket read timeout, a dropped connection or an SDK bug lands.
#: An order can be ACCEPTED by the broker and still produce one, so UNKNOWN means
#: "we do not know", never "it was refused".
_PROVEN_REJECTION_REASONS = frozenset({
    BrokerOrderErrorReason.INSUFFICIENT_FUNDS.value,
    BrokerOrderErrorReason.INSUFFICIENT_QTY.value,
    BrokerOrderErrorReason.WASH_TRADE.value,
    BrokerOrderErrorReason.INVALID_SYMBOL.value,
    BrokerOrderErrorReason.STOP_THROUGH_MARKET.value,
    BrokerOrderErrorReason.UNAUTHORIZED.value,
})

#: ``AccountInterface._handle_order_submit_error`` renders ``[{reason.value}]
#: {broker message}`` onto the order's comment. That tag is the only
#: machine-readable trace of the classification anywhere in the system.
_REASON_TAG_RE = re.compile(r"\[([a-z_]+)\]")

#: Appended to a failed row that will NOT be retried at whole shares. It has to
#: name the action a human must take: "failed" alone would read as "nothing
#: happened", and the whole reason we stopped is that something might have.
_AMBIGUOUS_NO_RETRY_NOTE = (
    "not retried at whole shares - this failure does not prove the order never "
    "reached the broker, and a retry could place a SECOND order; check the broker")


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


def _leg_status(succeeded: float, failed: int) -> str:
    """The row status for a multi-leg symbol: SUBMITTED / PARTIAL / FAILED.

    A row is one SYMBOL, but a close or a trim can be several transactions and
    each is its own order. Collapsing "2 of 3 legs went out" to FAILED is not a
    conservative rounding: ``summarise_outcomes`` values a non-money-out row at
    ZERO, so the two sells that really happened stop funding the buys they paid
    for and the run over-consumes the income ledger by their whole value.
    Collapsing it to SUBMITTED is the opposite lie -- the user is told the
    position is at target when it is not.
    """
    if not failed:
        return OUTCOME_SUBMITTED
    return OUTCOME_PARTIAL if succeeded > 0 else OUTCOME_FAILED


def _transaction_quantity(txn_id: int) -> float:
    """The open quantity of one transaction, or 0.0 when it cannot be read.

    Never raises: this is only ever used to measure what a leg is worth, and a
    row that vanished must not take the rest of the symbol down with it.
    """
    try:
        return float(get_instance(Transaction, txn_id).quantity or 0.0)
    except Exception as e:  # noqa: BLE001 -- InstanceNotFound and anything else
        logger.warning(f"Allocation: could not read transaction {txn_id} quantity: {e}")
        return 0.0


def _close_one_transaction(account, txn_id: int, symbol: str) -> Tuple[bool, List[int], str]:
    """``close_transaction`` for ONE transaction.

    Returns:
        (ok, order_ids, message). ``order_ids`` carries the broker order the
        close created -- ``close_transaction`` documents it as ``close_order_id``
        (AccountInterface.py:1567) and it is a TradingOrder this run is
        responsible for, so dropping it leaves the run audit unable to show the
        orders that closed the positions.
    """
    try:
        result = account.close_transaction(txn_id)
    except Exception as e:
        logger.error(f"Allocation close of transaction {txn_id} ({symbol}) failed: {e}",
                     exc_info=True)
        return False, [], f"txn {txn_id}: {e or e.__class__.__name__}"

    result = result or {}
    close_order_id = result.get('close_order_id')
    # A FAILED close still documents `close_order_id: None`, so truthiness rather
    # than presence -- and a refused close that nevertheless created an order row
    # still reports it.
    order_ids = [close_order_id] if close_order_id else []
    if result.get('success'):
        return True, order_ids, ""
    return False, order_ids, f"txn {txn_id}: {result.get('message', 'close failed')}"


def _close_symbol(account, row, state) -> RowOutcome:
    """Target 0 on a held symbol -> close every open transaction for it.

    Each transaction is closed independently: one refusal or one exception must
    not leave the others open with the run reporting a single failure and nothing
    about the rest.
    """
    messages: List[str] = []
    closed: List[int] = []
    order_ids: List[int] = []
    sent = 0.0
    failed = 0
    for txn_id in state.transaction_ids:
        # Read BEFORE the close: that is the quantity the close order is built
        # from (``submit_close_order_for_transaction`` sizes it off the
        # transaction), and it is what the row really put on the market.
        quantity = _transaction_quantity(txn_id)
        ok, ids, message = _close_one_transaction(account, txn_id, row.symbol)
        order_ids.extend(ids)
        if ok:
            closed.append(txn_id)
            sent += quantity
        else:
            failed += 1
            messages.append(message)
    return RowOutcome(
        symbol=row.symbol, action=ACTION_CLOSE,
        status=_leg_status(sent, failed),
        quantity=sent, order_ids=order_ids, transaction_ids=closed,
        message="; ".join(messages),
    )


def _adjust_symbol(account, row, state) -> RowOutcome:
    """Held, target > 0 -> resize the existing transaction(s), FIFO.

    A leg that EXACTLY EXHAUSTS its transaction goes through ``close_transaction``
    rather than ``adjust_quantity_with_tpsl``. That is not a workaround, it is the
    right API: the helper is a PARTIAL-close facility and refuses
    ``close_qty >= current_qty`` outright, while ``split_delta_fifo`` produces an
    exhausting leg BY CONSTRUCTION for every trim that spans more than one
    transaction (30 shares held as 20 + 10, sell 25 -> [(t1,-20),(t2,-5)]). Routed
    the old way the first leg was simply rejected, so the trim under-sold and
    could never converge while the dry run promised the whole thing. Closing a
    transaction outright is exactly what that leg means.
    """
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

    held = dict(quantities)
    order_ids: List[int] = []
    touched: List[int] = []
    messages: List[str] = []
    sent = 0.0
    failed = 0
    for txn_id, qty_change in splits:
        magnitude = abs(float(qty_change))
        current = held.get(txn_id, 0.0)

        if qty_change < 0 and current > 0 and magnitude >= current:
            ok, ids, message = _close_one_transaction(account, txn_id, row.symbol)
            order_ids.extend(ids)
            if ok:
                touched.append(txn_id)
                sent += current
            else:
                failed += 1
                messages.append(message)
            continue

        try:
            txn = get_instance(Transaction, txn_id)
        except InstanceNotFound:
            failed += 1
            messages.append(f"txn {txn_id}: vanished")
            continue
        try:
            result = TransactionHelper.adjust_quantity_with_tpsl(account, txn, qty_change)
        except Exception as e:
            failed += 1
            messages.append(f"txn {txn_id}: {e or e.__class__.__name__}")
            logger.error(f"Allocation adjustment of transaction {txn_id} ({row.symbol}) "
                         f"failed: {e}", exc_info=True)
            continue
        result = result or {}
        order_ids.extend(result.get('orders_created') or [])
        if result.get('success'):
            touched.append(txn_id)
            sent += magnitude
        else:
            failed += 1
            messages.append(f"txn {txn_id}: {result.get('message')}")

    return RowOutcome(
        symbol=row.symbol, action=ACTION_ADJUST,
        status=_leg_status(sent, failed),
        quantity=sent, order_ids=order_ids,
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

    It is ALSO abandoned on any failure that does not PROVE the order never
    reached the broker (``_nothing_was_placed``). A rejection and a lost response
    look identical from here -- both arrive as ``submit_order`` returning None --
    and retrying the second one places a second order for the same intent. Under-
    investing is recoverable on the next run; buying the position twice is not.
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
    for index, (path, quantity) in enumerate(attempts):
        last, nothing_was_placed = _submit_new_order(
            account, row, quantity, run_tag=run_tag, path=path)
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
        if index == len(attempts) - 1:
            logger.warning(f"Allocation: {row.symbol} rejected at qty={quantity} "
                           f"({path}); no retry left")
            return last
        if not nothing_was_placed:
            logger.error(
                f"Allocation: {row.symbol} failed at qty={quantity} ({path}) with a "
                f"failure that does not prove the order never reached the broker; "
                f"NOT retrying at whole shares - a retry could double-place it. "
                f"Check the broker."
            )
            last.message = (f"{last.message} | {_AMBIGUOUS_NO_RETRY_NOTE}"
                            if last.message else _AMBIGUOUS_NO_RETRY_NOTE)
            return last
        logger.warning(
            f"Allocation: {row.symbol} rejected at qty={quantity} ({path}); "
            f"retrying at whole shares"
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


def _last_classified_reason(comment: str) -> Optional[str]:
    """The LAST ``[reason]`` tag on an order comment, or None when there is none.

    The last one, because ``_handle_order_submit_error`` APPENDS: a stop that was
    auto-converted to a market order and then failed again carries two, and it is
    the final verdict that describes the state the row ended in.
    """
    tags = _REASON_TAG_RE.findall(comment or "")
    return tags[-1] if tags else None


def _nothing_was_placed(order_id: Optional[int], status: Any,
                        filled: Optional[float]) -> bool:
    """Does this failure PROVE the broker never took the order? Conservative.

    True only on positive evidence, because the consequence of guessing wrong is
    a duplicate order:

    * the broker itself handed the order back REJECTED / CANCELED / EXPIRED with
      nothing filled -- we have its answer and the order is dead; or
    * the persisted row carries a classified reason from
      ``_handle_order_submit_error`` that only a broker REPLY can produce
      (``_PROVEN_REJECTION_REASONS``); or
    * there is no order row at all, so nothing was ever sent.

    Everything else is False: an ``[unknown]`` classification, no classification
    at all, an exception out of ``submit_order``, or anything that filled. See
    ``_PROVEN_REJECTION_REASONS`` for why UNKNOWN is on this side of the line.
    """
    if filled:
        return False
    if order_id is None:
        return True
    if status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
        return True
    reason = _last_classified_reason(_fresh_comment(order_id))
    return reason in _PROVEN_REJECTION_REASONS


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


def _submit_new_order(account, row, quantity: float, *, run_tag: str,
                      path: str) -> Tuple[RowOutcome, bool]:
    """Persist one TradingOrder and put it through the PUBLIC submit_order seam.

    Public, not ``_submit_order_impl``: that is what runs order validation,
    creates the Transaction for a bare MARKET order and applies the wash-trade
    gate. ``is_closing_order`` is passed EXPLICITLY -- an allocation buy always
    OPENS, and a close mispriced as a short open is commit 1d099e8.

    Never raises: a broker adapter that throws comes back as a FAILED outcome
    instead of a traceback reaching the user.

    Returns:
        Tuple[RowOutcome, bool]: the outcome, and whether the failure PROVES
        nothing was placed at the broker (``_nothing_was_placed``). The second
        value is meaningless unless the outcome FAILED, and it is what decides
        whether the whole-share fallback may run. It is returned rather than put
        on ``RowOutcome`` because it is an internal retry decision, not something
        the run audit records.
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
        # Nothing was persisted, so nothing was sent: a retry cannot duplicate it.
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path,
                          message="could not persist the TradingOrder"), True

    try:
        result = account.submit_order(order, is_closing_order=False)
    except Exception as e:
        logger.error(f"Allocation: submit_order raised for {row.symbol} at qty={quantity} "
                     f"({path}): {e}", exc_info=True)
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path, order_ids=[order_id],
                          message=str(e) or e.__class__.__name__), \
            _nothing_was_placed(order_id, None, None)

    # submit_order returns a TRUTHY order with status WASHTRADE_LOCKED when the
    # gate fires, and None on hard failure with the reason on .comment. Inspect
    # .status, never truthiness.
    if result is None:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
                          quantity=quantity, path=path, order_ids=[order_id],
                          message=_rejection_reason(order, order_id, stamped)), \
            _nothing_was_placed(order_id, None, None)

    status = getattr(result, 'status', None)
    filled = getattr(result, 'filled_qty', None)
    if status == OrderStatus.WASHTRADE_LOCKED:
        return RowOutcome(symbol=row.symbol, action=ACTION_NEW,
                          status=OUTCOME_WASHTRADE_LOCKED, quantity=quantity, path=path,
                          order_ids=[order_id],
                          message="wash-trade gate locked this symbol"), False
    if status in _DEAD_ON_ARRIVAL_STATUSES:
        return RowOutcome(
            symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_FAILED,
            quantity=quantity, filled_quantity=filled, path=path, order_ids=[order_id],
            message=f"broker returned the order {status.value}: "
                    f"{_rejection_reason(result, order_id, stamped)}"), \
            _nothing_was_placed(order_id, status, filled)
    if status == OrderStatus.PARTIALLY_FILLED:
        return RowOutcome(
            symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_PARTIAL,
            quantity=quantity, filled_quantity=filled, path=path, order_ids=[order_id],
            message=f"partially filled: {filled} of {quantity}"), False
    return RowOutcome(symbol=row.symbol, action=ACTION_NEW, status=OUTCOME_SUBMITTED,
                      quantity=quantity, filled_quantity=filled, path=path,
                      order_ids=[order_id]), False


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


# ---------------------------------------------------------------------------
# Run audit: portfolio_allocation_run + log_activity + income consumption.
# ---------------------------------------------------------------------------

#: Outcomes for which an order really is at the broker, so real money is
#: committed and the income that funded it has to be marked as spent.
#: OUTCOME_PARTIAL belongs here: a partially filled order has already moved the
#: position and is still working. OUTCOME_WASHTRADE_LOCKED does NOT: that order
#: is PENDING at our end, nothing was sent, and it is retried later.
_MONEY_OUT_STATUSES = frozenset({OUTCOME_SUBMITTED, OUTCOME_PARTIAL})


def _committed_quantity(outcome: RowOutcome) -> Optional[float]:
    """How much of this row really went out, or None when nothing did.

    Two ways money leaves the account, and the second one is the whole reason
    this is not a set membership test:

    * the order reached the broker (``_MONEY_OUT_STATUSES``) -> the quantity SENT,
      because the order is live and will fill; and
    * the row FAILED but the broker had already FILLED part of it -> the filled
      quantity. A market order cancelled after filling 6 of 10 shares is reported
      FAILED (correctly -- it did not do what was asked) and those 6 shares are
      nevertheless bought and paid for. Valuing that row at zero leaves the income
      that funded them looking unallocated, so the NEXT run spends it again, on
      top of shares the account already owns. The run is STAMPED either way, so it
      never appears in ``get_unconsumed_runs()`` and nothing else will ever
      reconcile it.
    """
    if outcome.status in _MONEY_OUT_STATUSES:
        return float(outcome.quantity)
    if outcome.filled_quantity:
        return float(outcome.filled_quantity)
    return None

#: The one-line activity-log summary of a run. Every outcome the vocabulary has
#: is named: a line that mentions only "submitted" and "failed" hides a
#: wash-trade lock completely, and that is an order the user never learns about.
RUN_ACTIVITY_FMT = (
    "Portfolio allocation run {run_id} ({scope}): {submitted} submitted, "
    "{partial} partially filled, {locked} wash-trade locked, {failed} failed, "
    "{skipped} skipped")


def summarise_outcomes(plan: AllocationPlan, outcomes: List[RowOutcome]) -> Dict[str, Any]:
    """What a run ACTUALLY committed, as opposed to what it planned. Pure.

    Only rows whose order reached the broker count towards the money totals; the
    value uses the quantity that really went in, so a fractional order that fell
    back to whole shares is worth less than the plan said. The row's own estimate
    is the fallback when there is no price to multiply by.

    ``order_ids`` is EVERY TradingOrder row this run created, refused ones
    included -- that is what the column says it holds, and an audit that cannot
    point at the order the broker rejected cannot explain the failure it reports.

    Note for the reader: these are SUBMITTED values. Replacing them with FILLED
    values is its own piece of work (the ledger must not mark income as spent for
    an order that never filled); the split between "what went out" and "what
    filled" already exists on ``RowOutcome.quantity`` / ``.filled_quantity``.
    """
    by_symbol = {row.symbol: row for row in plan.rows}
    buy_value = 0.0
    sell_value = 0.0
    order_ids: List[int] = []

    for outcome in outcomes:
        for order_id in outcome.order_ids:
            if order_id not in order_ids:
                order_ids.append(order_id)
        quantity = _committed_quantity(outcome)
        if quantity is None:
            continue
        row = by_symbol.get(outcome.symbol)
        if row is None:
            continue
        if row.price:
            value = float(row.price) * quantity
        else:
            # No price to multiply by, so the row's own estimate is all there is
            # -- pro-rated, because it was struck for the WHOLE row and only part
            # of the row may have gone out.
            sent = abs(float(outcome.quantity)) or abs(float(row.delta_quantity))
            fraction = min(1.0, quantity / sent) if sent else 1.0
            value = float(row.estimated_value) * fraction
        if row.is_buy:
            buy_value += value
        elif row.is_sell:
            sell_value += value

    return {
        "submitted_buy_value": buy_value,
        "submitted_sell_value": sell_value,
        # Never negative: a rebalance funded entirely by its own sells consumes
        # no income, and a negative would be handed to the ledger as a budget.
        "net_buy_value": max(0.0, buy_value - sell_value),
        "order_ids": order_ids,
    }


def _finalise_run(run_id: int, totals: Dict[str, Any]) -> float:
    """Write the run's totals and spend the ledger, in ONE store transaction.

    The net buy value is deliberately NOT passed: the store derives it from the
    totals it is writing, on the row as it re-reads it. That is what makes
    "consumed nothing because the caller handed over a stale zero" unrepresentable.

    Returns:
        float: what the ledger actually gave up -- possibly LESS than the net buy
        value, which is not an error (buying power, not the ledger, is the
        feasibility constraint), and 0.0 when the run row could not be found.
    """
    from .portfolio_allocation_store import finalise_allocation_run

    try:
        finalised = finalise_allocation_run(
            run_id,
            submitted_buy_value=totals["submitted_buy_value"],
            submitted_sell_value=totals["submitted_sell_value"],
            order_ids=totals["order_ids"])
    except InstanceNotFound:
        logger.error(f"Allocation run {run_id} vanished before it could be finalised; "
                     f"its income was NOT consumed")
        return 0.0
    return finalised.income_consumed_amount


def run_allocation(account, plan: AllocationPlan, current: Dict[str, PositionState],
                   base: BaseSnapshot, *, mode: str,
                   scope_label: Optional[str] = None) -> Dict[str, Any]:
    """Submit a reviewed plan and record it. The single Submit entry point.

    Order of operations:
      1. INSERT the ``portfolio_allocation_run`` row with the plan snapshot and
         zero submitted values, so its id can be stamped into every order comment.
      2. Submit (sells first, buys descending, per-row outcomes).
      3. FINALISE: write what was actually submitted AND consume the income
         ledger by the resulting net buy value -- one call, one transaction,
         idempotent on the run id.
      4. log_activity.

    Partial failure is normal and is reported per row; nothing is rolled back,
    and a run that submitted NOTHING is still finalised -- an unfinalised run
    sits in ``get_unconsumed_runs()``, which is meant to mean "money may have
    moved and only the broker knows", not "this run had nothing to do".

    ``allow_fractional`` is taken from the plan and from nowhere else: it is the
    setting the dry run the user approved was solved with.
    """
    from .portfolio_allocation_store import record_allocation_run

    run = record_allocation_run(
        account.id, mode, plan.to_dict(),
        scope_label=scope_label,
        base_notional=base.base_notional,
        available_buying_power=base.available_buying_power,
        allow_fractional=bool(plan.allow_fractional),
    )
    run_id = run.id

    try:
        outcomes = submit_plan(account, plan, current, run_tag=str(run_id),
                               allow_fractional=bool(plan.allow_fractional))
    except Exception:
        # submit_plan validates BEFORE its first order and catches per row, so a
        # raise out of it means nothing went out. Stamp the run as having taken
        # nothing rather than leaving a phantom in the recovery queue forever.
        logger.error(f"Allocation run {run_id} was refused before any order was sent",
                     exc_info=True)
        _finalise_run(run_id, {"submitted_buy_value": 0.0, "submitted_sell_value": 0.0,
                               "order_ids": []})
        raise

    totals = summarise_outcomes(plan, outcomes)
    income_consumed = _finalise_run(run_id, totals)

    counts = {status: sum(1 for o in outcomes if o.status == status)
              for status in (OUTCOME_SUBMITTED, OUTCOME_PARTIAL, OUTCOME_WASHTRADE_LOCKED,
                             OUTCOME_FAILED, OUTCOME_SKIPPED)}
    failed = counts[OUTCOME_FAILED]
    reached_the_broker = counts[OUTCOME_SUBMITTED] + counts[OUTCOME_PARTIAL]
    # A PARTIAL row is not a success even with nothing FAILED beside it: a close
    # where 2 of 3 transactions went out, or an order the broker part-filled,
    # leaves the account somewhere the user never approved. The one-line summary
    # is all most people read, and SUCCESS on it would end the conversation.
    if not failed and not counts[OUTCOME_PARTIAL]:
        severity = ActivityLogSeverity.SUCCESS
    elif reached_the_broker:
        severity = ActivityLogSeverity.WARNING
    else:
        # Nothing at all got out. WARNING would read as "mostly fine".
        severity = ActivityLogSeverity.FAILURE

    log_activity(
        severity,
        ActivityLogType.ORDER_SUBMITTED,
        RUN_ACTIVITY_FMT.format(
            run_id=run_id,
            scope=f"{mode} / {scope_label}" if scope_label else mode,
            submitted=counts[OUTCOME_SUBMITTED],
            partial=counts[OUTCOME_PARTIAL],
            locked=counts[OUTCOME_WASHTRADE_LOCKED],
            failed=failed,
            skipped=counts[OUTCOME_SKIPPED]),
        data={
            "run_id": run_id,
            "mode": mode,
            "scope_label": scope_label,
            "submitted_buy_value": totals["submitted_buy_value"],
            "submitted_sell_value": totals["submitted_sell_value"],
            "income_consumed": income_consumed,
            "rows": [o.to_dict() for o in outcomes],
        },
        source_account_id=account.id,
    )

    return {
        "run_id": run_id,
        "outcomes": outcomes,
        "submitted_buy_value": totals["submitted_buy_value"],
        "submitted_sell_value": totals["submitted_sell_value"],
        "order_ids": totals["order_ids"],
        "income_consumed": income_consumed,
    }
