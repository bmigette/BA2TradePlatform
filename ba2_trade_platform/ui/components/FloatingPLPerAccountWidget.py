"""
Floating P/L Widget Base and Per-Account Widget
Displays unrealized profit/loss for open positions grouped by account.
"""
from nicegui import ui
import asyncio
from typing import Dict, Optional, List, Sequence, Tuple
from ...logger import logger
from ...services.floating_pl import FloatingPLCalculator, PerAccountPL, PLRow, combine_measurements
from ..account_filter_context import get_selected_account_id, get_expert_ids_for_account


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

#: The BP cell when the BROKER's remaining buying power could not be read -- the
#: snapshot call failed, or the broker publishes none. Same rule again: not
#: '$0.00' (which would read as 'this account may buy nothing') and not blank. The
#: word, not the header's dash: this cell is LABELLED, and the card already says
#: 'Bal: unknown' for exactly this state one cell to the left -- the dash is for
#: the header's unlabelled slot, where there is no room to spell it out.
UNKNOWN_BP_TEXT = 'BP: unknown'

#: Hover text on the BP cell. The tradable balance is a DIFFERENT number from the
#: cell's: the cell is what the BROKER has left, this is the ceiling this
#: platform's own sizing obeys. The two swapped places on 2026-09-08 because the
#: broker's is the one that actually runs out -- an account whose manual allocator
#: already runs at 2x reports a tradable balance equal to its plain balance, and
#: the cell said the account was untouched while ~$8k of it was already deployed.
TRADABLE_TOOLTIP_FMT = 'Tradable (platform ceiling): {bp}'

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
BP_EXCLUDED_NOTE_FMT = '⚠️ Total BP excludes {names}: buying power could not be read'
UNPRICED_NOTE_FMT = ('⚠️ {name}: no broker price for {symbols} — that position is '
                     'missing from the row')



class _FloatingPLWidgetBase(FloatingPLCalculator):
    """Base class for floating P/L widgets: the calculation is ``services.floating_pl.FloatingPLCalculator``;
    this class adds the card around it.

    Subclasses define ``_title`` (card header text) plus the calculator hooks (``_scope_query``,
    ``_get_extra_filters``, ``_group_transactions``, ``_seed_rows``)."""

    _title: str = ""
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
                # The name takes whatever the card has left instead of a fixed 150px.
                #
                # Deployed instance names are long and front-loaded with the parts that do
                # NOT distinguish them -- 'goal2020-mid_ED_S1t...' and 'goal2020-mid_ED_S7t...'
                # truncate to the same string, so the widget could show two rows a reader
                # cannot tell apart. flex-1 + min-w-0 lets the label grow to the full row
                # (min-w-0 is load-bearing: a flex child's default min-width:auto refuses to
                # shrink below its content, which would push the numbers off the card
                # instead of eliding), and the numbers keep their natural width via
                # shrink-0. `truncate` stays as the last resort for a name longer than the
                # card itself -- and the tooltip means that resort never loses information.
                ui.label(row.name).classes('text-sm truncate min-w-0 flex-1').tooltip(row.name)
                with ui.row().classes('items-center gap-3 shrink-0'):
                    if self._show_balance:
                        ui.label(_balance_text(row.balance)).classes('text-xs text-gray-500')
                        ui.label(_bp_text(row.broker_bp)) \
                            .classes('text-xs text-gray-500') \
                            .tooltip(TRADABLE_TOOLTIP_FMT.format(
                                bp=_money_or_dash(row.tradable)))
                    _pl_label(row.pl, partial=bool(row.unpriced), size='text-sm')

        ui.separator().classes('my-2')

        pl_total, pl_missing = combine_measurements([(r.name, r.pl) for r in rows])
        understated = [r.name for r in rows if r.unpriced]
        bal_total, bal_missing = combine_measurements([(r.name, r.balance) for r in rows])
        bp_total, bp_missing = combine_measurements([(r.name, r.broker_bp) for r in rows])

        with ui.row().classes('w-full justify-between items-center'):
            ui.label('Total P/L:').classes('text-sm font-bold')
            with ui.row().classes('items-center gap-3'):
                if self._show_balance:
                    ui.label(_balance_text(bal_total, partial=bool(bal_missing))) \
                        .classes('text-xs text-gray-500 font-bold')
                    ui.label(_bp_text(bp_total, partial=bool(bp_missing))) \
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
        if self._show_balance and bp_missing:
            ui.label(BP_EXCLUDED_NOTE_FMT.format(names=', '.join(bp_missing))) \
                .classes('text-xs text-orange-600')


def _balance_text(value: Optional[float], *, partial: bool = False) -> str:
    """The 'Bal:' cell. ``None`` is unknown; ``0.0`` is an empty account."""
    if value is None:
        return UNKNOWN_BALANCE_TEXT
    return f'Bal: ${value:,.2f}' + (PARTIAL_SUFFIX if partial else '')


def _money_or_dash(value: Optional[float]) -> str:
    """A figure for a TOOLTIP. Same dash the header shows for a figure it lacks."""
    return '—' if value is None else f'${value:,.2f}'


def _bp_text(value: Optional[float], *, partial: bool = False) -> str:
    """The 'BP:' cell: the broker's REMAINING buying power. ``None`` is unknown.

    ``0.0`` is a measurement -- an account with nothing left to buy -- and prints
    as one, for the same reason a $0.00 balance does. The platform's own tradable
    ceiling is the hover, not this.
    """
    if value is None:
        return UNKNOWN_BP_TEXT
    return f'BP: ${value:,.2f}' + (PARTIAL_SUFFIX if partial else '')


def _pl_label(value: Optional[float], *, partial: bool, size: str) -> None:
    """Draw a P/L figure, or say it is unknown. Never both, never neither."""
    if value is None:
        ui.label(UNKNOWN_PL_TEXT).classes(f'{size} font-bold text-gray-500')
        return
    colour = 'text-green-600' if value >= 0 else 'text-red-600'
    text = f'${value:,.2f}' + (PARTIAL_SUFFIX if partial else '')
    ui.label(text).classes(f'{size} font-bold {colour}')


class FloatingPLPerAccountWidget(PerAccountPL, _FloatingPLWidgetBase):
    """Widget component showing floating profit/loss per account."""

    _title = '📊 Floating P/L Per Account'
    _show_balance = True
    _empty_text = NO_ACCOUNTS_TEXT
