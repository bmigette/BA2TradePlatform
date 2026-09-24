"""A stalled trial must not kill the job (2026-09-21, operator decision).

INCIDENT. On 2026-09-20 21:54Z the local pool's stall guard found one trial with no completion in
5400s and raised ``TimeoutError("local pool stalled")``. That failed the WHOLE job: the matrix
stopped at job 1/16, never launched jobs 2-16, and — because the launch is a detached Popen with no
auto-restart — the grid sat dead for 7.5h until the monitor cron reported it.

DECISION. A stall scores the offending INDIVIDUAL at ``STALLED_SENTINEL``, respawns its slot, and
the job continues. The sentinel is deliberately its own value (not ZERO_TRADE_SENTINEL) so the
frequency stays countable in ``all_results`` — the operator asked to see how often it happens.

The two properties that matter, and that this file pins:
  1. the sentinel is distinct and ranks worst (a wedge is not a measurement of the genome);
  2. ``abandon_slot`` recovers WITHOUT waiting — ``_recycle_pool``'s ``shutdown(wait=True)`` would
     block on the very worker being abandoned, which would hang the recovery itself.
"""
import time
from concurrent.futures import ProcessPoolExecutor

import importlib

mod = importlib.import_module("app.services.strategy_optimization_handler")
fitness = importlib.import_module("app.services.strategy_fitness")

SRC = (__import__("pathlib").Path(__file__).resolve().parents[1]
       / "app" / "services" / "strategy_optimization_handler.py").read_text(encoding="utf-8")


def _sleep_task(seconds):
    time.sleep(seconds)
    return "done"


# --- 1. the sentinel -------------------------------------------------------------------------

def test_stalled_sentinel_is_distinct_and_ranks_worst():
    """Distinct so a stall is countable; worst so the GA can never prefer a wedged genome."""
    stalled = fitness.STALLED_SENTINEL
    others = (fitness.WIPED_OUT_SENTINEL, fitness.ZERO_TRADE_SENTINEL, fitness.LOW_TRADE_SENTINEL)
    assert stalled not in others, "STALLED_SENTINEL collides with an existing sentinel"
    assert stalled < min(others), (
        f"STALLED_SENTINEL {stalled} must rank BELOW every real disqualification {others}: a "
        f"wedge is not a measurement of the genome, so it can never outrank one")
    assert stalled < 0


def test_the_fitness_paths_treat_the_stall_sentinel_as_a_non_measurement():
    """Wherever the other sentinels short-circuit the robustness/stress maths, so must this one --
    otherwise a stressed or robust-adjusted path would try to scale a wedge as if it were real."""
    text = (__import__("pathlib").Path(fitness.__file__)).read_text(encoding="utf-8")
    assert text.count("STALLED_SENTINEL, ZERO_TRADE_SENTINEL, LOW_TRADE_SENTINEL") == 2, (
        "both sentinel tuples (stressed_results + robustness-adjusted fitness) must list "
        "STALLED_SENTINEL")


# --- 2. the recovery primitive ---------------------------------------------------------------

def test_abandon_slot_recovers_without_waiting_on_the_wedged_worker():
    pools = mod._SlotPools(lambda: ProcessPoolExecutor(max_workers=1), 1, 0)
    try:
        wedged = pools.submit(0, _sleep_task, 60)
        assert pools.busy[0] is wedged
        old_pool = pools.pools[0]
        # Capture the worker objects BEFORE the abandon: shutdown() clears the executor's
        # _processes mapping (None after teardown), so a post-hoc read sees nothing.
        workers = list((getattr(old_pool, "_processes", None) or {}).values())
        assert workers, "the pool had no worker process to terminate"

        t0 = time.monotonic()
        slot = pools.abandon_slot(wedged)
        elapsed = time.monotonic() - t0

        assert slot == 0, "abandon_slot must report which slot it respawned"
        assert elapsed < 15.0, (
            f"abandon_slot took {elapsed:.1f}s -- it waited on the wedged worker instead of "
            f"terminating it (the incident's 2h47m wedge would have become a 2h47m recovery)")
        assert pools.busy[0] is None, "the slot was not returned to idle"

        # the wedged worker is really gone (terminated), not merely detached from its future
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and any(p.is_alive() for p in workers):
            time.sleep(0.1)
        assert not any(p.is_alive() for p in workers), (
            "the wedged worker survived the abandon -- it would keep holding its slot's RAM and "
            "the next generation could inherit it")

        # and the slot is immediately usable again
        assert pools.submit(0, _sleep_task, 0).result(timeout=60) == "done"
    finally:
        pools.shutdown(wait=False, cancel_futures=True)


def test_abandon_slot_is_a_no_op_for_a_future_that_is_not_in_flight():
    """A future the pool has already RELEASED (or one belonging to another pool) must not corrupt
    a slot's bookkeeping. Note a merely-FINISHED future is still in flight until mark_done -- the
    dispatcher only releases a slot when it consumes the result -- so the release is what makes it
    unknown, not the completion."""
    pools = mod._SlotPools(lambda: ProcessPoolExecutor(max_workers=1), 1, 0)
    try:
        fut = pools.submit(0, _sleep_task, 0)
        assert fut.result(timeout=60) == "done"
        pools.mark_done(fut)                      # released: no longer in flight
        assert pools.abandon_slot(fut) == -1
        assert pools.busy[0] is None, "bookkeeping changed for a future that was not in flight"
        # and the slot is still usable
        assert pools.submit(0, _sleep_task, 0).result(timeout=60) == "done"
    finally:
        pools.shutdown(wait=False, cancel_futures=True)


# --- 3. the wiring: score it, do not raise ---------------------------------------------------

def test_the_dispatcher_scores_a_stall_instead_of_raising():
    """The regression that caused the incident was a RAISE. Pin its absence, and pin that the
    stalled individual is scored at the dedicated sentinel and its slot respawned."""
    assert 'raise TimeoutError("local pool stalled")' not in SRC, (
        "the stall guard raises again -- one wedged genome would fail the whole job (2026-09-20)")
    assert "STALLED_SENTINEL" in SRC, "the stall is not scored at the dedicated sentinel"
    assert '"stalled": True' in SRC, "the stalled result is not flagged for the failure branch"
    assert "abandon_slot(" in SRC, "the wedged slot is not respawned"
    assert "LOCAL POOL STALLED" in SRC, "the stall must still be logged loudly"


def test_the_stalled_individual_is_memoized_and_recorded():
    """Memoized so the same wedge cannot burn another timeout every generation, and RECORDED so
    the operator can count how often it happens (the explicit ask)."""
    assert "memo.put(key, fit)" in SRC
    assert '"fitness_raw": fit, "robustness": None' in SRC, (
        "the stalled individual is not appended to all_results -- then it is uncountable")
