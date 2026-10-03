"""The Account Growth "Dividend History" table: column spec, DRIP visibility, phone CSS.

Pure (no NiceGUI). On a phone the table keeps every column, scrolls sideways inside its own
box with DATE + SYMBOL pinned on the left (the Live Trades pattern, ``ui/utils/responsive.py``),
headers never wrap, numbers are right-aligned with fixed decimals. The two DRIP columns exist
only when some row in the CURRENT data has a DRIP value.
"""
from typing import Any, Dict, List, Sequence

from .responsive import PinnedColumn, scroll_table_phone_css, tag_quasar_columns

DIVIDEND_ROOT_CLASS = 'dividend-history-table'
DIVIDEND_PREFIX = 'dh-c-'
#: Pinned left, in order; ``left`` is the sum of the widths before it.
DIVIDEND_PINNED = (PinnedColumn('date', 0, 104), PinnedColumn('symbol', 104, 76))
#: Minimum width per column on a phone (widest content: "2026-06-08", "1,234.56", "0.1234").
DIVIDEND_MIN_WIDTHS = {'date': 104, 'symbol': 76, 'amount': 76, 'tax': 68, 'net': 76,
                       'drip_shares': 88, 'drip_price': 88, 'account': 130}

_FMT2 = "(v) => (v === '' || v === null || v === undefined) ? '' : Number(v).toFixed(2)"
_FMT4 = "(v) => (v === '' || v === null || v === undefined) ? '' : Number(v).toFixed(4)"


def has_drip(rows: Sequence[Dict[str, Any]]) -> bool:
    """Does ANY row carry DRIP shares or a DRIP price? (``''`` / None / 0 = no.)"""
    return any(r.get('drip_shares') not in ('', None, 0) or r.get('drip_price') not in ('', None, 0)
               for r in rows)


def dividend_columns(rows: Sequence[Dict[str, Any]]) -> List[dict]:
    """Quasar columns (tagged for the phone CSS). Short labels; DRIP columns only with DRIP data."""
    cols: List[dict] = [
        {'name': 'date', 'label': 'Date', 'field': 'date', 'sortable': True, 'align': 'left'},
        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
        {'name': 'amount', 'label': 'Gross', 'field': 'amount', 'sortable': True, 'align': 'right',
         ':format': _FMT2},
        {'name': 'tax', 'label': 'Tax', 'field': 'tax', 'sortable': True, 'align': 'right',
         ':format': _FMT2},
        {'name': 'net', 'label': 'Net', 'field': 'net', 'sortable': True, 'align': 'right',
         ':format': _FMT2},
    ]
    if has_drip(rows):
        cols += [
            {'name': 'drip_shares', 'label': 'DRIP sh', 'field': 'drip_shares', 'sortable': False,
             'align': 'right', ':format': _FMT4},
            {'name': 'drip_price', 'label': 'DRIP px', 'field': 'drip_price', 'sortable': False,
             'align': 'right', ':format': _FMT2},
        ]
    cols.append({'name': 'account', 'label': 'Account', 'field': 'account', 'sortable': True,
                 'align': 'left', 'classes': 'mobile-hide', 'headerClasses': 'mobile-hide'})
    return tag_quasar_columns(cols, DIVIDEND_PREFIX)


def dividend_phone_css() -> str:
    """The phone CSS of the table (scroll box, nowrap headers, pinned date + symbol). Pure."""
    return scroll_table_phone_css(DIVIDEND_ROOT_CLASS, DIVIDEND_PREFIX, DIVIDEND_PINNED,
                                  DIVIDEND_MIN_WIDTHS, max_height='none')
