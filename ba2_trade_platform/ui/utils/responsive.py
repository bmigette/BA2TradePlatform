"""Phone layouts for wide tables, decided in pure code.

WHY A MODULE. The app's global phone layer (``ui/static/styles.css``, "RESPONSIVE /
MOBILE") makes every table scroll sideways under 640px. That is the right default
for a table nobody has looked at on a phone yet, and the wrong answer for the two
that matter most on the Portfolio Allocation page: a sixteen-column symbol table
and an eighteen-column dry-run plan. On a 390px screen the first shows two columns
at a time and the second -- hand-rolled out of flex rows -- collapsed into a single
column of unlabelled numbers, because the global rules turn every ``w-24`` into
``width:100%`` and wrap every ``ui.row``.

TWO MECHANISMS, one per kind of table, and the data model is NEVER forked: the
rows a table is built from are the rows both layouts read.

* A real ``ui.table`` (Quasar ``q-table``) switches to its own ``grid`` mode, whose
  ``item`` slot draws one card per row. ``phone_grid_prop`` emits the prop;
  ``PhoneTableRegistry`` flips it when the viewport crosses the breakpoint, because a
  NiceGUI ``:prop`` is evaluated once and a phone rotated to landscape must not stay
  stuck in the layout it loaded with.
* A hand-rolled grid of ``ui.row`` + ``ui.label`` cells keeps ONE DOM -- the same
  elements, the same markers, the same result cells a submit writes into -- and is
  re-laid-out by CSS alone (``card_rows_css``): each row becomes a two-column card
  of captioned values, with the secondary columns folded behind a tap. CSS-only is
  what makes the "one DOM" part possible; a second, phone-only copy of the rows
  would have needed its own result cells and its own tick boxes.

Every layout decision (which columns are primary, which fold, what each is
captioned) is DATA -- a tuple of ``CardColumn`` -- and the CSS and the Vue card are
generated from it, so a column added to a table without a decision about the phone
fails a unit test (``check_card_columns``) instead of silently landing nowhere.

The breakpoint is the app's own: 639px, the ``@media (max-width: 639px)`` of the
global phone layer, i.e. Quasar's ``xs``. Duplicating the number here is deliberate
and pinned by a test against ``styles.css``.
"""
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
import re
import weakref

#: The app's phone breakpoint, in CSS px. ``styles.css`` uses the same value in its
#: ``@media (max-width: 639px)`` block; a test keeps the two equal.
PHONE_MAX_WIDTH_PX = 639

#: Tiers of a card column.
#:   tick    the row's selection box, pinned to the card's top-left corner
#:   head    first line of the card: the symbol (left) and the verdict (right)
#:   primary the captioned two-column grid that is always visible
#:   detail  the same grid, folded behind "Details" until the card is tapped
#:   wide    a full-width line under the grid (free text), always visible
TIER_TICK = 'tick'
TIER_HEAD = 'head'
TIER_PRIMARY = 'primary'
TIER_DETAIL = 'detail'
TIER_WIDE = 'wide'
TIERS = (TIER_TICK, TIER_HEAD, TIER_PRIMARY, TIER_DETAIL, TIER_WIDE)

#: Class that marks a header row that is not drawn on a phone (the cards are
#: self-describing), and the class a tap on a card toggles to reveal its details.
CARD_HEAD_CLASS = 'pf-card-head'
CARD_ROW_CLASS = 'pf-card-row'
CARD_OPEN_CLASS = 'pf-open'


@dataclass(frozen=True)
class CardColumn:
    """What one column of a wide table becomes on a phone."""
    name: str
    caption: str
    tier: str


def phone_media(css: str) -> str:
    """Wrap ``css`` in the phone media query. Pure."""
    return f'@media (max-width: {PHONE_MAX_WIDTH_PX}px) {{\n{css}\n}}'


def is_phone_width(width_px: float) -> bool:
    """Whether a viewport ``width_px`` wide is a phone. The Python twin of the
    ``Quasar.Screen.width <= 639`` the first render evaluates in the browser, and the
    function the breakpoint-crossing handler decides with. Pure."""
    return float(width_px) <= PHONE_MAX_WIDTH_PX


def phone_grid_expression() -> str:
    """The JS expression a ``:grid`` prop evaluates on first render.

    ``Quasar.Screen`` is the UMD global NiceGUI already loads; ``<=`` against the
    same number the stylesheet's ``max-width`` uses, so the CSS layer and the table
    layout agree about which side of the line a 639px viewport is on.
    """
    return f'Quasar.Screen.width <= {PHONE_MAX_WIDTH_PX}'


def phone_grid_prop(phone: Optional[bool] = None) -> str:
    """The ``ui.table(...).props(...)`` string that puts a q-table in card mode on a
    phone. ``phone=None`` reads the viewport in the browser (first render); a bool
    pins it (the registry's re-flip after a resize)."""
    expression = phone_grid_expression() if phone is None else str(bool(phone)).lower()
    return f':grid="{expression}"'


# ---------------------------------------------------------------------------
# Validation + partition (pure; the unit tests lean on these)
# ---------------------------------------------------------------------------

def check_card_columns(card_columns: Sequence[CardColumn],
                       table_columns: Iterable[str]) -> None:
    """Refuse a card spec that does not account for exactly the table's columns.

    Raises ``ValueError`` naming every unclassified and every unknown column. A
    table column with no phone decision would be silently absent from the card --
    on a money table, the failure mode this app does not accept.
    """
    wanted = list(table_columns)
    declared = [c.name for c in card_columns]
    problems: List[str] = []
    missing = [n for n in wanted if n not in declared]
    unknown = [n for n in declared if n not in wanted]
    duplicate = sorted({n for n in declared if declared.count(n) > 1})
    bad_tier = [c.name for c in card_columns if c.tier not in TIERS]
    bad_caption = [c.name for c in card_columns
                   if '"' in c.caption or '\\' in c.caption or '\n' in c.caption]
    if missing:
        problems.append(f'columns with no phone decision: {missing}')
    if unknown:
        problems.append(f'phone decision for columns the table does not have: {unknown}')
    if duplicate:
        problems.append(f'columns declared twice: {duplicate}')
    if bad_tier:
        problems.append(f'unknown tier on: {bad_tier}')
    if bad_caption:
        problems.append(f'caption not safe inside a CSS string: {bad_caption}')
    if problems:
        raise ValueError('; '.join(problems))


def columns_by_tier(card_columns: Sequence[CardColumn]) -> Dict[str, List[str]]:
    """``{tier: [column names in declared order]}``, every tier present. Pure."""
    out: Dict[str, List[str]] = {tier: [] for tier in TIERS}
    for column in card_columns:
        out[column.tier].append(column.name)
    return out


# ---------------------------------------------------------------------------
# Hand-rolled grids -> cards, in CSS
# ---------------------------------------------------------------------------

#: The rules every card row shares, independent of which table it is. ``.x.x``
#: doubles a class to out-rank the global phone layer's single-class
#: ``.w-24 {{ width:100% !important }}`` family without resorting to more
#: ``!important`` than the two sides already trade.
_CARD_COMMON_CSS = f'''
    .{CARD_HEAD_CLASS} {{ display: none !important; }}
    .{CARD_ROW_CLASS}.{CARD_ROW_CLASS} > :empty {{ display: none !important; }}
    .{CARD_ROW_CLASS}.{CARD_ROW_CLASS} > .pf-empty {{ display: none !important; }}
    .{CARD_ROW_CLASS}.{CARD_ROW_CLASS} > * {{
        font-size: 0.9rem !important; text-transform: none !important;
        letter-spacing: 0 !important; min-width: 0 !important;
        overflow-wrap: anywhere; }}
    .pf-card-wrap.pf-card-wrap {{ min-width: 0 !important; max-width: 100% !important; }}
'''


def card_cell_class(prefix: str, name: str) -> str:
    """The class one cell wears so the card CSS can address it. Pure."""
    return f'{prefix}{name}'


def _caption_rule(selector: str, caption: str) -> str:
    return (f'{selector}::before {{ content: "{caption}"; margin-right: auto; '
            f'color: #94a3b8; font-size: 0.78rem; font-weight: 400; '
            f'white-space: nowrap; }}')


def card_rows_css(row_key: str, prefix: str, card_columns: Sequence[CardColumn],
                  ) -> str:
    """The phone CSS that lays one hand-rolled grid out as cards. Pure.

    ``row_key`` is the class every row (and the footer, if any) of this table wears
    beside ``pf-card-row``; ``prefix`` + column name is the class on each cell.
    Source order decides the card's order, so the spec's order IS the reading order.

    Only the media query and the rules for THIS table are returned; call
    ``card_rows_common_css`` once for the shared part.
    """
    base = f'.{CARD_ROW_CLASS}.{row_key}'
    has_tick = any(c.tier == TIER_TICK for c in card_columns)
    has_detail = any(c.tier == TIER_DETAIL for c in card_columns)
    left = '3rem' if has_tick else '0.5rem'
    rules: List[str] = [
        f'{base} {{ display: grid !important; '
        f'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); '
        f'column-gap: 0.75rem; row-gap: 0.125rem; align-items: baseline; '
        f'position: relative; width: 100% !important; min-width: 0 !important; '
        f'padding: 0.5rem 0.5rem 0.25rem {left} !important; }}',
    ]
    order = 0
    head_seen = 0
    for column in card_columns:
        cell = f'{base} > .{card_cell_class(prefix, column.name)}'
        order += 1
        common = 'width: auto !important; max-width: none !important; '
        if column.tier == TIER_TICK:
            rules.append(f'{cell} {{ position: absolute; left: 0.25rem; top: 0.1rem; '
                         f'{common}}}')
        elif column.tier == TIER_HEAD:
            head_seen += 1
            if head_seen == 1:
                rules.append(f'{cell} {{ order: {order}; grid-column: 1; {common}'
                             f'font-size: 1.05rem !important; font-weight: 600; '
                             f'text-align: left; }}')
            else:
                rules.append(f'{cell} {{ order: {order}; grid-column: 2; {common}'
                             f'text-align: right; justify-self: end; }}')
        elif column.tier in (TIER_PRIMARY, TIER_DETAIL):
            display = 'flex' if column.tier == TIER_PRIMARY else 'none'
            rules.append(
                f'{cell} {{ order: {order}; display: {display} !important; '
                f'justify-content: flex-end; align-items: baseline; gap: 0.25rem; '
                f'text-align: right; {common}}}')
            rules.append(_caption_rule(cell, column.caption))
            if column.tier == TIER_DETAIL:
                rules.append(f'{base}.{CARD_OPEN_CLASS} > '
                             f'.{card_cell_class(prefix, column.name)} '
                             f'{{ display: flex !important; }}')
        else:  # wide
            rules.append(f'{cell} {{ order: {order + 100}; grid-column: 1 / -1; '
                         f'{common}text-align: left; }}')
    if has_detail:
        rules.append(
            f'{base}::after {{ content: "Details \\25BE"; order: 99; '
            f'grid-column: 1 / -1; text-align: center; color: #94a3b8; '
            f'font-size: 0.85rem; line-height: 2.5rem; min-height: 2.5rem; }}')
        rules.append(f'{base}.{CARD_OPEN_CLASS}::after '
                     f'{{ content: "Hide details \\25B4"; }}')
    return phone_media('\n'.join('    ' + r for r in rules))


def card_rows_common_css() -> str:
    """The shared half of ``card_rows_css``. Pure."""
    return phone_media(_CARD_COMMON_CSS)


#: Tap on a card row: reveal / hide its details. Runs in the browser only (no server
#: round trip, so it costs nothing per tap and survives a slow link). A tap on a
#: checkbox, button, input or link is that control's own and must not fold the card.
#: Gated on the breakpoint so a desktop click on a row does nothing at all.
CARD_TOGGLE_JS = (
    '(e) => { if (e.target.closest(".q-checkbox, .q-btn, input, textarea, a")) return; '
    f'if (!window.matchMedia("(max-width: {PHONE_MAX_WIDTH_PX}px)").matches) return; '
    f'e.currentTarget.classList.toggle("{CARD_OPEN_CLASS}"); }}'
)


def attach_card_toggle(row):
    """Make ``row`` (a ``ui.row``) fold its details open on a phone tap. Returns it."""
    row.on('click', js_handler=CARD_TOGGLE_JS)
    return row


# ---------------------------------------------------------------------------
# Generic phone rules for the page's other widgets (CSS-only)
# ---------------------------------------------------------------------------

#: A ``ui.row`` carrying ``pf-wrap-row`` is a bar-plus-figures line. On a phone it
#: wraps, its children size to their content (the global layer's
#: ``.w-16 ... {width:100%}`` would otherwise make each figure a full line inside a
#: ``no-wrap`` row, which is overflow), and a bar track takes the whole line.
WRAP_ROW_CLASS = 'pf-wrap-row'
BAR_TRACK_CLASS = 'pf-bar-track'

#: A row of action buttons: tap targets of at least 40px on a phone.
ACTIONS_CLASS = 'pf-actions'

#: The dialogs' primary action: first and full width on a phone, where the
#: dialog's button row is the one thing that must stay reachable.
PRIMARY_ACTION_CLASS = 'pf-primary-action'

GENERIC_PHONE_CSS = phone_media(f'''
    .{WRAP_ROW_CLASS}.{WRAP_ROW_CLASS} {{ flex-wrap: wrap !important; row-gap: 0.25rem; }}
    .{WRAP_ROW_CLASS}.{WRAP_ROW_CLASS} > * {{
        width: auto !important; max-width: 100% !important; min-width: 0 !important; }}
    .{WRAP_ROW_CLASS}.{WRAP_ROW_CLASS} > .{BAR_TRACK_CLASS} {{
        flex: 1 1 100% !important; }}
    .{WRAP_ROW_CLASS}.{WRAP_ROW_CLASS} > .q-slider {{
        flex: 1 1 10rem !important; min-width: 10rem !important; }}
    .{ACTIONS_CLASS} .q-btn {{ min-height: 40px !important; }}
    .{ACTIONS_CLASS} .q-field__control {{ min-height: 40px !important; }}
    /* DIALOGS use the viewport: full width, a scrollable body, never a fixed box. */
    .q-dialog__inner--minimized {{ padding: 0.5rem !important; }}
    .q-dialog__inner--minimized > div {{
        width: 100% !important; max-width: 100% !important; min-width: 0 !important;
        max-height: calc(100vh - 1rem) !important; overflow-y: auto; }}
    .q-dialog .q-btn {{ min-height: 40px !important; }}
    .{PRIMARY_ACTION_CLASS} {{ order: -1; flex: 1 1 100% !important; }}
''')


# ---------------------------------------------------------------------------
# q-table grid mode: follow the viewport after first render
# ---------------------------------------------------------------------------

#: The browser event the page's resize listener emits when the viewport crosses the
#: breakpoint, and the script that emits it. ``matchMedia`` fires ``change`` only on
#: a crossing, so a desktop window drag costs nothing.
PHONE_EVENT = 'pf_phone_change'
PHONE_LISTENER_HEAD_HTML = (
    '<script>(function(){var m=window.matchMedia("(max-width: '
    f'{PHONE_MAX_WIDTH_PX}px)");'
    'var f=function(e){if(window.emitEvent){window.emitEvent("'
    f'{PHONE_EVENT}",{{phone:e.matches,width:window.innerWidth}});}}}};'
    'if(m.addEventListener){m.addEventListener("change",f);}else{m.addListener(f);}'
    '})();</script>'
)


class PhoneTableRegistry:
    """The q-tables of ONE page that are in card mode on a phone.

    ``register`` is called for each table as it is built; ``set_phone`` re-pins every
    live one. Held weakly: a refresh rebuilds the tables, and the old ones must be
    free to go.
    """

    def __init__(self) -> None:
        self._tables: 'weakref.WeakSet' = weakref.WeakSet()
        #: The layout the tables were last PINNED to; ``None`` until the first event.
        self._phone: Optional[bool] = None

    def register(self, table) -> None:
        self._tables.add(table)
        if self._phone is not None:
            table.props(phone_grid_prop(self._phone))

    def set_phone(self, phone: bool) -> None:
        for table in list(self._tables):
            table.props(phone_grid_prop(phone))

    def on_width(self, width_px: float) -> bool:
        """The viewport is now ``width_px`` wide. Re-pin the tables when that is a
        different layout from the one they are in; return whether anything changed.

        A phone rotated to landscape (390 -> 844) goes back to the table; rotated
        again (844 -> 390) it goes to cards; a resize within the same side of the
        line does nothing. Tables registered AFTER an event are pinned too (a refresh
        rebuilds them), which is why the last state is remembered.
        """
        phone = is_phone_width(width_px)
        if phone == self._phone:
            return False
        self._phone = phone
        self.set_phone(phone)
        return True

    def __len__(self) -> int:
        return len(self._tables)


def install_phone_listener(registry: PhoneTableRegistry, add_head_html: Callable[[str], None],
                           on_event: Callable[..., None]) -> None:
    """Wire the browser's breakpoint-crossing event to ``registry``.

    ``add_head_html`` and ``on_event`` are ``ui.add_head_html`` and ``ui.on``,
    passed in so this module imports no NiceGUI and the wiring is testable.
    """
    add_head_html(PHONE_LISTENER_HEAD_HTML)

    def _changed(event) -> None:
        registry.on_width(float(event.args['width']))

    on_event(PHONE_EVENT, _changed)


# ---------------------------------------------------------------------------
# Wide q-table kept as a TABLE: scroll sideways, pin the identifying column(s)
# ---------------------------------------------------------------------------
# (Added for the Live Trades page. Purely additive: nothing above changed.)
#
# The other mechanism in this module (``phone_grid_prop``) turns a table into cards.
# That is wrong for a table whose point is comparing a column down its rows, and the
# operator asked for the opposite on Live Trades: every column kept, scrolling inside
# the table's own box, the Symbol pinned on the left. Quasar adds no per-column class
# of its own, so the columns are tagged through their ``classes``/``headerClasses``
# (``column_tag``) and the CSS below addresses the tag, never a position.

#: Theme colours the pinned cells must be painted with. OPAQUE: a translucent sticky
#: cell lets the scrolling columns show through it. These are the solid equivalents
#: of the table theme's translucent row (``rgba(37, 43, 59, .5)``) and header
#: (``rgba(26, 31, 46, .9)``) colours in ``styles.css``.
PINNED_BODY_BG = '#252b3b'
PINNED_HEAD_BG = '#1a1f2e'
#: The row-hover tint of ``styles.css`` (``tbody tr:hover``), laid OVER the opaque
#: pinned background so a hovered / tapped row is highlighted across its pinned cells
#: too, instead of from the second column on.
PINNED_HOVER_TINT = 'rgba(0, 212, 170, 0.1)'

PIN_TAG_PREFIX = 'lt-c-'


def column_tag(prefix: str, name: str) -> str:
    """The class a column wears (cell and header) so CSS can address it. Anything that
    is not a letter, digit or underscore becomes ``-`` (a column named ``Fill prem.``
    must still give a valid class). Pure."""
    return f'{prefix}' + re.sub(r'[^A-Za-z0-9_]+', '-', str(name)).strip('-')


def tag_quasar_columns(quasar_columns: Sequence[dict], prefix: str) -> List[dict]:
    """Add the column tag to each Quasar column dict's ``classes`` and
    ``headerClasses`` (keeping any class already there, e.g. ``mobile-hide``).
    Returns new dicts; the input is not modified. Pure."""
    out: List[dict] = []
    for col in quasar_columns:
        tag = column_tag(prefix, col['name'])
        new = dict(col)
        for key in ('classes', 'headerClasses'):
            existing = str(new[key]) if key in new and new[key] else ''
            new[key] = f'{existing} {tag}'.strip()
        out.append(new)
    return out


@dataclass(frozen=True)
class PinnedColumn:
    """A column that stays at ``left_px`` while the rest scrolls, ``width_px`` wide."""
    name: str
    left_px: int
    width_px: int


def check_pinned_columns(pinned: Sequence[PinnedColumn], table_columns: Iterable[str],
                         min_widths: Dict[str, int]) -> None:
    """Refuse a pin / min-width spec that names a column the table does not have, or
    a pinned column whose offset is not the sum of the pinned widths before it (which
    would make two pinned cells overlap, or leave a gap the scroll shows through).
    Pure. Raises ``ValueError`` naming every problem."""
    names = list(table_columns)
    problems: List[str] = []
    unknown = [p.name for p in pinned if p.name not in names]
    unknown += [n for n in min_widths if n not in names]
    if unknown:
        problems.append(f'columns the table does not have: {sorted(set(unknown))}')
    expected = 0
    for p in pinned:
        if p.left_px != expected:
            problems.append(f'{p.name}: left {p.left_px}px, expected {expected}px '
                            f'(the widths pinned before it)')
        expected += p.width_px
    if problems:
        raise ValueError('; '.join(problems))


def pinned_offsets(pinned: Sequence[PinnedColumn]) -> Dict[str, int]:
    """``{column: left offset}`` from the widths, i.e. what ``left_px`` must be. Pure."""
    out: Dict[str, int] = {}
    left = 0
    for p in pinned:
        out[p.name] = left
        left += p.width_px
    return out


def scroll_table_phone_css(root_class: str, prefix: str, pinned: Sequence[PinnedColumn],
                           min_widths: Dict[str, int], max_height: str = '100vh - 8rem',
                           ) -> str:
    """The phone CSS of one wide q-table (``root_class`` is on the table root).

    * the table scrolls INSIDE ``.q-table__middle`` (momentum scrolling, no page-level
      horizontal scroll) and is height-capped so its header can stay sticky on top;
    * header labels and cells never wrap; each tagged column has a min-width that fits
      its content, so the reader scrolls instead of reading clipped text;
    * the pinned columns are sticky at their offsets, opaque, painted with the row's
      hover tint, and the last one carries a shadow so it reads as pinned.
    Pure.
    """
    r = f'.{root_class}'
    # Direct-child chain, NOT descendants: a row-expansion can hold a nested q-markup-table
    # (the orders list) that must keep its own layout.
    mid = f'{r} > .q-table__middle'
    tbl = f'{mid} > table'
    # ``max_height`` is an expression ("100vh - 8rem"), emitted twice: ``dvh`` (the visible
    # viewport as the iOS toolbar comes and goes) after a ``vh`` fallback for browsers that
    # do not know it. ``none`` is passed through. ``container-type`` makes the scroll
    # box a size container, so a descendant can be as wide as its VISIBLE width (``cqw``).
    if max_height == 'none':
        height_rules = 'max-height: none; '
    else:
        height_rules = (f'max-height: calc({max_height}); '
                        f'max-height: calc({max_height.replace("vh", "dvh")}); ')
    rules: List[str] = [
        f'{mid} {{ {height_rules}overflow: auto !important; '
        f'-webkit-overflow-scrolling: touch; overscroll-behavior-x: contain; '
        f'container-type: inline-size; }}',
        f'{tbl} {{ width: max-content; min-width: 100%; }}',
        f'{tbl} > thead > tr > th {{ white-space: nowrap !important; '
        f'word-break: normal !important; overflow-wrap: normal !important; '
        f'position: sticky; top: 0; z-index: 3; background: {PINNED_HEAD_BG} !important; }}',
        f'{tbl} > tbody > tr > td {{ white-space: nowrap; '
        f'font-variant-numeric: tabular-nums; }}',
    ]
    for name, width in min_widths.items():
        tag = f'.{column_tag(prefix, name)}'
        rules.append(f'{r} th{tag}, {r} td{tag} {{ min-width: {width}px; }}')
    if pinned:
        last = pinned[-1].name
        for p in pinned:
            tag = f'.{column_tag(prefix, p.name)}'
            shadow = ('box-shadow: 1px 0 0 rgba(255,255,255,0.14), '
                      '5px 0 6px -3px rgba(0,0,0,0.55); ' if p.name == last else '')
            rules.append(
                f'{r} th{tag}, {r} td{tag} {{ position: sticky !important; '
                f'left: {p.left_px}px; width: {p.width_px}px; min-width: {p.width_px}px; '
                f'max-width: none; {shadow}}}')
            rules.append(f'{r} td{tag} {{ z-index: 2 !important; background-color: {PINNED_BODY_BG} '
                         f'!important; }}')
            rules.append(f'{r} th{tag} {{ z-index: 4 !important; background: {PINNED_HEAD_BG} '
                         f'!important; }}')
            rules.append(f'{r} tbody tr:hover td{tag} {{ background-image: '
                         f'linear-gradient({PINNED_HOVER_TINT}, {PINNED_HOVER_TINT}); }}')
    return phone_media('\n'.join('    ' + x for x in rules))


def phone_grid_columns(desktop_columns: int) -> int:
    """How many columns a ``ui.grid(columns=N)`` of metric tiles has on a phone.

    Four and three tile grids go two-up (four tiles one per row is four screens to
    read four numbers); a two-column grid is label/value text blocks and goes to one.
    Pure.
    """
    if desktop_columns >= 3:
        return 2
    return 1


def grid_phone_class(desktop_columns: int) -> str:
    """The class that gives a ``ui.grid`` its phone column count. Pure."""
    return f'lt-grid-{phone_grid_columns(desktop_columns)}'


GRID_PHONE_CSS = phone_media('''
    .nicegui-grid.lt-grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
        gap: 0.5rem !important; }
    .nicegui-grid.lt-grid-1 { grid-template-columns: minmax(0, 1fr) !important; }
    .nicegui-grid.lt-grid-2 > *, .nicegui-grid.lt-grid-1 > * { min-width: 0; }
''')


class CssOnce:
    """Add a stylesheet once per browser client (page load), however many widgets ask.

    The Stocks and Options tables each call for the same sheet when they render, and a
    refresh re-renders them; without this every call appended another copy to the page.
    Keyed on the client object (weakly), so a new page load gets its own.
    """

    def __init__(self) -> None:
        self._seen: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()

    def add(self, client, key: str, css: str, add_css: Callable[[str], None]) -> bool:
        """Call ``add_css(css)`` unless ``key`` was already added for ``client``.
        Returns whether it was added. ``add_css`` is ``ui.add_css`` (injected so the
        module stays free of NiceGUI)."""
        keys = self._seen.setdefault(client, set())
        if key in keys:
            return False
        keys.add(key)
        add_css(css)
        return True
