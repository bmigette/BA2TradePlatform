"""cached_ruleset_eventactions: per-thread, per-run memo of a ruleset's (ruleset, event_actions),
active ONLY under a thread-local backtest DB (rulesets are static within a run). In live (no
thread-local DB) it must NOT cache (a user can edit a ruleset between analyses); and a re-configured
run on the same thread must drop the prior cache (parallel GA trials / reruns reuse ruleset ids)."""
from ba2_common.core.db import (cached_ruleset_eventactions, clear_threadlocal_db,
                                configure_db_threadlocal)


def test_no_cache_in_live_no_threadlocal():
    clear_threadlocal_db()  # ensure no thread-local DB (live)
    calls = []
    loader = lambda: (calls.append(1), ("rs", []))[1]
    cached_ruleset_eventactions(1, loader)
    cached_ruleset_eventactions(1, loader)
    assert len(calls) == 2  # live -> loader runs every time (never stale)


def test_caches_under_threadlocal_bt_db():
    configure_db_threadlocal(":memory:")
    try:
        calls = []
        loader = lambda: (calls.append(1), ("rs", []))[1]
        cached_ruleset_eventactions(1, loader)
        cached_ruleset_eventactions(1, loader)
        assert len(calls) == 1                       # second call is a cache hit
        cached_ruleset_eventactions(2, loader)       # different ruleset id -> fresh load
        assert len(calls) == 2
    finally:
        clear_threadlocal_db()


def test_reconfigure_run_drops_cache():
    configure_db_threadlocal(":memory:")
    try:
        calls = []
        loader = lambda: (calls.append(1), ("rs", []))[1]
        cached_ruleset_eventactions(1, loader)       # 1 load
        configure_db_threadlocal(":memory:")         # a new run on this thread -> clear
        cached_ruleset_eventactions(1, loader)       # must reload (no cross-run bleed)
        assert len(calls) == 2
    finally:
        clear_threadlocal_db()
