"""Cache-sync gate — manifest, diff, tar push round-trip, traversal guard.

Proves the "avoid redownload" core for the PUSH model: the master diffs its cache against a
worker's manifest, streams ONLY the missing files as one tar, and the worker extracts it
(traversal-guarded). A re-push after sync is a no-op.
"""
import io

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
