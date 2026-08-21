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
    AccountSnapshot, MarginInfo, OrderImpact,
)
from ba2_common.core.types import OrderDirection
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
    "QUANTITY_EPSILON", "MONEY_EPSILON",
    # reason / warning / error strings
    "REASON_NO_PRICE", "REASON_NOT_MARGINABLE", "REASON_FRACTIONAL",
    "REASON_WHOLE_SHARE_FLOOR", "REASON_NEGATIVE_CLAMPED", "REASON_CLOSE_TO_ZERO",
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
    # submission
    "ACTION_ADJUST", "ACTION_CLOSE", "ACTION_NEW", "ACTION_SKIP",
    "decide_symbol_action", "split_delta_fifo",
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

# Reason strings attached to AllocationRow.reasons / AllocationPlan.warnings.
# Pinned here so the UI and the tests agree on the exact text.
REASON_NO_PRICE = "no price - skipped"
REASON_NOT_MARGINABLE = "⚠ not marginable"
REASON_FRACTIONAL = "fractional"
REASON_WHOLE_SHARE_FLOOR = "rounded down to whole shares"
REASON_NEGATIVE_CLAMPED = "negative target clamped to 0"
REASON_CLOSE_TO_ZERO = "target 0 - close position"
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
    fractional: bool = False
    skipped: bool = False
    #: The broker precheck's own fee estimate, when one was run and accepted
    #: (``OrderImpact.estimated_fees``). ``None`` means "not prechecked", never
    #: "free" -- no fallback value for a number the broker did not supply.
    estimated_fees: Optional[float] = None
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
            "fractional": self.fractional,
            "skipped": self.skipped,
            "estimated_fees": self.estimated_fees,
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

    for symbol, target_notional in targets.items():
        ps = current.get(symbol)
        m = margin.get(symbol)
        row = AllocationRow(symbol=symbol, labels=list(sym_labels[symbol]))
        row.bp_factor = float(m.bp_factor) if m is not None else float(default_bp_factor)
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
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)

        if target_notional <= 0 and row.current_quantity > 0:
            # Same in both modes: a zero target flattens the position outright.
            # Keyed on the NOTIONAL, never on the rounded quantity: only an
            # actual instruction to hold nothing may liquidate, never a rounding
            # rule that happened to produce 0 shares.
            delta = -row.current_quantity
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
            delta = round_delta_quantity(
                gap, unit_value, m, allow_fractional=allow_fractional,
                current_quantity=row.current_quantity)
        else:
            # Target a SHARE COUNT: target_notional / price, delta vs what is held.
            ideal_quantity = round_quantity(target_notional, row.price, m,
                                            allow_fractional=allow_fractional,
                                            apply_min_order_size=False)
            # Round the DELTA, not just the target: an on-grid target minus an
            # off-grid holding is off-grid, and the delta is what is submitted.
            delta = _round_delta_shares(ideal_quantity - row.current_quantity, m,
                                        allow_fractional=allow_fractional,
                                        current_quantity=row.current_quantity)
        if abs(delta) < QUANTITY_EPSILON:
            delta = 0.0
        # ONE minimum-order check, on the signed delta both branches produce -- the
        # only quantity that is actually sent to the broker.
        delta = _suppress_below_min_order(delta, m, row.reasons, row.price)
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
    ``plan.net_buy_value == plan.total_buy_value``. Buying-power scaling,
    rounding, missing prices and missing margin info behave exactly as in
    ``compute_allocation``, and a symbol repeated inside the label COALESCES into
    one row whose weight is the sum -- again as in ``compute_allocation``.

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
                          valuation_mode=valuation_mode)
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
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        qty = round_quantity(target_notional, row.price, m,
                             allow_fractional=allow_fractional,
                             apply_min_order_size=False)
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)
        # Buys only here, so the budget IS the order: same suppression, same reason.
        qty = _suppress_below_min_order(qty, m, row.reasons, row.price)
        row.delta_quantity = qty
        row.target_quantity = row.current_quantity + qty
        if qty > 0:
            row.side = OrderDirection.BUY
        row.estimated_value = qty * row.price
        row.bp_cost = row.estimated_value * row.bp_factor
        plan.rows.append(row)
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
        net_buy_value: ``max(0, submitted_buy_value - submitted_sell_value)`` -- a
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
    """
    available_buying_power: float
    managed_value: float
    base_notional: float
    default_bp_factor: float
    valuation_mode: str = VALUATION_MODE_COST
    cash: Optional[float] = None
    is_margin_account: bool = False
    supports_fractional: bool = False
    taken_at: DateTime = field(default_factory=lambda: DateTime.now(timezone.utc))
    warnings: List[str] = field(default_factory=list)


def build_base_snapshot(
    snapshot: "AccountSnapshot",
    current: Dict[str, PositionState],
    managed_symbols: List[str],
    *,
    valuation_mode: str = VALUATION_MODE_COST,
) -> BaseSnapshot:
    """Turn a broker AccountSnapshot into the wizard's frozen base.

    Raises:
        ValueError: when there is no snapshot at all, when the broker published no
        ``buying_power`` (a plan sized against a guessed balance is worse than no
        plan), or when ``valuation_mode`` is unknown.
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
        by the buying-power scaler, or by the broker's own precheck refusing it.

    Only the third is suppression. Reporting it is the point: the sub-$5
    fractional floor (TastyTrade ``below_notional_value_minimum``) does not mean
    "this rounds to zero", it means fractional is UNAVAILABLE at that size, and a
    table that drops the row tells the user there was nothing to do.

    Detected from the reasons rather than from a flag because the engine's
    suppression paths are several and each already records WHY -- a parallel
    boolean would be one more thing to forget to set.
    """
    if abs(float(row.delta_quantity or 0.0)) > QUANTITY_EPSILON:
        return False
    if any(reason.startswith(prefix)
           for reason in row.reasons for prefix in _SUPPRESSION_REASON_PREFIXES):
        return True
    # A row the broker's precheck REFUSED is zeroed and marked skipped with the
    # broker's own error strings, which match no format of ours. A no-price skip
    # is the one skip that never had an order, so it is the one exclusion.
    return bool(row.skipped) and REASON_NO_PRICE not in row.reasons


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
    """
    available = float(plan.available_buying_power or 0.0)
    out: List[Dict[str, Any]] = []
    for row in plan.rows:
        suppressed = _is_suppressed_row(row)
        if not suppressed and (row.side is None or row.delta_quantity == 0):
            continue
        out.append({
            "symbol": row.symbol,
            "side": row.side.value if row.side is not None else "",
            "quantity": round(abs(row.delta_quantity), DRY_RUN_QUANTITY_DECIMALS),
            "price": row.price,
            "estimated_value": round(row.estimated_value, 2),
            "bp_cost": round(row.bp_cost, 2),
            "bp_usage_pct": (round(row.bp_cost / available * 100.0, 2)
                             if available > 0 else 0.0),
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
