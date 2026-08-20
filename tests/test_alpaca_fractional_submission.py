"""Alpaca accepts a fractional quantity ONLY on a DAY MARKET order.

Two independent traps, both of which the adapter has to close:

1. ``tif_map`` inside ``_submit_order_impl`` resolves ``good_for`` with
   ``tif_map.get(good_for_value, TimeInForce.GTC)`` -- so ``good_for=None`` (which is
   exactly what the allocation actions produce, they never set it) silently becomes
   GTC, and Alpaca refuses a fractional GTC order.
2. Every non-MARKET type (limit / stop / stop-limit / OCO) refuses a fractional
   quantity outright -- including a protective TP/SL leg sized from a fractional
   position. Those fall back ONCE to ``floor(qty)`` whole shares; a floor of 0 leaves
   nothing to send, which is a SKIP (CANCELED + reason), not a failure.

No live API call anywhere: ``client`` is a MagicMock, so ``client.submit_order``
records the request object that WOULD have gone to Alpaca.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.enums import TimeInForce

from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _alpaca_response(order_id="brk-1", order_type="market"):
    """A stand-in for the Alpaca SDK Order the client returns on a successful submit.

    SimpleNamespace (not MagicMock) on purpose: alpaca_order_to_tradingorder reads the
    response with getattr(..., None) into a pydantic TradingOrder, and a MagicMock
    attribute would fail validation instead of falling back to None.
    """
    return SimpleNamespace(
        id=order_id, symbol="AAPL", qty="1", side="buy", type=order_type,
        status="new", time_in_force="day", order_class=None, legs=None,
        filled_qty="0", filled_avg_price=None, created_at=None,
        limit_price=None, stop_price=None,
    )


def _bare_account():
    """An AlpacaAccount without __init__ (no credentials, no broker connection).

    client is a MagicMock so _check_authentication() passes and every submission is
    captured rather than sent. _balance_cache_lock is real because the post-submit
    path calls invalidate_balance_cache().
    """
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct.client.submit_order.return_value = _alpaca_response()
    acct._margin_info_cache = {}
    acct._balance_cache_lock = threading.Lock()
    acct._balance_cache_time = 0.0
    return acct


def _saved_order(**kwargs):
    """Persist the row, then hand back a detached copy -- _submit_order_impl looks
    the row up again after submitting."""
    defaults = dict(account_id=1, symbol="AAPL", quantity=1.5, side=OrderDirection.BUY,
                    order_type=OrderType.MARKET, status=OrderStatus.PENDING, good_for=None)
    defaults.update(kwargs)
    return get_instance(TradingOrder, add_instance(TradingOrder(**defaults)))


def _submitted_request(acct):
    return acct.client.submit_order.call_args[0][0]


# ---------------------------------------------------------------------------
# Time-in-force forcing on fractional MARKET orders
# ---------------------------------------------------------------------------

def test_fractional_market_order_goes_out_as_day_even_when_good_for_is_unset():
    acct = _bare_account()

    acct._submit_order_impl(_saved_order(quantity=1.5, good_for=None))

    assert _submitted_request(acct).time_in_force == TimeInForce.DAY
    assert float(_submitted_request(acct).qty) == 1.5


def test_fractional_market_order_overrides_an_explicit_gtc():
    """A caller asking for GTC on a fractional quantity is asking for a rejection."""
    acct = _bare_account()

    acct._submit_order_impl(_saved_order(quantity=0.25, good_for='gtc'))

    assert _submitted_request(acct).time_in_force == TimeInForce.DAY
    assert float(_submitted_request(acct).qty) == 0.25


def test_whole_share_market_order_keeps_the_existing_gtc_default():
    """The fix must not quietly re-time-in-force every order in the platform."""
    acct = _bare_account()

    acct._submit_order_impl(_saved_order(quantity=3.0, good_for=None))

    assert _submitted_request(acct).time_in_force == TimeInForce.GTC


def test_whole_share_order_with_an_explicit_day_still_goes_out_as_day():
    acct = _bare_account()

    acct._submit_order_impl(_saved_order(quantity=3.0, good_for='day'))

    assert _submitted_request(acct).time_in_force == TimeInForce.DAY


# ---------------------------------------------------------------------------
# Non-MARKET fractional: never sent fractional, retried once at floor(qty)
# ---------------------------------------------------------------------------

def test_fractional_quantity_is_never_sent_on_a_limit_order():
    """Alpaca only accepts fractional on MARKET, so the 1.5 must not reach the wire."""
    acct = _bare_account()
    acct.client.submit_order.return_value = _alpaca_response(order_type="limit")

    acct._submit_order_impl(
        _saved_order(quantity=1.5, order_type=OrderType.BUY_LIMIT,
                     limit_price=100.0, good_for='day'))

    sent_qty = float(_submitted_request(acct).qty)
    assert sent_qty == int(sent_qty), f"fractional qty {sent_qty} reached Alpaca"


def test_fractional_limit_order_is_retried_once_at_floor_qty():
    """The fallback floors -- it never rounds up, which would overspend the target."""
    acct = _bare_account()
    acct.client.submit_order.return_value = _alpaca_response(order_type="limit")
    order = _saved_order(quantity=1.5, order_type=OrderType.BUY_LIMIT,
                         limit_price=100.0, good_for='day')

    result = acct._submit_order_impl(order)

    assert acct.client.submit_order.call_count == 1
    assert float(_submitted_request(acct).qty) == 1.0
    # The ledger has to agree with what the broker was actually given.
    assert result is not None
    assert get_instance(TradingOrder, order.id).quantity == 1.0


def test_fractional_stop_limit_leg_also_floors():
    """Not limit-specific: a TP/SL leg sized from a fractional position hits this too."""
    acct = _bare_account()
    acct.client.submit_order.return_value = _alpaca_response(order_type="stop_limit")

    acct._submit_order_impl(
        _saved_order(quantity=4.25, order_type=OrderType.SELL_STOP_LIMIT,
                     side=OrderDirection.SELL, stop_price=90.0, limit_price=89.5))

    assert float(_submitted_request(acct).qty) == 4.0


def test_whole_share_limit_order_is_untouched_by_the_fractional_path():
    acct = _bare_account()
    acct.client.submit_order.return_value = _alpaca_response(order_type="limit")

    acct._submit_order_impl(
        _saved_order(quantity=7.0, order_type=OrderType.BUY_LIMIT, limit_price=100.0))

    assert float(_submitted_request(acct).qty) == 7.0


# ---------------------------------------------------------------------------
# floor(qty) == 0 is a SKIP, not a failure
# ---------------------------------------------------------------------------

def test_fractional_limit_order_that_floors_to_zero_is_skipped_not_failed():
    """0.4 shares floors to nothing. No broker round-trip, and crucially NOT an
    ERROR: nothing was rejected and nothing is wrong with the account -- there was
    simply no whole share left to trade."""
    acct = _bare_account()
    order = _saved_order(quantity=0.4, order_type=OrderType.BUY_LIMIT,
                         limit_price=100.0, good_for='day')

    result = acct._submit_order_impl(order)

    acct.client.submit_order.assert_not_called()
    assert result is None  # nothing was placed, so no order to chain TP/SL onto

    stored = get_instance(TradingOrder, order.id)
    assert stored.status != OrderStatus.ERROR
    assert stored.status == OrderStatus.CANCELED
    assert "skipped" in (stored.comment or "").lower()
    # The reason has to be legible in the Pending Orders UI, not only in the log.
    assert "fractional" in (stored.comment or "").lower()


def test_a_skipped_order_does_not_keep_the_fractional_quantity_as_if_it_were_live():
    """The row must not sit there claiming 0.4 shares are working at the broker."""
    acct = _bare_account()
    order = _saved_order(quantity=0.4, order_type=OrderType.SELL_LIMIT,
                         side=OrderDirection.SELL, limit_price=100.0)

    acct._submit_order_impl(order)

    assert get_instance(TradingOrder, order.id).broker_order_id is None
