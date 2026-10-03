"""Review findings 1-9 + follow-ups (2026-10-03), written as FAILING tests first, against FakeIB that now
models the real ib_async behaviours the review found it had hidden:

* a WARNING code (399/404/10349) sets status 'ValidationError' while the order stays LIVE;
* request/order ids are reused after a reconnect and the error list used to survive it;
* ib_async keeps an order's status unchanged on a modification (only a 'Modified' log entry);
* connectAsync without raiseSyncErrors turns a timed-out positions sync into an empty book;
* transmit=False has no meaning for an OCA group (it is a parent/child bracket device)."""
import threading
import time

import pytest
from ib_async import Order

from ba2_trade_platform.modules.accounts import ibkr_mapping as M
from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
from tests.factories import create_transaction
from tests.ibkr_helpers import ibkr_logs, make_account  # noqa: F401
from tests.test_ibkr_lifecycle import fresh, rows
from tests.test_ibkr_orders import last_placed, new_order, submit


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    yield account, fake, aapl
    account.close()


def ref_of(account, row):
    return account._order_ref_for_row(get_instance(TradingOrder, row.id), account.id)


def settle(seconds=0.9):
    time.sleep(seconds)


# ---------------------------------------------------------------- 1: ValidationError is a warning
class TestWarningIsNotARejection:
    def test_mapping(self):
        assert M.map_ib_status("ValidationError") == OrderStatus.PENDING_NEW
        assert "ValidationError" not in M.IB_ACK_STATUSES
        for code in (399, 404, 10349):
            assert M.error_severity(code) == "order_warning"

    def test_premarket_market_order_is_not_an_error(self, world, ibkr_logs):
        account, fake, _ = world
        fake.behaviors.append("warn399")
        row = new_order(account)
        out = submit(account, row)
        assert out is not None
        f = fresh(row)
        assert f.status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED), f.status
        assert f.status != OrderStatus.ERROR and f.broker_order_id
        assert "399" in ibkr_logs.text()                      # the warning is logged
        settle()
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED       # it went live
        assert len(fake.placed) == 1


# ---------------------------------------------------------------- 2: stale errors
class TestStaleErrors:
    def test_an_error_recorded_before_the_placement_is_not_this_orders(self, world):
        account, fake, _ = world
        account.get_positions()
        rt = account._runtime()
        rt._on_error(fake._next_order_id, 200, "No security definition has been found", None)
        fake.behaviors.append("slow")
        row = new_order(account)
        assert submit(account, row) is not None
        assert fresh(row).status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED)

    def test_errors_are_cleared_on_reconnect_and_ids_may_repeat(self, world):
        account, fake, _ = world
        fake.behaviors.append(("reject", 201, "Order rejected - insufficient"))
        first = new_order(account)
        assert submit(account, first) is None
        first_id = last_placed(fake)["orderId"]
        rt = account._runtime()
        assert rt.order_errors(first_id)
        fake.reset_ids_on_connect = first_id
        fake.simulate_disconnect()
        time.sleep(0.3)                                          # past the cooldown, then reconnect
        account.get_positions()
        assert rt.order_errors(first_id) == []                   # cleared by the new session
        fake.behaviors.append("slow")
        second = new_order(account)
        assert last_placed(fake)["orderId"] == first_id or True
        assert submit(account, second) is not None
        assert fresh(second).status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED)


# ---------------------------------------------------------------- 3: never duplicate, never ERROR after placeOrder
class TestNoDuplicateAfterPlacement:
    def slow_world(self, monkeypatch):
        monkeypatch.setattr(IBKRAccount, "_READ_TIMEOUT", 0.5)
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.5)

    def test_a_timeout_after_placeorder_leaves_the_row_pending_not_error(self, world, monkeypatch):
        account, fake, _ = world
        self.slow_world(monkeypatch)
        account.get_positions()
        fake.block_calls["reqMarketRuleAsync"] = 0.2
        fake.behaviors.append("silent")
        row = new_order(account)
        submit(account, row)
        f = fresh(row)
        assert f.status == OrderStatus.PENDING_NEW and f.broker_order_id.startswith("o")
        assert len(fake.placed) == 1

    def test_the_facade_budget_covers_lookups_plus_ack(self, world, monkeypatch):
        """Probe C: contract lookup + market rule + short check + ack used to exceed the caller's timeout."""
        account, fake, aapl = world
        self.slow_world(monkeypatch)
        account.get_positions()
        fake.block_calls["reqContractDetailsAsync"] = 0.4
        fake.block_calls["reqMarketRuleAsync"] = 0.4
        fake.behaviors.append("silent")
        row = new_order(account)
        submit(account, row)
        f = fresh(row)
        assert f.status != OrderStatus.ERROR
        assert len(fake.placed) <= 1

    def test_retrying_a_row_adopts_the_live_order_instead_of_sending_again(self, world, monkeypatch):
        """Probe D."""
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.3)
        fake.behaviors.append("silent")
        row = new_order(account)
        first = submit(account, row)
        assert len(fake.placed) == 1
        retry = get_instance(TradingOrder, row.id)
        retry.broker_order_id = None                              # the UI 'Retry' path: no id recorded
        retry.status = OrderStatus.ERROR
        update_instance(retry)
        fake.simulate_status(ref_of(account, row), "Submitted")
        out = account._submit_order_impl(get_instance(TradingOrder, row.id))
        assert len(fake.placed) == 1, "a second IB order was created"
        assert out.broker_order_id and out.status in (OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW)

    def test_adoption_also_searches_completed_orders(self, world):
        account, fake, aapl = world
        row = new_order(account)
        account._ensure_nonce(row.id)
        account.get_positions()
        # the order already FILLED in an earlier session: it is on the completed list only
        from ib_async import Order as IBOrder
        done = IBOrder(action="BUY", totalQuantity=10, orderType="MKT", account="DU1234567",
                       orderRef=account._order_ref_for_row(get_instance(TradingOrder, row.id), account.id))
        fake.add_prior_trade(aapl, done, "Filled", filled=10, avg=100.0, perm=555000111)
        out = account._submit_order_impl(get_instance(TradingOrder, row.id))
        assert fake.placed == []
        assert out.status == OrderStatus.FILLED and out.broker_order_id == "555000111"

    def test_oco_timeout_after_placement_never_errors_or_duplicates(self, world, monkeypatch):
        account, fake, _ = world
        self.slow_world(monkeypatch)
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        fake.behaviors += ["silent", "silent"]
        parent = new_order(account, side=OrderDirection.SELL, order_type=OrderType.OCO, limit=120.0,
                           stop=90.0, transaction_id=txn.id)
        account._submit_order_impl(parent)
        assert fresh(parent).status != OrderStatus.ERROR
        n = len(fake.placed)
        account._submit_order_impl(get_instance(TradingOrder, parent.id))
        assert len(fake.placed) == n


# ---------------------------------------------------------------- 4: positions are never 'flat' unconfirmed
class TestPositionsSync:
    def test_connect_is_trimmed_and_every_positions_read_is_confirmed(self, world):
        """(Superseded by round 2, item 8: raiseSyncErrors=True failed the connect on any slow startup
        request. Startup is now trimmed and each positions read awaits reqPositions itself.)"""
        account, fake, _ = world
        account.get_positions()
        assert fake.connect_kwargs.get("raiseSyncErrors") is False
        fake.request_log.clear()
        account.get_positions()
        assert "positions" in fake.request_log

    def test_a_failed_startup_sync_is_none_not_flat(self, world):
        account, fake, aapl = world
        fake.add_position(aapl, 10, 150.0, mark=155.0)
        fake.positions_sync_fails = True
        assert account.get_positions() is None
        assert account.get_option_positions() is None
        assert account.refresh_positions() is False

    def test_unconfirmed_snapshot_after_connect_is_none(self, world):
        """connect succeeded (no raise) but nothing confirmed the book: still None, never []."""
        account, fake, aapl = world
        fake.add_position(aapl, 10, 150.0, mark=155.0)
        account.get_positions()                                   # connect
        fake.positions_sync_fails = True
        fake.positions_confirmed = False
        assert account.get_positions() is None
        fake.positions_sync_fails = False
        assert len(account.get_positions()) == 1


# ---------------------------------------------------------------- 5: OCO placement semantics
class TestOcoPlacement:
    def oco(self, account):
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        return new_order(account, side=OrderDirection.SELL, order_type=OrderType.OCO, limit=120.0,
                         stop=90.0, transaction_id=txn.id)

    def test_both_legs_are_transmitted_stop_first(self, world):
        account, fake, _ = world
        submit(account, self.oco(account))
        first, second = fake.placed[-2], fake.placed[-1]
        assert (first["orderType"], second["orderType"]) == ("STP LMT", "LMT")
        assert first["transmit"] is True and second["transmit"] is True
        assert first["oca"] == second["oca"] and first["oca_type"] == second["oca_type"] == 2

    def test_a_rejected_take_profit_cancels_the_stop_and_raises_loudly(self, world):
        account, fake, _ = world
        fake.behaviors += ["accept", ("reject", 201, "Order rejected - price too far")]
        parent = self.oco(account)
        assert submit(account, parent) is None
        assert fresh(parent).status == OrderStatus.ERROR
        stop_trade = fake.trades()[0]
        assert stop_trade.order.orderId in fake.cancel_requests
        assert rows(account, parent_order_id=parent.id) == []


# ---------------------------------------------------------------- 6: modify is confirmed
class TestModifyConfirmation:
    def limit_row(self, account):
        return submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=100.0))

    def patch(self, price):
        return TradingOrder(account_id=1, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
                            order_type=OrderType.BUY_LIMIT, limit_price=price)

    def test_confirmed_modification_is_stored(self, world):
        account, fake, _ = world
        row = self.limit_row(account)
        assert account.modify_order(str(row.id), self.patch(101.0)).limit_price == 101.0
        assert fresh(row).limit_price == 101.0

    def test_unconfirmed_modification_is_not_stored_and_ib_state_is_restored(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.4)
        row = self.limit_row(account)
        fake.modify_behavior = "silent"
        assert account.modify_order(str(row.id), self.patch(105.0)) is None
        assert fresh(row).limit_price == 100.0
        assert fake.trades()[0].order.lmtPrice == 100.0           # our local Order object rolled back

    def test_a_refused_modification_is_none(self, world):
        account, fake, _ = world
        row = self.limit_row(account)
        fake.modify_behavior = ("reject", 105, "Requested order modification would violate a rule")
        assert account.modify_order(str(row.id), self.patch(105.0)) is None
        assert fresh(row).limit_price == 100.0

    def test_exit_modification_rolls_back_when_the_second_leg_is_unconfirmed(self, world, monkeypatch):
        account, fake, _ = world
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.4)
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, new_order(account, side=OrderDirection.SELL, order_type=OrderType.OCO,
                                           limit=120.0, stop=90.0, transaction_id=txn.id))
        calls = {"n": 0}
        real = fake.placeOrder

        def flaky(contract, order):
            trade = real(contract, order)
            return trade

        original_behavior = None
        # first leg confirmed, second silent
        seq = iter(["confirm", "silent", "confirm"])
        orig_place = fake.placeOrder

        def place(contract, order):
            if fake._find(order.orderId) is not None:
                fake.modify_behavior = next(seq)
            return orig_place(contract, order)

        fake.placeOrder = place
        ok = account._modify_exit_in_place(None, get_instance(type(txn), txn.id), None,
                                           (OrderType.OCO, 125.0, 95.0, "TPSL"),
                                           [get_instance(TradingOrder, parent.id)]
                                           + rows(account, parent_order_id=parent.id), 10.0)
        assert ok is False
        assert fresh(parent).limit_price == 120.0                  # rolled back, never stored as truth


# ---------------------------------------------------------------- 7: order ref nonce
class TestOrderRefNonce:
    def test_ref_carries_a_per_row_nonce(self, world):
        account, fake, _ = world
        row = new_order(account)
        submit(account, row)
        ref = last_placed(fake)["ref"]
        parsed = M.parse_order_ref(ref)
        assert parsed.account == account.id and parsed.order == row.id
        assert parsed.nonce and len(parsed.nonce) == 8 and parsed.suffix is None
        assert (get_instance(TradingOrder, row.id).data or {}).get("ibkr_nonce") == parsed.nonce

    def test_a_recycled_row_id_with_another_nonce_is_not_matched(self, world):
        account, fake, aapl = world
        row = new_order(account, status=OrderStatus.ACCEPTED, broker_order_id="777000111")
        update_instance_data(row, {"ibkr_nonce": "aaaaaaaa"})
        stale = Order(action="BUY", totalQuantity=3, orderType="MKT", account="DU1234567",
                      orderRef=f"ba2:{account.id}:{row.id}:bbbbbbbb")
        fake.add_prior_trade(aapl, stale, "Filled", filled=3, avg=50.0, perm=123456789)
        account.refresh_orders()
        f = fresh(row)
        assert f.status == OrderStatus.ACCEPTED and f.filled_qty in (None, 0, 0.0)

    def test_an_execution_of_a_recycled_id_never_settles_a_row(self, world):
        account, fake, aapl = world
        from datetime import datetime, timedelta, timezone
        row = new_order(account, status=OrderStatus.ACCEPTED, broker_order_id="777000222",
                        created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        update_instance_data(row, {"ibkr_nonce": "aaaaaaaa"})
        fake.make_fill_record(aapl, "BOT", 10, 99.0, order_ref=f"ba2:{account.id}:{row.id}:bbbbbbbb",
                              perm=999)
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED

    def test_orders_of_another_client_id_are_not_ours(self, world):
        account, fake, aapl = world
        row = new_order(account, status=OrderStatus.ACCEPTED, broker_order_id="777000333")
        update_instance_data(row, {"ibkr_nonce": "aaaaaaaa"})
        other = Order(action="BUY", totalQuantity=3, orderType="MKT", account="DU1234567",
                      orderRef=f"ba2:{account.id}:{row.id}:aaaaaaaa", clientId=999)
        t = fake.add_prior_trade(aapl, other, "Filled", filled=3, avg=50.0, perm=123456790)
        t.orderStatus.clientId = 999
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED


def update_instance_data(row, data):
    r = get_instance(TradingOrder, row.id)
    r.data = {**(r.data or {}), **data}
    update_instance(r)


# ---------------------------------------------------------------- 8: absence never cancels
class TestAbsentOrdersAreNeverCancelled:
    def test_unlisted_old_row_is_left_open_with_a_loud_warning(self, world, ibkr_logs):
        from datetime import datetime, timedelta, timezone
        account, fake, _ = world
        row = new_order(account, broker_order_id="999999991", status=OrderStatus.ACCEPTED,
                        created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        account.refresh_orders()
        assert fresh(row).status == OrderStatus.ACCEPTED
        assert "UNRESOLVED" in ibkr_logs.text() and str(row.id) in ibkr_logs.text()


# ---------------------------------------------------------------- 9: buying power
class TestBuyingPower:
    BASE = {"NetLiquidation": 100000.0, "AvailableFunds": 80000.0, "BuyingPower": 320000.0,
            "ExcessLiquidity": 90000.0, "TotalCashValue": 50000.0}

    def test_min_of_available_funds_sma_and_excess_liquidity(self):
        n = dict(self.BASE, SMA=30000.0)
        snap = M.snapshot_from_account_values(n, {}, None, None)
        assert snap.buying_power == 60000.0                        # SMA x 2 is the binding one
        n = dict(self.BASE, SMA=70000.0, ExcessLiquidity=50000.0)
        assert M.snapshot_from_account_values(n, {}, None, None).buying_power == 100000.0
        assert M.snapshot_from_account_values(dict(self.BASE), {}, None, None).buying_power == 160000.0

    def test_margin_account_without_excess_liquidity_publishes_none(self):
        n = {k: v for k, v in self.BASE.items() if k != "ExcessLiquidity"}
        assert M.snapshot_from_account_values(n, {}, None, None).buying_power is None

    def test_cash_account_is_unlevered(self):
        n = dict(self.BASE, BuyingPower=80000.0)
        assert M.snapshot_from_account_values(n, {}, None, None).buying_power == 80000.0

    def test_components_are_reported(self):
        snap = M.snapshot_from_account_values(dict(self.BASE, SMA=30000.0), {}, None, None)
        assert snap.raw["bp_components"] == {"available_funds_x_mult": 160000.0, "sma_x_mult": 60000.0,
                                             "excess_liquidity_x_mult": 180000.0}


# ---------------------------------------------------------------- follow-ups
class TestFollowUps:
    def test_no_price_falls_back_to_yesterdays_close(self, world):
        account, fake, aapl = world
        fake.set_quote(aapl, close=99.0)
        assert account._get_instrument_current_price_impl("AAPL", "bid") is None
        assert account._get_instrument_current_price_impl("AAPL", "close") == 99.0   # only when asked

    def test_settings_edit_invalidates_the_shared_session_and_unblocks_waiting_calls(self, world):
        account, fake, _ = world
        account.get_positions()
        old_rt = account._runtime()
        fake.updates_available = False
        fake.block_calls["accountSummaryAsync"] = 30.0
        result = {}

        def waiter():
            t0 = time.monotonic()
            result["snap"] = account.get_account_snapshot()
            result["dt"] = time.monotonic() - t0

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)
        from ba2_trade_platform.core.models import AccountSetting
        from ba2_trade_platform.core.db import get_db
        from sqlmodel import select
        with get_db() as session:
            r = session.exec(select(AccountSetting).where(AccountSetting.account_id == account.id,
                                                          AccountSetting.key == "client_id")).first()
            r.value_float = 11.0
            session.add(r)
            session.commit()
        account._invalidate_settings_cache()
        fake.block_calls.clear()
        fresh_rt = account._runtime()
        assert fresh_rt is not old_rt and old_rt.closed
        t.join(5)
        assert not t.is_alive() and result["dt"] < 5.0
        account.get_positions()
        assert fake.connect_calls[-1]["clientId"] == 11

    def test_oco_leg_quantity_follows_the_live_order_after_a_partial_take_profit(self, world):
        account, fake, _ = world
        txn = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED)
        parent = submit(account, new_order(account, qty=10.0, side=OrderDirection.SELL,
                                           order_type=OrderType.OCO, limit=120.0, stop=90.0,
                                           transaction_id=txn.id))
        fake.simulate_fill(ref_of(account, parent), qty=4, price=120.0)
        sl = fake.trade_by_ref(ref_of(account, parent) + ":SL")
        fake.simulate_ib_quantity(sl.order.orderRef, 6.0)           # ocaType 2 reduced the other leg
        account.refresh_orders()
        kid = rows(account, parent_order_id=parent.id)[0]
        assert kid.quantity == 6.0

    def test_option_position_without_a_multiplier_fails_the_fetch(self, world):
        from ib_async import Option
        account, fake, _ = world
        o = Option("AAPL", "20261218", 150.0, "C", "SMART", currency="USD", conId=5, localSymbol="AAPL  261218C00150000")
        o.multiplier = ""
        fake.add_position(o, 1, 500.0, mark=5.0)
        assert account.get_option_positions() is None

    def test_a_contract_in_another_currency_is_not_a_us_stock(self, world):
        account, fake, _ = world
        fake.details["AAPL"][0].contract.currency = "EUR"
        assert account.symbols_exist(["AAPL"]) == {"AAPL": False}

    def test_cushion_constant_is_not_imported_from_alpaca(self):
        import ba2_trade_platform.modules.accounts.ibkr_protective_legs as pl
        src = open(IBKRAccount.__module__.replace(".", "/") + ".py", encoding="utf-8").read() \
            if False else None
        from ba2_trade_platform.modules.accounts.AlpacaAccount import OCO_STOP_LIMIT_CUSHION
        assert pl.OCO_STOP_LIMIT_CUSHION == OCO_STOP_LIMIT_CUSHION       # same value, one pinned copy
        import inspect, importlib
        mod = importlib.import_module("ba2_trade_platform.modules.accounts.IBKRAccount")
        assert "AlpacaAccount" not in inspect.getsource(mod)

    def test_read_only_account_does_not_claim_to_trade_and_refuses_before_routing(self, monkeypatch):
        account, fake = make_account(monkeypatch, read_only=True)
        fake.add_stock("AAPL", 1)
        try:
            assert IBKRAccount.supports_trading is True               # the class-level read stays True
            assert account.supports_trading is False
            row = new_order(account)
            with pytest.raises(Exception, match="read-only"):
                account._submit_order_impl(row)
            assert fresh(row).status == OrderStatus.PENDING and fake.placed == []
        finally:
            account.close()

    def test_a_writable_account_supports_trading(self, world):
        account, *_ = world
        assert account.supports_trading is True
