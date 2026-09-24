"""The Floating P/L cards price an OPTION transaction off its premium, multiplier-aware.

The regression (8082, 2026-09-24): two open long calls (GILD 2 contracts, KO 5) showed
"$0.00 (partial) -- no broker price for GILD, KO". The card looked the broker position up
under the transaction's symbol, which for an option is the UNDERLYING, while Alpaca files the
position under the CONTRACT symbol -- and its equity formula ``(price - avg) * qty`` would have
dropped the x100 multiplier even had the lookup hit.

Pinned here: an option row goes through ``open_option_transaction_pnl`` (the Options tab's own
seam), an equity row keeps the position-map path, a working (WAITING) option entry is a
measured 0, and an unpriceable option is named as unpriced rather than zeroed.
"""
import sys
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core import option_pnl_display
from ba2_trade_platform.core.option_pnl_display import OptionPnlDisplay, unavailable_pnl
from ba2_trade_platform.core.types import AssetClass, OrderDirection, TransactionStatus
from ba2_trade_platform.ui.components.FloatingPLPerAccountWidget import (
    FloatingPLPerAccountWidget,
)

mod = sys.modules[FloatingPLPerAccountWidget.__module__]


class _Account:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


class _Session:
    """Every query answers empty: the option seam is faked, so no row is ever read."""

    def exec(self, _statement):
        return SimpleNamespace(all=lambda: [], first=lambda: None)


def _txn(id, symbol, asset_class, status=TransactionStatus.OPENED):
    return SimpleNamespace(id=id, symbol=symbol, asset_class=asset_class, status=status,
                           side=OrderDirection.BUY, quantity=2.0)


@pytest.fixture
def widget(monkeypatch):
    w = object.__new__(FloatingPLPerAccountWidget)
    monkeypatch.setattr(FloatingPLPerAccountWidget, "_read_money",
                        lambda self, account, account_id: (None, None, None))
    # An EXPERT-driven account (8082's AlpacaOptions): the local-fills path, not the manual
    # account's broker-figure path.
    monkeypatch.setattr(FloatingPLPerAccountWidget, "_is_manual_account",
                        lambda self, account_id, session: False)
    return w


def _rows(widget, monkeypatch, positions, trans, priced):
    monkeypatch.setattr(mod, "get_account_instance_from_id",
                        lambda account_id, session=None: _Account(positions))
    calls = []

    def fake(account, transaction, orders):
        calls.append(transaction.id)
        result = priced[transaction.id]
        return result, SimpleNamespace(count=1 if result.available else 0, incomplete=[])

    monkeypatch.setattr(option_pnl_display, "open_option_transaction_pnl", fake)
    rows = widget._rows_for_account(1, [(t, "expert") for t in trans], None, _Session())
    assert len(rows) == 1
    return rows[0], calls


def test_option_rows_are_priced_by_the_option_seam_not_the_position_map(widget, monkeypatch):
    # Alpaca's positions: keyed by CONTRACT symbol -- no 'GILD' or 'KO' entry at all.
    positions = [{"symbol": "GILD261120C00165000", "current_price": 3.4},
                 {"symbol": "KO261120C00095000", "current_price": 1.1}]
    trans = [_txn(1, "GILD", AssetClass.OPTION), _txn(2, "KO", AssetClass.OPTION)]
    priced = {1: OptionPnlDisplay(amount=90.0, percent=15.3, source="single_leg_premium"),
              2: OptionPnlDisplay(amount=35.0, percent=6.8, source="single_leg_premium")}

    row, calls = _rows(widget, monkeypatch, positions, trans, priced)

    assert sorted(calls) == [1, 2]
    assert row.pl == pytest.approx(125.0), "premium P/L x multiplier, from the shared seam"
    assert row.unpriced == (), "a priced option position is not 'missing from the row'"


def test_an_unpriceable_option_is_named_not_zeroed(widget, monkeypatch):
    trans = [_txn(1, "GILD", AssetClass.OPTION)]
    priced = {1: unavailable_pnl("no current option quote or contract multiplier")}
    # count=0 with the fake means "nothing executed" -> 0.0; make it a HELD position instead.
    monkeypatch.setattr(mod, "get_account_instance_from_id",
                        lambda account_id, session=None: _Account([]))
    monkeypatch.setattr(option_pnl_display, "open_option_transaction_pnl",
                        lambda a, t, o: (priced[t.id], SimpleNamespace(count=1, incomplete=[])))
    (row,) = widget._rows_for_account(1, [(t, "expert") for t in trans], None, _Session())
    assert row.unpriced == ("GILD",)


def test_a_working_option_entry_is_a_measured_zero(widget, monkeypatch):
    trans = [_txn(1, "KO", AssetClass.OPTION, status=TransactionStatus.WAITING)]
    row, calls = _rows(widget, monkeypatch, [], trans, {})
    assert calls == [], "nothing held yet: nothing to price"
    assert row.pl == 0.0 and row.unpriced == ()


def test_an_equity_row_keeps_the_position_map(widget, monkeypatch):
    trans = [_txn(1, "AAPL", AssetClass.EQUITY)]
    seen = []
    monkeypatch.setattr(FloatingPLPerAccountWidget, "_transaction_pl",
                        lambda self, t, prices, session: seen.append(prices) or 12.5)
    row, calls = _rows(widget, monkeypatch, [{"symbol": "AAPL", "current_price": 200.0}],
                       trans, {})
    assert calls == [] and seen == [{"AAPL": 200.0}]
    assert row.pl == pytest.approx(12.5)
