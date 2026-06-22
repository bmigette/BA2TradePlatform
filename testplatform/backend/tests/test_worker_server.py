"""Worker server gate — password auth, /run-trial via the pool, and tar /cache/push extraction."""
import io

import pytest

import app.worker_server as ws
from app.services import cache_sync


class _FakeFuture:
    def __init__(self, v):
        self._v = v

    def result(self):
        return self._v


class _FakePool:
    def submit(self, _fn, config, metric):
        return _FakeFuture({"ok": True, "fitness": 42.0, "trades": 3, "error": None})


@pytest.fixture()
def client(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(ws, "_PASSWORD", "secret")
    monkeypatch.setattr(ws, "_CAPACITY", 4)
    monkeypatch.setattr(ws, "_POOL", _FakePool())
    return TestClient(ws.worker_app)


H = {"Authorization": "Bearer secret"}


def test_auth_gating(client):
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 403
    r = client.get("/health", headers=H)
    assert r.status_code == 200 and r.json()["capacity"] == 4 and r.json()["ok"] is True


def test_version(client):
    r = client.get("/version", headers=H)
    assert r.status_code == 200 and "git_commit" in r.json()


def test_run_trial(client):
    r = client.post("/run-trial", headers=H,
                    json={"config": {"v": 1}, "fitness_metric": "sharpe"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "fitness": 42.0, "trades": 3, "error": None}
    # auth still enforced
    assert client.post("/run-trial", json={"config": {}, "fitness_metric": "x"}).status_code == 401


def test_cache_push_extracts(client, tmp_path, monkeypatch):
    # Point the worker's cache at a temp dir; push a tar built from a separate "master" dir.
    dst = tmp_path / "worker_cache"
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))
    src = tmp_path / "master_cache"
    (src / "FMPOHLCVProvider").mkdir(parents=True)
    (src / "FMPOHLCVProvider" / "AAPL_1d.parquet").write_bytes(b"data" * 100)
    tar_bytes = b"".join(cache_sync.iter_tar(["FMPOHLCVProvider/AAPL_1d.parquet"], str(src)))

    r = client.post("/cache/push", headers=H, content=tar_bytes)
    assert r.status_code == 200 and r.json()["extracted"] == 1
    assert (dst / "FMPOHLCVProvider" / "AAPL_1d.parquet").read_bytes() == b"data" * 100


def test_localize_paths_remaps_master_cache_to_local():
    cfg = {
        "universe": {"mode": "screener",
                     "screener_store": r"C:\Users\basti\Documents\ba2\common\cache\screener\metric_store"},
        "screener_runtime": {"store": r"C:\Users\basti\Documents\ba2\common\cache\screener\metric_store"},
        "options_cache_db": r"C:\Users\basti\Documents\ba2\common\cache\options\options_history.sqlite",
        "experts": [{"class": "FMPRating"}],  # non-path data untouched
        "seed": 42,
    }
    out = ws._localize_paths(cfg, r"C:\Users\basti\Documents\ba2\common\cache", "/local/ba2/common/cache")
    assert out["universe"]["screener_store"] == "/local/ba2/common/cache/screener/metric_store"
    assert out["screener_runtime"]["store"] == "/local/ba2/common/cache/screener/metric_store"
    assert out["options_cache_db"] == "/local/ba2/common/cache/options/options_history.sqlite"
    assert out["experts"] == [{"class": "FMPRating"}] and out["seed"] == 42  # untouched
    # a path NOT under the master cache root is left alone
    assert ws._localize_paths("/some/other/path", r"C:\Users\basti\Documents\ba2\common\cache", "/local/c") == "/some/other/path"


def test_cache_push_rejects_traversal(client, tmp_path, monkeypatch):
    import tarfile
    dst = tmp_path / "worker_cache"
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode="w") as t:
        payload = b"x" * 10
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    r = client.post("/cache/push", headers=H, content=tb.getvalue())
    assert r.status_code == 200 and r.json()["skipped"] == 1
    assert not (tmp_path / "evil.txt").exists()
