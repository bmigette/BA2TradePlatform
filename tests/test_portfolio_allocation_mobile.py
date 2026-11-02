"""Phone layout of the Portfolio Allocation page and its dry-run dialog.

The layout decisions are DATA (``CardColumn`` specs) and pure functions, so most of
this needs no browser: it pins that every column has a phone decision, that the
generated CSS / Vue card names every one of them, that the data model is shared (the
card reads the same row dicts and emits the same events as the desktop cells), and
that the desktop DOM -- ids, markers, classes the other tests lean on -- is intact.
What it cannot pin is how it LOOKS; that was checked in a browser at 390px (see the
commit message).
"""
import re
from pathlib import Path

import pytest

from ba2_trade_platform.core.portfolio_allocation import (
    AllocationPlan, AllocationRow, BaseSnapshot, VALUATION_MODE_MARKET,
)
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.pages import portfolio_allocation_wizard as wiz
from ba2_trade_platform.ui.utils import responsive as rsp
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    MARKET_GATE_OPEN, ManagedLabel, MarketGateResult, build_label_views,
    positions_by_symbol,
)

STYLES = Path(rsp.__file__).resolve().parents[1] / 'static' / 'styles.css'


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-pf-mobile'), request=None)
    yield client
    client.remove_elements(client.elements.values())


# -- the breakpoint ----------------------------------------------------------

def test_the_breakpoint_is_the_stylesheets_phone_breakpoint():
    css = STYLES.read_text(encoding='utf-8')
    assert f'@media (max-width: {rsp.PHONE_MAX_WIDTH_PX}px)' in css


def test_the_grid_expression_uses_the_same_number():
    assert str(rsp.PHONE_MAX_WIDTH_PX) in rsp.phone_grid_expression()
    assert rsp.phone_grid_prop() == f':grid="{rsp.phone_grid_expression()}"'
    assert rsp.phone_grid_prop(True) == ':grid="true"'
    assert rsp.phone_grid_prop(False) == ':grid="false"'


# -- the spec check ----------------------------------------------------------

def _spec(*pairs):
    return tuple(rsp.CardColumn(n, n.title(), t) for n, t in pairs)


def test_check_card_columns_names_every_problem():
    spec = _spec(('a', rsp.TIER_HEAD), ('a', rsp.TIER_PRIMARY), ('x', 'bogus'))
    with pytest.raises(ValueError) as err:
        rsp.check_card_columns(spec, ['a', 'b'])
    message = str(err.value)
    assert "['b']" in message          # no phone decision
    assert "['x']" in message          # unknown column / bad tier
    assert "['a']" in message          # declared twice


def test_a_caption_that_would_break_the_css_string_is_refused():
    bad = (rsp.CardColumn('a', 'say "hi"', rsp.TIER_PRIMARY),)
    with pytest.raises(ValueError):
        rsp.check_card_columns(bad, ['a'])


def test_every_dry_run_column_has_a_phone_decision():
    rsp.check_card_columns(wiz.DRY_RUN_CARD, [c[0] for c in wiz.DRY_RUN_COLUMNS])


def test_every_symbol_table_column_has_a_phone_decision():
    rsp.check_card_columns(page.SYMBOL_CARD,
                           [c['name'] for c in page.symbol_table_columns()])


def test_the_essentials_are_visible_without_a_tap():
    tiers = rsp.columns_by_tier(wiz.DRY_RUN_CARD)
    for essential in ('symbol', 'side', 'qty', 'estimated_value', 'reasons'):
        assert essential in tiers[rsp.TIER_HEAD] + tiers[rsp.TIER_PRIMARY] \
            + tiers[rsp.TIER_WIDE], essential
    sym = rsp.columns_by_tier(page.SYMBOL_CARD)
    for essential in ('symbol', 'weight_pct', 'current_value', 'target_value', 'pnl'):
        assert essential in sym[rsp.TIER_HEAD] + sym[rsp.TIER_PRIMARY] \
            + sym[rsp.TIER_WIDE], essential


# -- generated CSS -----------------------------------------------------------

def test_card_css_addresses_every_cell_and_is_phone_only():
    css = rsp.card_rows_css(wiz.DRY_RUN_ROW_KEY, wiz.DRY_RUN_CELL_PREFIX,
                            wiz.DRY_RUN_CARD)
    assert css.startswith('@media (max-width: 639px)')
    for column in wiz.DRY_RUN_CARD:
        assert f'.{wiz.DRY_RUN_CELL_PREFIX}{column.name}' in css, column.name
    # details are folded until the card is opened, then shown
    assert re.search(r'\.pf-c-cost \{[^}]*display: none !important', css)
    assert '.pf-open > .pf-c-cost' in css
    # a primary cell is captioned
    assert 'content: "Order value"' in css


def test_a_table_with_no_details_gets_no_details_toggle():
    css = rsp.card_rows_css(wiz.NOT_TRADED_ROW_KEY, wiz.NOT_TRADED_CELL_PREFIX,
                            wiz.NOT_TRADED_CARD)
    assert '::after' not in css


def test_page_phone_css_is_one_string_covering_every_widget():
    css = page.page_phone_css()
    for needle in ('.pf-bar-row', '.pf-sym-card', '.pf-dry-row', '.pf-nt-row',
                   '.pf-wrap-row', '.pf-swatch'):
        assert needle in css, needle


# -- the symbol table: ONE data model, two layouts ---------------------------

def _views():
    book = [{'symbol': s, 'qty': 10, 'cost_basis': 1000.0, 'market_value': 1200.0,
             'side': 'BUY'} for s in ('AAPL', 'MSFT')]
    return build_label_views(
        [ManagedLabel('ARK26', 100.0)], {'ARK26': ['AAPL', 'MSFT']},
        positions_by_symbol(book), {'AAPL': 120.0, 'MSFT': 120.0},
        valuation_mode=VALUATION_MODE_MARKET, base_notional=10_000.0,
        symbol_weights={'ARK26': {'AAPL': 60.0, 'MSFT': 40.0}})


async def _noop():
    return None


def _table(nicegui_client):
    from nicegui import ui
    with nicegui_client:
        page._render_labels(1, {'views': _views(), 'symbols_by_label': {},
                                'valuation_mode': VALUATION_MODE_MARKET,
                                'base_notional': 10_000.0,
                                'available_buying_power': 1_000.0,
                                'account_value': None, 'unallocated_pct': 0.0}, _noop)
    return next(el for el in nicegui_client.layout.descendants()
                if isinstance(el, ui.table))


def test_the_symbol_table_switches_to_cards_on_a_phone(nicegui_client):
    table = _table(nicegui_client)
    assert table._props[':grid'] == rsp.phone_grid_expression()
    assert 'item' in table.slots
    # the desktop cells are all still there: same table, one more slot
    for slot in ('body-cell-weight_pct', 'body-cell-comment', 'body-cell-info',
                 'body-cell-pnl', 'body-cell-flag'):
        assert slot in table.slots, slot
    # same rows feed both layouts
    assert table.rows[0]['symbol'] in ('AAPL', 'MSFT')


def test_the_card_names_every_non_header_column_and_shares_the_write_paths():
    template = page.symbol_card_template()
    for column in page.SYMBOL_CARD:
        if column.tier in (rsp.TIER_PRIMARY, rsp.TIER_DETAIL):
            assert f'props.row.{column.name}' in template, column.name
    # selection (Compare / Remove read the table's selection)
    assert 'v-model="props.selected"' in template
    # the very same events the desktop cells emit
    assert "$emit('weightChange', props.row.symbol, val)" in template
    assert "$emit('commentChange', props.row.symbol, val)" in template
    assert "$emit('symbolInfo', props.row.symbol)" in template
    # a refused weight edit still puts the typed text back
    assert ':key="props.row.weight_key"' in template
    # numeric keypad for the weight
    assert 'inputmode="decimal"' in template
    assert f'debounce="{page.TARGET_DEBOUNCE_MS}"' in template
    assert f'debounce="{page.COMMENT_DEBOUNCE_MS}"' in template
    # the live change under target value / target qty
    assert 'props.row.value_delta' in template and 'props.row.qty_delta' in template
    # the detail columns sit behind the fold, and only they
    fold = template[template.index('<q-expansion-item'):]
    assert 'props.row.cost_basis' in fold and 'props.row.current_value' not in fold


def test_the_registry_repins_live_tables_and_forgets_dead_ones(nicegui_client):
    table = _table(nicegui_client)
    registry = rsp.PhoneTableRegistry()
    registry.register(table)
    registry.set_phone(False)
    assert table._props[':grid'] == 'false'
    registry.set_phone(True)
    assert table._props[':grid'] == 'true'
    assert len(registry) == 1


def test_the_breakpoint_event_reaches_the_registry():
    registry = rsp.PhoneTableRegistry()
    heads, handlers = [], {}

    class _E:
        args = {'phone': True, 'width': 390}

    seen = []
    registry.set_phone = lambda phone: seen.append(phone)
    rsp.install_phone_listener(registry, heads.append,
                               lambda name, fn: handlers.__setitem__(name, fn))
    assert rsp.PHONE_EVENT in heads[0]
    handlers[rsp.PHONE_EVENT](_E())
    assert seen == [True]


def test_the_listener_script_is_well_formed_and_sends_the_phone_flag():
    script = rsp.PHONE_LISTENER_HEAD_HTML
    assert script.count('{') == script.count('}')
    assert script.count('(') == script.count(')')
    assert 'phone:e.matches' in script
    assert f'max-width: {rsp.PHONE_MAX_WIDTH_PX}px' in script


@pytest.mark.parametrize('width,phone', [(320, True), (390, True), (639, True),
                                         (640, False), (844, False), (1280, False)])
def test_which_widths_are_phones(width, phone):
    assert rsp.is_phone_width(width) is phone


def test_rotating_a_phone_crosses_the_breakpoint_both_ways(nicegui_client):
    """Portrait 390 -> landscape 844 -> portrait 390: cards, table, cards. A resize on
    the same side of the line changes nothing."""
    table = _table(nicegui_client)
    registry = rsp.PhoneTableRegistry()
    registry.register(table)

    assert registry.on_width(390) is True and table._props[':grid'] == 'true'
    assert registry.on_width(400) is False and table._props[':grid'] == 'true'
    assert registry.on_width(844) is True and table._props[':grid'] == 'false'
    assert registry.on_width(900) is False and table._props[':grid'] == 'false'
    assert registry.on_width(390) is True and table._props[':grid'] == 'true'


def test_a_table_built_after_the_crossing_starts_in_the_current_layout(nicegui_client):
    """A refresh rebuilds every table; the new ones must not come up in the layout the
    browser has left."""
    registry = rsp.PhoneTableRegistry()
    registry.on_width(844)
    table = _table(nicegui_client)
    registry.register(table)
    assert table._props[':grid'] == 'false'


def test_the_phone_registry_entry_is_dropped_when_the_client_is_deleted(
        nicegui_client):
    """``client.on_delete`` hands the handler the CLIENT. The first version was
    ``lambda cid=client.id: pop(cid)``, so ``cid`` became the Client and nothing was
    ever removed: one leaked registry per page load."""
    with nicegui_client:
        registry = page._phone_tables()
    assert page._PHONE_REGISTRIES[nicegui_client.id] is registry
    for handler in nicegui_client.delete_handlers:
        nicegui_client.safe_invoke(handler)
    assert nicegui_client.id not in page._PHONE_REGISTRIES


# -- the dry run: same elements, tagged ---------------------------------------

def _plan():
    return AllocationPlan(
        rows=[AllocationRow(symbol='AAPL', price=160.0, delta_quantity=10.0,
                            side=OrderDirection.BUY, estimated_value=1600.0,
                            bp_cost=1600.0, bp_factor=1.0)],
        base_notional=10_000.0, available_buying_power=10_000.0,
        required_buying_power=1600.0, bp_usage_pct=16.0, total_buy_value=1600.0)


def test_dry_run_rows_are_cards_with_addressable_cells_and_a_hidden_header(
        nicegui_client):
    base = BaseSnapshot(available_buying_power=10_000.0, managed_value=0.0,
                        base_notional=10_000.0, default_bp_factor=1.0,
                        valuation_mode=VALUATION_MODE_MARKET, cash=10_000.0)
    gate = MarketGateResult(allowed=True, reason_code=MARKET_GATE_OPEN, message='')
    with nicegui_client:
        wiz.AllocationWizard(base, _plan(), market=gate,
                             on_refresh=lambda f: (_plan(), gate),
                             on_submit=lambda p: None).open()
    elements = list(nicegui_client.layout.descendants())

    def classes(el):
        return ' '.join(getattr(el, '_classes', []))

    rows = [e for e in elements if 'pf-grid-row' in classes(e)]
    assert rows and all('pf-card-row' in classes(e) and 'pf-dry-row' in classes(e)
                        for e in rows)
    head = next(e for e in elements if wiz.MARKER_TABLE_HEAD in getattr(e, '_markers', []))
    assert 'pf-card-head' in classes(head)
    foot = next(e for e in elements if wiz.MARKER_TABLE_FOOT in getattr(e, '_markers', []))
    assert 'pf-card-row' in classes(foot)
    # every column has a tagged cell in the data row
    row_classes = ' '.join(classes(e) for e in rows[0].descendants())
    for name, *_ in wiz.DRY_RUN_COLUMNS:
        assert f'pf-c-{name}' in row_classes, name
    # the dialog's primary action is first on a phone
    submit = next(e for e in elements if getattr(e, '_props', {}).get('label') == 'Submit')
    assert rsp.PRIMARY_ACTION_CLASS in classes(submit)


def test_the_rotation_listener_dedupes_on_the_media_query_not_the_width():
    """A pinch-zoomed iOS page reports the ZOOMED innerWidth; the media query is the
    layout's own verdict."""
    script = rsp.PHONE_LISTENER_HEAD_HTML
    assert 'e.matches===last' in script
    assert 'innerWidth' not in script and ',width' not in script


def test_the_breakpoint_handler_follows_the_phone_flag(nicegui_client):
    table = _table(nicegui_client)
    registry = rsp.PhoneTableRegistry()
    registry.register(table)
    heads, handlers = [], {}
    rsp.install_phone_listener(registry, heads.append,
                               lambda name, fn: handlers.__setitem__(name, fn))

    class _E:
        def __init__(self, phone):
            self.args = {'phone': phone}

    handlers[rsp.PHONE_EVENT](_E(True))
    assert table._props[':grid'] == 'true'
    handlers[rsp.PHONE_EVENT](_E(False))
    assert table._props[':grid'] == 'false'
    assert registry.on_phone(False) is False          # no change, nothing re-pinned


# ---------------------------------------------------------------------------
# The redesigned phone cards: tiles, boundaries, one value size, delta INSIDE its tile
# (operator's iPhone screenshot, 2026-10-03: no boundary between cards, caption run into
# its value, deltas floating alone, ragged column)
# ---------------------------------------------------------------------------

def test_the_page_stylesheet_is_a_served_file_equal_to_the_generated_css():
    """On the operator's phone the page's rules did not arrive through ``ui.add_css``
    (every ``pf-*`` rule was missing) while the linked ``styles.css`` did. The sheet is
    now ALSO a file linked from the head; this pins the file to the generator and the
    link to the file's content."""
    css_file = page.page_css_path()
    assert css_file.read_text(encoding='utf-8') == page.page_phone_css(), (
        'static/portfolio_allocation.css is stale: run '
        'write_page_css() from ui/pages/portfolio_allocation.py')
    link = page.page_css_link_html()
    assert link.startswith('<link rel="stylesheet" href="/static/portfolio_allocation.css?v=')
    assert page.page_css_link_html() == link          # stable for unchanged content


def test_a_tile_is_a_caption_line_above_a_value_line():
    html = page.card_tile_html('Current value', '{{ props.row.current_value }}',
                               field='current_value')
    assert html.index('class="pf-tile-k"') < html.index('class="pf-tile-v"')
    assert 'Current value</div>' in html and 'data-field="current_value"' in html
    assert 'pf-tile--wide' not in html
    assert 'pf-tile--wide' in page.card_tile_html('P&L', 'x', wide=True)


def _tiles(template):
    import re
    return re.findall(r'<div class="pf-tile[^"]*" data-field="(\w+)">', template)


def test_every_figure_is_a_tile_in_a_regular_grid():
    template = page.symbol_card_template()
    tiles = _tiles(template)
    by_tier = rsp.columns_by_tier(page.SYMBOL_CARD)
    # every primary and detail column is exactly one tile, plus the wide P&L tile
    assert sorted(tiles) == sorted(by_tier[rsp.TIER_PRIMARY] + by_tier[rsp.TIER_DETAIL]
                                   + ['pnl'])
    # an even number of half-width tiles in the grid and in the fold: no ragged last row
    assert len(by_tier[rsp.TIER_PRIMARY]) % 2 == 0
    assert len(by_tier[rsp.TIER_DETAIL]) % 2 == 0
    # no caption-beside-value rows left over from the first design
    assert 'pf-kv' not in template and 'pf-k"' not in template


def test_a_change_sits_inside_the_tile_it_belongs_to():
    template = page.symbol_card_template()
    import re
    for field, delta in (('target_value', 'value_delta'), ('target_quantity', 'qty_delta')):
        tile = re.search(r'<div class="pf-tile" data-field="%s">.*?(?=<div class="pf-tile"'
                         r'|</div></div><div class="pf-tile|</div><div class="pf-field|$)'
                         % field, template, re.S).group(0)
        assert f'props.row.{delta}' in tile, field
        assert 'to target' in tile and '▼' in tile and '▲' in tile
    # and nowhere else: the delta appears only within those two tiles
    assert template.count('props.row.value_delta') == template.split(
        'data-field="target_quantity"')[0].count('props.row.value_delta')


def test_the_pnl_tile_is_full_width_with_the_sentence_on_a_second_line():
    template = page.symbol_card_template()
    tile = template[template.index('data-field="pnl"'):]
    assert 'pf-tile--wide' in template[template.index('data-field="pnl"') - 60:
                                       template.index('data-field="pnl"')]
    assert tile.index('pf-tile-v') < tile.index('pf-tile-sub pf-pnl-rest')
    assert "indexOf(' (')" in tile                       # split on the first " ("
    assert 'props.row.pnl_div' in tile and 'props.row.pnl_tail' in tile


def test_colours_are_inline_tones_not_the_dark_quasar_classes():
    """Quasar's text-negative is #C10015 -- close to unreadable on the dark card -- and
    the global ``span {color:#fff}`` beats an inherited class colour."""
    template = page.symbol_card_template()
    assert 'text-negative' not in template and 'text-positive' not in template
    assert "#f87171" in template and "#4ade80" in template
    assert page.card_tone_style('value_delta_color').startswith('{ color: ({ positive')


def test_the_two_inputs_are_outlined_fields_in_their_own_blocks_and_keep_their_wiring():
    template = page.symbol_card_template()
    assert template.count('class="pf-field"') == 2
    assert 'dense outlined' in template
    assert 'inputmode="decimal"' in template and 'label="Share of label %"' in template
    assert 'label="Comment"' in template


def test_the_header_row_holds_the_checkbox_the_symbol_and_the_chips():
    template = page.symbol_card_template()
    head = template[template.index('class="pf-sym-head"'):template.index('class="pf-field"')]
    assert head.index('q-checkbox') < head.index('pf-sym-name') < head.index('symbolInfo')
    assert 'lev_badge' in head and 'frac_badge' in head


def test_the_card_markup_is_balanced_so_the_vue_compile_cannot_blow_up():
    """A malformed slot template blanks the whole page (it did once, with a doubled
    quote); the tag structure is checked here, the expressions in the browser."""
    from html.parser import HTMLParser

    class _Counter(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = {}

        def handle_starttag(self, tag, attrs):
            if tag in ('div', 'span', 'q-expansion-item', 'q-input'):
                self.depth[tag] = self.depth.get(tag, 0) + 1
            for _name, value in attrs:
                assert value is None or '"' not in value, (tag, value)

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if tag in self.depth:
                self.depth[tag] -= 1

        def handle_endtag(self, tag):
            if tag in self.depth:
                self.depth[tag] -= 1

    counter = _Counter()
    counter.feed(page.symbol_card_template())
    assert all(v == 0 for v in counter.depth.values()), counter.depth


def test_the_card_css_gives_each_card_a_boundary_and_one_size_per_role():
    css = page.SYMBOL_CARD_CSS
    card = css[css.index('.pf-sym-card {'):css.index('}', css.index('.pf-sym-card {'))]
    assert 'border: 1px solid' in card and 'border-radius: 12px' in card
    assert 'margin: 0 0 12px' in card and 'background:' in card
    value = css[css.index('.pf-tile-v {'):css.index('}', css.index('.pf-tile-v {'))]
    assert 'font-size: 16px' in value and 'tabular-nums' in value
    caption = css[css.index('.pf-tile-k {'):css.index('}', css.index('.pf-tile-k {'))]
    assert 'font-size: 11px' in caption and 'uppercase' in caption
    assert 'repeat(2, minmax(0, 1fr))' in css          # a strict two-column grid


def test_hand_rolled_cards_get_the_same_structure_from_the_generator():
    css = rsp.card_rows_css(wiz.DRY_RUN_ROW_KEY, wiz.DRY_RUN_CELL_PREFIX, wiz.DRY_RUN_CARD)
    card = css[css.index('.pf-card-row.pf-dry-row {'):]
    card = card[:card.index('}')]
    assert 'border: 1px solid' in card and 'border-radius: 12px' in card
    assert 'margin: 0 0 12px' in card and 'box-sizing: border-box' in card
    # a tile: caption above (own grid line), padded, equal minimum height
    assert 'min-height: 4rem' in css and 'grid-column: 1 / -1; color: #94a3b8' in css
    assert 'font-size: 11px' in css and 'text-transform: uppercase' in css
    # the head line has a divider; Details is a full-width 44px button row
    assert 'border-bottom: 1px solid' in css
    assert 'line-height: 44px' in css and 'content: "Details' in css
    # one value size
    assert 'font-size: 16px !important' in rsp.card_rows_common_css()
    # no left indent for the tick: tiles use the card's full width
    assert 'padding: 10px 8px 10px 8px' in card


def test_the_label_header_is_a_card_too():
    assert '.q-expansion-item:has(.pf-bar-row)' in page.LABEL_BAR_PHONE_CSS
    assert 'border-radius: 12px' in page.LABEL_BAR_PHONE_CSS


# -- label header: name on its own line, one card (operator, 2026-10-03) -----------

def _bar_rule(selector_fragment):
    css = page.LABEL_BAR_PHONE_CSS
    return next(m.group(0) for m in re.finditer(r'[^{}]*\{[^}]*\}', css)
                if selector_fragment in m.group(0))


def test_the_label_name_has_its_own_full_width_wrapping_line():
    rule = _bar_rule('> .pf-b-name {')
    assert 'order: 1' in rule and 'flex: 1 1 100%' in rule
    assert 'white-space: normal' in rule and 'text-overflow: clip' in rule
    assert 'font-weight: 700' in rule


def test_the_identity_controls_sit_on_the_second_line_after_the_name():
    css = page.LABEL_BAR_PHONE_CSS
    orders = {cls: int(re.search(rf'\.pf-bar-row > \.pf-b-{cls} \{{[^}}]*order: (\d+)', css).group(1))
              for cls in ('icon', 'count', 'edit', 'info')}
    assert 1 < orders['icon'] <= orders['count'] < orders['edit'] < orders['info'] < 9
    # 40px tap targets: 24px glyph + 8px padding each side
    assert 'padding: 8px; box-sizing: content-box; font-size: 24px' in css


def test_the_label_is_one_card_not_a_box_in_a_box():
    css = page.LABEL_BAR_PHONE_CSS
    assert '.q-expansion-item:has(.pf-bar-row) > .q-expansion-item__container' in css
    assert '.q-expansion-item:has(.pf-bar-row) .q-item { background: transparent' in css
    assert 'background: #232a3d !important' in css      # the ONE card's own fill
