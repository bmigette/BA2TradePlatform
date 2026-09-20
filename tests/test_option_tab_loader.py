"""Regressions for review findings R9 and R10 (the Options tab's totals and its event loop).

R9 — the totals strip was summed from whichever PAGE `_build_rows` happened to receive, so
changing the sort mode changed the displayed cost and P&L for the same filter. They now cover
the whole filtered set, bounded, and the page is sliced out of the same rows.

R10 — the async loader called synchronous DB and broker reads on the event loop, so a slow
broker froze every other UI callback. The blocking pass now runs in a worker thread, and quote
reads are deduplicated per contract per refresh.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, TransactionStatus
from ba2_trade_platform.ui.pages import option_trades
from ba2_trade_platform.ui.pages.option_trades import OptionTradesTab


def txn(ident, symbol='XYZ', quantity=1, open_price=6):
    return NS(id=ident, symbol=symbol, side=OrderDirection.BUY, quantity=quantity,
              open_price=open_price, close_price=None, multiplier=100,
              expiry=None, option_strategy='long_call', status=TransactionStatus.OPENED,
              expert_id=None, take_profit=None, stop_loss=None,
              created_at=datetime(2026, 9, 8, 13, 30), open_date=None, close_date=None)


def order(oid, transaction_id):
    return NS(id=oid, account_id=1, transaction_id=transaction_id, contract_symbol=f'XYZ_C95_{oid}',
              side=OrderDirection.BUY, option_type=NS(value='call'), strike=95.0,
              open_price=8.0, quantity=1, filled_qty=1, multiplier=100,
              underlying_symbol='XYZ', expiry=None, asset_class=AssetClass.OPTION,
              status=OrderStatus.FILLED, position_intent=None,
              created_at=datetime(2026, 9, 8, 13, 30))


class LoaderSession:
    """A session whose exec() serves both the count query and the row query."""

    def __init__(self, rows, count):
        self._rows = rows
        self._count = count

    def exec(self, _statement):
        return NS(all=lambda: list(self._rows), one=lambda: self._count)

    def get(self, *_):
        return NS(name='Fixture account')

    def close(self):
        pass


def build_tab():
    """A tab with the REAL loader methods bound, plus inert filters.

    The real ``_build_rows`` is used on purpose: the totals ARE what R9 is about, so a stubbed
    row builder would assert nothing.
    """
    tab = NS()
    for name in ('_build_rows', '_contract_quote', '_collect_rows'):
        setattr(tab, name, types.MethodType(getattr(OptionTradesTab, name), tab))
    tab.status_filter = None
    tab.expert_filter = None
    tab.symbol_filter = None
    tab.strategy_filter = None
    tab.expert_id_map = {}
    tab._quote_snapshot = {}
    tab._totals = {}
    tab._totals_truncated = False
    return tab


def collect(tab, rows, count, page=1, page_size=20, sort_by='created_at', descending=True):
    session = LoaderSession(rows, count)
    with patch.object(option_trades, 'get_db', lambda: session), \
            patch.object(option_trades, 'scope_transactions_to_account', lambda q, *a: q), \
            patch.object(option_trades, 'get_selected_account_id', lambda: None):
        return OptionTradesTab._collect_rows(tab, page, page_size, {}, sort_by, descending)


class TestTotalsIgnorePagination:
    """R9: the same filter must show the same totals under every page and sort."""

    def _rows(self, count):
        return [(txn(index), None) for index in range(1, count + 1)]

    def test_the_totals_cover_the_whole_filtered_set_not_the_page(self):
        tab = build_tab()
        rows, total_count = collect(tab, self._rows(30), 30, page=1, page_size=20)
        assert len(rows) == 20            # the page
        assert total_count == 30
        assert tab._totals['cost'] == pytest.approx(30 * 6 * 1 * 100)  # every row, not 20

    def test_the_totals_do_not_move_with_the_page(self):
        first = build_tab()
        collect(first, self._rows(30), 30, page=1, page_size=20)
        last = build_tab()
        collect(last, self._rows(30), 30, page=2, page_size=20)
        assert first._totals == last._totals

    def test_the_totals_do_not_move_with_the_sort_mode(self):
        by_date = build_tab()
        collect(by_date, self._rows(30), 30, sort_by='created_at')
        by_symbol = build_tab()
        collect(by_symbol, self._rows(30), 30, sort_by='symbol')
        assert by_date._totals == by_symbol._totals

    def test_a_page_beyond_the_cap_is_still_a_page_of_the_same_totals(self):
        tab = build_tab()
        rows, _ = collect(tab, self._rows(option_trades._TOTALS_ROW_LIMIT), option_trades._TOTALS_ROW_LIMIT,
                          page=1, page_size=20)
        assert len(rows) == 20
        assert tab._totals_truncated is False

    def test_the_totals_say_when_they_are_partial(self):
        tab = build_tab()
        collect(tab, self._rows(5), 5000, page=1, page_size=20)
        assert tab._totals_truncated is True


class TestBlockingWorkStaysOffTheEventLoop:
    """R10: the reads happen in a worker thread, the UI paint happens here."""

    def test_the_loader_delegates_to_a_thread_and_paints_after(self):
        tab = build_tab()
        tab._refresh_totals = lambda: None
        calls = []

        async def fake_to_thread(function, *args, **kwargs):
            calls.append(getattr(function, '__name__', str(function)))
            return ([], 0)

        async def run():
            with patch.object(option_trades.asyncio, 'to_thread', fake_to_thread):
                return await OptionTradesTab._data_loader(tab, 1, 20, {}, 'created_at', True)

        rows, total = asyncio.run(run())
        assert calls == ['_collect_rows']
        assert (rows, total) == ([], 0)

    def test_a_failing_totals_repaint_does_not_break_the_table(self):
        tab = build_tab()
        tab._refresh_totals = MagicMock(side_effect=RuntimeError('no slot'))

        async def fake_to_thread(function, *args, **kwargs):
            return ([{'id': 1}], 1)

        async def run():
            with patch.object(option_trades.asyncio, 'to_thread', fake_to_thread):
                return await OptionTradesTab._data_loader(tab, 1, 20, {}, 'created_at', True)

        assert asyncio.run(run()) == ([{'id': 1}], 1)


class TestQuoteSnapshot:
    """R10: one broker read per contract per refresh."""

    def test_the_same_contract_is_read_once(self):
        tab = build_tab()
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        same_contract = [order(1, 7), order(2, 7)]
        same_contract[1].contract_symbol = same_contract[0].contract_symbol

        first = OptionTradesTab._contract_quote(tab, account, same_contract[0])
        second = OptionTradesTab._contract_quote(tab, account, same_contract[1])

        assert first == second == 13.3
        assert account.get_option_quote.call_count == 1

    def test_a_new_refresh_reads_again(self):
        tab = build_tab()
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        OptionTradesTab._contract_quote(tab, account, order(1, 7))
        tab._quote_snapshot = {}  # what _collect_rows does at the start of a pass
        OptionTradesTab._contract_quote(tab, account, order(1, 7))
        assert account.get_option_quote.call_count == 2

    def test_a_failed_read_is_remembered_as_a_failure_not_retried_per_row(self):
        tab = build_tab()
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.side_effect = RuntimeError('broker down')

        assert OptionTradesTab._contract_quote(tab, account, order(1, 7)) is None
        assert OptionTradesTab._contract_quote(tab, account, order(1, 7)) is None
        assert account.get_option_quote.call_count == 1
