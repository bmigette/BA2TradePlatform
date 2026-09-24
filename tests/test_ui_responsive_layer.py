"""The app had no responsive handling at all. This pins the layer that added it.

Reported: "many widget / page are not readable on mobile". Before this there was not a
single breakpoint class in the Python and not one media query in the stylesheet, against
16 pages, ~30 tables (two of them 15+ columns), 30 fixed multi-column grids and ~180
fixed-width utility classes. On a 390px phone every one of those overflowed.

These tests pin the RULES rather than any rendering, because a stylesheet is the one
place where a selector that matches nothing is indistinguishable from a selector that
works — the warning toast in this same file was broken for exactly that reason, asking
for black text through a class Quasar never emits. So each test names the class the
framework actually produces, verified against the installed NiceGUI/Quasar.
"""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1]
       / 'ba2_trade_platform' / 'ui' / 'static' / 'styles.css')


@pytest.fixture(scope='module')
def css() -> str:
    return CSS.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def phone_block(css: str) -> str:
    """Everything inside the <=639px query."""
    start = css.index('@media (max-width: 639px)')
    return css[start:]


def test_the_stylesheet_has_a_responsive_layer_at_all(css):
    assert '@media (max-width: 1023px)' in css
    assert '@media (max-width: 639px)' in css


def test_the_braces_balance(css):
    """A stylesheet with one stray brace silently drops every rule after it, and
    nothing reports that."""
    assert css.count('{') == css.count('}')


# ---------------------------------------------------------------------------
# The selectors must be the ones the frameworks actually emit
# ---------------------------------------------------------------------------

def test_the_grid_selector_is_the_class_nicegui_emits(css):
    """`ui.grid` carries `nicegui-grid` (Grid(default_classes='nicegui-grid'))."""
    from nicegui.elements.grid import Grid

    assert 'nicegui-grid' in Grid._default_classes
    assert '.nicegui-grid' in css


def test_the_chart_selector_is_the_class_nicegui_emits(css):
    """`ui.echart` carries `nicegui-echart`, not `echarts`."""
    from nicegui.elements.echart import EChart

    assert 'nicegui-echart' in EChart._default_classes
    assert '.nicegui-echart' in css


def test_grid_collapse_overrides_an_INLINE_style(css):
    """`ui.grid(columns=N)` writes `grid-template-columns` as an INLINE style, so the
    rule only wins with `!important` — an ordinary stylesheet declaration loses to it.
    """
    block = css[css.index('@media (max-width: 1023px)'):]
    rule = block[block.index('.nicegui-grid'):]
    rule = rule[:rule.index('}')]

    assert 'grid-template-columns' in rule
    assert '!important' in rule


# ---------------------------------------------------------------------------
# What the phone breakpoint has to do
# ---------------------------------------------------------------------------

def test_fixed_width_utilities_are_released_on_a_phone(phone_block):
    """`w-64` is 16rem — wider than a 390px phone's content column. `w-full` and the
    fractional widths must NOT be caught, which is why these are listed explicitly
    rather than matched with a wildcard."""
    for cls in ('.w-24', '.w-32', '.w-48', '.w-64', '.w-96'):
        assert cls in phone_block, cls
    assert '.w-full' not in phone_block, "w-full must not be overridden to 100%-of-what"


def test_a_wide_table_scrolls_sideways_rather_than_overflowing_the_page(phone_block):
    assert '.q-table__container' in phone_block
    assert 'overflow-x: auto' in phone_block


def test_the_first_table_column_stays_put_while_the_rest_scroll(phone_block):
    """Without it the row loses its identity as soon as it is scrolled: fifteen
    anonymous numbers. This is what keeps a wide table usable rather than merely
    reachable."""
    assert 'position: sticky' in phone_block
    assert 'td:first-child' in phone_block


def test_the_sticky_column_is_opaque(phone_block):
    """The theme's card colour is translucent; a transparent sticky cell lets the
    scrolling columns show through and the text becomes illegible exactly when it
    matters."""
    sticky = phone_block[phone_block.index('td:first-child'):]
    sticky = sticky[:sticky.index('}')]

    assert 'background:' in sticky
    assert 'rgba' not in sticky, "an opaque colour, not a translucent one"


def test_dialogs_lose_their_fixed_minimum_width(phone_block):
    """`min-w-[420px]` on a 390px screen scrolls the DIALOG sideways — the one element
    a user cannot scroll without losing the page behind it."""
    assert '.q-dialog__inner' in phone_block
    assert 'min-width: 0' in phone_block


def test_plotly_height_is_left_alone(css):
    """It sizes its own SVG inline from the figure layout; overriding the container
    underneath it CLIPS the plot instead of shrinking it."""
    assert 'js-plotly-plot' not in css


# ---------------------------------------------------------------------------
# Hiding a column needs a TAG, because Quasar gives a stylesheet nothing to aim at
# ---------------------------------------------------------------------------

def test_the_phone_layer_hides_tagged_columns(phone_block):
    assert '.mobile-hide' in phone_block
    assert 'display: none' in phone_block


def test_a_lazytable_column_can_ask_to_be_hidden():
    """`classes`/`headerClasses` is Quasar's documented per-column hook, and the only
    handle a stylesheet has: QTable adds no class of its own naming the column."""
    from ba2_trade_platform.ui.components.LazyTable import ColumnDef

    col = ColumnDef(name='account', label='Account', field='account',
                    mobile_hide=True).to_quasar_column()

    assert col['classes'] == 'mobile-hide'
    assert col['headerClasses'] == 'mobile-hide'


def test_a_lazytable_column_shows_by_default():
    """Nothing disappears without being asked for."""
    from ba2_trade_platform.ui.components.LazyTable import ColumnDef

    col = ColumnDef(name='symbol', label='Symbol', field='symbol').to_quasar_column()

    assert 'classes' not in col
    assert 'headerClasses' not in col


def test_the_performance_table_hides_only_what_it_was_told_to():
    from ba2_trade_platform.ui.components.performance_charts import PerformanceTable

    table = PerformanceTable(title='', columns=['Symbol', 'Sharpe Ratio'], rows=[],
                             mobile_hide=['Sharpe Ratio'])

    assert table.mobile_hide == {'Sharpe Ratio'}
    assert PerformanceTable(title='', columns=['Symbol'], rows=[]).mobile_hide == set()


def test_the_identity_column_is_never_hidden_on_the_priority_tables():
    """The sticky first column is what keeps a scrolled row identifiable. Hiding the
    symbol would leave a phone user scrolling anonymous numbers -- and it is the one
    column every one of these tables is read BY."""
    import pathlib
    for page in ('overview.py', 'marketanalysis.py'):
        src = (pathlib.Path(__file__).resolve().parents[1]
               / 'ba2_trade_platform' / 'ui' / 'pages' / page).read_text(encoding='utf-8')
        for line in src.splitlines():
            if 'mobile-hide' in line:
                assert "'label': 'Symbol'" not in line, f"{page}: {line.strip()}"


def test_kpi_tiles_stay_two_up_rather_than_one_per_screen(phone_block):
    """The blanket 1-column collapse is right for a chart and wrong for four small
    metric cards, which would otherwise be four screens of scrolling to read four
    numbers."""
    assert '.metric-grid' in phone_block
    block = phone_block[phone_block.index('.metric-grid'):]
    assert 'repeat(2' in block[:block.index('}')]


# ---------------------------------------------------------------------------
# Tab strips, the header, and the padding cascade (mobile pass, 2026-09-23)
#
# Reported from a phone: "the tabs mess with" the page. Measured on dev at iPhone 13
# size (390px): the layout viewport was 699px on Overview, 700px on Market Analysis and
# 868px on Settings, so each of those pages opened panned sideways with the tab strip
# and the first card both cut off. The OUTERMOST element wider than the screen was
# `.q-tabs` in every case -- the header and Quasar's notification layer are fixed and
# full-width, so they only followed the widened layout.
# ---------------------------------------------------------------------------

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw. No browser."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-responsive-layer'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _block(css: str, selector: str, *, before: int = None) -> str:
    """The declaration block of the first rule whose selector line is exactly *selector*.

    ``before`` restricts the search to ``css[:before]`` -- how a test says "outside any
    media query".
    """
    haystack = css if before is None else css[:before]
    start = haystack.index(f'\n{selector} {{')
    return haystack[start:haystack.index('}', start)]


def _phone_block_rule(phone_block: str, selector: str) -> str:
    start = phone_block.index(f'{selector} {{')
    return phone_block[start:phone_block.index('}', start)]


def _render_layout(nicegui_client):
    from nicegui import ui
    from ba2_trade_platform.ui import layout
    with nicegui_client:
        with layout.layout_render('Test'):
            ui.label('body')
    return list(nicegui_client.layout.descendants(include_self=True))


def test_a_tab_strip_is_held_to_its_container_at_EVERY_width(css):
    """QTabs decides whether to scroll by comparing its content with its own width, and
    in a flex column whose children default to `min-width: auto` the strip grew to fit
    its tabs -- so QTabs measured a container exactly as wide as its content, declared
    itself `q-tabs--not-scrollable`, and widened the page instead.

    OUTSIDE any media query on purpose: a narrow desktop window has the same problem as a
    phone, and this rule changes nothing where the tabs already fit."""
    rule = _block(css, '.q-tabs', before=css.index('@media'))

    assert 'max-width: 100%' in rule, rule
    assert 'min-width: 0' in rule, rule


def test_every_tab_strip_gets_scroll_arrows_on_a_phone():
    """Quasar hides a scrolling strip's arrows on touch devices unless asked, and a strip
    that scrolls with no arrows just looks clipped mid-word. Set once, as the class
    default, so no call site can forget it -- and OUTSIDE the strip, because the theme
    makes `.q-tabs` transparent and inside arrows were drawn over the last label."""
    from nicegui import ui
    import ba2_trade_platform.ui.layout  # noqa: F401 - importing it sets the default

    assert ui.tabs._default_props.get('mobile-arrows') is True
    assert ui.tabs._default_props.get('outside-arrows') is True


def test_a_tab_strip_actually_carries_the_arrow_props(nicegui_client):
    """The default is only worth pinning if a real ``ui.tabs`` picks it up."""
    from nicegui import ui
    import ba2_trade_platform.ui.layout  # noqa: F401

    with nicegui_client:
        tabs = ui.tabs()

    assert tabs._props.get('mobile-arrows') is True
    assert tabs._props.get('outside-arrows') is True


def test_the_github_link_hides_on_a_phone_through_the_stylesheet(nicegui_client, phone_block):
    """It was the item that pushed the account selector onto a THIRD header row, so the
    header took a quarter of a 390px screen before any content. Hidden through a class
    the stylesheet owns, so the header's phone layout is decided in one place."""
    links = [el for el in _render_layout(nicegui_client)
             if 'github.com' in str(el._props.get('href', ''))]

    assert links, 'the GitHub link is no longer rendered where this test looks for it'
    assert all('phone-hidden' in el._classes for el in links)
    assert 'display: none' in _phone_block_rule(phone_block, '.phone-hidden')


def test_the_header_margin_rule_targets_a_class_the_header_actually_carries(
        nicegui_client, phone_block):
    """In the spirit of this file: a stylesheet selector that matches nothing is
    indistinguishable from one that works. `.q-header .mr-4` is only worth having if the
    balance and the account selector still carry `mr-4`."""
    from nicegui import ui
    elements = _render_layout(nicegui_client)
    header = next(el for el in elements if isinstance(el, ui.header))
    carriers = [el for el in header.descendants() if 'mr-4' in el._classes]

    assert len(carriers) >= 2, 'the balance and the account selector each carried mr-4'
    assert 'margin-right: 0' in _phone_block_rule(phone_block, '.q-header .mr-4')


def test_the_page_column_carries_the_stylesheets_handle(nicegui_client, phone_block):
    """The column's `p-6` is right on a desktop; the stylesheet needs a handle to take it
    back on a phone, and a Tailwind utility is not one -- `.p-6` also sits on cards."""
    pages = [el for el in _render_layout(nicegui_client) if 'ba2-page' in el._classes]

    assert len(pages) == 1, [el._classes for el in pages]
    assert 'padding' in _phone_block_rule(phone_block, '.ba2-page')


def test_the_padding_cascade_is_trimmed_on_a_phone(phone_block):
    """Page column (24px) + tab panel (16px) + card (12px) + card inside the card (12px)
    left Live Trades' filter fields about 250px wide on a 390px screen. The two outer
    layers have no visible edge, so their padding was pure loss; a nested card keeps half
    of the outer card's, so the two edges stay distinguishable."""
    assert 'padding' in _phone_block_rule(phone_block, '.q-tab-panel')
    nested = phone_block[phone_block.index('.q-card .q-card'):]
    assert 'padding: 0.5rem' in nested[:nested.index('}')]


def test_the_phone_only_rules_do_not_reach_a_desktop(css):
    """The one global change in this pass is the tab constraint above. Everything else is
    a phone decision, and each of these selectors appearing before the phone query would
    restyle every desktop page."""
    desktop = css[:css.index('@media (max-width: 639px)')]

    for selector in ('.phone-hidden', '.ba2-page', '.q-header .mr-4', '.q-card .q-card'):
        assert selector not in desktop, f'{selector} leaks outside the phone query'
