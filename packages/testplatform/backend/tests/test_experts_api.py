"""Tests for the expert catalog + settings-definitions endpoints (Task 3).

Uses fastapi.testclient.TestClient(app) like the rest of the repo. The catalog
sources its class->module map from
``app.services.backtest.daily_backtest_handler._SUPPORTED_EXPERTS`` and reads the
expert class attributes (no DB / full init).
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_list_experts():
    resp = client.get("/api/experts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "experts" in body
    experts = body["experts"]

    by_class = {e["class"]: e for e in experts}
    for expected in ("FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FactorRanker"):
        assert expected in by_class, f"{expected} missing from {list(by_class)}"

    assert by_class["FactorRanker"]["bypasses_classic_rm"] is True


def test_settings_definitions_known_expert():
    resp = client.get("/api/experts/FMPRating/settings-definitions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "definitions" in body
    defs = body["definitions"]
    assert "sizing_mode" in defs
    assert "risk_per_trade_pct" in defs
    assert defs["risk_per_trade_pct"]["type"] == "float"


def test_settings_definitions_unknown_expert_404():
    resp = client.get("/api/experts/NotAnExpert/settings-definitions")
    assert resp.status_code == 404, resp.text
