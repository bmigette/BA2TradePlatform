"""Probe: does the GA's result depend on WHERE the fitness is evaluated?

Mechanism under test (F4). ``GeneticOptimizer`` draws every one of its own random numbers
(initial population, tournament selection, crossover points, mutation gates/gaussians) from the
PROCESS-GLOBAL ``random`` module. Whether the fitness evaluation shares that module is decided
purely by ``--parallel``:

  parallel <= 1 (no workers)  -> ``_dispatch_engages`` is False, ``batch_fitness`` is None, and
                                 genetic.optimize() runs ``fitness_function`` IN THIS PROCESS.
                                 ``DailyBacktestEngine.run()`` opens with ``random.seed(self.seed)``
                                 (daily_engine.py:481), so every trial RESETS the GA's own RNG.
  parallel  > 1               -> each trial runs in a spawned worker PROCESS; the master's
                                 ``random`` module is never touched.

This script reproduces that WITHOUT a backtest: the only difference between the two runs is
whether the fitness function reseeds the global RNG the way the engine does.

Run:  ./venv/bin/python test_files/probe_ga_parallel_determinism.py
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "testplatform", "backend"))

from app.services.genetic import GeneticOptimizer  # noqa: E402

SPACE = {
    "a": {"type": "int", "min": 0, "max": 100, "step": 1},
    "b": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.01},
    "c": {"type": "int", "min": 1, "max": 20, "step": 1},
}


def _fitness(params):
    """Deterministic function of the genome only -- no randomness of its own."""
    return params["a"] * 0.5 - abs(params["b"] - 0.3) * 100 + params["c"]


def _run(reseeds_global_rng: bool, seed: int = 42) -> float:
    random.seed(seed)
    opt = GeneticOptimizer(param_ranges=SPACE, population_size=16, n_generations=6,
                           crossover_prob=0.7, mutation_prob=0.2,
                           early_stopping_generations=99, elitism_percent=10.0)

    def f(params):
        if reseeds_global_rng:
            # EXACTLY what DailyBacktestEngine.run() does at daily_engine.py:481.
            random.seed(1234)
        return _fitness(params)

    return opt.optimize(fitness_function=f)["best_fitness"]


def _run_batch(seed: int = 42) -> float:
    """The parallel>1 shape: fitness goes through batch_fitness, i.e. out-of-process. The
    reseed happens in the worker, so the master's RNG is untouched -- modelled here by simply
    not touching it."""
    random.seed(seed)
    opt = GeneticOptimizer(param_ranges=SPACE, population_size=16, n_generations=6,
                           crossover_prob=0.7, mutation_prob=0.2,
                           early_stopping_generations=99, elitism_percent=10.0)
    return opt.optimize(fitness_function=lambda p: 0.0,
                        batch_fitness=lambda pds: [_fitness(p) for p in pds])["best_fitness"]


if __name__ == "__main__":
    clean = _run(reseeds_global_rng=False)
    dirty = _run(reseeds_global_rng=True)
    batch = _run_batch()
    print(f"in-process, fitness leaves global RNG alone : {clean}")
    print(f"in-process, fitness reseeds global RNG      : {dirty}   <- the parallel<=1 path")
    print(f"batch (out-of-process) path                 : {batch}   <- the parallel>1 path")
    print()
    print("SAME  " if clean == batch else "DIFFER", "clean vs batch")
    print("SAME  " if dirty == batch else "DIFFER", "dirty vs batch   (this is bug F4)")
