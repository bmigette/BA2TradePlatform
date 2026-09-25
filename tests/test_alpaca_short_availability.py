"""Alpaca refuses to OPEN a short unless the asset is shortable AND easy to borrow.

The gate sits in ``AlpacaAccount._submit_order_impl``, before the request is built:

* an order that opens a short (a non-closing SELL on a SELL transaction) fetches the
  Asset through the shared ``_asset_cache`` and needs ``shortable`` and
  ``easy_to_borrow`` both True;
* a failure is surfaced like every other submit refusal: nothing reaches the broker,
  the row is marked ERROR, and the reason (symbol + failing flag) is in ``comment``;
* an asset fetch failure REFUSES -- shortability is never assumed;
* closing sells, a long's own SELL protective legs, and buys never fetch the asset.

No live API call anywhere: ``client`` is a MagicMock returning real alpaca-py Asset
objects, so ``client.submit_order`` records the request that WOULD have gone out.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
from alpaca.trading.models import Asset

from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _asset(symbol="AAPL", shortable=True, easy_to_borrow=True):
    # `asset_class` is exposed under the pydantic alias "class" (see
    # tests/test_alpaca_asset_cache.py), so it is passed via a dict splat.
    return Asset(
        id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
        symbol=symbol, status=AssetStatus.ACTIVE, tradable=True, marginable=True,
        shortable=shortable, easy_to_borrow=easy_to_borrow, fractionable=True,
        min_order_size=0.001, min_trade_increment=0.001,
        maintenance_margin_requirement=30.0)


def _alpaca_response(side="sell"):
    """SimpleNamespace, not MagicMock: alpaca_order_to_tradingorder reads it with
    getattr(..., None) into a pydantic TradingOrder."""
    return SimpleNamespace(
        id="brk-1", symbol="AAPL", qty="10", side=side, type="market",
        status="new", time_in_force="gtc", order_class=None, legs=None,
        filled_qty="0", filled_avg_price=None, created_at=None,
        limit_price=None, stop_price=None,
    )


def _bare_account(asset=None, asset_error=None):
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._authentication_error = None
    acct._asset_cache = {}
    acct._margin_info_cache = {}
    acct._balance_cache_lock = threading.Lock()
    acct._balance_cache_time = 0.0
    acct._account_snapshot_cache = None
    acct.client.get_all_positions.return_value = []
    acct.client.submit_order.return_value = _alpaca_response()
    if asset_error is not None:
        acct.client.get_asset.side_effect = asset_error
    else:
        acct.client.get_asset.return_value = asset if asset is not None else _asset()
    return acct


def _transaction(side, status=TransactionStatus.WAITING, symbol="AAPL"):
    return add_instance(Transaction(symbol=symbol, quantity=10, side=side, status=status))


def _saved_order(**kwargs):
    defaults = dict(account_id=1, symbol="AAPL", quantity=10, side=OrderDirection.SELL,
                    order_type=OrderType.MARKET, status=OrderStatus.PENDING, good_for=None)
    defaults.update(kwargs)
    return get_instance(TradingOrder, add_instance(TradingOrder(**defaults)))


def _short_entry(symbol="AAPL"):
    txn_id = _transaction(OrderDirection.SELL, symbol=symbol)
    return _saved_order(symbol=symbol, side=OrderDirection.SELL, transaction_id=txn_id)


# ---------------------------------------------------------------------------
# Opening a short
# ---------------------------------------------------------------------------

def test_shortable_easy_to_borrow_short_entry_is_submitted():
    acct = _bare_account(_asset(shortable=True, easy_to_borrow=True))
    order = _short_entry()

    result = acct._submit_order_impl(order)

    assert result is not None
    acct.client.get_asset.assert_called_once_with("AAPL")
    acct.client.submit_order.assert_called_once()
    assert get_instance(TradingOrder, order.id).broker_order_id == "brk-1"


def test_not_shortable_is_refused_before_any_broker_order():
    acct = _bare_account(_asset(shortable=False, easy_to_borrow=True))
    order = _short_entry()

    result = acct._submit_order_impl(order)

    assert result is None
    acct.client.submit_order.assert_not_called()
    row = get_instance(TradingOrder, order.id)
    assert row.status == OrderStatus.ERROR
    assert "AAPL" in row.comment
    assert "shortable=False" in row.comment
    assert "easy_to_borrow" not in row.comment


def test_not_easy_to_borrow_is_refused():
    acct = _bare_account(_asset(shortable=True, easy_to_borrow=False))
    order = _short_entry()

    result = acct._submit_order_impl(order)

    assert result is None
    acct.client.submit_order.assert_not_called()
    row = get_instance(TradingOrder, order.id)
    assert row.status == OrderStatus.ERROR
    assert "AAPL" in row.comment
    assert "easy_to_borrow=False" in row.comment


def test_asset_fetch_error_refuses_rather_than_assuming_shortable():
    acct = _bare_account(asset_error=RuntimeError("boom: asset endpoint down"))
    order = _short_entry()

    result = acct._submit_order_impl(order)

    assert result is None
    acct.client.submit_order.assert_not_called()
    row = get_instance(TradingOrder, order.id)
    assert row.status == OrderStatus.ERROR
    assert "AAPL" in row.comment
    assert "could not be fetched" in row.comment


def test_asset_fetch_returning_nothing_refuses():
    acct = _bare_account()
    acct.client.get_asset.return_value = None
    order = _short_entry()

    assert acct._submit_order_impl(order) is None
    acct.client.submit_order.assert_not_called()
    assert get_instance(TradingOrder, order.id).status == OrderStatus.ERROR


def test_the_asset_cache_is_reused_across_two_short_entries_on_one_symbol():
    acct = _bare_account(_asset())

    acct._submit_order_impl(_short_entry())
    acct._submit_order_impl(_short_entry())

    assert acct.client.get_asset.call_count == 1
    assert acct.client.submit_order.call_count == 2


def test_a_sell_with_no_transaction_is_not_checked():
    """Unreachable through submit_order (every non-closing order gets a transaction);
    direct _submit_order_impl callers use it for protective legs, which must not
    start fetching assets."""
    acct = _bare_account(_asset(shortable=False))
    order = _saved_order(side=OrderDirection.SELL, transaction_id=None)

    assert acct._submit_order_impl(order) is not None
    acct.client.get_asset.assert_not_called()
    acct.client.submit_order.assert_called_once()


# ---------------------------------------------------------------------------
# Never checked
# ---------------------------------------------------------------------------

def test_a_dependent_leg_never_fetches_the_asset():
    """A TP/SL leg waiting on its entry (depends_on_order set) never opens a position,
    so the gate never runs for it -- not even on a SELL-side transaction."""
    acct = _bare_account(_asset(shortable=False, easy_to_borrow=False))
    entry = _short_entry()
    leg = _saved_order(side=OrderDirection.SELL, transaction_id=entry.transaction_id,
                       depends_on_order=entry.id)

    assert AlpacaAccount._order_opens_short(leg, is_closing_order=False) is False
    acct.client.get_asset.assert_not_called()


def test_a_closing_sell_never_fetches_the_asset():
    acct = _bare_account(_asset(shortable=False, easy_to_borrow=False))
    txn_id = _transaction(OrderDirection.BUY, status=TransactionStatus.OPENED)
    order = _saved_order(side=OrderDirection.SELL, transaction_id=txn_id)

    result = acct._submit_order_impl(order, is_closing_order=True)

    assert result is not None
    acct.client.get_asset.assert_not_called()
    acct.client.submit_order.assert_called_once()


def test_a_longs_protective_sell_leg_never_fetches_the_asset():
    """A long's TP/SL legs are SELLs submitted WITHOUT is_closing_order
    (_create_broker_tp_order and friends); the BUY transaction is what exempts them."""
    acct = _bare_account(_asset(shortable=False, easy_to_borrow=False))
    txn_id = _transaction(OrderDirection.BUY, status=TransactionStatus.OPENED)
    order = _saved_order(side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                         limit_price=250.0, transaction_id=txn_id)

    result = acct._submit_order_impl(order)

    assert result is not None
    acct.client.get_asset.assert_not_called()
    acct.client.submit_order.assert_called_once()


def test_a_buy_never_fetches_the_asset():
    acct = _bare_account(_asset(shortable=False, easy_to_borrow=False))
    acct.client.submit_order.return_value = _alpaca_response(side="buy")
    txn_id = _transaction(OrderDirection.BUY)
    order = _saved_order(side=OrderDirection.BUY, transaction_id=txn_id)

    result = acct._submit_order_impl(order)

    assert result is not None
    acct.client.get_asset.assert_not_called()
    acct.client.submit_order.assert_called_once()


def test_a_shorts_buy_cover_never_fetches_the_asset():
    acct = _bare_account(_asset(shortable=False, easy_to_borrow=False))
    acct.client.submit_order.return_value = _alpaca_response(side="buy")
    txn_id = _transaction(OrderDirection.SELL, status=TransactionStatus.OPENED)
    order = _saved_order(side=OrderDirection.BUY, transaction_id=txn_id)

    assert acct._submit_order_impl(order, is_closing_order=True) is not None
    acct.client.get_asset.assert_not_called()


def test_an_option_sell_is_not_an_equity_short():
    from ba2_trade_platform.core.types import AssetClass as CoreAssetClass
    assert AlpacaAccount._order_opens_short(
        SimpleNamespace(side=OrderDirection.SELL, asset_class=CoreAssetClass.OPTION,
                        transaction_id=None),
        is_closing_order=False) is False
