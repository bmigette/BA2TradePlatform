"""One adapter-level life of a protected position on the fake, with the corrected IBKR semantics:
entry that draws a 399 warning (status ValidationError, order live) -> fill -> OCO exit (stop first,
both transmitted) -> modify (confirmed, then unconfirmed) -> partial fill -> refresh -> reconnect with
reused ids and a failed positions sync -> a retry that must not duplicate."""
import time

import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
from tests.ibkr_helpers import ibkr_logs, make_account  # noqa: F401
from tests.test_ibkr_lifecycle import fresh, rows
from tests.test_ibkr_orders import ref
from tests.test_ibkr_protective_legs import enter, exits, fill_entry, trigger_exit


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    yield account, fake, aapl
    account.close()


def test_the_whole_life_of_a_protected_position(world, monkeypatch):
    account, fake, aapl = world
    monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 1.0)

    # 1. a pre-market entry: IB warns (399, status ValidationError) but the order is live
    fake.behaviors.append("warn399")
    entry = enter(account, fake, tp=120.0, sl=90.0)
    assert entry is not None and entry.status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED)
    assert len(fake.placed) == 1
    time.sleep(0.5)
    account.refresh_orders()
    assert fresh(entry).status == OrderStatus.ACCEPTED
    entry = fill_entry(account, fake, entry, price=100.0)
    assert entry.status == OrderStatus.FILLED
    fake.add_position(aapl, 10, 100.0, mark=125.0)

    # 2. the exit: stop first, both transmitted, one OCA group
    trigger_exit(account, fake)
    sl, tp = fake.placed[-2], fake.placed[-1]
    assert (sl["orderType"], tp["orderType"]) == ("STP LMT", "LMT")
    assert sl["transmit"] is True and tp["transmit"] is True and sl["oca"] == tp["oca"]
    (oco,) = [e for e in exits(account, entry) if e.status == OrderStatus.ACCEPTED]
    txn = get_instance(Transaction, entry.transaction_id)

    # 3. a confirmed modification moves both legs in place
    assert account.adjust_tp_sl(txn, 125.0, 95.0, source="ruleset") is True
    assert len([p for p in fake.placed if p["modification"]]) == 2 and not fake.cancel_requests
    assert fresh(oco).limit_price == 125.0

    # 4. an UNCONFIRMED modification is not stored as truth: the safe cancel-and-chain path runs
    fake.modify_behavior = "silent"
    monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.3)
    assert account.adjust_tp_sl(get_instance(Transaction, txn.id), 130.0, 96.0, source="ruleset") is True
    assert fresh(oco).limit_price == 125.0 and fresh(oco).stop_price == 95.0
    assert len(fake.cancel_requests) == 2
    fake.modify_behavior = "confirm"

    # 5. the exit legs are cancelled for good (the staged replacement waits on that)
    account.refresh_orders()
    assert fresh(oco).status == OrderStatus.CANCELED

    # 6. a fresh OCO from the staged replacement, then a PARTIAL take-profit shrinks the stop leg
    account.refresh_orders()
    trigger_exit(account, fake)                                   # TradeManager submits the staged exit
    live = [e for e in exits(account, entry) if e.status == OrderStatus.ACCEPTED and e.order_type == OrderType.OCO]
    assert live, "the staged replacement exit was submitted once the cancel was confirmed"
    new_oco = live[0]
    fake.simulate_fill(ref(account, new_oco), qty=4, price=130.0)
    stop_leg = fake.trade_by_ref(ref(account, new_oco) + ":SL")
    fake.simulate_ib_quantity(stop_leg.order.orderRef, new_oco.quantity - 4)   # ocaType 2: reduced
    account.refresh_orders()
    assert fresh(new_oco).status == OrderStatus.PARTIALLY_FILLED
    assert rows(account, parent_order_id=new_oco.id)[0].quantity == new_oco.quantity - 4

    # 7. a reconnect: ids restart and the positions sync fails; nothing reads as FLAT
    fake._positions.clear()
    fake._portfolio.clear()
    fake.add_position(aapl, 6, 100.0, mark=130.0)
    fake._trades.clear()                                          # a new session: the id counter restarts
    fake.reset_ids_on_connect = 100
    fake.simulate_disconnect()
    time.sleep(0.3)
    fake.positions_sync_fails = True
    assert account.get_positions() is None
    time.sleep(0.3)
    fake.positions_sync_fails = False
    assert len(account.get_positions()) == 1
    rt = account._runtime()
    assert rt.order_errors(100) == []                             # history cleared on connect

    # 8. a new order reuses id 100: no stale error, and a retry of it does not duplicate
    fake.behaviors.append("slow")
    second = TradingOrder(account_id=account.id, symbol="AAPL", quantity=1, side=OrderDirection.BUY,
                          order_type=OrderType.MARKET, status=OrderStatus.PENDING)
    from ba2_trade_platform.core.db import add_instance
    second = get_instance(TradingOrder, add_instance(second))
    out = account._submit_order_impl(second)
    assert out is not None and out.status in (OrderStatus.PENDING_NEW, OrderStatus.ACCEPTED)
    n = len(fake.placed)
    retry = get_instance(TradingOrder, second.id)
    retry.broker_order_id = None
    from ba2_trade_platform.core.db import update_instance
    update_instance(retry)
    account._submit_order_impl(get_instance(TradingOrder, second.id))
    assert len(fake.placed) == n
