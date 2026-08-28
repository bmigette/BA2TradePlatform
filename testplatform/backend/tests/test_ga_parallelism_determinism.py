"""F4 regression: a GA result must depend on (seed, population, generations) -- NOT on --parallel.

THE BUG. ``GeneticOptimizer`` used to draw every one of its own random numbers (initial
population, tournament selection, crossover gate + points, mutation gate + gaussians) from the
process-global ``random`` module -- directly, and via ``tools.cxTwoPoint`` / ``tools.selTournament``
which are documented as doing the same. Whether the fitness evaluation shared that module was
decided purely by ``--parallel``:

  * ``parallel <= 1`` and no remote workers -> ``strategy_optimization_handler._dispatch_engages``
    is False, ``batch_fitness`` is None, and ``optimize()`` evaluates IN THIS PROCESS. The first
    statement of ``DailyBacktestEngine.run()`` is ``random.seed(self.seed)``
    (backtest/daily_engine.py) -- so every trial RESET the GA's own stream.
  * ``parallel > 1`` -> each trial runs in a spawned worker process and the master's ``random``
    module is never touched.

Measured on this box (5 symbols, pop=16, gen=3, seed=42, only ``--parallel`` differing):
parallel=2/4/8 -> -6.04, parallel=1 -> -43.66, with generation 0 (drawn before any evaluation)
identical in both and every later generation different.

THE FIX. The GA owns a private ``random.Random`` (``GeneticOptimizer._rng``), cloned from the
global module state at construction so it still inherits the caller's ``random.seed(seed)``.
Nothing a fitness function does to the global RNG can reach it.
"""

import random

import numpy as np
import pytest

from app.services.genetic import GeneticOptimizer


SPACE = {
    "a": {"type": "int", "min": 0, "max": 100, "step": 1},
    "b": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.01},
    "c": {"type": "int", "min": 1, "max": 20, "step": 1},
    # min/max/step mirror what strategy_param_space emits for a categorical gene (the index
    # range), so this space is shaped like a real one.
    "d": {"type": "choice", "choices": ["x", "y", "z"], "min": 0, "max": 2, "step": 1},
}

# The engine's seed, i.e. the value the in-process backtest slams into the global RNG.
ENGINE_SEED = 1234


def _score(params) -> float:
    """A deterministic function of the genome alone -- no randomness of its own."""
    return (params["a"] * 0.5
            - abs(params["b"] - 0.3) * 100
            + params["c"]
            + {"x": 0.0, "y": 3.0, "z": -3.0}[params["d"]])


def _make(seed=42, **kw):
    random.seed(seed)
    np.random.seed(seed)
    opts = dict(param_ranges=SPACE, population_size=12, n_generations=5,
                crossover_prob=0.7, mutation_prob=0.3,
                early_stopping_generations=99, elitism_percent=10.0)
    opts.update(kw)
    return GeneticOptimizer(**opts)


def _run_in_process(fitness, seed=42, **kw):
    """The ``parallel <= 1`` shape: ``optimize()`` maps the fitness function itself."""
    return _make(seed, **kw).optimize(fitness_function=fitness)


def _run_batched(fitness, seed=42, **kw):
    """The ``parallel > 1`` shape: the whole generation goes out through ``batch_fitness``
    (in production, to worker PROCESSES -- so the master's global RNG is untouched)."""
    return _make(seed, **kw).optimize(
        fitness_function=lambda p: 0.0,
        batch_fitness=lambda param_dicts: [fitness(p) for p in param_dicts],
    )


def _reseeding(fitness):
    """Wrap a fitness function so it clobbers the global RNG exactly like the daily engine."""
    def f(params):
        random.seed(ENGINE_SEED)
        return fitness(params)
    return f


def _signature(result):
    """Everything about a run that must not move: the winner and the whole trajectory."""
    return (result["best_fitness"],
            tuple(sorted(result["best_params"].items(), key=lambda kv: kv[0])),
            tuple(h["best_fitness"] for h in result["history"]))


# ---------------------------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------------------------

def test_in_process_and_batched_paths_agree():
    """parallel=1 (in-process map) and parallel>1 (batched) must find the SAME optimum.

    This is F4 in miniature: identical seed, identical gene space, identical fitness -- only the
    evaluation path differs, exactly as ``--parallel`` makes it differ in production.
    """
    assert (_signature(_run_in_process(_reseeding(_score)))
            == _signature(_run_batched(_score)))


def test_a_fitness_function_that_reseeds_the_global_rng_cannot_move_the_search():
    """``DailyBacktestEngine.run()`` opens with ``random.seed(self.seed)``; in-process that used
    to reset the GA's stream after every trial."""
    assert (_signature(_run_in_process(_score))
            == _signature(_run_in_process(_reseeding(_score))))


def test_a_fitness_function_that_merely_consumes_the_global_rng_cannot_move_the_search():
    """Not just reseeding: any consumption used to shift the GA's draws. A fitness function that
    burns a genome-DEPENDENT number of draws is the general form of the bug -- and is what an
    in-process backtest really does."""
    def greedy(params):
        for _ in range(int(params["a"]) % 7 + 1):
            random.random()
        return _score(params)

    assert _signature(_run_in_process(_score)) == _signature(_run_in_process(greedy))


def test_the_search_consumes_no_draws_from_the_global_rng():
    """The other half of the isolation: the GA must not perturb the process RNG either, so a
    fitness function's own reproducibility never depends on how many draws the search made.

    Deliberately does NOT go through ``_make`` -- that reseeds, which would hide the effect.
    """
    random.seed(7)
    baseline = [random.random() for _ in range(5)]

    random.seed(7)
    opt = GeneticOptimizer(param_ranges=SPACE, population_size=12, n_generations=5,
                           crossover_prob=0.7, mutation_prob=0.3,
                           early_stopping_generations=99, elitism_percent=10.0)
    opt.optimize(fitness_function=_score)
    assert [random.random() for _ in range(5)] == baseline


def test_no_ga_operator_draws_from_the_module_level_rng(monkeypatch):
    """Structural guard: break every module-level draw and the GA must still run.

    This is the test that fails LOUDLY if someone re-registers ``tools.cxTwoPoint`` /
    ``tools.selTournament`` (both draw from the module) or writes a bare ``random.random()`` back
    into an operator. ``getstate``/``setstate`` stay real -- the private RNG is seeded from them.
    """
    def boom(*_a, **_k):
        raise AssertionError("GA drew from the process-global random module")

    for name in ("random", "randint", "uniform", "gauss", "choice", "randrange",
                 "getrandbits", "sample", "shuffle", "normalvariate"):
        monkeypatch.setattr(random, name, boom)

    result = _run_batched(_score)
    assert result["best_fitness"] is not None
    assert len(result["history"]) == 5


# ---------------------------------------------------------------------------------------------
# Checkpoint/resume must carry the private generator, or a resumed run diverges
# ---------------------------------------------------------------------------------------------

def test_checkpoint_carries_the_ga_generator_state():
    opt = _make()
    cp = opt.get_checkpoint_data(0, opt.toolbox.population(n=4))
    assert "ga_random_state" in cp
    # Still JSON-safe (the checkpoint lives in a JSON column).
    import json
    json.loads(json.dumps(cp["ga_random_state"]))


def test_resume_restores_the_ga_generator():
    opt = _make()
    _ = [opt._rng.random() for _ in range(11)]      # advance it somewhere non-trivial
    import json
    cp = json.loads(json.dumps(opt.get_checkpoint_data(0, opt.toolbox.population(n=4))))
    expected = [opt._rng.random() for _ in range(5)]

    other = _make(seed=999)                          # a differently-positioned generator
    other.resume_from_checkpoint(cp)
    assert [other._rng.random() for _ in range(5)] == expected


def test_resume_from_a_legacy_checkpoint_falls_back_to_the_global_state():
    """Checkpoints written before ``ga_random_state`` existed are already in the database. Those
    runs drew from the global module, so continuing from the restored GLOBAL state is exactly
    what they would have done next."""
    import json
    from app.services.genetic import _py_state_to_jsonable

    random.seed(4242)
    legacy = json.loads(json.dumps({
        "generation": 1,
        "population": [[1, 0.5, 3, 0]],
        "history": [],
        "random_state": _py_state_to_jsonable(random.getstate()),
    }))
    expected = [random.random() for _ in range(5)]

    opt = _make(seed=1)
    start_gen, pop, fits = opt.resume_from_checkpoint(legacy)
    assert (start_gen, pop, fits) == (2, [[1, 0.5, 3, 0]], None)
    assert [opt._rng.random() for _ in range(5)] == expected


def test_resume_with_a_malformed_ga_state_warns_and_continues(caplog):
    import logging
    opt = _make()
    with caplog.at_level(logging.WARNING, logger="app.services.genetic"):
        opt.resume_from_checkpoint(
            {"generation": 0, "population": [], "ga_random_state": "not-a-state"})
    assert [r for r in caplog.records if "Could not restore GA random state" in r.message]
    opt._rng.random()          # still usable


# ---------------------------------------------------------------------------------------------
# The private generator must still honour the seed it is given
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 42, 12345])
def test_same_seed_reproduces_and_different_seeds_diverge(seed):
    assert _signature(_run_batched(_score, seed=seed)) == _signature(_run_batched(_score, seed=seed))
    assert _signature(_run_batched(_score, seed=seed)) != _signature(_run_batched(_score, seed=seed + 1))


def test_population_still_inherits_the_callers_seed():
    """The optimizer takes no seed argument -- it clones the global RNG at construction, which is
    how ``handle_strategy_optimization``'s ``random.seed(ga["seed"])`` still reaches it."""
    random.seed(31337)
    a = [list(_ind) for _ind in GeneticOptimizer(
        param_ranges=SPACE, population_size=6, n_generations=1).toolbox.population(n=6)]
    random.seed(31337)
    b = [list(_ind) for _ind in GeneticOptimizer(
        param_ranges=SPACE, population_size=6, n_generations=1).toolbox.population(n=6)]
    assert a == b
    random.seed(31338)
    c = [list(_ind) for _ind in GeneticOptimizer(
        param_ranges=SPACE, population_size=6, n_generations=1).toolbox.population(n=6)]
    assert a != c
