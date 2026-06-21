"""Cache-sync gate — manifest enumeration, traversal guard, token auth, and a real round-trip.

Proves the "avoid redownload" core: a worker mirrors the master's cache over HTTP, downloads
exactly the missing files, and a re-run is a no-op (size-match skip). Path traversal is rejected.
"""
import os
import socket
import threading
import time

import httpx
import pytest

from app.services import cache_sync


def _write(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_build_manifest_lists_and_excludes(tmp_path):
    _write(tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"x" * 100)
    _write(tmp_path / "screener" / "metric_store" / "ym=2024-01" / "part.parquet", b"y" * 50)
    _write(tmp_path / "options" / "options_history.sqlite", b"z" * 10)
    _write(tmp_path / "junk.tmp", b"nope")           # excluded (temp)
    _write(tmp_path / "screener" / ".hidden", b"no")  # excluded (hidden)

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


@pytest.fixture(scope="module")
def app_and_token():
    os.environ["BA2_WORKER_TOKEN"] = "unit-tok"
    import app.main as m
    from app.models.database import init_db
    init_db()  # create tables in the isolated test DB (TestClient doesn't fire startup events)
    return m.app, "unit-tok"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_server(app_and_token):
    """Run the real ASGI app on an ephemeral port (faithful HTTP, also the in-process e2e check)."""
    import uvicorn
    app, token = app_and_token
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("live server did not start")
    yield base, token
    server.should_exit = True
    thread.join(timeout=5)


def test_endpoints_auth_and_traversal(app_and_token, tmp_path, monkeypatch):
    app, token = app_and_token
    from starlette.testclient import TestClient
    src = tmp_path / "cache"
    _write(src / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"data" * 25)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(src))

    c = TestClient(app)
    H = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/cache/manifest").status_code == 401            # no auth
    assert c.get("/api/cache/manifest", headers={"Authorization": "Bearer x"}).status_code == 403
    man = c.get("/api/cache/manifest", headers=H).json()
    assert man["count"] == 1
    # download a real file
    r = c.get("/api/cache/download", params={"path": "FMPOHLCVProvider/AAPL_1d.parquet"}, headers=H)
    assert r.status_code == 200 and r.content == b"data" * 25
    # traversal blocked, missing -> 404
    assert c.get("/api/cache/download", params={"path": "../../etc/passwd"}, headers=H).status_code == 400
    assert c.get("/api/cache/download", params={"path": "nope.parquet"}, headers=H).status_code == 404


def test_sync_cache_roundtrip_and_noop(live_server, tmp_path, monkeypatch):
    base, token = live_server
    src = tmp_path / "master_cache"
    dst = tmp_path / "worker_cache"
    _write(src / "FMPOHLCVProvider" / "AAPL_1d.parquet", b"a" * 1000)
    _write(src / "fmp_history" / "MSFT.json", b"b" * 500)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(src))

    res = cache_sync.sync_cache(base, token, dest=str(dst), max_workers=4, log=lambda *_: None)
    assert res["total"] == 2 and res["downloaded"] == 2 and not res["failed"]
    assert (dst / "FMPOHLCVProvider" / "AAPL_1d.parquet").read_bytes() == b"a" * 1000
    assert (dst / "fmp_history" / "MSFT.json").read_bytes() == b"b" * 500

    # Re-run: everything is up to date (size match) -> no downloads.
    res2 = cache_sync.sync_cache(base, token, dest=str(dst), max_workers=4, log=lambda *_: None)
    assert res2["downloaded"] == 0 and res2["skipped"] == 2
