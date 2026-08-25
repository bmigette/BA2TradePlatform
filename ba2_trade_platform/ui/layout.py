import asyncio
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .menus import topmenu, sidemenu
from .theme import COLORS
from .account_filter_context import get_accounts_for_filter, get_selected_account_id, set_selected_account_id
# THE SAME helper the Portfolio Allocation page's 'Account value' card uses. Two
# places showing "the account's value" from two different snapshot fields is the
# drift this project keeps paying for, so the field choice (net_liquidation) and
# the money formatting are imported, not restated. Cheap: the module is pure
# (math/re/dataclasses) and costs ~0.1s.
from .utils.portfolio_allocation_view import account_value_from_snapshot, format_account_money
from ..logger import logger

from nicegui import ui, app


# ---------------------------------------------------------------------------
# THE HEADER'S ACCOUNT BALANCE
#
# What used to sit here was ``ui.label('LIVE')`` -- a hardcoded string that
# reflected no state at all: not live-vs-paper, not a connection, not an account.
# It is now the selected account's net liquidating value.
#
# THE CONSTRAINT: this header is built on EVERY page render. A broker call here
# is a broker call per navigation, and a hanging one would hang the whole app.
# So the render path is a DICT LOOKUP and nothing else; the broker is read at
# most hourly, off the event loop, by a timer that cannot fail loudly.
#
# WHY A SECOND CACHE LAYER. ``get_account_snapshot()`` is cached only by
# AlpacaAccount, for ``_ACCOUNT_SNAPSHOT_CACHE_TTL`` = 5 SECONDS (deliberately
# tiny -- it is on the live order path, where ``_validate_position_size_limits``
# needs an equity that is minutes-fresh at worst). TastyTradeAccount caches it
# not at all and the base ``ReadOnlyAccountInterface`` implementation does not
# either. A 5-second window is a broker call on essentially every navigation, so
# a header-owned hourly cache is a genuinely different layer rather than a
# duplicate one. ``fmp_common.fmp_live_cached`` was not reused: it is keyed for
# FMP HTTP payloads and, more importantly, honours ``set_ttl_frozen`` (a backtest
# switch that makes entries never expire) -- exactly the wrong semantics for a
# live money readout.
# ---------------------------------------------------------------------------

#: How often the broker is re-read. Hourly: an account's net liquidating value is
#: a context number in a page header, not a trading input, and the page that
#: actually plans against it (Portfolio Allocation) reads the broker itself.
HEADER_BALANCE_REFRESH_SECONDS = 3600.0

#: When the displayed figure stops being presented as current. TWO refresh
#: windows, not one: a value 61 minutes old simply has not hit its timer yet,
#: which is ordinary scheduling. A value past two windows means a refresh came
#: due and did not land -- a failing broker, or nobody opened a page for hours --
#: and at that point a confidently-displayed number is worse than an honest one.
HEADER_BALANCE_STALE_AFTER_SECONDS = 2 * HEADER_BALANCE_REFRESH_SECONDS

#: How soon after a page is built the first refresh is attempted. Non-zero so the
#: HTML goes out first; small so the number is current within a second of arrival.
HEADER_BALANCE_FIRST_REFRESH_SECONDS = 0.2

#: What the header shows when the value is UNKNOWN. An em dash, and deliberately
#: NOT ``$0.00``: a broker that will not answer is not an empty account. (The
#: inverse error is guarded too -- a genuinely empty account still prints
#: ``$0.00``, because ``None`` and ``0.0`` are never conflated below.)
HEADER_BALANCE_UNAVAILABLE_TEXT = '—'

#: Appended to the money when the figure is past ``HEADER_BALANCE_STALE_AFTER_SECONDS``.
#: In the TEXT, not only in a tooltip: a marker that needs a hover is a marker the
#: reader of a wrong number never sees.
HEADER_BALANCE_STALE_SUFFIX = ' (stale)'

#: Timestamp format for the as-of clause. UTC and explicit about it -- the broker
#: read happened at an instant, and a naive local time would be unreadable next to
#: a broker's own reporting.
HEADER_BALANCE_TIME_FMT = '%Y-%m-%d %H:%M UTC'

HEADER_BALANCE_FRESH_DETAIL_FMT = 'Account value as of {when} (refreshed hourly)'
HEADER_BALANCE_STALE_DETAIL_FMT = (
    'Account value as of {when} — stale: no successful refresh since, '
    'the broker may be unreachable')
HEADER_BALANCE_UNDATED_DETAIL = (
    'Account value of unknown age — stale: it carries no read timestamp')
HEADER_BALANCE_UNREADABLE_DETAIL_FMT = (
    'Account value unknown, not zero — could not read: {names}')
HEADER_BALANCE_NOTHING_TO_READ_DETAIL = 'No account to read a balance from'

HEADER_BALANCE_MARKER = 'header-balance'
HEADER_BALANCE_FIRST_TIMER_MARKER = 'header-balance-first-refresh'
HEADER_BALANCE_TICK_TIMER_MARKER = 'header-balance-tick'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HeaderBalance:
    """What the header's money slot should say, decided. Pure.

    ``available is False`` means the figure is UNKNOWN: ``text`` is a dash and
    ``detail`` says which account could not be read. It is NOT the same as an
    account genuinely worth nothing, which is ``available=True`` with
    ``text='$0.00'``.

    ``stale`` means the number is real but old enough that a refresh should have
    landed and did not. The money is still shown -- discarding a known figure
    because it aged an hour would be its own kind of lie -- but it is labelled,
    in the text, and ``detail`` carries the instant it was actually read.
    """
    text: str
    detail: str
    available: bool
    stale: bool


def combine_account_values(
        reads: Sequence[Tuple[str, Optional[float]]]
) -> Tuple[Optional[float], List[str]]:
    """Total the per-account values for the "All accounts" selection. Pure.

    WHY SUM AT ALL, rather than show nothing under "All": "All" is this app's
    DEFAULT selection and the aggregate view everywhere else (every dashboard
    widget aggregates across accounts under it). A header that went blank in the
    default case would be a balance most users never see, which is not a feature.

    THE RULE THAT MATTERS: an account whose balance could not be read makes the
    TOTAL unknown. It does not silently drop out of the sum. ``1000 + unknown``
    is not ``1000`` -- printing it would be a confident, wrong, and specifically
    SMALLER number, which is the unknown-reads-as-zero pattern this project has
    just removed 25 instances of. The names of the failing accounts come back so
    the reader can be told which one.

    An EMPTY sequence (no broker configured) is likewise ``None`` and not ``0.0``:
    there is no total, as opposed to a total of nothing.

    Nothing is rounded here. The legs are summed at full precision and formatted
    once, at the end, by ``format_account_money`` -- rounding the parts first
    drifts the total.

    Returns:
        (total, unreadable_labels). ``total`` is ``None`` iff ``unreadable_labels``
        is non-empty or ``reads`` is empty.
    """
    unreadable = [label for label, value in reads if value is None]
    if unreadable or not reads:
        return None, unreadable
    return float(sum(value for _, value in reads)), []


def header_balance(*, value: Optional[float], as_of: Optional[datetime],
                   now: datetime,
                   unreadable: Sequence[str] = ()) -> HeaderBalance:
    """Turn (value, age) into what the header says. Pure; never raises.

    Args:
        value: the combined account value, or ``None`` for UNKNOWN. ``0.0`` is a
            real, printable balance and is never treated as unknown -- the test
            for falsiness that would conflate them is the bug this signature
            exists to make impossible.
        as_of: when ``value`` was actually read from the broker. ``None`` with a
            non-``None`` value means we cannot date the figure, which is reported
            as stale: freshness we cannot evidence is not freshness.
        now: the current instant, injected so the expiry rule is testable.
        unreadable: labels of the accounts that could not be read; used only to
            explain an unknown.
    """
    if value is None:
        detail = (HEADER_BALANCE_UNREADABLE_DETAIL_FMT.format(names=', '.join(unreadable))
                  if unreadable else HEADER_BALANCE_NOTHING_TO_READ_DETAIL)
        return HeaderBalance(text=HEADER_BALANCE_UNAVAILABLE_TEXT, detail=detail,
                             available=False, stale=False)

    if as_of is None:
        return HeaderBalance(
            text=format_account_money(value) + HEADER_BALANCE_STALE_SUFFIX,
            detail=HEADER_BALANCE_UNDATED_DETAIL, available=True, stale=True)

    when = as_of.strftime(HEADER_BALANCE_TIME_FMT)
    stale = (now - as_of).total_seconds() > HEADER_BALANCE_STALE_AFTER_SECONDS
    text = format_account_money(value) + (HEADER_BALANCE_STALE_SUFFIX if stale else '')
    detail = (HEADER_BALANCE_STALE_DETAIL_FMT if stale
              else HEADER_BALANCE_FRESH_DETAIL_FMT).format(when=when)
    return HeaderBalance(text=text, detail=detail, available=True, stale=stale)


@dataclass
class _BalanceEntry:
    """One account's cached value.

    ``value``/``as_of`` describe the last SUCCESSFUL read and are never
    overwritten by a failure -- a broker outage must not turn a known balance into
    an unknown one, it must turn a fresh one into a visibly stale one.
    ``attempted_at`` is bumped on every attempt and is what the hourly window is
    measured against, so a failing broker is retried hourly rather than hammered.
    """
    value: Optional[float] = None
    as_of: Optional[datetime] = None
    attempted_at: Optional[datetime] = None


#: account_id -> _BalanceEntry. Process-wide: this is a single-user app and the
#: balance is a property of the broker account, not of the browser session.
_BALANCE_CACHE: Dict[int, _BalanceEntry] = {}
_BALANCE_LOCKS: Dict[int, threading.Lock] = {}
_BALANCE_LOCKS_GUARD = threading.Lock()


def clear_header_balance_cache() -> None:
    """Drop every cached balance. For tests and for an explicit user refresh."""
    with _BALANCE_LOCKS_GUARD:
        _BALANCE_CACHE.clear()
        _BALANCE_LOCKS.clear()


def _lock_for(account_id: int) -> threading.Lock:
    with _BALANCE_LOCKS_GUARD:
        lock = _BALANCE_LOCKS.get(account_id)
        if lock is None:
            lock = _BALANCE_LOCKS[account_id] = threading.Lock()
        return lock


def _read_account_value(account_id: int) -> Optional[float]:
    """One broker read. BLOCKING -- only ever called inside ``asyncio.to_thread``.

    Imported lazily because ``core.utils``' instance factory pulls the live
    account registry, which must not be a page-import-time dependency.
    """
    from ..core.utils import get_account_instance_from_id
    account = get_account_instance_from_id(account_id)
    if account is None:
        return None
    return account_value_from_snapshot(account.get_account_snapshot())


def refresh_header_balance_cache(account_ids: Sequence[int], *, force: bool = False,
                                 utcnow=_utcnow) -> bool:
    """Re-read any account whose hourly window has elapsed. BLOCKING.

    Runs in a worker thread (``asyncio.to_thread``); NEVER on the render path.
    Never raises: a broker problem is logged and leaves the previous value in
    place. One dead account does not stop the others being read.

    Returns:
        True if any cached value actually changed (i.e. the header is worth
        redrawing).
    """
    changed = False
    for account_id in account_ids:
        try:
            if _refresh_one_balance(account_id, force=force, utcnow=utcnow):
                changed = True
        except Exception as e:
            # Defence in depth: _refresh_one_balance already swallows the broker
            # call's exceptions. Anything that escapes it is a bug here, and a bug
            # in a header must not become an unhandled task exception.
            logger.error(f"Header balance: refresh of account {account_id} failed: {e}",
                         exc_info=True)
    return changed


def _refresh_one_balance(account_id: int, *, force: bool, utcnow) -> bool:
    entry = _BALANCE_CACHE.get(account_id)
    if not force and _is_within_window(entry, utcnow()):
        return False

    # NON-BLOCKING acquire: if another page load is already refreshing this
    # account, this caller has nothing to add and returns rather than queueing a
    # second worker thread behind a broker round trip.
    lock = _lock_for(account_id)
    if not lock.acquire(blocking=False):
        return False
    try:
        entry = _BALANCE_CACHE.get(account_id)
        now = utcnow()
        if not force and _is_within_window(entry, now):
            return False

        try:
            value = _read_account_value(account_id)
        except Exception as e:
            logger.warning(f"Header balance: could not read account {account_id}: {e}")
            value = None

        if entry is None:
            entry = _BALANCE_CACHE[account_id] = _BalanceEntry()
        entry.attempted_at = utcnow()
        if value is None:
            # A FAILED read is not cached as a value. The previous good figure
            # stays, and because as_of is untouched it will age into "(stale)"
            # on its own -- which is the honest outcome, unlike either a dash
            # that discards a real number or a fresh-looking one that lies.
            return False
        changed = entry.value != value
        entry.value = value
        entry.as_of = entry.attempted_at
        return changed
    finally:
        lock.release()


def _is_within_window(entry: Optional[_BalanceEntry], now: datetime) -> bool:
    if entry is None or entry.attempted_at is None:
        return False
    return (now - entry.attempted_at).total_seconds() < HEADER_BALANCE_REFRESH_SECONDS


def header_balance_from_cache(accounts: Sequence[Tuple[int, str]], *,
                              utcnow=_utcnow) -> HeaderBalance:
    """The header's text for ``accounts``, out of the cache. NO BROKER CALL.

    This is the render path: a dict lookup per account and some arithmetic. An
    account with no cache entry at all reads as UNKNOWN, exactly like one whose
    broker refused -- we have no figure either way, and inventing one is the whole
    class of bug this widget is shaped around.

    The total is dated by its OLDEST leg: a sum is only as fresh as its stalest
    component.
    """
    reads: List[Tuple[str, Optional[float]]] = []
    oldest: Optional[datetime] = None
    for account_id, label in accounts:
        entry = _BALANCE_CACHE.get(account_id)
        value = entry.value if entry is not None else None
        reads.append((label, value))
        if value is not None and entry is not None and entry.as_of is not None:
            oldest = entry.as_of if oldest is None else min(oldest, entry.as_of)
    total, unreadable = combine_account_values(reads)
    return header_balance(value=total, as_of=oldest, now=utcnow(),
                          unreadable=unreadable)


def accounts_in_scope() -> List[Tuple[int, str]]:
    """The (id, label) accounts the header should total, honouring the dropdown.

    "All" (a ``None`` selection) means every real account; a specific selection
    means just that one. Both source functions already swallow their own errors,
    so this returns ``[]`` rather than raising when the DB is unreachable -- and
    ``[]`` renders as unknown, not as zero.
    """
    options = get_accounts_for_filter()
    pairs = [(acc_id, label) for label, acc_id in options if acc_id is not None]
    selected = get_selected_account_id()
    if selected is None:
        return pairs
    return [(acc_id, label) for acc_id, label in pairs if acc_id == selected]


@contextmanager
def layout_render(navigation_title: str):
    """Custom page frame for modern AI trading platform UI"""
    
    # Serve static files
    static_dir = Path(__file__).parent / 'static'
    app.add_static_files('/static', static_dir)
    
    # Set Quasar/NiceGUI colors
    ui.colors(
        primary=COLORS['accent'],
        secondary=COLORS['accent_blue'],
        accent=COLORS['accent_purple'],
        positive=COLORS['success'],
        negative=COLORS['danger'],
        warning=COLORS['warning'],
        info=COLORS['accent_blue'],
        dark=COLORS['primary']
    )
    
    # Link to external CSS file
    ui.add_head_html('<link rel="stylesheet" href="/static/styles.css">')
    
    # Footer (hidden by default)
    with ui.footer(value=False) as footer:
        ui.label('BA2 Trade Platform © 2025').classes('text-secondary-custom')

    # Modern side drawer
    with ui.left_drawer().classes('bg-transparent') as left_drawer:
        # Logo/Brand section
        with ui.column().classes('w-full p-4 mb-4'):
            with ui.row().classes('items-center gap-3'):
                ui.icon('show_chart', size='lg').classes('text-accent')
                ui.label('BA2 Trade').classes('text-xl font-bold text-white')
            ui.label('AI Trading Platform').classes('text-xs text-secondary-custom mt-1')
        
        ui.separator().classes('mb-2')
        sidemenu()
        
        # Version info at bottom
        with ui.column().classes('absolute bottom-4 left-4 right-4'):
            ui.separator().classes('mb-4')
            from ..version import APP_VERSION
            ui.label(APP_VERSION).classes('text-xs text-secondary-custom text-center w-full')

    # Help button
    with ui.page_sticky(position='bottom-right', x_offset=20, y_offset=20):
        ui.button(on_click=footer.toggle, icon='help_outline').props('fab color=accent').classes('glow-accent')

    # Modern header
    with ui.header().classes('items-center'):
        ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat round color=white')
        ui.space()
        
        # Page title with breadcrumb style
        with ui.row().classes('items-center gap-2'):
            ui.icon('chevron_right', size='sm').classes('text-secondary-custom')
            ui.label(navigation_title).classes('text-lg font-medium')
        
        ui.space()
        
        # Right side actions
        with ui.row().classes('items-center gap-2'):
            # Selected account's balance (replaced a hardcoded 'LIVE' badge)
            _render_account_balance()

            # Account filter dropdown
            _render_account_filter_dropdown()
            
            topmenu()
    
    # Main content area with padding
    with ui.column().classes('w-full p-6 text-white'):
        yield


def _render_account_balance():
    """Draw the header's balance slot. SYNCHRONOUS, and never touches a broker.

    Everything drawn here comes out of ``_BALANCE_CACHE``. The broker read is
    deferred to two ``ui.timer``s -- one shortly after the page is built, one
    hourly -- whose bodies hand the blocking work to ``asyncio.to_thread`` and
    swallow every exception. So the three ways this could hurt the app are all
    closed:

      * a SLOW broker cannot delay the render, because the render does not wait
        on it and the read runs off the event loop;
      * a FAILING broker cannot break the render, because the failure happens
        after the HTML exists and is caught;
      * a BUG HERE cannot take the page down, because the decision is computed
        inside a try/except that degrades to "unknown".

    The timers are per-client elements, so they die with the tab that made them.
    """
    accounts = _scope_or_empty()
    view = _view_or_unknown(accounts)

    with ui.row().classes('items-center gap-1 mr-4').mark(HEADER_BALANCE_MARKER):
        icon = ui.icon('account_balance_wallet', size='xs').classes('text-secondary-custom')
        label = ui.label(view.text).classes('text-xs font-medium')
        tooltip = ui.tooltip(view.detail)
    _paint(label, icon, tooltip, view)

    async def _refresh() -> None:
        try:
            # asyncio.to_thread per this repo's convention: the broker round trip
            # must never run on the event loop, or one slow account freezes every
            # open page rather than just this widget.
            await asyncio.to_thread(refresh_header_balance_cache,
                                    [acc_id for acc_id, _ in accounts])
        except Exception as e:
            logger.warning(f"Header balance: refresh task failed: {e}")
        try:
            _paint(label, icon, tooltip, _view_or_unknown(accounts))
        except Exception as e:
            logger.warning(f"Header balance: could not repaint: {e}")

    ui.timer(HEADER_BALANCE_FIRST_REFRESH_SECONDS, _refresh,
             once=True).mark(HEADER_BALANCE_FIRST_TIMER_MARKER)
    ui.timer(HEADER_BALANCE_REFRESH_SECONDS, _refresh).mark(HEADER_BALANCE_TICK_TIMER_MARKER)


def _scope_or_empty() -> List[Tuple[int, str]]:
    try:
        return accounts_in_scope()
    except Exception as e:
        logger.warning(f"Header balance: could not resolve the account scope: {e}")
        return []


def _view_or_unknown(accounts: Sequence[Tuple[int, str]]) -> HeaderBalance:
    try:
        return header_balance_from_cache(accounts)
    except Exception as e:
        logger.error(f"Header balance: could not build the view: {e}", exc_info=True)
        return HeaderBalance(text=HEADER_BALANCE_UNAVAILABLE_TEXT,
                             detail=HEADER_BALANCE_NOTHING_TO_READ_DETAIL,
                             available=False, stale=False)


def _paint(label, icon, tooltip, view: HeaderBalance) -> None:
    """Push a decided ``HeaderBalance`` onto the three header elements."""
    label.set_text(view.text)
    tooltip.set_text(view.detail)
    if not view.available:
        colour, glyph = 'text-secondary-custom', 'help_outline'
    elif view.stale:
        colour, glyph = 'text-warning', 'history'
    else:
        colour, glyph = 'text-accent', 'account_balance_wallet'
    label.classes(replace=f'text-xs font-medium {colour}')
    icon.classes(replace=colour)
    icon.props(f'name={glyph}')


def _render_account_filter_dropdown():
    """Render the account filter dropdown in the header."""
    # Get accounts for dropdown options
    account_options = get_accounts_for_filter()
    
    # Build options dict for ui.select: {value: label}
    # Use "all" string instead of None for the "All" option (None doesn't work well with ui.select)
    options_dict = {}
    for label, acc_id in account_options:
        key = "all" if acc_id is None else acc_id
        options_dict[key] = label
    
    # Get current selection - convert None to "all" for ui.select
    current_selection = get_selected_account_id()
    current_value = "all" if current_selection is None else current_selection
    
    async def on_account_change(e):
        """Handle account selection change."""
        new_value = e.value
        # Convert "all" back to None for storage
        account_id = None if new_value == "all" else new_value
        set_selected_account_id(account_id)
        # Reload the page so every tab re-reads the new selection. Use an explicit
        # window.location.reload() rather than ui.navigate.to(current_path) or
        # ui.navigate.reload():
        #   - navigate.to(current_path) targets the URL we're already on (it always
        #     carries the active tab's hash, e.g. "/#account_growth"), which the
        #     browser treats as a same-fragment no-op — the page never rebuilt and
        #     the account filter silently stopped applying.
        #   - ui.navigate.reload() issues history.go(0), which is an unreliable
        #     reload (a soft no-op in several browsers / SPA contexts).
        # window.location.reload() forces a real document reload of the current URL
        # *including* the hash, so the active tab is still restored by
        # setup_tab_navigation while the filter actually refreshes.
        #
        # NOT awaited, on purpose: awaiting an AwaitableResponse waits for a REPLY from a
        # document that is busy tearing itself down. The un-awaited call is still sent --
        # AwaitableResponse.__init__ schedules it (nicegui/awaitable_response.py:21). That
        # only holds while nothing wraps Client.run_javascript in a coroutine; ui/main.py
        # once did, and this reload silently stopped happening. See
        # tests/test_ui_account_switch_reload.py.
        ui.run_javascript('window.location.reload()')
    
    with ui.row().classes('items-center gap-1 mr-4'):
        ui.icon('account_circle', size='xs').classes('text-secondary-custom')
        ui.select(
            options=options_dict,
            value=current_value,
            on_change=on_account_change
        ).props('dense outlined dark color=white').classes('text-xs min-w-32').style('font-size: 0.75rem;')
