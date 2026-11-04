"""GET /accounts, /accounts/{id}, /accounts/{id}/positions, /accounts/{id}/orders."""
import pytest

from ba2_trade_platform.services import accounts as svc
from tests.api_helpers import FakeAccount, v1_client
from tests.factories import create_account_definition

pytestmark = pytest.mark.api_db


@pytest.fixture
def fake(monkeypatch):
    acct = FakeAccount()
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda account_id: acct)
    return acct


def test_list_and_detail(fake):
    d = create_account_definition(name="Paper")
    c = v1_client()
    body = c.get("/accounts").json()
    assert body["total"] == 1 and body["items"][0]["snapshot"]["equity"] == 10000.0
    detail = c.get(f"/accounts/{d.id}").json()
    assert detail["snapshot"]["headroom"] == 7000.0
    assert c.get("/accounts/999").json()["error"] == "not_found"


def test_unmeasurable_detail_is_503_envelope(fake):
    fake.snapshot.equity = None
    d = create_account_definition()
    r = v1_client().get(f"/accounts/{d.id}")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"


def test_positions_and_orders_carry_as_of(fake):
    d = create_account_definition()
    c = v1_client()
    pos = c.get(f"/accounts/{d.id}/positions").json()
    assert pos["items"] == [{"symbol": "AAPL", "qty": 10.0}] and "as_of" in pos
    ords = c.get(f"/accounts/{d.id}/orders?status=open").json()
    assert ords["items"][0]["id"] == "b-1" and "as_of" in ords


def test_positions_none_means_broker_unreachable_not_flat(fake):
    fake.positions = None          # the get_positions() tri-state: None = the FETCH failed, [] = flat
    d = create_account_definition()
    r = v1_client().get(f"/accounts/{d.id}/positions")
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable"
    fake.positions = []
    assert v1_client().get(f"/accounts/{d.id}/positions").json()["items"] == []


def test_orders_route_validates_status_and_reports_fetch_ok(fake):
    d = create_account_definition()
    c = v1_client()
    assert c.get(f"/accounts/{d.id}/orders?status=bogus").status_code == 422
    body = c.get(f"/accounts/{d.id}/orders").json()
    assert body["fetch_ok"] is True and body["best_effort"] is True
    fake.orders = []
    body = c.get(f"/accounts/{d.id}/orders").json()
    assert body["items"] == [] and body["fetch_ok"] is False
