"""GET /api/backtests must be answered from ix_backtests_summary (db_migrate/031), never by a row
scan: with ~10 MB of blobs per row in the live DB the un-indexed list measured 13 s and ran on the
event loop. Captures the statement the route actually emits and EXPLAINs it on the gate engine.
"""
from __future__ import annotations

import pytest
from sqlalchemy import event


@pytest.fixture
def captured_sql(gate_engine):
    seen: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(gate_engine, "before_cursor_execute", _capture)
    try:
        yield seen
    finally:
        event.remove(gate_engine, "before_cursor_execute", _capture)


def test_list_backtests_query_is_served_by_the_covering_index(client, gate_engine, captured_sql):
    resp = client.get("/api/backtests")
    assert resp.status_code == 200, resp.text

    list_sql = [s for s in captured_sql if "FROM backtests b" in s]
    assert len(list_sql) == 1, captured_sql
    with gate_engine.connect() as c:
        cur = c.connection.dbapi_connection.cursor()
        cur.execute("EXPLAIN QUERY PLAN " + list_sql[0])
        plan = " | ".join(str(r) for r in cur.fetchall())
    assert "COVERING INDEX ix_backtests_summary" in plan, plan
    assert "TEMP B-TREE" not in plan, plan
