"""The Options tab's loader: pagination, totals, event loop, quotes.

Rewritten after the SECOND review (reports/ui/option_trade_ui_recheck_2026-09-21.md), which
found that the first version's fixtures were not the real thing:

* N1 — `_contract_quote` had been left decorated `@staticmethod` while the caller passed the
  two arguments of an INSTANCE method, so on a real tab it raised TypeError and the loader's
  except turned the whole table empty. These tests exercise the real class and the real call.
* N3 — the 500-row totals cap was applied to the FETCH, so the same cap removed transactions
  from the table. These tests use a real SQLite database, where SQL's own offset/limit applies,
  instead of a fake session that ignored them.
* N5 — the quote cache was keyed by contract alone (cross-account) and did not cover the
  pricing seam's own reads. The dedup lives in `QuoteCachingAccount` now, and is tested there.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from ba2_common.core import TradeConditions
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.core.models import AccountDefinition, ExpertInstance, Transaction, TradingOrder
from ba2_trade_platform.core.option_pnl_display import QuoteCachingAccount, quote_caching_account
from ba2_trade_platform.ui.pages import option_trades
from ba2_trade_platform.ui.pages.option_trades import OptionTradesTab


def tab():
    """The REAL tab, headless.

    `__init__` only assigns attributes, so it constructs without a UI. The filter widgets are
    created in `render()`, so they are set to None here — which the loader tolerates by design.
    Using the real class is the point: the N1 crash was invisible to a hand-built namespace.
    """
    instance = OptionTradesTab()
    instance.status_filter = None
    instance.expert_filter = None
    instance.symbol_filter = None
    instance.strategy_filter = None
    instance.expert_id_map = {}
    return instance


def order(oid, transaction_id, symbol='XYZ_C95', side=OrderDirection.BUY):
    return NS(id=oid, account_id=1, transaction_id=transaction_id, contract_symbol=symbol,
              side=side, option_type=NS(value='call'), strike=95.0,
              open_price=8.0, quantity=1, filled_qty=1, multiplier=100,
              underlying_symbol='XYZ', expiry=None, asset_class=AssetClass.OPTION,
              status=OrderStatus.FILLED, position_intent=None,
              created_at=datetime(2026, 9, 8, 13, 30))


# ---------------------------------------------------------------- real database fixture

def _seed_database(count: int):
    """A real in-memory SQLite database with `count` OPEN single-leg option transactions.

    Open, because the totals strip is a statement about OPEN structures -- closed rows
    deliberately contribute nothing to it. Each transaction has one executed opening order so
    the row is a real single-leg structure, and the broker is stubbed (no network).
    """
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    for _ in range(count):
        transaction = Transaction(
            symbol='XYZ', quantity=1, open_price=6.0, multiplier=100,
            asset_class=AssetClass.OPTION, option_strategy='long_call',
            status=TransactionStatus.OPENED, side=OrderDirection.BUY,
            created_at=datetime(2026, 9, 1, 14, 0), open_date=datetime(2026, 9, 1, 14, 0),
        )
        session.add(transaction)
        session.flush()
        session.add(TradingOrder(
            transaction_id=transaction.id, account_id=1, symbol='XYZ',
            order_type=OrderType.MARKET,
            contract_symbol='XYZ_C95', side=OrderDirection.BUY, option_type=OptionRight.CALL,
            strike=95.0, quantity=1, filled_qty=1, open_price=8.0, multiplier=100,
            asset_class=AssetClass.OPTION, status=OrderStatus.FILLED, underlying_symbol='XYZ',
            created_at=datetime(2026, 9, 1, 14, 0),
        ))
    session.commit()
    return engine, session


def collect(tab_instance, session, page=1, page_size=20, sort_by='created_at', descending=True):
    account = MagicMock(spec=OptionsAccountInterface)
    account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
    with patch.object(option_trades, 'get_db', lambda: session), \
            patch.object(option_trades, 'scope_transactions_to_account', lambda q, *a: q), \
            patch.object(option_trades, 'get_selected_account_id', lambda: None), \
            patch.object(option_trades, 'get_account_instance_from_id', lambda *a, **k: account), \
            patch.object(TradeConditions, '_get_option_pnl_via_transaction',
                         lambda acct, o: {'amount': 370.0, 'percent': 6.16}):
        return OptionTradesTab._collect_rows(tab_instance, page, page_size, {}, sort_by, descending)


class TestBrowsingIsNotCappedByTheTotals:
    """N3: a totals disclaimer must not remove transactions from the table."""

    def test_the_last_page_of_a_501_row_result_is_not_empty(self):
        engine, session = _seed_database(501)
        instance = tab()
        try:
            rows, total = collect(instance, session, page=26, page_size=20)
        finally:
            session.close()
        assert total == 501
        assert len(rows) == 1          # 501 rows / 20 per page -> page 26 holds the last one

    def test_a_page_beyond_the_old_cap_still_returns_rows(self):
        # Under the previous code the fetch stopped at 500, so page 26 (and every row past
        # 500) was unreachable while the UI still advertised 26 pages.
        engine, session = _seed_database(501)
        instance = tab()
        try:
            first, _ = collect(instance, session, page=1, page_size=20)
            last, _ = collect(instance, session, page=26, page_size=20)
        finally:
            session.close()
        assert len(first) == 20
        assert len(last) == 1
        assert {row['id'] for row in first}.isdisjoint({row['id'] for row in last})

    def test_the_totals_still_cover_the_whole_set(self):
        engine, session = _seed_database(501)
        instance = tab()
        try:
            collect(instance, session, page=1, page_size=20)
        finally:
            session.close()
        # The TOTALS pass is capped at 500 rows, so it sums 500 of the 501 open structures --
        # and says so. (Browsing is not capped; see the pagination tests above.)
        assert instance._totals['pnl'] == pytest.approx(500 * 370.0)
        assert instance._totals_truncated is True

    def test_the_page_pass_does_not_overwrite_the_totals_with_its_own(self):
        engine, session = _seed_database(30)
        instance = tab()
        try:
            collect(instance, session, page=1, page_size=20)
        finally:
            session.close()
        assert instance._totals['pnl'] == pytest.approx(30 * 370.0)   # not 20 x 370


# ---------------------------------------------------------------- N1: the real call

class TestTheRealClass:
    def test_contract_quote_is_callable_on_an_instance(self):
        # N1: `@staticmethod` was left on the definition while the caller passed the two
        # arguments of an instance method -> TypeError -> the loader's except emptied the tab.
        instance = tab()
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)

        assert OptionTradesTab._contract_quote(instance, account, order(1, 7)) == 13.3

    def test_the_loader_prices_a_single_leg_row_without_emptying_the_table(self):
        instance = tab()
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        single = Transaction(
            symbol='XYZ', quantity=1, open_price=6.0, multiplier=100,
            asset_class=AssetClass.OPTION, option_strategy='long_call',
            status=TransactionStatus.OPENED, side=OrderDirection.BUY,
            created_at=datetime(2026, 9, 1, 14, 0), open_date=datetime(2026, 9, 1, 14, 0),
        )
        with patch.object(option_trades, 'get_account_instance_from_id', lambda *a, **k: account):
            rows = OptionTradesTab._build_rows(instance, [single], {}, _OrdersOnly([order(1, 1)]))
        assert len(rows) == 1
        assert rows[0]['leg_count'] == 1


class _OrdersOnly:
    """A session that serves the per-transaction order lookup `_build_rows` performs."""

    def __init__(self, orders):
        self._orders = orders

    def exec(self, _):
        return NS(all=lambda: list(self._orders))

    def get(self, *_):
        return NS(name='Fixture account')


# ---------------------------------------------------------------- N5: quote reuse

class TestQuoteCachingAccount:
    def test_it_is_an_options_account_so_the_pricing_seam_accepts_it(self):
        account = MagicMock(spec=OptionsAccountInterface)
        assert isinstance(quote_caching_account(account, {}, 1), OptionsAccountInterface)

    def test_one_broker_call_per_contract_per_refresh(self):
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        cache: dict = {}
        wrapped = quote_caching_account(account, cache, 1)

        assert wrapped.get_option_quote('XYZ_C95').bid == 13.3
        assert wrapped.get_option_quote('XYZ_C95').bid == 13.3
        assert account.get_option_quote.call_count == 1

    def test_the_cache_is_per_ACCOUNT_not_per_contract(self):
        # Two accounts can hold the same contract; one account's quote is not the other's.
        first = MagicMock(spec=OptionsAccountInterface)
        first.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        second = MagicMock(spec=OptionsAccountInterface)
        second.get_option_quote.return_value = NS(bid=12.0, ask=12.1, last=12.05)

        cache: dict = {}
        assert quote_caching_account(first, cache, 1).get_option_quote('XYZ_C95').bid == 13.3
        assert quote_caching_account(second, cache, 2).get_option_quote('XYZ_C95').bid == 12.0
        assert cache[1, 'XYZ_C95'].bid == 13.3
        assert cache[2, 'XYZ_C95'].bid == 12.0

    def test_a_new_refresh_starts_a_new_cache(self):
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        quote_caching_account(account, {}, 1).get_option_quote('XYZ_C95')
        quote_caching_account(account, {}, 1).get_option_quote('XYZ_C95')   # a fresh pass
        assert account.get_option_quote.call_count == 2

    def test_an_abstract_method_is_delegated_not_answered_by_the_abc(self):
        # `get_option_positions` is defined ON the ABC, so plain __getattr__ never sees it and
        # the ABC's own stub would answer None.
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_positions.return_value = ['a-position']
        wrapped = quote_caching_account(account, {}, 1)
        assert wrapped.get_option_positions() == ['a-position']

    def test_a_concrete_abc_method_is_delegated_too(self):
        # The ABC carries real implementations as well; they must run against the ACCOUNT.
        account = MagicMock(spec=OptionsAccountInterface)
        account.check_cover_for_covered_call.return_value = True
        wrapped = quote_caching_account(account, {}, 1)
        assert wrapped.check_cover_for_covered_call([], 1) is True

    def test_plain_attributes_are_delegated(self):
        account = MagicMock(spec=OptionsAccountInterface)
        account.name = 'Fixture account'
        assert quote_caching_account(account, {}, 1).name == 'Fixture account'

    def test_the_seam_and_the_current_column_share_one_call(self):
        # The point of the wrapper: the pricing seam quotes the contract itself, and the tab
        # wants the same contract's premium for its Current column.
        account = MagicMock(spec=OptionsAccountInterface)
        account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
        cache: dict = {}
        wrapped = quote_caching_account(account, cache, 1)

        seam_quote = wrapped.get_option_quote('XYZ_C95')
        column_quote = wrapped.get_option_quote('XYZ_C95')

        assert seam_quote is column_quote
        assert account.get_option_quote.call_count == 1


# ---------------------------------------------------------------- R10: threading

class TestBlockingWorkStaysOffTheEventLoop:
    def test_the_loader_delegates_to_a_thread_and_paints_after(self):
        instance = tab()
        instance._refresh_totals = lambda: None
        calls = []

        async def fake_to_thread(function, *args, **kwargs):
            calls.append(getattr(function, '__name__', str(function)))
            return ([], 0)

        async def run():
            with patch.object(option_trades.asyncio, 'to_thread', fake_to_thread):
                return await OptionTradesTab._data_loader(instance, 1, 20, {}, 'created_at', True)

        assert asyncio.run(run()) == ([], 0)
        assert calls == ['_collect_rows']

    def test_a_failing_totals_repaint_does_not_break_the_table(self):
        instance = tab()
        instance._refresh_totals = MagicMock(side_effect=RuntimeError('no slot'))

        async def fake_to_thread(function, *args, **kwargs):
            return ([{'id': 1}], 1)

        async def run():
            with patch.object(option_trades.asyncio, 'to_thread', fake_to_thread):
                return await OptionTradesTab._data_loader(instance, 1, 20, {}, 'created_at', True)

        assert asyncio.run(run()) == ([{'id': 1}], 1)
