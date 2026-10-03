"""The standing TP/SL exit order on IBKR: template flow, OCO / limit / stop shapes, in-place price
moves (no gap in protection), cancel-and-chain fallback. Against FakeIB; no network."""
import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus)
from tests.ibkr_helpers import ibkr_logs, make_account  # noqa: F401
from tests.test_ibkr_lifecycle import fresh, rows
from tests.test_ibkr_orders import last_placed, new_order, ref, submit


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    yield account, fake, aapl
    account.close()


def enter(account, fake, tp=None, sl=None, qty=10.0):
    """An entry through the REAL template (validation, transaction, protective legs)."""
    entry = TradingOrder(account_id=account.id, symbol="AAPL", quantity=qty, side=OrderDirection.BUY,
                         order_type=OrderType.MARKET, status=OrderStatus.PENDING)
    return account.submit_order(entry, tp_price=tp, sl_price=sl)


def exits(account, entry):
    return [r for r in rows(account, transaction_id=entry.transaction_id) if r.id != entry.id
            and r.parent_order_id is None]


def fill_entry(account, fake, entry, price=100.0):
    fake.simulate_fill(ref(account, entry), price=price)
    account.refresh_orders()
    return fresh(entry)


def trigger_exit(account, fake):
    """What the platform does next: TradeManager submits the WAITING_TRIGGER exit once its entry
    has FILLED (re-basing it, then ``account.submit_order``). Runs the REAL TradeManager step."""
    from ba2_trade_platform.core.TradeManager import TradeManager
    TradeManager()._check_all_waiting_trigger_orders()


class TestTemplateFlow:
    def test_entry_goes_out_and_the_exit_waits_in_the_db(self, world):
        account, fake, _ = world
        entry = enter(account, fake, tp=120.0, sl=90.0)
        assert entry.status == OrderStatus.ACCEPTED and entry.transaction_id
        assert len(fake.placed) == 1                      # ONLY the entry went to IB
        (waiting,) = exits(account, entry)
        assert waiting.order_type == OrderType.OCO and waiting.status == OrderStatus.WAITING_TRIGGER
        assert waiting.depends_on_order == entry.id
        assert (waiting.limit_price, waiting.stop_price) == (120.0, 90.0)
        txn = get_instance(Transaction, entry.transaction_id)
        assert (txn.take_profit, txn.stop_loss) == (120.0, 90.0)

    def test_stop_only_and_tp_only_shapes(self, world):
        account, fake, _ = world
        a = enter(account, fake, sl=90.0)
        assert [e.order_type for e in exits(account, a)] == [OrderType.SELL_STOP]
        b = enter(account, fake, tp=130.0)
        assert [e.order_type for e in exits(account, b)] == [OrderType.SELL_LIMIT]

    def test_no_protection_requested_creates_no_exit(self, world):
        account, fake, _ = world
        entry = enter(account, fake)
        assert exits(account, entry) == []

    def test_after_the_fill_adjust_places_the_oco_at_ibkr(self, world):
        account, fake, _ = world
        entry = enter(account, fake, tp=120.0, sl=90.0)
        entry = fill_entry(account, fake, entry)
        assert entry.status == OrderStatus.FILLED
        trigger_exit(account, fake)
        placed = fake.placed[-2:]
        assert [p["orderType"] for p in placed] == ["LMT", "STP LMT"]
        assert placed[0]["qty"] == 10.0 and placed[0]["action"] == "SELL"
        live = [e for e in exits(account, entry) if e.status != OrderStatus.CANCELED]
        assert [(e.order_type, e.status) for e in live] == [(OrderType.OCO, OrderStatus.ACCEPTED)]
        assert len(rows(account, parent_order_id=live[0].id)) == 1

    def test_idempotent_when_nothing_changed(self, world):
        account, fake, _ = world
        entry = fill_entry(account, fake, enter(account, fake, tp=120.0, sl=90.0))
        txn = get_instance(Transaction, entry.transaction_id)
        trigger_exit(account, fake)
        n = len(fake.placed)
        assert account.adjust_tp_sl(txn, 120.0, 90.0) is True
        assert len(fake.placed) == n and not fake.cancel_requests

    def test_manual_lock_blocks_an_automatic_change(self, world):
        account, fake, _ = world
        entry = fill_entry(account, fake, enter(account, fake, tp=120.0, sl=90.0))
        txn = get_instance(Transaction, entry.transaction_id)
        trigger_exit(account, fake)
        account.adjust_sl(txn, 85.0, source="manual")
        n = len(fake.placed)
        assert account.adjust_sl(get_instance(Transaction, txn.id), 80.0, source="ruleset") is True
        assert get_instance(Transaction, txn.id).stop_loss == 85.0 and len(fake.placed) == n


class TestInPlaceModification:
    def setup_oco(self, account, fake):
        entry = fill_entry(account, fake, enter(account, fake, tp=120.0, sl=90.0))
        txn = get_instance(Transaction, entry.transaction_id)
        trigger_exit(account, fake)
        (oco,) = [e for e in exits(account, entry) if e.status == OrderStatus.ACCEPTED]
        return entry, txn, oco

    def test_new_prices_move_both_legs_in_place_with_no_gap(self, world):
        account, fake, _ = world
        entry, txn, oco = self.setup_oco(account, fake)
        before = len(fake.trades())
        assert account.adjust_tp_sl(txn, 125.0, 95.0, source="ruleset") is True
        mods = [p for p in fake.placed if p["modification"]]
        assert len(mods) == 2 and fake.cancel_requests == []         # the stop was never cancelled
        assert {m["orderType"] for m in mods} == {"LMT", "STP LMT"}
        assert next(m for m in mods if m["orderType"] == "LMT")["lmt"] == 125.0
        stp = next(m for m in mods if m["orderType"] == "STP LMT")
        assert stp["aux"] == 95.0 and stp["lmt"] == pytest.approx(94.53)
        assert len(fake.trades()) == before                          # no new IB orders
        o = fresh(oco)
        assert (o.limit_price, o.stop_price, o.status) == (125.0, 95.0, OrderStatus.ACCEPTED)
        kid = rows(account, parent_order_id=oco.id)[0]
        assert kid.stop_price == 95.0 and kid.limit_price == pytest.approx(94.53)
        assert get_instance(Transaction, txn.id).stop_loss == 95.0

    def test_stop_only_and_tp_only_orders_move_in_place_too(self, world):
        account, fake, _ = world
        entry = fill_entry(account, fake, enter(account, fake, sl=90.0))
        txn = get_instance(Transaction, entry.transaction_id)
        trigger_exit(account, fake)
        assert account.adjust_sl(txn, 92.5, source="ruleset") is True
        assert [p for p in fake.placed if p["modification"]][-1]["aux"] == 92.5
        assert fake.cancel_requests == []

    def test_a_size_change_falls_back_to_cancel_and_chained_replacement(self, world):
        account, fake, _ = world
        entry, txn, oco = self.setup_oco(account, fake)
        # the position grew: an add of 5 filled on the same transaction
        add = new_order(account, qty=5.0, transaction_id=txn.id, status=OrderStatus.FILLED,
                        filled_qty=5.0)
        assert account.adjust_tp_sl(txn, 125.0, 95.0, source="ruleset") is True
        assert not [p for p in fake.placed if p["modification"]]
        assert len(fake.cancel_requests) == 2                         # both legs cancelled
        staged = [e for e in exits(account, entry) if e.status == OrderStatus.WAITING_TRIGGER]
        assert len(staged) == 1
        kid = rows(account, parent_order_id=oco.id)[0]
        assert staged[0].depends_on_order == kid.id        # chained on the newest live leg
        assert staged[0].depends_order_status_trigger == OrderStatus.CANCELED
        assert staged[0].quantity == 15.0

    def test_structure_change_oco_to_stop_only_cancels_the_pair(self, world):
        account, fake, _ = world
        entry, txn, oco = self.setup_oco(account, fake)
        from ba2_trade_platform.core.db import update_instance
        t = get_instance(Transaction, txn.id)
        t.take_profit = None
        update_instance(t)
        assert account.adjust_sl(get_instance(Transaction, txn.id), 91.0, source="ruleset") is True
        assert len(fake.cancel_requests) == 2
        assert [e.order_type for e in exits(account, entry) if e.status == OrderStatus.WAITING_TRIGGER] \
            == [OrderType.SELL_STOP]

    def test_failed_second_leg_modification_reverts_the_first_and_falls_back(self, world):
        account, fake, _ = world
        entry, txn, oco = self.setup_oco(account, fake)
        original = account.modify_order
        calls = []

        def flaky(order_id, trading_order=None):
            calls.append(order_id)
            if len(calls) == 2:
                return None                      # the SL leg refuses
            return original(order_id, trading_order)

        account.modify_order = flaky
        assert account.adjust_tp_sl(txn, 125.0, 95.0, source="ruleset") is True
        assert len(calls) >= 3                   # parent, child (refused), parent reverted
        assert len(fake.cancel_requests) == 2    # then the safe cancel-and-chain path ran


class TestDependentOrders:
    def test_pending_dependent_submits_when_its_parent_is_cancelled(self, world):
        account, fake, _ = world
        entry = fill_entry(account, fake, enter(account, fake, tp=120.0, sl=90.0))
        txn = get_instance(Transaction, entry.transaction_id)
        trigger_exit(account, fake)
        (oco,) = [e for e in exits(account, entry) if e.status == OrderStatus.ACCEPTED]
        child_row = new_order(account, side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                              stop=88.0, qty=10.0, transaction_id=txn.id, depends_on_order=oco.id,
                              depends_order_status_trigger=OrderStatus.CANCELED,
                              status=OrderStatus.PENDING)
        assert account.cancel_order(str(oco.id))
        account.refresh_orders()           # IB confirmed the cancel -> dependent fires in this pass
        assert fresh(child_row).status in (OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW)
        assert fresh(child_row).broker_order_id


class TestClosingAPositionThatIsProtected:
    def test_close_transaction_cancels_the_oco_and_sells_at_market(self, world):
        account, fake, aapl = world
        entry = fill_entry(account, fake, enter(account, fake, tp=120.0, sl=90.0))
        txn = get_instance(Transaction, entry.transaction_id)
        from ba2_trade_platform.core.db import update_instance
        txn.status = TransactionStatus.OPENED
        txn.open_price = 100.0
        update_instance(txn)
        trigger_exit(account, fake)
        fake.add_position(aapl, 10, 100.0, mark=101.0)
        result = account.close_transaction(txn.id)
        assert result["success"] is True, result
        assert len(fake.cancel_requests) == 2                         # both OCO legs
        market_sells = [p for p in fake.placed if p["orderType"] == "MKT" and p["action"] == "SELL"]
        closes = [r for r in rows(account, transaction_id=txn.id)
                  if r.side == OrderDirection.SELL and r.order_type == OrderType.MARKET]
        assert closes, "a market close order must exist for the transaction"
        # the close either went straight out or waits for the cancels to be confirmed
        assert market_sells or closes[0].status in (OrderStatus.WAITING_TRIGGER, OrderStatus.PENDING)
