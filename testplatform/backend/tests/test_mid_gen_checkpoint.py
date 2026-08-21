"""Mid-generation checkpoint + resume."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.genetic import GeneticOptimizer  # noqa: E402


def _opt(**kw):
    """A tiny optimizer over two integer genes.

    TWO genes, not one: ``tools.cxTwoPoint`` does ``random.randint(1, size - 1)``, which on a
    length-1 individual is ``randrange(1, 1)`` -> ``ValueError: empty range``. Any test that runs
    ``optimize()`` across a generation boundary would die in crossover with a single gene.
    """
    ranges = {
        "x": {"type": "int", "min": 0, "max": 10},
        "y": {"type": "int", "min": 0, "max": 10},
    }
    kw.setdefault("population_size", 6)
    kw.setdefault("n_generations", 3)
    return GeneticOptimizer(param_ranges=ranges, **kw)


# ---------------------------------------------------------------------------------------------
# Task 1: persist and restore fitness values
# ---------------------------------------------------------------------------------------------

def test_checkpoint_carries_fitness_values():
    """Genes alone are not enough: a restored individual with no fitness is INVALID, so DEAP
    re-evaluates the entire population on every resume -- measured at ~3h of wasted compute per
    restart on the small cap band."""
    opt = _opt()
    pop = opt.toolbox.population(n=3)
    for i, ind in enumerate(pop):
        ind.fitness.values = (float(i),)

    data = opt.get_checkpoint_data(2, pop)

    assert "fitnesses" in data
    assert data["fitnesses"] == [0.0, 1.0, 2.0]


def test_unevaluated_individual_checkpoints_as_none():
    """The mid-generation half: an individual still waiting on a worker must serialise as None,
    not be silently dropped or defaulted."""
    opt = _opt()
    pop = opt.toolbox.population(n=3)
    pop[0].fitness.values = (1.5,)
    pop[2].fitness.values = (2.5,)

    assert opt.get_checkpoint_data(0, pop)["fitnesses"] == [1.5, None, 2.5]


def test_restored_population_keeps_its_fitness():
    """The other half: storing fitness is useless if optimize() rebuilds bare individuals."""
    opt = _opt()
    pop = opt.toolbox.population(n=3)
    for i, ind in enumerate(pop):
        ind.fitness.values = (float(i),)
    data = opt.get_checkpoint_data(2, pop)

    restored = opt.rebuild_population(data["population"], data["fitnesses"])

    assert [ind.fitness.valid for ind in restored] == [True, True, True]
    assert [ind.fitness.values[0] for ind in restored] == [0.0, 1.0, 2.0]


def test_unevaluated_individuals_restore_as_invalid():
    """A None must come back INVALID so DEAP re-runs it -- not as 0.0, which would silently
    poison the search with a fake score."""
    opt = _opt()
    restored = opt.rebuild_population([[1, 1], [2, 2], [3, 3]], [5.0, None, 7.0])
    assert [ind.fitness.valid for ind in restored] == [True, False, True]


def test_missing_fitness_list_restores_everything_invalid():
    """Backward compatibility: checkpoints written before this change have no 'fitnesses' key."""
    opt = _opt()
    restored = opt.rebuild_population([[1, 1], [2, 2]], None)
    assert [ind.fitness.valid for ind in restored] == [False, False]


def test_short_fitness_list_does_not_raise():
    """Defensive: a truncated/corrupt checkpoint must degrade to re-evaluating, never crash."""
    opt = _opt()
    restored = opt.rebuild_population([[1, 1], [2, 2], [3, 3]], [1.0])
    assert [ind.fitness.valid for ind in restored] == [True, False, False]


def test_unusable_fitness_value_does_not_raise():
    """A corrupt entry (a string, a dict) must leave that individual invalid, not kill the resume."""
    opt = _opt()
    restored = opt.rebuild_population([[1, 1], [2, 2]], ["not-a-number", 3.0])
    assert [ind.fitness.valid for ind in restored] == [False, True]


def test_resume_returns_the_fitness_list():
    """resume_from_checkpoint is the only route the handler has to the stored fitnesses."""
    opt = _opt()
    ckpt = {"generation": 4, "population": [[1, 1], [2, 2]], "fitnesses": [3.0, None]}
    start_gen, pop, fits = opt.resume_from_checkpoint(ckpt)
    assert start_gen == 5                     # non-partial: resume AFTER the stored generation
    assert pop == [[1, 1], [2, 2]]
    assert fits == [3.0, None]


def test_resume_of_a_legacy_checkpoint_returns_no_fitnesses():
    opt = _opt()
    start_gen, pop, fits = opt.resume_from_checkpoint({"generation": 1, "population": [[1, 1]]})
    assert (start_gen, pop, fits) == (2, [[1, 1]], None)


def test_optimize_does_not_rerun_restored_individuals():
    """End of Task 1's value: a plain (non-partial) resume must not re-evaluate the elites it
    just restored."""
    opt = _opt(n_generations=1)
    batches = []

    def batch_fitness(param_dicts):
        batches.append(len(param_dicts))
        return [1.0] * len(param_dicts)

    opt.optimize(
        fitness_function=lambda p: 0.0,
        batch_fitness=batch_fitness,
        start_generation=0,
        initial_population=[[1, 1], [2, 2], [3, 3], [4, 4]],
        restored_fitnesses=[9.0, 8.0, None, None],
    )
    assert batches == [2], f"only the 2 unevaluated individuals should run, got {batches}"


# ---------------------------------------------------------------------------------------------
# Task 2: report each trial result as it lands
#
# NOTE ON COVERAGE. The plan asked for a module-level ``_run_batch_with_progress(jobs, on_result)``
# extracted from ``batch_fitness``. That extraction is not honest: the result loop reads 25+
# closure names (memo, _cost_model, _governor, _pool, _evaluator, all_results, best, fatal,
# _persist_live, _emit_intra, _log_trial_memory, last_gen_full_results, _widths, ...) and a helper
# taking ``[("a", 1.0), ...]`` would be a fake seam that production code does not use. The plan
# anticipated this ("If the existing loop is too entangled to extract cleanly, add the on_result
# callback in place"). What IS extracted is the part with real behaviour worth pinning -- the
# best-effort reporting contract -- and the CALL SITE is guarded structurally, the same way
# tests/test_pool_recycle.py guards the recycle call site.
# ---------------------------------------------------------------------------------------------

def test_report_trial_result_forwards_index_and_fitness():
    from app.services import strategy_optimization_handler as H
    seen = []
    H._report_trial_result(lambda i, f: seen.append((i, f)), 3, 1.25)
    assert seen == [(3, 1.25)]


def test_report_trial_result_is_a_noop_without_a_callback():
    """batch_fitness is also driven by callers that pass no callback (brute force, the ML GA)."""
    from app.services import strategy_optimization_handler as H
    H._report_trial_result(None, 0, 1.0)          # must not raise


def test_report_trial_result_swallows_a_raising_callback():
    """A failing checkpoint write must never take down an otherwise healthy generation."""
    from app.services import strategy_optimization_handler as H

    def boom(i, f):
        raise RuntimeError("checkpoint disk full")

    H._report_trial_result(boom, 0, 1.0)          # must not raise


def _batch_fitness_source() -> str:
    src = (Path(__file__).resolve().parents[1]
           / "app" / "services" / "strategy_optimization_handler.py").read_text(encoding="utf-8")
    body = src[src.index("def make_batch_fitness"):]
    return body[:body.index("return batch_fitness")]


def test_batch_fitness_accepts_an_on_result_callback():
    assert "def batch_fitness(param_dicts: list, on_result=None)" in _batch_fitness_source()


def test_each_landed_trial_is_reported_inside_the_result_loop():
    """THE call site. Reporting after the loop would be worthless -- the whole point is that the
    GA can checkpoint a population mid-flight, which requires the fitness the MOMENT it lands."""
    body = _batch_fitness_source()
    loop = body[body.index("for i, flat, key, out in execute_jobs(jobs):"):]
    loop = loop[:loop.index("# RECYCLE, DISTRIBUTED PATH")]
    assert "_report_trial_result(on_result, i, fit)" in loop, (
        "the per-trial result is no longer handed to on_result inside the execute_jobs loop; "
        "without it a partial checkpoint records every in-flight individual as unevaluated")


def test_memo_hits_are_reported_too():
    """A cached individual IS evaluated. Leaving it out of the report would checkpoint it as a
    None and re-run it after a restart -- the in-process memo does not survive the restart."""
    body = _batch_fitness_source()
    memo_branch = body[body.index("cached = memo.get(key)"):]
    memo_branch = memo_branch[:memo_branch.index("config = _build_daily_trial_config")]
    assert "_report_trial_result(on_result, i, cached)" in memo_branch


# ---------------------------------------------------------------------------------------------
# Task 3: assign fitness incrementally in the generation loop
#
# NOTE. The plan's version of the first test asserts only ``assert snapshots``, but its
# ``snapshots.append(...)`` sits OUTSIDE the ``if on_result:`` guard -- so it is appended whether
# or not the GA passes a callback, and the test passes green against unmodified code. Rewritten
# to observe the thing that actually has to change: WHEN each individual's fitness is assigned.
# ---------------------------------------------------------------------------------------------

def test_fitness_is_assigned_as_results_arrive_not_after_the_batch():
    """The population must be checkpointable DURING evaluation, which means individuals get
    their fitness as results land rather than in one pass at the end."""
    opt = _opt(n_generations=1)

    # Capture the actual Individual objects the GA is about to evaluate: decode_individual is
    # called on each of them, in order, immediately before batch_fitness runs.
    inds: list = []
    real_decode = opt.decode_individual
    opt.decode_individual = lambda ind: (inds.append(ind), real_decode(ind))[1]

    observed: list = []
    evaluated: list = []

    def batch_fitness(param_dicts, on_result=None):
        assert on_result is not None, "the GA must pass on_result so trials can land early"
        # Freeze the batch here: decode_individual is called again AFTER the loop (best_params,
        # history), which would otherwise keep growing `inds`.
        evaluated[:] = list(inds)
        for i in range(len(param_dicts)):
            on_result(i, 100.0 + i)
            observed.append([ind.fitness.valid for ind in evaluated])
        return [100.0 + i for i in range(len(param_dicts))]

    opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness)

    assert observed, "batch_fitness was never called"
    n = len(evaluated)
    assert n == 6
    assert observed[0] == [True] + [False] * (n - 1), (
        "after the FIRST trial lands, exactly one individual may be valid -- if the whole "
        "population flips at once, fitness is still being assigned after the batch")
    assert all(observed[-1]), "every individual must be valid once the batch is done"
    assert [ind.fitness.values[0] for ind in evaluated] == [100.0 + i for i in range(n)]


def test_a_bad_index_from_on_result_does_not_kill_the_run():
    """Defensive: a backend that reports a nonsense index must lose that one report, not the job."""
    opt = _opt(n_generations=1)

    def batch_fitness(param_dicts, on_result=None):
        on_result(999, 1.0)               # out of range
        on_result(0, "not-a-number")      # unusable value
        return [1.0] * len(param_dicts)

    res = opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness)
    assert res["best_fitness"] == 1.0


# ---------------------------------------------------------------------------------------------
# Task 4: write partial checkpoints, throttled
# ---------------------------------------------------------------------------------------------

def test_partial_checkpoints_are_throttled():
    """One write per completed trial would be wasteful; one per N bounds the loss instead."""
    opt = _opt()
    opt.partial_checkpoint_every = 3
    calls = []
    pop = opt.toolbox.population(n=6)
    for _ in range(7):
        opt._maybe_partial_checkpoint(2, pop, lambda g, p, partial=False: calls.append(partial))
    assert calls == [True, True], "7 results at every-3 -> 2 writes"


def test_partial_checkpoint_is_flagged_partial():
    opt = _opt()
    opt.partial_checkpoint_every = 1
    calls = []
    opt._maybe_partial_checkpoint(2, opt.toolbox.population(n=2),
                                  lambda g, p, partial=False: calls.append(partial))
    assert calls == [True]


def test_partial_checkpoint_passes_the_generation_and_population_through():
    opt = _opt()
    opt.partial_checkpoint_every = 1
    seen = []
    pop = opt.toolbox.population(n=2)
    opt._maybe_partial_checkpoint(5, pop, lambda g, p, partial=False: seen.append((g, p)))
    assert seen == [(5, pop)]


def test_partial_checkpointing_can_be_disabled():
    """0 disables it -- an escape hatch if a run ever needs the old behaviour."""
    opt = _opt()
    opt.partial_checkpoint_every = 0
    calls = []
    for _ in range(10):
        opt._maybe_partial_checkpoint(2, [], lambda g, p, partial=False: calls.append(partial))
    assert calls == []


def test_a_failing_partial_checkpoint_never_fails_the_generation():
    """A full disk or a locked task DB must cost a checkpoint, not several hours of compute."""
    opt = _opt()
    opt.partial_checkpoint_every = 1

    def boom(g, p, partial=False):
        raise OSError("no space left on device")

    opt._maybe_partial_checkpoint(1, [], boom)      # must not raise


def test_the_partial_counter_resets_between_generations():
    """Otherwise a generation would inherit the previous one's phase and the first write would
    land at an arbitrary offset."""
    opt = _opt(n_generations=2)
    opt.partial_checkpoint_every = 100          # high enough that nothing is ever written
    counters = []

    def batch_fitness(param_dicts, on_result=None):
        counters.append(opt._partial_counter)   # the phase this generation starts from
        for i in range(len(param_dicts)):
            on_result(i, float(i))
        return [float(i) for i in range(len(param_dicts))]

    opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness,
                 checkpoint_callback=lambda g, p, partial=False: None)

    assert len(counters) == 2
    assert counters == [0, 0], "generation 2 inherited generation 1's throttle phase"


def test_partial_checkpoint_defaults_to_every_10():
    opt = _opt()
    assert opt.partial_checkpoint_every == 10


def test_partial_checkpoint_interval_is_env_overridable(monkeypatch):
    monkeypatch.setenv("BT_PARTIAL_CHECKPOINT_EVERY", "25")
    assert _opt().partial_checkpoint_every == 25


def test_a_garbage_env_value_falls_back_to_the_default(monkeypatch):
    """A typo in the environment must not make every GeneticOptimizer construction raise."""
    monkeypatch.setenv("BT_PARTIAL_CHECKPOINT_EVERY", "ten")
    assert _opt().partial_checkpoint_every == 10


def test_the_generation_end_checkpoint_is_not_partial():
    """The boundary write must CLEAR the partial flag, or a completed generation would be
    resumed into as if it were still running."""
    opt = _opt(n_generations=1)
    calls = []
    opt.optimize(fitness_function=lambda p: 0.0,
                 checkpoint_callback=lambda g, p, partial=False: calls.append(partial))
    assert calls == [False]


# ---------------------------------------------------------------------------------------------
# Task 5: the exhausted-checkpoint guard must respect `partial`
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gen,partial,n_gens,expect_exhausted", [
    (7, False, 8, True),    # finished the last generation -> nothing left
    (7, True,  8, False),   # INTERRUPTED during the last generation -> work remains
    (3, False, 8, False),
    (3, True,  8, False),
    (8, True,  8, True),    # partial past the end (a shrunken --generations) -> nothing left
    (9, False, 8, True),
])
def test_exhausted_guard_respects_partial(gen, partial, n_gens, expect_exhausted):
    from app.services.strategy_optimization_handler import _checkpoint_exhausted
    assert _checkpoint_exhausted({"generation": gen, "partial": partial}, n_gens) is expect_exhausted


def test_no_checkpoint_is_not_exhausted():
    """None/{} means 'nothing saved', which is a fresh start -- not a finished search. The old
    inline guard short-circuited on this and the extraction must keep doing so."""
    from app.services.strategy_optimization_handler import _checkpoint_exhausted
    assert _checkpoint_exhausted(None, 8) is False
    assert _checkpoint_exhausted({}, 8) is False


def test_a_checkpoint_without_a_partial_key_is_treated_as_complete():
    """Every checkpoint written before this feature lacks the key; they are all generation-end
    writes, so the old gen+1 >= n rule is the correct reading."""
    from app.services.strategy_optimization_handler import _checkpoint_exhausted
    assert _checkpoint_exhausted({"generation": 7}, 8) is True
    assert _checkpoint_exhausted({"generation": 6}, 8) is False


def test_the_handler_uses_the_helper_for_its_exhausted_guard():
    """The guard is inline in handle_strategy_optimization; if the extraction is not actually
    wired up, every test above passes while the live path keeps discarding partials."""
    src = (Path(__file__).resolve().parents[1]
           / "app" / "services" / "strategy_optimization_handler.py").read_text(encoding="utf-8")
    assert 'if _checkpoint_exhausted(ckpt, ga["generations"]):' in src
    assert 'int(ckpt.get("generation", -1)) + 1 >= int(ga["generations"])' not in src


def test_a_batch_fitness_taking_kwargs_is_given_on_result():
    """A **kwargs evaluator can take it -- do not silently downgrade it to the legacy call."""
    opt = _opt(n_generations=1)
    got = {}

    def batch_fitness(param_dicts, **kw):
        got["on_result"] = kw.get("on_result")
        return [1.0] * len(param_dicts)

    opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness)
    assert callable(got["on_result"])


def test_a_batch_fitness_that_predates_on_result_still_works():
    """Backward compatibility: brute force and the ML GA pass a 1-argument batch_fitness."""
    opt = _opt(n_generations=1)

    def batch_fitness(param_dicts):
        return [1.0] * len(param_dicts)

    res = opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness)
    assert res["best_fitness"] == 1.0
