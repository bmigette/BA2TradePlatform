"""``optimize`` must not re-run a job that has already COMPLETED under the same name.

A grid that loops over jobs and calls the launcher directly has no idempotence of its
own, so every restart re-ran what had already finished: ``sen-S1-goal2020-risk_atr``
completed as optimization 434, again as 442 with the same fitness to fourteen decimal
places, and a third copy was a quarter of the way through ~6h of remote work before it
was spotted. Each launch mints a fresh Strategy row, so nothing deduped it either, and
a duplicate completed run is indistinguishable from a first one after the fact.

The matrix DRIVER has checked this by name since it was written
(``tools/run_screener_capband_matrix.py:_completed_names``) -- which is why the
DeterministicScorer phase of the same grid never had the problem -- and the launcher's
own ``--sizing-mode`` help already promised the behaviour. These tests pin it where
every caller gets it.
"""
import argparse
import importlib.util
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _args(**over):
    """The optimize arguments the guard reads, and only those.

    Everything after the guard needs a real universe, a metric store and hours of
    compute; the point of these tests is that none of it is reached.
    """
    kw = dict(expert="FMPSenateTraderWeight", strategy="S1",
              fitness="consistent_annual_return", universe="AAPL,MSFT",
              start="2020-01-01", end="2025-12-31", run_schedule="daily",
              run_schedule_day="monday", name="sen-S1-goal2020-risk_atr",
              rerun=False, option_min_volume=0, gates_off=False)
    kw.update(over)
    return argparse.Namespace(**kw)


@pytest.fixture()
def completed_optimization():
    """One COMPLETED optimization row named like the grid's job. Returns its id."""
    import app.models  # noqa: F401 -- register ORM models
    from app.models.database import SessionLocal, init_db
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization

    init_db()
    db = SessionLocal()
    try:
        strat = Strategy(name="sen-S1-goal2020-risk_atr", description="x")
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="sen-S1-goal2020-risk_atr",
            fitness_metric="consistent_annual_return", optimization_type="genetic",
            optimization_config={}, status="completed", best_fitness=6.907800925115058)
        db.add(opt); db.commit(); db.refresh(opt)
        return opt.id
    finally:
        db.close()


def _optimization_count(name):
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        return (db.query(StrategyOptimization)
                .filter(StrategyOptimization.name == name).count())
    finally:
        db.close()


def test_a_completed_job_is_skipped_and_creates_nothing(completed_optimization, capsys):
    """The whole point: no second Strategy row, no second optimization, no trials."""
    rc = mod._cmd_optimize(_args())

    assert rc == 0
    assert _optimization_count("sen-S1-goal2020-risk_atr") == 1


def test_the_skip_is_not_a_FAILURE(completed_optimization):
    """Exit 0. A grid loop that treats a non-zero rc as "the job broke" would turn a
    correctly-skipped job into a red run and mask the failures that are real."""
    assert mod._cmd_optimize(_args()) == 0


def test_the_skip_says_which_run_already_answered(completed_optimization, capsys):
    """Auditable, not taken on trust -- the id is what a reader checks the fitness of."""
    mod._cmd_optimize(_args())

    out = capsys.readouterr().out
    assert "SKIP" in out
    assert f"#{completed_optimization}" in out
    assert "--rerun" in out


def test_a_DIFFERENT_name_is_not_skipped(completed_optimization, monkeypatch):
    """The two sizing modes differ only by name suffix, so a match on anything looser
    than the full name would silently drop half the matrix."""
    reached = {}

    def _stop(*a, **kw):
        reached['yes'] = True
        raise RuntimeError('reached the real optimize')

    monkeypatch.setattr(mod, "_build_strategy", _stop)
    with pytest.raises(RuntimeError):
        mod._cmd_optimize(_args(name="sen-S1-goal2020-notional"))
    assert reached


def test_rerun_forces_a_completed_job_to_run_again(completed_optimization, monkeypatch):
    """The deliberate repeat -- a code change to re-measure against, a reproducibility
    check. Opt-in, because the accident it re-enables costs hours of cluster time."""
    def _stop(*a, **kw):
        raise RuntimeError('reached the real optimize')

    monkeypatch.setattr(mod, "_build_strategy", _stop)
    with pytest.raises(RuntimeError):
        mod._cmd_optimize(_args(rerun=True))


def test_an_UNFINISHED_run_of_the_same_name_does_not_skip(completed_optimization,
                                                          monkeypatch):
    """Only ``completed`` counts. A cancelled or failed row is work still owed, and
    skipping on it would strand a job the grid believes it has done."""
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    db = SessionLocal()
    try:
        # EVERY row of this name, not just this test's: the isolated DB is shared by
        # the module, so the rows the earlier tests created are still completed and
        # one of them would satisfy the guard on its own.
        for row in db.query(StrategyOptimization).filter(
                StrategyOptimization.name == "sen-S1-goal2020-risk_atr").all():
            row.status = "cancelled"
        db.commit()
    finally:
        db.close()

    def _stop(*a, **kw):
        raise RuntimeError('reached the real optimize')

    monkeypatch.setattr(mod, "_build_strategy", _stop)
    with pytest.raises(RuntimeError):
        mod._cmd_optimize(_args())
