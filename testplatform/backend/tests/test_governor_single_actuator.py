"""A remote worker's concurrency must have exactly ONE actuator.

THE 2026-08-20 UNDER-UTILISATION. The watchdog was fixed to compute a pool size and go there in
one move -- and it did, logging "pool 6 -> 4 child(ren)". But `assess_remote`, which runs on every
trial RESULT, still called MemoryGovernor.assess(), and assess() decrements `current` as a SIDE
EFFECT of returning its verdict. So a second, uncoordinated actuator walked the same governor
4 -> 3 -> 2 -> 1 behind the watchdog's back, ignoring both the calculated target and the settling
window. remote150 sat at busy 1/4 with 71.7% of the box free.

It hid because the two paths log different strings: "dispatch slots N -> M" from the shed, versus
"memory governor THROTTLE ... reducing concurrency N -> M" from assess() itself.

Fix: the trial-snapshot path observes, it does not actuate. `verdict()` is the pure query;
`assess()` keeps its mutating behaviour for the LOCAL governor, which owns a pool it can act on.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import distributed_eval as de  # noqa: E402
from app.services.strategy_optimization_handler import MemoryGovernor  # noqa: E402


def _mem(pct_free):
    return {"sys": {"pct_free": pct_free, "free_mb": int(65450 * pct_free / 100), "total_mb": 65450}}


def _ev():
    ev = de.DistributedEvaluator.__new__(de.DistributedEvaluator)
    ev.log = lambda *_a, **_k: None
    ev._stop = threading.Event()
    ev._worker_lock = threading.Lock()
    ev._remote_govs = {}
    ev._remote_gov_lock = threading.Lock()
    ev._remote_peak_child = {}
    ev._remote_settle_until = {}
    return ev


# --------------------------------------------------------------- verdict() is pure

def test_verdict_reports_without_changing_anything():
    gov = MemoryGovernor(6, log=lambda *_: None)
    assert gov.verdict(_mem(8.0)) == "release"
    assert gov.current == 6, "a QUERY must not move the ceiling"


def test_verdict_still_classifies_all_three_states():
    gov = MemoryGovernor(6, log=lambda *_: None)
    assert gov.verdict(_mem(80.0)) == "ok"
    assert gov.verdict(_mem(8.0)) == "release"
    assert gov.verdict(_mem(3.0)) == "emergency"
    assert gov.current == 6


def test_verdict_is_safe_on_a_junk_snapshot():
    gov = MemoryGovernor(6, log=lambda *_: None)
    for junk in (None, {}, {"sys": {}}, "nonsense", 7):
        assert gov.verdict(junk) == "ok"
    assert gov.current == 6


def test_assess_still_mutates_for_the_local_pool():
    """The LOCAL governor owns a pool it can actually break; that behaviour is unchanged."""
    gov = MemoryGovernor(4, log=lambda *_: None)
    assert gov.assess(_mem(8.0)) == "release"
    assert gov.current == 3


# --------------------------------------------------------------- the regression

def test_a_trial_snapshot_never_moves_remote_concurrency():
    """THE BUG. Four results under the floor used to walk the governor 4 -> 1 while the watchdog
    believed it had set 4."""
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    gov.current = 4                      # what the watchdog calculated
    ev._remote_govs["remote150"] = gov
    for _ in range(6):
        ev.assess_remote("remote150", _mem(7.5))
    assert gov.current == 4, "the trial-snapshot path must observe, not actuate"


def test_an_emergency_snapshot_also_does_not_step():
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    gov.current = 4
    ev._remote_govs["remote150"] = gov
    ev.assess_remote("remote150", _mem(2.0))
    assert gov.current == 4


def test_pressure_shortens_the_settling_window_so_the_watchdog_acts_sooner():
    """Observing is not ignoring: a starved box should be re-evaluated on the NEXT poll rather
    than waiting out a settle deadline set before the pressure appeared."""
    import time
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    ev._remote_settle_until["remote150"] = time.monotonic() + 10_000
    ev.assess_remote("remote150", _mem(2.0))
    assert ev._remote_settle_until["remote150"] <= time.monotonic() + 1, \
        "an emergency must let the watchdog re-measure promptly"


def test_a_healthy_snapshot_leaves_the_settling_window_alone():
    import time
    ev = _ev()
    gov = MemoryGovernor(6, log=lambda *_: None)
    ev._remote_govs["remote150"] = gov
    deadline = time.monotonic() + 120
    ev._remote_settle_until["remote150"] = deadline
    ev.assess_remote("remote150", _mem(80.0))
    assert ev._remote_settle_until["remote150"] == deadline


def test_assess_remote_survives_an_unknown_worker():
    ev = _ev()
    ev.assess_remote("never-seen", _mem(2.0))   # must not raise
