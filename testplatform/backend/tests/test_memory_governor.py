"""Dynamic worker allocation: throttle on memory pressure, reclaim for real, never lose an individual.

The governor exists because 6 local slots at the measured 4-7 GB/worker do not reliably fit a
64 GB box alongside the live trade platform. It acts on SYSTEM free memory (a worker's own rss says
nothing about whether the box is in trouble), sampled per individual on the box that ran the trial.

The subtle requirement -- and the reason throttling alone was rejected -- is that ceasing to
dispatch frees NOTHING: with flush-per-individual an idled worker holds its last working set until
its next preload. So a throttle MUST be paired with an explicit cache release.
"""
import logging

import pytest

from app.services.strategy_optimization_handler import (
    MemoryGovernor,
    system_memory,
)


def _snap(pct_free, total_mb=65240):
    return {"sys": {"pct_free": pct_free, "free_mb": int(total_mb * pct_free / 100),
                    "total_mb": total_mb}}


def test_ample_memory_does_not_throttle():
    g = MemoryGovernor(6, log=lambda *_a, **_k: None)
    assert g.assess(_snap(45.0)) == "ok"
    assert g.current == 6


def test_below_10pct_releases_and_steps_concurrency_down():
    g = MemoryGovernor(6, log=lambda *_a, **_k: None)
    assert g.assess(_snap(8.0)) == "release"
    assert g.current == 5
    assert g.assess(_snap(8.0)) == "release"
    assert g.current == 4


def test_below_5pct_is_an_emergency_not_a_throttle():
    """At 5% the box cannot wait for a trial to finish; breaking the pool reclaims every worker's
    memory at once, and the existing BrokenProcessPool handler requeues the in-flight individual."""
    g = MemoryGovernor(6, log=lambda *_a, **_k: None)
    assert g.assess(_snap(3.0)) == "emergency"


def test_concurrency_never_falls_below_one():
    g = MemoryGovernor(2, log=lambda *_a, **_k: None)
    for _ in range(10):
        g.assess(_snap(1.0) if False else _snap(8.0))
    assert g.current == 1, "a governor that throttles to zero would deadlock the run"


def test_restore_returns_to_full_at_a_job_boundary():
    """Restoring per JOB is what stops one heavy job permanently shrinking the fleet."""
    g = MemoryGovernor(6, log=lambda *_a, **_k: None)
    g.assess(_snap(8.0)); g.assess(_snap(8.0))
    assert g.current == 4
    g.restore()
    assert g.current == 6


def test_transitions_log_at_WARNING_for_visibility(caplog):
    """An optimize run installs logging.disable(INFO), so anything below WARNING is never emitted --
    and a governor silently halving throughput is indistinguishable from a slow box."""
    prior = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        g = MemoryGovernor(6)          # default log = logger.warning
        with caplog.at_level(logging.DEBUG):
            g.assess(_snap(8.0))
            g.assess(_snap(3.0))
            g.restore()
        assert "THROTTLE" in caplog.text
        assert "EMERGENCY" in caplog.text
        assert "restoring concurrency" in caplog.text
        assert "REQUEUED, not lost" in caplog.text     # the retry guarantee is stated in the log
    finally:
        logging.disable(prior)


def test_malformed_or_missing_snapshot_is_inert():
    """Runs in the per-individual hot path: a bad probe must never throttle or raise."""
    g = MemoryGovernor(6, log=lambda *_a, **_k: None)
    for bad in (None, {}, "nope", {"sys": {}}, {"sys": {"pct_free": None}}, {"error": "x"}):
        assert g.assess(bad) == "ok"
    assert g.current == 6


def test_system_memory_reports_plausible_values():
    m = system_memory()
    assert "error" not in m, m
    assert m["total_mb"] > 0 and 0 <= m["pct_free"] <= 100
    assert m["free_mb"] <= m["total_mb"]
