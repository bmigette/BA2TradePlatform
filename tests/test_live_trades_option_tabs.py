"""The Live Trades tab split (spec 2026-09-20, decision 5).

The contract these tests pin down: Stocks is FIRST and therefore the default, the split is
made on the explicit ``asset_class`` field, and the Options tab's columns carry the terms an
equity column set has no room for. The Stocks column set itself must stay exactly as it was
-- the whole point of adding a second tab rather than refactoring the first one.
"""
from __future__ import annotations

from ba2_trade_platform.core.types import AssetClass
from ba2_trade_platform.ui.components.LiveTradesTable import LiveTradesTable
from ba2_trade_platform.ui.pages.live_trades import ASSET_CLASS_TABS


def test_stocks_is_first_and_therefore_the_default_tab():
    assert [label for label, _asset, _icon in ASSET_CLASS_TABS] == ['Stocks', 'Options']
    assert ASSET_CLASS_TABS[0][1] == AssetClass.EQUITY


def test_the_split_is_on_asset_class():
    assert [asset for _label, asset, _icon in ASSET_CLASS_TABS] == [
        AssetClass.EQUITY, AssetClass.OPTION,
    ]


def test_each_tab_has_an_icon_and_no_duplicate_asset_class():
    icons = [icon for _label, _asset, icon in ASSET_CLASS_TABS]
    assert all(icons)
    assert len(set(icons)) == len(icons)
    assert len({asset for _l, asset, _i in ASSET_CLASS_TABS}) == len(ASSET_CLASS_TABS)


class TestColumnSets:
    def _names(self, columns):
        return [column.name for column in columns]

    def test_the_equity_column_set_is_untouched(self):
        names = self._names(LiveTradesTable.TRANSACTION_COLUMNS)
        # 20 columns (Current P/L + Closed P/L became one P/L on 2026-09-24), and NOT an
        # option term among them -- the option set beside it must not have edited this one.
        assert len(names) == 20
        for absent in ('strategy', 'expiry', 'legs'):
            assert absent not in names

    def test_the_option_set_carries_the_terms_the_equity_set_lacks(self):
        names = self._names(LiveTradesTable.OPTION_TRANSACTION_COLUMNS)
        for present in ('strategy', 'expiry', 'legs', 'symbol', 'open_price', 'pnl'):
            assert present in names

    def test_tp_and_sl_are_labelled_as_premium_levels(self):
        labels = {column.name: column.label for column in LiveTradesTable.OPTION_TRANSACTION_COLUMNS}
        # An option's TP/SL are PREMIUM levels; comparing them to the underlying is
        # meaningless, so the column has to say which it is.
        assert labels['take_profit'] == 'TP (prem.)'
        assert labels['stop_loss'] == 'SL (prem.)'
        assert labels['symbol'] == 'Underlying'
        assert labels['open_price'] == 'Net Premium'

    def test_the_option_set_keeps_the_actions_the_table_wires_up(self):
        names = self._names(LiveTradesTable.OPTION_TRANSACTION_COLUMNS)
        for required in ('select', 'expand', 'actions', 'id', 'status'):
            assert required in names


def test_the_table_accepts_a_column_set():
    table = LiveTradesTable(
        data_loader=lambda *args, **kwargs: ([], 0),
        columns=LiveTradesTable.OPTION_TRANSACTION_COLUMNS,
    )
    assert [column.name for column in table.columns] == \
        [column.name for column in LiveTradesTable.OPTION_TRANSACTION_COLUMNS]


def test_the_table_defaults_to_the_equity_column_set():
    table = LiveTradesTable(data_loader=lambda *args, **kwargs: ([], 0))
    assert [column.name for column in table.columns] == \
        [column.name for column in LiveTradesTable.TRANSACTION_COLUMNS]


def test_the_stocks_tab_lists_no_option_transaction(monkeypatch):
    """8082, 2026-09-24: two open long calls were listed on the STOCKS tab too, priced off the
    underlying with no multiplier (a $1.03 KO call read "+8567%"). The Stocks query must keep
    only its own asset class; every count and total on the tab is built from that query."""
    import asyncio
    import sys
    from types import SimpleNamespace

    from tests import factories
    from ba2_trade_platform.ui.pages.live_trades import LiveTradesTab

    factories.create_transaction(symbol="AAPL")
    factories.create_transaction(symbol="KO", asset_class=AssetClass.OPTION, multiplier=100)

    page = sys.modules[LiveTradesTab.__module__]
    monkeypatch.setattr(page, "get_selected_account_id", lambda: None)
    tab = object.__new__(LiveTradesTab)
    tab.status_filter = SimpleNamespace(value=['Waiting', 'Open', 'Closing'])
    tab.expert_filter = SimpleNamespace(value='All')
    tab.symbol_filter = SimpleNamespace(value='')
    tab.broker_order_id_filter = SimpleNamespace(value='')
    tab.expert_id_map = {}
    totals_seen = []
    monkeypatch.setattr(LiveTradesTab, "_compute_filtered_totals",
                        lambda self, session, q: totals_seen.append(
                            [t.symbol for t, _e in session.exec(q).all()]))
    monkeypatch.setattr(LiveTradesTab, "_build_transaction_rows",
                        lambda self, txns, experts, session: [{'symbol': t.symbol} for t in txns])

    rows, total = asyncio.run(tab._transactions_data_loader(1, 20, {}, None, False))

    assert [r['symbol'] for r in rows] == ['AAPL'] and total == 1
    assert totals_seen == [['AAPL']], "the totals strip must not sum an option row either"
