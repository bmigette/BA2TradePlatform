"""Portfolio allocation arithmetic (pure; no DB, no broker, no UI).

Turns target percentages into per-symbol share deltas:

    label_notional  = base_notional * label.target_pct / 100
    symbol_notional = label_notional * symbol.weight_pct / 100   (targets SUM
                      when a symbol carries more than one managed label)
    delta_quantity  = SIGNED shares to trade; how it is derived depends on the
                      valuation mode (see ``compute_allocation``)
    target_quantity = current_quantity + delta_quantity          (POST-TRADE)
    bp_cost         = |delta_notional| * bp_factor(symbol)       (buys only)

When ``sum(bp_cost of buys) > available_buying_power`` every BUY scales down
pro-rata and the plan records ``scale_factor``. Sells never scale.

Percentages are validated to total 100 within ``LABEL_TOTAL_TOLERANCE_PCT`` (0.01
PERCENTAGE POINTS) at BOTH levels: label targets across the account, and symbol
weights within each label. That tolerance is tight enough to reject a naive 2dp
even split -- ``3 x 33.33 == 99.99`` misses by a hair MORE than 0.01 -- so ALWAYS
generate default percentages with ``even_split_pct``, which drops the remainder on
the last slot and totals exactly 100.0. Never hand-roll a split.
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
    # wizard
    "BaseSnapshot", "build_base_snapshot", "WARNING_NO_MULTIPLIER",
    "held_symbols_without_price", "held_no_price_block", "ERROR_HELD_NO_PRICE_FMT",
    "dry_run_rows", "filter_plan_rows", "summarise_plan", "DRY_RUN_QUANTITY_DECIMALS",
    "even_split_targets", "steps_validation_messages", "validate_invest_amount",
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
    "REASON_MULTI_LABEL_FMT", "REASON_SCALED_FMT",
    "REASON_SCALED_PREFIX", "REASON_BELOW_MIN_ORDER_PREFIX",
    "REASON_BELOW_MIN_FRACTIONAL_NOTIONAL_PREFIX",
    "WARNING_EMPTY_LABEL_FMT", "WARNING_PRECHECK_DISAGREED_FMT",
    "ERROR_LABEL_TOTAL_FMT", "ERROR_LABEL_NEGATIVE_FMT", "ERROR_LABEL_DUPLICATE_FMT",
    "ERROR_LABEL_NO_SYMBOLS_FMT", "ERROR_SYMBOL_TOTAL_FMT", "ERROR_SYMBOL_NEGATIVE_FMT",
    "ERROR_SYMBOL_DUPLICATE_FMT",
    # engine
    "current_value", "round_quantity", "round_delta_quantity", "even_split_pct",
    "build_symbol_targets", "validate_symbol_weights", "validate_label_targets",
    "compute_base_notional", "compute_allocation", "compute_label_investment",
    "apply_order_impacts", "consume_income_events",
    # filled-value measurement (the income ledger's input)
    "SETTLED_ORDER_STATUSES", "OrderFill", "FilledTotals", "measure_filled_values",
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
    "BUMP_NOTICE_FMT", "REDISTRIBUTION_NOTICE_FMT",
    # submission
    "ACTION_ADJUST", "ACTION_CLOSE", "ACTION_NEW", "ACTION_SKIP",
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
#: 1.5 == "one share may cost at most 150% of what this symbol was allocated".
#:
#: ONE constant, quoted in the refusal message, never a scattered literal. The value
#: is set so the two worked examples of the decision land on opposite sides of it: a
#: 200 target on a 300 share (150%) is the case that must be filled and sits exactly
#: ON the bound (the comparison is inclusive), and a 50 target on a 500 share (1000%)
#: is the case that must be refused. It also bounds the damage: the worst bump
#: over-allocates its symbol by 50% of that symbol's target, which is normally inside
#: what ``redistribute_label_residuals`` can take back off the label's fractionable
#: siblings. At 2x and above the excess routinely exceeds what the siblings hold and
#: the label is left structurally over target with nothing able to fix it.
BUMP_TO_ONE_SHARE_MAX_MULTIPLE = 1.5

#: What the sizing rules DID to a row, so the dry run never has to pattern-match
#: reason prose. A bump is a deliberate over-allocation and must be visible.
SIZING_OUTCOME_NORMAL = "normal"                        #: ordinary grid rounding
SIZING_OUTCOME_BUMPED = "bumped-to-1"                   #: under one unit, bumped UP
SIZING_OUTCOME_SKIPPED_TOO_LARGE = "skipped-too-large"  #: under one unit, one unit too big

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
WARNING_EMPTY_LABEL_FMT = "label '{label}' has no symbols - {pct:.2f}% unallocated"
WARNING_PRECHECK_DISAGREED_FMT = "broker precheck disagreed on {symbol} - re-solved"

# Validation messages from ``validate_label_targets``. Pinned so the UI and the
# tests agree on the exact text.
ERROR_LABEL_TOTAL_FMT = "label targets total {total:.2f}% - must total 100%"
ERROR_LABEL_NEGATIVE_FMT = "label '{label}' has a negative target ({pct:.2f}%)"
ERROR_LABEL_DUPLICATE_FMT = "duplicate label '{label}'"
ERROR_LABEL_NO_SYMBOLS_FMT = "label '{label}' has target {pct:.2f}% but no symbols"
ERROR_SYMBOL_TOTAL_FMT = "label '{label}' symbol weights total {total:.2f}% - must total 100%"
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
    """
    symbol: str
    quantity: float = 0.0
    cost_basis: float = 0.0
    price: Optional[float] = None
    market_value: Optional[float] = None
    transaction_ids: List[int] = field(default_factory=list)


@dataclass
class SymbolTarget:
    """A symbol's weight WITHIN one label. ``weight_pct`` is 1-100, not 0-1."""
    symbol: str
    weight_pct: float
    comment: Optional[str] = None


@dataclass
class LabelTarget:
    """A managed label and its share of the base notional.

    ``target_pct`` is 1-100 of ``base_notional``. Across all managed labels of an
    account it must total exactly 100 before a REBALANCE may be submitted. An
    empty ``symbols`` list cannot absorb its percentage: the engine allocates it
    nothing and adds ``target_pct`` to ``AllocationPlan.unallocatable_pct``.
    """
    label: str
    target_pct: float
    symbols: List[SymbolTarget] = field(default_factory=list)
    comment: Optional[str] = None


@dataclass
class AllocationRow:
    """One symbol's line in a plan: where it is, where it should be, the delta.

    ``delta_quantity`` is SIGNED (positive = buy, negative = sell); ``side`` is
    the matching ``OrderDirection`` (``None`` when the delta is exactly zero or
    the row was skipped). ``target_quantity`` is the POST-TRADE holding --
    ``current_quantity + delta_quantity``, what the account owns if this row
    executes -- and NOT an ideal share count the rounding may never reach; it is
    the same measure in both valuation modes, so it compares across rows.
    ``estimated_value`` and ``bp_cost`` are always POSITIVE
    magnitudes. ``bp_cost`` is 0.0 for sells -- sells free buying power and never
    scale. ``fractional`` records the SIZING MODE: True when this row was rounded
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
    ``sum(bp_cost of buys) > available_buying_power``. Sells never scale.
    ``unallocatable_pct`` is the share of the base that no label could absorb
    (empty labels, skipped no-price symbols) and shows in the dry-run as cash
    left over. ``required_buying_power`` is the POST-scaling figure -- what the
    plan as displayed actually needs.

    ``base_notional`` carries TWO meanings depending on which solver built the
    plan: in a REBALANCE it is the ALLOCATABLE BASE (buying power plus the current
    value of managed positions, which the label percentages divide up), and in an
    INVEST_LABEL run it is simply THE BUDGET being spent. One field, because both
    are "the money this plan is dividing", but a caller rendering it must know
    which run produced the plan.

    ``valuation_mode`` records what "current value" MEANT for every number here --
    the base, the percentages and every delta (decision 5a). It is a user-flippable
    toggle, so a ``plan_json`` without it cannot be reproduced or even read
    correctly six months later.
    """
    rows: List[AllocationRow] = field(default_factory=list)
    base_notional: float = 0.0
    available_buying_power: float = 0.0
    required_buying_power: float = 0.0
    bp_usage_pct: float = 0.0
    scale_factor: float = 1.0
    unallocatable_pct: float = 0.0
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
    warnings: List[str] = field(default_factory=list)

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
            "required_buying_power": self.required_buying_power,
            "bp_usage_pct": self.bp_usage_pct,
            "scale_factor": self.scale_factor,
            "unallocatable_pct": self.unallocatable_pct,
            "total_buy_value": self.total_buy_value,
            "total_sell_value": self.total_sell_value,
            "allow_fractional": self.allow_fractional,
            "valuation_mode": self.valuation_mode,
            "allocation_basis": self.allocation_basis,
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
    # INCLUSIVE bound: exactly 150% bumps. MONEY_EPSILON absorbs the float noise of
    # a target computed through two percentage multiplications.
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


def even_split_pct(count: int) -> List[float]:
    """Split 100% evenly across ``count`` slots, exact to 2dp.

    The remainder lands on the LAST slot so the list always totals exactly 100.0
    (``even_split_pct(3) == [33.33, 33.33, 33.34]``). Returns ``[]`` for
    ``count <= 0`` -- an empty label gets nothing, not a ZeroDivisionError.
    """
    if count <= 0:
        return []
    each = math.floor(100.0 / count * 100.0) / 100.0
    out = [each] * count
    out[-1] = round(100.0 - each * (count - 1), 2)
    return out


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

    This is the INVEST_LABEL submit gate. Without it ``compute_label_investment``
    multiplies whatever weights it is handed straight through, so a hand-edited
    150% set turns a 10,000 budget into 15,000 of buys, and a 60% set silently
    leaves 40% of the amount as cash with nothing on the plan to say so.

    A label with NO symbols returns no errors -- it has no weights to be wrong.
    Whether an empty label may be invested into is the caller's decision.

    Returns:
        List[str]: ``ERROR_SYMBOL_*`` strings naming the offending label and
        symbol, ready to show verbatim; EMPTY means valid. ``validate_label_targets``
        calls this for its per-label symbol checks, so the two can never drift.
    """
    errors = []
    if not label.symbols:
        return errors
    weight_total = sum(float(st.weight_pct or 0.0) for st in label.symbols)
    if abs(weight_total - 100.0) > tolerance:
        errors.append(ERROR_SYMBOL_TOTAL_FMT.format(label=label.label, total=weight_total))
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

    LABEL level: targets total 100 +/- ``tolerance`` (0.01 PERCENTAGE POINTS by
    default, so 99.995 passes and 99.98 does not); no negative ``target_pct``; no
    duplicate label names; every non-zero label has at least one symbol.

    SYMBOL level, per label that HAS symbols: delegated in full to
    ``validate_symbol_weights`` (weights total 100 +/- the same ``tolerance``; no
    negative weight; no symbol repeated within the label) so that the REBALANCE
    gate here and the INVEST_LABEL gate there can never disagree. The same symbol
    appearing in DIFFERENT labels is legal and its targets sum (decision 7) --
    only a repeat inside one label is an error. Without these checks a hand-edited
    weight set totalling 150% would silently over-deploy its label, since
    ``compute_allocation`` multiplies the weights straight through.

    A label with no symbols is skipped here (an empty label at 0% stays valid; a
    non-zero one is already reported by ``ERROR_LABEL_NO_SYMBOLS_FMT``).

    Note that ``tolerance`` rejects a naive 2dp split (``3 x 33.33 == 99.99``);
    build defaults with ``even_split_pct`` and both levels pass by construction.

    Returns:
        List[str]: human-readable error strings built from the ``ERROR_LABEL_*``
        and ``ERROR_SYMBOL_*`` formats, each naming the offending label (and
        symbol) so the UI can show it verbatim; EMPTY means valid. Submit must be
        blocked while this is non-empty (decision 3).
    """
    errors = []
    total = sum(float(lt.target_pct or 0.0) for lt in labels or [])
    if abs(total - 100.0) > tolerance:
        errors.append(ERROR_LABEL_TOTAL_FMT.format(total=total))
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


def _finalise_totals(plan: AllocationPlan) -> None:
    """Fill the plan-level money totals from its rows."""
    plan.total_buy_value = sum(r.estimated_value for r in plan.rows if r.is_buy)
    plan.total_sell_value = sum(r.estimated_value for r in plan.rows if r.is_sell)
    plan.required_buying_power = sum(r.bp_cost for r in plan.rows if r.is_buy)
    plan.bp_usage_pct = (plan.required_buying_power / plan.available_buying_power * 100.0
                         if plan.available_buying_power > 0 else 0.0)


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
    """Scale every BUY pro-rata until the plan fits available buying power.

    SELLS NEVER SCALE -- they free buying power. The re-rounded quantity is fed
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
    avail = float(available_buying_power or 0.0)
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
    return scale


def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float,
                       valuation_mode: str) -> AllocationPlan:
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

    Behaviour on degenerate DATA (records a reason, never raises -- contrast the
    degenerate MONEY inputs under Raises, which must never be guessed at):
        * a label with no symbols -> allocates nothing, ``target_pct`` added to
          ``plan.unallocatable_pct`` and, above 0%, a ``WARNING_EMPTY_LABEL_FMT``;
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
    the base and leaves the rest as cash. Blocking submission is
    ``validate_label_targets``' job, not this function's.

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
                          valuation_mode=valuation_mode)
    targets = {}
    target_pcts = {}
    sym_labels = {}
    # The PER-LABEL split, which row.target_notional cannot carry: a symbol in two
    # labels sums their shares into one row. Redistribution needs the split.
    label_targets: Dict[str, Dict[str, float]] = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        if not lt.symbols:
            # An empty label cannot absorb its percentage: it becomes cash left over.
            # At 0% there is nothing to absorb and nothing to warn about.
            plan.unallocatable_pct += max(0.0, pct)
            if pct > 0:
                plan.warnings.append(
                    WARNING_EMPTY_LABEL_FMT.format(label=lt.label, pct=pct))
            continue
        for st in lt.symbols:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + plan.base_notional * share / 100.0
            target_pcts[st.symbol] = target_pcts.get(st.symbol, 0.0) + share
            sym_labels.setdefault(st.symbol, []).append(lt.label)
            per_label = label_targets.setdefault(lt.label, {})
            per_label[st.symbol] = (per_label.get(st.symbol, 0.0)
                                    + plan.base_notional * share / 100.0)

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
            plan.unallocatable_pct += max(0.0, target_pcts.get(symbol, 0.0))
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
            ideal_quantity = round_quantity(target_notional, row.price, m,
                                            allow_fractional=allow_fractional,
                                            apply_min_order_size=False)
            raw_delta = float(target_notional) / float(row.price) - row.current_quantity
            # Round the DELTA, not just the target: an on-grid target minus an
            # off-grid holding is off-grid, and the delta is what is submitted.
            delta = _round_delta_shares(ideal_quantity - row.current_quantity, m,
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
        row.bp_cost = row.estimated_value * row.bp_factor if delta > 0 else 0.0
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
                          allocation_basis=ALLOCATION_BASIS_BUDGET)
    if not label.symbols:
        plan.unallocatable_pct = 100.0
        plan.warnings.append(WARNING_EMPTY_LABEL_FMT.format(label=label.label, pct=100.0))
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
        allow_fractional=plan.allow_fractional,
        valuation_mode=plan.valuation_mode,
        allocation_basis=plan.allocation_basis,
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
            row.reasons.extend(impact.errors)
            continue
        row.estimated_fees = impact.estimated_fees
        if row.is_buy and abs(impact.bp_cost - row.bp_cost) > 0.005:
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
    available = float(plan.available_buying_power or 0.0)
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
            "current_cost_basis": round(row.current_cost_basis, 2),
            "current_value": (None if row.price is None
                              else round(row.current_quantity * row.price, 2)),
            "estimated_value": round(row.estimated_value, 2),
            # The broker precheck's own fee estimate. None means "not prechecked",
            # never "free" -- there is no fallback for a number nobody published.
            "estimated_fees": row.estimated_fees,
            "bp_cost": round(row.bp_cost, 2),
            "bp_usage_pct": (round(row.bp_cost / available * 100.0, 2)
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
            # The weight the user TYPED and the weight the plan will really use. They
            # differ whenever the grid, a bump or redistribution moved the row, and
            # showing both is what makes moving it acceptable.
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
    available = float(plan.available_buying_power or 0.0)

    return AllocationPlan(
        rows=rows,
        base_notional=plan.base_notional,
        available_buying_power=plan.available_buying_power,
        required_buying_power=required,
        bp_usage_pct=(required / available * 100.0) if available > 0 else 0.0,
        scale_factor=plan.scale_factor,
        # Not recomputed: it is a property of the BASE (labels that could absorb
        # nothing), which un-ticking a row does not alter. Dropping it would
        # silently report a plan as fully deployed when it never was.
        unallocatable_pct=plan.unallocatable_pct,
        total_buy_value=buy_value,
        total_sell_value=sell_value,
        allow_fractional=plan.allow_fractional,
        valuation_mode=plan.valuation_mode,
        allocation_basis=plan.allocation_basis,
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
ADVISORY_MESSAGE_FRAGMENTS = (_INVEST_EXCEEDS_BP_FRAGMENT,)


def even_split_targets(labels: List[LabelTarget]) -> List[LabelTarget]:
    """The "Even split" button: every label gets an equal share of 100%.

    Returns NEW LabelTarget objects with their own symbol LISTS (the SymbolTarget
    objects inside are shared, which is fine -- step 2 replaces a weight by
    assigning to the object the dialog is already editing), so the caller can
    still cancel out of the dialog without having mutated its inputs. The
    remainder lands on the LAST label so the set totals exactly 100.
    """
    items = list(labels or [])
    if not items:
        return []
    return [LabelTarget(label=lt.label, target_pct=pct, symbols=list(lt.symbols),
                        comment=lt.comment)
            for lt, pct in zip(items, even_split_pct(len(items)))]


def steps_validation_messages(labels: List[LabelTarget], *,
                              tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> List[str]:
    """Every reason a REBALANCE cannot proceed, from step 1 AND step 2. Pure.

    Step 1's rule (labels total 100, no duplicates, no negatives, no non-zero
    label without symbols) and step 2's (weights total 100 inside each label, no
    negatives, no symbol repeated in one label) are BOTH already covered by
    ``validate_label_targets``, which delegates the symbol half to
    ``validate_symbol_weights``. This is a named seam for the wizard, not a second
    implementation: re-checking the symbol totals here emitted every one of them
    twice.

    Returns:
        List[str]: EMPTY means the wizard may proceed to the dry-run.
    """
    return validate_label_targets(labels, tolerance=tolerance)


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


def decide_symbol_action(row: "AllocationRow", state: Optional["PositionState"]) -> str:
    """Which of the three submission paths this row takes (decision 14). Pure.

    Long-only: a SELL on a symbol we do not hold would open a short, so it is
    skipped rather than submitted. A row that the engine already marked
    ``skipped`` (no price, precheck rejected) is never traded, and neither is a
    row whose delta was zeroed -- a suppressed trim HOLDS the position, it does
    not become a liquidation.

    "Held" means held BY US: a position with no open Transaction of ours cannot
    be adjusted, because the adjust path resizes a transaction. A BUY there opens
    a fresh transaction; a SELL is refused rather than trimming a position this
    platform does not track.
    """
    if row.skipped or row.side is None or row.delta_quantity == 0:
        return ACTION_SKIP

    held = state is not None and (state.quantity or 0.0) > 0 and bool(state.transaction_ids)
    if held:
        return ACTION_CLOSE if row.target_quantity <= 0 else ACTION_ADJUST

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
    """Buying power the plan is NOT already spending. Recomputed after every move."""
    return (float(plan.available_buying_power or 0.0)
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
    row.bp_cost = row.estimated_value * float(row.bp_factor or 1.0) if after > 0 else 0.0
    row.side = (OrderDirection.BUY if after > 0
                else OrderDirection.SELL if after < 0 else None)
    row.redistributed = True
    # One reason per row, always quoting the ORIGINAL quantity, however many passes
    # touched it -- two half-truths side by side is worse than no reason at all.
    row.reasons = [r for r in row.reasons if not r.startswith(REASON_REDISTRIBUTED_PREFIX)]
    row.reasons.append(REASON_REDISTRIBUTED_FMT.format(
        before=baseline, after=after, label=label))


def _absorber_order(members: List["AllocationRow"]) -> List["AllocationRow"]:
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
      * a skipped or unpriced row -- there is nothing to measure or to trade.
    """
    absorbers = [r for r in members
                 if r.sizing_outcome == SIZING_OUTCOME_NORMAL
                 and float(r.unmet_notional or 0.0) <= MONEY_EPSILON]
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
        absorbers = _absorber_order(members)
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
#: Shown when at least one symbol gets no order at all.
NO_ORDER_NOTICE_FMT = (
    "{count} symbol(s) get NO order at all, leaving {total:,.2f} unallocated - see "
    "'Not traded' below. One whole share of each would be more than {limit:.0f}% of "
    "its target, so buying one is a different trade, not a rounding fix.")
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
        "no_order_notional": sum(float(r.unmet_notional or 0.0) for r in dropped),
        "bumped_rows": len(bumped),
        "bumped_notional": bumped_over,
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
    """The "some symbols get nothing" warning, or ``None`` when every row trades."""
    if summary["no_order_rows"] <= 0:
        return None
    return NO_ORDER_NOTICE_FMT.format(count=summary["no_order_rows"],
                                      total=summary["no_order_notional"],
                                      limit=BUMP_TO_ONE_SHARE_MAX_MULTIPLE * 100.0)


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
#: REPLACED. Three more belong here for the ledger's purposes:
#:
#:   FILLED            complete by definition, and NOT in the terminal set.
#:   DONE_FOR_DAY      the broker will send no further update today, so an
#:                     unfilled residue never fills; waiting on it would wedge
#:                     the run's income overnight. (User decision D5.)
#:   WASHTRADE_LOCKED  our own gate. The order was never sent, so it is as final
#:                     as an order can be, and it is worth exactly 0.
#:
#: UNKNOWN is deliberately ABSENT. "We do not know what this order did" is not
#: "this order is over", and the difference is whether income gets spent.
SETTLED_ORDER_STATUSES = frozenset(OrderStatus.get_terminal_statuses()) | {
    OrderStatus.FILLED,
    OrderStatus.DONE_FOR_DAY,
    OrderStatus.WASHTRADE_LOCKED,
}


@dataclass(frozen=True)
class OrderFill:
    """One order's fill facts, lifted off a ``TradingOrder`` row.

    Plain values, not an ORM row, so this stays IO-free and unit-testable -- the
    same contract ``consume_income_events`` uses for the ledger.

    ``status=None`` is the live collector's spelling for "that order id has no row
    any more", which is an inconsistency, not an emptiness: it is treated as still
    working so the run stalls instead of quietly consuming income.
    """
    order_id: int
    side: Optional[OrderDirection] = None
    status: Optional[OrderStatus] = None
    filled_quantity: float = 0.0
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

        quantity = float(fill.filled_quantity or 0.0)
        if abs(quantity) <= QUANTITY_EPSILON:
            continue

        price = fill.fill_price
        if fill.side is None or price is None or float(price) <= 0:
            totals.settled = False
            totals.unmeasurable_order_ids.append(fill.order_id)
            continue

        value = abs(quantity) * float(price)
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
