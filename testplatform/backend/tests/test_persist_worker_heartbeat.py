"""P4 (2026-10-24): a remote TOP-N persist re-run must publish the same per-bar heartbeat as a trial.

``worker_server`` submits ``/submit-trial-full`` jobs as ``_persist_trial_worker(config, ctl)`` but
that function ignored ``ctl``: ``run_daily_backtest`` ran with no progress callback, so
``/job-status`` reported ``bars: 0`` for the whole re-run and ``worker_client._submit_and_poll``
cancelled any persist longer than ``_NEVER_STARTED_GRACE`` (420 s) as "accepted but never started".
"""
import sys
import types

import pytest

import app.services.strategy_optimization_handler as h
from app.services import worker_client as wc


def _patch_backtest(monkeypatch, fn):
    mod = types.ModuleType("app.services.backtest.daily_backtest_handler")
    mod.run_daily_backtest = fn
    monkeypatch.setitem(sys.modules, "app.services.backtest.daily_backtest_handler", mod)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(h, "_CANCEL_CHECK_INTERVAL_S", 0.0)       # publish on every callback
    monkeypatch.setattr(h, "_release_option_overlays", lambda: {})
    monkeypatch.setattr(h, "_LOCAL_RETRY_BACKOFF_S", 0.0)
    import app.services.backtest.backtest_account as ba
    monkeypatch.setattr(ba, "assert_fill_rebase_enabled", lambda: None)


def test_a_slow_persist_publishes_bars_on_the_control_block(monkeypatch):
    def slow_backtest(cfg, progress_cb=None):
        assert progress_cb is not None, "persist ran without a heartbeat callback"
        for i in range(5):
            progress_cb(i / 5, f"bar {i}")       # a long re-run: many throttled callbacks
        return {"total_return": 1.0}

    _patch_backtest(monkeypatch, slow_backtest)
    ctl = {}
    out = h._persist_trial_worker({}, ctl)
    assert out["ok"] is True
    assert ctl["bars"] == 5, "the heartbeat the master's watchdog reads was never written"


def test_the_watchdog_would_have_cancelled_the_old_persist_but_not_the_new_one(monkeypatch):
    """Drive the REAL poller against a worker whose /job-status reports the ctl's bars."""
    clock = {"t": 0.0}
    monkeypatch.setattr(wc, "time", types.SimpleNamespace(
        monotonic=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + 60.0)))
    monkeypatch.setattr(wc, "cancel_job", lambda *a, **k: {"cancelled": True})

    def run(report_bars):
        clock["t"] = 0.0

        class Resp:
            status_code = 200

            def __init__(self, body): self._b = body
            def raise_for_status(self): pass
            def json(self): return self._b

        class Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

            def post(self, *a, **k): return Resp({"job_id": "J"})

            def get(self, *a, **k):
                t = clock["t"]
                if t >= 600.0:                     # a 10-minute persist, done at t=600
                    return Resp({"status": "done", "result": {"ok": True}})
                return Resp({"status": "running", "bars": int(t // 60) + 1 if report_bars else 0})

        monkeypatch.setattr(wc.httpx, "Client", Client)
        return wc._submit_and_poll({"name": "w", "url": "http://x", "password": "p"},
                                   "/submit-trial-full", {}, timeout=10_000.0)

    with pytest.raises(wc.WorkerJobStalled):
        run(report_bars=False)        # what the old persist looked like: bars stuck at 0
    assert run(report_bars=True) == {"ok": True}


def test_a_cancelled_persist_is_not_retried(monkeypatch):
    calls = []

    def cancelled(cfg, progress_cb=None):
        calls.append(1)
        raise h.TrialCancelled("cancelled by master (abandoned)")

    _patch_backtest(monkeypatch, cancelled)
    out = h._persist_trial_worker({}, {"cancel": True})
    assert out["ok"] is False and out.get("cancelled") is True and out.get("retryable") is True
    assert len(calls) == 1, "a cancelled re-run was retried with backoff"


def test_persist_without_a_control_block_is_unchanged(monkeypatch):
    seen = {}

    def bt(cfg, progress_cb=None):
        seen["cb"] = progress_cb
        return {"x": 1}

    _patch_backtest(monkeypatch, bt)
    assert h._persist_trial_worker({}) == {"ok": True, "results": {"x": 1}}
    assert seen["cb"] is None
