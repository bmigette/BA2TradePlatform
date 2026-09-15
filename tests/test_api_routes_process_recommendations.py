"""POST /api/process-recommendations -- the risk-manager pass over EXISTING recommendations.

Exists because on 2026-09-07 the pass that should have fired on its own never did (a
self-deadlock in the trigger), and the only way to run it afterwards was a UI dialog driven by
a Quasar dropdown. ``/run-schedule`` re-runs the whole analysis instead; this makes the SAME
call the dialog makes.

Same harness as test_api_routes_reload.py: a bare FastAPI app with the router. Both
collaborators the route imports at call time -- the existence check and the trade manager --
are replaced at the modules it imports them from. The route's contract is "check the expert
exists, then delegate"; what ``get_instance`` does against a real DB is the DB layer's tests'
business, and a plain ``def`` route runs in TestClient's worker thread where a thread-bound
test DB would not be visible anyway.
"""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ba2_trade_platform.ui import api_routes

EXPERT = 10


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_routes.router)
    return TestClient(app)


def _expert_exists(monkeypatch, *ids):
    """``get_instance(ExpertInstance, id)`` answers a record for ``ids`` and RAISES otherwise.

    Raises, because that is the real contract: ``get_instance`` enforces raise-if-not-found
    (its docstring says None). A fake that returned None here is exactly how the first live
    probe of this route came back as a bare 500 -- the test encoded the wrong contract.
    """
    from ba2_trade_platform.core.db import InstanceNotFound

    def fake(model, ident):
        if ident in ids:
            return SimpleNamespace(id=ident)
        raise InstanceNotFound(f"Instance with id {ident}/{model} not found.")
    monkeypatch.setattr("ba2_trade_platform.core.db.get_instance", fake)


def _fake_trade_manager(monkeypatch, *, orders=(), raises=None):
    """Record what the route asks for; return ``orders`` (objects with ``.id``) or raise."""
    calls = []

    def process(expert_instance_id, lookback_days=1):
        calls.append((expert_instance_id, lookback_days))
        if raises is not None:
            raise raises
        return [SimpleNamespace(id=i) for i in orders]

    monkeypatch.setattr("ba2_trade_platform.core.TradeManager.get_trade_manager",
                        lambda: SimpleNamespace(process_expert_recommendations_after_analysis=process))
    return calls


def test_runs_the_pass_for_that_expert_and_reports_the_orders_it_created(monkeypatch):
    _expert_exists(monkeypatch, EXPERT)
    calls = _fake_trade_manager(monkeypatch, orders=(482, 483))

    r = _client().post("/api/process-recommendations",
                       json={"expert_instance_id": EXPERT, "lookback_days": 3})

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "expert_instance_id": EXPERT,
                        "lookback_days": 3, "created_orders": [482, 483]}
    assert calls == [(EXPERT, 3)]


def test_lookback_defaults_to_one_day_like_the_dialog(monkeypatch):
    _expert_exists(monkeypatch, EXPERT)
    calls = _fake_trade_manager(monkeypatch)

    r = _client().post("/api/process-recommendations", json={"expert_instance_id": EXPERT})

    assert r.status_code == 200
    assert r.json()["created_orders"] == []
    assert calls == [(EXPERT, 1)]


def test_no_orders_is_still_ok_not_an_error(monkeypatch):
    """Nothing passing the ruleset is a normal outcome -- the dialog says so in blue, not red."""
    _expert_exists(monkeypatch, EXPERT)
    _fake_trade_manager(monkeypatch, orders=())

    r = _client().post("/api/process-recommendations", json={"expert_instance_id": EXPERT})

    assert r.status_code == 200
    assert r.json()["created_orders"] == []


def test_unknown_expert_is_404_and_never_reaches_the_trade_manager(monkeypatch):
    _expert_exists(monkeypatch, EXPERT)          # 999999 is not in the list
    calls = _fake_trade_manager(monkeypatch)

    r = _client().post("/api/process-recommendations", json={"expert_instance_id": 999_999})

    assert r.status_code == 404
    assert "999999" in r.json()["detail"]
    assert calls == []


def test_a_lookback_below_one_day_is_refused_before_anything_is_looked_up(monkeypatch):
    looked_up = []
    monkeypatch.setattr("ba2_trade_platform.core.db.get_instance",
                        lambda model, ident: looked_up.append(ident))
    calls = _fake_trade_manager(monkeypatch)

    r = _client().post("/api/process-recommendations",
                       json={"expert_instance_id": EXPERT, "lookback_days": 0})

    assert r.status_code == 400
    assert looked_up == [] and calls == []


def test_a_pass_that_raises_surfaces_its_own_message(monkeypatch):
    """The operator gets the error the pass produced, not a generic 500 to go grep for."""
    _expert_exists(monkeypatch, EXPERT)
    _fake_trade_manager(monkeypatch, raises=RuntimeError("broker unreachable"))

    r = _client().post("/api/process-recommendations", json={"expert_instance_id": EXPERT})

    assert r.status_code == 500
    assert "broker unreachable" in r.json()["detail"]
