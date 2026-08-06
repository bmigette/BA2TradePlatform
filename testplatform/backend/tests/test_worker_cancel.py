"""Cancel-on-abandon: a trial the master gave up on must STOP, not keep its slot (2026-07-28).

WHY. worker_client's poll gives up after its timeout, but the worker never learned that -- the
trial ran to completion for a result nobody would collect, holding a slot until the 6h
_JOBS_MAX_ORPHAN_AGE sweep. So a TIMEOUT REMOVED CAPACITY instead of freeing it: retries landed
on an ever-more-loaded worker, and the Senate S5 grid went 4 slots x 3 retries = 12 timeouts
straight into "worker remote150 giving up (dead)" (opt 226).

The measurement that framed this: a full-window Senate trial is ~15 min on an IDLE box
(test_files/profile_senate_trial.py), against a budget that was 1800s -- so trials crossed the
line under load and the missing cancel turned that into a total outage.
"""
import time

import pytest

from app import worker_server as ws


@pytest.fixture(autouse=True)
def _clean_registry():
    with ws._JOBS_LOCK:
        ws._JOBS.clear(); ws._JOBS_SUBMITTED_AT.clear(); ws._JOB_CTL.clear()
    yield
    with ws._JOBS_LOCK:
        ws._JOBS.clear(); ws._JOBS_SUBMITTED_AT.clear(); ws._JOB_CTL.clear()


# --------------------------------------------------------------------------- #
# the control block
# --------------------------------------------------------------------------- #
def test_cancel_flags_a_running_job():
    from concurrent.futures import Future
    fut = Future(); fut.set_running_or_notify_cancel()      # running: cannot be Future.cancel()ed
    ctl = {"cancel": False, "bars": 0}
    with ws._JOBS_LOCK:
        ws._JOBS["j1"] = fut; ws._JOBS_SUBMITTED_AT["j1"] = time.monotonic(); ws._JOB_CTL["j1"] = ctl

    out = ws._cancel_job("j1")
    assert out == {"cancelled": True, "was_running": True}
    assert ctl["cancel"] is True, "the running trial was never told to stop"
    assert "j1" not in ws._JOBS and "j1" not in ws._JOB_CTL


def test_cancel_a_queued_job_uses_future_cancel():
    """Not yet started -> Future.cancel() frees the slot outright, no cooperation needed."""
    from concurrent.futures import Future
    fut = Future()
    with ws._JOBS_LOCK:
        ws._JOBS["j2"] = fut; ws._JOBS_SUBMITTED_AT["j2"] = time.monotonic()
        ws._JOB_CTL["j2"] = {"cancel": False}
    out = ws._cancel_job("j2")
    assert out == {"cancelled": True, "was_running": False}
    assert fut.cancelled()


def test_cancel_unknown_job_404s():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        ws._cancel_job("nope")
    assert ei.value.status_code == 404


def test_cancel_without_a_control_block_reports_honestly():
    """No ctl (Manager unavailable) => it CANNOT be stopped. Say so rather than claim success --
    the master uses this only as best-effort cleanup and must not be misled."""
    from concurrent.futures import Future
    fut = Future(); fut.set_running_or_notify_cancel()
    with ws._JOBS_LOCK:
        ws._JOBS["j3"] = fut; ws._JOBS_SUBMITTED_AT["j3"] = time.monotonic()
    assert ws._cancel_job("j3") == {"cancelled": False, "was_running": True}


def test_job_status_drops_the_control_block_on_completion():
    """Otherwise _JOB_CTL leaks a Manager proxy per trial for the worker's whole uptime."""
    from concurrent.futures import Future
    fut = Future(); fut.set_result({"ok": True})
    with ws._JOBS_LOCK:
        ws._JOBS["j4"] = fut; ws._JOBS_SUBMITTED_AT["j4"] = time.monotonic()
        ws._JOB_CTL["j4"] = {"cancel": False}
    ws._job_status("j4")
    assert "j4" not in ws._JOB_CTL


def test_orphan_sweep_also_stops_the_trial():
    """The sweep used to drop the registry entry while the trial kept burning a slot."""
    from concurrent.futures import Future
    fut = Future(); fut.set_running_or_notify_cancel()
    ctl = {"cancel": False}
    with ws._JOBS_LOCK:
        ws._JOBS["old"] = fut
        ws._JOBS_SUBMITTED_AT["old"] = time.monotonic() - (ws._JOBS_MAX_ORPHAN_AGE + 60)
        ws._JOB_CTL["old"] = ctl
    ws._sweep_orphaned_jobs()
    assert ctl["cancel"] is True
    assert "old" not in ws._JOBS


# --------------------------------------------------------------------------- #
# the trial side
# --------------------------------------------------------------------------- #
def test_progress_cb_raises_once_cancelled(monkeypatch):
    """Interval forced to 0 so this asserts the CANCELLATION, not the throttle."""
    import app.services.strategy_optimization_handler as h
    monkeypatch.setattr(h, "_CANCEL_CHECK_INTERVAL_S", 0.0)
    ctl = {"cancel": False}
    cb = h._cancel_progress_cb(ctl)
    cb(0.1, "bar")                      # not cancelled -> silent
    ctl["cancel"] = True
    with pytest.raises(h.TrialCancelled):
        cb(0.2, "bar")


def test_progress_cb_does_not_hit_the_proxy_on_every_call():
    """ctl is a multiprocessing.Manager PROXY -- every read is a cross-process round-trip, so
    reading it per progress_cb call puts IPC on the engine's per-bar hook. Only the throttled
    reads may touch it."""
    from app.services.strategy_optimization_handler import _cancel_progress_cb

    class CountingCtl(dict):
        reads = 0
        def get(self, k, *a):
            CountingCtl.reads += 1
            return False

    cb = _cancel_progress_cb(CountingCtl())
    for _ in range(1000):
        cb(0.5, "bar")
    assert CountingCtl.reads <= 2, f"proxy read {CountingCtl.reads} times in 1000 calls"


def test_no_control_block_means_no_callback():
    """Uncancellable path must stay byte-identical to the old behaviour: run_daily_backtest
    substitutes its own no-op when progress_cb is None."""
    from app.services.strategy_optimization_handler import _cancel_progress_cb
    assert _cancel_progress_cb(None) is None


def test_a_dead_manager_never_fails_a_healthy_trial():
    """If the proxy raises (manager gone), the trial must carry on -- losing cancellability is
    acceptable, killing a good trial is not."""
    from app.services.strategy_optimization_handler import _cancel_progress_cb

    class Dead:
        def get(self, *_a, **_k):
            raise EOFError("manager gone")

    _cancel_progress_cb(Dead())(0.5, "bar")   # must not raise


def test_cancelled_trial_is_reported_retryable_not_zero_fitness(monkeypatch):
    """A cancel is not a bad genome. Recording it as a real 0-fitness result would teach the GA
    that a perfectly good individual is worthless."""
    import app.services.strategy_optimization_handler as h

    def _boom(cfg, progress_cb=None):
        raise h.TrialCancelled("cancelled by master (abandoned)")

    import sys, types
    mod = types.ModuleType("app.services.backtest.daily_backtest_handler")
    mod.run_daily_backtest = _boom
    monkeypatch.setitem(sys.modules, "app.services.backtest.daily_backtest_handler", mod)

    out = h._trial_worker({}, "sharpe_ratio", {"cancel": True})
    assert out["ok"] is False
    assert out.get("cancelled") is True
    assert out.get("retryable") is True
    assert out.get("fatal") is False


# --------------------------------------------------------------------------- #
# master side
# --------------------------------------------------------------------------- #
def test_timeout_asks_the_worker_to_cancel(monkeypatch):
    from app.services import worker_client as wc
    seen = {}

    monkeypatch.setattr(wc, "cancel_job", lambda w, jid, **k: seen.update(job=jid) or {"cancelled": True})

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"job_id": "J", "status": "running"}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _Resp()
        def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(wc.httpx, "Client", _Client)
    monkeypatch.setattr(wc.time, "sleep", lambda *_: None)
    # monotonic must ADVANCE past the deadline. A constant value hangs forever: the deadline is
    # monotonic()+timeout, so `now >= deadline` is never true and sleep is stubbed out.
    ticks = iter([0.0, 0.0, 10_000.0] + [10_000.0] * 50)
    monkeypatch.setattr(wc.time, "monotonic", lambda: next(ticks))

    with pytest.raises(TimeoutError):
        wc._submit_and_poll({"name": "w", "url": "http://x", "password": "p"},
                            "/submit-trial", {}, timeout=1.0)
    assert seen.get("job") == "J", "gave up without telling the worker to stop"


def test_cancel_job_never_raises(monkeypatch):
    """It runs on the failure path; an exception here would mask the real error."""
    from app.services import worker_client as wc

    class _Boom:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): raise OSError("connection refused")

    monkeypatch.setattr(wc.httpx, "Client", _Boom)
    out = wc.cancel_job({"name": "w", "url": "http://x", "password": "p"}, "J")
    assert out["cancelled"] is False


def test_progress_cb_writes_the_bars_heartbeat():
    """REGRESSION (2026-08-06): `bars` was declared in the control block on 2026-07-28 and never
    written by anything. `_job_status` therefore had no liveness to report, the master could not
    tell a queued trial from a running one, and opt 251 burned ~9h waiting out 3h timeouts on a
    worker whose pool was jammed. The counter going inert again would silently restore that."""
    from app.services.strategy_optimization_handler import _cancel_progress_cb

    ctl = {"cancel": False, "bars": 0}
    cb = _cancel_progress_cb(ctl)
    for _ in range(3):
        cb(0.5, "bar 2024-01-02")

    assert ctl["bars"] > 0, "progress_cb must publish a heartbeat, not just read the cancel flag"


def test_progress_cb_still_honours_cancel_alongside_the_heartbeat():
    from app.services.strategy_optimization_handler import _cancel_progress_cb, TrialCancelled

    ctl = {"cancel": True, "bars": 0}
    cb = _cancel_progress_cb(ctl)
    with pytest.raises(TrialCancelled):
        cb(0.5, "bar 2024-01-02")
