"""ba2_common.core.shared_arrays -- one derived-cache mechanism for every read-only array set
a GA worker used to hold privately. See docs/plans/2026-09-14-shared-arrays-across-workers.md."""
import gc
import json
import os
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
        assert results.count(True) == 1, f"attempt {attempt}: exactly one winner, got {results}"
        assert not d.exists(), f"attempt {attempt}: the winner must have removed it"
        claim = d.with_name(d.name + SA.EVICTING_SUFFIX)
        assert not claim.exists(), "the eviction claim must be released"


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
    assert left == sorted([new_done.name, live_tmp.name])
    assert removed == 3                          # old_done, orphan, dead_tmp
    assert not old_done.exists() and not orphan.exists() and not dead_tmp.exists()
    assert store.sweep() == 0, "sweep must be idempotent"
