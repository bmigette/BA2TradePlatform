"""The FMP seams the pure warm mechanism is injected with (spec sections 6 and 9).

The queue and the budget know nothing about FMP. These four callables are the whole
of what makes a warm an FMP warm, and each of them has a failure mode that has
already cost a run:

* the FREEZE flag is thread-local and is what makes ``fmp_history_disk_cached`` write
  at all -- set on the submitting thread only (the 2026-09-10 audit's finding), every
  worker fetches over the network and writes nothing while the run reports success;
* the empty-result SENTINEL must be scoped to the warm's own thread, never the
  process-global one, because a warm runs inside the live trading process (spec
  section 9: "process-global empty-sentinel flags must not leak into concurrent live
  work");
* the PURPOSE tag is what charges the bytes to the warm allowance instead of to live.
"""
import threading

import pytest

from ba2_providers import fmp_common
from ba2_providers.warm import seams


@pytest.fixture(autouse=True)
def clean_counters():
    fmp_common.reset_purpose_stats()
    yield
    fmp_common.reset_purpose_stats()
    fmp_common.set_ttl_frozen(False)


def test_the_fetch_context_freezes_scopes_the_sentinel_and_tags_the_purpose():
    with seams.fmp_fetch_context():
        assert fmp_common._is_ttl_frozen() is True
        assert fmp_common._persist_empty_sentinel_enabled() is True
        assert fmp_common.current_fmp_purpose() == "warm"

    assert fmp_common.current_fmp_purpose() == "live"
    assert fmp_common._persist_empty_sentinel_enabled() is False


def test_the_sentinel_is_scoped_to_the_thread_that_entered_the_context():
    """A concurrent frozen reader in the same process keeps live sentinel semantics."""
    inside = threading.Event()
    release = threading.Event()
    observed = {}

    def _warm_thread():
        with seams.fmp_fetch_context():
            inside.set()
            release.wait(5.0)

    thread = threading.Thread(target=_warm_thread, daemon=True)
    thread.start()
    try:
        assert inside.wait(5.0)
        observed["main"] = fmp_common._persist_empty_sentinel_enabled()
    finally:
        release.set()
        thread.join(timeout=5.0)

    assert observed["main"] is False, (
        "the warm worker's sentinel must not reach a concurrent thread")


def test_the_process_global_sentinel_still_reaches_pool_workers():
    """``run_prewarm`` depends on it: the freeze is thread-local, this one is not."""
    seen = []

    def _worker():
        seen.append(fmp_common._persist_empty_sentinel_enabled())

    with fmp_common.persist_empty_sentinel():
        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=5.0)

    assert seen == [True]


def test_the_worker_init_freezes_its_own_thread():
    seen = {}

    def _worker():
        seams.fmp_worker_init()
        seen["frozen"] = fmp_common._is_ttl_frozen()

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5.0)

    assert seen["frozen"] is True
    assert fmp_common._is_ttl_frozen() is False, "the calling thread is untouched"


def test_the_meter_reports_warm_bytes_only():
    fmp_common.record_fmp_request("live-endpoint", 900)
    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.record_fmp_request("warm-endpoint", 300)

    assert seams.fmp_meter() == 300


def test_the_gate_reports_the_shared_cooldown():
    try:
        fmp_common._gate_arm(5.0)
        assert seams.fmp_gate() > 0
    finally:
        fmp_common._GATE_UNTIL = 0.0
    assert seams.fmp_gate() == 0.0


def test_a_budget_built_by_the_seam_is_metered_and_gated():
    fmp_common.reset_purpose_stats()
    budget = seams.new_warm_budget(allowance_bytes=1000, unknown_reserve_bytes=100)

    with fmp_common.fmp_purpose(fmp_common.PURPOSE_WARM):
        fmp_common.record_fmp_request("warm-endpoint", 400)

    assert budget.spent_bytes() == 400
    assert budget.remaining_bytes() == 600
