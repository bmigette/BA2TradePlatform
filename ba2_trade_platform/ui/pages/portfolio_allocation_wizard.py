"""Portfolio Allocation wizard: steps, dry-run dialog, income panel, outcome table.

Section G owns this module. The allocation PAGE
(``ui/pages/portfolio_allocation.py``) renders the label/symbol editor and calls
``open_allocation_steps()``, ``open_allocation_wizard()`` and
``render_income_panel()`` from here.

This module only DRAWS. Every decision lives in
``ba2_common.core.portfolio_allocation`` (pure) or in
``core.portfolio_allocation_service`` (live), both of which are unit-tested
without NiceGUI. The one exception is the wizard CONSTRUCTOR, which decides which
rows start ticked; that is reachable without a client context and is pinned in
``tests/test_portfolio_allocation_wizard_ui.py``.

Valid ``ui.notify`` types are 'positive' | 'negative' | 'warning' | 'info' --
'error' is not one of them (settings.py gets this wrong; do not copy it).
"""
from typing import Callable, Dict, List, Optional

from nicegui import ui

from ...core.portfolio_allocation import (
    AllocationPlan,
    BaseSnapshot,
    dry_run_rows,
    filter_plan_rows,
    summarise_plan,
)
from ...logger import logger

#: Shown above the dry-run table whenever any row will be sent as a FRACTIONAL
#: order. Both brokers refuse a fractional equity LIMIT order (TastyTrade
#: ``fractional_market_orders_only``; Alpaca floors a fractional non-market order
#: to whole shares), so a fractional row is a market order by construction and
#: inherits every market-order constraint. Stated rather than enforced: the
#: market-hours gate is its own piece of work.
FRACTIONAL_IS_MARKET_ONLY_NOTE = (
    'Fractional rows are sent as MARKET orders - the broker accepts no other '
    'order type for a fractional quantity.')

#: Shown always. Deliberately does NOT promise queueing: an opening market order
#: can be refused outright outside regular trading hours
#: (``tif_no_after_hours_opening_market_orders``), not merely held.
MARKET_ORDER_TIMING_NOTE = (
    'Market orders fill at the fill price, not at the prices shown. Outside '
    'regular trading hours the broker may refuse or defer them.')

#: NiceGUI marker on the dry-run table's "Order" cell. The cell's TEXT is not a
#: usable handle -- 'fractional' is also the engine's own REASON_FRACTIONAL and
#: shows up in the reasons column of the very same row -- so the tests locate it
#: by marker instead of by string.
MARKER_ORDER_KIND = 'dry-run-order-kind'

#: NiceGUI marker on the dry-run table's per-row tick box. Rendered in plan order.
MARKER_ROW_TICK = 'dry-run-row-tick'


class AllocationWizard:
    """The dry-run dialog: base panel, fractional toggle, tickable rows, totals.

    Nothing is written to the database until the user presses Submit, which hands
    the FILTERED plan (ticked rows only) to ``on_submit``.

    A SUPPRESSED row (an order the broker's minimum order size, its $5 fractional
    notional floor, the buying-power scaler or its own precheck already killed) is
    shown -- dropping it would tell the user there was nothing to do about a
    symbol they targeted -- but it is neither pre-ticked nor tickable: there is no
    order to submit.
    """

    def __init__(
        self,
        base: BaseSnapshot,
        plan: AllocationPlan,
        *,
        on_refresh: Callable[[bool], AllocationPlan],
        on_submit: Callable[[AllocationPlan], None],
        title: str = 'Portfolio allocation - dry run',
    ):
        self.base = base
        self.plan = plan
        self.on_refresh = on_refresh
        self.on_submit = on_submit
        self.title = title
        self.allow_fractional = bool(plan.allow_fractional)
        self.selected = self._default_selection(plan)
        self.dialog = None
        self._rows_container = None
        self._totals_container = None

    # -- public -----------------------------------------------------------
    def open(self):
        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            self.dialog = dialog
            ui.label(self.title).classes('text-xl font-bold')
            self._render_base_panel()
            ui.switch('Allow fractional shares', value=self.allow_fractional,
                      on_change=lambda e: self._refresh(bool(e.value)))
            ui.label(MARKET_ORDER_TIMING_NOTE).classes('text-xs text-orange-400')
            self._rows_container = ui.column().classes('w-full gap-0')
            self._totals_container = ui.column().classes('w-full')
            self._render_rows()
            self._render_totals()
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Refresh', on_click=lambda: self._refresh(self.allow_fractional)).props('outline')
                ui.button('Cancel', on_click=dialog.close).props('flat')
                ui.button('Submit', on_click=self._submit).props('color=primary')
        dialog.open()
        return dialog

    # -- internals --------------------------------------------------------
    @staticmethod
    def _default_selection(plan: AllocationPlan) -> set:
        """Every row that will actually produce an order.

        ``skipped`` is NOT enough on its own: a row suppressed by
        ``_suppress_below_min_order`` keeps ``skipped is False`` (it was never
        skipped -- it was sized, then its order was killed), so filtering on
        ``skipped`` alone pre-ticks orders the broker has already refused.
        """
        return {r['symbol'] for r in dry_run_rows(plan)
                if not r['skipped'] and not r['suppressed']}

    def _render_base_panel(self):
        with ui.row().classes('w-full gap-6 items-center'):
            ui.label(f'Buying power: {self.base.available_buying_power:,.2f}')
            ui.label(f'Managed value ({self.base.valuation_mode}): '
                     f'{self.base.managed_value:,.2f}')
            ui.label(f'Base notional: {self.base.base_notional:,.2f}').classes('font-bold')
            ui.label(f"as of {self.base.taken_at:%Y-%m-%d %H:%M UTC}").classes('text-xs text-gray-400')
        for warning in self.base.warnings:
            ui.label(warning).classes('text-xs text-orange-400')
        for warning in self.plan.warnings:
            ui.label(warning).classes('text-xs text-orange-400')

    def _render_rows(self):
        self._rows_container.clear()
        rows = dry_run_rows(self.plan)
        with self._rows_container:
            if not rows:
                ui.label('No orders required - the account already matches its targets.') \
                    .classes('text-sm text-gray-400')
                return
            if any(r['fractional'] and not r['suppressed'] for r in rows):
                ui.label(FRACTIONAL_IS_MARKET_ONLY_NOTE).classes('text-xs text-orange-400')
            with ui.row().classes('w-full text-xs font-bold border-b py-1'):
                for header, width in (('', 'w-10'), ('Symbol', 'w-24'), ('Side', 'w-16'),
                                      ('Qty', 'w-28'), ('Order', 'w-28'),
                                      ('Est. value', 'w-28'), ('BP cost', 'w-28'),
                                      ('BP %', 'w-16'), ('Reasons', 'flex-1')):
                    ui.label(header).classes(width)
            for row in rows:
                self._render_row(row)

    def _render_row(self, row: Dict):
        """One line of the dry-run table.

        The ``Order`` column is the whole point of the table: it says whether the
        broker will receive a fractional or a whole-share order, which decides
        both the order type and whether the $5 notional floor applies. It reports
        the ORDER (``fractional``), not the grid the row was sized on
        (``sized_fractional``) -- a row sized fractionally that landed on exactly
        5.0 shares sends an ordinary whole-share order.
        """
        blocked = row['suppressed'] or row['skipped']
        with ui.row().classes('w-full text-sm items-center border-b py-1'
                              + (' opacity-60' if blocked else '')):
            checkbox = ui.checkbox(
                value=row['symbol'] in self.selected,
                on_change=lambda e, s=row['symbol']: self._toggle(s, bool(e.value)),
            ).classes('w-10').mark(MARKER_ROW_TICK)
            # A suppressed row has no order to submit, so it must not be tickable
            # -- greying it is not enough, the box would still be clickable.
            checkbox.set_enabled(not blocked)
            ui.label(row['symbol']).classes('w-24 font-medium')
            ui.label(row['side'] or '-').classes(
                'w-16 ' + ('text-green-500' if row['side'] == 'BUY'
                           else 'text-red-500' if row['side'] == 'SELL'
                           else 'text-gray-400'))
            ui.label(f"{row['quantity']:,.5f}".rstrip('0').rstrip('.') or '0').classes('w-28')
            if row['suppressed']:
                order_kind, order_class = 'no order', 'text-orange-400'
            elif row['fractional']:
                order_kind, order_class = 'fractional', 'text-blue-400'
            else:
                order_kind, order_class = 'whole shares', 'text-gray-400'
            ui.label(order_kind).classes('w-28 text-xs ' + order_class) \
                .mark(MARKER_ORDER_KIND)
            ui.label(f"{row['estimated_value']:,.2f}").classes('w-28')
            ui.label(f"{row['bp_cost']:,.2f}").classes('w-28')
            ui.label(f"{row['bp_usage_pct']:.1f}%").classes('w-16')
            ui.label(row['reasons']).classes(
                'flex-1 text-xs ' + ('text-orange-400' if row['suppressed']
                                     else 'text-gray-400'))

    def _render_totals(self):
        self._totals_container.clear()
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        try:
            totals = summarise_plan(selected_plan, cash=self.base.cash)
        except ValueError:
            totals = None
        with self._totals_container:
            with ui.row().classes('w-full gap-6 mt-2 text-sm'):
                ui.label(f"Sell value: {selected_plan.total_sell_value:,.2f}")
                ui.label(f"Buy value: {selected_plan.total_buy_value:,.2f}")
                ui.label(f"Required BP: {selected_plan.required_buying_power:,.2f} "
                         f"/ {selected_plan.available_buying_power:,.2f} "
                         f"({selected_plan.bp_usage_pct:.1f}%)")
                if totals is not None:
                    ui.label(f"Est. cash after: {totals['estimated_cash_after']:,.2f}")
                else:
                    ui.label('Est. cash after: unknown (broker published no cash balance)') \
                        .classes('text-orange-400')
            if selected_plan.required_buying_power > selected_plan.available_buying_power:
                ui.label('Required buying power exceeds available - the smallest buys will be '
                         'truncated as buying power runs out.').classes('text-xs text-orange-400')

    def _toggle(self, symbol: str, checked: bool):
        if checked:
            self.selected.add(symbol)
        else:
            self.selected.discard(symbol)
        self._render_totals()

    def _refresh(self, allow_fractional: bool):
        self.allow_fractional = allow_fractional
        try:
            self.plan = self.on_refresh(allow_fractional)
        except Exception as e:
            logger.error(f"Allocation dry-run refresh failed: {e}", exc_info=True)
            ui.notify(f'Refresh failed: {e}', type='negative')
            return
        self.selected = self._default_selection(self.plan)
        self._render_rows()
        self._render_totals()
        ui.notify('Dry run refreshed', type='info')

    def _submit(self):
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        if not selected_plan.rows:
            ui.notify('Nothing selected to submit', type='warning')
            return
        if self.dialog is not None:
            self.dialog.close()
        self.on_submit(selected_plan)


def open_allocation_wizard(
    base: BaseSnapshot,
    plan: AllocationPlan,
    *,
    on_refresh: Callable[[bool], AllocationPlan],
    on_submit: Callable[[AllocationPlan], None],
    title: str = 'Portfolio allocation - dry run',
) -> AllocationWizard:
    """Open the dry-run dialog. Returns the wizard so the caller can keep a handle."""
    wizard = AllocationWizard(base, plan, on_refresh=on_refresh, on_submit=on_submit, title=title)
    wizard.open()
    return wizard


def render_income_panel(events: List[Dict], open_total: float,
                        *, on_sync: Callable[[], None],
                        on_invest: Callable[[float], None]) -> None:
    """Placeholder replaced in Task 74."""
    ui.label(f'Unallocated income: {open_total:,.2f}')


def render_outcomes(outcomes: List, *, run_id: Optional[int] = None) -> None:
    """Placeholder replaced in Task 75."""
    ui.label(f'{len(outcomes)} row(s) processed')
