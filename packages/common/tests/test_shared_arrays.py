"""ba2_common.core.shared_arrays -- one derived-cache mechanism for every read-only array set
a GA worker used to hold privately. See docs/plans/2026-09-14-shared-arrays-across-workers.md."""
import errno
import gc
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from ba2_common.core import shared_arrays as SA


def _src(tmp_path, name="a.parquet", payload=b"x" * 100):
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return p


def _arrays():
    return {
        "close": np.array([1.0, 2.5, np.nan], dtype="float64"),
        "bar_ord": np.array([737000, 737001, 737002], dtype="int32"),
        "is_call": np.array([True, False, True], dtype=bool),
        "empty": np.empty(0, dtype="float64"),
    }


def _write_done_dir(d: Path, marker_mtime=None) -> Path:
    """A published signature directory, written by hand so sweep() can be aimed at it."""
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "close.npy", np.array([1.0]), allow_pickle=False)
    (d / SA.DONE_MARKER).write_text(
        json.dumps({"arrays": ["close"], "schema": SA.SCHEMA_VERSION}), encoding="utf-8"
    )
    if marker_mtime is not None:
        os.utime(d / SA.DONE_MARKER, (marker_mtime, marker_mtime))
    return d


def _age(p: Path, seconds=10_000) -> None:
    old = time.time() - seconds
    os.utime(p, (old, old))


def test_build_then_open_returns_equal_arrays_not_memmap_subclass(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    # ONE source file for both calls: _src() rewrites it, and a rewrite advances mtime (~11ms
    # of NTFS tick here), which is a real source change and correctly forces a rebuild.
    src = _src(tmp_path)
    calls = []
    def build():
        calls.append(1)
        return _arrays()
    got = store.build_or_open("AAPL", [src], build)
    assert calls == [1]
    for k, v in _arrays().items():
        np.testing.assert_array_equal(got[k], v)
        assert got[k].dtype == v.dtype
        assert type(got[k]) is np.ndarray, "must be np.asarray-wrapped, not np.memmap"
    got2 = store.build_or_open("AAPL", [src], build)   # second open: no rebuild
    assert calls == [1]
    np.testing.assert_array_equal(got2["close"], _arrays()["close"])


def test_opened_arrays_are_file_backed_and_read_only(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    got = store.build_or_open("AAPL", [_src(tmp_path)], _arrays)
    assert isinstance(got["close"].base, np.memmap), "must be a view over the mapping itself"
    with pytest.raises((ValueError, TypeError)):
        got["close"][0] = 99.0


def test_signature_tracks_source_size_and_mtime(tmp_path):
    src = _src(tmp_path)
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    s1 = store.signature([src])
    assert s1 == store.signature([src])
    src.write_bytes(b"y" * 101)
    s2 = store.signature([src])
    assert s2 != s1
    os.utime(src, (time.time() + 100, time.time() + 100))
    assert store.signature([src]) != s2


def test_same_named_sources_in_different_directories_do_not_collide(tmp_path):
    """Basename identity is not identity: <symbol>/<year>.parquet trees are full of clashes."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    a = _src(tmp_path / "A", name="2020.parquet", payload=b"k" * 10)
    b = _src(tmp_path / "B", name="2020.parquet", payload=b"k" * 10)
    st = a.stat()
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns))     # same name, same size, same mtime
    assert a.stat().st_size == b.stat().st_size
    assert a.stat().st_mtime_ns == b.stat().st_mtime_ns
    assert store.signature([a]) != store.signature([b])


def test_signature_refuses_an_empty_source_list(tmp_path):
    """A constant hash over nothing is a cache entry that can never be invalidated."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    with pytest.raises(ValueError):
        store.signature([])
    with pytest.raises(ValueError):
        store.signature(iter(()))


def test_unusable_cache_keys_are_refused(tmp_path):
    """An empty/dotted key would collapse key_dir onto the store root and eviction would then
    wipe every OTHER key; a Windows device name would fail much later, on first write."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    for bad in ("", " ", ".", "..", "...", "PRN", "aux", "com1.parquet"):
        with pytest.raises(ValueError):
            store.key_dir(bad)
    assert store.key_dir("AAPL").name == "AAPL"
    # A separator inside a real ticker is SANITISED, not refused: BRK/B must stay usable, and
    # the result is still one segment, so it cannot collapse onto the root.
    assert store.key_dir("BRK/B").name == "BRK_B"
    assert store.key_dir("BRK/B").parent == store.root


def test_source_change_triggers_rebuild_in_a_new_directory(tmp_path):
    src = _src(tmp_path)
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    calls = []
    def build():
        calls.append(1)
        return _arrays()
    store.build_or_open("AAPL", [src], build)
    d1 = store.current_dir("AAPL", [src])
    src.write_bytes(b"z" * 200)
    store.build_or_open("AAPL", [src], build)
    d2 = store.current_dir("AAPL", [src])
    assert calls == [1, 1]
    assert d1 != d2 and d2.exists()


def test_a_fresh_sibling_survives_eviction_but_an_old_one_does_not(tmp_path):
    """A sibling published seconds ago may belong to a worker that has not opened it yet."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    d1 = store.current_dir("AAPL", [src])
    src.write_bytes(b"z" * 200)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()
    assert d1.exists(), "a freshly published sibling must not be evicted"
    _age(d1 / SA.DONE_MARKER)
    src.write_bytes(b"w" * 400)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()
    assert not d1.exists(), "a sibling older than LOCK_STALE_S is obsolete and must go"


def test_eviction_never_touches_the_lock_of_another_signature(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    old_dir = store.current_dir("AAPL", [src])
    _age(old_dir / SA.DONE_MARKER)
    other_lock = store.key_dir("AAPL") / "someothersignature.lock"
    other_lock.write_text("999")
    src.write_bytes(b"z" * 200)
    gc.collect()
    store.build_or_open("AAPL", [src], _arrays)          # runs _remove_stale_siblings
    assert not old_dir.exists()
    assert other_lock.exists(), "another builder's lock is not a stale sibling"


def test_done_marker_is_required_and_written_last(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()                                  # release the mapping before the rebuild
    d = store.current_dir("AAPL", [src])
    (d / SA.DONE_MARKER).unlink()
    calls = []
    def build():
        calls.append(1)
        return _arrays()
    store.build_or_open("AAPL", [src], build)   # no marker => not trusted => rebuilt
    assert calls == [1]
    marker = json.loads((store.current_dir("AAPL", [src]) / SA.DONE_MARKER).read_text())
    assert marker["arrays"] == sorted(_arrays())


def test_rebuild_of_untrusted_dir_while_arrays_are_held(tmp_path):
    """The one state Windows cannot dig itself out of, and the only acceptable outcomes.

    Arrays are HELD (a worker mid-trial), and the directory has lost its marker. On NTFS every
    escape is refused -- the mapped .npy cannot be deleted or renamed, and its parent cannot be
    renamed either (WinError 32/32/5) -- so the honest answer is a legible refusal naming the
    marker. On POSIX the rename-aside works and the rebuild simply succeeds. What must NEVER
    happen is a raw PermissionError escaping build_or_open at every worker, forever.
    """
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    held = store.build_or_open("AAPL", [src], _arrays)     # keep the mapping alive
    assert held["close"][0] == 1.0
    (store.current_dir("AAPL", [src]) / SA.DONE_MARKER).unlink()
    try:
        got = store.build_or_open("AAPL", [src], _arrays)
    except Exception as exc:
        assert isinstance(exc, RuntimeError) and SA.DONE_MARKER in str(exc), (
            f"the mapped-directory failure must be legible, got {exc!r}"
        )
    else:
        np.testing.assert_array_equal(got["close"], _arrays()["close"])
    assert held["close"][1] == 2.5, "the held mapping must survive the attempt either way"


def test_rebuild_of_marked_but_unreadable_dir_while_arrays_are_held(tmp_path):
    """The SECOND door into the same dead end, which N1 found still raising a raw WinError 5.

    The marker is present, so _publish skips the untrusted branch; but one array the marker
    lists is gone, so _try_open refuses, os.replace cannot land on the existing directory, and
    a held mapping blocks eviction. The only acceptable outcomes are the same two as above.
    """
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    held = store.build_or_open("AAPL", [src], _arrays)
    d = store.current_dir("AAPL", [src])
    for k in [k for k in held if k != "close"]:
        del held[k]                       # keep ONLY close.npy mapped
    gc.collect()
    (d / "bar_ord.npy").unlink()          # unmapped, so NTFS lets it go
    assert store._try_open(d) is None, "the premise: marked, but no longer readable"
    try:
        got = store.build_or_open("AAPL", [src], _arrays)
    except Exception as exc:
        assert isinstance(exc, RuntimeError) and "neither opened nor removed" in str(exc), (
            f"the mapped-directory failure must be legible, got {exc!r}"
        )
    else:
        np.testing.assert_array_equal(got["close"], _arrays()["close"])
    assert held["close"][1] == 2.5, "the held mapping must survive the attempt either way"


def test_two_concurrent_evictors_never_half_delete(tmp_path):
    """Two evictors renaming into the same .evict names used to roll back each other's work and
    both report "left intact" over a directory they had destroyed between them (3/5 trials)."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    names = [f"a{i}" for i in range(5)]
    expected = sorted([f"{n}.npy" for n in names] + [SA.DONE_MARKER])
    for attempt in range(5):
        d = store.key_dir("AAPL") / f"sig{attempt}"
        d.mkdir(parents=True)
        for n in names:
            np.save(d / f"{n}.npy", np.arange(3.0), allow_pickle=False)
        (d / SA.DONE_MARKER).write_text(
            json.dumps({"arrays": names, "schema": SA.SCHEMA_VERSION}), encoding="utf-8"
        )
        results = []
        guard = threading.Lock()
        def run():
            r = store._evict_dir(d)
            with guard:
                results.append(r)
        ts = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]

        if d.exists():
            left = sorted(p.name for p in d.iterdir())
            assert left == expected, f"attempt {attempt}: half-deleted directory {left}"
        # ">= 1", not "== 1": the threads are not guaranteed to overlap, and what must hold is
        # that the directory is never left half-deleted, not that a race actually happened.
        assert results.count(True) >= 1, f"attempt {attempt}: nobody removed it, got {results}"
        assert not d.exists(), f"attempt {attempt}: the winner must have removed it"
        claim = d.with_name(d.name + SA.EVICTING_SUFFIX)
        assert not claim.exists(), "the eviction claim must be released"


def _vanishing_claim(store, victim: Path):
    """Wrap _claim_eviction so ``victim`` disappears the instant the claim is won.

    That is what a concurrent sweep() doing its (correctly unguarded) orphan collection looks
    like from inside _publish: the directory was there at the exists() check and is gone by the
    time the probe runs, so _evict_dir reports False for a directory already out of the way.
    """
    original = store._claim_eviction

    def wrapper(claim, wait_s):
        won = original(claim, wait_s)
        if won and victim.exists():
            shutil.rmtree(victim)
        return won
    return wrapper


def _opens_or_refuses(call, phrase):
    """Either the rebuild opened, or it refused legibly naming ``phrase``. Never a raw OSError."""
    try:
        return call()
    except Exception as exc:
        assert isinstance(exc, RuntimeError) and phrase in str(exc), f"illegible: {exc!r}"
        return None


def test_publish_tolerates_a_final_that_vanishes_under_it(tmp_path, monkeypatch):
    """Both doors must read _evict_dir's False as "did not remove it", not "still in the way"."""
    # DOOR 1: untrusted final (no marker), collected by the "sweeper" mid-probe.
    store = SA.DerivedArrayStore(tmp_path / "d1")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()
    d = store.current_dir("AAPL", [src])
    (d / SA.DONE_MARKER).unlink()
    monkeypatch.setattr(store, "_claim_eviction", _vanishing_claim(store, d))
    got = store.build_or_open("AAPL", [src], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])

    # DOOR 2: marked but unreadable, likewise collected mid-probe.
    store2 = SA.DerivedArrayStore(tmp_path / "d2")
    src2 = _src(tmp_path, name="b.parquet")
    store2.build_or_open("AAPL", [src2], _arrays)
    gc.collect()
    d2 = store2.current_dir("AAPL", [src2])
    (d2 / "bar_ord.npy").unlink()
    assert store2._try_open(d2) is None, "the premise: marked, but not readable"
    monkeypatch.setattr(store2, "_claim_eviction", _vanishing_claim(store2, d2))
    got2 = store2.build_or_open("AAPL", [src2], _arrays)
    np.testing.assert_array_equal(got2["bar_ord"], _arrays()["bar_ord"])

    # ... while a final that is genuinely immovable still refuses, legibly.
    store3 = SA.DerivedArrayStore(tmp_path / "d3")
    src3 = _src(tmp_path, name="c.parquet")
    held = store3.build_or_open("AAPL", [src3], _arrays)
    (store3.current_dir("AAPL", [src3]) / SA.DONE_MARKER).unlink()
    _opens_or_refuses(lambda: store3.build_or_open("AAPL", [src3], _arrays), SA.DONE_MARKER)
    assert held["close"][1] == 2.5


def test_eviction_waits_for_a_held_claim_but_housekeeping_never_does(tmp_path):
    """A probe is renames only, so a publish waits a held claim out rather than escalating it
    to "Stop the workers"; sweep() and _remove_stale_siblings pass wait_s=0.0 and move on."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    d = _write_done_dir(store.key_dir("AAPL") / "sig")
    claim = d.with_name(d.name + SA.EVICTING_SUFFIX)
    claim.write_text(str(os.getpid()))

    assert store._evict_dir(d, wait_s=0.0) is False, "housekeeping must not block"
    assert d.is_dir() and (d / SA.DONE_MARKER).is_file(), "and must leave the directory alone"

    def hold():
        time.sleep(0.3)
        claim.unlink()
    t = threading.Thread(target=hold)
    t.start()
    try:
        assert store._evict_dir(d) is True, "a publish waits the momentary overlap out"
    finally:
        t.join()
    assert not d.exists()


def test_corrupt_array_in_a_marked_dir_is_rebuilt(tmp_path):
    """A marker is a claim, not proof: _publish must trust _try_open, not the marker's presence,
    or a truncated .npy makes the rebuild discard its own good build and fail forever."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()
    (store.current_dir("AAPL", [src]) / "close.npy").write_bytes(b"not a npy file at all")
    got = store.build_or_open("AAPL", [src], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])


def test_build_fn_exception_leaves_nothing_behind(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    def boom():
        raise RuntimeError("cache miss")
    with pytest.raises(RuntimeError):
        store.build_or_open("AAPL", [src], boom)
    key_dir = tmp_path / "_derived" / "X" / "AAPL"
    assert not key_dir.exists() or not any(p for p in key_dir.iterdir() if p.is_dir())
    assert not store.lock_path("AAPL", [src]).exists(), (
        "a failed build must release its lock, or every other worker waits out LOCK_STALE_S"
    )


def test_concurrent_builders_build_once(tmp_path):
    """Two threads (stand-ins for two worker processes) race on a cold key: one builds, the
    other waits on the lock and opens the result. build_fn runs exactly once."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    calls = []
    def slow_build():
        calls.append(1)
        time.sleep(0.5)
        return _arrays()
    out = {}
    def run(i):
        out[i] = store.build_or_open("AAPL", [src], slow_build)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert calls == [1]
    np.testing.assert_array_equal(out[0]["close"], out[1]["close"])


# --------------------------------------------------------------------------- multi-process
# The real shape of the problem -- N OS processes, no shared memory, no inherited locks --
# which the thread test above only approximates.
#
# Real subprocesses rather than multiprocessing("spawn"): a spawn child has to unpickle the
# target by importing THIS module by name, and under the full suite that fails with
# ModuleNotFoundError because an earlier test has churned sys.path (measured). The child below
# imports only ba2_common, so it cannot be broken by whatever ran before it.

_CHILD_SCRIPT = '''
import json, os, sys, time
from pathlib import Path
import numpy as np
from ba2_common.core import shared_arrays as SA

root, src, log_dir, out = sys.argv[1:5]

def build():
    Path(log_dir, "built.%d" % os.getpid()).write_text("1")
    time.sleep(0.5)
    return {"bar_ord": np.array([737000, 737001, 737002], dtype="int32")}

got = SA.DerivedArrayStore(root).build_or_open("AAPL", [src], build)
assert type(got["bar_ord"]) is np.ndarray
Path(out).write_text(json.dumps(np.asarray(got["bar_ord"]).tolist()))
'''


def test_four_processes_on_a_cold_key_build_once(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    log_dir = tmp_path / "buildlog"
    log_dir.mkdir()
    script = tmp_path / "child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))

    outs = [tmp_path / f"out{i}.json" for i in range(4)]
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(store.root), str(src), str(log_dir), str(o)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for o in outs
    ]
    try:
        for p in procs:
            _, err = p.communicate(timeout=120)
            assert p.returncode == 0, f"child failed ({p.returncode}):\n{err}"
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    assert len(list(log_dir.iterdir())) == 1, "exactly one process may run build_fn"
    sig_dirs = [d for d in store.key_dir("AAPL").iterdir() if d.is_dir()]
    assert len(sig_dirs) == 1, f"one signature directory, got {[d.name for d in sig_dirs]}"
    for o in outs:
        assert json.loads(o.read_text()) == [737000, 737001, 737002]


def test_stale_lock_is_broken(tmp_path, monkeypatch):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("dead pid")
    _age(lock)
    monkeypatch.setattr(SA, "LOCK_STALE_S", 60.0)
    got = store.build_or_open("AAPL", [src], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])
    assert not lock.exists(), "the broken lock must be released, not left for the next waiter"


def test_release_leaves_a_lock_owned_by_another_process_alone(tmp_path):
    """A build that overran LOCK_STALE_S has had its lock broken and re-taken; unlinking that
    one would hand a third process a lock the second still believes it holds."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999")                     # a pid that is not ours
    store._release(lock)
    assert lock.exists(), "releasing somebody else's lock is how two builders end up at once"
    lock.write_text(str(os.getpid()))
    store._release(lock)
    assert not lock.exists()


def test_waiting_on_a_live_lock_times_out(tmp_path, monkeypatch):
    """A lock that never goes stale and never publishes must fail loudly, not hang a worker."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999")
    monkeypatch.setattr(SA, "LOCK_STALE_S", 0.5)
    monkeypatch.setattr(SA, "_WAIT_POLL_S", 0.05)
    monkeypatch.setattr(SA.DerivedArrayStore, "_lock_is_stale", staticmethod(lambda p: False))
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        store.build_or_open("AAPL", [src], _arrays)
    assert time.monotonic() - t0 < 10.0


def test_escape_hatch_returns_private_arrays_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    got = store.build_or_open("AAPL", [_src(tmp_path)], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])
    assert not (tmp_path / "_derived").exists()
    got["close"][0] = 5.0   # private, writable


def test_object_arrays_are_refused(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    with pytest.raises(TypeError):
        store.build_or_open("AAPL", [_src(tmp_path)], lambda: {"s": np.array(["a", None], dtype=object)})


def test_non_contiguous_arrays_are_refused(tmp_path):
    """np.ascontiguousarray here would silently double peak RSS on the 15 GB set."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    with pytest.raises(ValueError, match="C-contiguous"):
        store.build_or_open("AAPL", [_src(tmp_path)], lambda: {"v": np.arange(10)[::2]})


def test_derived_root_for_source_tree():
    assert SA.derived_root_for(r"C:\cache\ThetaDataOptionsProvider") == \
        os.path.join(r"C:\cache", "_derived", "ThetaDataOptionsProvider")
    assert SA.derived_root_for("/home/x/cache/FMPOHLCVProvider/") == \
        os.path.join("/home/x/cache", "_derived", "FMPOHLCVProvider")


def test_derived_root_for_refuses_a_bare_root():
    for bad in ("C:\\", "C:", "/", "", "\\"):
        with pytest.raises(ValueError):
            SA.derived_root_for(bad)


def test_sweep_collects_old_signatures_orphans_and_dead_tmps_only(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    kd = store.key_dir("AAPL")
    old_done = _write_done_dir(kd / "sig_old", marker_mtime=time.time() - 10_000)
    # Superseded but PUBLISHED SECONDS AGO: with a churning source the newest-by-mtime is often
    # not the one a builder just published, and collecting this one raced that builder between
    # its _publish and its _try_open.
    fresh_superseded = _write_done_dir(kd / "sig_fresh", marker_mtime=time.time() - 5)
    new_done = _write_done_dir(kd / "sig_new", marker_mtime=time.time())
    orphan = kd / "sig_partial"                 # what a pre-_evict_dir partial delete leaves
    orphan.mkdir()
    (orphan / "close.npy").write_bytes(b"junk")
    live_tmp = kd / "sig_new.111.222.tmp"       # a builder is writing into this one
    live_tmp.mkdir()
    dead_tmp = kd / "sig_old.333.444.tmp"
    dead_tmp.mkdir()
    _age(dead_tmp)

    removed = store.sweep()

    left = sorted(p.name for p in kd.iterdir() if p.is_dir())
    assert left == sorted([new_done.name, fresh_superseded.name, live_tmp.name])
    assert removed == 3                          # old_done, orphan, dead_tmp
    assert not old_done.exists() and not orphan.exists() and not dead_tmp.exists()
    assert fresh_superseded.is_dir(), "a directory published seconds ago is not garbage yet"
    assert store.sweep() == 0, "sweep must be idempotent"


def test_a_long_build_heartbeats_its_own_lock(tmp_path, monkeypatch):
    """A build that outlives LOCK_STALE_S must not have its lock broken UNDER it.

    The lock's mtime is stamped once, at _acquire, and staleness is measured from that stamp,
    so without a heartbeat a slow build hands a second process permission to start the same
    multi-GB build beside it — precisely on the largest underlyings, where a duplicate build
    is what this module exists to prevent.

    Made deterministic by slowing the WRITE of each array (0.2 s) past a shortened
    LOCK_STALE_S (0.3 s) and asking, at each array boundary, whether the lock has gone stale.
    Without the heartbeat the observed ages are 0.2/0.4/0.6/0.8 s and every answer after the
    first is "stale"; with it each age is one array long. The observation is taken INSIDE the
    write, so it sees the state the build itself would be judged on.
    """
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    monkeypatch.setattr(SA, "LOCK_STALE_S", 0.3)

    real_save = np.save
    stale_at_each_array = []

    def slow_save(f, a, **kw):
        time.sleep(0.2)
        out = real_save(f, a, **kw)
        stale_at_each_array.append(SA.DerivedArrayStore._lock_is_stale(lock))
        return out

    monkeypatch.setattr(np, "save", slow_save)
    got = store.build_or_open("AAPL", [src], _arrays)

    assert len(stale_at_each_array) == len(_arrays()), stale_at_each_array
    assert not any(stale_at_each_array), (
        f"the build's own lock went stale mid-build: {stale_at_each_array}")
    np.testing.assert_array_equal(got["close"], _arrays()["close"])


def test_a_vanished_lock_does_not_kill_the_build(tmp_path, monkeypatch):
    """The heartbeat is best-effort. A lock broken by a waiter that gave up is not something a
    builder in flight can repair, and throwing the finished arrays away over it would be the
    worse answer: the publish still either wins the rename or opens the winner's set."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    real_save = np.save
    killed = []

    def save_then_break_the_lock(f, a, **kw):
        out = real_save(f, a, **kw)
        if not killed:
            lock.unlink()
            killed.append(1)
        return out

    monkeypatch.setattr(np, "save", save_then_break_the_lock)
    got = store.build_or_open("AAPL", [src], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])


def test_evict_key_removes_a_whole_key_directory_all_or_nothing(tmp_path):
    """The public seam for the only collector an OBSOLETE KEY has.

    ``sweep()`` deliberately never removes a key -- it cannot tell "nothing asks for this key any
    more" from "nothing has asked YET on this host". ``tools/build_shared_arrays.py --sweep``
    can (it holds the consumers' ARRAYS_VERSION and an age policy), so it needs a way in that
    still gets ``_evict_dir``'s all-or-nothing probe rather than an rmtree.
    """
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    kd = store.key_dir("u_AAPL.v0")
    _write_done_dir(kd / "sig_a")
    _write_done_dir(kd / "sig_b")
    (kd / "sig_c.lock").write_text("123")

    assert store.evict_key(kd) is True
    assert not kd.exists()
    # Already gone is NOT "removed by this call" -- the tool sums the return value into a count
    # and bytes it reports as reclaimed.
    assert store.evict_key(kd) is False
    assert not (tmp_path / "_derived" / "X" / "u_AAPL.v0.evicting").exists()


def test_evict_key_leaves_a_key_whose_arrays_are_still_mapped(tmp_path):
    """The Windows contract, stated at the KEY level: a directory holding a file another process
    maps is left byte-for-byte intact and reported False (on POSIX every rename succeeds, so the
    claim there is only that the key is gone and the caller was told so)."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    held = store.build_or_open("AAPL", [src], _arrays)
    kd = store.key_dir("AAPL")

    removed = store.evict_key(kd)

    if removed:                                  # POSIX: unlinking a mapped file is legal
        assert not kd.exists()
    else:                                        # NTFS: refused, and nothing was half-deleted
        assert (store.current_dir("AAPL", [src]) / SA.DONE_MARKER).is_file()
        np.testing.assert_array_equal(held["close"], _arrays()["close"])
    del held
    gc.collect()


def test_opening_a_set_refreshes_its_marker_so_mtime_means_last_use(tmp_path):
    """An age-based collector is only safe if the timestamp it reads moves when the set is USED.

    Otherwise the marker keeps the timestamp of the day the set was BUILT, and
    ``tools/build_shared_arrays.py --sweep --sweep-max-age-days 14`` deletes the very key every
    trial on the host has been mapping daily -- charging the next grid a cold rebuild for the
    hottest entry on the box.
    """
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    marker = store.current_dir("AAPL", [src]) / SA.DONE_MARKER
    _age(marker, seconds=30 * 86400)
    assert time.time() - marker.stat().st_mtime > 29 * 86400

    got = store.build_or_open("AAPL", [src], _arrays)      # an OPEN, not a build

    np.testing.assert_array_equal(got["close"], _arrays()["close"])
    assert time.time() - marker.stat().st_mtime < 60, "a successful open must restamp the marker"
    del got
    gc.collect()


def test_a_failed_open_does_not_restamp_the_marker(tmp_path):
    """Only a set that actually OPENED counts as used: a marked-but-unreadable directory is
    about to be rebuilt, and refreshing it would hide it from the collector forever."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    kd = store.key_dir("AAPL")
    d = _write_done_dir(kd / "sig_broken")
    (d / "close.npy").write_bytes(b"not a npy at all")
    _age(d / SA.DONE_MARKER, seconds=30 * 86400)
    before = (d / SA.DONE_MARKER).stat().st_mtime

    assert store._try_open(d) is None
    assert (d / SA.DONE_MARKER).stat().st_mtime == before


# --------------------------------------------------------------------------------------------
# File descriptors. Every mapped array holds ONE fd for the life of the mapping, so an option
# universe (98 underlyings x 18 arrays = 1764) blows straight through a systemd soft
# RLIMIT_NOFILE of 1024. Measured on remote227 2026-09-14: a worker sat at 1014 open fds, the
# next np.load raised EMFILE, _try_open read that as "not usable" and REBUILT a 7 GB set under
# the lock while 27 of 30 workers slept in the wait loop -- a silent whole-grid stall.
# --------------------------------------------------------------------------------------------
class _FakeResource:
    """Stand-in for the POSIX ``resource`` module, so the limit logic is testable on Windows."""

    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, soft, hard, fail=None):
        self.limits = (soft, hard)
        self.calls = []
        self.fail = fail

    def getrlimit(self, which):
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which, pair):
        assert which == self.RLIMIT_NOFILE
        self.calls.append(pair)
        if self.fail is not None:
            raise self.fail
        self.limits = (pair[0], self.limits[1])


def test_ensure_fd_headroom_raises_the_soft_limit_towards_the_hard_one(monkeypatch):
    fake = _FakeResource(1024, 4096)
    monkeypatch.setitem(sys.modules, "resource", fake)

    old, new = SA.ensure_fd_headroom()

    assert (old, new) == (1024, 4096)
    assert fake.calls == [(4096, 4096)], "the hard limit is the ceiling we may take for free"


def test_ensure_fd_headroom_caps_at_the_target_when_the_hard_limit_is_huge(monkeypatch):
    fake = _FakeResource(1024, 524288)            # the systemd default hard limit on remote227
    monkeypatch.setitem(sys.modules, "resource", fake)

    old, new = SA.ensure_fd_headroom()

    assert (old, new) == (1024, 65536)
    assert fake.calls == [(65536, 524288)]


def test_ensure_fd_headroom_honours_an_explicit_need(monkeypatch):
    fake = _FakeResource(1024, 524288)
    monkeypatch.setitem(sys.modules, "resource", fake)

    assert SA.ensure_fd_headroom(200_000) == (1024, 200_000)


def test_ensure_fd_headroom_is_a_no_op_when_the_soft_limit_is_already_the_hard_one(monkeypatch):
    fake = _FakeResource(524288, 524288)
    monkeypatch.setitem(sys.modules, "resource", fake)

    assert SA.ensure_fd_headroom() == (524288, 524288)
    assert fake.calls == []


def test_ensure_fd_headroom_never_raises_when_the_kernel_refuses(monkeypatch):
    """Best effort by contract: a container that forbids the raise must not kill the worker."""
    fake = _FakeResource(1024, 4096, fail=OSError(1, "Operation not permitted"))
    monkeypatch.setitem(sys.modules, "resource", fake)

    assert SA.ensure_fd_headroom() == (1024, 1024)

    fake2 = _FakeResource(1024, 4096, fail=ValueError("bad limit"))
    monkeypatch.setitem(sys.modules, "resource", fake2)
    assert SA.ensure_fd_headroom() == (1024, 1024)


def test_ensure_fd_headroom_on_this_host(monkeypatch):
    """Called for real: on Windows there is no RLIMIT_NOFILE and it must report (0, 0)."""
    old, new = SA.ensure_fd_headroom()
    again = SA.ensure_fd_headroom()

    if os.name == "nt":
        assert (old, new) == (0, 0) and again == (0, 0)
    else:
        import resource as _r
        soft, hard = _r.getrlimit(_r.RLIMIT_NOFILE)
        assert new >= old and soft == new
        assert again == (new, new), "idempotent: a second call changes nothing"


def test_constructing_a_store_raises_the_limit_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(SA, "ensure_fd_headroom", lambda *a, **k: calls.append(1) or (0, 0))
    monkeypatch.setattr(SA, "_FD_HEADROOM_DONE", False)

    SA.DerivedArrayStore("x")
    SA.DerivedArrayStore("y")

    assert calls == [1], "every consumer builds a store; the raise is lazy and once"


def test_descriptor_exhaustion_is_loud_and_never_triggers_a_rebuild(tmp_path, monkeypatch):
    """EMFILE is NOT a missing set. Rebuilding cannot make descriptors appear -- it takes the
    lock, spends a multi-GB build, and stalls every other worker behind it."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    calls = []

    def build():
        calls.append(1)
        return _arrays()

    got = store.build_or_open("AAPL", [src], build)
    del got
    gc.collect()
    assert calls == [1]

    def boom(*a, **k):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(np, "load", boom)

    with pytest.raises(RuntimeError) as ei:
        store.build_or_open("AAPL", [src], build)

    assert "file descriptors" in str(ei.value)
    assert "RLIMIT_NOFILE" in str(ei.value)
    assert calls == [1], "exhaustion must never be read as 'absent' and rebuilt"
    assert not (store.key_dir("AAPL") / (store.signature([src]) + ".lock")).exists(), \
        "no lock may be taken: 27 of 30 workers sleeping behind one is the field failure"


def test_out_of_memory_mapping_is_also_loud(tmp_path, monkeypatch):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()

    def boom(*a, **k):
        raise OSError(errno.ENOMEM, "Cannot allocate memory")

    monkeypatch.setattr(np, "load", boom)
    with pytest.raises(RuntimeError, match="file descriptors"):
        store.build_or_open("AAPL", [src], _arrays)


def test_a_plain_oserror_still_reports_not_usable(tmp_path, monkeypatch):
    """Everything that is not resource exhaustion keeps the old contract: None -> rebuild."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    gc.collect()
    d = store.current_dir("AAPL", [src])

    for exc in (OSError(errno.ENOENT, "No such file or directory"),
                OSError("something else entirely"),
                ValueError("not a .npy")):
        monkeypatch.setattr(np, "load", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        assert store._try_open(d) is None
    monkeypatch.undo()


def test_a_missing_array_file_is_rebuilt(tmp_path):
    """The end-to-end half of the above: a genuinely incomplete set is still rebuilt."""
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    calls = []

    def build():
        calls.append(1)
        return _arrays()

    got = store.build_or_open("AAPL", [src], build)
    del got
    gc.collect()
    (store.current_dir("AAPL", [src]) / "close.npy").unlink()

    again = store.build_or_open("AAPL", [src], build)

    assert calls == [1, 1]
    np.testing.assert_array_equal(again["close"], _arrays()["close"])
    del again
    gc.collect()
