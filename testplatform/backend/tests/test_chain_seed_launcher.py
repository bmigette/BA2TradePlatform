"""``ba2-test optimize --chain-seed-from <job name>`` resolves to the completed optimization's id."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))
sys.path.insert(0, os.path.dirname(__file__))

import ba2test_launcher as L  # noqa: E402
from test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize  # noqa: E402

from app.models.database import Base, SessionLocal, engine  # noqa: E402
from app.models.strategy import Strategy  # noqa: E402
from app.models.strategy_optimization import StrategyOptimization  # noqa: E402


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(bind=engine)


def _opt(name, status):
    db = SessionLocal()
    try:
        s = Strategy(name="chain-src", entry_rules=[], exit_rules=[])
        db.add(s)
        db.commit()
        row = StrategyOptimization(strategy_id=s.id, name=name, fitness_metric="sharpe",
                                   optimization_type="genetic", optimization_config={},
                                   status=status)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_chain_seed_name_resolves_to_the_completed_row_id(monkeypatch):
    oid = _opt("cell-launcher-wf1", "completed")
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--chain-seed-from", "cell-launcher-wf1"]), monkeypatch)
    assert cfg["chainSeedFromOptimizationId"] == oid
    assert "warmStartFromOptimizationId" not in cfg


def test_no_flag_no_key(monkeypatch):
    assert "chainSeedFromOptimizationId" not in _run_optimize(_parse(_BASE_ARGV), monkeypatch)


@pytest.mark.parametrize("setup,needle", [
    ("none", "no optimization has that name"),
    ("running", "not completed"),
    ("dup", "share that name"),
])
def test_chain_seed_refusals(monkeypatch, setup, needle):
    name = f"cell-launcher-{setup}"
    if setup == "running":
        _opt(name, "running")
    if setup == "dup":
        _opt(name, "completed")
        _opt(name, "failed")
    with pytest.raises(SystemExit) as e:
        _run_optimize(_parse(_BASE_ARGV + ["--chain-seed-from", name]), monkeypatch)
    assert needle in str(e.value)


def test_chain_seed_with_warm_start_is_refused(monkeypatch):
    _opt("cell-launcher-both", "completed")
    with pytest.raises(SystemExit) as e:
        _run_optimize(_parse(_BASE_ARGV + ["--chain-seed-from", "cell-launcher-both",
                                           "--warm-start-from", "1"]), monkeypatch)
    assert "mutually exclusive" in str(e.value)
