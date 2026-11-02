"""Account Growth dividend table: DRIP column visibility, phone column spec, phone CSS."""
from pathlib import Path

from ba2_trade_platform.ui.utils.dividend_table import (
    DIVIDEND_MIN_WIDTHS, DIVIDEND_PINNED, DIVIDEND_PREFIX, DIVIDEND_ROOT_CLASS, dividend_columns,
    dividend_phone_css, has_drip,
)
from ba2_trade_platform.ui.utils.responsive import check_pinned_columns, column_tag

OVERVIEW = Path(__file__).resolve().parents[1] / 'ba2_trade_platform' / 'ui' / 'pages' / 'overview.py'


def _row(**kw):
    base = {'id': 0, 'date': '2026-06-08', 'symbol': 'PATK', 'amount': 0.47, 'tax': 0.07, 'net': 0.4,
            'account': 'A', 'drip_shares': '', 'drip_price': ''}
    base.update(kw)
    return base


def test_drip_columns_hidden_when_no_row_has_drip_values():
    rows = [_row(), _row(id=1, drip_shares='', drip_price='')]
    assert has_drip(rows) is False
    names = [c['name'] for c in dividend_columns(rows)]
    assert 'drip_shares' not in names and 'drip_price' not in names
    assert names == ['date', 'symbol', 'amount', 'tax', 'net', 'account']
    assert has_drip([]) is False


def test_drip_columns_shown_when_any_row_has_drip():
    assert has_drip([_row(), _row(id=1, drip_shares=0.0123, drip_price=11.5)]) is True
    assert has_drip([_row(drip_price=11.5)]) is True
    names = [c['name'] for c in dividend_columns([_row(drip_shares=0.5)])]
    assert names[-3:] == ['drip_shares', 'drip_price', 'account']


def test_short_labels_and_right_aligned_formatted_numbers():
    cols = {c['name']: c for c in dividend_columns([_row(drip_shares=0.5, drip_price=3.0)])}
    assert [cols[n]['label'] for n in ('amount', 'tax', 'net', 'drip_shares', 'drip_price')] == [
        'Gross', 'Tax', 'Net', 'DRIP sh', 'DRIP px']
    for n in ('amount', 'tax', 'net', 'drip_shares', 'drip_price'):
        assert cols[n]['align'] == 'right' and 'toFixed' in cols[n][':format']
    assert 'toFixed(2)' in cols['net'][':format'] and 'toFixed(4)' in cols['drip_shares'][':format']


def test_every_column_is_tagged_for_the_phone_css_and_account_stays_phone_hidden():
    cols = dividend_columns([_row(drip_shares=0.5)])
    for c in cols:
        tag = column_tag(DIVIDEND_PREFIX, c['name'])
        assert tag in c['classes'] and tag in c['headerClasses']
    acct = next(c for c in cols if c['name'] == 'account')
    assert 'mobile-hide' in acct['classes']


def test_pinned_spec_is_consistent_and_names_real_columns():
    names = [c['name'] for c in dividend_columns([_row(drip_shares=0.5)])]
    check_pinned_columns(DIVIDEND_PINNED, names, DIVIDEND_MIN_WIDTHS)       # raises on a bad spec
    assert [p.name for p in DIVIDEND_PINNED] == ['date', 'symbol']
    assert DIVIDEND_PINNED[1].left_px == DIVIDEND_PINNED[0].width_px


def test_phone_css_scrolls_in_its_own_box_with_nowrap_headers_and_pinned_columns():
    css = dividend_phone_css()
    assert css.startswith('@media (max-width: 639px)')
    r = f'.{DIVIDEND_ROOT_CLASS}'
    assert f'{r} > .q-table__middle' in css and 'overflow: auto' in css
    assert 'white-space: nowrap !important' in css                      # headers never wrap
    for name in ('date', 'symbol'):
        assert f'th.{column_tag(DIVIDEND_PREFIX, name)}' in css and 'position: sticky' in css
    assert 'min-width: 88px' in css and 'width: max-content' in css
    assert css.count('{') == css.count('}')


def test_the_css_is_installed_in_render_before_the_first_await():
    src = OVERVIEW.read_text(encoding='utf-8')
    start = src.index('    def render(self):\n        logger.debug("[RENDER] AccountGrowthTab.render()')
    body = src[start:src.index('    async def _load_growth_data(', start)]
    assert body.index('dividend_phone_css()') < body.index('asyncio.create_task')
    code = [l for l in body[:body.index('dividend_phone_css()')].splitlines() if not l.strip().startswith('#')]
    assert not any('await ' in l for l in code)
