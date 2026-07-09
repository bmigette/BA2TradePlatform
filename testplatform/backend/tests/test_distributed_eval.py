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
