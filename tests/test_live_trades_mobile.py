"""Phone layout of the Live Trades tables: pure decisions + generated CSS + the DOM hooks.

How it LOOKS was checked in a browser at 390px (see the commit message); this pins the
rules that decide it: which columns are pinned, that every sized/pinned column exists,
that the columns carry the tag the CSS addresses, that the CSS is phone-only and
paints the pinned cells opaque, and that no existing marker or handler moved.
"""
import re

import pytest

import importlib
ltt = importlib.import_module('ba2_trade_platform.ui.components.LiveTradesTable')
LiveTradesTable = ltt.LiveTradesTable
from ba2_trade_platform.ui.pages import live_trades as page
from ba2_trade_platform.ui.utils import responsive as rsp


def _names(columns):
    return [c.name for c in columns]


ALL_COLUMNS = sorted(set(_names(LiveTradesTable.TRANSACTION_COLUMNS))
                     | set(_names(LiveTradesTable.OPTION_TRANSACTION_COLUMNS)))


def test_pin_spec_is_valid_for_both_column_sets():
    # the min-width map serves both tables, so its names are checked against the union
    rsp.check_pinned_columns(ltt.LIVE_TRADES_PINNED, ALL_COLUMNS, ltt.LIVE_TRADES_MIN_WIDTHS)
    for cols in (LiveTradesTable.TRANSACTION_COLUMNS,
                 LiveTradesTable.OPTION_TRANSACTION_COLUMNS):
        rsp.check_pinned_columns(ltt.LIVE_TRADES_PINNED, _names(cols), {})


def test_symbol_is_pinned_with_the_checkbox():
    pinned = [p.name for p in ltt.LIVE_TRADES_PINNED]
    assert pinned == ['select', 'symbol']


def test_every_data_column_has_a_min_width():
    sized = set(ltt.LIVE_TRADES_MIN_WIDTHS) | {p.name for p in ltt.LIVE_TRADES_PINNED}
    assert not set(ALL_COLUMNS) - sized


def test_pnl_and_value_columns_fit_their_text():
    w = ltt.LIVE_TRADES_MIN_WIDTHS
    assert w['pnl'] >= 160 and w['value'] >= 150
    assert w['close_price'] >= 100  # "CLOSE PRICE" on one line
    assert w['actions'] >= 4 * 40


def test_check_pinned_rejects_gaps_and_unknown_columns():
    with pytest.raises(ValueError) as err:
        rsp.check_pinned_columns(
            (rsp.PinnedColumn('a', 0, 40), rsp.PinnedColumn('b', 50, 40)), ['a'], {'zz': 1})
    msg = str(err.value)
    assert "'b'" in msg and "'zz'" in msg and 'b: left 50px, expected 40px' in msg


def test_pinned_offsets_are_cumulative():
    assert rsp.pinned_offsets(ltt.LIVE_TRADES_PINNED) == {'select': 0, 'symbol': 60}


def test_columns_carry_the_tag_and_keep_existing_classes():
    quasar = [{'name': 'a', 'classes': 'mobile-hide'}, {'name': 'Fill prem.'}]
    out = rsp.tag_quasar_columns(quasar, 'lt-c-')
    assert out[0]['classes'] == 'mobile-hide lt-c-a'
    assert out[0]['headerClasses'] == 'lt-c-a'
    assert out[1]['classes'] == 'lt-c-Fill-prem'      # a valid class name
    assert 'classes' not in quasar[1]                  # input untouched


def _top_level_preludes(css):
    """The text before every top-level ``{`` of a stylesheet (depth-0 blocks)."""
    depth, buf, out = 0, '', []
    for ch in css:
        if depth == 0 and ch == '{':
            out.append(buf.strip())
            buf = ''
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        elif depth == 0:
            buf += ch
    assert depth == 0, 'unbalanced braces'
    return out


def test_every_generated_sheet_is_phone_only():
    sheets = [ltt.live_trades_phone_css(), page.live_trades_page_phone_css(),
              rsp.GRID_PHONE_CSS]
    for css in sheets:
        preludes = _top_level_preludes(css)
        assert preludes, 'empty sheet'
        assert set(preludes) == {'@media (max-width: 639px)'}, set(preludes)


def test_pinned_cells_are_opaque_sticky_and_take_the_hover_tint():
    css = ltt.live_trades_phone_css()
    assert '.live-trades-table td.lt-c-symbol' in css
    assert 'position: sticky !important; left: 60px' in css
    assert f'background-color: {rsp.PINNED_BODY_BG} !important' in css
    assert 'tbody tr:hover td.lt-c-symbol' in css
    assert 'th.lt-c-select' in css
    assert 'white-space: nowrap !important' in css       # headers on one line


def test_expanded_row_does_not_scroll_away():
    tpl = LiveTradesTable.BODY_TEMPLATE
    assert 'lt-expand-td' in tpl and 'lt-expand-inner' in tpl
    assert 'Related Orders' in tpl


def test_every_desktop_handler_survives_in_the_template():
    tpl = LiveTradesTable.BODY_TEMPLATE
    for event in ('toggle_selection', 'view_transaction_details', 'recreate_tpsl',
                  'edit_transaction', 'close_transaction', 'retry_close',
                  'view_recommendation'):
        assert event in tpl, event


@pytest.mark.parametrize('desktop,phone', [(4, 2), (3, 2), (2, 1), (1, 1)])
def test_grid_columns_on_a_phone(desktop, phone):
    assert rsp.phone_grid_columns(desktop) == phone
    assert rsp.grid_phone_class(desktop) == f'lt-grid-{phone}'


def test_page_css_covers_grids_dialogs_and_plain_tables():
    css = page.live_trades_page_phone_css()
    for needle in ('.lt-page-card', '.lt-dialog-actions', '.lt-grid-2', '.lt-legs-table',
                   '.lt-chain-table', '.pf-primary-action'):
        assert needle in css, needle
    assert css.count('{') == css.count('}')


def test_plain_table_specs_name_real_columns():
    rsp.check_pinned_columns(page.OPTION_LEGS_PINNED, page.OPTION_LEGS_COLUMNS,
                             page.OPTION_LEGS_MIN_WIDTHS)
    rsp.check_pinned_columns(page.OPTION_CHAIN_PINNED, page.OPTION_CHAIN_COLUMNS,
                             page.OPTION_CHAIN_MIN_WIDTHS)


def test_the_page_uses_the_phone_grid_helper_for_every_grid():
    src = open(page.__file__, encoding='utf-8').read()
    assert len(re.findall(r'ui\.grid\(columns=\d\)', src)) == \
        len(re.findall(r'ui\.grid\(columns=(\d)\)\.classes\(grid_phone_class\(\1\)', src))


# -- the template is the base's, plus two layout classes ---------------------

#: Captured from the allocator-branch base (1a3ca317): every event the table emits, with
#: the argument it passes, the conditions that show each control, and the handlers.
BASE_EMITS = [
    "'toggle_selection', props.row.id", "'view_transaction_details', props.row.id",
    "'recreate_tpsl', props.row.id", "'edit_transaction', props.row.id",
    "'close_transaction', props.row.id", "'retry_close', props.row.id",
    "'view_recommendation', order.expert_recommendation_id",
]
BASE_CONDITIONS = [
    "col.name === 'select'", "col.name === 'expand'", "col.name === 'direction'",
    "col.name === 'status'", "col.name === 'current_price'", "col.name === 'current_pnl'",
    'props.row.pnl_reason', "col.name === 'pnl'", 'props.row.pnl_reason',
    "col.name === 'closed_pnl'", "col.name === 'actions'",
    'props.row.has_missing_tpsl_orders', 'props.row.is_open || props.row.is_waiting',
    '(props.row.is_open || props.row.is_waiting) && !props.row.is_closing',
    'props.row.is_closing',
    '!props.row.is_open && !props.row.is_waiting && !props.row.is_closing && '
    '!props.row.has_missing_tpsl_orders',
    'props.row.orders && props.row.orders.length > 0', 'order.has_recommendation',
]
BASE_CLICKS = [
    'props.expand = !props.expand',
    "$parent.$emit('view_transaction_details', props.row.id)",
    "$parent.$emit('recreate_tpsl', props.row.id)",
    "$parent.$emit('edit_transaction', props.row.id)",
    "$parent.$emit('close_transaction', props.row.id)",
    "$parent.$emit('retry_close', props.row.id)",
    "$parent.$emit('view_recommendation', order.expert_recommendation_id)",
]


def test_the_template_handlers_arguments_and_conditions_are_the_bases():
    tpl = LiveTradesTable.BODY_TEMPLATE
    assert re.findall(r'\$emit\(([^)]*)\)', tpl) == BASE_EMITS
    assert re.findall(r'(?:v-if|v-else-if)="([^"]*)"', tpl) == BASE_CONDITIONS
    assert re.findall(r'@click="([^"]*)"', tpl) == BASE_CLICKS
    assert re.findall(r'@update:model-value="([^"]*)"', tpl) ==         ["(val) => $parent.$emit('toggle_selection', props.row.id)"]


def test_destructive_dialog_buttons_do_not_take_the_primary_class():
    """The primary-action class moves a button first and full width on a phone: right
    for Update/Apply, wrong for a red button that closes positions, which must never be
    the biggest or first target, 8px from Cancel."""
    src = open(page.__file__, encoding='utf-8').read()
    for label in ('Reset & Retry', 'Close Position', 'Confirm Close'):
        line = next(l for l in src.splitlines() if f"ui.button('{label}'" in l)
        assert 'PRIMARY_ACTION_CLASS' not in line, label
    for label in ('Update', 'Apply'):
        assert 'PRIMARY_ACTION_CLASS' in src[src.index(f"ui.button('{label}'"):][:400]


def test_the_option_dialogs_build_their_columns_from_the_pinned_constants():
    src = open(page.__file__, encoding='utf-8').read()
    assert 'columns = OPTION_LEGS_COLUMNS' in src
    assert 'columns = OPTION_CHAIN_COLUMNS' in src
    assert src.count('_plain_table_columns(columns)') == 2
    # no second, inline copy of either list that a rename could drift from


def test_nested_tables_are_not_reached_by_the_table_rules():
    css = ltt.live_trades_phone_css()
    assert '.live-trades-table > .q-table__middle > table > thead > tr > th' in css
    assert '.live-trades-table table {' not in css
    assert 'dvh' in css and css.index('100vh') < css.index('100dvh')
    assert '100cqw' in css and 'container-type: inline-size' in css


def test_the_stylesheet_is_added_once_per_client():
    once = rsp.CssOnce()
    added = []
    class _C: pass
    a, b = _C(), _C()
    assert once.add(a, 'k', 'css', added.append) is True
    assert once.add(a, 'k', 'css', added.append) is False
    assert once.add(b, 'k', 'css', added.append) is True       # a new page load
    assert once.add(a, 'other', 'css2', added.append) is True
    assert added == ['css', 'css', 'css2']


def test_live_trades_grids_do_not_use_the_allocators_prefix():
    assert rsp.grid_phone_class(4) == 'lt-grid-2'
    assert 'pf-grid' not in rsp.GRID_PHONE_CSS
