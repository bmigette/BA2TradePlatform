"""GET /accounts/{id}/growth, /growth/labels, /growth/labels/{label}/positions."""
import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.types import InstrumentType
from ba2_trade_platform.services import account_growth as svc
from ba2_trade_platform.services import accounts as accounts_svc
from ba2_trade_platform.services import growth
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import create_account_definition
from tests.golden_overview import growth_seed as seed

pytestmark = pytest.mark.api_db
WINDOW = "from=2026-09-01&to=2026-10-31"


class AttrDict(dict):
    """A broker position: reads as a dict (``dict(pos)``) and by attribute, like the real adapters' rows."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class GrowthBroker(FakeAccount):
    def __init__(self, account_id):
        super().__init__(account_id)
        self.positions = [AttrDict(symbol=p.symbol, qty=p.qty, cost_basis=p.cost_basis) for p in seed.positions()]
        self.balance = seed.balance_history()
        self.dividends = seed.dividends()
        self.trades = seed.trades()

    def get_balance_history(self, start_date=None, end_date=None):
        return [dict(r) for r in self.balance if r["account_id"] == 1]

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return [dict(d) for d in self.dividends]

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return [dict(t) for t in self.trades]


@pytest.fixture
def world(monkeypatch):
    acct = create_account_definition(name="Paper")
    seed.seed_instruments()
    broker = GrowthBroker(acct.id)
    monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda account_id: broker if account_id == acct.id else None)
    closes = seed.historical_prices()
    broker.closes = closes
    monkeypatch.setattr(svc, "fetch_closes", lambda symbols, start: {s: closes[s] for s in symbols if s in closes})
    svc.clear_cache()
    from ba2_trade_platform.services import portfolio
    portfolio._DIVIDENDS_CACHE.clear()

    def no_auto_add():
        raise AssertionError("a GET must not queue instruments for auto-add")
    monkeypatch.setattr("ba2_trade_platform.core.InstrumentAutoAdder.get_instrument_auto_adder", no_auto_add)
    return acct, broker


def test_total_growth_is_the_service_series_and_carries_datasets(world):
    acct, broker = world
    body = v1_client().get(f"/accounts/{acct.id}/growth?{WINDOW}").json()
    from datetime import date
    want = growth.total_growth([dict(r, account_id=acct.id) for r in broker.get_balance_history()],
                               [dict(d, account_id=acct.id) for d in broker.get_dividends()],
                               [dict(t, account_id=acct.id) for t in broker.get_filled_trades()], date(2026, 9, 1))
    assert body["series"]["dates"] == want["all_dates"]
    assert body["series"]["balance"] == want["aligned_balance"]
    assert body["series"]["pnl_pct"] == want["pnl_pct_data"]
    assert body["latest"]["balance"] == want["aligned_balance"][-1]
    assert body["datasets"]["balance_history"] == {"rows": len(broker.get_balance_history())}
    assert "as_of" in body and body["from"] == "2026-09-01"


def test_the_window_is_cut_at_to(world):
    acct, _ = world
    body = v1_client().get(f"/accounts/{acct.id}/growth?from=2026-09-01&to=2026-09-10").json()
    assert body["series"]["dates"][-1] <= "2026-09-10"


def test_no_data_at_all_is_unmeasurable_not_a_flat_series(world):
    acct, broker = world
    broker.balance, broker.dividends, broker.trades = [], [], []
    r = v1_client().get(f"/accounts/{acct.id}/growth?{WINDOW}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
    assert r.json()["detail"]["datasets"]["balance_history"] == {"rows": 0}


def test_window_validation_and_unknown_account(world):
    acct, _ = world
    c = v1_client()
    assert c.get(f"/accounts/{acct.id}/growth?from=2026-10-01&to=2026-09-01").status_code == 422
    assert c.get(f"/accounts/{acct.id}/growth?from=2000-01-01&to=2026-09-01").status_code == 422
    assert c.get("/accounts/999/growth").status_code == 404


def test_growth_by_label_matches_the_service_and_can_be_filtered(world):
    acct, broker = world
    c = v1_client()
    body = c.get(f"/accounts/{acct.id}/growth/labels?{WINDOW}").json()
    by = {x["name"]: x for x in body["labels"]}
    assert set(by) == {"Core", "Energy", "Growth", "Unlabeled"}
    assert by["Energy"]["value"][-1] == pytest.approx(20 * broker.closes["XLE"][body["dates"][-1]], abs=0.01)
    only = c.get(f"/accounts/{acct.id}/growth/labels?{WINDOW}&labels=Energy,Core").json()
    assert {x["name"] for x in only["labels"]} == {"Energy", "Core"} and only["dates"] == body["dates"]
    assert c.get(f"/accounts/{acct.id}/growth/labels?{WINDOW}&labels=Nope").status_code == 404


def test_too_many_labels_must_be_asked_for_by_name(world, monkeypatch):
    acct, _ = world
    monkeypatch.setattr(svc, "MAX_LABELS", 2)
    r = v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}")
    assert r.status_code == 422 and "known" in r.json()["detail"]


def test_growth_by_position_inside_a_label(world):
    acct, _ = world
    body = v1_client().get(f"/accounts/{acct.id}/growth/labels/Growth/positions?{WINDOW}").json()
    pos = {x["symbol"]: x for x in body["positions"]}
    assert set(pos) == {"AAPL", "MSFT"} and body["label"] == "Growth"
    assert len(pos["AAPL"]["value"]) == len(body["dates"])
    assert v1_client().get(f"/accounts/{acct.id}/growth/labels/Nope/positions?{WINDOW}").status_code == 404


def test_symbols_without_prices_are_named_and_all_unpriced_is_unmeasurable(world, monkeypatch):
    acct, broker = world
    closes = {k: v for k, v in broker.closes.items() if k != "XLE"}
    monkeypatch.setattr(svc, "fetch_closes", lambda symbols, start: {s: closes[s] for s in symbols if s in closes})
    body = v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}").json()
    assert body["unpriced_symbols"] == ["XLE"]
    monkeypatch.setattr(svc, "fetch_closes", lambda symbols, start: {})
    r = v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"


def test_a_position_without_cost_basis_is_unmeasurable(world):
    acct, broker = world
    broker.positions = [AttrDict(symbol="AAPL", qty=1.0)]
    r = v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}")
    assert r.status_code == 503 and r.json()["detail"]["skipped_positions"][0]["reason"] == "no cost_basis"


def test_failed_positions_fetch_is_unreachable(world):
    acct, broker = world
    broker.positions = None
    assert v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}").json()["error"] == "broker_unreachable"


def test_to_in_the_past_keeps_todays_quantity_walk_and_only_truncates_the_output(world):
    """The seed has trades AFTER 2026-09-30 (an AAPL sell and buy in October, MSFT/XLE/BND buys): the quantity timeline
    walks back from today's quantity through ALL of them, so September's value is the same whatever `to` is."""
    acct, _ = world
    c = v1_client()
    full = c.get(f"/accounts/{acct.id}/growth/labels?{WINDOW}").json()
    early = c.get(f"/accounts/{acct.id}/growth/labels?from=2026-09-01&to=2026-09-30").json()
    n = len(early["dates"])
    assert 0 < n < len(full["dates"]) and early["dates"][-1] <= "2026-09-30"
    f = {x["name"]: x for x in full["labels"]}
    e = {x["name"]: x for x in early["labels"]}
    for name in f:
        for series in ("value", "cumulative_dividends", "invested"):
            assert e[name][series] == f[name][series][:n], (name, series)
    pos_full = c.get(f"/accounts/{acct.id}/growth/labels/Growth/positions?{WINDOW}").json()
    pos_early = c.get(f"/accounts/{acct.id}/growth/labels/Growth/positions?from=2026-09-01&to=2026-09-30").json()
    assert pos_early["positions"][0]["value"] == pos_full["positions"][0]["value"][:len(pos_early["dates"])]


def test_bad_broker_rows_are_skipped_and_counted_never_read_as_zero(world):
    acct, broker = world
    broker.balance.append({"date": seed.balance_history()[0]["date"], "account_id": 1,
                           "net_liquidating_value": None, "cash_balance": 1.0})
    broker.trades.append({"date": seed.trades()[0]["date"], "symbol": "AAPL", "side": "BUY", "qty": None, "price": 1.0})
    broker.dividends.append({"date": seed.dividends()[0]["date"], "symbol": "AAPL", "amount": None})
    body = v1_client().get(f"/accounts/{acct.id}/growth?{WINDOW}").json()
    assert body["partial"] is True and body["skipped_rows"] == 3
    assert body["skipped"]["balance_history"] == {"no net_liquidating_value": 1}
    assert body["skipped"]["filled_trades"] == {"no qty or price": 1} and body["skipped"]["dividends"] == {"no amount": 1}
    assert min(body["series"]["balance"]) > 0                                # no fabricated 0 balance
    clean = v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}").json()
    assert clean["partial"] is True and clean["skipped_rows"] == 2           # (trades + dividends; no balance read)


def test_adapter_rows_are_not_mutated_and_fetches_are_cached_for_60s(world):
    acct, broker = world
    calls = {"n": 0}
    orig = broker.get_filled_trades

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    broker.get_filled_trades = counting
    c = v1_client()
    c.get(f"/accounts/{acct.id}/growth?{WINDOW}")
    c.get(f"/accounts/{acct.id}/growth/labels?{WINDOW}")
    assert calls["n"] == 1
    assert all("account_id" not in t or t["account_id"] in (1, 2) for t in broker.trades)
    assert broker.trades == seed.trades()


def test_a_position_without_cost_basis_is_skipped_and_listed_not_a_503(world):
    acct, broker = world
    broker.positions.append(AttrDict(symbol="ZZZ", qty=1.0))
    body = v1_client().get(f"/accounts/{acct.id}/growth/labels?{WINDOW}").json()
    assert body["partial"] is True and body["skipped_positions"] == [{"symbol": "ZZZ", "reason": "no cost_basis"}]


def test_prices_are_fetched_only_for_the_requested_labels_symbols(world, monkeypatch):
    acct, broker = world
    asked = []
    monkeypatch.setattr(svc, "fetch_closes", lambda symbols, start: asked.append(list(symbols)) or
                        {s: broker.closes[s] for s in symbols})
    v1_client().get(f"/accounts/{acct.id}/growth/labels/Energy/positions?{WINDOW}")
    assert asked == [["XLE"]]


def test_price_deadline_is_a_503_price_source_unreachable(monkeypatch):
    import time
    monkeypatch.setattr(svc, "PRICE_DEADLINE_S", 0.2)
    import types, sys
    fake = types.ModuleType("yfinance")
    fake.download = lambda *a, **k: time.sleep(1.0)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    from ba2_trade_platform.services.errors import PriceSourceUnreachable
    from datetime import date
    with pytest.raises(PriceSourceUnreachable):
        svc.fetch_closes(["AAPL"], date(2026, 9, 1))
    assert PriceSourceUnreachable.code == "price_source_unreachable" and PriceSourceUnreachable.status_code == 503


def test_a_failed_trades_fetch_is_a_503_and_not_an_empty_history(world):
    acct, broker = world

    def down(*a, **k):
        raise TimeoutError("slow")
    broker.get_filled_trades_strict = down
    r = v1_client().get(f"/accounts/{acct.id}/growth?{WINDOW}")
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable"
