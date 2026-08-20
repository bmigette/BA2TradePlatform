"""Portfolio allocation arithmetic (pure; no DB, no broker, no UI).

Turns target percentages into per-symbol share deltas:

    label_notional  = base_notional * label.target_pct / 100
    symbol_notional = label_notional * symbol.weight_pct / 100   (targets SUM
                      when a symbol carries more than one managed label)
    target_quantity = round_quantity(symbol_notional, price, ...)
    delta_quantity  = target_quantity - current_quantity
    bp_cost         = |delta_notional| * bp_factor(symbol)       (buys only)

When ``sum(bp_cost of buys) > available_buying_power`` every BUY scales down
pro-rata and the plan records ``scale_factor``. Sells never scale.
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
REASON_MULTI_LABEL_FMT = "⚠ also in {labels}"
REASON_SCALED_FMT = "scaled ×{factor:.2f} to fit buying power"
WARNING_EMPTY_LABEL_FMT = "label '{label}' has no symbols - {pct:.2f}% unallocated"
WARNING_PRECHECK_DISAGREED_FMT = "broker precheck disagreed on {symbol} - re-solved"

# Validation messages from ``validate_label_targets``. Pinned so the UI and the
# tests agree on the exact text.
ERROR_LABEL_TOTAL_FMT = "label targets total {total:.2f}% - must total 100%"
ERROR_LABEL_NEGATIVE_FMT = "label '{label}' has a negative target ({pct:.2f}%)"
ERROR_LABEL_DUPLICATE_FMT = "duplicate label '{label}'"
ERROR_LABEL_NO_SYMBOLS_FMT = "label '{label}' has target {pct:.2f}% but no symbols"


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
    the row was skipped). ``estimated_value`` and ``bp_cost`` are always POSITIVE
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


def round_quantity(target_notional: float, price: float, margin: Optional[MarginInfo],
                   *, allow_fractional: bool) -> float:
    """Convert a notional to a tradeable share quantity.

    Fractional OFF: ``floor(target_notional / price)``.
    Fractional ON: rounded DOWN to ``margin.min_trade_increment`` when the broker
    publishes one, otherwise to ``DEFAULT_FRACTIONAL_DECIMALS`` (4) places.
    Fractional ON but ``margin`` is None or ``margin.fractionable`` is False:
    falls back to whole shares.

    Always rounds DOWN, so a plan never over-spends its notional. Returns 0.0
    when ``price <= 0``; the caller must have already skipped a ``None`` price.
    A result below ``margin.min_order_size`` is returned as 0.0.
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
    if margin is not None and margin.min_order_size is not None and qty < margin.min_order_size:
        return 0.0
    return qty


def _finalise_totals(plan: AllocationPlan) -> None:
    """Fill the plan-level money totals from its rows."""
    plan.total_buy_value = sum(r.estimated_value for r in plan.rows if r.is_buy)
    plan.total_sell_value = sum(r.estimated_value for r in plan.rows if r.is_sell)
    plan.required_buying_power = sum(r.bp_cost for r in plan.rows if r.is_buy)
    plan.bp_usage_pct = (plan.required_buying_power / plan.available_buying_power * 100.0
                         if plan.available_buying_power > 0 else 0.0)


def compute_allocation(base_notional: float, available_buying_power: float,
                       labels: List[LabelTarget], current: Dict[str, PositionState],
                       margin: Dict[str, MarginInfo], *, allow_fractional: bool,
                       default_bp_factor: float) -> AllocationPlan:
    """Solve a full REBALANCE: every managed label, buys and sells.

    ``delta_quantity = target_quantity - current_quantity``, signed. A target of
    zero on a held symbol closes it outright (``REASON_CLOSE_TO_ZERO``) --
    including a fractional holding, which a broker will always let you flatten.
    In whole-share mode any other delta is rounded TOWARDS ZERO to whole shares,
    so the plan can never emit a 0.37-share order; in fractional mode the target
    already sits on the broker's grid and the delta keeps its decimals.
    """
    current = current or {}
    margin = margin or {}
    plan = AllocationPlan(base_notional=float(base_notional or 0.0),
                          available_buying_power=float(available_buying_power or 0.0),
                          allow_fractional=bool(allow_fractional))
    targets = {}
    sym_labels = {}
    for lt in labels or []:
        pct = float(lt.target_pct or 0.0)
        for st in lt.symbols or []:
            share = pct * float(st.weight_pct or 0.0) / 100.0
            targets[st.symbol] = targets.get(st.symbol, 0.0) + plan.base_notional * share / 100.0
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
        row.target_notional = target_notional
        frac = bool(allow_fractional and m is not None and m.fractionable)
        row.fractional = frac
        row.target_quantity = round_quantity(target_notional, row.price, m,
                                             allow_fractional=allow_fractional)
        if frac:
            row.reasons.append(REASON_FRACTIONAL)
        elif allow_fractional:
            row.reasons.append(REASON_WHOLE_SHARE_FLOOR)
        delta = row.target_quantity - row.current_quantity
        if row.target_quantity <= 0 and row.current_quantity > 0:
            delta = -row.current_quantity
            row.reasons.append(REASON_CLOSE_TO_ZERO)
        elif not frac:
            delta = float(math.floor(delta) if delta > 0 else -math.floor(-delta))
        if abs(delta) < QUANTITY_EPSILON:
            delta = 0.0
        row.delta_quantity = delta
        if delta > 0:
            row.side = OrderDirection.BUY
        elif delta < 0:
            row.side = OrderDirection.SELL
        row.estimated_value = abs(delta) * row.price
        row.bp_cost = row.estimated_value * row.bp_factor if delta > 0 else 0.0
        plan.rows.append(row)

    _finalise_totals(plan)
    return plan
