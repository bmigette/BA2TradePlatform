"""Worker recycling: keep it, but never let the EXECUTOR do it.

WHY (measured 2026-08-16). opt 328 hung for 2h25m having completed exactly 32 individuals =
4 workers x max_tasks_per_child(8) -- the first recycle wave. py-spy on the master showed
MainThread parked in `as_completed`, `QueueFeederThread` blocked in `_send_bytes`, and ZERO worker
processes alive: every worker had retired and no replacement started, so the feeder was writing
into a call queue nobody drains. CPython raises no BrokenProcessPool in that state, so the pool
recovery path never fires and the job hangs SILENTLY -- no error, no log line, forever.

The recycle itself is worth keeping (a respawn returns pymalloc arenas the cache release cannot).
So it moved to the master, which rebuilds the pool between chunks when nothing is in flight.
"""
import re
from pathlib import Path

import pytest

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
        "belongs in _local_execute_jobs, at a chunk boundary where nothing is in flight.")


def test_recycle_is_still_enabled_by_default():
    """The fix must not have quietly become 'turn the feature off'."""
    m = re.search(r'_MAX_TASKS_PER_CHILD = int\(_os\.getenv\("BT_MAX_TASKS_PER_CHILD", "(\d+)"\)\)',
                  TEXT)
    assert m, "_MAX_TASKS_PER_CHILD definition not found"
    assert int(m.group(1)) > 0, "worker recycling was disabled rather than fixed"


def test_recycle_happens_only_between_chunks():
    """The teardown must sit AFTER the as_completed drain loop, not inside it."""
    body = TEXT[TEXT.index("def _local_execute_jobs"):]
    body = body[:body.index("def make_batch_fitness")]
    assert "_recycle_pool(" in body, "the chunk loop no longer recycles the pool"
    drain = body.index("as_completed(")
    recycle = body.index("_recycle_pool(")
    assert recycle > drain, "the recycle must follow the drain, never run during it"


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


@pytest.mark.parametrize("n_jobs,recycle,workers,expected_chunks", [
    (140, 8, 4, 5),    # the real shape: 140 individuals, chunk 32 -> 5 chunks (4 rebuilds)
    (32, 8, 4, 1),     # exactly one chunk -> NO rebuild (nothing follows it)
    (10, 8, 4, 1),
    (100, 8, 2, 7),    # chunk 16
])
def test_chunk_arithmetic(n_jobs, recycle, workers, expected_chunks):
    """Pins the cadence the comment promises, so a later tweak cannot silently make the chunk the
    whole batch (recycle never runs) or size 1 (a barrier per trial)."""
    chunk = max(1, recycle * max(1, workers))
    chunks = list(range(0, n_jobs, chunk))
    assert len(chunks) == expected_chunks
    rebuilds = sum(1 for start in chunks if (start + chunk) < n_jobs)
    assert rebuilds == expected_chunks - 1, "no rebuild after the final chunk -- it would be waste"
