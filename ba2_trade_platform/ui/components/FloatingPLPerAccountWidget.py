"""
Floating P/L Widget Base and Per-Account Widget
Displays unrealized profit/loss for open positions grouped by account.
"""
from nicegui import ui
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Sequence, Tuple
from sqlmodel import select, Session
from ...logger import logger
from ...core.db import get_db
from ...core.models import Transaction, AccountDefinition, TradingOrder
from ...core.types import TransactionStatus, OrderStatus, OrderDirection, OrderType
from ...core.utils import get_account_instance_from_id
from ..account_filter_context import get_selected_account_id, get_expert_ids_for_account
from .account_scope import scope_transactions_to_account


# ---------------------------------------------------------------------------
# WHAT THE CARD IS ALLOWED TO SAY
#
# Three states, never two. The bug this vocabulary exists to prevent is an
# account that is simply ABSENT from the card, which is not a statement about
# anything -- 'TastyTrade' had no row at all while holding real money, and the
# 'Total P/L' line presented one account's figure as the total of two.
#
#   measured        '$52.43'        positions were read and priced
#   measured ZERO   '$0.00'         the broker answered [] -- genuinely flat
#   unknown         'P/L unknown'   the broker answered None / raised / could
#                                   not be instantiated
#
# '$0.00' and 'P/L unknown' are opposite claims and each is wrong in the other's
# place: a flat account displayed as unknown hides a real, useful measurement,
# and an unreadable account displayed as $0.00 invents one.
# ---------------------------------------------------------------------------

#: The P/L cell when nothing could be measured. Deliberately not '$0.00', and
#: deliberately not blank -- a blank cell is read as "nothing here".
UNKNOWN_PL_TEXT = 'P/L unknown'

#: The balance cell when ``get_balance()`` would not answer. Same rule.
UNKNOWN_BALANCE_TEXT = 'Bal: unknown'

#: Appended to a figure that is real but INCOMPLETE -- a total missing an
#: account, or a row missing a position the broker did not quote. In the text
#: rather than a tooltip: a marker that needs a hover is one the reader of a
#: wrong number never sees.
PARTIAL_SUFFIX = ' (partial)'

#: Empty state for a card that lists EXPERTS and found none.
NO_ROWS_TEXT = 'No open positions'

#: Empty state for a card that lists ACCOUNTS and found none. Distinct from the
#: above on purpose: with the fix below, "no rows" on the per-account card can no
#: longer mean "no positions" -- every account in scope gets a row whatever its
#: book looks like -- so the only way to get here is to have no accounts at all.
NO_ACCOUNTS_TEXT = 'No accounts configured'

PL_EXCLUDED_NOTE_FMT = '⚠️ Total excludes {names}: floating P/L could not be measured'
BALANCE_EXCLUDED_NOTE_FMT = '⚠️ Total balance excludes {names}: balance could not be read'
UNPRICED_NOTE_FMT = ('⚠️ {name}: no broker price for {symbols} — that position is '
                     'missing from the row')


@dataclass
class PLRow:
    """One line of the card: a name, and what we could or could not measure for it.

    ``pl is None`` means UNMEASURABLE, and is never produced by a measurement that
    happened to come out at zero -- a flat account is ``pl=0.0``. Same contract for
    ``balance``.

    ``unpriced`` names the symbols this row HOLDS but the broker did not quote.
    Those legs are missing from ``pl``, so ``pl`` is real but incomplete: the row
    is rendered with :data:`PARTIAL_SUFFIX` rather than silently understated,
    which is what dropping them did.
    """
    name: str
    pl: Optional[float]
    balance: Optional[float] = None
    unpriced: Tuple[str, ...] = ()


def combine_measurements(
        reads: Sequence[Tuple[str, Optional[float]]]
) -> Tuple[Optional[float], List[str]]:
    """Total the legs that could be read, and NAME the ones that could not. Pure.

    THE RULE: an unreadable leg never silently disappears into the sum. Either the
    reader is told the total is partial and which account is missing from it, or --
    when nothing at all was readable -- there is no total to show.

    ``layout.combine_account_values`` -- the header badge's total -- now says the
    same thing by the same rule, and for the same reason: both surfaces show the
    accounts they are totalling, so "which one is missing" is on screen and
    discarding the readable part would throw away information the reader can see
    is real. Two totals over the same accounts must not be able to disagree.

    Nothing is rounded here; the legs are summed at full precision and formatted
    once, by the caller.

    Returns:
        ``(total, unreadable_names)``. ``total`` is ``None`` iff NOTHING was
        readable (which includes an empty input: no total, as opposed to a total
        of nothing). ``unreadable_names`` is what the total excludes, in input
        order, and is non-empty exactly when something was dropped.
    """
    unreadable = [name for name, value in reads if value is None]
    readable = [value for _, value in reads if value is not None]
    if not readable:
        return None, unreadable
    return float(sum(readable)), unreadable


class _FloatingPLWidgetBase:
    """Base class for floating P/L widgets.

    Subclasses must define:
        _title: str             - card header text
        _scope_query()          - narrow the open-transaction query to this widget's subject
        _get_extra_filters()    - additional SQLAlchemy where-clauses for the transaction query
        _group_transactions()   - group raw transactions into {account_id: [(trans, display_name), ...]}
        _seed_rows()            - the rows that must exist even with NO transactions

    ``_scope_query`` is the seam that keeps the two subclasses honest. 'Per Expert'
    is genuinely per-expert and scopes by expert id; 'Per Account' is account-level
    and scopes by account. The base used to hard-code the EXPERT filter for both,
    so the per-account widget went blank for an account with no experts and hid
    hand-placed (``expert_id IS NULL``) trades on the accounts that do have them.

    ``_seed_rows`` is the seam for the SECOND half of that bug. Fixing the query
    was not enough: the rows were still built by iterating the transactions that
    came back, so an account with nothing open produced no group, no row, and no
    statement about itself. An account-level card must list the accounts. An
    expert-level card must not -- 'Manual' is not an expert -- so the base seeds
    nothing by default and only the per-account subclass overrides it.
    """

    _title: str = ""
    # When True the widget also fetches and displays each account's broker balance
    # (only meaningful for the per-account view; per-expert balance is not a broker concept).
    _show_balance: bool = False
    # What to draw when there is not a single row. See NO_ACCOUNTS_TEXT.
    _empty_text: str = NO_ROWS_TEXT

    def __init__(self):
        """Initialize and render the widget."""
        self.render()

    def render(self):
        """Render the widget with loading state."""
        with ui.card().classes('p-4'):
            ui.label(self._title).classes('text-h6 mb-4')

            # Create loading placeholder
            loading_label = ui.label('🔄 Calculating floating P/L...').classes('text-sm text-gray-500')
            content_container = ui.column().classes('w-full')

            # Load data asynchronously (non-blocking)
            asyncio.create_task(self._load_data_async(loading_label, content_container))

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def _get_extra_filters(self) -> list:
        """Return additional where-clause expressions to apply to the transaction query.

        Default: no extra filters.
        """
        return []

    def _scope_query(self, query, selected_account_id: Optional[int],
                     account_expert_ids: Optional[List[int]]):
        """Narrow *query* to the transactions this widget is about.

        Args:
            query: the open/waiting ``Transaction`` select built by the base.
            selected_account_id: the header's account filter, ``None`` for "All".
            account_expert_ids: that account's expert ids -- ``None`` for "All",
                and ``[]`` for an account that HAS NO EXPERTS (which is not the
                same thing as an account with no data).

        Returns:
            The narrowed query, or ``None`` to mean "nothing can match" so the
            caller can skip the round trip entirely.
        """
        raise NotImplementedError

    def _seed_rows(self, selected_account_id: Optional[int],
                   session: Session) -> Dict[int, str]:
        """``{account_id: display_name}`` that must appear even with no transactions.

        Default: nothing. A widget whose rows ARE its subjects (the per-account
        card) overrides this so a subject with nothing open still gets measured
        and drawn, instead of vanishing.

        Deliberately NOT wrapped in a try/except: if the accounts cannot be listed
        we do not know what to draw, and the caller's error state ('❌ Error
        calculating P/L') is the honest answer. Swallowing it here would render
        :data:`NO_ACCOUNTS_TEXT` -- a confident claim that there are none.
        """
        return {}

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        """Group *transactions* by account_id for bulk price fetching.

        Must return ``{account_id: [(transaction, display_name), ...]}``.
        *display_name* is the label shown in the UI (e.g. account name or expert alias).

        Subclasses override this to implement their own grouping logic.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Core P/L calculation (shared)
    # ------------------------------------------------------------------

    def _calculate_pl_sync(
        self,
        selected_account_id: Optional[int],
        account_expert_ids: Optional[List[int]],
    ) -> List[PLRow]:
        """Synchronous P/L calculation (runs in thread pool to avoid blocking).

        Uses bulk price fetching per broker account.

        Args:
            selected_account_id: The selected account ID from filter, or None for all.
            account_expert_ids: List of expert IDs belonging to selected account, or None for all.

        Returns:
            One :class:`PLRow` per display name, in no particular order.
        """
        session = get_db()
        try:
            # Build query for open transactions
            query = (
                select(Transaction)
                .where(Transaction.status.in_([TransactionStatus.OPENED, TransactionStatus.WAITING]))
            )

            # Subclass-specific filters (e.g. expert_id IS NOT NULL)
            for clause in self._get_extra_filters():
                query = query.where(clause)

            # Apply the header's account filter the way THIS widget means it.
            query = self._scope_query(query, selected_account_id, account_expert_ids)

            transactions = session.exec(query).all() if query is not None else []

            # Group transactions by account for bulk price fetching
            account_transactions = self._group_transactions(transactions, session)

            # ...then add every account that MUST be shown regardless. setdefault,
            # not assignment: an account that does have transactions keeps them, or
            # it would be measured as flat and reported at a confident $0.00.
            seeds = self._seed_rows(selected_account_id, session)
            for account_id in seeds:
                account_transactions.setdefault(account_id, [])

            rows: List[PLRow] = []
            for account_id, trans_list in account_transactions.items():
                rows.extend(self._rows_for_account(
                    account_id, trans_list, seeds.get(account_id), session))
            return rows

        finally:
            session.close()

    def _rows_for_account(
        self, account_id: int, trans_list: List[Tuple[Transaction, str]],
        seed_name: Optional[str], session: Session,
    ) -> List[PLRow]:
        """Measure one broker account, and return the row(s) it is responsible for.

        Every exit from here produces a row for every name. There is no path that
        returns nothing: "we could not read this account" is a state the card
        displays, not a reason to leave it out.
        """
        # Ordered, de-duplicated: a group can carry several experts, and dict keys
        # preserve insertion order so the card is stable between renders.
        names = list(dict.fromkeys(name for _, name in trans_list))
        if not names and seed_name is not None:
            names = [seed_name]
        if not names:
            return []

        account = get_account_instance_from_id(account_id, session=session)
        if account is None:
            # Named explicitly rather than left to blow up on None.get_positions():
            # both end at the same 'P/L unknown', but the AttributeError sends
            # whoever reads the log hunting a broker outage that never happened.
            logger.error(f"Could not build an account instance for account {account_id}; "
                         f"its floating P/L is unknown, not zero")
            return [PLRow(name=name, pl=None) for name in names]

        balance: Optional[float] = None
        if self._show_balance:
            try:
                bal = account.get_balance()
                # ``is not None``, never truthiness: an account really worth $0.00
                # must print $0.00, not 'unknown'.
                balance = float(bal) if bal is not None else None
                if bal is None:
                    logger.warning(f"Balance unavailable for account {account_id}; "
                                   f"showing it as unknown rather than as zero")
            except Exception as e:
                logger.error(f"Could not fetch balance for account {account_id}: {e}",
                             exc_info=True)
                balance = None

        try:
            broker_positions = account.get_positions()
        except Exception as e:
            logger.error(f"get_positions() raised for account {account_id}: {e}",
                         exc_info=True)
            return [PLRow(name=name, pl=None, balance=balance) for name in names]

        # TRI-STATE (see ReadOnlyAccountInterface.get_positions): None is a FETCH
        # FAILURE, [] is a genuinely flat account. The old ``if broker_positions:``
        # folded them together and then dropped the row entirely, so a broker outage
        # and an empty book were both reported as "this account does not exist".
        if broker_positions is None:
            logger.error(f"get_positions() failed for account {account_id}; its "
                         f"floating P/L is unknown, not zero")
            return [PLRow(name=name, pl=None, balance=balance) for name in names]

        prices: Dict[str, float] = {}
        for pos in broker_positions:
            pos_dict = pos if isinstance(pos, dict) else dict(pos)
            symbol = pos_dict.get('symbol')
            current_price = pos_dict.get('current_price')
            # An unpriced position is left OUT of the map so it surfaces as
            # unpriced below, rather than being coerced into a zero price.
            if symbol is None or current_price is None:
                continue
            prices[symbol] = float(current_price)

        pl_by_name: Dict[str, float] = {name: 0.0 for name in names}
        unpriced_by_name: Dict[str, List[str]] = {name: [] for name in names}

        for trans, display_name in trans_list:
            try:
                measured = self._transaction_pl(trans, prices, session)
            except Exception as e:
                logger.error(f"Error calculating P/L for transaction {trans.id}: {e}",
                             exc_info=True)
                if trans.symbol not in unpriced_by_name[display_name]:
                    unpriced_by_name[display_name].append(trans.symbol)
                continue

            if measured is None:
                # HELD but not quoted. Not zero: the leg is missing, and the row
                # says so instead of quietly understating the account.
                if trans.symbol not in unpriced_by_name[display_name]:
                    unpriced_by_name[display_name].append(trans.symbol)
                continue
            pl_by_name[display_name] += measured

        return [PLRow(name=name, pl=pl_by_name[name], balance=balance,
                      unpriced=tuple(unpriced_by_name[name]))
                for name in names]

    def _transaction_pl(self, trans: Transaction, prices: Dict[str, float],
                        session: Session) -> Optional[float]:
        """This transaction's floating P/L, or ``None`` if it HOLDS but is unquoted.

        Returns ``0.0`` -- a measurement -- for a transaction that holds nothing:
        a WAITING order that has not filled has no position, so it contributes
        nothing and there is nothing missing. That case is checked BEFORE the price
        lookup on purpose; asking for a quote first (what this used to do) made
        every resting order look like a position the broker had failed to quote.
        """
        all_orders = session.exec(
            select(TradingOrder)
            .where(TradingOrder.transaction_id == trans.id)
        ).all()

        # Get all FILLED orders (any type that affects position)
        # Exclude: OCO, OTO orders (they are TP/SL brackets, not position-affecting until triggered)
        filled_orders = [
            o for o in all_orders
            if o.status in OrderStatus.get_executed_statuses()
            and o.order_type not in [OrderType.OCO, OrderType.OTO]
            and o.filled_qty and o.filled_qty > 0
        ]

        # Calculate net position and weighted average cost
        total_buy_cost = 0.0
        total_buy_qty = 0.0
        total_sell_cost = 0.0
        total_sell_qty = 0.0

        for order in filled_orders:
            if not order.open_price or not order.filled_qty:
                continue

            if order.side == OrderDirection.BUY:
                total_buy_cost += order.filled_qty * order.open_price
                total_buy_qty += order.filled_qty
            elif order.side == OrderDirection.SELL:
                total_sell_cost += order.filled_qty * order.open_price
                total_sell_qty += order.filled_qty

        # Net filled quantity = buys - sells
        net_filled_qty = total_buy_qty - total_sell_qty

        if abs(net_filled_qty) < 0.01:  # No net position: nothing held, nothing missing
            return 0.0

        # Get position side from transaction.side field
        # BUY = LONG position, SELL = SHORT position
        position_direction = trans.side

        # Calculate weighted average entry price based on position direction
        if position_direction == OrderDirection.BUY:
            # Long position: entry price is avg BUY price
            if total_buy_qty < 0.01:
                return 0.0
            avg_price = total_buy_cost / total_buy_qty
        else:
            # Short position: entry price is avg SELL price
            if total_sell_qty < 0.01:
                return 0.0
            avg_price = total_sell_cost / total_sell_qty

        current_price = prices.get(trans.symbol)
        if current_price is None:
            return None

        # Debug: Log when net filled qty differs from transaction qty
        if abs(net_filled_qty - abs(trans.quantity)) > 0.01:
            logger.debug(
                f"Transaction {trans.id}: net_filled_qty={net_filled_qty:.2f} "
                f"(buys={total_buy_qty:.2f} - sells={total_sell_qty:.2f}), "
                f"transaction.quantity={trans.quantity}"
            )

        # Use transaction.quantity as source of truth for current position
        # Calculate P/L: (current_price - avg_price) * position_quantity
        pl = (current_price - avg_price) * trans.quantity
        if position_direction == OrderDirection.SELL:
            pl = -pl  # Invert for short positions
        return pl

    # ------------------------------------------------------------------
    # Async UI rendering (shared)
    # ------------------------------------------------------------------

    async def _load_data_async(self, loading_label, content_container):
        """Calculate and display floating P/L (async wrapper for thread pool execution)."""
        try:
            # Capture account filter values BEFORE running in thread pool
            # (app.storage.user is request-context bound and not available in thread pool)
            selected_account_id = get_selected_account_id()
            account_expert_ids = get_expert_ids_for_account(selected_account_id)

            # Run database queries in thread pool to avoid blocking UI
            loop = asyncio.get_event_loop()
            rows = await loop.run_in_executor(
                None,
                lambda: self._calculate_pl_sync(selected_account_id, account_expert_ids)
            )

            # Clear loading message
            try:
                loading_label.delete()
            except RuntimeError:
                return

            # Display results
            try:
                with content_container:
                    self._draw(rows)
            except RuntimeError:
                return

        except Exception as e:
            logger.error(f"Error loading floating P/L ({self._title}): {e}", exc_info=True)
            try:
                loading_label.delete()
            except RuntimeError:
                return
            try:
                with content_container:
                    ui.label('❌ Error calculating P/L').classes('text-sm text-red-600')
            except RuntimeError:
                pass

    def _draw(self, rows: List[PLRow]) -> None:
        """Draw the rows, the total, and the notes that keep the total honest."""
        if not rows:
            ui.label(self._empty_text).classes('text-sm text-gray-500')
            return

        # Measured rows first, biggest P/L down; unknowns last, alphabetically.
        # An unknown has no size, so it cannot be sorted among the numbers.
        ordered = sorted(rows, key=lambda r: (r.pl is None,
                                              -(r.pl if r.pl is not None else 0.0),
                                              r.name))

        for row in ordered:
            with ui.row().classes('w-full justify-between items-center mb-1'):
                ui.label(row.name).classes('text-sm truncate max-w-[150px]')
                with ui.row().classes('items-center gap-3'):
                    if self._show_balance:
                        ui.label(_balance_text(row.balance)).classes('text-xs text-gray-500')
                    _pl_label(row.pl, partial=bool(row.unpriced), size='text-sm')

        ui.separator().classes('my-2')

        pl_total, pl_missing = combine_measurements([(r.name, r.pl) for r in rows])
        understated = [r.name for r in rows if r.unpriced]
        bal_total, bal_missing = combine_measurements([(r.name, r.balance) for r in rows])

        with ui.row().classes('w-full justify-between items-center'):
            ui.label('Total P/L:').classes('text-sm font-bold')
            with ui.row().classes('items-center gap-3'):
                if self._show_balance:
                    ui.label(_balance_text(bal_total, partial=bool(bal_missing))) \
                        .classes('text-xs text-gray-500 font-bold')
                _pl_label(pl_total, partial=bool(pl_missing or understated), size='text-lg')

        # WHY THE NOTES. '(partial)' says the number is incomplete; only these say
        # what is missing from it, which is the difference between a caveat the
        # reader can act on and one they learn to ignore.
        for row in ordered:
            if row.unpriced:
                ui.label(UNPRICED_NOTE_FMT.format(name=row.name,
                                                  symbols=', '.join(row.unpriced))) \
                    .classes('text-xs text-orange-600')
        if pl_missing:
            ui.label(PL_EXCLUDED_NOTE_FMT.format(names=', '.join(pl_missing))) \
                .classes('text-xs text-orange-600')
        if self._show_balance and bal_missing:
            ui.label(BALANCE_EXCLUDED_NOTE_FMT.format(names=', '.join(bal_missing))) \
                .classes('text-xs text-orange-600')


def _balance_text(value: Optional[float], *, partial: bool = False) -> str:
    """The 'Bal:' cell. ``None`` is unknown; ``0.0`` is an empty account."""
    if value is None:
        return UNKNOWN_BALANCE_TEXT
    return f'Bal: ${value:,.2f}' + (PARTIAL_SUFFIX if partial else '')


def _pl_label(value: Optional[float], *, partial: bool, size: str) -> None:
    """Draw a P/L figure, or say it is unknown. Never both, never neither."""
    if value is None:
        ui.label(UNKNOWN_PL_TEXT).classes(f'{size} font-bold text-gray-500')
        return
    colour = 'text-green-600' if value >= 0 else 'text-red-600'
    text = f'${value:,.2f}' + (PARTIAL_SUFFIX if partial else '')
    ui.label(text).classes(f'{size} font-bold {colour}')


class FloatingPLPerAccountWidget(_FloatingPLWidgetBase):
    """Widget component showing floating profit/loss per account."""

    _title = '📊 Floating P/L Per Account'
    _show_balance = True
    _empty_text = NO_ACCOUNTS_TEXT

    def _scope_query(self, query, selected_account_id: Optional[int],
                     account_expert_ids: Optional[List[int]]):
        """Scope by ACCOUNT. ``account_expert_ids`` is deliberately ignored.

        Every open position in the account counts towards the account's floating
        P/L, whoever opened it -- an expert, the Smart Risk Manager, or the user by
        hand. There is no "nothing can match" answer here: an account with no
        transactions still gets a row, seeded by ``_seed_rows``.
        """
        return scope_transactions_to_account(query, selected_account_id)

    def _seed_rows(self, selected_account_id: Optional[int],
                   session: Session) -> Dict[int, str]:
        """Every account the header is showing -- the card's rows ARE the accounts.

        This is the list the card is about, so it comes from ``AccountDefinition``
        and from nothing else. Deriving it from the open transactions (what the
        card used to do) drops any account that is flat; deriving it from the
        experts drops any account that is manual. TastyTrade is both, which is why
        it had no row at all -- and 'no row' is not one of the three things this
        card is allowed to say about an account.

        Honours the dropdown exactly as ``scope_transactions_to_account`` does:
        ``None`` is "All" and is the only value that widens the list.
        """
        query = select(AccountDefinition)
        if selected_account_id is not None:
            query = query.where(AccountDefinition.id == selected_account_id)
        return {account.id: account.name for account in session.exec(query).all()}

    def _group_transactions(
        self, transactions: List[Transaction], session: Session
    ) -> Dict[int, List[Tuple[Transaction, str]]]:
        # No account filter here: ``_scope_query`` already applied it IN SQL, in one
        # place. Re-deriving it here also meant calling get_selected_account_id()
        # from inside the executor thread, where app.storage.user does not exist --
        # the exact thing _load_data_async captures the filter up-front to avoid.
        account_transactions: Dict[int, List[Tuple[Transaction, str]]] = {}

        for trans in transactions:
            try:
                first_order = session.exec(
                    select(TradingOrder)
                    .where(TradingOrder.transaction_id == trans.id)
                    .limit(1)
                ).first()

                if not first_order or not first_order.account_id:
                    continue

                account_def = session.get(AccountDefinition, first_order.account_id)
                if not account_def:
                    continue

                account_id = first_order.account_id
                if account_id not in account_transactions:
                    account_transactions[account_id] = []
                account_transactions[account_id].append((trans, account_def.name))

            except Exception as e:
                logger.error(f"Error grouping transaction {trans.id}: {e}", exc_info=True)
                continue

        return account_transactions
