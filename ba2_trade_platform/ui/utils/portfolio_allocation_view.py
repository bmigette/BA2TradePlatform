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
    current_value, investable_notional, position_sign, signed_position_values,
)

# ``position_sign`` / ``signed_position_values`` are IMPORTED, not defined here,
# and are re-exported under these names so existing callers keep working. They
# moved into the pure engine the day ``build_position_states`` needed them too:
# the sign rule had one implementation and one caller, the live service grew a
# second caller WITHOUT it, and on TastyTrade a short then reached the allocation
# base as a long. A live service module must not import from ``ui/``, so the
# shared rule now lives where both sides already import from.

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

    **Shorts have ONE canonical representation here: signed negative.** A short's
    quantity, cost basis and market value are all negative, so a label's value is
    its NET exposure and every sum on the page is a plain sum. The two live
    brokers disagree at source — Alpaca passes the broker's own negative signs
    straight through (``alpaca_position_to_position``) while TastyTrade stores
    ``qty=abs_qty`` and puts the direction in ``side``
    (``TastyTradeAccount.py:520-547``) — so the same book used to render two
    different pages.

    The rule itself is ``signed_position_values`` in the pure engine, NOT a copy
    here: the live service's ``build_position_states`` normalises the identical
    rows for the allocation base and once did not, which read every TastyTrade
    short as a long. See that function for the three properties it guarantees
    (idempotent, longs untouched, unknown side trusted).

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

        # The ONE sign rule, shared with the live service's
        # ``build_position_states`` so the page and the plan can never disagree
        # about which way a position points.
        quantity, cost_basis, market_value = signed_position_values(
            _probe(row, 'side'), quantity=quantity, cost_basis=cost_basis,
            market_value=market_value)

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

    ``weight_pct`` is the TARGET share of this symbol within its label, and
    ``target_value`` the money that implies given the label's own target and the
    allocatable base. Both are ``None`` when the caller supplied no stored weights
    or no base notional -- there is no fallback, and a 0.00 there would be a fact
    rather than an absence. They are the "target" half of requirement 2 at the
    instrument level; the page previously showed a target exactly once, on the
    group header.
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
    weight_pct: Optional[float] = None
    target_value: Optional[float] = None

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

    ``pct_of_total`` and ``pct_of_base`` are TWO DIFFERENT denominators and the
    distinction is load-bearing. ``pct_of_total`` divides by the distinct MANAGED
    value; ``target_pct`` is a share of ``base_notional`` (buying power PLUS managed
    value). Whenever buying power is non-zero those two are not comparable, and the
    page header printed them next to each other -- inviting the user to type a wrong
    number. ``pct_of_base`` is the one that IS comparable with the target, and
    ``target_value`` is the target as money. Both are ``None`` when no base notional
    was supplied.
    """
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None
    current_value: float = 0.0
    cost_basis: float = 0.0
    pct_of_total: float = 0.0
    rows: List[SymbolRow] = field(default_factory=list)
    pct_of_base: Optional[float] = None
    target_value: Optional[float] = None


def build_label_views(managed,
                      symbols_by_label,
                      positions,
                      prices,
                      symbol_comments=None,
                      *,
                      valuation_mode: str,
                      base_notional: Optional[float] = None,
                      symbol_weights=None,
                      unallocated_pct: float = 0.0) -> List[LabelView]:
    """Build the default view: one LabelView per managed label. Pure.

    ``valuation_mode`` (decision 5a) selects what "current value" means: ``cost``
    (the cost basis) or ``market`` (``qty x price``). It drives BOTH percentage
    columns and both totals, so the page must state which mode produced them.
    A market-mode symbol with no price contributes 0 rather than a guessed value.

    It is REQUIRED and has NO default, matching ``build_base_snapshot`` and the
    three solvers. Whichever way a default pointed, a caller that forgot the keyword
    would get a PAGE measured on one definition of "current value" and a PLAN solved
    on another; the account's stored mode
    (``portfolio_allocation_config.valuation_mode``) is the only right answer and it
    has to be passed in.

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
        base_notional: the allocatable base (buying power + managed value) the
            TARGETS are shares of. Optional, and ``None`` propagates to
            ``pct_of_base`` / ``target_value`` rather than becoming 0.0: a caller
            that has no base has no answer, and 0.00% of base is a wrong one. Also
            treated as absent when it is 0.
        symbol_weights: ``{label: {symbol: weight_pct}}`` from ``get_symbol_weights``.
            Optional, on the same terms.
        unallocated_pct: the account's stored cash reserve, 0-100. It scales the
            TARGET money only -- ``target_value`` on the label and on every symbol
            row -- because the label percentages divide what the reserve LEFT.
            ``pct_of_base`` deliberately keeps dividing the GROSS base, so that it
            and ``UnallocatedRow.pct_of_base`` sit under one denominator and add up
            to 100; netting it here would make a fully invested book read 111%.

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

    # 0 is treated as absent: dividing by it is undefined, and reporting 0.00% of
    # base is a statement rather than an absence.
    base = float(base_notional) if base_notional else None
    # The one scaling, from the engine's own helper so the page cannot show a
    # target the solver would not have used.
    investable = None if base is None else investable_notional(base, unallocated_pct)
    weights_by_label = symbol_weights or {}

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
        label_target_value = (None if investable is None
                              else investable * float(entry.target_pct or 0.0) / 100.0)
        label_weights = weights_by_label.get(entry.label) or {}
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
                weight_pct=label_weights.get(sym),
                target_value=(None if (label_target_value is None
                                       or sym not in label_weights)
                              else label_target_value * float(label_weights[sym]) / 100.0),
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
            pct_of_base=(label_value / base * 100.0) if base else None,
            target_value=label_target_value,
        ))

    return views


@dataclass
class UnallocatedRow:
    """The cash-reserve row, at the TOP of the page's label list. Pure.

    ``target_pct`` is the account's STORED ``unallocated_pct`` -- an editable number
    in its own right, not ``100 - sum(label targets)``. Deriving it from a shortfall
    was tried and superseded: the labels now total 100 by rule, so a derived row
    would read 0 on every correctly configured account, and "labels sum to 90" would
    otherwise mean either a reserve or a typo.

    ``current_value`` is the account's AVAILABLE BUYING POWER -- what is actually
    uninvested right now -- while ``target_value`` is what the reserve says should
    be. The two differ exactly when the book is off target, which is the whole
    reason the row is worth drawing, and it is what makes "raising the reserve sells
    something" visible BEFORE the dry run.

    ``pct_of_base`` is the CURRENT figure as a share of the GROSS base, so it sits
    under the same denominator as every ``LabelView.pct_of_base`` above it and the
    column adds to 100. ``None`` when there is no base to divide by.

    There is deliberately no ``over_pct``. It reported label targets summing past
    100, which is a LABEL error the validator now names directly; carrying it on the
    reserve row conflated two independent numbers on the one line meant to explain
    the cash.
    """
    target_pct: float = 0.0
    target_value: float = 0.0
    current_value: float = 0.0
    pct_of_base: Optional[float] = None


def unallocated_row(*, base_notional: float, available_buying_power: float,
                    unallocated_pct: float) -> UnallocatedRow:
    """Build the cash-reserve row. Pure.

    Every argument is REQUIRED and keyword-only, ``unallocated_pct`` included --
    unlike the same argument on ``build_label_views``, which has other work to do
    without one. This row's entire content IS the reserve, so a caller that forgot
    it would silently draw "0.00% held as cash" over a real one.

    It no longer takes the label ``views``: the reserve used to be derived from
    their shortfall, and now reads nothing but its own stored number. The money
    goes through the engine's ``investable_notional`` so the row and the plan
    cannot disagree about what a reserve is worth.
    """
    base = float(base_notional or 0.0)
    reserved_pct = max(0.0, min(100.0, float(unallocated_pct or 0.0)))
    return UnallocatedRow(
        target_pct=reserved_pct,
        target_value=base - investable_notional(base, reserved_pct),
        current_value=float(available_buying_power or 0.0),
        pct_of_base=((float(available_buying_power or 0.0) / base * 100.0)
                     if base else None),
    )


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


# ---- market-hours gate ------------------------------------------------------
#
# The page and the dry run ALWAYS render: planning at 22:00 is the normal way to
# use this screen. Only SUBMIT is gated. Pure -- `now` is required so no test can
# depend on when it runs.

from datetime import datetime, timedelta
from typing import Sequence, Tuple

from ba2_common.core.account_types import (
    MARKET_HOURS_SOURCE_BROKER,
    MARKET_HOURS_SOURCE_FALLBACK,
    MARKET_HOURS_SOURCE_UNAVAILABLE,
)
from ba2_common.core.market_calendar import NY_TZ

MARKET_GATE_OPEN = "OPEN"
MARKET_GATE_CLOSED = "CLOSED"
MARKET_GATE_UNKNOWN = "UNKNOWN"

#: Provenance of the answer, as published in ``MarketHours.source``. RE-EXPORTS of
#: the interface's constants, never re-spelled literals: a local copy of "broker"
#: is how a broker answer ends up described to the user as a fallback one.
MARKET_SOURCE_BROKER = MARKET_HOURS_SOURCE_BROKER
MARKET_SOURCE_FALLBACK = MARKET_HOURS_SOURCE_FALLBACK
MARKET_SOURCE_UNAVAILABLE = MARKET_HOURS_SOURCE_UNAVAILABLE

#: Display label only. The CONVERSION goes through ``NY_TZ``, the one canonical New
#: York tzinfo object (ba2_common.core.market_calendar).
MARKET_TIMEZONE = "America/New_York"

#: Severity -> stylesheet class. Only warning / danger / info / success exist
#: (ui/static/styles.css); the severity strings themselves are the ``ui.notify``
#: vocabulary ('positive' | 'negative' | 'warning' | 'info' -- 'error' is NOT one
#: of them).
MARKET_BANNER_CLASSES = {
    "warning": "alert-banner warning",
    "negative": "alert-banner danger",
    "info": "alert-banner info",
}

MARKET_MSG_CLOSED_FMT = (
    "Market closed — Submit is disabled. The next regular session opens {when} "
    "({countdown} from now). You can still refresh and review this dry run.")
MARKET_MSG_CLOSED_NO_COUNTDOWN_FMT = (
    "Market closed — Submit is disabled. The next regular session opens {when}. "
    "You can still refresh and review this dry run.")
MARKET_MSG_CLOSED_NO_TIME = (
    "Market closed — Submit is disabled. No next-open time was published, so we "
    "cannot say when it re-enables. You can still refresh and review this dry run.")
MARKET_MSG_UNKNOWN = (
    "Market hours unavailable — Submit is disabled because the market could not be "
    "confirmed open. You can still refresh and review this dry run.")
#: Appended when the answer came from the offline calendar instead of the broker.
#: Both halves matter: the broker did not answer, AND the calendar only knows the
#: regular session, so it reads "closed" during extended hours and cannot know
#: about a broker-specific halt.
MARKET_NOTE_FALLBACK = (
    " These times come from the built-in NYSE calendar, not from the broker — the "
    "broker's own market-hours call did not answer, and the calendar covers the "
    "regular session only.")
#: Shown when the gate ALLOWS on an answer that did not come from the broker. The
#: closed and unknown branches fold their provenance into ``message``; an ALLOW had
#: nowhere to put it, so the user was never told that the thing about to send real
#: orders was a static calendar. Both sentences matter: the broker did not answer,
#: and the calendar cannot know about a halt, a suspension or an account
#: restriction -- only about the scheduled session.
MARKET_MSG_OPEN_FROM_FALLBACK = (
    "Market shown as open from the built-in NYSE calendar — the broker's own "
    "market-hours call did not answer. The calendar knows the scheduled session "
    "only: it cannot see an unscheduled halt, a broker-side suspension or a "
    "restriction on this account. Check before submitting.")

#: Post-submit / income-panel line. Orders that are still working contributed ZERO
#: to the ledger, so the run's income is deliberately NOT consumed yet.
WORKING_ORDERS_NOTICE_FMT = (
    "{count} order(s) still working — this run's income has NOT been consumed yet. "
    "It is picked up automatically on the next refresh once the orders settle.")
#: Said INSTEAD of the line above when the broker refresh FAILED. Not a variant of
#: it: a failed refresh leaves ``working_order_ids`` empty, so the count line reads
#: "0 order(s) still working" — i.e. "nothing outstanding" — for a run whose fills
#: were never confirmed at all.
REFRESH_FAILED_NOTICE = (
    "The broker order refresh FAILED, so nothing about this run's fills is "
    "confirmed and its income has NOT been consumed. Orders may well have gone "
    "out — check the broker. It is re-measured on the next refresh.")

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_market_time(when: datetime) -> str:
    """Render an aware datetime in the market's clock, e.g. ``Mon 05 Jan 2026 09:30 ET``.

    Converts through ``NY_TZ`` -- the same tzinfo object the offline calendar builds
    its sessions with, so a displayed open can never be one object's idea of New York
    and the calculation another's.

    Day and month names come from explicit tuples, not ``strftime('%a %b')``, which
    is locale-dependent and would make the output depend on the machine.

    Raises:
        ValueError: on a naive datetime. Guessing a timezone here would move the
        displayed open by up to a day.
    """
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError(f"format_market_time: {when!r} is naive; an aware datetime is required")
    local = when.astimezone(NY_TZ)
    return (f"{_WEEKDAY_NAMES[local.weekday()]} {local.day:02d} "
            f"{_MONTH_NAMES[local.month - 1]} {local.year} "
            f"{local.hour:02d}:{local.minute:02d} ET")


def format_countdown(delta: timedelta) -> str:
    """``2d 3h`` / ``15h 30m`` / ``42m``; EMPTY for anything at or below zero.

    Empty rather than "0m" so the caller can drop the "(… from now)" clause entirely
    instead of rendering a countdown that has already elapsed.
    """
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return ""
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@dataclass
class MarketGateResult:
    """May the wizard SUBMIT, and what to tell the user if not.

    ``allowed is False`` disables the Submit button ONLY -- the page, the dry run
    and Refresh all keep working, because planning while the market is shut is the
    normal case.

    ``severity`` is the ``ui.notify`` vocabulary ('warning' | 'negative' | 'info');
    ``MARKET_BANNER_CLASSES`` maps it to a stylesheet class.
    """
    allowed: bool
    reason_code: str
    message: str
    severity: str = "info"
    next_open_text: str = ""
    countdown_text: str = ""
    from_fallback: bool = False


def evaluate_market_gate(*, is_open: Optional[bool], next_open: Optional[datetime],
                         source: str, now: datetime) -> MarketGateResult:
    """Decide whether Submit may be pressed. Pure.

    Args:
        is_open: ``None`` when the answer is not known. The ONLY legal caller
            mapping is ``None if (hours is None or not hours.is_known) else
            hours.is_open`` -- an UNAVAILABLE ``MarketHours`` carries
            ``is_open=False`` so the money path fails closed, but the UI must say
            "unknown", not "closed". ``None`` BLOCKS: an unanswered market-hours
            call is not permission to send orders.
        next_open: ``MarketHours.next_open``, tz-AWARE. ``None`` is allowed (the
            broker published none) and blocks with a message that says so.
        source: ``MarketHours.source``, or ``MARKET_SOURCE_UNAVAILABLE`` when there
            is no ``MarketHours`` at all. Anything other than
            ``MARKET_SOURCE_BROKER`` is reported as not having come from the broker.
        now: the reference instant, tz-aware. REQUIRED, so no caller and no test
            silently depends on the wall clock.

    Returns:
        MarketGateResult: ``allowed=True`` only when ``is_open is True``.

    Raises:
        ValueError: if ``now`` or ``next_open`` is naive. Unreachable through the
        seam -- ``MarketHours.__post_init__`` already refuses a naive field -- and
        kept as defence-in-depth for hand-built scalars.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError(f"evaluate_market_gate: now={now!r} is naive")

    if is_open is None:
        # ``from_fallback`` means the same thing in all three branches: this answer
        # did not come from the broker. Leaving it False here would have an
        # unanswered lookup claim broker provenance.
        return MarketGateResult(allowed=False, reason_code=MARKET_GATE_UNKNOWN,
                                message=MARKET_MSG_UNKNOWN, severity="negative",
                                from_fallback=(source != MARKET_SOURCE_BROKER))
    if is_open:
        return MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN,
                                message="", severity="info",
                                from_fallback=(source != MARKET_SOURCE_BROKER))

    from_fallback = source != MARKET_SOURCE_BROKER
    if next_open is None:
        message = MARKET_MSG_CLOSED_NO_TIME
        when = ""
        countdown = ""
    else:
        when = format_market_time(next_open)       # raises on a naive next_open
        countdown = format_countdown(next_open - now)
        message = (MARKET_MSG_CLOSED_FMT.format(when=when, countdown=countdown)
                   if countdown else
                   MARKET_MSG_CLOSED_NO_COUNTDOWN_FMT.format(when=when))
    if from_fallback:
        message += MARKET_NOTE_FALLBACK
    return MarketGateResult(allowed=False, reason_code=MARKET_GATE_CLOSED,
                            message=message, severity="warning",
                            next_open_text=when, countdown_text=countdown,
                            from_fallback=from_fallback)


def market_provenance_notice(gate: MarketGateResult) -> Optional[Tuple[str, str]]:
    """"This ALLOW came from the offline calendar" — or ``None``. Pure.

    ``MarketGateResult.from_fallback`` is set on all three branches of
    ``evaluate_market_gate`` and used to be read by nobody, which made the ALLOW
    case silent in exactly the way that matters: if the broker's ``get_clock()``
    fails, the built-in NYSE calendar answers instead, and on a scheduled trading
    day it says OPEN. The gate then waves a real submission through on the
    strength of a static timetable that cannot know about an unscheduled halt.

    Only the ALLOW case produces a notice. The CLOSED branch already appends
    ``MARKET_NOTE_FALLBACK`` to its own message and the UNKNOWN branch says
    "unavailable" outright, so a second banner there would repeat them; and
    neither of those lets anything be sent.

    Returns:
        Optional[Tuple[str, str]]: ``(text, severity)`` for ``ui.notify`` and for
        ``MARKET_BANNER_CLASSES``, or ``None`` when the broker itself answered (or
        when the gate blocks and has already said so).
    """
    if not gate.allowed or not gate.from_fallback:
        return None
    return MARKET_MSG_OPEN_FROM_FALLBACK, "warning"


def working_orders_notice(*, settled: bool,
                          working_order_ids: Sequence[int],
                          refresh_failed: bool = False) -> Optional[Tuple[str, str]]:
    """"N orders still working" — text and severity, or ``None`` when there is none.

    Orders that have not settled contribute ZERO to a run's filled value, so the
    run's income is deliberately left UNCONSUMED and reconciled later. That is the
    common case here, not the rare one, so the fact has to reach the user rather than
    only the log.

    ``refresh_failed`` WINS over the count, and that is the whole reason it exists.
    ``measure_run_fills`` forces ``settled=False`` when the broker refresh failed,
    but ``working_order_ids`` then comes back EMPTY — our own rows may say FILLED,
    they are simply not evidence — so the count line rendered "0 order(s) still
    working". That reads as "nothing outstanding" for the one case where orders may
    have gone out and nobody can say. Different fact, different sentence.

    Takes plain values rather than a ``FilledTotals`` so it does not depend on the
    ledger chunk: it lands first and starts saying something the moment real
    ``settled`` / ``working_order_ids`` are supplied.

    Returns:
        Optional[Tuple[str, str]]: ``(text, severity)`` for ``ui.notify`` and for
        ``MARKET_BANNER_CLASSES``, or ``None`` when the run settled with a working
        refresh.
    """
    if refresh_failed:
        return REFRESH_FAILED_NOTICE, "negative"
    if settled:
        return None
    return (WORKING_ORDERS_NOTICE_FMT.format(count=len(working_order_ids or [])),
            "warning")
