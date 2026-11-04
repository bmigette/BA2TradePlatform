"""The /api/v1 write routes end to end (TestClient + a call-recording fake account)."""
import pytest

from ba2_trade_platform.api import audit
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import ExpertInstance, Transaction
from ba2_trade_platform.core.types import OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.services import experts as experts_svc
from tests.api_helpers import SK, TOKEN, set_api_token, v1_client
from tests.api_write_fakes import RecordingAccount, install_account
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

pytestmark = pytest.mark.api_db

#: Every non-GET route of /api/v1 and the test in THIS module (or, for the pre-existing maintenance posts, in the
#: named module) that exercises it. tests/test_api_v1_guard.py fails when a write route is missing from here.
COVERED = {
    ("POST", "/transactions/{transaction_id}/tp-sl"): "test_tp_sl_changes_one_side_with_source_api",
    ("POST", "/transactions/{transaction_id}/tp-sl/unlock"): "test_unlock_route",
    ("POST", "/transactions/{transaction_id}/close"): "test_close_route",
    ("POST", "/orders/{order_id}/cancel"): "test_cancel_route",
    ("PATCH", "/experts/{expert_id}"): "test_patch_expert_route",
    ("PATCH", "/experts/{expert_id}/settings"): "test_patch_settings_route_redacts_the_audit_trail",
    ("POST", "/experts/{expert_id}/run"): "test_run_route",
    ("POST", "/reload"): "tests.test_api_routes_reload",
    ("POST", "/run-schedule"): "tests.test_api_routes_run_schedule",
    ("POST", "/process-recommendations"): "tests.test_api_routes_process_recommendations",
}


@pytest.fixture
def acct(monkeypatch):
    set_api_token()
    account = RecordingAccount()
    install_account(monkeypatch, account)
    return account


@pytest.fixture
def audits(monkeypatch):
    captured = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: captured.append(k["data"]))
    return captured


@pytest.fixture
def client():
    set_api_token()
    return v1_client(token=TOKEN)


def _txn(**kw):
    a = create_account_definition()
    t = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, open_price=100.0, take_profit=120.0,
                           stop_loss=90.0, quantity=10.0, **kw)
    create_trading_order(a.id, symbol="AAPL", transaction_id=t.id, status=OrderStatus.FILLED)
    return t


def test_covered_registry_names_real_tests():
    import sys
    for (_, _), where in COVERED.items():
        if where.startswith("tests."):
            __import__(where)
            assert where in sys.modules
        else:
            assert where in globals(), where


# --- tp-sl -------------------------------------------------------------------------------------------------------

def test_tp_sl_changes_one_side_with_source_api(acct, client, audits):
    t = _txn()
    r = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0})
    assert r.status_code == 200, r.text
    assert acct.calls == [("adjust_tp", t.id, 130.0, "api")]
    body = r.json()
    assert body["changed"] and body["action"] == "adjust_tp" and body["source"] == "api" and body["lock"] is False
    assert audits and audits[0]["path"] == f"/transactions/{t.id}/tp-sl" and audits[0]["status"] == 200
    assert audits[0]["token_tail"] == TOKEN[-4:] and '"take_profit": 130.0' in audits[0]["arguments"]


def test_tp_sl_lock_true_is_the_dialogs_manual_call(acct, client):
    t = _txn()
    r = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0, "stop_loss": 80.0, "lock": True})
    assert r.status_code == 200 and acct.calls == [("adjust_tp_sl", t.id, 130.0, 80.0, "manual")]


@pytest.mark.parametrize("body,code,error", [
    ({}, 422, "invalid"), ({"take_profit": None}, 422, "invalid"), ({"take_profit": 95.0}, 422, "invalid"),
    ({"stop_loss": 105.0}, 422, "invalid"), ({"take_profit": "abc"}, 422, "invalid"),
    ({"take_profit": 130.0, "extra": 1}, 422, "invalid"), ({"take_profit": 130.0, "lock": "yes"}, 422, "invalid"),
    ({"take_profit": -1}, 422, "invalid"),
])
def test_tp_sl_rejections_make_no_broker_call(acct, client, body, code, error):
    t = _txn()
    r = client.post(f"/transactions/{t.id}/tp-sl", json=body)
    assert r.status_code == code and r.json()["error"] == error
    assert acct.calls == []


def test_tp_sl_conflict_not_found_and_broker_codes(acct, client):
    assert client.post("/transactions/9999/tp-sl", json={"take_profit": 130.0}).status_code == 404
    locked = _txn(tp_manual_override=True)
    r = client.post(f"/transactions/{locked.id}/tp-sl", json={"take_profit": 130.0})
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    t = _txn()
    acct.returns["adjust_tp"] = False
    r = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0})
    assert r.status_code == 502 and r.json()["error"] == "broker_refused"
    acct.returns.clear()
    acct.raises["adjust_tp"] = ConnectionError("secret-host")
    r = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 135.0})
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable" and "secret-host" not in r.text


def test_tp_sl_dry_run_and_idempotency(acct, client):
    t = _txn()
    r = client.post(f"/transactions/{t.id}/tp-sl?dry_run=true", json={"take_profit": 130.0})
    assert r.status_code == 200 and r.json()["dry_run"] is True and acct.calls == []
    h = {"Idempotency-Key": "abc-1"}
    first = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0}, headers=h)
    again = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0}, headers=h)
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert again.headers["idempotent-replay"] == "true"
    assert len(acct.calls) == 1


def test_a_running_pass_makes_the_write_a_409_without_a_broker_call(acct, client, monkeypatch):
    from ba2_trade_platform.core.TradeManager import TradeManager
    from ba2_trade_platform.services import _locks
    tm = TradeManager()
    monkeypatch.setattr(_locks, "get_trade_manager", lambda: tm)
    real = tm.hold_expert_locks
    monkeypatch.setattr(tm, "hold_expert_locks", lambda eid, timeout: real(eid, 0.2))
    a = create_account_definition()
    expert = create_expert_instance(account_id=a.id)
    t = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, open_price=100.0, take_profit=120.0,
                           stop_loss=90.0, expert_id=expert.id)
    create_trading_order(a.id, transaction_id=t.id, status=OrderStatus.FILLED)
    lock = tm._processing_lock(expert.id, "enter_market")
    lock.acquire()
    try:
        r = client.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0})
        assert r.status_code == 409 and "being processed" in r.json()["message"]
        assert client.post(f"/transactions/{t.id}/close").status_code == 409
        assert acct.calls == []
    finally:
        lock.release()


def test_unlock_route(acct, client):
    t = _txn(tp_manual_override=True, sl_manual_override=True)
    r = client.post(f"/transactions/{t.id}/tp-sl/unlock?dry_run=true")
    assert r.json()["cleared"] == ["TP", "SL"] and get_instance(Transaction, t.id).tp_manual_override
    r = client.post(f"/transactions/{t.id}/tp-sl/unlock")
    assert r.status_code == 200 and r.json()["cleared"] == ["TP", "SL"]
    assert not get_instance(Transaction, t.id).tp_manual_override
    assert client.post("/transactions/9999/tp-sl/unlock").status_code == 404


# --- close / cancel ----------------------------------------------------------------------------------------------

def test_close_route(acct, client):
    t = _txn()
    dry = client.post(f"/transactions/{t.id}/close?dry_run=true")
    assert dry.status_code == 200 and dry.json()["would_submit_close"]["side"] == "SELL" and acct.calls == []
    r = client.post(f"/transactions/{t.id}/close")
    assert r.status_code == 200 and r.json()["close_order_id"] == 99
    assert acct.calls == [("close_transaction_async", t.id)]
    closing = create_transaction(symbol="MSFT", status=TransactionStatus.CLOSING)
    r = client.post(f"/transactions/{closing.id}/close")
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    acct.close_result = {"success": False, "message": "Close refused (40310000)"}
    r = client.post(f"/transactions/{_txn().id}/close")
    assert r.status_code == 502 and "40310000" in r.json()["message"]


def test_cancel_route(acct, client):
    a = create_account_definition()
    o = create_trading_order(a.id, order_type=OrderType.BUY_LIMIT, status=OrderStatus.OPEN, broker_order_id="b-1")
    assert client.post(f"/orders/{o.id}/cancel?dry_run=true").json()["would_cancel"]["id"] == o.id
    assert acct.calls == []
    r = client.post(f"/orders/{o.id}/cancel")
    assert r.status_code == 200 and acct.calls == [("cancel_order", o.id)]
    done = create_trading_order(a.id, status=OrderStatus.CANCELED, broker_order_id="b-2")
    assert client.post(f"/orders/{done.id}/cancel").status_code == 409
    assert client.post("/orders/9999/cancel").status_code == 404


# --- experts -----------------------------------------------------------------------------------------------------

class _FakeExpert:
    saved = []

    def __init__(self, expert_id=1):
        self.id = expert_id
        self.settings = {"min_confidence": 70}

    @classmethod
    def get_merged_settings_definitions(cls):
        return {"min_confidence": {"type": "int"}, "openai_api_key": {"type": "str", "secret": True},
                "harmless_name": {"type": "str", "sensitive": True}}

    def save_setting(self, key, value, setting_type=None):
        _FakeExpert.saved.append((key, value))


@pytest.fixture
def fake_expert(monkeypatch):
    _FakeExpert.saved = []
    monkeypatch.setattr(experts_svc, "_live_expert", lambda expert_id: _FakeExpert(expert_id))
    monkeypatch.setattr(experts_svc, "refresh_schedules", lambda expert_id: None)
    monkeypatch.setattr(experts_svc, "_drop_caches", lambda expert_id: None)


def test_patch_expert_route(fake_expert, client):
    e = create_expert_instance(account_id=create_account_definition().id, enabled=True, virtual_equity_pct=100.0)
    r = client.patch(f"/experts/{e.id}", json={"enabled": False, "virtual_equity_pct": 40})
    assert r.status_code == 200 and r.json()["changed"] == ["enabled", "virtual_equity_pct"]
    row = get_instance(ExpertInstance, e.id)
    assert (row.enabled, row.virtual_equity_pct) == (False, 40.0)
    for bad in ({}, {"enabled": None}, {"enabled": "false"}, {"virtual_equity_pct": 0}, {"other": 1}):
        assert client.patch(f"/experts/{e.id}", json=bad).status_code == 422, bad
    assert client.patch("/experts/9999", json={"enabled": True}).status_code == 404
    assert client.patch(f"/experts/{e.id}?dry_run=1", json={"enabled": True}).json()["dry_run"] is True
    assert get_instance(ExpertInstance, e.id).enabled is False


def test_patch_settings_route_redacts_the_audit_trail(fake_expert, client, audits):
    e = create_expert_instance(account_id=create_account_definition().id)
    r = client.patch(f"/experts/{e.id}/settings",
                     json={"min_confidence": 80, "openai_api_key": SK + "NEWKEY987654", "harmless_name": "hush-value"})
    assert r.status_code == 200, r.text
    assert dict(_FakeExpert.saved)["openai_api_key"] == SK + "NEWKEY987654"          # really saved
    assert "NEWKEY987654" not in r.text and "hush-value" not in r.text
    assert r.json()["current"]["openai_api_key"] == {"set": True}
    args = audits[-1]["arguments"]
    assert "NEWKEY987654" not in args and "hush-value" not in args and "80" in args
    bad = client.patch(f"/experts/{e.id}/settings", json={"nope": SK + "LEAKCHECK123456"})
    assert bad.status_code == 422 and "LEAKCHECK123456" not in bad.text and "LEAKCHECK123456" not in audits[-1]["arguments"]
    assert client.patch(f"/experts/{e.id}/settings", json={}).status_code == 422
    assert client.patch(f"/experts/{e.id}/settings", json=[1]).status_code == 422


def test_run_route(client, monkeypatch):
    from types import SimpleNamespace
    import ba2_trade_platform.core.JobManager as jm_module
    from ba2_trade_platform.core.types import AnalysisUseCase

    submitted = []

    class _JM:
        _scheduled_jobs = {"j": SimpleNamespace(args=[5, "AAPL", AnalysisUseCase.ENTER_MARKET])}

        def submit_market_analysis(self, *a, **k):
            submitted.append((a, k))
            return "task-1"
    monkeypatch.setattr(jm_module, "get_job_manager", lambda: _JM())
    e = create_expert_instance(account_id=create_account_definition().id)
    assert e.id == 1
    _JM._scheduled_jobs = {"j": SimpleNamespace(args=[e.id, "AAPL", AnalysisUseCase.ENTER_MARKET])}
    dry = client.post(f"/experts/{e.id}/run?dry_run=true", json={"subtype": "enter_market"})
    assert dry.status_code == 200 and dry.json()["status"] == "dry_run" and submitted == []
    r = client.post(f"/experts/{e.id}/run", json={"subtype": "enter_market"})
    assert r.status_code == 200 and r.json()["task_id"] == "task-1" and len(submitted) == 1
    assert client.post(f"/experts/{e.id}/run", json={"subtype": "open_positions"}).status_code == 404
    assert client.post(f"/experts/{e.id}/run", json={"subtype": "bogus"}).status_code == 400
    assert client.post(f"/experts/{e.id}/run", json={}).status_code == 422


# --- auth on the write routes ------------------------------------------------------------------------------------

def test_writes_need_the_token_even_from_localhost(acct):
    t = _txn()
    c = v1_client()                                          # no Authorization header
    assert c.post(f"/transactions/{t.id}/tp-sl", json={"take_profit": 130.0}).status_code == 401
    assert c.post(f"/transactions/{t.id}/close").status_code == 401
    assert v1_client(token="x" * 44).post(f"/transactions/{t.id}/close").status_code == 401
    assert acct.calls == []
