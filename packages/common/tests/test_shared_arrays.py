"""ba2_common.core.shared_arrays -- one derived-cache mechanism for every read-only array set
a GA worker used to hold privately. See docs/plans/2026-09-14-shared-arrays-across-workers.md."""
import json
import os
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
    assert got["close"].base is not None            # a view over the mapping
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


def test_done_marker_is_required_and_written_last(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
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


def test_build_fn_exception_leaves_nothing_behind(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    def boom():
        raise RuntimeError("cache miss")
    with pytest.raises(RuntimeError):
        store.build_or_open("AAPL", [src], boom)
    key_dir = tmp_path / "_derived" / "X" / "AAPL"
    assert not key_dir.exists() or not any(p for p in key_dir.iterdir() if p.is_dir())


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


def test_stale_lock_is_broken(tmp_path, monkeypatch):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    lock = store.lock_path("AAPL", [src])
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("dead pid")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    monkeypatch.setattr(SA, "LOCK_STALE_S", 60.0)
    got = store.build_or_open("AAPL", [src], _arrays)
    np.testing.assert_array_equal(got["close"], _arrays()["close"])


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


def test_derived_root_for_source_tree():
    assert SA.derived_root_for(r"C:\cache\ThetaDataOptionsProvider") == \
        os.path.join(r"C:\cache", "_derived", "ThetaDataOptionsProvider")
    assert SA.derived_root_for("/home/x/cache/FMPOHLCVProvider/") == \
        os.path.join("/home/x/cache", "_derived", "FMPOHLCVProvider")


def test_tmp_dirs_use_the_skip_suffix_and_lock_files_the_lock_suffix(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    assert store.lock_path("AAPL", [src]).name.endswith(".lock")
    assert SA._tmp_dir_name("abc").endswith(".tmp")


def test_sweep_removes_older_signatures_and_tmp_leftovers(tmp_path):
    store = SA.DerivedArrayStore(tmp_path / "_derived" / "X")
    src = _src(tmp_path)
    store.build_or_open("AAPL", [src], _arrays)
    src.write_bytes(b"q" * 300)
    store.build_or_open("AAPL", [src], _arrays)       # newer signature
    (store.key_dir("AAPL") / "zzz.1.2.tmp").mkdir()
    keep = store.current_dir("AAPL", [src])
    # _remove_stale_siblings may already have removed the old one; sweep must be idempotent.
    store.sweep()
    left = sorted(p.name for p in store.key_dir("AAPL").iterdir() if p.is_dir())
    assert left == [keep.name]
