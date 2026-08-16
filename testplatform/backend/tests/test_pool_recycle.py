"""Worker recycling: keep it, but never let the EXECUTOR do it.

WHY (measured 2026-08-16). opt 328 hung for 2h25m having completed exactly 32 individuals =
4 workers x max_tasks_per_child(8) -- the first recycle wave. py-spy on the master showed
MainThread parked in `as_completed`, `QueueFeederThread` blocked in `_send_bytes`, and ZERO worker
processes alive: every worker had retired and no replacement started, so the feeder was writing
into a call queue nobody drains. CPython raises no BrokenProcessPool in that state, so the pool
recovery path never fires and the job hangs SILENTLY -- no error, no log line, forever.

The recycle itself is worth keeping (a respawn returns pymalloc arenas the cache release cannot),
so it moved to the master. LOCAL runs use one single-worker pool per slot (_SlotPools): a slot is
recycled while IDLE, between its own trials, so the other slots never pause. DISTRIBUTED runs keep
one shared executor -- several consumer threads submit into it -- and recycle it at the batch
boundary, where nothing is in flight (measured 1.9s).
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "app" / "services" / "strategy_optimization_handler.py")
TEXT = SRC.read_text(encoding="utf-8")


def test_executor_is_never_given_max_tasks_per_child():
    """THE regression guard. Passing this to ProcessPoolExecutor reintroduces the exact hang, and
    it would look completely reasonable in a diff -- the constant is still there, still 8, still
    called 'max tasks per child'. Only the CALL SITE distinguishes safe from wedged."""
    # Scan CODE only: the comment block above the constructor names the broken call verbatim
    # ("It is NOT implemented with ProcessPoolExecutor(max_tasks_per_child=...)"), which a naive
    # search matches -- the test would fail on its own documentation.
    code = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
    ctor = re.search(r"ProcessPoolExecutor\((.*?)\n\s*\)", code, re.S)
    assert ctor, "ProcessPoolExecutor construction not found"
    assert "max_tasks_per_child" not in ctor.group(1), (
        "ProcessPoolExecutor was given max_tasks_per_child again -- this deadlocks on "
        "Windows/spawn when workers retire mid-flight (opt 328, 2h25m silent hang). The recycle "
        "belongs in _SlotPools (local, per idle slot) or the batch boundary (distributed).")


def test_recycle_is_still_enabled_by_default():
    """The fix must not have quietly become 'turn the feature off'."""
    m = re.search(r'_MAX_TASKS_PER_CHILD = int\(_os\.getenv\("BT_MAX_TASKS_PER_CHILD", "(\d+)"\)\)',
                  TEXT)
    assert m, "_MAX_TASKS_PER_CHILD definition not found"
    assert int(m.group(1)) > 0, "worker recycling was disabled rather than fixed"


def test_local_path_recycles_per_slot_not_globally():
    """The local dispatcher must feed IDLE slots, never drain everything to rebuild one pool.

    A chunk-and-barrier design is correct but wastes the fleet: this grid has 1104s genomes
    against a 57s median, so three slots would idle a quarter of an hour waiting on one straggler,
    several times per generation.
    """
    body = TEXT[TEXT.index("def _local_execute_jobs"):]
    body = body[:body.index("def make_batch_fitness")]
    assert "idle_slots()" in body, "the dispatcher no longer fills idle slots"
    assert "mark_done(" in body, "slots are never released back to idle"
    assert "as_completed(" not in body, (
        "as_completed waits on a fixed submitted set -- that is the chunk-barrier shape this "
        "replaced. FIRST_COMPLETED + refill is what keeps every slot busy.")


def test_stall_guard_present():
    """Belt and braces: a wedge must abort loudly instead of hanging."""
    assert "BT_LOCAL_STALL_TIMEOUT_S" in TEXT
    assert "LOCAL POOL STALLED" in TEXT


def test_emergency_pool_break_is_distributed_only():
    """cancel_futures on the LOCAL path would strand the batch generator on cancelled futures --
    there is no requeue there. Only the distributed evaluator can survive it."""
    assert 'elif verdict == "emergency" and _evaluator is not None:' in TEXT


class _FakePool:
    """Records that shutdown was called, and how."""
    def __init__(self):
        self.shutdown_args = None

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = {"wait": wait, "cancel_futures": cancel_futures}


def test_recycle_pool_waits_and_returns_a_fresh_pool():
    from app.services.strategy_optimization_handler import _recycle_pool

    old = _FakePool()
    sentinel = object()
    logs = []
    fresh = _recycle_pool(old, lambda: sentinel, 32, 140, log=logs.append)

    assert fresh is sentinel, "a NEW pool must be returned, not the dead one"
    assert old.shutdown_args == {"wait": True, "cancel_futures": False}, (
        "shutdown(wait=True) is what makes the recycle safe -- it lets the retiring processes "
        "finish and the queue feeder drain, which is exactly what the executor's own recycling "
        "fails to do")
    assert any("32/140" in m for m in logs), "the recycle must be visible in the log"


def test_recycle_pool_survives_a_failing_shutdown():
    """A teardown that raises must not lose the run -- we still need a working pool."""
    from app.services.strategy_optimization_handler import _recycle_pool

    class _Bad(_FakePool):
        def shutdown(self, wait=True, cancel_futures=False):
            raise OSError("handle already closed")

    logs = []
    fresh = _recycle_pool(_Bad(), lambda: "new", 8, 40, log=logs.append)
    assert fresh == "new"
    assert any("shutdown raised" in m for m in logs)


# ---------------------------------------------------------------------------------------
# _SlotPools: the staggered recycle
# ---------------------------------------------------------------------------------------
class _FakeFuture:
    def __init__(self, val=None):
        self._val = val

    def result(self, timeout=None):
        return self._val


class _CountingPool:
    """A stand-in executor that records submissions and shutdowns."""
    _serial = 0

    def __init__(self):
        _CountingPool._serial += 1
        self.id = _CountingPool._serial
        self.submitted = []
        self.shut = False

    def submit(self, fn, *a):
        self.submitted.append((fn, a))
        return _FakeFuture(self.id)

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut = True


def _slots(n=3, every=2):
    from app.services.strategy_optimization_handler import _SlotPools
    return _SlotPools(_CountingPool, n, every, log=lambda m: None)


def test_only_the_due_slot_is_recycled():
    """THE point of the design: recycling slot 0 must leave slots 1..n-1 and their in-flight
    work completely untouched. A shared pool cannot do this."""
    sp = _slots(n=3, every=2)
    before = [p.id for p in sp.pools]

    for _ in range(2):                       # slot 0 reaches its quota
        f = sp.submit(0, print); sp.mark_done(f)
    f = sp.submit(0, print)                  # this submit triggers slot 0's recycle
    sp.mark_done(f)

    after = [p.id for p in sp.pools]
    assert after[0] != before[0], "the due slot was not recycled"
    assert after[1:] == before[1:], "recycling one slot must not disturb the others"
    assert sp.recycles == 1


def test_a_busy_slot_is_never_handed_more_work():
    sp = _slots(n=2, every=99)
    f = sp.submit(0, print)
    assert sp.idle_slots() == [1], "a slot with an in-flight trial must not look idle"
    sp.mark_done(f)
    assert sp.idle_slots() == [0, 1]


def test_task_counter_resets_on_recycle():
    """Otherwise the slot would rebuild on every subsequent submit."""
    sp = _slots(n=1, every=2)
    for _ in range(2):
        sp.mark_done(sp.submit(0, print))
    sp.mark_done(sp.submit(0, print))        # recycles, counter -> 1
    assert sp.tasks[0] == 1
    assert sp.recycles == 1
    sp.mark_done(sp.submit(0, print))        # counter 2, no recycle yet
    assert sp.recycles == 1


def test_recycle_disabled_when_interval_is_zero():
    sp = _slots(n=1, every=0)
    ids = [p.id for p in sp.pools]
    for _ in range(10):
        sp.mark_done(sp.submit(0, print))
    assert [p.id for p in sp.pools] == ids
    assert sp.recycles == 0


def test_release_all_hits_every_slot_exactly_once():
    """The shared-pool release oversubscribes 3x and HOPES each worker picks one up. With one
    worker per pool the coverage is exact -- a missed worker keeps its caches until its next
    preload, which is what the governor is trying to prevent."""
    sp = _slots(n=4, every=99)
    sp.release_all()
    assert all(len(p.submitted) == 1 for p in sp.pools)


def test_release_all_survives_a_dead_slot():
    sp = _slots(n=3, every=99)

    class _Dead(_CountingPool):
        def submit(self, fn, *a):
            raise RuntimeError("pool is shut down")

    sp.pools[1] = _Dead()
    sp.release_all()                                  # must not raise
    assert len(sp.pools[0].submitted) == 1
    assert len(sp.pools[2].submitted) == 1


def test_shutdown_covers_every_slot():
    sp = _slots(n=3, every=99)
    sp.shutdown()
    assert all(p.shut for p in sp.pools)


def test_release_pool_memory_prefers_the_exact_per_slot_path():
    from app.services.strategy_optimization_handler import _release_pool_memory
    sp = _slots(n=3, every=99)
    logs = []
    _release_pool_memory(sp, 3, log=logs.append)
    assert all(len(p.submitted) == 1 for p in sp.pools)
    assert any("per-slot" in m for m in logs)
