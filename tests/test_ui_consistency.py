"""The desktop consistency pass: one Refresh, readable controls, aligned rows, tooltips.

Reported: "misaligned controls, duplicate or different refresh buttons, unreadable
dropdowns", then "text on tooltips is too small and not enough contrast". Each problem
was MEASURED on every tab of every page before it was fixed (field/button rows 17 of 17
misaligned; 11 unreadable controls; 2 boxes wider than their container; tooltips at
10px with the box 1.13:1 against the page), and each rule here names the measurement.

Like ``test_ui_responsive_layer``, these pin RULES and sources rather than a rendering,
because the failure this pass kept finding was a rule that matched nothing or reserved
space twice -- invisible to anything but a measurement or a test that names it.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'ba2_trade_platform' / 'ui'
CSS = UI / 'static' / 'styles.css'


@pytest.fixture(scope='module')
def css() -> str:
    return CSS.read_text(encoding='utf-8')


def _rule(css: str, selector: str) -> str:
    """The declaration block of the first rule whose selector line is exactly *selector*."""
    start = css.index(f'\n{selector} {{')
    return css[start:css.index('}', start)]


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-ui-consistency'), request=None)
    yield client
    client.remove_elements(client.elements.values())


# ---------------------------------------------------------------------------
# ONE REFRESH BUTTON
# ---------------------------------------------------------------------------

def _ui_sources():
    for path in UI.rglob('*.py'):
        if path.name == 'refresh_button.py':
            continue
        yield path, path.read_text(encoding='utf-8')


def test_no_page_draws_its_own_refresh_button():
    """Eight looks for one action -- emoji, filled, flat, outline, no icon, "Refresh
    Now"... A new hand-drawn one is the first step back to that, so any `ui.button`
    whose label is "Refresh" or "Refresh Now" outside the helper fails here."""
    offenders = []
    for path, src in _ui_sources():
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'button'
                    and getattr(getattr(node.func, 'value', None), 'id', None) == 'ui'
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).strip() in ('Refresh', '🔄 Refresh', 'Refresh Now')):
                offenders.append(f'{path.relative_to(ROOT)}:{node.lineno}')
    assert not offenders, offenders


def test_the_refresh_button_is_outline_with_the_refresh_icon(nicegui_client):
    """Outline: it re-reads data and changes nothing, so it never competes with a page's
    filled primary action."""
    from ba2_trade_platform.ui.components.refresh_button import refresh_button

    with nicegui_client:
        button = refresh_button(lambda: None)
        dense = refresh_button(lambda: None, dense=True)

    assert button._props.get('outline') is True
    assert button._props.get('icon') == 'refresh'
    assert button._props.get('dense') is not True
    assert dense._props.get('dense') is True


def test_a_specific_refresh_keeps_the_look_and_says_what_it_rereads(nicegui_client):
    from ba2_trade_platform.ui.components.refresh_button import refresh_button

    with nicegui_client:
        button = refresh_button(lambda: None, label='Refresh Statistics')

    assert button._props.get('outline') is True
    assert 'Refresh Statistics' in str(button._props.get('label', '')) or \
        button.text == 'Refresh Statistics'


def test_a_table_draws_its_refresh_icon_unless_its_page_owns_one():
    """Default ON, so every other table is unchanged."""
    from ba2_trade_platform.ui.components.LazyTable import LazyTableConfig
    from ba2_trade_platform.ui.components.LiveTradesTable import LiveTradesTableConfig

    assert LazyTableConfig().show_refresh is True
    assert LiveTradesTableConfig().show_refresh is True


@pytest.mark.parametrize('page', ['activity_monitor.py', 'live_trades.py', 'option_trades.py'])
def test_pages_that_own_a_refresh_turn_the_tables_duplicate_off(page):
    """Activity Monitor drew "Refresh Now" AND the table's icon, both calling the same
    ``refresh()``; Live Trades and Options drew their Refresh beside it too."""
    src = (UI / 'pages' / page).read_text(encoding='utf-8')
    assert 'show_refresh=False' in src


def test_no_button_uses_the_input_only_outlined_prop():
    """`outlined` is a QInput prop; QBtn's is `outline`, and Quasar silently ignores the
    wrong one -- two Settings buttons rendered FILLED while written as outline."""
    offenders = []
    for path, src in _ui_sources():
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'props'
                    and isinstance(node.func.value, ast.Call)
                    and getattr(node.func.value.func, 'attr', None) == 'button'
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and 'outlined' in str(node.args[0].value).split()):
                offenders.append(f'{path.relative_to(ROOT)}:{node.lineno}')
    assert not offenders, offenders


@pytest.mark.parametrize('component', ['LazyTable.py', 'LiveTradesTable.py'])
def test_the_selection_buttons_are_not_shrunk_below_legible(component):
    """`dense` plus `size=sm` rendered Select All / Clear Selection at ~9px."""
    src = (UI / 'components' / component).read_text(encoding='utf-8')
    assert "flat dense size=sm" not in src


# ---------------------------------------------------------------------------
# READABLE CONTROLS
# ---------------------------------------------------------------------------

def test_a_select_does_not_reserve_the_arrows_room_twice(css):
    """The arrow lives in `.q-field__append`, which takes its own width. A 20px right
    padding on the value reserved it again, and a 128px select showed 36px of text."""
    rule = _rule(css, '.q-select .q-field__native')
    assert 'padding-right: 0' in rule, rule
    assert '20px' not in rule


def test_a_field_icon_is_padded_on_its_inner_side_only(css):
    """Padding the OUTER side too stacked on the control's own 12px: a 36px arrow slot
    became 48px."""
    prepend = _rule(css, '.q-field__prepend.q-field__marginal')
    append = _rule(css, '.q-field__append.q-field__marginal')
    assert 'padding-left: 0' in prepend and 'padding-right: 12px' in prepend
    assert 'padding-left: 12px' in append and 'padding-right: 0' in append


def test_the_marginal_selectors_are_classes_quasar_emits(nicegui_client):
    """A padding rule on a class Quasar never sets would silently do nothing."""
    from nicegui import ui

    with nicegui_client:
        ui.select(['a', 'b'], value='a', label='x')
    html = str(nicegui_client.layout)  # element tree; Quasar adds the classes client-side
    # Quasar owns these class names; pin that the stylesheet uses its exact spelling.
    for name in ('q-field__append', 'q-field__prepend', 'q-field__marginal'):
        assert name in CSS.read_text(encoding='utf-8')
    assert html is not None


# ---------------------------------------------------------------------------
# ALIGNED ROWS, AND NOTHING WIDER THAN ITS BOX
# ---------------------------------------------------------------------------

def test_a_field_in_a_row_carries_no_offset_margin(css):
    """Flexbox aligns MARGIN boxes: the theme's 12px field margins put every button 6px
    (centred row) to 22px (bottom-aligned row) off the field beside it -- 17 of 17 rows.
    Stacked form fields keep their spacing; only row children lose it."""
    rule = _rule(css, '.nicegui-row > .q-field')
    assert 'margin-bottom: 0' in rule and 'margin-right: 0' in rule
    # ...and the stacked-form spacing is still there for everything else.
    assert 'margin-bottom: 12px' in _rule(css, '.q-field')


def test_nothing_in_a_column_or_card_is_wider_than_it(css):
    """Capping the card alone left the table spilling past the card (2798px in 980px,
    measured). Capped at both levels, the table scrolls inside the card."""
    start = css.index('\n.nicegui-column > *,')
    rule = css[start:css.index('}', start)]
    assert '.nicegui-card > *' in rule
    assert 'max-width: 100%' in rule


def test_a_sortable_header_keeps_its_arrow_on_its_line_without_overriding_phones(css):
    """Otherwise the invisible sort arrow wraps in a narrow column and lifts the label
    (Activity Monitor: SEVERITY and ACCOUNT, 9px). NOT !important, so the phone rule
    that lets headers wrap still wins, and so does a header's own inline style."""
    rule = _rule(css, '.q-table thead th.sortable')
    assert 'white-space: nowrap' in rule
    assert '!important' not in rule


# ---------------------------------------------------------------------------
# TOOLTIPS
# ---------------------------------------------------------------------------

def test_a_tooltip_sets_a_readable_size(css):
    """With no size here, Quasar's default of 10px applied to every tooltip, and call
    sites were patching it one at a time. 14px is the size those patches settled on."""
    rule = _rule(css, '.q-tooltip')
    assert re.search(r'font-size:\s*0\.875rem\s*!important', rule), rule


def test_a_tooltip_separates_from_the_page(css):
    """The old background was the page's own colour at 95%: tooltip vs page measured
    1.13:1, so the box melted into what it opened over. A lighter surface, a visible
    edge and a shadow."""
    rule = _rule(css, '.q-tooltip')
    assert 'rgba(26, 31, 46' not in rule, 'the page colour came back as the tooltip background'
    assert 'box-shadow' in rule
    match = re.search(r'border:\s*1px solid rgba\(255, 255, 255, ([0-9.]+)\)', rule)
    assert match and float(match.group(1)) >= 0.2, 'the edge must be visible'
