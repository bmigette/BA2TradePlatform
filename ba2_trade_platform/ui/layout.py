import asyncio
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .menus import topmenu, sidemenu
from .theme import COLORS
from .account_filter_context import (get_accounts_for_filter, get_selected_account_id,
                                     refresh_accounts_for_filter, set_selected_account_id)
# THE SAME helper the Portfolio Allocation page's 'Account value' card uses. Two
# places showing "the account's value" from two different snapshot fields is the
# drift this project keeps paying for, so the field choice (net_liquidation) and
# the money formatting are imported, not restated. Cheap: the module is pure
# (math/re/dataclasses) and costs ~0.1s.
from .utils.portfolio_allocation_view import account_value_from_snapshot, format_account_money
# The size measurement and the 1 GB threshold are the SAME ones the startup
# maintenance pass uses (main.startup_db_maintenance -> db_maintenance), so the
# banner cannot come to a different conclusion about the database than the log does.
from ba2_common.core.db_maintenance import (database_file_size_bytes, format_bytes,
                                            resolve_db_size_warn_bytes)
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

#: Appended to a TOTAL that is real but INCOMPLETE -- one of the accounts under
#: "All" would not answer. Same word, same placement (in the text) and same
#: meaning as the 'Floating P/L Per Account' card's marker, so the two surfaces
#: cannot drift into two vocabularies for one situation.
HEADER_BALANCE_PARTIAL_SUFFIX = ' (partial)'

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
#: Appended to whichever detail applies when the total is missing a leg.
HEADER_BALANCE_PARTIAL_DETAIL_FMT = (
    ' — partial: excludes {names}, whose balance could not be read')

#: The breakdown's last line. Its own constant so the tests and the renderer
#: cannot disagree about the word.
HEADER_BALANCE_TOTAL_LABEL = 'Total'

HEADER_BALANCE_MARKER = 'header-balance'
HEADER_BALANCE_BREAKDOWN_MARKER = 'header-balance-breakdown'
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

    ``partial`` means the number is real but INCOMPLETE: it is a total under
    "All" and one of its accounts could not be read. Like ``stale`` it is marked
    in ``text``, and ``detail`` names the accounts the figure excludes.
    """
    text: str
    detail: str
    available: bool
    stale: bool
    partial: bool = False


def combine_account_values(
        reads: Sequence[Tuple[str, Optional[float]]]
) -> Tuple[Optional[float], List[str]]:
    """Total the per-account values for the "All accounts" selection. Pure.

    WHY SUM AT ALL, rather than show nothing under "All": "All" is this app's
    DEFAULT selection and the aggregate view everywhere else (every dashboard
    widget aggregates across accounts under it). A header that went blank in the
    default case would be a balance most users never see, which is not a feature.

    THE RULE THAT MATTERS: an account whose balance could not be read NEVER
    silently drops out of the sum. ``1000 + unknown`` is not ``1000`` -- printing
    that would be a confident, wrong, and specifically SMALLER number, which is
    the unknown-reads-as-zero pattern this project has removed dozens of
    instances of. The names of the failing accounts come back so the caller can
    mark the total ``(partial)`` and say what it excludes.

    THIS USED TO RETURN ``None`` -- unknown -- the moment ANY leg failed, on the
    grounds that a bare badge has no room to explain a caveat. The badge now
    carries a per-account BREAKDOWN (``header_balance_breakdown``), so the reader
    can see which leg is missing, and the marker rides in the badge's own text
    rather than only in the hover. Showing the readable part, marked, is then
    strictly more information than a dash -- and it is what the 'Floating P/L Per
    Account' card says in the same situation, in the same words.

    NOTHING READABLE AT ALL is still ``None``, not ``0.0``: there is no part to
    show, so there is no partial to mark. That also covers an EMPTY sequence (no
    broker configured): no total, as opposed to a total of nothing. It is what
    keeps the single-account case -- one leg, unreadable -- a dash.

    Nothing is rounded here. The legs are summed at full precision and formatted
    once, at the end, by ``format_account_money`` -- rounding the parts first
    drifts the total.

    Returns:
        ``(total, unreadable_labels)``. ``total`` is ``None`` iff nothing was
        readable. ``unreadable_labels`` is what the total excludes, in input
        order, and is non-empty exactly when something was left out.
    """
    unreadable = [label for label, value in reads if value is None]
    readable = [value for _, value in reads if value is not None]
    if not readable:
        return None, unreadable
    return float(sum(readable)), unreadable


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
        unreadable: labels of the accounts that could not be read. With no
            ``value`` they explain the unknown; WITH one they mean the figure is
            a total that excludes them, and it is marked ``(partial)``.
    """
    if value is None:
        detail = (HEADER_BALANCE_UNREADABLE_DETAIL_FMT.format(names=', '.join(unreadable))
                  if unreadable else HEADER_BALANCE_NOTHING_TO_READ_DETAIL)
        return HeaderBalance(text=HEADER_BALANCE_UNAVAILABLE_TEXT, detail=detail,
                             available=False, stale=False, partial=False)

    partial = bool(unreadable)
    if as_of is None:
        stale = True
        detail = HEADER_BALANCE_UNDATED_DETAIL
    else:
        stale = (now - as_of).total_seconds() > HEADER_BALANCE_STALE_AFTER_SECONDS
        detail = (HEADER_BALANCE_STALE_DETAIL_FMT if stale
                  else HEADER_BALANCE_FRESH_DETAIL_FMT).format(
                      when=as_of.strftime(HEADER_BALANCE_TIME_FMT))
    if partial:
        detail += HEADER_BALANCE_PARTIAL_DETAIL_FMT.format(names=', '.join(unreadable))

    # BOTH markers can apply and both are shown. They are different complaints --
    # 'the number is old' and 'the number is missing an account' -- and dropping
    # either because the other fired would hide a fault the reader needs.
    text = (format_account_money(value)
            + (HEADER_BALANCE_PARTIAL_SUFFIX if partial else '')
            + (HEADER_BALANCE_STALE_SUFFIX if stale else ''))
    return HeaderBalance(text=text, detail=detail, available=True, stale=stale,
                         partial=partial)


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


@dataclass(frozen=True)
class HeaderBalanceBreakdown:
    """What the badge says, and what it is made of. Pure.

    ``lines`` is one ``(label, HeaderBalance)`` per account IN SCOPE -- every one
    of them, whatever its state. An account missing from this list makes no
    statement about itself, which is the defect the 'Floating P/L Per Account'
    card was just fixed for; the three states a line can be in are the same three
    (a figure, a measured ``$0.00``, or ``—`` for could-not-read).
    """
    total: HeaderBalance
    lines: Tuple[Tuple[str, HeaderBalance], ...]


def header_balance_breakdown(accounts: Sequence[Tuple[int, str]], *,
                             utcnow=_utcnow) -> HeaderBalanceBreakdown:
    """The badge PLUS one line per account, out of the cache. NO BROKER CALL.

    Also a render-path function: a dict lookup per account. Each line is decided
    by the same ``header_balance`` the badge uses, so a leg and the total can
    never describe the same cache entry in two different vocabularies.
    """
    now = utcnow()
    lines: List[Tuple[str, HeaderBalance]] = []
    for account_id, label in accounts:
        entry = _BALANCE_CACHE.get(account_id)
        value = entry.value if entry is not None else None
        as_of = entry.as_of if entry is not None else None
        # ``unreadable`` only when there is nothing to show: passing the label
        # alongside a real value would mark a perfectly good leg as partial.
        lines.append((label, header_balance(value=value, as_of=as_of, now=now,
                                            unreadable=() if value is not None else (label,))))
    return HeaderBalanceBreakdown(
        total=header_balance_from_cache(accounts, utcnow=lambda: now),
        lines=tuple(lines))


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
        # Drawn (hidden) before the page body so the warning, if it fires, is the
        # first thing on the page rather than something below the fold.
        _render_db_size_banner()
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

    THE BREAKDOWN. Under "All" the badge is one number standing for several
    accounts, and there was no way to see what it was made of. A menu hanging off
    the badge lists each account and the total. It is only built when there is
    more than one account in scope: with a single account selected there is
    nothing to break down and the header is exactly what it was.
    """
    accounts = _scope_or_empty()
    view = _view_or_unknown(accounts)

    with ui.row().classes('items-center gap-1 mr-4 cursor-pointer').mark(HEADER_BALANCE_MARKER):
        icon = ui.icon('account_balance_wallet', size='xs').classes('text-secondary-custom')
        label = ui.label(view.text).classes('text-xs font-medium')
        tooltip = ui.tooltip(view.detail)
        breakdown = (ui.menu().mark(HEADER_BALANCE_BREAKDOWN_MARKER)
                     if len(accounts) > 1 else None)
    _paint(label, icon, tooltip, view)
    _paint_breakdown(breakdown, accounts)

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
        # Separately guarded: the breakdown is the secondary readout, and a bug
        # drawing it must not cost the badge the repaint it just computed.
        _paint_breakdown(breakdown, accounts)

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
    elif view.stale or view.partial:
        colour, glyph = 'text-warning', 'history' if view.stale else 'warning'
    else:
        colour, glyph = 'text-accent', 'account_balance_wallet'
    label.classes(replace=f'text-xs font-medium {colour}')
    icon.classes(replace=colour)
    icon.props(f'name={glyph}')


def _paint_breakdown(breakdown, accounts: Sequence[Tuple[int, str]]) -> None:
    """Rebuild the per-account menu from the cache. ``None`` means "not shown".

    ``clear()`` first, and every call: this runs again on every refresh, and
    appending would leave the previous (pre-refresh, all-dashes) copy of the list
    sitting above the new one.

    Never raises: this is a secondary readout on a header drawn by every route.
    """
    if breakdown is None:
        return
    try:
        view = header_balance_breakdown(accounts)
        breakdown.clear()
        with breakdown:
            with ui.column().classes('p-3 gap-1 min-w-56'):
                for label, line in view.lines:
                    with ui.row().classes('w-full justify-between items-center gap-6'):
                        ui.label(label).classes('text-xs text-secondary-custom')
                        ui.label(line.text).classes(
                            'text-xs font-medium'
                            + ('' if line.available else ' text-secondary-custom'))
                ui.separator()
                with ui.row().classes('w-full justify-between items-center gap-6'):
                    ui.label(HEADER_BALANCE_TOTAL_LABEL).classes('text-xs font-bold')
                    ui.label(view.total.text).classes('text-xs font-bold')
                # The detail, in the menu rather than only in the hover: it is
                # where '(partial)' is explained and the excluded account named.
                ui.label(view.total.detail).classes('text-xs text-secondary-custom')
    except Exception as e:
        logger.error(f"Header balance: could not build the breakdown: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# THE "YOUR DATABASE IS HUGE" BANNER
#
# Shown ONCE per app start, above 1 GB, naming the tool that can do something
# about it. Not once per page load: this layout is rebuilt on every navigation,
# and a warning that reappears on every click is one the reader learns to
# dismiss without reading.
#
# SAME RENDER-PATH DISCIPLINE AS THE BALANCE ABOVE. ``os.path.getsize`` is
# usually microseconds, but "usually" is doing a lot of work on a network mount
# or a stalled volume -- and this header is on the path of EVERY page. So the
# render draws an empty, hidden slot, and a ui.timer fills it afterwards from a
# worker thread.
#
# WHAT IT PROMISES. Only what the cleanup tool actually does: it removes old
# market analyses (with their outputs/recommendations), old activity logs, and
# -- since the trade_action_result retention windows were added to the same tab
# -- the JSON payloads of old trade action results (the biggest single thing on
# the user's database at 188.2 MB) and eventually those rows themselves. It
# still does NOT promise a specific saving: what any given run reclaims depends
# entirely on how much of the data is older than the configured windows.
# ---------------------------------------------------------------------------

DB_SIZE_BANNER_MARKER = 'db-size-banner'
DB_SIZE_BANNER_TIMER_MARKER = 'db-size-banner-check'

#: How soon after the page is built the size is measured. Non-zero so the HTML
#: goes out first.
DB_SIZE_BANNER_CHECK_SECONDS = 0.3

DB_SIZE_BANNER_TEXT_FMT = (
    'Database is {size}, at or over the {threshold} warning threshold. Old market '
    'analyses, activity logs and trade action result payloads can be removed in '
    'Settings → Cleanup → Batch Database Cleanup.')
DB_SIZE_BANNER_LINK_TEXT = 'Open Settings → Cleanup'
DB_SIZE_BANNER_LINK_TARGET = '/settings#cleanup'

#: Fires once per PROCESS. A plain bool behind a lock rather than a per-client
#: flag: "once per app start" is a property of the app, not of a browser tab.
_DB_SIZE_BANNER_LOCK = threading.Lock()
_DB_SIZE_BANNER_CHECKED = False


def reset_db_size_banner() -> None:
    """Re-arm the one-shot. For tests, and for an explicit re-check."""
    global _DB_SIZE_BANNER_CHECKED
    with _DB_SIZE_BANNER_LOCK:
        _DB_SIZE_BANNER_CHECKED = False


def _claim_db_size_check() -> bool:
    """True for the FIRST caller since app start; False for every one after.

    Claimed BEFORE the measurement, not after: two pages opened at once would
    otherwise both stat the file, and a check that keeps being retried is a
    check that runs on every navigation.
    """
    global _DB_SIZE_BANNER_CHECKED
    with _DB_SIZE_BANNER_LOCK:
        if _DB_SIZE_BANNER_CHECKED:
            return False
        _DB_SIZE_BANNER_CHECKED = True
        return True


def db_size_banner_text(size_bytes: Optional[int], threshold_bytes: int) -> Optional[str]:
    """What the banner should say, or ``None`` for "say nothing". Pure.

    ``size_bytes is None`` means the size could not be measured, and that is
    deliberately SILENT. A size we cannot read is not evidence of a large
    database; warning on it would fire for every backtest ``:memory:`` engine
    and would teach the reader to dismiss the banner. The operator hears about
    it in the log instead.

    The comparison is inclusive, matching the vacuum gate: exactly at the
    threshold counts as over it.
    """
    if size_bytes is None:
        return None
    if size_bytes < threshold_bytes:
        return None
    return DB_SIZE_BANNER_TEXT_FMT.format(size=format_bytes(size_bytes),
                                          threshold=format_bytes(threshold_bytes))


def _render_db_size_banner() -> None:
    """Reserve the banner slot and schedule the one check. NO filesystem access here."""
    container = ui.row().classes(
        'w-full items-center gap-2 mb-4 p-3 rounded'
    ).style('background: rgba(255, 167, 38, 0.12); border: 1px solid rgba(255, 167, 38, 0.5)'
            ).mark(DB_SIZE_BANNER_MARKER)
    container.set_visibility(False)

    async def _check() -> None:
        try:
            if not _claim_db_size_check():
                return
            size = await asyncio.to_thread(database_file_size_bytes)
            text = db_size_banner_text(size, resolve_db_size_warn_bytes())
            if text is None:
                if size is None:
                    logger.warning('Database size banner: the database size could not be '
                                   'measured, so no size warning can be made')
                return
            logger.warning(text)
            with container:
                ui.icon('storage', size='sm').classes('text-warning')
                ui.label(text).classes('text-sm')
                ui.link(DB_SIZE_BANNER_LINK_TEXT, DB_SIZE_BANNER_LINK_TARGET).classes('text-sm')
            container.set_visibility(True)
        except Exception as e:
            # A housekeeping warning must never cost a page.
            logger.error(f"Database size banner: the size check failed: {e}", exc_info=True)

    ui.timer(DB_SIZE_BANNER_CHECK_SECONDS, _check, once=True).mark(DB_SIZE_BANNER_TIMER_MARKER)


#: The ui.select key standing in for a ``None`` selection. A string, because
#: ``None`` does not work well as a ui.select option key.
ACCOUNT_FILTER_ALL = "all"


def _account_filter_options(account_options) -> Dict[Any, str]:
    """``[(label, id), ...]`` -> the ``{value: label}`` dict ui.select wants."""
    return {(ACCOUNT_FILTER_ALL if acc_id is None else acc_id): label
            for label, acc_id in account_options}


def _account_filter_state() -> Tuple[Dict[Any, str], Any]:
    """The dropdown's options and its value, with the value GUARANTEED to be one
    of them.

    THE CRASH THIS EXISTS FOR: the selected account can be DELETED while it is
    selected. The id outlives it in ``app.storage.user`` -- and in the
    process-global ``_last_known_account_id`` mirror, which nothing clears -- so
    the next render hands ``ui.select`` a value that is not among its options and
    NiceGUI raises ``ValueError: Invalid value: 2``. This header is built by EVERY
    route, before the page body, so one deleted account 500s the entire
    application on every page until storage is cleared by hand.

    THE OTHER DIRECTION MATTERS JUST AS MUCH. Resetting a selection that was
    actually fine is quieter than the crash and therefore worse to diagnose, so
    "not in the options" is deliberately not treated as proof of anything:

      * the listing is cached for 60 seconds, so an account created in the last
        minute is missing from it while being a perfectly valid choice. Before
        concluding an id is dead we re-read the accounts table once. That read is
        on the RARE path only -- a selection that is already in the cached options
        costs no database work, which is what keeps the cache worth having on a
        header drawn once per navigation;
      * a listing read can simply FAIL, and a failure is not a deletion. The
        widget still has to be given a valid value, so it falls back to "All" for
        this render, but nothing is persisted -- the next healthy render brings
        the account back on its own.

    Only a positively-confirmed absence is written back, and it is written through
    ``set_selected_account_id`` so there is exactly one writer of the two stores.
    """
    options_dict = _account_filter_options(get_accounts_for_filter())
    selected = get_selected_account_id()

    if selected is None:
        return options_dict, ACCOUNT_FILTER_ALL
    if selected in options_dict:
        return options_dict, selected

    fresh = refresh_accounts_for_filter()
    if fresh is None:
        logger.warning(f"Account filter: could not verify selected account {selected} "
                       f"(accounts unreadable); showing All for this render without "
                       f"clearing the selection")
        return options_dict, ACCOUNT_FILTER_ALL

    options_dict = _account_filter_options(fresh)
    if selected in options_dict:
        return options_dict, selected

    logger.warning(f"Account filter: selected account {selected} no longer exists; "
                   f"resetting the filter to All")
    set_selected_account_id(None)
    return options_dict, ACCOUNT_FILTER_ALL


def _render_account_filter_dropdown():
    """Render the account filter dropdown in the header."""
    options_dict, current_value = _account_filter_state()

    async def on_account_change(e):
        """Handle account selection change."""
        new_value = e.value
        # Convert "all" back to None for storage
        account_id = None if new_value == ACCOUNT_FILTER_ALL else new_value
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
