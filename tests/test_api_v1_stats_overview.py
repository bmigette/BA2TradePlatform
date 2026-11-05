"""GET /stats/overview: the Overview page's figures, composed from the same functions the widgets call."""
import pytest

from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, TransactionStatus
from ba2_trade_platform.services import accounts as accounts_svc
from ba2_trade_platform.services import floating_pl as fpl_svc
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

pytestmark = pytest.mark.api_db


class Broker(FakeAccount):
    """FakeAccount plus the reads the Overview calculators make."""

    def __init__(self, account_id, positions):
        super().__init__(account_id)
        self.positions = positions
        self.bids = {}
        self.fail_positions = False
        self.position_reads = 0

    def get_tradable_balance(self):
        return 10000.0

    def get_balance(self):
        return 10000.0

    def get_buying_power(self):
        return 20000.0

    def get_broker_floating_pl(self):
        return None

    def get_positions(self):
        self.position_reads += 1
        return None if self.fail_positions else self.positions

    def get_instrument_current_price(self, symbols, price_type="mid"):
        return {s: self.bids[s] for s in symbols if s in self.bids}


@pytest.fixture
def world(monkeypatch):
    from ba2_trade_platform.services import _positions_cache
    _positions_cache.clear()
    a1 = create_account_definition(name="Expert acct")
    a2 = create_account_definition(name="Manual acct")
    e1 = create_expert_instance(account_id=a1.id, alias="Alpha", virtual_equity_pct=50.0)
    tx = create_transaction(symbol="AAPL", quantity=10.0, status=TransactionStatus.OPENED, open_price=100.0,
                            expert_id=e1.id)
    create_trading_order(a1.id, symbol="AAPL", transaction_id=tx.id, status=OrderStatus.FILLED,
                         side=OrderDirection.BUY, open_price=100.0, filled_qty=10.0)
    brokers = {
        a1.id: Broker(a1.id, [{"symbol": "AAPL", "current_price": 110.0, "market_value": "1100", "qty": 10.0,
                               "unrealized_pl": 100.0}]),
        a2.id: Broker(a2.id, [{"symbol": "SPY", "current_price": 500.0, "market_value": "2500", "qty": 5.0,
                               "unrealized_pl": 25.0}]),
    }
    brokers[a1.id].bids = {"AAPL": 109.0}
    factory = lambda account_id, session=None, use_cache=True: brokers[account_id]   # noqa: E731
    monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", factory)
    monkeypatch.setattr(fpl_svc, "get_account_instance_from_id", factory)
    monkeypatch.setattr("ba2_trade_platform.core.utils.get_account_instance_from_id", factory)
    return {"a1": a1, "a2": a2, "e1": e1, "brokers": brokers}


def test_overview_has_every_section_and_the_floating_pl(world):
    body = v1_client().get("/stats/overview").json()
    assert set(body) >= {"as_of", "trade_performance", "orders", "profit_per_expert", "balance_usage",
                         "floating_pl", "allocation", "exposure"}
    assert body["trade_performance"]["open_count"] == 1
    per_account = {r["name"]: r for r in body["floating_pl"]["per_account"]["rows"]}
    assert per_account["Expert acct"]["pl"] == pytest.approx(100.0)       # (110 - 100) * 10 from the broker price
    assert per_account["Manual acct"]["pl"] == pytest.approx(25.0)        # the broker's own per-position P/L
    assert body["floating_pl"]["per_account"]["total"] == pytest.approx(125.0)
    assert body["floating_pl"]["per_account"]["status"] == "ok"
    assert [r["name"] for r in body["floating_pl"]["per_expert"]["rows"]] == [f"Alpha-{world['e1'].id}"]


def test_allocation_and_exposure_and_balance_usage(world):
    body = v1_client().get(f"/stats/overview?account={world['a1'].id}").json()
    assert body["account"] == world["a1"].id
    assert [r["account"] for r in body["orders"]] == ["Expert acct"]
    assert body["allocation"]["by_label"][0]["name"] == "Uncategorized"
    assert body["allocation"]["by_label"][0]["value"] == pytest.approx(1100.0)
    assert body["exposure"]["accounts"][0]["gross_exposure"] == 7500.0
    assert "as_of" in body["exposure"]["accounts"][0]
    experts = body["balance_usage"]["experts"]
    assert experts[0]["total"] == pytest.approx(5000.0)           # 50% sleeve of the 10 000 tradable balance
    assert experts[0]["filled"] == pytest.approx(1090.0)          # 10 shares at the 109 bid
    assert body["balance_usage"]["status"] == "ok"


def test_unreadable_account_marks_its_row_unmeasured_and_the_total_partial(world):
    world["brokers"][world["a2"].id].fail_positions = True
    body = v1_client().get("/stats/overview").json()
    pl = body["floating_pl"]["per_account"]
    rows = {r["name"]: r for r in pl["rows"]}
    assert rows["Manual acct"]["pl"] is None and rows["Manual acct"]["pl_status"] == "unmeasured"
    assert pl["status"] == "partial" and pl["unmeasured"] == ["Manual acct"]
    assert pl["total"] == pytest.approx(100.0)                    # the readable account only, and it says so
    assert body["allocation"]["status"] == "partial" and body["allocation"]["unmeasured"] == ["Manual acct"]


def test_every_account_unreadable_is_an_unmeasurable_section_not_a_zero(world):
    for b in world["brokers"].values():
        b.fail_positions = True
    body = v1_client().get("/stats/overview").json()
    assert body["allocation"]["status"] == "unmeasurable" and body["allocation"]["error"] == "unmeasurable"
    assert body["floating_pl"]["per_account"]["status"] == "unmeasurable"
    assert body["floating_pl"]["per_account"]["total"] is None


def test_exposure_reports_a_snapshot_failure_per_account(world):
    world["brokers"][world["a2"].id].snapshot.equity = None
    body = v1_client().get("/stats/overview").json()
    rows = {r["account"]: r for r in body["exposure"]["accounts"]}
    assert rows["Manual acct"]["status"] == "unmeasurable" and rows["Expert acct"]["status"] == "ok"
    assert body["exposure"]["status"] == "partial"


def test_unknown_account_is_404_and_an_unpriced_position_is_named(world):
    assert v1_client().get("/stats/overview?account=999").status_code == 404
    world["brokers"][world["a1"].id].bids = {}
    body = v1_client().get(f"/stats/overview?account={world['a1'].id}").json()
    assert body["balance_usage"]["understated"] == [{"expert": f"Alpha-{world['e1'].id}", "symbols": ["AAPL"]}]


def test_a_get_neither_writes_orders_nor_touches_the_buying_power_failure_memory(world):
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    before = dict(MarketExpertInterface._BP_FAILED_READS)
    v1_client().get("/stats/overview")
    assert dict(MarketExpertInterface._BP_FAILED_READS) == before
    from ba2_trade_platform.core.db import get_all_instances
    assert len(get_all_instances(TradingOrder)) == 1


def test_one_positions_read_per_account_serves_every_section(world):
    v1_client().get("/stats/overview")
    assert [b.position_reads for b in world["brokers"].values()] == [1, 1]


def test_a_section_that_blows_up_is_isolated_with_the_class_name(world, monkeypatch):
    from ba2_trade_platform.services import balance_usage
    def boom(account_id):
        raise OSError("connect failed: secret=NOTFORTHEBODY")
    monkeypatch.setattr(balance_usage, "balance_usage_per_expert", boom)
    r = v1_client().get("/stats/overview")
    body = r.json()
    assert r.status_code == 200
    assert body["balance_usage"] == {"status": "unmeasurable", "error": "internal", "message": "OSError"}
    assert "NOTFORTHEBODY" not in r.text
    assert body["floating_pl"]["status"] == "ok" and body["exposure"]["status"] == "ok"


def test_allocation_skips_a_bad_position_and_says_so(world):
    world["brokers"][world["a1"].id].positions.append({"symbol": "BAD", "market_value": None, "current_price": 1.0})
    body = v1_client().get(f"/stats/overview?account={world['a1'].id}").json()
    alloc = body["allocation"]
    assert alloc["status"] == "partial" and alloc["skipped"]["count"] == 1
    assert alloc["skipped"]["positions"][0] == {"symbol": "BAD", "reason": "TypeError"}
    assert alloc["by_label"][0]["percent"] == pytest.approx(100.0) and "included" in alloc["percent_basis"]


def test_all_unmeasured_window_has_null_pnl_in_the_overview(world):
    from ba2_trade_platform.core.types import TransactionStatus as TS
    from datetime import datetime, timedelta
    create_transaction(symbol="IBM", status=TS.CLOSED, open_price=100.0, close_price=None, quantity=1.0,
                       close_date=datetime.now() - timedelta(days=1))
    perf = v1_client().get("/stats/overview").json()["trade_performance"]
    assert perf["pnl_7d"] is None and perf["unmeasured_7d"] == 1
