"""The MCP tools against the in-process /api/v1 app: reads, the dry-run/confirm model, idempotency, errors."""
import asyncio

import pytest

from tests.mcp_server_support import wire_client

from ba2_mcp import tools  # noqa: E402
from ba2_mcp.client import Ba2Client  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from ba2_trade_platform.core.db import get_instance  # noqa: E402
from ba2_trade_platform.core.models import ExpertInstance, Transaction  # noqa: E402
from ba2_trade_platform.core.types import OrderStatus, OrderType, TransactionStatus  # noqa: E402
from ba2_trade_platform.services import experts as experts_svc  # noqa: E402
from tests.api_helpers import FakeAccount, TOKEN  # noqa: E402
from tests.api_write_fakes import RecordingAccount, install_account  # noqa: E402
from tests.factories import (create_account_definition, create_expert_instance,  # noqa: E402
                             create_trading_order, create_transaction)

pytestmark = pytest.mark.api_db

SENT = "Nothing was sent. Call again with confirm=true to execute."


@pytest.fixture
def wired():
    yield wire_client()
    tools.set_client(None)


@pytest.fixture
def acct(monkeypatch):
    account = RecordingAccount()
    install_account(monkeypatch, account)
    return account


def _txn():
    a = create_account_definition()
    t = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, open_price=100.0, take_profit=120.0,
                           stop_loss=90.0, quantity=10.0)
    create_trading_order(a.id, symbol="AAPL", transaction_id=t.id, status=OrderStatus.FILLED)
    return t


def _spy(http):
    """Record (method, url, headers, params) of every request the TestClient receives."""
    seen = []
    orig = http.request

    def spy(method, url, **kw):
        seen.append((method, str(url), dict(kw["headers"]), kw["params"]))
        return orig(method, url, **kw)
    http.request = spy
    return seen


# --- reads -------------------------------------------------------------------------------------------------------
def test_reads_return_the_api_body_and_translate_names(wired):
    t = _txn()
    assert tools.get_platform_status()["status"] == "ok"
    body = tools.list_transactions(symbol="AAPL", include_open_pnl=False)
    assert body["total"] == 1 and body["items"][0]["id"] == t.id
    assert tools.list_transactions(account_id=999)["total"] == 0           # account_id -> ?account=
    assert tools.get_transaction(t.id, include_open_pnl=False)["symbol"] == "AAPL"
    assert tools.list_orders(transaction_id=t.id)["total"] == 1             # transaction_id -> ?transaction=


def test_account_reads(wired, monkeypatch):
    from ba2_trade_platform.services import accounts as accounts_svc
    monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda i: FakeAccount())
    d = create_account_definition(name="Paper")
    assert tools.list_accounts()["total"] == 1
    assert tools.get_account(d.id)["snapshot"]["equity"] == 10000.0
    assert tools.get_account_positions(d.id)["items"][0]["symbol"] == "AAPL"
    assert tools.get_broker_orders(d.id, status="open")["items"][0]["id"] == "b-1"


def test_api_error_becomes_tool_error_with_code(wired):
    with pytest.raises(ToolError, match=r"\[not_found\]"):
        tools.get_transaction(99999)
    with pytest.raises(ToolError, match=r"\[invalid\]"):
        tools.list_transactions(limit=0)


def test_write_without_a_token_is_a_clear_403(wired):
    _, http = wire_client(token=None)
    tools.set_client(Ba2Client("http://localhost", None, 60.0, http=http))
    t = _txn()
    with pytest.raises(ToolError, match="BA2_API_TOKEN|Management API"):
        tools.close_transaction(t.id, confirm=True)


# --- the dry-run / confirm model ----------------------------------------------------------------------------------
def test_unconfirmed_write_previews_and_sends_nothing(wired, acct):
    t = _txn()
    out = tools.set_take_profit_and_stop_loss(t.id, take_profit=130.0)
    assert out["dry_run"] is True and out["note"] == SENT and out["preview"]
    assert acct.calls == []
    assert get_instance(Transaction, t.id).take_profit == 120.0
    out = tools.close_transaction(t.id)
    assert out["dry_run"] and out["note"] == SENT and acct.calls == []


def test_confirmed_write_executes_with_a_fresh_idempotency_key_dry_runs_have_none(wired, acct):
    t = _txn()
    seen = _spy(wired[1])
    out = tools.set_take_profit_and_stop_loss(t.id, take_profit=130.0, confirm=True)
    assert out["executed"] is True and out["result"]["source"] == "api"
    assert acct.calls == [("adjust_tp", t.id, 130.0, "api")]
    key = seen[0][2]["idempotency-key"] if "idempotency-key" in seen[0][2] else seen[0][2]["Idempotency-Key"]
    assert len(key) == 36 and "dry_run" not in seen[0][3]
    tools.set_take_profit_and_stop_loss(t.id, take_profit=125.0, confirm=True)
    key2 = seen[1][2]["idempotency-key"] if "idempotency-key" in seen[1][2] else seen[1][2]["Idempotency-Key"]
    assert key2 != key
    tools.set_take_profit_and_stop_loss(t.id, take_profit=126.0)
    assert not {k.lower() for k in seen[2][2]} & {"idempotency-key"} and seen[2][3]["dry_run"] == "true"


def test_same_key_replays_the_first_result(wired, acct):
    t = _txn()
    client = wired[0]
    first = client.request("POST", f"/transactions/{t.id}/tp-sl", body={"take_profit": 130.0},
                           idempotency_key="same-key", write=True)
    again = client.request("POST", f"/transactions/{t.id}/tp-sl", body={"take_profit": 130.0},
                           idempotency_key="same-key", write=True)
    assert first == again and acct.calls == [("adjust_tp", t.id, 130.0, "api")]


def test_tp_sl_requires_a_price_and_lock_is_sent(wired, acct):
    t = _txn()
    with pytest.raises(ToolError, match="take_profit and/or stop_loss"):
        tools.set_take_profit_and_stop_loss(t.id)
    tools.set_take_profit_and_stop_loss(t.id, take_profit=130.0, stop_loss=80.0, lock=True, confirm=True)
    assert acct.calls == [("adjust_tp_sl", t.id, 130.0, 80.0, "manual")]


def test_unlock_cancel_close(wired, acct):
    t = _txn()
    assert tools.unlock_take_profit_and_stop_loss(t.id)["dry_run"] is True
    assert tools.unlock_take_profit_and_stop_loss(t.id, confirm=True)["executed"] is True
    a = create_account_definition()
    o = create_trading_order(a.id, order_type=OrderType.BUY_LIMIT, status=OrderStatus.OPEN, broker_order_id="b-1")
    assert tools.cancel_order(o.id)["preview"]["would_cancel"]["id"] == o.id and acct.calls == []
    assert tools.cancel_order(o.id, confirm=True)["executed"] and acct.calls == [("cancel_order", o.id)]
    assert tools.close_transaction(t.id, confirm=True)["executed"]
    assert ("close_transaction_async", t.id) in acct.calls


def test_api_refusal_on_a_confirmed_write_is_a_tool_error(wired, acct):
    done = create_transaction(symbol="MSFT", status=TransactionStatus.CLOSED)
    with pytest.raises(ToolError, match=r"\[conflict\]"):
        tools.close_transaction(done.id, confirm=True)


@pytest.fixture
def fake_expert(monkeypatch):
    class _E:
        settings = {"min_confidence": 70}
        saved = []

        def __init__(self, i=1):
            self.id = i

        @classmethod
        def get_merged_settings_definitions(cls):
            return {"min_confidence": {"type": "int"}}

        def save_setting(self, key, value, setting_type=None):
            _E.saved.append((key, value))
    monkeypatch.setattr(experts_svc, "_live_expert", lambda i: _E(i))
    monkeypatch.setattr(experts_svc, "refresh_schedules", lambda i: None)
    monkeypatch.setattr(experts_svc, "_drop_caches", lambda i: None)
    return _E


def test_expert_writes(wired, fake_expert):
    e = create_expert_instance(account_id=create_account_definition().id, enabled=True, virtual_equity_pct=100.0)
    assert tools.disable_expert(e.id)["dry_run"] and get_instance(ExpertInstance, e.id).enabled is True
    tools.disable_expert(e.id, confirm=True)
    assert get_instance(ExpertInstance, e.id).enabled is False
    tools.enable_expert(e.id, confirm=True)
    assert get_instance(ExpertInstance, e.id).enabled is True
    tools.set_expert_virtual_equity_pct(e.id, 40, confirm=True)
    assert get_instance(ExpertInstance, e.id).virtual_equity_pct == 40.0
    with pytest.raises(ToolError, match=r"\[invalid\]"):
        tools.set_expert_virtual_equity_pct(e.id, -5, confirm=True)
    assert tools.update_expert_settings(e.id, {"min_confidence": 80})["dry_run"] is True
    assert fake_expert.saved == []
    tools.update_expert_settings(e.id, {"min_confidence": 80}, confirm=True)
    assert fake_expert.saved == [("min_confidence", 80)]
    assert tools.list_experts(enabled=True)["total"] == 1       # get_expert needs a live expert (covered by the API tests)


# --- endpoints with no server-side dry run must never be sent when unconfirmed ------------------------------------------
def test_local_preview_tools_send_nothing_when_unconfirmed(wired):
    seen = _spy(wired[1])
    for out in (tools.reload_platform_caches(), tools.process_recommendations(5)):
        assert out["dry_run"] is True and out["endpoint_has_dry_run"] is False and out["note"] == SENT
    assert seen == []
    assert {n for n, s in tools.REGISTRY.items() if s.write and not s.server_dry_run} == \
        {"reload_platform_caches", "process_recommendations"}


def test_reload_confirmed_executes(wired):
    out = tools.reload_platform_caches(confirm=True)
    assert out["executed"] is True and out["result"]["status"] == "ok"


# --- through the real MCP layer ---------------------------------------------------------------------------------------
def test_call_through_fastmcp_and_schema(wired, acct):
    t = _txn()
    res = asyncio.run(tools.mcp.call_tool("close_transaction", {"transaction_id": t.id}))
    assert "Nothing was sent" in str(res) and acct.calls == []
    listed = {x.name: x for x in asyncio.run(tools.mcp.list_tools())}
    schema = listed["close_transaction"].inputSchema
    assert schema["properties"]["confirm"]["default"] is False and schema["required"] == ["transaction_id"]
    assert "WRITE" in listed["close_transaction"].description


def test_token_not_in_any_tool_description_or_result(wired):
    for t in asyncio.run(tools.mcp.list_tools()):
        assert TOKEN not in (t.description or "")
    assert TOKEN not in str(tools.get_platform_status())
