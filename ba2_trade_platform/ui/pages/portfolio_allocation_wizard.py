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
    ALLOCATION_MODE_INVEST_LABEL,
    ALLOCATION_MODE_REBALANCE,
    AllocationPlan,
    BaseSnapshot,
    LabelTarget,
    SymbolTarget,
    blocking_messages,
    dry_run_rows,
    even_split_targets,
    filter_plan_rows,
    invest_validation_messages,
    is_blocking_message,
    steps_validation_messages,
    summarise_plan,
)
from ...core.portfolio_allocation_service import (
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SKIPPED,
    OUTCOME_SUBMITTED,
    OUTCOME_WASHTRADE_LOCKED,
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

#: Markers on the step 1 / step 2 percentage boxes, in label then symbol order.
MARKER_LABEL_PCT = 'steps-label-pct'
MARKER_SYMBOL_PCT = 'steps-symbol-pct'

#: Shown under the INVEST_LABEL amount box.
INVEST_SCOPE_NOTE = ('Pre-filled with the unallocated income total. Buys only - '
                     'an INVEST run never sells.')

#: Shown on the fractional switch when the broker does not split shares at all.
NO_FRACTIONAL_SUPPORT_NOTE = 'This broker does not support fractional shares.'


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
        self._submit_button = None
        #: One-shot latch. See ``_submit``: NiceGUI runs a sync click handler
        #: directly on the event loop, so the dialog stays on screen -- and
        #: clickable -- for the whole of a blocking submit.
        self._submitted = False

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
                self._submit_button = ui.button('Submit', on_click=self._submit) \
                    .props('color=primary')
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
        """Hand the ticked rows to ``on_submit``. ONCE, whatever the user clicks.

        ``on_submit`` runs the whole allocation -- every order, synchronously,
        for as long as the broker takes. NiceGUI dispatches a sync click handler
        directly on the event loop (``nicegui/events.py:444-448``), so NOTHING
        this method does reaches the browser until it returns: not
        ``dialog.close()``, not ``set_enabled(False)``. The Submit button stays
        on screen and stays clickable for the entire run, and a second click
        submits the WHOLE plan again -- every buy placed twice.

        The latch is one-shot and is never released. Clicks are dispatched
        sequentially, so a flag cleared in a ``finally`` would already be back to
        False by the time the queued second click ran; and it is set BEFORE
        ``on_submit``, so a submit that dies half way -- with orders already at
        the broker -- cannot be re-run on top of itself either. There is nothing
        left to submit once the plan has gone: the dialog is closed and the
        results table takes over.

        An EMPTY submit does not latch: nothing was sent, and the user still has
        to be able to tick a row and press Submit for real.
        """
        if self._submitted:
            logger.warning('Allocation submit ignored: this dry run has already been '
                           'submitted (a second click during a blocking submit)')
            ui.notify('This allocation has already been submitted', type='warning')
            return
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        if not selected_plan.rows:
            ui.notify('Nothing selected to submit', type='warning')
            return
        self._submitted = True
        if self._submit_button is not None:
            self._submit_button.set_enabled(False)
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


class AllocationSteps:
    """Steps 1-3 of the wizard, drawn as three sections in ONE dialog.

    This repo uses no ``ui.stepper``, so the three steps are stacked sections
    with a single validated Continue button; the pure validators decide whether
    Continue is enabled and their messages are shown verbatim.

    REBALANCE mode edits label percentages (step 1) and symbol weights (step 2),
    gated by ``steps_validation_messages``. INVEST_LABEL mode replaces step 1
    with a label picker plus an amount box and skips the labels-total-100 rule
    entirely -- the amount is the whole budget (decision: buys only, no sells) --
    but it is NOT ungated: ``invest_validation_messages`` still holds the chosen
    label's symbol weights to 100%, because ``compute_label_investment``
    multiplies them straight through and a 150% set would overspend the budget by
    half.

    Nothing is written here, and the caller's ``labels`` are deep-copied on the
    way in, so Cancel really cancels. Continue hands the edited targets to
    ``on_dry_run``, which solves and opens ``AllocationWizard``.
    """

    def __init__(self, base: BaseSnapshot, labels: List[LabelTarget], *,
                 on_dry_run: Callable[..., None],
                 allow_fractional: bool,
                 mode: str = ALLOCATION_MODE_REBALANCE,
                 invest_amount: float = 0.0):
        self.base = base
        self.labels = [LabelTarget(label=lt.label, target_pct=lt.target_pct,
                                   symbols=[SymbolTarget(st.symbol, st.weight_pct, st.comment)
                                            for st in lt.symbols],
                                   comment=lt.comment)
                       for lt in labels or []]
        self.on_dry_run = on_dry_run
        self.mode = mode
        self.invest_amount = float(invest_amount or 0.0)
        # The account's REMEMBERED choice (defaults ON), still vetoed by a broker
        # that cannot do fractional at all. Per-symbol eligibility is the engine's
        # job -- MarginInfo.fractionable -- not this switch's.
        self.allow_fractional = bool(base.supports_fractional and allow_fractional)
        self.scope_label = self.labels[0].label if self.labels else None
        self.dialog = None
        self._errors_container = None
        self._continue_button = None
        self._fractional_switch = None
        self._labels_container = None

    def open(self):
        title = ('Rebalance - set targets' if self.mode == ALLOCATION_MODE_REBALANCE
                 else 'Invest into one label')
        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            self.dialog = dialog
            ui.label(title).classes('text-xl font-bold')

            if self.mode == ALLOCATION_MODE_REBALANCE:
                self._render_step1_label_targets()
                self._render_step2_symbol_weights()
            else:
                self._render_invest_scope()

            self._render_step3_base_panel()
            self._errors_container = ui.column().classes('w-full')
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                self._continue_button = ui.button('Continue to dry run',
                                                  on_click=self._continue).props('color=primary')
            self._revalidate()
        dialog.open()
        return dialog

    # -- steps ------------------------------------------------------------
    def _render_step1_label_targets(self):
        ui.label('1. Label targets (% of the base notional, must total 100%)') \
            .classes('text-lg font-bold mt-2')
        ui.button('Even split', icon='balance', on_click=self._even_split).props('outline dense')
        self._labels_container = ui.column().classes('w-full gap-1')
        self._draw_label_targets()

    def _draw_label_targets(self):
        self._labels_container.clear()
        with self._labels_container:
            for lt in self.labels:
                with ui.row().classes('w-full items-center gap-3'):
                    ui.label(lt.label).classes('w-40 font-medium')
                    # ``t=lt`` is load-bearing: without the default-argument
                    # capture every box would write to the LAST label.
                    ui.number(value=lt.target_pct, min=0, max=100, step=0.01, suffix='%',
                              on_change=lambda e, t=lt: self._set_label_pct(t, e.value)
                              ).props('dense outlined').classes('w-32').mark(MARKER_LABEL_PCT)

    def _render_step2_symbol_weights(self):
        ui.label('2. Symbol weights within each label (each label must total 100%)') \
            .classes('text-lg font-bold mt-4')
        for lt in self.labels:
            with ui.expansion(f'{lt.label} - {len(lt.symbols)} symbol(s)').classes('w-full'):
                if not lt.symbols:
                    ui.label('No symbols carry this label - it can absorb no allocation.') \
                        .classes('text-xs text-orange-400')
                    continue
                for st in lt.symbols:
                    with ui.row().classes('w-full items-center gap-3'):
                        ui.label(st.symbol).classes('w-32')
                        ui.number(value=st.weight_pct, min=0, max=100, step=0.01, suffix='%',
                                  on_change=lambda e, t=st: self._set_symbol_pct(t, e.value)
                                  ).props('dense outlined').classes('w-32').mark(MARKER_SYMBOL_PCT)

    def _render_invest_scope(self):
        ui.label('1. Which label, and how much').classes('text-lg font-bold mt-2')
        with ui.row().classes('w-full items-center gap-3'):
            ui.select([lt.label for lt in self.labels], value=self.scope_label, label='Label',
                      on_change=self._set_scope).props('dense outlined').classes('w-56')
            ui.number(value=self.invest_amount, min=0, step=0.01, label='Amount',
                      on_change=self._set_amount).props('dense outlined').classes('w-40')
        ui.label(INVEST_SCOPE_NOTE).classes('text-xs text-secondary-custom')

    def _render_step3_base_panel(self):
        ui.label('3. What there is to allocate').classes('text-lg font-bold mt-4')
        with ui.row().classes('w-full gap-6 items-center'):
            ui.label(f'Buying power: {self.base.available_buying_power:,.2f}')
            ui.label(f'Managed value ({self.base.valuation_mode}): '
                     f'{self.base.managed_value:,.2f}')
            ui.label(f'Base notional: {self.base.base_notional:,.2f}').classes('font-bold')
            ui.label(f"as of {self.base.taken_at:%Y-%m-%d %H:%M UTC}").classes('text-xs text-gray-400')
        for warning in self.base.warnings:
            ui.label(warning).classes('text-xs text-orange-400')
        self._fractional_switch = ui.switch(
            'Allow fractional shares', value=self.allow_fractional,
            on_change=lambda e: setattr(self, 'allow_fractional', bool(e.value)))
        # Offering a toggle the broker cannot honour would produce a plan sized on
        # a grid that does not exist; the engine silently falls back to whole
        # shares, so the user would see targets they never asked for.
        self._fractional_switch.set_enabled(bool(self.base.supports_fractional))
        if not self.base.supports_fractional:
            ui.label(NO_FRACTIONAL_SUPPORT_NOTE).classes('text-xs text-gray-400')

    # -- state + validation ------------------------------------------------
    def _even_split(self):
        for edited, fresh in zip(self.labels, even_split_targets(self.labels)):
            edited.target_pct = fresh.target_pct
        self._draw_label_targets()
        self._revalidate()

    def _set_label_pct(self, target: LabelTarget, value):
        target.target_pct = float(value or 0.0)
        self._revalidate()

    def _set_symbol_pct(self, target: SymbolTarget, value):
        target.weight_pct = float(value or 0.0)
        self._revalidate()

    def _set_scope(self, event):
        self.scope_label = event.value
        self._revalidate()

    def _set_amount(self, event):
        self.invest_amount = float(event.value or 0.0)
        self._revalidate()

    def _scope_target(self) -> Optional[LabelTarget]:
        return next((lt for lt in self.labels if lt.label == self.scope_label), None)

    def _problems(self) -> List[str]:
        if self.mode == ALLOCATION_MODE_REBALANCE:
            return steps_validation_messages(self.labels)
        return invest_validation_messages(
            self._scope_target(), self.invest_amount,
            available_buying_power=self.base.available_buying_power)

    def _revalidate(self):
        if self._errors_container is None:
            return
        messages = self._problems()
        self._errors_container.clear()
        with self._errors_container:
            for message in messages:
                blocking = is_blocking_message(message)
                ui.label(('✖ ' if blocking else '⚠ ') + message).classes(
                    'text-xs ' + ('text-red-500' if blocking else 'text-orange-400'))
        if self._continue_button is not None:
            self._continue_button.set_enabled(not blocking_messages(messages))

    def _continue(self):
        # Re-checked here and not only on the button's enabled state: the button
        # is a mirror of this, and a mirror can be stale.
        if blocking_messages(self._problems()):
            ui.notify('Fix the highlighted problems first', type='warning')
            return
        if self.dialog is not None:
            self.dialog.close()
        if self.mode == ALLOCATION_MODE_REBALANCE:
            self.on_dry_run(mode=ALLOCATION_MODE_REBALANCE, labels=self.labels,
                            scope_label=None, amount=0.0,
                            allow_fractional=self.allow_fractional)
        else:
            scope = self._scope_target()
            self.on_dry_run(mode=ALLOCATION_MODE_INVEST_LABEL,
                            labels=[scope] if scope else [], scope_label=self.scope_label,
                            amount=self.invest_amount,
                            allow_fractional=self.allow_fractional)


def open_allocation_steps(base: BaseSnapshot, labels: List[LabelTarget], *,
                          on_dry_run: Callable[..., None],
                          allow_fractional: bool,
                          mode: str = ALLOCATION_MODE_REBALANCE,
                          invest_amount: float = 0.0) -> AllocationSteps:
    """Open steps 1-3. ``on_dry_run`` is called with keyword arguments
    ``mode``, ``labels``, ``scope_label``, ``amount`` and ``allow_fractional``.

    ``allow_fractional`` is REQUIRED and has no default: the caller passes
    ``get_allocation_config(account_id).allow_fractional`` (itself defaulting to
    True), and persists any change through
    ``portfolio_allocation_service.remember_fractional_choice``. A default here
    would silently re-answer a question the account has already answered."""
    steps = AllocationSteps(base, labels, on_dry_run=on_dry_run,
                            allow_fractional=allow_fractional, mode=mode,
                            invest_amount=invest_amount)
    steps.open()
    return steps


def render_income_panel(events: List[Dict], open_total: float,
                        *, on_sync: Callable[[], None],
                        on_invest: Callable[[float], None]) -> None:
    """Last 30 days of income, the open total, and the Invest shortcut.

    The panel NEVER polls. ``on_sync`` is wired to the Refresh button and is
    additionally called once by the page on load; there is deliberately no
    ``ui.timer`` here, so the page issues no background broker calls.

    ``on_invest(open_total)`` opens the wizard in INVEST_LABEL mode pre-filled
    with the UNALLOCATED amount -- what is left, not what arrived.

    Only absolute figures are shown. ``consumed / amount`` is deliberately NOT
    rendered as a fraction or a progress bar: ``consumed_amount > amount`` is a
    reachable state (a DIVNRA tax leg restates a dividend downwards while the
    consumed figure, the true record of the spend, is left alone), and a naive
    percentage renders above 100%.
    """
    with ui.card().classes('w-full'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Income (last 30 days)').classes('text-lg font-bold')
            with ui.row().classes('gap-2 items-center'):
                ui.label(f'Unallocated: {open_total:,.2f}').classes('font-bold text-green-500')
                ui.button('Refresh', on_click=on_sync).props('outline dense')
                # Nothing to invest is not a run worth opening the wizard for.
                ui.button('Invest', on_click=lambda: on_invest(open_total)) \
                    .props('color=primary dense').set_enabled(open_total > 0)
        if not events:
            ui.label('No deposits or dividends in the last 30 days.') \
                .classes('text-sm text-gray-400')
            return
        with ui.row().classes('w-full text-xs font-bold border-b py-1'):
            for header, width in (('Date', 'w-28'), ('Type', 'w-24'), ('Symbol', 'w-24'),
                                  ('Amount', 'w-28'), ('Open', 'w-28')):
                ui.label(header).classes(width)
        for event in events:
            with ui.row().classes('w-full text-sm border-b py-1'):
                ui.label(str(event['event_date'])).classes('w-28')
                ui.label(event['event_type']).classes('w-24')
                # A deposit has no payer symbol; a bare None would draw "None".
                ui.label(event['symbol'] or '-').classes('w-24')
                ui.label(f"{event['amount']:,.2f}").classes('w-28')
                ui.label(f"{event['open_amount']:,.2f}").classes('w-28')


#: Status -> colour class. Keyed on the SERVICE's own constants, never on
#: literals: a renamed constant would silently stop matching, and the failure
#: count below would then read 0 for a run in which everything failed.
OUTCOME_COLOURS = {
    OUTCOME_SUBMITTED: 'text-green-500',
    OUTCOME_PARTIAL: 'text-yellow-500',
    OUTCOME_SKIPPED: 'text-gray-400',
    OUTCOME_WASHTRADE_LOCKED: 'text-orange-400',
    OUTCOME_FAILED: 'text-red-500',
}

#: NiceGUI marker on the outcome table's "Filled" cell, so a test can read the
#: column without matching on a quantity string that also appears in "Qty".
MARKER_OUTCOME_FILLED = 'outcome-filled'


def render_outcomes(outcomes: List, *, run_id: Optional[int] = None) -> None:
    """Per-row outcome table shown after Submit.

    Partial failure is normal: a failed row sits next to a filled one and nothing
    is rolled back, so every row is listed with its own status and message.

    ``Filled`` is shown next to ``Qty`` because they differ in the cases that
    matter: a partially filled order, and a fractional order that fell back to
    whole shares. ``filled_quantity is None`` means the broker reported no fill
    at all -- an accepted market order before the open looks exactly like that --
    and is drawn as "-", never as 0, which would read as "nothing filled".
    """
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
        title = f'Allocation run {run_id} - results' if run_id else 'Allocation run - results'
        ui.label(title).classes('text-lg font-bold')
        with ui.row().classes('w-full text-xs font-bold border-b py-1'):
            for header, width in (('Symbol', 'w-24'), ('Action', 'w-24'), ('Status', 'w-36'),
                                  ('Qty', 'w-24'), ('Filled', 'w-24'), ('Path', 'w-24'),
                                  ('Detail', 'flex-1')):
                ui.label(header).classes(width)
        for outcome in outcomes:
            with ui.row().classes('w-full text-sm border-b py-1'):
                ui.label(outcome.symbol).classes('w-24 font-medium')
                ui.label(outcome.action).classes('w-24')
                ui.label(outcome.status).classes(
                    'w-36 ' + OUTCOME_COLOURS.get(outcome.status, ''))
                ui.label(f'{outcome.quantity:,.4f}').classes('w-24')
                ui.label('-' if outcome.filled_quantity is None
                         else f'{outcome.filled_quantity:,.4f}') \
                    .classes('w-24').mark(MARKER_OUTCOME_FILLED)
                ui.label(outcome.path or '-').classes('w-24')
                ui.label(outcome.message or '').classes('flex-1 text-xs text-gray-400')
        with ui.row().classes('w-full justify-end mt-2'):
            ui.button('Close', on_click=dialog.close).props('flat')
    dialog.open()

    failed = sum(1 for o in outcomes if o.status == OUTCOME_FAILED)
    locked = sum(1 for o in outcomes if o.status == OUTCOME_WASHTRADE_LOCKED)
    if failed:
        ui.notify(f'{failed} row(s) failed - see the results table', type='warning')
    elif locked:
        # Not a failure: the order is PENDING at our end and is retried once the
        # blocker clears. Saying nothing would leave the user believing it traded.
        ui.notify(f'{locked} row(s) are wash-trade locked and will be retried',
                  type='warning')
    else:
        ui.notify('Allocation run submitted', type='positive')
