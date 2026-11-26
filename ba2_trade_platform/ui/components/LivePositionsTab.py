"""The Live Trades "Live View" tab: the BROKER's open positions, laid out like the tastytrade mobile Positions screen
(a dense table: Symbol pinned on the left, P/L Day / P/L Opn% / P/L Open first on a phone, a Total row under the
header, a bar under each P/L Open), wider with more columns on a desktop, refreshed in place every few seconds.

THIN on purpose: every number, colour, column, sort and stale-row rule is in ``ui.utils.live_positions`` (pure,
unit-tested); the reads are ``services.live_positions`` in a worker thread. The loader is injectable (the demo).

LIVE REFRESH. A ``ui.timer`` refreshes the rows IN PLACE (the table is never rebuilt, so the sort, the selection and
the scroll position survive). It only works while this tab is the page's selected tab (``is_active``), one refresh
at a time (a tick that finds a read still running is skipped), the reads run off the event loop, and the timer is
stopped while the browser is disconnected. Interval 5 s / 10 s / 30 s / Off, remembered.

A FAILED READ IS NEVER "NO POSITIONS": the account's last good rows stay, marked stale, and a warning names it.

Remembered choices (interval, sort; the page adds the selected tab) live in ``app.storage.user`` under
``live_positions.storage_key(port)``, read before the first await. The phone sheet must be added before the PAGE's
first await (NiceGUI drops CSS added later): the page calls ``install_live_view_styles`` first thing."""
import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from nicegui import ui

from ...logger import logger
from ..utils import live_positions as lp
from ..utils.responsive import CssOnce
from .refresh_button import refresh_button

#: The whole load (the service bounds each broker at its own deadline; this is the backstop).
LOAD_DEADLINE_S = 30.0
#: How long the first rows wait for the browser to say whether it is a phone (after that: desktop order, re-ordered
#: in place when the answer comes).
PHONE_PROBE_WAIT_S = 3.0
#: The browser event a viewport crossing the phone breakpoint emits.
PHONE_EVENT = 'lv_phone_change'
#: Returns whether the viewport is a phone, and (once per window) installs the listener that emits ``PHONE_EVENT``.
PHONE_PROBE_JS = (
    '(() => { const m = window.matchMedia("(max-width: 639px)");'
    ' if (!window.__lvPhoneListener) { window.__lvPhoneListener = true; let last = m.matches;'
    ' const f = (e) => { if (e.matches === last) return; last = e.matches;'
    f' emitEvent("{PHONE_EVENT}", {{phone: e.matches}}); }};'
    ' if (m.addEventListener) { m.addEventListener("change", f); } else { m.addListener(f); } }'
    ' return m.matches; })()'
)

_CSS_ONCE = CssOnce()

TOP_ROW_SLOT = r'''
    <q-tr style="background: rgba(255,255,255,0.06);">
        <q-td auto-width />
        <q-td v-for="col in props.cols" :key="col.name"
              :class="[col.align === 'right' ? 'text-right' : 'text-left', col.classes]"
              :style="'font-weight: 700;' + (col.total_color ? ' color: ' + col.total_color + ' !important;' : '')">
            {{ col.total_text }}
        </q-td>
    </q-tr>
'''

BODY_CELL_SLOT = r'''
    <q-td :props="props">
        <span :style="props.row[props.col.name + '_color'] ? 'color: ' + props.row[props.col.name + '_color'] + ' !important;' : ''">
            {{ props.row[props.col.name + '_text'] !== undefined ? props.row[props.col.name + '_text'] : props.value }}
        </span>
    </q-td>
'''

SYMBOL_CELL_SLOT = r'''
    <q-td :props="props">
        <span style="font-weight: 700; color: #ffffff !important;">{{ props.value }}</span>
        <q-badge v-if="props.row.stale" color="orange-9" text-color="white" label="stale" class="q-ml-xs">
            <q-tooltip>This account could not be read just now: these are its last figures.</q-tooltip>
        </q-badge>
    </q-td>
'''

PL_OPEN_CELL_SLOT = r'''
    <q-td :props="props">
        <div :style="'color: ' + props.row.pl_open_color + ' !important;'">{{ props.row.pl_open_text }}</div>
        <div v-if="props.row.pl_open_bar !== null"
             style="height: 3px; margin-top: 2px; border-radius: 2px; overflow: hidden; display: flex;
                    justify-content: flex-end; background: rgba(255,255,255,0.08);">
            <div :style="'height: 100%; width: ' + (props.row.pl_open_bar * 100).toFixed(1) + '%; background: ' + props.row.pl_open_color + ';'"></div>
        </div>
    </q-td>
'''

# Labels WRAP, and are bounded (a symbol with eight labels must not push every column after it off-screen).
# Inline styles on purpose: this build ships a subset of the utility classes (see test_ui_colour_classes_paint).
LABELS_CELL_SLOT = r'''
    <q-td :props="props" style="white-space: normal; max-width: 20rem;">
        <div style="display: flex; flex-wrap: wrap; gap: 3px;">
            <q-chip v-for="l in props.row.labels_list" :key="l" dense size="sm"
                    color="indigo-5" text-color="white" class="q-ma-none">{{ l }}</q-chip>
        </div>
        <span v-if="!props.row.labels_list || props.row.labels_list.length === 0" class="text-grey-6">—</span>
    </q-td>
'''


def install_live_view_styles() -> None:
    """Add the Live View's phone sheet to this page, once. Call it before the page's first await."""
    _CSS_ONCE.add(ui.context.client, 'live-view', lp.live_view_phone_css(), ui.add_css)


def _port() -> Optional[int]:
    from ... import config
    return getattr(config, 'HTTP_PORT', None)


def page_storage_key(storage_port: Optional[int] = None) -> str:
    return lp.storage_key(storage_port if storage_port is not None else _port())


def read_stored(key: str) -> Any:
    try:
        from nicegui import app
        return app.storage.user.get(key)
    except (RuntimeError, AttributeError) as e:       # outside a UI context / no storage: defaults
        logger.debug(f"LiveView: storage unavailable for read of '{key}': {e}")
        return None


def write_choices(key: str, **changes) -> None:
    """Merge ``changes`` into the page's stored choices (the page stores the tab, this tab the interval / sort)."""
    try:
        from nicegui import app
        stored = app.storage.user.get(key)
        merged = dict(stored) if isinstance(stored, dict) else {}
        merged.update(changes)
        app.storage.user[key] = merged
    except (RuntimeError, AttributeError) as e:
        logger.warning(f"LiveView: could not remember the Live Trades choices: {e}")


def _default_loader(account_id, first_load):
    from ...services.live_positions import load_live_positions
    return load_live_positions(account_id, first_load=first_load)


def _default_account_id() -> Optional[int]:
    from ..account_filter_context import get_selected_account_id
    return get_selected_account_id()


def _why(e: BaseException) -> str:
    from ...services.price_history import redact_message
    return redact_message(f'{type(e).__name__}: {e}')


class LivePositionsTab:
    """``loader(account_id, first_load) -> services.live_positions.LiveLoad`` (sync; run in a worker thread).
    ``is_active()`` says whether this tab is the page's selected tab (the timer does nothing otherwise)."""

    def __init__(self, loader: Callable = None, account_id_fn: Callable[[], Optional[int]] = None,
                 is_active: Callable[[], bool] = None, storage_port: Optional[int] = None,
                 now_fn: Callable[[], datetime] = None):
        self._loader = loader or _default_loader
        self._account_id = (account_id_fn or _default_account_id)()
        self._show_account = self._account_id is None
        self._is_active = is_active or (lambda: True)
        self._now_fn = now_fn or datetime.now
        self._key = page_storage_key(storage_port)
        self._state = lp.resolve_state(read_stored(self._key), [lp.LIVE_VIEW_TAB])   # BEFORE any await
        self._book: Dict[int, lp.AccountBook] = {}
        self._rows = []
        self._labels_error: Optional[str] = None
        self._loaded_once = False
        #: a load was ATTEMPTED: only the very first attempt logs its timing at INFO, also when it failed
        self._attempted = False
        self._loading = False
        self._last_update: Optional[datetime] = None
        self._phone = False
        self._task: Optional[asyncio.Task] = None
        self._client = ui.context.client
        install_live_view_styles()
        self.render()
        ui.on(PHONE_EVENT, lambda e: self._set_phone(bool(e.args['phone'])))
        self._client.on_disconnect(self._on_disconnect)
        self._client.on_connect(self._on_connect)
        self._phone_task = asyncio.create_task(self._probe_phone())
        if self._is_active():
            self._task = asyncio.create_task(self._refresh())

    # ------------------------------------------------------------------ layout

    def render(self):
        with ui.column().classes('w-full gap-1'):
            with ui.row().classes('w-full items-center gap-2'):
                self._interval = ui.toggle(lp.INTERVALS, value=self._state.interval).props(
                    'dense no-caps').classes('ba2-ctl')
                self._interval.tooltip('How often the positions are re-read from the broker')
                ui.space()
                self._reload_btn = refresh_button(self.refresh_now, dense=True,
                                                  tooltip="Re-read the broker's positions now")
            with ui.row().classes('w-full gap-2 items-center'):
                self._filter = ui.input(label='Filter table',
                                        placeholder='Symbol, account, asset class or label...').props(
                    'stack-label dense clearable').classes('flex-grow')
                with ui.row().classes(f'items-center gap-2 {lp.DESKTOP_ONLY_CLASS}'):
                    ui.button('Manage Labels', icon='label',
                              on_click=self._show_manage_labels_dialog).props('outline color=primary')
                    ui.label('Tick rows, then add/remove a label for those instruments.').classes(
                        'text-xs text-gray-400')
            self._warnings = ui.column().classes('w-full gap-0')
            with ui.row().classes('w-full items-center gap-3'):
                self._updated = ui.label('').classes('text-xs text-gray-400')
                self._status = ui.label('Loading positions...').classes('text-sm text-gray-500')
            totals = lp.compute_totals([])
            self._columns = lp.quasar_columns(self._phone, self._show_account, totals)
            self._table = ui.table(
                columns=self._columns, rows=[], row_key='key', selection='multiple',
                pagination={'rowsPerPage': 0, 'sortBy': self._state.sort_by,
                            'descending': self._state.descending},
                on_pagination_change=self._on_sort,
            ).classes(f'w-full {lp.TABLE_CLASS}').props(
                f'dense flat hide-pagination binary-state-sort :filter-method="{lp.FILTER_METHOD_JS}"')
            self._table.add_slot('top-row', TOP_ROW_SLOT)
            self._table.add_slot('body-cell', BODY_CELL_SLOT)
            self._table.add_slot('body-cell-symbol', SYMBOL_CELL_SLOT)
            self._table.add_slot('body-cell-pl_open', PL_OPEN_CELL_SLOT)
            self._table.add_slot('body-cell-labels', LABELS_CELL_SLOT)
            self._filter.bind_value(self._table, 'filter')
            self._filter.on_value_change(lambda _e: self._update_totals())
        seconds = lp.interval_seconds(self._state.interval)
        self._timer = ui.timer(seconds or 10.0, self._tick, active=seconds is not None, immediate=False)
        self._interval.on_value_change(self._on_interval)

    # ------------------------------------------------------------------ status

    def _set_status(self, text: str, error: bool = False):
        try:
            self._status.set_text(text)
            self._status.classes(replace='text-sm text-red-600' if error else 'text-sm text-gray-500')
        except Exception as e:  # noqa: BLE001 -- the client went away: there is nothing left to tell
            logger.debug(f"LiveView: status not shown ({type(e).__name__}: {e})")

    def _set_busy(self, busy: bool):
        self._loading = busy
        try:
            if busy:
                self._reload_btn.disable()
            else:
                self._reload_btn.enable()
        except Exception as e:  # noqa: BLE001 -- the client went away
            logger.debug(f"LiveView: refresh button not updated ({type(e).__name__}: {e})")

    # ------------------------------------------------------------------ refresh

    async def _tick(self):
        if self._loading or not self._is_active() or not self._client.has_socket_connection:
            return
        await self._refresh()

    def refresh_now(self):
        if self._loading:
            self._set_status('Already reading the broker - the table updates when this read finishes.')
            return
        self._task = asyncio.create_task(self._refresh())

    def on_shown(self):
        """The page switched to this tab: load now if there is nothing on screen or it is older than one interval."""
        seconds = lp.interval_seconds(self._interval.value)
        stale = (self._last_update is None or seconds is None
                 or (self._now_fn() - self._last_update).total_seconds() >= seconds)
        if not self._loading and (not self._loaded_once or stale):
            self._task = asyncio.create_task(self._refresh())

    async def _refresh(self):
        if self._loading:
            return
        self._set_busy(True)
        first = not self._loaded_once
        log_at_info, self._attempted = not self._attempted, True
        try:
            load = await asyncio.wait_for(asyncio.to_thread(self._loader, self._account_id, log_at_info),
                                          LOAD_DEADLINE_S)
            if first and self._phone_task is not None and not self._phone_task.done():
                # the viewport is read while the brokers are: the first rows arrive in the phone's column order
                await asyncio.wait({self._phone_task}, timeout=PHONE_PROBE_WAIT_S)
            self._loaded_once = True
            self._labels_error = load.labels_error
            self._book = lp.merge_reads(self._book, load.reads, self._now_fn())
            self._set_status('')
            self._apply()
            self._last_update = self._now_fn()
            self._updated.set_text(f"Updated {self._last_update.strftime('%H:%M:%S')}")
        except asyncio.TimeoutError:
            logger.error(f"LiveView: the positions load did not finish within {LOAD_DEADLINE_S:.0f}s")
            self._fail_all(f'no answer within {LOAD_DEADLINE_S:.0f}s')
            self._set_status(f'The positions read did not finish within {LOAD_DEADLINE_S:.0f}s.', error=True)
        except Exception as e:  # noqa: BLE001 -- deliberate: every failure ends in the status line, never a blank tab
            logger.error(f"LiveView: positions load failed: {_why(e)}", exc_info=True)
            self._fail_all(f'the load failed ({type(e).__name__})')
            self._set_status(f'Could not load the positions: {_why(e)}', error=True)
        finally:
            self._set_busy(False)

    def _fail_all(self, why: str):
        """The whole load failed: every account on screen keeps its rows, stale."""
        if not self._book:
            return
        reads = [lp.AccountRead(aid, b.name, lp.STATUS_FAILED, why) for aid, b in self._book.items()]
        self._book = lp.merge_reads(self._book, reads, self._now_fn())
        try:
            self._apply()
        except Exception as e:  # noqa: BLE001 -- the client went away
            logger.debug(f"LiveView: stale rows not shown ({type(e).__name__}: {e})")

    # ------------------------------------------------------------------ drawing (in place)

    def _apply(self):
        self._rows = lp.book_rows(self._book)
        rows = lp.sort_rows(lp.table_rows(self._rows), self._state.sort_by, self._state.descending)
        keep = {r['key'] for r in (self._table.selected or [])}
        self._table.update_rows(rows, clear_selection=False)
        self._table.selected = [r for r in rows if r['key'] in keep]
        self._update_totals()
        self._warnings.clear()
        lines = lp.book_warnings(self._book) + ([f'Labels not shown: {self._labels_error}.']
                                                if self._labels_error else [])
        with self._warnings:
            for line in lines:
                ui.label(line).classes('text-sm text-orange-600')
        if not self._rows and not lines:
            self._set_status('No open positions at the broker in this scope.')

    def _update_totals(self):
        shown = lp.filter_rows(self._rows, self._filter.value)
        totals = lp.compute_totals(shown, unread_accounts=lp.unread_accounts(self._book))
        columns = lp.quasar_columns(self._phone, self._show_account, totals)
        if columns != self._columns:
            self._columns = columns
            self._table.columns = columns

    # ------------------------------------------------------------------ choices

    def _on_interval(self, e):
        choice = e.value
        seconds = lp.interval_seconds(choice)
        self._state = replace(self._state, interval=choice)
        write_choices(self._key, interval=choice)
        if seconds is None:
            self._timer.deactivate()
        else:
            self._timer.interval = seconds
            self._timer.activate()

    def _on_sort(self, e):
        value = e.value if isinstance(e.value, dict) else {}
        sort_by, descending = value.get('sortBy'), value.get('descending')
        if not isinstance(sort_by, str) or sort_by not in lp.COLUMN_BY_NAME or not isinstance(descending, bool):
            return
        if (sort_by, descending) != (self._state.sort_by, self._state.descending):
            self._state = replace(self._state, sort_by=sort_by, descending=descending)
            write_choices(self._key, sort_by=sort_by, descending=descending)

    # ------------------------------------------------------------------ browser lifecycle / phone

    def _on_disconnect(self):
        self._timer.deactivate()

    def _on_connect(self):
        if lp.interval_seconds(self._interval.value) is not None:
            self._timer.activate()

    async def _probe_phone(self):
        if getattr(self._client, '_request', None) is None:     # no browser behind this client: nothing to measure
            return
        try:
            await self._client.connected(timeout=60)
            phone = await self._client.run_javascript(PHONE_PROBE_JS, timeout=5)
        except Exception as e:  # noqa: BLE001 -- no answer: the desktop column order stays (the table still scrolls)
            logger.debug(f"LiveView: viewport not read ({type(e).__name__}: {e})")
            return
        self._set_phone(bool(phone))

    def _set_phone(self, phone: bool):
        if phone != self._phone:
            self._phone = phone
            self._columns = None
            self._update_totals()

    # ------------------------------------------------------------------ labels

    def _show_manage_labels_dialog(self):
        """Add or remove an instrument label across the selected rows. Labels live on the Instrument (by symbol), so
        the action applies to the distinct symbols of the selected rows; the label cells refresh in place."""
        from ...core.utils import (add_label_to_instruments, get_all_instrument_labels, get_labels_by_symbol,
                                   normalize_symbol, remove_label_from_instruments)
        selected = list(self._table.selected or [])
        symbols = sorted({r['symbol'] for r in selected if r.get('symbol')})
        if not symbols:
            ui.notify('Select one or more positions first (tick the checkboxes)', type='warning')
            return
        existing_labels = get_all_instrument_labels()

        with ui.dialog() as dialog, ui.card().classes('min-w-[420px]'):
            ui.label('Manage Labels').classes('text-h6')
            ui.label(f"{len(symbols)} instrument(s): {', '.join(symbols)}").classes('text-sm text-gray-400 mb-2')
            label_input = ui.input(label='Label',
                                   placeholder='Type a new label, or click an existing one below').classes('w-full')
            if existing_labels:
                ui.label('Existing labels (click to use):').classes('text-xs text-gray-400 mt-2')
                with ui.row().classes('gap-1 flex-wrap'):
                    for _lbl in existing_labels:
                        ui.chip(_lbl, on_click=lambda l=_lbl: label_input.set_value(l)).props(
                            'clickable dense color=indigo-5 text-color=white')

            def _apply(add: bool):
                label = (label_input.value or '').strip()
                if not label:
                    ui.notify('Enter or pick a label', type='warning')
                    return
                try:
                    if add:
                        n = add_label_to_instruments(symbols, label)
                        ui.notify(f"Added '{label}' to {n} instrument(s)", type='positive')
                    else:
                        n = remove_label_from_instruments(symbols, label)
                        ui.notify(f"Removed '{label}' from {n} instrument(s)", type='positive')
                except Exception as e:
                    logger.error(f"Label update failed: {e}", exc_info=True)
                    ui.notify(f'Error: {e}', type='negative')
                    return
                try:
                    refreshed = get_labels_by_symbol(symbols)
                    wanted = {normalize_symbol(s) for s in symbols}
                    self._book = {
                        aid: replace(b, rows=tuple(
                            replace(r, labels=tuple(refreshed.get(normalize_symbol(r.symbol), [])))
                            if normalize_symbol(r.symbol) in wanted else r for r in b.rows))
                        for aid, b in self._book.items()}
                    self._table.selected = []
                    self._apply()
                except Exception as e:
                    logger.warning(f"Could not refresh label cells in place: {e}")
                dialog.close()

            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Remove', on_click=lambda: _apply(False)).props('outline color=negative')
                ui.button('Add', on_click=lambda: _apply(True)).props('color=primary')

        dialog.open()
