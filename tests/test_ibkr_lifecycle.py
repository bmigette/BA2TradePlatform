"""IBKRAccount cancel / modify / refresh / OCO / protective legs against FakeIB. No network."""
from datetime import datetime, timedelta, timezone

import pytest
from ib_async import Order

from ba2_trade_platform.modules.accounts.ibkr_mapping import make_order_ref
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus)
from sqlmodel import select
from tests.factories import create_transaction
from tests.ibkr_helpers import ACCOUNT_ID, ibkr_logs, make_account  # noqa: F401
from tests.test_ibkr_orders import last_placed, new_order, ref, submit


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    fake.add_stock("MSFT", 272093)
    yield account, fake, aapl
    account.close()


def rows(account, **where):
    with get_db() as session:
        stmt = select(TradingOrder).where(TradingOrder.account_id == account.id)
        for k, v in where.items():
            stmt = stmt.where(getattr(TradingOrder, k) == v)
        return session.exec(stmt.order_by(TradingOrder.id)).all()


def fresh(row):
    return get_instance(TradingOrder, row.id)


# ======================================================================= cancel
class TestCancel:
    def test_cancel_by_db_id_requests_cancel_then_refresh_confirms(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=100.0))
        fake.hold_cancels = True
        assert account.cancel_order(str(row.id)) is True
        assert fresh(row).status == OrderStatus.PENDING_CANCEL
        assert fake.cancel_requests
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.PENDING_CANCEL      # IB has not confirmed yet
        fake.simulate_cancel_confirmed(ref(account, row))
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.CANCELED

    def test_cancel_by_broker_perm_id(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=100.0))
        assert account.cancel_order(row.broker_order_id) is True
        assert fresh(row).status == OrderStatus.PENDING_CANCEL

    def test_a_db_only_order_cannot_be_cancelled_at_the_broker(self, world):
        account, fake, _ = world
        row = new_order(account, status=OrderStatus.WAITING_TRIGGER)
        assert account.cancel_order(str(row.id)) is False
        assert fake.cancel_requests == []

    def test_an_order_from_another_client_is_reported_not_silently_dropped(self, world, ibkr_logs):
        account, fake, _ = world
        row = new_order(account, broker_order_id="555555555", status=OrderStatus.ACCEPTED)
        assert account.cancel_order(str(row.id)) is False
        assert "not known to this IBKR session" in ibkr_logs.text()
        assert fresh(row).status == OrderStatus.ACCEPTED

    def test_ib_refusing_the_cancel_is_false_and_leaves_the_status(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account))
        fake.simulate_status(ref(account, row), "Filled")
        assert account.cancel_order(str(row.id)) is False       # error 10148 from the fake
        assert fresh(row).status != OrderStatus.PENDING_CANCEL

    def test_read_only_refuses(self, monkeypatch):
        account, fake = make_account(monkeypatch, read_only=True)
        try:
            row = new_order(account, broker_order_id="1")
            assert account.cancel_order(str(row.id)) is False and fake.cancel_requests == []
        finally:
            account.close()


# ======================================================================= modify (true in-place)
class TestModify:
    def test_limit_price_modified_in_place_same_order_id(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=100.0))
        order_id = last_placed(fake)["orderId"]
        patch = TradingOrder(account_id=account.id, symbol="AAPL", quantity=10.0,
                             side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, limit_price=101.234)
        out = account.modify_order(str(row.id), patch)
        mod = last_placed(fake)
        assert mod["modification"] is True and mod["orderId"] == order_id
        assert mod["lmt"] == pytest.approx(101.23)
        assert out.limit_price == 101.23 and fresh(row).limit_price == 101.23   # what IB was sent
        assert len(fake.trades()) == 1                       # no new order was created

    def test_fractional_resize_is_refused(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=100.0))
        patch = TradingOrder(account_id=account.id, symbol="AAPL", quantity=2.5,
                             side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, limit_price=100.0)
        assert account.modify_order(str(row.id), patch) is None
        assert all(not p["modification"] for p in fake.placed)

    def test_unknown_order_is_none(self, world):
        account, *_ = world
        assert account.modify_order("424242") is None


# ======================================================================= refresh
class TestRefresh:
    def test_partial_then_full_fill(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account, qty=10.0))
        fake.simulate_fill(ref(account, row), qty=4, price=100.0)
        assert account.refresh_orders() is True
        f = fresh(row)
        assert f.status == OrderStatus.PARTIALLY_FILLED and f.filled_qty == 4 and f.open_price == 100.0
        fake.simulate_fill(ref(account, row), qty=6, price=101.0)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.FILLED and f.filled_qty == 10
        assert f.open_price == pytest.approx(100.6)

    def test_cancelled_after_a_partial_fill_folds_the_fill_back(self, world, monkeypatch):
        account, fake, _ = world
        calls = []
        from ba2_common.core import TransactionHelper as th
        monkeypatch.setattr(th.TransactionHelper, "reconcile_canceled_partial_fill",
                            staticmethod(lambda order: calls.append(order.id)))
        row = submit(account, new_order(account, qty=10.0, order_type=OrderType.BUY_LIMIT, limit=100.0))
        fake.simulate_fill(ref(account, row), qty=3, price=100.0)
        fake.simulate_status(ref(account, row), "Cancelled")
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.CANCELED and f.filled_qty == 3
        assert calls == [row.id]

    def test_rejected_inactive_maps_to_rejected(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account))
        fake.simulate_status(ref(account, row), "Inactive")
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.REJECTED

    def test_match_falls_back_to_broker_id_when_the_ref_is_missing(self, world):
        account, fake, _ = world
        row = submit(account, new_order(account))
        fake.trade_by_ref(ref(account, row)).order.orderRef = ""
        fake.simulate_status("", "Filled")
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.FILLED

    def test_prior_session_trade_matches_by_perm_id(self, world):
        account, fake, aapl = world
        row = new_order(account, broker_order_id="777777777", status=OrderStatus.ACCEPTED,
                        qty=5.0)
        order = Order(action="BUY", totalQuantity=5, orderType="MKT", account=ACCOUNT_ID)
        fake.add_prior_trade(aapl, order, "Filled", filled=5, avg=99.0, perm=777777777)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.FILLED and f.open_price == 99.0

    def test_other_accounts_orders_are_ignored(self, world):
        account, fake, aapl = world
        row = new_order(account, broker_order_id="888888888", status=OrderStatus.ACCEPTED)
        order = Order(action="BUY", totalQuantity=5, orderType="MKT", account="DU9999999")
        fake.add_prior_trade(aapl, order, "Filled", filled=5, avg=99.0, perm=888888888)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED

    def test_an_unknown_ib_status_is_logged_and_does_not_stop_the_pass(self, world, ibkr_logs):
        account, fake, _ = world
        weird = submit(account, new_order(account))
        fine = submit(account, new_order(account))
        fake.simulate_status(ref(account, weird), "Teleported")
        fake.simulate_status(ref(account, fine), "Filled")
        assert account.refresh_orders() is True
        assert fresh(fine).status == OrderStatus.FILLED
        assert fresh(weird).status == OrderStatus.ACCEPTED          # untouched, not guessed
        assert "Teleported" in ibkr_logs.text()

    def test_fetch_failure_is_false(self, world):
        account, fake, _ = world
        fake.connect_failure = ConnectionRefusedError("x")
        assert account.refresh_orders() is False

    def test_absent_old_row_is_never_cancelled_on_absence_alone(self, world, ibkr_logs):
        account, fake, _ = world
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        row = new_order(account, broker_order_id="999999991", status=OrderStatus.ACCEPTED,
                        created_at=old)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED
        assert "UNRESOLVED" in ibkr_logs.text()

    def test_absent_row_with_an_execution_is_filled_not_cancelled(self, world):
        account, fake, aapl = world
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        row = new_order(account, broker_order_id="999999992", status=OrderStatus.ACCEPTED,
                        created_at=old, qty=10.0)
        fake.make_fill_record(aapl, "BOT", 10, 150.0, perm=999999992)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.FILLED and f.open_price == 150.0

    def test_a_young_absent_row_is_left_alone(self, world):
        account, fake, _ = world
        row = new_order(account, broker_order_id="999999993", status=OrderStatus.ACCEPTED)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED

    def test_an_incomplete_book_never_cancels_anything(self, world):
        account, fake, _ = world
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        row = new_order(account, broker_order_id="999999994", status=OrderStatus.ACCEPTED,
                        created_at=old)
        fake.fail_calls["reqCompletedOrdersAsync"] = TimeoutError("slow")
        assert account.refresh_orders() is True
        assert fresh(row).status == OrderStatus.ACCEPTED

    def test_a_very_old_unlisted_row_is_not_guessed_at_either(self, world, ibkr_logs):
        account, fake, _ = world
        old = datetime.now(timezone.utc) - timedelta(days=30)
        row = new_order(account, broker_order_id="999999995", status=OrderStatus.ACCEPTED,
                        created_at=old)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED
        assert "UNRESOLVED" in ibkr_logs.text() and "Flex" in ibkr_logs.text()

    def test_get_orders_and_get_order(self, world):
        account, fake, _ = world
        open_row = submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=90.0))
        done = submit(account, new_order(account))
        fake.simulate_fill(ref(account, done), price=100.0)
        assert {o.status for o in account.get_orders()} == {OrderStatus.ACCEPTED, OrderStatus.FILLED}
        assert [o.status for o in account.get_orders(OrderStatus.OPEN)] == [OrderStatus.ACCEPTED]
        assert [o.status for o in account.get_orders("closed")] == [OrderStatus.FILLED]
        got = account.get_order(open_row.broker_order_id)
        assert (got.symbol, got.quantity, got.order_type, got.limit_price, got.good_for) == (
            "AAPL", 10.0, OrderType.BUY_LIMIT, 90.0, "gtc")
        assert account.get_order("1") is None

    def test_get_orders_failure_is_empty(self, world):
        account, fake, _ = world
        fake.connect_failure = ConnectionRefusedError("x")
        assert account.get_orders() == [] and account.get_order("1") is None


# ======================================================================= OCO
def oco_row(account, txn, qty=10.0, tp=120.0, sl=90.0, side=OrderDirection.SELL):
    return new_order(account, qty=qty, side=side, order_type=OrderType.OCO, limit=tp, stop=sl,
                     transaction_id=txn.id)


class TestOCO:
    def test_one_oca_group_two_orders_stop_first_both_transmitted(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = oco_row(account, txn)
        out = submit(account, parent)
        sl, tp = fake.placed[-2], fake.placed[-1]       # the protective stop exists BEFORE the take-profit
        assert (tp["orderType"], tp["lmt"], tp["transmit"]) == ("LMT", 120.0, True)
        assert (sl["orderType"], sl["aux"], sl["transmit"]) == ("STP LMT", 90.0, True)
        assert sl["lmt"] == pytest.approx(89.55)             # 0.5 % through the stop (the shared cushion)
        assert tp["oca"] == sl["oca"] and tp["oca"].startswith(f"ba2-oca-{account.id}-{parent.id}-")
        assert tp["oca_type"] == sl["oca_type"] == 2
        assert tp["tif"] == sl["tif"] == "GTC" and tp["action"] == sl["action"] == "SELL"
        assert tp["ref"] == ref(account, parent) and sl["ref"] == ref(account, parent) + ":SL"
        assert out.status == OrderStatus.ACCEPTED and len(out.legs_broker_ids) == 2
        kids = rows(account, parent_order_id=parent.id)
        assert len(kids) == 1
        kid = kids[0]
        assert kid.order_type == OrderType.SELL_STOP_LIMIT and kid.stop_price == 90.0
        assert kid.broker_order_id == out.legs_broker_ids[1] and kid.transaction_id == txn.id
        assert "-OCO-SL-[PARENT:" in kid.comment

    def test_buy_side_oco_for_a_short_places_the_cushion_above(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", side=OrderDirection.SELL, status=TransactionStatus.OPENED)
        submit(account, oco_row(account, txn, tp=80.0, sl=110.0, side=OrderDirection.BUY))
        assert fake.placed[-2]["lmt"] == pytest.approx(110.55)       # the stop leg is placed first

    def test_stop_leg_firing_cancels_the_take_profit_and_rows_follow(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        fake.simulate_fill(ref(account, parent) + ":SL", price=89.6)
        account.refresh_orders()
        kid = rows(account, parent_order_id=parent.id)[0]
        assert kid.status == OrderStatus.FILLED and kid.open_price == 89.6
        assert fresh(parent).status == OrderStatus.CANCELED          # IB's OCA cancelled the TP leg

    def test_take_profit_firing_fills_the_parent_and_cancels_the_stop(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        fake.simulate_fill(ref(account, parent), price=120.0)
        account.refresh_orders()
        assert fresh(parent).status == OrderStatus.FILLED
        assert rows(account, parent_order_id=parent.id)[0].status == OrderStatus.CANCELED

    def test_cancelling_the_parent_cancels_both_legs(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, oco_row(account, txn))
        assert account.cancel_order(str(parent.id)) is True
        assert len(fake.cancel_requests) == 2
        assert fresh(parent).status == OrderStatus.PENDING_CANCEL
        assert rows(account, parent_order_id=parent.id)[0].status == OrderStatus.PENDING_CANCEL

    def test_a_rejected_second_leg_never_leaves_the_first_working(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        fake.behaviors += ["accept", ("reject", 201, "Order rejected - stop price too close")]
        parent = oco_row(account, txn)
        assert submit(account, parent) is None
        assert fresh(parent).status == OrderStatus.ERROR
        tp_trade = fake.trades()[0]
        assert tp_trade.order.orderId in fake.cancel_requests          # the TP leg was cancelled
        assert rows(account, parent_order_id=parent.id) == []

    def test_oco_needs_both_prices(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        row = new_order(account, side=OrderDirection.SELL, order_type=OrderType.OCO, limit=120.0,
                        transaction_id=txn.id)
        assert submit(account, row) is None and fake.placed == []
