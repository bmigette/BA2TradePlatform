"""Pure view-model helpers for the Portfolio Allocation page.

No NiceGUI, no database, no broker SDK: plain data in, plain data out, so every
decision the page makes is unit-testable without a browser. The page module
(``ui/pages/portfolio_allocation.py``) does the IO and hands the results here.

Lives under ``ui/utils/`` rather than beside the page because
``ui/pages/__init__.py`` imports the whole page set (and through it the LLM/expert
stack); ``ui/utils/`` holds only perf_logger and imports in milliseconds.
"""
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...core.portfolio_allocation import (
    ERROR_LABEL_TOTAL_FMT, ERROR_LABEL_UNDER_FMT, LABEL_TOTAL_TOLERANCE_PCT,
    VALUATION_MODE_COST, VALUATION_MODE_MARKET, PositionFetchFailed, PositionState,
    clamp_unallocated_pct, current_value, effective_target_pct, investable_notional,
    position_sign, reserved_notional_for, signed_position_values,
    validate_unallocated_pct,
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


# ---------------------------------------------------------------------------
# LABEL COLOURS
#
# A FIXED palette, deliberately, not a free colour picker. Two reasons and both
# matter: this UI is dark-themed, so an arbitrary picker produces choices the user
# cannot read back (and cannot tell apart from the next label's); and the value
# ends up interpolated into a CSS ``style`` attribute, so an unbounded one is a
# place to put something that is not a colour. ``resolve_label_icon_color`` is a
# WHITELIST lookup for exactly that reason -- only a hex that is in this tuple can
# ever reach the DOM.
# ---------------------------------------------------------------------------

#: Okabe & Ito's Colour Universal Design set, in their published order, MINUS
#: black. It is the standard qualitative palette validated for protanopia,
#: deuteranopia and tritanopia: the seven hues stay distinguishable to all three,
#: they differ in LIGHTNESS as well as hue (so a greyscale or monochrome reading
#: still separates them), and no pair of them is a red-versus-green distinction
#: carrying meaning on its own.
#:
#: Black is dropped because the page's surface is near-black; every survivor clears
#: WCAG 1.4.11's 3:1 non-text contrast floor against #1E1E1E (the darkest, Blue
#: #0072B2, is 3.2:1), which is the bar for a graphical object such as an icon.
#: Nothing here is invented: substituting a "nicer" hex would silently leave the
#: validated set.
LABEL_COLOR_PALETTE = (
    ('Orange', '#E69F00'),
    ('Sky blue', '#56B4E9'),
    ('Bluish green', '#009E73'),
    ('Yellow', '#F0E442'),
    ('Blue', '#0072B2'),
    ('Vermillion', '#D55E00'),
    ('Reddish purple', '#CC79A7'),
)

#: The "no colour chosen" option's VALUE. The empty string rather than ``None``:
#: NiceGUI's ``Select`` treats ``None`` as "nothing selected" and drops any value
#: that is not among its options, so the cleared state needs a real one. It is
#: mapped to SQL NULL on the way into the store -- NULL means "no colour chosen",
#: which is a different fact from a stored default.
NO_LABEL_COLOR = ''
NO_LABEL_COLOR_CAPTION = 'No colour'

#: What an uncoloured label's icon is drawn in. A rendering fallback, NOT a stored
#: default: nothing writes this to the database, and ``LabelView.color`` stays
#: ``None`` so "the user has not chosen" remains readable everywhere above.
DEFAULT_LABEL_ICON_COLOR = '#9AA0A6'

_PALETTE_BY_HEX = {hex_value.upper(): hex_value for _n, hex_value in LABEL_COLOR_PALETTE}


def label_color_options() -> Dict[str, str]:
    """``{value: caption}`` for the colour picker, "No colour" FIRST.

    First because it is the state every label starts in and the one the user has to
    be able to get back to: a palette with no way out means a colour, once set,
    can never be cleared.
    """
    options = {NO_LABEL_COLOR: NO_LABEL_COLOR_CAPTION}
    for name, hex_value in LABEL_COLOR_PALETTE:
        options[hex_value] = name
    return options


def normalise_label_color(raw) -> Optional[str]:
    """The WRITE path: a palette hex in canonical case, or ``None`` for no colour.

    Case-insensitive and whitespace-tolerant, because the value makes a round trip
    through a widget and a database column.

    Raises:
        ValueError: for anything that is not in ``LABEL_COLOR_PALETTE``. Refusing
        rather than falling back is the point -- a value that is not a palette entry
        is either a bug in the caller or an attempt to put something other than a
        colour into a CSS ``style`` attribute, and silently storing it would make
        ``resolve_label_icon_color``'s whitelist the only thing standing in the way.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    canonical = _PALETTE_BY_HEX.get(text.upper())
    if canonical is None:
        raise ValueError(
            f"{raw!r} is not one of the {len(LABEL_COLOR_PALETTE)} palette colours "
            f"({', '.join(h for _n, h in LABEL_COLOR_PALETTE)}) — the picker is a "
            f"fixed set on purpose; see LABEL_COLOR_PALETTE")
    return canonical


def store_color_value(raw) -> str:
    """What to hand ``set_managed_label(color=...)``. NEVER ``None``.

    ``None`` means LEAVE UNCHANGED in the store -- that is how the target and comment
    writers avoid wiping each other -- so "the user picked No colour" cannot travel
    as the ``None`` ``normalise_label_color`` returns for it, or clearing a colour
    would silently do nothing and the swatch would be un-removable. It travels as
    ``''``, which the store maps to SQL NULL.

    Still refuses a value outside the palette, because it IS ``normalise_label_color``
    with the empty case pinned.
    """
    return normalise_label_color(raw) or ''


def resolve_label_icon_color(stored) -> str:
    """The READ path: the hex to draw a label's icon in. Always returns something.

    TOLERANT where ``normalise_label_color`` refuses, and the asymmetry is
    deliberate: a row hand-edited in sqlite must not take the page down, and it must
    not reach the ``style`` attribute either. Unrecognised values fall back to the
    neutral grey, so the whitelist -- not the caller -- decides what gets rendered.
    """
    if stored is None:
        return DEFAULT_LABEL_ICON_COLOR
    return _PALETTE_BY_HEX.get(str(stored).strip().upper(), DEFAULT_LABEL_ICON_COLOR)


@dataclass
class ManagedLabel:
    """One managed label as the page reads it out of ``portfolio_allocation_label``.

    ``color`` is the stored palette hex or ``None``. NULL is "no colour chosen",
    which is a different fact from a stored default, so it is carried through as
    ``None`` all the way to the render and only turned into a drawable colour there
    (``resolve_label_icon_color``).
    """
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None
    color: Optional[str] = None


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
    #: The stored palette hex, or ``None`` for "no colour chosen". Carried through
    #: UNRESOLVED so the absence stays readable; the render turns it into something
    #: drawable with ``resolve_label_icon_color``.
    color: Optional[str] = None


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
            color=entry.color,
        ))

    return views


# ---------------------------------------------------------------------------
# INLINE TARGET EDITING
#
# Targets used to be settable ONLY inside the Allocate wizard, so on a page that
# had never been through it every label sat at ``target_pct = 0`` while the symbol
# table happily printed "TARGET % 20" -- the lazy even-split default from
# ``get_symbol_weights`` -- resolving to a TARGET VALUE of $0.00, because 20% of a
# 0% label is nothing. The page showed a plausible number that meant nothing.
#
# The boxes are on the page now, and they persist ON CHANGE (this page has no Save
# button -- see the page module's docstring). That moves the wizard's validation
# onto the page with them: everything below is the DECISION half, pure and
# testable, and the page only shows what these return.
# ---------------------------------------------------------------------------

EDIT_OK = "OK"
EDIT_BLANK = "BLANK"
EDIT_NOT_A_NUMBER = "NOT_A_NUMBER"
EDIT_NEGATIVE = "NEGATIVE"
EDIT_OVER_100 = "OVER_100"
EDIT_LABELS_OVER_100 = "LABELS_OVER_100"

#: A cleared ``ui.number`` yields ``None``, and reading that as 0.0 would tell the
#: engine to hold NONE of this label -- i.e. a cleared box becomes a sell order.
#: Different fact, different answer: nothing is written and the box goes back.
EDIT_MSG_BLANK_FMT = ("{what} was left empty — nothing was saved. An empty box is "
                      "not 0%: 0% means 'hold none of this'.")
EDIT_MSG_NOT_A_NUMBER_FMT = "{what}: {raw!r} is not a number — enter a percentage, 0-100."
EDIT_MSG_NEGATIVE_FMT = "{what}: {value:g}% is below 0% — a target cannot be negative."
EDIT_MSG_OVER_100_FMT = "{what}: {value:g}% is above 100% — a target is a share, not a multiple."
#: The over-allocation refusal. The engine's own sentence is embedded verbatim so
#: that the inline refusal and the dry run's ``validate_label_targets`` describe the
#: same defect in the same words; the rest names the box and the way out, because
#: "118%" alone leaves the user hunting for which label to change.
EDIT_MSG_LABELS_OVER_100_FMT = (
    "'{label}' at {value:g}% was NOT saved — {engine}. Lower another label first.")


@dataclass
class TargetEdit:
    """One attempted edit of a percentage box, decided. Pure.

    ``accepted is False`` means NOTHING is persisted and the widget is put back to
    the stored value -- a rejected edit never leaves a number on screen the database
    does not have, which is the exact defect ("a plausible-looking number that means
    nothing") this whole feature exists to remove.

    ``value`` is the parsed float on acceptance and ``None`` on a blank box; on the
    other refusals it carries the number that was refused, so the message can name
    it.
    """
    accepted: bool
    value: Optional[float]
    reason_code: str
    message: str = ""


def parse_pct(raw) -> TargetEdit:
    """Read a percentage out of whatever a Quasar number box handed back. Pure.

    Reads the NUMBER only; the range belongs to the three validators below, which
    genuinely disagree about it -- a symbol weight is bounded 0-100, a label target
    also has to fit under 100 alongside its siblings, and the reserve reports an
    out-of-range value in the ENGINE's words.

    Three refusals that a naive ``float(raw)`` would miss:

    * ``None`` / ``''`` -- a CLEARED box, which is not 0. See ``EDIT_MSG_BLANK_FMT``.
    * ``True`` / ``False`` -- ``float(True) == 1.0`` and ``isinstance(True, int)`` is
      True, so a switch wired to the wrong handler would silently store a 1% target.
    * ``nan`` / ``inf`` -- ``nan < 0`` and ``nan > 100`` are BOTH False, so every
      range check waves it through and every derived figure on the page becomes NaN.

    A trailing ``%`` and thousands separators are tolerated: the boxes carry
    ``suffix='%'`` and Quasar can hand back the raw string it is holding.
    """
    if isinstance(raw, bool):
        return TargetEdit(False, None, EDIT_NOT_A_NUMBER, "")
    if raw is None:
        return TargetEdit(False, None, EDIT_BLANK, "")
    if isinstance(raw, str):
        text = raw.strip().rstrip('%').strip().replace(',', '')
        if not text:
            return TargetEdit(False, None, EDIT_BLANK, "")
        try:
            number = float(text)
        except ValueError:
            return TargetEdit(False, None, EDIT_NOT_A_NUMBER, "")
    else:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return TargetEdit(False, None, EDIT_NOT_A_NUMBER, "")
    if not math.isfinite(number):
        return TargetEdit(False, None, EDIT_NOT_A_NUMBER, "")
    return TargetEdit(True, number, EDIT_OK, "")


def _describe_pct(raw, what: str) -> TargetEdit:
    """``parse_pct`` with the messages filled in for a named box. Pure, internal."""
    parsed = parse_pct(raw)
    if parsed.accepted:
        return parsed
    if parsed.reason_code == EDIT_BLANK:
        return TargetEdit(False, None, EDIT_BLANK, EDIT_MSG_BLANK_FMT.format(what=what))
    return TargetEdit(False, None, EDIT_NOT_A_NUMBER,
                      EDIT_MSG_NOT_A_NUMBER_FMT.format(what=what, raw=raw))


def _range_0_100(value: float, what: str,
                 tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> Optional[TargetEdit]:
    """The plain 0-100 refusal, or ``None`` when the value is in range. Pure.

    The UPPER bound carries the engine's ``LABEL_TOTAL_TOLERANCE_PCT`` and the lower
    one does not, which mirrors ``validate_label_targets`` exactly: it measures the
    100 with a 0.01pp tolerance (a 2dp split of three ways totals 100.01) and
    refuses a negative outright. A hard 100 here would refuse a lone label the
    engine accepts; a tolerant 0 would accept a -0.005 the engine refuses.
    """
    if value < 0.0:
        return TargetEdit(False, value, EDIT_NEGATIVE,
                          EDIT_MSG_NEGATIVE_FMT.format(what=what, value=value))
    if value > 100.0 + tolerance:
        return TargetEdit(False, value, EDIT_OVER_100,
                          EDIT_MSG_OVER_100_FMT.format(what=what, value=value))
    return None


def validate_symbol_weight_edit(*, label: str, symbol: str, raw) -> TargetEdit:
    """One symbol's TARGET % box, inside one label. Pure.

    Bounded 0-100 and nothing else: the weights within a label must total 100, but
    a user typing the second of three boxes is legitimately mid-way through a set
    that does not, so a running-total refusal here would make the table unusable.
    The engine's ``validate_symbol_weights`` still gates SUBMIT on the total.

    0 and 100 are both legal: 0 is an explicit "hold none of this" (and is
    deliberately never re-read as "unstored" -- see ``_write_symbol_comment``), 100
    is a single-symbol label.
    """
    what = f"{label} / {symbol}"
    parsed = _describe_pct(raw, what)
    if not parsed.accepted:
        return parsed
    return _range_0_100(parsed.value, what) or parsed


def validate_label_target_edit(*, label: str, raw, other_targets,
                               tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> TargetEdit:
    """One label's own TARGET % box, against its siblings. Pure.

    THE over-100 guard, and it is on the inline path deliberately: the Allocate
    wizard has always refused a set summing past 100 without needing a dry run, and
    an inline box that persisted one anyway would let the user save a configuration
    the engine will not run and only find out two screens later.

    ``other_targets`` is a ``{label: pct}`` of the OTHER labels; an entry for
    ``label`` itself is DROPPED, or lowering an already-over-target label would be
    impossible. ``tolerance`` is the engine's own ``LABEL_TOTAL_TOLERANCE_PCT``
    (0.01pp), so the two-decimal splits this page offers cannot be refused here and
    accepted there.

    UNDER 100 is deliberately allowed even though ``validate_label_targets`` calls
    it an error: that check is a SUBMIT gate, and the user has to be able to pass
    through 40/0 on the way to 40/60. The page reports the shortfall; it does not
    refuse the keystroke.
    """
    what = f"label '{label}'"
    parsed = _describe_pct(raw, what)
    if not parsed.accepted:
        return parsed
    refusal = _range_0_100(parsed.value, what, tolerance)
    if refusal is not None:
        return refusal
    others = sum(float(pct or 0.0) for name, pct in (other_targets or {}).items()
                 if name != label)
    total = others + parsed.value
    if total > 100.0 + tolerance:
        return TargetEdit(
            False, parsed.value, EDIT_LABELS_OVER_100,
            EDIT_MSG_LABELS_OVER_100_FMT.format(
                label=label, value=parsed.value,
                engine=ERROR_LABEL_TOTAL_FMT.format(total=total, over=total - 100.0)))
    return parsed


def validate_reserve_edit(raw) -> TargetEdit:
    """The cash-reserve box on the page header. Pure.

    The range check is DELEGATED to the engine's ``validate_unallocated_pct`` rather
    than re-spelled, message and all: that validator has no tolerance on purpose
    (this is one number the user typed, not a sum of boxes) and its sentence prints
    ``{pct:g}`` so that a refused 100.005 is not rounded into the range it is being
    refused for.

    100% is a legitimate setting -- allocate nothing this cycle -- and 0% is the
    default. Both ends are accepted.
    """
    parsed = _describe_pct(raw, "the unallocated reserve")
    if not parsed.accepted:
        return parsed
    problems = validate_unallocated_pct(parsed.value)
    if problems:
        code = EDIT_NEGATIVE if parsed.value < 0.0 else EDIT_OVER_100
        return TargetEdit(False, parsed.value, code, problems[0])
    return parsed


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


# ---------------------------------------------------------------------------
# LIVE DERIVED FIGURES
#
# Every one of these has TWO callers -- the first render and the in-place update
# after a box changes -- and that is the point. Inline editing is only worth having
# if the consequence of an edit is visible immediately, and a second copy of the
# arithmetic in the update path is how a typed number and a reloaded page start
# disagreeing.
#
# The money always goes through the engine's own ``investable_notional`` /
# ``reserved_notional_for`` / ``effective_target_pct``: the reserve is scaled in
# exactly one place, so a figure drawn here cannot differ from the one the solver
# used. Note that every one of those MULTIPLIES by ``(100 - r)/100``. The inverse
# -- restating a share of the base back as a label weight -- would need
# ``/(1 - r/100)`` and is undefined at r = 100 (a legitimate setting: allocate
# nothing). It is deliberately not computed anywhere.
# ---------------------------------------------------------------------------

#: The label group header. "PORTFOLIO target", said out loud, because two different
#: quantities were both called "target" and neither stated its denominator: the
#: label's share of the whole portfolio, and -- in the table directly underneath --
#: each symbol's share of ITS LABEL. A user looking at "target 0.0%" above a column
#: of 20s asked, reasonably, which of them was wrong. Neither was.
#:
#: The figure printed is the target restated against the GROSS base
#: (``effective_target_pct``), so it sits on the same scale as the holding beside it
#: and as the mini-bar's notch. The stored number -- a share of what the reserve
#: LEFT -- is what the edit box holds, and the tooltip below reconciles the two.
#:
#: Short, deliberately. The old line carried ", target N% of what the reserve leaves
#: = $X, i.e. M% of base" on EVERY row: about a hundred characters, identical eight
#: times over, and the clause that actually varied was buried in the middle of it.
LABEL_HEADER_WITH_BASE_FMT = (
    '{label} — ${current:,.2f} ({pct_of_base:.1f}% of base, portfolio target '
    '{effective_pct:.1f}%)')
#: No base notional means no denominator the target could be restated against, so
#: the line says so instead of printing a share of a number it does not have.
LABEL_HEADER_NO_BASE_FMT = (
    '{label} — ${current:,.2f} ({pct_of_total:.1f}% of managed, portfolio target '
    'unavailable — no base notional)')

#: The ⓘ tooltip that took the clause off the header. Everything the line used to
#: print is still here, plus the sentence that resolves the two "targets".
LABEL_TARGET_TOOLTIP_FMT = (
    'Portfolio target: {target_pct:.1f}% of what the reserve leaves = '
    '${target_value:,.2f}, i.e. {effective_pct:.1f}% of the base. The table below '
    'splits that money by each row’s share of the label.')
LABEL_TARGET_TOOLTIP_NO_BASE_FMT = (
    'Portfolio target: {target_pct:.1f}% of what the reserve leaves. The broker '
    'published no base notional, so there is no dollar figure yet. The table below '
    'splits that money by each row’s share of the label.')

#: The cash-reserve line above the labels. ``.2f`` on the percentage where the
#: label headers use ``.1f``, and "of base" said out loud in both, because the two
#: percentages have DIFFERENT denominators and in identical grammar they read as
#: one column that sums past 100.
RESERVE_ROW_FMT = ('Unallocated (free buying power) — ${current:,.2f} '
                   '({pct_of_base:.1f}% of base, target {target_pct:.2f}% of base '
                   '= ${target_value:,.2f})')

#: Beside the reserve slider. States BOTH halves: what is held back and what is
#: left, because the slider's whole job is to make that trade visible.
RESERVE_CAPTION_FMT = '= ${reserved:,.2f} held back, ${investable:,.2f} investable'
#: Said instead when the broker published no buying power. A "$0.00 held back"
#: there would be a statement about money rather than an absence of one.
RESERVE_CAPTION_NO_BASE = ('no base notional yet — the broker published no buying '
                           'power, so this reserve has no dollar figure')


def format_label_header(*, label: str, current_value: float, target_pct: float,
                        pct_of_base: Optional[float], pct_of_total: float,
                        base_notional: Optional[float],
                        unallocated_pct: float) -> str:
    """The label group header line. Pure.

    ``target_value`` is RECOMPUTED here from ``base_notional`` and the reserve
    rather than taken from the ``LabelView``: the label box and the reserve slider
    are both live, so a precomputed figure would be stale the instant either moved
    -- which is the defect inline editing exists to remove. It lands on exactly
    ``build_label_views``'s number because both call ``investable_notional``.

    ``pct_of_base`` is the CURRENT holding as a share of the gross base and does not
    move with the reserve, so it is passed in; ``None`` selects the no-base branch.
    """
    if pct_of_base is None:
        return LABEL_HEADER_NO_BASE_FMT.format(label=label, current=current_value,
                                               pct_of_total=pct_of_total)
    return LABEL_HEADER_WITH_BASE_FMT.format(
        label=label, current=current_value, pct_of_base=pct_of_base,
        effective_pct=effective_target_pct(target_pct, unallocated_pct))


def format_label_target_tooltip(*, target_pct: float,
                                base_notional: Optional[float],
                                unallocated_pct: float) -> str:
    """The ⓘ beside a label header. Pure.

    Holds the clause the header used to repeat on every row -- the stored target,
    the money it comes to, and the same figure restated against the gross base --
    plus the sentence that resolves the naming collision this redesign is about:
    the header's target is a share of the PORTFOLIO, the table's ``Share of label %``
    is a share of THE LABEL.
    """
    if not base_notional:
        return LABEL_TARGET_TOOLTIP_NO_BASE_FMT.format(target_pct=target_pct)
    return LABEL_TARGET_TOOLTIP_FMT.format(
        target_pct=target_pct,
        target_value=(investable_notional(base_notional, unallocated_pct)
                      * float(target_pct or 0.0) / 100.0),
        effective_pct=effective_target_pct(target_pct, unallocated_pct))


def symbol_target_values(weights, *, label_target_pct: float,
                         base_notional: Optional[float],
                         unallocated_pct: float) -> Dict[str, Optional[float]]:
    """``{symbol: target money}`` for one label's TARGET VALUE column. Pure.

    The reserve is applied ONCE, on the label's money, and the weights then divide
    what is left of it -- applying the factor again per symbol is the obvious bug
    and ``build_label_views`` has a test pinning the same arithmetic.

    ``None`` (never 0.0) for every symbol when there is no base: 0.00 in that column
    would be a claim about money rather than the absence of an answer. A base of
    exactly 0 is treated as absent for the same reason it is in
    ``build_label_views`` -- it is not a denominator, and a brand-new account is a
    real state.
    """
    if not base_notional:
        return {symbol: None for symbol in (weights or {})}
    label_money = (investable_notional(base_notional, unallocated_pct)
                   * float(label_target_pct or 0.0) / 100.0)
    return {symbol: label_money * float(weight or 0.0) / 100.0
            for symbol, weight in (weights or {}).items()}


# ---------------------------------------------------------------------------
# THE LABEL MINI-BAR ROW
#
# One bar per label: the CURRENT share, tinted with that label's own colour, a
# NOTCH marking the target, and a status WORD.
#
# THE BAR AND THE NOTCH SHARE ONE SCALE, and that is the load-bearing property of
# this whole section. If they did not, "over" and "under" would be a lie that no
# reader could catch by eye -- the two marks would simply sit in the wrong order.
# Both go through ``_bar_fraction`` against the same ``bar_scale_pct``.
#
# The scale is the LARGEST figure on the page, not 100% of the base. Against 100
# every bar on a diversified book is stubby (eight labels at 12% each fill an
# eighth of the track apiece) and the differences that matter are invisible;
# against the page maximum the biggest row fills the track and every other row is
# read against it. The trade is that the track's meaning changes with the book,
# which is why the axis is labelled and the percentages are printed beside it.
#
# TARGETS ARE RESTATED BEFORE THEY ARE COMPARED. A holding is a share of the GROSS
# base and a stored target is a share of what the reserve LEFT; comparing them raw
# would make a 100% label under a 50% reserve stretch the track to twice anything
# reachable, and would call an on-target label "under". ``effective_target_pct``
# does the one conversion, exactly as the header does.
# ---------------------------------------------------------------------------

LABEL_STATUS_OVER = "over"
LABEL_STATUS_UNDER = "under"
LABEL_STATUS_OK = "ok"
#: No position AND no target -- nothing has been asked of this label and nothing is
#: held. Deliberately NOT "ok": a tick beside a label the user has not configured
#: reads as approval of a decision nobody made. Also used when there is no base
#: notional, where "over" and "under" cannot be computed at all.
LABEL_STATUS_NONE = "—"

#: How far off target a label may be and still read "ok", in PERCENTAGE POINTS OF
#: THE BASE. A band, not equality: floating point makes equality meaningless, and
#: the page stores targets to two decimals so a hair of drift is a rounding
#: artefact rather than a fact.
#:
#: 0.5pp, chosen as the smallest band that is neither. It is ten times the
#: hundredth-of-a-percent that ``ERROR_LABEL_TOTAL_FMT`` tolerates in a SUM (a
#: different quantity: that one is about boxes adding up, this one is about a book
#: drifting), and well inside what a single share of a mid-priced instrument moves
#: on a five-figure account -- so a label the user could actually rebalance never
#: reads "ok". Widening or narrowing it is a deliberate act: both edges are pinned
#: by a test.
LABEL_STATUS_TOLERANCE_PCT = 0.5

#: Status -> stylesheet classes. The WORD is the signal and the colour only backs it
#: up: over/under encoded as red/green alone is unreadable to exactly the users the
#: Okabe-Ito palette above was chosen for.
LABEL_STATUS_CLASSES = {
    LABEL_STATUS_OVER: 'text-orange-400',
    LABEL_STATUS_UNDER: 'text-sky-400',
    LABEL_STATUS_OK: 'text-secondary-custom',
    LABEL_STATUS_NONE: 'text-secondary-custom',
}

#: Never let the track's denominator be 0. Every label at zero with no target is a
#: real state (a brand-new account), and the bars are then simply empty.
_MIN_BAR_SCALE_PCT = 1.0


@dataclass
class LabelBar:
    """One label's row in the mini-bar list. Pure geometry plus a verdict.

    ``bar_fraction`` and ``notch_fraction`` are 0-1 of the SAME track.
    ``notch_fraction`` is ``None`` only when there is no base to place it against.

    ``current_pct`` keeps its sign: a net-short label is genuinely negative, and the
    figure printed beside the bar says so even though the bar itself clamps to empty
    (a negative width would otherwise render as a full track -- the wrong way round
    from the truth).

    ``target_pct`` is the EFFECTIVE target, i.e. the stored weight restated against
    the gross base, which is what makes it comparable with ``current_pct``.
    ``raw_target_pct`` is what the edit box holds.
    """
    label: str
    color: str
    current_value: float
    current_pct: float
    target_pct: float
    raw_target_pct: float
    bar_fraction: float
    notch_fraction: Optional[float]
    status: str


def bar_scale_pct(views, *, unallocated_pct: float) -> float:
    """What 100% of the mini-bar track represents, in percentage points. Pure.

    The largest of every label's CURRENT share and every label's EFFECTIVE target,
    floored at ``_MIN_BAR_SCALE_PCT`` so there is always something to divide by.
    Negative currents (shorts) are excluded from the maximum -- they cannot set the
    top of a track that starts at zero.

    Computed ONCE for the whole page. Per-row scaling would make the bars
    incomparable, which is the only thing they are for.
    """
    best = _MIN_BAR_SCALE_PCT
    for view in (views or []):
        best = max(best, float(view.pct_of_base or 0.0),
                   effective_target_pct(view.target_pct, unallocated_pct))
    return best


def _bar_fraction(pct: float, scale: float) -> float:
    """One figure as a fraction of the track, clamped to it. Pure.

    The clamp at 0 is what keeps a short label's negative value from rendering as a
    full bar; the clamp at 1 covers a holding that has outgrown a scale computed
    before it (it cannot, today, but a rounding hair should not overflow the track).
    """
    return max(0.0, min(1.0, pct / scale))


def build_label_bars(views, *, base_notional: Optional[float],
                     unallocated_pct: float) -> List[LabelBar]:
    """One ``LabelBar`` per view, in the order given. Pure.

    The order is the CALLER's -- pair this with ``sort_label_views`` -- because the
    scale spans every row and has to be computed over the whole set either way.
    """
    scale = bar_scale_pct(views, unallocated_pct=unallocated_pct)
    bars: List[LabelBar] = []
    for view in (views or []):
        effective = effective_target_pct(view.target_pct, unallocated_pct)
        has_base = bool(base_notional) and view.pct_of_base is not None
        current_pct = float(view.pct_of_base or 0.0) if has_base else 0.0
        if not has_base:
            status = LABEL_STATUS_NONE
            notch = None
        else:
            # ONE call, deliberately outside the status branches. It was written
            # twice -- once for the empty-label case and once for the rest -- and a
            # mutation that put a different scale into the empty branch could not be
            # caught by any test, because an empty label's target is 0 and 0/x is 0
            # whatever x is. Two spellings of one rule, one of them unobservable.
            notch = _bar_fraction(effective, scale)
            if not view.target_pct and not view.current_value:
                status = LABEL_STATUS_NONE
            else:
                drift = current_pct - effective
                if drift > LABEL_STATUS_TOLERANCE_PCT:
                    status = LABEL_STATUS_OVER
                elif drift < -LABEL_STATUS_TOLERANCE_PCT:
                    status = LABEL_STATUS_UNDER
                else:
                    status = LABEL_STATUS_OK
        bars.append(LabelBar(
            label=view.label,
            color=resolve_label_icon_color(view.color),
            current_value=view.current_value,
            current_pct=(float(view.pct_of_base) if view.pct_of_base is not None
                         else float(view.pct_of_total or 0.0)),
            target_pct=effective,
            raw_target_pct=float(view.target_pct or 0.0),
            bar_fraction=_bar_fraction(current_pct, scale),
            notch_fraction=notch,
            status=status,
        ))
    return bars


def sort_label_views(views) -> List["LabelView"]:
    """Display order: biggest holding first, ties broken by name. Pure.

    A NEW list -- the caller's is left alone, because the payload it came from is
    also what the live figure registry keys off.

    Nothing is dropped and nothing is dimmed: a label at $0.00 is exactly as visible
    as the rest. That was considered and declined -- an empty label is usually the
    one that needs attention, and hiding it is how it stays empty.
    """
    return sorted(views or [], key=lambda v: (-float(v.current_value or 0.0), v.label))


#: The totals line under the label list. States the label total in ITS OWN
#: denominator (a share of what the reserve leaves), then the arithmetic that turns
#: it into a share of the base alongside the reserve -- so the line adds to 100 and
#: the user can see WHY, rather than being handed two percentages that look wrong
#: together.
ALLOCATION_FOOTER_FMT = ('Label targets total {total:.2f}% of what the reserve '
                         'leaves = {effective:.2f}% of base, + {reserve:.2f}% '
                         'reserve = {grand:.2f}% of base')
ALLOCATION_FOOTER_EMPTY = 'No managed labels — nothing is allocated yet'


def format_allocation_footer(targets, unallocated_pct: float,
                             tolerance: float = LABEL_TOTAL_TOLERANCE_PCT):
    """The totals footer: ``(text, severity)``. Pure.

    ``severity`` is ``'negative'`` the moment the label targets pass 100,
    ``'warning'`` while they are short of it, and ``'ok'`` otherwise -- which is
    exactly ``format_label_total_notice``'s verdict, so the caption above the list
    and the footer below it cannot disagree. This one always has text: it is a
    running total, not an alarm.

    The reserve is folded in so the line tells a complete story. At every reserve
    the last figure is 100.00% of base when the labels total 100, INCLUDING at a
    100% reserve, where the labels divide nothing and the reserve is the whole book.
    """
    if not targets:
        return ALLOCATION_FOOTER_EMPTY, 'ok'
    total = sum(float(pct or 0.0) for pct in targets.values())
    reserved = clamp_unallocated_pct(unallocated_pct)
    effective = effective_target_pct(total, unallocated_pct)
    text = ALLOCATION_FOOTER_FMT.format(total=total, effective=effective,
                                        reserve=reserved, grand=effective + reserved)
    if total > 100.0 + tolerance:
        return text, 'negative'
    if total < 100.0 - tolerance:
        return text, 'warning'
    return text, 'ok'


#: Severity -> stylesheet classes for the running label-total advisory. Same
#: vocabulary as ``MARKET_BANNER_CLASSES`` ('warning' | 'negative'), different
#: treatment: this is a caption under the stat cards, not a banner.
LABEL_TOTAL_NOTICE_CLASSES = {
    'warning': 'text-xs text-orange-400',
    'negative': 'text-xs text-red-400',
}


def format_label_total_notice(targets,
                              tolerance: float = LABEL_TOTAL_TOLERANCE_PCT):
    """"These do not add up", live on the page, or ``None``. Pure.

    The Allocate wizard has always shown this at step 1, WITHOUT needing a dry run;
    moving the target boxes onto the page has to move the advisory with them, or the
    page becomes the one place a set can be edited and not checked.

    Both wordings are the engine's ``validate_label_targets`` constants verbatim, so
    the page, the wizard and the submit gate describe one defect one way.

    An account managing NOTHING gets no notice: there is no set to be wrong, and the
    empty-state banner above already says what to do. An account whose labels are all
    at 0 -- which is every account that predates inline editing, since targets were
    only ever settable in the wizard -- DOES get one, and that is the whole point.

    Returns:
        Optional[Tuple[str, str]]: ``(text, severity)`` where severity keys
        ``LABEL_TOTAL_NOTICE_CLASSES``, or ``None`` when the set totals 100.
    """
    if not targets:
        return None
    total = sum(float(pct or 0.0) for pct in targets.values())
    if total > 100.0 + tolerance:
        return (ERROR_LABEL_TOTAL_FMT.format(total=total, over=total - 100.0),
                'negative')
    if total < 100.0 - tolerance:
        return (ERROR_LABEL_UNDER_FMT.format(total=total, under=100.0 - total),
                'warning')
    return None


def reserve_dollars(base_notional: Optional[float],
                    unallocated_pct: float) -> Optional[float]:
    """The money the reserve holds back, or ``None`` when there is no base. Pure.

    ``reserved_notional_for`` is the engine's own "what ``investable_notional`` left
    behind", so reserved + investable IS the base exactly, at every reserve.
    """
    if not base_notional:
        return None
    return reserved_notional_for(base_notional, unallocated_pct)


def format_reserve_caption(base_notional: Optional[float],
                           unallocated_pct: float) -> str:
    """The live dollar caption beside the reserve slider. Pure.

    The user asked for "a field for the value we want to keep"; the stored field is
    a PERCENT, so the percent stays the single source of truth and the money is
    derived and shown next to it. There is deliberately no dollar INPUT: taking one
    would need ``pct = dollars / base``, which is undefined on the accounts that
    have no base and would silently disagree with the slider on the ones that do.
    """
    if not base_notional:
        return RESERVE_CAPTION_NO_BASE
    return RESERVE_CAPTION_FMT.format(
        reserved=reserved_notional_for(base_notional, unallocated_pct),
        investable=investable_notional(base_notional, unallocated_pct))


def format_reserve_row(*, base_notional: Optional[float],
                       available_buying_power: Optional[float],
                       unallocated_pct: float) -> Optional[str]:
    """The cash-reserve line above the labels, or ``None`` when it cannot be drawn.

    ``None`` on a missing OR ZERO base and on an unknown buying power, which is the
    page's long-standing guard moved into the pure layer: a base of exactly 0.0 is a
    real state (a brand-new or fully-withdrawn account) and is not a denominator, so
    ``UnallocatedRow.pct_of_base`` comes back ``None`` for it and the ``:.1f`` below
    used to raise on the None and 500 the whole page.
    """
    if not base_notional or available_buying_power is None:
        return None
    row = unallocated_row(base_notional=base_notional,
                          available_buying_power=available_buying_power,
                          unallocated_pct=unallocated_pct)
    return RESERVE_ROW_FMT.format(current=row.current_value,
                                  pct_of_base=row.pct_of_base,
                                  target_pct=row.target_pct,
                                  target_value=row.target_value)


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
