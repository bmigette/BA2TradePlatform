"""PATCH /api/backtests/{id} accepts a `labels` field (add/remove labels on any saved backtest).

Uses the shared conftest ``client``/``db`` fixtures (a throwaway SQLite gate engine).

NOTE: ``gate_engine`` is session-scoped and a committed row is never undone by the ``db``
fixture's teardown rollback — rows seeded here are visible to every other conftest-``client``-
based test in the same pytest session. Uses a distinctive, non-"FMPRating" expert class name so
it can't collide with other files' exact-set assertions on that widely-reused fixture default.
"""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture
def bt(db):
    from app.models.backtest import Backtest

    row = Backtest(
        name="labels-target",
        expert_name="TestOnlyExpert",
        engine_type="daily_expert",
        start_date=datetime(2020, 1, 1),
        end_date=datetime(2020, 6, 1),
        initial_capital=10000.0,
        status="completed",
        labels=["goal6"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_patch_sets_labels(client, bt):
    resp = client.patch(f"/api/backtests/{bt.id}", json={"labels": ["goal6", "S1"]})
    assert resp.status_code == 200, resp.text
    got = client.get(f"/api/backtests/{bt.id}")
    assert set(got.json()["labels"]) == {"goal6", "S1"}


def test_patch_removes_a_label(client, bt):
    resp = client.patch(f"/api/backtests/{bt.id}", json={"labels": []})
    assert resp.status_code == 200, resp.text
    got = client.get(f"/api/backtests/{bt.id}")
    assert got.json()["labels"] == []


def test_patch_clears_labels_with_null(client, bt):
    resp = client.patch(f"/api/backtests/{bt.id}", json={"labels": None})
    assert resp.status_code == 200, resp.text
    got = client.get(f"/api/backtests/{bt.id}")
    assert got.json()["labels"] == []


def test_patch_strips_blank_labels(client, bt):
    resp = client.patch(f"/api/backtests/{bt.id}", json={"labels": ["  goal6  ", "", "  "]})
    assert resp.status_code == 200, resp.text
    got = client.get(f"/api/backtests/{bt.id}")
    assert got.json()["labels"] == ["goal6"]


def test_patch_rejects_non_string_labels(client, bt):
    resp = client.patch(f"/api/backtests/{bt.id}", json={"labels": ["ok", 5]})
    assert resp.status_code == 400


def test_patch_404_on_missing_backtest(client):
    resp = client.patch("/api/backtests/999999", json={"labels": ["x"]})
    assert resp.status_code == 404
