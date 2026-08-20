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
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.account_types import MarginInfo, OrderImpact  # noqa: F401 (re-exported)
from ba2_common.core.types import OrderDirection

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

#: Tolerance (percentage points) when checking that label targets total 100.
LABEL_TOTAL_TOLERANCE_PCT = 0.01

#: Quantities closer to zero than this are exactly zero (float noise guard).
QUANTITY_EPSILON = 1e-9

# Reason strings attached to AllocationRow.reasons / AllocationPlan.warnings.
# Pinned here so the UI and the tests agree on the exact text.
REASON_NO_PRICE = "no price - skipped"
REASON_NOT_MARGINABLE = "⚠ not marginable"
REASON_FRACTIONAL = "fractional"
REASON_WHOLE_SHARE_FLOOR = "rounded down to whole shares"
REASON_NEGATIVE_CLAMPED = "negative target clamped to 0"
REASON_CLOSE_TO_ZERO = "target 0 - close position"
REASON_BELOW_MIN_ORDER_FMT = "below broker min order size {size:g} - position held"
REASON_MULTI_LABEL_FMT = "⚠ also in {labels}"
REASON_SCALED_FMT = "scaled ×{factor:.2f} to fit buying power"
#: The fixed part of REASON_SCALED_FMT, derived from it so the two cannot drift.
#: Used to RECOGNISE a scaling reason a row already carries, so that a plan scaled
#: twice (first solve, then broker precheck) reports ONE reason with the compounded
#: factor instead of two that each tell half the story.
REASON_SCALED_PREFIX = REASON_SCALED_FMT.split("{", 1)[0]
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


def round_delta_quantity(delta_notional: float, price: float,
                         margin: Optional[MarginInfo], *, allow_fractional: bool,
                         current_quantity: float,
                         apply_min_order_size: bool = True) -> float:
    """Turn a SIGNED notional delta into a SIGNED, tradeable share delta.

    Used by ``cost`` valuation mode, where the target is expressed against the
    purchase value rather than the share count. The magnitude is rounded DOWN by
    ``round_quantity`` (so trade increments still hold) and a sell is CLAMPED to
    ``current_quantity`` -- long-only, never oversell.

    ``apply_min_order_size=False`` defers the minimum-order check to AFTER that
    clamp, which is the only place it can be applied to the quantity actually
    being sent; ``compute_allocation`` does this so the suppressed row also gets
    a ``REASON_BELOW_MIN_ORDER_FMT`` explaining itself.
    """
    magnitude = round_quantity(abs(float(delta_notional or 0.0)), price, margin,
                               allow_fractional=allow_fractional,
                               apply_min_order_size=apply_min_order_size)
    if delta_notional >= 0:
        return magnitude
    return -min(magnitude, float(current_quantity or 0.0))


def _suppress_below_min_order(delta: float, margin: Optional[MarginInfo],
                              reasons: List[str]) -> float:
    """Zero a signed share delta the broker would refuse as too small, with a reason.

    ``min_order_size`` constrains the ORDER, not the TARGET: when the trade cannot
    be sent, the right answer is to LEAVE THE POSITION WHERE IT IS, not to rewrite
    what we want to hold. Filtering the target instead used to turn "hold the
    3.3333 shares you already have" into a full liquidation.

    Tests the MAGNITUDE, so an unsendable trim is suppressed exactly like an
    unsendable top-up. Appends to ``reasons`` in place -- a silently absent order
    is its own kind of wrong. Returns the delta to use.
    """
    if (delta and margin is not None and margin.min_order_size is not None
            and abs(delta) < float(margin.min_order_size)):
        reasons.append(REASON_BELOW_MIN_ORDER_FMT.format(size=margin.min_order_size))
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
                          *, valuation_mode: str = VALUATION_MODE_COST) -> float:
    """Allocatable base = broker buying power + current value of MANAGED positions.

    Decision 1 of the design; ``valuation_mode`` (decision 5a) selects whether
    "current value" is the cost basis or ``qty x price``. Unmanaged positions are
    deliberately excluded: they are invisible to the page and already reduce
    ``available_buying_power`` naturally. Symbols in ``managed_symbols`` with no
    ``current`` entry contribute 0; a repeated symbol is counted once.

    The Python default is ``cost``, this function's pinned behaviour. Live code
    passes the account's configured mode EXPLICITLY and relies on no default.

    Raises:
        ValueError: if ``available_buying_power`` is None (no fallback for
        balances), or if ``valuation_mode`` is unknown.
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
    raw = float(target_notional) / float(price)
    if allow_fractional and margin is not None and margin.fractionable:
        inc = margin.min_trade_increment
        if inc and inc > 0:
            qty = round(math.floor(round(raw / inc, 9)) * inc, 10)
        else:
            f = 10.0 ** DEFAULT_FRACTIONAL_DECIMALS
            qty = math.floor(raw * f) / f
    else:
        qty = float(math.floor(raw))
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
    substituted. A buy that scales to zero shares is marked ``skipped``.

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
                             allow_fractional=allow_fractional)
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
                       valuation_mode: str = VALUATION_MODE_MARKET) -> AllocationPlan:
    """Solve a full REBALANCE: every managed label, buys and sells.

    Args:
        base_notional: the allocatable base from ``compute_base_notional``.
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
        valuation_mode: ``cost`` or ``market`` (decision 5a). ``market`` targets
            a SHARE COUNT (``target_notional / price``) and deltas against the
            held quantity; ``cost`` targets a PURCHASE VALUE and deltas against
            ``cost_basis``. The Python default is ``market`` -- this function's
            pinned behaviour -- but live code always passes the account's
            configured mode explicitly.

    Behaviour on degenerate input (never raises, always records a reason):
        * a label with no symbols -> allocates nothing, ``target_pct`` added to
          ``plan.unallocatable_pct`` and a ``WARNING_EMPTY_LABEL_FMT`` warning;
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

    No minimum order threshold: every non-zero delta becomes a row (decision 11).
    Short positions are out of scope -- targets are long-only.

    Returns:
        AllocationPlan: one AllocationRow per managed symbol (including zero-delta
        and skipped rows, so the UI can show them) plus plan-level totals.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    current = current or {}
    margin = margin or {}
    plan = AllocationPlan(base_notional=float(base_notional or 0.0),
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
    targets = {}
    target_pcts = {}
    sym_labels = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        if not lt.symbols:
            # An empty label cannot absorb its percentage: it becomes cash left over.
            plan.unallocatable_pct += max(0.0, pct)
            plan.warnings.append(WARNING_EMPTY_LABEL_FMT.format(label=lt.label, pct=pct))
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
            # Target a PURCHASE VALUE: spend the gap between it and the cost basis.
            delta = round_delta_quantity(
                target_notional - current_value(ps, VALUATION_MODE_COST),
                row.price, m, allow_fractional=allow_fractional,
                current_quantity=row.current_quantity,
                apply_min_order_size=False)
        else:
            # Target a SHARE COUNT: target_notional / price, delta vs what is held.
            ideal_quantity = round_quantity(target_notional, row.price, m,
                                            allow_fractional=allow_fractional,
                                            apply_min_order_size=False)
            delta = ideal_quantity - row.current_quantity
            if not frac:
                delta = float(math.floor(delta) if delta > 0 else -math.floor(-delta))
        if abs(delta) < QUANTITY_EPSILON:
            delta = 0.0
        # ONE minimum-order check, on the signed delta both branches produce -- the
        # only quantity that is actually sent to the broker.
        delta = _suppress_below_min_order(delta, m, row.reasons)
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
                             valuation_mode: str = VALUATION_MODE_MARKET) -> AllocationPlan:
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

    ``valuation_mode`` is accepted for call-site symmetry and validated, but does
    not change the arithmetic: an INVEST_LABEL run ADDS a budget on top of the
    existing position rather than rebalancing towards a target value.
    """
    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")
    current = current or {}
    margin = margin or {}
    budget = max(0.0, float(amount or 0.0))
    plan = AllocationPlan(base_notional=budget,
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
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
        qty = _suppress_below_min_order(qty, m, row.reasons)
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
    estimated cost. ``impact.accepted is False`` marks the row ``skipped`` and
    copies ``impact.errors`` into ``row.reasons``.

    Pass the SAME ``margin`` dict the plan was solved with: without it the
    re-solve rebuilds a bare ``MarginInfo`` for each fractional row and so rounds
    on the default 4dp grid, losing ``min_trade_increment`` and
    ``min_order_size`` -- a broker-side rejection waiting to happen.

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
        warnings=list(plan.warnings),
    )
    for row in out.rows:
        impact = (impacts or {}).get(row.symbol)
        if impact is None:
            continue
        if not impact.accepted:
            row.skipped = True
            row.bp_cost = 0.0
            row.reasons.extend(impact.errors)
            continue
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
        if remaining <= QUANTITY_EPSILON:
            break
        available = float(open_amount or 0.0)
        if available <= 0:
            continue
        take = min(available, remaining)
        out.append((event_id, take))
        remaining -= take
    return out
