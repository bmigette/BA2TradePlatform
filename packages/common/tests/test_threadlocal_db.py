"""Thread-local DB override: parallel threads get isolated engines; the global is untouched.

This backs the multi-threaded backtest optimizer — each trial thread points ba2_common at
its OWN per-run sqlite via configure_db_threadlocal(), so concurrent trials never clobber
each other's DB, while threads without an override keep using the shared global engine."""
import os
import tempfile
import threading

from ba2_common.core import db


def test_threadlocal_override_isolates_threads_and_preserves_global():
    d = tempfile.mkdtemp()
    db.configure_db(os.path.join(d, "global.sqlite"))
    global_engine = db.get_engine()

    results = {}
    barrier = threading.Barrier(3)

    def worker(i):
        db.configure_db_threadlocal(os.path.join(d, f"trial_{i}.sqlite"))
        barrier.wait()  # all three hold their override at once
        results[i] = str(db.get_engine().url)
        db.clear_threadlocal_db()
        results[f"{i}_after"] = str(db.get_engine().url)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(3):
        assert f"trial_{i}.sqlite" in results[i]          # isolated per thread
        assert "global.sqlite" in results[f"{i}_after"]   # restored after clear
    # The main thread (no override) still resolves the SAME global engine, unchanged.
    assert db.get_engine() is global_engine


def test_no_override_uses_global():
    d = tempfile.mkdtemp()
    db.configure_db(os.path.join(d, "only.sqlite"))
    assert "only.sqlite" in str(db.get_engine().url)
    db.clear_threadlocal_db()  # no-op when nothing set
    assert "only.sqlite" in str(db.get_engine().url)
