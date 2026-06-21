"""DistributedEvaluator gate — master-as-worker consumers + broker barrier + order-correct fits.

Uses a fake pool so the test is fast + deterministic and asserts the key property the GA relies
on: ``execute_jobs`` returns each job's result keyed by its INPUT index, independent of the
(concurrent, out-of-order) completion order. This is the determinism/correctness contract that
makes "where a trial ran" irrelevant.
"""
from app.services.distributed_eval import DistributedEvaluator
from app.services.trial_broker import TrialBroker


class _FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _FakePool:
    """Stands in for the ProcessPoolExecutor: computes a deterministic fitness from the config
    (ignores the passed ``_trial_worker`` fn). config['v'] -> fitness v*2."""

    def submit(self, _fn, config, _metric):
        return _FakeFuture({"ok": True, "fitness": float(config["v"]) * 2.0,
                            "trades": int(config["v"]), "error": None})


def test_execute_jobs_returns_all_in_order():
    broker = TrialBroker()
    ev = DistributedEvaluator(_FakePool(), "sharpe", n_consumers=4,
                              optimization_id="t", broker=broker)
    ev.start()
    try:
        n = 50
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(n)]
        results = list(ev.execute_jobs(jobs))
    finally:
        ev.stop()

    assert len(results) == n
    # Reassemble by index and assert the deterministic fitness for each.
    by_idx = {i: out for (i, _flat, _key, out) in results}
    assert set(by_idx) == set(range(n))           # every index present exactly once
    for i in range(n):
        assert by_idx[i]["fitness"] == i * 2.0     # order-independent correctness
        assert by_idx[i]["trades"] == i


def test_stop_clears_broker_state():
    broker = TrialBroker()
    ev = DistributedEvaluator(_FakePool(), "m", n_consumers=2, optimization_id="t", broker=broker)
    ev.start()
    list(ev.execute_jobs([(0, {}, "k0", {"v": 1})]))
    ev.stop()
    assert broker.stats() == {"pending": 0, "claimed": 0, "results": 0, "seen": 0}
