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

Market hours gate SUBMIT only. The dialog opens, refreshes and recomputes with the
market shut, because planning outside market hours is the normal case; the button
is disabled and the banner names the next open. ``run_allocation`` re-checks
server-side, since this dialog can sit open across the close.

The Outcome and Weight columns are not decoration. The engine bumps a target
smaller than one share UP to one share, and moves quantities between a label's
symbols to keep the label on target; both spend money the typed weights did not ask
for, and both are only acceptable because these two columns show them.
"""
from typing import Callable, Dict, List, Optional, Tuple

from nicegui import ui

from ...core.portfolio_allocation import (
    ALLOCATION_MODE_INVEST_LABEL,
    ALLOCATION_MODE_REBALANCE,
    LEVERAGE_LEVERAGED,
    LEVERAGE_NONE,
    LEVERAGE_NOT_APPLICABLE,
    LEVERAGE_PENALISED,
    LEVERAGE_UNKNOWN,
    MONEY_EPSILON,
    VALUATION_MODE_MARKET,
    AllocationPlan,
    BaseSnapshot,
    LabelTarget,
    SymbolTarget,
    blocking_messages,
    bump_notice,
    dry_run_rows,
    even_split_targets,
    filter_plan_rows,
    fractional_summary,
    held_no_price_block,
    invest_validation_messages,
    is_blocking_message,
    no_order_notice,
    no_order_rows,
    redistribution_notice,
    steps_validation_messages,
    summarise_plan,
    unconsumed_income_notice,
    whole_share_notice,
)
from ..utils.portfolio_allocation_view import (
    MARKET_BANNER_CLASSES, MarketGateResult, market_provenance_notice,
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

#: Markers on the dry-run table's CURRENT-HOLDING columns -- what is owned, what it
#: cost, what it is worth now -- in plan order. By marker and never by text: every
#: one of these is a bare money figure that repeats in three other columns of the
#: same row, so a membership assertion could not tell which column it came from.
MARKER_ROW_HELD = 'dry-run-row-held'
MARKER_ROW_COST = 'dry-run-row-cost'
MARKER_ROW_VALUE = 'dry-run-row-value'

#: Marker on the dry-run table's leverage (``BP ×``) cell. Emphatically by marker:
#: the engine's own ``REASON_NOT_MARGINABLE`` ("⚠ not marginable") already appears
#: verbatim in the free-text Reasons column of exactly the rows a text search for a
#: leverage signal would go looking in.
MARKER_LEVERAGE = 'dry-run-leverage'

#: Marker on the totals' buying-power sentence (see ``BP_IS_A_CHARGE_NOTE_FMT``).
MARKER_BP_NOTE = 'dry-run-bp-note'

#: Drawn in the ``BP ×`` column when the broker published no margin rate for the
#: symbol. A ``?``, never the raw multiple: the number in that case is the
#: account's own conservative fallback, and printing it as if it were measured is
#: precisely the regression the ``MARGIN_SOURCE_DEFAULT`` guard exists to stop.
LEVERAGE_UNKNOWN_MARK = '?'

#: Tooltip under a ``BP ×`` cell the broker DID publish a rate for. It states the
#: two facts apart, because they are routinely confused and on a hard-to-borrow
#: name they point OPPOSITE ways: "the broker lends against this" is the initial
#: margin being under 100%, whereas the × is how much BUYING POWER the buy charges.
#: LAZR at a 98.9% initial margin is nearly cash-collateralised AND costs ×1.98 of
#: buying power; a bare red ×1.98 on its own says the reverse.
LEVERAGE_TOOLTIP_FMT = (
    '{symbol} charges {ratio:.2f}x its trade value against buying power. '
    'Initial margin {rate:.1%}, from the broker\'s {source} data. Above 1.00x '
    'consumes MORE buying power than the position is worth; below 1.00x is '
    'genuine leverage. "The broker lends against this" is the initial margin '
    'being under 100%, which is a different fact and often the opposite one.')

#: Tooltip when the rate is missing or came from the account-multiplier fallback.
#: Names the fallback rather than hiding it: the ratio IS what the plan charges, so
#: withholding the number would be its own lie -- what is withheld is the VERDICT.
LEVERAGE_TOOLTIP_UNKNOWN_FMT = (
    '{symbol}: the broker published no margin rate for this symbol, so buying '
    'power is charged at the account multiplier ({ratio:.2f}x) as a conservative '
    'fallback. That is not a measured figure, so this row is deliberately not '
    'flagged either way. Brokers commonly publish nothing for a symbol you do not '
    'already hold.')

#: Tooltip on a sell's (empty) leverage cell.
LEVERAGE_TOOLTIP_SELL = (
    'A sell FREES buying power rather than charging it, so there is no ratio to '
    'state.')

#: Tooltip on a neutral ×1.00, so the column never reads as "nothing measured".
LEVERAGE_TOOLTIP_NEUTRAL_FMT = (
    '{symbol} charges 1.00x -- a dollar of stock costs a dollar of buying power, '
    'which is the ordinary case for a marginable stock on a margin account '
    '(initial margin {rate:.1%} x the account multiplier). Not a leverage story.')

#: The totals' answer to requirement 1b. ``bp_factor`` is a notional-to-buying-power
#: CONVERSION and provably moves no target and no quantity -- the engine is already
#: right -- but a bare "BP cost 19,780" beside a "Buy value 10,000" reads as if
#: leverage had inflated the plan. This names the two apart in one sentence. It is a
#: LABELLING fix: nothing in the arithmetic changed to make it true.
BP_IS_A_CHARGE_NOTE_FMT = (
    'Buying power is CHARGED, not spent: this plan buys {buy_value:,.2f} of stock '
    'and the broker reserves {required:,.2f} of buying power against it '
    '({ratio:.2f}x). Only the {buy_value:,.2f} is invested - the ratio moves no '
    'target and buys no extra share.')

#: NiceGUI marker on the market-hours banner's sentence, and on each of the four
#: plan notices. Located by marker, not by text: the gate's message is ALSO the
#: Submit button's tooltip, and every notice quotes wording that recurs in the
#: reasons column, so a text search cannot tell whether the banner was drawn.
MARKER_MARKET_BANNER = 'dry-run-market-banner'
MARKER_PLAN_NOTICE = 'dry-run-plan-notice'

#: Marker on the income panel's working-orders line, for the same reason.
MARKER_WORKING_ORDERS = 'income-working-orders'

#: Markers on the step 1 / step 2 percentage boxes, in label then symbol order.
MARKER_LABEL_PCT = 'steps-label-pct'
MARKER_SYMBOL_PCT = 'steps-symbol-pct'

#: Marker on the base panel's BLOCKING banner -- today only the market-mode
#: "a held symbol has no quote" refusal (``held_no_price_block``). Located by
#: marker rather than by text: the symbol names it lists also appear in the table
#: right below it, so a text search cannot tell whether the banner was drawn.
MARKER_BASE_BLOCK = 'base-block'

#: Shown under the INVEST_LABEL amount box.
INVEST_SCOPE_NOTE = ('Pre-filled with the unallocated income total. Buys only - '
                     'an INVEST run never sells.')

#: Shown on the fractional switch when the broker does not split shares at all.
NO_FRACTIONAL_SUPPORT_NOTE = 'This broker does not support fractional shares.'


def _shares(quantity: float) -> str:
    """A share count for the table: full precision, no trailing-zero clutter.

    5 decimals because that is TastyTrade's equity quantity precision -- the dry
    run's job is to state what will be SENT, so it may not round tighter than the
    broker's own grid. ``or '0'`` catches exactly 0.0, which strips to ''.
    """
    return f"{quantity:,.5f}".rstrip('0').rstrip('.') or '0'


def _leverage_cell(row: Dict) -> Tuple[str, str, str]:
    """The dry run's ``BP ×`` cell: ``(text, css, tooltip)``. Pure; no ``ui`` call.

    Reads ``leverage`` -- the ENGINE's verdict, from ``bp_leverage``, which owns the
    predicate and its ``MARGIN_SOURCE_DEFAULT`` guard -- and never re-derives it
    from the ratio. Re-deriving is how the guard gets lost: the ratio for an unheld
    TastyTrade buy really is 2.0, and it is only the SOURCE of that 2.0 that makes
    it meaningless.

    The colour is the message. Orange is reserved for the case that costs the user
    something (a buying-power penalty); genuine leverage is green because it makes
    the plan cheaper to hold; neutral and unpublished are grey, because neither is a
    finding and painting the unpublished case would flag every first-time buy on a
    broker that does not publish per-symbol rates.

    Every branch returns a non-empty tooltip: a lone ``?`` or ``-`` in a numeric
    column is a question the user cannot answer from the screen.
    """
    verdict = row['leverage']
    ratio = row['bp_ratio']
    symbol = row['symbol']
    if verdict == LEVERAGE_NOT_APPLICABLE:
        return '-', 'text-gray-400', LEVERAGE_TOOLTIP_SELL
    if verdict == LEVERAGE_UNKNOWN:
        return (LEVERAGE_UNKNOWN_MARK, 'text-gray-400',
                LEVERAGE_TOOLTIP_UNKNOWN_FMT.format(symbol=symbol, ratio=ratio))
    text = f'×{ratio:.2f}'
    rate = row['initial_margin_rate']
    if verdict == LEVERAGE_PENALISED:
        return (text, 'text-orange-400 font-medium',
                LEVERAGE_TOOLTIP_FMT.format(symbol=symbol, ratio=ratio, rate=rate,
                                            source=row['margin_source']))
    if verdict == LEVERAGE_LEVERAGED:
        return (text, 'text-green-500 font-medium',
                LEVERAGE_TOOLTIP_FMT.format(symbol=symbol, ratio=ratio, rate=rate,
                                            source=row['margin_source']))
    if verdict == LEVERAGE_NONE:
        return (text, 'text-gray-400',
                LEVERAGE_TOOLTIP_NEUTRAL_FMT.format(symbol=symbol, rate=rate))
    # An engine verdict this cell has never heard of. Say so rather than draw a
    # blank column: a silently empty cell in a money table is indistinguishable
    # from "nothing to report".
    logger.error(f"_leverage_cell: unknown leverage verdict {verdict!r} for "
                 f"{symbol}; the engine and the table have drifted apart")
    return (LEVERAGE_UNKNOWN_MARK, 'text-gray-400',
            LEVERAGE_TOOLTIP_UNKNOWN_FMT.format(symbol=symbol, ratio=ratio or 0.0))


class AllocationWizard:
    """The dry-run dialog: base panel, fractional toggle, tickable rows, totals.

    Nothing is written to the database until the user presses Submit, which hands
    the FILTERED plan (ticked rows only) to ``on_submit``.

    A SUPPRESSED row (an order the broker's minimum order size, its $5 fractional
    notional floor, the buying-power scaler or its own precheck already killed) is
    shown -- dropping it would tell the user there was nothing to do about a
    symbol they targeted -- but it is neither pre-ticked nor tickable: there is no
    order to submit. The 'Not traded' section below the table gives those rows
    their money: what was wanted, what will be held, and what never left the cash.

    ``market`` gates Submit ONLY. When the market is closed the dialog still opens,
    still refreshes and still recomputes: planning outside market hours is the
    normal way to use this page. ``run_allocation`` re-checks the gate server-side,
    because this dialog can sit open across the close.

    Refresh re-reads the CLOCK as well as the plan. ``on_refresh`` returns
    ``(plan, market)`` -- one call, both answers, from the same solve, exactly as
    the initial open gets them -- and ``_refresh`` then re-renders the banner and
    re-syncs the Submit button. Returning only a plan is what left a dialog opened
    before the bell with Submit disabled all morning, and (the direction that costs
    money) a dialog opened while OPEN with Submit still enabled after 16:00.
    """

    def __init__(
        self,
        base: BaseSnapshot,
        plan: AllocationPlan,
        *,
        market: MarketGateResult,
        on_refresh: Callable[[bool], Tuple[AllocationPlan, MarketGateResult]],
        on_submit: Callable[[AllocationPlan], None],
        title: str = 'Portfolio allocation - dry run',
    ):
        self.base = base
        self.plan = plan
        self.market = market
        self.on_refresh = on_refresh
        self.on_submit = on_submit
        self.title = title
        self.allow_fractional = bool(plan.allow_fractional)
        self.selected = self._default_selection(plan)
        self.dialog = None
        self._banner_container = None
        self._notices_container = None
        self._rows_container = None
        self._no_order_container = None
        self._totals_container = None
        self._submit_button = None
        self._submit_tooltip = None
        #: One-shot latch. See ``_submit``: NiceGUI runs a sync click handler
        #: directly on the event loop, so the dialog stays on screen -- and
        #: clickable -- for the whole of a blocking submit.
        self._submitted = False

    # -- public -----------------------------------------------------------
    def open(self):
        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            self.dialog = dialog
            ui.label(self.title).classes('text-xl font-bold')
            # A CONTAINER, not a bare render: Refresh re-reads the clock, and a
            # banner drawn straight into the card could never be taken down again.
            self._banner_container = ui.column().classes('w-full')
            self._render_market_banner()
            self._render_base_panel()
            ui.switch('Allow fractional shares', value=self.allow_fractional,
                      on_change=lambda e: self._refresh(bool(e.value)))
            ui.label(MARKET_ORDER_TIMING_NOTE).classes('text-xs text-orange-400')
            self._notices_container = ui.column().classes('w-full')
            self._rows_container = ui.column().classes('w-full gap-0')
            self._no_order_container = ui.column().classes('w-full')
            self._totals_container = ui.column().classes('w-full')
            self._render_notices()
            self._render_rows()
            self._render_no_order_rows()
            self._render_totals()
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Refresh', on_click=lambda: self._refresh(self.allow_fractional)).props('outline')
                ui.button('Cancel', on_click=dialog.close).props('flat')
                self._submit_button = ui.button('Submit', on_click=self._submit) \
                    .props('color=primary')
                # Built ONCE and re-texted on refresh. ``Element.tooltip()`` creates
                # a fresh q-tooltip on every call (nicegui/element.py), so calling
                # it again from _refresh would stack one per refresh.
                with self._submit_button:
                    self._submit_tooltip = ui.tooltip('')
                self._sync_submit_button()
        dialog.open()
        return dialog

    # -- internals --------------------------------------------------------
    def _render_market_banner(self):
        """The market-hours banner. Nothing at all when the BROKER said "open".

        Two things reach this banner and they are not the same:

        * the gate BLOCKING -- closed, or unknown -- which carries its own message
          (including its own fallback wording, so it needs no second line); and
        * the gate ALLOWING on an answer that did NOT come from the broker
          (``market_provenance_notice``). That is the case worth shouting about:
          the built-in NYSE calendar says "scheduled trading day" and Submit goes
          live, on a timetable that cannot see an unscheduled halt.

        Nothing is drawn when the broker itself confirmed the market open -- not an
        empty box, which would leave a stray banner with nothing to say.
        """
        self._banner_container.clear()
        if not self.market.allowed:
            notice = (self.market.message, self.market.severity)
        else:
            notice = market_provenance_notice(self.market)
        if notice is None:
            return
        text, severity = notice
        css = MARKET_BANNER_CLASSES.get(severity, 'alert-banner warning')
        with self._banner_container:
            with ui.element('div').classes(f'{css} w-full p-3'):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('schedule')
                    ui.label(text).classes('text-sm').mark(MARKER_MARKET_BANNER)

    def _base_block(self) -> Optional[str]:
        """Why this BASE may not be submitted at all, or ``None``. Not the clock.

        Today there is exactly one such reason and it is the one W1 makes live: in
        MARKET valuation a symbol the account HOLDS but cannot price contributes 0
        to ``base_notional``, so every label's target is understated by its share of
        the missing money. The dry run cannot show that -- every row is
        proportionally too small, so the table looks perfectly self-consistent.

        Read off the base, not the plan: the plan's no-price rows are neither
        pre-ticked nor tickable, so ``filter_plan_rows`` drops them and the FILTERED
        plan Submit actually sends looks clean.

        Taken at wizard-open and NOT re-derived by ``_refresh`` -- ``on_refresh``
        returns ``(plan, market)`` and no new base. That errs the safe way in the
        expensive direction (a quote that recovers leaves Submit off until the
        dialog is reopened) and ``run_allocation`` re-derives it from the SOLVE-time
        base, so a quote that FAILS between opening and submitting is still refused.
        """
        return held_no_price_block(self.base.unpriced_held_symbols)

    def _sync_submit_button(self):
        """Point the Submit button at the CURRENT gate. Idempotent.

        Disabled, not hidden: the user must see that Submit exists and WHY it is
        off -- the banner right above says when it returns. Called from ``open``
        and from every ``_refresh``, because the gate moves while the dialog sits
        there and the button is only a mirror of it.

        TWO independent refusals, and the tooltip names whichever is in force: the
        market-hours gate (moves while the dialog is open) and ``_base_block`` (a
        held symbol with no quote, which does not). The base block is checked FIRST
        because it is the one the user can act on straight away.
        """
        if self._submit_button is None:
            return
        base_block = self._base_block()
        blocked = base_block is not None or not self.market.allowed
        self._submit_button.set_enabled(not blocked and not self._submitted)
        if self._submit_tooltip is not None:
            reason = base_block if base_block is not None else (
                self.market.message if not self.market.allowed else '')
            self._submit_tooltip.set_text(reason)
            self._submit_tooltip.set_visibility(blocked)

    def _render_notices(self):
        """The four plan-level sentences, up top.

        About a quarter of this book cannot trade fractionally, some rows were
        bumped UP to a whole share and some had their weight moved to keep a label
        on target. Every one of those is the plan doing something the user did not
        type, so none of them is a footnote.
        """
        self._notices_container.clear()
        summary = fractional_summary(self.plan)
        with self._notices_container:
            for text in (whole_share_notice(summary), bump_notice(summary),
                         no_order_notice(summary), redistribution_notice(summary)):
                if not text:
                    continue
                with ui.element('div').classes('alert-banner warning w-full p-2 mt-2'):
                    ui.label(text).classes('text-sm').mark(MARKER_PLAN_NOTICE)

    def _render_no_order_rows(self):
        """Symbols the plan wanted to trade and could not, with their money.

        The table above lists what will be SENT, so on one of these rows its
        quantity, side and value columns are all blank. This section says what was
        WANTED instead -- the target, what will actually be held, and how much never
        left the cash -- which is the whole point of surfacing them at all.
        """
        self._no_order_container.clear()
        dropped = no_order_rows(self.plan)
        if not dropped:
            return
        total = sum(r['unmet_notional'] for r in dropped)
        with self._no_order_container:
            with ui.expansion(f'Not traded ({len(dropped)}) - {total:,.2f} unallocated') \
                    .classes('w-full mt-2'):
                with ui.row().classes('w-full text-xs font-bold border-b py-1'):
                    for header, width in (('Symbol', 'w-24'), ('Price', 'w-28'),
                                          ('Outcome', 'w-32'), ('Target', 'w-28'),
                                          ('Projected', 'w-28'), ('Unallocated', 'w-28'),
                                          ('Why', 'flex-1')):
                        ui.label(header).classes(width)
                for row in dropped:
                    with ui.row().classes('w-full text-sm items-center border-b py-1'):
                        ui.label(row['symbol']).classes('w-24 font-medium')
                        ui.label('-' if row['price'] is None
                                 else f"{row['price']:,.2f}").classes('w-28')
                        ui.label(row['outcome']).classes('w-32 text-xs text-orange-400')
                        ui.label(f"{row['target_notional']:,.2f}").classes('w-28')
                        projected = row['projected_notional']
                        ui.label('-' if projected is None
                                 else f"{projected:,.2f}").classes('w-28')
                        ui.label(f"{row['unmet_notional']:,.2f}").classes('w-28 text-orange-400')
                        ui.label(row['reasons']).classes('flex-1 text-xs text-gray-400')

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
        # DANGER, not warning, and above the warnings: this one does not merely
        # qualify the numbers below it, it says they are wrong and Submit is off.
        base_block = self._base_block()
        if base_block is not None:
            with ui.element('div').classes('alert-banner danger w-full p-3'):
                with ui.row().classes('items-center gap-2'):
                    ui.icon('price_change')
                    ui.label(base_block).classes('text-sm').mark(MARKER_BASE_BLOCK)
        for warning in self.base.warnings:
            ui.label(warning).classes('text-xs text-orange-400')
        for warning in self.plan.warnings:
            ui.label(warning).classes('text-xs text-orange-400')

    def _render_rows(self):
        """The dry-run table, left to right: what you HOLD, what will be DONE,
        where that LEAVES you, and what buying power it costs.

        ``Projected`` is suffixed with the plan's own valuation mode because the
        column silently means two different things -- post-trade COST BASIS in cost
        mode, ``target quantity x price`` in market mode -- and the toggle that
        decides which lives on another page. An unlabelled column that changes
        meaning elsewhere is worse than no column.

        Eighteen columns do not fit a laptop even maximised, so the container
        scrolls SIDEWAYS and every row is ``min-w-max``: the alternative -- letting
        the flex row squeeze -- silently truncates money figures, which is the one
        failure mode a dry run may not have. ``classes()`` de-duplicates, so
        re-applying it on every refresh is a no-op.
        """
        self._rows_container.clear()
        self._rows_container.classes('overflow-x-auto')
        rows = dry_run_rows(self.plan)
        with self._rows_container:
            if not rows:
                ui.label('No orders required - the account already matches its targets.') \
                    .classes('text-sm text-gray-400')
                return
            if any(r['fractional'] and not r['suppressed'] for r in rows):
                ui.label(FRACTIONAL_IS_MARKET_ONLY_NOTE).classes('text-xs text-orange-400')
            with ui.row().classes('w-full min-w-max text-xs font-bold border-b py-1'):
                for header, width in (('', 'w-10'), ('Symbol', 'w-24'),
                                      # WHERE THE ROW STARTS -- the basis being
                                      # traded against, which the table never had.
                                      ('Held', 'w-20'), ('Cost', 'w-24'),
                                      ('Value', 'w-24'),
                                      ('Side', 'w-16'),
                                      ('Qty', 'w-24'), ('Order', 'w-24'),
                                      ('Sizing', 'w-20'), ('Outcome', 'w-28'),
                                      ('Est. value', 'w-24'), ('Target', 'w-24'),
                                      (f'Projected ({self.plan.valuation_mode})', 'w-32'),
                                      ('Weight', 'w-32'),
                                      ('BP cost', 'w-24'), ('BP ×', 'w-20'),
                                      ('BP %', 'w-16'),
                                      ('Reasons', 'flex-1 min-w-64')):
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

        ``Cost`` and ``Value`` are the CURRENT holding, not the trade: what was paid
        and what it is worth now. They are the pair that answers "am I topping up a
        winner or averaging down?", which the table could not answer before. A
        ``Value`` of ``-`` means there is no price, never that the holding is
        worthless.
        """
        blocked = row['suppressed'] or row['skipped']
        with ui.row().classes('w-full min-w-max text-sm items-center border-b py-1'
                              + (' opacity-60' if blocked else '')):
            checkbox = ui.checkbox(
                value=row['symbol'] in self.selected,
                on_change=lambda e, s=row['symbol']: self._toggle(s, bool(e.value)),
            ).classes('w-10').mark(MARKER_ROW_TICK)
            # A suppressed row has no order to submit, so it must not be tickable
            # -- greying it is not enough, the box would still be clickable.
            checkbox.set_enabled(not blocked)
            ui.label(row['symbol']).classes('w-24 font-medium')
            # THE BASIS THIS ROW IS TRADING AGAINST.
            ui.label(_shares(row['current_quantity'])).classes('w-20 text-gray-400') \
                .mark(MARKER_ROW_HELD)
            ui.label(f"{row['current_cost_basis']:,.2f}").classes('w-24 text-gray-400') \
                .mark(MARKER_ROW_COST)
            value = row['current_value']
            # '-', never 0.00: no price is "not measurable", not "worthless".
            ui.label('-' if value is None else f"{value:,.2f}") \
                .classes('w-24 text-gray-400').mark(MARKER_ROW_VALUE)
            ui.label(row['side'] or '-').classes(
                'w-16 ' + ('text-green-500' if row['side'] == 'BUY'
                           else 'text-red-500' if row['side'] == 'SELL'
                           else 'text-gray-400'))
            ui.label(_shares(row['quantity'])).classes('w-24')
            if row['suppressed']:
                order_kind, order_class = 'no order', 'text-orange-400'
            elif row['fractional']:
                order_kind, order_class = 'fractional', 'text-blue-400'
            else:
                order_kind, order_class = 'whole shares', 'text-gray-400'
            ui.label(order_kind).classes('w-24 text-xs ' + order_class) \
                .mark(MARKER_ORDER_KIND)
            # The GRID the row was sized on, which is the column to scan when a
            # quarter of the book cannot trade fractionally at all.
            ui.label(row['sizing']).classes(
                'w-20 text-xs ' + ('text-blue-400' if row['sizing'] == 'fractional'
                                   else 'text-orange-400'))
            # WHICH RULE produced the quantity. A bumped row holds MORE than the
            # weights asked for, and that must never be silent.
            ui.label(row['outcome']).classes(
                'w-28 text-xs ' + ('text-orange-400' if row['outcome'] != 'normal'
                                   else 'text-gray-400'))
            ui.label(f"{row['estimated_value']:,.2f}").classes('w-24')
            ui.label(f"{row['target_notional']:,.2f}").classes('w-24')
            projected = row['projected_notional']
            # The header names the mode this figure is in; the tooltip carries the
            # OTHER one, so cost and value are one hover apart instead of one
            # page-level toggle and a re-solve apart.
            projected_label = ui.label('-' if projected is None
                                       else f"{projected:,.2f}").classes('w-32')
            other = ('projected_cost' if self.plan.valuation_mode == VALUATION_MODE_MARKET
                     else 'projected_market')
            if row[other] is not None:
                with projected_label:
                    ui.tooltip(f"{other.replace('_', ' ')}: {row[other]:,.2f}")
            # ASKED -> ACTUAL. They differ whenever the grid, a bump or the label
            # redistribution moved this row, and hiding that would be rewriting the
            # user's weights behind their back.
            ui.label(f"{row['weight_pct']:.2f}% → {row['projected_weight_pct']:.2f}%") \
                .classes('w-32 text-xs ' + ('text-orange-400' if row['redistributed']
                                            else 'text-gray-400'))
            ui.label(f"{row['bp_cost']:,.2f}").classes('w-24')
            # Immediately beside BP cost ON PURPOSE: the x IS the explanation of why
            # that figure is not the Est. value, which is the misreading requirement
            # 1b is about.
            text, css, tip = _leverage_cell(row)
            with ui.label(text).classes('w-20 text-xs ' + css).mark(MARKER_LEVERAGE):
                ui.tooltip(tip)
            ui.label(f"{row['bp_usage_pct']:.1f}%").classes('w-16')
            ui.label(row['reasons']).classes(
                'flex-1 min-w-64 text-xs ' + ('text-orange-400' if row['suppressed']
                                              else 'text-gray-400'))

    def _render_totals(self):
        """The footer, over the TICKED rows only.

        Two additions beyond the money that was already here. ``Held cost`` /
        ``Held value`` total the basis the plan is trading against, so the new
        per-row columns have a bottom line instead of a column of figures to add up
        by eye; both are summed from ``dry_run_rows`` -- the same numbers the table
        drew -- rather than recomputed, so the footer can never disagree with the
        rows above it. ``Est. fees`` appears only when the precheck actually
        returned one: ``None`` there means "not prechecked", never "free", and a
        0.00 would be a fabricated broker figure.

        And ``BP_IS_A_CHARGE_NOTE_FMT``, which is requirement 1b. Nothing in the
        arithmetic moved to make it true -- ``bp_factor`` never touched a target or
        a quantity -- but the pairing on screen was misleading, and a labelling
        defect in a money table is still a defect.
        """
        self._totals_container.clear()
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        try:
            totals = summarise_plan(selected_plan, cash=self.base.cash)
        except ValueError:
            totals = None
        summary = fractional_summary(selected_plan)
        shown = dry_run_rows(selected_plan)
        held_cost = sum(r['current_cost_basis'] for r in shown)
        # None is EXCLUDED, not counted as zero: an unpriced holding is missing from
        # this total, and summing it as 0.0 would report a smaller basis as a fact.
        priced = [r['current_value'] for r in shown if r['current_value'] is not None]
        fees = [r['estimated_fees'] for r in shown if r['estimated_fees'] is not None]
        buy_value = selected_plan.total_buy_value
        required = selected_plan.required_buying_power
        with self._totals_container:
            with ui.row().classes('w-full gap-6 mt-2 text-sm'):
                ui.label(f"Sell value: {selected_plan.total_sell_value:,.2f}")
                ui.label(f"Buy value: {buy_value:,.2f}")
                ui.label(f"Required BP: {required:,.2f} "
                         f"/ {selected_plan.available_buying_power:,.2f} "
                         f"({selected_plan.bp_usage_pct:.1f}%)")
                if totals is not None:
                    ui.label(f"Est. cash after: {totals['estimated_cash_after']:,.2f}")
                else:
                    ui.label('Est. cash after: unknown (broker published no cash balance)') \
                        .classes('text-orange-400')
            with ui.row().classes('w-full gap-6 text-sm'):
                ui.label(f"Held cost: {held_cost:,.2f}").classes('text-gray-400')
                ui.label(f"Held value: {sum(priced):,.2f}"
                         + ('' if len(priced) == len(shown)
                            else f" ({len(shown) - len(priced)} unpriced, excluded)")) \
                    .classes('text-gray-400')
                if fees:
                    ui.label(f"Est. fees: {sum(fees):,.2f}").classes('text-gray-400')
            # Requirement 1b. Only when there is a charge to explain -- a sell-only
            # plan reserves nothing and the sentence would be noise.
            if buy_value > MONEY_EPSILON:
                ui.label(BP_IS_A_CHARGE_NOTE_FMT.format(
                    buy_value=buy_value, required=required,
                    ratio=required / buy_value)).classes('text-xs text-gray-400') \
                    .mark(MARKER_BP_NOTE)
            with ui.row().classes('w-full gap-6 text-sm'):
                # SIGNED: a bump's over-allocation nets against a rounding shortfall,
                # which is what "how far off target will I be" actually means.
                ui.label(f"Off target after rounding: {summary['residual_notional']:,.2f} "
                         f"({summary['residual_pct']:.2f}% of base)") \
                    .classes('text-orange-400' if abs(summary['residual_pct']) >= 1.0 else '')
                ui.label(f"Fractional: {summary['fractional_rows']} / "
                         f"whole shares: {summary['whole_share_rows']}"
                         + (f" / eligibility unknown: {summary['unknown_rows']}"
                            if summary['unknown_rows'] else ''))
                if summary['bumped_rows']:
                    ui.label(f"Bumped to 1 share: {summary['bumped_rows']} "
                             f"(+{summary['bumped_notional']:,.2f})").classes('text-orange-400')
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
        """Re-solve, and re-read the CLOCK with it.

        ``on_refresh`` returns ``(plan, market)`` from ONE solve, which is the same
        guarantee the initial open has: the banner and the gate can never describe
        different instants. Refreshing only the plan is what left a wizard opened
        at 09:00 with Submit disabled at 10:00, and a wizard opened at 15:00 with
        Submit still enabled at 16:30.

        A refresh that RAISES changes nothing at all -- not the plan, not the gate.
        Unlocking Submit because the clock could not be re-read would be exactly
        backwards.
        """
        self.allow_fractional = allow_fractional
        try:
            self.plan, self.market = self.on_refresh(allow_fractional)
        except Exception as e:
            logger.error(f"Allocation dry-run refresh failed: {e}", exc_info=True)
            ui.notify(f'Refresh failed: {e}', type='negative')
            return
        self.selected = self._default_selection(self.plan)
        self._render_market_banner()
        self._sync_submit_button()
        self._render_notices()
        self._render_rows()
        self._render_no_order_rows()
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
        to be able to tick a row and press Submit for real. Neither does a submit
        the MARKET GATE or ``_base_block`` refuses.
        """
        # FIRST, before touching any other state: the button is disabled, but a
        # stale client or a keyboard activation must not get past this either. The
        # real enforcement is in run_allocation, which re-reads the clock AND
        # re-derives the base block; this is the polite half, and it is deliberately
        # ahead of the one-shot latch so a refused click leaves the dialog exactly
        # as it found it.
        base_block = self._base_block()
        if base_block is not None:
            ui.notify(base_block, type='negative')
            return
        if not self.market.allowed:
            ui.notify(self.market.message, type=self.market.severity)
            return
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
    market: MarketGateResult,
    on_refresh: Callable[[bool], Tuple[AllocationPlan, MarketGateResult]],
    on_submit: Callable[[AllocationPlan], None],
    title: str = 'Portfolio allocation - dry run',
) -> AllocationWizard:
    """Open the dry-run dialog. Returns the wizard so the caller can keep a handle.

    ``market`` is REQUIRED and has no default: a default would mean a caller that
    forgets it submits into a closed market. Build it with
    ``evaluate_market_gate(is_open=..., next_open=..., source=..., now=...)`` from
    ``ui.utils.portfolio_allocation_view``, fed by
    ``portfolio_allocation_service.fetch_market_hours(account)`` -- pass
    ``is_open=None`` when that returns ``None`` OR when ``hours.is_known`` is False.

    ``on_refresh(allow_fractional)`` must return ``(plan, market)``: a re-solve
    re-reads the broker anyway, and the market hours it reads there are the ONLY
    thing that can move the gate while this dialog is open. Returning just the plan
    froze the gate at whatever it was when the wizard opened.
    """
    wizard = AllocationWizard(base, plan, market=market, on_refresh=on_refresh,
                              on_submit=on_submit, title=title)
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
                        on_invest: Callable[[float], None],
                        working_note: Optional[Tuple[str, str]]) -> None:
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

    ``working_note`` is a ``(text, severity)`` pair, or ``None``. Orders still
    working contribute ZERO to a run's ledger, so its income stays unconsumed until
    they settle -- with a quarter of the book on whole shares that is the COMMON
    outcome, and this is where the user is told. Either pure builder produces it:
    ``portfolio_allocation.unconsumed_income_notice`` for the account's standing
    backlog (what the panel shows) or
    ``portfolio_allocation_view.working_orders_notice`` for the run just submitted.
    The decision and the wording are pure and tested without NiceGUI; this function
    only draws them.

    It is a REQUIRED keyword. With a default of ``None`` the page glue simply never
    passed it, and the panel showed an "unallocated" figure that never went down
    and never said why -- which is the one fact decision D3 exists to surface.
    """
    with ui.card().classes('w-full'):
        if working_note is not None:
            text, severity = working_note
            css = MARKET_BANNER_CLASSES.get(severity, 'alert-banner warning')
            with ui.element('div').classes(f'{css} w-full p-2'):
                ui.label(text).classes('text-sm').mark(MARKER_WORKING_ORDERS)
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
