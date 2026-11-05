"""Cache-sync gate — manifest, diff, tar push round-trip, traversal guard.

Proves the "avoid redownload" core for the PUSH model: the master diffs its cache against a
worker's manifest, streams ONLY the missing files as one tar, and the worker extracts it
(traversal-guarded). A re-push after sync is a no-op.
"""
import io
from pathlib import Path

import pytest

from app.services import cache_sync


def _write(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_build_manifest_lists_and_excludes(tmp_path):
    _write(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"x" * 100)
    _write(tmp_path / "screener" / "metric_store" / "ym=2024-01" / "part.parquet", b"y" * 50)
    _write(tmp_path / "options" / "options_history.sqlite", b"z" * 10)
    _write(tmp_path / "junk.tmp", b"nope")                 # excluded (temp)
    _write(tmp_path / "options" / "options_history.sqlite-wal", b"w")  # excluded (sidecar)
    _write(tmp_path / "screener" / ".hidden", b"no")       # excluded (hidden)

    man = cache_sync.build_manifest(str(tmp_path))
    rels = {f["rel_path"] for f in man["files"]}
    assert rels == {
        "FMPOHLCVProvider/AAPL_1d.parquet",
        "screener/metric_store/ym=2024-01/part.parquet",
        "options/options_history.sqlite",
    }
    assert man["count"] == 3 and man["total_bytes"] == 160


def test_safe_resolve_blocks_traversal(tmp_path):
    assert cache_sync.safe_resolve("a/b.parquet", str(tmp_path)) == (tmp_path / "a/b.parquet").resolve()
    for bad in ("../escape", "../../etc/passwd", "/etc/passwd"):
        with pytest.raises(ValueError):
            cache_sync.safe_resolve(bad, str(tmp_path))


def test_diff_missing():
    local = [{"rel_path": "a", "size": 10}, {"rel_path": "b", "size": 20}, {"rel_path": "c", "size": 5}]
    remote = {"files": [{"rel_path": "a", "size": 10}, {"rel_path": "b", "size": 99}]}  # b size differs
    # a matches -> skip; b size mismatch -> resend; c absent -> send
    assert sorted(cache_sync.diff_missing(local, remote)) == ["b", "c"]


def test_diff_stale():
    # Master rebuilt/compacted the screener store: old per-generation fragments are gone locally,
    # but a worker synced before the rebuild still has them (same rel_path, any size).
    local = [{"rel_path": "screener/metric_store/ym=2024-01/part.parquet", "size": 100}]
    remote = {"files": [
        {"rel_path": "screener/metric_store/ym=2024-01/part.parquet", "size": 100},
        {"rel_path": "screener/metric_store/ym=2024-01/part-00001.parquet", "size": 40},
        {"rel_path": "screener/metric_store/ym=2024-01/part-00002.parquet", "size": 41},
    ]}
    assert sorted(cache_sync.diff_stale(local, remote)) == [
        "screener/metric_store/ym=2024-01/part-00001.parquet",
        "screener/metric_store/ym=2024-01/part-00002.parquet",
    ]


def test_build_manifest_with_hash(tmp_path):
    _write(tmp_path / "a.parquet", b"same content")
    man = cache_sync.build_manifest(str(tmp_path), with_hash=True)
    assert man["files"][0]["crc32"] == cache_sync._crc32_file(tmp_path / "a.parquet")
    # without with_hash, no crc32 key at all (fast path unaffected)
    man2 = cache_sync.build_manifest(str(tmp_path))
    assert "crc32" not in man2["files"][0]


def test_diff_content_mismatch_catches_same_size_different_content():
    # A rebuild rewrote the file at its OLD byte size — (rel_path, size) alone can't see this.
    local = [{"rel_path": "screener/metric_store/ym=2024-01/part.parquet", "size": 100, "crc32": 111},
             {"rel_path": "unchanged.parquet", "size": 50, "crc32": 222}]
    remote = {"files": [
        {"rel_path": "screener/metric_store/ym=2024-01/part.parquet", "size": 100, "crc32": 999},
        {"rel_path": "unchanged.parquet", "size": 50, "crc32": 222},
    ]}
    assert cache_sync.diff_content_mismatch(local, remote) == [
        "screener/metric_store/ym=2024-01/part.parquet"
    ]


def test_prune_paths_deletes_and_guards_traversal(tmp_path):
    _write(tmp_path / "screener" / "metric_store" / "ym=2024-01" / "part-00001.parquet", b"x")
    _write(tmp_path / "screener" / "metric_store" / "ym=2024-01" / "part.parquet", b"y")

    res = cache_sync.prune_paths(
        [
            "screener/metric_store/ym=2024-01/part-00001.parquet",  # deleted
            "screener/metric_store/ym=2024-01/does-not-exist.parquet",  # missing -> no-op
            "../escape.parquet",  # traversal -> skipped
        ],
        str(tmp_path),
    )
    assert res == {"pruned": 1, "skipped": 1, "failed": 0, "protected": 0}
    assert not (tmp_path / "screener" / "metric_store" / "ym=2024-01" / "part-00001.parquet").exists()
    assert (tmp_path / "screener" / "metric_store" / "ym=2024-01" / "part.parquet").exists()


def test_tar_roundtrip_and_traversal(tmp_path):
    src = tmp_path / "master"
    dst = tmp_path / "worker"
    files = {
        "FMPOHLCVProvider/AAPL_1d.parquet": b"a" * 5000,
        "fmp_history/MSFT.json": b"b" * 2000,
        "options/options_history.sqlite": b"c" * 99999,
    }
    for rel, data in files.items():
        _write(src / rel, data)

    man = cache_sync.build_manifest(str(src))
    missing = cache_sync.diff_missing(man["files"], {"files": []})
    assert sorted(missing) == sorted(files)

    buf = io.BytesIO(b"".join(cache_sync.iter_tar(missing, str(src))))
    res = cache_sync.extract_tar(buf, str(dst))
    assert res["extracted"] == 3 and res["skipped"] == 0
    for rel, data in files.items():
        assert (dst / rel).read_bytes() == data
    # After extract the worker is in sync -> diff is empty.
    assert cache_sync.diff_missing(man["files"], cache_sync.build_manifest(str(dst))) == []


def test_extract_tar_logs_progress_periodically(tmp_path, monkeypatch):
    """A push can run tens of GB / a long time on a slow disk (module docstring) -- extraction
    must not go silent for the whole duration. Time-based, not per-file: a fake monotonic clock
    proves the cadence without a real sleep."""
    src = tmp_path / "master"
    for i in range(3):
        _write(src / f"f{i}.parquet", b"x" * 100)

    # Every call to time.monotonic() (one before the loop, one per extracted file) advances by a
    # full interval, so each of the 3 files crosses the log threshold -- deterministic, no
    # reliance on real wall-clock timing or on the log callback itself driving the clock.
    calls = [0]

    def fake_monotonic():
        calls[0] += 1
        return 1000.0 + calls[0] * cache_sync._PROGRESS_LOG_INTERVAL_S

    monkeypatch.setattr(cache_sync.time, "monotonic", fake_monotonic)
    logged = []

    man = cache_sync.build_manifest(str(src))
    buf = io.BytesIO(b"".join(cache_sync.iter_tar([f["rel_path"] for f in man["files"]], str(src))))
    res = cache_sync.extract_tar(buf, str(tmp_path / "worker"), log=logged.append)

    assert res["extracted"] == 3
    assert len(logged) == 3, logged
    assert all("cache extract:" in m and "file(s)" in m for m in logged), logged


def test_extract_tar_default_log_is_module_logger(tmp_path):
    """No explicit ``log`` -> falls back to this module's own logger (the worker's
    _install_orchestration_file_logging root handler picks it up automatically)."""
    import inspect
    default = inspect.signature(cache_sync.extract_tar).parameters["log"].default
    assert default == cache_sync.logger.info  # bound methods compare equal by (__self__, __func__)


def test_extract_tar_rejects_traversal(tmp_path):
    import tarfile
    dst = tmp_path / "worker"
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode="w") as t:
        payload = b"x" * 10
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    tb.seek(0)
    res = cache_sync.extract_tar(tb, str(dst))
    assert res["skipped"] == 1 and res["extracted"] == 0
    assert not (tmp_path / "evil.txt").exists()  # did NOT escape the cache root


def test_derived_array_cache_is_never_part_of_the_manifest(tmp_path):
    """The per-host derived .npy cache (ba2_common.core.shared_arrays) is deterministic from
    parquet the worker already has and ~3x its size; it must never ride the pre-flight push.

    Excluded on BOTH ends by construction (master push and the worker's own manifest come from
    this one walker), so ``diff_stale``/``prune_paths`` can't mistake a worker's locally-built
    derived cache for a stale leftover and delete it either."""
    _write(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"p")
    d = tmp_path / "_derived" / "FMPOHLCVProvider" / "AAPL_1d" / "abc123"
    _write(d / "close.npy", b"n")
    _write(d / "_done.json", b"{}")
    # A provider tree that is not directly under CACHE_FOLDER derives into a NESTED _derived.
    _write(tmp_path / "options" / "_derived" / "thetadata" / "x" / "close.npy", b"n")

    man = cache_sync.build_manifest(str(tmp_path))
    rels = {f["rel_path"] for f in man["files"]}
    assert rels == {"FMPOHLCVProvider/AAPL_1d.parquet"}


def test_derived_exclusion_is_an_exact_path_component_match(tmp_path):
    """Only a path COMPONENT named exactly ``_derived`` is skipped -- a regular file called
    ``_derived`` or a directory whose name merely starts with it is ordinary cache content."""
    _write(tmp_path / "_derived_something" / "a.parquet", b"x")
    _write(tmp_path / "_derived", b"y")                      # a FILE named _derived
    _write(tmp_path / "FMPOHLCVProvider" / "not_derived" / "b.parquet", b"z")

    man = cache_sync.build_manifest(str(tmp_path))
    rels = {f["rel_path"] for f in man["files"]}
    assert rels == {
        "_derived_something/a.parquet",
        "_derived",
        "FMPOHLCVProvider/not_derived/b.parquet",
    }


def test_manifest_ignores_a_derived_component_in_the_cache_roots_own_prefix(tmp_path):
    """The skip is a component match on paths RELATIVE to the cache root. Were it applied to the
    absolute path, a cache root that merely LIVES under a directory named ``_derived`` would yield
    an empty manifest -- and an empty master manifest makes ``diff_stale`` return every worker
    file, so the next ``/cache/prune`` would wipe that worker's whole cache."""
    root = tmp_path / "_derived" / "cache"
    _write(root / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"x")

    man = cache_sync.build_manifest(str(root))
    assert man["count"] == 1
    assert man["files"][0]["rel_path"] == "FMPOHLCVProvider/AAPL_1d.parquet"


def test_skip_dirname_is_the_shared_arrays_constant():
    """One source of truth for the directory name -- a rename in shared_arrays must not leave
    cache_sync silently pushing the derived tree again."""
    from ba2_common.core.shared_arrays import DERIVED_DIRNAME
    assert cache_sync._SKIP_DIRNAMES == (DERIVED_DIRNAME,)


def test_build_manifest_does_not_descend_into_a_derived_tree(tmp_path, monkeypatch):
    """Not merely filtered afterwards: the walk must PRUNE the directory. A derived tree holds
    several .npy per signature dir across the whole provider tree, and the cold manifest walk is
    already the slow path (~140s over 312k files)."""
    _write(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"p")
    _write(tmp_path / "_derived" / "FMPOHLCVProvider" / "AAPL_1d" / "sig" / "close.npy", b"n")

    walked = []
    kwargs = {}
    real_walk = cache_sync.os.walk

    def spy_walk(top, *a, **k):
        kwargs.update(k)
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            walked.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(cache_sync.os, "walk", spy_walk)
    cache_sync.build_manifest(str(tmp_path))

    assert not any("_derived" in Path(d).parts for d in walked), walked
    # Strict parity with the Path.rglob this replaced (3.12: Path.walk(follow_symlinks=False)):
    # widening it to followlinks=True would let a symlink cycle loop the manifest walk.
    assert kwargs.get("followlinks", False) is False, kwargs


def test_prune_paths_survives_an_undeletable_file(tmp_path, monkeypatch):
    """A locked/memory-mapped file (Windows: WinError 32 -> PermissionError) must not abort the
    sweep -- everything AFTER it would stay un-pruned, which is the silent staleness diff_stale
    exists to prevent. It is counted in ``failed`` and logged, not raised."""
    for name in ("locked.parquet", "after.parquet"):
        _write(tmp_path / name, b"x")

    real_unlink = Path.unlink

    def fake_unlink(self, *a, **k):
        if self.name == "locked.parquet":
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    warned = []
    monkeypatch.setattr(cache_sync.logger, "warning", lambda m, *a, **k: warned.append(m))

    res = cache_sync.prune_paths(["locked.parquet", "after.parquet"], str(tmp_path))

    assert res == {"pruned": 1, "skipped": 0, "failed": 1, "protected": 0}
    assert not (tmp_path / "after.parquet").exists()   # the loop continued past the failure
    assert (tmp_path / "locked.parquet").exists()
    assert len(warned) == 1 and "locked.parquet" in warned[0], warned


# --------------------------------------------------------------------------------------------
# History-source scoped pushes
# --------------------------------------------------------------------------------------------
def _hist_cache(tmp_path, fmp=True, alpaca=True):
    if fmp:
        _write(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"f" * 10)
    if alpaca:
        _write(tmp_path / "AlpacaHistoryOHLCVProvider" / "AAPL_1d.parquet", b"a" * 10)
    _write(tmp_path / "options" / "o.sqlite", b"o")
    _write(tmp_path / "_derived" / "x" / "a.npy", b"d")
    return tmp_path


def test_exclude_dirnames_is_top_level_only(tmp_path):
    _write(tmp_path / "FMPOHLCVProvider" / "a.parquet", b"1")
    _write(tmp_path / "nested" / "FMPOHLCVProvider" / "b.parquet", b"2")
    man = cache_sync.build_manifest(str(tmp_path), exclude_dirnames=("FMPOHLCVProvider",))
    assert {f["rel_path"] for f in man["files"]} == {"nested/FMPOHLCVProvider/b.parquet"}


def test_history_manifest_alpaca_excludes_fmp_and_derived(tmp_path, monkeypatch):
    from app.services import worker_client
    _hist_cache(tmp_path)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(tmp_path))
    local = worker_client._history_manifest("alpaca")
    rels = {f["rel_path"] for f in local["files"]}
    assert rels == {"AlpacaHistoryOHLCVProvider/AAPL_1d.parquet", "options/o.sqlite"}


def test_history_manifest_fmp_keeps_fmp_excludes_alpaca(tmp_path, monkeypatch):
    from app.services import worker_client
    _hist_cache(tmp_path)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(tmp_path))
    rels = {f["rel_path"] for f in worker_client._history_manifest("fmp")["files"]}
    assert rels == {"FMPOHLCVProvider/AAPL_1d.parquet", "options/o.sqlite"}


def test_alpaca_push_without_local_alpaca_folder_refuses(tmp_path, monkeypatch):
    from app.services import worker_client
    _hist_cache(tmp_path, alpaca=False)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(tmp_path))
    with pytest.raises(ValueError, match="AlpacaHistoryOHLCVProvider"):
        worker_client.push_cache({"name": "w", "url": "http://w"}, history_source="alpaca")


def test_push_without_a_history_source_refuses():
    from app.services import worker_client
    with pytest.raises(TypeError):
        worker_client.push_cache({"name": "w", "url": "http://w"})
    with pytest.raises(Exception, match="unknown OHLCV history source"):
        worker_client.push_cache({"name": "w", "url": "http://w"}, history_source=None)


def test_prune_guard_refuses_paths_outside_the_cache_root(tmp_path):
    with pytest.raises(ValueError):
        cache_sync.assert_prunable(["../outside.txt"], str(tmp_path))
    with pytest.raises(ValueError):
        cache_sync.assert_prunable([str(tmp_path.parent / "abs.txt")], str(tmp_path))
    cache_sync.assert_prunable(["FMPOHLCVProvider/a.parquet"], str(tmp_path))


def test_cli_sync_cache_requires_history_source(monkeypatch, capsys):
    import ba2test_launcher
    # the launcher under test is THIS checkout's (the venv's editable install points at the main clone)
    import os
    here = os.path.normcase(os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..")))
    assert os.path.normcase(os.path.realpath(ba2test_launcher.__file__)).startswith(here)
    with pytest.raises(SystemExit) as e:
        ba2test_launcher.main(["sync-cache", "--worker", "w1"])
    assert e.value.code == 2
    assert "--ohlcv-history-source" in capsys.readouterr().err


def _push_with_remote(monkeypatch, tmp_path, source, remote_rels, with_crc=True):
    from app.services import worker_client
    _hist_cache(tmp_path)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(tmp_path))
    calls = []

    class _R:
        def __init__(self, p): self.p = p
        def raise_for_status(self): pass
        def json(self): return self.p

    class _C:
        def __init__(self, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **_k):
            # a worker that answers hash_dirs: the same content as the master's _hist_cache
            data = {"AlpacaHistoryOHLCVProvider": b"a" * 10, "FMPOHLCVProvider": b"f" * 10}
            files = []
            for r in remote_rels:
                e = {"rel_path": r, "size": 10}
                top = r.split("/")[0]
                if with_crc and top in data:
                    e["crc32"] = _crc_of(data[top])
                files.append(e)
            return _R({"files": files})
        def post(self, url, **k):
            calls.append((url, k.get("json")))
            return _R({"pruned": 1})

    monkeypatch.setattr(worker_client.httpx, "Client", _C)
    worker_client.push_cache({"name": "w", "url": "http://w"}, history_source=source,
                             log=lambda _m: None)
    return [c for c in calls if c[0].endswith("/cache/prune")]


@pytest.mark.parametrize("source,other", [("alpaca", "FMPOHLCVProvider"),
                                          ("fmp", "AlpacaHistoryOHLCVProvider")])
def test_push_leaves_other_history_untouched_but_prunes_elsewhere(tmp_path, monkeypatch, source, other):
    own = "AlpacaHistoryOHLCVProvider" if source == "alpaca" else "FMPOHLCVProvider"
    remote = [f"{own}/AAPL_1d.parquet", "options/o.sqlite", f"{other}/AAPL_1d.parquet",
              f"{other}/OLD_1d.parquet", "options/old.sqlite"]
    prunes = _push_with_remote(monkeypatch, tmp_path, source, remote)
    assert [p[1] for p in prunes] == [{"rel_paths": ["options/old.sqlite"]}]


def test_push_with_only_other_history_stale_sends_no_prune(tmp_path, monkeypatch):
    remote = ["AlpacaHistoryOHLCVProvider/AAPL_1d.parquet", "options/o.sqlite",
              "FMPOHLCVProvider/AAPL_1d.parquet"]
    assert _push_with_remote(monkeypatch, tmp_path, "alpaca", remote) == []


# --------------------------------------------------------------------------------------------
# Content check of the history folder (rebuilt in place): crc32 with a per-host cache
# --------------------------------------------------------------------------------------------
def _count_crc_reads(monkeypatch):
    reads = []
    real = cache_sync._crc32_file
    monkeypatch.setattr(cache_sync, "_crc32_file", lambda p, *a, **k: reads.append(Path(p).name) or real(p, *a, **k))
    return reads


def test_add_crc32_hashes_only_the_named_top_level_folders(tmp_path):
    _hist_cache(tmp_path)
    man = cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)),
                               ("AlpacaHistoryOHLCVProvider",), str(tmp_path))
    by = {f["rel_path"]: f for f in man["files"]}
    assert "crc32" in by["AlpacaHistoryOHLCVProvider/AAPL_1d.parquet"]
    assert "crc32" not in by["FMPOHLCVProvider/AAPL_1d.parquet"]
    assert "crc32" not in by["options/o.sqlite"]


def test_add_crc32_does_not_rehash_an_unchanged_file(tmp_path, monkeypatch):
    """The crc is computed when the cache CHANGES, not on every push: an unchanged file's crc
    comes from the per-host cache keyed by (size, mtime)."""
    _hist_cache(tmp_path)
    reads = _count_crc_reads(monkeypatch)
    dirs = ("AlpacaHistoryOHLCVProvider",)
    first = cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)), dirs, str(tmp_path))
    assert reads == ["AAPL_1d.parquet"]
    second = cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)), dirs, str(tmp_path))
    assert reads == ["AAPL_1d.parquet"]          # served from the cache
    assert first["files"] == second["files"]


def test_add_crc32_rehashes_a_file_rewritten_at_the_same_size(tmp_path, monkeypatch):
    import os
    _hist_cache(tmp_path)
    dirs = ("AlpacaHistoryOHLCVProvider",)
    p = tmp_path / "AlpacaHistoryOHLCVProvider" / "AAPL_1d.parquet"
    before = cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)), dirs, str(tmp_path))
    st = p.stat()
    p.write_bytes(b"b" * 10)                      # same size, new content
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    after = cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)), dirs, str(tmp_path))
    assert cache_sync.diff_content_mismatch(after["files"], before) == ["AlpacaHistoryOHLCVProvider/AAPL_1d.parquet"]


def test_add_crc32_survives_a_corrupt_cache_file(tmp_path, monkeypatch):
    _hist_cache(tmp_path)
    (tmp_path / cache_sync.CRC_CACHE_NAME).write_text("{not json")
    reads = _count_crc_reads(monkeypatch)
    man = cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)),
                               ("AlpacaHistoryOHLCVProvider",), str(tmp_path))
    assert reads == ["AAPL_1d.parquet"]
    assert all("crc32" in f for f in man["files"] if f["rel_path"].startswith("Alpaca"))


def test_add_crc32_keeps_other_folders_cache_entries(tmp_path, monkeypatch):
    """An fmp push must not evict the alpaca entries (and vice versa): each would re-hash its
    whole folder on the next push of the other source."""
    _hist_cache(tmp_path)
    reads = _count_crc_reads(monkeypatch)
    for dirs in (("AlpacaHistoryOHLCVProvider",), ("FMPOHLCVProvider",), ("AlpacaHistoryOHLCVProvider",)):
        cache_sync.add_crc32(cache_sync.build_manifest(str(tmp_path)), dirs, str(tmp_path))
    assert reads == ["AAPL_1d.parquet", "AAPL_1d.parquet"]   # once per folder, not three times


def test_missing_crc32_lists_same_size_files_the_remote_cannot_compare():
    local = [{"rel_path": "H/a", "size": 3, "crc32": 1}, {"rel_path": "H/b", "size": 3, "crc32": 2},
             {"rel_path": "H/c", "size": 3, "crc32": 3}, {"rel_path": "other/d", "size": 3}]
    remote = {"files": [{"rel_path": "H/a", "size": 3},               # no crc -> unverifiable
                        {"rel_path": "H/b", "size": 4},               # size differs -> diff_missing's
                        {"rel_path": "H/c", "size": 3, "crc32": 9},   # comparable
                        {"rel_path": "other/d", "size": 3}]}          # not a hashed folder
    assert cache_sync.missing_crc32(local, remote, ("H",)) == ["H/a"]


def _crc_of(data: bytes) -> int:
    import zlib
    return zlib.crc32(data)


def test_push_resends_a_history_file_changed_at_the_same_size(tmp_path, monkeypatch):
    """The 2026-10-22 failure: a rebuilt file at its old size was skipped by the size-only diff."""
    from app.services import worker_client
    _hist_cache(tmp_path)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(tmp_path))
    own = "AlpacaHistoryOHLCVProvider/AAPL_1d.parquet"
    gets, pushed = [], []

    class _R:
        def __init__(self, p): self.p = p
        def raise_for_status(self): pass
        def json(self): return self.p

    class _C:
        def __init__(self, **_k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **k):
            gets.append(k.get("params"))
            return _R({"files": [{"rel_path": own, "size": 10, "crc32": _crc_of(b"OLD-STALE!")},
                                 {"rel_path": "options/o.sqlite", "size": 1}]})
        def post(self, url, **k):
            if url.endswith("/cache/push"):
                import tarfile
                with tarfile.open(fileobj=io.BytesIO(b"".join(k["content"]))) as t:
                    pushed.extend(t.getnames())
            return _R({"extracted": len(pushed), "pruned": 0})

    monkeypatch.setattr(worker_client.httpx, "Client", _C)
    res = worker_client.push_cache({"name": "w", "url": "http://w"}, history_source="alpaca",
                                   log=lambda _m: None)
    assert gets == [{"hash_dirs": "AlpacaHistoryOHLCVProvider"}]
    assert pushed == [own] and res["pushed"] == 1


def test_push_refuses_a_worker_that_reports_no_crc(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="reports no crc32"):
        _push_with_remote(monkeypatch, tmp_path, "alpaca",
                          ["AlpacaHistoryOHLCVProvider/AAPL_1d.parquet", "options/o.sqlite"],
                          with_crc=False)
