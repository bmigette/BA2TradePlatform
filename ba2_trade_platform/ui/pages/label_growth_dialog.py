"""The label growth simulator dialog of the Portfolio Allocation page (the chart icon on each label's header).

"If I had put $X into this label at the start of the period, split by its CURRENT shares, what would it be worth
today?" Four horizons side by side (YTD, 1, 3, 5 years), each as price only and as price + distributions received in
cash; a per-symbol breakdown and a line chart for the selected horizon.

The arithmetic is ``ui/utils/label_growth_sim.py`` (pure); the fetch is ``services/label_growth.py`` and runs ONCE per
opening, in ``asyncio.to_thread``; changing the amount only re-runs the arithmetic. READ-ONLY: nothing here writes to
the database or touches an order, and nothing on the page behind it changes. Every refusal or provider error is drawn
IN the dialog -- a failure while drawing included, never a spinner left turning -- and every message shown or logged
is redacted (``services.label_growth.redacted_reason``). Closing the dialog deletes it; a load that finishes after the
dialog (or the browser tab) is gone draws nothing. CSS: none of its own (inline styles and utility classes only), so nothing here can be dropped by the
page's after-await rule.
"""
import asyncio
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from nicegui import ui

from ...logger import logger
from ...services.label_growth import fetch_label_growth_inputs, redacted_reason, symbols_to_fetch
from ..utils import label_growth_sim as sim

MARKER_SIM_DIALOG = 'pf-sim-dialog'
MARKER_SIM_AMOUNT = 'pf-sim-amount'
MARKER_SIM_HORIZON_CARD = 'pf-sim-horizon'
MARKER_SIM_ERROR = 'pf-sim-error'
MARKER_SIM_NOTE = 'pf-sim-note'
MARKER_SIM_TABLE = 'pf-sim-table'
MARKER_SIM_CHART = 'pf-sim-chart'

DIALOG_TITLE_FMT = 'Growth simulator — {label}'
INTRO_TEXT = ('What the amount below would be worth today had it been put into this label at the start of each '
              'period, split by the label’s CURRENT effective shares and held without rebalancing.')
DEFAULT_HORIZON = '1y'
CARD_STYLE = 'width: 1150px; max-width: 95vw; max-height: 90vh; background: #1a1f2e;'
CHART_HEIGHT_PX = 340
_MUTED = 'color: #a0aec0;'
_WARN = 'color: #fbbf24;'
_ERROR = 'color: #ff6b6b;'

TABLE_COLUMNS = [
    {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'align': 'left'},
    {'name': 'share', 'label': 'Share', 'field': 'share', 'align': 'right'},
    {'name': 'status', 'label': 'Status', 'field': 'status', 'align': 'left'},
    {'name': 'invested', 'label': 'Invested', 'field': 'invested', 'align': 'right'},
    {'name': 'start', 'label': 'Start price', 'field': 'start', 'align': 'right'},
    {'name': 'last', 'label': 'Last price', 'field': 'last', 'align': 'right'},
    {'name': 'distributions', 'label': 'Distributions', 'field': 'distributions', 'align': 'right'},
    {'name': 'price_value', 'label': 'Value (price)', 'field': 'price_value', 'align': 'right'},
    {'name': 'price_gain_pct', 'label': 'Gain (price)', 'field': 'price_gain_pct', 'align': 'right'},
    {'name': 'total_value', 'label': 'Value + distr.', 'field': 'total_value', 'align': 'right'},
    {'name': 'total_gain_pct', 'label': 'Gain + distr.', 'field': 'total_gain_pct', 'align': 'right'},
    {'name': 'note', 'label': 'Note', 'field': 'note', 'align': 'left',
     'style': 'white-space: normal; min-width: 14rem;'},
]

SOURCE_TEXT = {
    'fmp': 'FMP daily close (split-adjusted, not dividend-adjusted)',
    'alpaca': 'the account’s Alpaca daily history (split-adjusted, not dividend-adjusted)',
}


def outside_split_text(excluded: Sequence[str], unset: Sequence[str]) -> str:
    """The label's symbols that are NOT in the simulated split, and why. Pure; '' when there are none."""
    parts = []
    if excluded:
        parts.append(f"{', '.join(excluded)} (excluded on this page)")
    if unset:
        parts.append(f"{', '.join(unset)} (no share set)")
    return ('Not in the split: ' + '; '.join(parts) + '.') if parts else ''


class LabelGrowthDialog:
    """Built empty, filled by :meth:`load` (the blocking fetch in ``asyncio.to_thread``), redrawn with
    ``container.clear()`` on an amount or horizon change."""

    def __init__(self, account_id: int, label: str, shares: Mapping[str, float], *, api_key: str, as_of: date,
                 excluded: Sequence[str] = (), unset: Sequence[str] = (),
                 fetch: Optional[Callable[..., Any]] = None):
        self.account_id = account_id
        self.label = label
        self.shares = {str(k).strip().upper(): float(v) for k, v in shares.items()}
        self.api_key = api_key
        self.as_of = as_of
        self.excluded = list(excluded)
        self.unset = list(unset)
        self._fetch = fetch or fetch_label_growth_inputs
        self.amount: Any = sim.DEFAULT_AMOUNT
        self.horizon = DEFAULT_HORIZON
        self.inputs = None
        self.result: Optional[sim.SimulationResult] = None
        self.load_task: Optional[asyncio.Task] = None
        self._build()

    # -- layout ------------------------------------------------------------------------------------------------
    def _build(self) -> None:
        self.dialog = ui.dialog().mark(MARKER_SIM_DIALOG)
        # Deleted on close (the close button, Esc or a click outside): each click builds a fresh one, and a closed
        # dialog left in the tree would pile up for the life of the page.
        self.dialog.on_value_change(lambda e: None if e.value else self._dispose())
        with self.dialog:
            with ui.card().classes('flex flex-col flex-nowrap min-h-0 overflow-hidden').style(CARD_STYLE):
                with ui.row().classes('w-full items-center justify-between p-2 shrink-0'):
                    ui.label(DIALOG_TITLE_FMT.format(label=self.label)).classes('text-lg font-bold')
                    ui.button(icon='close', on_click=self.close).props('flat round')
                with ui.column().classes('flex-grow w-full min-h-0 overflow-y-auto overflow-x-hidden gap-2 p-2'):
                    ui.label(INTRO_TEXT).classes('text-sm').style(_MUTED)
                    with ui.row().classes('items-center gap-3'):
                        self.amount_input = ui.number(
                            label='Amount ($)', value=sim.DEFAULT_AMOUNT, min=0, step=1000, format='%.2f',
                            on_change=lambda e: self._amount_changed(e.value),
                        ).props('dense outlined debounce=400').classes('w-48').mark(MARKER_SIM_AMOUNT)
                        outside = outside_split_text(self.excluded, self.unset)
                        if outside:
                            ui.label(outside).classes('text-xs').style(_MUTED)
                    self.body = ui.column().classes('w-full gap-3')
        with self.body:
            ui.spinner(size='lg')
            ui.label('Loading prices and distributions…').style(_MUTED)

    def open(self) -> None:
        self.dialog.open()

    def close(self) -> None:
        self.dialog.close()

    def _dispose(self) -> None:
        if not self.dialog.is_deleted:
            self.dialog.delete()

    def _gone(self) -> bool:
        """The dialog (or the whole client) was deleted while the fetch ran: nothing left to draw into."""
        return self.dialog.is_deleted or self.body.is_deleted

    # -- data ------------------------------------------------------------------------------------------------------
    async def load(self) -> None:
        """Validate the split, fetch off the event loop, then draw. Never raises to the caller: every failure is
        drawn in the dialog (and logged)."""
        try:
            sim.simulate_label_growth(sim.DEFAULT_AMOUNT, self.shares, {}, {}, as_of=self.as_of, horizons=('ytd',))
        except ValueError as e:                          # shares over 100 / negative: nothing to fetch for
            self._render_error('The simulation cannot run on this split.', _message(e))
            return
        try:
            inputs = await asyncio.to_thread(self._fetch, self.account_id, symbols_to_fetch(self.shares),
                                             as_of=self.as_of, api_key=self.api_key)
        except Exception as e:  # noqa: BLE001 -- any provider failure is SHOWN in the dialog, and logged
            reason = redacted_reason(e)
            logger.error(f"label growth simulator: loading {self.label} failed: {reason}")
            if not self._gone():
                self._render_error('The prices or distributions could not be loaded.', reason)
            return
        if self._gone():
            logger.info(f"label growth simulator: {self.label} closed before its data arrived; nothing drawn")
            return
        self.inputs = inputs
        self._recompute()

    def _amount_changed(self, value) -> None:
        self.amount = value
        if self.inputs is not None:
            self._recompute()

    def _select(self, horizon: str) -> None:
        self.horizon = horizon
        if self.result is not None:
            self._draw_safely()

    def _recompute(self) -> None:
        try:
            self.result = sim.simulate_label_growth(self.amount, self.shares, self.inputs.closes,
                                                    self.inputs.distributions, as_of=self.inputs.as_of)
        except ValueError as e:
            self.result = None
            self._render_error('Enter a positive amount.' if 'amount' in str(e).lower()
                               else 'The simulation cannot run on this split.', _message(e))
            return
        except Exception as e:  # noqa: BLE001 -- an arithmetic defect is SHOWN, never a frozen spinner
            self.result = None
            self._failed('The simulation failed.', e)
            return
        self._draw_safely()

    def _draw_safely(self) -> None:
        """``_render``, with any failure drawn as an error card instead of a half-drawn body or a spinner."""
        if self._gone():
            return
        try:
            self._render()
        except Exception as e:  # noqa: BLE001 -- shown in the dialog and logged; nothing is retried
            self._failed('The results could not be drawn.', e)

    def _failed(self, headline: str, exc: BaseException) -> None:
        reason = redacted_reason(exc)
        logger.error(f"label growth simulator: {self.label}: {headline} {reason}")
        if not self._gone():
            self._render_error(headline, reason)

    # -- drawing ---------------------------------------------------------------------------------------------------
    def _render_error(self, headline: str, detail: str) -> None:
        self.body.clear()
        with self.body:
            with ui.card().classes('w-full p-4').mark(MARKER_SIM_ERROR):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('error_outline', color='negative')
                    ui.label(headline).classes('text-base')
                ui.label(detail).classes('text-sm').style(_ERROR)

    def _render(self) -> None:
        self.body.clear()
        res = self.result
        with self.body:
            with ui.row().classes('w-full gap-3 no-wrap items-stretch'):
                for key, h in res.horizons.items():
                    self._horizon_card(key, h)
            h = res.horizons[self.horizon]
            ui.toggle({k: v.name for k, v in res.horizons.items()}, value=self.horizon,
                      on_change=lambda e: self._select(e.value)).props('dense no-caps')
            ui.label(f"{h.name}: from {h.start} (base = last close on or before it) to "
                     f"{h.end or 'n/a'}.").classes('text-sm')
            for note in h.notes:
                ui.label(note).classes('text-sm').style(_WARN).mark(MARKER_SIM_NOTE)
            ui.echart(sim.growth_chart_options(h)).style(
                f'width: 100%; height: {CHART_HEIGHT_PX}px;').mark(MARKER_SIM_CHART)
            ui.table(columns=TABLE_COLUMNS, rows=sim.table_rows(h), row_key='symbol') \
                .props('dense flat').classes('w-full').mark(MARKER_SIM_TABLE)
            source = SOURCE_TEXT[self.inputs.price_source] if self.inputs.price_source in SOURCE_TEXT \
                else self.inputs.price_source
            ui.label(f"Prices: {source}. Distributions: FMP per-share amounts adjusted for later splits "
                     f"(adjDividend), counted on their ex-date as cash, not reinvested. Buy and hold from the start: "
                     f"no rebalancing, no fees or taxes.").classes('text-xs').style(_MUTED)

    def _horizon_card(self, key: str, h: sim.HorizonResult) -> None:
        selected = key == self.horizon
        border = 'border: 1px solid #3c8fd6;' if selected else 'border: 1px solid rgba(255,255,255,0.12);'
        with ui.card().classes('flex-1 p-3 cursor-pointer gap-1').style(border) \
                .mark(MARKER_SIM_HORIZON_CARD).on('click', lambda _e=None, k=key: self._select(k)):
            ui.label(h.name).classes('text-base font-bold')
            ui.label(f"from {h.start}").classes('text-xs').style(_MUTED)
            ui.label('Price only').classes('text-xs').style(_MUTED)
            ui.label(sim.fmt_money(h.price_value)).classes('text-lg font-medium')
            ui.label(f"{sim.fmt_signed_money(h.price_gain)} ({sim.fmt_signed_pct(h.price_gain_pct)})") \
                .classes('text-sm').style(f'color: {sim.gain_color(h.price_gain)};')
            ui.label('Price + distributions').classes('text-xs pt-1').style(_MUTED)
            if h.total_value is None:
                ui.label('unknown: distributions could not be measured').classes('text-sm').style(_WARN)
            else:
                ui.label(sim.fmt_money(h.total_value)).classes('text-lg font-medium')
                ui.label(f"{sim.fmt_signed_money(h.total_gain)} ({sim.fmt_signed_pct(h.total_gain_pct)})") \
                    .classes('text-sm').style(f'color: {sim.gain_color(h.total_gain)};')
                ui.label(f"of which distributions {sim.fmt_money(h.distributions)}").classes('text-xs').style(_MUTED)
            if h.late_symbols:
                ui.label(f"{len(h.late_symbols)} of {h.invested_count} symbols did not exist for the whole period") \
                    .classes('text-xs').style(_WARN)
            if h.no_history_symbols:
                ui.label(f"{len(h.no_history_symbols)} unmeasurable (held as cash)").classes('text-xs').style(_WARN)
            if h.gap_symbols:
                ui.label(f"{len(h.gap_symbols)} with a data gap at the start").classes('text-xs').style(_WARN)


def open_label_growth(account_id: int, label: str, shares: Mapping[str, float], *, api_key: str, as_of: date,
                      excluded: Sequence[str] = (), unset: Sequence[str] = (),
                      fetch: Optional[Callable[..., Any]] = None) -> LabelGrowthDialog:
    """Open the simulator for one label. ``shares`` = ``{SYMBOL: effective share of the label %}``. Must run on the
    event loop (a NiceGUI click handler does); returns the dialog so a caller can await ``load_task``."""
    dialog = LabelGrowthDialog(account_id, label, shares, api_key=api_key, as_of=as_of, excluded=excluded,
                               unset=unset, fetch=fetch)
    dialog.open()
    dialog.load_task = asyncio.create_task(dialog.load())
    dialog.load_task.add_done_callback(_retrieve)
    return dialog


def _message(exc: BaseException) -> str:
    """A refusal's own sentence, redacted, without the exception type in front."""
    return redacted_reason(exc).split(': ', 1)[-1]


def _retrieve(task: asyncio.Task) -> None:
    """``load`` never raises by design; should that ever break, the exception is logged here instead of being left as
    an unretrieved task exception (the click handler that started the task does not await it)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"label growth simulator: load task failed: {redacted_reason(exc)}")
