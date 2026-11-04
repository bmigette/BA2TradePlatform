"""The entry-gate counter is READ-ONLY: the same tiny backtest with and without it yields identical
trades and equity curve, and with it the run does record entry-gate counts."""
from __future__ import annotations

import pytest

from app.services.backtest.daily_engine import DailyBacktestEngine
from tests.backtest.fixtures.e2e_support import (
    earnings_drift_payload, ensure_host_schema, load_backtest, new_backtest_row, run_daily_backtest,
)


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    ensure_host_schema()
    yield


def _run(name):
    bid = new_backtest_row(name)
    r = run_daily_backtest(earnings_drift_payload(bid, seed=7), task_id=name)
    assert r["status"] == "completed", r.get("error")
    return load_backtest(bid)


def test_recorder_changes_nothing_and_records_counts(monkeypatch):
    with_rec = _run("eg-on")

    real_init = DailyBacktestEngine.__init__

    def init_without_recorder(self, *a, **k):
        real_init(self, *a, **k)
        self._eg = None

    monkeypatch.setattr(DailyBacktestEngine, "__init__", init_without_recorder)
    without_rec = _run("eg-off")

    assert with_rec.trades == without_rec.trades
    assert with_rec.equity_curve == without_rec.equity_curve
    assert with_rec.total_trades >= 1

    rec = (with_rec.results or {}).get("entry_gate_record")
    assert rec, "recorder produced no counts"
    assert sum(v["n"] for v in rec.values()) >= 1
    assert all(v["met"] <= v["n"] for v in rec.values())
    assert "entry_gate_record" not in (without_rec.results or {})


def test_seeding_registers_leaf_ids_matching_trigger_keys():
    """seed_ruleset_from_rules registers cond_i -> leaf id (dropped leaf shifts positions)."""
    from sqlmodel import Session

    import ba2_common.core.db as cdb
    from app.services.backtest import default_rulesets as dr
    from app.services.backtest import entry_gate_bt as egm
    from app.services.backtest.backtest_db import backtest_trading_db
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.models import Ruleset
    from ba2_common.core.types import AnalysisUseCase

    wire_backtest_seams()
    rule = {"id": "r-entry", "name": "r-entry",
            "conditions": {"type": "AND", "conditions": [
                {"id": "unk", "field": "no_such_field_ever", "op": ">", "value": 1},
                {"id": "shared-days", "field": "days_opened", "op": ">", "value": 10}]},
            "actions": [{"action_type": "buy"}]}
    with backtest_trading_db("entry-gate-seed-test"):
        rid = dr.seed_ruleset_from_rules([rule], AnalysisUseCase.ENTER_MARKET, name="eg")
        with Session(cdb.get_engine()) as s:
            ea = s.get(Ruleset, rid).event_actions[0]
            keys, ea_id, ea_name = list(ea.triggers), ea.id, ea.name
    assert keys == ["cond_1"]
    reg = egm._REGISTRY[ea_id]
    assert reg["name"] == ea_name and reg["rule"] == "r-entry" and reg["leaves"] == {"cond_1": "shared-days"}
