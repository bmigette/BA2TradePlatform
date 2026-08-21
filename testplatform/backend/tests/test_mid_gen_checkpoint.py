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
