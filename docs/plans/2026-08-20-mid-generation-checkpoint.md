# Mid-Generation Checkpoint + Resume Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let an interrupted GA optimization resume *inside* the generation it was running, instead of discarding every trial completed since the last generation boundary.

**Architecture:** DEAP already re-evaluates only individuals whose fitness is invalid (`genetic.py:440`). So the whole feature reduces to (a) persisting fitness values in the checkpoint, (b) assigning fitness incrementally as trials land instead of after the whole batch, (c) writing a checkpoint periodically mid-generation, and (d) resuming into that generation rather than the next one. No change to selection, crossover, mutation, or elitism.

**Tech Stack:** Python 3.11, DEAP, pytest. Files live under `testplatform/backend/`.

---

## Background — read this before writing code

Two measured facts from 2026-08-19/20 motivate this work.

**1. Restarts are routine and expensive.** Four grid restarts happened in two days. Small-band trials run 200-6400s each; a generation is 99-140 individuals. Each restart discarded every individual completed in the in-flight generation — on the last one, 39 individuals, several hours of compute.

**2. Fitness is not persisted today, so *every* resume re-runs a full generation.** `get_checkpoint_data` (`genetic.py:351-370`) stores:

```python
'population': [list(ind) for ind in population],
```

— genes only. On resume, `optimize()` rebuilds them as bare individuals (`genetic.py:400`):

```python
population = [creator.Individual(ind) for ind in initial_population]
```

Fresh `creator.Individual`s have **invalid fitness**, so `invalid_ind` is the entire population and the whole generation is re-evaluated. An uninterrupted run at the same point would only evaluate the offspring (elites keep their fitness). At ~600s/trial over 8 slots, that is roughly 3 wasted hours on every single resume, independent of mid-generation support.

**Task 1 fixes #2 and is worth shipping on its own.** Tasks 2-6 build #1 on top of it.

### Why this is safe

- **Results are order-blind.** Trials are dispatched LPT-ordered and results are assigned by their original index; `testplatform/backend/tests/test_lpt_dispatch.py::test_reordering_cannot_change_the_assembled_fitness_list` pins this. Re-dispatching the remainder of a generation in a different order cannot change any fitness value.
- **The RNG has not advanced mid-generation.** Selection, crossover and mutation all run at the *end* of the loop body (`genetic.py:503-530`), after the checkpoint call. A checkpoint taken during evaluation sees exactly the RNG state the generation started with.
- **The fingerprint guard already exists.** `checkpoint_fingerprint` (gene space + populationSize + generations) is written into every checkpoint and refuses a resume into a changed search.

### Where the code lives

| Thing | Location |
|---|---|
| Generation loop | `testplatform/backend/app/services/genetic.py:432-540` |
| `invalid_ind` filter | `genetic.py:440` |
| Batch evaluation call | `genetic.py:448-451` |
| Fitness assignment | `genetic.py:459-461` |
| `checkpoint_callback` call | `genetic.py:483-484` |
| `get_checkpoint_data` | `genetic.py:351-370` |
| `resume_from_checkpoint` | `genetic.py:319-349` |
| `checkpoint_cb` (writer) | `strategy_optimization_handler.py:1069-1072` |
| Resume site + exhausted guard | `strategy_optimization_handler.py:1320-1340` |
| `batch_fitness` implementation | `strategy_optimization_handler.py:~1090` onward |
| `execute_jobs` generator (yields per trial) | `distributed_eval.py:~600-651` |

### Running the tests

From `testplatform/backend/`:

```bash
../../.venv/Scripts/python.exe -m pytest tests/test_mid_gen_checkpoint.py -v
```

Full backend suite (must stay green — 1382 passing as of this plan):

```bash
../../.venv/Scripts/python.exe -m pytest tests/ -q
```

---

## Task 1: Persist and restore fitness values

Ships standalone value: removes a full redundant generation from every resume.

**Files:**
- Modify: `testplatform/backend/app/services/genetic.py:351-370` (`get_checkpoint_data`)
- Modify: `testplatform/backend/app/services/genetic.py:398-401` (population restore in `optimize`)
- Test: `testplatform/backend/tests/test_mid_gen_checkpoint.py` (create)

**Step 1: Write the failing test**

```python
"""Mid-generation checkpoint + resume."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.genetic import GeneticOptimizer  # noqa: E402


def _opt(**kw):
    """A tiny optimizer over one integer gene."""
    ranges = {"x": {"type": "int", "min": 0, "max": 10}}
    return GeneticOptimizer(param_ranges=ranges, population_size=6, n_generations=3, **kw)


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
```

**Step 2: Run it and watch it fail**

```bash
cd testplatform/backend
../../.venv/Scripts/python.exe -m pytest tests/test_mid_gen_checkpoint.py::test_checkpoint_carries_fitness_values -v
```

Expected: `FAIL` — `KeyError: 'fitnesses'` / `assert 'fitnesses' in data`.

**Step 3: Implement**

In `genetic.py`, replace the return of `get_checkpoint_data`:

```python
        return {
            'generation': generation,
            'population': [list(ind) for ind in population],
            # Fitness per individual, index-aligned with 'population'. None where the individual
            # has not been evaluated -- which is what makes a MID-GENERATION checkpoint possible:
            # on resume, DEAP's invalid-fitness filter re-runs exactly the Nones.
            'fitnesses': [
                ind.fitness.values[0] if ind.fitness.valid else None
                for ind in population
            ],
            'best_individual': list(self.best_individual) if self.best_individual else None,
            'best_fitness': self.best_fitness,
            'history': self.history,
            'random_state': list(random.getstate()),
            'np_random_state': _np_state_to_jsonable(np.random.get_state()),
        }
```

**Step 4: Run the test**

Expected: `PASS`.

**Step 5: Write the restore test**

```python
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
    restored = opt.rebuild_population([[1], [2], [3]], [5.0, None, 7.0])
    assert [ind.fitness.valid for ind in restored] == [True, False, True]


def test_missing_fitness_list_restores_everything_invalid():
    """Backward compatibility: checkpoints written before this change have no 'fitnesses' key."""
    opt = _opt()
    restored = opt.rebuild_population([[1], [2]], None)
    assert [ind.fitness.valid for ind in restored] == [False, False]


def test_short_fitness_list_does_not_raise():
    """Defensive: a truncated/corrupt checkpoint must degrade to re-evaluating, never crash."""
    opt = _opt()
    restored = opt.rebuild_population([[1], [2], [3]], [1.0])
    assert [ind.fitness.valid for ind in restored] == [True, False, False]
```

**Step 6: Run them and watch them fail**

Expected: `FAIL` — `AttributeError: 'GeneticOptimizer' object has no attribute 'rebuild_population'`.

**Step 7: Implement `rebuild_population`**

Add to `GeneticOptimizer` in `genetic.py`, just above `get_checkpoint_data`:

```python
    def rebuild_population(self, genes: list, fitnesses: list = None) -> list:
        """Rebuild a DEAP population from checkpointed genes + fitness values.

        A ``None`` fitness (or a missing/short list) leaves that individual INVALID, which is
        precisely the signal DEAP's ``invalid_ind`` filter acts on -- so an unevaluated
        individual is re-run and an evaluated one is not. Never raises on a malformed list:
        the worst outcome must be re-evaluating something, never crashing a resume.
        """
        population = []
        for i, g in enumerate(genes):
            ind = creator.Individual(g)
            fit = None
            if fitnesses is not None and i < len(fitnesses):
                fit = fitnesses[i]
            if fit is not None:
                try:
                    ind.fitness.values = (float(fit),)
                except (TypeError, ValueError):
                    pass          # unusable value -> leave invalid, it will be re-evaluated
            population.append(ind)
        return population
```

**Step 8: Run them**

Expected: all `PASS`.

**Step 9: Wire it into `optimize()`**

In `genetic.py`, replace the restore block (currently `genetic.py:399-401`):

```python
        # Create or restore population
        if initial_population:
            population = self.rebuild_population(initial_population, restored_fitnesses)
            n_valid = sum(1 for ind in population if ind.fitness.valid)
            logger.info(
                f"Restored population of {len(population)} individuals "
                f"({n_valid} already evaluated, {len(population) - n_valid} to run)")
        else:
            population = self.toolbox.population(n=self.population_size)
```

and add `restored_fitnesses: list = None` to the `optimize()` signature (after `initial_population`).

**Step 10: Make `resume_from_checkpoint` return the fitnesses**

Replace `genetic.py:349`:

```python
        logger.info(f"Resuming from generation {checkpoint.get('generation', 0)}")
        # partial=True means the stored generation was INTERRUPTED mid-evaluation, so resume INTO
        # it rather than after it. The fitness list carries which individuals are already done.
        next_gen = checkpoint.get('generation', 0)
        if not checkpoint.get('partial'):
            next_gen += 1
        return next_gen, checkpoint.get('population', []), checkpoint.get('fitnesses')
```

**Step 11: Update the one caller**

`strategy_optimization_handler.py:1333` currently unpacks two values:

```python
            start_gen, init_pop = optimizer.resume_from_checkpoint(ckpt)
```

becomes:

```python
            start_gen, init_pop, init_fits = optimizer.resume_from_checkpoint(ckpt)
```

and the `optimizer.optimize(...)` call (`strategy_optimization_handler.py:~1470`) gains `restored_fitnesses=init_fits`.

Check for other callers first:

```bash
grep -rn "resume_from_checkpoint" --include=*.py testplatform/
```

`job_handler.py:3259` also calls it — update that unpack too, passing `restored_fitnesses` through to its `optimize()` call.

**Step 12: Run the full suite**

```bash
../../.venv/Scripts/python.exe -m pytest tests/ -q
```

Expected: `1382 passed` (or higher), 0 failed.

**Step 13: Commit**

```bash
git add testplatform/backend/app/services/genetic.py \
        testplatform/backend/app/services/strategy_optimization_handler.py \
        testplatform/backend/app/services/job_handler.py \
        testplatform/backend/tests/test_mid_gen_checkpoint.py
git commit -m "fix(ga): persist fitness in checkpoints so a resume does not re-run a whole generation"
```

---

## Task 2: Report each trial result as it lands

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py` (the `batch_fitness` closure, ~line 1090 onward)
- Test: `testplatform/backend/tests/test_mid_gen_checkpoint.py`

**Step 1: Write the failing test**

```python
def test_batch_fitness_reports_results_incrementally(monkeypatch):
    """execute_jobs already YIELDS per trial; batch_fitness collapses that into a list before the
    GA sees it. Exposing the stream is what makes a mid-generation checkpoint possible."""
    from app.services import strategy_optimization_handler as H

    seen = []
    fits = H._run_batch_with_progress(              # thin seam, see implementation
        jobs=[("a", 1.0), ("b", 2.0), ("c", 3.0)],
        on_result=lambda idx, fit: seen.append((idx, fit)),
    )
    assert fits == [1.0, 2.0, 3.0]
    assert sorted(seen) == [(0, 1.0), (1, 2.0), (2, 3.0)]
```

> **Note for the implementer:** `batch_fitness` is a closure with heavy dependencies (broker,
> evaluator, memo, DB). Do **not** try to unit-test it whole. Extract the result-assembly loop
> into a module-level helper `_run_batch_with_progress(jobs, on_result)` and test that; leave the
> closure calling it. If the existing loop is too entangled to extract cleanly, add the
> `on_result` callback in place and cover it with Task 6's integration test instead — but say so
> in the commit message rather than leaving a silently untested path.

**Step 2: Run it and watch it fail.** Expected: `AttributeError: _run_batch_with_progress`.

**Step 3: Implement.** Thread an optional `on_result: Callable[[int, float], None] = None` through
`batch_fitness`, and call it inside the existing `for (i, flat, key, out) in evaluator.execute_jobs(jobs)`
loop the moment a fitness is computed — *before* the loop moves on. Keep it best-effort:

```python
            if on_result is not None:
                try:
                    on_result(i, fitness)
                except Exception as e:       # noqa: BLE001 -- progress reporting must never
                    logger.warning(f"on_result callback failed (ignored): {e}")
```

**Step 4: Run the test.** Expected: `PASS`.

**Step 5: Commit**

```bash
git commit -am "feat(ga): batch_fitness reports each trial result as it lands"
```

---

## Task 3: Assign fitness incrementally in the generation loop

**Files:**
- Modify: `testplatform/backend/app/services/genetic.py:448-461`
- Test: `testplatform/backend/tests/test_mid_gen_checkpoint.py`

**Step 1: Write the failing test**

```python
def test_fitness_is_assigned_as_results_arrive_not_after_the_batch():
    """The population must be checkpointable DURING evaluation, which means individuals get
    their fitness as results land rather than in one pass at the end."""
    opt = _opt()
    snapshots = []

    def batch_fitness(param_dicts, on_result=None):
        for i, _ in enumerate(param_dicts):
            if on_result:
                on_result(i, float(i))
            snapshots.append(len(snapshots))
        return [float(i) for i in range(len(param_dicts))]

    opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness,
                 checkpoint_callback=lambda gen, pop, partial=False: None)
    assert snapshots, "batch_fitness must have been called with an on_result callback"
```

**Step 2: Run it and watch it fail** (the current call passes only `param_dicts`).

**Step 3: Implement.** Replace `genetic.py:448-451`:

```python
            if batch_fitness is not None:
                param_dicts = [self.decode_individual(ind) for ind in invalid_ind]

                def _on_result(idx: int, fit: float, _inv=invalid_ind, _gen=gen):
                    """Assign fitness the moment a trial lands so the population is
                    checkpointable mid-generation. Guarded: a bad index must not kill the run."""
                    try:
                        _inv[idx].fitness.values = (float(fit),)
                    except (IndexError, TypeError, ValueError):
                        return
                    if checkpoint_callback:
                        self._maybe_partial_checkpoint(_gen, population, checkpoint_callback)

                fits = batch_fitness(param_dicts, on_result=_on_result) if param_dicts else []
                fitnesses = [(float(f),) for f in fits]
```

The `for ind, fit in zip(invalid_ind, fitnesses)` block at `genetic.py:459-461` stays: it is
idempotent (re-assigning the same value) and remains the authority for the non-batch paths.

> **Backward compatibility:** `batch_fitness` is also supplied by callers that do not accept
> `on_result`. Call it defensively:
> ```python
> import inspect
> _accepts = "on_result" in inspect.signature(batch_fitness).parameters
> fits = (batch_fitness(param_dicts, on_result=_on_result) if _accepts
>         else batch_fitness(param_dicts))
> ```

**Step 4: Run the test.** Expected: `PASS`.

**Step 5: Commit**

```bash
git commit -am "feat(ga): assign fitness incrementally so a generation is checkpointable mid-flight"
```

---

## Task 4: Write partial checkpoints, throttled

**Files:**
- Modify: `testplatform/backend/app/services/genetic.py` (add `_maybe_partial_checkpoint`)
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py:1069-1072` (`checkpoint_cb`)
- Test: `testplatform/backend/tests/test_mid_gen_checkpoint.py`

**Step 1: Write the failing tests**

```python
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


def test_partial_checkpointing_can_be_disabled():
    """0 disables it -- an escape hatch if a run ever needs the old behaviour."""
    opt = _opt()
    opt.partial_checkpoint_every = 0
    calls = []
    for _ in range(10):
        opt._maybe_partial_checkpoint(2, [], lambda g, p, partial=False: calls.append(partial))
    assert calls == []
```

**Step 2: Run and watch fail.**

**Step 3: Implement.** In `GeneticOptimizer.__init__`, add:

```python
        # Write a partial checkpoint every N completed trials within a generation. Bounds what a
        # restart loses to N trials instead of a whole generation. 0 disables.
        self.partial_checkpoint_every = int(os.getenv("BT_PARTIAL_CHECKPOINT_EVERY", "10"))
        self._partial_counter = 0
```

and the method:

```python
    def _maybe_partial_checkpoint(self, gen: int, population: list, checkpoint_callback) -> None:
        """Checkpoint mid-generation every ``partial_checkpoint_every`` completed trials.

        Best-effort: a checkpoint write must never fail an otherwise healthy generation.
        """
        if not self.partial_checkpoint_every:
            return
        self._partial_counter += 1
        if self._partial_counter % self.partial_checkpoint_every:
            return
        try:
            checkpoint_callback(gen, population, partial=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"partial checkpoint failed (ignored): {e}")
```

Reset `self._partial_counter = 0` at the top of each generation, right after `invalid_ind` is computed.

**Step 4: Update the writer.** `strategy_optimization_handler.py:1069`:

```python
        def checkpoint_cb(generation: int, population: list, partial: bool = False):
            data = optimizer.get_checkpoint_data(generation, population)
            data["fingerprint"] = ckpt_fingerprint   # refuse to resume into a changed gene space
            data["partial"] = partial               # resume INTO this generation, not after it
            _save_checkpoint(ckpt_task_id, data)
```

Do the same for `job_handler.py:3226`.

**Step 5: Run the tests.** Expected: `PASS`.

**Step 6: Commit**

```bash
git commit -am "feat(ga): write throttled partial checkpoints during a generation"
```

---

## Task 5: Make the exhausted-checkpoint guard partial-aware

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py:1326`
- Test: `testplatform/backend/tests/test_mid_gen_checkpoint.py`

The guard currently discards any checkpoint where `generation + 1 >= n_generations`. A *partial*
checkpoint on the final generation still has work left, and would be wrongly thrown away.

**Step 1: Write the failing test**

```python
@pytest.mark.parametrize("gen,partial,n_gens,expect_exhausted", [
    (7, False, 8, True),    # finished the last generation -> nothing left
    (7, True,  8, False),   # INTERRUPTED during the last generation -> work remains
    (3, False, 8, False),
    (3, True,  8, False),
])
def test_exhausted_guard_respects_partial(gen, partial, n_gens, expect_exhausted):
    from app.services.strategy_optimization_handler import _checkpoint_exhausted
    assert _checkpoint_exhausted({"generation": gen, "partial": partial}, n_gens) is expect_exhausted
```

**Step 2: Run and watch fail** (`ImportError: cannot import name '_checkpoint_exhausted'`).

**Step 3: Implement.** Add near `checkpoint_fingerprint` in `strategy_optimization_handler.py`:

```python
def _checkpoint_exhausted(ckpt: dict, n_generations: int) -> bool:
    """True when a checkpoint has no work left in it.

    A checkpoint written AFTER the final generation is exhausted. A PARTIAL one written DURING
    the final generation is not -- it still has unevaluated individuals, and discarding it would
    throw away exactly the work partial checkpointing exists to save.
    """
    if not ckpt:
        return False
    gen = int(ckpt.get("generation", -1))
    if ckpt.get("partial"):
        return gen >= int(n_generations)
    return gen + 1 >= int(n_generations)
```

Then replace the inline condition at `strategy_optimization_handler.py:1326`:

```python
        if _checkpoint_exhausted(ckpt, ga["generations"]):
```

**Step 4: Run the tests.** Expected: `PASS`.

**Step 5: Commit**

```bash
git commit -am "fix(ga): do not discard a partial checkpoint on the final generation"
```

---

## Task 6: End-to-end interrupt-and-resume test

The one that proves the feature. Everything above is machinery.

**Files:**
- Test: `testplatform/backend/tests/test_mid_gen_checkpoint.py`

**Step 1: Write the test**

```python
def test_interrupted_generation_resumes_without_rerunning_finished_trials():
    """THE POINT OF THIS FEATURE. Interrupt a generation after 4 of 6 individuals, resume, and
    assert the 4 are NOT re-evaluated. Before this work the resume re-ran all 6 -- measured at
    ~3h of wasted compute per restart on the small cap band."""
    opt = _opt()
    opt.partial_checkpoint_every = 1
    saved = {}

    class _Stop(Exception):
        pass

    evaluated_first = []

    def batch_fitness(param_dicts, on_result=None):
        for i in range(len(param_dicts)):
            if i >= 4:
                raise _Stop()               # die mid-generation, like a killed grid master
            evaluated_first.append(i)
            on_result(i, float(i))
        return [float(i) for i in range(len(param_dicts))]

    def checkpoint_cb(gen, pop, partial=False):
        saved.update(opt.get_checkpoint_data(gen, pop))
        saved["partial"] = partial

    with pytest.raises(_Stop):
        opt.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness,
                     checkpoint_callback=checkpoint_cb)

    assert saved.get("partial") is True
    assert sum(1 for f in saved["fitnesses"] if f is not None) == 4

    # --- resume ---
    opt2 = _opt()
    start_gen, pop, fits = opt2.resume_from_checkpoint(saved)
    assert start_gen == saved["generation"], "must resume INTO the generation, not after it"

    evaluated_second = []

    def batch_fitness2(param_dicts, on_result=None):
        for i in range(len(param_dicts)):
            evaluated_second.append(i)
            on_result(i, 9.0)
        return [9.0] * len(param_dicts)

    opt2.optimize(fitness_function=lambda p: 0.0, batch_fitness=batch_fitness2,
                  start_generation=start_gen, initial_population=pop, restored_fitnesses=fits,
                  checkpoint_callback=lambda g, p, partial=False: None)

    assert len(evaluated_second) == 2, \
        f"only the 2 unfinished individuals should re-run, got {len(evaluated_second)}"
```

**Step 2: Run it**

```bash
../../.venv/Scripts/python.exe -m pytest tests/test_mid_gen_checkpoint.py -v
```

Expected: `PASS`. If it fails with `len(evaluated_second) == 6`, fitness is not surviving the
round-trip — go back to Task 1.

**Step 3: Run the full suite**

Expected: `1382 passed` or higher, 0 failed.

**Step 4: Commit**

```bash
git commit -am "test(ga): end-to-end mid-generation interrupt and resume"
```

---

## Task 7: Verify against the live grid

**Not a code change** — the real acceptance test.

**Step 1:** Bump `ba2_trade_platform/version.py` (worker sync keys on an exact `app_version`
match). Push `dev` **and** fast-forward `main` — a worker's `git pull` can never reach an
unpushed commit:

```bash
git push origin dev && git push origin dev:main
```

Do **not** `git checkout main`: the live trading platforms run from this working tree via an
editable install.

**Step 2:** Stop the grid in order — wrapper bash → driver → master → pool children — selecting
processes by **explicit PID**, never by a blanket `python` match. The live platforms on :8080 and
:8081 must survive; verify with `netstat -ano | grep -E ":8080|:8081"` before and after.

**Step 3:** Relaunch and watch the resume line. Success looks like:

```
strategy_optimization NNN: RESUMING '<name>' at generation G/8 from checkpoint ckpt-...
Restored population of 140 individuals (117 already evaluated, 23 to run)
```

The parenthetical is the proof. Before this work it read `140 to run` every time.

**Step 4:** Confirm the first generation after the resume completes in roughly
`(remaining / total)` of a normal generation's wall time.

---

## Out of scope (deliberately)

- **`all_results` continuity.** A resumed run starts `all_results` empty, so the persisted top-N
  pool is thinner than an uninterrupted run's (documented at
  `strategy_optimization_handler.py:1337-1342`). Partial resumes make that more frequent. Fixing
  it means carrying trial summaries in the checkpoint, which would embed every trial's trades
  JSON — a separate decision with real size implications.
- **Cross-restart trial memo.** The in-process memo dies with the master; a genome completed in a
  previous process is re-run if it comes up again. Persisting it is a different feature.
- **Local-pool and remote-pool sizing.** Unrelated to checkpointing; see
  `distributed_eval.py` and the 2026-08-20 governor work.
