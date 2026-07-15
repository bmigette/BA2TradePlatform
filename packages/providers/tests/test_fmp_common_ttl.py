"""TTLCache freeze behaviour (backtest perf): a long backtest must NOT let the 15-min
FMP TTL expire mid-run and re-fetch. ``frozen_ttl_cache()`` makes entries non-expiring
for its duration; the LIVE path (no context) keeps normal TTL expiry.
"""
from ba2_providers.fmp_common import TTLCache, frozen_ttl_cache, set_ttl_frozen


def test_ttl_expires_normally_when_not_frozen():
    t = [1000.0]
    c = TTLCache(900, clock=lambda: t[0])
    calls = []
    c.get_or_call("AAPL", lambda: (calls.append(1), "v1")[1])
    t[0] += 1000  # past the 900s TTL
    c.get_or_call("AAPL", lambda: (calls.append(1), "v2")[1])
    assert len(calls) == 2  # expired -> re-fetched


def test_frozen_ignores_expiry():
    t = [1000.0]
    c = TTLCache(900, clock=lambda: t[0])
    calls = []
    with frozen_ttl_cache():
        first = c.get_or_call("AAPL", lambda: (calls.append(1), "v1")[1])
        t[0] += 100_000  # way past TTL
        again = c.get_or_call("AAPL", lambda: (calls.append(1), "v2")[1])
    assert len(calls) == 1   # frozen -> single fetch reused
    assert first == again == "v1"


def test_freeze_restored_after_context():
    t = [1000.0]
    c = TTLCache(900, clock=lambda: t[0])
    calls = []
    with frozen_ttl_cache():
        c.get_or_call("AAPL", lambda: (calls.append(1), "v1")[1])
    t[0] += 1000  # past TTL, now OUTSIDE the frozen context
    c.get_or_call("AAPL", lambda: (calls.append(1), "v2")[1])
    assert len(calls) == 2   # TTL expiry enforced again after the context


def test_frozen_context_is_reentrant_safe():
    # nested/!restore: the flag is saved/restored, not hard-reset to False
    set_ttl_frozen(True)
    try:
        with frozen_ttl_cache():
            pass
        from ba2_providers import fmp_common
        assert fmp_common._is_ttl_frozen() is True  # restored to prior (True), not forced False
    finally:
        set_ttl_frozen(False)


# --- ThreadPoolExecutor gotcha (2026-07-15 incident) ------------------------------------
# frozen_ttl_cache() (like hermetic_fmp_history()) sets a THREAD-LOCAL flag BY DESIGN — a
# live backtest thread must never see a sibling thread's freeze state. But threading.local()
# does NOT propagate into a ThreadPoolExecutor's worker threads: entering frozen_ttl_cache()
# in the submitting thread leaves every task the pool RUNS un-frozen. `ba2-test prewarm`
# hit this for real — a full run burned the FMP rate-limit budget on genuine network fetches
# and wrote ZERO disk-cache files, because fmp_history_disk_cached() took the "live: never
# persist" branch on every call from a worker thread. Fixed by passing
# initializer=set_ttl_frozen, initargs=(True,) to the pool (runs once per worker thread,
# before any task, setting the SAME thread-local from inside that thread).
def test_frozen_ttl_cache_does_not_propagate_to_threadpool_workers():
    """Documents the gotcha: naive frozen_ttl_cache() + ThreadPoolExecutor is broken."""
    from concurrent.futures import ThreadPoolExecutor
    from ba2_providers.fmp_common import _is_ttl_frozen

    with frozen_ttl_cache():
        assert _is_ttl_frozen() is True  # submitting thread: frozen
        with ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(lambda _: _is_ttl_frozen(), range(4)))
    assert results == [False, False, False, False], (
        "if this ever becomes True, ThreadPoolExecutor started propagating thread-locals "
        "and every initializer=set_ttl_frozen call site can be simplified/removed"
    )


def test_threadpool_initializer_propagates_freeze_to_worker_threads():
    """The actual fix pattern used by ba2-test prewarm: an initializer sets the thread-local
    freeze flag once per worker thread, so every task that thread ever runs sees frozen=True."""
    from concurrent.futures import ThreadPoolExecutor
    from ba2_providers.fmp_common import _is_ttl_frozen

    with frozen_ttl_cache():
        with ThreadPoolExecutor(max_workers=3, initializer=set_ttl_frozen, initargs=(True,)) as ex:
            results = list(ex.map(lambda _: _is_ttl_frozen(), range(9)))
    assert results == [True] * 9
    # restored after the context exits (no leakage into whatever runs next)
    assert _is_ttl_frozen() is False
