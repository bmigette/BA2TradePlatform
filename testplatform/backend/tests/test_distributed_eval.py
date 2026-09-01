"""DistributedEvaluator gate (PUSH model) — local consumers + remote dispatch + requeue fallback.

Uses a fake pool and a monkeypatched ``worker_client`` so the test is fast/deterministic and
asserts the contract the GA relies on: ``execute_jobs`` returns each job's result keyed by its
INPUT index regardless of where/when it ran, and a failing remote worker's trials fall back to
local (never lost).
"""
from concurrent.futures.process import BrokenProcessPool

import app.services.distributed_eval as de
from app.services.distributed_eval import DistributedEvaluator


def _fitness(config):
    return float(config["v"]) * 2.0


class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _FakePool:
    """Local pool: computes a deterministic fitness from config['v'] (ignores _trial_worker)."""
    def submit(self, _fn, config, _metric):
        return _FakeFuture({"ok": True, "fitness": _fitness(config), "trades": int(config["v"]), "error": None})


def test_local_only_returns_all_in_order():
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=4, optimization_id="t", workers=[])
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(40)]
        results = list(ev.execute_jobs(jobs))
    finally:
        ev.stop()
    by_idx = {i: out for (i, _f, _k, out) in results}
    assert set(by_idx) == set(range(40))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(40))


class _SlowPool:
    """Local pool that is slow, so in the test the (fast) remote dispatchers win most trials —
    making remote participation deterministic without a flaky race against an instant local pool."""
    def submit(self, _fn, config, _metric):
        import time
        time.sleep(0.15)
        return _FakeFuture({"ok": True, "fitness": _fitness(config), "trades": int(config["v"]), "error": None})


def test_remote_dispatch(monkeypatch):
    """A healthy remote worker runs trials via worker_client.run_trial; results stay order-correct."""
    seen = []

    def fake_run_trial(worker, config, metric, **kw):
        seen.append(worker["name"])
        return {"ok": True, "fitness": _fitness(config), "trades": 1, "error": None}

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 3})
    monkeypatch.setattr(de.worker_client, "run_trial", fake_run_trial)

    workers = [{"id": 1, "name": "box1", "url": "http://x", "password": "p"}]
    # 1 SLOW local consumer + 3 fast remote slots -> remote handles the bulk (deterministic).
    ev = DistributedEvaluator(_SlowPool(), "sharpe", n_consumers=1, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(30)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(30))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(30))
    assert seen  # the remote worker actually ran at least some trials


def test_failing_worker_falls_back_to_local(monkeypatch):
    """If the only remote worker errors on every trial, trials are requeued + finished locally."""
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 2})

    def boom(worker, config, metric, **kw):
        raise RuntimeError("worker down")
    monkeypatch.setattr(de.worker_client, "run_trial", boom)

    workers = [{"id": 1, "name": "deadbox", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=2, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(12)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    # Every trial still completes (locally), with the correct deterministic fitness.
    assert set(by_idx) == set(range(12))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(12))


def test_max_remote_slots_per_worker_caps_dispatcher_count(monkeypatch):
    """A worker reporting capacity=8 only gets ``max_remote_slots_per_worker`` dispatcher
    threads (and the fleet-state report reflects the capped count), not its full capacity —
    regression for capping memory-heavy experts (e.g. FMPSenateTraderWeight) below a worker's
    advertised /health capacity."""
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 8})
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: {"ok": True, "fitness": _fitness(config),
                                                          "trades": 1, "error": None})

    workers = [{"id": 1, "name": "remote150", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=1, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None,
                              max_remote_slots_per_worker=4)
    ev.start()
    try:
        remote_threads = [t for t in ev._threads if t.name.startswith("remote-remote150-")]
        assert len(remote_threads) == 4  # capped, not the worker's reported 8
        assert ev._active_workers[0]["capacity"] == 8  # reported capacity is untouched
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(10)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(10))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(10))


def test_flaky_health_check_is_retried_and_the_exception_is_logged(monkeypatch):
    """A /health call that fails ONCE (transient timeout/blip) must not be treated as gospel
    and permanently pin the job to 1 slot -- opt 420's real-world failure mode: the bare
    ``except Exception: capacity = 1`` swallowed the error silently and never retried. Here the
    second attempt succeeds and reports the worker's real capacity, which must win."""
    calls = {"health": 0}
    logs = []

    def flaky_health(w, **k):
        calls["health"] += 1
        if calls["health"] == 1:
            raise TimeoutError("read timed out")
        return {"capacity": 24, "capacity_max": 24}

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", flaky_health)
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: {"ok": True, "fitness": _fitness(config),
                                                          "trades": 1, "error": None})

    workers = [{"id": 1, "name": "remote227", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=0, optimization_id="t",
                              workers=workers, master_version="abc", log=logs.append)
    ev.start()
    try:
        assert calls["health"] == 2  # retried once after the first failure
        assert ev._active_workers[0]["capacity"] == 24  # the real value, not the 1-slot fallback
        assert ev._active_workers[0]["capacity_max"] == 24
        remote_threads = [t for t in ev._threads if t.name.startswith("remote-remote227-")]
        assert len(remote_threads) == 24
        assert any("timed out" in m or "TimeoutError" in m for m in logs), \
            "the /health failure must be logged, not silently swallowed"
    finally:
        ev.stop()


def test_health_permanently_down_falls_back_to_the_workers_configured_max(monkeypatch):
    """When /health never recovers (both attempts fail), the job must not be pinned to a bare
    1-slot guess -- it should discover the worker's actual configured ceiling via a pool-resize
    probe (the worker clamps an oversized request to its own daemon --workers ceiling and
    reports the real number back), and log why /health was unusable."""
    logs = []

    def always_fails(w, **k):
        raise TimeoutError("read timed out")

    def resize_reports_ceiling(w, workers, **k):
        # The worker clamps any request to its real daemon ceiling (here 24) and always answers
        # truthfully with its current capacity, even when it refuses the resize outright.
        return {"ok": True, "capacity": 24, "changed": True}

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", always_fails)
    monkeypatch.setattr(de.worker_client, "resize_pool", resize_reports_ceiling)
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: {"ok": True, "fitness": _fitness(config),
                                                          "trades": 1, "error": None})

    workers = [{"id": 1, "name": "remote227", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=0, optimization_id="t",
                              workers=workers, master_version="abc", log=logs.append)
    ev.start()
    try:
        assert ev._active_workers[0]["capacity"] == 24  # discovered, not a bare 1-slot guess
        remote_threads = [t for t in ev._threads if t.name.startswith("remote-remote227-")]
        assert len(remote_threads) == 24
        assert any("health" in m.lower() and ("timed out" in m or "TimeoutError" in m)
                   for m in logs), "the persistent /health failure must be logged"
    finally:
        ev.stop()


def test_health_and_resize_both_down_falls_back_to_one_slot_with_a_clear_log(monkeypatch):
    """The true last resort (nothing about the worker is discoverable at all) still keeps the
    worker usable at 1 slot rather than excluding it outright -- but must say so loudly, unlike
    the old silent ``except Exception: capacity = 1``."""
    logs = []

    def always_fails(w, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", always_fails)
    monkeypatch.setattr(de.worker_client, "resize_pool", always_fails)
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: {"ok": True, "fitness": _fitness(config),
                                                          "trades": 1, "error": None})

    workers = [{"id": 1, "name": "remote227", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=0, optimization_id="t",
                              workers=workers, master_version="abc", log=logs.append)
    ev.start()
    try:
        assert ev._active_workers[0]["capacity"] == 1
        assert any("1 slot" in m or "1-slot" in m for m in logs)
    finally:
        ev.stop()


def test_unsynced_worker_excluded(monkeypatch):
    """A worker that can't be version-matched is dropped; the run proceeds local-only."""
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: False)
    called = {"push": False, "run": False}
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: called.update(push=True) or {})
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda *a, **k: called.update(run=True) or {"ok": True, "fitness": 0})

    workers = [{"id": 1, "name": "stale", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=2, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    try:
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs([(0, {}, "k0", {"v": 7})])}
    finally:
        ev.stop()
    assert by_idx[0]["fitness"] == 14.0
    assert called["push"] is False and called["run"] is False  # excluded before any use


def test_recovered_worker_is_readmitted_mid_run(monkeypatch):
    """A worker excluded at pre-flight starts serving trials once it starts syncing, without
    needing a fresh job — re-checked every n_consumers individuals completed."""
    synced = {"ok": False}
    seen = []

    def fake_ensure_synced(w, c, **k):
        return synced["ok"]

    def fake_run_trial(worker, config, metric, **kw):
        seen.append(worker["name"])
        return {"ok": True, "fitness": _fitness(config), "trades": 1, "error": None}

    monkeypatch.setattr(de.worker_client, "ensure_synced", fake_ensure_synced)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 1})
    monkeypatch.setattr(de.worker_client, "run_trial", fake_run_trial)

    workers = [{"id": 1, "name": "recovering", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_SlowPool(), "sharpe", n_consumers=1, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    assert ev._down_workers and not ev._active_workers  # excluded at pre-flight (not yet synced)
    try:
        # "Bring the worker back" partway through, then let individuals keep completing so the
        # every-n_consumers recheck (n_consumers=1 here) gets a chance to re-admit it.
        synced["ok"] = True
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(30)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(30))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(30))
    assert seen  # the recovered worker actually picked up trials before the job finished
    assert ev._active_workers and not ev._down_workers


class _DeadFuture:
    def result(self):
        raise BrokenProcessPool("A process in the process pool was terminated abruptly "
                                 "while the future was running or pending.")


class _DeadPool:
    """A local pool whose FIRST submission (and every one after, matching the real
    ProcessPoolExecutor contract) raises BrokenProcessPool -- simulates a crashed worker."""
    def submit(self, _fn, _config, _metric):
        return _DeadFuture()


def test_broken_local_pool_recovers_via_pool_factory():
    """A dead local pool is replaced (not left broken for the rest of the run) when a
    pool_factory is supplied, and the trials that hit it are requeued -- not lost -- so a
    single-local-consumer, no-remote-worker run still finishes every trial."""
    built = {"count": 0}

    def factory():
        built["count"] += 1
        return _FakePool()  # the "recovered" pool behaves normally

    ev = DistributedEvaluator(_DeadPool(), "sharpe", n_consumers=1, optimization_id="t",
                              workers=[], log=lambda *_: None, pool_factory=factory)
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(10)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(10))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(10))
    assert built["count"] == 1  # recreated exactly once, not once per failed trial
    assert ev.pool is not None and not isinstance(ev.pool, _DeadPool)


def test_broken_local_pool_without_factory_degrades_to_remote(monkeypatch):
    """No pool_factory (the historical call sites) -> local trials keep failing over to remote,
    matching the pre-fix behaviour (no crash, no trial lost, just no local recovery)."""
    def fake_run_trial(worker, config, metric, **kw):
        return {"ok": True, "fitness": _fitness(config), "trades": 1, "error": None}

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 2})
    monkeypatch.setattr(de.worker_client, "run_trial", fake_run_trial)

    workers = [{"id": 1, "name": "box1", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_DeadPool(), "sharpe", n_consumers=1, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(10)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(10))  # remote covers every trial the dead local pool can't
    assert isinstance(ev.pool, _DeadPool)  # left as-is (no factory to rebuild it)


def test_remote_retryable_failure_requeues_instead_of_posting(monkeypatch):
    """A worker that returns HTTP-200 with ``retryable: True`` (its own trial pool broke —
    e.g. WinError 1450 killing a child) must have the trial REQUEUED, never recorded as a
    failed result: the failure says nothing about the genome. Regression for the 2026-07-09
    incident where 317 such results were accepted and fed fitness 0.0 to the GA."""
    calls = {"n": 0}

    def flaky_then_ok(worker, config, metric, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:  # first few dispatches: worker-side pool broken (retryable)
            return {"ok": False, "fitness": 0.0, "trades": 0,
                    "error": "BrokenProcessPool('child died')", "retryable": True}
        return {"ok": True, "fitness": _fitness(config), "trades": 1, "error": None}

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 2})
    monkeypatch.setattr(de.worker_client, "run_trial", flaky_then_ok)

    workers = [{"id": 1, "name": "flaky150", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_SlowPool(), "sharpe", n_consumers=1, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(8)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    # Every trial completes with a REAL fitness — no retryable failure leaked into results.
    assert set(by_idx) == set(range(8))
    assert all(by_idx[i]["ok"] for i in range(8))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(8))


def test_remote_repeated_retryable_failures_bench_the_worker(monkeypatch):
    """A worker that ONLY returns retryable failures gets benched after _MAX_WORKER_FAILURES
    (trials fall back to local) instead of spinning forever."""
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 2})
    monkeypatch.setattr(
        de.worker_client, "run_trial",
        lambda *a, **k: {"ok": False, "fitness": 0.0, "trades": 0,
                         "error": "BrokenProcessPool('dead')", "retryable": True})

    workers = [{"id": 1, "name": "dead150", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=2, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    try:
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(10)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(10))
    assert all(by_idx[i]["ok"] for i in range(10))  # local finished everything


def test_zero_consumers_worker_excluded_at_preflight_still_recovers(monkeypatch):
    """Regression for a real deadlock: a remote-only run (n_consumers=0) whose ONLY worker is
    excluded at pre-flight has NO local consumer to ever drive execute_jobs' completion-based
    re-admission cadence (nothing can complete -> nothing calls _maybe_recheck_async -> the
    healthy worker is never re-tried). Hit for real: opt 357 (goal2020 matrix1/2 on remote227,
    parallel=0) sat at "0 local + 0 remote slot(s) across 0 worker(s)" forever after remote227's
    self-update restart made its pre-flight lose the race, even though the worker came back
    within seconds. Needs an independent TIMER, not a completion-driven one."""
    monkeypatch.setattr(de, "_DOWN_WORKER_RECHECK_S", 0.05)   # fast for the test
    synced = {"ok": False}
    seen = []

    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: synced["ok"])
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 2})
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: seen.append(w["name"]) or
                        {"ok": True, "fitness": _fitness(config), "trades": 1, "error": None})

    workers = [{"id": 1, "name": "remote227", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    ev.start()
    assert ev._down_workers and not ev._active_workers   # excluded at pre-flight, as it was live
    try:
        # "The worker comes back" a moment after start() -- nothing local or remote is running
        # yet to notice on its own; only the independent timer can pick this up.
        synced["ok"] = True
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(10)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(10))
    assert seen, "the recovered worker never ran a single trial -- still deadlocked"
    assert ev._active_workers and not ev._down_workers


def test_zero_local_consumers_dispatches_remote_only(monkeypatch):
    """n_consumers=0 (a run with no local trial slots) must not be floored to 1, must spawn no
    local consumer thread, and must never touch the local pool -- passing pool=None here means
    any code path that DOES touch it fails loudly instead of masking the bug with a working fake.
    Regression for two bugs: n_consumers=0 silently became 1 (`max(1, n_consumers)`), and
    execute_jobs' re-admission cadence (`completed % self.n_consumers`) divides by zero once it
    isn't floored."""
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 3})
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: {"ok": True, "fitness": _fitness(config),
                                                          "trades": 1, "error": None})

    workers = [{"id": 1, "name": "remote227", "url": "http://x", "password": "p"}]
    ev = DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t",
                              workers=workers, master_version="abc", log=lambda *_: None)
    assert ev.n_consumers == 0
    ev.start()
    try:
        assert not any(t.name.startswith("local-trial-consumer-") for t in ev._threads)
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(10)]
        by_idx = {i: out for (i, _f, _k, out) in ev.execute_jobs(jobs)}
    finally:
        ev.stop()
    assert set(by_idx) == set(range(10))
    assert all(by_idx[i]["fitness"] == i * 2.0 for i in range(10))
