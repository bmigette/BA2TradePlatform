"""Unit tests for TastyTradeAccount against a MOCKED tastytrade SDK (12.0.2).

There is no TastyTrade account in the live database, so nothing here talks to a
broker. Broker responses are either REAL SDK pydantic objects (where a validator or
a sign convention is part of what is being tested) or SimpleNamespace stand-ins
(where the real model has 40+ required fields and the code only reads a handful).

Two SDK traps these tests exist to guard:
  * Account.place_order(session, order, dry_run=True) -- dry_run DEFAULTS TO TRUE
    (tastytrade/account.py:877). Real submissions must pass dry_run=False.
  * NewOrder.price_effect is a computed field derived from the SIGN of `price`
    (order.py:264-276): negative = debit. It must never be set by hand.
"""
import asyncio
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tastytrade.order import (
    BuyingPowerEffect,
    FeeCalculation,
    InstrumentType as TTInstrumentType,
    Leg,
    Message,
    OrderAction,
    OrderStatus as TTOrderStatus,
    OrderTimeInForce,
    OrderType as TTOrderType,
    PlacedOrder,
    PlacedOrderResponse,
    FillInfo,
)
from tastytrade.utils import PriceEffect

from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount


# ---------------------------------------------------------------------------
# SHARED BROKER DOUBLES  (used by every task in this module)
# ---------------------------------------------------------------------------

def _sync_run(coro):
    """Drive a coroutine to completion.

    Test stand-in for TastyTradeAccount._run_async, which in production hands the
    coroutine to a persistent background event loop. Tests that exercise _run_async
    ITSELF build a real loop instead (see _looped_account).
    """
    return asyncio.run(coro)


def _bare_account(settings=None):
    """A TastyTradeAccount with no __init__: no network, no DB settings lookup."""
    acct = object.__new__(TastyTradeAccount)
    acct.id = 1
    acct._authentication_error = None
    acct._session = SimpleNamespace(label="tasty-session")
    acct._account = SimpleNamespace(account_number="5WX00000", margin_or_cash="Margin",
                                    account_type_name="Individual")
    acct._loop = None
    acct._loop_thread = None
    acct._settings_cache = dict(settings or {})
    acct._run_async = _sync_run
    with TastyTradeAccount._CACHE_LOCK:
        TastyTradeAccount._GLOBAL_PRICE_CACHE[acct.id] = {}
    return acct


def _balances(**overrides):
    """Stand-in for tastytrade AccountBalance (the real model has 45 required fields)."""
    data = dict(
        cash_balance=Decimal("25000"),
        equity_buying_power=Decimal("50000"),
        derivative_buying_power=Decimal("25000"),
        long_equity_value=Decimal("75000"),
        short_equity_value=Decimal("0"),
        margin_equity=Decimal("100000"),
        maintenance_requirement=Decimal("18750"),
        net_liquidating_value=Decimal("100000"),
        cash_available_to_withdraw=Decimal("25000"),
        pending_cash=Decimal("0"),
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _tt_position(symbol="AAPL", quantity="10", direction="Long", average_open_price="140",
                 close_price="150", mark_price="155", multiplier=1,
                 instrument_type=TTInstrumentType.EQUITY, realized_day_gain="3"):
    """Stand-in for tastytrade CurrentPosition."""
    return SimpleNamespace(
        symbol=symbol,
        quantity=Decimal(quantity),
        quantity_direction=direction,
        average_open_price=Decimal(average_open_price),
        close_price=Decimal(close_price),
        mark_price=Decimal(mark_price),
        multiplier=multiplier,
        instrument_type=instrument_type,
        realized_day_gain=Decimal(realized_day_gain),
    )


def _placed_order(order_id=987654, symbol="AAPL", status=TTOrderStatus.RECEIVED,
                  order_type=TTOrderType.MARKET, action=OrderAction.BUY_TO_OPEN,
                  size="10", external_identifier=None, fills=None, price=None,
                  time_in_force=OrderTimeInForce.DAY):
    """A REAL tastytrade PlacedOrder -- the mapping code must survive its validators."""
    leg = Leg(instrument_type=TTInstrumentType.EQUITY, symbol=symbol,
              action=action, quantity=Decimal(size), fills=fills)
    return PlacedOrder(
        account_number="5WX00000",
        time_in_force=time_in_force,
        order_type=order_type,
        underlying_symbol=symbol,
        underlying_instrument_type=TTInstrumentType.EQUITY,
        status=status,
        cancellable=True,
        editable=False,
        edited=False,
        updated_at=datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc),
        received_at=datetime(2026, 8, 20, 14, 29, tzinfo=timezone.utc),
        legs=[leg],
        id=order_id,
        size=Decimal(size),
        price=price,
        external_identifier=external_identifier,
    )


def _fill(quantity="10", fill_price="150.25"):
    return FillInfo(fill_id="f-1", quantity=Decimal(quantity), fill_price=Decimal(fill_price),
                    filled_at=datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc))


def _placed_order_response(order, change_in_buying_power="-1500",
                           isolated_requirement="1500", total_fees="0.03",
                           warnings=None, errors=None):
    """A REAL PlacedOrderResponse. change_in_buying_power is SIGNED: a buy is negative."""
    bpe = BuyingPowerEffect(
        change_in_margin_requirement=Decimal("1500"),
        change_in_buying_power=Decimal(change_in_buying_power),
        current_buying_power=Decimal("10000"),
        new_buying_power=Decimal("8500"),
        isolated_order_margin_requirement=Decimal(isolated_requirement),
        is_spread=False,
        impact=Decimal("1500"),
        effect=PriceEffect.DEBIT,
    )
    return PlacedOrderResponse(
        buying_power_effect=bpe,
        order=order,
        fee_calculation=FeeCalculation(
            regulatory_fees=Decimal("0.01"), clearing_fees=Decimal("0.02"),
            commission=Decimal("0"), proprietary_index_option_fees=Decimal("0"),
            total_fees=Decimal(total_fees)),
        warnings=[Message(code="w", message=w) for w in (warnings or [])],
        errors=[Message(code="e", message=e) for e in (errors or [])],
    )


class _FakeEquity:
    """Stand-in for tastytrade.instruments.Equity.

    Only the two fields the account code reads, plus build_leg -- which returns a
    REAL SDK Leg so NewOrder validation is genuinely exercised.
    """

    def __init__(self, symbol, is_fractional_quantity_eligible=True):
        self.symbol = symbol
        self.instrument_type = TTInstrumentType.EQUITY
        self.is_fractional_quantity_eligible = is_fractional_quantity_eligible

    def build_leg(self, quantity, action):
        return Leg(instrument_type=TTInstrumentType.EQUITY, symbol=self.symbol,
                   quantity=quantity, action=action)


# ---------------------------------------------------------------------------
# Sandbox flag
# ---------------------------------------------------------------------------

def test_is_sandbox_with_string_none_stored_returns_false():
    """A legacy row holding the literal string "None" must NOT select the sandbox.

    bool("None") is True, so the old `self.settings.get("is_test", False)` would
    point a production account at TastyTrade's certification environment.
    """
    acct = _bare_account(settings={"is_test": "None"})
    assert acct._is_sandbox() is False


def test_is_sandbox_with_unsaved_setting_returns_interface_default():
    """A never-saved key is seeded to None by the settings property; the declared
    default (False) must still apply."""
    acct = _bare_account(settings={"is_test": None})
    assert acct._is_sandbox() is False


def test_is_sandbox_with_saved_true_returns_true():
    acct = _bare_account(settings={"is_test": True})
    assert acct._is_sandbox() is True


# ---------------------------------------------------------------------------
# _run_async
# ---------------------------------------------------------------------------

def _looped_account():
    """A bare account with a REAL persistent event loop, for testing _run_async itself."""
    acct = object.__new__(TastyTradeAccount)
    acct.id = 7
    acct._authentication_error = None
    acct._session = SimpleNamespace(label="tasty-session")
    acct._account = SimpleNamespace(account_number="5WX00000")
    acct._settings_cache = {}
    acct._loop = asyncio.new_event_loop()
    acct._loop_thread = threading.Thread(target=acct._loop.run_forever, daemon=True)
    acct._loop_thread.start()
    return acct


def _stop_loop(acct):
    acct._loop.call_soon_threadsafe(acct._loop.stop)
    acct._loop_thread.join(timeout=5)


def test_run_async_returns_the_coroutine_result():
    acct = _looped_account()
    try:
        async def _work():
            return {"items": [1, 2, 3]}

        assert acct._run_async(_work()) == {"items": [1, 2, 3]}
    finally:
        _stop_loop(acct)


def test_run_async_raises_timeout_error_naming_the_budget():
    """A slow SDK call must fail as an ERROR that says it timed out, not silently
    bubble a bare concurrent.futures timeout that every caller turns into `[]`."""
    acct = _looped_account()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            acct._run_async(asyncio.sleep(5), timeout=0.05)
        assert "timed out after 0.05" in str(excinfo.value)
    finally:
        _stop_loop(acct)


def test_run_async_default_budget_exceeds_the_old_thirty_second_limit():
    """Paginated history calls routinely exceed 30s; the default must be generous."""
    assert TastyTradeAccount._ASYNC_TIMEOUT_SECONDS >= 120


# ---------------------------------------------------------------------------
# get_account_info
# ---------------------------------------------------------------------------

def test_get_account_info_publishes_buying_power_from_equity_buying_power():
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(return_value=_balances())

    info = acct.get_account_info()

    assert info["buying_power"] == 50000.0


def test_actual_available_balance_uses_margin_buying_power_not_cash():
    """The expert probe stops at the FIRST of buying_power/cash/cash_balance/
    equity_buying_power. Without a buying_power key it fell through to cash_balance
    and a margin account was sized as if it were a cash account."""
    from ba2_trade_platform.core.interfaces.MarketExpertInterface import MarketExpertInterface

    acct = _bare_account()
    acct._account.get_balances = AsyncMock(
        return_value=_balances(cash_balance=Decimal("25000"),
                               equity_buying_power=Decimal("50000")))

    assert MarketExpertInterface._get_actual_available_balance(acct) == 50000.0


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

def test_get_positions_excludes_equity_option_rows():
    """An option's market_value is multiplier-scaled (x100). Folding it in with
    equities would blow up every allocation weight."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", instrument_type=TTInstrumentType.EQUITY),
        _tt_position(symbol="AAPL  260918C00150000", multiplier=100,
                     instrument_type=TTInstrumentType.EQUITY_OPTION),
    ])

    positions = acct.get_positions()

    assert [p.symbol for p in positions] == ["AAPL"]


def test_get_positions_returns_none_when_the_fetch_fails():
    """None means FETCH FAILED, [] means genuinely flat. Reconciliation mass-closes
    real transactions if a broker outage is allowed to look like an empty book."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(side_effect=RuntimeError("connection reset"))

    assert acct.get_positions() is None


def test_get_positions_returns_empty_list_when_genuinely_flat():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[])

    assert acct.get_positions() == []


def test_get_positions_derives_change_today_from_the_previous_close():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", quantity="10", close_price="150", mark_price="155"),
    ])

    position = acct.get_positions()[0]

    assert position.change_today == pytest.approx((155.0 - 150.0) / 150.0)


def test_get_positions_intraday_pl_is_mark_minus_close_not_realized_day_gain():
    """realized_day_gain is CLOSED-out P&L for the day; unrealized_intraday_pl is
    the OPEN position's move since the previous close. They are different numbers."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", quantity="10", close_price="150", mark_price="155",
                     realized_day_gain="3"),
    ])

    position = acct.get_positions()[0]

    assert position.unrealized_intraday_pl == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Pagination: the SDK only walks every page when page_offset is None
# (tastytrade/session.py:389-419). Omitting it truncates silently.
# ---------------------------------------------------------------------------

def test_get_orders_requests_all_pages():
    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders()

    assert acct._account.get_order_history.call_args.kwargs["page_offset"] is None


def test_get_filled_trades_requests_all_pages():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[])

    acct.get_filled_trades()

    assert acct._account.get_history.call_args.kwargs["page_offset"] is None


def test_symbols_exist_requests_all_pages():
    acct = _bare_account()
    fake_get = AsyncMock(return_value=[_FakeEquity("AAPL"), _FakeEquity("MSFT")])

    with patch("tastytrade.instruments.Equity.get", new=fake_get):
        result = acct.symbols_exist(["AAPL", "MSFT"])

    assert result == {"AAPL": True, "MSFT": True}
    assert fake_get.call_args.kwargs["page_offset"] is None


# ---------------------------------------------------------------------------
# get_orders status filter
# ---------------------------------------------------------------------------

def test_get_orders_open_filter_asks_the_broker_for_working_statuses_only():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.OPEN)

    requested = acct._account.get_order_history.call_args.kwargs["statuses"]
    assert set(requested) == {
        TTOrderStatus.RECEIVED, TTOrderStatus.ROUTED, TTOrderStatus.IN_FLIGHT,
        TTOrderStatus.LIVE, TTOrderStatus.CONTINGENT,
    }


def test_get_orders_all_filter_sends_no_status_filter():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.ALL)

    assert "statuses" not in acct._account.get_order_history.call_args.kwargs


def test_get_orders_filled_filter_maps_to_the_tastytrade_filled_status():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.FILLED)

    assert acct._account.get_order_history.call_args.kwargs["statuses"] == [TTOrderStatus.FILLED]


# ---------------------------------------------------------------------------
# get_order id handling
# ---------------------------------------------------------------------------

def _capture_errors(monkeypatch):
    """Collect ``logger.error`` messages emitted by the TastyTradeAccount module.

    NOT caplog. Two independent reasons it cannot be used here:
      * ba2_trade_platform.logger installs its own handler and sets
        propagate = False, so caplog's ROOT handler never sees the record; and
      * tests/test_penny_gainers_fix.py:53 replaces
        sys.modules["ba2_trade_platform.logger"] with a MagicMock at import time,
        so under a full-suite collection even re-enabling propagation on the
        object that import yields patches a mock, not the real logger.
    Patching the module-under-test's own ``logger`` is immune to both.
    """
    import sys
    TT = sys.modules[TastyTradeAccount.__module__]
    messages = []
    monkeypatch.setattr(TT.logger, "error", lambda msg, *a, **k: messages.append(str(msg)))
    return messages


def test_get_order_with_a_non_numeric_id_says_so_instead_of_blaming_the_broker(monkeypatch):
    """TastyTrade order ids are integers. A UUID left on a migrated row must be
    reported as a bad id, not logged as 'Error getting order ...' as if the broker
    had failed."""
    errors = _capture_errors(monkeypatch)

    acct = _bare_account()
    acct._account.get_order = AsyncMock()

    assert acct.get_order("6e2d1f3a-0000-4c11-9c1e-8d2f3a4b5c6d") is None

    assert any("is not a TastyTrade order id" in m for m in errors), errors
    acct._account.get_order.assert_not_called()


def test_get_order_with_numeric_id_queries_the_broker_with_an_int():
    """Regression guard: a padded numeric id must still resolve."""
    acct = _bare_account()
    acct._account.get_order = AsyncMock(return_value=_placed_order(order_id=987654))

    acct.get_order(" 987654 ")

    assert acct._account.get_order.call_args.args[1] == 987654


# ---------------------------------------------------------------------------
# PlacedOrder -> TradingOrder mapping
# ---------------------------------------------------------------------------

def test_order_mapping_derives_buy_side_from_the_leg_action():
    """PlacedOrder carries no top-level side -- it is on each leg's OrderAction."""
    from ba2_trade_platform.core.types import OrderDirection

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(action=OrderAction.BUY_TO_OPEN))

    assert mapped.side == OrderDirection.BUY


def test_order_mapping_derives_sell_side_from_a_closing_leg_action():
    from ba2_trade_platform.core.types import OrderDirection

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(action=OrderAction.SELL_TO_CLOSE))

    assert mapped.side == OrderDirection.SELL


def test_order_mapping_makes_a_sell_limit_type_from_side_plus_limit():
    """TastyTrade's order type is non-directional; ours is directional."""
    from ba2_trade_platform.core.types import OrderType

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(order_type=TTOrderType.LIMIT, action=OrderAction.SELL_TO_CLOSE,
                      price=Decimal("161.40")))

    assert mapped.order_type == OrderType.SELL_LIMIT


def test_order_mapping_stores_limit_price_unsigned():
    """PlacedOrder.price is SIGNED (negative = debit). TradingOrder.limit_price is a
    plain price."""
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(order_type=TTOrderType.LIMIT, action=OrderAction.BUY_TO_OPEN,
                      price=Decimal("-142.50")))

    assert mapped.limit_price == 142.50


def test_order_mapping_summarises_fills_into_quantity_and_average_price():
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(status=TTOrderStatus.FILLED, size="10",
                      fills=[_fill(quantity="4", fill_price="150.00"),
                             _fill(quantity="6", fill_price="151.00")]))

    assert mapped.filled_qty == 10.0
    assert mapped.open_price == pytest.approx(150.6)


def test_order_mapping_leaves_open_price_none_when_nothing_filled():
    """No fabricated fill price -- None means 'not filled', never zero."""
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(_placed_order(status=TTOrderStatus.LIVE))

    assert mapped.filled_qty == 0.0
    assert mapped.open_price is None


def test_order_mapping_translates_broker_status_to_platform_status():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(status=TTOrderStatus.CANCEL_REQUESTED))

    assert mapped.status == OrderStatus.PENDING_CANCEL


def test_get_orders_returns_trading_orders_not_raw_broker_objects():
    from ba2_trade_platform.core.models import TradingOrder

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=1), _placed_order(order_id=2)])

    orders = acct.get_orders()

    assert len(orders) == 2
    assert all(isinstance(o, TradingOrder) for o in orders)
    assert [o.broker_order_id for o in orders] == ["1", "2"]


def test_get_order_returns_a_trading_order():
    from ba2_trade_platform.core.models import TradingOrder

    acct = _bare_account()
    acct._account.get_order = AsyncMock(return_value=_placed_order(order_id=987654))

    order = acct.get_order("987654")

    assert isinstance(order, TradingOrder)
    assert order.broker_order_id == "987654"


# ---------------------------------------------------------------------------
# Order submission
# ---------------------------------------------------------------------------

def _tt_trading_order(**kwargs):
    """A PERSISTED TradingOrder owned by a persisted TastyTrade AccountDefinition."""
    from tests.factories import create_account_definition, create_trading_order
    from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType

    account_def = create_account_definition(name="TastyTrade Test", provider="TastyTrade")
    defaults = dict(symbol="AAPL", quantity=3.0, side=OrderDirection.BUY,
                    order_type=OrderType.MARKET, status=OrderStatus.PENDING,
                    good_for="day")
    defaults.update(kwargs)
    order = create_trading_order(account_id=account_def.id, **defaults)
    return account_def, order


def test_submit_order_impl_places_a_live_order_and_records_the_broker_id():
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(
            _placed_order(order_id=987654, status=TTOrderStatus.RECEIVED)))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        result = acct._submit_order_impl(order)

    assert result.broker_order_id == "987654"
    assert result.status == OrderStatus.NEW  # TastyTrade "Received"


def test_submit_order_impl_passes_dry_run_false_explicitly():
    """place_order's dry_run parameter DEFAULTS TO True (tastytrade/account.py:877)."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order()))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order)

    assert acct._account.place_order.call_args.kwargs["dry_run"] is False


def test_submit_order_impl_builds_a_buy_to_open_leg_tagged_with_our_row_id():
    account_def, order = _tt_trading_order(quantity=3.0)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order()))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order)

    sent = acct._account.place_order.call_args.args[1]
    assert sent.legs[0].action == OrderAction.BUY_TO_OPEN
    assert sent.legs[0].quantity == Decimal("3")
    assert sent.time_in_force == OrderTimeInForce.DAY
    assert sent.order_type == TTOrderType.MARKET
    # external_identifier is TastyTrade's client_order_id equivalent -- refresh_orders
    # matches on it.
    assert sent.external_identifier == str(order.id)


def test_submit_order_impl_closing_sell_uses_sell_to_close():
    from ba2_trade_platform.core.types import OrderDirection

    account_def, order = _tt_trading_order(side=OrderDirection.SELL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order()))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order, is_closing_order=True)

    assert acct._account.place_order.call_args.args[1].legs[0].action == OrderAction.SELL_TO_CLOSE


def test_build_new_order_prices_a_buy_limit_as_a_negative_debit():
    """NewOrder.price_effect is a COMPUTED field derived from the sign of `price`
    (order.py:264-276): negative = debit. It must never be set by hand."""
    from ba2_trade_platform.core.types import OrderType

    account_def, order = _tt_trading_order(order_type=OrderType.BUY_LIMIT, limit_price=142.5)
    acct = _bare_account()
    acct.id = account_def.id

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        new_order = acct._build_new_order(order)

    assert new_order.price == Decimal("-142.5")
    assert new_order.price_effect == PriceEffect.DEBIT


def test_build_new_order_prices_a_sell_limit_as_a_positive_credit():
    from ba2_trade_platform.core.types import OrderDirection, OrderType

    account_def, order = _tt_trading_order(side=OrderDirection.SELL,
                                           order_type=OrderType.SELL_LIMIT,
                                           limit_price=161.4)
    acct = _bare_account()
    acct.id = account_def.id

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        new_order = acct._build_new_order(order)

    assert new_order.price == Decimal("161.4")
    assert new_order.price_effect == PriceEffect.CREDIT


def test_submit_order_impl_skips_an_order_that_already_has_a_broker_id():
    """Idempotency guard: an order already sent to the broker is never re-sent."""
    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock()

    result = acct._submit_order_impl(order)

    assert result is order
    acct._account.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

def test_cancel_order_by_database_id_deletes_the_broker_order():
    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(return_value=None)

    assert acct.cancel_order(order.id) is True
    assert acct._account.delete_order.call_args.args[1] == 987654


def test_cancel_order_by_broker_id_resolves_the_same_row():
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(return_value=None)

    assert acct.cancel_order("987654") is True
    assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL


def test_cancel_order_marks_pending_cancel_not_canceled():
    """The cancel has only been REQUESTED. refresh_orders promotes it once the broker
    confirms -- a dependent replacement must not fire before the qty is released."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(return_value=None)

    acct.cancel_order(order.id)

    assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL


def test_cancel_order_without_a_broker_id_fails_without_calling_the_broker():
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock()

    assert acct.cancel_order(order.id) is False
    acct._account.delete_order.assert_not_called()


def test_cancel_order_for_an_unknown_id_returns_false():
    account_def, _order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock()

    assert acct.cancel_order("999999999") is False
    acct._account.delete_order.assert_not_called()


# ---------------------------------------------------------------------------
# Trading-capable class wiring
# ---------------------------------------------------------------------------

def test_tastytrade_account_is_a_trading_account_interface():
    from ba2_trade_platform.core.interfaces import AccountInterface

    assert issubclass(TastyTradeAccount, AccountInterface)


def test_every_abstract_method_is_implemented_so_the_class_can_be_constructed():
    """object.__new__ on an ABC raises unless every @abstractmethod is implemented."""
    assert object.__new__(TastyTradeAccount) is not None


def test_supports_trading_reads_true_from_the_provider_registry_class():
    """ui/pages/settings.py:1435 reads it from the CLASS via the provider registry."""
    from ba2_trade_platform.modules.accounts import providers

    assert getattr(providers["TastyTrade"], "supports_trading", True) is True


def test_supports_trading_reads_true_from_an_instance():
    """core/TradeManager.py:921 and :1223 read it from the INSTANCE."""
    assert getattr(_bare_account(), "supports_trading", True) is True


def test_supports_trading_is_not_pinned_on_the_class_itself():
    """A local pin is what made the class and instance reads disagree; inherit it."""
    assert "supports_trading" not in TastyTradeAccount.__dict__


def test_get_account_info_reports_trading_support():
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(return_value=_balances())

    assert acct.get_account_info()["supports_trading"] is True


def test_modify_order_is_reported_as_unsupported():
    assert _bare_account().modify_order("987654") is None


def test_tp_sl_adjustment_raises_not_implemented():
    """RETURNING False IS A LIE and it opened naked positions.

    AccountInterface.submit_order detects "this broker cannot attach a protective
    leg" with `except NotImplementedError` (AccountInterface.py:355-381). These
    stubs returned False, so that guard branch was dead code: submit_order swallowed
    the False, returned the successful ENTRY order, and the caller that asked for a
    stop got a live, unprotected position reported as success.
    """
    from tests.factories import create_transaction

    acct = _bare_account()
    transaction = create_transaction(symbol="AAPL")

    with pytest.raises(NotImplementedError):
        acct.adjust_tp(transaction, 160.0)
    with pytest.raises(NotImplementedError):
        acct.adjust_sl(transaction, 130.0)
    with pytest.raises(NotImplementedError):
        acct.adjust_tp_sl(transaction, 160.0, 130.0)


def test_tastytrade_declares_that_it_cannot_place_protective_legs():
    """The capability flag submit_order checks BEFORE opening anything."""
    assert TastyTradeAccount.supports_protective_legs is False


def test_submit_order_impl_raises_when_asked_for_a_native_complex_order():
    """The wash-trade gate sets use_complex_order=True when it has a TP/SL to attach.
    This used to be silently IGNORED: a plain order went out, submit_order then skipped
    the adjust block entirely (AccountInterface.py:354), and the position ended up with
    no complex legs AND no separate legs."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock()

    with pytest.raises(NotImplementedError):
        acct._submit_order_impl(order, sl_price=140.0, use_complex_order=True)

    acct._account.place_order.assert_not_called()


def test_submit_order_impl_raises_when_handed_a_protective_price():
    """Reaching here with a tp/sl means submit_order's capability guard was bypassed.
    Warning and sending the bare entry anyway is exactly the naked-position bug."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock()

    with pytest.raises(NotImplementedError):
        acct._submit_order_impl(order, sl_price=140.0)

    acct._account.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Submission failure handling
# ---------------------------------------------------------------------------

def test_submit_order_impl_marks_the_row_error_with_the_broker_message():
    """A rejected order must not sit at PENDING forever with no reason recorded."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        side_effect=RuntimeError("preflight failed: account is restricted"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        result = acct._submit_order_impl(order)

    assert result is None
    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.ERROR
    assert "account is restricted" in stored.comment


# ---------------------------------------------------------------------------
# refresh_orders
# ---------------------------------------------------------------------------

def test_refresh_orders_promotes_a_filled_order_and_records_the_fill():
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(quantity=10.0, broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                      external_identifier=str(order.id),
                      fills=[_fill(quantity="10", fill_price="150.25")]),
    ])

    assert acct.refresh_orders() is True
    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.FILLED
    assert stored.filled_qty == 10.0
    assert stored.open_price == pytest.approx(150.25)


def test_refresh_orders_backfills_broker_order_id_from_external_identifier():
    """external_identifier is our own row id -- TastyTrade's client_order_id."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder

    account_def, order = _tt_trading_order(quantity=10.0)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.LIVE, size="10",
                      external_identifier=str(order.id)),
    ])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).broker_order_id == "987654"


def test_refresh_orders_leaves_an_order_absent_from_the_response_untouched():
    """TastyTrade's order history is paginated and date-windowed, so absence is NOT
    evidence of cancellation. Unlike Alpaca's refresh, nothing is auto-canceled."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="111111",
                                           status=OrderStatus.ACCEPTED)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).status == OrderStatus.ACCEPTED


def test_refresh_orders_returns_false_when_the_fetch_fails():
    account_def, _order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    assert acct.refresh_orders() is False


# ---------------------------------------------------------------------------
# refresh_positions
# ---------------------------------------------------------------------------

def test_refresh_positions_returns_false_when_the_fetch_fails():
    """A stub that always returns True tells callers the book was confirmed when it
    was not."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(side_effect=RuntimeError("connection reset"))

    assert acct.refresh_positions() is False


def test_refresh_positions_returns_true_when_the_book_was_read():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[_tt_position(symbol="AAPL")])

    assert acct.refresh_positions() is True


def test_refresh_positions_returns_true_for_a_genuinely_flat_account():
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[])

    assert acct.refresh_positions() is True


# ---------------------------------------------------------------------------
# preview_order_impact
# ---------------------------------------------------------------------------

def test_preview_order_impact_passes_dry_run_true_explicitly():
    """It must never send a live order -- and must not rely on the SDK default."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1)))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct.preview_order_impact(order)

    assert acct._account.place_order.call_args.kwargs["dry_run"] is True


def test_preview_order_impact_turns_a_signed_debit_into_a_positive_bp_cost():
    """BuyingPowerEffect.change_in_buying_power is NEGATIVE for a buy (order.py:381)."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            change_in_buying_power="-1500",
                                            isolated_requirement="1500",
                                            total_fees="0.03"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.change_in_buying_power == -1500.0
    assert impact.bp_cost == 1500.0
    assert impact.margin_requirement == 1500.0
    assert impact.estimated_fees == pytest.approx(0.03)
    assert impact.accepted is True


def test_preview_order_impact_marks_a_rejected_preview_as_not_accepted():
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            errors=["insufficient buying power"]))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.accepted is False
    assert any("insufficient buying power" in e for e in impact.errors)


def test_preview_order_impact_returns_none_when_the_preview_call_fails():
    """None means 'no precheck available', NOT 'the order is free'. It must never be
    fabricated as a zero impact."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        assert acct.preview_order_impact(order) is None


# ---------------------------------------------------------------------------
# get_account_snapshot
# ---------------------------------------------------------------------------

def test_account_snapshot_maps_a_margin_account():
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    acct._account.get_balances = AsyncMock(return_value=_balances())

    snapshot = acct.get_account_snapshot()

    assert snapshot.cash == 25000.0
    assert snapshot.buying_power == 50000.0
    assert snapshot.net_liquidation == 100000.0
    assert snapshot.equity == 100000.0
    assert snapshot.long_market_value == 75000.0
    assert snapshot.is_margin_account is True
    assert snapshot.margin_multiplier == 2.0


def test_account_snapshot_of_a_cash_account_has_no_leverage():
    acct = _bare_account()
    acct._account.margin_or_cash = "Cash"
    acct._account.get_balances = AsyncMock(return_value=_balances())

    snapshot = acct.get_account_snapshot()

    assert snapshot.is_margin_account is False
    assert snapshot.margin_multiplier == 1.0


def test_account_snapshot_negates_tastytrades_positive_short_magnitude():
    """AccountSnapshot pins short_market_value as NEGATIVE while shorts are held
    (the Alpaca convention), but TastyTrade reports short-equity-value as a POSITIVE
    magnitude. If the adapter passes it through, gross exposure becomes
    broker-dependent — and every other fixture here uses a zero short, which is
    sign-agnostic and would never catch it."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    balances = _balances()
    balances.short_equity_value = Decimal("12000")
    acct._account.get_balances = AsyncMock(return_value=balances)

    snapshot = acct.get_account_snapshot()

    assert snapshot.short_market_value == -12000.0


def test_account_snapshot_leaves_an_absent_short_value_as_none():
    """None means 'the broker did not say', which must not be negated into -0.0."""
    acct = _bare_account()
    balances = _balances()
    balances.short_equity_value = None
    acct._account.get_balances = AsyncMock(return_value=balances)

    snapshot = acct.get_account_snapshot()

    assert snapshot.short_market_value is None


def test_account_snapshot_on_failure_is_all_none_not_zeros():
    """An all-None snapshot is a legitimate 'the broker told us nothing'. Zeros would
    be a fabricated balance, which the caller cannot distinguish from a real one."""
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    snapshot = acct.get_account_snapshot()

    assert snapshot.cash is None
    assert snapshot.buying_power is None
    assert snapshot.net_liquidation is None


# ---------------------------------------------------------------------------
# get_cash_transfers
# ---------------------------------------------------------------------------

def _money_movement(txn_id, sub_type, net_value, transaction_date=date(2026, 8, 3),
                    symbol=None, description="", underlying_symbol=None):
    """Stand-in for a tastytrade `Money Movement` Transaction."""
    return SimpleNamespace(
        id=txn_id,
        transaction_type="Money Movement",
        transaction_sub_type=sub_type,
        net_value=Decimal(net_value),
        transaction_date=transaction_date,
        symbol=symbol,
        underlying_symbol=underlying_symbol,
        description=description,
    )


def test_get_cash_transfers_requests_money_movement_over_all_pages():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[])

    acct.get_cash_transfers(start_date=date(2026, 7, 1), end_date=date(2026, 8, 20))

    kwargs = acct._account.get_history.call_args.kwargs
    assert kwargs["types"] == ["Money Movement"]
    assert kwargs["page_offset"] is None
    assert kwargs["start_date"] == date(2026, 7, 1)
    assert kwargs["end_date"] == date(2026, 8, 20)


def test_get_cash_transfers_reports_a_deposit_as_positive_income():
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_DEPOSIT

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9001, "Deposit", "2500", description="ACH DEPOSIT"),
    ])

    transfers = acct.get_cash_transfers()

    assert len(transfers) == 1
    assert transfers[0].external_id == "9001"
    assert transfers[0].event_type == CASH_TRANSFER_DEPOSIT
    assert transfers[0].amount == 2500.0
    assert transfers[0].is_income is True


def test_get_cash_transfers_nets_dividend_tax_off_the_gross_leg():
    """Gross and tax share (symbol, date). One row is emitted, keeping the GROSS leg's
    own broker id so the (account_id, external_id) idempotency key stays 1:1."""
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_DIVIDEND

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9101, "Dividend", "1.57", underlying_symbol="TIDL",
                        description="TIDAL TRUST II"),
        _money_movement(9102, "Dividend", "-0.24", underlying_symbol="TIDL",
                        description="TIDAL TRUST II"),
    ])

    transfers = acct.get_cash_transfers()

    assert len(transfers) == 1
    assert transfers[0].external_id == "9101"
    assert transfers[0].event_type == CASH_TRANSFER_DIVIDEND
    assert transfers[0].symbol == "TIDL"
    assert transfers[0].amount == pytest.approx(1.33)


def test_get_cash_transfers_reports_a_withdrawal_as_negative_and_not_income():
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_WITHDRAWAL

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9201, "Withdrawal", "-800", description="ACH WITHDRAWAL"),
    ])

    transfers = acct.get_cash_transfers()

    assert transfers[0].event_type == CASH_TRANSFER_WITHDRAWAL
    assert transfers[0].amount == -800.0
    assert transfers[0].is_income is False


def test_get_cash_transfers_ignores_the_drip_reinvestment_leg():
    """A DRIP 'Withdrawal' never left the account -- it bought shares with the dividend
    already recorded. Recording it would double-count the cash going out."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9301, "Withdrawal", "-1.33",
                        description="Cash dividend reinvested into TIDL"),
    ])

    assert acct.get_cash_transfers() == []


def test_get_cash_transfers_returns_empty_list_on_failure():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    assert acct.get_cash_transfers() == []


# ---------------------------------------------------------------------------
# get_symbol_margin_info
# ---------------------------------------------------------------------------

def _margin_report(*entries):
    """Stand-in for tastytrade MarginReport. `groups` legitimately contains EmptyDict
    placeholders, which carry no attributes at all."""
    return SimpleNamespace(groups=list(entries))


def _margin_entry(underlying_symbol, initial_requirement):
    return SimpleNamespace(underlying_symbol=underlying_symbol,
                           initial_requirement=Decimal(initial_requirement))


def _precision(minimum_increment_precision=5, symbol=None,
               instrument_type=TTInstrumentType.EQUITY):
    return SimpleNamespace(instrument_type=instrument_type, value=5, symbol=symbol,
                           minimum_increment_precision=minimum_increment_precision)


def _wire_margin_sources(acct, equities, report, precisions, positions):
    acct._account.get_margin_requirements = AsyncMock(return_value=report)
    acct._account.get_positions = AsyncMock(return_value=positions)
    return (
        patch("tastytrade.instruments.Equity.get", new=AsyncMock(return_value=equities)),
        patch("tastytrade.instruments.get_quantity_decimal_precisions",
              new=AsyncMock(return_value=precisions)),
    )


def test_symbol_margin_info_derives_the_real_rate_for_a_held_symbol():
    """initial_requirement / position notional is the actual Reg-T rate charged."""
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_POSITION

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct,
        equities=[_FakeEquity("AAPL")],
        # 10 shares marked at 155 = 1550 notional; 775 required = a 0.5 rate.
        report=_margin_report(_margin_entry("AAPL", "775")),
        precisions=[_precision(minimum_increment_precision=5)],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == pytest.approx(0.5)
    assert info["AAPL"].bp_factor == pytest.approx(1.0)  # 0.5 rate x 2:1 account
    assert info["AAPL"].source == MARGIN_SOURCE_POSITION


def test_symbol_margin_info_falls_back_to_the_account_multiplier_when_unheld():
    """Unheld symbols get bp_factor == the account multiplier -- exactly the caller's
    own conservative fallback, so nothing is over-committed."""
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_DEFAULT

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("MSFT")], report=_margin_report(),
        precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["MSFT"])

    assert info["MSFT"].bp_factor == 2.0
    assert info["MSFT"].initial_margin_rate is None
    assert info["MSFT"].source == MARGIN_SOURCE_DEFAULT


def test_symbol_margin_info_omits_a_symbol_the_broker_cannot_describe():
    """Omission, not a default -- the caller must know it fell back."""
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[], report=_margin_report(), precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["NOSUCH"])

    assert info == {}


def test_symbol_margin_info_reports_fractionability_and_increment():
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct,
        equities=[_FakeEquity("AAPL", is_fractional_quantity_eligible=True),
                  _FakeEquity("BRKA", is_fractional_quantity_eligible=False)],
        report=_margin_report(), precisions=[_precision(minimum_increment_precision=5)],
        positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL", "BRKA"])

    assert info["AAPL"].fractionable is True
    assert info["AAPL"].min_trade_increment == pytest.approx(1e-5)
    assert info["BRKA"].fractionable is False
    assert info["BRKA"].min_trade_increment == 1.0


def test_symbol_margin_info_skips_empty_margin_report_groups():
    """MarginReport.groups is `list[MarginReportEntry | EmptyDict]` -- the EmptyDict
    placeholders have no attributes at all."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")],
        report=_margin_report(SimpleNamespace(), _margin_entry("AAPL", "775")),
        precisions=[_precision()],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == pytest.approx(0.5)


def test_symbol_margin_info_clamps_a_rate_above_one_to_fully_cash_secured():
    """A requirement ABOVE the position's notional (a concentration/hard-to-borrow
    add-on) must not report a >100% initial margin rate: 1.0 is 'fully cash secured',
    and the account multiplier already carries the leverage. Added beyond the plan --
    dropping the min(1.0, ...) clamp left every planned test green."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")],
        # 1550 notional but 2000 required -> a raw ratio of ~1.29.
        report=_margin_report(_margin_entry("AAPL", "2000")),
        precisions=[_precision()],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == 1.0
    assert info["AAPL"].bp_factor == 2.0


def test_symbol_margin_info_reports_a_cash_account_as_not_marginable():
    """TastyTrade has no per-symbol marginability flag, so `marginable` mirrors the
    ACCOUNT. Added beyond the plan -- hardcoding marginable=True left every planned
    test green, and a cash account cannot borrow against anything."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Cash"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")], report=_margin_report(),
        precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].marginable is False
    assert info["AAPL"].bp_factor == 1.0


def test_symbol_margin_info_normalises_the_requested_symbols():
    """The docstring promises .strip().upper(); the returned dict must be keyed that
    way or every caller lookup misses. Added beyond the plan -- removing the
    normalisation left every planned test green."""
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")], report=_margin_report(),
        precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["  aapl "])

    assert list(info) == ["AAPL"]


# ---------------------------------------------------------------------------
# Bulk quotes
# ---------------------------------------------------------------------------

def _market_data(symbol, bid="149.90", ask="150.10", mid="150.00", last="150.05",
                 close="148.00"):
    return SimpleNamespace(
        symbol=symbol,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        mid=Decimal(mid) if mid is not None else None,
        last=Decimal(last) if last is not None else None,
        close=Decimal(close) if close is not None else None,
    )


def test_bulk_quotes_chunk_at_the_hundred_symbol_api_limit():
    """get_market_data_by_type's COMBINED limit across all types is 100 per call."""
    acct = _bare_account()
    symbols = [f"SYM{i:03d}" for i in range(150)]
    bulk = AsyncMock(side_effect=lambda session, equities: [_market_data(s) for s in equities])

    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        acct._get_instrument_current_price_impl(symbols, price_type="mid")

    chunk_sizes = [len(call.kwargs["equities"]) for call in bulk.call_args_list]
    assert chunk_sizes == [100, 50]


def test_bulk_quotes_return_the_requested_price_type():
    acct = _bare_account()
    bulk = AsyncMock(return_value=[_market_data("AAPL", bid="149.90", ask="150.10")])

    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        prices = acct._get_instrument_current_price_impl(["AAPL"], price_type="ask")

    assert prices == {"AAPL": 150.10}


def test_bulk_quotes_leave_a_missing_symbol_as_none():
    """No fabricated price for a symbol the broker did not return."""
    acct = _bare_account()
    bulk = AsyncMock(return_value=[_market_data("AAPL")])

    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        prices = acct._get_instrument_current_price_impl(["AAPL", "NOSUCH"], price_type="mid")

    assert prices["AAPL"] == 150.00
    assert prices["NOSUCH"] is None


def test_bulk_quotes_survive_a_failing_chunk():
    acct = _bare_account()
    symbols = [f"SYM{i:03d}" for i in range(150)]

    def _fail_first(session, equities):
        if equities[0] == "SYM000":
            raise RuntimeError("gateway timeout")
        return [_market_data(s) for s in equities]

    bulk = AsyncMock(side_effect=_fail_first)
    with patch("tastytrade.market_data.get_market_data_by_type", new=bulk):
        prices = acct._get_instrument_current_price_impl(symbols, price_type="mid")

    assert prices["SYM000"] is None
    assert prices["SYM100"] == 150.00
