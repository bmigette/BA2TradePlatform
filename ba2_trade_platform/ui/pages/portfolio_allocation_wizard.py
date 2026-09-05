"""Portfolio Allocation: the REVIEW-AND-COMMIT gate, income panel, outcome table.

Section G owns this module. The allocation PAGE
(``ui/pages/portfolio_allocation.py``) renders the label/symbol editor and calls
``open_invest_scope()``, ``open_allocation_wizard()`` and
``render_income_panel()`` from here.

THIS MODULE SETS NO TARGETS, AND THAT IS THE POINT
==================================================
It used to open a three-step dialog whose step 1 was "Rebalance - set targets"
and whose step 2 was the symbol weights. Both are on the PAGE now -- the label
target inputs, the symbol share inputs, the cash reserve, the per-row ``last``
target and P&L, and the six buttons over them (``Even split`` / ``Load last`` at
label level; ``Even split`` / ``Fill rest`` / ``Load last`` / ``Wipe`` per
label). Pressing Allocate goes STRAIGHT to the dry run.

Everything that expresses INTENT lives on the page; everything that shapes
EXECUTION lives at this gate, and there is exactly one of the latter:
``allow fractional shares``, which stays because it changes WHICH ORDERS are
produced rather than what is being aimed at -- and toggling it re-solves.

The prize is that the two screens no longer derive the same figures
independently. The wizard's ``target 13.50% of base`` and the page's
``tgt 13.5%`` were one number computed twice, on two denominators, and a whole
class of "these two screens disagree" bug lived in the gap.

``InvestScope`` is the one dialog left with an input, and it is not a target
editor: an INVEST run spends a specific amount on a single label, so the run has
to be told which label and how much. Neither is a stored target of anything.

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

TWO TABS: the order table on one, the notices and the totals on the other. The
user, looking at it live: "better to make 2 tabs here as it's unreadable, one tab
with table and one with the text etc." Submit, Cancel and Refresh -- and the two
banners that say Submit is OFF -- stay OUTSIDE both, because a control or a refusal
behind an unopened tab has not been shown. See the block comment above
``DIALOG_CARD_CLASSES``.

It stays MODAL. A commit gate for real orders should be a deliberate stop, not
something reachable by scrolling.
"""
import asyncio
from typing import Callable, Dict, List, Optional, Tuple

from nicegui import ui

# NOTHING that edits a target is imported here any more, and the absence is
# enforced by a test that greps this file. The engine's six target-editing
# helpers and their can-do predicates now reach the user through
# ``ui/utils/portfolio_allocation_view.py`` and the allocation PAGE. An import is
# a dependency and a dependency is an invitation: leaving them here would put this
# module one edit away from having a target editor again. (Their names are
# deliberately not spelled out in this comment -- the test looks for the strings.)
from ...core.portfolio_allocation import (
    ALLOCATION_BASIS_POSITION,
    ALLOCATION_MODE_INVEST_LABEL,
    LEVERAGE_LEVERAGED,
    LEVERAGE_NONE,
    LEVERAGE_NOT_APPLICABLE,
    LEVERAGE_PENALISED,
    LEVERAGE_UNKNOWN,
    MONEY_EPSILON,
    QUANTITY_EPSILON,
    VALUATION_MODE_MARKET,
    AllocationPlan,
    BaseSnapshot,
    LabelTarget,
    blocking_messages,
    bump_notice,
    dry_run_rows,
    filter_plan_rows,
    fractional_summary,
    LABEL_TOTAL_TOLERANCE_PCT,
    held_no_price_block,
    invest_validation_messages,
    is_blocking_message,
    no_order_notice,
    no_order_rows,
    redistribution_notice,
    summarise_plan,
    unconsumed_income_notice,
    validate_label_targets,
    whole_share_notice,
)
from ..utils.portfolio_allocation_view import (
    MARKET_BANNER_CLASSES, PLAN_WARNING_CLASSES, PLAN_WARNING_COLORS,
    STATUS_OVER_COLOR, MarketGateResult, class_color_style,
    important_color_style, market_provenance_notice, plan_warning_lines,
)
from ...core.portfolio_allocation_service import (
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SKIPPED,
    OUTCOME_SUBMITTED,
    OUTCOME_UNACTIONABLE,
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

#: Tooltip on a sell's (empty) leverage cell. This column is about the CHARGE, and
#: a sell makes none; how much it FREES is the BP effect column two cells left,
#: which is where the reader is sent rather than being told the fact does not
#: exist.
LEVERAGE_TOOLTIP_SELL = (
    'A sell FREES buying power rather than charging it, so there is no charge '
    'ratio to state - the BP effect column says how much it frees.')

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

#: The footer's buying-power line, and the marker that finds it.
#:
#: THE DENOMINATOR IS THE BUDGET, not the figure the broker published. It used to
#: read "Required BP: 359.26 / 394.87 (91.0%)" on a plan that was selling 2,112.58
#: -- a plan with 2,507 to spend, describing itself as 91% used, on the strength of
#: money it was about to hand back. ``AllocationPlan.total_buying_power`` is the
#: sum and this line shows BOTH halves of it, because a total the user cannot find
#: on any other screen is a number they have to take on trust.
MARKER_BP_BUDGET = 'dry-run-bp-budget'
BP_BUDGET_FMT = 'Required BP: {required:,.2f} / {budget:,.2f} ({pct:.1f}%)'
#: Appended only when the plan HAS sells. "1,000.00 on hand + 0.00 freed" on every
#: buy-only plan is noise, and noise is what hides the real case.
BP_BUDGET_BREAKDOWN_FMT = (' — {available:,.2f} on hand + {released:,.2f} freed by '
                           'this plan’s sells')

#: Shown when the plan does not fit even the full budget. Names the sells, because
#: un-ticking one is the commonest way to get here from a plan that DID fit.
BP_OVER_BUDGET_NOTE = (
    'Required buying power exceeds the budget (what the broker published plus what '
    'this plan’s sells free) - the smallest buys will be truncated as buying '
    'power runs out.')

#: Marker on the dry-run table's ``BP effect`` cell.
MARKER_BP_EFFECT = 'dry-run-bp-effect'

#: The ``BP effect`` cell's tooltips. SIGNED is the whole point of the column:
#: negative is buying power consumed, positive is buying power handed back. The
#: sign convention is the broker's own -- TastyTrade's
#: ``change_in_buying_power`` is negative for a buy -- so the dry run and a
#: precheck read the same way round. Every branch has a tooltip: a bare signed
#: figure in a money column is a question the user cannot answer from the screen,
#: and getting this one backwards is expensive.
BP_EFFECT_TOOLTIP_CHARGE_FMT = (
    '{symbol} charges {amount:,.2f} against buying power ({value:,.2f} of stock at '
    '{ratio:.2f}x). It is RESERVED, not spent.')
BP_EFFECT_TOOLTIP_RELEASE_FMT = (
    '{symbol} frees {amount:,.2f} of buying power - a sale returns what the '
    'position was reserving ({value:,.2f} at {ratio:.2f}x). Sells go to the broker '
    'before any buy, so this plan’s buys are sized with it counted in.')
BP_EFFECT_TOOLTIP_NONE = (
    'No order on this row, so it moves no buying power in either direction.')

#: NiceGUI marker on the market-hours banner's sentence, and on each of the four
#: plan notices. Located by marker, not by text: the gate's message is ALSO the
#: Submit button's tooltip, and every notice quotes wording that recurs in the
#: reasons column, so a text search cannot tell whether the banner was drawn.
MARKER_MARKET_BANNER = 'dry-run-market-banner'
MARKER_PLAN_NOTICE = 'dry-run-plan-notice'
#: One line of ``plan_warning_lines`` -- the empty-label / residual warnings and
#: the ONE collapsed broker-precheck line that replaced eight identical ones.
#: Marked, not text-matched: the collapsed line names symbols that also appear in
#: the table three inches below it.
MARKER_PLAN_WARNING = 'dry-run-plan-warning'
#: The block the notices and warnings live in, and the BADGE on the notices TAB
#: that says how many are in it. Both marked so a test can prove the count exists
#: without depending on how many notices a fixture happens to produce.
MARKER_NOTICE_BLOCK = 'dry-run-notice-block'
MARKER_NOTICE_COUNT = 'dry-run-notice-count'
#: The scrolling viewport the order table lives in, now filling its own tab.
MARKER_ROWS_VIEWPORT = 'dry-run-rows-viewport'
#: The order table's sticky header row.
MARKER_TABLE_HEAD = 'dry-run-table-head'
#: The order table's sticky FOOTER row: the column totals over the TICKED rows.
#: Rebuilt on every tick, so it can never describe a selection other than the one
#: the boxes show.
MARKER_TABLE_FOOT = 'dry-run-table-foot'
#: The selection toolbar above the table: 'Select all' / 'Deselect all', then one
#: ``all`` / ``none`` pair per label in the plan. Buttons, not links: they change
#: what Submit will SEND.
MARKER_SELECT_ALL = 'dry-run-select-all'
MARKER_DESELECT_ALL = 'dry-run-deselect-all'
MARKER_LABEL_SELECT = 'dry-run-label-select'
SELECT_ALL_LABEL = 'Select all'
DESELECT_ALL_LABEL = 'Deselect all'
#: The caption of the footer's first cell: how many of the sendable rows are ticked.
FOOTER_CAPTION_FMT = 'Total ({ticked}/{sendable} ticked)'
FOOTER_TOOLTIP = ('Column totals over the TICKED rows only - exactly what Submit '
                  'will send. Un-ticked rows are excluded here and are never ordered.')
#: The free-text Reasons cell, on the order table and on the 'Not traded' table.
#: By marker: every word in it also appears in a notice above, so a text search
#: cannot tell which of the two drew it.
MARKER_ROW_REASONS = 'dry-run-row-reasons'

#: Marker on the income panel's working-orders line, for the same reason.
MARKER_WORKING_ORDERS = 'income-working-orders'

#: Marker on the dry run's reserve chip.
MARKER_RESERVED = 'dry-run-reserved'

#: THE VALIDATE STEP: test the ticked orders against the broker before sending
#: any of them, then offer to drop the ones that would be refused.
MARKER_VALIDATE_BUTTON = 'dry-run-validate'
MARKER_VALIDATION_RESULT = 'dry-run-validation-result'
MARKER_VALIDATION_FINDING = 'dry-run-validation-finding'
MARKER_VALIDATION_DROP = 'dry-run-validation-drop'
VALIDATE_BUTTON_LABEL = 'Validate'
VALIDATE_TOOLTIP = (
    'Test the ticked orders against the broker without sending any of them. '
    'Anything that would be refused is listed, and you can drop just those and '
    'submit the rest.')
#: The clean result. Deliberately does NOT say "these will fill" -- passing the
#: checks is the absence of a known refusal, not a promise about the fill.
VALIDATION_CLEAN_FMT = (
    'Nothing found against {count} order(s). {precheck} No check can promise a '
    'fill: a market order is priced when it reaches the exchange.')
VALIDATION_FOUND_FMT = '{count} order(s) would be refused:'
#: What the broker itself was asked, spelled out. "0 of 7 broker-prechecked" is a
#: materially different statement from a clean bill of health, and Alpaca -- the
#: live account -- is always 0: it publishes no order-preview endpoint at all, so
#: nothing can be asked of it without actually sending the order.
VALIDATION_PRECHECK_FMT = '{done} of {total} buy(s) were checked by the broker itself.'
VALIDATION_NO_PRECHECK = (
    'This broker offers no order preview, so every check was made locally.')
#: The button that acts on the findings.
VALIDATION_DROP_FMT = 'Un-tick the {count} flagged order(s) and keep the rest'

#: THE RETRY STEP on the results table: try again on just the rows that failed.
MARKER_OUTCOME_RETRY = 'outcome-retry'
RETRY_FAILED_FMT = 'Retry the {count} that failed'
RETRY_TOOLTIP = (
    'Re-solve against the positions as they are now and open a fresh dry run. '
    'What filled is already out of the new plan; what failed is still in it. '
    'Nothing is re-sent without another Submit.')

#: Marker on the TARGET-TOTAL block: label percentages off 100%, or a label's
#: symbol weights off 100% within it. Decision 3 -- "Submit is blocked otherwise"
#: -- had no live enforcement anywhere in the app until this: ``compute_allocation``
#: deliberately does not renormalise ("blocking submission is the validator's job"),
#: and nothing called ``validate_label_targets`` before Submit. Two boxes at 100%
#: each used to buy DOUBLE the base with no warning on screen.
MARKER_TARGET_BLOCK = 'target-block'
#: Prefixes the joined validator errors so the banner reads as one sentence
#: rather than a bare semicolon-joined dump of ``ERROR_LABEL_*`` strings.
TARGET_BLOCK_PREFIX = 'Submit is off until the targets are fixed: '

#: Marker on the footer line that puts the expected cash next to the reserve.
MARKER_CASH_VS_RESERVE = 'dry-run-cash-vs-reserve'

#: The footer's arithmetic check for requirement 3. The reserve is a share of
#: ``base_notional`` -- buying power PLUS managed value -- so on a fully invested
#: account RAISING it generates SELL orders to free it. That is correct and it has
#: to be obvious rather than inferred from two numbers on different lines.
#:
#: The trailing clause is not decoration. The two figures are measured off
#: DIFFERENT things: the left is the broker's cash balance after the plan, the
#: right is a share of a base that counts borrowing capacity the cash balance never
#: held. On a cash account they nearly coincide; on a margin account they need not,
#: and ``estimated_cash_after`` is reachably negative on a plan whose reserve is
#: fully satisfied. Without the clause the delta reads as a shortfall to fund.
CASH_VS_RESERVE_FMT = ('Est. cash after {cash:,.2f} vs reserve {reserved:,.2f} '
                       '({delta:+,.2f}) — the reserve is a share of the base '
                       '(buying power + holdings), not a cash balance, so on a '
                       'margin account these two need not meet')

#: The dry run's reserve chip, drawn only when there IS one. A "Reserved: 0.00" on
#: every fully-allocated plan is noise, and noise is what hides the real case.
RESERVED_FMT = 'Reserved (not allocated): {amount:,.2f} ({pct:.2f}% of base)'

#: Marker on the invest dialog's "Continue saves your weights" note. By marker:
#: the sentence names Cancel, and 'Cancel' is also the label of the button beside
#: it.
MARKER_CONTINUE_SAVES = 'invest-continue-saves'

#: Shown above Continue. Continue is a WRITE: it persists the chosen label's symbol
#: weights so "load last" has something to load. It does NOT write a label target
#: or a reserve -- an invest run spends an explicit amount the user named, so the
#: label's percentage played no part and restating it would record a choice nobody
#: made. Cancel abandons the RUN, not the numbers.
CONTINUE_SAVES_NOTE = (
    'Continue SAVES this label\u2019s symbol weights for next time, then opens the '
    'dry run. Cancel abandons the run, not the saved numbers.')

#: Marker on the base panel's BLOCKING banner -- today only the market-mode
#: "a held symbol has no quote" refusal (``held_no_price_block``). Located by
#: marker rather than by text: the symbol names it lists also appear in the table
#: right below it, so a text search cannot tell whether the banner was drawn.
MARKER_BASE_BLOCK = 'base-block'

#: Shown under the INVEST_LABEL amount box.
INVEST_SCOPE_NOTE = ('Pre-filled with the unallocated income total. Buys only - '
                     'an INVEST run never sells.')

#: Shown beside the dry run's fractional switch when the broker does not split
#: shares at all. The switch is disabled there rather than hidden: a control that
#: vanishes is one the user cannot learn exists, and offering a toggle the broker
#: cannot honour would produce a plan sized on a grid that does not exist -- the
#: engine silently falls back to whole shares, so the user would see quantities
#: they never asked for.
NO_FRACTIONAL_SUPPORT_NOTE = 'This broker does not support fractional shares.'


# ---------------------------------------------------------------------------
# HOW THE DIALOG DIVIDES ITS HEIGHT: TWO TABS
#
# The user, looking at it live: "better to make 2 tabs here as it's unreadable,
# one tab with table and one with the text etc."
#
# Before this, the table -- the reason the dialog exists -- was a band in the
# middle. Notices, the fractional switch, the market-order caveat and six warnings
# stacked above it and the totals sat below, and the previous attempt to protect
# it CAPPED the two stacks (18vh + 7vh) so they could not grow. That worked and it
# was still the wrong shape: the caps mean the context is permanently squinting
# through a 5rem scroll box, and the table still only got ~60%.
#
# Tabs make it a choice instead of a compromise. The order table gets the WHOLE
# panel; the notices and the totals get the whole panel; neither is capped and
# neither can squeeze the other, so both stacks are readable at the size they
# actually are.
#
# WHAT IS NOT IN A TAB, and it is a short list on purpose:
#
#   * Submit / Cancel / Refresh. They are the point of the dialog and are drawn as
#     SIBLINGS of the panels, so no tab can hide them and there is exactly one of
#     each to press.
#   * The market-hours banner and the ``held symbol has no quote`` block. Those two
#     do not qualify the numbers, they say SUBMIT IS OFF. A refusal behind an
#     unopened tab is a refusal that was not shown.
#
# The fractional switch IS in a tab -- the ORDERS one -- because it re-solves, and
# what it visibly changes is the table. On the notices tab the user would have to
# change tabs to see what they had just done.
#
# ``vh``/``%`` sizing is gone with the caps; the panels take what the flex column
# leaves. The card still CLIPS (``overflow-hidden``) rather than growing past the
# viewport: what is at the bottom of it is the Submit button, and a card that
# overflows is a card whose Submit button is 23px below the fold, measured.
# ---------------------------------------------------------------------------

#: The card: a flex column that fills the maximized dialog and CLIPS rather than
#: growing. ``min-h-0`` is what lets its children shrink below their content size,
#: without which a flex child's implicit ``min-height:auto`` defeats every cap.
#: ``p-2`` rather than NiceGUI's default padding: the chrome is the part of the
#: budget that does NOT scale with the viewport, so every pixel of it is one the
#: table does not get on a small screen.
DIALOG_CARD_CLASSES = ('w-full h-full flex flex-col flex-nowrap '
                       'overflow-hidden min-h-0 gap-1 p-2')

#: ``ui.tab`` names. Constants because three call sites use them and a typo in one
#: silently produces a panel no tab can reach.
TAB_ORDERS = 'orders'
TAB_NOTICES = 'notices'
TAB_ORDERS_LABEL = 'Orders'
TAB_NOTICES_LABEL = 'Notices & totals'

#: The panel host: the ONE growing child of the card. ``p-0`` because each panel
#: does its own padding, and Quasar's default would be counted twice.
DIALOG_PANELS_CLASSES = 'w-full flex-grow min-h-0 overflow-hidden p-0'

#: One panel. A flex column of its own so the table inside can be the growing
#: child in turn, and ``min-h-0`` at both levels so nothing's implicit
#: ``min-height:auto`` re-inflates the card.
TAB_PANEL_CLASSES = ('w-full h-full flex flex-col flex-nowrap min-h-0 '
                     'overflow-hidden gap-1 p-1')

#: The table's viewport: the growing child of the ORDERS panel, and no minimum of
#: its own -- a ``min-h-[...]`` competes with its siblings instead of cooperating
#: with them, overflows the card, and what gets clipped is the Submit button.
DIALOG_ROWS_CLASSES = 'w-full gap-0 flex-grow min-h-0 overflow-auto relative'

#: The notices panel scrolls as a whole. No inner cap any more: the whole point of
#: giving it a tab is that it no longer has to fit above something else.
DIALOG_NOTICES_CLASSES = 'w-full h-full min-h-0 overflow-y-auto gap-1'

#: The notices/warnings stack inside that panel.
NOTICE_BLOCK_CLASSES = 'w-full'

#: The BADGE on the notices tab, and its tooltip. A tab is a place things hide, and
#: six warnings behind an unopened tab have not been shown; the badge is what says
#: they are there without the tab being opened. Its TEXT is the bare count -- a
#: sentence on a tab is unreadable -- and the sentence is one hover away.
NOTICE_COUNT_FMT = '{count} notice(s) about this plan'
NOTICE_BADGE_CLASSES = 'ml-2'

#: Rows drawn UNDER a sticky header need the header to be opaque and above them.
#: The look itself -- the dark bar, the uppercase grey caption, the row separator,
#: the hover -- is ``styles.css``'s ``.q-table`` treatment, applied to these
#: hand-rolled rows through ``.pf-grid-head`` / ``.pf-grid-row`` so the dry run and
#: the symbol tables in the label panels are literally the same rules. Eighteen
#: columns in a real ``ui.table`` would need eighteen cell slots with per-row
#: colour and tooltips, which is why these rows stay hand-rolled.
GRID_HEAD_CLASSES = 'pf-grid-head w-full min-w-max text-xs py-1 px-1'
GRID_ROW_CLASSES = 'pf-grid-row w-full min-w-max text-sm items-center py-1 px-1'
#: The footer: the header's opaque bar so rows scrolling under it stay hidden, but
#: pinned to the BOTTOM of the viewport (``pf-grid-head`` pins to the top, so the
#: inline style below overrides it). ``normal-case`` because the header's
#: uppercase transform would shout every money figure.
GRID_FOOT_CLASSES = ('pf-grid-head w-full min-w-max text-sm normal-case items-center '
                     'py-1 px-1')
GRID_FOOT_STYLE = 'top: auto; bottom: 0; border-top: 1px solid rgba(255,255,255,0.12);'

#: The NOTICES tab was set in ``text-xs`` throughout and the user could not read it
#: ("Text is too small here"). The notices are one size up and the totals two: the
#: totals are the numbers Submit commits to and they were the smallest thing on the
#: tab.
NOTICE_TEXT_CLASSES = 'text-sm'
TOTALS_TEXT_CLASSES = 'text-base'
TOTALS_NOTE_CLASSES = 'text-sm'

#: The order table's columns, ONCE. Header text, width, and whether the column is
#: numeric -- ``(name, header, width, numeric)``. The header row and the cell row
#: were two separate literal tuples that had to be kept in the same order by hand;
#: right-aligning the money made that a third thing to keep in step, so they read
#: it from here instead.
DRY_RUN_COLUMNS = (
    ('tick', '', 'w-10', False),
    ('symbol', 'Symbol', 'w-24', False),
    # WHERE THE ROW STARTS -- the basis being traded against.
    # w-32: the cell reads "7 -> 6.33622" since it started showing the
    # projected quantity beside the held one (2026-09-05), and at w-20 that
    # wrapped onto two lines and doubled every row's height.
    ('held', 'Held', 'w-32', True),
    ('cost', 'Cost', 'w-24', True),
    ('value', 'Value', 'w-24', True),
    ('side', 'Side', 'w-16', False),
    ('qty', 'Qty', 'w-24', True),
    # ONE column, not three. ``Order`` and ``Sizing`` said the same word on every
    # row that traded ('fractional'/'fractional'), and differed only where the row
    # did NOT trade -- which is where the grid is the explanation, so that case now
    # reads "no order (whole)". ``Outcome`` was 'normal' on every healthy row and is
    # gone: an abnormal sizing outcome is a REASON, and now appears in that column
    # in red beside the reason it caused (2026-09-05).
    ('order', 'Order', 'w-32', False),
    ('estimated_value', 'Est. value', 'w-24', True),
    ('target', 'Target', 'w-24', True),
    ('projected', 'Projected ({mode})', 'w-32', True),
    ('weight', 'Weight', 'w-32', False),
    # SIGNED, and no longer called a cost: a sale FREES buying power, and a sell
    # row reading "BP cost 0.00" said the opposite -- that a sale does nothing to
    # your buying power at all.
    ('bp_effect', 'BP effect', 'w-28', True),
    ('bp_ratio', 'BP ×', 'w-20', True),
    ('bp_pct', 'BP %', 'w-16', True),
    ('reasons', 'Reasons', 'flex-1 min-w-64', False),
)

_COLUMN_CLASSES = {
    name: width + (' text-right' if numeric else '')
    for name, _header, width, numeric in DRY_RUN_COLUMNS
}

#: THE REASONS CELL, in inline CSS rather than Tailwind classes.
#:
#: THE WIDTH BOUND IS THE ACTUAL FIX. Every row is ``min-w-max``, deliberately, so
#: money figures are never squeezed and truncated -- which also means the Reasons
#: cell simply grew to fit the longest reason on the plan and took the table off
#: the right-hand edge of the screen with it. Measured in headless Chrome at
#: 1600x1000: the longest reason on a realistic plan renders 1,178px wide
#: unwrapped, against the 448px this cap allows, and the table's own horizontal
#: scroll width drops from ~3,030px to 2,300px.
#:
#: Clamped to three lines rather than left to wrap freely: a four-line reason on
#: every row is a table nobody can scan. ``-webkit-line-clamp`` gives a visible
#: ellipsis, so the truncation announces itself, and the FULL text is on the
#: tooltip -- losing the explanation is not an option, it is the column's whole
#: purpose. Both the prefixed and the standard ``line-clamp`` are set.
#:
#: INLINE rather than ``max-w-[28rem] whitespace-normal line-clamp-3``. Measured in
#: the same run: Tailwind's LAYOUT utilities do reach this build (``w-24`` computes
#: to 96px, ``min-w-64`` to 256px) but its COLOUR utilities do not
#: (``text-orange-400`` computes to white -- see
#: ``tests/test_ui_colour_classes_paint.py``). So a class WOULD have worked here;
#: it is written inline because this is the one rule in the dialog whose failure is
#: invisible -- an uncapped cell just looks like a wide table -- and because
#: ``line-clamp`` needs the ``-webkit-`` pair, which is not a utility.
REASONS_CELL_STYLE = (
    'white-space: normal; overflow-wrap: anywhere; max-width: 28rem; '
    'display: -webkit-box; -webkit-box-orient: vertical; '
    '-webkit-line-clamp: 3; line-clamp: 3; overflow: hidden;')


def _col(name: str, extra: str = '') -> str:
    """The width + alignment classes of one dry-run column. Pure.

    A KeyError here is the intended failure: a cell drawn for a column the header
    does not declare is a column that will not line up, and finding that out at
    render time beats finding it out by eye in a money table.
    """
    return (_COLUMN_CLASSES[name] + ' ' + extra).strip()


def _shares(quantity: float) -> str:
    """A share count for the table: full precision, no trailing-zero clutter.

    5 decimals because that is TastyTrade's equity quantity precision -- the dry
    run's job is to state what will be SENT, so it may not round tighter than the
    broker's own grid. ``or '0'`` catches exactly 0.0, which strips to ''.
    """
    return f"{quantity:,.5f}".rstrip('0').rstrip('.') or '0'


# ``_pnl_classes`` used to live here, beside the step-1/step-2 P&L captions. It
# moved to ``ui/utils/portfolio_allocation_view.py`` as ``pnl_classes`` with the
# captions themselves: the P&L is a fact about a label and a symbol, not about a
# run, so it belongs on the screen the user reads them from.


def _paint(element, classes: str, *, color: Optional[str] = None):
    """Give one element its classes AND the inline colour they cannot paint.

    THE ONLY DOOR. Every coloured element in the dry-run dialog goes through this
    (or through ``_label`` below), because the alternative -- remembering to add a
    ``.style()`` beside each ``.classes()`` -- is precisely what was not
    remembered: BUY, SELL, the leverage verdicts, the off-target residual and
    every orange notice in this dialog carried a colour class that paints nothing
    on this build and rendered white from the day each was written. See
    ``class_color_style``.

    The classes STAY. They are what the DOM reads as and what a dozen tests locate
    cells by; what changes is that they are no longer relied on to paint.

    ``color`` overrides the class's own hex for the two places this module
    deliberately paints a class in something other than its Tailwind value --
    ``PLAN_WARNING_COLORS`` greys the informational collapse to the page's
    ``NEUTRAL_TEXT_COLOR`` rather than the stylesheet's ``.text-gray-400``.
    """
    element.classes(classes)
    style = important_color_style(color) if color else class_color_style(classes)
    if style:
        element.style(style)
    return element


def _label(text: str, classes: str = '', *, color: Optional[str] = None):
    """``ui.label`` that paints its colour class instead of only wearing it."""
    return _paint(ui.label(text), classes, color=color)


def _reasons_cell(text: str, classes: str, *, alert: str = ""):
    """A Reasons cell: WRAPPED, clamped, and with the whole text one hover away.

    THE ONE DOOR for the free-text column, used by both tables in this dialog --
    they sit two inches apart and a reader should not have to learn two behaviours
    for the same column.

    ``REASONS_CELL_STYLE`` is applied INLINE, on top of whatever ``_paint`` sets,
    because Tailwind emits nothing on this build; see the constant for why the
    max-width is the part that actually stops the table running off the screen.

    NO TOOLTIP ON AN EMPTY CELL. Most rows on a healthy plan have no reason at
    all, and a tooltip that opens onto nothing is worse than none.
    """
    if alert:
        # A ROW, so the alert can carry its own colour: ``_paint`` sets one inline
        # colour per element, so a red fragment inside a grey cell has to be its own
        # element. The tooltip below still carries both halves as one sentence.
        with ui.row().classes(classes).style(REASONS_CELL_STYLE) as cell:
            _paint(ui.label(alert), 'text-xs text-red-400 font-medium')
            if text:
                _paint(ui.label(text), 'text-xs')
        cell.mark(MARKER_ROW_REASONS)
        with cell:
            ui.tooltip(f"{alert} — {text}" if text else alert)
        return cell
    cell = _paint(ui.label(text), classes).mark(MARKER_ROW_REASONS)
    # MERGED, not replaced: ``_paint`` has already put the inline colour on this
    # element and NiceGUI's ``style()`` adds to what is there.
    cell.style(REASONS_CELL_STYLE)
    if text:
        with cell:
            ui.tooltip(text)
    return cell


#: The sizing outcome that needs no telling: the row was sized by the ordinary grid
#: rules. Every other value is a deviation the reader has to know about.
OUTCOME_NORMAL = 'normal'


def _render_held(row: Dict) -> None:
    """The Held cell: ``7`` unchanged, or ``7 → 5.3673`` with the DESTINATION painted
    green for a buy and red for a sell.

    The colour goes on the projected half only. Both halves painted would say nothing
    (the row already has a Side column); the arrow's head is the number that is about
    to become true, and its colour is the same green/red vocabulary the Side and BP
    effect columns already use, so the row reads consistently across.
    """
    held = row['current_quantity']
    projected = row.get('projected_quantity')
    if projected is None or abs(float(projected) - float(held)) <= QUANTITY_EPSILON:
        _label(_shares(held), _col('held', 'text-gray-400')).mark(MARKER_ROW_HELD)
        return
    side = row['side']
    colour = ('text-green-500' if side == 'BUY'
              else 'text-red-500' if side == 'SELL' else 'text-gray-400')
    with ui.row().classes(
            _col('held', 'no-wrap justify-end items-baseline')).style('gap:4px') as cell:
        _paint(ui.label(f"{_shares(held)} →"), 'text-gray-400')
        _paint(ui.label(_shares(projected)), colour + ' font-medium')
    cell.mark(MARKER_ROW_HELD)


def _order_kind(row: Dict) -> Tuple[str, str]:
    """The single Order cell: what will be sent, and the grid it was sized on. Pure.

    ``Order`` and ``Sizing`` used to be two columns that printed the same word on
    every row that traded. The grid is only news when the row did NOT trade -- it is
    then the whole explanation ("1.43 shares became 0.93 and there is no such thing as
    0.93 of this symbol") -- so it is appended in exactly that case and nowhere else.
    """
    if row['suppressed']:
        return f"no order ({row['sizing']})", 'text-orange-400'
    if row['fractional']:
        return 'fractional', 'text-blue-400'
    return 'whole shares', 'text-gray-400'


def _outcome_alert(row: Dict) -> str:
    """The sizing outcome, when it is worth saying. ``''`` on an ordinary row.

    Replaces the Outcome COLUMN, which read 'normal' on every healthy row and so
    spent a column of a fourteen-column table saying nothing. A deviation -- a bump,
    a bump the scaler took back, a row too large to size -- is a reason, belongs with
    the reasons, and is painted red there because it is the one thing in that cell
    the user did not ask for.
    """
    outcome = row.get('outcome') or OUTCOME_NORMAL
    return '' if outcome == OUTCOME_NORMAL else str(outcome)


def _held_text(row: Dict) -> str:
    """The Held cell: ``7`` when nothing changes, ``7 → 5.37`` when it does. Pure.

    The projected side is the row's own ``target_quantity``, not ``current + delta``
    re-added here: the two would be the same number computed twice, and the one place
    they could disagree -- a row the scaler or a precheck cut back after the delta was
    set -- is exactly the row a reader is checking.
    """
    held = row['current_quantity']
    projected = row.get('projected_quantity')
    if projected is None or abs(float(projected) - float(held)) <= QUANTITY_EPSILON:
        return _shares(held)
    return f"{_shares(held)} → {_shares(projected)}"


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
        on_validate: Optional[Callable[[AllocationPlan], Dict]] = None,
        title: str = 'Portfolio allocation - dry run',
    ):
        self.base = base
        self.plan = plan
        self.market = market
        self.on_refresh = on_refresh
        self.on_submit = on_submit
        #: ``portfolio_allocation_service.validate_plan``, or None for a caller
        #: that has no broker to test against (the button is then not drawn at
        #: all rather than drawn dead).
        self.on_validate = on_validate
        self.title = title
        self.allow_fractional = bool(plan.allow_fractional)
        self.selected = self._default_selection(plan)
        self.dialog = None
        self._banner_container = None
        self._base_block_container = None
        self._validation_container = None
        self._notices_container = None
        self._badge_container = None
        self._rows_container = None
        self._no_order_container = None
        self._totals_container = None
        self._selection_container = None
        self._footer_container = None
        self._submit_button = None
        self._submit_tooltip = None
        self._validate_button = None
        #: True while a validation is in flight. The broker work now runs in a
        #: thread, so the dialog stays responsive -- which means the button is
        #: still there to be clicked a second time, and two concurrent margin
        #: sweeps would race each other's verdict into the same container.
        self._validating = False
        #: One-shot latch. See ``_submit``: NiceGUI runs a sync click handler
        #: directly on the event loop, so the dialog stays on screen -- and
        #: clickable -- for the whole of a blocking submit.
        self._submitted = False

    # -- public -----------------------------------------------------------
    def open(self):
        with ui.dialog().props('maximized') as dialog, \
                ui.card().classes(DIALOG_CARD_CLASSES):
            self.dialog = dialog
            ui.label(self.title).classes('text-lg font-bold shrink-0')
            # ABOVE THE TABS, and deliberately so: these two do not qualify the
            # numbers, they say SUBMIT IS OFF. A refusal behind an unopened tab is
            # a refusal that was not shown.
            #
            # A CONTAINER EACH, not a bare render: Refresh re-reads the clock and
            # re-solves the plan, and a banner drawn straight into the card could
            # never be taken down again -- or, for the target block, never
            # updated to a NEW plan's targets.
            self._banner_container = ui.column().classes('w-full shrink-0')
            self._base_block_container = ui.column().classes('w-full shrink-0')
            self._render_market_banner()
            self._render_base_block()
            with ui.tabs().classes('w-full shrink-0') as tabs:
                ui.tab(TAB_ORDERS, label=TAB_ORDERS_LABEL)
                with ui.tab(TAB_NOTICES, label=TAB_NOTICES_LABEL):
                    # Rebuilt by ``_render_notices`` along with the notices it
                    # counts, so the badge can never describe the previous solve.
                    self._badge_container = ui.row().classes('gap-0')
            with ui.tab_panels(tabs, value=TAB_ORDERS) \
                    .classes(DIALOG_PANELS_CLASSES):
                with ui.tab_panel(TAB_ORDERS).classes(TAB_PANEL_CLASSES):
                    # THE ONE EXECUTION CONTROL, on the tab it visibly changes. It
                    # stays at the gate because it changes WHICH ORDERS are produced
                    # rather than what is being aimed at -- and it RE-SOLVES:
                    # ``_refresh`` replaces ``self.plan``, so the table, the totals
                    # and what Submit sends all move together. A switch that only
                    # recorded a preference would show one plan and submit another.
                    with ui.row().classes('w-full items-center gap-3 shrink-0'):
                        fractional = ui.switch(
                            'Allow fractional shares', value=self.allow_fractional,
                            on_change=lambda e: self._refresh(bool(e.value)))
                        # The broker's veto, which used to sit on the step-3 panel
                        # of the dialog that is gone. Offering a toggle the broker
                        # cannot honour would size the plan on a grid that does not
                        # exist: the engine silently falls back to whole shares, so
                        # the user would see quantities they never asked for.
                        # DISABLED, not hidden, and the reason said out loud.
                        fractional.set_enabled(bool(self.base.supports_fractional))
                        if not self.base.supports_fractional:
                            _label(NO_FRACTIONAL_SUPPORT_NOTE,
                                   'text-xs text-gray-400')
                    # ABOVE the scrolling viewport, so the buttons that decide
                    # what Submit sends never scroll out of reach.
                    self._selection_container = ui.row() \
                        .classes('w-full items-center gap-1 shrink-0 flex-wrap')
                    self._rows_container = ui.column().classes(DIALOG_ROWS_CLASSES) \
                        .mark(MARKER_ROWS_VIEWPORT)
                    self._no_order_container = ui.column().classes('w-full shrink-0')
                with ui.tab_panel(TAB_NOTICES).classes(TAB_PANEL_CLASSES):
                    with ui.column().classes(DIALOG_NOTICES_CLASSES):
                        self._render_base_figures()
                        _label(MARKET_ORDER_TIMING_NOTE,
                               f'{NOTICE_TEXT_CLASSES} text-orange-400')
                        self._notices_container = ui.column().classes('w-full')
                        self._totals_container = ui.column().classes('w-full')
            self._render_notices()
            self._render_rows()
            self._render_no_order_rows()
            self._render_totals()
            # The validation verdict, ABOVE the buttons and inside the card so it
            # cannot be lost behind a tab. Empty until Validate is pressed.
            self._validation_container = ui.column().classes('w-full shrink-0')
            with ui.row().classes('w-full justify-end gap-2 shrink-0'):
                ui.button('Refresh', on_click=lambda: self._refresh(self.allow_fractional)).props('outline')
                # TEST THE PLAN BEFORE SENDING IT. See ``_validate``: the broker's
                # own dry run where it has one, the locally knowable rejections
                # everywhere else. Never sends an order.
                if self.on_validate is not None:
                    self._validate_button = \
                        ui.button(VALIDATE_BUTTON_LABEL, on_click=self._validate) \
                        .props('outline').mark(MARKER_VALIDATE_BUTTON)
                    self._validate_button.tooltip(VALIDATE_TOOLTIP)
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

    def _target_block(self) -> Optional[str]:
        """Decision 3, finally enforced: ``None`` unless the label percentages or
        a label's symbol weights are off 100%.

        ``compute_allocation`` deliberately does not renormalise a bad target set
        -- its own docstring says "Blocking submission is the validator's job, not
        this function's" -- and nothing else in the app called
        ``validate_label_targets`` before Submit. Typing 100 into two symbol boxes
        of one label used to solve and SEND a plan that bought the label at twice
        its target, with no warning anywhere on screen; two labels totalling 60%
        deployed 60% of the account and called it done.

        Read off ``self.plan.labels`` -- the SAME ``LabelTarget`` list the plan
        was solved with, carried on the plan for exactly this (``plan_json``
        reproducibility) -- rather than re-reading the page's live boxes: the
        wizard has no editor for them, so this can only ever describe the plan
        actually on screen, never a value the user has since changed elsewhere.

        ONLY for a REBALANCE (``allocation_basis == ALLOCATION_BASIS_POSITION``).
        An INVEST_LABEL run solves a single label against an explicit amount, and
        decision 3's "100%" is a REBALANCE rule about dividing the whole
        investable pool -- ``compute_label_investment`` has its own gate
        (``invest_validation_messages``, checked before the dry run even opens).

        ALSO skipped when ``plan.labels`` is EMPTY -- not one label, not zero
        symbols in a label with a percentage, but literally nothing to divide.
        The page already refuses to open a REBALANCE dry run with no managed
        labels at all (``_open_allocation_flow``: "No managed labels yet"), so a
        plan with an empty label list reaching this far is not the "I typed a bad
        percentage" case decision 3 is about, and treating it as a 100%-short
        target would misreport a different, already-handled situation.
        """
        if self.plan.allocation_basis != ALLOCATION_BASIS_POSITION:
            return None
        if not self.plan.labels:
            return None
        errors = blocking_messages(validate_label_targets(self.plan.labels))
        if not errors:
            return None
        return TARGET_BLOCK_PREFIX + '; '.join(errors)

    def _first_plan_block(self) -> Optional[str]:
        """The FIRST reason this whole plan may not be submitted, or ``None``.

        Used by ``_sync_submit_button`` ONLY, which always has a real ``self.plan``
        (it runs from ``open``/``_refresh``, both well after construction) -- so
        both halves are safe to read eagerly here. ``_submit`` does NOT use this;
        see its own docstring for why its check has to stay lazier than this one.
        ``_render_base_block`` does not use it either -- it draws both banners
        independently, because a screen may need to show two reasons at once even
        though Submit only needs to refuse for one.
        """
        base_block = self._base_block()
        if base_block is not None:
            return base_block
        return self._target_block()

    def _sync_submit_button(self):
        """Point the Submit button at the CURRENT gate. Idempotent.

        Disabled, not hidden: the user must see that Submit exists and WHY it is
        off -- the banner right above says when it returns. Called from ``open``
        and from every ``_refresh``, because the gate moves while the dialog sits
        there and the button is only a mirror of it.

        THREE independent refusals, and the tooltip names whichever is in force,
        base-block-or-target-block FIRST: the market-hours gate moves while the
        dialog is open, but the plan blocks are what the user can act on right
        now.
        """
        if self._submit_button is None:
            return
        plan_block = self._first_plan_block()
        blocked = plan_block is not None or not self.market.allowed
        self._submit_button.set_enabled(not blocked and not self._submitted)
        if self._submit_tooltip is not None:
            reason = plan_block if plan_block is not None else (
                self.market.message if not self.market.allowed else '')
            self._submit_tooltip.set_text(reason)
            self._submit_tooltip.set_visibility(blocked)

    def _render_notices(self):
        """The plan-level sentences and the plan's own warnings, COMPRESSED.

        About a quarter of this book cannot trade fractionally, some rows were
        bumped UP to a whole share and some had their weight moved to keep a label
        on target. Every one of those is the plan doing something the user did not
        type, so none of them is deleted and none of them is hidden behind a
        disclosure the user has to know to open.

        What changed is the SHAPE. Each was a padded ``alert-banner`` div with a
        ``mt-2``, so four of them plus a column of warnings was most of the screen
        above a two-row table. They are dense single lines on their own TAB now,
        which is what stopped them and the table competing for the same pixels.

        THE BADGE IS DRAWN HERE, from the same two lists, and that is the point: a
        tab is a place things hide, and a count assembled anywhere else could
        describe a different set of notices from the ones behind it. Both
        containers are rebuilt on every refresh.

        THE PLAN'S WARNINGS MOVED HERE, out of the base panel, and that is a fix
        rather than tidying: the base panel is drawn ONCE and ``_refresh`` never
        redraws it, so after a Refresh the dialog was showing the PREVIOUS solve's
        warnings against the new plan. They also go through ``plan_warning_lines``,
        which is what collapses the eight identical per-symbol broker-precheck
        lines into one.
        """
        self._notices_container.clear()
        if self._badge_container is not None:
            self._badge_container.clear()
        summary = fractional_summary(self.plan)
        notices = [text for text in (whole_share_notice(summary),
                                     bump_notice(summary),
                                     no_order_notice(summary),
                                     redistribution_notice(summary)) if text]
        warnings = plan_warning_lines(self.plan.warnings,
                                      scale_factor=self.plan.scale_factor)
        if not notices and not warnings:
            return
        # A bare count, not a sentence: this rides on a tab. The sentence is the
        # tooltip. No badge at all when there is nothing to announce -- a "0" on
        # the tab is noise, and noise is what hides the real case.
        total = len(notices) + len(warnings)
        if self._badge_container is not None:
            with self._badge_container:
                badge = ui.badge(str(total)).props('color=orange') \
                    .classes(NOTICE_BADGE_CLASSES).mark(MARKER_NOTICE_COUNT)
                with badge:
                    ui.tooltip(NOTICE_COUNT_FMT.format(count=total))
        # Painted through ``_paint`` / ``_label`` like the rest of the dialog: on
        # this build a bare ``text-orange-400`` renders WHITE, and these lines
        # replaced padded ``alert-banner`` divs, which did paint -- so class-only
        # would have quietly turned every plan notice from an orange callout into
        # indistinguishable body text.
        with self._notices_container:
            with ui.column().classes(NOTICE_BLOCK_CLASSES) \
                    .mark(MARKER_NOTICE_BLOCK):
                for text in notices:
                    with ui.row().classes('w-full items-start gap-2 no-wrap'):
                        _paint(ui.icon('warning'), 'text-orange-400 text-sm shrink-0')
                        _label(text, f'{NOTICE_TEXT_CLASSES} text-orange-400') \
                            .mark(MARKER_PLAN_NOTICE)
                for text, severity in warnings:
                    # ``color=`` on purpose: 'info' is classed ``text-gray-400``,
                    # which the stylesheet DOES paint -- in #b0bec5, not the page's
                    # own ``NEUTRAL_TEXT_COLOR``. The severity's declared hex wins.
                    # The view's classes carry ``text-xs``; the size is restated
                    # here (last class wins) so this tab reads at ONE size.
                    _label(text, f'{PLAN_WARNING_CLASSES[severity]} {NOTICE_TEXT_CLASSES}',
                           color=PLAN_WARNING_COLORS[severity]) \
                        .mark(MARKER_PLAN_WARNING)

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
                    .classes('w-full mt-1'):
                # THE SAME grid treatment as the order table two inches above --
                # header bar, separators, right-aligned money. Two tables in one
                # dialog drawn two ways is a second look to learn for no reason.
                with ui.row().classes(GRID_HEAD_CLASSES):
                    for header, width in (('Symbol', 'w-24'),
                                          ('Price', 'w-28 text-right'),
                                          ('Outcome', 'w-32'),
                                          ('Target', 'w-28 text-right'),
                                          ('Projected', 'w-28 text-right'),
                                          ('Unallocated', 'w-28 text-right'),
                                          ('Why', 'flex-1')):
                        ui.label(header).classes(width)
                for row in dropped:
                    with ui.row().classes(GRID_ROW_CLASSES):
                        ui.label(row['symbol']).classes('w-24 font-medium')
                        ui.label('-' if row['price'] is None
                                 else f"{row['price']:,.2f}").classes('w-28 text-right')
                        _label(row['outcome'], 'w-32 text-xs text-orange-400')
                        ui.label(f"{row['target_notional']:,.2f}") \
                            .classes('w-28 text-right')
                        projected = row['projected_notional']
                        ui.label('-' if projected is None
                                 else f"{projected:,.2f}").classes('w-28 text-right')
                        _label(f"{row['unmet_notional']:,.2f}",
                               'w-28 text-right text-orange-400')
                        # The SAME treatment as the column two inches above: these
                        # are the longest strings in the dialog and used to run
                        # straight off the right-hand edge.
                        _reasons_cell(row['reasons'], 'flex-1 text-xs text-gray-400')

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

    def _render_base_block(self):
        """The banner(s) that say the whole plan may not be submitted: the base
        block and the target block, one ``div`` each so a test (and the user's
        eye) can tell which reason is in force.

        DANGER, and drawn ABOVE THE TABS rather than inside one. Neither merely
        qualifies the numbers below it -- each says they are wrong and Submit is
        off -- so neither may sit behind a tab the user has not opened.

        REDRAWN on every refresh, unlike the name suggests: ``_base_block`` reads
        the frozen base (which ``_refresh`` never replaces, so that HALF is inert
        on a redraw) but ``_target_block`` reads ``self.plan.labels``, which
        ``_refresh`` DOES replace. A stale target banner that outlived the plan it
        described would be exactly the "the two screens disagree" class of bug
        this feature exists to remove. Idempotent: the container is cleared first.
        """
        if self._base_block_container is None:
            return
        self._base_block_container.clear()
        with self._base_block_container:
            base_block = self._base_block()
            if base_block is not None:
                with ui.element('div').classes('alert-banner danger w-full p-3 shrink-0'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('price_change')
                        ui.label(base_block).classes('text-sm').mark(MARKER_BASE_BLOCK)
            target_block = self._target_block()
            if target_block is not None:
                with ui.element('div').classes('alert-banner danger w-full p-3 shrink-0'):
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('percent')
                        ui.label(target_block).classes('text-sm').mark(MARKER_TARGET_BLOCK)

    def _render_base_figures(self):
        with ui.row().classes('w-full gap-6 items-center'):
            ui.label(f'Buying power: {self.base.available_buying_power:,.2f}')
            ui.label(f'Managed value ({self.base.valuation_mode}): '
                     f'{self.base.managed_value:,.2f}')
            ui.label(f'Base notional: {self.base.base_notional:,.2f}').classes('font-bold')
            _label(f"as of {self.base.taken_at:%Y-%m-%d %H:%M UTC}",
                   'text-xs text-gray-400')
            # Only when there IS one: a "Reserved: 0.00" chip on every fully
            # allocated plan is noise, and noise is what hides the real case.
            if self.plan.reserved_pct > LABEL_TOTAL_TOLERANCE_PCT:
                _label(RESERVED_FMT.format(amount=self.plan.reserved_notional,
                                           pct=self.plan.reserved_pct),
                       'text-orange-400').mark(MARKER_RESERVED)
        # The submit-blocking banner is NOT here: it is drawn above the tabs by
        # ``_render_base_block``, because a refusal the user has to open a tab to
        # find is a refusal that was not shown.
        #
        # The BASE's warnings only. The PLAN's moved into ``_render_notices``:
        # this panel is drawn once and Refresh never redraws it, so a plan warning
        # here went stale the moment the user pressed Refresh -- the previous
        # solve's complaint sitting above the new solve's table.
        for warning in self.base.warnings:
            _label(warning, f'{NOTICE_TEXT_CLASSES} text-orange-400')

    # -- selection ----------------------------------------------------------
    def _sendable_rows(self) -> List[Dict]:
        """The dry-run rows that CAN be ticked: neither suppressed nor skipped."""
        return [r for r in dry_run_rows(self.plan)
                if not r['suppressed'] and not r['skipped']]

    def _symbol_labels(self) -> Dict[str, List[str]]:
        """symbol -> the labels the plan says it came from, in plan order."""
        return {row.symbol: list(row.labels) for row in self.plan.rows}

    def _labels_in_table(self) -> List[str]:
        """Every label carried by a sendable row, first-seen order, no repeats."""
        by_symbol = self._symbol_labels()
        seen: List[str] = []
        for row in self._sendable_rows():
            for label in by_symbol.get(row['symbol'], []):
                if label not in seen:
                    seen.append(label)
        return seen

    def _set_selection(self, symbols, checked: bool):
        """Tick or un-tick ``symbols`` in one go, then redraw the boxes and totals.

        Only SENDABLE symbols can enter the selection -- a suppressed row has no
        order, and ticking it here would put a refused order into what Submit
        sends, the exact thing ``_render_row`` disables its box to prevent.
        """
        sendable = {r['symbol'] for r in self._sendable_rows()}
        wanted = {s for s in symbols if s in sendable}
        if checked:
            self.selected |= wanted
        else:
            self.selected -= wanted
        self._render_rows()
        self._render_totals()

    def _select_all(self, checked: bool):
        self._set_selection([r['symbol'] for r in self._sendable_rows()], checked)

    def _select_label(self, label: str, checked: bool):
        """Every sendable row carrying ``label`` -- INCLUDING one that also carries
        another label. 'None for ARK26' means no ARK26 order goes out, whatever
        else the symbol belongs to."""
        by_symbol = self._symbol_labels()
        self._set_selection([r['symbol'] for r in self._sendable_rows()
                             if label in by_symbol.get(r['symbol'], [])], checked)

    def _render_selection_toolbar(self):
        """'Select all' / 'Deselect all', then an ``all`` / ``none`` pair per label.

        Drawn only when there is something to tick. The per-label pairs answer the
        question the table cannot: "send the NASDAQ30 rebalance but hold the ARK26
        one back", without un-ticking twenty rows by hand.
        """
        if self._selection_container is None:
            return
        self._selection_container.clear()
        if not self._sendable_rows():
            return
        labels = self._labels_in_table()
        with self._selection_container:
            ui.button(SELECT_ALL_LABEL, on_click=lambda: self._select_all(True)) \
                .props('dense outline size=sm').mark(MARKER_SELECT_ALL)
            ui.button(DESELECT_ALL_LABEL, on_click=lambda: self._select_all(False)) \
                .props('dense outline size=sm').mark(MARKER_DESELECT_ALL)
            for label in labels:
                with ui.row().classes('items-center gap-0 ml-2 no-wrap'):
                    _label(f'{label}:', 'text-xs text-gray-400 mr-1')
                    ui.button('all', on_click=lambda _e, lbl=label: self._select_label(lbl, True)) \
                        .props('dense flat size=sm').mark(MARKER_LABEL_SELECT) \
                        .tooltip(f'Tick every {label} order')
                    ui.button('none', on_click=lambda _e, lbl=label: self._select_label(lbl, False)) \
                        .props('dense flat size=sm').mark(MARKER_LABEL_SELECT) \
                        .tooltip(f'Un-tick every {label} order')

    def _render_table_footer(self):
        """The column totals over the TICKED rows, pinned to the table's foot.

        Summed from ``dry_run_rows`` -- the numbers the cells above were drawn
        from -- so the footer can never disagree with the column it closes. Money
        columns only; a quantity total across different symbols means nothing.
        ``Est. value`` is split into the buy and the sell total because the two
        move in opposite directions and one net figure would hide a large sell
        behind a large buy. ``None`` values (an unpriced holding) are EXCLUDED,
        not counted as zero.
        """
        if self._footer_container is None:
            return
        self._footer_container.clear()
        rows = [r for r in self._sendable_rows() if r['symbol'] in self.selected]
        sendable = len(self._sendable_rows())
        cost = sum(r['current_cost_basis'] for r in rows)
        values = [r['current_value'] for r in rows if r['current_value'] is not None]
        buys = sum(r['estimated_value'] for r in rows if r['side'] == 'BUY')
        sells = sum(r['estimated_value'] for r in rows if r['side'] == 'SELL')
        target = sum(r['target_notional'] for r in rows)
        projected = [r['projected_notional'] for r in rows
                     if r['projected_notional'] is not None]
        weight = sum(r['weight_pct'] for r in rows)
        projected_weight = sum(r['projected_weight_pct'] for r in rows)
        bp_effect = sum(r['bp_effect'] for r in rows)
        bp_pct = sum(r['bp_usage_pct'] for r in rows)
        with self._footer_container:
            with ui.row().classes(GRID_FOOT_CLASSES).style(GRID_FOOT_STYLE) \
                    .mark(MARKER_TABLE_FOOT):
                ui.label('').classes(_col('tick'))
                with ui.label(FOOTER_CAPTION_FMT.format(ticked=len(rows),
                                                        sendable=sendable)) \
                        .classes(_col('symbol', 'font-medium')):
                    ui.tooltip(FOOTER_TOOLTIP)
                ui.label('').classes(_col('held'))
                ui.label(f"{cost:,.2f}").classes(_col('cost'))
                ui.label(f"{sum(values):,.2f}"
                         + ('' if len(values) == len(rows) else ' *')) \
                    .classes(_col('value'))
                ui.label('').classes(_col('side'))
                ui.label('').classes(_col('qty'))
                ui.label('').classes(_col('order'))
                with ui.column().classes(_col('estimated_value', 'gap-0 leading-tight')):
                    _label(f"B {buys:,.2f}", 'text-green-500 text-xs')
                    _label(f"S {sells:,.2f}", 'text-red-500 text-xs')
                ui.label(f"{target:,.2f}").classes(_col('target'))
                ui.label(f"{sum(projected):,.2f}"
                         + ('' if len(projected) == len(rows) else ' *')) \
                    .classes(_col('projected'))
                _label(f"{weight:.2f}% → {projected_weight:.2f}%",
                       _col('weight', 'text-xs text-gray-400'))
                _label(f"{bp_effect:+,.2f}" if abs(bp_effect) >= 0.005 else '0.00',
                       _col('bp_effect', 'text-green-500 font-medium'
                            if bp_effect > 0 else ''))
                ui.label('').classes(_col('bp_ratio'))
                ui.label(f"{bp_pct:.1f}%").classes(_col('bp_pct'))
                _label('* an unpriced row is left out of this total'
                       if len(values) != len(rows) or len(projected) != len(rows)
                       else '', _col('reasons', 'text-xs text-gray-400'))

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

        It reads as a TABLE now rather than as a stack of rows: the header, the row
        separators and the hover are ``styles.css``'s own ``.q-table`` rules,
        reached through ``.pf-grid-head`` / ``.pf-grid-row`` so this grid and the
        symbol tables in the label panels are one look and not two. The header is
        STICKY, which is what makes the taller viewport usable -- eighteen columns
        whose names have scrolled away are eighteen anonymous numbers.

        The columns come from ``DRY_RUN_COLUMNS`` and so do the cells below, so a
        right-aligned money column cannot line up under a left-aligned heading.
        """
        self._rows_container.clear()
        self._footer_container = None
        self._render_selection_toolbar()
        rows = dry_run_rows(self.plan)
        with self._rows_container:
            if not rows:
                _label('No orders required - the account already matches its targets.',
                       'text-sm text-gray-400')
                return
            if any(r['fractional'] and not r['suppressed'] for r in rows):
                _label(FRACTIONAL_IS_MARKET_ONLY_NOTE,
                       'text-xs text-orange-400 shrink-0')
            with ui.row().classes(GRID_HEAD_CLASSES).mark(MARKER_TABLE_HEAD):
                for name, header, _width, _numeric in DRY_RUN_COLUMNS:
                    ui.label(header.format(mode=self.plan.valuation_mode)) \
                        .classes(_col(name))
            for row in rows:
                self._render_row(row)
            # The footer lives INSIDE the viewport so it can stick to its bottom
            # edge; its own container so a tick redraws the totals and nothing else.
            self._footer_container = ui.column().classes('w-full min-w-max gap-0')
        self._render_table_footer()

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
        with ui.row().classes(GRID_ROW_CLASSES
                              + (' opacity-60' if blocked else '')):
            checkbox = ui.checkbox(
                value=row['symbol'] in self.selected,
                on_change=lambda e, s=row['symbol']: self._toggle(s, bool(e.value)),
            ).classes(_col('tick')).mark(MARKER_ROW_TICK)
            # A suppressed row has no order to submit, so it must not be tickable
            # -- greying it is not enough, the box would still be clickable.
            checkbox.set_enabled(not blocked)
            ui.label(row['symbol']).classes(_col('symbol', 'font-medium'))
            # THE BASIS THIS ROW IS TRADING AGAINST.
            # "held -> projected", and ONLY when they differ: a row that trades
            # nothing would otherwise print "0 -> 0", which is noise on the majority
            # of rows in a plan that mostly leaves things alone.
            _render_held(row)
            _label(f"{row['current_cost_basis']:,.2f}",
                   _col('cost', 'text-gray-400')).mark(MARKER_ROW_COST)
            value = row['current_value']
            # '-', never 0.00: no price is "not measurable", not "worthless".
            _label('-' if value is None else f"{value:,.2f}",
                   _col('value', 'text-gray-400')).mark(MARKER_ROW_VALUE)
            # Green BUY, red SELL -- and PAINTED, which they never were: both
            # classes render white on this build, so the column that says which
            # way real money is about to move has always read as plain text.
            _label(row['side'] or '-', _col(
                'side', 'text-green-500' if row['side'] == 'BUY'
                else 'text-red-500' if row['side'] == 'SELL' else 'text-gray-400'))
            ui.label(_shares(row['quantity'])).classes(_col('qty'))
            order_kind, order_class = _order_kind(row)
            _label(order_kind, _col('order', 'text-xs ' + order_class)) \
                .mark(MARKER_ORDER_KIND)
            ui.label(f"{row['estimated_value']:,.2f}").classes(_col('estimated_value'))
            ui.label(f"{row['target_notional']:,.2f}").classes(_col('target'))
            projected = row['projected_notional']
            # The header names the mode this figure is in; the tooltip carries the
            # OTHER one, so cost and value are one hover apart instead of one
            # page-level toggle and a re-solve apart.
            projected_label = ui.label('-' if projected is None
                                       else f"{projected:,.2f}") \
                .classes(_col('projected'))
            other = ('projected_cost' if self.plan.valuation_mode == VALUATION_MODE_MARKET
                     else 'projected_market')
            if row[other] is not None:
                with projected_label:
                    ui.tooltip(f"{other.replace('_', ' ')}: {row[other]:,.2f}")
            # ASKED -> ACTUAL. They differ whenever the grid, a bump or the label
            # redistribution moved this row, and hiding that would be rewriting the
            # user's weights behind their back.
            _label(f"{row['weight_pct']:.2f}% → {row['projected_weight_pct']:.2f}%",
                   _col('weight', 'text-xs '
                        + ('text-orange-400' if row['redistributed']
                           else 'text-gray-400')))
            self._render_bp_effect(row)
            # Immediately beside BP effect ON PURPOSE: the x IS the explanation of
            # why that figure is not the Est. value, which is the misreading
            # requirement 1b is about.
            text, css, tip = _leverage_cell(row)
            with _label(text, _col('bp_ratio', 'text-xs ' + css)) \
                    .mark(MARKER_LEVERAGE):
                ui.tooltip(tip)
            ui.label(f"{row['bp_usage_pct']:.1f}%").classes(_col('bp_pct'))
            # An abnormal sizing outcome is a REASON and is drawn RED at the front
            # of that column; the rest of the reasons keep their own colour.
            _reasons_cell(row['reasons'], _col(
                'reasons', 'text-xs ' + ('text-orange-400' if row['suppressed']
                                         else 'text-gray-400')),
                          alert=_outcome_alert(row))

    @staticmethod
    def _render_bp_effect(row: Dict):
        """The ``BP effect`` cell: SIGNED, and the sign is the whole message.

        A sale FREES buying power. This column reported ``BP cost 0.00`` for one,
        which does not read as "nothing to say about a sale" -- it reads as "this
        trade does nothing to your buying power", the exact opposite of the truth,
        on the column a user checks before pressing Submit.

        Negative for a charge and positive for a release, which is the BROKER's own
        convention (``OrderImpact.change_in_buying_power``), so the dry run and a
        precheck cannot be read opposite ways round. Exactly ``0.00`` -- unsigned
        -- on a row with no order: a ``+0.00`` there would claim a direction.

        Only the RELEASE is painted. Green is a cue that this row hands money back;
        painting the charge as well would put a colour on nearly every row of an
        ordinary plan, which is not a signal.
        """
        effect = row['bp_effect']
        if abs(effect) < 0.005:
            with ui.label('0.00').classes(_col('bp_effect')) \
                    .mark(MARKER_BP_EFFECT):
                ui.tooltip(BP_EFFECT_TOOLTIP_NONE)
            return
        freeing = effect > 0
        amount = abs(effect)
        value = row['estimated_value']
        ratio = (amount / value) if value else float(row['bp_factor'])
        tip = (BP_EFFECT_TOOLTIP_RELEASE_FMT if freeing
               else BP_EFFECT_TOOLTIP_CHARGE_FMT).format(
            symbol=row['symbol'], amount=amount, value=value, ratio=ratio)
        cell = _label(f'{effect:+,.2f}',
                      _col('bp_effect', 'text-green-500 font-medium' if freeing
                           else '')).mark(MARKER_BP_EFFECT)
        with cell:
            ui.tooltip(tip)

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

        THE BUYING-POWER LINE DIVIDES THE BUDGET, not the published figure. Over
        the TICKED rows, which is what makes it live: un-tick the sell that funds a
        rebalance and ``filter_plan_rows`` takes its release straight back out, the
        budget drops to what the broker published, and the over-budget note appears
        -- before anything has been sent.
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
        released = selected_plan.released_buying_power
        budget = selected_plan.total_buying_power
        bp_line = BP_BUDGET_FMT.format(required=required, budget=budget,
                                       pct=selected_plan.bp_usage_pct)
        # Only when the sells actually free something: "+ 0.00 freed" on every
        # buy-only plan is noise.
        if released > MONEY_EPSILON:
            bp_line += BP_BUDGET_BREAKDOWN_FMT.format(
                available=selected_plan.available_buying_power, released=released)
        with self._totals_container:
            with ui.row().classes(f'w-full gap-6 mt-2 {TOTALS_TEXT_CLASSES}'):
                ui.label(f"Sell value: {selected_plan.total_sell_value:,.2f}")
                ui.label(f"Buy value: {buy_value:,.2f}")
                ui.label(bp_line).mark(MARKER_BP_BUDGET)
                if totals is not None:
                    ui.label(f"Est. cash after: {totals['estimated_cash_after']:,.2f}")
                else:
                    _label('Est. cash after: unknown (broker published no cash balance)',
                           'text-orange-400')
            # Only when a reserve was actually asked for, and only when the broker
            # gave a cash balance to compare it against -- there is no fallback for
            # a balance.
            if totals is not None and selected_plan.reserved_pct > LABEL_TOTAL_TOLERANCE_PCT:
                cash_after = totals['estimated_cash_after']
                reserved = selected_plan.reserved_notional
                with ui.row().classes(f'w-full {TOTALS_TEXT_CLASSES}'):
                    _label(CASH_VS_RESERVE_FMT.format(
                        cash=cash_after, reserved=reserved,
                        delta=cash_after - reserved),
                        'text-orange-400' if cash_after < reserved else '') \
                        .mark(MARKER_CASH_VS_RESERVE)
            with ui.row().classes(f'w-full gap-6 {TOTALS_TEXT_CLASSES}'):
                _label(f"Held cost: {held_cost:,.2f}", 'text-gray-400')
                _label(f"Held value: {sum(priced):,.2f}"
                       + ('' if len(priced) == len(shown)
                          else f" ({len(shown) - len(priced)} unpriced, excluded)"),
                       'text-gray-400')
                if fees:
                    _label(f"Est. fees: {sum(fees):,.2f}", 'text-gray-400')
            # Requirement 1b. Only when there is a charge to explain -- a sell-only
            # plan reserves nothing and the sentence would be noise.
            if buy_value > MONEY_EPSILON:
                _label(BP_IS_A_CHARGE_NOTE_FMT.format(
                    buy_value=buy_value, required=required,
                    ratio=required / buy_value), f'{TOTALS_NOTE_CLASSES} text-gray-400') \
                    .mark(MARKER_BP_NOTE)
            with ui.row().classes(f'w-full gap-6 {TOTALS_TEXT_CLASSES}'):
                # SIGNED: a bump's over-allocation nets against a rounding shortfall,
                # which is what "how far off target will I be" actually means.
                _label(f"Off target after rounding: {summary['residual_notional']:,.2f} "
                       f"({summary['residual_pct']:.2f}% of base)",
                       'text-orange-400' if abs(summary['residual_pct']) >= 1.0 else '')
                ui.label(f"Fractional: {summary['fractional_rows']} / "
                         f"whole shares: {summary['whole_share_rows']}"
                         + (f" / eligibility unknown: {summary['unknown_rows']}"
                            if summary['unknown_rows'] else ''))
                if summary['bumped_rows']:
                    _label(f"Bumped to 1 share: {summary['bumped_rows']} "
                           f"(+{summary['bumped_notional']:,.2f})", 'text-orange-400')
                # ``summary['bumped_dropped_rows']`` is deliberately NOT drawn
                # here. This footer measures the TICKED rows, and a bump a later
                # step took away has no order, so it is never tickable and the
                # count would be a permanent 0. It reaches the user where it
                # belongs instead: the Outcome column, the 'Not traded' table and
                # ``no_order_notice``. What matters at this level is that it is no
                # longer folded into "Bumped to 1 share" above -- that is what made
                # this line announce an over-allocation of 0.00.
            # Against the BUDGET, the same denominator the line above divides.
            if required > budget:
                _label(BP_OVER_BUDGET_NOTE, f'{TOTALS_NOTE_CLASSES} text-orange-400')

    async def _validate(self):
        """Test the TICKED orders against the broker, and offer to drop the bad ones.

        Sends nothing. ``on_validate`` re-reads the broker's per-symbol facts and
        runs its order preview where one exists (TastyTrade); on Alpaca, which
        publishes no preview endpoint, every check is local and the panel says so
        rather than implying the broker signed the plan off.

        Runs over ``filter_plan_rows``, not the whole plan: validating rows the
        user has already un-ticked would report problems with orders that are not
        going to be sent, and the drop button would then have nothing to drop.

        ASYNC, AND THE BROKER CALL GOES TO A THREAD. ``on_validate`` re-reads margin
        for every symbol in the plan -- on Alpaca that is a REST round trip per
        symbol -- and a sync click handler runs directly on the event loop, so
        NOTHING reaches the browser until it returns (the same fact ``_submit``
        documents at length). On a 40-row plan that outlasted the websocket
        heartbeat: the page reported "Connection lost. Trying to reconnect...", and
        the reconnect re-rendered the page and took the dialog with it -- reported
        as "clicking validate hangs the UI and eventually closes the dry run
        dialog". The work is unchanged; it just no longer runs where the heartbeat
        lives.

        The verdict panel is REPLACED on every press and cleared by ``_refresh``:
        a validation describes one exact set of orders, and a re-solve makes it a
        statement about a plan that no longer exists.
        """
        if self.on_validate is None or self._validation_container is None:
            return
        if self._validating:
            return
        selected_plan = filter_plan_rows(self.plan, sorted(self.selected))
        if not selected_plan.rows:
            ui.notify('Nothing selected to validate', type='warning')
            return
        # The button says what it is doing and refuses a second press while it
        # does it: the call takes tens of seconds against a live broker, and until
        # this ran off the event loop there was nothing on screen to say so.
        button, self._validating = self._validate_button, True
        if button is not None:
            button.props('loading')
            button.disable()
        try:
            report = await asyncio.to_thread(self.on_validate, selected_plan)
        except Exception as e:
            logger.error(f"Allocation validation failed: {e}", exc_info=True)
            ui.notify(f'Validation failed: {e}', type='negative')
            return
        finally:
            self._validating = False
            if button is not None:
                button.props(remove='loading')
                button.enable()
        self._render_validation(report, checked=len(selected_plan.rows))

    def _render_validation(self, report: Dict, *, checked: int):
        """Draw one validation verdict. Idempotent; the container is cleared first."""
        self._validation_container.clear()
        findings = list(report.get('findings') or [])
        symbols = list(report.get('symbols') or [])
        budget = report.get('budget')
        prechecked = int(report.get('prechecked') or 0)
        buy_rows = int(report.get('buy_rows') or 0)
        precheck_note = (VALIDATION_PRECHECK_FMT.format(done=prechecked, total=buy_rows)
                         if prechecked else VALIDATION_NO_PRECHECK)
        with self._validation_container:
            css = 'alert-banner warning' if findings else 'alert-banner info'
            with ui.element('div').classes(f'{css} w-full p-3 mt-1') \
                    .mark(MARKER_VALIDATION_RESULT):
                if findings:
                    _label(VALIDATION_FOUND_FMT.format(count=len(symbols)),
                           'text-sm font-medium text-orange-400')
                    for _symbol, reason in findings:
                        _label(f'• {reason}', 'text-sm text-orange-400') \
                            .mark(MARKER_VALIDATION_FINDING)
                    _label(precheck_note, 'text-xs text-gray-400')
                else:
                    _label(VALIDATION_CLEAN_FMT.format(count=checked,
                                                       precheck=precheck_note),
                           'text-sm text-gray-400')
                # The PLAN-level advisory, drawn whether or not there are row
                # findings: running out of buying power truncates the smallest
                # buys, and no row can be un-ticked to fix it.
                if budget:
                    _label(str(budget), 'text-sm text-orange-400') \
                        .mark(MARKER_VALIDATION_FINDING)
                if symbols:
                    ui.button(VALIDATION_DROP_FMT.format(count=len(symbols)),
                              on_click=lambda syms=list(symbols): self._drop(syms)) \
                        .props('outline dense').classes('mt-2') \
                        .mark(MARKER_VALIDATION_DROP)

    def _drop(self, symbols: List[str]):
        """Un-tick exactly the symbols validation flagged, and say what is left.

        The "continue without those" half of the request: the rest of the plan is
        untouched and immediately submittable, and the verdict panel is redrawn
        from the NEW selection so it cannot go on naming orders that are no longer
        going to be sent.
        """
        self.selected -= set(symbols)
        self._render_rows()
        self._render_table_footer()
        self._render_totals()
        remaining = len(self.selected)
        ui.notify(f'{len(symbols)} order(s) un-ticked; {remaining} still selected',
                  type='info')
        if self._validation_container is not None:
            self._validation_container.clear()

    def _toggle(self, symbol: str, checked: bool):
        if checked:
            self.selected.add(symbol)
        else:
            self.selected.discard(symbol)
        self._render_table_footer()
        self._render_totals()

    async def _refresh(self, allow_fractional: bool):
        """Re-solve, and re-read the CLOCK with it.

        ``on_refresh`` returns ``(plan, market)`` from ONE solve, which is the same
        guarantee the initial open has: the banner and the gate can never describe
        different instants. Refreshing only the plan is what left a wizard opened
        at 09:00 with Submit disabled at 10:00, and a wizard opened at 15:00 with
        Submit still enabled at 16:30.

        A refresh that RAISES changes nothing at all -- not the plan, not the gate.
        Unlocking Submit because the clock could not be re-read would be exactly
        backwards.

        OFF THE EVENT LOOP, for the reason spelled out in ``_validate``: ``on_refresh``
        re-reads positions, quotes and the clock and then solves the whole plan again.
        Run inline it blocks every browser this server is serving, and past the
        websocket heartbeat it costs the user the dialog.
        """
        self.allow_fractional = allow_fractional
        try:
            self.plan, self.market = await asyncio.to_thread(
                self.on_refresh, allow_fractional)
        except Exception as e:
            logger.error(f"Allocation dry-run refresh failed: {e}", exc_info=True)
            ui.notify(f'Refresh failed: {e}', type='negative')
            return
        self.selected = self._default_selection(self.plan)
        # A verdict describes ONE exact set of orders. This is a different plan.
        if self._validation_container is not None:
            self._validation_container.clear()
        self._render_market_banner()
        self._render_base_block()
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
        the MARKET GATE, ``_base_block`` or ``_target_block`` refuses.
        """
        # FIRST, before touching any other state: the button is disabled, but a
        # stale client or a keyboard activation must not get past this either. The
        # real enforcement is in run_allocation, which re-reads the clock AND
        # re-derives every gate; this is the polite half, and it is deliberately
        # ahead of the one-shot latch so a refused click leaves the dialog exactly
        # as it found it.
        #
        # STRICT ORDER, LAZILY: ``_base_block`` and the market check touch only
        # ``self.base`` / ``self.market``, which exist the instant the instance is
        # constructed; ``_target_block`` touches ``self.plan``, which does not,
        # in the one caller that proves this precondition
        # (``test_wizard_submit_bails_on_the_gate_before_it_touches_anything_else``
        # builds the wizard with ``object.__new__`` and no ``plan`` at all). Each
        # check therefore runs only once the one before it has already passed.
        base_block = self._base_block()
        if base_block is not None:
            ui.notify(base_block, type='negative')
            return
        if not self.market.allowed:
            ui.notify(self.market.message, type=self.market.severity)
            return
        target_block = self._target_block()
        if target_block is not None:
            ui.notify(target_block, type='negative')
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
    on_validate: Optional[Callable[[AllocationPlan], Dict]] = None,
    title: str = 'Portfolio allocation - dry run',
) -> AllocationWizard:
    """Open the dry-run dialog. Returns the wizard so the caller can keep a handle.

    ``on_validate`` is ``portfolio_allocation_service.validate_plan`` -- given the
    FILTERED plan it re-reads the broker's facts, runs its order preview where one
    exists, and returns the findings. Optional: omitted, the Validate button is not
    drawn at all rather than drawn dead.

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
                              on_submit=on_submit, on_validate=on_validate,
                              title=title)
    wizard.open()
    return wizard


class InvestScope:
    """"Invest into one label": which label, how much, and what there is to spend.

    NOT a target editor, and it never was. The wizard's three-step REBALANCE
    dialog is gone -- its step 1 ("Rebalance - set targets") and step 2 (symbol
    weights) now live on the Portfolio Allocation page, beside the numbers they
    rewrite -- and pressing Allocate goes straight to the dry run. What survives
    here is the one thing the page cannot express: an INVEST run spends a specific
    amount on a single label, so the run has to be told which label and how much,
    and neither is a stored target of anything.

    ``compute_label_investment`` multiplies the chosen label's symbol weights
    straight through, so a 150% set would overspend the budget by half;
    ``invest_validation_messages`` still holds them to 100 and gates Continue on
    it. The weights themselves are typed on the page.

    There is no fractional switch here. Exactly ONE execution control exists and
    it is at the gate (``AllocationWizard``), where toggling it re-solves the plan
    -- a second copy two dialogs earlier would be a question asked before there
    was anything to answer it about. The account's remembered choice is carried
    through untouched.

    Continue WRITES: ``on_dry_run`` persists the chosen label's symbol weights
    before it solves, so "load last" has something to load. It does NOT write a
    label target or a reserve -- an invest run spends an explicit amount, so the
    label's percentage played no part.
    """

    def __init__(self, base: BaseSnapshot, labels: List[LabelTarget], *,
                 on_dry_run: Callable[..., None],
                 allow_fractional: bool,
                 invest_amount: float = 0.0):
        self.base = base
        # A SHALLOW copy is enough now. The deep copy this replaced existed
        # because the dialog edited every target and weight in place and Cancel
        # had to abandon them; nothing here mutates a LabelTarget at all.
        self.labels = list(labels or [])
        self.on_dry_run = on_dry_run
        self.invest_amount = float(invest_amount or 0.0)
        # The account's REMEMBERED choice, still vetoed by a broker that cannot
        # split shares at all. Carried, not offered: the switch is at the gate.
        self.allow_fractional = bool(base.supports_fractional and allow_fractional)
        self.scope_label = self.labels[0].label if self.labels else None
        self.dialog = None
        self._errors_container = None
        self._continue_button = None

    def open(self):
        with ui.dialog().props('maximized') as dialog, ui.card().classes('w-full h-full overflow-auto'):
            self.dialog = dialog
            ui.label('Invest into one label').classes('text-xl font-bold')
            self._render_invest_scope()
            self._render_base_panel()
            self._errors_container = ui.column().classes('w-full')
            ui.label(CONTINUE_SAVES_NOTE).classes('text-xs text-orange-400 mt-2') \
                .mark(MARKER_CONTINUE_SAVES)
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                self._continue_button = ui.button('Continue to dry run',
                                                  on_click=self._continue).props('color=primary')
            self._revalidate()
        dialog.open()
        return dialog

    # -- rendering ---------------------------------------------------------
    def _render_invest_scope(self):
        ui.label('1. Which label, and how much').classes('text-lg font-bold mt-2')
        with ui.row().classes('w-full items-center gap-3'):
            ui.select([lt.label for lt in self.labels], value=self.scope_label, label='Label',
                      on_change=self._set_scope).props('dense outlined').classes('w-56')
            ui.number(value=self.invest_amount, min=0, step=0.01, label='Amount',
                      on_change=self._set_amount).props('dense outlined').classes('w-40')
        ui.label(INVEST_SCOPE_NOTE).classes('text-xs text-secondary-custom')

    def _render_base_panel(self):
        ui.label('2. What there is to allocate').classes('text-lg font-bold mt-4')
        with ui.row().classes('w-full gap-6 items-center'):
            ui.label(f'Buying power: {self.base.available_buying_power:,.2f}')
            ui.label(f'Managed value ({self.base.valuation_mode}): '
                     f'{self.base.managed_value:,.2f}')
            ui.label(f'Base notional: {self.base.base_notional:,.2f}').classes('font-bold')
            ui.label(f"as of {self.base.taken_at:%Y-%m-%d %H:%M UTC}").classes('text-xs text-gray-400')
        for warning in self.base.warnings:
            ui.label(warning).classes('text-xs text-orange-400')

    # -- state + validation ------------------------------------------------
    def _set_scope(self, event):
        self.scope_label = event.value
        self._revalidate()

    def _set_amount(self, event):
        self.invest_amount = float(event.value or 0.0)
        self._revalidate()

    def _scope_target(self) -> Optional[LabelTarget]:
        return next((lt for lt in self.labels if lt.label == self.scope_label), None)

    def _problems(self) -> List[str]:
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
        scope = self._scope_target()
        # ``unallocated_pct`` is 0.0, ALWAYS: an invest run spends a specific
        # amount the user named, and there is no portfolio base for a reserve to
        # be a share of. The page's stored reserve is deliberately NOT written.
        self.on_dry_run(mode=ALLOCATION_MODE_INVEST_LABEL,
                        labels=[scope] if scope else [], scope_label=self.scope_label,
                        amount=self.invest_amount,
                        allow_fractional=self.allow_fractional,
                        unallocated_pct=0.0)


def open_invest_scope(base: BaseSnapshot, labels: List[LabelTarget], *,
                      on_dry_run: Callable[..., None],
                      allow_fractional: bool,
                      invest_amount: float = 0.0) -> InvestScope:
    """Open the invest-scope dialog. ``on_dry_run`` is called with keyword
    arguments ``mode``, ``labels``, ``scope_label``, ``amount``,
    ``allow_fractional`` and ``unallocated_pct``.

    It replaces ``open_allocation_steps``, which opened a three-step dialog whose
    first two steps were the target editor. Those steps are on the Portfolio
    Allocation page now and the REBALANCE flow does not open a dialog at all --
    Allocate goes straight to the dry run.

    ``allow_fractional`` is REQUIRED and has no default: the caller passes
    ``get_allocation_config(account_id).allow_fractional`` (itself defaulting to
    True). It is carried through to ``on_dry_run`` and is not offered as a control
    here -- the one execution control lives at the gate, where toggling it
    re-solves the plan. A default here would silently re-answer a question the
    account has already answered.
    """
    scope = InvestScope(base, labels, on_dry_run=on_dry_run,
                        allow_fractional=allow_fractional,
                        invest_amount=invest_amount)
    scope.open()
    return scope


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
                _label(f'Unallocated: {open_total:,.2f}', 'font-bold text-green-500')
                ui.button('Refresh', on_click=on_sync).props('outline dense')
                # Nothing to invest is not a run worth opening the wizard for.
                ui.button('Invest', on_click=lambda: on_invest(open_total)) \
                    .props('color=primary dense').set_enabled(open_total > 0)
        if not events:
            _label('No deposits or dividends in the last 30 days.',
                   'text-sm text-gray-400')
            return
        # A real ``ui.table``, like every other table on this page. It was hand-rolled
        # out of ``ui.row()`` + ``ui.label()`` with hardcoded widths, so it had none of
        # the shared header treatment, alignment, sorting or pagination -- and the
        # columns drifted out of line with the rows on any zoom level.
        #
        # The money stays a FLOAT in the row and is formatted by Quasar's ``format``
        # (a dynamic ``:`` property, evaluated client-side). Pre-formatting
        # "5,000.00" into the row would sort it as a string, putting 5,000.00 before
        # 42.50 -- and the point of moving to a real table was to get sorting.
        money = (':format', 'value => Number(value).toLocaleString("en-US", '
                            '{minimumFractionDigits: 2, maximumFractionDigits: 2})')
        columns = [
            {'name': 'event_date', 'label': 'Date', 'field': 'event_date',
             'sortable': True, 'align': 'left'},
            {'name': 'event_type', 'label': 'Type', 'field': 'event_type',
             'sortable': True, 'align': 'left'},
            {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol',
             'sortable': True, 'align': 'left'},
            {'name': 'amount', 'label': 'Amount', 'field': 'amount',
             'sortable': True, 'align': 'right', money[0]: money[1]},
            {'name': 'open_amount', 'label': 'Open', 'field': 'open_amount',
             'sortable': True, 'align': 'right', money[0]: money[1]},
        ]
        rows = [{
            # ``str``, because NiceGUI sends the rows to the browser as JSON and a
            # ``date`` is not serialisable. The hand-rolled version got away with it
            # only because it called ``str()`` on the way into a label.
            'event_date': str(event['event_date']),
            'event_type': event['event_type'],
            # A deposit has no payer symbol; a bare None renders as an EMPTY cell,
            # which reads as "we do not know" rather than "there is none".
            'symbol': event['symbol'] or '-',
            'amount': float(event['amount']),
            'open_amount': float(event['open_amount']),
            'external_id': event['external_id'],
        } for event in events]
        ui.table(columns=columns, rows=rows, row_key='external_id') \
            .classes('w-full dark-pagination')


#: Status -> colour class. Keyed on the SERVICE's own constants, never on
#: literals: a renamed constant would silently stop matching, and the failure
#: count below would then read 0 for a run in which everything failed.
OUTCOME_COLOURS = {
    OUTCOME_SUBMITTED: 'text-green-500',
    OUTCOME_PARTIAL: 'text-yellow-500',
    OUTCOME_SKIPPED: 'text-gray-400',
    OUTCOME_WASHTRADE_LOCKED: 'text-orange-400',
    # NOT the grey of SKIPPED, which is the whole reason this status exists: the
    # position is held, the user asked to exit it and the run had no route to it.
    OUTCOME_UNACTIONABLE: 'text-red-400',
    OUTCOME_FAILED: 'text-red-500',
}

#: NiceGUI marker on the outcome table's "Filled" cell, so a test can read the
#: column without matching on a quantity string that also appears in "Qty".
MARKER_OUTCOME_FILLED = 'outcome-filled'


def retryable_outcomes(outcomes: List) -> List[str]:
    """The symbols worth a second attempt, in table order and de-duplicated. Pure.

    FAILED only. Deliberately NOT ``OUTCOME_WASHTRADE_LOCKED`` -- that order is
    still armed and TradeManager re-submits it once the blocker clears, so a
    retry here would queue a SECOND order for the same symbol and both would
    eventually fill. Nor ``OUTCOME_UNACTIONABLE``, which no retry can help (every
    open transaction behind the position is one the equity planner does not act
    on -- a human has to unwind it), nor ``OUTCOME_PARTIAL``, where the rest of
    the order may still be working at the broker.
    """
    seen, out = set(), []
    for outcome in outcomes or []:
        if outcome.status == OUTCOME_FAILED and outcome.symbol not in seen:
            seen.add(outcome.symbol)
            out.append(outcome.symbol)
    return out


def render_outcomes(outcomes: List, *, run_id: Optional[int] = None,
                    on_retry: Optional[Callable[[List[str]], None]] = None) -> None:
    """Per-row outcome table shown after Submit.

    Partial failure is normal: a failed row sits next to a filled one and nothing
    is rolled back, so every row is listed with its own status and message.

    ``Filled`` is shown next to ``Qty`` because they differ in the cases that
    matter: a partially filled order, and a fractional order that fell back to
    whole shares. ``filled_quantity is None`` means the broker reported no fill
    at all -- an accepted market order before the open looks exactly like that --
    and is drawn as "-", never as 0, which would read as "nothing filled".

    ``on_retry`` is offered ONLY when something actually failed
    (``retryable_outcomes``). Re-running the flow re-solves against the positions
    as they are NOW, so the rows that filled are already gone from the new plan
    and the ones that failed are still in it -- the retry is a re-solve, never a
    replay of the orders that were just sent.
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
        retryable = retryable_outcomes(outcomes)
        with ui.row().classes('w-full justify-end mt-2 gap-2'):
            if retryable and on_retry is not None:
                def _retry(symbols=list(retryable)):
                    # Closed FIRST: the retry re-solves and opens a fresh dry run,
                    # and two stacked dialogs describing two different plans is
                    # exactly the confusion this feature exists to remove.
                    dialog.close()
                    on_retry(symbols)
                ui.button(RETRY_FAILED_FMT.format(count=len(retryable)),
                          on_click=_retry).props('outline') \
                    .mark(MARKER_OUTCOME_RETRY).tooltip(RETRY_TOOLTIP)
            ui.button('Close', on_click=dialog.close).props('flat')
    dialog.open()

    failed = sum(1 for o in outcomes if o.status == OUTCOME_FAILED)
    unactionable = sum(1 for o in outcomes if o.status == OUTCOME_UNACTIONABLE)
    locked = sum(1 for o in outcomes if o.status == OUTCOME_WASHTRADE_LOCKED)
    if failed:
        ui.notify(f'{failed} row(s) failed - see the results table', type='warning')
    elif unactionable:
        # Ahead of the lock and, above all, ahead of the positive toast. Nothing
        # was refused and nothing was sent, so without this the user closes a
        # dialog full of grey rows on a green "submitted" while the position they
        # asked to exit is untouched.
        ui.notify(f'{unactionable} row(s) could NOT be acted on - the position is '
                  f'still open; see the results table', type='warning')
    elif locked:
        # Not a failure: the order is PENDING at our end and is retried once the
        # blocker clears. Saying nothing would leave the user believing it traded.
        ui.notify(f'{locked} row(s) are wash-trade locked and will be retried',
                  type='warning')
    else:
        ui.notify('Allocation run submitted', type='positive')
