"""One answer to "may this hand-driven submit go to the broker, and with what stop?".

WHY THIS EXISTS. Every automated submit passes the protective stop along
(``TradeManager`` submits funded entries with ``sl_price=fo.stop_price``; the wash-trade
unlock path re-threads the same value). The UI's own Submit buttons called
``submit_order(order)`` bare. On 2026-08-24 a funded WSC entry that had failed to reach
the broker was re-submitted by hand from the pending-orders table and filled as::

    465  WSC  BUY   MARKET      2.0              FILLED
    466  WSC  SELL  SELL_LIMIT  2.0  tp 22.9959  NEW      <- a take-profit
                                                          <- and no stop at all

The risk manager had already computed the stop ("safeguard SL for WSC: $19.01") and
stamped it on ``order.stop_price``; the button simply never passed it. The user held an
unprotected position.

THE POLICY IS NOT NEW. ``AccountInterface.submit_order`` already refuses to open a
position it cannot protect (the ``supports_protective_legs`` gate): *either the stop
exists, or nothing was opened*. This module applies the same rule one level up, to the
question the interface cannot answer for itself -- "the caller passed no stop; was that
because there is none, or because the caller dropped it?". Nothing here second-guesses
``supports_protective_legs``: when a derived stop reaches a broker that cannot place
legs, the interface's own gate raises and the UI shows it.

WHERE THE STOP COMES FROM, in order:

1. An explicit price the user typed (the Place Order dialog).
2. ``order.stop_price`` -- the risk manager's safeguard SL. This is the exact source
   ``TradeManager._check_all_washtrade_locked_orders`` re-threads, and it carries the
   same restriction, for the same reason: on a ``*_STOP`` / ``*_STOP_LIMIT`` order
   ``stop_price`` is the ENTRY TRIGGER, not a protective stop. Submitting a breakout's
   $25.00 trigger as a stop-loss on a $21.90 long would be an instant stop-out sold as
   protection.
3. ``Transaction.stop_loss`` -- what the position is supposed to be protected at,
   whoever wrote it.

An order that opens or increases exposure and has none of the three is REFUSED. Orders
that open no exposure are exempt and pass through untouched: a protective leg (it *is*
the protection), a close, and an entry that is already covered by a live protective leg.

OPTIONS ARE EXEMPT, deliberately. This platform never places protective stop legs on
option contracts -- option exits are managed by ``option_lifecycle_service``, and
``adjust_sl`` on an option transaction would place a stop on the UNDERLYING. Refusing
every manual option submit would block a working feature while protecting nothing.
"""
import math
from dataclasses import dataclass
from typing import Any, Optional

from ...core.db import get_db
from ...core.models import TradingOrder, Transaction
from ...core.types import AssetClass, OrderStatus, OrderType
from ...logger import logger

# Order types that open or increase a position and therefore need a protective stop.
# Anything else (OCO / OTO / TRAILING_STOP) is itself a protective construct.
_EXPOSING_ORDER_TYPES = frozenset({
    OrderType.MARKET,
    OrderType.BUY_LIMIT, OrderType.SELL_LIMIT,
    OrderType.BUY_STOP, OrderType.SELL_STOP,
    OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT,
})

# Types on which ``stop_price`` means "trigger me HERE to get in", not "protect me here".
_ENTRY_TRIGGER_TYPES = frozenset({
    OrderType.BUY_STOP, OrderType.SELL_STOP,
    OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT,
    OrderType.TRAILING_STOP,
})

# Types a live protective STOP leg can take.
_PROTECTIVE_STOP_TYPES = frozenset({
    OrderType.BUY_STOP, OrderType.SELL_STOP,
    OrderType.BUY_STOP_LIMIT, OrderType.SELL_STOP_LIMIT,
    OrderType.TRAILING_STOP,
    OrderType.OCO,
})

# Types a live TAKE-PROFIT leg can take.
_PROTECTIVE_TP_TYPES = frozenset({
    OrderType.BUY_LIMIT, OrderType.SELL_LIMIT,
    OrderType.OCO,
})


@dataclass(frozen=True)
class ProtectiveLegDecision:
    """What the caller should do with one order.

    ``allow=False`` means DO NOT SUBMIT and show ``reason`` to the user. ``allow=True``
    with ``sl_price=None`` means "submit as-is, this order opens nothing that needs a
    stop" -- it is NOT the same as "no stop was found".
    """
    allow: bool
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    reason: str = ""


def _price(value: Any) -> Optional[float]:
    """A usable price, or None.

    ``0.0`` is the value a blank ``ui.number`` yields and the value an unset column
    sometimes carries. A stop at zero can never trigger, so treating it as protection
    would be the original defect with one extra step. Same for negatives and NaN.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def _describe(order: TradingOrder) -> str:
    side = getattr(order.side, 'value', order.side)
    order_type = getattr(order.order_type, 'value', order.order_type)
    label = f"order {order.id}" if getattr(order, 'id', None) else "this order"
    return f"{label} ({order.symbol} {side} {order.quantity} {order_type})"


def _sibling_legs(order: TradingOrder):
    """Orders that could already be protecting the position ``order`` belongs to.

    Matched either by shared transaction or by hanging off this order directly -- an
    entry's bracket legs are staged with ``depends_on_order`` pointing at the entry
    before the entry itself has been submitted.
    """
    from sqlmodel import or_, select

    transaction_id = getattr(order, 'transaction_id', None)
    order_id = getattr(order, 'id', None)
    if transaction_id is None and order_id is None:
        return []

    clauses = []
    if transaction_id is not None:
        clauses.append(TradingOrder.transaction_id == transaction_id)
    if order_id is not None:
        clauses.append(TradingOrder.depends_on_order == order_id)

    try:
        with get_db() as session:
            rows = session.exec(
                select(TradingOrder).where(or_(*clauses))
            ).all()
        return [row for row in rows if row.id != order_id]
    except Exception as e:  # noqa: BLE001 — a lookup failure must not be read as "protected"
        logger.error(f"Could not load protective legs for {_describe(order)}: {e}",
                     exc_info=True)
        return []


def _live_legs(order: TradingOrder):
    active = OrderStatus.get_active_statuses()
    return [leg for leg in _sibling_legs(order) if leg.status in active]


def resolve_protective_legs(order: TradingOrder,
                            explicit_sl_price: Any = None,
                            explicit_tp_price: Any = None) -> ProtectiveLegDecision:
    """Decide whether ``order`` may be submitted, and with which protective prices.

    Args:
        order: the order about to be submitted. May be transient (no ``id``, no
            ``transaction_id``) -- the Place Order dialog gates before it persists
            anything, so a refusal leaves no orphan row behind.
        explicit_sl_price: a stop the user typed. Wins over everything derived.
        explicit_tp_price: a take-profit the user typed.

    Returns:
        ProtectiveLegDecision: never raises; a lookup failure is reported as "not
        protected" rather than swallowed as "protected".
    """
    # --- exemptions: orders that open no new exposure -----------------------------
    if getattr(order, 'asset_class', AssetClass.EQUITY) == AssetClass.OPTION:
        return ProtectiveLegDecision(
            True, None, None,
            f"{_describe(order)} is an option order — this platform manages option "
            f"exits through the option lifecycle service, not through stop legs")

    if getattr(order, 'depends_on_order', None) or getattr(order, 'parent_order_id', None):
        return ProtectiveLegDecision(
            True, None, None,
            f"{_describe(order)} is itself a protective leg")

    if order.order_type not in _EXPOSING_ORDER_TYPES:
        return ProtectiveLegDecision(
            True, None, None,
            f"{_describe(order)} is not a position-opening order type")

    transaction = None
    if getattr(order, 'transaction_id', None):
        with get_db() as session:
            transaction = session.get(Transaction, order.transaction_id)

    if transaction is not None and transaction.side != order.side:
        return ProtectiveLegDecision(
            True, None, None,
            f"{_describe(order)} closes transaction {transaction.id}")

    # --- what protection already exists -------------------------------------------
    live_legs = _live_legs(order)
    stop_leg = next((leg for leg in live_legs
                     if leg.order_type in _PROTECTIVE_STOP_TYPES and _price(leg.stop_price)),
                    None)
    tp_leg = next((leg for leg in live_legs
                   if leg.order_type in _PROTECTIVE_TP_TYPES and _price(leg.limit_price)),
                  None)

    if stop_leg is not None:
        # Already covered. Passing a stop as well would ask the broker for a SECOND one.
        return ProtectiveLegDecision(
            True, None, None,
            f"{_describe(order)} is already covered by protective leg {stop_leg.id}")

    # --- derive the stop ----------------------------------------------------------
    sl_price = _price(explicit_sl_price)
    source = "the price you entered"
    if sl_price is None and order.order_type not in _ENTRY_TRIGGER_TYPES:
        # order.stop_price is the risk manager's safeguard SL -- but ONLY on types where
        # stop_price is not the entry trigger. See module docstring.
        sl_price = _price(order.stop_price)
        source = "the order's stop price"
    if sl_price is None and transaction is not None:
        sl_price = _price(transaction.stop_loss)
        source = f"transaction {transaction.id}'s stop loss"

    if sl_price is None:
        return ProtectiveLegDecision(
            False, None, None,
            f"Refusing to submit {_describe(order)}: no protective stop-loss could be "
            f"found for it, and opening an unprotected position is not allowed. "
            f"Set a stop price on the order, or run Risk Management so the safeguard "
            f"stop is computed, then submit again.")

    # --- a take-profit rides along only when one is known and none is live ---------
    tp_price = _price(explicit_tp_price)
    if tp_price is None and tp_leg is None and transaction is not None:
        tp_price = _price(transaction.take_profit)

    return ProtectiveLegDecision(
        True, sl_price, tp_price,
        f"Submitting {_describe(order)} with a protective stop at {sl_price} "
        f"(from {source})")
