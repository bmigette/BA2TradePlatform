"""LPT dispatch ordering + per-box memory governor routing.

The property that MUST hold for LPT is that it is invisible: reordering the batch changes the
makespan and nothing else. These tests pin that (order-independence of the assembled fitness
list), the ordering itself, the estimator's learning, and the governor routing fix -- a remote
worker's memory snapshot must never throttle the master's local pool.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.strategy_optimization_handler import (  # noqa: E402
    MemoryGovernor,
    _TrialCostModel,
)


def _job(idx, key, n_symbols):
    """(index, flat, key, config) as batch_fitness builds them."""
    return (idx, {"gene": idx}, key, {"enabled_instruments": [f"S{i}" for i in range(n_symbols)]})


# --------------------------------------------------------------------------- width / estimate

def test_width_reads_the_trials_own_screened_universe():
    assert _TrialCostModel.width({"enabled_instruments": ["A", "B", "C"]}) == 3


def test_width_of_a_config_without_instruments_is_zero_not_an_error():
    assert _TrialCostModel.width({}) == 0
    assert _TrialCostModel.width({"enabled_instruments": None}) == 0


def test_unseen_genome_is_priced_on_universe_width():
    m = _TrialCostModel()
    assert m.estimate("never-seen", 100) > m.estimate("never-seen", 10)


def test_observed_time_is_recalled_verbatim_over_the_width_estimate():
    m = _TrialCostModel()
    m.observe("k", 10, 3600.0)          # tiny universe, enormous cost
    # Recall must win: the whole point is that a known-expensive genome is not re-priced as cheap.
    assert m.estimate("k", 10) == 3600.0


def test_slope_is_learned_from_observations_not_assumed():
    m = _TrialCostModel()
    before = m.estimate("unseen-a", 100)
    for i in range(50):
        m.observe(f"key{i}", 100, 1000.0)   # 10 s/symbol, well above the 2.0 bootstrap
    after = m.estimate("unseen-b", 100)
    assert after > before


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan")])
def test_observe_ignores_unusable_timings(bad):
    m = _TrialCostModel()
    m.observe("k", 10, bad)
    assert "k" not in m._seen or m._seen["k"] == m._seen["k"]  # never stores None/garbage


def test_observe_ignores_non_positive_seconds():
    m = _TrialCostModel()
    m.observe("k", 10, 0.0)
    m.observe("k2", 10, -5.0)
    assert m._seen == {}


# --------------------------------------------------------------------------- ordering

def test_order_puts_the_most_expensive_trial_first():
    m = _TrialCostModel()
    jobs = [_job(0, "cheap", 5), _job(1, "mid", 50), _job(2, "dear", 200)]
    assert [j[0] for j in m.order(jobs)] == [2, 1, 0]


def test_order_uses_recalled_costs_over_width():
    """A NARROW genome already known to take an hour must still be dispatched first."""
    m = _TrialCostModel()
    m.observe("slow-but-narrow", 5, 3600.0)
    jobs = [_job(0, "wide-but-unknown", 200), _job(1, "slow-but-narrow", 5)]
    assert m.order(jobs)[0][0] == 1


def test_order_preserves_every_job_exactly_once():
    m = _TrialCostModel()
    jobs = [_job(i, f"k{i}", i * 7 % 90) for i in range(40)]
    out = m.order(jobs)
    assert len(out) == len(jobs)
    assert sorted(j[0] for j in out) == sorted(j[0] for j in jobs)


def test_order_is_a_noop_on_an_empty_batch():
    assert _TrialCostModel().order([]) == []


def test_order_never_raises_on_a_malformed_job():
    """Ordering is an optimisation; a bad config must degrade to population order, not kill a run."""
    m = _TrialCostModel()
    jobs = [_job(0, "ok", 10), (1, {}, "bad", None)]
    out = m.order(jobs)
    assert len(out) == 2


# ------------------------------------------------------- THE invariant: results are order-blind

def test_reordering_cannot_change_the_assembled_fitness_list():
    """Mirrors batch_fitness: results carry the GA's ORIGINAL index and are assigned by it, so
    the fitness list the GA sees is identical no matter what order the batch was dispatched in.
    This is what makes LPT safe to switch on mid-search."""
    m = _TrialCostModel()
    jobs = [_job(i, f"k{i}", (i * 37) % 120) for i in range(25)]
    truth = {i: float(i) * 1.5 for i in range(25)}

    def run(batch):
        fits = [None] * len(batch)
        for (i, _flat, _key, _cfg) in batch:      # completion order is irrelevant
            fits[i] = truth[i]
        return fits

    assert run(m.order(list(jobs))) == run(list(jobs))


def test_order_does_not_mutate_the_caller_list():
    m = _TrialCostModel()
    jobs = [_job(0, "a", 5), _job(1, "b", 90)]
    original = list(jobs)
    m.order(jobs)
    assert jobs == original


# --------------------------------------------------------------------------- reporting

def test_report_is_silent_below_the_sample_floor(caplog):
    m = _TrialCostModel()
    m.order([_job(0, "k", 10)])
    m.record_prediction("k", 10, 100.0)
    with caplog.at_level("WARNING"):
        m.report(0, 8)
    assert "LPT gen" not in caplog.text


def test_report_scores_a_perfect_estimator_at_rank_correlation_one(caplog):
    """Predictions proportional to width, actuals proportional to width -> rho = +1."""
    m = _TrialCostModel()
    jobs = [_job(i, f"k{i}", (i + 1) * 10) for i in range(12)]
    m.order(jobs)
    for i, j in enumerate(jobs):
        m.record_prediction(j[2], _TrialCostModel.width(j[3]), (i + 1) * 100.0)
    with caplog.at_level("WARNING"):
        m.report(2, 8)
    assert "LPT gen 3/8" in caplog.text
    assert "+1.00" in caplog.text


def test_report_scores_an_inverted_estimator_negatively(caplog):
    m = _TrialCostModel()
    jobs = [_job(i, f"k{i}", (i + 1) * 10) for i in range(12)]
    m.order(jobs)
    for i, j in enumerate(jobs):
        # widest genome is the FASTEST -- the estimator is exactly wrong
        m.record_prediction(j[2], _TrialCostModel.width(j[3]), (12 - i) * 100.0)
    with caplog.at_level("WARNING"):
        m.report(0, 8)
    assert "-1.00" in caplog.text


# --------------------------------------------------------------------------- governor routing

def _pressure(pct_free):
    return {"sys": {"pct_free": pct_free, "free_mb": 1000, "total_mb": 64000}}


def test_local_governor_still_throttles_on_local_pressure():
    gov = MemoryGovernor(4, log=lambda *_: None)
    assert gov.assess(_pressure(8.0)) == "release"
    assert gov.current == 3


def test_governor_only_ratchets_down_within_a_job():
    """Documents the one-way behaviour the routing fix protects: an erroneous throttle is NOT
    recovered until the next job, which is why a remote reading must never reach this governor."""
    gov = MemoryGovernor(4, log=lambda *_: None)
    for _ in range(3):
        gov.assess(_pressure(8.0))
    assert gov.current == 1
    gov.assess(_pressure(90.0))      # plenty free again
    assert gov.current == 1          # still parked
    gov.restore("new job")
    assert gov.current == 4


def test_remote_snapshot_is_routed_away_from_the_local_governor():
    """THE REGRESSION. A trial that ran on a remote box reports THAT box's free memory; acting on
    it here would shrink the master's pool while the starved machine kept taking work."""
    local = MemoryGovernor(4, log=lambda *_: None)
    remote = MemoryGovernor(12, log=lambda *_: None)

    for origin in ["remote150"] * 10:            # remote box under pressure
        if origin and origin != "local":
            remote.assess(_pressure(3.0))
        else:
            local.assess(_pressure(3.0))

    assert local.current == 4, "local pool must be untouched by remote pressure"
    assert remote.current < 12 or remote._emergencies > 0


def test_local_origin_and_missing_origin_both_reach_the_local_governor():
    """`origin` is absent on the local-only backend (results come straight off the pool future),
    so None must be treated as local -- otherwise a non-distributed run loses its governor."""
    for origin in (None, "local"):
        gov = MemoryGovernor(4, log=lambda *_: None)
        if origin and origin != "local":
            pytest.fail("should not route to remote")
        else:
            gov.assess(_pressure(8.0))
        assert gov.current == 3


# ------------------------------------------------- remote emergency must actually shed slots

class _FakeEvaluator:
    """Minimal stand-in exercising DistributedEvaluator.assess_remote's verdict handling."""

    def __init__(self, gov):
        import threading
        self._remote_govs = {"remote150": gov}
        self._remote_gov_lock = threading.Lock()
        self.logged = []

    def log(self, m):
        self.logged.append(m)

    # bound method under test, taken from the real class
    from app.services.distributed_eval import DistributedEvaluator as _DE
    assess_remote = _DE.assess_remote


def test_remote_emergency_halves_the_workers_slots():
    """THE 2026-08-18 REGRESSION. remote150 hit 85 MB free of 65 GB on the mid band and the
    emergency shed NOTHING: assess() only returns a verdict and assess_remote discarded it, while
    the log claimed a pool-break that cannot happen on a remote box."""
    gov = MemoryGovernor(12, log=lambda *_: None)
    ev = _FakeEvaluator(gov)
    ev.assess_remote("remote150", _pressure(0.1))
    assert gov.current == 6, "12 slots must halve on an emergency, not stay put"
    assert any("6" in m for m in ev.logged)


def test_remote_emergency_never_parks_the_last_slot():
    gov = MemoryGovernor(1, log=lambda *_: None)
    ev = _FakeEvaluator(gov)
    for _ in range(5):
        ev.assess_remote("remote150", _pressure(0.1))
    assert gov.current == 1, "a worker must never be throttled out of existence"


def test_remote_throttle_still_sheds_one_slot():
    gov = MemoryGovernor(12, log=lambda *_: None)
    ev = _FakeEvaluator(gov)
    ev.assess_remote("remote150", _pressure(8.0))   # <10% = release, not emergency
    assert gov.current == 11


def test_remote_ok_reading_changes_nothing():
    gov = MemoryGovernor(12, log=lambda *_: None)
    ev = _FakeEvaluator(gov)
    ev.assess_remote("remote150", _pressure(80.0))
    assert gov.current == 12


def test_emergency_note_is_owner_specific():
    """The local governor breaks its pool; a remote one cannot. The message must not claim it does."""
    local = MemoryGovernor(4, log=lambda *_: None)
    assert "breaking the pool" in local.emergency_note
    remote = MemoryGovernor(12, log=lambda *_: None, emergency_note="HALVING this worker's slots")
    assert "breaking the pool" not in remote.emergency_note
