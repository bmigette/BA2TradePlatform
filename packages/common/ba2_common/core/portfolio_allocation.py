"""Portfolio allocation arithmetic (pure; no DB, no broker, no UI).

Turns target percentages into per-symbol share deltas:

    investable      = base_notional * (100 - unallocated_pct) / 100
    label_notional  = investable * label.target_pct / 100
    symbol_notional = label_notional * symbol.weight_pct / 100   (targets SUM
                      when a symbol carries more than one managed label)
    delta_quantity  = SIGNED shares to trade; how it is derived depends on the
                      valuation mode (see ``compute_allocation``)
    target_quantity = current_quantity + delta_quantity          (POST-TRADE)
    bp_cost         = |delta_notional| * bp_factor(symbol)       (buys only)
    bp_released     = |delta_notional| * bp_factor(symbol)       (sells only)

A SELL FREES BUYING POWER AND THE PLAN MAY SPEND IT. The scaling budget is
``available_buying_power + sum(bp_released of sells)``; when the buys still do not
fit it, every BUY scales down pro-rata and the plan records ``scale_factor``.
Sells never scale. ``bp_released`` is the exact mirror of ``bp_cost`` -- the same
notional at the same ``bp_factor`` -- because buying a position and selling it
back must move the budget by the same amount in both directions.

Submission sends every sell BEFORE any buy (decision 13), which is what makes the
freed money real by the time a buy needs it. A sell the broker refuses can leave a
buy short of room; that buy is rejected on its own row rather than silently
overspending, and un-ticking a sell in the dry run re-measures the budget before
anything is sent (``filter_plan_rows``).

THE RESERVE IS A SEPARATE, STORED NUMBER -- ``unallocated_pct``, held on
``PortfolioAllocationConfig``. Label targets are RELATIVE weights that always total
exactly 100 among themselves; the reserve says what share of the base is
deliberately NOT invested and scales the investable remainder ONCE, at the base.
Reserve 10% with labels 50/30/20 puts 45/27/18 of the base to work and holds 10% as
cash -- and nothing the user typed is ever rewritten to say so.

That is deliberately the ONLY way to hold cash. An earlier design made a label
total below 100 the reserve; it is superseded, because with both mechanisms present
"labels sum to 90" would mean two different things.

Percentages are validated within ``LABEL_TOTAL_TOLERANCE_PCT`` (0.01 PERCENTAGE
POINTS), and every level totals EXACTLY 100:

* LABEL targets across the account. Over AND under are both hard errors -- over
  because the plan cannot buy money the account does not have, under because the
  reserve is where that intent belongs.
* SYMBOL weights within each label. There is no per-label reserve:
  ``compute_allocation`` multiplies those weights straight through, so a 60% set
  would leave 40% of that label's money undeployed with nothing to record it.

The tolerance is tight enough to reject a naive 2dp even split -- ``3 x 33.33 ==
99.99`` misses by a hair MORE than 0.01 -- so ALWAYS generate default percentages
with ``even_split_pct``, which drops the remainder on the last slot and totals
exactly 100.0. Never hand-roll a split.
"""

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime as DateTime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.account_types import (  # noqa: F401 (re-exported)
    MARGIN_SOURCE_ASSET, MARGIN_SOURCE_DEFAULT, MARGIN_SOURCE_POSITION,
    MARGIN_SOURCE_PRECHECK, AccountSnapshot, MarginInfo, OrderImpact,
)
from ba2_common.core.types import OrderDirection, OrderStatus
from ba2_common.logger import logger

#: Public API. The in-tree module is a whole-module alias shim, so everything here
#: is reachable as ``ba2_trade_platform.core.portfolio_allocation.<name>`` too.
#: ``MarginInfo`` / ``OrderImpact`` / ``AccountSnapshot`` are deliberate re-exports:
#: a caller building engine inputs should not have to import from two modules.
__all__ = [
    # value objects and the fetch-failure sentinel
    "PositionState", "SymbolTarget", "LabelTarget", "AllocationRow", "AllocationPlan",
    "PositionFetchFailed", "AccountSnapshot", "MarginInfo", "OrderImpact",
    # the ONE reconciliation of the two brokers' short conventions
    "position_sign", "signed_position_values",
    # wizard
    "BaseSnapshot", "build_base_snapshot", "WARNING_NO_MULTIPLIER",
    "held_symbols_without_price", "held_no_price_block", "ERROR_HELD_NO_PRICE_FMT",
    "dry_run_rows", "filter_plan_rows", "summarise_plan", "DRY_RUN_QUANTITY_DECIMALS",
    # pre-submit validation
    "validate_plan_rows", "validate_plan_budget",
    "REFUSAL_NOT_TRADABLE_FMT", "REFUSAL_FRACTIONAL_NOT_ELIGIBLE_FMT",
    "REFUSAL_BELOW_MIN_ORDER_FMT", "REFUSAL_BELOW_MIN_NOTIONAL_FMT",
    "REFUSAL_NO_PRICE_FMT", "REFUSAL_PRECHECK_FMT", "REFUSAL_OVER_BUDGET_FMT",
    "PRECHECK_REASON_UNKNOWN",
    "even_split_targets", "steps_validation_messages", "validate_invest_amount",
    "load_previous_targets", "has_previous_targets",
    "load_previous_symbol_weights", "has_previous_symbol_weights",
    "even_split_symbol_weights", "can_even_split_symbols",
    "fill_remaining_symbol_weights", "can_fill_remaining_symbol_weights",
    "wipe_symbol_weights", "can_wipe_symbol_weights",
    "invest_validation_messages", "is_blocking_message", "blocking_messages",
    "ERROR_INVEST_AMOUNT_FMT", "ERROR_INVEST_NO_LABEL", "ERROR_INVEST_LABEL_EMPTY_FMT",
    "WARNING_INVEST_EXCEEDS_BP_FMT", "ADVISORY_MESSAGE_FRAGMENTS",
    # modes
    "ALLOCATION_MODE_REBALANCE", "ALLOCATION_MODE_INVEST_LABEL",
    "VALUATION_MODE_COST", "VALUATION_MODE_MARKET",
    # tolerances and grid
    "DEFAULT_FRACTIONAL_DECIMALS", "LABEL_TOTAL_TOLERANCE_PCT",
    "QUANTITY_EPSILON", "MONEY_EPSILON", "BUMP_TO_ONE_SHARE_MAX_MULTIPLE",
    # per-row sizing outcomes (D1)
    "SIZING_OUTCOME_NORMAL", "SIZING_OUTCOME_BUMPED", "SIZING_OUTCOME_SKIPPED_TOO_LARGE",
    "SIZING_OUTCOME_BUMPED_DROPPED",
    # what a plan's target_notional MEANS, and the residual loop's bound (D2)
    "ALLOCATION_BASIS_POSITION", "ALLOCATION_BASIS_BUDGET", "REDISTRIBUTION_MAX_PASSES",
    # reason / warning / error strings
    "REASON_NO_PRICE", "REASON_NOT_MARGINABLE", "REASON_FRACTIONAL",
    "REASON_WHOLE_SHARE_FLOOR", "REASON_FRACTIONAL_UNKNOWN",
    "REASON_NEGATIVE_CLAMPED", "REASON_CLOSE_TO_ZERO",
    "REASON_BUMPED_TO_ONE_SHARE_FMT", "REASON_BELOW_ONE_SHARE_FMT",
    "REASON_BUMP_BLOCKED_MIN_ORDER_FMT", "REASON_ROUNDS_TO_ZERO_FMT",
    "REASON_REDISTRIBUTED_FMT", "REASON_REDISTRIBUTED_PREFIX",
    "WARNING_RESIDUAL_LEFT_FMT", "WARNING_RESIDUAL_UNCONVERGED_FMT",
    "REASON_FRACTIONAL_FLOOR_BUMPED_FMT", "REASON_FRACTIONAL_FLOOR_SKIPPED_FMT",
    "REASON_BELOW_MIN_ORDER_FMT", "REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT",
    "REASON_MULTI_LABEL_FMT", "REASON_SCALED_FMT", "REASON_RECLAIMED_FMT",
    "REASON_SCALED_PREFIX", "REASON_BELOW_MIN_ORDER_PREFIX",
    "REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX",
    "WARNING_EMPTY_LABEL_FMT", "WARNING_PRECHECK_DISAGREED_FMT",
    "ERROR_LABEL_TOTAL_FMT", "ERROR_LABEL_UNDER_FMT", "ERROR_UNALLOCATED_RANGE_FMT",
    "ERROR_LABEL_NEGATIVE_FMT", "ERROR_LABEL_DUPLICATE_FMT",
    "ERROR_LABEL_NO_SYMBOLS_FMT", "ERROR_SYMBOL_OVER_FMT", "WARNING_SYMBOL_UNDER_FMT",
    "ERROR_SYMBOL_NEGATIVE_FMT", "ERROR_SYMBOL_DUPLICATE_FMT",
    # engine
    "current_value", "UnrealisedPnL", "unrealised_pnl", "format_unrealised_pnl",
    "PNL_UNMEASURABLE_MARK", "PNL_NO_PRICE_MARK", "PNL_PCT_FMT", "PNL_NO_COST_NOTE",
    "PNL_UNPRICED_FMT", "PNL_FMT",
    "round_quantity", "round_delta_quantity", "even_split_pct",
    "split_pct_across", "scale_pct_to_total",
    "build_symbol_targets", "validate_symbol_weights", "validate_label_targets",
    "validate_unallocated_pct", "clamp_unallocated_pct",
    "investable_notional", "reserved_notional_for",
    "compute_base_notional", "compute_allocation", "compute_label_investment",
    "apply_order_impacts", "consume_income_events",
    # filled-value measurement (the income ledger's input)
    "SETTLED_ORDER_STATUSES", "UNEXECUTED_ORDER_STATUSES",
    "OrderFill", "FilledTotals", "measure_filled_values",
    "UNCONSUMED_RUNS_NOTICE_FMT", "UNFINISHED_RUNS_NOTICE_FMT",
    "unconsumed_income_notice",
    "tradeable_unit", "size_sub_unit_target", "projected_value", "allocated_value",
    "redistribute_label_residuals",
    # per-row leverage, for the dry-run table (W6/W7)
    "bp_leverage", "LEVERAGE_VERDICTS", "LEVERAGE_NONE", "LEVERAGE_LEVERAGED",
    "LEVERAGE_PENALISED", "LEVERAGE_UNKNOWN", "LEVERAGE_NOT_APPLICABLE",
    "LEVERAGE_RATIO_TOLERANCE",
    "MARGIN_SOURCE_ASSET", "MARGIN_SOURCE_DEFAULT", "MARGIN_SOURCE_POSITION",
    "MARGIN_SOURCE_PRECHECK",
    # mixed eligibility, bumps and redistribution, surfaced for the dry run
    "fractional_summary", "no_order_rows", "whole_share_notice", "no_order_notice",
    "bump_notice", "redistribution_notice",
    "WHOLE_SHARE_NOTICE_FMT", "WHOLE_SHARE_NOTICE_OFF_FMT", "NO_ORDER_NOTICE_FMT",
    "NO_ORDER_NOTICE_MIXED_FMT",
    "BUMP_NOTICE_FMT", "REDISTRIBUTION_NOTICE_FMT",
    # submission
    "ACTION_ADJUST", "ACTION_CLOSE", "ACTION_NEW", "ACTION_SKIP",
    "ACTION_UNACTIONABLE",
    "decide_symbol_action", "split_delta_fifo",
    "FRACTIONAL_PATH_FRACTIONAL", "FRACTIONAL_PATH_WHOLE", "plan_quantity_attempts",
]

# ---- module constants (exact spellings; use these, never bare literals) ----

ALLOCATION_MODE_REBALANCE = "REBALANCE"
ALLOCATION_MODE_INVEST_LABEL = "INVEST_LABEL"

#: How "current value" is measured -- see ``current_value`` and decision 5a.
#: PLAIN str, matching the ``portfolio_allocation_config.valuation_mode`` column.
VALUATION_MODE_COST = "cost"
VALUATION_MODE_MARKET = "market"

#: Decimal places used for a fractional quantity when the broker publishes no
#: ``min_trade_increment``.
DEFAULT_FRACTIONAL_DECIMALS = 4

#: Decimal places the DRY-RUN TABLE shows a share quantity to. Deliberately wider
#: than DEFAULT_FRACTIONAL_DECIMALS: that 4 is a SIZING fallback, this is a
#: DISPLAY guard against float noise, and the two must not be tied together.
#: TastyTrade's equity ``QuantityDecimalPrecision.value`` is 5, so displaying at 4
#: would print 1.6667 for an order that is really 1.66666 -- the dry-run's whole
#: job is to state what will be sent, so it may never round the number tighter
#: than the broker's own grid.
DRY_RUN_QUANTITY_DECIMALS = 8

#: Tolerance (percentage points) when checking that label targets total 100.
LABEL_TOTAL_TOLERANCE_PCT = 0.01

#: SHARE quantities closer to zero than this are exactly zero (float noise guard).
#: Shares only -- money has its own tolerance, because the two are different units
#: and tightening one must never silently move the other.
QUANTITY_EPSILON = 1e-9

#: MONEY amounts closer to zero than this are exactly zero. Looser than
#: QUANTITY_EPSILON: a tenth of a microdollar of residual income is not worth a
#: ledger write, whereas 1e-9 of a share can matter on a fractional grid.
MONEY_EPSILON = 1e-6

#: The five verdicts ``bp_leverage`` can return, and the ONLY five the dry-run
#: table knows how to draw. See ``bp_leverage`` for the predicate and the live
#: numbers behind it; the short version is that the neutral point is 1.0 on every
#: live account shape, so a HIGHER ratio means LESS leverage, not more.
#:
#: ``LEVERAGE_NONE``            ratio == 1.0 -- a dollar of stock costs a dollar of BP
#: ``LEVERAGE_LEVERAGED``       ratio <  1.0 -- costs LESS BP than its notional
#: ``LEVERAGE_PENALISED``       ratio >  1.0 -- costs MORE BP than its notional
#: ``LEVERAGE_UNKNOWN``         the broker published no margin rate; no verdict
#: ``LEVERAGE_NOT_APPLICABLE``  a SELL: it frees buying power, it never charges any
LEVERAGE_NONE = "none"
LEVERAGE_LEVERAGED = "leveraged"
LEVERAGE_PENALISED = "penalised"
LEVERAGE_UNKNOWN = "unknown"
LEVERAGE_NOT_APPLICABLE = "n/a"
LEVERAGE_VERDICTS = (LEVERAGE_NONE, LEVERAGE_LEVERAGED, LEVERAGE_PENALISED,
                     LEVERAGE_UNKNOWN, LEVERAGE_NOT_APPLICABLE)

#: How far a BP ratio may sit from 1.0 and still be called neutral. Its own
#: constant and NOT MONEY_EPSILON: this is a dimensionless ratio, and a broker that
#: publishes a rate to 4dp lands a hair off 1.0 through float division alone.
LEVERAGE_RATIO_TOLERANCE = 1e-6

# Reason strings attached to AllocationRow.reasons / AllocationPlan.warnings.
# Pinned here so the UI and the tests agree on the exact text.
REASON_NO_PRICE = "no price - skipped"
REASON_NOT_MARGINABLE = "⚠ not marginable"
REASON_FRACTIONAL = "fractional"
REASON_WHOLE_SHARE_FLOOR = "rounded down to whole shares"
#: Fractional was requested but the broker did not SAY whether this symbol is
#: eligible: either no ``MarginInfo`` row at all, or one whose ``fractionable`` is
#: ``None``. DISTINCT from REASON_WHOLE_SHARE_FLOOR, which means the broker
#: positively answered "not fractionable". One is a data gap the user can fix with
#: Refresh; the other is a fact about the instrument. TastyTrade's own flag is
#: tri-state (``Equity.is_fractional_quantity_eligible: bool | None``), so
#: "unknown" is a real state, not a bug.
REASON_FRACTIONAL_UNKNOWN = "fractionable unknown - whole shares"
REASON_NEGATIVE_CLAMPED = "negative target clamped to 0"
REASON_CLOSE_TO_ZERO = "target 0 - close position"

#: How far ONE tradeable unit may overshoot a target before the bump is refused.
#: 2.0 == "one share may cost at most 200% of what this symbol was allocated".
#:
#: 2.0 IS THE ROUNDING RULE, stated as a multiple. Operator decision (2026-08-31):
#: "we should have 1 share if the round of qty is >= 1 -- buy 1 share if we want to
#: buy 0.6, but 0 if we want to buy 0.3". Round-half-up on the raw share count says
#: bump when ``raw >= 0.5``, and one unit costs ``1 / raw`` of the target, so
#: ``raw >= 0.5`` is exactly ``multiple <= 2.0``. Writing it as the multiple keeps ONE
#: comparison in the sizing path and keeps the refusal message able to quote the bound;
#: implementing it as a separate ``round()`` branch would be a second rule that agrees
#: with this one only by coincidence. The bound is INCLUSIVE, which is what puts the
#: exact 0.5 case (200%) on the "buy" side, as round-half-up requires.
#:
#: WAS 1.5, and the reason it changed is worth keeping. At 1.5 the worked examples were
#: a 200 target on a 300 share (150%, filled, exactly on the bound) and a 50 target on a
#: 500 share (1000%, refused) -- but it also refused ``raw = 0.6`` at 167%, which is the
#: case the operator wanted filled: a symbol allocated two thirds of a share bought
#: nothing at all.
#:
#: THE COST, stated plainly because the old comment warned about exactly this value:
#: the worst bump now over-allocates its symbol by 100% of that symbol's target instead
#: of 50%. ``redistribute_label_residuals`` takes the excess back off the label's
#: FRACTIONABLE siblings, and at 2x that excess can exceed what the siblings hold, which
#: leaves the label over target with nothing able to fix it. That is the trade the
#: rounding rule buys, and it is bounded: it can only happen on a symbol whose whole
#: allocation was under one share, so the absolute over-allocation is at most the price
#: of a single share.
BUMP_TO_ONE_SHARE_MAX_MULTIPLE = 2.0

#: What the sizing rules DID to a row, so the dry run never has to pattern-match
#: reason prose. A bump is a deliberate over-allocation and must be visible.
SIZING_OUTCOME_NORMAL = "normal"                        #: ordinary grid rounding
SIZING_OUTCOME_BUMPED = "bumped-to-1"                   #: under one unit, bumped UP
SIZING_OUTCOME_SKIPPED_TOO_LARGE = "skipped-too-large"  #: under one unit, one unit too big
#: The bump was taken and then UNDONE by a later step -- the buying-power scaler
#: cut the row back under one unit, or a broker precheck refused it. The row ends
#: with NO ORDER, so it may not keep saying ``bumped-to-1``: that outcome claims a
#: deliberate OVER-allocation and this row holds nothing. Emphatically NOT
#: ``skipped-too-large``, which means the bump was never taken because one unit
#: would have overshot the target; here one unit was perfectly acceptable and the
#: money ran out. See ``_reconcile_sizing_outcomes``.
SIZING_OUTCOME_BUMPED_DROPPED = "bumped-then-dropped"

#: The bump happened: the whole intended position was smaller than one tradeable
#: unit and one unit was inside BUMP_TO_ONE_SHARE_MAX_MULTIPLE, so the symbol is
#: OVER-allocated on purpose and the sentence says by how much.
#: Must not start with "below" and must not contain "held" -- both are pinned by
#: the landed suite (test_portfolio_allocation.py's min-order-size scenarios).
REASON_BUMPED_TO_ONE_SHARE_FMT = (
    "target {target:,.2f} buys {raw:.4f} shares at {price:,.2f} - BUMPED UP to "
    "{unit:g} share(s), {pct:.0f}% of target")
#: The bump was refused: one unit would overshoot past the bound. No order, and the
#: row carries the shortfall in ``unmet_notional`` so the user can widen the weight.
REASON_BELOW_ONE_SHARE_FMT = (
    "target {target:,.2f} buys {raw:.4f} shares at {price:,.2f} - no order; the "
    "smallest tradeable order is {unit:g} share(s), {pct:.0f}% of target, over "
    "the {limit:.0f}% bump limit")
#: The FRACTIONAL variants of the two above. The fraction was legal on the grid but
#: is under the broker's fractional NOTIONAL floor (TastyTrade HTTP 422
#: ``below_notional_value_minimum``). That floor is a minimum in MONEY, so the two
#: ways out are a BIGGER fraction (the smallest grid multiple worth the floor) and
#: one WHOLE share (exempt from the rule); ``{unit}`` is whichever is cheaper, which
#: is why these say "share(s)" and not "whole share(s)". Saying "rounds to zero"
#: here would be a lie: the quantity was fine, the money was not.
REASON_FRACTIONAL_FLOOR_BUMPED_FMT = (
    "target {target:,.2f} is under the broker's ${minimum:g} fractional minimum "
    "so no fraction that small can be sent - BUMPED UP to {unit:g} share(s) at "
    "{price:,.2f}, {pct:.0f}% of target")
REASON_FRACTIONAL_FLOOR_SKIPPED_FMT = (
    "target {target:,.2f} is under the broker's ${minimum:g} fractional minimum "
    "so no fraction that small can be sent - no order; {unit:g} share(s) at "
    "{price:,.2f} is {pct:.0f}% of target, over the {limit:.0f}% bump limit")
#: The bump was not even attempted: one share is under the broker's minimum ORDER
#: size, so there is no order to place at any size.
REASON_BUMP_BLOCKED_MIN_ORDER_FMT = (
    "target {target:,.2f} buys {raw:.4f} shares at {price:,.2f} - no order; one "
    "share is under the broker minimum order size {size:g}")
#: A non-zero ADJUSTMENT to an existing position that the tradeable grid rounded
#: away. Never bumped: the position already exists, and turning a -0.4 trim into a
#: whole-share sale is a trade nobody asked for.
REASON_ROUNDS_TO_ZERO_FMT = "{raw:+.4f} shares rounds to 0 on the tradeable grid - no order"

#: How many redistribution passes a label gets before the engine gives up and
#: reports what is left. The loop is finite on its own arithmetic -- every step is
#: floored onto the absorbing row's grid, so it can close the gap but never cross
#: it, |residual| is monotone, and a pass that moves nothing ends the loop at its
#: fixed point. In practice that happens on pass 1 or 2. This bound exists so that
#: TERMINATION is guaranteed by the code rather than by the argument: a future
#: absorber type that breaks the monotonicity still stops here, and says so.
REDISTRIBUTION_MAX_PASSES = 3

#: What ``AllocationRow.target_notional`` MEANS in a given plan, which differs by
#: solver and cannot be guessed from the numbers.
#:   position -- compute_allocation: the desired POST-TRADE holding value.
#:   budget   -- compute_label_investment: money to DEPLOY on top of what is held.
#: Comparing one against the other produces a nonsense residual, which is why the
#: plan carries the answer instead of each caller assuming one.
ALLOCATION_BASIS_POSITION = "position"
ALLOCATION_BASIS_BUDGET = "budget"

#: Stamped on a row whose quantity was moved to keep its LABEL on target. The
#: weights the user typed are NOT rewritten (``target_notional`` is untouched); the
#: quantity is, and this says so in shares. Silently changing a user's weights is
#: unacceptable -- showing the change is what makes the change allowed.
REASON_REDISTRIBUTED_FMT = (
    "weight adjusted {before:+.4f} -> {after:+.4f} shares to keep label "
    "'{label}' on target")
REASON_REDISTRIBUTED_PREFIX = REASON_REDISTRIBUTED_FMT.split("{", 1)[0]

#: The label finished off target at its FIXED POINT because nothing in it was
#: ALLOWED to absorb the rest (buying power, a broker minimum, the
#: no-negative-position clamp, an invest run's no-sell rule). Only raised when some
#: member could physically have taken it: a residual smaller than one tradeable unit
#: of every member is arithmetic, not a fault. "Smaller" is measured in the SAME
#: money as the residual -- in cost mode one share of a held position moves the
#: label by its AVERAGE COST, so comparing against the PRICE cries wolf on a
#: leftover no member could have taken.
WARNING_RESIDUAL_LEFT_FMT = (
    "label '{label}' is {residual:,.2f} off target after redistribution - nothing "
    "in it can absorb the rest (buying power, broker minimums, an invest run never "
    "sells, or it would drive a position to zero)")
#: The label finished off target because the pass bound stopped a loop that was
#: STILL MOVING -- the one case where a bigger bound would change the answer. A loop
#: that reached its own fixed point is never reported this way even when it used its
#: last allowed pass to get there: passes are not what is wrong with it, and sending
#: the user after the bound hides the absorber that actually said no.
WARNING_RESIDUAL_UNCONVERGED_FMT = (
    "label '{label}' is still {residual:,.2f} off target after the {passes} "
    "redistribution passes allowed")
#: Says only that no order is sent -- NOT what happens to the position, which
#: differs by branch (a suppressed trim holds; a suppressed top-up never opens).
#: "position held" here contradicted REASON_CLOSE_TO_ZERO on an unsendable close.
REASON_BELOW_MIN_ORDER_FMT = "below broker min order size {size:g} - no order"
#: The MONEY floor, and the "$" is load-bearing: REASON_BELOW_MIN_ORDER_FMT above
#: is a SHARE count and the two must never read as the same rule. Carries the
#: order's own value because "under $5" is only actionable next to "this one is
#: $1.95". Applies to FRACTIONAL quantities only -- see MarginInfo.
REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT = (
    "fractional order ${value:.2f} below the broker's ${minimum:g} minimum - no order")
#: Renders INSIDE a label's panel, so it must not read as "also in <this panel>";
#: the full list including the current label is the useful information.
REASON_MULTI_LABEL_FMT = "⚠ in {labels}"
REASON_SCALED_FMT = "scaled ×{factor:.2f} to fit buying power"
#: A row the pro-rata scale rounded away, then funded again out of the slack the
#: OTHER rows' rounding left behind. See ``_reclaim_rounding_slack``.
REASON_RECLAIMED_FMT = ("restored {qty:g} share(s) from the ${slack:,.2f} the "
                        "rounding left unspent")
#: The fixed part of REASON_SCALED_FMT, derived from it so the two cannot drift.
#: Used to RECOGNISE a scaling reason a row already carries, so that a plan scaled
#: twice (first solve, then broker precheck) reports ONE reason with the compounded
#: factor instead of two that each tell half the story.
REASON_SCALED_PREFIX = REASON_SCALED_FMT.split("{", 1)[0]
#: The fixed parts of the two ``_suppress_below_min_order`` reasons, derived from
#: the formats so they cannot drift. Used by the dry-run table to RECOGNISE a row
#: whose order was suppressed: those rows carry a zero delta, so without this they
#: would vanish from the review exactly like a row that never had a target, and
#: the user would be told "nothing to do" about an order the broker refused.
REASON_BELOW_MIN_ORDER_PREFIX = REASON_BELOW_MIN_ORDER_FMT.split("{", 1)[0]
REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX = (
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.split("{", 1)[0])
#: An empty label, named with BOTH of its percentages. They are the same number
#: only when there is no reserve: the weight is what the user typed into the box
#: and divides the investable remainder, while the share of base is what actually
#: goes idle and is the figure that lands in ``unallocatable_pct``. Naming only the
#: first restates the field's own denominator defect in prose; naming only the
#: second sends the user hunting for a box holding a percentage nobody typed.
WARNING_EMPTY_LABEL_FMT = ("label '{label}' has no symbols - its {pct:.2f}% weight "
                           "({base_pct:.2f}% of the base) can absorb nothing")
WARNING_PRECHECK_DISAGREED_FMT = "broker precheck disagreed on {symbol} - re-solved"

# Validation messages from ``validate_label_targets``. Pinned so the UI and the
# tests agree on the exact text.
#: OVER 100. Hard: the plan cannot buy money the account does not have, and
#: without this the buying-power scaler would silently shrink every row to fit
#: instead. Names the OVERSHOOT as well as the total, because "118%" alone leaves
#: the user doing the subtraction that tells them which box to change.
ERROR_LABEL_TOTAL_FMT = "label targets total {total:.2f}% - over 100% by {over:.2f}%"

#: UNDER 100, and equally hard. An earlier design let a shortfall BE the cash
#: reserve; the reserve is now a stored ``unallocated_pct``, and keeping both would
#: make "labels sum to 90" mean two different things -- "hold 10% in cash" and "you
#: mistyped a box" -- which produce identical numbers and only the user can tell
#: apart. So the shortfall goes back to being an error, and the sentence NAMES the
#: box that does want a number, or the user has been told off without being told
#: what to do.
ERROR_LABEL_UNDER_FMT = ("label targets total {total:.2f}% - under 100% by "
                         "{under:.2f}%. Use the Unallocated box to hold money "
                         "back.")

#: The reserve's own range check. Below 0 would INFLATE the investable base into
#: money the account does not have; above 100 would make it negative.
#:
#: ``compute_allocation`` does NOT refuse either -- it CLAMPS, through
#: ``clamp_unallocated_pct``, and that is exactly why this has to block. A clamped
#: reserve solves a different plan from the one the user asked for and says nothing
#: about having done so: a -20 silently becomes 0 and deploys the whole book. This
#: is where it is caught, where the message can name the value and the user can see
#: which box.
#:
#: ``{pct:g}``, never ``{pct:.2f}``. This validator has NO tolerance -- 100.005 is
#: out of range -- and at two decimals the message read "unallocated 100.00% is
#: outside 0-100%", a sentence that refutes itself by rounding the evidence into
#: the legal range. ``g`` prints the digits that are there and no more: 140, -5,
#: 100.005.
#:
#: "undeployed", not "held as cash": the reserve is a share of a base that INCLUDES
#: buying power, so what it holds back is money the plan does not put to work --
#: which on a margin account is not a cash balance.
ERROR_UNALLOCATED_RANGE_FMT = (
    "unallocated {pct:g}% is outside 0-100% - it is the share of the base to leave "
    "undeployed")
ERROR_LABEL_NEGATIVE_FMT = "label '{label}' has a negative target ({pct:.2f}%)"
ERROR_LABEL_DUPLICATE_FMT = "duplicate label '{label}'"
ERROR_LABEL_NO_SYMBOLS_FMT = "label '{label}' has target {pct:.2f}% but no symbols"

#: OVER 100 WITHIN one label. Hard, mirroring ERROR_LABEL_TOTAL_FMT at the symbol
#: scope: compute_allocation multiplies the weights straight through with no cap,
#: so a symbol split over 100% deploys MORE than the label's own target -- a 150%
#: split turns a label's share of the base into 1.5x that money, the same
#: over-deploy risk the cross-label check exists to catch, just scoped to one
#: label instead of the whole account.
ERROR_SYMBOL_OVER_FMT = ("label '{label}' symbol weights total {total:.2f}% - over 100% "
                         "by {over:.2f}%")

#: UNDER 100 WITHIN one label -- ADVISORY, unlike every other total-100 check in
#: this module (2026-09-05, live use: a 90% split was blocking Submit and the
#: operator asked for it not to). The two "under 100" checks LOOK identical but
#: are not: the cross-label one (ERROR_LABEL_UNDER_FMT) leaves a share of the
#: WHOLE ACCOUNT unaccounted for and the design deliberately reversed a prior
#: "shortfall is the reserve" reading to catch that as a mistake. A shortfall
#: HERE only ever leaves part of THIS ONE LABEL's own money undeployed -- exactly
#: the same harmless shape as an empty label's WARNING_EMPTY_LABEL_FMT, and
#: sometimes exactly what the user wants (a slice of a label held back on
#: purpose). Blocking Submit over it told the user to go fix a number that was
#: never wrong.
#:
#: The fragment is what ``is_blocking_message`` reads to tell this apart from
#: ``ERROR_SYMBOL_OVER_FMT`` (shares "symbol weights total") and from
#: ``ERROR_LABEL_UNDER_FMT`` (shares "under 100% by") -- neither of those two may
#: become advisory, so the fragment has to be text that occurs in THIS message
#: alone. Chosen to also read as a plain-English explanation on screen rather
#: than an opaque marker.
_SYMBOL_UNDER_FRAGMENT = "undeployed within this label"
WARNING_SYMBOL_UNDER_FMT = ("label '{label}' symbol weights total {total:.2f}% - under 100% "
                            "by {under:.2f}%; the shortfall stays " + _SYMBOL_UNDER_FRAGMENT)
ERROR_SYMBOL_NEGATIVE_FMT = "label '{label}' symbol '{symbol}' has a negative weight ({pct:.2f}%)"
ERROR_SYMBOL_DUPLICATE_FMT = "label '{label}' has duplicate symbol '{symbol}'"

#: MARKET mode, and a symbol the account HOLDS has no usable quote. BLOCKING, and
#: it is the flip to market valuation that makes it live -- in cost mode the price
#: is never read. The position contributes 0 to ``base_notional``, so every OTHER
#: label's target shrinks by its share of the missing money, and the dry run cannot
#: show it because every row is consistently too small. Names the symbols and the
#: two ways out (retry the quote, or fall back to cost basis). Never merged with
#: REASON_NO_PRICE: that one is about a row, this one is about the denominator.
ERROR_HELD_NO_PRICE_FMT = (
    "market valuation needs a live price for every HELD symbol - {count} have none "
    "({symbols}). They count as 0 in the allocatable base, so every label's target "
    "is understated. Retry the quote, or switch the page to cost basis.")


class PositionFetchFailed(RuntimeError):
    """The broker's position fetch FAILED (``get_positions()`` returned ``None``).

    Distinct from a genuinely flat account (``[]``). Conflating the two on
    2026-07-03 mass-closed 8 real open transactions during a DNS outage, which is
    why this is an exception rather than an empty dict.

    It lives in the PURE engine so that the live service
    (``core/portfolio_allocation_service.py``) and the UI view-model
    (``ui/utils/portfolio_allocation_view.py``) can raise and catch the same
    class without either importing the other.
    """


@dataclass
class PositionState:
    """What the account CURRENTLY holds in one symbol, as the engine sees it.

    ``price`` is ``None`` when no live quote is available; the engine then SKIPS
    the symbol with a reason rather than sizing it at a guessed price (platform
    rule: no fallback values for live data).

    ``transaction_ids`` are the OPEN Transaction ids for the symbol, oldest
    first -- submission consumes them FIFO.

    ``unactionable_transaction_ids`` are the open Transaction ids the caller
    DELIBERATELY left out of ``transaction_ids``: real positions in this symbol
    that this planner will not act on. Live, that is the option-classed ones
    (``portfolio_allocation_service._open_transaction_ids`` keeps EQUITY only,
    because an option Transaction's ``symbol`` is the UNDERLYING ticker and the
    equity planner would otherwise read a covered call as a holding of the
    stock). They are carried rather than discarded because an EMPTY
    ``transaction_ids`` has two completely different meanings -- "we hold nothing
    of ours here" and "everything we hold here is invisible to this planner" --
    and the second one has to be able to speak. See ``decide_symbol_action``.
    """
    symbol: str
    quantity: float = 0.0
    cost_basis: float = 0.0
    price: Optional[float] = None
    market_value: Optional[float] = None
    transaction_ids: List[int] = field(default_factory=list)
    unactionable_transaction_ids: List[int] = field(default_factory=list)


#: ``Position.side`` spellings meaning "this is a SHORT". ``OrderDirection`` is a
#: str enum, so its ``.value`` lands here; TastyTrade's own 'Short' does too.
_SHORT_SIDES = frozenset({'sell', 'short'})
_LONG_SIDES = frozenset({'buy', 'long'})


def position_sign(side) -> Optional[int]:
    """``-1`` for a short, ``+1`` for a long, ``None`` when the side is unknown.

    ``None`` means "this row did not say", and the caller must then trust the
    signs the broker already put on the numbers rather than invent a direction.
    """
    text = str(getattr(side, 'value', side) or "").strip().lower()
    if text in _SHORT_SIDES:
        return -1
    if text in _LONG_SIDES:
        return 1
    return None


def signed_position_values(side, *, quantity, cost_basis,
                           market_value=None) -> Tuple[float, float, Optional[float]]:
    """One broker position row's ``(quantity, cost_basis, market_value)``, SIGNED.

    **Shorts have ONE canonical representation in this engine: signed negative.**
    A short's quantity, cost basis and market value are all negative, so a label's
    value is its NET exposure, ``compute_base_notional`` measures a hedge as
    something that REDUCES the allocatable base, and every sum is a plain sum.

    The two live brokers disagree at source and this is the single place that
    reconciles them: Alpaca passes the broker's own negative signs straight
    through (``AlpacaAccount.alpaca_position_to_position``) while TastyTrade
    stores ``qty=abs_qty`` with positive money and puts the direction in ``side``
    (``TastyTradeAccount.py:520-547``). It lives HERE, in the pure engine, because
    it has two callers that must never drift apart -- the page's
    ``ui/utils/portfolio_allocation_view.positions_by_symbol`` and the live
    service's ``core/portfolio_allocation_service.build_position_states``. They
    DID drift: only the first read ``side``, so on TastyTrade a short reached the
    allocation base as a long and inflated every label target on the account.

    Three properties, all deliberate and all tested:

    * **Idempotent.** ``-abs(...)``, never a bare negation, so an already-negative
      Alpaca short is left alone rather than flipped back into a long.
    * **Longs are NOT rewritten.** Only a short forces a sign, so no broker's own
      numbers are ever "corrected" on the strength of a metadata field.
    * **Unknown or absent side trusts the given signs.** ``position_sign``
      returning ``None`` means the row did not say; inventing a direction there
      would rewrite real numbers on no evidence.

    Args:
        side: the row's ``Position.side`` -- an ``OrderDirection``, a plain
            string, or ``None``. Read through ``position_sign``.
        quantity: the row's quantity, in whatever sign the broker used.
        cost_basis: likewise.
        market_value: likewise, and ``None`` is preserved as ``None`` -- "the
            broker stamped no market value" is a different fact from 0.0 and the
            page renders it differently.

    Returns:
        Tuple[float, float, Optional[float]]: the three values as floats, with a
        short's signs forced negative.
    """
    value = None if market_value is None else float(market_value)
    if position_sign(side) != -1:
        return float(quantity), float(cost_basis), value
    return (-abs(float(quantity)), -abs(float(cost_basis)),
            None if value is None else -abs(value))


@dataclass
class SymbolTarget:
    """A symbol's weight WITHIN one label. ``weight_pct`` is 1-100, not 0-1.

    ``previous_weight_pct`` is a PURE CARRIER for the wizard's per-label "Load
    last" button -- the weight this symbol ran with before the current one, from
    ``portfolio_allocation_symbol.previous_weight_pct``. No solver reads it, and no
    solver may: a button that changes a plan merely by being available is not a
    button. ``None`` means there is no last, which is a different fact from 0.0.
    """
    symbol: str
    weight_pct: float
    comment: Optional[str] = None
    previous_weight_pct: Optional[float] = None


@dataclass
class LabelTarget:
    """A managed label and its share of the base notional.

    ``target_pct`` is 1-100 and is a RELATIVE weight: across all managed labels of
    an account the targets must total EXACTLY 100 before a REBALANCE may be
    submitted, and what they divide is the INVESTABLE remainder
    (``base_notional`` less the account's stored ``unallocated_pct``), not the base
    itself. Deliberate cash lives in that reserve and never in a shortfall here, so
    raising or lowering it rewrites none of these numbers.

    An empty ``symbols`` list cannot absorb its percentage: the engine allocates it
    nothing and adds ``target_pct`` -- restated as a share of the BASE, since that
    field's denominator is the base and this one's is the remainder -- to
    ``AllocationPlan.unallocatable_pct`` instead. That is a fault, not a reserve.

    ``previous_target_pct`` is a PURE CARRIER for the wizard's "Load last" button,
    on the same terms as ``SymbolTarget.previous_weight_pct``: read by the UI,
    ignored by every solver, and ``None`` rather than 0.0 when there is no last.
    """
    label: str
    target_pct: float
    symbols: List[SymbolTarget] = field(default_factory=list)
    comment: Optional[str] = None
    previous_target_pct: Optional[float] = None


@dataclass
class AllocationRow:
    """One symbol's line in a plan: where it is, where it should be, the delta.

    ``delta_quantity`` is SIGNED (positive = buy, negative = sell); ``side`` is
    the matching ``OrderDirection`` (``None`` when the delta is exactly zero or
    the row was skipped). ``target_quantity`` is the POST-TRADE holding --
    ``current_quantity + delta_quantity``, what the account owns if this row
    executes -- and NOT an ideal share count the rounding may never reach; it is
    the same measure in both valuation modes, so it compares across rows.
    ``estimated_value``, ``bp_cost`` and ``bp_released`` are always POSITIVE
    magnitudes and the two buying-power fields are MUTUALLY EXCLUSIVE: a buy
    charges (``bp_cost``), a sell frees (``bp_released``), and neither is ever the
    other's negative. Read ``bp_effect`` when what is wanted is the SIGNED change.
    Sells never scale. ``fractional`` records the SIZING MODE: True when this row was rounded
    on the fractional grid (toggle on AND the broker calls the symbol
    fractionable), not merely when the resulting quantity has a decimal part.

    ``unmet_notional`` is why a row with no order is still worth displaying: it is
    the money the plan wanted to move for this symbol and could not.
    ``sizing_outcome`` and ``redistributed`` are why a row with an order is worth a
    second look: the quantity may be MORE than the weights asked for.
    """
    symbol: str
    labels: List[str] = field(default_factory=list)
    price: Optional[float] = None
    current_quantity: float = 0.0
    current_cost_basis: float = 0.0
    target_notional: float = 0.0
    target_quantity: float = 0.0
    delta_quantity: float = 0.0
    side: Optional[OrderDirection] = None
    estimated_value: float = 0.0
    bp_cost: float = 0.0
    #: Buying power this row FREES, as a POSITIVE magnitude. Non-zero on SELLS
    #: only, and the exact mirror of ``bp_cost`` on a buy of the same notional:
    #: ``estimated_value * bp_factor``. Any other rate would make buying a
    #: position and selling it back move the plan's budget by different amounts.
    #:
    #: It is CREDITED to the scaling budget (``_apply_bp_scaling``), which is the
    #: whole point: a rebalance that sells 2,112 to buy 1,187 is not short of
    #: buying power, and reporting one used to shrink every buy to a third of what
    #: the weights asked for.
    bp_released: float = 0.0
    bp_factor: float = 1.0
    #: Copied verbatim from this symbol's ``MarginInfo``, and CARRIED rather than
    #: re-derived because the dry run cannot otherwise tell a broker-stated margin
    #: rate from the conservative account-multiplier fallback -- which is the whole
    #: difference between a leverage flag and a red badge on every new position.
    #: The defaults are the "no MarginInfo at all" state, deliberately identical to
    #: ``MarginInfo``'s own: ``marginable`` optimistic, the rate UNPUBLISHED (never
    #: 1.0, which would be a fabricated broker fact) and the source the fallback.
    #: Never read ``bp_factor`` and ``initial_margin_rate`` as if either implied the
    #: other: ``bp_factor = initial_margin_rate x account_multiplier``, so 1.0 is the
    #: neutral point and a rate of 0.5 and a rate of 1.0 both reach it.
    marginable: bool = True
    initial_margin_rate: Optional[float] = None
    #: Which ``MARGIN_SOURCE_*`` the numbers above came from. ``MARGIN_SOURCE_DEFAULT``
    #: means the broker published nothing per-symbol and the adapter fell back to the
    #: account multiplier -- TastyTrade does exactly that for every symbol the account
    #: does not already hold, so this field is what stops a first-time buy being read
    #: as a leveraged one.
    margin_source: str = MARGIN_SOURCE_DEFAULT
    fractional: bool = False
    skipped: bool = False
    #: The broker precheck's own fee estimate, when one was run and accepted
    #: (``OrderImpact.estimated_fees``). ``None`` means "not prechecked", never
    #: "free" -- no fallback value for a number the broker did not supply.
    estimated_fees: Optional[float] = None
    #: Money this row INTENDED to move and could not: the tradeable grid, the broker
    #: minimum order size, the fractional notional floor, the buying-power scaling
    #: or a precheck rejection took it away. 0.0 on a row that traded -- including a
    #: BUMPED row, which moved MORE than asked, not less. Deliberately 0.0 on a
    #: NO-PRICE row too: that row's whole target is already reported through
    #: ``AllocationPlan.unallocatable_pct``, and counting it in both places would
    #: double the money the dry run shows as unallocated.
    unmet_notional: float = 0.0
    #: Which sizing rule produced this row: ``SIZING_OUTCOME_NORMAL``,
    #: ``SIZING_OUTCOME_BUMPED`` (deliberate over-allocation to one unit) or
    #: ``SIZING_OUTCOME_SKIPPED_TOO_LARGE`` (one unit would have overshot the bound).
    sizing_outcome: str = SIZING_OUTCOME_NORMAL
    #: True when ``redistribute_label_residuals`` moved this row off the quantity the
    #: user's weights implied, to keep its LABEL on target. The dry run must show it.
    redistributed: bool = False
    reasons: List[str] = field(default_factory=list)

    @property
    def is_buy(self) -> bool:
        return self.side == OrderDirection.BUY and not self.skipped

    @property
    def is_sell(self) -> bool:
        return self.side == OrderDirection.SELL and not self.skipped

    @property
    def bp_effect(self) -> float:
        """SIGNED change in buying power: NEGATIVE for a buy, POSITIVE for a sell.

        The sign convention is the broker's own -- TastyTrade's
        ``BuyingPowerEffect.change_in_buying_power`` is negative for a buy (see
        ``OrderImpact``) -- so the dry run's column and the broker's precheck read
        the same way round. 0.0 on a row with no order, in both directions.
        """
        return float(self.bp_released or 0.0) - float(self.bp_cost or 0.0)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict for ``portfolio_allocation_run.plan_json``."""
        return {
            "symbol": self.symbol,
            "labels": list(self.labels),
            "price": self.price,
            "current_quantity": self.current_quantity,
            "current_cost_basis": self.current_cost_basis,
            "target_notional": self.target_notional,
            "target_quantity": self.target_quantity,
            "delta_quantity": self.delta_quantity,
            "side": self.side.value if self.side is not None else None,
            "estimated_value": self.estimated_value,
            "bp_cost": self.bp_cost,
            "bp_released": self.bp_released,
            # Derived, but written out: a stored plan is read by things that have
            # no engine to re-derive it with, and getting the sign wrong there
            # turns a release into a charge.
            "bp_effect": self.bp_effect,
            "bp_factor": self.bp_factor,
            "marginable": self.marginable,
            "initial_margin_rate": self.initial_margin_rate,
            "margin_source": self.margin_source,
            "fractional": self.fractional,
            "skipped": self.skipped,
            "estimated_fees": self.estimated_fees,
            "unmet_notional": self.unmet_notional,
            "sizing_outcome": self.sizing_outcome,
            "redistributed": self.redistributed,
            "reasons": list(self.reasons),
        }


@dataclass
class AllocationPlan:
    """A full dry-run: one AllocationRow per symbol plus plan-level totals.

    ``scale_factor`` < 1.0 means every BUY was scaled down pro-rata because
    ``sum(bp_cost of buys) > total_buying_power``. Sells never scale.
    ``required_buying_power`` is the POST-scaling figure -- what the plan as
    displayed actually needs.

    THREE buying-power numbers, and mixing them up is what produced the defect
    this note exists for. ``available_buying_power`` is what the BROKER published
    before anything is sent; ``released_buying_power`` is what this plan's own
    SELLS give back; ``total_buying_power`` is their sum and is THE BUDGET -- what
    the scaler measures against and what ``bp_usage_pct`` divides. A live plan
    selling 2,112 to buy 1,187 measured itself against the 394 it started with,
    called itself 91% used and scaled every buy to a third.

    TWO kinds of money end up idle, and they are deliberately separate fields.
    ``unallocatable_pct`` is the share of the GROSS BASE that no label COULD absorb
    (empty labels, skipped no-price symbols) -- a fault, and something to fix.
    ``reserved_pct`` / ``reserved_notional`` is the STORED reserve the user asked
    for (``PortfolioAllocationConfig.unallocated_pct``). Both show as money left
    over on the dry run, and telling them apart is the difference between "you
    asked for 30% to stay out of the market" and "30% of your book had no price".

    Both divide ``base_notional``, and that is load-bearing rather than incidental.
    ``unallocatable_pct`` is accumulated from RELATIVE label weights -- which are
    shares of the investable remainder, not of the base -- and is restated against
    the base as it is accumulated (``effective_target_pct``). Left relative it read
    as a bigger number than it was: base 10,000, reserve 50%, labels 50 (held) / 50
    (empty) reported ``reserved_pct=50`` AND ``unallocatable_pct=50``, which sums to
    100 and says "the whole book is idle" on a plan that invests 2,500. They are two
    answers to one question and a reader will add them; the only way that is safe is
    for them to share a denominator with each other and with the money.

    A THIRD thing that is NOT either of them: ``scale_factor``. A buying-power
    shortfall is a constraint the plan HIT, not money set aside, and merging it
    with the reserve would tell a user to lower a reserve they never set.

    ``base_notional`` carries TWO meanings depending on which solver built the
    plan: in a REBALANCE it is the ALLOCATABLE BASE (buying power plus the current
    value of managed positions), and in an INVEST_LABEL run it is simply THE BUDGET
    being spent. One field, because both are "the money this plan is dividing", but
    a caller rendering it must know which run produced the plan. It is always the
    GROSS figure: what the labels actually divide is ``investable_notional``.

    ``valuation_mode`` records what "current value" MEANT for every number here --
    the base, the percentages and every delta (decision 5a). It is a user-flippable
    toggle, so a ``plan_json`` without it cannot be reproduced or even read
    correctly six months later.
    """
    rows: List[AllocationRow] = field(default_factory=list)
    base_notional: float = 0.0
    available_buying_power: float = 0.0
    #: What this plan's own SELLS free, summed from ``AllocationRow.bp_released``.
    #: Added to ``available_buying_power`` to make the budget the buys are sized
    #: against. Recomputed by ``filter_plan_rows``, so un-ticking the sell that
    #: funds a rebalance takes its money straight back out of the footer.
    released_buying_power: float = 0.0
    required_buying_power: float = 0.0
    bp_usage_pct: float = 0.0
    scale_factor: float = 1.0
    #: The share of ``base_notional`` no label COULD absorb -- an empty label, a
    #: symbol with no price. A FAULT, and the same denominator as ``reserved_pct``
    #: beneath it: accumulated from relative label weights and restated against the
    #: base through ``effective_target_pct`` as it goes in. See the class docstring
    #: for why the two must be addable.
    unallocatable_pct: float = 0.0
    #: The DELIBERATE cash reserve: the account's STORED ``unallocated_pct``, and
    #: the same share of ``base_notional`` in money. Kept SEPARATE from
    #: ``unallocatable_pct``, which means "no label could absorb this" -- a fault
    #: (an empty label, a no-price symbol). Merging them would leave the dry run
    #: unable to tell a cash target from a pricing failure, which are opposite
    #: problems with opposite fixes. Always 0.0 for an INVEST_LABEL plan, which
    #: deploys a specific amount the user named and has no portfolio-level base to
    #: take a share of.
    reserved_pct: float = 0.0
    reserved_notional: float = 0.0
    total_buy_value: float = 0.0
    total_sell_value: float = 0.0
    allow_fractional: bool = False
    valuation_mode: str = VALUATION_MODE_MARKET
    #: What every ``target_notional`` in ``rows`` MEANS -- ``ALLOCATION_BASIS_POSITION``
    #: for a REBALANCE (the desired post-trade holding value) or
    #: ``ALLOCATION_BASIS_BUDGET`` for an INVEST_LABEL run (money to deploy on top of
    #: what is held). Every "is this plan on target?" measurement reads it; without
    #: it the same arithmetic silently means two different things.
    allocation_basis: str = ALLOCATION_BASIS_POSITION
    #: The label targets this plan was SOLVED with, for ``plan_json`` only. A stored
    #: plan that cannot say which percentages produced it is not reproducible, and
    #: nothing else on the plan carries them (a symbol in two labels merges into one
    #: row, and a skipped no-price symbol's share vanishes into ``unallocatable_pct``).
    #: Emphatically NOT the source for "load last" -- that is the ``previous_*``
    #: columns, which survive a dry run the user cancelled.
    labels: List[LabelTarget] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def investable_notional(self) -> float:
        """The money the label targets actually divided: base MINUS the reserve.

        A derived PROPERTY, not a fourth stored money field. ``base_notional``,
        ``reserved_notional`` and this must always agree, and the only way to
        guarantee that is to keep two of them and subtract. It also means
        ``filter_plan_rows`` and ``apply_order_impacts`` get it right for free --
        they already carry both operands.
        """
        return self.base_notional - self.reserved_notional

    @property
    def total_buying_power(self) -> float:
        """THE BUDGET: what the broker published PLUS what this plan's sells free.

        A derived PROPERTY for the same reason ``investable_notional`` is one --
        the three figures must always agree and the only way to guarantee that is
        to keep two and add. Every "does this plan fit?" question divides this:
        ``_apply_bp_scaling``, ``bp_usage_pct``, the per-row ``BP %`` and the dry
        run's footer.
        """
        # No ``or 0.0``: both are non-Optional ``float`` fields, so the coercion
        # could never fire and ``tests/test_no_zero_coercion.py`` is a ratchet on
        # adding ones that look like it could. Same as ``investable_notional``.
        return float(self.available_buying_power) + float(self.released_buying_power)

    @property
    def buy_rows(self) -> List[AllocationRow]:
        """Buys, DESCENDING by estimated value -- the submission order (a
        shortfall then truncates the smallest positions)."""
        return sorted((r for r in self.rows if r.is_buy),
                      key=lambda r: r.estimated_value, reverse=True)

    @property
    def sell_rows(self) -> List[AllocationRow]:
        """Sells, descending by estimated value. Submitted BEFORE any buy."""
        return sorted((r for r in self.rows if r.is_sell),
                      key=lambda r: r.estimated_value, reverse=True)

    @property
    def net_buy_value(self) -> float:
        """``max(0, buys - sells)`` -- exactly what consumes the income ledger."""
        return max(0.0, self.total_buy_value - self.total_sell_value)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict for ``portfolio_allocation_run.plan_json``."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "base_notional": self.base_notional,
            "available_buying_power": self.available_buying_power,
            "released_buying_power": self.released_buying_power,
            # Derived, but written out, like ``investable_notional`` below: a
            # reader that adds the wrong pair reports the wrong budget.
            "total_buying_power": self.total_buying_power,
            "required_buying_power": self.required_buying_power,
            "bp_usage_pct": self.bp_usage_pct,
            "scale_factor": self.scale_factor,
            # Both of these divide ``base_notional``, so a reader may add them --
            # see the class docstring. Neither is a share of the investable
            # remainder, even though the label weights they are derived from are.
            "unallocatable_pct": self.unallocatable_pct,
            "reserved_pct": self.reserved_pct,
            "reserved_notional": self.reserved_notional,
            # Derived, but written out: a stored plan is read by things that have
            # no engine to re-derive it with, and a reader that subtracts the wrong
            # pair silently reports the wrong money.
            "investable_notional": self.investable_notional,
            "total_buy_value": self.total_buy_value,
            "total_sell_value": self.total_sell_value,
            "allow_fractional": self.allow_fractional,
            "valuation_mode": self.valuation_mode,
            "allocation_basis": self.allocation_basis,
            "labels": [{"label": lt.label, "target_pct": lt.target_pct,
                        "symbols": [{"symbol": st.symbol, "weight_pct": st.weight_pct}
                                    for st in lt.symbols]}
                       for lt in self.labels],
            "warnings": list(self.warnings),
        }


def current_value(state: Optional[PositionState], valuation_mode: str) -> float:
    """A position's CURRENT VALUE under the selected valuation mode (decision 5a).

    ``cost``   -> ``cost_basis`` (what you paid).
    ``market`` -> ``quantity * price`` (what it is worth now).

    A symbol with no position is 0.0 in both modes. In ``market`` mode a symbol
    with no price is 0.0 too -- the caller has already skipped it with
    ``REASON_NO_PRICE``, and inventing a value here would be exactly the
    guessed-price the platform forbids.

    ``PositionState.market_value`` -- the broker's OWN figure -- is deliberately
    not consulted: it can be stamped at a different price from ``price`` (a
    previous close, a delayed quote), and the allocatable base, the displayed
    percentages and every delta must all be measured with the SAME price or they
    disagree. This is the single definition all three go through.

    Raises:
        ValueError: on any other mode string. A typo would silently reinterpret
        every percentage on the page.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    if state is None:
        return 0.0
    if valuation_mode == VALUATION_MODE_MARKET:
        if state.price is None or state.price <= 0:
            return 0.0
        return float(state.quantity or 0.0) * float(state.price)
    return float(state.cost_basis or 0.0)


#: Drawn where an unrealised P&L would be when NOTHING is held. A dash, never a
#: 0.00: "there is no position" and "this position is exactly break-even" are
#: different facts and only one of them is worth a number.
PNL_UNMEASURABLE_MARK = '-'

#: Drawn when something IS held and not one lot of it could be priced. Names the
#: reason, because here the dash is a MISSING measurement rather than an absent
#: position -- a failed quote reading as "flat" or "break-even" is the incident
#: class this whole module keeps guarding against.
PNL_NO_PRICE_MARK = '- (no price)'

#: The percentage half of the P&L string, inside the parentheses.
PNL_PCT_FMT = '{pct:+.2f}%'

#: Replaces the percentage when the cost basis is zero. There is no return on
#: nothing, and a 0.00% or an inf there would both be inventions.
PNL_NO_COST_NOTE = 'no cost basis'

#: Appended inside the parentheses when the figure left holdings out. Same rule and
#: nearly the same words as the dry run's "Held value" total: unpriced rows are
#: EXCLUDED from the money and COUNTED in the sentence.
PNL_UNPRICED_FMT = '{count} unpriced excluded'

#: The whole thing: signed money, then the notes. The SIGN is what makes the column
#: readable without colour -- colour is an accent here, never the message.
PNL_FMT = '{amount:+,.2f} ({notes})'


@dataclass
class UnrealisedPnL:
    """Unrealised profit on one holding, or on a group of them. Pure data.

    ``amount`` is ``market_value - cost_basis`` -- SIGNED, and already correct for
    a short: a short is stored signed negative (quantity, cost basis and market
    value all), so a short that fell in price has a market value less negative than
    its basis and nets out positive.

    ``pct`` divides by ``abs_cost_basis`` -- ``sum(abs(cost))`` and NOT
    ``abs(sum(cost))``. Two independent reasons, and they point the same way:

    * a profitable short divided by its own signed basis renders as a LOSS; and
    * a hedged label's signed cost basis nets towards zero, so ``abs(sum(cost))``
      is a division by zero on a label that made real money, while the gross
      capital deployed is a perfectly ordinary number.

    For a single position the two are identical, so the per-symbol figure and the
    per-label figure are the same rule at two scopes rather than two rules.

    ``amount`` and ``pct`` are ``None``, never 0.0, when they cannot be measured:
    ``amount`` when nothing could be priced, ``pct`` additionally when the priced
    holdings cost nothing. A fabricated 0.00 in a money column is the failure mode
    that has actually cost this platform money.

    ``priced`` / ``unpriced`` count HOLDINGS, not symbols: a flat or absent symbol
    is in neither, so a healthy label does not permanently report exclusions for
    the managed symbols it happens not to own.
    """
    amount: Optional[float] = None
    pct: Optional[float] = None
    market_value: float = 0.0
    cost_basis: float = 0.0
    abs_cost_basis: float = 0.0
    priced: int = 0
    unpriced: int = 0


def unrealised_pnl(states) -> UnrealisedPnL:
    """Unrealised P&L over ``states``: money and percent. Pure; never raises.

    **Takes no ``valuation_mode``, deliberately, and must never grow one.** In
    COST valuation ``current_value`` IS the cost basis, so a P&L derived from the
    mode-aware "current value" is identically 0.00 -- a column that silently reads
    break-even for every position on the account's default mode. The true market
    value is the only input from which this question has an answer, so it is the
    only one taken.

    Market value is ``quantity x price`` from the LIVE quote, exactly as
    ``current_value(state, VALUATION_MODE_MARKET)`` defines it, and
    ``PositionState.market_value`` -- the broker's own stamped figure -- is
    deliberately not consulted even as a fallback. It can be stamped at a different
    price (a previous close, a delayed quote), and mixing the two bases inside one
    total is precisely why ``LabelView`` has no label-level market value any more.
    A holding whose price is missing is therefore EXCLUDED and counted, never
    valued at 0.

    Args:
        states: an iterable of ``Optional[PositionState]``. ``None`` entries and
            genuinely flat states (no quantity AND no cost) are skipped entirely.
            Pass one state for a symbol, or a label's whole membership for the
            label total -- the label figure is then the same summation, which is
            what makes it money-weighted rather than a mean of the symbols'
            percentages.

    Returns:
        UnrealisedPnL: over the PRICED holdings only, with the unpriced counted.
    """
    out = UnrealisedPnL()
    for state in (states or []):
        if state is None:
            continue
        quantity = float(state.quantity or 0.0)
        cost = float(state.cost_basis or 0.0)
        if abs(quantity) <= QUANTITY_EPSILON and abs(cost) <= MONEY_EPSILON:
            continue                      # flat: nothing owned, nothing paid
        price = state.price
        if price is None or float(price) <= 0:
            out.unpriced += 1
            continue
        out.priced += 1
        out.market_value += quantity * float(price)
        out.cost_basis += cost
        out.abs_cost_basis += abs(cost)

    if out.priced:
        out.amount = out.market_value - out.cost_basis
        if out.abs_cost_basis > MONEY_EPSILON:
            out.pct = out.amount / out.abs_cost_basis * 100.0
    return out


def format_unrealised_pnl(pnl: UnrealisedPnL) -> str:
    """Render an ``UnrealisedPnL`` for a caption. Pure.

    Lives beside the arithmetic rather than in the wizard because every branch here
    is a DECISION about what may be shown -- blank versus 0.00, a percentage versus
    "no cost basis" -- and those are exactly the ones worth pinning without NiceGUI.
    """
    if pnl.amount is None:
        return PNL_NO_PRICE_MARK if pnl.unpriced else PNL_UNMEASURABLE_MARK
    notes = [PNL_NO_COST_NOTE if pnl.pct is None else PNL_PCT_FMT.format(pct=pnl.pct)]
    if pnl.unpriced:
        notes.append(PNL_UNPRICED_FMT.format(count=pnl.unpriced))
    return PNL_FMT.format(amount=pnl.amount, notes=', '.join(notes))


def _round_shares(raw: float, margin: Optional[MarginInfo], *,
                  allow_fractional: bool) -> float:
    """Round a POSITIVE share count DOWN onto the broker's tradeable grid.

    The single definition of that grid: whole shares unless fractional trading is
    on AND the broker calls the symbol fractionable, then the published
    ``min_trade_increment`` or ``DEFAULT_FRACTIONAL_DECIMALS`` places. Everything
    that produces a quantity goes through here, so a target and a delta can never
    be rounded onto two different grids.
    """
    if raw is None or raw <= 0:
        return 0.0
    if allow_fractional and margin is not None and margin.fractionable:
        inc = margin.min_trade_increment
        if inc and inc > 0:
            qty = round(math.floor(round(raw / inc, 9)) * inc, 10)
        else:
            f = 10.0 ** DEFAULT_FRACTIONAL_DECIMALS
            qty = math.floor(raw * f) / f
    else:
        qty = float(math.floor(raw))
    return qty if qty > 0 else 0.0


def _round_delta_shares(delta: float, margin: Optional[MarginInfo], *,
                        allow_fractional: bool, current_quantity: float) -> float:
    """Round a SIGNED share delta onto the grid, preserving sign, never shorting.

    The DELTA is what the broker receives, so the delta is what has to sit on the
    increment -- an on-grid target minus an off-grid holding is off-grid.

    A sell is clamped to ``max(0, current_quantity)``: it can never exceed the
    holding, and against a SHORT position it is clamped to zero rather than to the
    negative quantity, so the engine can only ever buy a short back (targets are
    long-only).
    """
    magnitude = _round_shares(abs(float(delta or 0.0)), margin,
                              allow_fractional=allow_fractional)
    if delta >= 0:
        return magnitude
    return -min(magnitude, max(0.0, float(current_quantity or 0.0)))


def tradeable_unit(margin: Optional[MarginInfo], *, allow_fractional: bool) -> float:
    """The SMALLEST quantity this symbol can trade -- one step of the grid.

    1.0 on the whole-share grid; the broker's published ``min_trade_increment`` on
    the fractional one, or ``10 ** -DEFAULT_FRACTIONAL_DECIMALS`` when fractional
    trading is allowed but no step was published (``fractionable=True,
    min_trade_increment=None`` is a legal pair).

    Deliberately mirrors ``_round_shares``' branch exactly, including the tri-state
    read: only ``fractionable is True`` selects the fractional grid, so ``None``
    ("the broker did not say") sizes as whole shares -- the conservative direction,
    and the same one the rounding itself takes. Plan and execution must never round
    on two different grids.

    This is the QUANTITY grid only. It says nothing about whether an order of that
    size is ACCEPTABLE: ``min_order_size`` (shares) and ``min_fractional_notional``
    (dollars) are separate thresholds, weighed together in ``size_sub_unit_target``.
    """
    if allow_fractional and margin is not None and margin.fractionable is True:
        inc = margin.min_trade_increment
        if inc and inc > 0:
            return float(inc)
        return 10.0 ** -DEFAULT_FRACTIONAL_DECIMALS
    return 1.0


def size_sub_unit_target(target_notional: float, price: float,
                         margin: Optional[MarginInfo], *,
                         allow_fractional: bool) -> Tuple[float, str, str]:
    """Decide what to do with a WHOLE intended allocation the grid cannot send.

    Decision D1: bump it UP to the smallest SENDABLE quantity so the symbol
    actually gets a position -- but only when that quantity costs at most
    ``BUMP_TO_ONE_SHARE_MAX_MULTIPLE`` times the target. A bump is an intentional
    over-allocation, it takes buying power promised to the rest of the plan, and
    past the bound it stops being a rounding fix and becomes a different trade.

    THREE thresholds are weighed TOGETHER, never in sequence (live finding L8b):

      * the quantity grid (``tradeable_unit``);
      * the broker's fractional NOTIONAL floor (``min_fractional_notional``,
        DOLLARS). Under it no FRACTION can be sent at all, and a WHOLE share is
        exempt from the rule, so the bump destination escalates to one whole
        share and the reason says the floor is why -- not "rounds to zero", which
        would be false: the quantity was legal, the money was not;
      * the broker's minimum ORDER size (``min_order_size``, SHARES). One unit
        under it means there is no order to place at any size, which is neither a
        bump nor a bound problem.

    Call this ONLY for the case where the sub-unit amount IS the symbol's whole
    intended position (a flat holding being opened, or a symbol's share of an
    INVEST_LABEL budget). A sub-unit ADJUSTMENT to a position that already exists
    is left alone with ``REASON_ROUNDS_TO_ZERO_FMT``: the position exists, and
    bumping a -0.4 trim into a whole-share sale is a trade nobody requested.

    Returns:
        Tuple[float, str, str]: ``(quantity, sizing_outcome, reason)``.
        ``(unit, SIZING_OUTCOME_BUMPED, ...)`` when the bump is taken;
        ``(0.0, SIZING_OUTCOME_SKIPPED_TOO_LARGE, ...)`` when the smallest
        sendable order overshoots the bound; ``(0.0, SIZING_OUTCOME_NORMAL, ...)``
        when one unit is under the broker's minimum ORDER size, which is neither a
        bump nor a bound problem -- there is simply no order to place at any size.

    Raises:
        ValueError: on a non-positive price or target. Both callers have already
        skipped a no-price row and clamped a negative target; there is no fallback
        price or fallback target in this platform.
    """
    if price is None or float(price) <= 0:
        raise ValueError(f"size_sub_unit_target: price must be positive, got {price!r}")
    if target_notional is None or float(target_notional) <= 0:
        raise ValueError(
            f"size_sub_unit_target: target_notional must be positive, got {target_notional!r}")
    px = float(price)
    target = float(target_notional)
    raw = target / px
    unit = tradeable_unit(margin, allow_fractional=allow_fractional)

    # The MONEY floor, weighed here rather than left to _suppress_below_min_order:
    # by the time that runs the row is already zero and there is nothing to decide.
    floor_usd = None if margin is None else margin.min_fractional_notional
    hit_floor = False
    if (floor_usd is not None and _is_fractional_quantity(unit)
            and unit * px < float(floor_usd)):
        # No fraction THIS SMALL can be sent -- but the rule is a minimum NOTIONAL,
        # not a ban on fractions, so a BIGGER fraction clears it. Two legal
        # destinations: the smallest multiple of the grid worth ``floor_usd``, and
        # one WHOLE share (exempt from the rule). Take the cheaper of the two.
        # Escalating straight to a whole share throws the symbol away whenever the
        # share is the dearer one: a $4 slice of a $34 ETF clears the floor at
        # 0.14706 shares (125% of target, inside the bump bound) while one share is
        # 850% and gets refused, so the user ends up with no order at all.
        # ``ceil`` without a tolerance on purpose: rounding UP by one increment
        # costs a fraction of a cent, rounding down puts the order under the floor.
        clearing = round(math.ceil(float(floor_usd) / (px * unit)) * unit, 10)
        unit = clearing if 0 < clearing < 1.0 else 1.0
        hit_floor = True

    unit_notional = unit * px
    pct = unit_notional / target * 100.0
    limit_pct = BUMP_TO_ONE_SHARE_MAX_MULTIPLE * 100.0
    # INCLUSIVE bound: exactly 200% bumps, which is the ``raw == 0.5`` case and is what
    # makes this comparison round-half-up. MONEY_EPSILON absorbs the float noise of a
    # target computed through two percentage multiplications.
    if unit_notional > target * BUMP_TO_ONE_SHARE_MAX_MULTIPLE + MONEY_EPSILON:
        if hit_floor:
            return 0.0, SIZING_OUTCOME_SKIPPED_TOO_LARGE, (
                REASON_FRACTIONAL_FLOOR_SKIPPED_FMT.format(
                    target=target, minimum=float(floor_usd), unit=unit, price=px,
                    pct=pct, limit=limit_pct))
        return 0.0, SIZING_OUTCOME_SKIPPED_TOO_LARGE, REASON_BELOW_ONE_SHARE_FMT.format(
            target=target, raw=raw, price=px, unit=unit, pct=pct, limit=limit_pct)
    min_size = None if margin is None else margin.min_order_size
    if min_size is not None and unit < float(min_size):
        # The broker will not accept an order this small, so there is nothing to
        # bump TO. Reported as NORMAL: the bound had nothing to do with it.
        return 0.0, SIZING_OUTCOME_NORMAL, REASON_BUMP_BLOCKED_MIN_ORDER_FMT.format(
            target=target, raw=raw, price=px, size=float(min_size))
    if hit_floor:
        return unit, SIZING_OUTCOME_BUMPED, REASON_FRACTIONAL_FLOOR_BUMPED_FMT.format(
            target=target, minimum=float(floor_usd), unit=unit, price=px, pct=pct)
    return unit, SIZING_OUTCOME_BUMPED, REASON_BUMPED_TO_ONE_SHARE_FMT.format(
        target=target, raw=raw, price=px, unit=unit, pct=pct)


def _below_fractional_notional_floor(delta: float, margin: Optional[MarginInfo],
                                     price: Optional[float]) -> bool:
    """True when ``_suppress_below_min_order`` would zero this delta on the $5 rule.

    A non-mutating twin of that check, so the D1 sizing decision can run BEFORE the
    suppression instead of finding a row it has already zeroed (live finding L8b).
    The two must agree exactly, which is why both read the same three fields.
    """
    if not delta or margin is None or margin.min_fractional_notional is None:
        return False
    if price is None or float(price) <= 0:
        return False
    if not _is_fractional_quantity(delta):
        return False
    return abs(delta) * float(price) < float(margin.min_fractional_notional)


def round_delta_quantity(delta_notional: float, unit_value: float,
                         margin: Optional[MarginInfo], *, allow_fractional: bool,
                         current_quantity: float,
                         apply_min_order_size: bool = False) -> float:
    """Turn a SIGNED money delta into a SIGNED, tradeable share delta.

    ``unit_value`` is THE MONEY ONE SHARE MOVES, and it is NOT always the price.
    Used by ``cost`` valuation mode, where the gap being closed is a COST BASIS
    gap: buying a share adds ``price`` to the basis, but selling one removes the
    AVERAGE COST (``cost_basis / quantity``), so the two legs convert with
    different divisors. Passing the market price for a sell is the bug that made a
    50%-down position liquidate instead of half-trim -- see ``compute_allocation``.

    The magnitude is rounded DOWN onto the broker's grid and a sell is clamped to
    the holding (never oversell, never short).

    ``apply_min_order_size`` defaults to FALSE. A minimum ORDER size must be
    checked on the final signed delta, after the clamp, by the caller -- which
    also gets to attach ``REASON_BELOW_MIN_ORDER_FMT`` instead of silently
    returning zero. The keyword remains for a standalone caller that wants the
    check inline.
    """
    if unit_value is None or unit_value <= 0:
        return 0.0
    delta = _round_delta_shares(float(delta_notional or 0.0) / float(unit_value),
                                margin, allow_fractional=allow_fractional,
                                current_quantity=current_quantity)
    if (apply_min_order_size and delta and margin is not None
            and margin.min_order_size is not None
            and abs(delta) < float(margin.min_order_size)):
        return 0.0
    return delta


def _is_fractional_quantity(quantity: float) -> bool:
    """True when this share count is NOT a whole number of shares.

    Tested against ``QUANTITY_EPSILON`` from BOTH sides, because a quantity that
    came off a fractional grid can land at 2.9999999999 as easily as at
    3.0000000001, and calling either of those "fractional" would apply a
    fractional-only broker rule to what is really a 3-share order.
    """
    part = abs(float(quantity)) % 1.0
    return min(part, 1.0 - part) > QUANTITY_EPSILON


def _suppress_below_min_order(delta: float, margin: Optional[MarginInfo],
                              reasons: List[str],
                              price: Optional[float] = None) -> float:
    """Zero a signed share delta the broker would refuse as too small, with a reason.

    TWO independent minimums, in DIFFERENT UNITS, and conflating them is the bug
    this signature exists to prevent:

      * ``margin.min_order_size`` -- a SHARE count;
      * ``margin.min_fractional_notional`` -- DOLLARS, and only for a FRACTIONAL
        quantity. TastyTrade returns HTTP 422 ``below_notional_value_minimum``
        ("Fractional equities orders cannot have a notional value less than $5.")
        Needs ``price``; with no price the check is skipped rather than guessed at.

    The fractional floor is applied to the QUANTITY, not to the symbol's
    ``fractionable`` flag: the broker's rule is worded "Fractional equities
    orders ...", so a whole-share order is exempt even in a splittable symbol, and
    a legal 1-share buy of a $3 stock must not be refused.

    Both minimums constrain the ORDER, not the TARGET: when the trade cannot be
    sent, the right answer is to LEAVE THE POSITION WHERE IT IS, not to rewrite
    what we want to hold. Filtering the target instead used to turn "hold the
    3.3333 shares you already have" into a full liquidation.

    Suppression, deliberately, rather than flooring the quantity to whole shares.
    Flooring would sometimes salvage the order (2.4 shares at $2 is $4.80, but 2
    shares is a legal $4.00 whole-share order), and it is the right answer -- but
    it is a SIZING policy, bounded by an overshoot guard, and it belongs to the
    bump-to-one-share task that owns that guard. Suppressing here never sends an
    order the broker would refuse and never overshoots a target; it only
    under-trades, and it says so.

    Tests the MAGNITUDE, so an unsendable trim is suppressed exactly like an
    unsendable top-up. Appends to ``reasons`` in place -- a silently absent order
    is its own kind of wrong. Returns the delta to use.
    """
    if not delta or margin is None:
        return delta
    if (margin.min_order_size is not None
            and abs(delta) < float(margin.min_order_size)):
        reasons.append(REASON_BELOW_MIN_ORDER_FMT.format(size=margin.min_order_size))
        return 0.0
    if (margin.min_fractional_notional is not None and price is not None
            and price > 0 and _is_fractional_quantity(delta)):
        value = abs(delta) * float(price)
        if value < float(margin.min_fractional_notional):
            reasons.append(REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_FMT.format(
                value=value, minimum=margin.min_fractional_notional))
            return 0.0
    return delta


def split_pct_across(total_pct: float, count: int) -> List[float]:
    """Split ``total_pct`` evenly across ``count`` slots, exact to 2dp.

    Every slot gets the total FLOORED to the cent, and the residual lands on the
    LAST one, so the parts sum to ``total_pct`` exactly in decimal
    (``split_pct_across(40, 3) == [13.33, 13.33, 13.34]`` -- not 13.33 three times,
    which loses a cent, and not 13.34 three times, which invents two). Returns
    ``[]`` for ``count <= 0``: nothing to split across is an empty list, not a
    ZeroDivisionError.

    This is the ONE splitter. ``even_split_pct`` is this at a total of 100, and
    "fill what is left of the 100 across the empty slots" is this at a total of the
    remainder -- deliberately the same code rather than the same idea written
    twice, because a second two-decimal split is exactly the thing that drifts. A
    hand-rolled ``round(total / n, 2)`` agrees here at n=2, 3 and 5 and parts
    company at n=6, where it produces a set ``validate_symbol_weights`` refuses.

    Binary float addition of the returned parts can still drift by ~1e-14; the
    guarantee is about CENTS, and ``LABEL_TOTAL_TOLERANCE_PCT`` sits twelve orders
    of magnitude above that.
    """
    if count <= 0:
        return []
    each = math.floor(total_pct / count * 100.0) / 100.0
    out = [each] * count
    out[-1] = round(total_pct - each * (count - 1), 2)
    return out


def scale_pct_to_total(values, total_pct: float = 100.0) -> List[float]:
    """Scale a set of weights PROPORTIONALLY so they sum to ``total_pct``, exact to 2dp.

    ``split_pct_across``'s sibling, and deliberately its sibling rather than its
    rival: both floor every part to the cent and hand the WHOLE residual to ONE
    slot, so the parts sum to the target exactly in decimal. What differs is the
    shares -- even there, proportional here -- and for equal inputs the two return
    the identical list at every count. That equivalence is pinned by a test, and it
    is the reason this lives beside the splitter instead of being a fourth
    two-decimal rounding rule written in the UI layer.

    The residual goes to the LARGEST part, not to the last one. That is the single
    deliberate divergence, and it is about zeros: a slot at 0 is a symbol the user
    asked to hold NONE of, and the splitter's last-slot rule would hand it a cent
    whenever it happened to sit last -- turning "sell this out" into a position. A
    tie goes to the LAST of the tied slots, which is what makes the all-equal case
    reproduce ``split_pct_across`` exactly.

    Args:
        values: the current weights, in display order. ``None`` reads as 0.0.
        total_pct: what they must add up to afterwards. 100 for a symbol set.

    Returns:
        List[float]: one part per input, in the same order, summing to
        ``total_pct`` exactly in decimal. ``[]`` for an empty input.

        An ALL-ZERO input has no proportions to preserve, so it falls back to
        ``split_pct_across`` -- the even split -- rather than raising or returning
        zeros. That is what makes "every symbol is empty" reach the same numbers
        whichever of the two helpers a caller routes it through.
    """
    parts = [float(v or 0.0) for v in (values or [])]
    if not parts:
        return []
    total = sum(parts)
    if total <= 0.0:
        return split_pct_across(total_pct, len(parts))
    out = [math.floor(p / total * total_pct * 100.0) / 100.0 for p in parts]
    # max() returns the FIRST maximum; the residual belongs on the last of the tied
    # slots so that an all-equal input lands on ``split_pct_across``'s answer.
    best = max(out)
    index = len(out) - 1 - out[::-1].index(best)
    out[index] = round(total_pct - sum(out[:index]) - sum(out[index + 1:]), 2)
    return out


def even_split_pct(count: int) -> List[float]:
    """Split 100% evenly across ``count`` slots, exact to 2dp.

    The remainder lands on the LAST slot so the list always totals exactly 100.0
    (``even_split_pct(3) == [33.33, 33.33, 33.34]``). Returns ``[]`` for
    ``count <= 0`` -- an empty label gets nothing, not a ZeroDivisionError.

    A named alias for ``split_pct_across(100.0, count)``, and kept named because
    "the even split" is what the two buttons and the stored default all mean.
    """
    return split_pct_across(100.0, count)


def build_symbol_targets(symbols: List[str],
                         stored_weights: Optional[Dict[str, float]] = None) -> List[SymbolTarget]:
    """Resolve a label's symbol weights, filling in the even-split default.

    ``stored_weights`` is ``{symbol: weight_pct}`` from
    ``portfolio_allocation_symbol`` -- absent symbols are NOT an error, they take
    the even-split default (rows are created lazily by design).

    If ANY weight is stored for the label, the un-stored symbols share what is
    left of 100% evenly; if none are, every symbol gets ``even_split_pct``.
    Order of ``symbols`` is preserved.
    """
    syms = list(symbols or [])
    if not syms:
        return []
    stored = stored_weights or {}
    known = {s: float(stored[s]) for s in syms if s in stored}
    if not known:
        return [SymbolTarget(symbol=s, weight_pct=p)
                for s, p in zip(syms, even_split_pct(len(syms)))]
    unknown = [s for s in syms if s not in known]
    weights = dict(known)
    if unknown:
        remaining = max(0.0, 100.0 - sum(known.values()))
        for s, p in zip(unknown, even_split_pct(len(unknown))):
            weights[s] = round(remaining * p / 100.0, 4)
    return [SymbolTarget(symbol=s, weight_pct=weights[s]) for s in syms]


def validate_symbol_weights(label: LabelTarget, *,
                            tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Validate ONE label's symbol weights. Pure -- returns problems, never raises.

    Checks, in this order: weights total 100 +/- ``tolerance`` (0.01 PERCENTAGE
    POINTS by default); no negative weight; no symbol repeated within the label.

    The label's OWN ``target_pct`` is deliberately NOT checked, which is why this
    is separate from ``validate_label_targets``: an INVEST_LABEL run picks a
    SINGLE label and spends an explicit amount on it, so that label's percentage
    is meaningless and the labels-total-100 rule would fire spuriously.

    This is the INVEST_LABEL submit gate. Without the OVER half
    (``ERROR_SYMBOL_OVER_FMT``) ``compute_label_investment`` multiplies whatever
    weights it is handed straight through, so a hand-edited 150% set turns a
    10,000 budget into 15,000 of buys.

    OVER 100 blocks (``ERROR_SYMBOL_OVER_FMT``); UNDER 100 is ADVISORY
    (``WARNING_SYMBOL_UNDER_FMT``, 2026-09-05) -- a 60% set leaves 40% of the
    label's own money undeployed, which is a fact worth showing and not a reason
    to refuse Submit; see ``WARNING_SYMBOL_UNDER_FMT``'s own docstring for why
    that is a different risk from the cross-label shortfall
    ``validate_label_targets`` still blocks.

    A label with NO symbols returns no errors -- it has no weights to be wrong.
    Whether an empty label may be invested into is the caller's decision.

    Returns:
        List[str]: ``ERROR_SYMBOL_*`` / ``WARNING_SYMBOL_UNDER_FMT`` strings
        naming the offending label and symbol, ready to show verbatim; EMPTY
        means valid. Not all of them BLOCK -- pass through ``blocking_messages``
        before refusing Submit on the strength of one. ``validate_label_targets``
        calls this for its per-label symbol checks, so the two can never drift.
    """
    errors = []
    if not label.symbols:
        return errors
    weight_total = sum(float(st.weight_pct or 0.0) for st in label.symbols)
    over = weight_total - 100.0
    if over > tolerance:
        errors.append(ERROR_SYMBOL_OVER_FMT.format(label=label.label, total=weight_total,
                                                    over=over))
    elif -over > tolerance:
        errors.append(WARNING_SYMBOL_UNDER_FMT.format(label=label.label, total=weight_total,
                                                       under=-over))
    seen_symbols = set()
    for st in label.symbols:
        weight = float(st.weight_pct or 0.0)
        if st.symbol in seen_symbols:
            errors.append(ERROR_SYMBOL_DUPLICATE_FMT.format(label=label.label,
                                                            symbol=st.symbol))
        seen_symbols.add(st.symbol)
        if weight < 0:
            errors.append(ERROR_SYMBOL_NEGATIVE_FMT.format(label=label.label,
                                                           symbol=st.symbol, pct=weight))
    return errors


def validate_label_targets(labels: List[LabelTarget], *,
                           tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Validate a REBALANCE label set. Pure -- returns problems, never raises.

    LABEL level: targets total EXACTLY 100 +/- ``tolerance`` (0.01 PERCENTAGE
    POINTS by default, so 99.995 passes and 99.98 does not); no negative
    ``target_pct``; no duplicate label names; every non-zero label has at least one
    symbol.

    BOTH SIDES ARE ERRORS, and the under case is a deliberate reversal of the
    design that briefly made a shortfall the cash reserve. The reserve now lives in
    ``unallocated_pct`` (see ``investable_notional``), which is the single
    unambiguous way to hold cash; leaving the shortfall legal as well would make
    "labels sum to 90" mean either "hold 10% in cash" or "you mistyped a box",
    which produce identical numbers and only the user can tell apart.

    The label targets are therefore RELATIVE weights: they say how to divide
    whatever the reserve leaves investable, and they do not change when the reserve
    does. That is the point -- the user never does the arithmetic.

    SYMBOL weights inside a label do NOT follow exactly the same rule (changed
    2026-09-05). A label whose weights total 60 leaves 40% of THAT label's money
    undeployed -- unlike the cross-label shortfall above, that is scoped to one
    label's own money, not the whole account, and it is now ADVISORY rather than
    blocking; see ``WARNING_SYMBOL_UNDER_FMT``. Over 100% still blocks: it
    deploys MORE than the label's target, which ``compute_allocation`` multiplies
    straight through with no cap.

    SYMBOL level, per label that HAS symbols: delegated in full to
    ``validate_symbol_weights`` (weights total 100 +/- the same ``tolerance``; no
    negative weight; no symbol repeated within the label) so that the REBALANCE
    gate here and the INVEST_LABEL gate there can never disagree. The same symbol
    appearing in DIFFERENT labels is legal and its targets sum (decision 7) --
    only a repeat inside one label is an error. Without the OVER check a
    hand-edited weight set totalling 150% would silently over-deploy its label,
    since ``compute_allocation`` multiplies the weights straight through.

    A label with no symbols is skipped here (an empty label at 0% stays valid; a
    non-zero one is already reported by ``ERROR_LABEL_NO_SYMBOLS_FMT``).

    Note that ``tolerance`` rejects a naive 2dp split (``3 x 33.33 == 99.99``);
    build defaults with ``even_split_pct`` and both levels pass by construction.

    Returns:
        List[str]: human-readable error/warning strings built from the
        ``ERROR_LABEL_*``, ``ERROR_SYMBOL_*`` and ``WARNING_SYMBOL_UNDER_FMT``
        formats, each naming the offending label (and symbol) so the UI can show
        it verbatim; EMPTY means valid. NOT every entry blocks Submit any more --
        pass the result through ``blocking_messages`` before refusing on the
        strength of it (decision 3 still requires that call to be non-empty).
    """
    errors = []
    total = sum(float(lt.target_pct or 0.0) for lt in labels or [])
    if total > 100.0 + tolerance:
        errors.append(ERROR_LABEL_TOTAL_FMT.format(total=total, over=total - 100.0))
    elif total < 100.0 - tolerance:
        errors.append(ERROR_LABEL_UNDER_FMT.format(total=total, under=100.0 - total))
    seen = set()
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        if lt.label in seen:
            errors.append(ERROR_LABEL_DUPLICATE_FMT.format(label=lt.label))
        seen.add(lt.label)
        if pct < 0:
            errors.append(ERROR_LABEL_NEGATIVE_FMT.format(label=lt.label, pct=pct))
        if pct > 0 and not lt.symbols:
            errors.append(ERROR_LABEL_NO_SYMBOLS_FMT.format(label=lt.label, pct=pct))
        errors.extend(validate_symbol_weights(lt, tolerance=tolerance))
    return errors


def validate_unallocated_pct(unallocated_pct: float) -> List[str]:
    """Validate the stored cash reserve. Pure -- returns problems, never raises.

    NO TOLERANCE, unlike the label totals: those are a SUM of several boxes and
    2dp rounding can legitimately land a hair off 100, while this is one number the
    user typed. 100.005 here is simply out of range -- and the message says
    "100.005", not a "100.00" rounded back into the range it is being refused for.

    Both bounds BLOCK, and the reason is NOT that the engine would raise --
    ``compute_allocation`` clamps (``clamp_unallocated_pct``) and solves happily.
    That is the problem. A -20 clamps to 0 and deploys the entire book; a 120
    clamps to 100 and liquidates it. Either way the plan the user reviews is not
    the plan they described, the number they typed is nowhere on screen, and
    nothing says a substitution happened. The clamp is a floor under the
    arithmetic; this is the thing that keeps the user's intent and the plan the
    same object.

    Returns:
        List[str]: EMPTY means the reserve is usable.
    """
    pct = float(unallocated_pct or 0.0)
    if pct < 0.0 or pct > 100.0:
        return [ERROR_UNALLOCATED_RANGE_FMT.format(pct=pct)]
    return []


def clamp_unallocated_pct(unallocated_pct: float) -> float:
    """The reserve, forced into 0-100. Pure.

    A DEFENCE, not the rule: ``validate_unallocated_pct`` refuses an out-of-range
    reserve before a plan is ever solved, and the wizard cannot submit while it
    does. This exists so the arithmetic below cannot produce a monster if one gets
    through anyway -- a negative reserve would inflate the investable base into
    money the account does not have, which is the exact class of accident the
    no-fallback rule is about.
    """
    return min(100.0, max(0.0, float(unallocated_pct or 0.0)))


def investable_notional(base_notional: float, unallocated_pct: float) -> float:
    """The share of the base the labels actually divide. Pure.

    THE SINGLE SCALING POINT for the reserve, and the only place this formula is
    written. ``compute_allocation`` calls it ONCE per plan and multiplies every
    label target by the result; the UI calls it to show the same money the engine
    used. Scaling per target instead would invite drift and would make the
    percentages on screen disagree with the ones the plan was solved with.

    The base itself stays GROSS everywhere -- ``AllocationPlan.base_notional``,
    ``BaseSnapshot.base_notional``, ``LabelView.pct_of_base``. Netting it at source
    would give ``base_notional`` two meanings, and would make a fully invested book
    read as 111% of a 90% base.
    """
    return float(base_notional or 0.0) * (100.0 - clamp_unallocated_pct(unallocated_pct)) / 100.0


def reserved_notional_for(base_notional: float, unallocated_pct: float) -> float:
    """The money the reserve holds back, in dollars. Pure.

    Defined as "what ``investable_notional`` left behind" rather than as its own
    multiplication, so the two can never disagree: reserved + investable IS the
    base, exactly, at every reserve.
    """
    return float(base_notional or 0.0) - investable_notional(base_notional, unallocated_pct)


def effective_target_pct(target_pct: float, unallocated_pct: float) -> float:
    """A label's RELATIVE weight restated as a share of the GROSS base. Pure.

    The one conversion between the two percentages this feature carries, and the
    reason it is a named function rather than an inline multiplication in two UI
    modules: they are DIFFERENT NUMBERS printed with the same '%' sign, and the
    screens that show them side by side have to agree on the arithmetic.

    A 50% label under a 10% reserve targets 45% of the base -- it divides what the
    reserve left, not the base. Both the wizard's per-label caption and the page's
    label header print the current holding as a share of the GROSS base (so that
    the column adds to 100 alongside the reserve row), so a bare "target 50%" next
    to "50.00% of base" reads as on-target on a row the plan will trim by a tenth.

    Clamped through ``clamp_unallocated_pct`` for the same reason
    ``investable_notional`` is: this is drawn live, per keystroke, from a box the
    validator has not necessarily accepted yet, and a negative reserve would
    otherwise print a target ABOVE the weight the user typed.
    """
    return (float(target_pct or 0.0)
            * (100.0 - clamp_unallocated_pct(unallocated_pct)) / 100.0)


def compute_base_notional(available_buying_power: float,
                          current: Dict[str, PositionState],
                          managed_symbols: List[str],
                          *, valuation_mode: str) -> float:
    """Allocatable base = broker buying power + current value of MANAGED positions.

    Decision 1 of the design; ``valuation_mode`` (decision 5a) selects whether
    "current value" is the cost basis or ``qty x price``. Unmanaged positions are
    deliberately excluded: they are invisible to the page and already reduce
    ``available_buying_power`` naturally. Symbols in ``managed_symbols`` with no
    ``current`` entry contribute 0; a repeated symbol is counted once.

    ``valuation_mode`` is REQUIRED and has NO Python default -- see the note on
    ``compute_allocation``. Pass the account's configured mode.

    Raises:
        ValueError: if ``available_buying_power`` is None (no fallback for
        balances), or if ``valuation_mode`` is unknown.
        TypeError: if ``valuation_mode`` is omitted.
    """
    if available_buying_power is None:
        raise ValueError("compute_base_notional: available_buying_power is None")
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        # Explicit, so a typo still raises on an account holding NOTHING managed
        # (the loop below would otherwise never reach current_value's own check).
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    base = float(available_buying_power)
    for sym in dict.fromkeys(managed_symbols or []):
        base += current_value((current or {}).get(sym), valuation_mode)
    return base


def round_quantity(target_notional: float, price: float, margin: Optional[MarginInfo],
                   *, allow_fractional: bool, apply_min_order_size: bool = True) -> float:
    """Convert a notional to a tradeable share quantity.

    Fractional OFF: ``floor(target_notional / price)``.
    Fractional ON: rounded DOWN to ``margin.min_trade_increment`` when the broker
    publishes one, otherwise to ``DEFAULT_FRACTIONAL_DECIMALS`` (4) places.
    Fractional ON but ``margin`` is None or ``margin.fractionable`` is False:
    falls back to whole shares.

    Always rounds DOWN, so a plan never over-spends its notional. Returns 0.0
    when ``price <= 0``; the caller must have already skipped a ``None`` price.

    A result below ``margin.min_order_size`` is returned as 0.0 -- correct when
    the value being rounded is an ORDER, WRONG when it is a TARGET holding, since
    a minimum order size says nothing about what you may hold. Pass
    ``apply_min_order_size=False`` when sizing a target and filter the resulting
    DELTA instead (``_suppress_below_min_order``).
    """
    if price is None or price <= 0:
        return 0.0
    if target_notional is None or target_notional <= 0:
        return 0.0
    qty = _round_shares(float(target_notional) / float(price), margin,
                        allow_fractional=allow_fractional)
    if qty <= 0:
        return 0.0
    if (apply_min_order_size and margin is not None
            and margin.min_order_size is not None and qty < margin.min_order_size):
        return 0.0
    return qty


def _reconcile_sizing_outcomes(rows: List[AllocationRow]) -> None:
    """Make every ``sizing_outcome`` describe the row's FINAL state.

    THE ONE DOOR, and it is at the END of the solve on purpose. ``sizing_outcome``
    is stamped by the sizing rules and then several later steps can take the order
    away again -- the buying-power scaler, a broker precheck refusal -- and neither
    of them was revisiting the stamp. A live row rendered ``SIDE -``, ``QTY 0``,
    ``ORDER no order`` and ``OUTCOME bumped-to-1`` at the same time, and the notice
    above the table dutifully reported that it "over-allocates them by 0.00".

    ``SIZING_OUTCOME_BUMPED`` is the only outcome that makes a claim a later step
    can falsify: it says the row deliberately holds MORE than its weight asked for.
    A row with no order holds nothing, so it becomes
    ``SIZING_OUTCOME_BUMPED_DROPPED`` -- which keeps both halves of the story (the
    position was wanted; it could not be had) without pretending an order exists.
    ``normal`` claims nothing and ``skipped-too-large`` already means "no order",
    so neither is touched.

    Put here rather than inside ``_apply_bp_scaling`` because the scaler is only
    one of the paths: ``apply_order_impacts`` zeroes a refused row without going
    near it, and a future step that zeroes a row will not have to remember either.
    """
    for row in rows:
        # ``delta_quantity`` is a non-Optional float; no ``or 0.0`` (see
        # ``tests/test_no_zero_coercion.py`` -- the rule is a ratchet).
        if (row.sizing_outcome == SIZING_OUTCOME_BUMPED
                and abs(float(row.delta_quantity)) <= QUANTITY_EPSILON):
            row.sizing_outcome = SIZING_OUTCOME_BUMPED_DROPPED


def _finalise_totals(plan: AllocationPlan) -> None:
    """Fill the plan-level money totals from its rows, and settle their outcomes.

    Called at the end of BOTH solvers and of ``apply_order_impacts``, which is
    exactly the set of places where the rows have stopped moving -- so it is also
    where ``_reconcile_sizing_outcomes`` belongs.
    """
    _reconcile_sizing_outcomes(plan.rows)
    plan.total_buy_value = sum(r.estimated_value for r in plan.rows if r.is_buy)
    plan.total_sell_value = sum(r.estimated_value for r in plan.rows if r.is_sell)
    plan.required_buying_power = sum(r.bp_cost for r in plan.rows if r.is_buy)
    plan.released_buying_power = sum(r.bp_released for r in plan.rows if r.is_sell)
    # Against the BUDGET -- broker buying power plus what this plan's own sells
    # free -- and never against the pre-sell figure alone. See AllocationPlan.
    plan.bp_usage_pct = (plan.required_buying_power / plan.total_buying_power * 100.0
                         if plan.total_buying_power > 0 else 0.0)


def _carry_margin_facts(row: AllocationRow, margin: Optional[MarginInfo]) -> None:
    """Copy the margin facts the DISPLAY needs from ``MarginInfo`` onto the row.

    Both solvers build their rows in their own loop, and both used to read
    ``m.marginable`` for one reason string and then drop the object -- so a plan,
    once solved, could no longer say whether the broker had published a margin rate
    at all. That distinction is not cosmetic: it is the difference between "this
    instrument is buying-power-penalised" and "this broker says nothing about
    symbols you do not already hold".

    ``margin is None`` leaves the dataclass defaults in place, which already ARE the
    "no MarginInfo" state. Nothing is invented and nothing is coerced -- in
    particular ``initial_margin_rate`` stays ``None`` rather than becoming a
    plausible 1.0.

    Deliberately does NOT touch ``bp_factor``: that one has a caller-supplied
    fallback (``default_bp_factor``, the account multiplier) and each solver
    already sets it on the line above.
    """
    if margin is None:
        return
    row.marginable = bool(margin.marginable)
    row.initial_margin_rate = margin.initial_margin_rate
    row.margin_source = margin.source


def _apply_bp_scaling(rows: List[AllocationRow], available_buying_power: float, *,
                      allow_fractional: bool,
                      margin: Optional[Dict[str, MarginInfo]] = None) -> float:
    """Scale every BUY pro-rata until the plan fits the buying-power BUDGET.

    THE BUDGET IS ``available_buying_power`` PLUS WHAT THE SELLS FREE, not the
    published figure alone. Sells go to the broker first (decision 13), so their
    ``bp_released`` is real money by the time a buy needs it -- and measuring
    against the pre-sell figure told a plan that sold 2,112 to buy 1,187 that it
    was 91% used and cut every buy to a third of itself. A sell the broker then
    REFUSES leaves a buy short of room and the broker rejects that one row
    (``_submit_row`` reports it); the dry run can see the same thing coming,
    because un-ticking a sell re-measures the budget through ``filter_plan_rows``.

    SELLS NEVER SCALE -- they only ever free. The re-rounded quantity is fed
    back through ``round_quantity`` (so increments and min order sizes still
    hold) and each row's ``bp_cost`` is scaled by the SAME quantity ratio rather
    than recomputed, which preserves a broker-precheck cost when one has been
    substituted. A buy that scales to zero shares is marked ``skipped`` with its
    ``side`` cleared, and one stopped by ``min_order_size`` rather than by the
    scaling itself also gets ``REASON_BELOW_MIN_ORDER_FMT`` -- the two have
    different fixes and must not be confused for each other.

    ``margin`` may be omitted (the precheck path has no margin dict); a
    fractional row then keeps its fractional grid via a synthetic MarginInfo.

    Returns the scale factor applied (1.0 when the plan already fitted).
    """
    buys = [r for r in rows if r.is_buy]
    required = sum(r.bp_cost for r in buys)
    # ``is_sell`` and not the raw side: a sell a precheck refused is skipped, and
    # a refused close frees nothing.
    avail = (float(available_buying_power or 0.0)
             + sum(r.bp_released for r in rows if r.is_sell))
    if not buys or required <= avail:
        return 1.0
    scale = (avail / required) if required > 0 else 0.0
    for r in buys:
        m = (margin or {}).get(r.symbol)
        if m is None and r.fractional:
            m = MarginInfo(symbol=r.symbol, bp_factor=r.bp_factor, fractionable=True)
        prev_qty = r.delta_quantity
        qty = round_quantity(r.estimated_value * scale, r.price, m,
                             allow_fractional=allow_fractional,
                             apply_min_order_size=False)
        # Scaling and the minimum order size are different causes with different
        # fixes (add buying power vs. nothing the user can do), so a row stopped by
        # the minimum must say so rather than blame the scaling alone.
        qty = _suppress_below_min_order(qty, m, r.reasons, r.price)
        ratio = (qty / prev_qty) if prev_qty > 0 else 0.0
        r.delta_quantity = qty
        r.target_quantity = r.current_quantity + qty
        r.estimated_value = qty * r.price
        r.bp_cost = r.bp_cost * ratio
        r.reasons.append(REASON_SCALED_FMT.format(factor=scale))
        if qty <= 0:
            # ONE shape for "no order": a scaled-away buy reads exactly like a
            # no-price skip (side None, skipped True) to anything inspecting the
            # raw field rather than is_buy/is_sell.
            r.skipped = True
            r.side = None
            # The whole pre-scaling intent is unmet, not just the scaled-away part.
            r.unmet_notional = abs(prev_qty) * float(r.price or 0.0)
    _reclaim_rounding_slack(buys, avail, margin=margin, allow_fractional=allow_fractional)
    return scale


def _reclaim_rounding_slack(buys: List[AllocationRow], avail: float, *,
                            margin: Optional[Dict[str, MarginInfo]],
                            allow_fractional: bool) -> None:
    """Fund rows the pro-rata scale rounded away, out of the slack rounding left.

    THE PROBLEM THIS SOLVES, from live use (2026-09-05). Scaling multiplies every buy
    by one factor and THEN rounds, so a whole-share symbol whose target was 1.43
    shares becomes 0.93 and floors to nothing -- while every fractionable symbol beside
    it keeps its fraction. The dropped row's budget is not reallocated, so the plan
    ends with cash it declined to spend: one real plan reported seven untraded rows and
    $334.36 unallocated while a $105.89 share of CARZ sat rounded to zero. The bias is
    systematic and falls entirely on symbols the broker will not split.

    Rounding slack is REAL, not notional: fractional rows round down to a 5-decimal
    grid and whole-share rows floor, so the plan always consumes less than the budget
    the scale factor assumed. This hands that remainder back to the rows that got
    nothing, largest denied first, one tradeable unit at a time.

    Three limits, each of which is the point rather than a detail:
      * only rows the SCALING zeroed -- a row stopped by ``min_order_size`` is refused
        by the broker, not by arithmetic, and topping it up would rebuild an order the
        broker will reject;
      * never past what the row would have had UNSCALED, so a symbol whose true target
        is half a share is not handed a whole one it was never owed;
      * only while the slack actually covers the unit's buying-power cost, so this can
        never turn a fitted plan into an over-committed one.
    """
    if not buys:
        return
    slack = float(avail) - sum(r.bp_cost for r in buys)
    if slack <= MONEY_EPSILON:
        return
    denied = [r for r in buys
              if r.delta_quantity <= 0
              and r.unmet_notional
              and float(r.price or 0.0) > 0
              # NORMAL rows only. A BUMPED row is one whose target was UNDER a whole
              # unit and which the sizer deliberately rounded UP -- an over-allocation
              # granted while money was loose. The scaler cutting it back to nothing is
              # that generosity being withdrawn when money is tight, which is correct;
              # re-funding it here would spend the slack on a symbol that was never
              # owed a whole share, ahead of one that was.
              and r.sizing_outcome == SIZING_OUTCOME_NORMAL
              # Neither broker floor: both are the broker refusing an order, not
              # arithmetic losing one, and topping either up rebuilds a rejection.
              and not any(x.startswith(REASON_BELOW_MIN_ORDER_PREFIX)
                          or x.startswith(REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX)
                          for x in r.reasons)]
    # Largest denied intent first: the biggest hole in the plan is the one worth
    # closing, and it keeps the outcome independent of row order.
    for r in sorted(denied, key=lambda x: -float(x.unmet_notional or 0.0)):
        m = (margin or {}).get(r.symbol)
        unit = tradeable_unit(m, allow_fractional=allow_fractional)
        # WHOLE-SHARE ROWS ONLY. This exists because pro-rata scaling rounds a
        # non-fractionable symbol DOWN THROUGH ONE WHOLE SHARE to nothing while its
        # fractionable neighbours keep a proportional slice -- a bias that falls
        # entirely on symbols the broker will not split. A fractional row loses at
        # most one 1e-5 step to the same rounding, so "restoring" it would hand back
        # a third of a cent and mean nothing.
        if unit < 1.0:
            continue
        price = float(r.price)
        wanted = float(r.unmet_notional or 0.0) / price      # the unscaled share count
        if wanted + QUANTITY_EPSILON < unit:
            continue                                          # never owed a whole unit
        cost = unit * price * float(r.bp_factor or 1.0)
        if cost > slack + MONEY_EPSILON:
            continue                                          # try the next one down
        slack -= cost
        r.delta_quantity = unit
        r.target_quantity = r.current_quantity + unit
        r.estimated_value = unit * price
        r.bp_cost = cost
        r.skipped = False
        r.side = OrderDirection.BUY
        r.unmet_notional = max(0.0, float(r.unmet_notional or 0.0) - r.estimated_value)
        r.reasons.append(REASON_RECLAIMED_FMT.format(qty=unit, slack=slack + cost))


def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float,
                       valuation_mode: str,
                       unallocated_pct: float = 0.0) -> AllocationPlan:
    """Solve a full REBALANCE: every managed label, buys and sells.

    ``valuation_mode`` is REQUIRED on all three entry points
    (``compute_base_notional``, this, and ``compute_label_investment``) and NONE of
    them has a Python default. They used to have defaults that DISAGREED -- the
    base fell back to ``cost`` and the two solvers to ``market``, each matching its
    own historically-pinned behaviour -- so a single call site that forgot the
    keyword measured its base one way and its deltas another. The mode selects the
    meaning of "current value" in three places at once (the allocatable base, the
    displayed percentages, and every delta) and they must never disagree; a
    ``TypeError`` at the call site is the only version of that mistake anyone ever
    sees. There is no default that would be right for every caller, so there is no
    default at all.

    Args:
        base_notional: the allocatable base from ``compute_base_notional``. MUST be
            a real, non-negative number -- see Raises.
        available_buying_power: broker buying power, the FEASIBILITY constraint
            (targets are notional, not buying power -- decision 2).
        labels: managed labels with ``target_pct`` (1-100) and symbol weights.
        current: ``{symbol: PositionState}``; a symbol absent here is treated as
            flat. The CALLER must have refused to build this dict when
            ``get_positions()`` returned ``None`` (fetch failure, not flat).
        margin: ``{symbol: MarginInfo}``; a symbol MISSING here falls back to
            ``default_bp_factor``.
        allow_fractional: opt-in per run (decision 12).
        default_bp_factor: conservative fallback == the account margin multiplier
            (assume no leverage); under-deploys rather than over-commits.
        valuation_mode: ``cost`` or ``market`` (decision 5a), REQUIRED. ``market``
            targets a SHARE COUNT (``target_notional / price``) and deltas against
            the held quantity; ``cost`` targets a PURCHASE VALUE and deltas against
            ``cost_basis``. Pass the same mode used to build ``base_notional``.
        unallocated_pct: the account's STORED cash reserve, 0-100, defaulting to 0
            (no reserve). It scales the base ONCE -- see ``investable_notional`` --
            and does NOT touch the label percentages, which stay the relative
            weights the user typed. Defaulted rather than required because it is a
            preference with a meaningful "none", unlike ``valuation_mode``, whose
            two candidate defaults were both wrong.

    Behaviour on degenerate DATA (records a reason, never raises -- contrast the
    degenerate MONEY inputs under Raises, which must never be guessed at):
        * a label with no symbols -> allocates nothing, ``target_pct`` restated as a
          share of the base and added to ``plan.unallocatable_pct``, and above 0% a
          ``WARNING_EMPTY_LABEL_FMT`` naming both percentages;
        * a symbol whose ``PositionState.price`` is ``None`` or <= 0 -> skipped
          with ``REASON_NO_PRICE`` (no guessed price -- no-fallback rule);
        * a negative computed target -> clamped to 0 with ``REASON_NEGATIVE_CLAMPED``;
        * a delta the broker would refuse as too small (``margin.min_order_size``)
          -> the ORDER is suppressed and the POSITION IS HELD, with
          ``REASON_BELOW_MIN_ORDER_FMT``; the target itself is never rewritten;
        * a symbol in several managed labels -> targets SUM, one row, and
          ``REASON_MULTI_LABEL_FMT`` (no enforcement -- decision 7);
        * ``sum(bp_cost of buys) > available_buying_power`` -> every buy scaled
          pro-rata, ``plan.scale_factor`` set and ``REASON_SCALED_FMT`` added.

    Label percentages are NOT renormalised: a set totalling 90% deploys 90% of
    the INVESTABLE remainder and leaves the rest as cash. That is a degenerate
    input, not a feature -- ``validate_label_targets`` refuses it, and the cash the
    user actually meant to hold belongs in ``unallocated_pct``, which this function
    DOES record. Blocking submission is the validator's job, not this function's.

    RAISING THE RESERVE ON A FULLY INVESTED ACCOUNT PRODUCES SELLS. Every target
    shrinks by the same factor, so a book already worth its whole base has to shed
    the difference. That is arithmetically right and the dry run shows it as
    ordinary sell rows.

    No minimum order threshold of our own: every non-zero delta becomes a row
    (decision 11); only the BROKER's ``min_order_size`` suppresses one.

    Targets are LONG-ONLY. A sell can never exceed the holding, and against a
    pre-existing SHORT the engine buys back towards the target rather than
    extending it -- a zero target on a short buys it to flat.

    Returns:
        AllocationPlan: one AllocationRow per managed symbol (including zero-delta
        and skipped rows, so the UI can show them) plus plan-level totals, tagged
        with the ``valuation_mode`` every number in it was measured in.

    Raises:
        TypeError: if ``valuation_mode`` is omitted (it has no default).
        ValueError: on an unknown ``valuation_mode``; on a ``None`` or NEGATIVE
        ``base_notional``; or on a ``None`` ``available_buying_power``. These are
        money inputs and there is no fallback for them (platform rule): a base
        coerced to zero is indistinguishable from "sell everything", which is
        precisely the accident ``PositionFetchFailed`` exists to prevent.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    # A missing or negative base is NOT a base of zero. Coercing it made every
    # managed target 0, i.e. a full liquidation -- the exact accident
    # PositionFetchFailed exists to prevent, through a different door.
    if base_notional is None:
        raise ValueError("compute_allocation: base_notional is None")
    if float(base_notional) < 0:
        raise ValueError(
            f"compute_allocation: base_notional is negative ({base_notional})")
    if available_buying_power is None:
        raise ValueError("compute_allocation: available_buying_power is None")
    current = current or {}
    margin = margin or {}
    plan = AllocationPlan(base_notional=float(base_notional),
                          available_buying_power=float(available_buying_power),
                          allow_fractional=bool(allow_fractional),
                          valuation_mode=valuation_mode,
                          labels=list(labels or []))
    # THE SINGLE SCALING POINT. The reserve is applied ONCE, here, to produce the
    # money the labels divide; every target below is a share of ``investable``
    # rather than of the base. Scaling each target instead would repeat the factor
    # N times and let the percentages on screen drift from the ones solved with.
    # ``base_notional`` on the plan stays GROSS, so reserved + investable == base.
    plan.reserved_pct = clamp_unallocated_pct(unallocated_pct)
    plan.reserved_notional = reserved_notional_for(plan.base_notional, plan.reserved_pct)
    investable = plan.investable_notional
    targets = {}
    target_pcts = {}
    sym_labels = {}
    # The PER-LABEL split, which row.target_notional cannot carry: a symbol in two
    # labels sums their shares into one row. Redistribution needs the split.
    label_targets: Dict[str, Dict[str, float]] = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        if not lt.symbols:
            # An empty label cannot absorb its percentage: the money stays idle.
            # At 0% there is nothing to absorb and nothing to warn about.
            #
            # Restated against the BASE on the way in. ``pct`` is a relative weight
            # on the investable remainder, and ``unallocatable_pct`` sits beside
            # ``reserved_pct`` -- which is a share of the base -- as the other half
            # of "what is not going to work". Two denominators there make the pair
            # sum past what the account holds.
            plan.unallocatable_pct += effective_target_pct(max(0.0, pct),
                                                           plan.reserved_pct)
            if pct > 0:
                plan.warnings.append(WARNING_EMPTY_LABEL_FMT.format(
                    label=lt.label, pct=pct,
                    base_pct=effective_target_pct(pct, plan.reserved_pct)))
            continue
        for st in lt.symbols:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + investable * share / 100.0
            target_pcts[st.symbol] = target_pcts.get(st.symbol, 0.0) + share
            sym_labels.setdefault(st.symbol, []).append(lt.label)
            per_label = label_targets.setdefault(lt.label, {})
            per_label[st.symbol] = (per_label.get(st.symbol, 0.0)
                                    + investable * share / 100.0)

    for symbol, target_notional in targets.items():
        ps = current.get(symbol)
        m = margin.get(symbol)
        row = AllocationRow(symbol=symbol, labels=list(sym_labels[symbol]))
        row.bp_factor = float(m.bp_factor) if m is not None else float(default_bp_factor)
        _carry_margin_facts(row, m)
        row.current_quantity = float(ps.quantity) if ps is not None else 0.0
        row.current_cost_basis = float(ps.cost_basis) if ps is not None else 0.0
        row.price = ps.price if ps is not None else None
        if len(row.labels) > 1:
            row.reasons.append(REASON_MULTI_LABEL_FMT.format(labels=", ".join(row.labels)))
        if m is not None and not m.marginable:
            row.reasons.append(REASON_NOT_MARGINABLE)
        if target_notional < 0:
            target_notional = 0.0
            row.reasons.append(REASON_NEGATIVE_CLAMPED)
        row.target_notional = target_notional
        if row.price is None or row.price <= 0:
            # No fallback price for live data -- skip the symbol and report it.
            row.skipped = True
            row.reasons.append(REASON_NO_PRICE)
            # A share of the INVESTABLE remainder like every other label figure
            # here (it is ``label_pct x symbol_weight``), so it is restated against
            # the base for the same reason the empty-label branch above is.
            plan.unallocatable_pct += effective_target_pct(
                max(0.0, target_pcts.get(symbol, 0.0)), plan.reserved_pct)
            plan.rows.append(row)
            continue
        # Tri-state, explicitly. ``m.fractionable is None`` means the broker did
        # not SAY -- a data gap Refresh can fix -- and must never be reported as
        # "not fractionable", which is a fact about the instrument.
        frac = bool(allow_fractional and m is not None and m.fractionable is True)
        row.fractional = frac
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional and (m is None or m.fractionable is None):
            row.reasons.append(REASON_FRACTIONAL_UNKNOWN)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)

        if target_notional <= 0 and row.current_quantity > 0:
            # Same in both modes: a zero target flattens the position outright.
            # Keyed on the NOTIONAL, never on the rounded quantity: only an
            # actual instruction to hold nothing may liquidate, never a rounding
            # rule that happened to produce 0 shares.
            delta = -row.current_quantity
            raw_delta = delta
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        elif valuation_mode == VALUATION_MODE_COST:
            # Target a PURCHASE VALUE: close the gap to the current cost basis.
            #
            # The two legs convert at DIFFERENT rates. Buying a share adds the
            # market PRICE to the basis, but selling one removes the AVERAGE COST
            # (cost_basis is quantity x avg_entry_price). Dividing a basis
            # reduction by the price is wrong by price/avg_cost -- with the price
            # halved it asks for twice the shares, which the hold-clamp then turns
            # into a full liquidation of a position that only wanted trimming.
            basis = current_value(ps, VALUATION_MODE_COST)
            gap = target_notional - basis
            unit_value = row.price
            if gap < 0:
                avg_cost = (basis / row.current_quantity
                            if row.current_quantity > 0 else 0.0)
                if avg_cost <= 0:
                    # No usable average cost (no shares, or a non-positive basis):
                    # there is nothing to trim and nothing to guess from.
                    gap = 0.0
                else:
                    unit_value = avg_cost
            # The UNROUNDED intent, kept so a row the grid zeroes can say by how
            # much it missed instead of rendering as "nothing to do".
            raw_delta = (gap / unit_value) if unit_value and unit_value > 0 else 0.0
            delta = round_delta_quantity(
                gap, unit_value, m, allow_fractional=allow_fractional,
                current_quantity=row.current_quantity)
        else:
            # Target a SHARE COUNT: target_notional / price, delta vs what is held.
            raw_delta = float(target_notional) / float(row.price) - row.current_quantity
            # Round the DELTA ITSELF -- never the target first. Flooring the
            # TARGET to a whole share count and then subtracting the holding is
            # off by exactly the fractional part of the target: a $50 target on a
            # $150 stock floors to 0 target shares, and "0 minus the 1 share
            # held" sells the WHOLE position on a trim the user never asked for
            # -- then the next run's target is still sub-share, so it buys the
            # share straight back. ``round_delta_quantity`` (COST mode, two
            # branches up) already rounds the delta and not the target for
            # exactly this reason; this mirrors it via the same
            # ``_round_delta_shares``, so a trim that cannot be sent on the grid
            # leaves the position where it is instead of closing it -- matching
            # ``grid_zeroed`` below, which is what "leave it alone" means.
            delta = _round_delta_shares(raw_delta, m,
                                        allow_fractional=allow_fractional,
                                        current_quantity=row.current_quantity)
        if abs(delta) < QUANTITY_EPSILON:
            delta = 0.0
        # D1, and it runs BEFORE _suppress_below_min_order on purpose (L8b): the
        # suppression zeroes the row, and a bump that runs afterwards finds nothing
        # left to decide about.
        whole_position = (row.current_quantity == 0.0 and target_notional > 0
                          and raw_delta > 0)
        grid_zeroed = (delta == 0.0 and abs(raw_delta) >= QUANTITY_EPSILON)
        floor_refuses = _below_fractional_notional_floor(delta, m, row.price)
        if whole_position and (grid_zeroed or floor_refuses):
            # The whole intended POSITION cannot be sent as sized: decision D1
            # weighs the grid, the $5 fractional floor and the overshoot guard
            # together and either bumps to one sendable unit or prices the miss.
            delta, outcome, reason = size_sub_unit_target(
                target_notional, row.price, m, allow_fractional=allow_fractional)
            row.sizing_outcome = outcome
            row.reasons.append(reason)
            if delta <= 0:
                row.unmet_notional = abs(raw_delta) * row.price
        elif grid_zeroed:
            # A sub-unit ADJUSTMENT to an existing position (or to a short).
            # Never bumped: the position exists, and the landed rule for a trade
            # that cannot be sent is to LEAVE THE POSITION WHERE IT IS.
            row.reasons.append(REASON_ROUNDS_TO_ZERO_FMT.format(raw=raw_delta))
            row.unmet_notional = abs(raw_delta) * row.price
        # ONE minimum-order check, on the signed delta every branch produces -- the
        # only quantity that is actually sent to the broker.
        before_min_order = delta
        delta = _suppress_below_min_order(delta, m, row.reasons, row.price)
        if delta == 0.0 and before_min_order != 0.0:
            row.unmet_notional = abs(before_min_order) * row.price
        row.delta_quantity = delta
        # target_quantity is the POST-TRADE holding in every mode and every branch:
        # what the account owns if this row executes, never an unreachable ideal.
        row.target_quantity = row.current_quantity + delta
        if delta > 0:
            row.side = OrderDirection.BUY
        elif delta < 0:
            row.side = OrderDirection.SELL
        row.estimated_value = abs(delta) * row.price
        # MUTUALLY EXCLUSIVE, and the same rate in both directions: a buy charges,
        # a sell frees, and selling a position back must give up exactly what
        # buying it consumed.
        row.bp_cost = row.estimated_value * row.bp_factor if delta > 0 else 0.0
        row.bp_released = row.estimated_value * row.bp_factor if delta < 0 else 0.0
        plan.rows.append(row)

    # D2, BEFORE the buying-power pass: the label's own arithmetic first, the
    # account-wide feasibility constraint second. Redistribution is capped by the
    # live headroom, so it can never be the reason scaling has to fire.
    redistribute_label_residuals(plan, label_targets, margin,
                                 allow_fractional=allow_fractional)
    plan.scale_factor = _apply_bp_scaling(plan.rows, plan.available_buying_power,
                                          allow_fractional=allow_fractional, margin=margin)
    _finalise_totals(plan)
    return plan


def compute_label_investment(label: LabelTarget, amount: float,
                             current: Dict[str, PositionState],
                             margin: Dict[str, MarginInfo], *,
                             available_buying_power: float, allow_fractional: bool,
                             default_bp_factor: float,
                             valuation_mode: str) -> AllocationPlan:
    """Solve an INVEST_LABEL run: put ``amount`` into ONE label. Buys only.

    NO RESERVE APPLIES HERE, and there is deliberately no keyword to pass one. The
    portfolio-level ``unallocated_pct`` is a share of the BASE -- buying power plus
    the managed book -- and this run has no base: ``amount`` is a specific sum the
    user named and typed. Skimming 10% off it would spend less than they asked for,
    with the shortfall explained by a setting on a different screen. The reserve
    still governs the REBALANCE that decides how much of the book is invested at
    all; an invest run deploys income into one label and is not that decision.

    ``amount`` is split by the label's symbol weights. ``label.target_pct`` is
    IGNORED -- the amount is the whole budget, and it is ADDED to whatever the
    account already holds rather than rebalanced towards. No sells are ever
    produced, so ``plan.total_sell_value`` is always 0.0 and
    ``plan.net_buy_value == plan.total_buy_value`` -- redistribution is inside that
    promise: on a budget basis it may only make a BUY smaller, never open a sale, so
    a bump that overshoots the budget is reported rather than sold back off.
    Buying-power scaling, rounding, missing prices and missing margin info behave
    exactly as in ``compute_allocation``, and a symbol repeated inside the label
    COALESCES into one row whose weight is the sum -- again as in
    ``compute_allocation``.

    The weights are multiplied straight through, so the caller MUST gate submission
    on ``validate_symbol_weights``: a 150% set deploys 150% of ``amount`` and a 60%
    set leaves 40% of it as cash, neither of which this function refuses.

    ``valuation_mode`` is accepted for call-site symmetry and validated, and is
    RECORDED on the returned plan, but does not change the arithmetic: an
    INVEST_LABEL run ADDS a budget on top of the existing position rather than
    rebalancing towards a target value. It is REQUIRED and has NO default anyway
    (see ``compute_allocation``): the plan is STAMPED with it, and a plan stamped
    with a mode nobody chose cannot be read back correctly six months later.

    The returned plan's ``base_notional`` is the BUDGET, not an allocatable base.

    Raises:
        TypeError: if ``valuation_mode`` is omitted (it has no default).
        ValueError: on an unknown ``valuation_mode``, or on a ``None`` ``amount``
        or ``available_buying_power`` -- money inputs have no fallback, and a
        budget silently coerced to zero would report "nothing to do" for what was
        actually a caller bug.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    if amount is None:
        raise ValueError("compute_label_investment: amount is None")
    if available_buying_power is None:
        raise ValueError("compute_label_investment: available_buying_power is None")
    current = current or {}
    margin = margin or {}
    budget = max(0.0, float(amount))
    plan = AllocationPlan(base_notional=budget,
                          available_buying_power=float(available_buying_power),
                          allow_fractional=bool(allow_fractional),
                          valuation_mode=valuation_mode,
                          # The target is money to DEPLOY, not a post-trade holding
                          # value: this run ADDS to whatever is already owned.
                          allocation_basis=ALLOCATION_BASIS_BUDGET,
                          # ``reserved_pct`` stays 0.0: base_notional IS the budget
                          # here, so "100 minus the label percentages" means nothing.
                          labels=[label] if label is not None else [])
    if not label.symbols:
        # Both percentages are 100 and that is not a coincidence: an INVEST_LABEL
        # run's ``base_notional`` IS the budget and its ``reserved_pct`` is always
        # 0, so the weight and the share of base cannot diverge here.
        plan.unallocatable_pct = 100.0
        plan.warnings.append(WARNING_EMPTY_LABEL_FMT.format(
            label=label.label, pct=100.0, base_pct=100.0))
        return plan
    weights: Dict[str, float] = {}
    for st in label.symbols:
        # A symbol repeated inside one label SUMS into a single row, exactly as
        # compute_allocation's per-symbol dict does. Two rows would submit two
        # independent orders for the same symbol in one run, which nothing
        # downstream expects. validate_symbol_weights is what REPORTS the duplicate.
        weights[st.symbol] = weights.get(st.symbol, 0.0) + float(st.weight_pct or 0.0)
    for symbol, weight in weights.items():
        target_notional = budget * weight / 100.0
        ps = current.get(symbol)
        m = margin.get(symbol)
        row = AllocationRow(symbol=symbol, labels=[label.label])
        row.bp_factor = float(m.bp_factor) if m is not None else float(default_bp_factor)
        _carry_margin_facts(row, m)
        row.current_quantity = float(ps.quantity) if ps is not None else 0.0
        row.current_cost_basis = float(ps.cost_basis) if ps is not None else 0.0
        row.price = ps.price if ps is not None else None
        if m is not None and not m.marginable:
            row.reasons.append(REASON_NOT_MARGINABLE)
        if target_notional < 0:
            target_notional = 0.0
            row.reasons.append(REASON_NEGATIVE_CLAMPED)
        row.target_notional = target_notional
        if row.price is None or row.price <= 0:
            row.skipped = True
            row.reasons.append(REASON_NO_PRICE)
            plan.unallocatable_pct += max(0.0, weight)
            plan.rows.append(row)
            continue
        frac = bool(allow_fractional and m is not None and m.fractionable is True)
        row.fractional = frac
        qty = round_quantity(target_notional, row.price, m,
                             allow_fractional=allow_fractional,
                             apply_min_order_size=False)
        raw_qty = float(target_notional) / float(row.price)
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional and (m is None or m.fractionable is None):
            row.reasons.append(REASON_FRACTIONAL_UNKNOWN)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)
        if raw_qty >= QUANTITY_EPSILON and (
                qty <= 0 or _below_fractional_notional_floor(qty, m, row.price)):
            # The budget share IS the intended allocation for this symbol, so D1
            # applies even when the symbol is already held: deploying nothing is the
            # wrong answer to "put this money into this label". Runs BEFORE the
            # suppression below, for the reason given in compute_allocation (L8b).
            qty, outcome, reason = size_sub_unit_target(
                target_notional, row.price, m, allow_fractional=allow_fractional)
            row.sizing_outcome = outcome
            row.reasons.append(reason)
            if qty <= 0:
                row.unmet_notional = raw_qty * row.price
        # Buys only here, so the budget IS the order: same suppression, same reason.
        before_min_order = qty
        qty = _suppress_below_min_order(qty, m, row.reasons, row.price)
        if qty == 0.0 and before_min_order != 0.0:
            row.unmet_notional = before_min_order * row.price
        row.delta_quantity = qty
        row.target_quantity = row.current_quantity + qty
        if qty > 0:
            row.side = OrderDirection.BUY
        row.estimated_value = qty * row.price
        row.bp_cost = row.estimated_value * row.bp_factor
        plan.rows.append(row)
    redistribute_label_residuals(
        plan, {label.label: {sym: budget * w / 100.0 for sym, w in weights.items()}},
        margin, allow_fractional=allow_fractional)
    plan.scale_factor = _apply_bp_scaling(plan.rows, plan.available_buying_power,
                                          allow_fractional=allow_fractional, margin=margin)
    _finalise_totals(plan)
    return plan


def apply_order_impacts(plan: AllocationPlan, impacts: Dict[str, OrderImpact], *,
                        available_buying_power: float,
                        margin: Optional[Dict[str, MarginInfo]] = None) -> AllocationPlan:
    """Re-solve a plan against broker PRECHECK results (precheck over estimation).

    For each row with a matching ``OrderImpact``, replaces the estimated
    ``bp_cost`` with ``impact.bp_cost`` (the positive, sign-corrected value) and
    re-runs the pro-rata buying-power scaling. A symbol with no impact keeps its
    estimated cost. ``impact.warnings`` are copied onto ``row.reasons`` and
    ``impact.estimated_fees`` onto ``row.estimated_fees``, so the broker's own
    advisory output reaches the dry-run instead of being dropped.

    ``impact.accepted is False`` ZEROES the row -- ``skipped``, ``side`` cleared,
    delta, value and cost to 0, ``target_quantity`` back to what is held -- and
    copies ``impact.errors`` into ``row.reasons``. A refused order that kept its
    side and quantity would render as a live BUY to anything reading the raw
    fields, which the dry-run table does.

    Pass the SAME ``margin`` dict the plan was solved with: without it the
    re-solve rebuilds a bare ``MarginInfo`` for each fractional row and so rounds
    on the default 4dp grid, losing ``min_trade_increment``, ``min_order_size``
    AND ``min_fractional_notional`` -- a broker-side rejection waiting to happen,
    and the notional floor is the one most likely to bite, because scaling a buy
    down is exactly what pushes a fractional order under it.

    ``scale_factor`` COMPOUNDS: a plan already scaled to 0.6 that the precheck
    halves again reports 0.3, the true factor against the ORIGINAL target, and
    the affected rows carry one ``REASON_SCALED_FMT`` saying so rather than two
    that each tell half the story.

    Deliberately NOT done: a FAVOURABLE precheck lowers ``bp_cost`` but the freed
    buying power is never re-deployed, because ``_apply_bp_scaling`` only ever
    scales down. Never overspending beats fully deploying.

    Also deliberately NOT done: the label residual is not re-redistributed after a
    precheck rejection. This function has no per-label target split in hand, the
    user is looking at the plan at that moment, and the refused money is already
    reported on the row as ``unmet_notional``.

    Returns a NEW AllocationPlan; ``plan`` is not mutated. Adds
    ``WARNING_PRECHECK_DISAGREED_FMT`` for each row whose cost changed.
    """
    out = AllocationPlan(
        rows=[copy.deepcopy(r) for r in plan.rows],
        base_notional=plan.base_notional,
        available_buying_power=float(available_buying_power or 0.0),
        unallocatable_pct=plan.unallocatable_pct,
        reserved_pct=plan.reserved_pct,
        reserved_notional=plan.reserved_notional,
        allow_fractional=plan.allow_fractional,
        valuation_mode=plan.valuation_mode,
        allocation_basis=plan.allocation_basis,
        labels=list(plan.labels),
        warnings=list(plan.warnings),
    )
    for row in out.rows:
        impact = (impacts or {}).get(row.symbol)
        if impact is None:
            continue
        if impact.warnings:
            row.reasons.extend(impact.warnings)
        if not impact.accepted:
            # ONE shape for "no order" -- a refused order that kept its side and
            # quantity renders as a live BUY to anything reading the raw fields.
            row.skipped = True
            row.side = None
            # What the broker refused is unmet money, exactly like a grid-zeroed row.
            row.unmet_notional = abs(row.delta_quantity) * float(row.price or 0.0)
            row.delta_quantity = 0.0
            row.target_quantity = row.current_quantity
            row.estimated_value = 0.0
            row.bp_cost = 0.0
            # A close the broker refused frees NOTHING, so its release has to come
            # straight back out of the budget the buys were sized against -- which
            # the re-scale below then acts on.
            row.bp_released = 0.0
            row.reasons.extend(impact.errors)
            continue
        row.estimated_fees = impact.estimated_fees
        if row.is_buy:
            # THE SOURCE MOVES WITH THE NUMBER. ``bp_leverage`` shows "?" instead of a
            # multiple whenever ``margin_source`` is MARGIN_SOURCE_DEFAULT, because a
            # default-sourced ratio is the account's conservative fallback rather than
            # a fact. Once the BROKER has measured this order that reasoning no longer
            # applies -- the ratio is now the realised charge the broker itself quoted
            # -- but the source was left saying "default", so every TastyTrade dry run
            # reported a genuinely measured buying-power cost as unknown (2026-09-05).
            row.margin_source = MARGIN_SOURCE_PRECHECK
            if abs(impact.bp_cost - row.bp_cost) > 0.005:
                out.warnings.append(WARNING_PRECHECK_DISAGREED_FMT.format(symbol=row.symbol))
                row.bp_cost = impact.bp_cost
    factor = _apply_bp_scaling(out.rows, out.available_buying_power,
                               allow_fractional=out.allow_fractional, margin=margin)
    out.scale_factor = float(plan.scale_factor) * factor
    if factor < 1.0:
        for row in out.rows:
            # Every buy still standing was scaled by BOTH passes (the first was
            # plan-wide), so collapsing its two reasons into the compounded factor
            # is exact. A row the precheck rejected keeps its first-pass reason.
            if len([r for r in row.reasons if r.startswith(REASON_SCALED_PREFIX)]) > 1:
                row.reasons = [r for r in row.reasons
                               if not r.startswith(REASON_SCALED_PREFIX)]
                row.reasons.append(REASON_SCALED_FMT.format(factor=out.scale_factor))
    _finalise_totals(out)
    return out


def consume_income_events(events: List[Tuple[int, float]],
                          net_buy_value: float) -> List[Tuple[int, float]]:
    """FIFO-consume the income ledger against a run's NET buy value. Pure.

    Args:
        events: ``[(income_event_id, open_amount)]``, ALREADY sorted oldest-first
            by ``event_date`` then ``id``. Pass plain tuples, not ORM rows, so
            this stays IO-free and unit-testable.
        net_buy_value: ``max(0, filled_buy_value - filled_sell_value)`` -- a
            rebalance funded entirely by its own sells consumes nothing.

    Returns:
        List[Tuple[int, float]]: ``[(income_event_id, amount_to_consume)]``, only
        for events actually touched. The last one may be PARTIAL; its remainder
        stays open. Empty when ``net_buy_value <= 0`` or the ledger is empty.
        The caller adds each amount to ``PortfolioIncomeEvent.consumed_amount``.
    """
    remaining = float(net_buy_value or 0.0)
    out = []
    if remaining <= 0:
        return out
    for event_id, open_amount in events or []:
        if remaining <= MONEY_EPSILON:
            break
        available = float(open_amount or 0.0)
        if available <= 0:
            continue
        take = min(available, remaining)
        out.append((event_id, take))
        remaining -= take
    return out


# ---------------------------------------------------------------------------
# Wizard: the allocatable base, snapshotted when the wizard opens.
# ---------------------------------------------------------------------------

#: Added to ``BaseSnapshot.warnings`` when the broker published no multiplier.
WARNING_NO_MULTIPLIER = "broker published no margin multiplier - assuming a cash account (1.0)"


@dataclass
class BaseSnapshot:
    """Frozen-at-wizard-open view of what there is to allocate.

    The wizard reads this ONCE when it opens and re-reads it only when the user
    presses Refresh, so the numbers cannot move underneath an edit. That is the
    whole point: buying power moves on every fill, dividend and mark, and a plan
    solved against 10,000 but submitted against a base that has since become
    9,000 is not the plan the user approved. ``taken_at`` is therefore part of
    the value, not decoration -- it is what the wizard displays and what a later
    submission compares against to decide the plan is stale.

    ``managed_value`` is the current value of the managed positions under
    ``valuation_mode`` (decision 5a), and ``base_notional`` is that plus buying
    power (decision 1).

    ``default_bp_factor`` is the conservative per-dollar buying-power cost fed to
    the engine for symbols the broker could not describe. It is the account
    margin multiplier when the broker publishes one; when it does not, it is
    1.0 -- "one dollar of notional costs one dollar of buying power", i.e. a cash
    account. Never guess HIGHER leverage than the broker admitted to.

    ``unpriced_held_symbols`` is the market-mode SUBMIT BLOCKER (see
    ``held_symbols_without_price``): managed symbols the account HOLDS and has no
    usable quote for. It is always empty in cost mode. Non-empty means every
    percentage on this snapshot is measured against a base that is missing those
    positions entirely -- feed it to ``held_no_price_block`` for the sentence.
    """
    available_buying_power: float
    managed_value: float
    base_notional: float
    default_bp_factor: float
    valuation_mode: str = VALUATION_MODE_MARKET
    cash: Optional[float] = None
    is_margin_account: bool = False
    supports_fractional: bool = False
    taken_at: DateTime = field(default_factory=lambda: DateTime.now(timezone.utc))
    warnings: List[str] = field(default_factory=list)
    unpriced_held_symbols: List[str] = field(default_factory=list)


def held_symbols_without_price(current: Dict[str, PositionState],
                               managed_symbols: List[str],
                               *, valuation_mode: str) -> List[str]:
    """Managed symbols the account HOLDS and cannot price. Pure. Market mode only.

    In ``market`` mode ``current_value`` returns 0.0 for a position with no usable
    quote, which is right (there is no fallback price in this platform) and
    silently WRONG for the base: the position drops out of ``base_notional``
    entirely, so every label's target shrinks by its share of the missing money.
    Measured: a 5,000 held position with a failed quote takes a 10,000 base to
    5,000 and HALVES every other label's target, with nothing on screen saying so.

    An UNHELD unpriced symbol is a non-event and is deliberately excluded: the
    solver already skips it with ``REASON_NO_PRICE`` and counts its share in
    ``AllocationPlan.unallocatable_pct``, so it corrupts no denominator.

    "Held" is ``quantity != 0``, tested on the MAGNITUDE: a short carries a
    negative quantity and a negative value and moves the base exactly as much as a
    long does. "No price" is ``None`` OR ``<= 0``, matching ``current_value``'s own
    branch -- a broker that answers 0.0 values the position at nothing just as
    surely as one that answers nothing at all.

    Returns ``[]`` in cost mode, where the price is never read and its absence
    changes no number.

    Returns:
        List[str]: in ``managed_symbols`` order, de-duplicated. EMPTY means the base
        is measured on complete data.
    """
    if valuation_mode != VALUATION_MODE_MARKET:
        return []
    out: List[str] = []
    for sym in dict.fromkeys(managed_symbols or []):
        state = (current or {}).get(sym)
        if state is None:
            continue
        if abs(float(state.quantity or 0.0)) <= QUANTITY_EPSILON:
            continue
        if state.price is None or float(state.price) <= 0:
            out.append(sym)
    return out


def held_no_price_block(symbols: Optional[List[str]]) -> Optional[str]:
    """The blocking sentence for ``held_symbols_without_price``, or ``None``. Pure.

    ``None`` means "nothing to say", so a caller can write
    ``if held_no_price_block(base.unpriced_held_symbols):`` and get the gate and the
    wording from one call.

    Deliberately NOT in ``ADVISORY_MESSAGE_FRAGMENTS``: this must BLOCK. The plan it
    describes is not merely imprecise, it is sized against a base that is missing
    whole positions, and the direction of the error (every target too small) is
    invisible on the dry run because every row is consistently too small.
    """
    syms = list(symbols or [])
    if not syms:
        return None
    return ERROR_HELD_NO_PRICE_FMT.format(count=len(syms), symbols=", ".join(syms))


def build_base_snapshot(
    snapshot: "AccountSnapshot",
    current: Dict[str, PositionState],
    managed_symbols: List[str],
    *,
    valuation_mode: str,
) -> BaseSnapshot:
    """Turn a broker AccountSnapshot into the wizard's frozen base.

    ``valuation_mode`` is REQUIRED and has NO Python default, exactly like the three
    solvers (``compute_base_notional``, ``compute_allocation``,
    ``compute_label_investment``). A default here and a different one at the solver
    is how the base and the deltas end up measured with two definitions of "current
    value"; making the omission a loud ``TypeError`` is cheaper than finding it in a
    plan. Pass the account's configured mode.

    Raises:
        ValueError: when there is no snapshot at all, when the broker published no
        ``buying_power`` (a plan sized against a guessed balance is worse than no
        plan), or when ``valuation_mode`` is unknown.
        TypeError: when ``valuation_mode`` is omitted.
    """
    if snapshot is None:
        raise ValueError("build_base_snapshot: no AccountSnapshot (the broker call failed).")
    if snapshot.buying_power is None:
        raise ValueError(
            "build_base_snapshot: broker published no buying_power; refusing to plan "
            "against a substituted default."
        )
    buying_power = float(snapshot.buying_power)
    managed_value = compute_base_notional(0.0, current, managed_symbols,
                                          valuation_mode=valuation_mode)

    warnings: List[str] = []
    if snapshot.margin_multiplier is None:
        default_bp_factor = 1.0
        warnings.append(WARNING_NO_MULTIPLIER)
        logger.warning("build_base_snapshot: no margin multiplier; using default_bp_factor=1.0")
    else:
        default_bp_factor = float(snapshot.margin_multiplier)

    unpriced = held_symbols_without_price(current, managed_symbols,
                                          valuation_mode=valuation_mode)
    if unpriced:
        logger.warning(
            f"build_base_snapshot: {len(unpriced)} HELD symbol(s) have no usable quote "
            f"in market mode ({', '.join(unpriced)}); they contribute 0 to the "
            f"{buying_power + managed_value:,.2f} base, so every label target is "
            f"understated. Submission is blocked until they price.")

    return BaseSnapshot(
        available_buying_power=buying_power,
        managed_value=managed_value,
        base_notional=buying_power + managed_value,
        default_bp_factor=default_bp_factor,
        valuation_mode=valuation_mode,
        cash=snapshot.cash,
        is_margin_account=bool(snapshot.is_margin_account),
        supports_fractional=bool(snapshot.supports_fractional),
        warnings=warnings,
        unpriced_held_symbols=unpriced,
    )


# ---------------------------------------------------------------------------
# Wizard step 4: the dry-run table. Pure -- the NiceGUI module only draws these.
# ---------------------------------------------------------------------------

#: A zero-delta row carrying one of these reason prefixes had an order and lost
#: it. Kept together so the dry-run and anything auditing a stored plan agree on
#: what "suppressed" means.
_SUPPRESSION_REASON_PREFIXES = (
    REASON_BELOW_MIN_ORDER_PREFIX,
    REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX,
    REASON_SCALED_PREFIX,
)


def _is_suppressed_row(row: "AllocationRow") -> bool:
    """True when this row WANTED an order and will not get one.

    The distinction the dry-run turns on. A zero delta has three very different
    causes and only two of them are worth the user's attention:

      * already on target -- nothing to review, and nothing to say;
      * NO PRICE -- there was never a target to miss, the symbol is already
        counted in ``plan.unallocatable_pct``, and no order was ever possible;
      * SUPPRESSED -- a real target that produced a real delta which was then
        zeroed by a broker rule (``min_order_size``, ``min_fractional_notional``),
        by the tradeable grid, by D1's bump bound, by the buying-power scaler, or
        by the broker's own precheck refusing it.

    Only the third is suppression. Reporting it is the point: the sub-$5
    fractional floor (TastyTrade ``below_notional_value_minimum``) does not mean
    "this rounds to zero", it means fractional is UNAVAILABLE at that size, and a
    table that drops the row tells the user there was nothing to do.

    ``unmet_notional`` is the primary test and covers every path the engine has:
    whatever zeroed a live intent SET that field, so this cannot fall behind a new
    suppression path the way a reason-prefix list does. The prefix scan is kept
    behind it for a hand-built or stored row that carries the reason without the
    number (``plan_json`` written before ``unmet_notional`` existed).
    """
    if abs(float(row.delta_quantity or 0.0)) > QUANTITY_EPSILON:
        return False
    if float(row.unmet_notional or 0.0) > MONEY_EPSILON:
        return True
    if any(reason.startswith(prefix)
           for reason in row.reasons for prefix in _SUPPRESSION_REASON_PREFIXES):
        return True
    # A row the broker's precheck REFUSED is zeroed and marked skipped with the
    # broker's own error strings, which match no format of ours. A no-price skip
    # is the one skip that never had an order, so it is the one exclusion.
    return bool(row.skipped) and REASON_NO_PRICE not in row.reasons


def bp_leverage(row: "AllocationRow") -> Tuple[Optional[float], str]:
    """How much buying power one dollar of this row's notional costs, and a verdict.

    Pure, and DERIVED rather than stored: ``apply_order_impacts`` replaces
    ``row.bp_cost`` with the broker's own measured figure and never touches
    ``row.bp_factor``, so a flag computed once at solve time goes stale in exactly
    the case that matters. The ratio is therefore
    ``bp_cost / estimated_value`` -- the realised charge -- and falls back to
    ``bp_factor`` only when there is no trade value to divide by (a suppressed row,
    where the instrument's factor is still the honest thing to show next to the
    reason the order died).

    THE PREDICATE, and why it points the way it does.
    ``bp_factor = initial_margin_rate x account_multiplier`` = the dollars of
    buying power ONE DOLLAR OF NOTIONAL consumes, so the neutral point is 1.0 on
    every live account shape, and a HIGHER number means LESS leverage:

        ordinary marginable stock, Reg-T 2:1   0.500 x 2 = 1.000
        cash / limited-margin account          1.000 x 1 = 1.000
        leveraged ETF, 75% maintenance         0.750 x 2 = 1.500
        LAZR (hard to borrow), verified live   0.989 x 2 = 1.978
        non-marginable in a margin account     1.000 x 2 = 2.000

    So "leveraged" in the plain sense -- consumes LESS buying power than its
    notional -- is ``< 1.0``, and above 1.0 is a buying-power PENALTY, which is the
    opposite. Both are named, because a single boolean cannot say which side of
    neutral a row is on and the penalty is the side that actually occurs today.

    THE GUARD, which is not optional. TastyTrade publishes no per-symbol margin
    requirement for a symbol the account does not already hold, so its adapter
    returns ``bp_factor = multiplier = 2.0``, ``initial_margin_rate = None`` and
    ``source = MARGIN_SOURCE_DEFAULT`` -- its own docstring calls that "assume no
    leverage". A first-time buy is unheld BY DEFINITION, so a bare ``ratio > 1.0``
    would brand every new TastyTrade position as penalised. When the rate is
    missing OR the source is the fallback, the ratio is still reported (it is what
    the plan really charges) and the VERDICT is withheld as
    ``LEVERAGE_UNKNOWN``.

    A SELL gets ``(None, LEVERAGE_NOT_APPLICABLE)``: ``bp_cost`` is 0.0 for sells by
    construction -- they free buying power -- so any ratio computed for one is an
    artefact of that zero, not a fact about the instrument.

    Returns:
        Tuple[Optional[float], str]: ``(ratio, verdict)``. ``ratio`` is ``None``
        only for a sell; ``verdict`` is always one of ``LEVERAGE_VERDICTS``.
    """
    if row.side == OrderDirection.SELL:
        return None, LEVERAGE_NOT_APPLICABLE
    estimated = float(row.estimated_value or 0.0)
    if estimated > MONEY_EPSILON:
        ratio = float(row.bp_cost or 0.0) / estimated
    else:
        ratio = float(row.bp_factor)
    # The guard runs BEFORE the comparison, never after: an unpublished rate must
    # produce no verdict at all, not a verdict that happens to be right.
    if row.initial_margin_rate is None or row.margin_source == MARGIN_SOURCE_DEFAULT:
        return ratio, LEVERAGE_UNKNOWN
    if ratio > 1.0 + LEVERAGE_RATIO_TOLERANCE:
        return ratio, LEVERAGE_PENALISED
    if ratio < 1.0 - LEVERAGE_RATIO_TOLERANCE:
        return ratio, LEVERAGE_LEVERAGED
    return ratio, LEVERAGE_NONE


# ---------------------------------------------------------------------------
# PRE-SUBMIT VALIDATION. Pure -- the live service refreshes the broker facts and
# runs the broker's own precheck around it.
# ---------------------------------------------------------------------------

#: A row the broker is EXPECTED to refuse, and why. One per finding, so a symbol
#: with two problems is reported twice rather than losing one of them.
REFUSAL_NOT_TRADABLE_FMT = "{symbol}: the broker does not accept orders for this symbol"
REFUSAL_FRACTIONAL_NOT_ELIGIBLE_FMT = (
    "{symbol}: {quantity:g} is a fractional quantity and the broker does not split "
    "this symbol")
REFUSAL_BELOW_MIN_ORDER_FMT = (
    "{symbol}: {quantity:g} share(s) is below the broker's {minimum:g}-share minimum")
REFUSAL_BELOW_MIN_NOTIONAL_FMT = (
    "{symbol}: a fractional order of ${value:,.2f} is below the broker's ${minimum:g} "
    "minimum")
REFUSAL_NO_PRICE_FMT = "{symbol}: no price, so the order cannot be sized"
REFUSAL_PRECHECK_FMT = "{symbol}: the broker's own precheck refused it - {reason}"
#: NOT per-row: the plan as a whole asks for more buying power than the budget.
#: An advisory rather than a refusal, because the broker fills what it can and
#: the shortfall truncates the SMALLEST buys (they are submitted last).
REFUSAL_OVER_BUDGET_FMT = (
    "the selected orders need ${required:,.2f} of buying power against ${budget:,.2f} "
    "available - the smallest buys will be refused as it runs out")

#: What ``validate_plan_rows`` returns per finding.
PRECHECK_REASON_UNKNOWN = "no reason given"


def validate_plan_rows(plan: "AllocationPlan",
                       margin: Optional[Dict[str, MarginInfo]] = None,
                       impacts: Optional[Dict[str, "OrderImpact"]] = None,
                       ) -> List[Tuple[str, str]]:
    """Which of this plan's orders the broker is expected to REFUSE. Pure.

    The dry run already suppresses the rows the SOLVE knew about, and those never
    reach here -- a suppressed row carries no order and is not tickable. This
    answers the different question the solve cannot: given the broker facts AS
    THEY ARE NOW, which of the orders about to be sent would come back rejected?
    Two things make that different from re-reading the plan:

      * ``MarginInfo.tradable`` is not part of sizing at all, so nothing upstream
        has ever looked at it. A halted or delisted symbol sizes perfectly and is
        refused every time.
      * the facts are re-read at COMMIT time. A plan solved twenty minutes ago
        was sized against the buying power, prices and asset flags of then.

    ``impacts`` is the broker's OWN answer where it has one
    (``AccountInterface.preview_order_impact``): an impact with ``accepted=False``
    is a refusal in the broker's own words and outranks every local guess. Alpaca
    publishes no such endpoint and passes ``None``/``{}``; TastyTrade fills it.

    A missing ``MarginInfo`` (or a ``None`` tri-state field) is NEVER a refusal --
    "the broker did not say" is not "the broker said no", the same rule
    ``fractionable`` has carried since it became tri-state. This function's whole
    value is that a finding here is worth acting on; one false positive that
    un-ticks a good order costs more than the check saves.

    Args:
        plan: the FILTERED plan -- exactly the rows the user has ticked.
        margin: ``{symbol: MarginInfo}`` re-read at validation time.
        impacts: ``{symbol: OrderImpact}`` from the broker's precheck, where it has one.

    Returns:
        List[Tuple[str, str]]: ``(symbol, reason)`` per finding, in plan order.
        EMPTY means nothing local says these orders will be refused -- which is
        not a promise that they will fill, only that nothing known says otherwise.
    """
    margin = margin or {}
    impacts = impacts or {}
    findings: List[Tuple[str, str]] = []
    for row in plan.rows:
        if row.skipped or row.side is None or not row.delta_quantity:
            continue
        symbol = row.symbol
        quantity = abs(float(row.delta_quantity))
        m = margin.get(symbol)

        impact = impacts.get(symbol)
        if impact is not None and not impact.accepted:
            reason = "; ".join(impact.errors) or PRECHECK_REASON_UNKNOWN
            findings.append((symbol, REFUSAL_PRECHECK_FMT.format(
                symbol=symbol, reason=reason)))

        if m is not None and m.tradable is False:
            findings.append((symbol, REFUSAL_NOT_TRADABLE_FMT.format(symbol=symbol)))

        if row.price is None or float(row.price) <= 0:
            findings.append((symbol, REFUSAL_NO_PRICE_FMT.format(symbol=symbol)))

        fractional = _is_fractional_quantity(quantity)
        if fractional and m is not None and m.fractionable is False:
            findings.append((symbol, REFUSAL_FRACTIONAL_NOT_ELIGIBLE_FMT.format(
                symbol=symbol, quantity=quantity)))

        if (m is not None and m.min_order_size is not None
                and quantity < float(m.min_order_size)):
            findings.append((symbol, REFUSAL_BELOW_MIN_ORDER_FMT.format(
                symbol=symbol, quantity=quantity, minimum=float(m.min_order_size))))

        if (fractional and m is not None and m.min_fractional_notional is not None
                and row.price is not None and float(row.price) > 0):
            value = quantity * float(row.price)
            if value < float(m.min_fractional_notional):
                findings.append((symbol, REFUSAL_BELOW_MIN_NOTIONAL_FMT.format(
                    symbol=symbol, value=value,
                    minimum=float(m.min_fractional_notional))))
    return findings


def validate_plan_budget(plan: "AllocationPlan") -> Optional[str]:
    """The one PLAN-level finding: the ticked buys ask for more buying power than
    the budget (published plus what this plan's own sells free). Pure.

    Advisory, not a refusal, and deliberately separate from ``validate_plan_rows``
    for that reason: the broker does not reject the plan, it fills until the money
    runs out. Buys go out in descending value, so what gets refused is the
    SMALLEST ones -- which is worth saying before the user commits, and is not a
    row anybody can un-tick to fix.
    """
    required = float(plan.required_buying_power or 0.0)
    budget = float(plan.total_buying_power or 0.0)
    if required <= budget + MONEY_EPSILON:
        return None
    return REFUSAL_OVER_BUDGET_FMT.format(required=required, budget=budget)


def dry_run_rows(plan: "AllocationPlan") -> List[Dict[str, Any]]:
    """One display dict per row the user must look at, in plan order.

    Included: every row with a NON-ZERO delta (the orders that will be sent), and
    every SUPPRESSED row (see ``_is_suppressed_row`` -- an order that was wanted
    and will not be sent). Excluded: rows already on target and rows skipped for
    want of a price, neither of which represents a decision.

    ``quantity`` and every money figure is a POSITIVE magnitude; the direction is
    in ``side``, which is ``""`` on a suppressed row because no order means no
    side. ``quantity`` is rounded only to ``DRY_RUN_QUANTITY_DECIMALS``, wide
    enough that the figure shown is the figure submitted.

    Two DIFFERENT fractional facts, both needed and routinely confused:

      * ``fractional`` -- the ORDER: its quantity is not a whole number of
        shares. This is the one that carries broker consequences. A fractional
        equity order is MARKET-only (never a limit) at both TastyTrade and
        Alpaca, and it is the only kind the $5 notional floor applies to.
      * ``sized_fractional`` -- the GRID the row was sized on (``AllocationRow.
        fractional``): the toggle was on AND the broker calls the symbol
        fractionable. A row sized on that grid can still land on exactly 5.0
        shares, and that order is an ordinary whole-share order.

    On a suppressed row ``fractional`` is False -- there is no order to describe;
    ``sized_fractional`` and the reason string carry the story instead.

    COST AND VALUE, both, per row. ``current_quantity`` / ``current_cost_basis`` /
    ``current_value`` are the holding the row is trading AGAINST -- what you own,
    what you paid, what it is worth now -- and ``projected_cost`` /
    ``projected_market`` are the same pair AFTER the trade. ``projected_notional``
    is whichever of those two the plan's own ``valuation_mode`` selected, and it is
    kept so the landed column keeps working; the point of emitting both is that one
    basis at a time cannot answer "am I trading against my basis or against the
    market?". ``current_value`` is ``None``, NEVER 0.0, when there is no price: 0.0
    would report a holding as worthless, which is a fabricated live-data fallback.

    ``bp_ratio`` / ``leverage`` come from ``bp_leverage`` -- read its docstring
    before touching either, especially the ``MARGIN_SOURCE_DEFAULT`` guard.
    ``marginable`` / ``initial_margin_rate`` / ``margin_source`` ride along
    unrounded so the display can say WHY it reached its verdict: "the broker lends
    against this" is ``initial_margin_rate < 1.0``, which is a DIFFERENT statement
    from ``bp_ratio > 1.0`` and frequently the opposite one.
    """
    # THE BUDGET, not the published figure: ``bp_usage_pct`` sits three inches
    # under a footer that divides the same thing, and two denominators there is
    # two answers to one question.
    available = float(plan.total_buying_power or 0.0)
    base = float(plan.base_notional or 0.0)
    mode = plan.valuation_mode
    basis = plan.allocation_basis
    out: List[Dict[str, Any]] = []
    for row in plan.rows:
        suppressed = _is_suppressed_row(row)
        if not suppressed and (row.side is None or row.delta_quantity == 0):
            continue
        projected = allocated_value(row, mode, basis)
        projected_cost = allocated_value(row, VALUATION_MODE_COST, basis)
        projected_market = allocated_value(row, VALUATION_MODE_MARKET, basis)
        bp_ratio, leverage = bp_leverage(row)
        out.append({
            "symbol": row.symbol,
            "side": row.side.value if row.side is not None else "",
            "quantity": round(abs(row.delta_quantity), DRY_RUN_QUANTITY_DECIMALS),
            "price": row.price,
            # WHERE THE ROW STARTS: the basis it is trading against. Without these
            # the table's only holding figure is the post-trade projection, so the
            # user cannot see what they already own.
            "current_quantity": round(row.current_quantity, DRY_RUN_QUANTITY_DECIMALS),
            # WHERE THE ROW ENDS UP, beside where it starts, so the Held column can
            # read "7 -> 5.37" instead of making the reader add the signed quantity
            # to the holding in their head (asked for 2026-09-05).
            "projected_quantity": round(row.target_quantity, DRY_RUN_QUANTITY_DECIMALS),
            "current_cost_basis": round(row.current_cost_basis, 2),
            "current_value": (None if row.price is None
                              else round(row.current_quantity * row.price, 2)),
            "estimated_value": round(row.estimated_value, 2),
            # The broker precheck's own fee estimate. None means "not prechecked",
            # never "free" -- there is no fallback for a number nobody published.
            "estimated_fees": row.estimated_fees,
            "bp_cost": round(row.bp_cost, 2),
            # Both POSITIVE magnitudes and mutually exclusive; ``bp_effect`` is
            # the SIGNED one and is what the table's single column draws. A sell
            # showing a bare 0.00 under a "BP cost" heading was the display half
            # of the budget defect -- it reads as "this trade does nothing to your
            # buying power", which is the opposite of what a sale does.
            "bp_released": round(row.bp_released, 2),
            "bp_effect": round(row.bp_effect, 2),
            # SIGNED too, and over the same denominator as the footer: negative
            # is the share of the budget this row consumes, positive the share it
            # hands back.
            "bp_usage_pct": (round(row.bp_effect / available * 100.0, 2)
                             if available > 0 else 0.0),
            # Per-symbol leverage. See bp_leverage: the ratio is the REALISED
            # charge, bp_factor is what the solver assumed, and after a precheck
            # they disagree.
            "bp_factor": row.bp_factor,
            "bp_ratio": None if bp_ratio is None else round(bp_ratio, 4),
            "leverage": leverage,
            "marginable": bool(row.marginable),
            "initial_margin_rate": row.initial_margin_rate,
            "margin_source": row.margin_source,
            # SIZING MODE, not "does the number have a decimal part": at ~25%
            # ineligibility this column is the one the user scans.
            "sizing": "fractional" if row.fractional else "whole",
            # WHICH RULE produced the quantity -- a bumped row holds MORE than the
            # weights asked for, and that must never be silent.
            "outcome": row.sizing_outcome,
            "redistributed": bool(row.redistributed),
            "target_notional": round(row.target_notional, 2),
            # Already reflects whole-share rounding, the bump and the redistribution:
            # what is displayed is what will be owned.
            "projected_notional": None if projected is None else round(projected, 2),
            # The SAME projection measured both ways, so cost and value sit side by
            # side instead of the table silently showing whichever one the global
            # toggle happens to select. Equal to each other in an INVEST_LABEL run,
            # where the target is money to deploy and the mode does not enter.
            "projected_cost": (None if projected_cost is None
                               else round(projected_cost, 2)),
            "projected_market": (None if projected_market is None
                                 else round(projected_market, 2)),
            "residual_notional": (None if projected is None
                                  else round(row.target_notional - projected, 2)),
            # The weight the plan ASKED for and the weight it will really use. They
            # differ whenever the grid, a bump or redistribution moved the row, and
            # showing both is what makes moving it acceptable.
            #
            # NOT "the weight the user typed", which it stopped being the moment the
            # reserve became a stored number: what the user typed is a RELATIVE
            # weight on the investable remainder, and both of these divide the GROSS
            # ``base_notional``, so under a 10% reserve a symbol typed at 50% shows
            # 45%. Deliberate, and the denominator is pinned. ``projected_weight_pct``
            # beside it is a realised post-trade HOLDING with no relative reading at
            # all, so the pair only means "asked -> actual" if both divide the same
            # thing -- and the reserve chip, ``bp_usage_pct`` and ``residual_pct`` on
            # the same screen are all shares of the base too.
            "weight_pct": round(row.target_notional / base * 100.0, 3) if base > 0 else 0.0,
            "projected_weight_pct": (round(projected / base * 100.0, 3)
                                     if base > 0 and projected is not None else 0.0),
            "unmet_notional": round(float(row.unmet_notional or 0.0), 2),
            "reasons": ", ".join(row.reasons),
            "fractional": _is_fractional_quantity(row.delta_quantity),
            "sized_fractional": bool(row.fractional),
            "suppressed": suppressed,
            "skipped": bool(row.skipped),
        })
    return out


def filter_plan_rows(plan: "AllocationPlan", selected_symbols: List[str]) -> "AllocationPlan":
    """A NEW plan holding only the ticked symbols, with the totals recomputed.

    Un-ticking a row must change the buy/sell totals and the buying-power
    requirement the user is about to commit to, so this is what Submit consumes
    -- never the unfiltered plan. ``plan`` is not mutated (the rows are shared by
    reference; nothing here writes to one).

    Every plan-level field that filtering does not change is carried across,
    ``valuation_mode`` included: the filtered plan is what gets persisted into
    ``portfolio_allocation_run.plan_json``, and a cost-basis plan stored without
    its mode reads back as a market one.
    """
    wanted = {s.strip().upper() for s in selected_symbols}
    rows = [r for r in plan.rows if r.symbol.strip().upper() in wanted]

    buy_value = sum(r.estimated_value for r in rows if r.is_buy)
    sell_value = sum(r.estimated_value for r in rows if r.is_sell)
    required = sum(r.bp_cost for r in rows if r.is_buy)
    # RECOMPUTED from the ticked rows, exactly like ``required``. Un-ticking the
    # sell that funds a rebalance takes its money back out of the budget, and the
    # footer says the plan no longer fits BEFORE anything is submitted -- which is
    # the dry run's own answer to "what if that close does not happen?".
    released = sum(r.bp_released for r in rows if r.is_sell)
    budget = float(plan.available_buying_power or 0.0) + released

    return AllocationPlan(
        rows=rows,
        base_notional=plan.base_notional,
        available_buying_power=plan.available_buying_power,
        released_buying_power=released,
        required_buying_power=required,
        bp_usage_pct=(required / budget * 100.0) if budget > 0 else 0.0,
        scale_factor=plan.scale_factor,
        # Not recomputed: it is a property of the BASE (labels that could absorb
        # nothing), which un-ticking a row does not alter. Dropping it would
        # silently report a plan as fully deployed when it never was.
        unallocatable_pct=plan.unallocatable_pct,
        # Same reasoning as ``unallocatable_pct``: the reserve is a property of the
        # TARGETS, which un-ticking a row does not change. Dropping it would report
        # a plan as fully deployed when the user deliberately left cash aside.
        reserved_pct=plan.reserved_pct,
        reserved_notional=plan.reserved_notional,
        total_buy_value=buy_value,
        total_sell_value=sell_value,
        allow_fractional=plan.allow_fractional,
        valuation_mode=plan.valuation_mode,
        allocation_basis=plan.allocation_basis,
        labels=list(plan.labels),
        warnings=list(plan.warnings),
    )


def summarise_plan(plan: "AllocationPlan", *, cash: float) -> Dict[str, float]:
    """Plan-level totals for the dry-run footer.

    ``estimated_cash_after = cash - buys + sells``. It is an ESTIMATE: market
    orders fill at the fill price, not the quoted one, and off-hours orders queue
    until the open.

    Raises:
        ValueError: if ``cash`` is None -- no fallback for a balance. Tested
        against None specifically, never for falsiness: a genuinely empty account
        has ``cash == 0.0`` and that is a real, usable number.
    """
    if cash is None:
        raise ValueError("summarise_plan: cash is None; the broker published no cash balance.")
    return {
        "reserved_pct": plan.reserved_pct,
        "reserved_notional": plan.reserved_notional,
        "total_sell_value": plan.total_sell_value,
        "total_buy_value": plan.total_buy_value,
        "net_buy_value": plan.net_buy_value,
        "required_buying_power": plan.required_buying_power,
        "available_buying_power": plan.available_buying_power,
        "bp_usage_pct": plan.bp_usage_pct,
        "estimated_cash_after": float(cash) - plan.total_buy_value + plan.total_sell_value,
    }


# ---------------------------------------------------------------------------
# Wizard steps 1-3: label percentages, symbol weights, and the INVEST amount.
# ---------------------------------------------------------------------------

ERROR_INVEST_AMOUNT_FMT = "amount {amount:,.2f} must be greater than zero"
ERROR_INVEST_NO_LABEL = "pick a label to invest into"
ERROR_INVEST_LABEL_EMPTY_FMT = (
    "label '{label}' has no symbols - there is nothing to invest into")

#: Spelled out separately, and the WARNING built from it, because
#: ``blocking_messages`` has to recognise this message and it CANNOT do so from a
#: leading prefix: ERROR_INVEST_AMOUNT_FMT starts with the same "amount ", so
#: prefix-matching would silently downgrade a real zero-amount error to advice.
_INVEST_EXCEEDS_BP_FRAGMENT = " exceeds available buying power "
WARNING_INVEST_EXCEEDS_BP_FMT = ("amount {amount:,.2f}" + _INVEST_EXCEEDS_BP_FRAGMENT
                                 + "{available:,.2f} - the plan will be scaled down")

#: Fragments identifying a message that EXPLAINS rather than blocks. Everything
#: the validators produce blocks by default: a new error added without touching
#: this tuple stops Submit, which is the safe direction to be wrong in.
ADVISORY_MESSAGE_FRAGMENTS = (_INVEST_EXCEEDS_BP_FRAGMENT, _SYMBOL_UNDER_FRAGMENT)


def even_split_targets(labels: List[LabelTarget]) -> List[LabelTarget]:
    """The "Even split" button: every label gets an equal share of 100%.

    Returns NEW LabelTarget objects with their own symbol LISTS (the SymbolTarget
    objects inside are shared, which is fine -- step 2 replaces a weight by
    assigning to the object the dialog is already editing), so the caller can
    still cancel out of the dialog without having mutated its inputs. The
    remainder lands on the LAST label so the set totals exactly 100.

    ALWAYS 100, independent of the reserve. A ``total_pct`` knob existed briefly,
    to stop an even split wiping a reserve that was then DERIVED from the label
    shortfall; the reserve is stored separately now, so there is nothing here to
    preserve and any total but 100 would produce a set the validator refuses.
    """
    items = list(labels or [])
    if not items:
        return []
    return [LabelTarget(label=lt.label, target_pct=pct, symbols=list(lt.symbols),
                        comment=lt.comment,
                        previous_target_pct=lt.previous_target_pct)
            for lt, pct in zip(items, even_split_pct(len(items)))]


def has_previous_targets(labels: Optional[List[LabelTarget]]) -> bool:
    """True when ANY label has a previous target -- the Load-last button's state.

    Tested with ``is not None`` and never for truthiness: a previous target of 0.0
    is a real prior state (the engine reads 0 as "hold none of this") and a button
    that refuses to restore it would be refusing to undo the user's last change.
    """
    return any(lt.previous_target_pct is not None for lt in (labels or []))


def load_previous_targets(labels: Optional[List[LabelTarget]]) -> List[LabelTarget]:
    """The "Load last" button: restore the percentages the last run used. Pure.

    The mirror of ``even_split_targets``, and it copies on exactly the same terms:
    NEW ``LabelTarget`` objects with their own symbol LISTS (the ``SymbolTarget``
    objects are shared), so the caller can still cancel out of the dialog without
    having mutated its inputs.

    A label with NO history keeps the target it already has. A partial history is
    the ORDINARY state -- a label added yesterday has none -- and zeroing those
    would silently unallocate a real basket the moment the button was pressed.

    ``previous_target_pct`` is carried across unchanged, so the button stays usable
    and pressing it twice is idempotent rather than self-erasing.
    """
    items = list(labels or [])
    if not items:
        return []
    return [LabelTarget(
        label=lt.label,
        target_pct=(lt.target_pct if lt.previous_target_pct is None
                    else float(lt.previous_target_pct)),
        symbols=list(lt.symbols), comment=lt.comment,
        previous_target_pct=lt.previous_target_pct) for lt in items]


def has_previous_symbol_weights(label: Optional[LabelTarget]) -> bool:
    """True when ANY symbol in this label has a previous weight. Per-label button."""
    if label is None:
        return False
    return any(st.previous_weight_pct is not None for st in (label.symbols or []))


def load_previous_symbol_weights(label: LabelTarget) -> LabelTarget:
    """The per-label "Load last": restore ONE label's symbol weights. Pure.

    Returns a NEW ``LabelTarget`` with NEW ``SymbolTarget`` objects -- unlike
    ``load_previous_targets``, which shares them, because this is the function that
    changes them. The label's own ``target_pct`` is untouched: step 2 is about
    weights WITHIN a label and must not silently move the label itself.

    A symbol with no history keeps the weight it already has, for the same reason a
    label with no history does.
    """
    return LabelTarget(
        label=label.label, target_pct=label.target_pct, comment=label.comment,
        previous_target_pct=label.previous_target_pct,
        symbols=[SymbolTarget(
            symbol=st.symbol,
            weight_pct=(st.weight_pct if st.previous_weight_pct is None
                        else float(st.previous_weight_pct)),
            comment=st.comment, previous_weight_pct=st.previous_weight_pct)
            for st in (label.symbols or [])])


def even_split_symbol_weights(label: LabelTarget) -> LabelTarget:
    """The per-label "Even split": ONE label's symbols share its 100% equally. Pure.

    ``even_split_targets`` does this for the LABELS; this is its step-2 pair, and
    the two are deliberately built the same way -- both defer the arithmetic to
    ``even_split_pct``, so the remainder lands on the last slot and the set totals
    exactly 100. That delegation is the whole point rather than a detail: a
    hand-rolled ``round(100 / n, 2)`` agrees at n=2, 3 and 5 and then parts company
    at n=6 (16.67 against 16.66, last slot 16.65 against 16.70), which is both a
    set ``validate_symbol_weights`` refuses and a set that disagrees with the
    stored default ``build_symbol_targets`` hands the very same symbols.

    Returns a NEW ``LabelTarget`` with NEW ``SymbolTarget`` objects, on exactly the
    terms ``load_previous_symbol_weights`` uses: this is the function that changes
    them, so the caller can still cancel out of the dialog. ``comment`` and
    ``previous_weight_pct`` are carried across -- the latter is what the Load-last
    button beside it reads, and dropping it would disable that button as a side
    effect of pressing this one.

    The label's own ``target_pct`` is untouched. Step 2 is about weights WITHIN a
    label, and those have always been relative to their own label: the reserve and
    the label percentages divide the base ABOVE this level and no scaling of them
    reaches these numbers.

    An empty label comes back empty rather than raising -- ``even_split_pct(0)``
    is ``[]``.
    """
    symbols = list(label.symbols or [])
    return LabelTarget(
        label=label.label, target_pct=label.target_pct, comment=label.comment,
        previous_target_pct=label.previous_target_pct,
        symbols=[SymbolTarget(symbol=st.symbol, weight_pct=pct, comment=st.comment,
                              previous_weight_pct=st.previous_weight_pct)
                 for st, pct in zip(symbols, even_split_pct(len(symbols)))])


def can_even_split_symbols(label: Optional[LabelTarget]) -> bool:
    """True when a label has two or more symbols -- the per-label button's state.

    The mirror of ``has_previous_symbol_weights``, and its button is DISABLED
    rather than hidden for the same reason: a control that vanishes is one the user
    cannot learn exists. Below two there is nothing to spread -- an empty label has
    no symbols and a single symbol already owns the whole 100 by construction, so
    the button would be a no-op on the only set that label can legally hold.
    """
    if label is None:
        return False
    return len(label.symbols or []) > 1


def _symbol_weight(target: SymbolTarget) -> float:
    """One symbol's weight as a number. ``None`` is 0.0.

    ``SymbolTarget.weight_pct`` is declared ``float``, but the wizard's setter
    builds it with ``float(value or 0.0)`` from a ``ui.number`` that yields ``None``
    when the box is cleared, and ``validate_symbol_weights`` already reads it
    through the same ``or 0.0``. "Unset" and 0 are one fact here; this is the
    single place that says so.
    """
    return float(target.weight_pct or 0.0)


def _fill_remainder_pct(label: LabelTarget) -> float:
    """What is left of this label's 100 after the weights the user typed.

    Rounded to the CENT before anything looks at it, and that rounding is the
    point rather than tidiness: 33.33 + 33.33 + 33.34 is exactly 100 in decimal and
    99.99999999999999 in binary, and without the round a fully-spent label would
    offer to fill a slot with a hundredth of nothing. Can be negative -- the
    callers decide what to do about that, and they do not agree.
    """
    return round(100.0 - sum(_symbol_weight(st) for st in (label.symbols or [])), 2)


def fill_remaining_symbol_weights(label: LabelTarget) -> LabelTarget:
    """The per-label "Fill rest evenly": spread what is left over the EMPTY slots.

    The workflow it exists for is "type the two you care about, let the rest sort
    themselves out". Every symbol carrying a non-zero weight is left EXACTLY as
    typed -- not re-normalised, not nudged onto the cent grid -- and what is left of
    the label's 100 is divided among the symbols sitting at 0 or unset.

    A 0 is therefore FILLABLE, not a deliberate "sell this position out to nothing".
    That is a real cost and it was accepted knowingly: the alternative is a
    per-symbol lock, which is a second piece of state on every row to protect a
    case ``Wipe`` and a retype already cover. ``Wipe`` is the other half of this
    feature for exactly that reason.

    The arithmetic is ``split_pct_across``, which is ``even_split_pct``, which is
    what ``build_symbol_targets`` fills an untouched label in with -- so filling a
    COMPLETELY empty label produces the identical numbers to pressing Even split,
    at every count. That equivalence is not a coincidence to be re-checked, it is
    the same function called with a total of 100.

    With nothing left to give (the typed weights already total 100 or more) the
    empty slots are written 0.0 rather than a share of a negative. The UI never
    reaches that branch -- ``can_fill_remaining_symbol_weights`` is False there, so
    the button is disabled and the validator says why -- but a pure function that
    only behaves while its own predicate agrees is a trap for the next caller.

    A NEGATIVE weight is a value, not an empty slot, so it survives untouched and
    inflates the remainder. Silently rewriting it would be this button repairing an
    error the user needs to see; ``validate_symbol_weights`` owns that message.

    Returns a NEW ``LabelTarget`` with NEW ``SymbolTarget`` objects, on exactly the
    terms ``even_split_symbol_weights`` uses, carrying ``comment`` and
    ``previous_weight_pct`` across -- the latter is what the Load-last button beside
    it reads. The label's own ``target_pct`` is untouched: step 2 is about weights
    WITHIN a label.
    """
    symbols = list(label.symbols or [])
    empty = [i for i, st in enumerate(symbols) if _symbol_weight(st) == 0.0]
    remainder = _fill_remainder_pct(label)
    shares = (split_pct_across(remainder, len(empty)) if remainder > 0
              else [0.0] * len(empty))
    filled = {i: pct for i, pct in zip(empty, shares)}
    return LabelTarget(
        label=label.label, target_pct=label.target_pct, comment=label.comment,
        previous_target_pct=label.previous_target_pct,
        symbols=[SymbolTarget(symbol=st.symbol,
                              weight_pct=filled.get(i, st.weight_pct),
                              comment=st.comment,
                              previous_weight_pct=st.previous_weight_pct)
                 for i, st in enumerate(symbols)])


def can_fill_remaining_symbol_weights(label: Optional[LabelTarget]) -> bool:
    """True when there is a slot to fill AND something left to fill it with.

    Both halves are required, and the second is why this is not simply "has an
    empty box". A label whose typed weights already total 100 or more has no
    remainder to hand out: filling would either write zeros over zeros (a click
    that visibly does nothing) or negatives (a set no validator will pass). The
    button is DISABLED there rather than made a silent no-op, on
    ``can_even_split_symbols``' terms -- a control that does nothing when pressed
    is indistinguishable from a broken one -- and the validator underneath already
    names the real problem, that the label totals more than 100.

    The user is never cornered by that: ``can_wipe_symbol_weights`` is True in
    exactly the over-allocated case, so Wipe is always the way out.
    """
    if label is None or not label.symbols:
        return False
    if not any(_symbol_weight(st) == 0.0 for st in label.symbols):
        return False
    return _fill_remainder_pct(label) > 0


def wipe_symbol_weights(label: LabelTarget) -> LabelTarget:
    """The per-label "Wipe": clear ONE label's symbol weights and start over.

    What makes ``fill_remaining_symbol_weights`` coherent. Filling treats a 0 as an
    empty slot, so the honest way to redo a label is to empty it outright, type the
    handful that matter and fill the rest -- rather than hunt down whichever old
    weights are still lurking in the boxes below the fold.

    Writes 0.0, not ``None``. ``SymbolTarget.weight_pct`` is declared ``float`` and
    every solver does arithmetic on it; the wizard's own setter already turns a
    cleared box into 0.0, so 0.0 IS "empty" in this model and ``None`` would buy a
    marginally emptier looking box at the cost of the field's type being a lie.

    ``previous_weight_pct`` and ``comment`` are carried across, and here that is
    load-bearing rather than merely consistent: Load last is the control that UNDOES
    a wipe, and a wipe that cleared the history would disable its own undo. Between
    that, Even split, Fill rest and a dialog whose Cancel discards the whole edited
    copy, the wipe is cheap enough to reverse that it is not worth a confirmation
    step -- unlike removing a label from the managed set, which changes a stored,
    cross-run fact.

    The label's own ``target_pct`` does not move: step 2 is about weights WITHIN a
    label.
    """
    return LabelTarget(
        label=label.label, target_pct=label.target_pct, comment=label.comment,
        previous_target_pct=label.previous_target_pct,
        symbols=[SymbolTarget(symbol=st.symbol, weight_pct=0.0, comment=st.comment,
                              previous_weight_pct=st.previous_weight_pct)
                 for st in (label.symbols or [])])


def can_wipe_symbol_weights(label: Optional[LabelTarget]) -> bool:
    """True when this label has a weight worth destroying -- the button's state.

    A label already at all-zero has nothing to clear and a label with no symbols has
    no boxes at all; both draw the button DISABLED rather than hidden, on the same
    terms as the three controls beside it.
    """
    if label is None:
        return False
    return any(_symbol_weight(st) != 0.0 for st in (label.symbols or []))


def steps_validation_messages(labels: List[LabelTarget], *,
                              unallocated_pct: float = 0.0,
                              tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Every reason a REBALANCE cannot proceed, from step 1 AND step 2. Pure.

    Step 1's rule (labels total 100, no duplicates, no negatives, no non-zero
    label without symbols) and step 2's (weights total 100 inside each label, no
    negatives, no symbol repeated in one label) are BOTH already covered by
    ``validate_label_targets``, which delegates the symbol half to
    ``validate_symbol_weights``. This is a named seam for the wizard, not a second
    implementation: re-checking the symbol totals here emitted every one of them
    twice.

    ``unallocated_pct`` is the reserve box on step 1 and is checked HERE rather
    than inside ``validate_label_targets``: it is not a label, it does not enter
    the label total, and folding it in is exactly the arithmetic the user is not
    supposed to do. Defaults to 0 so an INVEST-side or test caller that has no
    reserve says nothing about one.

    Returns:
        List[str]: EMPTY means the wizard may proceed to the dry-run.
    """
    return (validate_label_targets(labels, tolerance=tolerance)
            + validate_unallocated_pct(unallocated_pct))


def validate_invest_amount(amount: float, *, available_buying_power: float) -> List[str]:
    """Validate an INVEST_LABEL amount. Pure -- returns problems, never raises.

    A zero or negative amount is an ERROR. An amount above available buying power
    is reported too, but as an explanation rather than a hard block: the engine
    scales the plan down pro-rata and the dry-run shows the result, which is more
    useful than refusing to compute it. Use ``blocking_messages`` to tell the two
    apart -- do not test the strings by hand.
    """
    messages: List[str] = []
    value = float(amount or 0.0)
    if value <= 0:
        messages.append(ERROR_INVEST_AMOUNT_FMT.format(amount=value))
        return messages
    available = float(available_buying_power or 0.0)
    if value > available:
        messages.append(WARNING_INVEST_EXCEEDS_BP_FMT.format(
            amount=value, available=available))
    return messages


def invest_validation_messages(label: Optional[LabelTarget], amount: float, *,
                               available_buying_power: float,
                               tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Every reason an INVEST_LABEL run cannot proceed. Pure.

    The amount is NOT the only thing that can be wrong, and this is the half that
    is easy to forget. ``compute_label_investment`` multiplies the label's symbol
    weights straight through, so a hand-edited 150% set turns a 10,000 budget into
    15,000 of buys and a 60% set silently leaves 40% of it as cash. Neither is
    something ``validate_invest_amount`` can see.

    ``validate_label_targets`` cannot be reused on this path: a single chosen
    label at 40% would fail its labels-total-100 rule spuriously. Task 23's
    ``validate_symbol_weights`` exists for exactly this -- symbol level only,
    ``target_pct`` deliberately ignored.

    Returns:
        List[str]: EMPTY means the wizard may proceed to the dry-run. May contain
        the buying-power ADVISORY, which explains rather than blocks; filter with
        ``blocking_messages``.
    """
    if label is None:
        return [ERROR_INVEST_NO_LABEL]
    messages: List[str] = []
    if not label.symbols:
        # An empty label absorbs nothing, so the whole amount would sit as cash
        # and the run would do literally nothing. Say so instead.
        messages.append(ERROR_INVEST_LABEL_EMPTY_FMT.format(label=label.label))
    messages.extend(validate_symbol_weights(label, tolerance=tolerance))
    messages.extend(validate_invest_amount(
        amount, available_buying_power=available_buying_power))
    return messages


def is_blocking_message(message: str) -> bool:
    """True when this validation message must stop Submit. Pure.

    Blocking is the DEFAULT: only the fragments in ``ADVISORY_MESSAGE_FRAGMENTS``
    are advisory, so a validator gaining a new error keeps Submit blocked without
    anyone remembering to update this.
    """
    return not any(fragment in message for fragment in ADVISORY_MESSAGE_FRAGMENTS)


def blocking_messages(messages: List[str]) -> List[str]:
    """The subset of ``messages`` that must stop Submit. Pure.

    EMPTY means the wizard may proceed even though ``messages`` may not be: an
    amount over buying power is worth showing and not worth refusing.
    """
    return [m for m in messages or [] if is_blocking_message(m)]


# ---------------------------------------------------------------------------
# Submission decisions. Pure -- the live service does the IO around them.
# ---------------------------------------------------------------------------

ACTION_ADJUST = "adjust"   # held, target > 0, delta != 0 -> adjust_quantity_with_tpsl
ACTION_CLOSE = "close"     # held, target == 0            -> close_transaction
ACTION_NEW = "new"         # not held, target > 0         -> new TradingOrder
ACTION_SKIP = "skip"       # nothing to do (or nothing we are willing to do)
#: Held at the broker, and every open transaction behind it is one this planner
#: does not act on -- so the SELL cannot be routed at all. NOT a skip: see
#: ``decide_symbol_action``.
ACTION_UNACTIONABLE = "unactionable"


def decide_symbol_action(row: "AllocationRow", state: Optional["PositionState"]) -> str:
    """Which of the four submission paths this row takes (decision 14). Pure.

    Long-only: a SELL on a symbol we do not hold would open a short, so it is
    skipped rather than submitted. A row that the engine already marked
    ``skipped`` (no price, precheck rejected) is never traded, and neither is a
    row whose delta was zeroed -- a suppressed trim HOLDS the position, it does
    not become a liquidation.

    "Held" means held BY US: a position with no open Transaction of ours cannot
    be adjusted, because the adjust path resizes a transaction. A BUY there opens
    a fresh transaction; a SELL is refused rather than trimming a position this
    platform does not track.

    ``ACTION_UNACTIONABLE`` splits that last refusal in two. A SELL on a symbol
    with an EMPTY ``transaction_ids`` used to be one case -- "we do not track
    this position" -- and it now has a second, created by the caller's own
    filtering: the transactions exist, we DO track them, and they were held back
    because this planner will not act on them (live: option-classed, per
    ``PositionState.unactionable_transaction_ids``). An assigned wheel is exactly
    that shape, so it is not a corner case. Reporting it as ACTION_SKIP told the
    operator "nothing to do" about shares they had just asked to sell -- the same
    words a symbol already at target gets. The caller must say something else.

    BUY is deliberately untouched by it: topping a holding up submits
    ``row.delta_quantity`` through a brand new equity transaction, which is
    correct and cannot double-buy. Only the SELL side has nowhere to go.

    ``held`` still wins over it, so a symbol with one equity transaction and one
    option transaction closes or trims the equity exactly as before.
    """
    if row.skipped or row.side is None or row.delta_quantity == 0:
        return ACTION_SKIP

    # ONE reading of "there is a position here", shared by both tests below, so
    # they can never come to disagree about what that means.
    at_the_broker = state is not None and (state.quantity or 0.0) > 0

    held = at_the_broker and bool(state.transaction_ids)
    if held:
        return ACTION_CLOSE if row.target_quantity <= 0 else ACTION_ADJUST

    if (row.side == OrderDirection.SELL and at_the_broker
            and bool(state.unactionable_transaction_ids)):
        return ACTION_UNACTIONABLE

    return ACTION_NEW if row.side == OrderDirection.BUY else ACTION_SKIP


def split_delta_fifo(
    delta_quantity: float,
    transaction_quantities: List[Tuple[int, float]],
) -> List[Tuple[int, float]]:
    """Spread a signed delta across a symbol's open transactions, oldest first. Pure.

    Args:
        delta_quantity: SIGNED. Negative trims, positive adds.
        transaction_quantities: ``[(transaction_id, quantity)]``, ALREADY sorted
            oldest first.

    Returns:
        List[Tuple[int, float]]: ``[(transaction_id, signed_qty_change)]``, only
        for transactions actually touched. A trim consumes them FIFO and is
        CLAMPED to what is actually held (never oversells), stops at the first
        transaction that covers it, and skips an empty one -- every extra
        transaction touched is an extra broker order and an extra TP/SL rebuild.
        An add lands entirely on the OLDEST transaction, so the account keeps one
        transaction per symbol (decision 14). Empty when there is nothing to do.

    CALLER CONTRACT -- a trim leg may EXACTLY EXHAUST its transaction, and by
    construction it does whenever the trim spans more than one of them (30 shares
    held as 20 + 10, sell 25 -> ``[(t1, -20), (t2, -5)]``: the first leg takes all
    of t1). Such a leg is a CLOSE, not an adjustment, and must be routed to
    ``close_transaction``. ``TransactionHelper.adjust_quantity_with_tpsl`` is a
    PARTIAL-close facility and refuses ``close_qty >= current_qty`` outright, so
    handing it an exhausting leg silently under-sells by that leg's whole size and
    the position can never converge on the target the dry run promised.

    The clamp is deliberately NOT relaxed to leave a share behind on each
    transaction: dust remainders are un-closeable positions, they can fall under
    the broker's minimum order size or its fractional notional floor, and they
    would make the quantity submitted differ from the quantity the user approved.
    """
    if not transaction_quantities or delta_quantity == 0:
        return []

    if delta_quantity > 0:
        return [(transaction_quantities[0][0], float(delta_quantity))]

    remaining = abs(float(delta_quantity))
    out: List[Tuple[int, float]] = []
    for txn_id, quantity in transaction_quantities:
        if remaining <= 0:
            break
        available = float(quantity or 0.0)
        if available <= 0:
            continue
        take = min(available, remaining)
        out.append((txn_id, -take))
        remaining -= take
    return out


# ---------------------------------------------------------------------------
# Fractional shares: which quantities to attempt, and in what order (decision 12).
# ---------------------------------------------------------------------------

FRACTIONAL_PATH_FRACTIONAL = "fractional"
FRACTIONAL_PATH_WHOLE = "whole"


def plan_quantity_attempts(
    quantity: float,
    *,
    allow_fractional: bool,
    fractionable: bool,
) -> List[Tuple[str, float]]:
    """The ordered submission attempts for one order quantity. Pure.

    Fractional ON, symbol fractionable and the quantity really is fractional ->
    try the fractional quantity first, then ONE retry at ``floor(quantity)``.
    Anything else goes straight to whole shares.

    ONE retry, deliberately: a loop that kept shrinking would walk a rejected
    order down to a single share and buy something nobody reviewed.

    "Really is fractional" is ``_is_fractional_quantity``, not ``q > floor(q)``.
    A quantity off a fractional grid lands at 2.9999999999 as readily as at
    3.0000000001; both ARE three shares, and flooring the low one would send two
    where the dry run showed three.

    A whole-share attempt is always an exact whole number, which is the whole
    point of it: a fractional equity order is MARKET-only at both TastyTrade
    (``fractional_market_orders_only``) and Alpaca, and it is the only kind the
    broker's fractional notional floor applies to.

    Returns:
        List[Tuple[str, float]]: ``[(path, quantity)]``, first attempt first.
        EMPTY when there is nothing sendable -- ``floor(quantity)`` is 0 and
        fractional is unavailable, or the quantity is 0. The caller reports that
        as SKIPPED, not as a failure.
    """
    magnitude = abs(float(quantity))

    if not _is_fractional_quantity(magnitude):
        settled = float(round(magnitude))
        return [(FRACTIONAL_PATH_WHOLE, settled)] if settled > 0 else []

    whole = float(math.floor(magnitude))
    if allow_fractional and fractionable:
        attempts = [(FRACTIONAL_PATH_FRACTIONAL, magnitude)]
        if whole > 0:
            attempts.append((FRACTIONAL_PATH_WHOLE, whole))
        return attempts

    return [(FRACTIONAL_PATH_WHOLE, whole)] if whole > 0 else []


# ---------------------------------------------------------------------------
# D2: the label rounding residual, redistributed inside the label, both ways.
#
# Whole-share rounding leaves a label short; D1's bumps can push it long. One
# signed pass moves the difference onto the symbols that can absorb it. Pure.
# ---------------------------------------------------------------------------


def projected_value(row: "AllocationRow", valuation_mode: str) -> Optional[float]:
    """The POST-TRADE value of one row, measured the same way as a REBALANCE target.

    This is what makes "did the plan hit its target?" answerable: both sides of the
    comparison are in the same unit. ``market`` mode projects
    ``target_quantity * price``; ``cost`` mode projects the post-trade COST BASIS,
    removing average cost on a sell exactly as ``compute_allocation`` adds price on
    a buy -- the two legs convert at different rates and mixing them is the bug that
    once turned a half-trim into a liquidation.

    ``target_quantity`` is already the post-trade holding AFTER whole-share rounding
    and after any redistribution, so this number is what the account will really be
    worth, not an ideal the rounding never reaches.

    Returns:
        Optional[float]: ``None`` when the row has no usable price -- nothing can be
        measured and no fallback price may be invented (platform rule).

    Raises:
        ValueError: on an unknown ``valuation_mode``.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    if row.price is None or row.price <= 0:
        return None
    if valuation_mode == VALUATION_MODE_MARKET:
        return float(row.target_quantity) * float(row.price)
    delta = float(row.delta_quantity or 0.0)
    basis = float(row.current_cost_basis or 0.0)
    if delta >= 0:
        return basis + delta * float(row.price)
    if row.current_quantity <= 0:
        return basis
    return basis * (float(row.target_quantity) / float(row.current_quantity))


def allocated_value(row: "AllocationRow", valuation_mode: str,
                    allocation_basis: str) -> Optional[float]:
    """What this row ACTUALLY allocates, measured the way its plan's target means it.

    ``ALLOCATION_BASIS_POSITION`` (a REBALANCE) -> ``projected_value``: the target is
    a post-trade holding value. ``ALLOCATION_BASIS_BUDGET`` (an INVEST_LABEL run) ->
    the money this row deploys, because the target is money to ADD on top of an
    existing holding and the post-trade value would double-count what is already
    owned.

    Returns:
        Optional[float]: ``None`` when the row has no usable price.

    Raises:
        ValueError: on an unknown basis or valuation mode.
    """
    if allocation_basis == ALLOCATION_BASIS_POSITION:
        return projected_value(row, valuation_mode)
    if allocation_basis != ALLOCATION_BASIS_BUDGET:
        raise ValueError(
            f"Unknown allocation_basis {allocation_basis!r}; expected "
            f"{ALLOCATION_BASIS_POSITION!r} or {ALLOCATION_BASIS_BUDGET!r}")
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(f"Unknown valuation_mode {valuation_mode!r}")
    if row.price is None or row.price <= 0:
        return None
    return float(row.delta_quantity or 0.0) * float(row.price)


def _label_residual(target_total: float, members: List["AllocationRow"],
                    valuation_mode: str, allocation_basis: str) -> float:
    """SIGNED money the label is off its target: + is short, - is over."""
    total = 0.0
    for row in members:
        value = allocated_value(row, valuation_mode, allocation_basis)
        if value is not None:
            total += value
    return float(target_total) - total


def _row_is_buying(row: "AllocationRow", residual: float) -> bool:
    """Which SIDE the row will be on once ``_absorb_residual`` has moved it.

    The conversion rate belongs to the ROW, never to the label's residual, and the
    two disagree constantly: closing a shortfall by selling LESS moves a row that is
    still a SELL, and closing an overshoot by buying LESS moves a row that is still a
    BUY. Mirrors ``_absorb_residual``'s four branches exactly, and it is safe to
    decide this BEFORE the move because none of those branches may flip a row's sign:
    the two "never delete the order" clamps stop one whole unit short of zero.
    """
    current = float(row.delta_quantity or 0.0)
    return current >= 0 if residual > 0 else current > 0


def _absorption_unit_money(row: "AllocationRow", valuation_mode: str,
                           allocation_basis: str, *, buying: bool) -> float:
    """How much the LABEL's measured total moves per share moved on this row.

    ``buying`` is the side of the ROW being moved (``_row_is_buying``), NOT the sign
    of the label's residual: a row selling less to close a shortfall is still a sell
    and still converts at a sell's rate.

    Not always the price. In ``cost`` mode on a position basis, buying a share adds
    the market PRICE to the basis but selling one removes the AVERAGE COST, exactly
    as in ``compute_allocation``. Using the price for a cost-mode reduction
    under-counts by ``price / avg_cost`` and the label never converges.
    """
    price = float(row.price)
    if allocation_basis == ALLOCATION_BASIS_BUDGET:
        return price
    if valuation_mode == VALUATION_MODE_MARKET or buying:
        return price
    qty = float(row.current_quantity or 0.0)
    if qty <= 0:
        return price
    avg = float(row.current_cost_basis or 0.0) / qty
    return avg if avg > 0 else price


def _buying_power_headroom(plan: "AllocationPlan") -> float:
    """Buying power the plan is NOT already spending. Recomputed after every move.

    Reads the same BUDGET ``_apply_bp_scaling`` does -- published buying power
    plus what the plan's sells free -- so redistribution and the scaler cannot
    disagree about how much room there is. ``plan.total_buying_power`` is not used
    here because it is only refreshed by ``_finalise_totals``, which has not run
    yet while redistribution is still moving rows.
    """
    return (float(plan.available_buying_power or 0.0)
            + sum(r.bp_released for r in plan.rows if r.is_sell)
            - sum(r.bp_cost for r in plan.rows if r.is_buy))


def _absorb_residual(row: "AllocationRow", residual: float,
                     margin: Optional[MarginInfo], *, allow_fractional: bool,
                     valuation_mode: str, allocation_basis: str,
                     bp_headroom: float) -> float:
    """Move ONE row by whole grid units toward closing ``residual``. Pure arithmetic.

    Returns the SIGNED quantity to move (0.0 when nothing may move). Every cap is
    expressed in UNITS, so the result is always back on the row's own grid and never
    needs re-rounding.

    The step count is FLOORED: a step can close the gap but never cross it. That is
    what makes ``|residual|`` monotone and the outer loop finite, and it is also why
    redistribution cannot create a new overshoot.
    """
    price = float(row.price or 0.0)
    unit = tradeable_unit(margin, allow_fractional=allow_fractional)
    if price <= 0 or unit <= 0:
        return 0.0
    buying = residual > 0
    # The DIRECTION of the move comes from the residual; the RATE comes from the
    # row. In cost mode on a position basis those differ whenever the row's own side
    # disagrees with the residual's sign, and converting at the label's sign moves
    # every share at the wrong price.
    unit_money = _absorption_unit_money(row, valuation_mode, allocation_basis,
                                        buying=_row_is_buying(row, residual))
    if unit_money <= 0:
        return 0.0
    steps = math.floor(abs(residual) / (unit_money * unit) + QUANTITY_EPSILON)
    if steps < 1:
        return 0.0
    current = float(row.delta_quantity or 0.0)
    if buying:
        if current < 0:
            # Selling LESS. Stop one unit short of deleting the order: a row the user
            # reviewed must not vanish because a sibling rounded badly.
            steps = min(steps, int(math.floor(abs(current) / unit + QUANTITY_EPSILON)) - 1)
        else:
            # Buying MORE. Never past the buying power the plan has left.
            cost_per_unit = unit * price * float(row.bp_factor or 1.0)
            if cost_per_unit <= 0:
                return 0.0
            steps = min(steps, int(math.floor(
                max(0.0, bp_headroom) / cost_per_unit + QUANTITY_EPSILON)))
    else:
        if current > 0:
            # Buying LESS, again never to zero.
            steps = min(steps, int(math.floor(current / unit + QUANTITY_EPSILON)) - 1)
        elif allocation_basis == ALLOCATION_BASIS_BUDGET:
            # An INVEST_LABEL run DEPLOYS money and never sells -- the contract
            # ``compute_label_investment`` states and everything downstream reads
            # (``total_sell_value == 0``, ``net_buy_value == total_buy_value``, and
            # so the income ledger's consumption). A bump can push such a label over
            # its budget, and the honest answer is to buy less somewhere -- opening
            # a SELL on a position the user never mentioned, to give back 50 of
            # rounding, realises a gain and is a different trade. Reported instead.
            return 0.0
        else:
            # Selling MORE. Never below flat, and never all the way to flat: closing
            # a position is a target decision, not a rounding fix. One unit stays.
            room = float(row.current_quantity or 0.0) + current - unit
            steps = min(steps, int(math.floor(max(0.0, room) / unit + QUANTITY_EPSILON)))
    if steps < 1:
        return 0.0
    move = (1.0 if buying else -1.0) * steps * unit
    proposed = round(current + move, 10)
    min_size = None if margin is None else margin.min_order_size
    if min_size is not None and 0 < abs(proposed) < float(min_size):
        # The broker would refuse the order the move produces, so there is no move.
        return 0.0
    if _below_fractional_notional_floor(proposed, margin, price):
        # Same rule, the other unit: a fractional order under the broker's money
        # floor is refused outright, so producing one is not an option either.
        return 0.0
    return round(move, 10)


def _apply_absorption(row: "AllocationRow", move: float, label: str,
                      baseline: float) -> None:
    """Write an absorbed move onto a row, and SAY SO in one reason, not two.

    ``bp_cost`` is recomputed from the estimate rather than scaled, which is correct
    here and only here: redistribution runs inside the two solvers, before any broker
    precheck has substituted a cost.
    """
    after = round(float(row.delta_quantity or 0.0) + move, 10)
    row.delta_quantity = after
    row.target_quantity = float(row.current_quantity or 0.0) + after
    row.estimated_value = abs(after) * float(row.price)
    factor = float(row.bp_factor or 1.0)
    # BOTH, because an absorption can flip a row's side: a buy trimmed past zero
    # becomes a sell, and a stale ``bp_released`` on a row that is now buying
    # would credit the budget with money nobody is freeing.
    row.bp_cost = row.estimated_value * factor if after > 0 else 0.0
    row.bp_released = row.estimated_value * factor if after < 0 else 0.0
    row.side = (OrderDirection.BUY if after > 0
                else OrderDirection.SELL if after < 0 else None)
    row.redistributed = True
    # One reason per row, always quoting the ORIGINAL quantity, however many passes
    # touched it -- two half-truths side by side is worse than no reason at all.
    row.reasons = [r for r in row.reasons if not r.startswith(REASON_REDISTRIBUTED_PREFIX)]
    row.reasons.append(REASON_REDISTRIBUTED_FMT.format(
        before=baseline, after=after, label=label))


def _absorber_order(members: List["AllocationRow"],
                    wanted: Optional[Dict[str, float]] = None,
                    ) -> List["AllocationRow"]:
    """The rows a label may move, best first, deterministically.

    FRACTIONABLE first (they absorb the residual almost exactly, on a 4dp grid,
    instead of in whole-share lumps), then rows that are already trading (adjusting
    an order the user has reviewed is less surprising than inventing a new one), then
    by symbol so two identical plans produce two identical results.

    EXCLUDED, and this is the rule that makes D1 and D2 compatible:
      * a row D1 BUMPED -- the only way to absorb an overshoot on it is to sell it
        back to zero, which re-creates the floor-to-zero D1 exists to remove, in a
        loop. Bumps are one-way;
      * a row the grid or the broker minimum already refused (``unmet_notional`` set,
        or ``SIZING_OUTCOME_SKIPPED_TOO_LARGE``) -- it already says "no order" on its
        face, and giving it one anyway makes the two halves of the row contradict
        each other;
      * a skipped or unpriced row -- there is nothing to measure or to trade;
      * a row being CLOSED to a zero target (``REASON_CLOSE_TO_ZERO``) -- decision
        14 flattens such a position outright, and absorbing into it means selling
        LESS than everything, so the position it was told to flatten survives at
        a residual size that nobody chose. "Sell it all" is an instruction, not a
        rounding a later pass may trim;
      * a symbol this LABEL wants NONE of, that is NOT HELD, and that has NO
        ORDER YET -- a 0%-weighted member the account is flat in. Absorbing into
        one opens a position out of nothing, on a row that had nothing to do
        until redistribution invented it, and ``scale_pct_to_total``'s own rule
        is that a slot at 0 is a symbol the user asked to hold NONE of.

        ALL THREE halves are required, and each excludes a legitimate absorber
        on its own: "wants none" alone would exclude the ordinary over-target
        seller (a HELD symbol whose target is 0 is exactly what an
        over-subscribed label gives back into); "not held" alone would exclude
        every legitimate new buy; "no order yet" alone would exclude every row
        being shrunk. Together they name one thing only -- a row that would go
        from nothing at all to an order nobody asked for.

        ``wanted`` is the LABEL'S OWN SPLIT rather than ``row.target_notional``
        because a multi-label symbol's row carries the SUM of its labels'
        targets, so the row-level figure cannot answer "does THIS label want any
        of it". It is optional: a caller testing this ordering in isolation gets
        the old behaviour, and ``redistribute_label_residuals`` always passes it.
    """
    wanted = wanted or {}
    absorbers = [r for r in members
                 if r.sizing_outcome == SIZING_OUTCOME_NORMAL
                 and float(r.unmet_notional or 0.0) <= MONEY_EPSILON
                 and REASON_CLOSE_TO_ZERO not in r.reasons
                 and not (wanted
                          and float(wanted.get(r.symbol, 0.0) or 0.0) <= MONEY_EPSILON
                          and abs(float(r.current_quantity or 0.0)) <= QUANTITY_EPSILON
                          and abs(float(r.delta_quantity or 0.0)) <= QUANTITY_EPSILON)]
    return sorted(absorbers, key=lambda r: (0 if r.fractional else 1,
                                            0 if r.delta_quantity else 1,
                                            r.symbol))


def _smallest_absorbable(absorbers: List["AllocationRow"],
                         margin: Dict[str, MarginInfo], *,
                         allow_fractional: bool, residual: float,
                         valuation_mode: str, allocation_basis: str) -> float:
    """The cheapest single step any absorber could take, or +inf if there are none.

    Measured in the SAME money as the residual, which is why it takes the mode, the
    basis and the residual's sign: in ``cost`` mode a share off a position down 50%
    moves the label by the AVERAGE COST, so a leftover under that is unabsorbable
    even when it is comfortably over one share's PRICE. Comparing the two units
    turns "the arithmetic does not divide" into a warning about a fault.
    """
    steps = [tradeable_unit(margin.get(r.symbol), allow_fractional=allow_fractional)
             * _absorption_unit_money(r, valuation_mode, allocation_basis,
                                      buying=_row_is_buying(r, residual))
             for r in absorbers]
    return min(steps) if steps else float("inf")


def redistribute_label_residuals(plan: "AllocationPlan",
                                 label_targets: Dict[str, Dict[str, float]],
                                 margin: Dict[str, MarginInfo], *,
                                 allow_fractional: bool) -> Dict[str, float]:
    """Move whole grid units between a label's rows until the LABEL hits its total.

    Decision D2. Rounding leaves a label short of its target; D1's bumps can push it
    over. The difference is SIGNED and redistribution is BIDIRECTIONAL: it adds
    weight to close a shortfall and REMOVES weight to close an overshoot. A
    one-directional "top up" is wrong the moment a single symbol is bumped.

    On ``ALLOCATION_BASIS_BUDGET`` (an INVEST_LABEL run) removing weight means
    BUYING LESS and nothing else: a row that is not already buying is left alone
    rather than turned into a SELL. That flow's contract is "deploy this money", and
    opening a sale of an existing holding to give back a bump's rounding is a
    different trade with a realised gain behind it. The give-back that no buy can
    absorb is reported as the label's residual instead.

    Mutates ``plan.rows`` in place and appends to ``plan.warnings``. Runs INSIDE the
    two solvers, before ``_apply_bp_scaling`` -- and it can never be the reason that
    scaling has to fire, because every upward move is capped by the plan's live
    buying-power headroom.

    Args:
        plan: the solved plan. ``valuation_mode`` and ``allocation_basis`` decide
            what "on target" means.
        label_targets: ``{label: {symbol: target_notional_for_THIS_label}}`` -- the
            per-label split, which ``AllocationRow.target_notional`` cannot provide
            because a symbol in two labels carries their SUM.
        margin: ``{symbol: MarginInfo}``, the same dict the plan was solved with, so
            redistribution rounds on the same grid the sizing did.
        allow_fractional: the run's toggle, again so the grids match.

    Returns:
        Dict[str, float]: ``{label: residual left over}``, signed. A label with no
        movable member is absent.

    Termination: every step is ``floor(|residual| / one_unit) * one_unit``, so it can
    close the gap but never cross it; ``|residual|`` therefore decreases strictly
    whenever anything moves. A pass walks a fixed absorber list once and recomputes
    the residual after each row; a pass that moves nothing exits at the fixed point.
    ``REDISTRIBUTION_MAX_PASSES`` bounds it regardless, and a bound that actually
    stops the loop is REPORTED, never swallowed.
    """
    margin = margin or {}
    mode = plan.valuation_mode
    basis = plan.allocation_basis
    out: Dict[str, float] = {}
    for label in sorted(label_targets or {}):
        wanted = label_targets[label] or {}
        # Both sides of the comparison are restricted to the SAME member set: rows
        # this label alone owns. A multi-label symbol is out of the scheme entirely,
        # target and projection together, so the arithmetic stays self-consistent.
        members = [r for r in plan.rows
                   if len(r.labels) == 1 and r.labels[0] == label
                   and not r.skipped and r.price is not None and r.price > 0]
        if not members:
            continue
        target_total = sum(float(wanted.get(r.symbol, 0.0) or 0.0) for r in members)
        absorbers = _absorber_order(members, wanted)
        baselines = {r.symbol: float(r.delta_quantity or 0.0) for r in members}
        residual = _label_residual(target_total, members, mode, basis)
        passes = 0
        # WHY the loop ended, which is the whole difference between the two
        # warnings: True only when the loop was still moving and ran out of passes.
        stopped_on_bound = False
        while abs(residual) > MONEY_EPSILON:
            if passes >= REDISTRIBUTION_MAX_PASSES:
                stopped_on_bound = True
                break
            passes += 1
            moved_this_pass = False
            for row in absorbers:
                if abs(residual) <= MONEY_EPSILON:
                    break
                move = _absorb_residual(
                    row, residual, margin.get(row.symbol),
                    allow_fractional=allow_fractional, valuation_mode=mode,
                    allocation_basis=basis, bp_headroom=_buying_power_headroom(plan))
                if not move:
                    continue
                _apply_absorption(row, move, label, baselines[row.symbol])
                moved_this_pass = True
                residual = _label_residual(target_total, members, mode, basis)
            if not moved_this_pass:
                # The fixed point: another pass would walk the same list and move
                # the same nothing. The bound is irrelevant to what happens next.
                break
        out[label] = residual
        # THREE outcomes, and each names a different cause, because each has a
        # different fix: raise the bound / free up an absorber / nothing to do.
        if abs(residual) <= MONEY_EPSILON:
            continue
        if stopped_on_bound:
            plan.warnings.append(WARNING_RESIDUAL_UNCONVERGED_FMT.format(
                label=label, residual=residual, passes=REDISTRIBUTION_MAX_PASSES))
        elif abs(residual) >= _smallest_absorbable(
                absorbers, margin, allow_fractional=allow_fractional,
                residual=residual, valuation_mode=mode, allocation_basis=basis):
            # Some member could physically have taken this and none was allowed to.
            plan.warnings.append(WARNING_RESIDUAL_LEFT_FMT.format(
                label=label, residual=residual))
        # Otherwise the leftover is under one tradeable unit of every absorber:
        # pure arithmetic, reported as money by fractional_summary, not as a fault.
    return out


# ---------------------------------------------------------------------------
# Reporting. About a quarter of a real book is not fractionable, the engine bumps
# some rows up and redistributes others, and every one of those decisions has to
# reach the screen. Pure: text and numbers, no styling, no widgets.
# ---------------------------------------------------------------------------

#: Shown when fractional is ON but some symbols could not use it.
WHOLE_SHARE_NOTICE_FMT = (
    "{count} of {total} symbols cannot trade fractionally ({pct:.0f}%) - their orders "
    "round to whole shares, leaving the plan {residual:,.2f} off target. The "
    "Projected columns already reflect this.")
#: Shown when the fractional toggle itself is off -- every row rounds down.
WHOLE_SHARE_NOTICE_OFF_FMT = (
    "Fractional shares are OFF - every order rounds to whole shares, leaving the "
    "plan {residual:,.2f} off target. The Projected columns already reflect this.")
#: Shown when at least one symbol was BUMPED UP to one whole share. This is the plan
#: spending more than the weights asked for, so it is stated as money, up front.
BUMP_NOTICE_FMT = (
    "{count} symbol(s) had a target smaller than one whole share and were BUMPED UP "
    "to one, so they get a position at all - that over-allocates them by {total:,.2f} "
    "in total. Marked 'bumped-to-1' in the Outcome column.")
#: Shown when at least one symbol gets no order at all AND the BUMP BOUND is why
#: for every one of them -- one whole share would have overshot the target.
NO_ORDER_NOTICE_FMT = (
    "{count} symbol(s) get NO order at all, leaving {total:,.2f} unallocated - see "
    "'Not traded' below. One whole share of each would be more than {limit:.0f}% of "
    "its target, so buying one is a different trade, not a rounding fix.")
#: The same headline WITHOUT the cause, for the mixed case. A row buying power
#: took away, a row under a broker minimum and a row the precheck refused all end
#: up here too, and the sentence above is simply false about them: one share of a
#: buying-power casualty was 109% of its target and perfectly acceptable. The
#: per-row reason is in the 'Not traded' table, which is where a plan with several
#: different causes has to send the reader anyway.
NO_ORDER_NOTICE_MIXED_FMT = (
    "{count} symbol(s) get NO order at all, leaving {total:,.2f} unallocated - see "
    "'Not traded' below, which gives the reason for each.")
#: Shown when redistribution moved a row off the quantity the weights implied.
REDISTRIBUTION_NOTICE_FMT = (
    "{count} symbol(s) had their share count adjusted so their label still hits its "
    "total after rounding. The Weight columns show what you asked for and what the "
    "plan will actually hold.")


def fractional_summary(plan: "AllocationPlan") -> Dict[str, Any]:
    """How the plan's rows were SIZED, and what the sizing rules cost. Pure.

    Rows with no usable price are excluded from every count and from the residual:
    their whole target is already reported through ``plan.unallocatable_pct``, and
    counting it here too would double the money the dry run calls unallocated.

    ``residual_notional`` is ``sum(target_notional - allocated_value)`` over the
    priced rows: SIGNED, so a bump's over-allocation nets against a rounding
    shortfall, which is what "how far off target will I be" actually means.

    ``unknown_rows`` counts rows carrying ``REASON_FRACTIONAL_UNKNOWN`` -- exactly
    the rows where the broker published no eligibility answer (no ``MarginInfo`` at
    all, or ``fractionable is None``). With the fractional toggle OFF no eligibility
    reason is appended at all and this is 0, correctly: nothing was consulted.

    Returns:
        Dict[str, Any]: ``allow_fractional``, ``total_rows``, ``fractional_rows``,
        ``whole_share_rows``, ``unknown_rows``, ``whole_share_symbols`` (sorted),
        ``whole_share_pct``, ``target_notional``, ``projected_notional``,
        ``residual_notional``, ``residual_pct`` (of ``base_notional``),
        ``no_order_rows``, ``no_order_notional``, ``bumped_rows``,
        ``bumped_notional`` (the deliberate over-allocation),
        ``skipped_too_large_rows`` and ``redistributed_rows``.
    """
    mode = plan.valuation_mode
    basis = plan.allocation_basis
    priced = [r for r in plan.rows if r.price is not None and r.price > 0]
    total = len(priced)
    whole = [r for r in priced if not r.fractional]
    unknown = [r for r in priced if REASON_FRACTIONAL_UNKNOWN in r.reasons]
    bumped = [r for r in priced if r.sizing_outcome == SIZING_OUTCOME_BUMPED]
    too_large = [r for r in priced if r.sizing_outcome == SIZING_OUTCOME_SKIPPED_TOO_LARGE]
    # A bump a later step UNDID. Counted separately and NOT in ``bumped``: it
    # spends nothing, so folding it in is what made the bump notice report an
    # over-allocation of 0.00. See ``_reconcile_sizing_outcomes``.
    bumped_dropped = [r for r in priced
                      if r.sizing_outcome == SIZING_OUTCOME_BUMPED_DROPPED]

    target_total = sum(float(r.target_notional or 0.0) for r in priced)
    projected_total = 0.0
    bumped_over = 0.0
    for r in priced:
        value = allocated_value(r, mode, basis)
        if value is None:
            continue
        projected_total += value
        if r.sizing_outcome == SIZING_OUTCOME_BUMPED:
            bumped_over += max(0.0, value - float(r.target_notional or 0.0))

    dropped = [r for r in plan.rows if float(r.unmet_notional or 0.0) > MONEY_EPSILON]
    base = float(plan.base_notional or 0.0)
    residual = target_total - projected_total

    return {
        "allow_fractional": bool(plan.allow_fractional),
        "total_rows": total,
        "fractional_rows": len([r for r in priced if r.fractional]),
        "whole_share_rows": len(whole),
        "unknown_rows": len(unknown),
        "whole_share_symbols": sorted(r.symbol for r in whole),
        "whole_share_pct": (len(whole) / total * 100.0) if total else 0.0,
        "target_notional": target_total,
        "projected_notional": projected_total,
        "residual_notional": residual,
        "residual_pct": (residual / base * 100.0) if base > 0 else 0.0,
        "no_order_rows": len(dropped),
        # How many of those the BUMP BOUND refused, which is a different cause
        # from every other way a row loses its order (the grid, a broker minimum,
        # buying power, a precheck refusal). The notice used to assert the bound
        # for all of them; it may only do so when it is true of all of them.
        "no_order_rows_over_bump_limit": len(
            [r for r in dropped
             if r.sizing_outcome == SIZING_OUTCOME_SKIPPED_TOO_LARGE]),
        "no_order_notional": sum(float(r.unmet_notional or 0.0) for r in dropped),
        "bumped_rows": len(bumped),
        "bumped_notional": bumped_over,
        "bumped_dropped_rows": len(bumped_dropped),
        "skipped_too_large_rows": len(too_large),
        "redistributed_rows": len([r for r in plan.rows if r.redistributed]),
    }


def no_order_rows(plan: "AllocationPlan") -> List[Dict[str, Any]]:
    """One display dict per row the plan wanted to trade and could NOT, biggest first.

    The DETAIL view for the money. ``dry_run_rows`` lists what will be SENT, so on
    one of these rows its quantity, side and value columns are all blank -- there is
    no order to describe. This says what was WANTED instead: the target, the weight
    it came from, what will actually be held, and how much never left the cash.

    Selected by ``unmet_notional``, so the reason strings never have to be
    pattern-matched: whatever zeroed the row -- the bump bound, the tradeable grid,
    the broker minimum, buying-power scaling, a precheck rejection -- set that field.

    ``weight_pct`` divides the GROSS ``base_notional``, exactly as ``dry_run_rows``
    does: this sits under the same column heading and must not answer a different
    question from the row it is explaining.
    """
    mode = plan.valuation_mode
    basis = plan.allocation_basis
    base = float(plan.base_notional or 0.0)
    out: List[Dict[str, Any]] = []
    for row in plan.rows:
        if float(row.unmet_notional or 0.0) <= MONEY_EPSILON:
            continue
        projected = allocated_value(row, mode, basis)
        out.append({
            "symbol": row.symbol,
            "price": row.price,
            "current_quantity": row.current_quantity,
            "outcome": row.sizing_outcome,
            "target_notional": round(row.target_notional, 2),
            "weight_pct": round(row.target_notional / base * 100.0, 3) if base > 0 else 0.0,
            "projected_notional": None if projected is None else round(projected, 2),
            "unmet_notional": round(float(row.unmet_notional), 2),
            "reasons": ", ".join(row.reasons),
        })
    return sorted(out, key=lambda d: d["unmet_notional"], reverse=True)


def whole_share_notice(summary: Dict[str, Any]) -> Optional[str]:
    """The prominent whole-share warning for the dry run, or ``None`` if there is none.

    ``None`` means every priced row was sized on the fractional grid, so there is
    nothing to warn about. Text only: the caller picks the banner styling.
    """
    if summary["whole_share_rows"] <= 0:
        return None
    if not summary["allow_fractional"]:
        return WHOLE_SHARE_NOTICE_OFF_FMT.format(residual=summary["residual_notional"])
    return WHOLE_SHARE_NOTICE_FMT.format(
        count=summary["whole_share_rows"],
        total=summary["total_rows"],
        pct=summary["whole_share_pct"],
        residual=summary["residual_notional"])


def bump_notice(summary: Dict[str, Any]) -> Optional[str]:
    """The "we are spending more than you asked" warning, or ``None``.

    A bump is a deliberate over-allocation taken so that a symbol gets a position at
    all. It is the one thing in this plan that spends money the weights did not ask
    for, so it gets its own sentence with its own number.
    """
    if summary["bumped_rows"] <= 0:
        return None
    return BUMP_NOTICE_FMT.format(count=summary["bumped_rows"],
                                  total=summary["bumped_notional"])


def no_order_notice(summary: Dict[str, Any]) -> Optional[str]:
    """The "some symbols get nothing" warning, or ``None`` when every row trades.

    NAMES THE CAUSE ONLY WHEN IT IS THE CAUSE OF ALL OF THEM. The bump bound is
    one of several ways a row loses its order and this sentence used to assert it
    for every one -- so a row the buying-power scaler took away was explained as
    "one whole share would be more than 150% of its target" when one whole share
    had been 109% of its target and entirely acceptable. Mixed causes send the
    reader to the per-row reasons instead of picking one and stating it as fact.
    """
    count = summary["no_order_rows"]
    if count <= 0:
        return None
    if summary["no_order_rows_over_bump_limit"] == count:
        return NO_ORDER_NOTICE_FMT.format(
            count=count, total=summary["no_order_notional"],
            limit=BUMP_TO_ONE_SHARE_MAX_MULTIPLE * 100.0)
    return NO_ORDER_NOTICE_MIXED_FMT.format(count=count,
                                            total=summary["no_order_notional"])


def redistribution_notice(summary: Dict[str, Any]) -> Optional[str]:
    """The "your weights moved" warning, or ``None`` when none of them did.

    Redistribution is allowed to change a quantity the user's weights implied ONLY
    because the change is shown. This sentence, plus the Weight columns, is that
    showing.
    """
    if summary["redistributed_rows"] <= 0:
        return None
    return REDISTRIBUTION_NOTICE_FMT.format(count=summary["redistributed_rows"])


# ---------------------------------------------------------------------------
# What a run ACTUALLY filled. Pure -- the live service does the IO around it.
# ---------------------------------------------------------------------------

#: Statuses after which a TradingOrder can never fill any further.
#:
#: ``OrderStatus.get_terminal_statuses()`` is the broker-side "will not change
#: anymore" set -- CLOSED / REJECTED / CANCELED / EXPIRED / STOPPED / ERROR /
#: REPLACED. Two more belong here for the ledger's purposes:
#:
#:   FILLED            complete by definition, and NOT in the terminal set.
#:   DONE_FOR_DAY      the broker will send no further update today, so an
#:                     unfilled residue never fills; waiting on it would wedge
#:                     the run's income overnight. (User decision D5.)
#:
#: WASHTRADE_LOCKED is DELIBERATELY ABSENT (bug fix 2026-09-04 -- it used to be
#: here on the premise "our own gate, the order was never sent, so it is as
#: final as an order can be"). That premise is false:
#: ``TradeManager._check_all_washtrade_locked_orders`` re-submits a locked order
#: the moment its blocker clears, for up to ``_WASHTRADE_LOCK_MAX_AGE_HOURS``
#: (24h) before giving up and expiring it into a real terminal status. Counting
#: it as settled-at-zero here made ``run_allocation`` finalise the run and stamp
#: its income consumed at 0 while the order was still armed -- so when the lock
#: cleared and the buy filled hours later, no run's ledger was ever charged for
#: it, and the SAME income got deployed again by the next rebalance. Leaving it
#: out means a locked order reads as still WORKING, exactly like any other
#: order that has not resolved yet: the run stays in ``get_unconsumed_runs()``
#: until the lock clears (fills, at which point it settles for real) or expires
#: (which is a terminal status already in the set above).
#:
#: UNKNOWN is deliberately ABSENT too. "We do not know what this order did" is
#: not "this order is over", and the difference is whether income gets spent.
SETTLED_ORDER_STATUSES = frozenset(OrderStatus.get_terminal_statuses()) | {
    OrderStatus.FILLED,
    OrderStatus.DONE_FOR_DAY,
}

#: Statuses that are THEMSELVES a measurement of zero, so a missing
#: ``filled_quantity`` on one of them needs no explanation:
#:
#:   REJECTED          the broker refused the order. It was never working, so no
#:                     share of it can have traded.
#:   ERROR             our own stamp for a submission that failed
#:                     (``AccountInterface._handle_order_submit_error``).
#:
#: WASHTRADE_LOCKED is absent here too, for the same reason it left
#: ``SETTLED_ORDER_STATUSES`` -- it is not settled at all, so it is not a
#: measurement of zero either; it is UNKNOWN (still working) until it resolves.
#:
#: Needed because a refused allocation order reaches the ledger with a NULL
#: quantity and NOT an explicit 0.0: the row is persisted with ``filled_qty``
#: unset (``portfolio_allocation_service._submit_row``) and the refusal stamps
#: only the status, while a broker refresh writes no quantity for an order the
#: broker never accepted. Without this set the commonest outcome there is -- one
#: row refused -- would make the whole run permanently unmeasurable, and income
#: that is never consumed gets deployed a second time by the next run just as
#: surely as income consumed for zero.
#:
#: Every OTHER settled status -- FILLED, CANCELED, EXPIRED, STOPPED, REPLACED,
#: CLOSED, DONE_FOR_DAY -- can carry shares that really traded (a cancel after a
#: partial is the ordinary case), so a missing quantity on one of those is
#: UNKNOWN, not zero, and stalls the ledger.
UNEXECUTED_ORDER_STATUSES = frozenset({
    OrderStatus.REJECTED,
    OrderStatus.ERROR,
})


@dataclass(frozen=True)
class OrderFill:
    """One order's fill facts, lifted off a ``TradingOrder`` row.

    Plain values, not an ORM row, so this stays IO-free and unit-testable -- the
    same contract ``consume_income_events`` uses for the ledger.

    ``status=None`` is the live collector's spelling for "that order id has no row
    any more", which is an inconsistency, not an emptiness: it is treated as still
    working so the run stalls instead of quietly consuming income.

    ``filled_quantity=None`` and ``fill_price=None`` mean the same thing as each
    other -- "nobody reported this" -- and both make the order UNMEASURABLE. The
    quantity default is ``None`` and NOT ``0.0`` for the same reason the price
    default is: ``0.0`` is a measurement of zero shares, and reading a NULL
    ``filled_qty`` as one is what let two runs deploy the same income (a FILLED
    TastyTrade order whose ``_fills_summary`` answered ``(0.0, None)`` settled the
    run at zero, took the one-shot ``income_consumed_at`` stamp, and left the money
    looking unallocated). A quantity that is genuinely zero must be passed
    EXPLICITLY, which is what a broker that answered "nothing filled" produces.
    """
    order_id: int
    side: Optional[OrderDirection] = None
    status: Optional[OrderStatus] = None
    filled_quantity: Optional[float] = None
    fill_price: Optional[float] = None


@dataclass
class FilledTotals:
    """What a run's orders REALLY moved, and whether that answer is final.

    ``settled`` is the gate on income consumption. False means at least one order
    can still fill (or could not be valued), so the ledger must NOT be spent yet --
    ``finalise_allocation_run(..., orders_settled=False)`` records the totals and
    leaves ``income_consumed_at`` NULL, which keeps the run in
    ``get_unconsumed_runs()`` where a later reconcile pass picks it up. With ~25%
    of symbols non-fractionable and the ADJUST path creating WAITING_TRIGGER
    orders, False is the COMMON outcome, not an edge case (user decision D3), so
    ``settled`` and ``working_order_ids`` are user-facing facts, not just logging.

    Both list fields use ``field(default_factory=list)``: a bare ``= []`` default
    is a dataclass ``ValueError`` at import, and would otherwise share one list
    across every instance.
    """
    buy_value: float = 0.0
    sell_value: float = 0.0
    settled: bool = True
    working_order_ids: List[int] = field(default_factory=list)
    unmeasurable_order_ids: List[int] = field(default_factory=list)

    @property
    def net_buy_value(self) -> float:
        """``max(0, filled buys - filled sells)``, mirroring the model property."""
        return max(0.0, self.buy_value - self.sell_value)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe, for the activity log's data blob and the income panel's copy."""
        return {
            "buy_value": self.buy_value,
            "sell_value": self.sell_value,
            "net_buy_value": self.net_buy_value,
            "settled": self.settled,
            "working_order_ids": list(self.working_order_ids),
            "unmeasurable_order_ids": list(self.unmeasurable_order_ids),
        }


def measure_filled_values(fills: List[OrderFill]) -> FilledTotals:
    """Sum what a run's orders actually filled, and say whether that is final. Pure.

    Value is ``abs(filled_quantity) * fill_price``, with the DIRECTION taken from
    ``side`` -- the same signed-field normalisation ``OrderImpact.bp_cost`` applies
    to the broker's ``change_in_buying_power``. Never infer a sell from a negative
    quantity: ``side`` is the fact, the sign is a broker convention.

    There is NO fallback to a planned price or an estimated value: an order
    reporting a fill it cannot price is UNMEASURABLE, and unmeasurable stalls the
    ledger rather than guessing. Guessing is the bug this function exists to kill --
    income consumed against money that was only ever intended, made permanent by
    the one-shot ``income_consumed_at``.

    A MISSING QUANTITY is treated exactly like a missing price: ``None`` is
    unmeasurable, and only an EXPLICIT zero (``abs(quantity) <= QUANTITY_EPSILON``)
    is the measured "nothing filled" that contributes zero and settles. Zero was
    once the reading of both, so a NULL ``filled_qty`` on a FILLED order settled the
    run at zero value, stamped it, and left the income it had really spent looking
    unallocated for the next run to spend again. The one exception is
    ``UNEXECUTED_ORDER_STATUSES``, where the status alone proves nothing traded.

    A partial fill counts its filled part (that money moved) AND blocks settlement
    (more can still move). A rejected, cancelled, errored or wash-trade-locked
    order contributes zero and is settled.

    Args:
        fills: one entry per EXECUTION order of the run. The caller is responsible
            for having excluded protective TP/SL legs -- see
            ``portfolio_allocation_service.collect_order_fills``.

    Returns:
        FilledTotals: buy/sell money, plus ``settled`` and the two id lists that
        explain a False. Empty input is settled and worth zero: a run whose every
        row was skipped consumes nothing, which is correct, not a stall.
    """
    totals = FilledTotals()
    for fill in fills or []:
        if fill.status not in SETTLED_ORDER_STATUSES:
            totals.settled = False
            totals.working_order_ids.append(fill.order_id)

        quantity = fill.filled_quantity
        if quantity is None and fill.status in UNEXECUTED_ORDER_STATUSES:
            # The status IS the measurement: the order was refused or never sent,
            # so there is no quantity to be missing. See UNEXECUTED_ORDER_STATUSES.
            continue
        if quantity is not None and abs(float(quantity)) <= QUANTITY_EPSILON:
            continue

        price = fill.fill_price
        if quantity is None or fill.side is None or price is None or float(price) <= 0:
            totals.settled = False
            totals.unmeasurable_order_ids.append(fill.order_id)
            continue

        value = abs(float(quantity)) * float(price)
        if fill.side == OrderDirection.BUY:
            totals.buy_value += value
        elif fill.side == OrderDirection.SELL:
            totals.sell_value += value
    return totals


#: Income-panel copy for a run whose orders have not settled. Decision D3 makes
#: this the COMMON case, so it is a first-class message, not a log line.
UNCONSUMED_RUNS_NOTICE_FMT = (
    "Income not consumed yet - {orders} order(s) from {runs} allocation run(s) are "
    "still working at the broker. They are re-measured automatically on the next "
    "Refresh or allocation run.")

#: Same situation, but the run has no working order to point at: it died between
#: submitting and recording. Saying "0 orders still working" would be nonsense.
UNFINISHED_RUNS_NOTICE_FMT = (
    "Income not consumed yet - {runs} allocation run(s) never finished recording. "
    "They are re-checked on the next Refresh or allocation run.")


def unconsumed_income_notice(run_count: int,
                             order_count: int) -> Optional[Tuple[str, str]]:
    """``(text, severity)`` for the income panel, or ``None`` when nothing is open.

    PURE, and deliberately shaped like ``whole_share_notice`` / ``no_order_notice``:
    the wizard module only DRAWS it, so the wording is testable without a NiceGUI
    client.

    Distinct from ``ui.utils.portfolio_allocation_view.working_orders_notice``,
    which speaks about ONE run the user has just submitted ("2 orders still
    working"). This one speaks about the ACCOUNT's backlog -- every run whose
    income is still unconsumed -- which is what the income panel shows. Both return
    the same ``(text, severity)`` shape, so ``render_income_panel`` draws either.

    ``severity`` is a NiceGUI notify/badge word -- one of
    ``positive`` | ``negative`` | ``warning`` | ``info``. ``error`` is NOT one of
    them (settings.py gets that wrong; do not copy it).

    Args:
        run_count: how many of the account's runs still have a NULL
            ``income_consumed_at``.
        order_count: how many MARKET orders across those runs are still working.

    Returns:
        Optional[Tuple[str, str]]: ``None`` when ``run_count <= 0``. Otherwise the
        sentence and ``"warning"`` -- unallocated income that the user believes is
        already invested is worth interrupting for, but it is not an error: the
        recovery path is automatic.
    """
    if run_count <= 0:
        return None
    if order_count <= 0:
        return (UNFINISHED_RUNS_NOTICE_FMT.format(runs=run_count), "warning")
    return (UNCONSUMED_RUNS_NOTICE_FMT.format(orders=order_count, runs=run_count),
            "warning")
