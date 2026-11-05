"""Incident 2026-10-12: FMP 429s at the 09:30 collision were stored as `no_spot` tombstones and every
iv_rank gate stayed closed. A transient spot-source failure must never become a tombstone; the pre-open
warm and the per-pass gate summary make the failure survivable and visible."""
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from ba2_trade_platform.modules.dataproviders.options import atm_iv_task_hook as K
from tests.atm_iv_fakes import FakeSpots, FakeWorld, make_provider, reset_module_state

END = date(2026, 3, 6)
LOOK = 60
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    with K._PASS_LOCK:
        K._PASS.clear()
    yield
    reset_module_state()


class FlakySpots:
    """Raises a rate-limit error for the first ``fail_first`` calls (what FmpSpotSource wraps into
    SpotSourceTransient), then delegates."""

    def __init__(self, world, fail_first, always=False):
        self.inner = FakeSpots(world)
        self.fail_first, self.always, self.calls = fail_first, always, 0

    def spots(self, symbol, start, end):
        self.calls += 1
        if self.always or self.calls <= self.fail_first:
            raise H.SpotSourceTransient("TEST: spot fetch failed: FMPError: HTTP 429") from RuntimeError("429")
        return self.inner.spots(symbol, start, end)


def _reasons(prov, sym="TEST"):
    df = prov.read_store(sym)
    return [] if df.empty else list(df["reason"])


# ---- (1) transient failure is never a tombstone ------------------------------------------------
def test_persistent_spot_failure_leaves_sessions_unresolved_then_next_call_fills(tmp_path):
    world = FakeWorld()
    spots = FlakySpots(world, fail_first=0, always=True)
    sleeps = []
    prov, world, bars, lister = make_provider(tmp_path, world, spot_source=spots, sleep=sleeps.append)
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_FILLING
    assert res.reason.startswith("spot source failing:")
    assert "no_spot" not in _reasons(prov) and _reasons(prov) == []      # nothing persisted at all
    assert spots.calls == H.SPOT_RETRIES_IN_TASK + 1 and len(sleeps) == H.SPOT_RETRIES_IN_TASK
    assert sleeps[0] > 0
    assert res.coverage.startswith("0/")
    # the source recovers: the very next call resumes and completes
    spots.always = False
    again = prov.ensure_filled("TEST", END, LOOK)
    assert again.status == H.STATUS_COMPLETE and again.covered == again.expected
    assert "no_spot" not in _reasons(prov)


def test_transient_failure_that_clears_within_the_retries_completes_in_one_call(tmp_path):
    world = FakeWorld()
    spots = FlakySpots(world, fail_first=2)
    prov, *_ = make_provider(tmp_path, world, spot_source=spots)
    res = prov.ensure_filled("TEST", END, LOOK, spot_retries=H.SPOT_RETRIES_WARM)
    assert res.status == H.STATUS_COMPLETE and res.reason is None
    assert spots.calls == 3 and "no_spot" not in _reasons(prov)


def test_backoff_honours_the_fmp_global_cooldown(tmp_path, monkeypatch):
    import ba2_providers.fmp_common as fc
    monkeypatch.setattr(fc, "gate_remaining_seconds", lambda: 20.0)
    prov, *_ = make_provider(tmp_path)
    assert prov._spot_backoff(1) == pytest.approx(20.0)
    monkeypatch.setattr(fc, "gate_remaining_seconds", lambda: 99.0)
    assert prov._spot_backoff(1) == pytest.approx(H.SPOT_GATE_WAIT_CAP_SECONDS)     # never sleeps longer than the cap


def test_successful_fetch_lacking_the_bar_still_writes_the_no_spot_tombstone(tmp_path):
    world = FakeWorld()
    hole = date(2026, 2, 26)
    world.no_spot_days = {hole}
    prov, *_ = make_provider(tmp_path, world)
    res = prov.ensure_filled("TEST", END, LOOK)
    assert res.status == H.STATUS_COMPLETE
    df = prov.read_store("TEST")
    assert list(df[df["reason"] == "no_spot"]["session_date"].dt.date) == [hole]


def test_fmp_spot_source_wraps_loader_and_split_failures_as_transient():
    def boom(sym, a, b):
        raise ConnectionError("reset")
    with pytest.raises(H.SpotSourceTransient) as ei:
        H.FmpSpotSource(ohlcv_loader=boom).spots("AAPL", END, END)
    assert ei.value.cause_name == "ConnectionError"

    df = pd.DataFrame({"Date": pd.to_datetime(["2026-03-05", "2026-03-06"]), "Open": [1.0, 1.0],
                       "High": [1.0, 1.0], "Low": [1.0, 1.0], "Close": [1.0, 1.0]})

    def bad_splits(sym):
        raise RuntimeError("FMP stock_split 429")
    with pytest.raises(H.SpotSourceTransient):
        H.FmpSpotSource(ohlcv_loader=lambda s, a, b: (df, None), split_loader=bad_splits).spots("AAPL", END, END)


# ---- (2) pre-open warm -------------------------------------------------------------------------
class _Acct:
    def __init__(self, prov):
        self.p = prov

    def _atm_iv_history(self):
        return self.p


def _clock(start):
    t = [start]

    def now():
        return t[0]

    def sleep(sec):
        from datetime import timedelta
        t[0] = t[0] + timedelta(seconds=sec)
    return now, sleep


def test_warm_fills_symbols_sequentially_and_logs_one_summary(tmp_path, caplog):
    prov, *_ = make_provider(tmp_path)
    now, sleep = _clock(datetime(2026, 3, 9, 8, 30, tzinfo=NY))
    seen = []
    orig = prov.ensure_filled

    def spy(sym, **kw):
        seen.append(sym)
        return orig("TEST", END, LOOK, **{k: v for k, v in kw.items() if k == "deadline_seconds"})
    prov.ensure_filled = spy
    out = K.warm_iv_history(targets={1: ["AAA", "BBB"]}, account_resolver=lambda i: _Acct(prov),
                            now=now, sleep=sleep, pace_seconds=1.0)
    assert seen == ["AAA", "BBB"]
    assert out["complete"] == 2 and out["filling"] == 0 and out["failed"] == 0


def test_warm_reports_filling_and_failed_with_reasons(tmp_path):
    world = FakeWorld()
    prov, *_ = make_provider(tmp_path, world, spot_source=FlakySpots(world, 0, always=True))
    now, sleep = _clock(datetime(2026, 3, 9, 8, 30, tzinfo=NY))

    class Boom:
        def _atm_iv_history(self):
            raise RuntimeError("no creds")
    accts = {1: _Acct(prov), 2: Boom()}
    out = K.warm_iv_history(targets={1: ["AAA"], 2: ["BBB"]}, account_resolver=accts.get,
                            now=now, sleep=sleep)
    assert out["filling"] == 1 and out["failed"] == 1
    assert any("spot source failing" in r for r in out["reasons"])


def test_warm_stops_at_the_0920_deadline(tmp_path):
    prov, *_ = make_provider(tmp_path)
    now, sleep = _clock(datetime(2026, 3, 9, 9, 18, tzinfo=NY))
    out = K.warm_iv_history(targets={1: [f"S{i}" for i in range(10)]}, account_resolver=lambda i: _Acct(prov),
                            now=now, sleep=sleep, pace_seconds=60.0)
    assert out["skipped_deadline"] > 0 and out["complete"] + out["filling"] + out["failed"] < 10
    assert out["complete"] + out["filling"] + out["failed"] + out["skipped_deadline"] == 10


def test_warm_never_runs_inside_the_0925_0945_window(tmp_path):
    prov, *_ = make_provider(tmp_path)
    for hh, mm in ((9, 25), (9, 30), (9, 44)):
        now, sleep = _clock(datetime(2026, 3, 9, hh, mm, tzinfo=NY))
        out = K.warm_iv_history(targets={1: ["AAA"]}, account_resolver=lambda i: pytest.fail("must not resolve"),
                                now=now, sleep=sleep)
        assert out.get("skipped_window") is True


def test_job_manager_schedules_the_warm_at_0830_et_mon_fri_without_a_scorer():
    from apscheduler.schedulers.background import BackgroundScheduler
    from ba2_trade_platform.core import JobManager as JM
    jm = JM.JobManager.__new__(JM.JobManager)
    jm._scheduler = BackgroundScheduler()
    jm._scheduler.start(paused=True)
    jm._scheduled_jobs = {}
    jm._lock = threading.Lock()
    try:
        jm._schedule_iv_warm_job()
        job = jm._scheduler.get_job(JM.IV_WARM_JOB_ID)
        assert job is not None
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "8" and fields["minute"] == "30" and fields["day_of_week"] == "mon-fri"
        assert str(job.trigger.timezone) == "America/New_York"
        assert job.max_instances == 1
        # part of the idempotent system-job list
        called = []
        for n in ("_schedule_account_refresh_job", "_schedule_iv_snapshot_job", "_schedule_fred_preopen_job",
                  "_schedule_warm_jobs"):
            setattr(jm, n, lambda: None)
        jm._schedule_iv_warm_job = lambda: called.append(1)
        jm._schedule_system_jobs()
        assert called == [1]
    finally:
        jm._scheduler.shutdown(wait=False)


def test_execute_warm_is_a_noop_without_gates(monkeypatch):
    from ba2_trade_platform.core import JobManager as JM
    jm = JM.JobManager.__new__(JM.JobManager)
    monkeypatch.setattr(K, "has_iv_rank_gates", lambda: False)
    monkeypatch.setattr(K, "warm_iv_history", lambda **k: pytest.fail("must not warm"))
    jm._execute_iv_warm()


# ---- (3) operator visibility -------------------------------------------------------------------
def _series(status, reason=None, values=(), end=END, tomb=()):
    return H.AtmIvSeries(symbol="X", end_session=end, status=status, values={d: 0.3 for d in values},
                         reason=reason, tombstone_days=list(tomb))


def test_gate_summary_is_one_entry_per_pass_with_reason_counts(monkeypatch):
    logged = []
    import ba2_common.core.db as dbm
    monkeypatch.setattr(dbm, "log_activity", lambda *a, **k: logged.append((a, k)))
    for i, sym in enumerate(["AAA", "BBB", "CCC"]):
        K.record_gate_outcome(7, "b1", sym, _series("complete", values=[]))              # no_spot
    K.record_gate_outcome(7, "b1", "DDD", _series("filling", "spot source failing: FMPError"))
    K.record_gate_outcome(7, "b1", "EEE", _series("complete", values=[END]))               # open
    K.record_gate_outcome(7, "b2", "AAA", _series("complete", values=[]))                  # other pass
    msg = K.flush_gate_report(7, "b1")
    assert msg.startswith("IV gate closed for 4 of 5 symbols:")
    assert "no_spot" in msg and "spot source failing: FMPError x1" in msg and "x3" in msg
    assert len(logged) == 1 and logged[0][1]["source_expert_id"] == 7
    assert K.flush_gate_report(7, "b1") is None and len(logged) == 1       # deduplicated
    assert K.flush_gate_report(7, "b2").startswith("IV gate closed for 1 of 1")


def test_open_gate_writes_nothing(monkeypatch):
    import ba2_common.core.db as dbm
    monkeypatch.setattr(dbm, "log_activity", lambda *a, **k: pytest.fail("no entry when nothing is closed"))
    K.record_gate_outcome(1, "b", "AAA", _series("complete", values=[END]))
    assert K.flush_gate_report(1, "b") is None


def test_task_seam_records_outcome_for_enter_market_only(mock_account_def):
    from tests.test_atm_iv_task_hook import _expert, IV
    inst = _expert(mock_account_def.id, IV)
    K.invalidate_gate_cache()
    world = FakeWorld()
    prov, *_ = make_provider(__import__("pathlib").Path(__import__("tempfile").mkdtemp()), world,
                             spot_source=FlakySpots(world, 0, always=True))
    K.ensure_for_analysis_task(inst.id, "TEST", "enter_market", account_resolver=lambda i: _Acct(prov),
                               batch_id="bx")
    with K._PASS_LOCK:
        entry = K._PASS[(inst.id, "bx")]
    assert "TEST" in entry["closed"] and "spot source failing" in entry["closed"]["TEST"]
