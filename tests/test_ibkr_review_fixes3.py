"""Third review round (2026-10-03, re-review of 476c8e43), failing tests first.

Preferred form: REAL ib_async objects (``TestRealIbAsync*`` + ``tests/ibkr_tws_sim.py``) rather than the fake,
because rounds 1 and 2 were both built on a fake that hid what the library really does:

* a COMPLETED order comes back with an EMPTY status record (filled 0, avgFillPrice 0); the traded size is
  ``order.filledQuantity``;
* our own re-read of the open orders makes TWS send an unchanged orderStatus that ib_async logs as 'Modified'
  even when IB kept the OLD prices;
* a modification IB refuses with a non-warning error turns the still-live trade 'Cancelled' LOCALLY.
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
from tests.ibkr_tws_sim import (
    ACCOUNT, AAPL, TwsSim, ib_applies_modifications, make_shim, resting_stop)
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


def make_old(row_id, minutes=60):
    """Age a row the way real time would: from the moment it was PLACED (not created)."""
    r = get_instance(TradingOrder, row_id)
    r.data = {**(r.data or {}), "ibkr_placed_at":
              (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()}
    update_instance(r)


def completed_trade(ib, *, status="Filled", filled=10.0, total=10.0, perm=777001, ref="x"):
    """A completed order EXACTLY as ib_async 2.1.0 builds it (wrapper.completedOrder)."""
    ib.wrapper._results["completedOrders"] = []
    order = Order(permId=perm, action="BUY", totalQuantity=total, orderType="LMT", lmtPrice=100.0,
                  orderRef=ref, account=ACCOUNT, filledQuantity=filled)
    ib.wrapper.completedOrder(AAPL, order, OrderState(status=status, completedStatus=status))
    return ib.wrapper._results["completedOrders"][-1]


# ======================================================================= 1  completed orders
class TestCompletedOrders:
    def test_real_wrapper_shape_the_figure_is_on_the_order(self):
        t = completed_trade(IB())
        assert t.orderStatus.filled == 0 and t.orderStatus.avgFillPrice == 0 and t.order.filledQuantity == 10
        v = BrokerOrderView.from_trade(t)
        assert v.filled == 10.0 and v.remaining == 0.0 and v.status == "Filled"

    def test_a_cancelled_completed_order_that_traded_keeps_its_fill(self):
        v = BrokerOrderView.from_trade(completed_trade(IB(), status="Cancelled", filled=4.0))
        assert v.filled == 4.0 and v.remaining == 6.0

    def test_an_untraded_completed_order_reads_zero_not_the_unset_marker(self):
        ib = IB()
        ib.wrapper._results["completedOrders"] = []
        order = Order(permId=1, action="BUY", totalQuantity=10, orderType="LMT", lmtPrice=1.0, account=ACCOUNT)
        ib.wrapper.completedOrder(AAPL, order, OrderState(status="Cancelled"))
        v = BrokerOrderView.from_trade(ib.wrapper._results["completedOrders"][-1])
        assert v.filled == 0.0

    def test_a_partly_filled_then_cancelled_order_is_not_dead_so_a_retry_does_not_rebuy(self, world):
        account, fake, _ = world
        v = BrokerOrderView.from_trade(completed_trade(IB(), status="Cancelled", filled=4.0))
        kind, _view = account._classify_prior([v], action="BUY", ib_type="LMT", qty=10.0)
        assert kind == "adopt"

    def test_a_filled_row_keeps_its_fill_and_price_after_a_restart(self, world):
        """probe p6 B: refresh after a restart set filled_qty 10 -> 0.0 and protective exits are sized on it."""
        account, fake, _ = world
        row = new_order(account)
        assert submit(account, row) is not None
        fake.simulate_fill(ref_of(account, row), price=99.87)
        account.refresh_orders()
        assert fresh(row).filled_qty == 10.0 and fresh(row).open_price == 99.87
        fake.reset_ids_on_connect = 100
        fake.simulate_disconnect()                    # the restart: the trade is now only a COMPLETED order
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.FILLED and f.filled_qty == 10.0 and f.open_price == 99.87

    def test_a_view_never_lowers_a_recorded_fill_or_zeroes_a_price(self, world):
        account, fake, _ = world
        row = new_order(account, status=OrderStatus.FILLED, broker_order_id="777001")
        row.filled_qty, row.open_price = 10.0, 99.5
        update_instance(row)
        v = BrokerOrderView.from_trade(completed_trade(IB(), filled=0.0, perm=777001))
        # an empty completed record (filledQuantity unset) says filled 0
        assert v.filled == 0.0
        account._apply_view(get_instance(TradingOrder, row.id), v)
        f = fresh(row)
        assert f.filled_qty == 10.0 and f.open_price == 99.5

    def test_the_average_price_of_a_completed_order_comes_from_the_executions(self, world):
        account, fake, aapl = world
        row = new_order(account, broker_order_id="777777777", status=OrderStatus.ACCEPTED, qty=5.0)
        order = Order(action="BUY", totalQuantity=5, orderType="MKT", account="DU1234567")
        fake.add_prior_trade(aapl, order, "Filled", filled=5, avg=0.0, perm=777777777)
        fake.make_fill_record(aapl, "BOT", 5, 99.0, perm=777777777)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.FILLED and f.filled_qty == 5.0 and f.open_price == 99.0

    def test_a_retry_after_a_restart_adopts_the_partly_filled_order(self, world):
        account, fake, _ = world
        fake.behaviors.append(("partial_then_cancel", 4))
        row = new_order(account)
        submit(account, row)
        fake.reset_ids_on_connect = 100
        fake.simulate_disconnect()
        x = fresh(row)
        x.broker_order_id, x.status, x.filled_qty = None, OrderStatus.ERROR, None
        update_instance(x)
        n = len(fake.placed)
        submit(account, fresh(row))
        assert len(fake.placed) == n, "the 4 shares are real: do not buy 10 more"


# ======================================================================= 2  'Modified' needs the sent values
def run(coro):
    return asyncio.run(coro)


async def _modify(resting, applies, ack=3.0, *, ack_timeout=None):
    sim = TwsSim()
    ib = sim.ib
    rt = IBKRRuntime(label="probe", ib_factory=lambda: ib, host="h", port=4002, client_id=7,
                     account_id=ACCOUNT, paper=True, read_only=False)
    ib.errorEvent += rt._on_error
    shim = make_shim(sim, rt, IBKRAccount)
    t, o, ref = resting_stop(sim, resting)
    if applies:
        ib_applies_modifications(sim, o.orderId)
    old = IBKRAccount._ORDER_ACK_TIMEOUT
    IBKRAccount._ORDER_ACK_TIMEOUT = ack
    try:
        try:
            await shim._modify_order_object(ib, {"order_ref": ref, "broker_order_id": str(o.permId)},
                                            qty=None, limit=94.5, stop=95.0, tif=None, symbol="AAPL")
            verdict = "confirmed"
        except IBKROrderRejected:
            verdict = "refused"
    finally:
        IBKRAccount._ORDER_ACK_TIMEOUT = old
    result = (verdict, sim.truth[o.orderId]["aux"], t.order.auxPrice)
    rt.close()
    return result


class TestRealIbAsyncModifyConfirmation:
    """probe p6 (A): the adapter's own re-read made TWS send an unchanged orderStatus that ib_async logs as
    'Modified'; with Submitted resting and IB NOT applying, that was accepted although the open order showed
    the OLD prices."""

    def test_submitted_and_ignored_is_refused_not_confirmed(self):
        verdict, ib_aux, local_aux = run(_modify("Submitted", applies=False))
        assert verdict == "refused" and ib_aux == 90.0 and local_aux == 90.0

    def test_submitted_and_applied_is_confirmed(self):
        verdict, ib_aux, local_aux = run(_modify("Submitted", applies=True))
        assert verdict == "confirmed" and ib_aux == 95.0 and local_aux == 95.0

    def test_presubmitted_and_ignored_is_refused(self):
        assert run(_modify("PreSubmitted", applies=False))[0] == "refused"

    def test_presubmitted_and_applied_is_confirmed(self):
        assert run(_modify("PreSubmitted", applies=True))[0] == "confirmed"

    def test_the_fake_agrees_a_silent_modify_of_a_submitted_stop_is_refused(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 1.5)
        fake.regular_session = True
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        child = rows(account, parent_order_id=parent.id)[0]
        fake.modify_behavior = "silent"
        patch = TradingOrder(symbol="AAPL", quantity=10.0, side=OrderDirection.SELL,
                             order_type=OrderType.SELL_STOP_LIMIT, stop_price=95.0, limit_price=94.5)
        assert account.modify_order(str(child.id), patch) is None
        assert fresh(child).stop_price == 90.0


# ======================================================================= 3  refused modify, then cancel
class TestRealIbAsyncRefusedModifyThenCancel:
    def test_p7_a_local_cancelled_is_not_trusted_and_a_real_cancel_is_sent(self):
        async def main():
            sim = TwsSim()
            ib = sim.ib
            rt = IBKRRuntime(label="probe", ib_factory=lambda: ib, host="h", port=4002, client_id=7,
                             account_id=ACCOUNT, paper=True, read_only=False)
            ib.errorEvent += rt._on_error
            shim = make_shim(sim, rt, IBKRAccount)
            t, o, ref = resting_stop(sim, "Submitted")
            orig = ib.client.placeOrder

            def on_place(oid, contract, order):
                orig(oid, contract, order)
                asyncio.get_event_loop().call_later(
                    0.05, lambda: ib.wrapper.error(oid, 201, "Order rejected - reason:example refusal", ""))
            ib.client.placeOrder = on_place
            old = IBKRAccount._ORDER_ACK_TIMEOUT
            IBKRAccount._ORDER_ACK_TIMEOUT = 2.0
            payload = {"order_ref": ref, "broker_order_id": str(o.permId)}
            try:
                with pytest.raises(IBKROrderRejected):
                    await shim._modify_order_object(ib, payload, qty=None, limit=94.5, stop=95.0, tif=None,
                                                    symbol="AAPL")
            finally:
                IBKRAccount._ORDER_ACK_TIMEOUT = old
            ib.client.placeOrder = orig
            # the refused modification re-read IB's open orders, so the status is already restored ...
            status_after_refusal = t.orderStatus.status
            # ... and even when it is NOT (force the stale local state), the cancel path must ask IB
            t.orderStatus.status = "Cancelled"
            before = len([s for s in sim.sent if s[0] == "cancel"])
            results = await shim._cancel_trades(ib, [payload])
            sent = len([s for s in sim.sent if s[0] == "cancel"]) - before
            rt.close()
            return status_after_refusal, results, sent
        status, results, sent = run(main())
        assert status == "Submitted", "a refused modification must re-read IB so the status is restored"
        assert sent == 1 and results == {"9500": True}, (results, sent)

    def test_a_trade_ib_does_not_list_and_marks_cancelled_is_really_cancelled(self):
        async def main():
            sim = TwsSim()
            ib = sim.ib
            rt = IBKRRuntime(label="probe", ib_factory=lambda: ib, host="h", port=4002, client_id=7,
                             account_id=ACCOUNT, paper=True, read_only=False)
            shim = make_shim(sim, rt, IBKRAccount)
            t, o, ref = resting_stop(sim, "Submitted")
            sim.truth[o.orderId]["status"] = "Cancelled"       # IB really cancelled it
            t.orderStatus.status = "Cancelled"
            before = len([s for s in sim.sent if s[0] == "cancel"])
            results = await shim._cancel_trades(ib, [{"order_ref": ref, "broker_order_id": str(o.permId)}])
            sent = len([s for s in sim.sent if s[0] == "cancel"]) - before
            rt.close()
            return results, sent
        results, sent = run(main())
        assert sent == 0 and results == {"9500": True}

    def test_the_fake_cancel_of_a_locally_cancelled_live_stop_is_sent_and_confirmed(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")

        def stale():
            sl._restore_status = sl.orderStatus.status      # what IB still has
            sl.orderStatus.status = "Cancelled"             # what ib_async marked locally
        fake._on_loop(stale)
        assert account.cancel_order(str(parent.id)) is True
        assert sl.order.orderId in fake.cancel_requests
        time.sleep(0.2)
        assert sl.orderStatus.status == "Cancelled"


# ======================================================================= 4  grace ages from placement
class TestNeverReachedAgesFromPlacement:
    def test_a_row_created_long_ago_is_not_settled_one_second_after_placement(self, world, monkeypatch):
        """probe p6 D: dependent exits, staged replacements and stop->MARKET retries are created hours
        before they are placed."""
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.3)
        row = new_order(account)
        r = get_instance(TradingOrder, row.id)
        r.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        update_instance(r)
        fake.behaviors.append("lost")
        submit(account, fresh(row))
        assert fresh(row).status == OrderStatus.PENDING_NEW
        assert fresh(row).data["ibkr_placed_at"]
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.PENDING_NEW        # NOT ERROR: placed a moment ago
        make_old(row.id, minutes=30)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ERROR

    def test_the_callers_object_carries_the_stamp(self, world):
        account, fake, _ = world
        row = new_order(account)
        submit(account, row)
        assert row.data["ibkr_placed_at"] == fresh(row).data["ibkr_placed_at"]


# ======================================================================= 5  the shared clamp
class TestClampBuyingPowerMandatory:
    def test_ibkr_declares_it_and_the_others_do_not(self):
        from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
        from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount
        assert IBKRAccount.buying_power_is_mandatory is True
        assert AlpacaAccount.buying_power_is_mandatory is False
        assert TastyTradeAccount.buying_power_is_mandatory is False

    def test_ibkr_without_a_buying_power_refuses_sizing(self, world):
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
        account, fake, _ = world
        fake.account_rows = [r for r in fake.account_rows if r.tag != "ExcessLiquidity"]
        with pytest.raises(ValueError, match="mandatory"):
            MarketExpertInterface._get_actual_available_balance(account)

    def test_ibkr_with_a_buying_power_is_clamped_to_it(self, world):
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
        account, fake, _ = world
        assert MarketExpertInterface._get_actual_available_balance(account) == 160000.0

    @pytest.mark.parametrize("cls_name", ["AlpacaAccount", "TastyTradeAccount"])
    def test_alpaca_and_tastytrade_keep_the_existing_fallback_chain(self, cls_name, monkeypatch):
        """Chain: snapshot.buying_power -> info[buying_power|cash|cash_balance|equity_buying_power] -> None.
        The get_balance() (equity) substitute was REMOVED 2026-10-07 (audit item 19): equity is not
        buying power, a missing figure is retried then reported at ERROR, and the clamp is skipped."""
        import importlib
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface as _M
        monkeypatch.setattr(_M, "_ACTUAL_BP_SLEEP", staticmethod(lambda s: None))
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
        cls = getattr(importlib.import_module(f"ba2_trade_platform.modules.accounts.{cls_name}"), cls_name)
        clamp = MarketExpertInterface._get_actual_available_balance

        def build(snap_bp, info, balance, snap_raises=False, info_raises=False, balance_raises=False):
            acct = object.__new__(cls)

            def snap():
                if snap_raises:
                    raise RuntimeError("snap")
                return SimpleNamespace(buying_power=snap_bp)

            def get_info():
                if info_raises:
                    raise RuntimeError("info")
                return info

            def get_balance():
                if balance_raises:
                    raise RuntimeError("bal")
                return balance
            acct.get_account_snapshot, acct.get_account_info, acct.get_balance = snap, get_info, get_balance
            return acct
        assert clamp(build(1234.5, {"buying_power": 9.0}, 7.0)) == 1234.5            # snapshot first
        assert clamp(build(None, {"buying_power": 55.0, "cash": 9.0}, 7.0)) == 55.0   # then info
        assert clamp(build(None, {"cash": 42.0}, 7.0)) == 42.0                        # then cash
        assert clamp(build(None, {}, 7.0)) is None                                    # no figure: no clamp (was: equity)
        assert clamp(build(None, None, 7.0, info_raises=True)) is None                # failing info: retried, no clamp
        assert clamp(build(None, {}, None, balance_raises=True)) is None              # nothing: None
        assert clamp(build(None, {"equity_buying_power": 11.0}, 7.0, snap_raises=True)) == 11.0
        assert clamp(build(float("nan"), {"cash": 5.0}, 7.0)) == 5.0                  # NaN snapshot is unusable


# ======================================================================= 6 / 7  nonce noise, refused re-submission
class TestNonceNoiseAndRebinding:
    def test_a_retired_nonce_does_not_warn_on_every_refresh(self, world, ibkr_logs):
        """probe p6 F: three refreshes -> three warnings from the spent ref."""
        account, fake, _ = world
        fake.behaviors.append(("reject", 201, "Order rejected - reason:Insufficient buying power"))
        row = new_order(account)
        submit(account, row)
        x = fresh(row)
        x.broker_order_id, x.status = None, OrderStatus.PENDING
        update_instance(x)
        submit(account, fresh(row))
        for _ in range(3):
            account.refresh_orders()
        assert "nonce does not match" not in ibkr_logs.text()
        assert fresh(row).status == OrderStatus.ACCEPTED

    def test_an_unknown_foreign_nonce_still_warns(self, world, ibkr_logs):
        account, fake, _ = world
        row = new_order(account)
        submit(account, row)
        r = fresh(row)
        r.data = {**r.data, "ibkr_nonce": "feedbeef"}      # the live order now carries a nonce the row does not
        update_instance(r)
        account.refresh_orders()
        assert "nonce does not match" in ibkr_logs.text()

    def test_a_refused_resubmission_is_rebound_to_the_live_order_on_refresh(self, world):
        """probe p8 (kept on purpose, documented): the live order is tracked, never orphaned; the comment
        keeps the explanation."""
        account, fake, _ = world
        row = new_order(account, qty=10.0)
        submit(account, row)
        x = fresh(row)
        x.broker_order_id, x.status, x.quantity = None, OrderStatus.ERROR, 3.0
        update_instance(x)
        assert submit(account, fresh(row)) is None
        assert fresh(row).status == OrderStatus.ERROR and "differs" in fresh(row).comment
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.ACCEPTED and f.quantity == 10.0 and "differs" in f.comment


# ======================================================================= 8  ValidationError limbo
class TestValidationErrorLimbo:
    def test_a_non_readonly_warning_on_a_new_order_that_never_goes_live_is_settled(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.4)
        fake.behaviors.append(("lost_with_warning", 434, "The order size cannot be zero."))
        row = new_order(account)
        submit(account, row)
        assert fresh(row).status == OrderStatus.PENDING_NEW
        make_old(row.id)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.ERROR and "never reached IBKR" in f.comment

    def test_a_warned_order_that_IB_lists_is_left_alone(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.3)
        fake.behaviors.append(("warn", 399, "held until the open"))
        row = new_order(account)
        submit(account, row)
        make_old(row.id)
        account.refresh_orders()
        assert fresh(row).status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED)


# ======================================================================= 9  gate the session
class TestSessionGate:
    def test_nothing_uses_the_session_before_the_connect_checks_finished(self, world):
        account, fake, _ = world
        fake.connect_sync_delay = 0.6
        rt = account._runtime()
        seen = []

        def make(tag):
            async def fn(ib):
                seen.append((tag, rt._ready))
                return tag
            return fn
        out = []
        threads = [threading.Thread(target=lambda t=t: out.append(rt.call(make(t), timeout=5, op=t)))
                   for t in ("a", "b", "c")]
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join(20)
        assert sorted(out) == ["a", "b", "c"]
        assert all(ready for _, ready in seen), seen

    def test_a_managed_account_mismatch_blocks_every_waiting_caller(self, world):
        account, fake, _ = world
        fake.managed = ["DU9999999"]
        fake.connect_sync_delay = 0.5
        rt = account._runtime()
        ran = []
        errors = []

        def attempt(tag):
            async def fn(ib):
                ran.append(tag)
            try:
                rt.call(fn, timeout=5, op=tag)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=attempt, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join(20)
        assert ran == [], "no call may run on a session whose account was never verified"
        assert len(errors) == 2 and all(isinstance(e, IBKRConnectionError) for e in errors)
