"""worker_client.run_trial/run_trial_full — submit-then-poll (2026-07-19), replacing the old
single-blocking-POST design. Mocks httpx, no real network or worker process involved."""
from unittest.mock import MagicMock, patch

import pytest

from app.services import worker_client


WORKER = {"id": 1, "name": "remote1", "url": "http://remote1:8100", "password": "secret"}


def _resp(status_code=200, json_body=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    if status_code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        r.raise_for_status.return_value = None
    return r


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every test drives its own fake time; a real sleep would just slow the suite down."""
    monkeypatch.setattr(worker_client.time, "sleep", lambda _s: None)


def _patched_client(post_response=None, get_responses=None):
    """patch("httpx.Client") returning a MagicMock whose .post()/.get() are pre-scripted --
    mirrors test_sync_client.py's pattern for this same module family."""
    mock_client = MagicMock()
    if post_response is not None:
        mock_client.post.return_value = post_response
    if get_responses is not None:
        mock_client.get.side_effect = get_responses
    patcher = patch("httpx.Client")
    mock_cls = patcher.start()
    mock_cls.return_value.__enter__.return_value = mock_client
    return patcher, mock_client


def test_run_trial_submits_then_polls_until_done():
    patcher, mock_client = _patched_client(
        post_response=_resp(json_body={"job_id": "job-1"}),
        get_responses=[
            _resp(json_body={"status": "running"}),
            _resp(json_body={"status": "running"}),
            _resp(json_body={"status": "done",
                             "result": {"ok": True, "fitness": 7.5, "trades": 4, "error": None}}),
        ],
    )
    try:
        out = worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe")
    finally:
        patcher.stop()

    assert out == {"ok": True, "fitness": 7.5, "trades": 4, "error": None}
    assert mock_client.post.call_args[0][0] == "http://remote1:8100/submit-trial"
    assert mock_client.get.call_count == 3
    assert mock_client.get.call_args_list[0][0][0] == "http://remote1:8100/job-status/job-1"


def test_run_trial_full_hits_the_full_submit_path():
    patcher, mock_client = _patched_client(
        post_response=_resp(json_body={"job_id": "job-2"}),
        get_responses=[_resp(json_body={"status": "done", "result": {"ok": True, "results": {}}})],
    )
    try:
        out = worker_client.run_trial_full(WORKER, {"cfg": 1}, "sharpe")
    finally:
        patcher.stop()

    assert out == {"ok": True, "results": {}}
    assert mock_client.post.call_args[0][0] == "http://remote1:8100/submit-trial-full"


def test_run_trial_raises_worker_job_lost_on_404_poll():
    """A 404 on poll (job_id unknown -- almost always a worker restart mid-job) must raise
    immediately, not be retried as if it were 'still running'."""
    patcher, mock_client = _patched_client(
        post_response=_resp(json_body={"job_id": "job-3"}),
        get_responses=[_resp(status_code=404)],
    )
    try:
        with pytest.raises(worker_client.WorkerJobLost):
            worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe")
    finally:
        patcher.stop()
    assert mock_client.get.call_count == 1, "must fail on the FIRST 404, not keep polling"


def test_run_trial_raises_timeout_error_when_deadline_exceeded(monkeypatch):
    """If the job never reports done within the timeout budget, raise TimeoutError rather than
    poll forever."""
    patcher, mock_client = _patched_client(
        post_response=_resp(json_body={"job_id": "job-4"}),
        get_responses=[_resp(json_body={"status": "running"})] * 5,
    )
    # Simulate elapsed time jumping straight past the deadline on the second monotonic() call
    # (the first is the deadline computation itself) so the test doesn't need a real timeout.
    real_monotonic = worker_client.time.monotonic
    calls = {"n": 0}

    def _fake_monotonic():
        calls["n"] += 1
        return real_monotonic() + (0 if calls["n"] == 1 else 10_000)

    monkeypatch.setattr(worker_client.time, "monotonic", _fake_monotonic)
    try:
        with pytest.raises(TimeoutError):
            worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe", timeout=5.0)
    finally:
        patcher.stop()
    assert mock_client.get.call_count == 0, "deadline must be checked BEFORE the first poll"


# --- heartbeat / stall detection (2026-08-06) -------------------------------------------------
# Before this, "running" was the ONLY signal: a trial queued behind a saturated pool looked
# exactly like one computing, so the master could only wait out the full trial_timeout. opt 251
# lost ~9h that way (3h x 3 retries per slot, zero trials run).

def _clock(monkeypatch, step: float):
    """monotonic() that advances *step* seconds per call, so a test can drive elapsed time."""
    state = {"t": 0.0}

    def _fake():
        state["t"] += step
        return state["t"]

    monkeypatch.setattr(worker_client.time, "monotonic", _fake)
    return state


def test_never_started_job_fails_fast_instead_of_burning_the_whole_timeout(monkeypatch):
    """bars stuck at 0 == accepted but never scheduled. Waiting cannot help."""
    cancelled = []
    monkeypatch.setattr(worker_client, "cancel_job", lambda w, j, **k: cancelled.append(j))
    _clock(monkeypatch, step=60.0)
    patcher, mock_client = _patched_client(
        post_response=_resp(json_body={"job_id": "job-stuck"}),
        get_responses=[_resp(json_body={"status": "running", "bars": 0, "started": False})] * 50,
    )
    try:
        with pytest.raises(worker_client.WorkerJobStalled, match="never started"):
            worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe", timeout=10800.0)
    finally:
        patcher.stop()

    assert cancelled == ["job-stuck"], "must release the worker's slot on give-up"
    # The point of the fix: minutes, not the 10800s budget.
    assert mock_client.get.call_count < 12


def test_stalled_midrun_job_is_detected(monkeypatch):
    """Bars advanced, then stopped — the trial wedged after starting."""
    cancelled = []
    monkeypatch.setattr(worker_client, "cancel_job", lambda w, j, **k: cancelled.append(j))
    _clock(monkeypatch, step=60.0)
    patcher, _ = _patched_client(
        post_response=_resp(json_body={"job_id": "job-wedged"}),
        get_responses=([_resp(json_body={"status": "running", "bars": 3})] +
                       [_resp(json_body={"status": "running", "bars": 7})] * 60),
    )
    try:
        with pytest.raises(worker_client.WorkerJobStalled, match="stalled at bar 7"):
            worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe", timeout=10800.0)
    finally:
        patcher.stop()
    assert cancelled == ["job-wedged"]


def test_advancing_bars_are_never_treated_as_stalled(monkeypatch):
    """A slow but progressing trial must run to completion, well past both thresholds."""
    monkeypatch.setattr(worker_client, "cancel_job",
                        lambda w, j, **k: pytest.fail("a progressing trial must not be cancelled"))
    _clock(monkeypatch, step=60.0)
    polls = [_resp(json_body={"status": "running", "bars": n}) for n in range(1, 40)]
    polls.append(_resp(json_body={"status": "done", "result": {"ok": True, "fitness": 1.0}}))
    patcher, _ = _patched_client(
        post_response=_resp(json_body={"job_id": "job-slow"}), get_responses=polls)
    try:
        out = worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe", timeout=10800.0)
    finally:
        patcher.stop()
    assert out == {"ok": True, "fitness": 1.0}


def test_worker_too_old_to_report_bars_keeps_the_old_behaviour(monkeypatch):
    """A version-skewed worker omits `bars` entirely. It must never be failed for that —
    absence of the field means 'no heartbeat available', not 'no progress'."""
    monkeypatch.setattr(worker_client, "cancel_job",
                        lambda w, j, **k: pytest.fail("must not cancel a worker that lacks bars"))
    _clock(monkeypatch, step=60.0)
    polls = [_resp(json_body={"status": "running"})] * 30
    polls.append(_resp(json_body={"status": "done", "result": {"ok": True, "fitness": 2.0}}))
    patcher, _ = _patched_client(
        post_response=_resp(json_body={"job_id": "job-old"}), get_responses=polls)
    try:
        out = worker_client.run_trial(WORKER, {"cfg": 1}, "sharpe", timeout=10800.0)
    finally:
        patcher.stop()
    assert out == {"ok": True, "fitness": 2.0}
