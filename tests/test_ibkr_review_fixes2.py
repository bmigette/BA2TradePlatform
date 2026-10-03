"""Second review round (2026-10-03, re-review of 4cb38821): written as FAILING tests first, against a
FakeIB that now reproduces the REAL ib_async 2.1.0 behaviours the first fake had hidden (and the first
fixes were therefore built on):

* a modification is acknowledged by a 'Modified' log entry ONLY when the echoed status is exactly
  'Submitted': a resting 'PreSubmitted' stop gets none, although IB applied it;
* a rejected order turns 'Cancelled' (not 'Inactive') with the error in the log; EVERY warning
  (105 110 165 321 329 399 404 434 492 and all 21xx) turns it 'ValidationError' while it stays live;
* openOrders / completedOrders / positions have ONE pending future each: a second concurrent identical
  request steals the first one's answer;
* order ids restart after a reconnect.

``TestRealIbAsync`` drives the REAL library (socket stubbed) so the fake cannot drift from it again.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from ib_async import IB, Order, OrderState, Stock

from ba2_common.core import ibkr_mapping as M
from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.modules.accounts.IBKRAccount import BrokerOrderView, IBKRAccount
from ba2_trade_platform.modules.accounts.ibkr_runtime import (
    IBKRConnectionError, IBKROrderRejected, IBKRRuntime)
from tests.factories import create_transaction
from tests.ibkr_helpers import ibkr_logs, make_account  # noqa: F401
from tests.test_ibkr_lifecycle import fresh, oco_row, rows
from tests.test_ibkr_orders import new_order, submit


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    yield account, fake, aapl
    account.close()


def ref_of(account, row):
    return account._order_ref_for_row(get_instance(TradingOrder, row.id), account.id)


@pytest.fixture
def activity(monkeypatch):
    """Captures ``log_activity`` calls (the real one queues to a DB worker)."""
    calls = []
    import ba2_common.core.db as cdb
    monkeypatch.setattr(cdb, "log_activity", lambda *a, **k: calls.append((a, k)))
    return calls


def age(row_id, minutes=60):
    r = get_instance(TradingOrder, row_id)
    r.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    update_instance(r)


# ======================================================================= 1  adoption only of live orders
class TestAdoptionOnlyOfLiveOrders:
    def test_stop_through_market_auto_retry_places_the_market_order(self, world):
        """The shared reject -> MARKET retry re-submits the SAME row: the dead stop carries the same
        orderRef, and must not be 'adopted' (probe p1 E: row ended CANCELED/MARKET, nothing placed)."""
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        fake.behaviors.append(("reject", 201, "Order rejected - reason:The stop price is above the "
                                              "current market price"))
        row = new_order(account, side=OrderDirection.SELL, order_type=OrderType.SELL_STOP, stop=150.0,
                        transaction_id=txn.id)
        out = submit(account, row)
        time.sleep(0.2)
        assert out is not None, "the auto-retry must hand back the market order"
        placed = [p["orderType"] for p in fake.placed if not p["modification"]]
        assert placed == ["STP", "MKT"], placed
        f = fresh(row)
        assert f.order_type == OrderType.MARKET and f.status in (OrderStatus.ACCEPTED,
                                                                 OrderStatus.PENDING_NEW)
        assert f.broker_order_id
        assert fake.placed[0]["ref"] != fake.placed[1]["ref"]       # a NEW nonce: the old ref is a dead order

    def test_ui_retry_after_a_legitimate_rejection_places_a_new_order(self, world):
        account, fake, _ = world
        fake.behaviors.append(("reject", 201, "Order rejected - reason:Insufficient buying power"))
        row = new_order(account)
        assert submit(account, row) is None
        assert fresh(row).status == OrderStatus.ERROR and not fresh(row).broker_order_id
        n = len(fake.placed)
        out = submit(account, fresh(row))                      # what overview._confirm_retry_orders does
        assert out is not None and len(fake.placed) == n + 1
        f = fresh(row)
        assert f.status in (OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW) and f.broker_order_id
        assert fake.placed[0]["ref"] != fake.placed[-1]["ref"]

    def test_retry_of_a_row_whose_order_is_live_adopts_it_no_duplicate(self, world):
        account, fake, _ = world
        row = new_order(account)
        assert submit(account, row) is not None
        x = fresh(row)
        x.broker_order_id, x.status = None, OrderStatus.ERROR         # the answer was lost
        update_instance(x)
        n = len(fake.placed)
        out = submit(account, fresh(row))
        assert len(fake.placed) == n, "a live order with this ref must be adopted, not duplicated"
        assert out is not None and fresh(row).status == OrderStatus.ACCEPTED

    def test_a_live_order_that_does_not_match_is_never_adopted_and_never_duplicated(self, world):
        account, fake, _ = world
        row = new_order(account, qty=10.0)
        assert submit(account, row) is not None
        x = fresh(row)
        x.broker_order_id, x.status, x.quantity = None, OrderStatus.ERROR, 3.0   # a DIFFERENT order now
        update_instance(x)
        n = len(fake.placed)
        assert submit(account, fresh(row)) is None
        assert len(fake.placed) == n
        f = fresh(row)
        assert f.status == OrderStatus.ERROR and "differs" in f.comment

    def test_a_cancelled_order_that_traded_is_adopted(self, world):
        account, fake, _ = world
        row = new_order(account)
        fake.behaviors.append(("partial_then_cancel", 4))
        submit(account, row)
        x = fresh(row)
        x.broker_order_id, x.status, x.filled_qty = None, OrderStatus.ERROR, None
        update_instance(x)
        n = len(fake.placed)
        submit(account, fresh(row))
        assert len(fake.placed) == n                              # the fill is real: no second order

    def test_a_first_attempt_makes_no_lookup_at_all(self, world):
        account, fake, _ = world
        account.get_positions()                                    # connect
        fake.request_log.clear()
        assert submit(account, new_order(account)) is not None
        assert "openOrders" not in fake.request_log and "completedOrders" not in fake.request_log

    def test_the_callers_order_object_carries_the_nonce(self, world):
        account, fake, _ = world
        row = new_order(account)
        submit(account, row)
        assert row.data["ibkr_nonce"] == fresh(row).data["ibkr_nonce"]


# ======================================================================= 2  modify confirmation
class TestModifyConfirmation:
    def _oco(self, account, fake):
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        child = rows(account, parent_order_id=parent.id)[0]
        return parent, child

    @staticmethod
    def _patch(**kw):
        return TradingOrder(symbol="AAPL", quantity=10.0, side=OrderDirection.SELL,
                            order_type=OrderType.SELL_STOP_LIMIT, **kw)

    def test_a_resting_presubmitted_stop_is_confirmed_and_kept(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 3.0)
        parent, child = self._oco(account, fake)
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        assert sl.orderStatus.status == "PreSubmitted"            # the awkward, typical state
        out = account.modify_order(str(child.id), self._patch(stop_price=95.0, limit_price=94.5))
        assert out is not None, "IB applied it: it must not be reported as failed"
        assert sl.order.auxPrice == 95.0 and sl.order.lmtPrice == 94.5   # NOT rolled back
        f = fresh(child)
        assert f.stop_price == 95.0 and f.limit_price == 94.5
        assert not fake.cancel_requests

    def test_a_submitted_stop_inside_the_session_is_confirmed_by_the_modified_log(self, world, monkeypatch):
        account, fake, _ = world
        fake.regular_session = True
        parent, child = self._oco(account, fake)
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        assert sl.orderStatus.status == "Submitted"
        assert account.modify_order(str(child.id), self._patch(stop_price=95.0, limit_price=94.5)) is not None
        assert any(e.message == "Modified" for e in sl.log)

    def test_applied_without_any_echo_is_confirmed_by_rereading_the_open_order(self, world, monkeypatch):
        account, fake, _ = world
        parent, child = self._oco(account, fake)
        fake.modify_behavior = "applied_no_echo"                  # IB applied it, told nobody
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        out = account.modify_order(str(child.id), self._patch(stop_price=95.0, limit_price=94.5))
        assert out is not None and sl.order.auxPrice == 95.0

    def test_an_ignored_modification_rolls_back_and_stores_nothing(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.6)
        parent, child = self._oco(account, fake)
        fake.modify_behavior = "silent"
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        assert account.modify_order(str(child.id), self._patch(stop_price=95.0, limit_price=94.5)) is None
        assert sl.order.auxPrice == 90.0 and sl.order.lmtPrice == pytest.approx(89.55)   # rolled back
        assert fresh(child).stop_price == 90.0

    def test_a_refused_modification_warning_rolls_back(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.6)
        parent, child = self._oco(account, fake)
        fake.modify_behavior = ("reject", 329, "Order modify failed. Cannot change to the new order type.")
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        assert account.modify_order(str(child.id), self._patch(stop_price=95.0, limit_price=94.5)) is None
        assert sl.order.auxPrice == 90.0
        assert fresh(child).stop_price == 90.0

    def test_stop_adjustment_through_the_template_stays_in_place(self, world, monkeypatch):
        """With the old rule every PreSubmitted stop adjustment degraded to cancel + replace (an
        unprotected window): the exit must move in place."""
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 3.0)
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        from ba2_trade_platform.core.models import Transaction
        entry = new_order(account, transaction_id=txn.id, status=OrderStatus.FILLED)
        assert account.adjust_tp_sl(get_instance(Transaction, txn.id), 125.0, 95.0, source="ruleset") is True
        assert not fake.cancel_requests
        assert len([p for p in fake.placed if p["modification"]]) == 2


# ======================================================================= 3  warnings are not rejections
class TestWarningsAreNotRejections:
    @pytest.mark.parametrize("code", [105, 110, 165, 321, 329, 399, 404, 434, 492, 10349, 2100, 2111, 2148,
                                      2161, 2199])
    def test_every_ib_async_warning_code_is_a_warning(self, code):
        assert M.error_severity(code) in ("order_warning", "info")
        assert M.error_severity(code) != "order"

    def test_a_2161_warning_then_submitted_is_an_acknowledged_order(self, world):
        account, fake, _ = world
        fake.behaviors.append(("warn", 2161, "Order Message: example IB 21xx warning"))
        row = new_order(account)
        out = submit(account, row)
        assert out is not None
        assert fresh(row).status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED)
        settle = time.time() + 2
        while time.time() < settle and fake.trades()[-1].orderStatus.status != "Submitted":
            time.sleep(0.05)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED and len(fake.placed) == 1

    def test_a_non_warning_error_cancels_and_is_the_rejection(self, world):
        account, fake, _ = world
        fake.behaviors.append(("reject", 201, "Order rejected - reason:Insufficient buying power"))
        row = new_order(account)
        assert submit(account, row) is None
        f = fresh(row)
        assert f.status == OrderStatus.ERROR and "201" in f.comment

    def test_110_on_a_new_order_cancels_it_in_ib_async_so_it_is_a_rejection(self, world):
        account, fake, _ = world
        # ib_async: 110 is a warning EXCEPT while the trade is PendingSubmit (then the order is cancelled)
        fake.behaviors.append(("reject", 110, "The price does not conform to the minimum price variation"))
        row = new_order(account, order_type=OrderType.BUY_LIMIT, limit=100.123456)
        assert submit(account, row) is None
        assert fresh(row).status == OrderStatus.ERROR

    def test_321_read_only_gateway_on_a_new_order_is_a_refusal_not_a_hang(self, world, monkeypatch):
        """321 is a warning to ib_async (the trade stays PendingSubmit), but with 'read-only' in the
        text the order can never have been placed: refuse it instead of waiting forever."""
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.6)
        fake.behaviors.append(("lost_with_warning", 321, "Error validating request:-'bN' : cause - "
                                                          "API interface is currently in Read-Only mode."))
        row = new_order(account)
        assert submit(account, row) is None
        f = fresh(row)
        assert f.status == OrderStatus.ERROR and "unauthorized" in f.comment

    def test_a_partial_fill_then_cancel_inside_the_ack_window_is_not_an_error(self, world):
        """probe p5: 4 of 10 bought, then 'Cancelled': the row must keep the shares, not turn ERROR."""
        account, fake, _ = world
        fake.behaviors.append(("partial_then_cancel", 4))
        row = new_order(account)
        out = submit(account, row)
        f = fresh(row)
        assert f.status != OrderStatus.ERROR, f.comment
        assert f.filled_qty == 4.0 and f.broker_order_id
        account.refresh_orders()
        g = fresh(row)
        assert g.filled_qty == 4.0 and g.status != OrderStatus.ERROR


# ======================================================================= 4  concurrent reads
class TestConcurrentReads:
    def test_the_fake_reproduces_the_stolen_answer(self, world):
        account, fake, _ = world
        account.get_positions()
        rt = account._runtime()
        fake.request_delay = 0.2

        async def both(ib):
            a = asyncio.ensure_future(asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 1.0))
            b = asyncio.ensure_future(asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 1.0))
            return await asyncio.gather(a, b, return_exceptions=True)
        first, second = rt.call(both, timeout=5, op="probe")
        assert isinstance(first, (TimeoutError, asyncio.TimeoutError)) and isinstance(second, list)

    def test_two_concurrent_refreshes_both_succeed(self, world):
        account, fake, _ = world
        submit(account, new_order(account))
        fake.request_delay = 0.15
        results = []

        def read():
            # the order-book read is what refresh_orders does on the IB thread (the DB half cannot run on
            # a bare test thread: in-memory SQLite is per-thread)
            try:
                results.append(len(account._call(account._read_order_book, op="book", timeout=30.0).views))
            except Exception as e:  # noqa: BLE001
                results.append(e)
        threads = [threading.Thread(target=read) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert len(results) == 3 and all(isinstance(r, int) for r in results), results

    def test_two_concurrent_position_reads_both_succeed(self, world):
        account, fake, aapl = world
        fake.add_position(aapl, 10, 100.0, mark=101.0)
        fake.request_delay = 0.15
        out = []
        threads = [threading.Thread(target=lambda: out.append(account.get_positions())) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert all(o is not None and len(o) == 1 for o in out), out

    def test_refresh_racing_a_retry_lookup_does_not_time_the_placement_out(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_READ_TIMEOUT", 3.0)
        row = new_order(account)
        assert submit(account, row) is not None
        x = fresh(row)
        x.broker_order_id, x.status = None, OrderStatus.ERROR
        update_instance(x)
        fake.request_delay = 0.25
        res = {}

        def read():
            try:
                res["r"] = account._call(account._read_order_book, op="book", timeout=30.0)
            except Exception as e:  # noqa: BLE001
                res["r"] = e
        t = threading.Thread(target=read)
        t.start()
        time.sleep(0.05)
        out = submit(account, fresh(row))
        t.join(30)
        assert out is not None and fresh(row).status == OrderStatus.ACCEPTED, fresh(row).comment
        assert not isinstance(res["r"], Exception), res


# ======================================================================= 5  orphan stop after a rejected OCO
class TestOrphanStop:
    def test_a_rejected_take_profit_cancels_the_stop_and_waits_for_the_confirmation(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        fake.behaviors += ["accept", ("reject", 201, "Order rejected - price too far")]
        parent = oco_row(account, txn)
        assert submit(account, parent) is None
        assert fresh(parent).status == OrderStatus.ERROR
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        assert sl.orderStatus.status == "Cancelled", "the cancel must be CONFIRMED before the row says ERROR"

    def test_an_unconfirmed_cancel_never_leaves_a_live_stop_without_a_row(self, world, monkeypatch, ibkr_logs):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_CANCEL_ACK_TIMEOUT", 0.4)
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        fake.lose_cancels = True
        fake.behaviors += ["accept", ("reject", 201, "Order rejected - price too far")]
        parent = oco_row(account, txn)
        assert submit(account, parent) is None
        assert fresh(parent).status == OrderStatus.ERROR and "stop" in fresh(parent).comment.lower()
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        assert sl.orderStatus.status in ("PreSubmitted", "Submitted")        # really still live
        exits = [r for r in rows(account, transaction_id=txn.id) if r.id != parent.id]
        assert len(exits) == 1, "the live stop must have its own row"
        stop_row = exits[0]
        assert stop_row.order_type == OrderType.SELL_STOP_LIMIT
        assert stop_row.status in (OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW)
        assert stop_row.broker_order_id
        assert account._row_for_view(BrokerOrderView.from_trade(sl)).id == stop_row.id
        assert "ORPHAN" in ibkr_logs.text(logging.ERROR)
        account.refresh_orders()
        assert fresh(stop_row).status == OrderStatus.ACCEPTED


# ======================================================================= 6  rows pending forever
class TestRowsPendingForever:
    def _unconfirmed_row(self, account, fake, monkeypatch, behavior="lost"):
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.3)
        fake.behaviors.append(behavior)
        row = new_order(account)
        submit(account, row)
        assert fresh(row).status == OrderStatus.PENDING_NEW
        return row

    def test_an_order_that_never_reached_ib_is_settled_as_error_with_a_visible_entry(
            self, world, monkeypatch, activity):
        account, fake, _ = world
        row = self._unconfirmed_row(account, fake, monkeypatch)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.PENDING_NEW          # inside the grace period: untouched
        age(row.id)
        assert account.refresh_orders() is True
        f = fresh(row)
        assert f.status == OrderStatus.ERROR and "never reached IBKR" in f.comment
        assert any("never reached IBKR" in str(c) for c in activity)

    def test_the_same_after_a_reconnect_when_the_session_trade_is_gone(self, world, monkeypatch, activity):
        account, fake, _ = world
        row = self._unconfirmed_row(account, fake, monkeypatch)
        fake.reset_ids_on_connect = 100
        fake.simulate_disconnect()
        age(row.id)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ERROR

    def test_an_execution_under_its_ref_settles_it_as_filled_not_error(self, world, monkeypatch):
        account, fake, aapl = world
        row = self._unconfirmed_row(account, fake, monkeypatch)
        fake.make_fill_record(aapl, "BOT", 10, 100.0, order_ref=ref_of(account, row))
        age(row.id)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.FILLED and f.filled_qty == 10

    def test_an_unreadable_book_never_settles_anything(self, world, monkeypatch):
        account, fake, _ = world
        row = self._unconfirmed_row(account, fake, monkeypatch)
        age(row.id)
        fake.fail_calls["reqCompletedOrdersAsync"] = RuntimeError("boom")
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.PENDING_NEW

    def test_an_acknowledged_order_that_vanishes_stays_unresolved_and_is_flagged(
            self, world, monkeypatch, activity, ibkr_logs):
        """Once IB acknowledged it, absence proves nothing (the Gateway restart drops completed orders):
        never ERROR, never CANCELED -- an activity-log entry shows the operator what to reconcile."""
        account, fake, _ = world
        row = new_order(account)
        submit(account, row)
        fake.reset_ids_on_connect = 100
        fake.simulate_disconnect()
        account.get_positions()                                     # reconnect: the old trade is now "prior"
        fake.prior_trades.clear()                                   # the restart dropped it everywhere
        age(row.id)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED
        assert "UNRESOLVED" in ibkr_logs.text()
        assert any("UNRESOLVED" in str(c) for c in activity)


# ======================================================================= 7  buying power
class TestBuyingPowerEdges:
    def _set(self, fake, **tags):
        for tag, value in tags.items():
            if value is None:
                fake.account_rows = [r for r in fake.account_rows if r.tag != tag]
            else:
                fake.set_account_value(tag, str(value))

    @pytest.mark.parametrize("sma", [0, -2000])
    def test_a_zero_or_negative_sma_is_not_a_bound(self, world, sma):
        account, fake, _ = world
        self._set(fake, SMA=sma)
        snap = account.get_account_snapshot()
        assert snap.buying_power == 160000.0                       # AvailableFunds 80000 x 2
        assert snap.raw["bp_binding"] == "available_funds_x_mult"
        assert "sma_x_mult" not in snap.raw["bp_components"]

    def test_a_positive_sma_binds_when_it_is_the_smallest(self, world, ibkr_logs):
        account, fake, _ = world
        self._set(fake, SMA=30000)
        info = account.get_account_info()
        assert info["buying_power"] == 60000.0
        assert "sma_x_mult" in ibkr_logs.text(logging.INFO)          # which component bound is logged

    def test_a_margin_account_without_excess_liquidity_raises_instead_of_falling_back(self, world):
        account, fake, _ = world
        self._set(fake, ExcessLiquidity=None)
        with pytest.raises(Exception, match="buying power"):
            account.get_account_info()
        assert account.get_account_snapshot().buying_power is None

    def test_missing_available_funds_raises(self, world):
        account, fake, _ = world
        self._set(fake, AvailableFunds=None)
        with pytest.raises(Exception, match="buying power"):
            account.get_account_info()

    def test_the_expert_clamp_does_not_receive_cash_as_the_buying_power(self, world):
        account, fake, _ = world
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
        self._set(fake, ExcessLiquidity=None)
        got = MarketExpertInterface._get_actual_available_balance(account)
        assert got != 50000.0, "TotalCashValue must never stand in for an unknown buying power"


# ======================================================================= 8  connect
class TestConnectIsTrimmed:
    def test_startup_loads_only_the_account_feed_and_does_not_raise_on_slow_syncs(self, world):
        account, fake, _ = world
        account.get_positions()
        from ib_async import StartupFetch
        assert fake.connect_kwargs["fetchFields"] == StartupFetch.ACCOUNT_UPDATES
        assert fake.connect_kwargs["raiseSyncErrors"] is False

    def test_a_timed_out_positions_sync_still_connects_and_the_read_confirms(self, world):
        account, fake, aapl = world
        fake.add_position(aapl, 10, 100.0, mark=101.0)
        fake.positions_sync_fails = True
        assert account.get_positions() is None                      # the explicit confirmation fails
        fake.positions_sync_ok_on_request = True
        got = account.get_positions()
        assert got is not None and len(got) == 1


# ======================================================================= follow-ups
class TestFollowUps:
    def test_an_oco_with_only_the_stop_placed_does_not_hand_its_id_to_the_parent(self, world):
        account, fake, _ = world
        from ib_async import Order as IBOrder
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = oco_row(account, txn)
        nonce = account._ensure_nonce(parent.id)
        account.get_positions()
        sl = IBOrder(action="SELL", orderType="STP LMT", totalQuantity=10, auxPrice=90.0, lmtPrice=89.55,
                     orderRef=M.make_order_ref(account.id, parent.id, "SL", nonce=nonce),
                     account="DU1234567", ocaGroup="g", ocaType=2)
        aapl = fake.details["AAPL"][0].contract
        fake._on_loop(lambda: fake.placeOrder(aapl, sl))
        time.sleep(0.1)
        account._record_unconfirmed_placement(parent.id, {"order_ids": [sl.orderId], "sl_id": sl.orderId},
                                              "GTC", None, TimeoutError("probe"))
        p = fresh(parent)
        assert p.broker_order_id is None, "the parent is the TP leg: it must not take the stop's id"
        kids = rows(account, parent_order_id=parent.id)
        assert len(kids) == 1 and kids[0].broker_order_id
        account.refresh_orders()
        assert fresh(parent).limit_price == 120.0, "the stop's limit must never overwrite the TP price"
        assert fresh(kids[0]).status == OrderStatus.ACCEPTED

    def test_the_submit_budget_counts_every_read(self, world):
        account, fake, _ = world
        a = account
        one = a._submit_budget(1, reads=2)
        assert one == pytest.approx(2 * a._READ_TIMEOUT + 3.0 + 1 * a._ORDER_ACK_TIMEOUT)
        oco = a._submit_budget(2, reads=2, cancel_waits=1)
        assert oco == pytest.approx(2 * a._READ_TIMEOUT + 3.0 + 2 * a._ORDER_ACK_TIMEOUT
                                    + a._CANCEL_ACK_TIMEOUT)
        assert a._lookup_budget() == pytest.approx(2 * 2 * a._READ_TIMEOUT)   # open + completed, lock wait

    def test_an_inner_timeout_names_which_read_expired(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_READ_TIMEOUT", 0.3)
        fake.block_calls["reqContractDetailsAsync"] = 3.0
        row = new_order(account)
        assert submit(account, row) is None
        c = fresh(row).comment
        assert "inner wait" in c and "contract details" in c, c
        assert "total budget" not in c

    def test_the_outer_budget_is_reported_as_the_outer_budget(self, world):
        account, fake, _ = world
        account.get_positions()                               # connected: no connect allowance in the budget
        rt = account._runtime()

        async def slow(ib):
            await asyncio.sleep(5)
        with pytest.raises(TimeoutError, match="total budget"):
            rt.call(slow, timeout=0.3, op="slow")

    def test_runtime_lookup_inside_a_coroutine_never_closes_the_old_runtime_from_its_own_thread(
            self, world):
        account, fake, _ = world
        account.get_positions()
        old = account._runtime()
        seen = {}

        async def inside(ib):
            account._invalidate_settings_cache()            # settings edited while a call is in flight
            try:
                seen["rt"] = account._runtime()
            except Exception as e:  # noqa: BLE001
                seen["err"] = e
            return True
        assert old.call(inside, timeout=5, op="inside") is True
        assert "err" not in seen or not isinstance(seen["err"], RuntimeError), seen
        assert not old.closed or seen.get("rt") is not old

    def test_changing_the_client_id_with_live_orders_warns_loudly(self, world, ibkr_logs):
        account, fake, _ = world
        row = new_order(account)
        submit(account, row)
        from ba2_trade_platform.core.models import AccountSetting
        from ba2_trade_platform.core.db import get_db
        from sqlmodel import select
        with get_db() as session:
            s = session.exec(select(AccountSetting).where(AccountSetting.account_id == account.id,
                                                          AccountSetting.key == "client_id")).first()
            s.value_float = 8.0
            session.add(s)
            session.commit()
        account._invalidate_settings_cache()
        account._runtime()
        text = ibkr_logs.text(logging.ERROR)
        assert "client id" in text.lower() and "live" in text.lower()


# ======================================================================= the REAL ib_async
def offline_ib():
    ib = IB()
    c = ib.client
    c.isReady = lambda: True
    c.isConnected = lambda: True
    c._reqIdSeq = 500
    for name in ("placeOrder", "cancelOrder", "reqAllOpenOrders", "reqOpenOrders",
                 "reqCompletedOrders", "reqPositions"):
        setattr(c, name, lambda *a, **k: None)
    ib.wrapper.clientId = 7
    return ib


def echo(ib, trade, status, filled=0.0):
    o = trade.order
    ib.wrapper.orderStatus(o.orderId, status, filled, o.totalQuantity - filled, 0.0,
                           o.permId or 9000 + o.orderId, 0, 0.0, 7, "", 0.0)


class TestRealIbAsync:
    """These pin the facts the fake encodes, against the library itself."""

    @pytest.mark.parametrize("resting,logged", [("Submitted", True), ("PreSubmitted", False)])
    def test_modified_is_logged_only_for_a_submitted_echo(self, resting, logged):
        ib = offline_ib()
        aapl = Stock("AAPL", "SMART", "USD")
        aapl.conId = 265598
        o = Order(action="SELL", orderType="STP LMT", totalQuantity=10, auxPrice=90, lmtPrice=89.55)
        t = ib.placeOrder(aapl, o)
        o.permId = 9000 + o.orderId
        echo(ib, t, resting)
        n = len(t.log)
        o.auxPrice = 95
        ib.placeOrder(aapl, o)
        ib.wrapper.openOrder(o.orderId, aapl, o, OrderState(status=resting))
        echo(ib, t, resting)
        assert ("Modified" in [e.message for e in t.log[n:]]) is logged

    def test_a_warning_keeps_the_order_live_and_an_error_cancels_it(self):
        ib = offline_ib()
        aapl = Stock("AAPL", "SMART", "USD")
        aapl.conId = 265598
        o = Order(action="BUY", orderType="MKT", totalQuantity=10)
        t = ib.placeOrder(aapl, o)
        ib.wrapper.error(o.orderId, 2161, "example warning", "")
        assert t.orderStatus.status == "ValidationError" and not t.isDone()
        ib.wrapper.error(o.orderId, 201, "Order rejected", "")
        assert t.orderStatus.status == "Cancelled" and t.log[-1].errorCode == 201

    def test_the_adapters_wait_ack_survives_a_2161_warning_then_submitted(self):
        ib = offline_ib()
        aapl = Stock("AAPL", "SMART", "USD")
        aapl.conId = 265598
        rt = IBKRRuntime(label="probe", ib_factory=lambda: ib, host="h", port=4002, client_id=7,
                         account_id="DU1", paper=True, read_only=False)
        ib.errorEvent += rt._on_error

        class Shim:
            id = 1

            def _runtime(self):
                return rt
        async def main():
            o = Order(action="BUY", orderType="LMT", totalQuantity=10, lmtPrice=100.0)
            seq = rt.mark()
            t = ib.placeOrder(aapl, o)
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, lambda: ib.wrapper.error(o.orderId, 2161, "warning", ""))
            loop.call_later(0.5, lambda: echo(ib, t, "Submitted"))
            return await IBKRAccount._wait_ack(Shim(), ib, t, 2.0, seq)
        view = asyncio.run(main())
        rt.close()
        assert view.status == "Submitted"

    def test_concurrent_identical_requests_steal_each_others_answer(self):
        ib = offline_ib()

        async def main():
            f1 = asyncio.ensure_future(asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 0.5))
            f2 = asyncio.ensure_future(asyncio.wait_for(ib.reqAllOpenOrdersAsync(), 0.5))
            await asyncio.sleep(0.05)
            ib.wrapper.openOrderEnd()
            ib.wrapper.openOrderEnd()
            return await asyncio.gather(f1, f2, return_exceptions=True)
        r = asyncio.run(main())
        assert isinstance(r[0], (TimeoutError, asyncio.TimeoutError)) and isinstance(r[1], list)
