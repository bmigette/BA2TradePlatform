"""The Overview "Heatmap" tab: one treemap tile per held symbol (size = company market cap or my exposure, colour =
price change or my P/L over Today / This week / This month / This year), filtered by expert and allocation label.

THIN on purpose: every number, colour, size, caption and remembered-choice rule is in
``ui.utils.position_heatmap`` (pure, unit-tested); the data comes from ``services.position_heatmap`` in a worker
thread (which also builds the market-calendar tables, so the event loop never does). The data is loaded ONCE per tab
(and on Refresh): a toggle or a filter only recomputes and redraws.

EVERY LOAD ENDS IN A VISIBLE STATE: the load is bounded by ``LOAD_DEADLINE_S``; whatever fails after it (the
maths, the drawing, a client that went away) ends in the status line, with exception text redacted
(``price_history.redact_message``). Refresh is disabled while a load runs.

Remembered choices: ``app.storage.user`` -- the mechanism the header's account filter uses
(``ui.account_filter_context``) -- under ``position_heatmap.storage_key(port)``. Read synchronously before the first
await (storage needs the UI context), validated against what the loaded data offers, written on every change. A
stored expert / label that is not available for the current account is kept in storage (it applies again when an
account that has it is selected), not applied here, and named on screen.

No page CSS is added (inline styles only), so NiceGUI's "add_css after an await is dropped" trap does not apply."""
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

from ...logger import logger
from ...services.load_timing import LoadBreakdown
from ...services.price_history import redact_message
from ..utils import position_heatmap as hm

#: The whole load (the service bounds each broker / source itself; this is the backstop).
LOAD_DEADLINE_S = 120.0
#: How often the status line shows the load's progress.
PROGRESS_POLL_S = 0.5


def _port() -> Optional[int]:
    from ... import config
    return getattr(config, 'HTTP_PORT', None)


def _read_stored(key: str) -> Any:
    try:
        from nicegui import app
        return app.storage.user.get(key)
    except (RuntimeError, AttributeError) as e:       # outside a UI context: defaults
        logger.debug(f"Heatmap: storage unavailable for read of '{key}': {e}")
        return None


def _write_stored(key: str, value: Dict[str, Any]) -> None:
    try:
        from nicegui import app
        app.storage.user[key] = value
    except (RuntimeError, AttributeError) as e:
        logger.warning(f"Heatmap: could not remember the heatmap choices: {e}")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _default_loader(account_id, now, timing, progress=None):
    from ...services.position_heatmap import load_heatmap_data
    return load_heatmap_data(account_id, now, timing=timing, progress=progress)


def _default_account_id() -> Optional[int]:
    from ..account_filter_context import get_selected_account_id
    return get_selected_account_id()


def _why(e: BaseException) -> str:
    return redact_message(f'{type(e).__name__}: {e}')


class PositionHeatmapTab:
    """``loader(account_id, now, timing, progress) -> services.position_heatmap.HeatmapData``; injectable (the demo).
    ``progress(text)`` is called from the worker thread with the step it is on."""

    CHART_HEIGHT = '560px'

    def __init__(self, loader: Callable = None, now_fn: Callable[[], datetime] = None,
                 account_id_fn: Callable[[], Optional[int]] = None, storage_port: Optional[int] = None):
        self._loader = loader or _default_loader
        self._now_fn = now_fn or _now_utc
        self._account_id = (account_id_fn or _default_account_id)()
        self._key = hm.storage_key(storage_port if storage_port is not None else _port())
        self._stored = _read_stored(self._key)               # BEFORE any await
        self._state = hm.resolve_state(self._stored, [], [])
        self._data = None
        self._now = None
        self._experts: List[str] = []
        self._labels: List[str] = []
        self._syncing = False
        self._loading = False
        self.render()

    # ------------------------------------------------------------------ layout

    def render(self):
        s = self._state
        with ui.card().classes('w-full p-4'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label('Positions Heatmap').classes('text-lg font-bold')
                with ui.row().classes('items-center gap-2'):
                    self._updated = ui.label('').classes('text-xs text-gray-500')
                    self._reload_btn = ui.button(icon='refresh', on_click=self._reload).props(
                        'flat round dense').classes('ba2-ctl')
                    self._reload_btn.tooltip('Reload positions, prices, history and market caps now')
            with ui.row().classes('w-full items-center gap-3 mb-2'):
                self._tf = ui.toggle(hm.TIMEFRAMES, value=s.timeframe).props('dense no-caps').classes('ba2-ctl')
                self._metric = ui.toggle(hm.METRICS, value=s.metric).props('dense no-caps').classes('ba2-ctl')
                self._metric.tooltip('Price change: the instrument, independent of when you bought. My P/L: your '
                                     "position's profit or loss in the period, counting buys and sells made in it.")
                self._size = ui.toggle(hm.SIZE_MODES, value=s.size).props('dense no-caps').classes('ba2-ctl')
                self._size.tooltip('Company size: tile area = market cap. My exposure: tile area = my position '
                                   'value.')
            with ui.row().classes('w-full items-center gap-3 mb-1'):
                self._expert_sel = ui.select(options=[], value=[], label='Experts', multiple=True,
                                             clearable=True).props('dense outlined').classes('w-64')
                self._label_sel = ui.select(options=[], value=[], label='Labels', multiple=True,
                                            clearable=True).props('dense outlined').classes('w-64')
            self._filter_note = ui.label('').classes('text-xs text-orange-600')
            self._status = ui.label('Loading positions...').classes('text-sm text-gray-500')
            self._chart_box = ui.column().classes('w-full')
            with self._chart_box:
                ui.skeleton(type='rect', height=self.CHART_HEIGHT).classes('w-full')
            self._legend_box = ui.row().classes('w-full items-center gap-0 mt-2')
            self._caption = ui.label('').classes('text-xs text-gray-400 mt-1')
            self._issues_box = ui.column().classes('w-full gap-0 mt-2')
            self._mismatch_box = ui.column().classes('w-full gap-0 mt-2')

        for el in (self._tf, self._metric, self._size, self._expert_sel, self._label_sel):
            el.on_value_change(self._on_change)
        self._summaries()
        self._task = asyncio.create_task(self._load())

    # ------------------------------------------------------------------ status

    def _set_status(self, text: str, error: bool = False):
        try:
            self._status.set_text(text)
            self._status.classes(replace='text-sm text-red-600' if error else 'text-sm text-gray-500')
        except Exception as e:  # noqa: BLE001 -- the client went away: there is nothing left to tell
            logger.debug(f"Heatmap: status not shown ({type(e).__name__}: {e})")

    def _set_busy(self, busy: bool):
        self._loading = busy
        try:
            if busy:
                self._reload_btn.disable()
            else:
                self._reload_btn.enable()
        except Exception as e:  # noqa: BLE001 -- the client went away
            logger.debug(f"Heatmap: refresh button not updated ({type(e).__name__}: {e})")

    # ------------------------------------------------------------------ data

    async def _load(self):
        self._set_busy(True)
        timing = LoadBreakdown('Heatmap', f'account={self._account_id}')
        try:
            now = self._now_fn()
            data = await self._run_loader(now, timing)
            self._apply(data, now)
        except asyncio.TimeoutError:
            logger.error(f"Heatmap: the load did not finish within {LOAD_DEADLINE_S:.0f}s")
            self._set_status(f'The heatmap load did not finish within {LOAD_DEADLINE_S:.0f}s'
                             + ('; the last data stays shown.' if self._data is not None else '.')
                             + ' Use Refresh to try again.', error=True)
            if self._data is None:
                self._clear_chart()
        except Exception as e:  # noqa: BLE001 -- deliberate: every failure ends in the status line, never a blank card
            logger.error(f"Heatmap: load failed: {_why(e)}", exc_info=True)
            self._set_status(f'Could not load the heatmap: {_why(e)}', error=True)
            if self._data is None:
                self._clear_chart()
        finally:
            self._set_busy(False)
            timing.log()

    async def _run_loader(self, now, timing):
        """The loader in a worker thread, bounded by ``LOAD_DEADLINE_S``. While it runs, the step it reports through
        ``progress`` (the worker only stores the text) is copied to the status line every ``PROGRESS_POLL_S``."""
        progress_box = {'text': None}

        def progress(text):
            progress_box['text'] = text

        loop = asyncio.get_running_loop()
        ends = loop.time() + LOAD_DEADLINE_S
        work = asyncio.ensure_future(asyncio.to_thread(self._loader, self._account_id, now, timing, progress))
        shown = None
        while True:
            left = ends - loop.time()
            if left <= 0:
                work.cancel()                  # the worker thread finishes on its own; its result is dropped
                raise asyncio.TimeoutError
            done, _pending = await asyncio.wait({work}, timeout=min(PROGRESS_POLL_S, left))
            if done:
                return work.result()
            text = progress_box['text']
            if text and text != shown:
                shown = text
                self._set_status(text)

    def _clear_chart(self):
        try:
            self._chart_box.clear()
        except Exception as e:  # noqa: BLE001 -- the client went away
            logger.debug(f"Heatmap: chart not cleared ({type(e).__name__}: {e})")

    def _apply(self, data, now):
        self._data, self._now = data, now
        self._experts = hm.available_experts(data.lots, data.market)
        self._labels = hm.available_labels(data.lots, data.market.labels, data.market)
        self._state = hm.resolve_state(self._stored, self._experts, self._labels)
        self._syncing = True
        try:
            self._expert_sel.set_options(self._experts, value=list(self._state.experts))
            self._label_sel.set_options(self._labels, value=list(self._state.labels))
        finally:
            self._syncing = False
        self._summaries()
        gone_experts, gone_labels = hm.dropped_filters(self._stored, self._experts, self._labels)
        notes = []
        if gone_experts:
            notes.append(f"expert filter {', '.join(gone_experts)} not in this account"
                         + ('' if self._state.experts else ' (showing all experts)'))
        if gone_labels:
            notes.append(f"label filter {', '.join(gone_labels)} not in this account"
                         + ('' if self._state.labels else ' (showing all labels)'))
        self._filter_note.set_text(('Filter reset: ' + '; '.join(notes) + '.') if notes else '')
        self._updated.set_text(f"Updated {datetime.now().strftime('%H:%M:%S')}")
        self._draw()
        self._set_status('')

    def _reload(self):
        if self._loading:
            self._set_status('Already loading - the heatmap will update when this load finishes.')
            return
        self._set_status('Reloading...')
        self._task = asyncio.create_task(self._load())

    # ------------------------------------------------------------------ choices

    def _current_state(self) -> hm.HeatmapState:
        return hm.HeatmapState(timeframe=self._tf.value, metric=self._metric.value, size=self._size.value,
                               experts=tuple(self._expert_sel.value or ()), labels=tuple(self._label_sel.value or ()))

    def _on_change(self, _=None):
        if self._syncing:
            return
        self._state = self._current_state()
        self._summaries()
        stored = hm.merge_for_storage(self._state, self._stored, self._experts, self._labels)
        self._stored = stored
        _write_stored(self._key, stored)
        if self._data is not None:
            try:
                self._draw()
            except Exception as e:  # noqa: BLE001 -- deliberate: a failed redraw is shown, not swallowed
                logger.error(f"Heatmap: redraw failed: {_why(e)}", exc_info=True)
                self._set_status(f'Could not draw the heatmap: {_why(e)}', error=True)

    def _summaries(self):
        for sel, options, noun in ((self._expert_sel, self._experts, 'experts'),
                                   (self._label_sel, self._labels, 'labels')):
            chosen = len(sel.value or [])
            text = f'All {noun}' if not chosen else f'{chosen} of {len(options)} {noun}'
            sel.props(f'display-value="{text}"')

    # ------------------------------------------------------------------ drawing

    def _draw(self):
        view = hm.build_view(self._data.lots, self._data.market, self._state, self._now)
        self._chart_box.clear()
        with self._chart_box:
            if view.tiles:
                ui.echart(hm.chart_options(view)).classes('w-full').style(f'height: {self.CHART_HEIGHT}')
            else:
                ui.label('No open share positions in this scope.').classes('text-sm text-gray-500 py-8')
        self._legend_box.clear()
        with self._legend_box:
            for text, colour in hm.legend_stops(view.clamp):
                ui.label(text).style(f'background:{colour}; color:#fff; padding:2px 10px; font-size:11px;'
                                     'min-width:58px; text-align:center')
            ui.label('n/a').style(f'background:{hm.NA_COLOR}; color:#fff; padding:1px 10px; font-size:11px; '
                                  'margin-left:8px; border:1px dashed #64748b')
            if view.state.size == 'company':
                ui.label('no market cap').style(f'border:2px solid {hm.UNKNOWN_SIZE_BORDER}; color:#fff; '
                                                'padding:0 8px; font-size:11px; margin-left:8px')
        self._caption.set_text(hm.caption(view))
        self._issues_box.clear()
        if view.issues:
            with self._issues_box:
                ui.label(f'Not measured or estimated ({len({s for s, _ in view.issues})} symbols):').classes(
                    'text-xs text-orange-600')
                for symbol, why in view.issues:
                    ui.label(f'{symbol}: {why}').classes('text-xs text-gray-400')
        self._mismatch_box.clear()
        if view.quantity_mismatches:
            with self._mismatch_box:
                ui.label('Platform vs broker quantity differs (whole account, every expert):').classes(
                    'text-xs text-orange-600')
                for symbol, account_id, mine, theirs in view.quantity_mismatches:
                    ui.label(f'{symbol} (account {account_id}): platform {mine:g}, broker {theirs:g}').classes(
                        'text-xs text-gray-400')
