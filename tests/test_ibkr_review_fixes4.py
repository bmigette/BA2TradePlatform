"""Fourth review round (2026-10-03, verification of 51a347a0), failing tests first.

* a completed / settled option COMBO: its executions are LEG fills -- the price is the NET per combo unit
  (debit spread bought 5.00 / sold 2.00 = 3.00, never the 3.50 average of the legs) and the filled size is
  combo units, not the sum of the legs' shares;
* the distrust loops of the cancel paths must read order errors (IB refuses a cancel with 10148);
* ``_listed_by_ib`` must not match ANOTHER client's order that happens to carry the same orderId;
* a LIMIT order's ``auxPrice`` is not a price we sent (IB may echo 0.0), so it cannot veto a confirmation.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from ib_async import (
    ComboLeg, CommissionReport, Contract, Execution, Fill, IB, Option, Order, OrderState)

from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus)
from ba2_trade_platform.modules.accounts.IBKRAccount import BrokerOrderView, IBKRAccount, OrderBook
from ba2_trade_platform.modules.accounts.ibkr_runtime import IBKROrderRejected, IBKRRuntime
from tests.factories import create_transaction
from tests.ibkr_helpers import ibkr_logs, make_account  # noqa: F401
from tests.ibkr_tws_sim import ACCOUNT, AAPL, TwsSim, make_shim, resting_stop
from tests.test_ibkr_lifecycle import fresh, oco_row, rows
from tests.test_ibkr_orders import new_order, submit


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    yield account, fake, aapl
    account.close()


def run(coro):
    return asyncio.run(coro)


def combo_view(legs, filled=1.0, perm=888001, ref="ref"):
    """A completed BAG combo exactly as ib_async builds it."""
    ib = IB()
    ib.wrapper._results["completedOrders"] = []
    bag = Contract(secType="BAG", symbol="AAPL", exchange="SMART", currency="USD",
                   comboLegs=[ComboLeg(conId=cid, ratio=ratio, action=act) for cid, ratio, act in legs])
    co = Order(permId=perm, action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=3.1, orderRef=ref,
               account=ACCOUNT, filledQuantity=filled)
    ib.wrapper.completedOrder(bag, co, OrderState(status="Filled"))
    return BrokerOrderView.from_trade(ib.wrapper._results["completedOrders"][-1])


def leg_fill(con_id, side, shares, price, perm=888001, ref="ref"):
    c = Option("AAPL", "20261218", 100 + con_id, "C", "SMART", conId=con_id,
               localSymbol=f"AAPL  261218C{(100 + con_id) * 1000:08d}")
    c.tradingClass = "AAPL"
    ex = Execution(execId=f"X{con_id}{side}", time=datetime.now(timezone.utc), acctNumber=ACCOUNT, side=side,
                   shares=shares, price=price, permId=perm, orderId=0, orderRef=ref)
    return Fill(c, ex, CommissionReport(), datetime.now(timezone.utc))


def combo_row(account, view):
    row = new_order(account, qty=1.0, order_type=OrderType.BUY_LIMIT, limit=3.1, status=OrderStatus.ACCEPTED)
    row.asset_class = AssetClass.OPTION
    row.broker_order_id = "888001"
    update_instance(row)
    return get_instance(TradingOrder, row.id)


# ======================================================================= 1  combo price and units
class TestComboNetPrice:
    def test_a_debit_spread_costs_the_net_not_the_average_of_the_legs(self, world):
        account, _, _ = world
        view = combo_view([(1, 1, "BUY"), (2, 1, "SELL")])
        book = OrderBook(views=[view], open_ok=True, completed_ok=True)
        IBKRAccount._aggregate_executions(book, [leg_fill(1, "BOT", 1, 5.0), leg_fill(2, "SLD", 1, 2.0)])
        row = combo_row(account, view)
        account._apply_view(row, view, book)
        f = fresh(row)
        assert f.open_price == pytest.approx(3.0), "bought 5.00, sold 2.00: the net debit is 3.00 (not 3.50)"
        assert f.filled_qty == 1.0

    def test_a_credit_spread_is_a_negative_net(self, world):
        account, _, _ = world
        view = combo_view([(1, 1, "SELL"), (2, 1, "BUY")])
        book = OrderBook(views=[view], open_ok=True, completed_ok=True)
        IBKRAccount._aggregate_executions(book, [leg_fill(1, "SLD", 1, 5.0), leg_fill(2, "BOT", 1, 2.0)])
        row = combo_row(account, view)
        account._apply_view(row, view, book)
        assert fresh(row).open_price == pytest.approx(-3.0)

    def test_leg_ratios_weigh_the_legs(self, world):
        account, _, _ = world
        view = combo_view([(1, 1, "BUY"), (2, 2, "SELL")])          # buy 1 x 5.00, sell 2 x 2.00
        book = OrderBook(views=[view], open_ok=True, completed_ok=True)
        IBKRAccount._aggregate_executions(book, [leg_fill(1, "BOT", 1, 5.0), leg_fill(2, "SLD", 2, 2.0)])
        row = combo_row(account, view)
        account._apply_view(row, view, book)
        assert fresh(row).open_price == pytest.approx(1.0)

    def test_a_leg_without_an_execution_leaves_the_price_unset(self, world):
        account, _, _ = world
        view = combo_view([(1, 1, "BUY"), (2, 1, "SELL")])
        book = OrderBook(views=[view], open_ok=True, completed_ok=True)
        IBKRAccount._aggregate_executions(book, [leg_fill(1, "BOT", 1, 5.0)])
        row = combo_row(account, view)
        account._apply_view(row, view, book)
        assert fresh(row).open_price is None

    def test_a_settled_combo_counts_combo_units_not_the_sum_of_the_leg_shares(self, world):
        """The settle path (an order IB lists nowhere) summed the legs' shares into filled_qty."""
        account, fake, _ = world
        c1 = fake.add_option("AAPL", "20261218", 100.0, "C")
        c2 = fake.add_option("AAPL", "20261218", 110.0, "C")
        parent = new_order(account, qty=1.0, order_type=OrderType.BUY_LIMIT, limit=3.1,
                           status=OrderStatus.ACCEPTED)
        parent.asset_class, parent.broker_order_id = AssetClass.OPTION, "888001"
        parent.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        update_instance(parent)
        for contract, side in ((c1, OrderDirection.BUY), (c2, OrderDirection.SELL)):
            add_instance(TradingOrder(
                account_id=account.id, symbol="AAPL", quantity=1.0, side=side, order_type=OrderType.BUY_LIMIT,
                status=OrderStatus.ACCEPTED, asset_class=AssetClass.OPTION, parent_order_id=parent.id,
                contract_symbol=account._occ_of(contract), created_at=datetime.now(timezone.utc)))
        fake.make_fill_record(c1, "BOT", 1, 5.0, perm=888001)
        fake.make_fill_record(c2, "SLD", 1, 2.0, perm=888001)
        account.refresh_orders()
        f = fresh(parent)
        assert f.status == OrderStatus.FILLED
        assert f.filled_qty == 1.0, "one combo unit, not 2 (the sum of the legs' shares)"
        assert f.open_price == pytest.approx(3.0)

    def test_a_half_executed_combo_is_not_settled_from_one_leg(self, world, ibkr_logs):
        account, fake, _ = world
        c1 = fake.add_option("AAPL", "20261218", 100.0, "C")
        c2 = fake.add_option("AAPL", "20261218", 110.0, "C")
        parent = new_order(account, qty=1.0, order_type=OrderType.BUY_LIMIT, limit=3.1,
                           status=OrderStatus.ACCEPTED)
        parent.asset_class, parent.broker_order_id = AssetClass.OPTION, "888001"
        parent.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        update_instance(parent)
        for contract, side in ((c1, OrderDirection.BUY), (c2, OrderDirection.SELL)):
            add_instance(TradingOrder(
                account_id=account.id, symbol="AAPL", quantity=1.0, side=side, order_type=OrderType.BUY_LIMIT,
                status=OrderStatus.ACCEPTED, asset_class=AssetClass.OPTION, parent_order_id=parent.id,
                contract_symbol=account._occ_of(contract), created_at=datetime.now(timezone.utc)))
        fake.make_fill_record(c1, "BOT", 1, 5.0, perm=888001)
        account.refresh_orders()
        assert fresh(parent).status == OrderStatus.ACCEPTED
        assert "UNRESOLVED" in ibkr_logs.text()


# ======================================================================= helpers for the real-wrapper tests
def _runtime_and_shim(sim):
    ib = sim.ib
    rt = IBKRRuntime(label="probe", ib_factory=lambda: ib, host="h", port=4002, client_id=7,
                     account_id=ACCOUNT, paper=True, read_only=False)
    ib.errorEvent += rt._on_error
    return ib, rt, make_shim(sim, rt, IBKRAccount)


def _locally_cancelled_live_stop(sim, ib):
    t, o, ref = resting_stop(sim, "Submitted")
    ib.wrapper.error(o.orderId, 201, "Order rejected - modify refusal", "")     # a refused MODIFY
    assert t.orderStatus.status == "Cancelled" and sim.truth[o.orderId]["status"] == "Submitted"
    return t, o, ref


# ======================================================================= 2  the cancel is refused
class TestRefusedCancelIsNotSuccess:
    def test_ib_refusing_the_cancel_of_a_locally_cancelled_order_is_reported(self):
        async def main():
            sim = TwsSim()
            sim.cancel_mode, sim.status_on_open = "refuse", False        # openOrder only, then 10148
            ib, rt, shim = _runtime_and_shim(sim)
            t, o, ref = _locally_cancelled_live_stop(sim, ib)
            payload = {"order_ref": ref, "broker_order_id": str(o.permId)}
            old = IBKRAccount._CANCEL_ACK_TIMEOUT
            IBKRAccount._CANCEL_ACK_TIMEOUT = 2.0
            try:
                results = await shim._cancel_trades(ib, [payload])
                outcome = await shim._cancel_trade_confirmed(ib, t)
            finally:
                IBKRAccount._CANCEL_ACK_TIMEOUT = old
            rt.close()
            return results, outcome, sim.truth[o.orderId]["status"]
        results, outcome, truth = run(main())
        assert results == {"9500": False}, "IB refused (10148): that is not a successful cancel"
        assert outcome == "unconfirmed" and truth == "Submitted"


# ======================================================================= 3  another client's orderId
class TestForeignOrderIdCollision:
    def _setup(self, sim, ib):
        t, o, ref = resting_stop(sim, "Submitted")
        sim.truth[o.orderId]["status"] = "Cancelled"                    # OUR order is really dead
        sim.status(t, "Cancelled")
        sim.foreign[o.orderId] = {"client": 8, "perm": 77777}           # another client: SAME orderId
        return t, o, ref

    def test_a_dead_order_is_not_kept_alive_by_a_foreign_order_with_the_same_id(self):
        async def main():
            sim = TwsSim()
            ib, rt, shim = _runtime_and_shim(sim)
            t, o, ref = self._setup(sim, ib)
            t0 = time.monotonic()
            outcome = await shim._cancel_trade_confirmed(ib, t)
            took = time.monotonic() - t0
            results = await shim._cancel_trades(ib, [{"order_ref": ref, "broker_order_id": str(o.permId)}])
            rt.close()
            return outcome, took, results, [s for s in sim.sent if s[0] == "cancel"]
        outcome, took, results, cancels = run(main())
        assert outcome == "cancelled" and took < 1.0 and results == {"9500": True}
        assert cancels == [], "nothing of ours is live, so no cancel may be sent (and a foreign order never)"

    def test_the_listed_check_still_recognises_our_own_order(self):
        async def main():
            sim = TwsSim()
            ib, rt, shim = _runtime_and_shim(sim)
            t, o, ref = resting_stop(sim, "Submitted")
            sim.foreign[o.orderId] = {"client": 8, "perm": 77777}
            listed = await shim._listed_by_ib(ib, t)
            rt.close()
            return listed
        assert run(main()) is True


# ======================================================================= 4  distinguish confirmed from timed out
class TestRefusedModifyThenCancelOutcomes:
    def test_a_cancel_IB_confirms_is_cancelled_and_quick(self):
        async def main():
            sim = TwsSim()
            sim.cancel_mode = "ok"
            ib, rt, shim = _runtime_and_shim(sim)
            t, o, ref = _locally_cancelled_live_stop(sim, ib)
            t0 = time.monotonic()
            outcome = await shim._cancel_trade_confirmed(ib, t)
            took = time.monotonic() - t0
            truth = sim.truth[o.orderId]["status"]
            rt.close()
            return outcome, took, truth, len([s for s in sim.sent if s[0] == "cancel"])
        outcome, took, truth, sent = run(main())
        assert outcome == "cancelled" and truth == "Cancelled" and sent == 1
        assert took < IBKRAccount._CANCEL_ACK_TIMEOUT, "confirmed, not timed out"

    def test_a_cancel_nobody_answers_is_unconfirmed_after_the_window(self):
        async def main():
            sim = TwsSim()
            sim.cancel_mode = "lost"
            ib, rt, shim = _runtime_and_shim(sim)
            t, o, ref = _locally_cancelled_live_stop(sim, ib)
            old = IBKRAccount._CANCEL_ACK_TIMEOUT
            IBKRAccount._CANCEL_ACK_TIMEOUT = 1.0
            try:
                t0 = time.monotonic()
                outcome = await shim._cancel_trade_confirmed(ib, t)
                took = time.monotonic() - t0
            finally:
                IBKRAccount._CANCEL_ACK_TIMEOUT = old
            rt.close()
            return outcome, took
        outcome, took = run(main())
        assert outcome == "unconfirmed" and took >= 0.9

    def test_a_partly_filled_order_is_cancelled_through_pendingcancel_and_error_202(self):
        async def main():
            sim = TwsSim()
            sim.cancel_mode = "ok"
            ib, rt, shim = _runtime_and_shim(sim)
            t, o, ref = resting_stop(sim, "Submitted")
            sim.fill(o.orderId, 4, 99.5)
            outcome = await shim._cancel_trade_confirmed(ib, t)
            view = BrokerOrderView.from_trade(t)
            errors = rt.order_errors(o.orderId, 0, kinds=None)
            rt.close()
            return outcome, view, errors
        outcome, view, errors = run(main())
        assert outcome == "cancelled"
        assert view.filled == 4.0 and view.status == "Cancelled"
        assert any(code == 202 for code, _ in errors)


# ======================================================================= J: LIMIT order aux echo
class TestLimitOrderModifyConfirmation:
    @pytest.mark.parametrize("echoed_aux", [0.0, 1.7976931348623157e308])
    def test_a_limit_modification_is_confirmed_whatever_IB_echoes_for_the_unused_aux(self, echoed_aux):
        async def main():
            sim = TwsSim()
            ib, rt, shim = _runtime_and_shim(sim)
            ref = "ba2:1:6:abcd1234"
            o = Order(action="SELL", orderType="LMT", totalQuantity=10, lmtPrice=120.0, orderRef=ref,
                      account=ACCOUNT)
            t = ib.placeOrder(AAPL, o)
            o.permId = 9100 + o.orderId
            sim.truth[o.orderId] = {"lmt": 120.0, "aux": echoed_aux, "qty": 10.0, "status": "Submitted"}
            sim.status(t, "Submitted")
            orig = ib.client.placeOrder

            def on_place(oid, contract, order):
                orig(oid, contract, order)

                def apply():
                    sim.truth[oid].update(lmt=order.lmtPrice)
                    sim.deliver_open()
                asyncio.get_event_loop().call_later(0.1, apply)
            ib.client.placeOrder = on_place
            old = IBKRAccount._ORDER_ACK_TIMEOUT
            IBKRAccount._ORDER_ACK_TIMEOUT = 3.0
            try:
                await shim._modify_order_object(ib, {"order_ref": ref, "broker_order_id": str(o.permId)},
                                                qty=None, limit=125.0, stop=None, tif=None, symbol="AAPL")
                verdict = "confirmed"
            except IBKROrderRejected:
                verdict = "refused"
            finally:
                IBKRAccount._ORDER_ACK_TIMEOUT = old
            rt.close()
            return verdict
        assert run(main()) == "confirmed"


# ======================================================================= end to end: sizing refuses
class TestSizingRefusesWithoutBuyingPower:
    def _expert(self, account):
        from tests import factories
        from tests.test_available_balance_clamp import _BalanceExpert as Expert
        inst = factories.create_expert_instance(account_id=account.id, expert="_BalanceExpert",
                                                virtual_equity_pct=100.0)
        return Expert(inst.id)

    def _with_resolver(self, account, fn):
        from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver
        from tests.test_available_balance_clamp import _resolver_for
        prev = get_instance_resolver()
        try:
            set_instance_resolver(_resolver_for(account))
            return fn()
        finally:
            set_instance_resolver(prev)

    def test_get_available_balance_is_none_for_an_ibkr_account_without_buying_power(self, world):
        account, fake, _ = world
        fake.account_rows = [r for r in fake.account_rows if r.tag != "ExcessLiquidity"]
        expert = self._expert(account)
        assert self._with_resolver(account, expert.get_available_balance) is None

    def test_and_it_is_a_number_when_the_buying_power_exists(self, world):
        account, fake, _ = world
        expert = self._expert(account)
        got = self._with_resolver(account, expert.get_available_balance)
        assert got is not None and got <= 160000.0
