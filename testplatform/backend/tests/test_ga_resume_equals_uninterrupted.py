"""A resumed GA must finish exactly where the uninterrupted one does, whichever checkpoint it
resumes from: mid-generation (partial), the PARTIAL one written by the generation's LAST trial
(every fitness valid, generation not yet recorded), and the end-of-generation one.

The last case of a partial checkpoint was resumed as if it were an end-of-generation checkpoint:
``optimize()`` saw a fully evaluated population and ``history`` ending one generation earlier,
bred offspring from it and so dropped that generation's record (and its best/patience update).
"""
from __future__ import annotations

import json
import random

import numpy as np
import pytest

from app.services.genetic import GeneticOptimizer

pytest.importorskip("deap")

_RANGES = {"x": {"type": "int", "min": 0, "max": 20}, "y": {"type": "int", "min": 0, "max": 20},
           "z": {"type": "int", "min": 0, "max": 20}}


class _Killed(BaseException):
    pass


def _optimizer():
    return GeneticOptimizer(param_ranges=_RANGES, population_size=8, n_generations=5,
                            crossover_prob=0.7, mutation_prob=0.3, early_stopping_generations=99,
                            elitism_percent=25.0)


def _batch(dicts, on_result=None):
    fits = []
    for i, d in enumerate(dicts):
        f = float(-(d["x"] - 13) ** 2 - (d["y"] - 4) ** 2 + d["z"])
        fits.append(f)
        if on_result is not None:
            on_result(i, f)
    return fits


def _summary(res):
    return (res["best_params"], res["best_fitness"],
            [h["best_fitness"] for h in res["history"]],
            [h["generation"] for h in res["history"]],
            res["final_population"])


def _run(kill=None):
    """Seeded run; ``kill(gen, population, partial)`` True raises after that checkpoint is saved.
    Returns (summary or None, the last saved checkpoint as it would come back from the JSON column)."""
    random.seed(11)
    np.random.seed(11)
    opt = _optimizer()
    opt.partial_checkpoint_every = 1
    saved = {}

    def cb(gen, population, partial=False):
        d = opt.get_checkpoint_data(gen, population)
        d["partial"] = partial
        saved["ckpt"] = json.loads(json.dumps(d))
        if kill is not None and kill(gen, population, partial):
            raise _Killed()

    try:
        res = opt.optimize(fitness_function=None, batch_fitness=_batch, checkpoint_callback=cb)
    except _Killed:
        return None, saved["ckpt"]
    return _summary(res), saved["ckpt"]


def _resume(ckpt):
    random.seed(999)                # the resumed process starts from an unrelated global state
    np.random.seed(999)
    opt = _optimizer()
    opt.partial_checkpoint_every = 0
    start, pop, fits = opt.resume_from_checkpoint(ckpt)
    res = opt.optimize(fitness_function=None, batch_fitness=_batch, start_generation=start,
                       initial_population=pop, restored_fitnesses=fits)
    return _summary(res)


def _all_valid(population):
    return all(ind.fitness.valid for ind in population)


@pytest.mark.parametrize("kill", [
    pytest.param(lambda g, p, partial: g == 2 and partial and not _all_valid(p), id="mid-generation"),
    pytest.param(lambda g, p, partial: g == 2 and partial and _all_valid(p), id="partial-after-last-trial"),
    pytest.param(lambda g, p, partial: g == 2 and not partial, id="end-of-generation"),
])
def test_resumed_run_equals_the_uninterrupted_run(kill):
    full, _ = _run()
    cut, ckpt = _run(kill)
    assert cut is None, "the kill did not happen"
    assert _resume(ckpt) == full
