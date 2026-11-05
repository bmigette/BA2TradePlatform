"""GET /accounts/{id}/dividends."""
from datetime import date, datetime, timedelta

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import PortfolioIncomeEvent
from ba2_trade_platform.services import accounts as accounts_svc
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import create_account_definition

pytestmark = pytest.mark.api_db
TODAY = date.today()


def _div(days_ago, symbol, amount, **extra):
    row = {"date": datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()), "symbol": symbol,
           "amount": amount, "gross_amount": amount + 0.1, "tax_withheld": 0.1, "drip_quantity": None,
           "drip_price": None}
    row.update(extra)
    return row


class DivBroker(FakeAccount):
    def __init__(self, account_id):
        super().__init__(account_id)
        self.rows = [_div(3, "KO", 4.0), _div(10, "KO", 4.0, drip_quantity=0.07, drip_price=57.0),
                     _div(20, "PEP", 6.5), _div(400, "KO", 9.0)]
        self.calls = []

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        self.calls.append((symbol, start_date, end_date))
        return [dict(r) for r in self.rows if symbol is None or r["symbol"] == symbol]


@pytest.fixture
def world(monkeypatch):
    from ba2_trade_platform.services import portfolio
    portfolio._DIVIDENDS_CACHE.clear()
    acct = create_account_definition(name="Paper")
    broker = DivBroker(acct.id)
    monkeypatch.setattr(accounts_svc, "get_account_instance_from_id",
                        lambda account_id: broker if account_id == acct.id else None)
    return acct, broker


def _get(acct, **params):
    r = v1_client().get(f"/accounts/{acct.id}/dividends", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_broker_rows_totals_cash_vs_drip_and_cash_by_symbol(world):
    acct, _ = world
    body = _get(acct)                 # default window: the last 365 days (the 400-day-old row is out)
    b = body["broker"]
    assert b["total"] == 3 and "fetch_ok" not in b
    assert [i["symbol"] for i in b["items"]] == ["KO", "KO", "PEP"]            # newest first
    assert [i["kind"] for i in b["items"]] == ["cash", "drip", "cash"]
    t = b["totals"]
    assert t["net"] == pytest.approx(14.5) and t["cash_net"] == pytest.approx(10.5) and t["drip_net"] == pytest.approx(4.0)
    assert t["tax_withheld"] == pytest.approx(0.3) and t["gross"] == pytest.approx(14.8)
    assert {x["symbol"]: x["cash"] for x in b["cash_by_symbol"]} == {"KO": pytest.approx(4.0), "PEP": pytest.approx(6.5)}
    assert "as_of" in body


def test_symbol_filter_window_and_paging(world):
    acct, broker = world
    ko = _get(acct, symbol="ko")
    assert ko["symbol"] == "KO" and ko["broker"]["total"] == 2
    wide = _get(acct, **{"from": (TODAY - timedelta(days=500)).isoformat()})
    assert wide["broker"]["total"] == 4
    page = _get(acct, limit=1, offset=1)
    assert len(page["broker"]["items"]) == 1 and page["broker"]["total"] == 3 and page["broker"]["totals"]["payments"] == 3


def test_a_failed_broker_read_is_a_503_not_an_empty_list(world):
    acct, broker = world

    def down(*a, **k):
        raise ConnectionError("down")
    broker.get_dividends_strict = down
    r = v1_client().get(f"/accounts/{acct.id}/dividends")
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable"
    broker.rows = []
    broker.get_dividends_strict = lambda *a, **k: []
    assert _get(acct)["broker"]["total"] == 0                       # a strict [] really is "no dividends"


def test_an_adapter_without_the_strict_read_is_unmeasurable_with_a_clear_message(world, monkeypatch):
    acct, broker = world
    monkeypatch.setattr(type(broker), "get_dividends_strict", None)
    r = v1_client().get(f"/accounts/{acct.id}/dividends")
    assert r.status_code == 503 and "get_dividends_strict" in r.json()["message"]


def test_rows_without_gross_or_tax_are_counted_not_zeroed(world):
    acct, broker = world
    broker.rows = [{"date": datetime.combine(TODAY - timedelta(days=2), datetime.min.time()), "symbol": "KO",
                    "amount": 4.0, "drip_quantity": None}]
    t = _get(acct)["broker"]["totals"]
    assert t["rows_without_gross"] == 1 and t["rows_without_tax"] == 1 and t["net"] == 4.0


def test_ledger_events_and_open_income_total(world):
    acct, _ = world
    add_instance(PortfolioIncomeEvent(account_id=acct.id, external_id="a", event_date=TODAY - timedelta(days=2),
                                      event_type="DIVIDEND", symbol="KO", amount=10.0, consumed_amount=4.0),
                 expunge_after_flush=True)
    add_instance(PortfolioIncomeEvent(account_id=acct.id, external_id="b", event_date=TODAY - timedelta(days=5),
                                      event_type="DEPOSIT", symbol=None, amount=100.0), expunge_after_flush=True)
    body = _get(acct)
    assert body["ledger"]["open_income_total"] == pytest.approx(106.0)
    assert [(e["external_id"], e["open_amount"]) for e in body["ledger"]["events"]] == [("a", 6.0), ("b", 100.0)]
    only_ko = _get(acct, symbol="KO")
    assert [e["external_id"] for e in only_ko["ledger"]["events"]] == ["a"]


def test_validation_and_unknown_account(world):
    acct, _ = world
    c = v1_client()
    assert c.get(f"/accounts/{acct.id}/dividends?from=2026-10-01&to=2026-09-01").status_code == 422
    assert c.get(f"/accounts/{acct.id}/dividends?from=2000-01-01").status_code == 422
    assert c.get("/accounts/999/dividends").status_code == 404


def test_a_payment_without_an_amount_is_skipped_and_flagged_partial(world):
    acct, broker = world
    broker.rows.append(dict(_div(5, "KO", 0.0), amount=None))
    b = _get(acct)["broker"]
    assert b["partial"] is True and b["skipped_rows"] == 1 and b["total"] == 3


def test_one_cached_fetch_serves_repeat_calls_and_the_rows_are_not_mutated(world):
    acct, broker = world
    _get(acct)
    _get(acct, symbol="KO")
    assert len(broker.calls) == 1 and _get(acct)["broker"]["from_cache"] is True
    assert "account_id" not in broker.rows[0]


def test_a_portfolio_call_fills_the_shared_cache_with_the_strict_read(world, monkeypatch):
    """The portfolio endpoint and /dividends share ``portfolio.cached_dividends``: whoever fills it must use the strict
    read, or /dividends would serve a lenient entry (first page, tax-blind) as if it were strict."""
    from ba2_trade_platform.core.portfolio_allocation_store import (
        set_allocation_config, set_managed_label, set_symbol_weight)
    from ba2_trade_platform.core.utils import add_label_to_instruments
    acct, broker = world
    lenient, strict = [_div(3, "KO", 99.0)], [_div(3, "KO", 4.0)]
    broker.get_dividends = lambda *a, **k: lenient
    broker.get_dividends_strict = lambda *a, **k: strict
    broker.positions = [{"symbol": "KO", "qty": 10.0, "cost_basis": 500.0, "market_value": 600.0, "side": "BUY",
                         "current_price": 60.0, "unrealized_pl": 100.0}]
    set_allocation_config(acct.id, valuation_mode="market")
    set_managed_label(acct.id, "CORE", target_pct=100.0, comment="x")
    add_label_to_instruments(["KO"], "CORE")
    set_symbol_weight(acct.id, "CORE", "KO", weight_pct=100.0, comment="x")
    assert v1_client().get(f"/accounts/{acct.id}/portfolio").status_code == 200
    body = _get(acct)
    assert [i["amount"] for i in body["broker"]["items"]] == [4.0] and body["broker"]["from_cache"] is True


def test_cached_dividends_hands_out_copies_on_a_miss_as_well_as_on_a_hit():
    from ba2_trade_platform.services import portfolio
    portfolio._DIVIDENDS_CACHE.clear()
    source = [{"symbol": "KO", "amount": 1.0}]
    first, _, hit1 = portfolio.cached_dividends(7, lambda: source)
    first[0]["amount"] = 999.0
    second, _, hit2 = portfolio.cached_dividends(7, lambda: source)
    assert (hit1, hit2) == (False, True) and second[0]["amount"] == 1.0 and source[0]["amount"] == 1.0
