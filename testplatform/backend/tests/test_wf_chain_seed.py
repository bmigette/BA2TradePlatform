"""Walk-forward chain seeding (goal2027 item 2.5): the final evaluated population of a completed
GA job is persisted on its row and read back, space-checked, to seed the next fold's job.

Hermetic: the per-trial backtest is the deterministic stub of test_strategy_optimization_handler
and the DB is the isolated test engine of conftest. Nothing is optimized for real.
"""
from __future__ import annotations

import pytest

from app.models.database import Base, SessionLocal, engine
from app.models.strategy_optimization import StrategyOptimization
from app.services import strategy_optimization_handler as H
from tests import test_strategy_optimization_handler as T


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})


def _run(sid, name, **over):
    """Create + run one stub optimization; returns (opt_id, handler result)."""
    cfg = T._ga_config(**{k: v for k, v in over.items() if k != "backtest"})
    if "backtest" in over:
        cfg["backtest"] = {**cfg["backtest"], **over["backtest"]}
    opt_id = T._seed_opt(sid, config=cfg)
    _rename(opt_id, name)
    return opt_id, H.handle_strategy_optimization(f"t-{name}", {"optimization_id": opt_id})


def _rename(opt_id, name):
    db = SessionLocal()
    try:
        db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).update(
            {"name": name})
        db.commit()
    finally:
        db.close()


def _cfg(opt_id):
    return T._load_opt(opt_id).optimization_config


# ----------------------------------------------------------------------------- task 1: persist


def test_optimize_returns_the_last_evaluated_population():
    from app.services.genetic import GeneticOptimizer
    space = {"a": {"type": "float", "min": 0, "max": 10, "step": 1},
             "b": {"type": "float", "min": 0, "max": 10, "step": 1}}
    opt = GeneticOptimizer(param_ranges=space, population_size=6, n_generations=3,
                           crossover_prob=0.7, mutation_prob=0.3, early_stopping_generations=10,
                           elitism_percent=10.0)
    res = opt.optimize(fitness_function=lambda p: p["a"] + p["b"])
    fp = res["final_population"]
    assert fp["generation"] == 2
    assert len(fp["genes"]) == len(fp["fitnesses"]) == 6
    # every individual of the snapshot WAS evaluated (the evolved-but-unevaluated generation
    # that follows the last evaluation must not leak in)
    assert all(f is not None for f in fp["fitnesses"])
    assert max(fp["fitnesses"]) == res["history"][-1]["best_fitness"]


def test_early_stop_snapshot_is_the_generation_that_stopped():
    from app.services.genetic import GeneticOptimizer
    space = {"a": {"type": "float", "min": 0, "max": 10, "step": 1},
             "b": {"type": "float", "min": 0, "max": 10, "step": 1}}   # >= 2 genes: two-point cx
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=20,
                           crossover_prob=0.7, mutation_prob=0.3, early_stopping_generations=1,
                           elitism_percent=10.0)
    res = opt.optimize(fitness_function=lambda p: 1.0)      # flat: stops at generation 1
    assert res["final_population"]["generation"] == res["generations_run"] - 1
    assert all(f == 1.0 for f in res["final_population"]["fitnesses"])


def test_handler_stores_final_population_with_gene_space():
    sid = T._seed_strategy()
    opt_id, out = _run(sid, "src-store", populationSize=8, generations=3)
    assert out["status"] == "completed", out
    fp = _cfg(opt_id)["finalPopulation"]
    names = ["entry:bracket:a1:action_value", "entry:bracket:a2:action_value"]
    assert fp["gene_names"] == names
    assert [g["name"] for g in fp["gene_space"]] == names
    assert len(fp["genes"]) == len(fp["fitnesses"]) == 8
    assert fp["generation"] == 2


# ----------------------------------------------------------------------------- task 2: seeding


class _Spy:
    """Captures what GeneticOptimizer.optimize receives."""

    def __init__(self, monkeypatch):
        import app.services.genetic as g
        Real = g.GeneticOptimizer
        captured = self.captured = {}

        class Spy(Real):
            def optimize(self, *a, **kw):
                captured["initial_population"] = kw.get("initial_population")
                return super().optimize(*a, **kw)

        monkeypatch.setattr(H, "GeneticOptimizer", Spy)


def _source(sid, name="cell-wf1", pop=8, end="2024-01-08"):
    opt_id, out = _run(sid, name, populationSize=pop, generations=3, backtest={"end_date": end})
    assert out["status"] == "completed", out
    return opt_id


def test_new_job_starts_from_the_sources_stored_final_population(monkeypatch):
    sid = T._seed_strategy()
    src = _source(sid)
    stored = _cfg(src)["finalPopulation"]
    spy = _Spy(monkeypatch)
    tgt, out = _run(sid, "cell-wf2", populationSize=8, generations=1, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    init = [list(i) for i in spy.captured["initial_population"]]
    assert sorted(init) == sorted(stored["genes"])          # same 8 genomes, nothing random
    seeded = _cfg(tgt)["seededFrom"]
    assert seeded["optimization_id"] == src and seeded["name"] == "cell-wf1"
    assert seeded["stored_population"] == 8 and seeded["filled_random"] == 0


def test_short_stored_population_is_filled_randomly_and_counted(monkeypatch, caplog):
    sid = T._seed_strategy()
    src = _source(sid, pop=8)
    spy = _Spy(monkeypatch)
    with caplog.at_level("WARNING"):
        tgt, out = _run(sid, "cell-wf2", populationSize=12, generations=1,
                        chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    assert len(spy.captured["initial_population"]) == 12
    seeded = _cfg(tgt)["seededFrom"]
    assert seeded["seeded"] == 8 and seeded["filled_random"] == 4
    assert any("filled 4 individual" in r.getMessage() for r in caplog.records)


def test_long_stored_population_keeps_the_fittest(monkeypatch):
    sid = T._seed_strategy()
    src = _source(sid, pop=8)
    stored = _cfg(src)["finalPopulation"]
    spy = _Spy(monkeypatch)
    tgt, out = _run(sid, "cell-wf2", populationSize=4, generations=1, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    top = sorted(range(8), key=lambda i: stored["fitnesses"][i], reverse=True)[:4]
    assert sorted(list(i) for i in spy.captured["initial_population"]) == \
        sorted(stored["genes"][i] for i in top)


def test_space_mismatch_is_refused_not_dropped(monkeypatch):
    sid = T._seed_strategy()
    src = _source(sid)
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(StrategyOptimization.id == src).one()
        cfg = dict(row.optimization_config)
        fp = dict(cfg["finalPopulation"])
        fp["gene_space"] = [dict(fp["gene_space"][0], max=99.0), fp["gene_space"][1]]
        cfg["finalPopulation"] = fp
        row.optimization_config = cfg
        db.commit()
    finally:
        db.close()
    tgt, out = _run(sid, "cell-wf2", chainSeedFromOptimizationId=src)
    assert out["status"] == "failed"
    assert "different gene space" in out["error"] and "entry:bracket:a1:action_value" in out["error"]


def test_source_without_final_population_is_refused():
    sid = T._seed_strategy()
    bare = T._seed_opt(sid)                               # pending, no population
    tgt, out = _run(sid, "cell-wf2", chainSeedFromOptimizationId=bare)
    assert out["status"] == "failed" and "not completed" in out["error"]
    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(StrategyOptimization.id == bare).one()
        row.status = "completed"
        db.commit()
    finally:
        db.close()
    tgt2, out2 = _run(sid, "cell-wf2b", chainSeedFromOptimizationId=bare)
    assert out2["status"] == "failed" and "no stored final population" in out2["error"]


def test_missing_source_is_refused():
    sid = T._seed_strategy()
    tgt, out = _run(sid, "cell-wf2", chainSeedFromOptimizationId=987654)
    assert out["status"] == "failed" and "987654" in out["error"]


def test_source_that_saw_later_data_is_refused_as_look_ahead():
    sid = T._seed_strategy()
    src = _source(sid, end="2024-12-31")
    tgt, out = _run(sid, "cell-wf2", chainSeedFromOptimizationId=src,
                    backtest={"end_date": "2023-12-31"})
    assert out["status"] == "failed" and "look-ahead" in out["error"]


def test_chain_seed_and_warm_start_are_mutually_exclusive():
    sid = T._seed_strategy()
    src = _source(sid)
    tgt, out = _run(sid, "cell-wf2", chainSeedFromOptimizationId=src,
                    warmStartFromOptimizationId=src)
    assert out["status"] == "failed" and "mutually exclusive" in out["error"]


def test_real_runner_reads_back_the_recorded_seed_source(monkeypatch):
    from tests.test_walk_forward import _tool
    sid = T._seed_strategy()
    src = _source(sid, name="lin-wf1")
    tgt, out = _run(sid, "lin-wf2", populationSize=8, generations=1, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    rr = _tool().RealRunner(None)
    monkeypatch.setattr(rr, "_tool", lambda: None)
    assert rr.seed_lineage("lin-wf1") is None            # cold start
    assert rr.seed_lineage("lin-wf2") == "lin-wf1"
