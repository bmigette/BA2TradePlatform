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
        args = {'phone': True}

    seen = []
    registry.set_phone = lambda phone: seen.append(phone)
    rsp.install_phone_listener(registry, heads.append,
                               lambda name, fn: handlers.__setitem__(name, fn))
    assert rsp.PHONE_EVENT in heads[0]
    handlers[rsp.PHONE_EVENT](_E())
    assert seen == [True]


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
