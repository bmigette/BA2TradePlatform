"""Task 6: backtest list filters (expert/optimization_id/saved) + create accepts engine/expert/universe.

Uses the shared conftest ``client``/``db`` fixtures (a throwaway SQLite gate engine with
``queue_task`` stubbed). The ``db`` and ``client`` fixtures bind to the SAME gate engine, so
rows seeded through ``db`` are visible to the route under test.
"""
from __future__ import annotations

from datetime import datetime

import pytest


def _seed_backtest(db, *, name, expert_name, optimization_id, is_saved=False, engine_type="daily_expert"):
    from app.models.backtest import Backtest

    bt = Backtest(
        name=name,
        expert_name=expert_name,
        optimization_id=optimization_id,
        is_saved=is_saved,
        engine_type=engine_type,
        start_date=datetime(2020, 1, 1),
        end_date=datetime(2020, 6, 1),
        initial_capital=10000.0,
        status="completed",
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


@pytest.fixture
def seeded(db):
    """Three rows: (FMPRating, opt None), (FMPRating, opt 5), (FMPEarningsDrift, opt 5)."""
    a = _seed_backtest(db, name="rating-none", expert_name="FMPRating", optimization_id=None)
    b = _seed_backtest(db, name="rating-opt5", expert_name="FMPRating", optimization_id=5, is_saved=True)
    c = _seed_backtest(db, name="drift-opt5", expert_name="FMPEarningsDrift", optimization_id=5)
    return {"a": a, "b": b, "c": c}


# --- Part A: list filters ---------------------------------------------------
def test_list_filter_by_expert(client, seeded):
    resp = client.get("/api/backtests?expert=FMPRating")
    assert resp.status_code == 200, resp.text
    items = resp.json()["backtests"]
    names = {i["name"] for i in items}
    assert names == {"rating-none", "rating-opt5"}
    # every item exposes expert_name + optimization_id
    for i in items:
        assert i["expertName"] == "FMPRating"
        assert "optimizationId" in i


def test_list_filter_by_optimization_id(client, seeded):
    resp = client.get("/api/backtests?optimization_id=5")
    assert resp.status_code == 200, resp.text
    items = resp.json()["backtests"]
    names = {i["name"] for i in items}
    assert names == {"rating-opt5", "drift-opt5"}
    for i in items:
        assert i["optimizationId"] == 5


def test_list_filter_by_saved(client, seeded):
    resp = client.get("/api/backtests?saved=true")
    assert resp.status_code == 200, resp.text
    items = resp.json()["backtests"]
    names = {i["name"] for i in items}
    assert names == {"rating-opt5"}


def test_list_no_filter_returns_all(client, seeded):
    resp = client.get("/api/backtests")
    assert resp.status_code == 200, resp.text
    items = resp.json()["backtests"]
    names = {i["name"] for i in items}
    assert {"rating-none", "rating-opt5", "drift-opt5"} <= names


# --- Part B: create accepts engine/expert/universe --------------------------
def test_create_daily_expert_static_universe(client, db):
    """A daily_expert create with a supported expert + static universe returns a queued backtest."""
    payload = {
        "name": "daily-create-test",
        "engine": "daily_expert",
        "expert": {"class": "FMPRating", "settings": {}},
        "universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]},
        "start_date": "2020-01-01",
        "end_date": "2020-06-01",
        "initial_capital": 10000.0,
        "commission": 1.0,
        "slippage": 5.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }
    resp = client.post("/api/backtests", json=payload)
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["expertName"] == "FMPRating"
    assert body["engineType"] == "daily_expert"


def test_create_daily_expert_forwards_conditions_and_tp_sl(client, db, monkeypatch):
    """A daily_expert create WITH a buy-entry tree + TP/SL forwards them into the enqueued
    task payload using the handler's exact keys (buy_tree / sell_tree / exit_rules /
    initial_tp_percent / initial_sl_percent)."""
    from app.services import task_queue as tq

    captured: dict = {}

    def _capture(*args, **kwargs):
        captured["payload"] = kwargs.get("payload")
        return "stub-task-id"

    monkeypatch.setattr(tq.get_task_queue(), "queue_task", _capture, raising=False)

    buy_tree = {
        "operator": "AND",
        "conditions": [{"field": "confidence", "op": ">", "value": 0.7}],
    }
    sell_tree = {
        "operator": "AND",
        "conditions": [{"field": "confidence", "op": "<", "value": 0.3}],
    }
    exit_rules = [{"id": "exit-1", "action": "close", "conditions": {}}]

    payload = {
        "name": "daily-conditions-test",
        "engine": "daily_expert",
        "expert": {"class": "FMPRating", "settings": {}},
        "universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]},
        "start_date": "2020-01-01",
        "end_date": "2020-06-01",
        "initial_capital": 10000.0,
        "commission": 1.0,
        "slippage": 5.0,
        "fill_model": "next_bar_open",
        "seed": 42,
        "buy_entry_conditions": buy_tree,
        "sell_entry_conditions": sell_tree,
        "exit_conditions": exit_rules,
        "initial_tp_percent": 8.0,
        "initial_sl_percent": 4.0,
    }
    resp = client.post("/api/backtests", json=payload)
    assert resp.status_code in (200, 201), resp.text

    enqueued = captured["payload"]
    assert enqueued is not None, "queue_task was not called with a payload"
    # The handler reads the buy-entry tree from "buy_tree" (seed_ruleset_from_tree),
    # and the bracket from "initial_tp_percent"/"initial_sl_percent".
    assert enqueued["buy_tree"] == buy_tree
    assert enqueued["sell_tree"] == sell_tree
    assert enqueued["exit_rules"] == exit_rules
    assert enqueued["initial_tp_percent"] == 8.0
    assert enqueued["initial_sl_percent"] == 4.0


def test_create_daily_expert_omits_unset_conditions(client, db, monkeypatch):
    """When conditions/TP/SL are not provided, the enqueued payload must NOT carry those keys
    (so the handler's own defaults apply rather than being overridden with None)."""
    from app.services import task_queue as tq

    captured: dict = {}
    monkeypatch.setattr(
        tq.get_task_queue(),
        "queue_task",
        lambda *a, **kw: captured.update(payload=kw.get("payload")) or "stub-task-id",
        raising=False,
    )

    payload = {
        "name": "daily-no-conditions",
        "engine": "daily_expert",
        "expert": {"class": "FMPRating", "settings": {}},
        "universe": {"mode": "static", "symbols": ["AAPL"]},
        "start_date": "2020-01-01",
        "end_date": "2020-06-01",
        "initial_capital": 10000.0,
        "commission": 1.0,
        "slippage": 5.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }
    resp = client.post("/api/backtests", json=payload)
    assert resp.status_code in (200, 201), resp.text

    enqueued = captured["payload"]
    assert enqueued is not None
    for k in ("buy_tree", "sell_tree", "exit_rules", "initial_tp_percent", "initial_sl_percent"):
        assert k not in enqueued, f"unset {k} must be omitted from the payload"


def test_create_daily_expert_rejects_unknown_expert(client):
    payload = {
        "name": "bad-expert",
        "engine": "daily_expert",
        "expert": {"class": "NotARealExpert"},
        "universe": {"mode": "static", "symbols": ["AAPL"]},
        "start_date": "2020-01-01",
        "end_date": "2020-06-01",
        "initial_capital": 10000.0,
        "commission": 1.0,
        "slippage": 5.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }
    resp = client.post("/api/backtests", json=payload)
    assert resp.status_code == 400, resp.text


def test_create_daily_expert_rejects_empty_universe(client):
    payload = {
        "name": "empty-universe",
        "engine": "daily_expert",
        "expert": {"class": "FMPRating"},
        "universe": {"mode": "static", "symbols": []},
        "start_date": "2020-01-01",
        "end_date": "2020-06-01",
        "initial_capital": 10000.0,
        "commission": 1.0,
        "slippage": 5.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }
    resp = client.post("/api/backtests", json=payload)
    assert resp.status_code == 400, resp.text


def test_create_ml_still_works(client, db, monkeypatch):
    """The existing ML create path must keep working byte-for-byte (engine defaults to 'ml')."""
    from app.models.model import TrainedModel
    from app.models.dataset import Dataset

    model = TrainedModel(model_id="mdl-test123", name="m1", model_type="LSTM", status="completed")
    db.add(model)

    def _ds(name):
        return Dataset(
            name=name,
            ticker="AAPL",
            timeframe="1d",
            start_date=datetime(2020, 1, 1),
            end_date=datetime(2020, 6, 1),
            file_path=f"/tmp/{name}.csv",
        )

    pred = _ds("pred-ds")
    exe = _ds("exec-ds")
    db.add(pred)
    db.add(exe)
    db.commit()
    db.refresh(model)
    db.refresh(pred)
    db.refresh(exe)

    payload = {
        "name": "ml-create-test",
        "model_id": "mdl-test123",
        "prediction_dataset_id": pred.id,
        "execution_dataset_id": exe.id,
        "start_date": "2020-01-01",
        "end_date": "2020-06-01",
        "initial_capital": 10000.0,
    }
    resp = client.post("/api/backtests", json=payload)
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["engineType"] == "ml"
