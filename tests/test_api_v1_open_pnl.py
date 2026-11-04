"""Open P&L on GET /transactions and /transactions/{id}."""
import pytest

from ba2_common.core.TransactionHelper import TransactionHelper
from ba2_trade_platform.core.types import AssetClass, OrderDirection, OrderStatus, TransactionStatus
from ba2_trade_platform.services import open_pnl as svc
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import create_account_definition, create_trading_order, create_transaction

pytestmark = pytest.mark.api_db


class PriceBroker(FakeAccount):
    def __init__(self, account_id, positions, feed=None):
        super().__init__(account_id)
        self.positions = positions
        self.feed = feed or {}
        self.position_reads = 0
        self.feed_error = None

    def get_positions(self):
        self.position_reads += 1
        return self.positions

    def get_instrument_current_price(self, symbols, price_type="mid"):
        if self.feed_error:
            raise self.feed_error
        return {s: self.feed[s] for s in symbols if s in self.feed}


def _open(account, symbol, qty=10.0, price=100.0, side=OrderDirection.BUY, status=TransactionStatus.OPENED, **kw):
    tx = create_transaction(symbol=symbol, quantity=qty, side=side, status=status, open_price=price, **kw)
    create_trading_order(account.id, symbol=symbol, transaction_id=tx.id, status=OrderStatus.FILLED)
    return tx


@pytest.fixture
def world(monkeypatch):
    a1, a2 = create_account_definition(name="One"), create_account_definition(name="Two")
    brokers = {a1.id: PriceBroker(a1.id, [{"symbol": "AAPL", "current_price": 110.0},
                                          {"symbol": "TSLA", "current_price": 90.0}], feed={"MSFT": 305.0}),
               a2.id: PriceBroker(a2.id, [])}
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: brokers.get(i))
    return a1, a2, brokers


def _items(**params):
    body = v1_client().get("/transactions", params=params).json()
    return {i["symbol"]: i for i in body["items"]}, body


def test_long_short_and_feed_priced_rows_use_the_live_trades_formula(world):
    a1, a2, brokers = world
    _open(a1, "AAPL")
    _open(a1, "TSLA", qty=5.0, price=100.0, side=OrderDirection.SELL)
    _open(a1, "MSFT", qty=2.0, price=300.0)                     # not held at the broker: the account feed prices it
    items, body = _items()
    assert items["AAPL"]["open_pnl"] == {"amount": 100.0, "percent": 10.0} and items["AAPL"]["current_price"] == 110.0
    assert items["AAPL"]["pnl_status"] == "open_measured" and items["AAPL"]["realized_pnl"] is None
    assert items["TSLA"]["open_pnl"]["amount"] == 50.0           # short: (100 - 90) * 5
    assert items["MSFT"]["open_pnl"]["amount"] == 10.0 and items["MSFT"]["current_price"] == 305.0
    tx = type("T", (), {"open_price": 100.0, "quantity": 10.0, "side": OrderDirection.BUY})
    assert items["AAPL"]["open_pnl"] == TransactionHelper.calculate_pnl(tx, 110.0)
    assert "as_of" in body
    assert brokers[a1.id].position_reads == 1                    # one get_positions per account, not per row


def test_no_price_is_unmeasured_with_a_reason_never_zero(world):
    a1, a2, _ = world
    _open(a1, "NVDA")
    items, _ = _items()
    row = items["NVDA"]
    assert row["pnl_status"] == "unmeasured" and row["open_pnl"] is None and row["current_price"] is None
    assert "no price for NVDA" in row["open_pnl_reason"]


def test_one_unreachable_account_marks_only_its_rows(world):
    a1, a2, brokers = world
    _open(a1, "AAPL")
    _open(a2, "KO", price=50.0)
    brokers[a2.id].feed_error = ConnectionError("down")
    items, _ = _items()
    assert items["AAPL"]["pnl_status"] == "open_measured"
    assert items["KO"]["pnl_status"] == "unmeasured" and "broker_unreachable" in items["KO"]["open_pnl_reason"]


def test_waiting_and_closed_rows_are_not_priced(world):
    a1, _, brokers = world
    _open(a1, "AAPL", status=TransactionStatus.WAITING)
    create_transaction(symbol="IBM", status=TransactionStatus.CLOSED, open_price=100.0, close_price=110.0, quantity=1.0)
    items, body = _items()
    assert items["AAPL"]["pnl_status"] == "open" and items["AAPL"]["open_pnl"] is None
    assert items["IBM"]["pnl_status"] == "realized" and items["IBM"]["realized_pnl"] == 10.0
    assert "as_of" not in body and brokers[a1.id].position_reads == 0


def test_detail_carries_open_pnl_and_as_of(world):
    a1, _, _ = world
    tx = _open(a1, "AAPL")
    d = v1_client().get(f"/transactions/{tx.id}").json()
    assert d["open_pnl"]["amount"] == 100.0 and d["pnl_status"] == "open_measured" and "as_of" in d


def test_option_structures_use_the_options_tab_rule(world, monkeypatch):
    a1, _, _ = world
    tx = _open(a1, "AAPL", asset_class=AssetClass.OPTION)
    seen = {}

    def fake_rule(trans, orders, account, account_id, quotes, marks):
        seen.update(marks=marks, orders=len(orders))
        return 12.5
    monkeypatch.setattr(svc.FloatingPLCalculator, "_option_transaction_pl", staticmethod(fake_rule))
    d = v1_client().get(f"/transactions/{tx.id}").json()
    assert d["open_pnl"] == {"amount": 12.5} and d["pnl_status"] == "open_measured"
    assert seen == {"marks": {"AAPL": 110.0, "TSLA": 90.0}, "orders": 1}
    monkeypatch.setattr(svc.FloatingPLCalculator, "_option_transaction_pl", staticmethod(lambda *a: None))
    d = v1_client().get(f"/transactions/{tx.id}").json()
    assert d["pnl_status"] == "unmeasured" and d["open_pnl"] is None


def test_a_transaction_without_orders_has_no_known_account(world):
    create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, open_price=100.0)
    row = _items()[0]["AAPL"]
    assert row["pnl_status"] == "unmeasured" and "no order" in row["open_pnl_reason"]
