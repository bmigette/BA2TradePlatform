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
import inspect
import re
from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from ..logger import logger
from .db import InstanceNotFound, add_instance, get_db, get_instance, log_activity
from .models import Transaction, TradingOrder
from .portfolio_allocation import (
    ACTION_ADJUST, ACTION_CLOSE, ACTION_NEW, ACTION_SKIP, ACTION_UNACTIONABLE,
    FRACTIONAL_PATH_WHOLE,
    AllocationPlan, BaseSnapshot, FilledTotals, MarginInfo, OrderFill,
    PositionFetchFailed, PositionState,
    apply_order_impacts, decide_symbol_action, held_no_price_block,
    measure_filled_values, plan_quantity_attempts, signed_position_values,
    split_delta_fifo,
)
from .TransactionHelper import TransactionHelper
from .types import (
    ActivityLogSeverity, ActivityLogType, AssetClass, BrokerOrderErrorReason,
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)


def _open_transaction_ids(account_id: int, symbols: List[str]) -> Dict[str, List[int]]:
    """``{symbol: [transaction_id]}`` for OPENED/CLOSING EQUITY transactions, oldest first.

    Transaction has NO account_id column -- it links to an account only through
    ``TradingOrder.account_id``, hence the join. Ordering is by primary key,
    which is creation order, so submission can consume them FIFO.

    The ELIGIBLE half of ``_partition_open_transaction_ids``; see there for the
    filter and for why the rejected half is kept rather than dropped.
    """
    return _partition_open_transaction_ids(account_id, symbols)[0]


def _partition_open_transaction_ids(
        account_id: int, symbols: List[str]) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
    """Split this account's open transactions into the ones the equity planner may
    act on and the ones it may not.

    Returns:
        Tuple[Dict[str, List[int]], Dict[str, List[int]]]: ``(eligible,
        unactionable)``, each ``{symbol: [transaction_id]}`` oldest first, each
        holding only the symbols that have any. Together they are every
        OPENED/CLOSING transaction the query found, so a symbol missing from BOTH
        really has none.

    The second dict exists ONLY so a filtered-out holding can still be described.
    Nothing downstream may trade from it: ``build_position_states`` puts it on
    ``PositionState.unactionable_transaction_ids``, which no submission path
    walks. Dropping it on the floor is what made a symbol whose only transactions
    are option-classed report "nothing to do" for shares the user had asked to
    sell -- ``decide_symbol_action`` gates "held" on the ELIGIBLE list, so the
    filter changed the ACTION and not merely the ids, and ACTION_SKIP's message
    is the one an untouched symbol gets.
    """
    if not symbols:
        return {}, {}
    out: Dict[str, List[int]] = {}
    rejected: Dict[str, List[int]] = {}
    with get_db() as session:
        statement = select(Transaction).join(TradingOrder).where(
            TradingOrder.account_id == account_id,
            Transaction.symbol.in_(symbols),
            Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.CLOSING]),
        ).distinct()
        for txn in session.exec(statement).all():
            # SEAM 1 / OPT-L2 — an option Transaction's `symbol` is the UNDERLYING ticker
            # (models.Transaction: "symbol stays the UNDERLYING and must never hold an OCC
            # contract string"), so without this filter the allocation planner reads a covered
            # call as a holding of the stock. Every id here is fed to PositionState, and both
            # submission paths then walk it: `_close_symbol` calls close_transaction on each,
            # `_adjust_symbol` splits the delta across them FIFO and calls
            # adjust_quantity_with_tpsl. Those two are now guarded, so the option leg is
            # REFUSED rather than routed -- but a refusal is not the fix. A "set AAPL to 0%"
            # run still sells the 100 shares and reports the row `partially_filled`, i.e. it
            # sells the collateral out from under a short call and leaves it NAKED; and an
            # ADJUST row still hands the option's CONTRACT count to split_delta_fifo, so the
            # trim under-sells the equity by that much and can never converge. The option must
            # not be in the plan at all.
            #
            # ALLOW-list, not deny-list: `== EQUITY` rather than `!= OPTION`. This planner
            # builds bare equity MARKET orders (`_open_symbol`) and exits through the equity
            # close path, so an asset class it has never heard of must be INVISIBLE here until
            # someone deliberately teaches it that class -- `!= OPTION` would sweep the next
            # one in by default. Safe against legacy rows: the column is NOT NULL with
            # server_default 'EQUITY' (alembic b2f4c81d6a35), so nothing pre-dating options
            # can fall out. option_lifecycle_service._open_option_transactions already filters
            # on exactly this column; allocation was the caller that skipped it.
            if txn.asset_class != AssetClass.EQUITY:
                # Remembered, never routed. The row is a real position in this
                # symbol and the operator has to be able to see WHY the equity
                # planner did nothing about it.
                rejected.setdefault(txn.symbol, []).append(txn.id)
                continue
            out.setdefault(txn.symbol, []).append(txn.id)
    return ({symbol: sorted(ids) for symbol, ids in out.items()},
            {symbol: sorted(ids) for symbol, ids in rejected.items()})


def build_position_states(account, symbols: List[str]) -> Dict[str, PositionState]:
    """Positions + live prices + open transaction ids for the managed symbols.

    A managed symbol with no position is returned FLAT (quantity 0) but priced,
    so the wizard can open a position in it. A symbol with no price keeps
    ``price=None`` and the engine will skip it with a reason.

    ``transaction_ids`` carries only what this planner may act on;
    ``unactionable_transaction_ids`` carries what the equity filter held back
    (see ``_partition_open_transaction_ids``). Both are populated from ONE query,
    so a symbol can never appear to have no transactions merely because the
    second read raced the first.

    **Shorts come back signed negative**, via the pure engine's
    ``signed_position_values`` -- the SAME call the page's
    ``ui/utils/portfolio_allocation_view.positions_by_symbol`` makes, deliberately
    shared rather than copied. This path read ``qty`` RAW for two releases while
    the page normalised, so on TastyTrade (``qty=abs_qty`` with the direction in
    ``side``) a short arrived POSITIVE and was counted as a long by
    ``compute_base_notional`` -- inflating ``BaseSnapshot.base_notional`` and with
    it every label target on the account, and flipping the sign of the wizard's
    unrealised P&L. Alpaca signs its own shorts, so the forcing must be (and is)
    idempotent; latent rather than live only because the production DB holds
    Alpaca accounts only.

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
    txn_ids, unactionable_ids = _partition_open_transaction_ids(account.id, wanted)

    states: Dict[str, PositionState] = {}
    for symbol in wanted:
        position = held.get(symbol)
        quantity, cost_basis, market_value = signed_position_values(
            getattr(position, 'side', None) if position else None,
            quantity=float(getattr(position, 'qty', 0.0) or 0.0) if position else 0.0,
            cost_basis=float(getattr(position, 'cost_basis', 0.0) or 0.0) if position else 0.0,
            market_value=float(getattr(position, 'market_value', 0.0) or 0.0) if position else 0.0,
        )
        states[symbol] = PositionState(
            symbol=symbol,
            quantity=quantity,
            cost_basis=cost_basis,
            price=prices.get(symbol),
            market_value=market_value,
            transaction_ids=list(txn_ids.get(symbol, [])),
            unactionable_transaction_ids=list(unactionable_ids.get(symbol, [])),
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

#: The account holds the shares, the user asked to sell them, and this planner
#: has no route to them: every open transaction behind the position is one it
#: does not act on (live: option-classed). Its own status rather than
#: OUTCOME_SKIPPED, and that distinction is the whole point -- "skipped /
#: nothing to do" is what a symbol ALREADY at target says, so an exit that
#: cannot happen and an exit that does not need to happen read identically. It
#: is not OUTCOME_FAILED either: nothing was refused, nothing was sent, and no
#: retry will help. A human has to look.
OUTCOME_UNACTIONABLE = "unactionable"

#: The whole of an unactionable row's Detail cell. Names the share count, the
#: reason and the transaction ids, because "unactionable" alone sends the
#: operator looking through the transactions list for something to notice.
UNACTIONABLE_OPTION_HOLDING_FMT = (
    "{quantity:g} share(s) of {symbol} are held at the broker, but every open "
    "transaction for the symbol is an OPTION (transaction {ids}). The equity "
    "allocation planner does not act on option transactions, so NOTHING was "
    "submitted and the position is unchanged. Unwind those transaction(s) by "
    "hand, or re-run once they are closed.")

#: Appended to a CLOSE or ADJUST row that DID trade, when some of the symbol's
#: open transactions were filtered out of it. Before the equity filter those
#: legs were in the list and failed loudly; now they are simply absent, so a
#: "set AAPL to 0%" run over a covered call reported a clean green ``submitted``
#: with an empty Detail. The shares moved and the option did not, and that is
#: exactly the fact the row stopped mentioning.
UNACTED_OPTION_LEGS_FMT = (
    "note: {count} open OPTION transaction(s) on {symbol} (transaction {ids}) "
    "were NOT part of this row - the equity planner leaves them alone, so "
    "anything these shares were covering is still open")


def _txn_id_list(ids: List[int]) -> str:
    """``[41, 42]`` -> ``"41, 42"``. Never an empty string: both callers are
    guarded on a non-empty list, and a message that named no ids would be the
    silence this whole path exists to remove."""
    return ", ".join(str(i) for i in ids)


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


def _noop_order_id(order_id: int) -> None:
    """The default ``on_order_id``: remember nothing.

    A separate named function rather than ``lambda _: None`` so that a caller that
    forgot to pass a recorder is visible in a traceback, and so the parameter can
    never be ``None`` at a call site.
    """


def _record_order_ids(on_order_id, order_ids) -> None:
    """Hand each id to the recorder, and never let the recorder break a submission.

    Swallowing is the lesser evil in BOTH directions. Before a new order goes out,
    raising would abort a submission over a bookkeeping write; after a close, the
    order is already at the broker and raising would lose the whole row's outcome
    as well as the id. The recorder's other job -- the in-process list
    ``run_allocation`` keeps -- happens before the DB write, so a failed write
    still leaves the ids in the finalise call.
    """
    for order_id in (order_ids or []):
        if order_id is None:
            continue
        try:
            on_order_id(order_id)
        except Exception as e:  # noqa: BLE001 -- a bookkeeping write, not the money path
            logger.error(f"Allocation: could not record order {order_id} against its "
                         f"run: {e}", exc_info=True)


def submit_plan(account, plan: AllocationPlan, current: Dict[str, PositionState],
                *, run_tag: str, allow_fractional: bool,
                on_order_id=_noop_order_id) -> List[RowOutcome]:
    """Submit a plan: every SELL first, then the BUYs by descending value.

    Decision 13 (sells before buys) and the "buying_power shrinks as buys fill"
    risk: descending value means a shortfall truncates the SMALLEST positions.

    ``on_order_id`` is called with each TradingOrder id THE MOMENT it exists, not
    at the end. For a new order that is after the row is persisted and BEFORE the
    broker is handed it, so the id is durable while the order is still incapable
    of filling; for a close or an adjustment the id is minted inside the adapter,
    so the earliest possible point is the instant the adapter returns it -- which
    is still before the NEXT row is attempted. The outcomes carry the same ids, but
    only for the rows that survived to return one: the backstop in ``_submit_row``
    turns any unexpected raise into an outcome with an EMPTY id list, and a run
    that then dies has no other record of the orders it really sent.

    A sell that FAILS does not abandon the buys. Stopping half way would leave the
    account further from target than doing nothing, while the outcome table said
    nothing at all about the rows never attempted. Partial failure is normal: each
    row reports its own outcome and nothing is rolled back.

    THE SELLS-FIRST ORDER IS LOAD-BEARING NOW, not merely tidy. ``_apply_bp_scaling``
    sizes the buys against ``available_buying_power`` PLUS what this plan's own
    sells free, so the freed money has to be at the broker before a buy asks for
    it. It used to size them against the pre-sell figure alone -- which meant a
    refused close could not make the buys overspend, and also meant a rebalance
    that sold 2,112 to buy 1,187 scaled every buy to a third of itself for no
    reason. A refused close can now leave a buy short of room; the broker rejects
    that one buy and ``_submit_row`` reports it on its own row, which is the same
    containment every other per-row failure gets. The dry run can see it coming:
    un-ticking a sell there re-measures the budget through ``filter_plan_rows``.

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
                                    run_tag=run_tag, allow_fractional=allow_fractional,
                                    on_order_id=on_order_id))
    for row in plan.buy_rows:
        outcomes.append(_submit_row(account, row, current.get(row.symbol),
                                    run_tag=run_tag, allow_fractional=allow_fractional,
                                    on_order_id=on_order_id))

    traded = {o.symbol for o in outcomes}
    for row in plan.rows:
        if row.symbol not in traded:
            outcomes.append(RowOutcome(
                symbol=row.symbol, action=ACTION_SKIP, status=OUTCOME_SKIPPED,
                message="; ".join(row.reasons) or "no delta",
            ))
    return outcomes


def _submit_row(account, row, state, *, run_tag: str, allow_fractional: bool,
                on_order_id=_noop_order_id) -> RowOutcome:
    action = decide_symbol_action(row, state)
    try:
        if action == ACTION_SKIP:
            return RowOutcome(symbol=row.symbol, action=ACTION_SKIP, status=OUTCOME_SKIPPED,
                              message="; ".join(row.reasons) or "nothing to do")
        if action == ACTION_UNACTIONABLE:
            return _unactionable_row(row, state)
        if action == ACTION_CLOSE:
            return _note_unacted_legs(
                _close_symbol(account, row, state, on_order_id=on_order_id), state)
        if action == ACTION_ADJUST:
            return _note_unacted_legs(
                _adjust_symbol(account, row, state, on_order_id=on_order_id), state)
        if action == ACTION_NEW:
            return _open_symbol(account, row, run_tag=run_tag,
                                allow_fractional=allow_fractional, on_order_id=on_order_id)
        # An action this function does not recognise must NOT fall through to
        # _open_symbol, which BUYS. The backstop below turns this into a FAILED
        # row, which is the right answer for "the engine asked for something we
        # cannot do".
        raise ValueError(f"unknown allocation action {action!r} for {row.symbol}")
    except Exception as e:
        # Backstop only -- every branch below catches its own IO per unit of work,
        # so that one dead transaction does not abandon the rest of the symbol.
        logger.error(f"Allocation submission failed for {row.symbol}: {e}", exc_info=True)
        return RowOutcome(symbol=row.symbol, action=action, status=OUTCOME_FAILED,
                          message=str(e) or e.__class__.__name__)


def _unactionable_row(row, state) -> RowOutcome:
    """The loud outcome for a holding this planner has no route to.

    ``quantity`` is the shares the BROKER reports, not what was sent -- nothing
    was sent -- because the size of the thing that did not happen is the first
    question anyone reading the table asks. ``transaction_ids`` carries the
    option ids so the run's activity-log JSON keeps them too, not just the
    sentence.

    ``float(state.quantity)`` with NO ``or 0.0``: ``decide_symbol_action`` only
    reaches this branch once it has established the quantity is a number greater
    than zero, and a message whose whole job is to name the size of an
    unreachable holding must not be able to say "0 share(s)".
    """
    ids = list(state.unactionable_transaction_ids)
    shares = abs(float(state.quantity))
    logger.warning(
        f"Allocation: {row.symbol} is held ({shares}) but every open transaction "
        f"for it is one the equity planner does not act on "
        f"(transaction {_txn_id_list(ids)}); NOTHING was submitted for this row"
    )
    return RowOutcome(
        symbol=row.symbol, action=ACTION_UNACTIONABLE, status=OUTCOME_UNACTIONABLE,
        quantity=shares,
        transaction_ids=ids,
        message=UNACTIONABLE_OPTION_HOLDING_FMT.format(
            quantity=shares, symbol=row.symbol, ids=_txn_id_list(ids)),
    )


def _note_unacted_legs(outcome: RowOutcome, state) -> RowOutcome:
    """Append the "and these were left alone" note to a row that DID trade.

    The status is deliberately untouched. The equity sale really was submitted,
    and calling that a partial failure would make every ordinary rebalance of a
    covered-call symbol look broken; what was missing was the SENTENCE. Mutated
    in place rather than rebuilt so no field of the real outcome can be lost in
    a copy.
    """
    ids = list(getattr(state, "unactionable_transaction_ids", None) or [])
    if not ids:
        return outcome
    note = UNACTED_OPTION_LEGS_FMT.format(count=len(ids), symbol=outcome.symbol,
                                          ids=_txn_id_list(ids))
    outcome.message = f"{outcome.message}; {note}" if outcome.message else note
    return outcome


def _leg_status(succeeded: float, failed: int) -> str:
    """The row status for a multi-leg symbol: SUBMITTED / PARTIAL / FAILED.

    A row is one SYMBOL, but a close or a trim can be several transactions and
    each is its own order. Collapsing "2 of 3 legs went out" to FAILED is not a
    conservative rounding: the run's audit row would then describe a symbol whose
    sells really happened as one that did nothing, and the user reading it cannot
    tell which two positions are now closed. (The MONEY is not at risk either way
    -- ``collect_order_fills`` measures every order id the row created, whatever
    the row's status -- but the report is.) Collapsing it to SUBMITTED is the
    opposite lie: the user is told the position is at target when it is not.
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


def _close_one_transaction(account, txn_id: int, symbol: str,
                           on_order_id=_noop_order_id) -> Tuple[bool, List[int], str]:
    """``close_transaction`` for ONE transaction.

    Returns:
        (ok, order_ids, message). ``order_ids`` carries the broker order the
        close created -- ``close_transaction`` documents it as ``close_order_id``
        (AccountInterface.py:1567) and it is a TradingOrder this run is
        responsible for, so dropping it leaves the run audit unable to show the
        orders that closed the positions. It is ALSO handed to ``on_order_id``
        here rather than only returned, because a symbol can be several
        transactions and a raise on a later leg would take the earlier legs' ids
        down with the row.
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
    _record_order_ids(on_order_id, order_ids)
    if result.get('success'):
        return True, order_ids, ""
    return False, order_ids, f"txn {txn_id}: {result.get('message', 'close failed')}"


def _close_symbol(account, row, state, *, on_order_id=_noop_order_id) -> RowOutcome:
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
        ok, ids, message = _close_one_transaction(account, txn_id, row.symbol,
                                                  on_order_id)
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


def _adjust_symbol(account, row, state, *, on_order_id=_noop_order_id) -> RowOutcome:
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
            ok, ids, message = _close_one_transaction(account, txn_id, row.symbol,
                                                      on_order_id)
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
        created = list(result.get('orders_created') or [])
        order_ids.extend(created)
        _record_order_ids(on_order_id, created)
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


def _open_symbol(account, row, *, run_tag: str, allow_fractional: bool,
                 on_order_id=_noop_order_id) -> RowOutcome:
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
            account, row, quantity, run_tag=run_tag, path=path,
            on_order_id=on_order_id)
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
                      path: str, on_order_id=_noop_order_id) -> Tuple[RowOutcome, bool]:
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

    # HERE, and not one line later. The order row exists and the broker has not
    # been told about it yet, so this is the last instant at which the id can be
    # made durable while the order is still incapable of filling. Everything below
    # -- the submit itself, the classification, the whole-share retry -- can raise,
    # time out or be killed with the order live at the broker.
    _record_order_ids(on_order_id, [order_id])

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

    ORDER MATTERS, and it is upsert THEN drain. The two halves of a Refresh --
    "what money arrived" and "what did the last run really spend" -- routinely
    surface together: a dividend is credited and the buy it funded fills on the
    same day. Draining first measured the deferred run against a ledger that did
    not yet hold the backdated event, so it consumed nothing AND stamped itself,
    and ``finalise_allocation_run``'s stamp is one-shot -- the deposit then showed
    as 100% unallocated for good while its position sat in the book.

    Returns:
        int: how many NEW events were inserted (a restatement is not counted; a
        broker failure is logged and returns 0 rather than looking like "no
        income").
    """
    inserted = _upsert_income_window(account, days=days)

    # Decision D3: a deferred run's income stays open until something re-measures
    # it, and deferral is the common case. This is the income panel's Refresh AND
    # the allocation page's load call, so draining here is what stops a quarterly
    # rebalancer's income from sitting unallocated for a quarter. DB-only unless
    # there is genuinely something pending.
    #
    # Outside the upsert's own error handling on purpose: it is the one half that
    # can still make progress when the broker's activity feed is down, so it must
    # not sit behind that failure's early return. Never fatal either way -- the
    # sync is what refills the panel, and a broken drain must degrade to "not
    # consumed yet", not to an empty income panel.
    try:
        reconcile_unconsumed_runs(account)
    except Exception as e:
        logger.error(f"Reconciling unconsumed allocation runs failed for account "
                     f"{account.id}: {e}", exc_info=True)

    return inserted


def _upsert_income_window(account, *, days: int) -> int:
    """The broker half of ``sync_income_events``: read the window, write the ledger.

    Split out so the drain above it can run unconditionally -- inline, its early
    returns took the drain with them.

    Returns:
        int: how many NEW events were inserted. 0 for a broker failure, which is
        logged; a restatement is not counted.
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

#: The one-line activity-log summary of a run. Every outcome the vocabulary has
#: is named: a line that mentions only "submitted" and "failed" hides a
#: wash-trade lock completely, and that is an order the user never learns about.
RUN_ACTIVITY_FMT = (
    "Portfolio allocation run {run_id} ({scope}): {submitted} submitted, "
    "{partial} partially filled, {locked} wash-trade locked, {failed} failed, "
    "{unactionable} unactionable, {skipped} skipped")

#: What the run really MOVED, appended to the line above. Submitted counts say
#: how many orders went out; only this says how much money did.
RUN_FILLED_FMT = "; {buys:.2f} bought / {sells:.2f} sold (filled)"

#: Appended when at least one order can still fill. The income was NOT consumed,
#: and a line that did not say so would leave the user reading "0 failed" while
#: their deposit still shows as unallocated.
RUN_UNSETTLED_FMT = ("; {orders} order(s) still working, income not consumed yet "
                     "- it is re-measured on the next Refresh or allocation run")

#: Said INSTEAD of the line above when the broker refresh itself failed. The two
#: are not the same fact and must not share wording: "0 order(s) still working"
#: describes a run with nothing outstanding, while this one means our rows were
#: never confirmed at all, so whatever they say is not evidence.
RUN_REFRESH_FAILED_FMT = (
    "; the broker order refresh FAILED, so nothing about this run's fills is "
    "confirmed and no income was consumed - it is re-measured on the next Refresh "
    "or allocation run")


def _finalise_run(run_id: int, totals: FilledTotals, order_ids: List[int]) -> float:
    """Write the run's FILLED totals and spend the ledger, in ONE store transaction.

    The net buy value is deliberately NOT passed: the store derives it from the
    totals it is writing, on the row as it re-reads it. That is what makes
    "consumed nothing because the caller handed over a stale zero" unrepresentable.

    ``totals.settled`` decides whether the ledger is spent AT ALL. False means at
    least one order can still fill, so the money figure is not final: the store
    records it and leaves ``income_consumed_at`` NULL, keeping the run in
    ``get_unconsumed_runs()`` for the next reconcile pass.

    Returns:
        float: what the ledger actually gave up -- possibly LESS than the net buy
        value, which is not an error (buying power, not the ledger, is the
        feasibility constraint), 0.0 for a deferred run, and 0.0 when the run row
        could not be found.
    """
    from .portfolio_allocation_store import finalise_allocation_run

    try:
        finalised = finalise_allocation_run(
            run_id,
            filled_buy_value=totals.buy_value,
            filled_sell_value=totals.sell_value,
            order_ids=list(order_ids),
            orders_settled=totals.settled)
    except InstanceNotFound:
        logger.error(f"Allocation run {run_id} vanished before it could be finalised; "
                     f"its income was NOT consumed")
        return 0.0
    return finalised.income_consumed_amount


# ---------------------------------------------------------------------------
# What the run actually filled. The income ledger's only input.
# ---------------------------------------------------------------------------

def get_unconsumed_runs(account_id: int, limit: Optional[int] = 20):
    """Runs whose income was never consumed -- the recovery view. Read-only.

    Re-exported here so the UI and the reconcile path have ONE service-level
    surface; the query itself belongs to the store. ``limit=None`` means no cap,
    and both money paths below pass it -- see the store's docstring for why
    inheriting the display default silently strands the oldest runs forever.
    """
    from .portfolio_allocation_store import get_unconsumed_runs as _store_runs
    return _store_runs(account_id, limit)


def refresh_orders_from_broker(account) -> bool:
    """``account.refresh_orders(fetch_all=True)``, tolerant of three signatures.

    Mirrors TradeManager.py:1600-1607, which does exactly this immediately after
    its own submissions -- "important for market orders which fill immediately".

    The signature is INSPECTED rather than the TypeError caught: AlpacaAccount
    takes ``(heuristic_mapping, fetch_all)`` (:2180), TastyTradeAccount takes
    ``**kwargs`` (:988), IBKRAccount takes nothing at all (:541). Catching TypeError
    and retrying bare would also swallow a TypeError raised INSIDE a broker's
    refresh and then run the whole thing a second time.

    Returns:
        bool: False when the refresh failed. The caller must then treat its fill
        measurement as NOT settled -- our rows still say whatever they said before
        the broker was asked, and consuming income against that is guessing.
    """
    try:
        parameters = inspect.signature(account.refresh_orders).parameters
        accepts_fetch_all = "fetch_all" in parameters or any(
            p.kind == p.VAR_KEYWORD for p in parameters.values())
        if accepts_fetch_all:
            account.refresh_orders(fetch_all=True)
        else:
            account.refresh_orders()
        return True
    except Exception as e:
        logger.error(f"[Account {account.id}] Could not refresh orders after an "
                     f"allocation submission: {e}", exc_info=True)
        return False


def collect_order_fills(account_id: int, order_ids: List[int]) -> List[OrderFill]:
    """Read the run's EXECUTION orders back out of the DB as pure fill facts.

    ONLY ``OrderType.MARKET`` rows. A run's ``order_ids`` also carry the protective
    legs that ``TransactionHelper.adjust_quantity_with_tpsl`` rebuilds around a
    resized position -- OCO, SELL_LIMIT/BUY_LIMIT and SELL_STOP/BUY_STOP. Those are
    not trades this run made; they sit unfilled for weeks by design. Counting them
    would book a stop-loss as a sale, and waiting for them would stall the run's
    income indefinitely. Every execution order IS a MARKET order: the new order,
    the partial-close order and the add-to-position order.

    An id with no row emits ``OrderFill(status=None)``, which ``measure_filled_values``
    treats as still working -- a vanished order row is an inconsistency, and
    stalling is the only safe reading of it. The query is scoped by ``account_id``
    as well, so an order that belongs to somebody else reads as absent rather than
    pricing this run from another account's fill.

    A NULL ``filled_qty`` is passed through AS ``None``, exactly like a NULL
    ``open_price`` on the next line -- never coerced to 0.0. Both broker adapters
    leave that column NULL after advancing a status (AlpacaAccount only writes a
    quantity the broker actually sent; TastyTradeAccount's
    ``float(db or 0.0) != float(broker or 0.0)`` never overwrites a NULL with its
    ``_fills_summary`` zero), and reading it as a measured zero-share fill let the
    run settle for nothing and its income be deployed a second time.
    """
    if not order_ids:
        return []
    wanted = list(dict.fromkeys(int(order_id) for order_id in order_ids))
    with get_db() as session:
        rows = session.exec(
            select(TradingOrder).where(
                TradingOrder.account_id == account_id,
                TradingOrder.id.in_(wanted),
            )
        ).all()
        by_id = {row.id: (row.order_type, row.side, row.status,
                          row.filled_qty, row.open_price) for row in rows}

    fills: List[OrderFill] = []
    for order_id in wanted:
        found = by_id.get(order_id)
        if found is None:
            logger.error(f"[Account {account_id}] Allocation order {order_id} has no "
                         f"TradingOrder row; its run cannot be income-consumed until "
                         f"someone works out what happened to it")
            fills.append(OrderFill(order_id=order_id))
            continue
        order_type, side, status, filled_qty, open_price = found
        if order_type != OrderType.MARKET:
            logger.debug(f"[Account {account_id}] Order {order_id} is a {order_type} "
                         f"protective leg, not an execution order -- not measured")
            continue
        fills.append(OrderFill(
            order_id=order_id, side=side, status=status,
            filled_quantity=float(filled_qty) if filled_qty is not None else None,
            fill_price=float(open_price) if open_price is not None else None,
        ))
    return fills


def measure_run_fills(account, order_ids: List[int]) -> Tuple[FilledTotals, bool]:
    """Ask the broker what really happened, then price it. The ledger's input.

    Refresh once, read our own rows back, measure. No polling and no waiting: the
    wizard submits MARKET orders during market hours, which is the case TradeManager
    already relies on this single refresh for. Anything still working comes back
    ``settled=False`` and is reconciled later -- by the income panel's Refresh or by
    the next run.

    Returns:
        Tuple[FilledTotals, bool]: the totals, and whether the broker refresh
        SUCCEEDED. The flag is returned rather than folded into the totals because
        the two unsettled cases read identically otherwise and need different
        words: "N orders are still working" versus "we could not ask". A failed
        refresh forces ``settled=False`` with ``working_order_ids`` EMPTY -- our
        rows may well say FILLED, they are just not evidence -- and a caller with
        only the totals then tells the user "0 order(s) still working", which
        describes a run with nothing outstanding rather than one nobody could
        price.
    """
    refreshed = refresh_orders_from_broker(account)
    totals = measure_filled_values(collect_order_fills(account.id, order_ids))
    if not refreshed:
        # Our rows say whatever they said BEFORE the broker was asked. Whatever
        # they show, it is not evidence, so nothing may be consumed against it.
        totals.settled = False
    return totals, refreshed


def reconcile_unconsumed_runs(account) -> List[int]:
    """Finalise every past run whose orders have settled since. The recovery drain.

    ``get_unconsumed_runs`` lists runs with a NULL ``income_consumed_at``: ones that
    died mid-submit, and ones deliberately deferred because an order could still
    fill. This re-measures each and consumes the ledger for those now settled. Runs
    still working are left exactly where they are.

    Called from TWO places, both of them things the user just did: the top of
    ``run_allocation`` (after the market gate, before anything is written), and
    ``sync_income_events`` -- i.e. the income panel's Refresh and the allocation
    page's load. Deferral is the common case, so waiting for the next allocation run
    would leave a quarterly rebalancer's income open for a quarter. It is NOT a job
    and NOT a timer: one function, two existing entry points, at most one extra
    ``refresh_orders`` call, and only when there is something to reconcile.

    Returns:
        List[int]: the run ids that were consumed on this pass, oldest first.
    """
    from .portfolio_allocation_store import finalise_allocation_run

    # limit=None: EVERY unconsumed run, not the display default of 20. With 25
    # deferred runs the capped query drains the newest 20 and re-reads the same
    # oldest 5 into the window on the next pass, so those 5 never settle and their
    # income never comes off the "unallocated" figure.
    pending = list(reversed(get_unconsumed_runs(account.id, limit=None)))
    if not pending:
        return []

    logger.info(f"[Account {account.id}] Reconciling {len(pending)} allocation run(s) "
                f"that never consumed income")
    if not refresh_orders_from_broker(account):
        logger.warning(f"[Account {account.id}] Skipping reconciliation: the order "
                       f"refresh failed, so every fill figure would be stale")
        return []

    consumed: List[int] = []
    for run in pending:
        totals = measure_filled_values(collect_order_fills(account.id, run.order_ids or []))
        try:
            finalise_allocation_run(
                run.id,
                filled_buy_value=totals.buy_value,
                filled_sell_value=totals.sell_value,
                order_ids=list(run.order_ids or []),
                orders_settled=totals.settled)
        except InstanceNotFound:
            logger.error(f"Allocation run {run.id} vanished during reconciliation")
            continue
        if totals.settled:
            consumed.append(run.id)
    return consumed


def describe_unconsumed_runs(account_id: int) -> Dict[str, Any]:
    """Which runs still owe the ledger, and how many of their orders are working.

    DB ONLY -- no broker call, because this renders a panel. The actual re-measure
    is ``reconcile_unconsumed_runs``, which the same Refresh already ran.

    Returns:
        Dict[str, Any]: ``{"run_ids": [...], "working_order_ids": [...]}``, both
        oldest-first. Feed the two lengths to
        ``ba2_common.core.portfolio_allocation.unconsumed_income_notice`` for the
        panel's sentence.
    """
    # limit=None, for the same reason the drain passes it: the panel's whole job
    # is to say how much is outstanding, and a capped count under-reports it.
    runs = list(reversed(get_unconsumed_runs(account_id, limit=None)))
    working: List[int] = []
    for run in runs:
        totals = measure_filled_values(collect_order_fills(account_id, run.order_ids or []))
        working.extend(totals.working_order_ids)
    return {"run_ids": [run.id for run in runs], "working_order_ids": working}


def run_allocation(account, plan: AllocationPlan, current: Dict[str, PositionState],
                   base: BaseSnapshot, *, mode: str,
                   scope_label: Optional[str] = None) -> Dict[str, Any]:
    """Submit a reviewed plan and record it. The single Submit entry point.

    Order of operations:
      0. GATE, twice: on the BASE (market valuation with a held symbol nobody could
         price -- ``held_no_price_block``) and then on MARKET HOURS. Either returns
         ``blocked=True`` having written nothing at all -- no run row, no stamped
         order comments and, above all, no income consumed. This is the FIRST
         statement in the function, above the reconcile in step 1: a blocked
         attempt must not move an earlier run's money either. The base gate comes
         first because its reason is the one the user can act on immediately.
      1. RECONCILE any earlier run whose income was left unconsumed, so this run's
         ledger reflects reality before it spends from it.
      2. INSERT the ``portfolio_allocation_run`` row with the plan snapshot and
         zero values, so its id can be stamped into every order comment.
      3. Submit (sells first, buys descending, per-row outcomes).
      4. REFRESH from the broker and MEASURE what actually filled.
      5. FINALISE: write the FILLED totals and consume the ledger by the resulting
         net buy value -- one call, one transaction, idempotent on the run id. If
         any order can still fill, the totals are written but the ledger is NOT
         spent, and the run stays in ``get_unconsumed_runs()`` for step 1 of the
         next run or the income panel's next Refresh. That is the COMMON outcome,
         not an error.
      6. log_activity.

    Money never comes from the plan. ``row.price`` is a quote taken before
    submission and ``outcome.quantity`` is what was SENT; the ledger is spent on
    ``filled_qty * open_price``, which is the only number that describes cash that
    left the account.

    Partial failure is normal and is reported per row; nothing is rolled back,
    and a run that submitted NOTHING is still finalised -- an unfinalised run
    sits in ``get_unconsumed_runs()``, which is meant to mean "money may have
    moved and only the broker knows", not "this run had nothing to do".

    ``allow_fractional`` is taken from the plan and from nowhere else: it is the
    setting the dry run the user approved was solved with.

    Returns:
        Dict[str, Any]: ``run_id``, ``outcomes``, ``order_ids``, ``income_consumed``,
        ``filled_buy_value``, ``filled_sell_value``, ``settled``,
        ``working_order_ids``, ``refresh_failed``, ``blocked``, ``blocked_reason``.
        When blocked the money keys are 0.0, ``settled`` is True (nothing was
        submitted, so there is nothing to wait for), ``working_order_ids`` is
        empty, ``refresh_failed`` is False (the broker was never asked) and
        ``run_id`` is None.

        ``refresh_failed`` is NOT redundant with ``settled``. A failed refresh
        forces ``settled=False`` with an EMPTY ``working_order_ids``, so a caller
        reading only those two tells the user "0 order(s) still working" -- which
        reads as "nothing outstanding" for a run nobody has been able to price.
    """
    # Market-hours gate, FIRST, before anything is written. Disabling the Submit
    # button is a courtesy; this is the enforcement -- the wizard can sit open across
    # 16:00. Blocking here means no run row, no stamped order comments and, above
    # all, no income consumed: finalise_allocation_run is one-shot, so a run created
    # for orders that were all rejected would mark that income spent forever.
    # TastyTrade's closed-market refusal is a SERVER rule that arrives as an opaque
    # Message, so without this the user would get a screen of unexplained per-row
    # failures after the ledger had already been hit.
    # The BASE gate, ahead of the clock. In market valuation a symbol the account
    # HOLDS but could not price contributes 0 to base_notional, so EVERY label's
    # target is understated by its share of the missing money -- and the dry run
    # cannot show it, because every row is proportionally too small and the table
    # looks self-consistent. Read off the BASE and not the plan: the plan's
    # no-price rows are never tickable, so filter_plan_rows drops them and the
    # filtered plan that arrives here looks clean.
    #
    # Checked BEFORE the market gate on purpose: both refuse, but a failed quote is
    # something the user can retry now while a closed market is only something to
    # wait out, so the actionable reason is the one they are told.
    reason = held_no_price_block(base.unpriced_held_symbols)
    if reason is None:
        reason = _market_blocked_reason(fetch_market_hours(account))
    if reason is not None:
        logger.warning(f"Allocation run for account {account.id} BLOCKED: {reason}; "
                       f"nothing was submitted and no run was recorded")
        return {
            "run_id": None,
            "outcomes": [],
            "order_ids": [],
            "income_consumed": 0.0,
            "filled_buy_value": 0.0,
            "filled_sell_value": 0.0,
            "settled": True,              # nothing was submitted, so nothing is pending
            "working_order_ids": [],
            "refresh_failed": False,      # the broker was never asked anything
            "blocked": True,
            "blocked_reason": f"{reason}. Nothing was submitted.",
        }

    # Drain any earlier run whose income is still unconsumed BEFORE this one
    # records anything. Deferral is the ordinary outcome (decision D3), so without
    # this the previous rebalance's income would still look unallocated and this
    # run would spend it a second time.
    reconcile_unconsumed_runs(account)

    from .portfolio_allocation_store import append_run_order_ids, record_allocation_run

    run = record_allocation_run(
        account.id, mode, plan.to_dict(),
        scope_label=scope_label,
        base_notional=base.base_notional,
        available_buying_power=base.available_buying_power,
        allow_fractional=bool(plan.allow_fractional),
    )
    run_id = run.id
    # Remember what this run actually used, so the next wizard opens on it.
    remember_fractional_choice(account.id, bool(plan.allow_fractional))

    # Every order id, written to the run row THE MOMENT it exists. Until this
    # existed the row said "created nothing" for the whole of the submission loop,
    # so a process killed in there -- or an OperationalError out of the measurement
    # below -- stranded orders that had really reached the broker in a run the
    # recovery drain then priced at zero, stamped, and dropped forever.
    recorded_ids: List[int] = []

    def _remember(order_id: int) -> None:
        # In-memory FIRST: the local list is what the finalise call uses, and it
        # must survive a DB write that fails. append_run_order_ids is what survives
        # the process instead.
        if order_id in recorded_ids:
            return
        recorded_ids.append(order_id)
        append_run_order_ids(run_id, [order_id])

    try:
        outcomes = submit_plan(account, plan, current, run_tag=str(run_id),
                               allow_fractional=bool(plan.allow_fractional),
                               on_order_id=_remember)
    except Exception:
        if recorded_ids:
            # An id was recorded, so this raise did NOT come before the first order:
            # something is live at the broker. Stamping FilledTotals() here would
            # mark the run "took nothing" PERMANENTLY (the stamp is one-shot).
            # Leaving it unfinalised is exactly what get_unconsumed_runs() is for.
            logger.error(f"Allocation run {run_id} raised with order(s) {recorded_ids} "
                         f"already created; leaving it UNCONSUMED for the reconcile "
                         f"drain rather than stamping it as having taken nothing",
                         exc_info=True)
        else:
            # submit_plan validates BEFORE its first order and catches per row, so a
            # raise with nothing recorded really does mean nothing went out. Stamp
            # the run rather than leaving a phantom in the recovery queue forever.
            logger.error(f"Allocation run {run_id} was refused before any order was sent",
                         exc_info=True)
            _finalise_run(run_id, FilledTotals(), [])
        raise

    # EVERY outcome's ids, not just the submitted ones. A hard submit failure leaves
    # the row at OrderStatus.ERROR (AccountInterface.py:148) -- terminal, worth 0,
    # settled, so it costs nothing to include. What it buys is the case that matters:
    # submit_order returned None on a response timeout while the broker actually took
    # the order. The refresh finds the fill and the ledger charges for it.
    #
    # Seeded from what was RECORDED, because the two are not the same set: the
    # backstop in _submit_row turns an unexpected raise into an outcome with no ids
    # at all, and finalise_allocation_run restates order_ids wholesale, so building
    # this from the outcomes alone would erase from the row an order the run had
    # already persisted.
    order_ids: List[int] = list(recorded_ids)
    for outcome in outcomes:
        for order_id in outcome.order_ids:
            if order_id not in order_ids:
                order_ids.append(order_id)

    totals, refreshed = measure_run_fills(account, order_ids)
    if totals.unmeasurable_order_ids:
        logger.error(f"Allocation run {run_id}: order(s) "
                     f"{totals.unmeasurable_order_ids} report a fill with no usable "
                     f"price or side; their value is NOT being guessed at, so this "
                     f"run's income stays unconsumed until they can be read")
    income_consumed = _finalise_run(run_id, totals, order_ids)

    counts = {status: sum(1 for o in outcomes if o.status == status)
              for status in (OUTCOME_SUBMITTED, OUTCOME_PARTIAL, OUTCOME_WASHTRADE_LOCKED,
                             OUTCOME_FAILED, OUTCOME_UNACTIONABLE, OUTCOME_SKIPPED)}
    failed = counts[OUTCOME_FAILED]
    reached_the_broker = counts[OUTCOME_SUBMITTED] + counts[OUTCOME_PARTIAL]
    # A PARTIAL row is not a success even with nothing FAILED beside it: a close
    # where 2 of 3 transactions went out, or an order the broker part-filled,
    # leaves the account somewhere the user never approved. The one-line summary
    # is all most people read, and SUCCESS on it would end the conversation.
    # An UNACTIONABLE row is on the same side of that line: the user asked to
    # exit a position and the run had no route to it.
    if not failed and not counts[OUTCOME_PARTIAL] and not counts[OUTCOME_UNACTIONABLE]:
        severity = ActivityLogSeverity.SUCCESS
    elif reached_the_broker:
        severity = ActivityLogSeverity.WARNING
    else:
        # Nothing at all got out. WARNING would read as "mostly fine".
        severity = ActivityLogSeverity.FAILURE

    description = RUN_ACTIVITY_FMT.format(
        run_id=run_id,
        scope=f"{mode} / {scope_label}" if scope_label else mode,
        submitted=counts[OUTCOME_SUBMITTED],
        partial=counts[OUTCOME_PARTIAL],
        locked=counts[OUTCOME_WASHTRADE_LOCKED],
        failed=failed,
        unactionable=counts[OUTCOME_UNACTIONABLE],
        skipped=counts[OUTCOME_SKIPPED])
    description += RUN_FILLED_FMT.format(buys=totals.buy_value, sells=totals.sell_value)
    if not refreshed:
        description += RUN_REFRESH_FAILED_FMT
    elif not totals.settled:
        description += RUN_UNSETTLED_FMT.format(orders=len(totals.working_order_ids))

    log_activity(
        severity,
        ActivityLogType.ORDER_SUBMITTED,
        description,
        data={
            "run_id": run_id,
            "mode": mode,
            "scope_label": scope_label,
            "filled": totals.to_dict(),
            "income_consumed": income_consumed,
            "rows": [o.to_dict() for o in outcomes],
        },
        source_account_id=account.id,
    )

    return {
        "run_id": run_id,
        "outcomes": outcomes,
        "order_ids": order_ids,
        "income_consumed": income_consumed,
        "filled_buy_value": totals.buy_value,
        "filled_sell_value": totals.sell_value,
        "settled": totals.settled,
        "working_order_ids": list(totals.working_order_ids),
        "refresh_failed": not refreshed,
        "blocked": False,
        "blocked_reason": None,
    }


def remember_fractional_choice(account_id: int, allow_fractional: bool) -> None:
    """Persist the fractional-shares choice so the next wizard opens on it.

    Never raises: forgetting a preference must not take down a submission. The
    default when nothing has ever been stored is ON
    (``PortfolioAllocationConfig.allow_fractional``).
    """
    from .portfolio_allocation_store import set_allocation_config
    try:
        set_allocation_config(account_id, allow_fractional=bool(allow_fractional))
    except Exception as e:
        logger.error(f"Could not remember the fractional choice for account "
                     f"{account_id}: {e}", exc_info=True)


def fetch_market_hours(account):
    """The broker's market-hours answer, or ``None`` when there is not one.

    Adapter only -- the DISPLAY decision is
    ``ui.utils.portfolio_allocation_view.evaluate_market_gate``, which is pure and
    takes plain values, and the MONEY decision is ``_market_blocked_reason`` below.
    This module never imports the UI: ``core -> ui`` is a layering inversion.

    ``None`` means "we do not know", which every caller must treat as NOT open. It
    covers a seam that raised, a seam that returned nothing, and an account object
    that predates the seam. ``get_market_hours`` is concrete on
    ``ReadOnlyAccountInterface`` and documented never to raise, so a real account
    always answers; the getattr guard and the try/except are for test doubles and for
    an account class that has not been rebased yet.

    Returns:
        Optional[MarketHours]: the broker's answer, or ``None``. Note that a
        ``MarketHours`` with ``is_known is False`` is NOT ``None`` -- it is a real
        answer meaning "unavailable", and callers must check both.
    """
    getter = getattr(account, "get_market_hours", None)
    if getter is None:
        logger.warning(f"Account {getattr(account, 'id', '?')} publishes no market hours "
                       f"seam; treating the market as unknown")
        return None
    try:
        hours = getter()
    except Exception as e:
        logger.error(f"Could not read market hours for account "
                     f"{getattr(account, 'id', '?')}: {e}", exc_info=True)
        return None
    if hours is None:
        logger.warning(f"Account {getattr(account, 'id', '?')} returned no market hours; "
                       f"treating the market as unknown")
    return hours


def clear_market_hours_cache(account) -> bool:
    """Drop the account's cached market-status answer so the next read refetches.

    ``ReadOnlyAccountInterface.get_market_hours`` caches for ``min(TTL, the next
    session boundary)``, which is right for the several reads one page render
    makes and wrong for a user who pressed Refresh *because* they believe the bell
    has gone. ``clear_market_hours_cache()`` is the interface's own EXPLICIT path
    for that, and it documents itself as being "for a user who hits Refresh"; this
    is the adapter that makes the docstring true, and it is called from the
    wizard's Refresh (``ui/pages/portfolio_allocation.py:_on_refresh`` via
    ``_solve_plan(force_market_refresh=True)``).

    Tolerant in the same way ``fetch_market_hours`` is: a test double or an account
    class that predates the seam simply has nothing to clear, and a broker whose
    clear explodes must not take a dry run down with it -- the worst case is a
    market-hours answer up to one TTL old, which is what the caller had anyway.

    Returns:
        bool: True when a cache was actually cleared.
    """
    clear = getattr(account, "clear_market_hours_cache", None)
    if clear is None:
        logger.debug(f"Account {getattr(account, 'id', '?')} publishes no market-hours "
                     f"cache to clear")
        return False
    try:
        clear()
    except Exception as e:
        logger.error(f"Could not clear the market-hours cache for account "
                     f"{getattr(account, 'id', '?')}: {e}", exc_info=True)
        return False
    return True


def _market_blocked_reason(hours) -> Optional[str]:
    """``None`` when the market is confirmed open, else the sentence to show.

    Derived from ``MarketHours`` DIRECTLY -- the view module owns the banner copy and
    must not be imported here. The two pieces of prose are allowed to differ in
    wording; they may not differ in the DECISION, which is why both are computed from
    one ``fetch_market_hours`` call.
    """
    if hours is None:
        return "Market hours could not be read, so the market is not confirmed open"
    if not hours.is_known:
        reason = "Market hours are unavailable, so the market is not confirmed open"
    elif not hours.is_open:
        reason = "Market is closed"
        # The broker's OWN word, display-only and never branched on: it is what tells
        # a user blocked at 17:00 that the gate is regular-session only.
        if hours.status:
            reason += f" (broker status: {hours.status})"
    else:
        return None
    if hours.detail:
        reason += f" - {hours.detail}"
    return reason
