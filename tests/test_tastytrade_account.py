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
