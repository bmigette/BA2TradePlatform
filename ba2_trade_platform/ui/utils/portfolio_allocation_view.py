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


def positions_by_symbol(raw_positions) -> Dict[str, PositionState]:
    """Turn a broker position list into ``{SYMBOL: PositionState}``.

    **Shorts have ONE canonical representation here: signed negative.** A short's
    quantity, cost basis and market value are all negative, so a label's value is
    its NET exposure and every sum on the page is a plain sum. The two live
    brokers disagree at source — Alpaca passes the broker's own negative signs
    straight through (``alpaca_position_to_position``) while TastyTrade stores
    ``qty=abs_qty`` and puts the direction in ``side``
    (``TastyTradeAccount.py:520-547``) — so the same book used to render two
    different pages. Reading ``side`` and forcing the sign is idempotent: an
    already-negative Alpaca short is left alone rather than flipped back to a long.

    Long rows are NOT rewritten: only a short forces a sign, so a broker's own
    numbers are never "corrected" on the strength of a metadata field.

    Args:
        raw_positions: whatever ``account.get_positions()`` returned — a list of
            Position rows (objects or dicts), ``[]`` for a genuinely flat account,
            or ``None`` for a FAILED fetch.

    Returns:
        Dict[str, PositionState]: keyed by normalised (.strip().upper()) symbol.
        Duplicate rows for one symbol are summed, so a long and a short of the
        same symbol net out.

    Raises:
        PositionFetchFailed: when ``raw_positions`` is ``None``. Defined in the
            pure engine so the live service and this module raise the same class.
        ValueError: when a row has no symbol, no quantity or no cost basis — no
            fallback values for quantities or balances (platform rule). A nameless
            row used to be skipped, which silently removed its money from every
            total on the page.
    """
    if raw_positions is None:
        raise PositionFetchFailed(
            "get_positions() returned None: the broker fetch FAILED. No allocation "
            "may be computed against an unknown book — an empty list would mean "
            "genuinely flat, None does not."
        )

    out: Dict[str, PositionState] = {}
    for index, row in enumerate(raw_positions):
        raw_symbol = _probe(row, 'symbol')
        symbol = str(raw_symbol).strip().upper() if raw_symbol else ""
        if not symbol:
            raise ValueError(f"Position row {index} has no symbol — refusing to drop a "
                             f"position whose money would vanish from every total")

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

        sign = position_sign(_probe(row, 'side'))
        if sign == -1:
            quantity = -abs(float(quantity))
            cost_basis = -abs(float(cost_basis))
            if market_value is not None:
                market_value = -abs(float(market_value))

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
    """A managed label's expansion: its totals and its symbol rows.

    ``target_pct`` is the stored target this label is measured against and is
    what the allocation engine reads back as ``LabelTarget.target_pct`` — it is
    carried through untouched, never re-derived.

    There is deliberately NO label-level ``market_value``. The one that used to
    live here summed a MIXED basis — the live quote for priced symbols, the
    broker's own stamped figure for the rest — so it was not comparable with
    anything, it was never rendered, and no test could say what it should be. The
    rows keep their own ``market_value`` (a display column), and ``current_value``
    remains the single mode-aware basis.
    """
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None
    current_value: float = 0.0
    cost_basis: float = 0.0
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

            row_value = _value_of(sym)
            rows.append(SymbolRow(
                symbol=sym,
                labels=list(membership.get(sym, [entry.label])),
                quantity=quantity,
                cost_basis=cost_basis,
                current_value=row_value,
                price=price,
                market_value=market_value,
                pct_of_label=(row_value / label_value * 100.0) if label_value else 0.0,
                pct_of_total=(row_value / total_value * 100.0) if total_value else 0.0,
                comment=comments.get((entry.label, sym)),
            ))

        rows.sort(key=lambda r: (-r.current_value, r.symbol))
        views.append(LabelView(
            label=entry.label,
            target_pct=entry.target_pct,
            comment=entry.comment,
            current_value=label_value,
            cost_basis=sum(positions[s].cost_basis for s in symbols if s in positions),
            pct_of_total=(label_value / total_value * 100.0) if total_value else 0.0,
            rows=rows,
        ))

    return views


def managed_total_value(views) -> float:
    """The DISTINCT managed value of a whole page: every symbol counted ONCE.

    ``sum(v.current_value for v in views)`` is NOT this number. A symbol carrying
    two managed labels contributes to both label totals, so summing them
    double-counts it — 40 symbols currently carry two managed labels — while
    ``build_label_views`` computes ``pct_of_total`` against the distinct
    membership set (decision 7). Using the per-label sum for the headline puts a
    different denominator directly above the rows it is supposed to explain.

    A symbol's ``current_value`` is the same in every label that holds it (it is
    one position under one valuation mode), so de-duplicating by symbol reproduces
    exactly the denominator the rows were divided by.
    """
    seen: Dict[str, float] = {}
    for view in (views or []):
        for row in view.rows:
            seen[row.symbol] = row.current_value
    return float(sum(seen.values()))


def missing_quote_symbols(views) -> List[str]:
    """Symbols that are HELD but have no quote, sorted and de-duplicated.

    In market mode an unpriced position contributes 0 to every total, which is
    indistinguishable on screen from a position that is genuinely flat — a
    bulk-quote outage renders the whole page at $0.00 with no hint that anything
    is missing. The page names these instead. A symbol with no position and no
    quote is not a loss (it is worth 0 either way) and is not reported.
    """
    out = {row.symbol for view in (views or []) for row in view.rows
           if row.price is None and row.quantity}
    return sorted(out)


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

#: Families whose generating expert class no longer exists but whose tags are
#: still on live instrument rows. 'penny-17' and 'penny-4' predate the rename of
#: ``Penny`` to ``PennyMomentumTrader``, so no registry can produce 'penny' any
#: more and a purely derived set would put both tags back in the user's picker.
LEGACY_MACHINE_LABEL_FAMILIES = frozenset({'penny'})

#: Used when the caller passes no families. The live set is DERIVED from the
#: expert registry (``expert_shortname_families``) and passed in by the page; this
#: is only the floor, so an un-wired caller still hides the tags we know about.
DEFAULT_MACHINE_LABEL_FAMILIES = LEGACY_MACHINE_LABEL_FAMILIES | frozenset(
    {'tradingagents', 'fmprating'})


def expert_shortname_families(expert_classes) -> frozenset:
    """The ``<family>-<id>`` prefixes ``MarketExpertInterface.shortname`` generates.

    ``shortname`` is ``f"{self.__class__.__name__.lower()}-{self.id}"`` for every
    expert class that does not override it, and ``InstrumentAutoAdder`` writes that
    string onto each instrument it touches. So the family of an expert is simply
    its lower-cased class name — derive the filter from THAT rule and a newly
    registered expert is hidden the day it ships, instead of leaking into the
    picker until someone remembers to edit a literal.

    Pure: takes the classes, reads only ``__name__``, imports nothing.
    """
    return frozenset(cls.__name__.lower() for cls in (expert_classes or [])
                     if getattr(cls, '__name__', None))


def _machine_family_re(families) -> 're.Pattern':
    """``^(?:family|family|...)-\\d+$``, case-insensitive, families escaped.

    The ``$`` is load-bearing: without it a user label 'penny-17-core' would be
    classified as a machine tag and disappear from the picker. ``^`` likewise, or
    'my-penny-17' would. Escaping is load-bearing too — a class name is
    interpolated straight into this pattern.
    """
    names = sorted({str(f).strip().lower() for f in (families or []) if str(f).strip()})
    if not names:
        return re.compile(r'(?!)')      # matches nothing
    return re.compile(r'^(?:' + '|'.join(re.escape(n) for n in names) + r')-\d+$',
                      re.IGNORECASE)


def is_machine_label(label, machine_families=None) -> bool:
    """True when ``label`` was written by the platform rather than by the user.

    Case-insensitive on both the exact tags and the numbered families. A blank or
    ``None`` label is not a machine label (it is simply dropped by the caller).

    ``machine_families`` is the registry-derived set from
    ``expert_shortname_families``; ``LEGACY_MACHINE_LABEL_FAMILIES`` is always
    added to it, and ``DEFAULT_MACHINE_LABEL_FAMILIES`` is used when it is None.
    The bare family name with no index ('Penny') is a USER label and is
    deliberately not matched.
    """
    text = (label or "").strip()
    if not text:
        return False
    if text.lower() in MACHINE_LABELS:
        return True
    families = (DEFAULT_MACHINE_LABEL_FAMILIES if machine_families is None
                else frozenset(machine_families) | LEGACY_MACHINE_LABEL_FAMILIES)
    return bool(_machine_family_re(families).match(text))


def filter_selectable_labels(all_labels, show_all: bool = False,
                             machine_families=None) -> List[str]:
    """The labels offered in the managed-label picker. Pure.

    Args:
        all_labels: everything ``get_all_instrument_labels()`` returned.
        show_all: the picker's escape hatch — when True nothing is hidden, so a
            user who really does want to manage 'auto_added' still can.
        machine_families: the expert families to hide, from
            ``expert_shortname_families``; None uses the built-in floor.

    Returns:
        List[str]: de-duplicated, blank-stripped, sorted case-insensitively.
    """
    seen, kept = set(), []
    for label in (all_labels or []):
        text = (label or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if show_all or not is_machine_label(text, machine_families):
            kept.append(text)
    return sorted(kept, key=lambda s: s.lower())


def picker_options(all_labels, managed, show_all: bool = False,
                   machine_families=None) -> List[str]:
    """The managed-label picker's OPTION list: selectable labels UNION managed ones.

    A managed label MUST be an option, always. NiceGUI's ``Select`` drops any
    selected value that is not in its options — ``_value_to_model_value`` skips
    what ``self._values.index()`` cannot find, and ``_event_args_to_value`` ends
    with ``[arg for arg in args if arg in self._values]`` — so an absent managed
    label is invisible in the browser AND missing from the very first change
    event. The picker's writer (``replace_managed_labels``) treats that event as
    the whole truth and deletes the label's row along with every per-symbol weight
    and comment beneath it. Silent, irreversible loss of user configuration.

    Two ways a managed label ends up absent from ``all_labels``: its last
    instrument lost the label (so no instrument row carries it any more), or it is
    a machine tag the user deliberately manages while the 'show all' switch is off.
    Both are covered by taking the union.
    """
    kept = filter_selectable_labels(all_labels, show_all=show_all,
                                    machine_families=machine_families)
    seen = set(kept)
    for label in (managed or []):
        text = (label or "").strip()
        if text and text not in seen:
            seen.add(text)
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
