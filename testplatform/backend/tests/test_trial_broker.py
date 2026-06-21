"""TrialBroker unit gate — atomic claim (no double-execution), result/wait barrier, requeue.

The broker is what lets local consumer threads and remote HTTP workers pull from ONE queue
without ever running a trial twice; these tests pin that invariant + the fault-tolerance requeue.
"""
import threading

from app.services.trial_broker import TrialBroker, get_broker


def test_singleton():
    assert get_broker() is get_broker()


def test_submit_claim_result_wait():
    b = TrialBroker()
    tid = b.submit_one("opt", {"v": 5}, "sharpe")
    job = b.claim("w1")
    assert job["trial_id"] == tid and job["config"] == {"v": 5}
    assert b.claim("w2") is None  # queue now empty
    b.post_result(tid, {"ok": True, "fitness": 9.0})
    ready = b.wait_ready({tid}, timeout=1.0)
    assert ready == {tid: {"ok": True, "fitness": 9.0}}
    # result is drained — a second wait times out
    assert b.wait_ready({tid}, timeout=0.05) == {}


def test_concurrent_claims_never_duplicate():
    b = TrialBroker()
    n = 200
    ids = {b.submit_one("opt", {"i": i}, "m") for i in range(n)}
    claimed = []
    lock = threading.Lock()

    def worker():
        while True:
            job = b.claim("w")
            if job is None:
                return
            with lock:
                claimed.append(job["trial_id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == n              # every trial claimed exactly once
    assert set(claimed) == ids
    assert len(set(claimed)) == n         # no duplicates


def test_duplicate_result_ignored():
    b = TrialBroker()
    tid = b.submit_one("opt", {}, "m")
    b.claim("w")
    assert b.post_result(tid, {"fitness": 1.0}) is True
    # a requeue race could post a second result — first wins, second ignored
    assert b.post_result(tid, {"fitness": 2.0}) is False
    assert b.wait_ready({tid}, timeout=1.0)[tid]["fitness"] == 1.0


def test_requeue_stale():
    b = TrialBroker()
    tid = b.submit_one("opt", {}, "m")
    b.claim("dead-worker")
    assert b.stats()["pending"] == 0 and b.stats()["claimed"] == 1
    # nothing stale yet at a long timeout
    assert b.requeue_stale(claim_timeout=10_000) == 0
    # everything stale at timeout 0 -> back on the queue
    assert b.requeue_stale(claim_timeout=0) == 1
    assert b.stats()["pending"] == 1 and b.stats()["claimed"] == 0
    assert b.claim("w2")["trial_id"] == tid  # re-claimable


def test_clear_scoped_by_optimization():
    b = TrialBroker()
    a1 = b.submit_one("A", {}, "m")
    b.submit_one("B", {}, "m")
    b.clear("A")
    s = b.stats()
    assert s["pending"] == 1  # only B remains
    # A's trial is gone
    seen = set()
    while True:
        j = b.claim("w")
        if j is None:
            break
        seen.add(j["optimization_id"])
    assert seen == {"B"} and a1 not in seen
