"""A resumed GA run must keep its pre-checkpoint ELITES eligible for the top-N persist.

WHAT HAPPENED (matrix3, 2026-09-09). The box rebooted mid-job; ``sen-S5-goal2020-risk_atr``
resumed from its generation-7 checkpoint and finished correctly -- ``best_fitness`` came back at
5.5741, bit-identical to the interrupted run. But ``all_results`` restarts EMPTY on resume, and
``_persist_top_backtests`` ranks off ``all_results``. So the resumed row carried 16 entries
(generations 7-8) instead of ~190, its best entry scored 5.2598, and the genome that actually won
the search -- 5.5741, sitting right there in ``best_params`` -- was never persisted as a Backtest.
Every saved S5 row understates the cell.

WHY IT WAS LEFT: the handler says "Carrying all_results in the checkpoint would embed every
trial's trades JSON in it." That is not what an entry holds. Measured on opt 489, an entry is
``{params, fitness, key, trades, fitness_raw, robustness, total_return, max_drawdown}`` where
``trades`` is an int COUNT (49), not a trade list: 1,949 bytes per entry, 359 KB for a whole
186-trial run, and 19 KB for a top-10 slice -- against a checkpoint column already holding 64 KB.
The objection does not survive measurement, so the elites are carried, BOUNDED to a top slice so
the column can never grow with the run.
"""
import pytest

from app.services import strategy_optimization_handler as H


def _r(fitness, key, trades=10):
    """An ``all_results`` entry in the shape the handler appends (see the module docstring)."""
    return {
        "params": {"model:x": fitness},
        "fitness": fitness,
        "key": key,
        "trades": trades,
        "fitness_raw": fitness,
        "robustness": {"top1_pct": 5.0, "top5_pct": 20.0},
        "total_return": fitness * 10,
        "max_drawdown": -5.0,
    }


# --------------------------------------------------------------------------------------------
# The slice that goes INTO the checkpoint
# --------------------------------------------------------------------------------------------

def test_elite_slice_keeps_the_best_by_fitness():
    results = [_r(1.0, "a"), _r(9.0, "b"), _r(5.0, "c"), _r(7.0, "d")]

    got = H._elite_slice(results, n=2)

    assert [r["fitness"] for r in got] == [9.0, 7.0]


def test_elite_slice_is_BOUNDED_so_the_checkpoint_cannot_grow_with_the_run():
    """The whole reason the original code refused to carry results. A 40x8 run must not put
    320 entries in a column that is rewritten once per generation."""
    results = [_r(float(i), f"k{i}") for i in range(500)]

    got = H._elite_slice(results, n=20)

    assert len(got) == 20
    assert got[0]["fitness"] == 499.0


def test_elite_slice_survives_entries_with_no_fitness():
    """A failed trial records ``fitness: None``; sorting must not raise on it."""
    results = [_r(3.0, "a"), {"params": {}, "key": "bad", "fitness": None}, _r(8.0, "b")]

    got = H._elite_slice(results, n=3)

    assert [r["fitness"] for r in got][:2] == [8.0, 3.0]


def test_elite_slice_of_nothing_is_nothing():
    assert H._elite_slice([], n=5) == []
    assert H._elite_slice(None, n=5) == []


# --------------------------------------------------------------------------------------------
# Reading it back OUT on resume
# --------------------------------------------------------------------------------------------

def test_resume_seeds_all_results_with_the_carried_elites():
    """The fix, stated directly: after a resume the pre-checkpoint winners are eligible again."""
    ckpt = {"generation": 7, "top_results": [_r(5.5741, "winner"), _r(5.2598, "runner-up")]}
    all_results = []

    H._seed_all_results_from_checkpoint(ckpt, all_results)

    assert [r["key"] for r in all_results] == ["winner", "runner-up"]


def test_resume_APPENDS_rather_than_replacing_what_the_new_run_records():
    ckpt = {"generation": 7, "top_results": [_r(9.0, "old-elite")]}
    all_results = [_r(1.0, "already-here")]

    H._seed_all_results_from_checkpoint(ckpt, all_results)

    assert [r["key"] for r in all_results] == ["already-here", "old-elite"]


def test_a_LEGACY_checkpoint_without_top_results_resumes_unchanged():
    """Every checkpoint written before this change lacks the key -- including the ones on disk
    for the jobs still queued in matrix3. Those must resume exactly as they do today."""
    all_results = []

    H._seed_all_results_from_checkpoint({"generation": 7, "population": [[1]]}, all_results)

    assert all_results == []


def test_a_malformed_top_results_never_breaks_a_resume():
    """A resume recovers hours of compute. It must not die because one column is the wrong
    shape -- the worst acceptable outcome is a thinner candidate pool, which is today's status
    quo, not a crashed job."""
    all_results = []

    for junk in ("not-a-list", {"a": 1}, 17):
        H._seed_all_results_from_checkpoint({"top_results": junk}, all_results)

    assert all_results == []
