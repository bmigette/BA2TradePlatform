"""Unit tests for TastyTradeAccount against a MOCKED tastytrade SDK (12.0.2).

There is no TastyTrade account in the live database, so nothing here talks to a
broker. Broker responses are either REAL SDK pydantic objects (where a validator or
a sign convention is part of what is being tested) or SimpleNamespace stand-ins
(where the real model has 40+ required fields and the code only reads a handful).

THREE SDK traps these tests exist to guard:
  * Account.place_order(session, order, dry_run=True) -- dry_run DEFAULTS TO TRUE
    (tastytrade/account.py:877). Real submissions must pass dry_run=False.
  * NewOrder.price_effect is a computed field derived from the SIGN of `price`
    (order.py:264-276): negative = debit. It must never be set by hand.
  * `tastytrade.utils.set_sign_for` (utils.py:292-305) does NOT normalise a sign, it
    CREATES a negative one: `if data["<key>-effect"] == DEBIT: data[key] = -abs(...)`.
    It runs in a `model_validator(mode="before")`, which means CONSTRUCTING THESE
    MODELS WITH PYTHON KWARGS BYPASSES IT ENTIRELY. A margin requirement and a fee are
    both debits, so in production they arrive NEGATIVE (-1500, -0.03) while a kwargs
    fixture hands the code a cheerful +1500 / +0.03 and every sign bug stays invisible.
    So BuyingPowerEffect / FeeCalculation / MarginReport(Entry) are built here from
    RAW DASHERIZED PAYLOADS INCLUDING THE `-effect` KEYS -- see
    `_buying_power_effect_payload`, `_fee_calculation_payload` and `_margin_entry`.
"""
import asyncio
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tastytrade.order import (
    InstrumentType as TTInstrumentType,
    Leg,
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


def _buying_power_effect_payload(change_in_buying_power="1500", bp_effect="Debit",
                                 isolated_requirement="1500",
                                 change_in_margin_requirement="900"):
    """RAW dasherized BuyingPowerEffect payload, WITH the `-effect` keys.

    Every value here is an unsigned MAGNITUDE, exactly as TastyTrade sends it on the
    wire; the sign is applied by `set_sign_for` inside the model's
    `model_validator(mode="before")` (order.py:381-393). For a BUY, both
    `change-in-buying-power` and `isolated-order-margin-requirement` are DEBITS, so the
    parsed model carries -1500 for each -- which is exactly what production sees and
    what a kwargs-built fixture never showed.

    `change-in-margin-requirement` deliberately DIFFERS from
    `isolated-order-margin-requirement`: they are different numbers on a real account
    (the first is the whole account's margin delta, the second is this order in
    isolation), and while the fixture made them equal, sourcing OrderImpact's
    `margin_requirement` from the wrong one was invisible.
    """
    return {
        "change-in-margin-requirement": change_in_margin_requirement,
        "change-in-margin-requirement-effect": "Debit",
        "change-in-buying-power": change_in_buying_power,
        "change-in-buying-power-effect": bp_effect,
        "current-buying-power": "10000",
        "current-buying-power-effect": "Credit",
        "new-buying-power": "8500",
        "new-buying-power-effect": "Credit",
        "isolated-order-margin-requirement": isolated_requirement,
        "isolated-order-margin-requirement-effect": "Debit",
        "is-spread": False,
        "impact": "1500",
        "effect": "Debit",
    }


def _fee_calculation_payload(total_fees="0.03"):
    """RAW dasherized FeeCalculation payload. A fee is a DEBIT, so the parsed
    `total_fees` is NEGATIVE (order.py:407-419)."""
    return {
        "regulatory-fees": "0.01",
        "regulatory-fees-effect": "Debit",
        "clearing-fees": "0.02",
        "clearing-fees-effect": "Debit",
        "commission": "0",
        "commission-effect": "None",
        "proprietary-index-option-fees": "0",
        "proprietary-index-option-fees-effect": "None",
        "total-fees": total_fees,
        "total-fees-effect": "Debit",
    }


def _placed_order_response(order, change_in_buying_power="1500", bp_effect="Debit",
                           isolated_requirement="1500", total_fees="0.03",
                           warnings=None, errors=None,
                           change_in_margin_requirement="900"):
    """A REAL PlacedOrderResponse parsed from a RAW dasherized payload.

    `model_validate` (not kwargs) so the nested BuyingPowerEffect / FeeCalculation
    `model_validator(mode="before")` actually runs and applies `set_sign_for`. The
    numeric arguments are unsigned MAGNITUDES; `bp_effect` picks Debit (a buy, which
    parses NEGATIVE) or Credit (a sell).
    """
    return PlacedOrderResponse.model_validate({
        "buying-power-effect": _buying_power_effect_payload(
            change_in_buying_power=change_in_buying_power, bp_effect=bp_effect,
            isolated_requirement=isolated_requirement,
            change_in_margin_requirement=change_in_margin_requirement),
        "order": order,
        "fee-calculation": _fee_calculation_payload(total_fees=total_fees),
        "warnings": [{"code": "w", "message": w} for w in (warnings or [])],
        "errors": [{"code": "e", "message": e} for e in (errors or [])],
    })


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


def test_get_positions_maps_a_long_to_the_buy_side():
    """N05. Nothing asserted Position.side at all. Inverting it makes every long look
    like a short: reconciliation would try to close it the wrong way, and exposure
    would be counted with the wrong sign."""
    from ba2_trade_platform.core.types import OrderDirection

    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", direction="Long")])

    assert acct.get_positions()[0].side == OrderDirection.BUY


def test_get_positions_maps_a_short_to_the_sell_side():
    """N05."""
    from ba2_trade_platform.core.types import OrderDirection

    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", direction="Short")])

    assert acct.get_positions()[0].side == OrderDirection.SELL


def test_get_positions_reports_quantity_as_a_positive_magnitude():
    """N06/N07. Position.qty is a MAGNITUDE and `side` carries the direction -- the
    Alpaca convention every consumer here assumes. A signed qty would make
    market_value, cost_basis and every allocation weight negative for a short.
    qty_available mirrors it: TastyTrade publishes no separate held-for-orders
    quantity, so reporting anything else would understate what can be closed."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", quantity="-10", direction="Short",
                     average_open_price="140", close_price="150", mark_price="155")])

    position = acct.get_positions()[0]

    assert position.qty == 10.0
    assert position.qty_available == 10.0
    assert position.cost_basis == pytest.approx(1400.0)
    assert position.market_value == pytest.approx(1550.0)


def test_get_positions_treats_an_absent_multiplier_as_one():
    """N08. `multiplier` scales cost basis and market value. Defaulting an absent one
    to anything but 1 would inflate an equity position's notional by that factor."""
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(return_value=[
        _tt_position(symbol="AAPL", quantity="10", average_open_price="140",
                     mark_price="155", multiplier=None)])

    position = acct.get_positions()[0]

    assert position.cost_basis == pytest.approx(1400.0)
    assert position.market_value == pytest.approx(1550.0)


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
        TTOrderStatus.LIVE, TTOrderStatus.CONTINGENT, TTOrderStatus.CANCEL_REQUESTED,
        TTOrderStatus.REPLACE_REQUESTED,
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


def test_get_orders_closed_filter_asks_the_broker_for_finished_statuses_only():
    """M49. The CLOSED set was pinned nowhere, so dropping or adding a member was
    invisible."""
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.CLOSED)

    requested = acct._account.get_order_history.call_args.kwargs["statuses"]
    assert set(requested) == {
        TTOrderStatus.FILLED, TTOrderStatus.CANCELLED, TTOrderStatus.EXPIRED,
        TTOrderStatus.REJECTED, TTOrderStatus.REMOVED, TTOrderStatus.PARTIALLY_REMOVED,
    }


def test_every_mapped_broker_status_is_either_open_or_closed():
    """M11. CANCEL_REQUESTED, REPLACE_REQUESTED and PARTIALLY_REMOVED were in
    _TT_STATUS_MAP but in NEITHER _TT_OPEN_STATUSES nor _TT_CLOSED_STATUSES, so
    get_orders(OPEN) missed an order the broker was still cancelling and
    get_orders(CLOSED) missed one it had partially removed. Every status the adapter
    can translate must be reachable through exactly one of the two filters."""
    open_set = set(TastyTradeAccount._TT_OPEN_STATUSES)
    closed_set = set(TastyTradeAccount._TT_CLOSED_STATUSES)

    assert not (open_set & closed_set), "a status cannot be both working and finished"
    assert open_set | closed_set == set(TastyTradeAccount._TT_STATUS_MAP)
    # ... and the map itself must cover every status the SDK can hand us.
    assert set(TastyTradeAccount._TT_STATUS_MAP) == set(TTOrderStatus)


def test_get_orders_open_filter_includes_an_order_the_broker_is_still_cancelling():
    """M11. A CANCEL_REQUESTED order is still working: the quantity is not released
    and a dependent replacement must not fire yet. Omitting it from the OPEN filter
    made it invisible to every 'what is still live?' query."""
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.get_orders(status=OrderStatus.OPEN)

    requested = acct._account.get_order_history.call_args.kwargs["statuses"]
    assert TTOrderStatus.CANCEL_REQUESTED in requested
    assert TTOrderStatus.REPLACE_REQUESTED in requested


def test_status_filter_refuses_a_platform_status_it_cannot_express():
    """M12. `_tt_statuses_for` returned None -- the SDK's "no filter" sentinel -- for
    any platform status with no TastyTrade equivalent. Combined with page_offset=None
    that silently walked EVERY order ever placed and handed the caller the lot as if
    it were the filtered answer. Fail loudly on a filter we cannot express."""
    from ba2_trade_platform.core.types import OrderStatus

    with pytest.raises(ValueError) as excinfo:
        TastyTradeAccount._tt_statuses_for(OrderStatus.PARTIALLY_FILLED)

    assert "partially_filled" in str(excinfo.value).lower()


def test_get_orders_does_not_return_everything_for_an_unexpressible_filter():
    from ba2_trade_platform.core.types import OrderStatus

    acct = _bare_account()
    acct._account.get_order_history = AsyncMock(return_value=[_placed_order(order_id=1)])

    with pytest.raises(ValueError):
        acct.get_orders(status=OrderStatus.DONE_FOR_DAY)

    acct._account.get_order_history.assert_not_called()


def test_status_filter_treats_none_and_all_as_unfiltered():
    from ba2_trade_platform.core.types import OrderStatus

    assert TastyTradeAccount._tt_statuses_for(None) is None
    assert TastyTradeAccount._tt_statuses_for(OrderStatus.ALL) is None


# ---------------------------------------------------------------------------
# Broker status -> platform status
# ---------------------------------------------------------------------------

def test_an_unmapped_broker_status_records_unknown_not_filled():
    """M40. An unrecognised broker status mapped to FILLED would open a transaction
    and fire the TP/SL legs for an order that never filled. UNKNOWN is the only safe
    answer: it is not an executed status, so nothing downstream acts on it."""
    from ba2_trade_platform.core.types import OrderStatus

    mapped = TastyTradeAccount._map_order_status("Reorganisation Pending")

    assert mapped == OrderStatus.UNKNOWN
    assert mapped not in OrderStatus.get_executed_statuses()


def test_a_missing_broker_status_records_unknown_not_filled():
    """M41. A PlacedOrder with no status at all must not be read as executed."""
    from ba2_trade_platform.core.types import OrderStatus

    mapped = TastyTradeAccount._map_order_status(None)

    assert mapped == OrderStatus.UNKNOWN
    assert mapped not in OrderStatus.get_executed_statuses()


@pytest.mark.parametrize("tt_status,expected", [
    (TTOrderStatus.RECEIVED, "new"),
    (TTOrderStatus.ROUTED, "new"),
    (TTOrderStatus.IN_FLIGHT, "pending_new"),
    (TTOrderStatus.LIVE, "accepted"),
    (TTOrderStatus.FILLED, "filled"),
    (TTOrderStatus.CANCELLED, "canceled"),
    (TTOrderStatus.CANCEL_REQUESTED, "pending_cancel"),
    (TTOrderStatus.REPLACE_REQUESTED, "pending_replace"),
    (TTOrderStatus.EXPIRED, "expired"),
    (TTOrderStatus.REJECTED, "rejected"),
    (TTOrderStatus.REMOVED, "canceled"),
    (TTOrderStatus.PARTIALLY_REMOVED, "canceled"),
])
def test_broker_status_maps_to_the_documented_platform_status(tt_status, expected):
    """Only FILLED may map to FILLED. Every other row is pinned so a mutation that
    widens the mapping toward FILLED is caught."""
    assert TastyTradeAccount._map_order_status(tt_status).value == expected


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


def test_order_mapping_refuses_to_store_a_dry_run_id_as_a_broker_id():
    """N14. A dry run comes back with `id == -1`. Stored as a broker_order_id it would
    make the _submit_order_impl idempotency guard reject the genuine submission that
    follows -- the order would never be sent, and would look as if it had been."""
    acct = _bare_account()

    mapped = acct.tastytrade_order_to_tradingorder(_placed_order(order_id=-1))

    assert mapped.broker_order_id is None


def test_order_mapping_takes_quantity_from_the_order_size_not_the_fills():
    """N15. `quantity` is what was ORDERED; `filled_qty` is what has filled. Reading
    quantity off the fills makes a partially filled order look fully filled -- the
    remaining quantity vanishes from every "how much is still working" sum."""
    acct = _bare_account()

    mapped = acct.tastytrade_order_to_tradingorder(
        _placed_order(status=TTOrderStatus.LIVE, size="10",
                      fills=[_fill(quantity="4", fill_price="150.00")]))

    assert mapped.quantity == 10.0
    assert mapped.filled_qty == 4.0


def test_order_mapping_falls_back_to_the_filled_quantity_when_size_is_absent():
    acct = _bare_account()
    order = _placed_order(status=TTOrderStatus.FILLED, size="10",
                          fills=[_fill(quantity="4", fill_price="150.00")])
    object.__setattr__(order, "size", None)

    assert acct.tastytrade_order_to_tradingorder(order).quantity == 4.0


@pytest.mark.parametrize("legs", [None, []])
def test_side_from_legs_returns_none_rather_than_guessing(legs):
    """N16. There is no top-level side on a PlacedOrder. When no leg yields one, the
    answer is "I do not know" -- defaulting to BUY puts the row on the wrong side of
    the book, and every consumer of TradingOrder.side then acts on it."""
    assert TastyTradeAccount._side_from_legs(legs) is None


def test_side_from_legs_ignores_a_leg_with_no_action():
    acct = _bare_account()
    order = _placed_order()
    object.__setattr__(order, "legs", [SimpleNamespace(action=None)])

    assert acct._side_from_legs(order.legs) is None
    assert acct.tastytrade_order_to_tradingorder(order) is None


def test_get_orders_drops_a_row_whose_side_cannot_be_determined():
    """N16. The undeterminable row is skipped, not defaulted onto the buy side."""
    acct = _bare_account()
    good = _placed_order(order_id=1)
    sideless = _placed_order(order_id=2)
    object.__setattr__(sideless, "legs", [])
    acct._account.get_order_history = AsyncMock(return_value=[good, sideless])

    orders = acct.get_orders()

    assert [o.broker_order_id for o in orders] == ["1"]


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


# ---------------------------------------------------------------------------
# _build_new_order: the stop / stop-limit surface.
#
# Nothing exercised this at all, so a stop-limit could send its stop price AS the
# limit, a stop could go out with no trigger, an unsupported type could quietly
# become a MARKET order and a missing price could become a penny order -- all with
# the suite green.
# ---------------------------------------------------------------------------

def _built_order(**order_kwargs):
    account_def, order = _tt_trading_order(**order_kwargs)
    acct = _bare_account()
    acct.id = account_def.id
    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity(order.symbol))):
        return acct._build_new_order(order)


def test_build_new_order_sends_a_stop_with_its_trigger_and_no_limit_price():
    """N21. A STOP order whose stop_trigger is dropped becomes a resting order with no
    trigger at all -- on TastyTrade a Stop with no stop-trigger is not a stop."""
    from ba2_trade_platform.core.types import OrderType

    new_order = _built_order(order_type=OrderType.BUY_STOP, stop_price=138.0)

    assert new_order.order_type == TTOrderType.STOP
    assert new_order.stop_trigger == Decimal("138.0")
    # A plain stop carries no price: NewOrder.price is the LIMIT, and setting it would
    # turn a stop into a stop-limit that may never fill.
    assert new_order.price is None


def test_build_new_order_sends_a_stop_limit_with_the_trigger_and_the_limit_apart():
    """N20. `stop_trigger` is the trigger and `price` is the LIMIT (SDK: "For a
    stop/stop limit order. If the latter, use price for the limit price"). Sending the
    STOP price as the limit prices the order at the trigger, so a gap through the stop
    leaves it resting unfilled at a price the market has already left."""
    from ba2_trade_platform.core.types import OrderType

    new_order = _built_order(order_type=OrderType.BUY_STOP_LIMIT,
                             stop_price=138.0, limit_price=139.5)

    assert new_order.order_type == TTOrderType.STOP_LIMIT
    assert new_order.stop_trigger == Decimal("138.0")
    # Signed: a BUY is a DEBIT, and the magnitude is the LIMIT, not the trigger.
    assert new_order.price == Decimal("-139.5")
    assert new_order.price_effect == PriceEffect.DEBIT


def test_build_new_order_sends_a_sell_stop_limit_as_a_credit_at_the_limit():
    from ba2_trade_platform.core.types import OrderDirection, OrderType

    new_order = _built_order(side=OrderDirection.SELL,
                             order_type=OrderType.SELL_STOP_LIMIT,
                             stop_price=142.0, limit_price=141.0)

    assert new_order.stop_trigger == Decimal("142.0")
    assert new_order.price == Decimal("141.0")
    assert new_order.price_effect == PriceEffect.CREDIT


@pytest.mark.parametrize("order_type,prices", [
    ("BUY_LIMIT", {}),                              # N23: no limit price
    ("SELL_LIMIT", {}),
    ("BUY_STOP", {}),                               # N21: no stop price
    ("BUY_STOP_LIMIT", {"stop_price": 138.0}),      # stop-limit missing the limit
    ("BUY_STOP_LIMIT", {"limit_price": 139.5}),     # stop-limit missing the stop
])
def test_build_new_order_refuses_an_order_with_a_missing_required_price(order_type, prices):
    """N23. A missing limit price must NOT be silently replaced -- least of all with a
    fabricated 0.01, which sends a real penny order to the broker (platform rule: no
    fallback values for live prices)."""
    from ba2_trade_platform.core.types import OrderType

    with pytest.raises(ValueError):
        _built_order(order_type=getattr(OrderType, order_type), **prices)


@pytest.mark.parametrize("unsupported", ["TRAILING_STOP", "OCO", "OTO"])
def test_build_new_order_refuses_an_order_type_tastytrade_cannot_send(unsupported):
    """N22. Falling through to MARKET turns a resting/conditional order into an
    IMMEDIATE fill at whatever the market is -- the single most expensive way to
    misread an order type."""
    from ba2_trade_platform.core.types import OrderType

    with pytest.raises(ValueError) as excinfo:
        _built_order(order_type=getattr(OrderType, unsupported), limit_price=140.0,
                     stop_price=138.0)

    assert "does not support order type" in str(excinfo.value)


def test_submit_order_impl_does_not_send_an_order_it_cannot_build():
    """The ValueError must stop the submission, not be reported as a broker failure
    after something has already gone out."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus, OrderType

    account_def, order = _tt_trading_order(order_type=OrderType.BUY_LIMIT,
                                           limit_price=None)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock()

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        assert acct._submit_order_impl(order) is None

    acct._account.place_order.assert_not_called()
    assert get_instance(TradingOrder, order.id).status == OrderStatus.ERROR


# ---------------------------------------------------------------------------
# Time in force
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tif", list(OrderTimeInForce))
def test_time_in_force_survives_a_broker_round_trip(tif):
    """M13. `tastytrade_order_to_tradingorder` stores the broker's TIF VALUE
    ("GTC Ext"), and `_build_new_order` looked it up as `good_for.lower()` against a
    map keyed "gtc_ext" -- so the lookup missed and a GTC-Ext order was silently
    resubmitted as plain GTC, which expires at a different time and does not trade the
    extended session. Every TIF the SDK can report must come back unchanged."""
    acct = _bare_account()
    mapped = acct.tastytrade_order_to_tradingorder(_placed_order(time_in_force=tif))
    assert mapped.good_for == tif.value

    account_def, order = _tt_trading_order(good_for=mapped.good_for)
    acct.id = account_def.id
    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        rebuilt = acct._build_new_order(order)

    assert rebuilt.time_in_force == tif


@pytest.mark.parametrize("good_for", [None, "", "  ", "banana"])
def test_an_unknown_time_in_force_falls_back_to_gtc(good_for):
    """N19. The documented default, matching AlpacaAccount's tif_map default."""
    new_order = _built_order(good_for=good_for)

    assert new_order.time_in_force == OrderTimeInForce.GTC


def test_an_unrecognised_time_in_force_is_logged_rather_than_silently_downgraded(monkeypatch):
    import sys
    TT = sys.modules[TastyTradeAccount.__module__]
    warnings = []
    monkeypatch.setattr(TT.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    _built_order(good_for="banana")

    assert any("banana" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# _signed_price
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price", [142.5, -142.5])
def test_signed_price_encodes_the_side_not_the_input_sign(price):
    """M07. TastyTrade derives `price_effect` from the SIGN of `price`: negative =
    debit (a buy), positive = credit (a sell). The MAGNITUDE is taken because the
    caller's price is a plain, unsigned limit -- but a TradingOrder that ever carries a
    signed price (round-tripped from a broker row) would otherwise flip a BUY into a
    CREDIT and be rejected, or worse, filled as the wrong cash flow."""
    from ba2_trade_platform.core.types import OrderDirection

    assert TastyTradeAccount._signed_price(price, OrderDirection.BUY) == Decimal("-142.5")
    assert TastyTradeAccount._signed_price(price, OrderDirection.SELL) == Decimal("142.5")


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


# --- Cross-account isolation -------------------------------------------------
#
# Both resolution paths in cancel_order (and both in refresh_orders) are scoped to
# `account_id == self.id`. Without that scope, a platform running two brokerage
# accounts lets one account cancel or mutate the other's rows -- and TastyTrade
# broker ids are small integers, so an id collision between two accounts is not a
# remote possibility, it is the normal case.

def _order_on_another_account(**kwargs):
    """A persisted TradingOrder belonging to a DIFFERENT account than the one under
    test, returned alongside an account whose id is not its owner's."""
    from tests.factories import create_account_definition

    other_def, order = _tt_trading_order(**kwargs)
    mine = create_account_definition(name="TastyTrade Mine", provider="TastyTrade")
    assert mine.id != other_def.id
    acct = _bare_account()
    acct.id = mine.id
    return acct, order


def test_cancel_order_refuses_a_database_id_belonging_to_another_account():
    """N02."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    acct, order = _order_on_another_account(broker_order_id="987654",
                                            status=OrderStatus.ACCEPTED)
    acct._account.delete_order = AsyncMock()

    assert acct.cancel_order(order.id) is False
    acct._account.delete_order.assert_not_called()
    assert get_instance(TradingOrder, order.id).status == OrderStatus.ACCEPTED


def test_cancel_order_refuses_a_broker_id_belonging_to_another_account():
    """N03."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    acct, order = _order_on_another_account(broker_order_id="987654",
                                            status=OrderStatus.ACCEPTED)
    acct._account.delete_order = AsyncMock()

    assert acct.cancel_order("987654") is False
    acct._account.delete_order.assert_not_called()
    assert get_instance(TradingOrder, order.id).status == OrderStatus.ACCEPTED


def test_refresh_orders_ignores_an_external_identifier_owned_by_another_account():
    """N04. external_identifier is OUR row id, and row ids are global -- so without the
    account scope, account A's refresh would happily rewrite account B's order from a
    broker response that has nothing to do with it."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    acct, order = _order_on_another_account(quantity=10.0, status=OrderStatus.ACCEPTED)
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                      external_identifier=str(order.id),
                      fills=[_fill(quantity="10", fill_price="150.25")]),
    ])

    acct.refresh_orders()

    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.ACCEPTED
    assert stored.broker_order_id is None
    assert stored.open_price is None


def test_refresh_orders_ignores_a_broker_order_id_owned_by_another_account():
    """N04, the fallback lookup."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    acct, order = _order_on_another_account(quantity=10.0, broker_order_id="987654",
                                            status=OrderStatus.ACCEPTED)
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                      fills=[_fill(quantity="10", fill_price="150.25")]),
    ])

    acct.refresh_orders()

    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.ACCEPTED
    assert stored.open_price is None


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


def test_refresh_orders_requests_all_pages():
    """M53. `get_order_history`'s per_page DEFAULTS TO 50 (account.py:808), so without
    the page_offset=None all-pages sentinel only the 50 NEWEST orders would ever sync
    -- and everything older would sit at its last-known status forever, leaving its
    transaction WAITING."""
    account_def, _order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[])

    acct.refresh_orders()

    assert acct._account.get_order_history.call_args.kwargs["page_offset"] is None


def test_refresh_orders_keeps_pending_cancel_until_the_broker_reaches_a_final_state():
    """N01. A cancel we REQUESTED has not happened yet: while the broker still reports
    the order LIVE its quantity is not released, and promoting it to ACCEPTED lets a
    dependent replacement fire against quantity that is still committed."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="987654",
                                           status=OrderStatus.PENDING_CANCEL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.LIVE,
                      external_identifier=str(order.id)),
    ])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).status == OrderStatus.PENDING_CANCEL


def test_refresh_orders_promotes_pending_cancel_once_the_broker_confirms():
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(broker_order_id="987654",
                                           status=OrderStatus.PENDING_CANCEL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.CANCELLED,
                      external_identifier=str(order.id)),
    ])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).status == OrderStatus.CANCELED


def test_refresh_orders_promotes_pending_cancel_to_filled_when_the_cancel_lost_the_race():
    """The order completed before the cancel landed -- that is a FILL, and the
    transaction must be opened."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(quantity=10.0, broker_order_id="987654",
                                           status=OrderStatus.PENDING_CANCEL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                      external_identifier=str(order.id),
                      fills=[_fill(quantity="10", fill_price="150.25")]),
    ])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).status == OrderStatus.FILLED


def test_refresh_orders_skips_a_dry_run_row():
    """N17. A dry run comes back with `id == -1`, which is not a broker id. Storing it
    as broker_order_id would make the idempotency guard in _submit_order_impl reject
    the genuine submission that follows."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(quantity=10.0, status=OrderStatus.PENDING)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=-1, status=TTOrderStatus.RECEIVED, size="10",
                      external_identifier=str(order.id)),
    ])

    acct.refresh_orders()

    stored = get_instance(TradingOrder, order.id)
    assert stored.broker_order_id is None
    assert stored.status == OrderStatus.PENDING


def test_refresh_orders_does_not_erase_a_known_fill_price():
    """N18. A later broker snapshot may carry no fills (a partial history page, an
    order re-reported after a replace). Overwriting a recorded open_price with None
    destroys the entry price the whole P&L of the transaction is computed from."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(quantity=10.0, broker_order_id="987654",
                                           status=OrderStatus.FILLED, open_price=150.25)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.get_order_history = AsyncMock(return_value=[
        _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                      external_identifier=str(order.id), fills=None),
    ])

    acct.refresh_orders()

    assert get_instance(TradingOrder, order.id).open_price == pytest.approx(150.25)


def test_refresh_orders_skips_a_row_whose_side_cannot_be_determined():
    """N16 at the refresh seam: a row with no usable leg action is dropped, never
    guessed onto the BUY side of the book."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    account_def, order = _tt_trading_order(quantity=10.0, broker_order_id="987654",
                                           status=OrderStatus.ACCEPTED)
    acct = _bare_account()
    acct.id = account_def.id
    sideless = _placed_order(order_id=987654, status=TTOrderStatus.FILLED, size="10",
                             external_identifier=str(order.id))
    object.__setattr__(sideless, "legs", [])
    acct._account.get_order_history = AsyncMock(return_value=[sideless])

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


def test_the_sdk_really_does_negate_a_debit_when_the_model_is_parsed():
    """GUARD ON THE FIXTURES THEMSELVES.

    Everything below only tests the sign bugs because `_placed_order_response` and
    `_margin_entry` go through `model_validate` on raw dasherized payloads, so
    `set_sign_for` runs. If someone "simplifies" them back to python kwargs or a
    SimpleNamespace, the validator is bypassed, every value turns cheerfully positive
    and the sign assertions below all pass vacuously. This test fails loudly instead.
    """
    from tastytrade.account import MarginReportEntry

    response = _placed_order_response(_placed_order(order_id=-1))
    assert response.buying_power_effect.change_in_buying_power == Decimal("-1500")
    assert response.buying_power_effect.isolated_order_margin_requirement == Decimal("-1500")
    assert response.fee_calculation.total_fees == Decimal("-0.03")

    entry = _margin_report(_margin_entry("AAPL", "775")).groups[0]
    assert isinstance(entry, MarginReportEntry)
    assert entry.initial_requirement == Decimal("-775")


def test_preview_order_impact_turns_a_signed_debit_into_a_positive_bp_cost():
    """BuyingPowerEffect.change_in_buying_power is NEGATIVE for a buy (order.py:381)."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            change_in_buying_power="1500",
                                            isolated_requirement="1500",
                                            total_fees="0.03"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.change_in_buying_power == -1500.0
    assert impact.bp_cost == 1500.0
    assert impact.accepted is True


def test_preview_order_impact_reports_the_margin_requirement_as_a_positive_cost():
    """I3. `isolated_order_margin_requirement` goes through `set_sign_for`
    (order.py:381-393) and a margin requirement is a DEBIT, so the model carries
    -1500. `OrderImpact.margin_requirement` is documented as a REQUIREMENT, not a
    signed cash flow -- unlike `change_in_buying_power`, which has a `bp_cost`
    property to re-sign it, this field is consumed directly. Passing -1500 through
    understates the capital an order ties up by 2x its own size."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            isolated_requirement="1500"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.margin_requirement == 1500.0


def test_preview_order_impact_reports_the_isolated_requirement_not_the_account_delta():
    """M10. `isolated_order_margin_requirement` is what THIS order ties up;
    `change_in_margin_requirement` is the whole account's margin delta, which nets
    against existing positions and can be far smaller (or zero for a hedge). Sourcing
    OrderImpact.margin_requirement from the account delta understates the capital the
    order commits. Both were 1500 in the fixture, so the swap was invisible."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            isolated_requirement="1500",
                                            change_in_margin_requirement="900"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.margin_requirement == 1500.0
    assert impact.raw["change_in_margin_requirement"] == -900.0


def test_preview_order_impact_reports_estimated_fees_as_a_positive_cost():
    """I3. `FeeCalculation.total_fees` goes through `set_sign_for` too
    (order.py:407-419), and a fee is a DEBIT: the model carries -0.03. A NEGATIVE
    'estimated fee' reads as a rebate and makes an order look cheaper than free."""
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1),
                                            total_fees="0.03"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        impact = acct.preview_order_impact(order)

    assert impact.estimated_fees == pytest.approx(0.03)


def test_preview_order_impact_prices_a_close_as_a_close_not_a_short_open():
    """I4. The preview used to call `_build_new_order(order)` with the default
    `is_closing_order=False` while the live submit passed the caller's value, so:

        preview leg action : OrderAction.SELL_TO_OPEN
        submit  leg action : OrderAction.SELL_TO_CLOSE

    A close FREES buying power; a short open CONSUMES margin and needs short
    approval. On a cash account the dry run therefore came back with errors and
    accepted=False, and the allocation engine skipped a legitimate sell -- while the
    docstring promised "a preview prices exactly what would be sent"."""
    from ba2_trade_platform.core.types import OrderDirection

    account_def, order = _tt_trading_order(side=OrderDirection.SELL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1)))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct.preview_order_impact(order, is_closing_order=True)

    previewed = acct._account.place_order.call_args.args[1]
    assert previewed.legs[0].action == OrderAction.SELL_TO_CLOSE


def test_preview_order_impact_still_prices_an_opening_sell_as_a_short():
    """The default must stay OPEN -- the flag adds an intent, it does not invert one."""
    from ba2_trade_platform.core.types import OrderDirection

    account_def, order = _tt_trading_order(side=OrderDirection.SELL)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(
        return_value=_placed_order_response(_placed_order(order_id=-1)))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct.preview_order_impact(order)

    assert acct._account.place_order.call_args.args[1].legs[0].action == OrderAction.SELL_TO_OPEN


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


def test_account_snapshot_declares_fractional_support():
    """N09. TastyTrade supports fractional equity quantities; reporting otherwise makes
    the allocation engine round every target to whole shares and silently drop any
    position smaller than one share."""
    acct = _bare_account()
    acct._account.get_balances = AsyncMock(return_value=_balances())

    assert acct.get_account_snapshot().supports_fractional is True


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


def _history_by_type(money_movement=(), receive_deliver=()):
    """A get_history side_effect that answers by the ``types`` it was asked for.

    ``get_dividends`` makes TWO history calls (Receive Deliver / Dividend for the DRIP
    share-receipt legs, then Money Movement for the cash) while ``get_cash_transfers``
    makes one. Feeding both seams through this lets a single fixture drive them and be
    compared -- which is the only direct way to see them disagree.
    """
    def _answer(session, **kwargs):
        if kwargs.get("types") == ["Receive Deliver"]:
            return list(receive_deliver)
        return list(money_movement)
    return AsyncMock(side_effect=_answer)


def test_get_cash_transfers_nets_dividend_tax_once_across_two_gross_legs():
    """I5 (MONEY BUG). A regular dividend and a special dividend can share a
    (symbol, date) with ONE withholding line. The tax used to be subtracted from
    EVERY gross leg, so a 1.00 + 0.57 pair with 0.24 tax posted 0.76 + 0.33 = 1.09
    to the ledger instead of the 1.33 actually kept -- the account was under-credited
    by the full tax again, once per extra leg."""
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_DIVIDEND

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9101, "Dividend", "1.00", underlying_symbol="TIDL"),
        _money_movement(9102, "Dividend", "0.57", underlying_symbol="TIDL"),
        _money_movement(9103, "Dividend", "-0.24", underlying_symbol="TIDL"),
    ])

    transfers = acct.get_cash_transfers()

    assert [t.event_type for t in transfers] == [CASH_TRANSFER_DIVIDEND] * 2
    # Each gross leg keeps its OWN broker id: (account_id, external_id) is the
    # portfolio_income_event idempotency key, so a re-sync must upsert both rows.
    assert [t.external_id for t in transfers] == ["9101", "9102"]
    assert sum(t.amount for t in transfers) == pytest.approx(1.33)


def test_get_cash_transfers_allocates_dividend_tax_pro_rata_across_the_legs():
    """The 0.24 tax belongs to the whole (symbol, date), so it is split in proportion
    to each leg's gross -- and the split sums to the cent."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9101, "Dividend", "1.00", underlying_symbol="TIDL"),
        _money_movement(9102, "Dividend", "0.57", underlying_symbol="TIDL"),
        _money_movement(9103, "Dividend", "-0.24", underlying_symbol="TIDL"),
    ])

    by_id = {t.external_id: t.amount for t in acct.get_cash_transfers()}

    assert by_id == {"9101": pytest.approx(0.85), "9102": pytest.approx(0.48)}


def test_get_cash_transfers_never_emits_a_negative_dividend_for_a_tiny_correction():
    """I5. A 0.10 correction alongside a 1.47 regular dividend with 0.24 tax used to
    emit `amount=-0.14` for the correction: a NEGATIVE dividend, though CashTransfer
    documents `amount` as POSITIVE for dividends and `is_income` gates on amount > 0.
    A clawback must never be published as negative income."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9201, "Dividend", "0.10", underlying_symbol="TIDL"),
        _money_movement(9202, "Dividend", "1.47", underlying_symbol="TIDL"),
        _money_movement(9203, "Dividend", "-0.24", underlying_symbol="TIDL"),
    ])

    transfers = acct.get_cash_transfers()

    assert all(t.amount >= 0 for t in transfers), [t.amount for t in transfers]
    assert sum(t.amount for t in transfers) == pytest.approx(1.33)


def test_get_cash_transfers_floors_a_dividend_at_zero_when_tax_exceeds_the_gross():
    """Withholding larger than the gross it belongs to is a CORRECTION, not negative
    income. The row is still emitted (keeping its broker id, so a re-sync overwrites
    a previously credited amount) but at 0.00, which `is_income` rejects."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9301, "Dividend", "1.00", underlying_symbol="TIDL"),
        _money_movement(9302, "Dividend", "-1.50", underlying_symbol="TIDL"),
    ])

    transfers = acct.get_cash_transfers()

    assert [t.external_id for t in transfers] == ["9301"]
    assert transfers[0].amount == 0.0
    assert transfers[0].is_income is False


def test_get_cash_transfers_keeps_each_symbols_tax_on_its_own_dividend():
    """Two symbols paying on the SAME date must not cross-net."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9401, "Dividend", "1.00", underlying_symbol="TIDL"),
        _money_movement(9402, "Dividend", "-0.20", underlying_symbol="TIDL"),
        _money_movement(9403, "Dividend", "5.00", underlying_symbol="SCHD"),
    ])

    by_symbol = {t.symbol: t.amount for t in acct.get_cash_transfers()}

    assert by_symbol == {"TIDL": pytest.approx(0.80), "SCHD": pytest.approx(5.00)}


@pytest.mark.parametrize("legs", [
    # one gross leg + tax (the plain case)
    [("Dividend", "1.57"), ("Dividend", "-0.24")],
    # regular + special sharing one withholding line
    [("Dividend", "1.00"), ("Dividend", "0.57"), ("Dividend", "-0.24")],
    # a tiny correction alongside the regular dividend
    [("Dividend", "0.10"), ("Dividend", "1.47"), ("Dividend", "-0.24")],
    # withholding bigger than the gross
    [("Dividend", "1.00"), ("Dividend", "-1.50")],
    # no withholding at all
    [("Dividend", "2.25")],
])
def test_cash_transfers_and_dividends_agree_on_the_same_history(legs):
    """I5. `get_cash_transfers` and `get_dividends` are two seams onto ONE broker
    history, and the ledger reads the first while the reporting UI reads the second.
    They disagreed by exactly the withholding for every multi-leg dividend, which is
    the clearest symptom of the double-subtraction. Pin the agreement directly."""
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_DIVIDEND

    rows = [_money_movement(9500 + i, sub_type, value, underlying_symbol="TIDL")
            for i, (sub_type, value) in enumerate(legs)]

    ledger = _bare_account()
    ledger._account.get_history = _history_by_type(money_movement=rows)
    reported = _bare_account()
    reported._account.get_history = _history_by_type(money_movement=rows)

    from_transfers = sum(t.amount for t in ledger.get_cash_transfers()
                         if t.event_type == CASH_TRANSFER_DIVIDEND)
    from_dividends = sum(d["amount"] for d in reported.get_dividends())

    assert from_transfers == pytest.approx(from_dividends)


def test_get_dividends_never_reports_a_negative_amount():
    """The same floor as the ledger seam: a withholding correction is 0.00 income."""
    acct = _bare_account()
    acct._account.get_history = _history_by_type(money_movement=[
        _money_movement(9601, "Dividend", "1.00", underlying_symbol="TIDL"),
        _money_movement(9602, "Dividend", "-1.50", underlying_symbol="TIDL"),
    ])

    dividends = acct.get_dividends()

    assert [d["amount"] for d in dividends] == [0.0]
    assert dividends[0]["gross_amount"] == 1.00
    assert dividends[0]["tax_withheld"] == 1.50


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


def test_get_cash_transfers_refuses_a_negative_deposit():
    """N25. `Transfer` is in BOTH the deposit and the withdrawal sub-type sets, so only
    the sign tells them apart. Dropping the `net_value > 0` guard turns a clawback into
    income: CashTransfer.is_income is true for a DEPOSIT, and the allocation engine
    funds a run off income events."""
    from ba2_trade_platform.core.account_types import CASH_TRANSFER_WITHDRAWAL

    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9701, "Transfer", "-500", description="ACAT TRANSFER OUT"),
    ])

    transfers = acct.get_cash_transfers()

    assert [t.event_type for t in transfers] == [CASH_TRANSFER_WITHDRAWAL]
    assert transfers[0].amount == -500.0
    assert transfers[0].is_income is False


def test_get_cash_transfers_never_reports_a_negative_amount_as_income():
    """The invariant behind N25, asserted directly across a mixed history."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9801, "Deposit", "2500"),
        _money_movement(9802, "Deposit", "-2500", description="DEPOSIT REVERSAL"),
        _money_movement(9803, "Transfer", "-500"),
        _money_movement(9804, "Withdrawal", "-800"),
        _money_movement(9805, "Dividend", "1.00", underlying_symbol="TIDL"),
    ])

    assert all(t.amount > 0 for t in acct.get_cash_transfers() if t.is_income)


def test_get_cash_transfers_skips_a_row_with_no_broker_id():
    """N27. `external_id` is the (account_id, external_id) idempotency key of
    portfolio_income_event. A row with no broker id cannot be de-duplicated, so
    re-syncing the window would credit it again on every run."""
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(None, "Deposit", "2500"),
        _money_movement(9901, "Deposit", "1000"),
    ])

    assert [t.external_id for t in acct.get_cash_transfers()] == ["9901"]


def test_get_cash_transfers_skips_a_row_with_no_event_date():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(return_value=[
        _money_movement(9902, "Deposit", "2500", transaction_date=None),
    ])

    assert acct.get_cash_transfers() == []


def test_get_cash_transfers_returns_empty_list_on_failure():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    assert acct.get_cash_transfers() == []


# ---------------------------------------------------------------------------
# get_dividends
# ---------------------------------------------------------------------------

def test_get_dividends_requests_both_history_legs_over_all_pages():
    """M56/M57. TWO paginated calls, and neither was pinned: page_offset=0 would cap
    each at the SDK's first page, so a year of dividends would silently be truncated
    to whatever fits in 250 rows of transaction history."""
    acct = _bare_account()
    acct._account.get_history = _history_by_type()

    acct.get_dividends(start_date=date(2026, 1, 1), end_date=date(2026, 8, 20))

    calls = {tuple(call.kwargs["types"]): call.kwargs
             for call in acct._account.get_history.call_args_list}
    assert set(calls) == {("Receive Deliver",), ("Money Movement",)}
    for kwargs in calls.values():
        assert kwargs["page_offset"] is None
        assert kwargs["start_date"] == date(2026, 1, 1)
        assert kwargs["end_date"] == date(2026, 8, 20)


def test_get_dividends_anchors_on_the_cash_leg_and_reports_the_breakdown():
    """A cash (non-DRIP) dividend: no Receive Deliver leg exists at all, so anchoring
    on the share receipt would report nothing."""
    acct = _bare_account()
    acct._account.get_history = _history_by_type(money_movement=[
        _money_movement(9101, "Dividend", "1.57", underlying_symbol="TIDL"),
        _money_movement(9102, "Dividend", "-0.24", underlying_symbol="TIDL"),
    ])

    dividends = acct.get_dividends()

    assert dividends == [{
        'symbol': 'TIDL', 'amount': 1.33, 'gross_amount': 1.57, 'tax_withheld': 0.24,
        'date': date(2026, 8, 3), 'drip_quantity': None, 'drip_price': None,
    }]


def test_get_dividends_enriches_a_reinvested_dividend_with_the_share_receipt():
    acct = _bare_account()
    acct._account.get_history = _history_by_type(
        money_movement=[_money_movement(9101, "Dividend", "1.33",
                                        underlying_symbol="TIDL")],
        receive_deliver=[SimpleNamespace(
            id=9110, underlying_symbol="TIDL", symbol="TIDL",
            transaction_date=date(2026, 8, 3), quantity=Decimal("0.05"),
            price=Decimal("26.60"))])

    dividend = acct.get_dividends()[0]

    assert dividend['drip_quantity'] == pytest.approx(0.05)
    assert dividend['drip_price'] == pytest.approx(26.60)


def test_get_dividends_survives_the_drip_leg_fetch_failing():
    """The share receipt is ENRICHMENT only -- losing it must not lose the dividend."""
    def _answer(session, **kwargs):
        if kwargs.get("types") == ["Receive Deliver"]:
            raise RuntimeError("gateway timeout")
        return [_money_movement(9101, "Dividend", "1.33", underlying_symbol="TIDL")]

    acct = _bare_account()
    acct._account.get_history = AsyncMock(side_effect=_answer)

    assert [d['amount'] for d in acct.get_dividends()] == [1.33]


def test_get_dividends_returns_empty_list_on_failure():
    acct = _bare_account()
    acct._account.get_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    assert acct.get_dividends() == []


# ---------------------------------------------------------------------------
# get_balance_history
# ---------------------------------------------------------------------------

def _snapshot(snapshot_date=date(2026, 8, 3), cash="25000", nlv="100000"):
    return SimpleNamespace(snapshot_date=snapshot_date,
                           cash_balance=Decimal(cash),
                           net_liquidating_value=Decimal(nlv))


def test_get_balance_history_requests_all_pages():
    """M58. Untested entirely. per_page defaults to 250, so page_offset=0 would cap a
    daily equity curve at 250 days and silently truncate the rest."""
    acct = _bare_account()
    acct._account.get_balance_snapshots = AsyncMock(return_value=[])

    acct.get_balance_history(start_date=date(2026, 1, 1))

    assert acct._account.get_balance_snapshots.call_args.kwargs["page_offset"] is None


def test_get_balance_history_defaults_to_a_year_when_no_start_date_is_given():
    """TastyTrade returns only a handful of snapshots without a start_date -- not a
    daily series -- so the adapter must always send one."""
    acct = _bare_account()
    acct._account.get_balance_snapshots = AsyncMock(return_value=[])

    acct.get_balance_history()

    requested = acct._account.get_balance_snapshots.call_args.kwargs["start_date"]
    assert requested == date.today() - timedelta(days=365)


def test_get_balance_history_splits_net_liquidation_into_cash_and_equity():
    acct = _bare_account()
    acct._account.get_balance_snapshots = AsyncMock(
        return_value=[_snapshot(cash="25000", nlv="100000")])

    row = acct.get_balance_history(start_date=date(2026, 1, 1))[0]

    assert row == {'date': date(2026, 8, 3), 'net_liquidating_value': 100000.0,
                   'cash_balance': 25000.0, 'equity_value': 75000.0}


def test_get_balance_history_returns_empty_list_on_failure():
    acct = _bare_account()
    acct._account.get_balance_snapshots = AsyncMock(
        side_effect=RuntimeError("gateway timeout"))

    assert acct.get_balance_history() == []


# ---------------------------------------------------------------------------
# get_symbol_margin_info
# ---------------------------------------------------------------------------

def _margin_report(*group_payloads):
    """A REAL tastytrade MarginReport parsed from a RAW dasherized payload.

    NOT a SimpleNamespace. `MarginReportEntry` applies `set_sign_for` in a
    `model_validator(mode="before")`, so an `initial-requirement` of 775 with
    `initial-requirement-effect: Debit` parses as **-775**. A namespace fixture
    hard-coding +775 cannot see that, and without `abs()` in the adapter the derived
    rate goes NEGATIVE (-775/1550 = -0.5 -> bp_factor -1.0), which makes a buy report
    negative buying-power consumption and lets the allocation engine's
    `sum(notional * bp_factor) <= available_bp` check approve unbounded notional.

    `groups` is `list[MarginReportEntry | EmptyDict]`; pass a bare `{}` for the
    EmptyDict placeholders TastyTrade really does return.
    """
    from tastytrade.account import MarginReport

    return MarginReport.model_validate({
        "account-number": "5WX00000",
        "description": "Total",
        "margin-calculation-type": "IRA Margin",
        "option-level": "No Restrictions",
        "margin-requirement": "775",
        "margin-requirement-effect": "Debit",
        "maintenance-requirement": "775",
        "maintenance-requirement-effect": "Debit",
        "margin-equity": "100000",
        "margin-equity-effect": "Credit",
        "option-buying-power": "50000",
        "option-buying-power-effect": "Credit",
        "reg-t-margin-requirement": "775",
        "reg-t-margin-requirement-effect": "Debit",
        "reg-t-option-buying-power": "50000",
        "reg-t-option-buying-power-effect": "Credit",
        "maintenance-excess": "99225",
        "maintenance-excess-effect": "Credit",
        "last-state-timestamp": 1756000000000,
        "initial-requirement": "775",
        "initial-requirement-effect": "Debit",
        "groups": list(group_payloads),
    })


def _margin_entry(underlying_symbol, initial_requirement):
    """RAW dasherized MarginReportEntry payload, WITH the `-effect` keys.

    `initial_requirement` is the unsigned MAGNITUDE the broker sends; the parsed model
    carries the NEGATIVE of it, because a margin requirement is a Debit.
    """
    return {
        "description": underlying_symbol,
        "code": "EQUITY",
        "underlying-symbol": underlying_symbol,
        "underlying-type": "Equity",
        "margin-calculation-type": "IRA Margin",
        "buying-power": initial_requirement,
        "buying-power-effect": "Debit",
        "margin-requirement": initial_requirement,
        "margin-requirement-effect": "Debit",
        "initial-requirement": initial_requirement,
        "initial-requirement-effect": "Debit",
        "maintenance-requirement": initial_requirement,
        "maintenance-requirement-effect": "Debit",
    }


def _precision(value=5, symbol=None, instrument_type="Equity",
               minimum_increment_precision=0):
    """RAW DASHERIZED QuantityDecimalPrecision payload, in the shape the API really sends.

    Built through `model_validate` rather than python kwargs, for the same reason
    `_margin_entry` is: kwargs go straight past the alias generator and any
    `model_validator(mode="before")`, so a fixture built that way can quietly agree
    with a bug instead of catching it. The previous stand-in was a SimpleNamespace
    that set BOTH `value=5` and `minimum_increment_precision=5`, which made the two
    fields indistinguishable and hid the fact that the adapter read the wrong one.

    The defaults are the user's REAL production row (probed 2026-08-21): 48 rows in
    the table, EXACTLY ONE equity row, and it is the generic one (`symbol is None`)
    carrying `value=5, minimum-increment-precision=0`. `value` is the quantity
    decimal precision -- that same account holds SCHD at 0.05715 and VYMI at 0.01955,
    i.e. 5 decimal places, which matches `value`, not `minimum-increment-precision`.
    """
    from tastytrade.instruments import QuantityDecimalPrecision

    return QuantityDecimalPrecision.model_validate({
        "instrument-type": instrument_type,
        "value": value,
        "minimum-increment-precision": minimum_increment_precision,
        "symbol": symbol,
    })


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
        precisions=[_precision(value=5)],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == pytest.approx(0.5)
    assert info["AAPL"].bp_factor == pytest.approx(1.0)  # 0.5 rate x 2:1 account
    assert info["AAPL"].source == MARGIN_SOURCE_POSITION


def test_symbol_margin_info_takes_the_magnitude_of_a_debit_signed_requirement():
    """D1. `MarginReportEntry.initial_requirement` goes through `set_sign_for`
    (account.py:240-251) and a margin requirement is a DEBIT, so the parsed model
    carries **-775**, never +775. Without `abs()` the derived rate is
    `min(1.0, -775/1550) = -0.5` and `bp_factor = -1.0`: a BUY then reports NEGATIVE
    buying-power consumption, and the allocation engine's
    `sum(notional * bp_factor) <= available_bp` check approves unbounded notional --
    every extra share makes the sum look *smaller*.

    The `abs()` in TastyTradeAccount.get_symbol_margin_info is therefore load-bearing,
    not belt-and-braces. It went untested only because the old fixture built the entry
    with python kwargs, which bypasses the `model_validator(mode="before")` entirely.
    """
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_POSITION

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    report = _margin_report(_margin_entry("AAPL", "775"))
    # The premise, stated out loud: the broker's number reaches the adapter NEGATIVE.
    assert report.groups[0].initial_requirement == Decimal("-775")

    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")], report=report,
        precisions=[_precision()],
        positions=[_tt_position(symbol="AAPL", quantity="10", mark_price="155")])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].initial_margin_rate == pytest.approx(0.5)
    assert info["AAPL"].bp_factor == pytest.approx(1.0)
    assert info["AAPL"].bp_factor > 0, "a buy must never consume negative buying power"
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
        report=_margin_report(), precisions=[_precision(value=5)],
        positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL", "BRKA"])

    assert info["AAPL"].fractionable is True
    assert info["AAPL"].min_trade_increment == pytest.approx(1e-5)
    assert info["BRKA"].fractionable is False
    assert info["BRKA"].min_trade_increment == 1.0


def test_symbol_margin_info_takes_the_quantity_step_from_value_not_increment_precision():
    """The quantity decimal precision is `QuantityDecimalPrecision.value`, NOT
    `minimum_increment_precision`, and reading the wrong one silently switched
    fractional trading off for this whole broker.

    Live evidence from the user's production account (2026-08-21): the precision
    table has 48 rows and exactly ONE equity row, the generic one, reading
    `value=5, minimum-increment-precision=0`. Reading
    `10 ** -minimum_increment_precision` therefore yields 1.0 and the adapter
    reported `SCHD frac=True min_trade_increment=1.0` -- "fractionable, but whole
    shares only". Meanwhile 18 of that account's 25 positions are HELD at fractional
    quantities (SCHD 0.05715, VYMI 0.01955, IDVO 2.03896, MAIN 4.0685), and 0.05715
    is five decimal places: exactly `value`, not `minimum_increment_precision`.

    The damage was downstream and silent: `_round_shares` floors every target onto
    `min_trade_increment`, so a 1.0 step snapped every fractional target to a whole
    share and no error was raised anywhere.
    """
    acct = _bare_account()
    row = _precision(value=5, minimum_increment_precision=0)
    # The premise, stated out loud: the two fields DISAGREE in production.
    assert (row.value, row.minimum_increment_precision) == (5, 0)

    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("SCHD", is_fractional_quantity_eligible=True)],
        report=_margin_report(), precisions=[row], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["SCHD"])

    assert info["SCHD"].fractionable is True
    assert info["SCHD"].min_trade_increment == pytest.approx(1e-5)

    # And the step must TRACK `value`: on the live row alone, `10 ** -value` and a
    # hardcoded 1e-5 are indistinguishable.
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("SCHD", is_fractional_quantity_eligible=True)],
        report=_margin_report(),
        precisions=[_precision(value=3, minimum_increment_precision=0)], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["SCHD"])

    assert info["SCHD"].min_trade_increment == pytest.approx(1e-3)


def test_symbol_margin_info_ignores_precision_rows_for_other_instrument_types():
    """The live table is 48 rows across many instrument types and the equity row is
    not first. A crypto row (8 decimal places) must not become the equity step."""
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL", is_fractional_quantity_eligible=True)],
        report=_margin_report(),
        precisions=[_precision(value=8, instrument_type="Cryptocurrency",
                               minimum_increment_precision=8),
                    _precision(value=5)],
        positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].min_trade_increment == pytest.approx(1e-5)


def test_symbol_margin_info_returns_nothing_when_the_position_fetch_fails(monkeypatch):
    """I6. `get_positions()` returns None on a FETCH FAILURE and [] when the account is
    genuinely flat -- a distinction this very file documents 50 lines earlier, and one
    that once mass-closed 8 real transactions when it was collapsed.

    `for position in (self.get_positions() or [])` collapsed it again: with the fetch
    failing, a HELD AAPL carrying a real initial_requirement came back bp_factor=2.0,
    initial_margin_rate=None, source='default' -- byte-identical to an UNHELD symbol.
    Conservative in direction, but it silently discards the only real per-symbol margin
    data TastyTrade publishes and gives the caller no way to know it fell back."""
    errors = _capture_errors(monkeypatch)

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    acct._account.get_margin_requirements = AsyncMock(
        return_value=_margin_report(_margin_entry("AAPL", "775")))
    acct._account.get_positions = AsyncMock(side_effect=RuntimeError("connection reset"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=[_FakeEquity("AAPL")])), \
         patch("tastytrade.instruments.get_quantity_decimal_precisions",
               new=AsyncMock(return_value=[_precision()])):
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info == {}
    assert any("position fetch failed" in m for m in errors), errors


def test_symbol_margin_info_still_answers_for_a_genuinely_flat_account():
    """The I6 guard must fire on None (fetch failed), NOT on [] (really flat) --
    otherwise a brand-new account can never be sized at all."""
    from ba2_trade_platform.core.account_types import MARGIN_SOURCE_DEFAULT

    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")], report=_margin_report(),
        precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].source == MARGIN_SOURCE_DEFAULT
    assert info["AAPL"].bp_factor == 2.0


def test_symbol_margin_info_leaves_the_increment_unknown_when_precision_is_unavailable():
    """I7. `min_trade_increment` is the broker's published quantity step, and None
    means "the broker did not say" -- never a fabricated or derived number. With the
    precision table unreachable, a fractionable symbol's step is genuinely unknown."""
    acct = _bare_account()
    acct._account.get_margin_requirements = AsyncMock(return_value=_margin_report())
    acct._account.get_positions = AsyncMock(return_value=[])

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=[_FakeEquity("AAPL")])), \
         patch("tastytrade.instruments.get_quantity_decimal_precisions",
               new=AsyncMock(side_effect=RuntimeError("gateway timeout"))):
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].fractionable is True
    assert info["AAPL"].min_trade_increment is None


def test_symbol_margin_info_reports_whole_shares_for_a_non_fractionable_symbol_even_without_precision():
    """I7. A symbol the broker will not split trades in WHOLE SHARES -- that is a fact
    about the symbol, not a reading from the precision table, so it survives the
    precision fetch failing."""
    acct = _bare_account()
    acct._account.get_margin_requirements = AsyncMock(return_value=_margin_report())
    acct._account.get_positions = AsyncMock(return_value=[])

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=[
                   _FakeEquity("BRKA", is_fractional_quantity_eligible=False)])), \
         patch("tastytrade.instruments.get_quantity_decimal_precisions",
               new=AsyncMock(side_effect=RuntimeError("gateway timeout"))):
        info = acct.get_symbol_margin_info(["BRKA"])

    assert info["BRKA"].fractionable is False
    assert info["BRKA"].min_trade_increment == 1.0


def test_symbol_margin_info_treats_an_unknown_fractionability_as_not_fractionable():
    """M19. `is_fractional_quantity_eligible` is Optional in the SDK. None means the
    broker did not say, which must NOT be read as "yes, split it" -- a fractional
    quantity on a whole-share-only name is rejected at submission."""
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL", is_fractional_quantity_eligible=None)],
        report=_margin_report(), precisions=[_precision()], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["AAPL"])

    assert info["AAPL"].fractionable is False
    assert info["AAPL"].min_trade_increment == 1.0


def test_symbol_margin_info_skips_empty_margin_report_groups():
    """MarginReport.groups is `list[MarginReportEntry | EmptyDict]` -- the EmptyDict
    placeholders have no attributes at all."""
    acct = _bare_account()
    acct._account.margin_or_cash = "Margin"
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("AAPL")],
        report=_margin_report({}, _margin_entry("AAPL", "775")),
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


def test_symbol_margin_info_requests_equity_metadata_over_all_pages():
    """M51. The Equity lookup here caps at 250 symbols per page by default. A large
    universe would silently lose every symbol past the first page -- and this method
    OMITS a symbol with no Equity record, so the loss looks exactly like "the broker
    does not know that symbol"."""
    acct = _bare_account()
    equity_get = AsyncMock(return_value=[_FakeEquity("AAPL")])
    acct._account.get_margin_requirements = AsyncMock(return_value=_margin_report())
    acct._account.get_positions = AsyncMock(return_value=[])

    with patch("tastytrade.instruments.Equity.get", new=equity_get), \
         patch("tastytrade.instruments.get_quantity_decimal_precisions",
               new=AsyncMock(return_value=[_precision()])):
        acct.get_symbol_margin_info(["AAPL"])

    assert equity_get.call_args.kwargs["page_offset"] is None


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
# get_symbol_margin_info: the $5 fractional notional floor
#
# Verified against the user's REAL production account on 2026-08-21 with
# POST /accounts/{n}/orders/dry-run. A 0.05 AND a 0.05715 share order of SCHD at
# ~$34 (so ~$1.70 and ~$1.94 of notional) both came back HTTP 422:
#     below_notional_value_minimum: Fractional equities orders cannot have a
#     notional value less than $5.
# Nothing in the adapter knew this, so every such order was planned, submitted and
# rejected at the broker.
# ---------------------------------------------------------------------------

def test_symbol_margin_info_publishes_the_fractional_notional_floor():
    """The engine is pure and never calls a broker, so a broker rule it must honour
    can only reach it as DATA on MarginInfo -- exactly how `fractionable` and
    `min_trade_increment` already travel."""
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import (
        MIN_FRACTIONAL_NOTIONAL_USD)

    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("SCHD", is_fractional_quantity_eligible=True)],
        report=_margin_report(), precisions=[_precision(value=5)], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["SCHD"])

    assert info["SCHD"].min_fractional_notional == 5.0
    # And it must TRACK the named constant, not a coincidentally equal literal.
    assert info["SCHD"].min_fractional_notional == MIN_FRACTIONAL_NOTIONAL_USD


def test_symbol_margin_info_never_reports_the_dollar_floor_as_a_share_minimum():
    """UNITS. `min_order_size` is a SHARE COUNT -- the engine compares it against a
    rounded share quantity. Putting the $5 there would read as "5 SHARES", which on
    a $34 ETF suppresses every order under $170 instead of every order under $5.

    TastyTrade publishes no per-symbol share minimum at all, so `min_order_size`
    stays None ("the broker did not say"), and the money floor lives in its own
    correctly-named field.
    """
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("SCHD", is_fractional_quantity_eligible=True)],
        report=_margin_report(), precisions=[_precision(value=5)], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["SCHD"])

    assert info["SCHD"].min_order_size is None
    assert info["SCHD"].min_fractional_notional == 5.0


def test_symbol_margin_info_leaves_the_notional_floor_off_a_whole_share_symbol():
    """The broker's rule is spelled "Fractional equities orders ...". A symbol that
    cannot be split never places one, so claiming a $5 floor for it would refuse a
    perfectly legal 1-share order in a stock trading under $5."""
    acct = _bare_account()
    equity_patch, precision_patch = _wire_margin_sources(
        acct, equities=[_FakeEquity("BRKA", is_fractional_quantity_eligible=False)],
        report=_margin_report(), precisions=[_precision(value=5)], positions=[])

    with equity_patch, precision_patch:
        info = acct.get_symbol_margin_info(["BRKA"])

    assert info["BRKA"].fractionable is False
    assert info["BRKA"].min_fractional_notional is None


def test_the_fractional_notional_floor_is_one_named_constant():
    """A money threshold spelled as a bare literal at its use site is a threshold
    nobody can find when the broker moves it."""
    import inspect
    import sys

    from ba2_trade_platform.modules.accounts.TastyTradeAccount import (
        MIN_FRACTIONAL_NOTIONAL_USD)

    assert MIN_FRACTIONAL_NOTIONAL_USD == 5.0
    source = inspect.getsource(sys.modules[TastyTradeAccount.__module__])
    # The broker's verbatim rejection must sit next to the number.
    assert "notional value less than $5" in source


# ---------------------------------------------------------------------------
# Bulk quotes
# ---------------------------------------------------------------------------

def _market_data(symbol, bid="149.90", ask="150.10", mid="150.00", last="150.05",
                 close="148.00", mark="150.02"):
    """Stand-in for tastytrade MarketData.

    `mark` is the only price field the real model declares REQUIRED (market_data.py):
    bid, ask, mid, last and close are all Optional and routinely absent outside
    regular hours.
    """
    return SimpleNamespace(
        symbol=symbol,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        mid=Decimal(mid) if mid is not None else None,
        last=Decimal(last) if last is not None else None,
        close=Decimal(close) if close is not None else None,
        mark=Decimal(mark) if mark is not None else None,
    )


@pytest.mark.parametrize("price_type,expected", [("bid", 149.90), ("ask", 150.10),
                                                 ("mid", 150.00), ("mark", 150.02)])
def test_pick_price_returns_the_requested_price_when_present(price_type, expected):
    assert TastyTradeAccount._pick_price(
        _market_data("AAPL"), price_type) == pytest.approx(expected)


@pytest.mark.parametrize("price_type", ["bid", "ask", "mid"])
def test_pick_price_falls_back_to_the_mark_before_a_stale_close(price_type):
    """M18. `mark` is the only REQUIRED price field on MarketData -- the broker's own
    consolidated live price -- and the ladder never read it. An `ask` request with no
    ask therefore fell through to `last` and then to `close`, so a thin or after-hours
    symbol could be priced at YESTERDAY'S CLOSE while a live mark sat unused."""
    data = _market_data("AAPL", bid=None, ask=None, mid=None,
                        last="140.00", close="130.00", mark="150.02")

    assert TastyTradeAccount._pick_price(data, price_type) == pytest.approx(150.02)


def test_pick_price_falls_back_past_an_absent_mark_to_the_last_trade():
    data = _market_data("AAPL", bid=None, ask=None, mid=None, mark=None,
                        last="140.00", close="130.00")

    assert TastyTradeAccount._pick_price(data, "ask") == pytest.approx(140.00)


def test_pick_price_falls_back_to_the_close_when_nothing_else_traded():
    data = _market_data("AAPL", bid=None, ask=None, mid=None, mark=None, last=None,
                        close="130.00")

    assert TastyTradeAccount._pick_price(data, "ask") == pytest.approx(130.00)


def test_pick_price_returns_none_rather_than_fabricating_a_zero():
    """N31. A row with no usable price means "the broker did not quote this", and the
    platform rule is explicit: never a fallback value for live data. A fabricated 0.0
    reads as a real price and sizes an infinite position."""
    data = _market_data("AAPL", bid=None, ask=None, mid=None, mark=None, last=None,
                        close=None)

    assert TastyTradeAccount._pick_price(data, "bid") is None


def test_pick_price_treats_a_zero_quote_as_no_quote():
    """A 0.00 bid is not a price anyone can trade at."""
    data = _market_data("AAPL", bid="0", ask=None, mid=None, mark="150.02", last=None,
                        close=None)

    assert TastyTradeAccount._pick_price(data, "bid") == pytest.approx(150.02)


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


# ---------------------------------------------------------------------------
# EMPTY broker errors
#
# LIVE EVIDENCE (2026-08-21, the user's real TastyTrade account): every write call
# -- place_order(dry_run=True) included -- came back
#
#     HTTP 403  {"error":{"message":"Token has insufficient scopes for this request"}}
#
# and what the adapter actually caught was `TastytradeError('')`, args == ('',).
#
# The SDK throws the reason away: `tastytrade.utils.validate_response`
# (utils.py:240-258) builds its message ONLY from error objects that carry BOTH a
# `code` and a `message` (or a `domain` + `reason`). This 403 carries a `message`
# and no `code`, so the loop appends nothing and `TastytradeError("")` is raised.
# The vendor file lives in venv/ and is wiped by the next `pip install -r
# requirements.txt`, so the fix belongs HERE, in the adapter.
#
# An empty string in `TradingOrder.comment` is the worst possible outcome: the
# Pending Orders UI shows a failed order with no reason at all.
# ---------------------------------------------------------------------------

def _empty_broker_error():
    """Exactly what the live 403 produced: TastytradeError with args == ('',)."""
    from tastytrade.utils import TastytradeError
    exc = TastytradeError("")
    assert exc.args == ("",) and str(exc) == ""
    return exc


def test_an_empty_broker_error_still_explains_the_failed_submission(monkeypatch):
    """The persisted comment (what the user sees) must name the operation and the
    most likely cause, not be an empty string behind a bracketed reason."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderStatus

    errors = _capture_errors(monkeypatch)
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=_empty_broker_error())

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        assert acct._submit_order_impl(order) is None

    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.ERROR
    comment = stored.comment or ""
    # The degenerate pre-fix value was exactly "[unknown] " -- a reason tag and
    # nothing else.
    assert comment.replace("[unknown]", "").strip(), f"comment carries no reason: {comment!r}"
    assert "submission" in comment.lower(), comment
    assert "scope" in comment.lower(), comment
    # AccountInterface._handle_order_submit_error truncates the message at 180 chars,
    # so the diagnosis has to be FRONT-LOADED. Pinned here: a future edit that pushes
    # the broker's own 403 wording past the cut would leave the UI with a comment that
    # trails off mid-explanation, which is how this becomes useless again.
    assert "insufficient scopes for this request" in comment, comment
    assert any("scope" in m.lower() for m in errors), errors


def test_a_real_broker_message_is_never_overwritten_by_the_empty_error_hint(monkeypatch):
    """Only an EMPTY message is substituted. A broker that actually said something
    keeps saying it, verbatim."""
    from tastytrade.utils import TastytradeError
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder

    _capture_errors(monkeypatch)
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=TastytradeError(
        "insufficient_buying_power: Account 5WX00000 has insufficient buying power"))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order)

    comment = get_instance(TradingOrder, order.id).comment or ""
    assert "insufficient buying power" in comment
    assert "OAuth" not in comment, comment


def test_a_whitespace_only_broker_message_counts_as_empty(monkeypatch):
    """`TastytradeError("\\n")` is just as useless as `TastytradeError("")` -- the SDK
    appends a trailing newline per error object, so a single unparsed error can
    produce exactly this."""
    from tastytrade.utils import TastytradeError
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder

    _capture_errors(monkeypatch)
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=TastytradeError("   \n "))

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        acct._submit_order_impl(order)

    assert "scope" in (get_instance(TradingOrder, order.id).comment or "").lower()


def test_an_empty_broker_error_is_classified_as_an_auth_failure():
    """UNKNOWN is not good enough: it is the bucket for "we have no idea", and this
    one we DO know."""
    from ba2_trade_platform.core.types import BrokerOrderErrorReason

    acct = _bare_account()
    reason = acct._classify_order_error(_empty_broker_error())

    assert reason != BrokerOrderErrorReason.UNKNOWN
    assert reason != BrokerOrderErrorReason.STOP_THROUGH_MARKET


def test_the_explicit_scope_rejection_is_classified_as_an_auth_failure():
    """The same 403 reaches us with a real message whenever the SDK manages to parse
    it (or a future SDK fixes utils.py). Both spellings must classify the same."""
    from tastytrade.utils import TastytradeError
    from ba2_trade_platform.core.types import BrokerOrderErrorReason

    acct = _bare_account()
    exc = TastytradeError("Token has insufficient scopes for this request")

    assert acct._classify_order_error(exc) == acct._classify_order_error(_empty_broker_error())
    assert acct._classify_order_error(exc) != BrokerOrderErrorReason.UNKNOWN


def test_an_unremarkable_broker_error_is_still_unknown():
    """The classifier must not swallow everything into the auth bucket."""
    from tastytrade.utils import TastytradeError
    from ba2_trade_platform.core.types import BrokerOrderErrorReason

    acct = _bare_account()
    exc = TastytradeError("preflight-check-failure: order quantity is not a multiple of 1")

    assert acct._classify_order_error(exc) == BrokerOrderErrorReason.UNKNOWN


def test_a_scope_rejection_on_a_stop_order_is_not_retried_as_a_market_order(monkeypatch):
    """THE failure mode to avoid. A 403 is permanent until the token is re-scoped, so
    resubmitting is a guaranteed-losing loop -- and AccountInterface's
    STOP_THROUGH_MARKET recovery would silently convert a protective stop into an
    immediate MARKET order, which on an ACCEPTED token would dump the position at any
    price. The broker must be called exactly ONCE."""
    from ba2_trade_platform.core.db import get_instance
    from ba2_trade_platform.core.models import TradingOrder
    from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType

    _capture_errors(monkeypatch)
    account_def, order = _tt_trading_order(side=OrderDirection.SELL,
                                           order_type=OrderType.SELL_STOP,
                                           stop_price=52.88)
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=_empty_broker_error())

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        assert acct._submit_order_impl(order) is None

    assert acct._account.place_order.await_count == 1, "the 403 was resubmitted"
    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.ERROR
    # Not converted to MARKET, and the stop it was protecting with is intact.
    assert stored.order_type == OrderType.SELL_STOP
    assert stored.stop_price == 52.88


def test_an_empty_broker_error_explains_a_failed_cancellation(monkeypatch):
    errors = _capture_errors(monkeypatch)
    account_def, order = _tt_trading_order(broker_order_id="987654")
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.delete_order = AsyncMock(side_effect=_empty_broker_error())

    assert acct.cancel_order(order.id) is False

    assert any("scope" in m.lower() and "cancel" in m.lower() for m in errors), errors


def test_an_empty_broker_error_explains_a_failed_preview(monkeypatch):
    errors = _capture_errors(monkeypatch)
    account_def, order = _tt_trading_order()
    acct = _bare_account()
    acct.id = account_def.id
    acct._account.place_order = AsyncMock(side_effect=_empty_broker_error())

    with patch("tastytrade.instruments.Equity.get",
               new=AsyncMock(return_value=_FakeEquity("AAPL"))):
        assert acct.preview_order_impact(order) is None

    assert any("scope" in m.lower() and "preview" in m.lower() for m in errors), errors


def test_an_empty_broker_error_explains_a_degraded_read_path(monkeypatch):
    """get_positions returns None on failure and the caller only ever sees the log --
    an empty message there is a silent outage."""
    errors = _capture_errors(monkeypatch)
    acct = _bare_account()
    acct._account.get_positions = AsyncMock(side_effect=_empty_broker_error())

    assert acct.get_positions() is None

    assert any("scope" in m.lower() for m in errors), errors
