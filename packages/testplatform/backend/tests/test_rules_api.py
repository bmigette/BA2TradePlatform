"""Tests for the rules import/export endpoints (Task 5).

Uses fastapi.testclient.TestClient(app) like the rest of the repo. DB rows are
seeded via app.models.database.SessionLocal after ensuring tables exist.
"""
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Base, SessionLocal, engine
from app.models import Strategy

client = TestClient(app)

# Ensure tables exist for direct DB seeding (mirrors other tests' pattern).
Base.metadata.create_all(bind=engine)


# v1.1 ruleset JSON (one rule: confidence > 0.7)
RULESET_JSON = {
    "export_version": "1.1", "export_type": "ruleset",
    "ruleset": {"name": "enter", "type": "trading_recommendation_rule",
                "subtype": "enter_market", "rules": [{
        "triggers": {
            "gate_0": {"event_type": "confidence", "operator": ">", "value": 0.7,
                       "enabled": True},
        },
        "actions": {"buy": {"action_type": "buy"}},
        "continue_processing": False, "order_index": 0}]}}


def test_import_rules_returns_or_tree():
    resp = client.post("/api/strategies/import-rules",
                       json={"json": RULESET_JSON, "which": "enter"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "tree" in body
    assert body["tree"]["operator"] == "OR"


def test_import_rules_unknown_event_type_422():
    bad = {"export_version": "1.1", "export_type": "ruleset", "ruleset": {"rules": [{
        "triggers": {"x": {"event_type": "totally_unknown", "value": 1}},
        "actions": {"buy": {"action_type": "buy"}}}]}}
    resp = client.post("/api/strategies/import-rules",
                       json={"json": bad, "which": "enter"})
    assert resp.status_code == 422, resp.text


def test_import_rules_bad_which_422():
    resp = client.post("/api/strategies/import-rules",
                       json={"json": RULESET_JSON, "which": "sideways"})
    assert resp.status_code == 422, resp.text


def test_export_rules_enter():
    # Seed a strategy with a simple buy_entry_conditions tree.
    tree = {
        "id": "root", "operator": "OR", "conditions": [
            {"id": "and0", "operator": "AND", "conditions": [
                {"id": "leaf0", "field": "confidence", "op": ">", "value": 0.7,
                 "enabled": True, "optimize": False},
            ]},
        ],
    }
    db = SessionLocal()
    try:
        s = Strategy(name="export-enter-test", buy_entry_conditions=tree,
                     exit_conditions=[])
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    resp = client.get(f"/api/strategies/{sid}/export-rules?which=enter")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["export_version"] == "1.1"
    assert body["ruleset"]["subtype"] == "enter_market"


def test_export_rules_exit_from_rule_list():
    # exit_conditions is a LIST of exit-rule dicts, each with a `conditions` sub-tree.
    exit_rules = [{
        "id": "ex0", "name": "take-profit",
        "conditions": {"id": "ec0", "operator": "AND", "conditions": [
            {"id": "el0", "field": "confidence", "op": "<", "value": 0.3,
             "enabled": True, "optimize": False},
        ]},
        "action": "close",
    }]
    db = SessionLocal()
    try:
        s = Strategy(name="export-exit-test", buy_entry_conditions=None,
                     exit_conditions=exit_rules)
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    resp = client.get(f"/api/strategies/{sid}/export-rules?which=exit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["export_version"] == "1.1"
    assert body["ruleset"]["subtype"] == "exit_market"
    assert len(body["ruleset"]["rules"]) >= 1


def test_export_rules_empty_returns_valid_ruleset():
    db = SessionLocal()
    try:
        s = Strategy(name="export-empty-test", buy_entry_conditions=None,
                     exit_conditions=[])
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    resp = client.get(f"/api/strategies/{sid}/export-rules?which=exit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["export_version"] == "1.1"
    assert body["ruleset"]["rules"] == []


def test_export_rules_not_found_404():
    resp = client.get("/api/strategies/99999999/export-rules?which=enter")
    assert resp.status_code == 404, resp.text


def test_export_rules_bad_which_422():
    db = SessionLocal()
    try:
        s = Strategy(name="export-badwhich-test", exit_conditions=[])
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    resp = client.get(f"/api/strategies/{sid}/export-rules?which=sideways")
    assert resp.status_code == 422, resp.text
