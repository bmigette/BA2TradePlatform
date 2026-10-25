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


def _distinct(rec):
    """Distinct genomes of a stored population, fittest first (the seeder drops the GA's elite clones)."""
    out, seen = [], set()
    for i in sorted(range(len(rec["genes"])), key=lambda i: rec["fitnesses"][i], reverse=True):
        k = tuple(rec["genes"][i])
        if k not in seen:
            seen.add(k)
            out.append(rec["genes"][i])
    return out


def _pop(opt_id):
    """The stored final population as the flat record the seeder reads (table row, not config)."""
    from app.models.strategy_optimization import StrategyOptimizationPopulation as P
    from app.services.chain_seed import stored_record
    db = SessionLocal()
    try:
        return stored_record(db.get(P, opt_id))
    finally:
        db.close()


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
    fp = _pop(opt_id)
    names = ["entry:bracket:a1:action_value", "entry:bracket:a2:action_value"]
    assert fp["gene_names"] == names
    assert [g["name"] for g in fp["gene_space"]] == names
    assert "finalPopulation" not in _cfg(opt_id)
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
    stored = _pop(src)
    spy = _Spy(monkeypatch)
    tgt, out = _run(sid, "cell-wf2", populationSize=8, generations=1, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    init = [list(i) for i in spy.captured["initial_population"]]
    distinct = _distinct(stored)
    assert sorted(init[:len(distinct)]) == sorted(distinct)    # every distinct genome, none random
    seeded = _cfg(tgt)["seededFrom"]
    assert seeded["optimization_id"] == src and seeded["name"] == "cell-wf1"
    assert seeded["stored_population"] == 8 and seeded["seeded"] == len(distinct)
    assert seeded["dropped_duplicates"] == 8 - len(distinct)      # the GA clones its elites


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
    n = len(_distinct(_pop(src)))
    assert seeded["seeded"] == n and seeded["filled_random"] == 12 - n
    assert any(f"filled {12 - n} randomly" in r.getMessage() for r in caplog.records)


def test_long_stored_population_keeps_the_fittest(monkeypatch):
    sid = T._seed_strategy()
    src = _source(sid, pop=8)
    stored = _pop(src)
    spy = _Spy(monkeypatch)
    tgt, out = _run(sid, "cell-wf2", populationSize=4, generations=1, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    top = _distinct(stored)[:4]
    assert len(top) == 4
    assert sorted(list(i) for i in spy.captured["initial_population"]) == sorted(top)


def test_space_mismatch_is_refused_not_dropped(monkeypatch):
    sid = T._seed_strategy()
    src = _source(sid)
    from app.models.strategy_optimization import StrategyOptimizationPopulation as P
    db = SessionLocal()
    try:
        row = db.get(P, src)
        sig = [dict(g) for g in row.gene_space_signature]
        sig[0]["max"] = 99.0
        row.gene_space_signature = sig
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


def test_checkpoint_resumed_chain_job_still_records_its_seed_source(monkeypatch):
    """A relaunch inserts a NEW row and resumes the job's checkpoint (the seed block is skipped):
    the row must still name the seed source so the chain's lineage check holds."""
    sid = T._seed_strategy()
    src = _source(sid, name="res-wf1")
    stored = _pop(src)
    ckpt = {"generation": 0, "partial": True, "population": stored["genes"],
            "fitnesses": [None] * len(stored["genes"]), "history": [], "best_individual": stored["genes"][0],
            "best_fitness": max(stored["fitnesses"]), "robust_fitness": False,
            "fitness_metric": "sharpe", "top_results": []}
    monkeypatch.setattr(H, "_load_checkpoint", lambda task_id, fingerprint=None: dict(ckpt))
    tgt, out = _run(sid, "res-wf2", populationSize=8, generations=2, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    seeded = _cfg(tgt)["seededFrom"]
    assert seeded["name"] == "res-wf1" and seeded["resumed_from_checkpoint"] is True


# ----------------------------------------------------------------------------- review fixes


def test_resume_past_the_last_generation_still_returns_the_restored_population():
    from app.services.genetic import GeneticOptimizer
    space = {"a": {"type": "float", "min": 0, "max": 10, "step": 1},
             "b": {"type": "float", "min": 0, "max": 10, "step": 1}}
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=3,
                           crossover_prob=0.7, mutation_prob=0.3, early_stopping_generations=10,
                           elitism_percent=10.0)
    genes = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]
    fits = [3.0, 7.0, 11.0, 15.0]
    start, pop, rf = opt.resume_from_checkpoint({
        "generation": 2, "partial": False, "population": genes, "fitnesses": fits, "history": [],
        "best_individual": genes[3], "best_fitness": 15.0})
    assert start == 3                                   # nothing left to run
    res = opt.optimize(fitness_function=lambda p: 0.0, start_generation=start,
                       initial_population=pop, restored_fitnesses=rf)
    fp = res["final_population"]
    assert fp["genes"] == genes and fp["fitnesses"] == fits and fp["generation"] == 2


def test_a_job_that_must_persist_a_population_fails_loudly_without_one(monkeypatch):
    import app.services.genetic as g
    Real = g.GeneticOptimizer

    class NoPop(Real):
        def optimize(self, *a, **kw):
            r = super().optimize(*a, **kw)
            r["final_population"] = None
            return r

    monkeypatch.setattr(H, "GeneticOptimizer", NoPop)
    sid = T._seed_strategy()
    opt_id, out = _run(sid, "needs-pop", requireFinalPopulation=True)
    assert out["status"] == "failed" and "final population" in out["error"]
    # an ordinary job is unaffected
    opt2, out2 = _run(sid, "plain-pop")
    assert out2["status"] == "completed"


def test_a_chain_job_without_an_end_date_is_refused(monkeypatch):
    sid = T._seed_strategy()
    src = _source(sid)
    opt_id = T._seed_opt(sid, config=T._ga_config(chainSeedFromOptimizationId=src))
    db = SessionLocal()
    try:
        row = db.get(StrategyOptimization, opt_id)
        cfg = dict(row.optimization_config)
        cfg["backtest"] = {k: v for k, v in cfg["backtest"].items() if k != "end_date"}
        row.optimization_config = cfg
        db.commit()
    finally:
        db.close()
    out = H.handle_strategy_optimization("t-no-end", {"optimization_id": opt_id})
    assert out["status"] == "failed" and "no end_date" in out["error"]


def test_failed_and_duplicate_individuals_are_dropped_before_padding(monkeypatch):
    from app.models.strategy_optimization import StrategyOptimizationPopulation as P
    sid = T._seed_strategy()
    src = _source(sid)
    db = SessionLocal()
    try:
        row = db.get(P, src)
        pop = dict(row.population)
        g0 = pop["genes"][0]
        pop["genes"] = [g0, list(g0), [3.0, -2.0], [4.0, -2.0], [5.0, -3.0]]
        pop["fitnesses"] = [9.0, 8.5, -1.0e9, 7.0, 6.0]
        row.population = pop
        db.commit()
    finally:
        db.close()
    spy = _Spy(monkeypatch)
    tgt, out = _run(sid, "dedupe-wf2", populationSize=6, generations=1, chainSeedFromOptimizationId=src)
    assert out["status"] == "completed", out
    s = _cfg(tgt)["seededFrom"]
    assert (s["stored_population"], s["dropped_failed"], s["dropped_duplicates"], s["seeded"],
            s["filled_random"]) == (5, 1, 1, 3, 3)
    init = [list(i) for i in spy.captured["initial_population"]]
    assert [3.0, -2.0] not in init[:3] and init.count(list(g0)) >= 1
