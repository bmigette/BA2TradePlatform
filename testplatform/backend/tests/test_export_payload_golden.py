"""Golden pin for _derive_export_payload (both kinds). The derivation moves to ba2_common
(site plan P0a); these goldens were captured from the pre-move code and must never change
unless a behaviour change is intended. Re-baseline: BA2_UPDATE_GOLDEN=1 pytest this file."""
import json
import os
from datetime import datetime

import pytest

from app.api.backtests import _derive_export_payload
from app.models.backtest import Backtest
from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization

HERE = os.path.join(os.path.dirname(__file__), "fixtures", "export_golden")
with open(os.path.join(HERE, "cases.json")) as _f:
    CASES = json.load(_f)
UPDATE = os.environ.get("BA2_UPDATE_GOLDEN") == "1"


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _run(case, kind):
    db = SessionLocal()
    try:
        fields = dict(case["backtest"])
        if case["opt_backtest_block"] is not None:
            strat = Strategy(name=f"golden-{case['case_id']}-{kind}", entry_rules=[], exit_rules=[])
            db.add(strat)
            db.flush()
            opt = StrategyOptimization(
                strategy_id=strat.id, name=f"golden-{case['case_id']}-{kind}",
                fitness_metric="sharpe", optimization_type="genetic",
                optimization_config={"backtest": case["opt_backtest_block"]},
                all_results=[], best_params={}, best_fitness=0.0, status="completed")
            db.add(opt)
            db.flush()
            fields["optimization_id"] = opt.id
        fields["start_date"] = _dt(fields["start_date"])
        fields["end_date"] = _dt(fields["end_date"])
        bt = Backtest(**fields)
        try:
            payload = _derive_export_payload(bt, kind, db)
        except Exception as e:  # noqa: BLE001
            payload = {"__error__": type(e).__name__, "detail": str(getattr(e, "detail", e))}
        # backtest_id is the ORIGINAL row id carried in the case; optimization ids are
        # host-DB artefacts and are not part of any payload.
        return json.dumps(payload, indent=1, default=str)
    finally:
        # Host rows were only flushed (visible to the export's queries on this same session);
        # roll them back so the test leaves nothing behind.
        db.rollback()
        db.close()


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
@pytest.mark.parametrize("kind", ["expert_settings", "ruleset"])
def test_export_payload_matches_golden(case, kind):
    got = _run(case, kind)
    path = os.path.join(HERE, "golden", f"{case['case_id']}__{kind}.json")
    if UPDATE:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(got)
    with open(path) as f:
        assert got == f.read()
