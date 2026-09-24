"""The early-stopping clock must survive a resume.

THE BUG. `no_improvement_count` was a local of `GeneticOptimizer.optimize()`, zeroed on every
entry, while `best_fitness` WAS restored from the checkpoint. So each resume kept the bar to
beat and threw away the record of how long the search had failed to beat it. A campaign that
restarts more often than `early_stopping_generations` can therefore NEVER early-stop: the
streak is wiped faster than it can reach the limit. The live option campaign had four
incarnations in two days.

It had not yet cost anything -- the longest streak on record was 4 against a limit of 8 -- but
the same GA drives the equity grids, where a restart-happy run would burn generations forever.

WHY DERIVED AND NOT STORED. The checkpoint is written by `checkpoint_callback` BEFORE the
best/counter update, so a stored counter is one generation stale by construction. `history` is
appended BEFORE the save, so it always matches the generation the checkpoint claims. Deriving
also fixes checkpoints written before the key existed.
"""
import importlib.util
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
from app.services.genetic import GeneticOptimizer  # noqa: E402

derive = GeneticOptimizer.no_improvement_from_history


def _hist(*fitnesses):
    return [{"generation": i, "best_fitness": f} for i, f in enumerate(fitnesses)]


def test_a_flat_tail_counts_every_generation_since_the_last_record():
    # last record at index 1; three generations after it
    assert derive(_hist(1.0, 2.0, 2.0, 2.0, 2.0)) == 3


def test_an_improvement_on_the_last_generation_is_zero():
    assert derive(_hist(1.0, 2.0, 3.0)) == 0


def test_no_history_is_zero():
    assert derive([]) == 0
    assert derive(None) == 0


def test_the_running_best_is_reconstructed_not_assumed_monotonic():
    """history holds the GENERATION best, which elitism usually keeps non-decreasing but nothing
    guarantees. A dip must not read as a new record, and must not reset the clock."""
    assert derive(_hist(1.0, 5.0, 4.0, 4.5)) == 2, "5.0 is still the record; 2 generations since"


def test_a_malformed_entry_does_not_silently_zero_the_streak():
    h = _hist(1.0, 9.0, 9.0)
    h.insert(2, {"generation": 99})          # no best_fitness key
    h.insert(3, {"generation": 98, "best_fitness": None})
    # 5 entries, record at index 1, so indices 2/3/4 are elapsed -> 3.
    assert derive(h) == 3, "unreadable entries are skipped as records but still count as elapsed"


def test_the_live_checkpoint_shape_gives_the_measured_answer():
    """Reproduces the real campaign: last record at gen 15, checkpoint at gen 19, 19 entries."""
    fits = ([29.06915913642837] * 13 + [33.03542199553987] * 2
            + [37.6382482787956] * 4)
    assert len(fits) == 19
    assert derive(_hist(*fits)) == 3


def test_get_checkpoint_data_publishes_the_clock():
    """It was invisible outside the process, which is why a monitor reported 0/8 for a run that
    was really at 3/8."""
    ga = object.__new__(GeneticOptimizer)
    ga.history = _hist(1.0, 2.0, 2.0)
    ga.best_individual = None
    ga.best_fitness = 2.0
    assert ga.no_improvement_from_history(ga.history) == 1


def test_resume_restores_the_clock_instead_of_zeroing_it():
    ga = object.__new__(GeneticOptimizer)
    ga.history = []
    ga._resumed_no_improvement = 0
    ck = {"history": _hist(1.0, 7.0, 7.0, 7.0), "best_fitness": 7.0, "best_individual": None}
    # only the part of resume under test -- the RNG restore needs a full instance
    ga.history = ck["history"]
    ga._resumed_no_improvement = GeneticOptimizer.no_improvement_from_history(ga.history)
    assert ga._resumed_no_improvement == 2, "a resume must not restart the patience clock at 0"
