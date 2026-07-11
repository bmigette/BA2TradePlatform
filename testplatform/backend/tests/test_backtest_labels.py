"""Backtest.labels: free-form tags (e.g. ["goal5", "S4"]) settable independently, one per
grid/batch id and one per strategy. Covers the model round-trip and the
GET /api/backtests?label=... filter (SQLite json_each containment match).

Self-contained: a throwaway sqlite DATABASE_URL is set before any app import
(mirrors tests/test_fitness_catalog.py).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_backtest_labels.db")
if os.path.exists(TEST_DB_PATH):
    os.remove(TEST_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

import pytest


@pytest.fixture(scope="module")
def test_db():
    from app.models.database import engine, Base, SessionLocal

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    yield db
    db.close()
    engine.dispose()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(scope="module")
def client(test_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def _mk(db, name, labels=None):
    from app.models.backtest import Backtest

    bt = Backtest(
        name=name, engine_type="daily_expert", expert_name="FMPRating",
        labels=labels, start_date=datetime(2023, 1, 1), end_date=datetime(2026, 1, 1),
        initial_capital=10000.0, status="completed", total_trades=10, win_rate=50.0,
    )
    db.add(bt); db.commit(); db.refresh(bt)
    return bt


def test_labels_round_trip_through_model(test_db):
    bt = _mk(test_db, "bt-labels-roundtrip", labels=["goal5", "S4"])
    assert bt.labels == ["goal5", "S4"]
    assert bt.to_summary_dict()["labels"] == ["goal5", "S4"]
    assert bt.to_dict()["labels"] == ["goal5", "S4"]


def test_labels_none_serializes_to_empty_list(test_db):
    bt = _mk(test_db, "bt-labels-none", labels=None)
    assert bt.to_summary_dict()["labels"] == []


def test_filter_by_label_matches_any_element(client, test_db):
    _mk(test_db, "bt-goal5-s4", labels=["goal5", "S4"])
    _mk(test_db, "bt-goal5-s7", labels=["goal5", "S7"])
    _mk(test_db, "bt-goal4-s4", labels=["goal4", "S4"])
    _mk(test_db, "bt-no-labels", labels=None)

    r = client.get("/api/backtests", params={"label": "goal5"})
    assert r.status_code == 200, r.text
    names = {b["name"] for b in r.json()["backtests"]}
    assert names >= {"bt-goal5-s4", "bt-goal5-s7"}
    assert "bt-goal4-s4" not in names
    assert "bt-no-labels" not in names

    r2 = client.get("/api/backtests", params={"label": "S4"})
    names2 = {b["name"] for b in r2.json()["backtests"]}
    assert names2 >= {"bt-goal5-s4", "bt-goal4-s4"}
    assert "bt-goal5-s7" not in names2


def test_filter_by_label_combines_with_other_filters(client, test_db):
    r = client.get("/api/backtests", params={"label": "S4", "expert": "FMPRating"})
    assert r.status_code == 200, r.text
    names = {b["name"] for b in r.json()["backtests"]}
    assert "bt-goal5-s4" in names
