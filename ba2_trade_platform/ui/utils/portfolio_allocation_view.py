"""Pure view-model helpers for the Portfolio Allocation page.

No NiceGUI, no database, no broker SDK: plain data in, plain data out, so every
decision the page makes is unit-testable without a browser. The page module
(``ui/pages/portfolio_allocation.py``) does the IO and hands the results here.

Lives under ``ui/utils/`` rather than beside the page because
``ui/pages/__init__.py`` imports the whole page set (and through it the LLM/expert
stack); ``ui/utils/`` holds only perf_logger and imports in milliseconds.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...core.portfolio_allocation import (
    VALUATION_MODE_COST, VALUATION_MODE_MARKET, PositionFetchFailed, PositionState,
    current_value,
)

# ---- gate reason codes (exact spellings; use these, never bare literals) ----

GATE_OK = "OK"
GATE_NO_ACCOUNT = "NO_ACCOUNT"
GATE_NOT_MANUAL = "NOT_MANUAL"
GATE_HAS_EXPERTS = "HAS_EXPERTS"


@dataclass
class GateResult:
    """Whether the page may run for the current selection, and why not if not.

    ``allowed is False`` means the page renders ``message`` and NOTHING else --
    no broker calls, no plan. ``expert_names`` is populated only for
    ``GATE_HAS_EXPERTS``.
    """
    allowed: bool
    reason_code: str
    message: str
    expert_names: List[str] = field(default_factory=list)


def evaluate_gate(account_id: Optional[int],
                  has_manual_flag: bool,
                  enabled_expert_names) -> GateResult:
    """Decide whether Portfolio Allocation may run. Pure; never raises.

    Precedence is deliberate and tested: with no account selected we cannot even
    know whether the account is manual or expert-driven, so "pick an account" is
    the only actionable message and it wins.

    Args:
        account_id: the global account filter's value; ``None`` == "All accounts".
        has_manual_flag: the account's ``manual_trading_enabled`` setting, read via
            ``get_setting_with_interface_default('manual_trading_enabled',
            log_warning=False)`` -- NOT ``settings.get(...)``, which returns None
            for a never-saved key.
        enabled_expert_names: display names of the account's ENABLED experts.
            Blank/None entries are dropped.

    Returns:
        GateResult: ``allowed=True`` only when an account is selected, it is
        flagged manual, and it has no enabled expert.
    """
    names = [n for n in (enabled_expert_names or []) if n]

    if account_id is None:
        return GateResult(
            allowed=False,
            reason_code=GATE_NO_ACCOUNT,
            message="Pick a single account in the header selector — portfolio "
                    "allocation is computed per account.",
        )

    if not has_manual_flag:
        return GateResult(
            allowed=False,
            reason_code=GATE_NOT_MANUAL,
            message="This account is not flagged as manually traded. Tick "
                    "'Manually traded account' in its Settings to enable this page.",
        )

    if names:
        return GateResult(
            allowed=False,
            reason_code=GATE_HAS_EXPERTS,
            message="This account has enabled experts (" + ", ".join(names) +
                    "). Disable them in Settings before allocating by hand — "
                    "otherwise the experts and this page would fight over the "
                    "same buying power.",
            expert_names=list(names),
        )

    return GateResult(allowed=True, reason_code=GATE_OK, message="")


def _probe(obj: Any, name: str) -> Any:
    """Read ``name`` off a dict OR an object, tolerantly (brokers return both)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def positions_by_symbol(raw_positions) -> Dict[str, PositionState]:
    """Turn a broker position list into ``{SYMBOL: PositionState}``.

    Args:
        raw_positions: whatever ``account.get_positions()`` returned — a list of
            Position rows (objects or dicts), ``[]`` for a genuinely flat account,
            or ``None`` for a FAILED fetch.

    Returns:
        Dict[str, PositionState]: keyed by normalised (.strip().upper()) symbol.
        Duplicate rows for one symbol are summed.

    Raises:
        PositionFetchFailed: when ``raw_positions`` is ``None``. Defined in the
            pure engine so the live service and this module raise the same class.
        ValueError: when a row has no quantity or no cost basis — no fallback
            values for quantities or balances (platform rule).
    """
    if raw_positions is None:
        raise PositionFetchFailed(
            "get_positions() returned None: the broker fetch FAILED. No allocation "
            "may be computed against an unknown book — an empty list would mean "
            "genuinely flat, None does not."
        )

    out: Dict[str, PositionState] = {}
    for row in raw_positions:
        raw_symbol = _probe(row, 'symbol')
        if not raw_symbol:
            continue
        symbol = str(raw_symbol).strip().upper()

        quantity = _probe(row, 'qty')
        if quantity is None:
            quantity = _probe(row, 'quantity')
        if quantity is None:
            raise ValueError(f"Position for {symbol} has no quantity — refusing to "
                             f"substitute a default")

        cost_basis = _probe(row, 'cost_basis')
        if cost_basis is None:
            raise ValueError(f"Position for {symbol} has no cost basis — refusing to "
                             f"substitute a default")

        market_value = _probe(row, 'market_value')

        state = out.get(symbol)
        if state is None:
            state = PositionState(symbol=symbol)
            out[symbol] = state
        state.quantity += float(quantity)
        state.cost_basis += float(cost_basis)
        if market_value is not None:
            state.market_value = (state.market_value or 0.0) + float(market_value)

    return out


@dataclass
class ManagedLabel:
    """One managed label as the page reads it out of ``portfolio_allocation_label``."""
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None


@dataclass
class SymbolRow:
    """One symbol's line in the default view.

    ``current_value`` is what the account holds in this symbol under the active
    valuation mode (cost basis, or ``qty x price`` -- see Task 66);
    ``pct_of_label`` and ``pct_of_total`` are 1-100 of it. ``price`` is ``None``
    when no quote is available; ``market_value`` then falls back to the broker's
    own figure and is ``None`` when there is neither (never a guessed number).
    """
    symbol: str
    labels: List[str] = field(default_factory=list)
    quantity: float = 0.0
    cost_basis: float = 0.0
    current_value: float = 0.0
    price: Optional[float] = None
    market_value: Optional[float] = None
    pct_of_label: float = 0.0
    pct_of_total: float = 0.0
    comment: Optional[str] = None

    @property
    def multi_label(self) -> bool:
        """True when this symbol carries more than one MANAGED label (⚠ in the UI)."""
        return len(self.labels) > 1


@dataclass
class LabelView:
    """A managed label's expansion: its totals and its symbol rows."""
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None
    current_value: float = 0.0
    cost_basis: float = 0.0
    market_value: Optional[float] = None
    pct_of_total: float = 0.0
    rows: List[SymbolRow] = field(default_factory=list)


def build_label_views(managed,
                      symbols_by_label,
                      positions,
                      prices,
                      symbol_comments=None,
                      valuation_mode: str = VALUATION_MODE_COST) -> List[LabelView]:
    """Build the default view: one LabelView per managed label. Pure.

    ``valuation_mode`` (decision 5a) selects what "current value" means: ``cost``
    (the cost basis) or ``market`` (``qty x price``). It drives BOTH percentage
    columns and both totals, so the page must state which mode produced them.
    Defaults to ``cost``, matching ``portfolio_allocation_config.valuation_mode``.
    A market-mode symbol with no price contributes 0 rather than a guessed value.

    Args:
        managed: ``List[ManagedLabel]`` in display order.
        symbols_by_label: ``{label: [symbols]}`` from ``get_symbols_by_label`` — a
            managed label with no instruments maps to an empty list and yields a
            LabelView with no rows.
        positions: ``{SYMBOL: PositionState}`` from ``positions_by_symbol``. A
            symbol absent here is flat, NOT unknown (the caller must already have
            refused a ``None`` fetch).
        prices: ``{SYMBOL: price or None}`` from the bulk quote call.
        symbol_comments: ``{(label, symbol): comment}``; optional.

    Returns:
        List[LabelView]: labels in the given order, rows within each ordered by
        current value descending then symbol. ``pct_of_total`` is computed against
        the DISTINCT managed value, so a symbol in two labels is counted once.
    """
    comments = symbol_comments or {}

    if valuation_mode not in (VALUATION_MODE_COST, VALUATION_MODE_MARKET):
        raise ValueError(
            f"Unknown valuation_mode {valuation_mode!r}; expected "
            f"{VALUATION_MODE_COST!r} or {VALUATION_MODE_MARKET!r}")

    def _clean(label: str) -> List[str]:
        seen, out = set(), []
        for sym in (symbols_by_label or {}).get(label, []) or []:
            s = (sym or "").strip().upper()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    # Membership first, so a multi-label symbol knows all of its managed labels.
    membership: Dict[str, List[str]] = {}
    for entry in managed:
        for sym in _clean(entry.label):
            membership.setdefault(sym, [])
            if entry.label not in membership[sym]:
                membership[sym].append(entry.label)

    def _value_of(sym: str) -> float:
        """This symbol's current value under the active mode, using the LIVE price.

        ``PositionState.price`` is not populated on this path -- the page fetches
        quotes in one bulk call -- so a shallow copy carrying the live price is fed
        to the engine's ``current_value``, keeping ONE definition of the rule.
        """
        state = positions.get(sym)
        if state is None:
            return 0.0
        if valuation_mode == VALUATION_MODE_COST:
            return current_value(state, VALUATION_MODE_COST)
        priced = PositionState(symbol=state.symbol, quantity=state.quantity,
                               cost_basis=state.cost_basis, price=(prices or {}).get(sym))
        return current_value(priced, VALUATION_MODE_MARKET)

    total_value = sum(_value_of(sym) for sym in membership)

    views: List[LabelView] = []
    for entry in managed:
        symbols = _clean(entry.label)
        label_value = sum(_value_of(s) for s in symbols)
        label_market_value: Optional[float] = None
        rows: List[SymbolRow] = []

        for sym in symbols:
            state = positions.get(sym)
            quantity = state.quantity if state is not None else 0.0
            cost_basis = state.cost_basis if state is not None else 0.0
            price = (prices or {}).get(sym)
            if price is not None:
                market_value = quantity * price
            elif state is not None:
                market_value = state.market_value
            else:
                market_value = None
            if market_value is not None:
                label_market_value = (label_market_value or 0.0) + market_value

            row_value = _value_of(sym)
            rows.append(SymbolRow(
                symbol=sym,
                labels=list(membership.get(sym, [entry.label])),
                quantity=quantity,
                cost_basis=cost_basis,
                current_value=row_value,
                price=price,
                market_value=market_value,
                pct_of_label=(row_value / label_value * 100.0) if label_value > 0 else 0.0,
                pct_of_total=(row_value / total_value * 100.0) if total_value > 0 else 0.0,
                comment=comments.get((entry.label, sym)),
            ))

        rows.sort(key=lambda r: (-r.current_value, r.symbol))
        views.append(LabelView(
            label=entry.label,
            target_pct=entry.target_pct,
            comment=entry.comment,
            current_value=label_value,
            cost_basis=sum(positions[s].cost_basis for s in symbols if s in positions),
            market_value=label_market_value,
            pct_of_total=(label_value / total_value * 100.0) if total_value > 0 else 0.0,
            rows=rows,
        ))

    return views


def collect_managed_symbols(symbols_by_label) -> List[str]:
    """Every distinct symbol across the managed labels, normalised and sorted.

    This is the bulk-quote request list for
    ``account.get_instrument_current_price(symbols)``: ONE call for the whole page,
    deduplicated so a symbol carrying two managed labels is quoted once.

    Normalisation happens BEFORE de-duplication, so a legacy ``tsla`` instrument
    row and a modern ``TSLA`` one collapse to the single key
    ``build_label_views`` will look the price up under.
    """
    out = set()
    for symbols in (symbols_by_label or {}).values():
        for sym in (symbols or []):
            text = (sym or "").strip().upper()
            if text:
                out.add(text)
    return sorted(out)


#: Machine-written instrument tags that must not appear in the managed-label picker.
MACHINE_LABELS = frozenset({'auto_added', 'expert_selected', 'ai_selected', 'not_found'})

#: Per-expert-instance tags written by InstrumentAutoAdder: 'penny-17',
#: 'tradingagents-16', 'fmprating-18'. The bare family name without an index
#: ('Penny') is a USER label and is deliberately NOT matched.
MACHINE_LABEL_FAMILY_RE = re.compile(r'^(?:penny|tradingagents|fmprating)-\d+$',
                                     re.IGNORECASE)


def is_machine_label(label) -> bool:
    """True when ``label`` was written by the platform rather than by the user.

    Case-insensitive on both the exact tags and the numbered families. A blank or
    ``None`` label is not a machine label (it is simply dropped by the caller).
    """
    text = (label or "").strip()
    if not text:
        return False
    if text.lower() in MACHINE_LABELS:
        return True
    return bool(MACHINE_LABEL_FAMILY_RE.match(text))


def filter_selectable_labels(all_labels, show_all: bool = False) -> List[str]:
    """The labels offered in the managed-label picker. Pure.

    Args:
        all_labels: everything ``get_all_instrument_labels()`` returned.
        show_all: the picker's escape hatch — when True nothing is hidden, so a
            user who really does want to manage 'auto_added' still can.

    Returns:
        List[str]: de-duplicated, blank-stripped, sorted case-insensitively.
    """
    seen, kept = set(), []
    for label in (all_labels or []):
        text = (label or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if show_all or not is_machine_label(text):
            kept.append(text)
    return sorted(kept, key=lambda s: s.lower())


def diff_managed_labels(current, selected):
    """Return ``(to_add, to_remove)`` for a managed-label selection change. Pure.

    Both sides are normalised (stripped, de-duplicated, blank/None dropped) and the
    results are sorted, so persistence is order-independent and idempotent:
    re-saving the same selection returns two empty lists, which lets the eager
    on-change handler skip a pointless write.

    Case is NOT folded: ``get_symbols_by_label`` matches instrument labels raw, so
    'ark26' and 'ARK26' are two different baskets and swapping one for the other
    is a real change.
    """
    cur = {s.strip() for s in (current or []) if s and s.strip()}
    sel = {s.strip() for s in (selected or []) if s and s.strip()}
    return sorted(sel - cur), sorted(cur - sel)
