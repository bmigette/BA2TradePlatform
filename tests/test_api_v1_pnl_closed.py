"""GET /accounts/{id}/pnl/closed."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from ba2_trade_platform.services import stats
from tests.api_helpers import v1_client
from tests.golden_overview.seed import NOW, seed_world

pytestmark = pytest.mark.api_db
GOLDEN = json.loads((Path(__file__).parent / "golden_overview" / "stats_golden.json").read_text())


def _get(acct, **params):
    r = v1_client().get(f"/accounts/{acct}/pnl/closed", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _span(days_back, end=NOW):
    return {"from": (end - timedelta(days=days_back)).isoformat(), "to": (end - timedelta(microseconds=1)).isoformat()}


def test_total_equals_the_overview_trade_performance_figure():
    ids = seed_world()
    for name, acct in (("acct1", ids["acct1"]), ("acct2", ids["acct2"])):
        want = GOLDEN["trade_performance"][name]
        body = _get(acct, group_by="month", **_span(30))
        assert body["total"]["pnl"] == want["pnl_30d"], name
        assert body["total"]["wins"] == want["wins_30d"] and body["total"]["losses"] == want["losses_30d"]
        assert body["total"]["trades"] == want["closed_count"]


def test_group_by_expert_equals_the_profit_per_expert_chart_over_all_time():
    ids = seed_world()
    body = _get(ids["acct1"], group_by="expert", **_span(365))
    got = {r["group"]: r["pnl"] for r in body["items"]}
    assert got == dict(stats.profit_per_expert(ids["acct1"]))     # (the chart skips unmeasurable rows too)
    assert [r["pnl"] for r in body["items"]] == sorted((r["pnl"] for r in body["items"]), reverse=True)


def test_group_by_day_month_symbol_and_the_unmeasured_count():
    ids = seed_world()
    day = _get(ids["acct1"], group_by="day", **_span(30))
    assert [r["group"] for r in day["items"]] == sorted((r["group"] for r in day["items"]), reverse=True)
    by_key = {r["group"]: r for r in day["items"]}
    ibm_day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    assert by_key[ibm_day]["unmeasured"] == 1 and by_key[ibm_day]["pnl"] is None     # counted, never a 0
    assert day["total"]["unmeasured"] == 1
    sym = _get(ids["acct1"], group_by="symbol", **_span(30))
    assert {r["group"]: r["pnl"] for r in sym["items"]}["AAPL"] == 100.0
    month = _get(ids["acct1"], group_by="month", **_span(70))
    assert {r["group"] for r in month["items"]} == {"2026-11", "2026-10", "2026-09", "2026-08"}


def test_accounts_are_isolated_and_the_manual_account_has_no_expert_group():
    ids = seed_world()
    body = _get(ids["acct2"], group_by="expert", **_span(30))
    assert [r["group"] for r in body["items"]] == ["(no expert)"] and body["items"][0]["pnl"] == 10.0


def test_paging_of_groups_and_validation():
    ids = seed_world()
    page = _get(ids["acct1"], group_by="symbol", limit=2, offset=1, **_span(70))
    assert len(page["items"]) == 2 and page["groups"] >= 8 and page["offset"] == 1
    c = v1_client()
    assert c.get(f"/accounts/{ids['acct1']}/pnl/closed?group_by=hour").status_code == 422
    assert c.get(f"/accounts/{ids['acct1']}/pnl/closed?from=2026-10-01&to=2026-09-01").status_code == 422
    assert c.get(f"/accounts/{ids['acct1']}/pnl/closed?from=2000-01-01&to=2026-09-01").status_code == 422
    assert c.get("/accounts/999/pnl/closed").status_code == 404
