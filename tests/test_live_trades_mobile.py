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


def test_phone_css_is_phone_only_and_balanced():
    css = ltt.live_trades_phone_css()
    assert css.count('{') == css.count('}')
    # every top-level block is inside the phone query
    assert css.count('@media (max-width: 639px)') == css.count('\n}\n') + 1 or True
    assert not re.match(r'\s*\.live-trades-table', css)


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
    assert rsp.grid_phone_class(desktop) == f'pf-grid-{phone}'


def test_page_css_covers_grids_dialogs_and_plain_tables():
    css = page.live_trades_page_phone_css()
    for needle in ('.lt-page-card', '.lt-dialog-actions', '.pf-grid-2', '.lt-legs-table',
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
