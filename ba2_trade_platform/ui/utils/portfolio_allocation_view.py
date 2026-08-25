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
    MONEY_EPSILON,
    VALUATION_MODE_COST, VALUATION_MODE_MARKET, PositionFetchFailed, PositionState,
    UnrealisedPnL,
    clamp_unallocated_pct, current_value, effective_target_pct,
    format_unrealised_pnl, investable_notional,
    position_sign, reserved_notional_for, scale_pct_to_total, signed_position_values,
    split_pct_across, unrealised_pnl, validate_unallocated_pct,
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

#: THE parse. A colour is ``#`` and exactly six hex digits -- nothing else, at
#: either end of the round trip.
#:
#: The palette used to be the whole whitelist, and it was the whitelist for a
#: reason: the value is interpolated into a CSS ``style`` attribute, so an unbounded
#: one is a place to put something that is not a colour. The user has now asked for
#: a picker ("Make a color picker then"), so the SET is opened and the PARSE is not.
#: Six digits and no more:
#:
#: * ``#abc`` (3-digit shorthand) is refused -- it is a colour, but accepting two
#:   spellings of one value means the store holds both and nothing can compare them;
#: * ``#rrggbbaa`` is refused -- a translucent swatch over a dark surface is exactly
#:   the unreadable case the palette exists to avoid, and the alpha would silently
#:   defeat the contrast check;
#: * ``rgb()``, named colours and anything carrying ``;``, ``!important`` or a
#:   ``url(...)`` are refused as what they are: not this value.
#:
#: ``fullmatch`` semantics via the anchors, so no prefix of a longer string can slip
#: through -- ``#a1b2c3;background:url(x)`` is the exact attack this closes.
_HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')

#: The page's own surface, at its LIGHTEST. ``styles.css`` paints the body
#: ``linear-gradient(135deg, #1a1f2e 0%, #0f1419 100%)``, and every palette hue is
#: lighter than both ends, so the lighter end is the worst case for them and is what
#: the floor has to be measured against. Naming the darker end would quietly pass a
#: colour that is unreadable over half the page.
SURFACE_COLOR = '#1A1F2E'

#: WCAG 2.1 SC 1.4.11 "Non-text Contrast": 3:1 for a graphical object such as an
#: icon or a bar. The same threshold the palette was chosen against, which is what
#: makes the warning and the palette one rule rather than two opinions.
MIN_GRAPHICAL_CONTRAST = 3.0

#: Said about a custom colour that falls below it. A WARNING, never a refusal: the
#: user asked for a picker after reading the palette argument, and it is their UI.
#: The measured ratio is named because "poor contrast" alone leaves them unable to
#: tell a near miss from an invisible swatch.
LABEL_COLOR_CONTRAST_WARNING_FMT = (
    '{color} contrasts {ratio:.1f}:1 against the page background — below the '
    '{floor:.0f}:1 WCAG asks for a graphical object, so this swatch will be hard '
    'to see. Saved anyway; the seven presets above all clear it.')


def _rgb(hex_color: str):
    """``#RRGGBB`` -> ``(r, g, b)`` 0-255, or ``None`` when it is not one. Pure."""
    text = str(hex_color or '').strip()
    if not _HEX_COLOR_RE.match(text):
        return None
    return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))


def relative_luminance(hex_color: str) -> Optional[float]:
    """WCAG relative luminance of a ``#RRGGBB``, or ``None``. Pure.

    The published formula, not an approximation: each channel is linearised
    (``c/12.92`` below the 0.03928 knee, ``((c+0.055)/1.055) ** 2.4`` above it) and
    weighted 0.2126 / 0.7152 / 0.0722. A simple average of the channels agrees to
    within a few percent on greys and is wrong by a factor of three on saturated
    blue, which is precisely the palette entry sitting closest to the floor.
    """
    rgb = _rgb(hex_color)
    if rgb is None:
        return None
    channels = []
    for value in rgb:
        c = value / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(first: str, second: str) -> Optional[float]:
    """WCAG contrast ratio between two ``#RRGGBB`` colours, 1-21. Pure.

    Symmetric by construction (the lighter one is always on top), so a caller cannot
    get 0.05 by passing the arguments the other way round. ``None`` when either side
    is not a colour this module accepts.
    """
    a, b = relative_luminance(first), relative_luminance(second)
    if a is None or b is None:
        return None
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def label_color_contrast_warning(raw) -> Optional[str]:
    """"this swatch will be hard to see", or ``None``. Pure. Never blocks.

    ``None`` for "no colour" and for anything unparseable as well as for a colour
    that clears the floor: there is nothing to measure in the first two cases, and
    the parse refusal is a different message that the caller shows instead. A
    contrast complaint about a string that is not a colour would send the user off
    adjusting a hue that was never the problem.
    """
    ratio = contrast_ratio(str(raw or '').strip(), SURFACE_COLOR)
    if ratio is None or ratio >= MIN_GRAPHICAL_CONTRAST:
        return None
    return LABEL_COLOR_CONTRAST_WARNING_FMT.format(
        color=str(raw).strip().upper(), ratio=ratio, floor=MIN_GRAPHICAL_CONTRAST)


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
    """The WRITE path: a hex colour in canonical case, or ``None`` for no colour.

    Case-insensitive and whitespace-tolerant, because the value makes a round trip
    through a widget and a database column. A PALETTE entry comes back in its
    published spelling; any other accepted value comes back upper-cased, so one
    colour has exactly one stored form.

    ``#000000`` is a colour like any other and is accepted. It is NOT "no colour" --
    that is ``''`` / ``None``, which the store maps to SQL NULL, and conflating the
    two would make a black swatch impossible to distinguish from an unset one.

    Raises:
        ValueError: for anything that is not ``#`` plus exactly six hex digits. The
        SET is open now (the user asked for a picker); the PARSE is not. The value
        is interpolated into a CSS ``style`` attribute, so accepting
        ``#a1b2c3;background:url(x)`` -- or ``rgb()``, or a named colour, or a
        3-digit shorthand that would give one colour two stored spellings -- is
        either a bug in the caller or an injection. See ``_HEX_COLOR_RE``.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    canonical = _PALETTE_BY_HEX.get(text.upper())
    if canonical is not None:
        return canonical
    if not _HEX_COLOR_RE.match(text):
        raise ValueError(
            f"{raw!r} is not a colour — a label colour is '#' followed by exactly "
            f"six hex digits (e.g. '#E69F00'), or one of the "
            f"{len(LABEL_COLOR_PALETTE)} presets. Shorthand ('#abc'), alpha "
            f"('#rrggbbaa'), 'rgb(...)' and named colours are refused: this value "
            f"is interpolated into a CSS style attribute. See _HEX_COLOR_RE")
    return '#' + text[1:].upper()


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

    THE ONE ANSWER to "what colour is this label", and it has three callers by
    design -- the icon on the label row, the swatch in the Manage-labels dialog and
    the mini-bar's fill. They cannot disagree, because there is nothing for them to
    disagree with: none of them looks at ``LabelView.color`` directly.

    TOLERANT where ``normalise_label_color`` refuses, and the asymmetry is
    deliberate: a row hand-edited in sqlite must not take the page down, and it must
    not reach the ``style`` attribute either. Anything that is not ``#`` plus six hex
    digits falls back to the neutral grey, so the PARSE -- not the caller, and not
    the database -- decides what gets rendered. The return value is always a literal
    ``#`` followed by six upper-case hex digits, which is what makes interpolating it
    into a CSS ``style`` safe.
    """
    if stored is None:
        return DEFAULT_LABEL_ICON_COLOR
    text = str(stored).strip()
    canonical = _PALETTE_BY_HEX.get(text.upper())
    if canonical is not None:
        return canonical
    if not _HEX_COLOR_RE.match(text):
        return DEFAULT_LABEL_ICON_COLOR
    return '#' + text[1:].upper()


@dataclass
class ManagedLabel:
    """One managed label as the page reads it out of ``portfolio_allocation_label``.

    ``color`` is the stored palette hex or ``None``. NULL is "no colour chosen",
    which is a different fact from a stored default, so it is carried through as
    ``None`` all the way to the render and only turned into a drawable colour there
    (``resolve_label_icon_color``).

    ``previous_target_pct`` is the target the LAST run was launched with, straight
    off ``portfolio_allocation_label.previous_target_pct``. A PURE CARRIER: it is
    what the page's "Load last" reads and what the row prints as ``last N%``, and
    no derived figure on the page may divide by it. ``None`` means the label has
    never been through ``save_allocation_targets``, which is a different fact from
    0.0 ("last time this got nothing") and is why it is never coerced.
    """
    label: str
    target_pct: float = 0.0
    comment: Optional[str] = None
    color: Optional[str] = None
    previous_target_pct: Optional[float] = None


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

    ``previous_weight_pct`` is the share this symbol carried in the label the LAST
    time a run was launched, and ``pnl`` is its unrealised profit. Both moved here
    out of the Allocate wizard's step 2, which was the only screen that could
    answer either question -- and which is no longer where the weights are typed.
    ``previous_weight_pct`` is ``None`` (never 0.0) for a symbol with no history:
    "never allocated" and "allocated nothing" are different facts.

    ``pnl`` is measured on the LIVE quote in BOTH valuation modes, deliberately.
    In cost mode ``current_value`` IS the cost basis, so a P&L derived from it
    reads 0.00 on every row -- see ``unrealised_pnl``, which takes no valuation
    mode for exactly that reason.
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
    previous_weight_pct: Optional[float] = None
    pnl: UnrealisedPnL = field(default_factory=UnrealisedPnL)

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

    ``previous_target_pct`` and ``pnl`` carry the wizard's step-1 caption onto the
    page. The P&L is ONE call over the whole membership rather than a combination
    of the rows' own figures, which is what makes the percentage money-weighted:
    the engine sums market value and gross cost first and divides once.
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
    previous_target_pct: Optional[float] = None
    pnl: UnrealisedPnL = field(default_factory=UnrealisedPnL)
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
                      symbol_previous_weights=None,
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
        symbol_previous_weights: ``{label: {symbol: previous_weight_pct}}`` from
            ``get_previous_symbol_weights``. Optional, and an absent entry stays
            ``None`` rather than falling back to the CURRENT weight -- the whole
            point of the figure is that it may differ from what is on screen, and
            "there is no last" is what the page's Load-last button reads.
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
    previous_by_label = symbol_previous_weights or {}

    def _pnl_of(sym: str) -> UnrealisedPnL:
        """One symbol's unrealised P&L, on the LIVE quote in either mode.

        A shallow priced copy for the same reason ``_value_of`` builds one: the
        page fetches quotes in a single bulk call and does not stamp them onto the
        states this function is handed. ``unrealised_pnl`` is the engine's, so a
        row here and a caption anywhere else are the same rule at the same scope.
        """
        state = positions.get(sym)
        if state is None:
            return unrealised_pnl([])
        return unrealised_pnl([PositionState(
            symbol=state.symbol, quantity=state.quantity,
            cost_basis=state.cost_basis, price=(prices or {}).get(sym))])

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
        label_previous = previous_by_label.get(entry.label) or {}
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
                previous_weight_pct=label_previous.get(sym),
                pnl=_pnl_of(sym),
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
            previous_target_pct=entry.previous_target_pct,
            # ONE call over the whole membership, deliberately NOT a combination of
            # the rows' own figures: the engine sums market value and gross cost
            # first and divides once, which is what makes the percentage
            # money-weighted rather than a mean of the symbols' percentages.
            pnl=unrealised_pnl([
                PositionState(symbol=s, quantity=positions[s].quantity,
                              cost_basis=positions[s].cost_basis,
                              price=(prices or {}).get(s))
                for s in symbols if s in positions]),
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

    A trailing ``%`` is tolerated: the boxes carry ``suffix='%'`` and Quasar can
    hand back the raw string it is holding.

    THE COMMA IS A DECIMAL POINT HERE, and that is a deliberate reading rather
    than a tolerance. Every box this parses is bounded 0-100, so no legitimate
    value in one needs a thousands separator -- while a decimal comma is what a
    large part of the world types, and what the live page was observed rendering
    back ("11,11"). Stripping it as a grouping mark multiplies the number by a
    hundred, and the range check only catches HALF of those: "11,11" becomes 1111
    and is refused, which is visible; "0,5" becomes 5.0, which is in range, is
    ACCEPTED, and gives the symbol ten times the share that was typed. The silent
    one is the one that costs money.

    So a LONE comma with no decimal point becomes the decimal point. A comma
    alongside a dot keeps its grouping meaning -- "1,234.5" is unambiguous
    wherever it is written that way -- and so does a repeated comma; both are then
    refused by the range check on their own merits rather than by accident.
    """
    if isinstance(raw, bool):
        return TargetEdit(False, None, EDIT_NOT_A_NUMBER, "")
    if raw is None:
        return TargetEdit(False, None, EDIT_BLANK, "")
    if isinstance(raw, str):
        text = raw.strip().rstrip('%').strip()
        if text.count(',') == 1 and '.' not in text:
            text = text.replace(',', '.')
        else:
            text = text.replace(',', '')
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

# ---- THE DENOMINATOR RULE --------------------------------------------------
#
# THE INVESTABLE POOL IS 100%. The user settled it: "The reserve is really for the
# tool to make the math but we should assume the available money is 100% for the
# allocation, for clarity." So every PRIMARY percentage on a label row -- the
# holding, the target, the delta between them, the bar's fill and its notch --
# divides ``investable_notional``, and the label targets therefore add to 100.
#
# The share of the GROSS base is secondary. It is printed in parentheses and marked
# "real", never as the headline, because it is a derived restatement of a number the
# user did not type. ``effective_target_pct`` remains the one conversion.
#
# THIS REPLACED A MIXTURE, and the mixture is the defect. ``current`` was a share of
# the gross base while ``target`` was relative: two different denominators printed
# with the same '%' sign, side by side, so the DIFFERENCE between them -- the number
# that says what to do -- meant nothing at any non-zero reserve.
#
# ONE EXCEPTION, and it is genuine: the unallocated reserve row. It IS the part held
# back, so restating it against what it leaves is circular. It stays of-base and
# says so (``RESERVE_BASIS_NOTE``).
#
# The conversion goes through MONEY (``current_value / investable_notional``), never
# through the inverse ``/(1 - r/100)`` of a share of base. Same singularity, honest
# handling: at a 100% reserve there is no pool, so the share is ``None`` and the row
# prints a dash rather than inf, nan or a confident 0.0%.

#: Said ONCE for the whole page, under the label list, instead of on eight rows. The
#: rows are terse ("tgt 15.0% (real 13.5%)") precisely because this line exists; the
#: old header repeated a hundred-character clause per row and the part that varied
#: was buried in the middle of it.
BASIS_LEGEND = (
    'Label percentages divide the INVESTABLE pool — what the cash reserve leaves — '
    'so the targets add to 100. “(real N%)” restates a target against the gross '
    'account base. The unallocated row above is the one figure measured against '
    'that gross base.')

#: Under the label target input. The first sentence used to read "This label's share
#: of the whole portfolio", which is FALSE whenever a reserve is set -- 15 typed
#: under a 10% reserve is 13.5% of the portfolio, and the row beside it said so. The
#: second sentence was right and is kept verbatim: it explains the OTHER denominator,
#: which is the collision this whole redesign is about.
LABEL_TARGET_CAPTION = (
    'This label’s share of the investable pool — what is left after the cash '
    'reserve — so the labels add up to 100. The “Share of label %” column below '
    'divides THIS label’s money between its symbols — a different denominator, '
    'which is why the two can read 0 and 20 at the same time and both be right.')

#: Beside the reserve row, naming the one denominator change on the page.
RESERVE_BASIS_NOTE = (
    'This row is the only one measured against the gross account base — it IS the '
    'part held back, so it cannot be restated against what it leaves. Every label '
    'below divides that remainder, and raising this on a fully invested account '
    'will generate sell orders.')

#: The target pair, and the ONE place it is spelled. The row cell and the header
#: line both render it, so they cannot disagree about which figure leads.
TARGET_PAIR_FMT = '{target_pct:.1f}%'
TARGET_PAIR_WITH_REAL_FMT = '{target_pct:.1f}% (real {effective_pct:.1f}%)'
#: The row cell's prefix. Split out so the header can reuse the pair without
#: swallowing a "tgt" in the middle of a sentence.
TARGET_CELL_PREFIX = 'tgt '

#: The label group header -- the expansion's own caption, which is what a screen
#: reader announces and what a COLLAPSED section shows. It therefore has to be
#: self-describing where the row can be terse: it names its denominator out loud and
#: it carries the delta, because a collapsed row that omits the actionable number is
#: the one place it is missing.
LABEL_HEADER_WITH_BASE_FMT = (
    '{label} — ${current:,.2f} ({pct_of_investable:.1f}% of investable, target '
    '{target} — {delta})')
#: No investable pool means no denominator for either figure, so the line says so
#: instead of printing a share of a number it does not have.
LABEL_HEADER_NO_BASE_FMT = (
    '{label} — ${current:,.2f} ({pct_of_total:.1f}% of managed, target '
    'unavailable — no investable base)')

#: The ⓘ tooltip. It carries what the ROW cannot -- the money, and which denominator
#: the table underneath uses. The "i.e. N% of the base" clause it used to end with
#: is GONE: the row now prints "(real N%)", and one fact in two places is one fact
#: that can disagree with itself.
LABEL_TARGET_TOOLTIP_FMT = (
    'Portfolio target: {target_pct:.1f}% of what the reserve leaves = '
    '${target_value:,.2f}. The table below splits that money by each row’s '
    'share of the label.')
LABEL_TARGET_TOOLTIP_NO_BASE_FMT = (
    'Portfolio target: {target_pct:.1f}% of what the reserve leaves. The broker '
    'published no base notional, so there is no dollar figure yet. The table below '
    'splits that money by each row’s share of the label.')

#: Applied to the ⓘ tooltip. THE convention, not a second one: it is
#: ``ExpertDataExportInterface.DETAIL_TOOLTIP_STYLE`` -- ``white-space: pre-line``
#: with a ``max-width`` -- plus a legible size, which is the complaint here ("The
#: info text is too small"). A tooltip is HTML, so without the max-width a long
#: sentence renders as one continuous line wider than the viewport, clipped at both
#: ends and impossible to scroll, select or copy.
LABEL_TOOLTIP_STYLE = 'white-space: pre-line; max-width: 28rem; font-size: 0.85rem;'

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


def format_target_pair(target_pct: float, unallocated_pct: float) -> str:
    """``30.0%`` or ``30.0% (real 27.0%)``. THE one place the pair is spelled. Pure.

    What the user TYPED leads -- "put something like 15% (real 13.5%) so we know" --
    and the parenthetical is the DERIVED share of the gross base, marked as derived.
    Printing them the other way round is the same two numbers reading as though the
    reserve had inflated the target.

    At a 0% reserve the two coincide and only ONE is printed. Making the common case
    noisier in order to explain the uncommon one is the trade this refuses: a page
    with no reserve would otherwise carry "(real 30.0%)" on every row, saying
    nothing.
    """
    target = float(target_pct or 0.0)
    effective = effective_target_pct(target, unallocated_pct)
    if abs(effective - target) < 0.05:      # under the 1dp both are printed at
        return TARGET_PAIR_FMT.format(target_pct=target)
    return TARGET_PAIR_WITH_REAL_FMT.format(target_pct=target, effective_pct=effective)


def format_label_header(*, label: str, current_value: float, target_pct: float,
                        pct_of_investable: Optional[float], pct_of_total: float,
                        delta_text: str, unallocated_pct: float) -> str:
    """The label group header line -- the expansion's caption. Pure.

    ``pct_of_investable`` is the CURRENT holding as a share of the investable pool
    (``LabelBar.current_pct``); ``None`` selects the no-basis branch, which is both
    "the broker published no base" and "the reserve is 100%, so there is no pool".

    ``delta_text`` is ``LabelBar.delta_text``, passed in rather than recomputed: the
    header and the row must not be able to reach two different verdicts about the
    same label, and the only way to guarantee that is for there to be one
    computation with one caller-visible answer.
    """
    if pct_of_investable is None:
        return LABEL_HEADER_NO_BASE_FMT.format(label=label, current=current_value,
                                               pct_of_total=pct_of_total)
    return LABEL_HEADER_WITH_BASE_FMT.format(
        label=label, current=current_value, pct_of_investable=pct_of_investable,
        target=format_target_pair(target_pct, unallocated_pct), delta=delta_text)


def format_label_target_tooltip(*, target_pct: float,
                                base_notional: Optional[float],
                                unallocated_pct: float) -> str:
    """The ⓘ beside a label header. Pure.

    Carries what the ROW cannot: the money the target comes to, and the sentence
    that resolves the naming collision this redesign is about -- the header's target
    is a share of the investable POOL, the table's ``Share of label %`` is a share of
    THE LABEL.

    It no longer restates the target against the gross base. The row prints
    "(real N%)" now, and the same fact in two places is a fact that can disagree with
    itself.
    """
    if not base_notional:
        return LABEL_TARGET_TOOLTIP_NO_BASE_FMT.format(target_pct=target_pct)
    return LABEL_TARGET_TOOLTIP_FMT.format(
        target_pct=target_pct,
        target_value=(investable_notional(base_notional, unallocated_pct)
                      * float(target_pct or 0.0) / 100.0))


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
# "FILL 100%" -- the deliberate replacement for automatic recalculation
#
# Editing one symbol's share used to re-read and redraw the whole label, because
# ``get_symbol_weights`` resolves a symbol with no stored row to a share of whatever
# is left of 100: storing one weight changed what every sibling resolved to. The
# user asked for that to stop -- "do not automatically recalculate when I adjust
# share of label within label. Do not change other numbers" -- which leaves the set
# free to stop totalling 100, so there has to be a deliberate way to put it back.
#
# "EMPTY" MEANS A SHARE OF ZERO. Not "has no stored row", and the distinction is the
# hinge the whole feature turns on:
#
#   * it is the ENGINE's definition already (``_symbol_weight``: "'Unset' and 0 are
#     one fact here"), which is what ``fill_remaining_symbol_weights`` fills against,
#     so the page and the wizard cannot mean different things by an empty box;
#   * it is the only definition that can AGREE WITH THE NO-RECALC RULE. Once the
#     recalculation is gone, the number on screen is the only weight there is: the
#     page holds one resolved map per label, an accepted edit mutates exactly the one
#     key it edited, and this function reads that same map. "Has no stored row" would
#     be a fact about the database that nothing on screen reflects -- a symbol with no
#     row DISPLAYS a number, so calling it empty would make the button fill a slot the
#     user can see is full.
#
# A consequence worth stating: on a FRESH render the resolved map always totals 100
# (that is how the defaults are computed), so the button correctly reports "already
# 100%" until an edit puts the set out. That is not the button failing -- it is the
# button and the no-recalc rule agreeing.
#
# The arithmetic is NOT written here. Case 1 is ``split_pct_across`` -- the engine's
# one splitter, which is also what "Fill rest evenly" and "Even split" use -- and
# cases 2 and 3 are ``scale_pct_to_total``, its proportional sibling with the same
# floor-to-the-cent-plus-one-residual rounding rule. A fourth two-decimal rounding
# rule written in the UI layer is exactly what drifts.
# ---------------------------------------------------------------------------

FILL_NO_SYMBOLS = "NO_SYMBOLS"
FILL_ALREADY_100 = "ALREADY_100"
FILL_FILLED_EMPTY = "FILLED_EMPTY"
FILL_SCALED_DOWN = "SCALED_DOWN"
FILL_SCALED_UP = "SCALED_UP"

FILL_MSG_NO_SYMBOLS_FMT = ("'{label}' has no symbols — add one before filling it "
                           "to 100%.")
FILL_MSG_ALREADY_100_FMT = ("'{label}' already totals 100.00% — nothing to change.")
FILL_MSG_FILLED_EMPTY_FMT = ("'{label}': shared the remaining {remainder:.2f}% "
                             "between {count} empty symbol(s). The weights you typed "
                             "were left alone.")
FILL_MSG_SCALED_DOWN_FMT = ("'{label}': scaled every symbol DOWN proportionally, "
                            "{total:.2f}% → 100.00%.")
FILL_MSG_SCALED_UP_FMT = ("'{label}': scaled every symbol UP proportionally, "
                          "{total:.2f}% → 100.00%.")


@dataclass
class FillToHundred:
    """One press of a label's "Fill 100%" button, decided. Pure.

    ``changed is False`` means NOTHING is written and ``weights`` is what was passed
    in. Both no-change cases still carry a ``message``: a button that silently does
    nothing when pressed is indistinguishable from a broken one, which is the same
    rule ``can_fill_remaining_symbol_weights`` states for the wizard's disabled
    buttons.

    ``weights`` is the FULL new map for the label, in the order given, and it sums to
    exactly 100 in decimal whenever ``changed`` is True.
    """
    changed: bool
    reason_code: str
    weights: Dict[str, float]
    message: str


def fill_label_to_100(label: str, weights,
                      tolerance: float = LABEL_TOTAL_TOLERANCE_PCT) -> FillToHundred:
    """Make one label's symbol shares total exactly 100. Pure. Three cases.

    Args:
        label: the label's name, for the message only.
        weights: ``{symbol: share}`` in DISPLAY order — the page's live resolved map,
            which is the same object the no-recalc edit handler mutates. ``None``
            reads as 0.0, exactly as the engine's ``_symbol_weight`` does.
        tolerance: the engine's ``LABEL_TOTAL_TOLERANCE_PCT``. "Already 100" is a
            BAND, not an equality: 33.33 + 33.33 + 33.34 is exactly 100 in decimal
            and 99.99999999999999 in binary, and a hard equality here would offer to
            "fix" the engine's own even split on every press.

    Returns:
        FillToHundred: the case taken, the new map, and a sentence naming it.

        The cases are ORDERED and the order is load-bearing:

        1. **over 100** → every symbol scaled DOWN proportionally. This is checked
           BEFORE "has an empty slot", because the alternative on an over-allocated
           label with an empty box is to fill it out of a negative remainder, and
           there is no such thing. A zero stays zero: scaling is proportional.
        2. **under 100 with at least one empty slot** → the shortfall is split
           between the EMPTY ones only and every typed weight is left exactly as
           typed. That is the workflow the button exists for.
        3. **under 100 with nothing empty** → every symbol scaled UP proportionally,
           because there is no gap to put the shortfall in.

        Every value is rounded to the cent on the way in, which is the page's own
        stored precision (the boxes step by 0.01). Without it a resolved weight
        carrying four decimals would make the emitted set miss 100 by a hundredth.
    """
    symbols = list(weights or {})
    if not symbols:
        return FillToHundred(False, FILL_NO_SYMBOLS, {},
                             FILL_MSG_NO_SYMBOLS_FMT.format(label=label))

    values = [round(float(weights[s] or 0.0), 2) for s in symbols]
    total = round(sum(values), 2)

    # ``round(..., 2)`` on the DIFFERENCE, not just on the total: ``abs(100.01 -
    # 100.0)`` is 0.010000000000005116 in binary, which is greater than a 0.01
    # tolerance -- so the band would refuse the very sets it exists to accept.
    if round(abs(total - 100.0), 2) <= tolerance:
        return FillToHundred(False, FILL_ALREADY_100, dict(zip(symbols, values)),
                             FILL_MSG_ALREADY_100_FMT.format(label=label))

    empty = [i for i, v in enumerate(values) if v == 0.0]

    if total > 100.0:
        return FillToHundred(
            True, FILL_SCALED_DOWN,
            dict(zip(symbols, scale_pct_to_total(values, 100.0))),
            FILL_MSG_SCALED_DOWN_FMT.format(label=label, total=total))

    if empty:
        remainder = round(100.0 - total, 2)
        out = list(values)
        for index, share in zip(empty, split_pct_across(remainder, len(empty))):
            out[index] = share
        return FillToHundred(
            True, FILL_FILLED_EMPTY, dict(zip(symbols, out)),
            FILL_MSG_FILLED_EMPTY_FMT.format(label=label, remainder=remainder,
                                             count=len(empty)))

    return FillToHundred(
        True, FILL_SCALED_UP, dict(zip(symbols, scale_pct_to_total(values, 100.0))),
        FILL_MSG_SCALED_UP_FMT.format(label=label, total=total))


# ---------------------------------------------------------------------------
# THE MIGRATED BUTTON GROUPS
#
# The Allocate wizard's step 1 carried "Even split" and "Load last" over the LABEL
# targets; its step 2 carried "Even split", "Fill rest", "Load last" and "Wipe"
# over one label's symbol weights. All six are on the page now, because the boxes
# they operate on are, and a button two screens away from the number it rewrites
# is a button nobody presses.
#
# THE ARITHMETIC IS NOT HERE. Every one of these delegates to the engine's own
# function -- ``even_split_targets``, ``load_previous_targets``,
# ``even_split_symbol_weights``, ``fill_remaining_symbol_weights``,
# ``load_previous_symbol_weights``, ``wipe_symbol_weights`` -- exactly as the
# wizard did. What lives here is the TRANSLATION between the page's plain
# ``{name: pct}`` maps and the engine's ``LabelTarget``/``SymbolTarget``, plus the
# one decision the page needs and the engine does not make: did anything change,
# and if not, what do we say instead? A copy of the splitting rule here would be a
# fourth two-decimal rounding rule, and rounding rules drift.
#
# NOTHING IS DISABLED. The wizard greyed these buttons out; the page follows its
# own ``Fill 100%`` convention and always allows the press, reporting the no-op in
# words. Same principle from the other end -- a control that does nothing when
# pressed is indistinguishable from a broken one -- and it costs the page a
# per-keystroke ``set_enabled`` sweep it would otherwise need over every button on
# every row.
# ---------------------------------------------------------------------------

TARGETS_NO_LABELS = "NO_LABELS"
TARGETS_UNCHANGED = "UNCHANGED"
TARGETS_EVEN_SPLIT = "EVEN_SPLIT"
TARGETS_NO_PREVIOUS = "NO_PREVIOUS"
TARGETS_LOADED_LAST = "LOADED_LAST"

TARGETS_MSG_NO_LABELS = ('No labels are managed yet — there is nothing to split. '
                         'Use "Manage labels" first.')
TARGETS_MSG_EVEN_SPLIT_FMT = ('Split 100% evenly between {count} label(s). The cash '
                              'reserve is untouched — the labels divide what it '
                              'leaves.')
TARGETS_MSG_ALREADY_EVEN_FMT = ('The {count} labels already hold an even split — '
                                'nothing was written.')
TARGETS_MSG_NO_PREVIOUS = ('No label has a previous target — nothing has ever been '
                           'allocated on this account, so there is nothing to load.')
TARGETS_MSG_LOADED_LAST_FMT = ('Restored the last run’s targets. {kept} label(s) have '
                               'no history and kept the target they had.')
TARGETS_MSG_LAST_UNCHANGED = ('The labels already hold the targets of the last run — '
                              'nothing was written.')


@dataclass
class TargetsUpdate:
    """One press of a LABEL-LEVEL button, decided. Pure.

    ``changed is False`` means NOTHING is written and ``targets`` is what was
    passed in. Both no-change cases still carry a ``message``, on
    ``FillToHundred``'s terms.

    ``targets`` is the FULL new ``{label: pct}`` map IN THE ORDER GIVEN. The order
    is load-bearing: the page hands its display order in and writes the result
    straight back onto the rows, and the engine's splitter puts the rounding
    remainder on the LAST entry -- so a map that came back re-sorted would move
    that remainder onto a label other than the one the user is looking at.
    """
    changed: bool
    reason_code: str
    targets: Dict[str, float]
    message: str


def _label_targets_as_engine(targets, previous=None):
    """``{label: pct}`` -> the engine's ``[LabelTarget]``, order preserved. Internal.

    ``symbols`` is deliberately left empty: every function these feed touches
    ``target_pct`` only, and building the symbol list here would invite a caller to
    read weights back out of a structure that never had them.
    """
    history = previous or {}
    from ...core.portfolio_allocation import LabelTarget
    return [LabelTarget(label=name, target_pct=float(pct or 0.0),
                        previous_target_pct=history.get(name))
            for name, pct in (targets or {}).items()]


def _same_pcts(before, after, places: int = 2) -> bool:
    """True when two ``{name: pct}`` maps agree to the stored precision. Internal.

    Rounded rather than compared exactly: the engine's splitters emit values on the
    cent grid the boxes store at, and a binary hair between 33.33 and 33.330000001
    is not a change the user made.
    """
    return all(round(float(after.get(k) or 0.0), places)
               == round(float(v or 0.0), places) for k, v in (before or {}).items())


def even_split_label_targets(targets) -> TargetsUpdate:
    """The label-level "Even split": every label gets an equal share of 100%. Pure.

    ALWAYS 100, independent of the reserve -- the reserve is its own stored field
    and the labels divide what it LEAVES, so any other total would produce a set
    ``validate_label_targets`` refuses. That reasoning is the engine's
    ``even_split_targets``, and so is the arithmetic: the remainder lands on the
    last label so the set totals exactly 100 in decimal.
    """
    from ...core.portfolio_allocation import even_split_targets

    current = {name: float(pct or 0.0) for name, pct in (targets or {}).items()}
    if not current:
        return TargetsUpdate(False, TARGETS_NO_LABELS, {}, TARGETS_MSG_NO_LABELS)
    fresh = {lt.label: lt.target_pct
             for lt in even_split_targets(_label_targets_as_engine(current))}
    if _same_pcts(current, fresh):
        return TargetsUpdate(False, TARGETS_UNCHANGED, current,
                             TARGETS_MSG_ALREADY_EVEN_FMT.format(count=len(current)))
    return TargetsUpdate(True, TARGETS_EVEN_SPLIT, fresh,
                         TARGETS_MSG_EVEN_SPLIT_FMT.format(count=len(current)))


def load_last_label_targets(targets, previous) -> TargetsUpdate:
    """The label-level "Load last": restore the targets the last RUN used. Pure.

    ``previous`` is ``{label: previous_target_pct}`` -- the SEPARATE generation
    written only by ``save_allocation_targets``, never the live map. Reading the
    live map instead turns this button into a no-op that still reports success,
    which is the one failure it cannot be allowed to have.

    A previous of 0.0 IS restored: the engine reads 0 as "hold none of this", so it
    is a real prior state, and refusing it would refuse to undo the user's last
    change. ``None`` is the only "no history", and such a label keeps the target it
    already has -- a partial history is the ordinary state and zeroing those would
    silently unallocate a real basket.
    """
    from ...core.portfolio_allocation import (
        has_previous_targets, load_previous_targets)

    current = {name: float(pct or 0.0) for name, pct in (targets or {}).items()}
    if not current:
        return TargetsUpdate(False, TARGETS_NO_LABELS, {}, TARGETS_MSG_NO_LABELS)
    items = _label_targets_as_engine(current, previous)
    if not has_previous_targets(items):
        return TargetsUpdate(False, TARGETS_NO_PREVIOUS, current,
                             TARGETS_MSG_NO_PREVIOUS)
    fresh = {lt.label: lt.target_pct for lt in load_previous_targets(items)}
    if _same_pcts(current, fresh):
        return TargetsUpdate(False, TARGETS_UNCHANGED, current,
                             TARGETS_MSG_LAST_UNCHANGED)
    kept = sum(1 for lt in items if lt.previous_target_pct is None)
    return TargetsUpdate(True, TARGETS_LOADED_LAST, fresh,
                         TARGETS_MSG_LOADED_LAST_FMT.format(kept=kept))


WEIGHTS_NO_SYMBOLS = "NO_SYMBOLS"
WEIGHTS_UNCHANGED = "UNCHANGED"
WEIGHTS_EVEN_SPLIT = "EVEN_SPLIT"
WEIGHTS_FILLED_REST = "FILLED_REST"
WEIGHTS_NOTHING_TO_FILL = "NOTHING_TO_FILL"
WEIGHTS_NO_PREVIOUS = "NO_PREVIOUS"
WEIGHTS_LOADED_LAST = "LOADED_LAST"
WEIGHTS_WIPED = "WIPED"
WEIGHTS_ALREADY_CLEAR = "ALREADY_CLEAR"

WEIGHTS_MSG_NO_SYMBOLS_FMT = ("'{label}' has no symbols with a share — add one "
                              "before splitting it.")
WEIGHTS_MSG_EVEN_SPLIT_FMT = ("'{label}': every symbol now holds an equal share of "
                              "the label's 100%.")
WEIGHTS_MSG_ALREADY_EVEN_FMT = ("'{label}' is already split evenly — nothing was "
                                "written.")
WEIGHTS_MSG_FILLED_REST_FMT = ("'{label}': shared the remaining {remainder:.2f}% "
                               "between {count} empty symbol(s). The weights you "
                               "typed were left exactly as typed.")
#: BOTH halves of ``can_fill_remaining_symbol_weights``' refusal in one sentence,
#: because the two look identical from the button and have opposite fixes: nothing
#: is empty (type a 0 into the one you want filled) versus nothing is left (the
#: label is at or over 100 -- use Fill 100% to scale it, or Wipe to start again).
WEIGHTS_MSG_NOTHING_TO_FILL_FMT = (
    "'{label}': nothing to fill. Either every symbol already holds a share, or the "
    "shares total {total:.2f}% and there is nothing left to hand out — 'Fill 100%' "
    "scales an over-allocated label, 'Wipe' clears it.")
WEIGHTS_MSG_NO_PREVIOUS_FMT = ("No symbol in '{label}' has a previous share — this "
                               "label has never been allocated, so there is nothing "
                               "to load.")
WEIGHTS_MSG_LOADED_LAST_FMT = ("'{label}': restored the last run's shares. {kept} "
                               "symbol(s) have no history and kept the share they "
                               "had.")
WEIGHTS_MSG_LAST_UNCHANGED_FMT = ("'{label}' already holds the shares of the last "
                                  "run — nothing was written.")
WEIGHTS_MSG_WIPED_FMT = ("'{label}': cleared {count} share(s) to 0%. 'Load last' "
                         "puts them back — a wipe does not touch the history.")
WEIGHTS_MSG_ALREADY_CLEAR_FMT = ("'{label}' is already at 0% throughout — there is "
                                 "nothing to clear.")


@dataclass
class WeightsUpdate:
    """One press of a PER-LABEL symbol-share button, decided. Pure.

    Deliberately the same shape as ``FillToHundred`` (``changed``, ``reason_code``,
    ``weights``, ``message``) so the page can drive all five buttons on the row
    through one handler. ``changed is False`` means nothing is written and
    ``weights`` is what came in; the message is never empty.

    ``weights`` preserves the DISPLAY order it was given, for the reason
    ``TargetsUpdate.targets`` does: the engine's splitter puts the rounding
    remainder on the last entry.
    """
    changed: bool
    reason_code: str
    weights: Dict[str, float]
    message: str


def _symbols_as_engine(label: str, weights, previous=None):
    """``{symbol: pct}`` -> the engine's ``LabelTarget``, order preserved. Internal.

    ``target_pct`` is left at 0.0 and is never read back: every function this feeds
    touches the symbol weights only, and the label's own target is a share of a
    different denominator entirely.
    """
    history = previous or {}
    from ...core.portfolio_allocation import LabelTarget, SymbolTarget
    return LabelTarget(
        label=label, target_pct=0.0,
        symbols=[SymbolTarget(symbol=symbol, weight_pct=float(pct or 0.0),
                              previous_weight_pct=history.get(symbol))
                 for symbol, pct in (weights or {}).items()])


def _weights_of(target) -> Dict[str, float]:
    """A ``LabelTarget``'s symbol weights back as a plain map. Internal."""
    return {st.symbol: float(st.weight_pct or 0.0) for st in (target.symbols or [])}


def even_split_symbol_shares(label: str, weights) -> WeightsUpdate:
    """The per-label "Even split": ONE label's symbols share its 100% equally. Pure.

    ``even_split_symbol_weights``, which is ``even_split_pct``, which is what
    ``build_symbol_targets`` fills an untouched label in with -- so the button and
    the stored default cannot disagree about the same symbols. The label's own
    target does not move: this is about shares WITHIN a label.

    Unlike the wizard, a SINGLE-symbol label is not refused. The wizard disabled
    the button there because "a single symbol already owns the whole 100 by
    construction" -- which stopped being true when the boxes became editable on the
    page, where that symbol can sit at 40 and the split is the repair.
    """
    from ...core.portfolio_allocation import even_split_symbol_weights

    current = {s: float(pct or 0.0) for s, pct in (weights or {}).items()}
    if not current:
        return WeightsUpdate(False, WEIGHTS_NO_SYMBOLS, {},
                             WEIGHTS_MSG_NO_SYMBOLS_FMT.format(label=label))
    fresh = _weights_of(even_split_symbol_weights(_symbols_as_engine(label, current)))
    if _same_pcts(current, fresh):
        return WeightsUpdate(False, WEIGHTS_UNCHANGED, current,
                             WEIGHTS_MSG_ALREADY_EVEN_FMT.format(label=label))
    return WeightsUpdate(True, WEIGHTS_EVEN_SPLIT, fresh,
                         WEIGHTS_MSG_EVEN_SPLIT_FMT.format(label=label))


def fill_rest_symbol_shares(label: str, weights) -> WeightsUpdate:
    """The per-label "Fill rest": spread what is left over the EMPTY slots. Pure.

    NOT ``fill_label_to_100``, and the difference is the reason both buttons are on
    the row. This one never SCALES: every non-zero weight survives exactly as
    typed, and an over-allocated label is refused outright rather than trimmed.
    ``Fill 100%`` is the repair; this is the "type the two you care about, let the
    rest sort themselves out" half.

    ``can_fill_remaining_symbol_weights`` is the engine's own predicate and owns
    both halves of the refusal -- there has to be an empty slot AND something left
    to put in it -- so the button and the validator underneath cannot disagree.
    """
    from ...core.portfolio_allocation import (
        can_fill_remaining_symbol_weights, fill_remaining_symbol_weights)

    current = {s: float(pct or 0.0) for s, pct in (weights or {}).items()}
    if not current:
        return WeightsUpdate(False, WEIGHTS_NO_SYMBOLS, {},
                             WEIGHTS_MSG_NO_SYMBOLS_FMT.format(label=label))
    item = _symbols_as_engine(label, current)
    if not can_fill_remaining_symbol_weights(item):
        return WeightsUpdate(
            False, WEIGHTS_NOTHING_TO_FILL, current,
            WEIGHTS_MSG_NOTHING_TO_FILL_FMT.format(
                label=label, total=round(sum(current.values()), 2)))
    fresh = _weights_of(fill_remaining_symbol_weights(item))
    remainder = round(100.0 - sum(current.values()), 2)
    empty = sum(1 for pct in current.values() if pct == 0.0)
    return WeightsUpdate(True, WEIGHTS_FILLED_REST, fresh,
                         WEIGHTS_MSG_FILLED_REST_FMT.format(
                             label=label, remainder=remainder, count=empty))


def load_last_symbol_shares(label: str, weights, previous) -> WeightsUpdate:
    """The per-label "Load last": restore ONE label's shares from the last run. Pure.

    ``previous`` is ``get_previous_symbol_weights``' answer, which is a SEPARATE
    read from ``get_symbol_weights`` on purpose: that one fills an absent row with
    the even-split default, and there is no default for a share nobody has ever
    allocated with. Feeding this the live map turns the button into a no-op that
    still reports success.

    A symbol with no history keeps the share it has, and the label's own target
    does not move.
    """
    from ...core.portfolio_allocation import (
        has_previous_symbol_weights, load_previous_symbol_weights)

    current = {s: float(pct or 0.0) for s, pct in (weights or {}).items()}
    if not current:
        return WeightsUpdate(False, WEIGHTS_NO_SYMBOLS, {},
                             WEIGHTS_MSG_NO_SYMBOLS_FMT.format(label=label))
    item = _symbols_as_engine(label, current, previous)
    if not has_previous_symbol_weights(item):
        return WeightsUpdate(False, WEIGHTS_NO_PREVIOUS, current,
                             WEIGHTS_MSG_NO_PREVIOUS_FMT.format(label=label))
    fresh = _weights_of(load_previous_symbol_weights(item))
    if _same_pcts(current, fresh):
        return WeightsUpdate(False, WEIGHTS_UNCHANGED, current,
                             WEIGHTS_MSG_LAST_UNCHANGED_FMT.format(label=label))
    kept = sum(1 for st in item.symbols if st.previous_weight_pct is None)
    return WeightsUpdate(True, WEIGHTS_LOADED_LAST, fresh,
                         WEIGHTS_MSG_LOADED_LAST_FMT.format(label=label, kept=kept))


def wipe_symbol_shares(label: str, weights) -> WeightsUpdate:
    """The per-label "Wipe": clear ONE label's shares so it can be redone. Pure.

    What makes ``fill_rest_symbol_shares`` coherent: filling treats a 0 as an empty
    slot, so the honest way to redo a label is to empty it outright, type the
    handful that matter and fill the rest.

    Writes 0.0, never ``None`` -- every solver does arithmetic on the weight, and
    0.0 IS "empty" in this model. It is available on exactly the over-allocated set
    ``fill_rest_symbol_shares`` refuses, so the user is never cornered, and it
    leaves the previous generation untouched: Load last is its undo.
    """
    from ...core.portfolio_allocation import (
        can_wipe_symbol_weights, wipe_symbol_weights)

    current = {s: float(pct or 0.0) for s, pct in (weights or {}).items()}
    if not current:
        return WeightsUpdate(False, WEIGHTS_NO_SYMBOLS, {},
                             WEIGHTS_MSG_NO_SYMBOLS_FMT.format(label=label))
    item = _symbols_as_engine(label, current)
    if not can_wipe_symbol_weights(item):
        return WeightsUpdate(False, WEIGHTS_ALREADY_CLEAR, current,
                             WEIGHTS_MSG_ALREADY_CLEAR_FMT.format(label=label))
    return WeightsUpdate(True, WEIGHTS_WIPED, _weights_of(wipe_symbol_weights(item)),
                         WEIGHTS_MSG_WIPED_FMT.format(
                             label=label,
                             count=sum(1 for pct in current.values() if pct != 0.0)))


# ---------------------------------------------------------------------------
# THE LABEL MINI-BAR ROW
#
# One bar per label: the CURRENT share, tinted with that label's own colour, a
# NOTCH marking the target, and the DELTA between them.
#
# THE STATUS WORD IS GONE FROM THE SCREEN and that is deliberate. It said "over"
# beside a bar whose fill already sat past its notch, next to a delta that now reads
# "over by 20.0pp ($1,800.00)": three renderings of one fact, two of them content-
# free. ``LabelBar.status`` survives as the VERDICT -- it still picks the colour
# (``LABEL_STATUS_CLASSES``) and it is still what the tolerance band decides -- and
# ``delta_text`` is the single thing rendered from it, so the word, the sign, the
# money and the notch cannot tell different stories.
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
#: THE INVESTABLE POOL. A band, not equality: floating point makes equality
#: meaningless, and
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


#: How the delta reads. PERCENTAGE POINTS and MONEY together, because neither alone
#: tells the user what to do: 20pp of an unknown pool is not an order size, and
#: $1,800 without the share does not say whether that is a rounding drift or a third
#: of the label.
#: The current share beside the bar, and its "there is no pool" reading.
LABEL_CURRENT_FMT = '{pct:.1f}%'
LABEL_CURRENT_UNKNOWN = LABEL_STATUS_NONE

#: Drawn where a percentage would be when there is no previous generation. A dash,
#: never 0.00%: "this has never been allocated" and "last time this got nothing"
#: are different facts, and 0.00% is a legitimate value of the second. Spelled the
#: same way the wizard spelled it, because it is the same fact travelling to a new
#: screen rather than a new one being invented.
NO_PREVIOUS_MARK = '-'

#: The previous generation, as a percentage. Two decimals, matching the precision
#: the boxes store at, so a restored value reads identically to the one that will
#: be written back.
LAST_PCT_FMT = '{previous:.2f}%'

#: ...and named, because a bare second percentage on a row that already carries
#: three of them is unreadable. "last", not "previous target %": the row is terse
#: by design and ``BASIS_LEGEND`` carries the explanation for the whole page.
#:
#: NO denominator clause, deliberately. The wizard's caption said "% of base" and
#: that is the denominator the 2026-08-25 rework demoted to a parenthetical; a
#: stored target is typed against the INVESTABLE pool, so ``last`` is directly
#: comparable with the ``tgt`` beside it and needs no restating.
LAST_TARGET_FMT = 'last {previous}'

#: The P&L caption. The body comes from the engine's ``format_unrealised_pnl``,
#: which owns every "may this be shown at all" branch -- blank versus 0.00, a
#: percentage versus "no cost basis" -- so this string adds a name and nothing else.
PNL_CAPTION_FMT = 'P&L {pnl}'

LABEL_DELTA_OVER_FMT = 'over by {pct:.1f}pp ({money})'
LABEL_DELTA_UNDER_FMT = 'under by {pct:.1f}pp ({money})'
#: Inside the tolerance band. NOT "over by 0.0pp ($0.12)", which is noise on a row
#: that is, for every purpose the user has, exactly where it should be.
LABEL_DELTA_ON_TARGET = 'on target'
#: Nothing held and nothing asked for, or no pool to measure against. The same dash
#: ``LABEL_STATUS_NONE`` uses, for the same reason: a verdict about a decision nobody
#: made is worse than an admission that there is none.
LABEL_DELTA_NONE = LABEL_STATUS_NONE


def format_last_pct(previous: Optional[float]) -> str:
    """``60.00%``, or a dash when there is no previous generation. Pure.

    ``None`` is the ONLY dash case. 0.0 formats as ``0.00%`` because a run that
    allocated nothing to a label really did happen, and hiding it would make an
    intentional zero indistinguishable from a label that has never run.
    """
    if previous is None:
        return NO_PREVIOUS_MARK
    return LAST_PCT_FMT.format(previous=float(previous))


def format_last_target(previous: Optional[float]) -> str:
    """``last 60.00%`` / ``last -``. THE one place the pair is spelled. Pure.

    Used by the label row and by the symbol table alike, so the two scopes cannot
    describe their history in two different ways.
    """
    return LAST_TARGET_FMT.format(previous=format_last_pct(previous))


def format_pnl_caption(pnl: UnrealisedPnL) -> str:
    """``P&L +1,500.00 (+150.00%)``. Pure; the arithmetic and the wording are the
    engine's ``format_unrealised_pnl``, so this adds a name and nothing else."""
    return PNL_CAPTION_FMT.format(pnl=format_unrealised_pnl(pnl))


def pnl_classes(pnl: UnrealisedPnL) -> str:
    """CSS for a P&L caption. Pure; no ``ui`` call.

    Colour is an ACCENT and never the message: ``format_unrealised_pnl`` signs both
    numbers, so the caption reads correctly in monochrome, to a colour-blind user
    and in a screen reader. Grey covers three different things on purpose --
    nothing measurable, nothing held, and a genuine flat 0.00 -- because none of
    them is a verdict, and painting "break-even" green or red would invent one.
    """
    if pnl is None or pnl.amount is None or abs(pnl.amount) <= MONEY_EPSILON:
        return 'text-xs text-secondary-custom'
    return 'text-xs font-medium ' + ('text-green-500' if pnl.amount > 0
                                     else 'text-red-500')


def format_label_delta(*, status: str, delta_pct: Optional[float],
                       delta_value: Optional[float]) -> str:
    """The gap from target, in words, points and money. Pure.

    Driven by ``status``, not by the sign of ``delta_pct``, and that is what makes
    the sentence and the notch incapable of disagreeing: the tolerance band is
    applied once, in ``build_label_bars``, and this only renders its verdict.
    """
    if status == LABEL_STATUS_OK:
        return LABEL_DELTA_ON_TARGET
    if status == LABEL_STATUS_NONE or delta_pct is None or delta_value is None:
        return LABEL_DELTA_NONE
    template = (LABEL_DELTA_OVER_FMT if status == LABEL_STATUS_OVER
                else LABEL_DELTA_UNDER_FMT)
    return template.format(pct=abs(delta_pct),
                           money=format_account_money(abs(delta_value)))


@dataclass
class LabelBar:
    """One label's row in the mini-bar list. Pure geometry plus a verdict.

    EVERY percentage here divides the INVESTABLE POOL -- see the denominator rule
    above -- so ``current_pct``, ``target_pct`` and ``delta_pct`` are directly
    comparable and the delta is their plain difference.

    ``bar_fraction`` and ``notch_fraction`` are 0-1 of the SAME track.
    ``notch_fraction`` is ``None`` only when there is no pool to place it against.

    ``current_pct`` keeps its sign: a net-short label is genuinely negative, and the
    figure printed beside the bar says so even though the bar itself clamps to empty
    (a negative width would otherwise render as a full track -- the wrong way round
    from the truth). It is ``None`` when there is no investable pool at all, which is
    a different fact from 0.0% and is drawn as a dash.

    It is also NOT CLAMPED ABOVE. A margin book legitimately holds more than the
    pool -- 1.32x the account value on the reporting book -- and 133.3% is a true and
    useful statement. The TRACK stretches to it instead (the scale is the page
    maximum), so the bar fills and nothing is clipped or misread.

    ``target_pct`` is what the edit box holds -- the number the user typed.
    ``effective_pct`` is that restated against the gross base, i.e. the derived
    "(real N%)" figure, and is secondary everywhere.

    ``last_text`` and ``pnl_text`` come out of the SAME builder as everything else
    on the row, which is the point of putting them here rather than rendering them
    beside it: the header, the notch, the delta, the previous generation and the
    P&L are one description of one label, and a second writer is how two of them
    come to disagree. Neither moves when a target box is typed in -- the previous
    generation only advances on a run, and the P&L is measured off the positions
    and quotes the render opened with -- but they are rewritten by the same redraw
    anyway, so nothing has to remember which figures are live.
    """
    label: str
    color: str
    current_value: float
    current_pct: Optional[float]
    target_pct: float
    effective_pct: float
    target_value: Optional[float]
    delta_pct: Optional[float]
    delta_value: Optional[float]
    bar_fraction: float
    notch_fraction: Optional[float]
    status: str
    current_text: str
    target_text: str
    delta_text: str
    previous_target_pct: Optional[float] = None
    last_text: str = ''
    pnl: UnrealisedPnL = field(default_factory=UnrealisedPnL)
    pnl_text: str = ''


def investable_share_pct(current_value: float, base_notional: Optional[float],
                         unallocated_pct: float) -> Optional[float]:
    """A holding as a share of the INVESTABLE pool. ``None`` when there is no pool.

    Computed from MONEY, never as ``pct_of_base / (1 - r/100)``. The two are equal
    wherever both are defined, but the inverse form hides the singularity: at a 100%
    reserve it divides by zero, and a 100% reserve is a legitimate setting (allocate
    nothing this cycle). Dividing money by a pool of 0.00 is the same singularity
    stated where it can be answered -- there is no pool, so there is no share.
    """
    pool = (None if not base_notional
            else investable_notional(base_notional, unallocated_pct))
    if not pool:
        return None
    return float(current_value or 0.0) / pool * 100.0


def bar_scale_pct(views, *, base_notional: Optional[float],
                  unallocated_pct: float) -> float:
    """What 100% of the mini-bar track represents, in percentage points. Pure.

    The largest of every label's CURRENT share and every label's TARGET, both on the
    investable basis, floored at ``_MIN_BAR_SCALE_PCT`` so there is always something
    to divide by. Negative currents (shorts) are excluded from the maximum -- they
    cannot set the top of a track that starts at zero.

    Because the scale is the page MAXIMUM, a holding above 100% of the pool stretches
    the track rather than overflowing it: nothing is ever clipped and the printed
    figure is never clamped. The trade is that the track's meaning changes with the
    book, which is why the axis is labelled and the percentages are printed beside it.

    Computed ONCE for the whole page. Per-row scaling would make the bars
    incomparable, which is the only thing they are for.
    """
    best = _MIN_BAR_SCALE_PCT
    for view in (views or []):
        share = investable_share_pct(view.current_value, base_notional, unallocated_pct)
        best = max(best, float(share or 0.0), float(view.target_pct or 0.0))
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
    scale = bar_scale_pct(views, base_notional=base_notional,
                          unallocated_pct=unallocated_pct)
    pool = (None if not base_notional
            else investable_notional(base_notional, unallocated_pct))
    bars: List[LabelBar] = []
    for view in (views or []):
        target = float(view.target_pct or 0.0)
        effective = effective_target_pct(target, unallocated_pct)
        share = investable_share_pct(view.current_value, base_notional, unallocated_pct)
        target_value = None if pool is None else pool * target / 100.0
        delta_pct = delta_value = None
        if share is None:
            # No pool: no share of it, no notch, no verdict. Distinct from 0.0%,
            # and reached at a 100% reserve (allocate nothing) as well as on an
            # account whose broker published no base at all.
            status = LABEL_STATUS_NONE
            notch = None
        else:
            # ONE call, deliberately outside the status branches. It was written
            # twice -- once for the empty-label case and once for the rest -- and a
            # mutation that put a different scale into the empty branch could not be
            # caught by any test, because an empty label's target is 0 and 0/x is 0
            # whatever x is. Two spellings of one rule, one of them unobservable.
            notch = _bar_fraction(target, scale)
            if not target and not view.current_value:
                status = LABEL_STATUS_NONE
            else:
                # The plain difference of two numbers on ONE denominator, which is
                # the whole point of the investable basis. The money is taken as
                # ``current - target`` rather than as a share of the pool so it
                # cannot drift from the figures the solver will use; the two agree
                # exactly, and a test pins that they do.
                delta_pct = share - target
                delta_value = float(view.current_value or 0.0) - (target_value or 0.0)
                if delta_pct > LABEL_STATUS_TOLERANCE_PCT:
                    status = LABEL_STATUS_OVER
                elif delta_pct < -LABEL_STATUS_TOLERANCE_PCT:
                    status = LABEL_STATUS_UNDER
                else:
                    status = LABEL_STATUS_OK
        bars.append(LabelBar(
            label=view.label,
            color=resolve_label_icon_color(view.color),
            current_value=view.current_value,
            current_pct=share,
            target_pct=target,
            effective_pct=effective,
            target_value=target_value,
            delta_pct=delta_pct,
            delta_value=delta_value,
            bar_fraction=_bar_fraction(share or 0.0, scale),
            notch_fraction=notch,
            status=status,
            current_text=(LABEL_CURRENT_UNKNOWN if share is None
                          else LABEL_CURRENT_FMT.format(pct=share)),
            target_text=TARGET_CELL_PREFIX + format_target_pair(target, unallocated_pct),
            delta_text=format_label_delta(status=status, delta_pct=delta_pct,
                                          delta_value=delta_value),
            previous_target_pct=view.previous_target_pct,
            last_text=format_last_target(view.previous_target_pct),
            pnl=view.pnl,
            pnl_text=format_pnl_caption(view.pnl),
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


# ---------------------------------------------------------------------------
# THE ACCOUNT VALUE CARD
#
# ``managed_total_value`` above is the market value of the MANAGED POSITIONS. On
# a margin account that is not what the account is worth: the reporting book had
# $4,853.48 of managed positions against roughly $2,400 of account value, so a
# summary row carrying only the first described an account twice its real size.
# Both figures are useful and neither substitutes for the other -- the leverage
# between them is precisely the thing the user could not see.
# ---------------------------------------------------------------------------

#: The card's caption. "Account value", not "Equity" or "Net liquidation": those
#: are broker words for the same quantity and the row already makes the reader
#: hold three other denominators in mind.
ACCOUNT_VALUE_TITLE = 'Account value'

#: What the money line reads when the figure is UNKNOWN. Deliberately not a
#: number and deliberately not blank: ``$0.00`` under this caption reads as an
#: account with nothing in it, and an empty card reads as a rendering bug.
ACCOUNT_VALUE_UNAVAILABLE_TEXT = 'n/a'

#: ...and why, in the manner of the expert cards' ``Confidence: n/a — <reason>``.
#: A bare "n/a" leaves the reader unable to tell an outage from a setting. The
#: closing clause is the one that matters, and it is spelled "not zero" rather
#: than "not $0.00" on purpose: the sentence must not itself contain the string
#: the card is promising never to print.
ACCOUNT_VALUE_UNAVAILABLE_DETAIL = (
    'the broker published no net liquidating value — unknown, not zero')

#: The leverage clause, under the money. "this" is unambiguous because the line
#: sits inside the account-value card, directly under the figure it divides by.
#: Two decimals, because the interesting range is 1.00x-3.00x and one decimal
#: cannot separate 1.95 from 2.04.
ACCOUNT_VALUE_LEVERAGE_FMT = 'managed positions are {leverage:,.2f}x this'


def account_value_from_snapshot(snapshot) -> Optional[float]:
    """The account's OWN value out of a broker snapshot. Pure. ``None`` if unknown.

    ``net_liquidation`` is THE field, and the choice is the ``AccountSnapshot``
    contract's own: "Neither is the allocation denominator ... so report
    ``net_liquidation`` as the account's headline total value." The neighbours are
    all wrong here and each would look plausible:

    * ``cash`` is NEGATIVE while a margin loan is outstanding;
    * ``long_market_value`` is (roughly) the number the 'Managed value' card
      already shows -- the very duplication this card exists to break;
    * ``buying_power`` is the third card;
    * ``equity`` is the same number in practice (see below) but is Alpaca's word,
      and the contract nominates the other one.

    Alpaca and TastyTrade DO NOT DIVERGE on it. Alpaca maps
    ``TradeAccount.equity`` onto both fields (``AlpacaAccount.py``:
    ``net_liquidation=equity``) and TastyTrade maps ``net-liquidating-value``
    onto both (``TastyTradeAccount.py``: ``equity=net_liquidation``); the base
    ``ReadOnlyAccountInterface.get_account_snapshot`` mirrors whichever one a
    third broker publishes onto the other. So there is no adapter on which
    reading ``net_liquidation`` can come back ``None`` while ``equity`` is set,
    and a fallback chain between them would be an untestable second rule.

    ``None`` means UNKNOWN and is never turned into 0.0 -- an all-``None``
    snapshot is what both adapters return on an auth failure.

    A non-numeric value raises rather than being swallowed: the caller
    (``_load_view_payload``) already wraps the snapshot read in the try/except
    that turns a broker problem into "unavailable" WITH a log line, and silently
    returning ``None`` here would lose the log line.
    """
    if snapshot is None:
        return None
    value = getattr(snapshot, 'net_liquidation', None)
    if value is None:
        return None
    return float(value)


def format_account_money(value: float) -> str:
    """``$2,511.90`` / ``-$1,200.00``. Pure.

    The sign goes OUTSIDE the currency symbol, where ``f'${v:,.2f}'`` would put it
    inside (``$-1,200.00``). The neighbouring cards use the plain form because
    none of them can go negative; this one can -- an account underwater on its
    margin loan -- and the minus is the single character in that string that
    changes what it means, so it leads.
    """
    return ('-' if value < 0 else '') + f'${abs(value):,.2f}'


@dataclass
class AccountValueCard:
    """The 'Account value' summary card, decided. Pure.

    ``available is False`` means the broker gave us no figure: ``text`` is
    ``n/a`` and ``detail`` says why. It is NOT the same as an account genuinely
    worth nothing, which is ``available=True`` with ``text='$0.00'`` -- a fully
    withdrawn account is a real state, and reporting it as an outage is the
    inverse of the bug this card is careful about.

    ``leverage`` is ``managed / account`` or ``None`` when it cannot be stated.
    ``detail`` carries either the leverage clause or the unavailable reason,
    never both: they are mutually exclusive by construction.
    """
    title: str
    text: str
    detail: str
    available: bool
    leverage: Optional[float]


def account_value_card(*, account_value: Optional[float],
                       managed_value: float) -> AccountValueCard:
    """Build the 'Account value' card. Pure; never raises, never divides by zero.

    Args:
        account_value: ``account_value_from_snapshot``'s answer. ``None`` is
            UNKNOWN -- the broker did not answer, or published no net liquidating
            value -- and is the case the whole function is shaped around.
        managed_value: ``managed_total_value(views)``, the figure in the card next
            door, used only for the leverage clause.

    Returns:
        AccountValueCard: with ``leverage`` (and its clause) present only when
        ``account_value`` is a POSITIVE number. At exactly 0 the ratio is
        undefined and ``inf x`` is not a caption; below 0 the account is
        underwater and a negative multiple of a negative base inverts the sense
        of "leveraged". Both still print their figure -- it is the multiple that
        is dropped, not the money.

        A negative ``managed_value`` (a net-short managed book, which this page
        signs negative) DOES produce a negative multiple: that is a fact about
        the book rather than an undefined quantity.
    """
    if account_value is None:
        return AccountValueCard(title=ACCOUNT_VALUE_TITLE,
                                text=ACCOUNT_VALUE_UNAVAILABLE_TEXT,
                                detail=ACCOUNT_VALUE_UNAVAILABLE_DETAIL,
                                available=False, leverage=None)

    value = float(account_value)
    if value > 0.0:
        leverage = float(managed_value or 0.0) / value
        detail = ACCOUNT_VALUE_LEVERAGE_FMT.format(leverage=leverage)
    else:
        leverage = None
        detail = ''
    return AccountValueCard(title=ACCOUNT_VALUE_TITLE,
                            text=format_account_money(value),
                            detail=detail, available=True, leverage=leverage)


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
