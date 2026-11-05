"""GET /risk-manager/runs and /risk-manager/runs/{kind}-{id}."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import RiskManagerRun, SmartRiskManagerJob
from tests.api_helpers import v1_client
from tests.factories import create_account_definition, create_expert_instance

pytestmark = pytest.mark.api_db


def _setup():
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    now = datetime.now(timezone.utc)
    add_instance(RiskManagerRun(expert_instance_id=e.id, account_id=acct.id, mode="classic",
                                run_date=now - timedelta(hours=2), symbols_received=3, symbols_funded=1,
                                decisions=[{"symbol": "AAPL", "outcome": "funded"}],
                                context={"available_balance": 1000.0}), expunge_after_flush=True)
    add_instance(SmartRiskManagerJob(expert_instance_id=e.id, account_id=acct.id, run_date=now,
                                     model_used="m", user_instructions="u", status="COMPLETED",
                                     graph_state={"actions_log": [1]}, actions_summary="did things"),
                 expunge_after_flush=True)
    return e


def test_list_merges_both_kinds_newest_first_without_big_json():
    e = _setup()
    body = v1_client().get(f"/risk-manager/runs?expert={e.id}").json()
    assert [i["kind"] for i in body["items"]] == ["smart", "rm"]
    assert body["total"] == 2
    assert all("graph_state" not in i and "decisions" not in i for i in body["items"])
    assert body["items"][0]["ref"].startswith("smart-") and body["items"][1]["ref"].startswith("rm-")
    assert v1_client().get("/risk-manager/runs?kind=rm").json()["total"] == 1


def test_detail_by_ref():
    _setup()
    c = v1_client()
    ref_rm = next(i["ref"] for i in c.get("/risk-manager/runs").json()["items"] if i["kind"] == "rm")
    d = c.get(f"/risk-manager/runs/{ref_rm}").json()
    assert d["decisions"] == [{"symbol": "AAPL", "outcome": "funded"}] and d["context"]["available_balance"] == 1000.0
    ref_s = next(i["ref"] for i in c.get("/risk-manager/runs").json()["items"] if i["kind"] == "smart")
    assert c.get(f"/risk-manager/runs/{ref_s}").json()["graph_state"] == {"actions_log": [1]}
    assert c.get("/risk-manager/runs/rm-99999").status_code == 404
    assert c.get("/risk-manager/runs/garbage").status_code == 422


def test_list_paginates_in_sql_and_never_selects_the_big_json_columns():
    from sqlalchemy import event
    import ba2_common.core.db as pkg_db
    acct = create_account_definition()
    e = create_expert_instance(account_id=acct.id)
    now = datetime.now(timezone.utc)
    for i in range(6):
        add_instance(RiskManagerRun(expert_instance_id=e.id, account_id=acct.id, mode="classic",
                                    run_date=now - timedelta(minutes=10 * i), symbols_received=i, symbols_funded=0,
                                    decisions=[{"x": "D" * 50}], context={"c": "C" * 50}), expunge_after_flush=True)
        add_instance(SmartRiskManagerJob(expert_instance_id=e.id, account_id=acct.id,
                                         run_date=now - timedelta(minutes=10 * i + 5), model_used="m",
                                         user_instructions="u", status="COMPLETED",
                                         graph_state={"g": "G" * 50}), expunge_after_flush=True)
    statements = []
    engine = pkg_db.get_engine()

    def spy(conn, cursor, statement, *a):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", spy)
    try:
        body = v1_client().get("/risk-manager/runs?limit=3&offset=4").json()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert body["total"] == 12 and len(body["items"]) == 3
    # globally ordered newest first: rm0, smart0, rm1, smart1, rm2, smart2 ... -> offset 4 = rm2, smart2, rm3
    assert [i["ref"].split("-")[0] for i in body["items"]] == ["rm", "smart", "rm"]
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT") and
               ("riskmanagerrun" in s.lower() or "smartriskmanagerjob" in s.lower()) and "count(" not in s.lower()]
    assert selects
    for s in selects:
        head = s.lower().split(" from ")[0]
        assert "decisions" not in head and "graph_state" not in head and "context" not in head, head
        assert "limit" in s.lower()
