"""The REBALANCE TESTER dialog of the Portfolio Allocation page (the rebalance icon on each label's header, and the
"Rebalance tester" button under the label list for the whole account).

"Would rebalancing back to the target weights every 1 / 3 / 6 / 12 months have beaten simply holding?" -- for one
label or the whole account, optionally with a tilt toward / against the members that moved. Read-only: it places no
order, schedules nothing and writes nothing; nothing on the page behind it changes.

The arithmetic is ``ui/utils/rebalance_tester_sim.py`` (pure). The data is the growth simulator's, read symbol by
symbol by ``services.rebalance_data.fetch_rebalance_inputs`` (the same per-symbol reads: the account's own history
source -- FMP close / Alpaca store, split-adjusted, not dividend-adjusted -- and FMP ``adjDividend``; every FMP request
through ``fmp_common.fmp_http_get``'s shared backoff). NO DEADLINE: a whole account downloads to completion behind a
determinate progress bar ("n of N", the current symbol) with a Cancel that stops at the next symbol. Symbols already
downloaded in this dialog stay cached: switching scope, Resume after a Cancel, or Retry after a failure downloads only
what is missing. A symbol that FAILED is named and the scope is refused (no partial result). Changing the lookback,
cost or tilt only re-runs the arithmetic. The worker thread only RECORDS progress; a timer in the dialog paints it. Every refusal or error is drawn IN the dialog, never a spinner
left turning; a closed dialog is deleted and a fetch that lands after it is gone draws nothing. CSS: none of its own
(inline styles and utility classes only), so nothing here can be dropped by the page's after-await rule.
"""
import asyncio
import threading
from datetime import date
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from nicegui import ui

from ...logger import logger
from ...services.label_growth import redacted_reason, symbols_to_fetch
from ...services.rebalance_data import fetch_rebalance_inputs
from ..utils import rebalance_tester_sim as sim

MARKER_REBAL_DIALOG = 'pf-rebal-dialog'
MARKER_REBAL_SCOPE = 'pf-rebal-scope'
MARKER_REBAL_LOOKBACK = 'pf-rebal-lookback'
MARKER_REBAL_COST = 'pf-rebal-cost'
MARKER_REBAL_K_WIN = 'pf-rebal-k-win'
MARKER_REBAL_K_LOSE = 'pf-rebal-k-lose'
MARKER_REBAL_ERROR = 'pf-rebal-error'
MARKER_REBAL_REFUSAL = 'pf-rebal-refusal'
MARKER_REBAL_WINDOW = 'pf-rebal-window'
MARKER_REBAL_NOTE = 'pf-rebal-note'
MARKER_REBAL_CHART = 'pf-rebal-chart'
MARKER_REBAL_TABLE = 'pf-rebal-table'
MARKER_REBAL_PROGRESS = 'pf-rebal-progress'
MARKER_REBAL_PROGRESS_TEXT = 'pf-rebal-progress-text'
MARKER_REBAL_CANCEL = 'pf-rebal-cancel'
MARKER_REBAL_FAILED = 'pf-rebal-failed'
MARKER_REBAL_CANCELLED = 'pf-rebal-cancelled'
MARKER_REBAL_RETRY = 'pf-rebal-retry'
#: Seconds between paints of the download progress.
PROGRESS_PAINT_S = 0.25

#: The scope key of the whole account (a label can never be named with a leading NUL).
ACCOUNT_SCOPE = '\x00account'
ACCOUNT_SCOPE_NAME = 'Whole account (label target × in-label weight)'

DIALOG_TITLE = 'Rebalance tester'
INTRO_TEXT = ('Would rebalancing back to the target weights every 1, 3, 6 or 12 months have beaten simply holding? '
              'A what-if on past prices: it places no orders and changes nothing.')
CARD_STYLE = 'width: 1200px; max-width: 95vw; max-height: 90vh; background: #1a1f2e;'
CHART_HEIGHT_PX = 340
_MUTED = 'color: #a0aec0;'
_WARN = 'color: #fbbf24;'
_ERROR = 'color: #ff6b6b;'

TILT_WIN_LABEL = 'Tilt k, winners'
TILT_LOSE_LABEL = 'Tilt k, losers'
TILT_TOOLTIP = ('At each rebalance a member\'s weight is target × clip(1 + k × r, 0.25, 3), normalised, where r is its '
                'return since the previous rebalance. "Winners" (r > 0) use the first k, "losers" (r < 0) the second. '
                'k > 0 follows the move (more to a winner, less to a loser); k < 0 leans against it; 0 = plain '
                'rebalance to target.')

TABLE_COLUMNS = [
    {'name': 'variant', 'label': 'Variant', 'field': 'variant', 'align': 'left'},
    {'name': 'total', 'label': 'Total return', 'field': 'total', 'align': 'right'},
    {'name': 'annualised', 'label': 'Annualised', 'field': 'annualised', 'align': 'right'},
    {'name': 'range', 'label': 'Annualised range over start months', 'field': 'range', 'align': 'right'},
    {'name': 'drawdown', 'label': 'Max drawdown', 'field': 'drawdown', 'align': 'right'},
    {'name': 'turnover', 'label': 'Turnover / year', 'field': 'turnover', 'align': 'right'},
    {'name': 'rebalances', 'label': 'Rebalances', 'field': 'rebalances', 'align': 'right'},
    {'name': 'd_total', 'label': 'Δ total vs hold', 'field': 'd_total', 'align': 'right'},
    {'name': 'd_annualised', 'label': 'Δ annualised vs hold', 'field': 'd_annualised', 'align': 'right'},
    {'name': 'd_drawdown', 'label': 'Δ drawdown vs hold', 'field': 'd_drawdown', 'align': 'right'},
]

SOURCE_TEXT = {
    'fmp': 'FMP daily close (split-adjusted, not dividend-adjusted)',
    'alpaca': 'the account’s Alpaca daily history (split-adjusted, not dividend-adjusted)',
}


class RebalanceTesterDialog:
    """Built empty; :meth:`run` fetches what the scope still lacks (in ``asyncio.to_thread``) and draws. A parameter
    change re-runs the arithmetic only."""

    def __init__(self, account_id: int, label_shares: Mapping[str, Mapping[str, float]],
                 label_targets: Mapping[str, float], *, scope: str, api_key: str, as_of: date,
                 outside: Optional[Mapping[str, str]] = None, fetch: Optional[Callable[..., Any]] = None):
        self.account_id = account_id
        self.label_shares = {lbl: {str(k).strip().upper(): float(v) for k, v in shares.items()}
                             for lbl, shares in label_shares.items()}
        self.label_targets = dict(label_targets)
        if scope != ACCOUNT_SCOPE and scope not in self.label_shares:
            raise ValueError(f"unknown scope {scope!r}")
        self.scope = scope
        self.api_key = api_key
        self.as_of = as_of
        self.outside = dict(outside or {})
        self._fetch = fetch or fetch_rebalance_inputs
        #: The running download's Cancel; set by the Cancel button, by a newer download, and by closing the dialog.
        self._cancel: Optional[threading.Event] = None
        self.params = sim.RebalanceParams()
        self.closes: Dict[str, Dict[str, float]] = {}
        self.distributions: Dict[str, Any] = {}
        self.fetched: set = set()
        self.price_source: Optional[str] = None
        self.result: Optional[sim.RebalanceTestResult] = None
        self.load_task: Optional[asyncio.Task] = None
        self._generation = 0
        self._build()

    # -- scopes ----------------------------------------------------------------------------------------------------
    def scope_options(self) -> Dict[str, str]:
        return {ACCOUNT_SCOPE: ACCOUNT_SCOPE_NAME, **{lbl: lbl for lbl in self.label_shares}}

    def scope_weights(self, scope: str):
        """``(weights, notes)`` of a scope: a label's members at their in-label shares, or the account's symbols at
        label target × in-label share."""
        if scope == ACCOUNT_SCOPE:
            weights, skipped = sim.account_weights(self.label_targets, self.label_shares)
            notes = [f"Label(s) with a target but no member share, not represented: {', '.join(skipped)}."] \
                if skipped else []
            notes += [f"{lbl}: {text}" for lbl, text in self.outside.items() if text]
            return weights, notes
        text = self.outside[scope] if scope in self.outside else ''
        return dict(self.label_shares[scope]), ([text] if text else [])

    # -- layout ----------------------------------------------------------------------------------------------------
    def _build(self) -> None:
        self.dialog = ui.dialog().mark(MARKER_REBAL_DIALOG)
        self.dialog.on_value_change(lambda e: None if e.value else self._dispose())
        p = self.params
        with self.dialog:
            with ui.card().classes('flex flex-col flex-nowrap min-h-0 overflow-hidden').style(CARD_STYLE):
                with ui.row().classes('w-full items-center justify-between p-2 shrink-0'):
                    ui.label(DIALOG_TITLE).classes('text-lg font-bold')
                    ui.button(icon='close', on_click=self.close).props('flat round')
                with ui.column().classes('flex-grow w-full min-h-0 overflow-y-auto overflow-x-hidden gap-2 p-2'):
                    ui.label(INTRO_TEXT).classes('text-sm').style(_MUTED)
                    with ui.row().classes('items-center gap-3 flex-wrap'):
                        self.scope_select = ui.select(
                            self.scope_options(), value=self.scope, label='Scope',
                            on_change=lambda e: self._scope_changed(e.value),
                        ).props('dense outlined').classes('w-72').mark(MARKER_REBAL_SCOPE)
                        self.lookback_toggle = ui.toggle(
                            {y: f'{y}y' for y in sim.LOOKBACK_YEARS}, value=p.lookback_years,
                            on_change=lambda e: self._param_changed(lookback_years=e.value),
                        ).props('dense no-caps').mark(MARKER_REBAL_LOOKBACK).tooltip('Lookback (maximum window)')
                        self.cost_input = ui.number(
                            label='Cost (bps of traded value)', value=p.cost_bps, min=0, step=1, format='%.1f',
                            on_change=lambda e: self._param_changed(cost_bps=e.value),
                        ).props('dense outlined debounce=400').classes('w-52').mark(MARKER_REBAL_COST)
                        self.k_win_input = ui.number(
                            label=TILT_WIN_LABEL, value=p.k_winners, step=0.1, format='%.2f',
                            on_change=lambda e: self._param_changed(k_winners=e.value),
                        ).props('dense outlined debounce=400').classes('w-40').mark(MARKER_REBAL_K_WIN)
                        self.k_win_input.tooltip(TILT_TOOLTIP)
                        self.k_lose_input = ui.number(
                            label=TILT_LOSE_LABEL, value=p.k_losers, step=0.1, format='%.2f',
                            on_change=lambda e: self._param_changed(k_losers=e.value),
                        ).props('dense outlined debounce=400').classes('w-40').mark(MARKER_REBAL_K_LOSE)
                        self.k_lose_input.tooltip(TILT_TOOLTIP)
                    self.body = ui.column().classes('w-full gap-3')
        self._spinner()

    def _spinner(self) -> None:
        self.body.clear()
        with self.body:
            ui.spinner(size='lg')
            ui.label('Loading prices and distributions…').style(_MUTED)

    def open(self) -> None:
        self.dialog.open()

    def close(self) -> None:
        self.dialog.close()

    def _dispose(self) -> None:
        if self._cancel is not None:
            self._cancel.set()              # a closed dialog stops its download at the next symbol
        if not self.dialog.is_deleted:
            self.dialog.delete()

    def _gone(self) -> bool:
        return self.dialog.is_deleted or self.body.is_deleted

    # -- data ------------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        """Download whatever the current scope still lacks (with progress and Cancel), then compute and draw. Never
        raises: every failure is drawn in the dialog (and logged). A run superseded by a later scope change draws
        nothing, but what it downloaded is kept."""
        self._generation += 1
        generation = self._generation
        scope = self.scope
        try:
            weights, _notes = self.scope_weights(scope)
            missing = [s for s in symbols_to_fetch(weights) if s not in self.fetched]
        except ValueError as e:
            self._render_error('The test cannot run on this split.', _message(e))
            return
        if missing:
            if self._cancel is not None:
                self._cancel.set()          # an older scope's download stops at its next symbol
            cancel = threading.Event()
            self._cancel = cancel
            sink = {'done': 0, 'total': len(missing), 'symbol': ''}

            def _record(done: int, total: int, symbol: str) -> None:
                sink['done'], sink['total'], sink['symbol'] = done, total, symbol

            self._render_progress(sink, cancel)
            try:
                fetched = await asyncio.to_thread(self._fetch, self.account_id, missing, as_of=self.as_of,
                                                  api_key=self.api_key, on_progress=_record, cancel=cancel)
            except Exception as e:  # noqa: BLE001 -- any provider failure is SHOWN in the dialog, and logged
                reason = redacted_reason(e)
                logger.error(f"rebalance tester: loading {len(missing)} symbol(s) for account {self.account_id} "
                             f"failed: {reason}")
                if not self._gone() and generation == self._generation:
                    self._render_error('The prices or distributions could not be loaded.', reason)
                return
            finally:
                if self._cancel is cancel:
                    self._cancel = None
            # Keep every COMPLETE symbol, even from a superseded or cancelled run; failed ones are never cached.
            self.closes.update(fetched.closes)
            self.distributions.update(fetched.distributions)
            self.fetched.update(fetched.completed)
            self.price_source = fetched.price_source
            if self._gone():
                logger.info("rebalance tester: closed before its data arrived; nothing drawn")
                return
            if generation != self._generation:
                return
            if fetched.failed:
                self._render_failed(fetched)
                return
            if fetched.cancelled:
                self._render_cancelled(fetched)
                return
            wanted = symbols_to_fetch(weights)
            if wanted and not any(s in self.closes for s in wanted):
                # Every symbol empty is a source problem far more often than a scope of unlisted symbols.
                self._render_error('The price source returned no history.',
                                   f"The price source ({fetched.price_source}) returned no history for any of "
                                   f"{', '.join(wanted)}; see the log.")
                return
        if self._gone() or generation != self._generation:
            return
        self._recompute()

    def _start_run(self) -> None:
        self.load_task = asyncio.create_task(self.run())
        self.load_task.add_done_callback(_retrieve)

    def _scope_changed(self, value) -> None:
        if value is None or value == self.scope:
            return
        self.scope = value
        self._start_run()

    def _param_changed(self, **changes) -> None:
        fields = {'lookback_years': self.params.lookback_years, 'cost_bps': self.params.cost_bps,
                  'k_winners': self.params.k_winners, 'k_losers': self.params.k_losers}
        fields.update(changes)
        self.params = sim.RebalanceParams(**fields)
        # Only on data the scope already has: while its fetch runs, the run that ends it computes with these params.
        if self._has_scope_data():
            self._recompute()

    def _has_scope_data(self) -> bool:
        try:
            weights, _ = self.scope_weights(self.scope)
        except ValueError:
            return True                     # let _recompute draw the refusal
        return all(s in self.fetched for s in symbols_to_fetch(weights))

    def _recompute(self) -> None:
        if self._gone():
            return
        try:
            weights, notes = self.scope_weights(self.scope)
            self.result = sim.run_rebalance_test(weights, self.closes, self.distributions, self.params,
                                                 as_of=self.as_of)
        except sim.RebalanceInputError as e:
            self.result = None
            self._render_error('Check the inputs.', _message(e))
            return
        except Exception as e:  # noqa: BLE001 -- an arithmetic defect is SHOWN, never a frozen spinner
            self.result = None
            self._failed('The test failed.', e)
            return
        try:
            self._render(notes)
        except Exception as e:  # noqa: BLE001 -- shown in the dialog and logged; nothing is retried
            self._failed('The results could not be drawn.', e)

    def _failed(self, headline: str, exc: BaseException) -> None:
        reason = redacted_reason(exc)
        logger.error(f"rebalance tester: {headline} {reason}")
        if not self._gone():
            self._render_error(headline, reason)

    # -- drawing ---------------------------------------------------------------------------------------------------
    def _render_error(self, headline: str, detail: str, marker: str = MARKER_REBAL_ERROR) -> None:
        self.body.clear()
        with self.body:
            with ui.card().classes('w-full p-4').mark(marker):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('error_outline', color='negative')
                    ui.label(headline).classes('text-base')
                ui.label(detail).classes('text-sm').style(_ERROR)

    def _render_progress(self, sink: Dict[str, Any], cancel: threading.Event) -> None:
        """The download's progress: "n of N", the current symbol, a determinate bar and Cancel. Painted by a timer
        from ``sink`` (the worker thread only records into it: an element touched from the thread has no client)."""
        self.body.clear()
        with self.body:
            text = ui.label('Starting the download…').classes('text-sm').mark(MARKER_REBAL_PROGRESS_TEXT)
            bar = ui.linear_progress(value=0.0, show_value=False, size='8px').classes('w-full') \
                .mark(MARKER_REBAL_PROGRESS)
            note = ui.label('Prices and distributions are downloaded one symbol at a time; nothing is ordered.') \
                .classes('text-xs').style(_MUTED)

            def _cancel() -> None:
                cancel.set()
                button.disable()
                note.set_text('Cancelling after the current symbol… what is downloaded stays loaded.')

            button = ui.button('Cancel', icon='stop', on_click=_cancel).props('outline dense') \
                .mark(MARKER_REBAL_CANCEL)

            def _paint() -> None:
                text.set_text(progress_text(sink['done'], sink['total'], sink['symbol']))
                bar.set_value(progress_fraction(sink['done'], sink['total']))

            ui.timer(PROGRESS_PAINT_S, _paint)

    def _render_failed(self, fetched) -> None:
        """Some symbols failed after the shared retries: name them and refuse the scope; Retry downloads only them."""
        names = sorted(fetched.failed)
        self.body.clear()
        with self.body:
            with ui.card().classes('w-full p-4').mark(MARKER_REBAL_FAILED):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('error_outline', color='negative')
                    ui.label(f"{len(names)} of {fetched.total} symbol(s) could not be downloaded, so this scope "
                             f"has no result.").classes('text-base')
                for sym in names:
                    ui.label(f"{sym}: {fetched.failed[sym]}").classes('text-sm').style(_ERROR)
                ui.label(f"The {len(fetched.completed)} downloaded symbol(s) are kept; Retry downloads only the "
                         f"failed ones.").classes('text-xs').style(_MUTED)
                ui.button('Retry', icon='refresh', on_click=self._start_run).props('outline dense') \
                    .mark(MARKER_REBAL_RETRY)

    def _render_cancelled(self, fetched) -> None:
        self.body.clear()
        with self.body:
            with ui.card().classes('w-full p-4').mark(MARKER_REBAL_CANCELLED):
                ui.label(f"Download cancelled: {len(fetched.completed)} of {fetched.total} symbol(s) downloaded "
                         f"and kept.").classes('text-base')
                ui.button('Resume', icon='play_arrow', on_click=self._start_run).props('outline dense') \
                    .mark(MARKER_REBAL_RETRY)

    def _render(self, scope_notes: Sequence[str]) -> None:
        res = self.result
        self.body.clear()
        with self.body:
            ui.label(sim.window_text(res.window)).classes('text-sm').mark(MARKER_REBAL_WINDOW)
            for note in list(scope_notes) + list(res.notes):
                ui.label(note).classes('text-sm').style(_WARN).mark(MARKER_REBAL_NOTE)
            if res.refusal:
                with ui.card().classes('w-full p-4').mark(MARKER_REBAL_REFUSAL):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('block', color='warning')
                        ui.label('No result for this scope.').classes('text-base')
                    ui.label(res.refusal).classes('text-sm').style(_WARN)
                return
            p = res.params
            tilt = ('no tilt (plain rebalance to target)' if p.k_winners == 0 and p.k_losers == 0
                    else f"tilt k = {p.k_winners:+.2f} for winners, {p.k_losers:+.2f} for losers")
            ui.label(f"Cost {p.cost_bps:g} bps of traded value per rebalance; {tilt}. Chart: every frequency from "
                     f"its first possible month-end (offset 0); table: mean over every start month.") \
                .classes('text-xs').style(_MUTED)
            ui.echart(sim.chart_options(res)).style(f'width: 100%; height: {CHART_HEIGHT_PX}px;') \
                .mark(MARKER_REBAL_CHART)
            ui.table(columns=TABLE_COLUMNS, rows=sim.table_rows(res), row_key='variant') \
                .props('dense flat').classes('w-full').mark(MARKER_REBAL_TABLE)
            source = SOURCE_TEXT[self.price_source] if self.price_source in SOURCE_TEXT else self.price_source
            ui.label(f"Prices: {source}. Distributions: FMP per-share amounts adjusted for later splits (adjDividend), "
                     f"REINVESTED on their ex-date (total return). Rebalances at the last session of a month; "
                     f"turnover counts buys and sells. Drawdown Δ: positive = shallower than hold. No taxes, no "
                     f"spreads beyond the cost above.").classes('text-xs').style(_MUTED)


def progress_text(done: int, total: int, symbol: str) -> str:
    """ "Downloading ...: 12 of 90 symbols — AAPL" (``done`` = symbols finished; the current one is ``done + 1``)."""
    current = f" — {symbol}" if symbol else ''
    return f"Downloading prices and distributions: {min(done + 1, total)} of {total} symbols{current}"


def progress_fraction(done: int, total: int) -> float:
    return 0.0 if total <= 0 else min(1.0, done / total)


def open_rebalance_tester(account_id: int, label_shares: Mapping[str, Mapping[str, float]],
                          label_targets: Mapping[str, float], *, scope: str, api_key: str, as_of: date,
                          outside: Optional[Mapping[str, str]] = None,
                          fetch: Optional[Callable[..., Any]] = None) -> RebalanceTesterDialog:
    """Open the tester. ``label_shares`` = ``{label: {SYMBOL: effective share %}}``, ``label_targets`` =
    ``{label: target %}``, ``scope`` = a label or ``ACCOUNT_SCOPE``. Must run on the event loop (a NiceGUI click
    handler does); returns the dialog so a caller can await ``load_task``."""
    dialog = RebalanceTesterDialog(account_id, label_shares, label_targets, scope=scope, api_key=api_key,
                                   as_of=as_of, outside=outside, fetch=fetch)
    dialog.open()
    dialog._start_run()
    return dialog


def _message(exc: BaseException) -> str:
    return redacted_reason(exc).split(': ', 1)[-1]


def _retrieve(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"rebalance tester: load task failed: {redacted_reason(exc)}")
