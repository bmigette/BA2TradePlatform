"""A covered call SUSPENDS the platform's own resting stop on the shares it pledges.

Observed live (paper, Alpaca): an expert bought 100 U and 100 B, the platform staged a resting
GTC SELL_STOP for the 100 shares; next day "Sell Covered Call" was rejected with
``{"code":40310000,"message":"account not eligible to trade uncovered option contracts"}``
because the stop held every share (``qty_available`` = 0) and the broker saw the call as
uncovered. It failed every day.

The behaviour (it is what the backtest does: ``_pledged_share_lock``): shares pledged to an open
short call are never sold, so a stock stop / take-profit cannot fire while the call is open and
works again once it is gone. Live implements it as SUSPENSION, not removal:

* writing the call cancels the platform's resting protective legs on the pledging equity
  transaction(s), waits (bounded) for the broker to report the shares available, then sends the
  call; ``transaction.stop_loss`` / ``take_profit`` are untouched;
* a call that is not accepted re-stages the legs at once;
* while the call is open nothing re-stages a sell on those shares and the breached-stop safety
  net does not market-sell them;
* when the call is gone and the shares are still held the legs are re-staged from the stored
  values; called-away shares close their transaction as before and nothing is re-staged.

No live API call: the broker is ``FakeBroker`` below, which keeps the one number Alpaca
enforces -- ``qty_available = qty - shares held for open orders`` -- and rejects a short call it
does not cover with the real error text.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.option_types import OptionLeg
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.modules.accounts import alpaca_cover_protection as cp
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_common.core.TransactionHelper import TransactionHelper
from tests.factories import create_account_definition, create_trading_order, create_transaction

OCC = "AAPL260116C00150000"
UNCOVERED = ('{"code":40310000,"message":"account not eligible to trade uncovered option '
             'contracts"}')


class FakeBroker:
    """The slice of Alpaca these paths touch, with Alpaca's own hold arithmetic."""

    def __init__(self, held=100, symbol="AAPL"):
        self.held = held
        self.symbol = symbol
        self.resting = {}          # broker order id -> shares held for that order
        self.state = {}            # broker order id -> "new" | "canceled"
        self.calls = []            # ordered event log
        self.cancel_lands = True   # False: the cancel is accepted but the order stays live
        self.reject_call = None    # an exception to raise when the option order is sent
        self.position_error = None
        self._n = 0

    # -- the book ---------------------------------------------------------
    def seed(self, broker_id, shares):
        self.resting[broker_id] = shares
        self.state[broker_id] = "new"

    @property
    def available(self):
        return self.held - sum(self.resting.values())

    # -- the client API ---------------------------------------------------
    def get_open_position(self, symbol):
        self.calls.append("get_open_position")
        if self.position_error is not None:
            raise self.position_error
        return SimpleNamespace(qty=str(self.held), qty_available=str(self.available))

    def get_all_positions(self):
        return [SimpleNamespace(
            symbol=self.symbol, qty=str(self.held), qty_available=str(self.available),
            avg_entry_price="150", avg_entry_swap_rate=None, current_price="150",
            lastday_price="150", change_today="0", unrealized_pl="0", unrealized_plpc="0",
            unrealized_intraday_pl="0", unrealized_intraday_plpc="0", market_value="15000",
            cost_basis="15000", side="long", exchange="NASDAQ", asset_class="us_equity",
            swap_rate=None)]

    def cancel_order_by_id(self, broker_id):
        self.calls.append(f"cancel:{broker_id}")
        if self.cancel_lands:
            self.resting.pop(broker_id, None)
            self.state[broker_id] = "canceled"

    def get_order_by_id(self, broker_id):
        self.calls.append(f"get_order:{broker_id}")
        return SimpleNamespace(id=broker_id, status=self.state[broker_id])

    def submit_order(self, req):
        self.calls.append("submit_option")
        if self.reject_call is not None:
            raise self.reject_call
        need = int(req.qty) * 100
        if self.available < need:
            raise RuntimeError(UNCOVERED)
        self._n += 1
        broker_id = f"call-{self._n}"
        self.seed(broker_id, need)
        return SimpleNamespace(id=broker_id, legs=None, status="new")


def _status_of(raw):
    return SimpleNamespace(
        status={"new": OrderStatus.NEW, "canceled": OrderStatus.CANCELED}[raw.status],
        filled_qty=None, open_price=None)


@pytest.fixture(autouse=True)
def _activity_log(monkeypatch):
    """``log_activity`` is asynchronous (a writer thread with its own in-memory DB here); the
    tests read what was logged instead."""
    logged = []
    monkeypatch.setattr("ba2_trade_platform.core.db.log_activity",
                        lambda **kw: logged.append(kw))
    return logged


@pytest.fixture(autouse=True)
def _instant_waits(monkeypatch):
    """The bounded wait must not sleep in a test; it polls a fixed number of times."""
    monkeypatch.setattr(cp, "RELEASE_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(cp, "RELEASE_POLL_SECONDS", 0.0)
    monkeypatch.setattr(cp, "_sleep", lambda s: None)


@pytest.fixture
def world(monkeypatch):
    """(account, broker, restaged) -- an AlpacaAccount over the fake broker.

    ``restaged`` collects every protective order the account sends to the broker, so a test
    can say exactly what was (not) re-staged; each is booked at the broker like a real one.
    """
    acct_def = create_account_definition(provider="AlpacaAccount")
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = acct_def.id
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    broker = FakeBroker()
    acct.client = broker
    restaged = []

    monkeypatch.setattr(acct, "invalidate_balance_cache", lambda: None, raising=False)
    monkeypatch.setattr(acct, "get_option_quote", lambda cs: None, raising=False)
    monkeypatch.setattr(acct, "alpaca_order_to_tradingorder", _status_of, raising=False)
    monkeypatch.setattr(acct, "get_instrument_current_price", lambda *a, **k: 150.0)
    monkeypatch.setattr(acct, "_margin_enabled", lambda: False)

    def fake_submit_order(order, **kwargs):
        broker_id = f"stop-{len(restaged) + 1}"
        fresh = get_instance(TradingOrder, order.id)
        fresh.status = OrderStatus.NEW
        fresh.broker_order_id = broker_id
        update_instance(fresh)
        broker.seed(broker_id, fresh.quantity)
        restaged.append(fresh)
        return fresh

    monkeypatch.setattr(acct, "submit_order", fake_submit_order)
    return acct, broker, restaged


def _equity_with_stop(acct, broker, *, shares=100.0, stop=140.0, take_profit=None,
                      oco=False, broker_id="bkr-stop"):
    """An OPENED long AAPL transaction with its filled entry and a RESTING protective leg."""
    txn = create_transaction(symbol="AAPL", quantity=shares, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0,
                             stop_loss=stop, take_profit=take_profit)
    entry = create_trading_order(
        account_id=acct.id, symbol="AAPL", quantity=shares, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=shares,
        open_price=150.0, transaction_id=txn.id, broker_order_id=f"entry-{txn.id}")
    kind = "TPSL" if oco else "SL"
    leg = create_trading_order(
        account_id=acct.id, symbol="AAPL", quantity=shares, side=OrderDirection.SELL,
        order_type=OrderType.OCO if oco else OrderType.SELL_STOP, status=OrderStatus.NEW,
        stop_price=stop, limit_price=take_profit if oco else None, transaction_id=txn.id,
        broker_order_id=broker_id,
        comment=TransactionHelper.tpsl_comment(kind, acct.id, txn.id, entry.id))
    broker.seed(broker_id, shares)
    return txn, leg


def _call_legs(contracts_strike=150.0):
    return [OptionLeg(contract_symbol=OCC, side=OrderDirection.SELL,
                      position_intent="sell_to_open", option_type=OptionRight.CALL,
                      strike=contracts_strike, expiry=date(2026, 1, 16), underlying="AAPL")]


def _write_call(acct, contracts=1):
    return acct.submit_option_order(_call_legs(), contracts, order_type="limit",
                                    limit_price=1.2, option_strategy="covered_call")


def _marker(txn_id):
    return (get_instance(Transaction, txn_id).meta_data or {}).get(cp.PROTECTION_SUSPENDED_KEY)


def _sell_rows(txn_id):
    from ba2_common.core.trade_store import orders_where
    return [o for o in orders_where(transaction_id=txn_id)
            if o.side == OrderDirection.SELL and o.asset_class != AssetClass.OPTION]


def _working_protection(txn_id):
    dead = OrderStatus.get_terminal_statuses() | {OrderStatus.PENDING_CANCEL}
    return [o for o in _sell_rows(txn_id)
            if o.status not in dead and TransactionHelper.is_resting_protection(o)]


def _expire_the_call(call_order):
    """The call is over: its transaction is CLOSED, as ``_settle_option_leg`` leaves it."""
    txn = get_instance(Transaction, call_order.transaction_id)
    txn.status = TransactionStatus.CLOSED
    update_instance(txn)
    row = get_instance(TradingOrder, call_order.id)
    row.status = OrderStatus.EXPIRED
    update_instance(row)


# ===========================================================================================
# 1. writing the call
# ===========================================================================================
class TestWritingTheCall:
    def test_the_resting_stop_is_cancelled_before_the_call_is_sent(self, world):
        """The headline: this exact sequence is what the broker needs."""
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        assert broker.available == 0          # the observed state: every share is held

        order = _write_call(acct)

        assert order is not None, "the call was not submitted"
        assert broker.calls.index("cancel:bkr-stop") < broker.calls.index("submit_option")
        assert broker.available == 0         # the call now holds the shares instead
        assert restaged == [], "nothing may be re-staged on shares the call pledges"

    def test_the_protection_is_suspended_not_removed(self, world):
        acct, broker, _ = world
        txn, leg = _equity_with_stop(acct, broker, stop=140.0, take_profit=180.0, oco=True)

        assert _write_call(acct) is not None

        stored = get_instance(Transaction, txn.id)
        assert (stored.stop_loss, stored.take_profit) == (140.0, 180.0)
        marker = _marker(txn.id)
        assert marker["reason"] == cp.REASON_PLEDGED
        assert marker["stage"] == cp.STAGE_PLEDGED
        assert marker["cancelled_order_ids"] == [leg.id]
        assert _working_protection(txn.id) == []

    def test_no_resting_protection_means_nothing_is_cancelled(self, world):
        """Shares the platform has not committed to a leg are the broker's business, as before."""
        acct, broker, _ = world
        txn = create_transaction(symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED, open_price=150.0)
        create_trading_order(account_id=acct.id, symbol="AAPL", quantity=100.0,
                             side=OrderDirection.BUY, status=OrderStatus.FILLED,
                             filled_qty=100.0, transaction_id=txn.id)

        assert _write_call(acct) is not None
        assert [c for c in broker.calls if c.startswith("cancel")] == []
        assert _marker(txn.id) is None

    def test_only_the_lot_that_is_needed_is_suspended(self, world):
        """Two 100-share lots, one contract: the first lot (FIFO) is suspended, the second
        keeps its stop."""
        acct, broker, _ = world
        broker.held = 200
        first, first_leg = _equity_with_stop(acct, broker, broker_id="stop-a")
        second, second_leg = _equity_with_stop(acct, broker, broker_id="stop-b")

        assert _write_call(acct) is not None

        assert _marker(first.id)["stage"] == cp.STAGE_PLEDGED
        assert _marker(second.id) is None
        assert "cancel:stop-b" not in broker.calls
        assert [o.id for o in _working_protection(second.id)] == [second_leg.id]

    def test_a_call_that_cannot_be_covered_even_by_suspending_cancels_nothing(self, world):
        """Two contracts need 200 shares and the account holds them, but 100 are held for an
        order that is not the platform's (a manual stop); the platform's own leg frees only 100.
        Cancelling would be churn for a call the broker will refuse."""
        acct, broker, _ = world
        broker.held = 200
        broker.seed("manual-stop", 100)
        txn, leg = _equity_with_stop(acct, broker)

        assert _write_call(acct, contracts=2) is None
        assert [c for c in broker.calls if c.startswith("cancel")] == []
        assert "submit_option" not in broker.calls
        assert _marker(txn.id) is None
        assert [o.id for o in _working_protection(txn.id)] == [leg.id]
        assert broker.resting == {"manual-stop": 100, "bkr-stop": 100}

    def test_an_unreadable_available_quantity_cancels_nothing_and_submits_nothing(self, world):
        """Unknown is not zero: with qty_available unmeasurable it cannot be known whether
        the stop has to go, so nothing is touched."""
        acct, broker, _ = world
        txn, leg = _equity_with_stop(acct, broker)
        broker.position_error = ConnectionError("getaddrinfo failed")

        assert _write_call(acct) is None
        assert [c for c in broker.calls if c.startswith("cancel")] == []
        assert "submit_option" not in broker.calls
        assert _marker(txn.id) is None


# ===========================================================================================
# 2. the call is not accepted -> never leave the stock unprotected
# ===========================================================================================
class TestAFailedCallRestagesTheStop:
    def test_a_broker_rejection_restages_the_stop_at_the_stored_price(self, world):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker, stop=140.0)
        broker.reject_call = RuntimeError("APIError: option trading not enabled")

        assert _write_call(acct) is None

        assert broker.calls.index("cancel:bkr-stop") < broker.calls.index("submit_option")
        assert len(restaged) == 1, "the stop was not re-staged"
        new = restaged[0]
        assert (new.order_type, new.side, new.quantity, new.stop_price) == \
            (OrderType.SELL_STOP, OrderDirection.SELL, 100.0, 140.0)
        assert get_instance(Transaction, txn.id).stop_loss == 140.0
        assert _marker(txn.id) is None, "a restored position is not 'suspended'"
        assert [o.id for o in _working_protection(txn.id)] == [new.id]

    def test_a_cancel_that_never_lands_aborts_the_call_and_keeps_the_original_stop(self, world):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        broker.cancel_lands = False          # accepted, never executed: the stop is still live

        assert _write_call(acct) is None

        assert "submit_option" not in broker.calls, "a call was sent against shares still held"
        assert restaged == [], "the original stop is still working; nothing needs re-staging"
        assert _marker(txn.id) is None
        assert get_instance(TradingOrder, leg.id).status == OrderStatus.NEW, (
            "the row must show the stop as the working order it still is")

    def test_a_restage_the_broker_refuses_is_loud_and_retried(self, world, monkeypatch):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        broker.reject_call = RuntimeError("APIError: option trading not enabled")

        def refusing(order, **kwargs):
            fresh = get_instance(TradingOrder, order.id)
            fresh.status = OrderStatus.ERROR
            update_instance(fresh)
            return None
        monkeypatch.setattr(acct, "submit_order", refusing)

        assert _write_call(acct) is None

        marker = _marker(txn.id)
        assert marker is not None and marker["stage"] == cp.STAGE_REARM_FAILED, (
            "a stock that could not be re-protected must stay marked, so the refresh retries")
        assert not cp.is_protection_suspended(get_instance(Transaction, txn.id)), (
            "a failed re-arm must not keep blocking the safety net")


# ===========================================================================================
# 3. while the call is open
# ===========================================================================================
class TestWhileTheCallIsOpen:
    def _suspended(self, world):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker, stop=140.0)
        call = _write_call(acct)
        assert call is not None
        return acct, broker, restaged, txn, call

    def test_adjusting_the_stop_stores_it_and_stages_nothing(self, world):
        acct, broker, restaged, txn, call = self._suspended(world)

        assert acct.adjust_tp_sl(get_instance(Transaction, txn.id), None, 145.0,
                                 source="ruleset") is True

        assert get_instance(Transaction, txn.id).stop_loss == 145.0
        assert restaged == []
        assert _working_protection(txn.id) == []
        assert _marker(txn.id)["stage"] == cp.STAGE_PLEDGED

    def test_a_protective_leg_reaching_the_submit_seam_is_not_sent(self, world):
        """The net under every path that writes a leg (the waiting-trigger sweep, the
        PENDING dependents, the inline creators): the broker call never happens."""
        acct, broker, restaged, txn, call = self._suspended(world)
        calls_before = list(broker.calls)
        leg = create_trading_order(
            account_id=acct.id, symbol="AAPL", quantity=100.0, side=OrderDirection.SELL,
            order_type=OrderType.SELL_STOP, status=OrderStatus.WAITING_TRIGGER,
            stop_price=140.0, transaction_id=txn.id,
            comment=TransactionHelper.tpsl_comment("SL", acct.id, txn.id, 1))
        del restaged[:]

        # The real Alpaca send, not the fixture's stand-in for it.
        result = acct._submit_order_impl(leg)

        assert result is None
        assert broker.calls == calls_before
        row = get_instance(TradingOrder, leg.id)
        assert row.status == OrderStatus.ERROR
        assert "protection_suspended" in (row.comment or "")

    def test_the_breached_stop_safety_net_skips_the_pledged_lot(self, world):
        from ba2_trade_platform.core.TradeManager import TradeManager
        acct, broker, restaged, txn, call = self._suspended(world)
        closed = []
        acct.is_market_open = lambda: True
        acct.get_instrument_current_price = lambda symbols, *a, **k: {s: 100.0 for s in symbols}
        acct.get_signed_position_quantity = lambda s: 100.0
        acct.close_transaction = lambda tid: closed.append(tid) or {
            "success": True, "message": "", "canceled_count": 0, "deleted_count": 0,
            "close_order_id": None}

        TradeManager()._force_close_breached_stops(acct)

        assert closed == [], "the sweep market-sold shares pledged to an open short call"

    def test_the_breached_stop_safety_net_still_guards_a_lot_whose_rearm_failed(self, world):
        """A marker that no longer blocks must not exempt the stock from the safety net."""
        from ba2_trade_platform.core.TradeManager import TradeManager
        acct, broker, restaged, txn, call = self._suspended(world)
        row = get_instance(Transaction, txn.id)
        row.meta_data = {cp.PROTECTION_SUSPENDED_KEY: {
            **row.meta_data[cp.PROTECTION_SUSPENDED_KEY], "stage": cp.STAGE_REARM_FAILED}}
        update_instance(row)
        closed = []
        acct.is_market_open = lambda: True
        acct.get_instrument_current_price = lambda symbols, *a, **k: {s: 100.0 for s in symbols}
        acct.get_signed_position_quantity = lambda s: 100.0
        acct.close_transaction = lambda tid: closed.append(tid) or {
            "success": True, "message": "", "canceled_count": 0, "deleted_count": 0,
            "close_order_id": None}

        TradeManager()._force_close_breached_stops(acct)

        assert closed == [txn.id]

    def test_the_refresh_leaves_a_pledged_lot_suspended(self, world):
        acct, broker, restaged, txn, call = self._suspended(world)
        calls_before = list(broker.calls)

        assert acct.reconcile_suspended_protection() == 0

        assert restaged == []
        assert _marker(txn.id)["stage"] == cp.STAGE_PLEDGED
        assert broker.calls == calls_before

    def test_an_unreadable_pledge_keeps_the_lot_suspended(self, world, monkeypatch):
        """Unknown is not zero: with the option book unreadable it cannot be said the call is gone."""
        acct, broker, restaged, txn, call = self._suspended(world)
        monkeypatch.setattr(acct, "shares_pledged_to_short_calls", lambda u: None)

        assert acct.reconcile_suspended_protection() == 0
        assert restaged == []
        assert _marker(txn.id)["stage"] == cp.STAGE_PLEDGED

    def test_a_call_still_being_written_is_not_re_armed_from_under_the_writer(self, world):
        """``releasing`` is the window between the cancel and the call reaching the broker. A
        refresh in that window must not put the stop back."""
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        row = get_instance(Transaction, txn.id)
        from datetime import datetime, timezone
        row.meta_data = {cp.PROTECTION_SUSPENDED_KEY: {
            "reason": cp.REASON_PLEDGED, "stage": cp.STAGE_RELEASING, "symbol": "AAPL",
            "since": datetime.now(timezone.utc).isoformat(), "cancelled_order_ids": []}}
        update_instance(row)

        assert acct.reconcile_suspended_protection() == 0
        assert restaged == []


# ===========================================================================================
# 4. the call is gone
# ===========================================================================================
class TestWhenTheCallIsGone:
    def test_a_call_that_expires_worthless_re_stages_the_stop_from_the_stored_values(self, world):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker, stop=140.0, take_profit=180.0, oco=True)
        call = _write_call(acct)
        assert call is not None and restaged == []
        # the stop moved while the call was open: the stored value is what comes back
        acct.adjust_tp_sl(get_instance(Transaction, txn.id), None, 145.0, source="ruleset")
        _expire_the_call(call)
        broker.resting = {k: v for k, v in broker.resting.items() if not k.startswith("call-")}

        assert acct.reconcile_suspended_protection() == 1

        assert len(restaged) == 1
        new = restaged[0]
        assert (new.order_type, new.quantity, new.stop_price, new.limit_price) == \
            (OrderType.OCO, 100.0, 145.0, 180.0)
        assert _marker(txn.id) is None
        assert get_instance(Transaction, txn.id).stop_loss == 145.0

    def test_a_call_bought_back_re_stages_the_stop(self, world):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        call = _write_call(acct)
        # the buy-to-close fills: the call is flat and its transaction closed
        _expire_the_call(call)
        broker.resting.pop("call-1", None)

        assert acct.reconcile_suspended_protection() == 1
        assert [(o.order_type, o.stop_price) for o in restaged] == [(OrderType.SELL_STOP, 140.0)]

    def test_a_re_arm_the_broker_refuses_stays_marked_and_is_retried(self, world, monkeypatch):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        call = _write_call(acct)
        _expire_the_call(call)
        broker.resting.pop("call-1", None)
        good_submit = acct.submit_order

        def refusing(order, **kwargs):
            fresh = get_instance(TradingOrder, order.id)
            fresh.status = OrderStatus.ERROR
            update_instance(fresh)
            return None
        monkeypatch.setattr(acct, "submit_order", refusing)

        assert acct.reconcile_suspended_protection() == 0
        assert _marker(txn.id)["stage"] == cp.STAGE_REARM_FAILED

        monkeypatch.setattr(acct, "submit_order", good_submit)
        assert acct.reconcile_suspended_protection() == 1
        assert _marker(txn.id) is None
        assert len(restaged) == 1

    def test_shares_called_away_close_the_transaction_and_restage_nothing(self, world):
        """Assignment: the equity transaction closes (``_settle_called_away``) and its marker is
        moot. The refresh must not put a stop under shares that are gone."""
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        call = _write_call(acct)
        _expire_the_call(call)                      # the call transaction ends ...
        gone = get_instance(Transaction, txn.id)
        gone.status = TransactionStatus.CLOSED      # ... and the shares are called away
        update_instance(gone)
        broker.held = 0
        broker.resting.clear()

        assert acct.reconcile_suspended_protection() == 0
        assert restaged == []

    def test_a_partial_unwind_keeps_only_the_lots_the_remaining_calls_need(self, world):
        """Two lots suspended for two calls; one call ends -> one lot is re-armed, the other stays."""
        acct, broker, restaged = world
        broker.held = 200
        first, _ = _equity_with_stop(acct, broker, broker_id="stop-a")
        second, _ = _equity_with_stop(acct, broker, broker_id="stop-b")
        call_a = _write_call(acct)
        call_b = _write_call(acct)
        assert call_a is not None and call_b is not None
        assert _marker(first.id) and _marker(second.id)

        _expire_the_call(call_a)
        broker.resting.pop("call-1", None)

        # The platform does not track which call pledges which lot, so the lots are kept
        # oldest first until their size covers what is still pledged (100 shares).
        assert acct.reconcile_suspended_protection() == 1
        assert len(restaged) == 1
        assert _marker(first.id)["stage"] == cp.STAGE_PLEDGED and _marker(second.id) is None


# ===========================================================================================
# 5. the shared seam
# ===========================================================================================
class TestTheSharedSeam:
    """``OptionsAccountInterface._release_cover_protection_for_short_calls``: a default no-op
    (backtest, IBKR, TastyTrade are untouched), a token confirmed on success and aborted on
    any failure."""

    class Token:
        def __init__(self):
            self.confirmed, self.aborted = [], []

        def confirm(self, order):
            self.confirmed.append(order)

        def abort(self, why):
            self.aborted.append(why)

    def _account(self, monkeypatch, token=None, release_raises=None):
        from tests.conftest import MockAccount
        acct_def = create_account_definition()
        account = MockAccount(acct_def.id)
        account._positions = [SimpleNamespace(
            symbol="AAPL", qty=100.0, qty_available=100.0, side=OrderDirection.BUY,
            asset_class="us_equity")]
        released = []

        def hook(legs, quantity, option_strategy):
            released.append((quantity, option_strategy))
            if release_raises is not None:
                raise release_raises
            return token
        monkeypatch.setattr(account, "_release_cover_protection_for_short_calls", hook)
        return account, released

    def test_the_default_hook_releases_nothing(self):
        from tests.conftest import MockAccount
        acct_def = create_account_definition()
        assert MockAccount(acct_def.id)._release_cover_protection_for_short_calls(
            _call_legs(), 1, "covered_call") is None

    def test_a_submitted_call_confirms_the_token(self, monkeypatch):
        token = self.Token()
        account, released = self._account(monkeypatch, token)

        order = _write_call(account)

        assert order is not None
        assert released == [(1, "covered_call")]
        assert token.confirmed == [order] and token.aborted == []

    def test_a_failed_submission_aborts_the_token(self, monkeypatch):
        token = self.Token()
        account, _ = self._account(monkeypatch, token)

        def boom(*a, **k):
            raise RuntimeError("broker said no")
        monkeypatch.setattr(account, "_submit_option_order_impl", boom)

        assert _write_call(account) is None
        assert token.confirmed == [] and len(token.aborted) == 1
        assert "broker said no" in token.aborted[0]

    def test_a_refused_release_submits_nothing(self, monkeypatch):
        account, _ = self._account(monkeypatch,
                                   release_raises=cp.CoverReleaseRefused("cannot free shares"))

        assert _write_call(account) is None
        assert account._submitted_option_orders == []

    def test_only_a_covered_call_asks_for_a_release(self, monkeypatch):
        token = self.Token()
        account, released = self._account(monkeypatch, token)
        put = [OptionLeg(contract_symbol="AAPL260116P00150000", side=OrderDirection.SELL,
                         position_intent="sell_to_open", option_type=OptionRight.PUT,
                         strike=150.0, expiry=date(2026, 1, 16), underlying="AAPL")]

        account.submit_option_order(put, 1, order_type="limit", limit_price=1.0,
                                    option_strategy="cash_secured_put")

        assert released == []


# ===========================================================================================
# 6. the account refresh
# ===========================================================================================
class TestTheAccountRefreshReArms:
    def test_the_refresh_re_arms_before_the_breached_stop_sweep(self, monkeypatch):
        """Order matters: the option events that end a call are reconciled first, the suspended
        lots are re-armed next, and only then does the safety net look at stops."""
        from ba2_trade_platform.core import TradeManager as tm_mod
        import ba2_trade_platform.modules.accounts as accounts_mod

        events = []

        class RefreshAccount:
            def __init__(self, id):
                self.id = id

            def refresh_positions(self):
                events.append("positions")

            def reconcile_suspended_protection(self):
                events.append("reconcile_suspended_protection")
                return 1

            def is_market_open(self):
                return False

        manager = tm_mod.TradeManager()
        acct_def = create_account_definition(provider="RefreshAccount")
        monkeypatch.setattr(tm_mod, "get_all_instances", lambda model: [acct_def])
        monkeypatch.setattr(accounts_mod, "get_account_class", lambda provider: RefreshAccount)
        monkeypatch.setattr(manager, "_publish_market_regime", lambda: None)
        monkeypatch.setattr(manager, "_publish_benchmark_regime", lambda: None)
        monkeypatch.setattr(manager, "_reconcile_account_option_activities",
                            lambda account: events.append("option_events"))
        monkeypatch.setattr(manager, "_force_close_breached_stops",
                            lambda account: events.append("breach_sweep"))
        monkeypatch.setattr(manager, "_check_all_waiting_trigger_orders", lambda: None)
        monkeypatch.setattr(manager, "_check_all_washtrade_locked_orders", lambda: None)
        monkeypatch.setattr(manager, "_check_stranded_waiting_transactions", lambda: None)

        manager.refresh_accounts()

        assert events.index("option_events") < events.index("reconcile_suspended_protection") \
            < events.index("breach_sweep")

    def test_the_partial_fill_reprotection_skips_a_pledged_lot(self, world):
        from ba2_trade_platform.core.TradeManager import TradeManager
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker, oco=True, take_profit=180.0)
        assert _write_call(acct) is not None
        row = get_instance(TradingOrder, leg.id)
        row.status = OrderStatus.PARTIALLY_FILLED       # the stranded-OCO shape it looks for
        update_instance(row)
        adjusted = []
        acct.adjust_tp_sl = lambda *a, **k: adjusted.append(a) or True

        TradeManager()._reprotect_partially_filled_exits(acct)

        assert adjusted == []


class TestTheBrokersThatAreNotAlpaca:
    def test_only_alpaca_releases_cover_for_a_covered_call(self):
        """IBKR's and TastyTrade's hold rules for a resting protective leg are unverified (IBKR is
        written against ``tests/ibkr_fakes.py``, no live account), so they inherit the default
        seam -- which releases nothing -- and their behaviour is unchanged."""
        from ba2_trade_platform.core.interfaces.OptionsAccountInterface import (
            OptionsAccountInterface)
        default = OptionsAccountInterface._release_cover_protection_for_short_calls
        assert AlpacaAccount._release_cover_protection_for_short_calls is not default

        for module, name in (("IBKRAccount", "IBKRAccount"),
                             ("TastyTradeAccount", "TastyTradeAccount")):
            try:
                cls = getattr(__import__(f"ba2_trade_platform.modules.accounts.{module}",
                                         fromlist=[name]), name)
            except ImportError:
                pytest.skip(f"{name} needs a broker SDK this environment does not have")
            # TastyTrade cannot write options at all (it has no seam); IBKR inherits the default.
            assert getattr(cls, "_release_cover_protection_for_short_calls", default) is default, name
            assert not hasattr(cls, "reconcile_suspended_protection"), name


class TestACallHandedBackDead:
    def test_a_call_the_broker_returns_already_rejected_restages_the_stop(self, world, monkeypatch):
        acct, broker, restaged = world
        txn, leg = _equity_with_stop(acct, broker)
        monkeypatch.setattr(
            acct, "alpaca_order_to_tradingorder",
            lambda raw: SimpleNamespace(
                status=OrderStatus.REJECTED if raw.id.startswith("call-") else _status_of(raw).status,
                filled_qty=None, open_price=None))

        assert _write_call(acct) is not None        # the order row exists, REJECTED

        assert len(restaged) == 1, "a rejected call left the stock unprotected until the refresh"
        assert _marker(txn.id) is None
