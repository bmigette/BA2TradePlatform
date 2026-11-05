"""WHICH order opened a position: the one read both runtimes' exit passes hand every condition.

THE QUESTION. The OPEN_POSITIONS pass evaluates one ruleset per SYMBOL and gives every condition
and action one ``existing_order``: the entry order of the position being managed. A lot hangs off
that row -- ``DaysOpenedCondition`` and the P&L conditions read it, ``CloseAction`` closes ITS
transaction, the option exits read their entry facts off it (``max_loss_per_contract``,
``earnings_event_date``, ``entry_cross``), and ``EntryTagCondition`` reads the style-group
MEMBER TAG off it. If the two runtimes pick different rows, a style member's exits fire in the
backtest and never fire live (or the reverse), so there is exactly ONE definition, here, and
live ``TradeManager`` and the backtest ``daily_engine`` both call it (plan 2026-09-25
option-stage2-build, Task 8 review I1).

THE POSITION: the OLDEST OPENED transaction (:func:`oldest_open_transaction`).

* OPENED only. A WAITING transaction has not opened -- nothing is held yet, so there is nothing
  for an exit to act on, and its entry order is not filled. Old live code included WAITING
  transactions here, so an older WAITING entry (a second lot still working beside a held one)
  was picked, no FILLED order was found on it, and every order-reading exit on the symbol
  evaluated False -- the held position's own exits included. The backtest already read OPENED
  only.
* Ordered by the UTC instant of ``open_date`` (``created_at`` when it has none; neither sorts
  last), then by ``id``. ``as_utc_key`` normalises naive and aware values: a raw comparison
  raises ``TypeError`` when one book holds both shapes (SQLite hands back either).

THE ENTRY ORDER of a transaction (:func:`entry_order_for_transaction`): its FIRST-WRITTEN ROOT
order -- ``depends_on_order IS NULL`` and ``parent_order_id IS NULL`` -- by the UTC instant of
``created_at`` (none sorts first), then ``id``. Any status, any side.

* That is the row every entry path writes its facts onto: ``_OptionEntryAction`` stamps the
  member tag and the entry facts on the order ``submit_option_order`` returns (a single leg's
  only row, or a combo's PARENT); an equity entry's tag rides the entry order it creates; an
  assignment's delivered stock gets a synthetic FILLED root row carrying the inherited tag
  (``assignment_hold.assigned_stock_entry_data``, live and backtest).
* Not the leg children of a combo: they also have no ``depends_on_order`` and carry the
  per-leg contracts, but none of the entry facts. Not TP/SL legs or an assignment's call-away
  close: those carry ``depends_on_order``.
* Not "the oldest FILLED order on the transaction's side" (old live): on every normal row that
  is the same order, because the entry is filled and its side is the transaction's (the
  transaction is created FROM that order, ``_create_transaction_for_order``). Where they differ
  the root row is the right one: an entry that filled partly and was then cancelled holds the
  position and the tag but is not FILLED, and a combo leg on the net side with an equal
  timestamp is FILLED but carries no tag.

Refusing, not guessing: an OPENED position with NO root order raises
:class:`EntryOrderUnresolved`. Returning None would silently turn every order-reading exit on
the symbol False -- for a style expert, a position no member's exit can ever close.

ba2_common-pure: no ba2_providers / ba2_trade_platform imports.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

from ba2_common.core.types import TransactionStatus
from ba2_common.core.utils import as_utc_key


class EntryOrderUnresolved(RuntimeError):
    """An OPENED transaction has no entry (root) order row, so its exits cannot be evaluated."""


def transaction_age_key(transaction: Any) -> tuple:
    """Sort key: oldest position first (UTC ``open_date``, else ``created_at``; then id)."""
    return (as_utc_key(transaction.open_date or transaction.created_at),
            transaction.id or 0)


def entry_order_sort_key(order: Any) -> tuple:
    """Sort key: first-written order first (UTC ``created_at``, unknown first; then id)."""
    return (as_utc_key(order.created_at, default=datetime.min), order.id or 0)


def oldest_open_transaction(transactions: Iterable[Any]) -> Optional[Any]:
    """The OPENED transaction whose position the exits manage, or None when none is OPENED."""
    opened = [t for t in transactions if t.status == TransactionStatus.OPENED]
    if not opened:
        return None
    return min(opened, key=transaction_age_key)


def positions_to_manage(transactions: Iterable[Any], account: Any) -> list:
    """THE BOOK an OPEN_POSITIONS pass evaluates a symbol against (the evaluator's
    ``existing_transactions``): the OPENED transactions, in the order given, minus any with a
    closing order already working (``account.has_pending_closing_order``, so a close in flight
    is not submitted again every cycle). An empty result means the symbol is NOT evaluated.

    Shared by live ``TradeManager.process_open_positions_recommendations`` and the backtest
    ``daily_engine._held_transactions`` (plan 2026-09-25 option-stage2-build, Task 8 review,
    asymmetry (a)). Live used to hand the evaluator OPENED **and** WAITING transactions, so it
    evaluated WAITING-only symbols the backtest never evaluates -- and in the filled-but-not-
    yet-promoted window (up to one account refresh) that meant ``existing_order=None``: a close
    rule whose leaves need no entry order then took ``CloseAction``'s legacy broker-position
    branch and closed the broker position without linking the close to the transaction, which
    could afterwards be promoted to OPENED while the broker was flat. WAITING transactions are
    still counted by the ENTRY gate (``trade_cycle.open_or_waiting_position``); they are simply
    not positions to exit.
    """
    return [t for t in transactions
            if t.status == TransactionStatus.OPENED
            and not account.has_pending_closing_order(t.id)]


def entry_order_for_transaction(transaction_id: Any, *, session=None) -> Optional[Any]:
    """The transaction's entry order (its first-written root order), or None if it has none.

    Reads through ``trade_store.orders_where``: the backtest's in-memory store when it is
    active, else SQLite -- through ``session`` when one is given, so a live caller keeps its
    own session's view (and its identity map) exactly as before.
    """
    from ba2_common.core.trade_store import orders_where

    if transaction_id is None:
        return None
    roots = [o for o in orders_where(transaction_id=transaction_id, depends_on_order=None,
                                     session=session)
             if o.parent_order_id is None]
    if not roots:
        return None
    return min(roots, key=entry_order_sort_key)


def oldest_entry_order(transactions: Iterable[Any], *, session=None) -> Optional[Any]:
    """The ``existing_order`` an OPEN_POSITIONS pass evaluates a symbol's exits against.

    ``transactions``: the expert's transactions on the symbol, as the caller gathered them
    (either runtime may include WAITING ones; only OPENED ones are positions). Returns None
    when none is OPENED. Raises :class:`EntryOrderUnresolved` when the oldest OPENED one has no
    entry order row.
    """
    transaction = oldest_open_transaction(transactions)
    if transaction is None:
        return None
    order = entry_order_for_transaction(transaction.id, session=session)
    if order is None:
        raise EntryOrderUnresolved(
            f"ENTRY_ORDER_UNRESOLVED: OPENED transaction {transaction.id} "
            f"({transaction.symbol}) has no entry order row (an order with neither "
            f"depends_on_order nor parent_order_id); its exits cannot be evaluated -- every "
            f"P&L / days-opened / entry_tag condition would read False and a style member's "
            f"exit could never close it")
    return order
