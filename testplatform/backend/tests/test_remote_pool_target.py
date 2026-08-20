"""Size a remote pool by CALCULATION, not by walking down one slot at a time.

THE 2026-08-20 OVERSHOOT. Stepping plus a forced resize is a feedback loop that eats itself: a
resize tears the pool down and the replacement children immediately reload ~7-9 GB universes, so
the next poll 60s later still reads "under floor" -- not because the box is over-committed but
because it is mid-reload. Every resize therefore triggered the next one. remote150 walked
6 -> 5 -> 4 -> 3 -> 2 -> 1 and only then reported 74.3% free: it had been throttled to a single
child on a box that could hold five.

Two changes make the loop stable:
  * compute the target from MEASURED per-child footprint and go there in ONE move, and
  * ignore readings for a settling window after any resize, because a mid-reload sample is not
    a measurement of anything.

Sizing uses the PEAK child ever seen, not the mean: children grow through a trial (7.4 GB to
13.0 GB observed on the small band), and a pool sized on the average is one simultaneous spike
away from the state we are trying to avoid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.distributed_eval import _target_pool_size  # noqa: E402


def _diag(total=65450, server=250, kids=(9000, 9000, 9000, 9000, 9000, 9000)):
    return {"system_total_mb": total, "server_rss_mb": server,
            "child_rss_mb": list(kids) + [12]}   # the 12 MB resource tracker is always present


# --------------------------------------------------------------------------- the calculation

def test_target_is_derived_from_the_measured_peak_child():
    """65450 MB * 0.85 - 250 = 55,382 usable; at 9 GB a child that is 6."""
    assert _target_pool_size(_diag(), peak_mb=9000) == 6


def test_a_heavier_peak_gives_a_smaller_pool():
    """The 13 GB child actually observed on the small band is what makes 6 unsafe."""
    assert _target_pool_size(_diag(), peak_mb=13043) == 4


def test_the_tiny_resource_tracker_is_not_counted_as_a_child():
    """A 12 MB helper process must not drag the peak down and inflate the target."""
    d = {"system_total_mb": 65450, "server_rss_mb": 250, "child_rss_mb": [9000, 12, 15]}
    assert _target_pool_size(d, peak_mb=None) == 6


def test_peak_is_taken_over_the_supplied_value_and_the_current_children():
    """A worker that just grew a heavier child must shrink on THAT, not on a stale peak."""
    d = _diag(kids=(13043, 9000))
    assert _target_pool_size(d, peak_mb=7000) == 4, "the live 13 GB child must win"


def test_never_returns_less_than_one():
    assert _target_pool_size(_diag(total=8000), peak_mb=60000) == 1


def test_no_loaded_children_means_no_opinion():
    """Before anything has run there is nothing to size against; the caller must not act."""
    d = {"system_total_mb": 65450, "server_rss_mb": 250, "child_rss_mb": [12]}
    assert _target_pool_size(d, peak_mb=None) is None


def test_missing_fields_are_survivable():
    assert _target_pool_size({}, peak_mb=9000) is None
    assert _target_pool_size(None, peak_mb=9000) is None


# --------------------------------------------------------------------------- the overshoot itself

def test_the_measured_overshoot_cannot_recur():
    """Replay the real sequence. At every point the calculation answers 4-6, never 1."""
    for kids in [(9000,) * 6, (9000,) * 5, (9000,) * 4, (9000,) * 2, (7369,)]:
        t = _target_pool_size(_diag(kids=kids), peak_mb=13043)
        assert t == 4, f"{len(kids)} children -> target {t}, expected a stable 4"


def test_a_worker_that_has_recovered_is_not_shrunk_further():
    """The final reading of the incident: ONE child, 74% free. Stepping shed anyway; the
    calculation says the box can hold four."""
    assert _target_pool_size(_diag(kids=(7369,)), peak_mb=7369) > 1


def test_headroom_is_actually_reserved():
    """Whatever the target, the pool it implies must leave real room -- the incident happened at
    ~4 GB free, which is inside the noise of a single child's growth."""
    total, peak = 65450, 9000
    t = _target_pool_size(_diag(), peak_mb=peak)
    assert total - (t * peak) >= total * 0.10
