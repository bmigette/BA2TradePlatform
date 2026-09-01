"""Worker server gate — password auth, /submit-trial + /job-status via the pool, and tar
/cache/push extraction."""
import io

import pytest

import app.worker_server as ws
from app.services import cache_sync


class _FakeFuture:
    """Immediately-resolved stand-in for concurrent.futures.Future -- .done() is always True,
    matching a pool that finishes synchronously inside .submit() (fine for these tests: they
    only care about the request/response shape, not real async timing)."""
    def __init__(self, v):
        self._v = v

    def done(self):
        return True

    def result(self):
        return self._v


class _FakePool:
    def submit(self, _fn, *args):
        if _fn.__name__ == "_persist_trial_worker":
            return _FakeFuture({"ok": True, "results": {"total_return": 12.3, "total_trades": 5}})
        return _FakeFuture({"ok": True, "fitness": 42.0, "trades": 3, "error": None})

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@pytest.fixture()
def client(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setattr(ws, "_PASSWORD", "secret")
    monkeypatch.setattr(ws, "_CAPACITY", 4)
    monkeypatch.setattr(ws, "_POOL", _FakePool())
    # Fresh job registry per test (module-level dicts would otherwise bleed state across tests).
    monkeypatch.setattr(ws, "_JOBS", {})
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {})
    # Fresh manifest cache per test (it is module-global; CACHE_FOLDER is re-pointed per test).
    monkeypatch.setattr(ws, "_MANIFEST_CACHE", {})
    return TestClient(ws.worker_app)


def _submit_and_get_job_id(client, path, config=None):
    r = client.post(path, headers=H,
                    json={"config": config or {"v": 1}, "fitness_metric": "sharpe"})
    assert r.status_code == 200
    return r.json()["job_id"]


H = {"Authorization": "Bearer secret"}


def test_auth_gating(client):
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 403
    r = client.get("/health", headers=H)
    assert r.status_code == 200 and r.json()["capacity"] == 4 and r.json()["ok"] is True


def test_version(client):
    r = client.get("/version", headers=H)
    assert r.status_code == 200 and "git_commit" in r.json()


def test_diag_memory_returns_snapshot(client):
    r = client.get("/diag/memory", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "server_rss_mb" in body and "child_rss_mb" in body and "system_available_mb" in body
    # auth still enforced
    assert client.get("/diag/memory").status_code == 401


def test_logs_list_and_tail(client, tmp_path, monkeypatch):
    import ba2_common.logger as bl
    monkeypatch.setattr(bl, "LOGS_DIR", str(tmp_path))
    (tmp_path / "app.log").write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    (tmp_path / "app.debug.log").write_text("debug\n")

    r = client.get("/logs/list", headers=H)
    assert r.status_code == 200
    assert set(r.json()["files"]) == {"app.log", "app.debug.log"}

    r = client.get("/logs", headers=H, params={"file": "app.log", "tail_lines": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["total_lines"] == 10
    assert body["lines"] == ["line7\n", "line8\n", "line9\n"]

    # auth still enforced
    assert client.get("/logs", params={"file": "app.log"}).status_code == 401


def test_logs_rejects_path_traversal(client, tmp_path, monkeypatch):
    import ba2_common.logger as bl
    monkeypatch.setattr(bl, "LOGS_DIR", str(tmp_path))
    (tmp_path / "app.log").write_text("safe\n")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("should never be readable via /logs")

    for traversal in ("../secret.txt", "..\\secret.txt", "/etc/passwd", "sub/app.log"):
        r = client.get("/logs", headers=H, params={"file": traversal})
        assert r.status_code == 400, f"expected 400 for {traversal!r}, got {r.status_code}"


def test_logs_unknown_file_404s(client, tmp_path, monkeypatch):
    import ba2_common.logger as bl
    monkeypatch.setattr(bl, "LOGS_DIR", str(tmp_path))
    r = client.get("/logs", headers=H, params={"file": "does-not-exist.log"})
    assert r.status_code == 404


def test_install_orchestration_file_logging_persists_root_warnings(tmp_path, monkeypatch):
    """The orchestration layer (worker_server.py's own logger, e.g. pool-crash / memory-dump
    warnings) previously went to console only -- nothing to show via /logs. Regression for the
    2026-07-20 remote150 incident where NEITHER the spawned trial children (file logging off by
    design) NOR this layer had persisted any record of what happened."""
    import logging as _logging
    import ba2_common.logger as bl

    monkeypatch.setattr(bl, "LOGS_DIR", str(tmp_path))
    monkeypatch.delenv("BA2_FILE_LOGGING", raising=False)
    root = _logging.getLogger()
    before = list(root.handlers)
    try:
        ws._install_orchestration_file_logging()
        ws.logger.warning("test warning for the orchestration log file")
        log_path = tmp_path / "worker_server.log"
        assert log_path.exists()
        assert "test warning for the orchestration log file" in log_path.read_text()

        # Calling it again must not attach a duplicate handler.
        n_before = len(root.handlers)
        ws._install_orchestration_file_logging()
        assert len(root.handlers) == n_before
    finally:
        # Restore the root logger to its pre-test state (avoid bleeding a file handler into
        # other tests' logging).
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_submit_trial_then_poll_returns_done_with_result(client):
    job_id = _submit_and_get_job_id(client, "/submit-trial")
    r = client.get(f"/job-status/{job_id}", headers=H)
    assert r.status_code == 200
    assert r.json() == {"status": "done",
                        "result": {"ok": True, "fitness": 42.0, "trades": 3, "error": None}}
    # auth still enforced on both endpoints
    assert client.post("/submit-trial", json={"config": {}, "fitness_metric": "x"}).status_code == 401
    assert client.get(f"/job-status/{job_id}").status_code == 401


class _PendingFuture:
    def done(self):
        return False


def test_job_status_running_before_done(client, monkeypatch):
    """A future that isn't done yet must report status=running, not block.

    Since 2026-08-06 it also carries the trial's `bars` heartbeat: without it the master cannot
    distinguish a queued trial from a computing one and can only wait out the full trial_timeout
    (see worker_client._submit_and_poll). No control block registered here -> bars 0.
    """
    monkeypatch.setattr(ws, "_JOBS", {"pending-job": _PendingFuture()})
    monkeypatch.setattr(ws, "_JOB_CTL", {})
    r = client.get("/job-status/pending-job", headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["bars"] == 0


def test_job_status_reports_the_trial_heartbeat(client, monkeypatch):
    """A started, progressing trial reports its bar count so the master can tell it apart from
    one that was accepted and never scheduled (the saturated-pool case that cost opt 251 ~9h)."""
    monkeypatch.setattr(ws, "_JOBS", {"live-job": _PendingFuture()})
    monkeypatch.setattr(ws, "_JOB_CTL", {"live-job": {"cancel": False, "bars": 42}})
    body = client.get("/job-status/live-job", headers=H).json()
    assert body == {"status": "running", "bars": 42, "started": True}


def test_job_status_unknown_id_returns_404(client):
    """An id the registry has never seen -- including one from BEFORE a restart, since the
    registry is in-memory only -- must 404 so the client can fail fast (see worker_client.py's
    WorkerJobLost) instead of polling forever."""
    r = client.get("/job-status/never-existed", headers=H)
    assert r.status_code == 404


def test_job_status_is_one_shot(client):
    """Once a done job's result has been fetched, the SAME job_id must 404 on a second poll —
    proves the registry entry is popped (bounded growth), not leaked."""
    job_id = _submit_and_get_job_id(client, "/submit-trial")
    first = client.get(f"/job-status/{job_id}", headers=H)
    assert first.status_code == 200 and first.json()["status"] == "done"
    second = client.get(f"/job-status/{job_id}", headers=H)
    assert second.status_code == 404


def test_submit_trial_broken_pool_at_submit_time_yields_retryable_done_job(client, monkeypatch):
    """A pool that's ALREADY broken when /submit-trial is called (submit() itself raises) must
    still hand back a normal job_id -- polling it should report done with a retryable failure,
    not a submit-time 500."""
    from concurrent.futures.process import BrokenProcessPool

    class _DeadPool:
        def submit(self, *a, **k):
            raise BrokenProcessPool("already dead")

    monkeypatch.setattr(ws, "_POOL", _DeadPool())
    rebuilt = []
    monkeypatch.setattr(ws, "_rebuild_pool", lambda exc: rebuilt.append(exc))

    job_id = _submit_and_get_job_id(client, "/submit-trial")
    r = client.get(f"/job-status/{job_id}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"]["retryable"] is True
    assert len(rebuilt) == 1


def test_shutdown_pool_for_restart_stops_rebuild(monkeypatch):
    """_shutdown_pool_for_restart() must clear _POOL_FACTORY (not just _POOL) so a concurrent
    /submit-trial handler (or /job-status, polling a future that breaks) that catches
    BrokenProcessPool during the restart window no-ops instead of spawning a fresh pool that
    _kill_children() would immediately kill again — the rebuild-then-kill storm this fix
    prevents."""
    built = []
    monkeypatch.setattr(ws, "_POOL", _FakePool())
    monkeypatch.setattr(ws, "_POOL_FACTORY", lambda: built.append(1) or _FakePool())

    ws._shutdown_pool_for_restart()

    assert ws._POOL is None
    assert ws._POOL_FACTORY is None
    ws._rebuild_pool(Exception("broken"))  # simulates an in-flight handler racing the shutdown
    assert built == [], "rebuild must no-op once _POOL_FACTORY has been cleared for restart"


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


def test_cache_manifest_with_hash(client, tmp_path, monkeypatch):
    dst = tmp_path / "worker_cache"
    (dst).mkdir(parents=True)
    (dst / "a.parquet").write_bytes(b"content")
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))

    r = client.get("/cache/manifest", headers=H)
    assert r.status_code == 200 and "crc32" not in r.json()["files"][0]

    r = client.get("/cache/manifest", headers=H, params={"with_hash": "true"})
    assert r.status_code == 200 and "crc32" in r.json()["files"][0]


def test_cache_manifest_is_cached(client, tmp_path, monkeypatch):
    # Second call must be served from the module cache, not rebuilt from disk.
    dst = tmp_path / "worker_cache"
    dst.mkdir(parents=True)
    (dst / "a.parquet").write_bytes(b"content")
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))

    assert client.get("/cache/manifest", headers=H).json()["count"] == 1

    # Add a file on disk WITHOUT going through /cache/push: a cached manifest must NOT see it.
    (dst / "b.parquet").write_bytes(b"more")
    assert client.get("/cache/manifest", headers=H).json()["count"] == 1  # served from cache

    # ...and a plain disk change must invalidate the next call once push/prune runs.


def test_cache_push_invalidates_manifest(client, tmp_path, monkeypatch):
    dst = tmp_path / "worker_cache"
    dst.mkdir(parents=True)
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))
    src = tmp_path / "master_cache"
    (src / "FMPOHLCVProvider").mkdir(parents=True)
    (src / "FMPOHLCVProvider" / "AAPL_1d.parquet").write_bytes(b"data")
    tar_bytes = b"".join(cache_sync.iter_tar(["FMPOHLCVProvider/AAPL_1d.parquet"], str(src)))

    assert client.get("/cache/manifest", headers=H).json()["count"] == 0   # cold, empty
    assert client.post("/cache/push", headers=H, content=tar_bytes).json()["extracted"] == 1
    assert client.get("/cache/manifest", headers=H).json()["count"] == 1   # push invalidated


def test_cache_prune_invalidates_manifest(client, tmp_path, monkeypatch):
    dst = tmp_path / "worker_cache"
    (dst / "screener").mkdir(parents=True)
    stale = dst / "screener" / "old.parquet"
    stale.write_bytes(b"stale")
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))

    assert client.get("/cache/manifest", headers=H).json()["count"] == 1
    r = client.post("/cache/prune", headers=H, json={"rel_paths": ["screener/old.parquet"]})
    assert r.json() == {"pruned": 1, "skipped": 0}
    assert client.get("/cache/manifest", headers=H).json()["count"] == 0   # prune invalidated


def test_submit_trial_full_then_poll_returns_complete_results(client):
    job_id = _submit_and_get_job_id(client, "/submit-trial-full")
    r = client.get(f"/job-status/{job_id}", headers=H)
    assert r.status_code == 200
    assert r.json() == {"status": "done",
                        "result": {"ok": True, "results": {"total_return": 12.3, "total_trades": 5}}}
    assert client.post("/submit-trial-full", json={"config": {}, "fitness_metric": "x"}).status_code == 401


def test_cache_prune_deletes_stale_leftovers(client, tmp_path, monkeypatch):
    dst = tmp_path / "worker_cache"
    (dst / "screener" / "metric_store" / "ym=2024-01").mkdir(parents=True)
    stale = dst / "screener" / "metric_store" / "ym=2024-01" / "part-00001.parquet"
    stale.write_bytes(b"old fragment")
    fresh = dst / "screener" / "metric_store" / "ym=2024-01" / "part.parquet"
    fresh.write_bytes(b"current")
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(dst))

    r = client.post("/cache/prune", headers=H,
                    json={"rel_paths": ["screener/metric_store/ym=2024-01/part-00001.parquet"]})
    assert r.status_code == 200 and r.json() == {"pruned": 1, "skipped": 0}
    assert not stale.exists()
    assert fresh.exists()
    # auth still enforced
    assert client.post("/cache/prune", json={"rel_paths": []}).status_code == 401


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


def test_localize_paths_windows_to_windows_worker_uses_backslashes():
    """The real production topology (master + remote150 both Windows): local_root is itself a
    backslash path, so the remapped result must be ALL backslashes too, not mixed -- os.path.join
    would have been fine here in isolation, but the fix must not regress THIS common case while
    fixing the POSIX-local_root one above."""
    out = ws._localize_paths(
        r"C:\Users\basti\Documents\ba2\common\cache\screener\metric_store",
        r"C:\Users\basti\Documents\ba2\common\cache",
        r"D:\ba2-worker\cache",
    )
    assert out == r"D:\ba2-worker\cache\screener\metric_store"


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


def test_sync_strategy_inserts_row(client, monkeypatch, tmp_path):
    db_path = tmp_path / "sync_test.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import importlib
    import app.models.database as dbmod
    importlib.reload(dbmod)
    monkeypatch.setattr(ws, "SessionLocal", dbmod.SessionLocal, raising=False)

    payload = {"id": 999, "name": "synced-strategy", "created_at": "2026-01-01T00:00:00+00:00"}
    r = client.post("/sync/strategy", headers=H, json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True

    from app.models.strategy import Strategy
    s = dbmod.SessionLocal()
    try:
        rows = s.query(Strategy).all()
        assert len(rows) == 1
        assert rows[0].name == "synced-strategy"
        assert rows[0].id != 999
    finally:
        s.close()


def test_sync_optimization_resolves_parent_strategy_id(client, monkeypatch, tmp_path):
    """End-to-end: seed a parent Strategy via a real /sync/strategy POST, then push a
    StrategyOptimization whose strategy_name/strategy_created_at match it, and confirm the FK
    resolves to the LOCAL strategy id (not the master's), matching the parent_fk contract
    exercised at the unit level in test_sync_receiver.py."""
    db_path = tmp_path / "sync_test_opt.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import importlib
    import app.models.database as dbmod
    importlib.reload(dbmod)
    monkeypatch.setattr(ws, "SessionLocal", dbmod.SessionLocal, raising=False)

    strategy_created_at = "2026-01-01T00:00:00+00:00"
    strat_payload = {"id": 111, "name": "opt-parent-strategy", "created_at": strategy_created_at}
    r = client.post("/sync/strategy", headers=H, json=strat_payload)
    assert r.status_code == 200 and r.json() == {"ok": True, "skipped": False}

    opt_created_at = "2026-01-02T00:00:00+00:00"
    opt_payload = {
        "id": 222, "name": "opt-1", "created_at": opt_created_at,
        "strategy_id": 111,  # the MASTER's id — must NOT end up on the local row verbatim
        "strategy_name": "opt-parent-strategy", "strategy_created_at": strategy_created_at,
        "status": "running", "fitness_metric": "sharpe", "optimization_type": "genetic",
    }
    r = client.post("/sync/optimization", headers=H, json=opt_payload)
    assert r.status_code == 200 and r.json() == {"ok": True, "skipped": False}

    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    s = dbmod.SessionLocal()
    try:
        strategy = s.query(Strategy).one()
        opt = s.query(StrategyOptimization).one()
        assert opt.name == "opt-1"
        assert opt.strategy_id == strategy.id
        assert opt.strategy_id != 111
    finally:
        s.close()


def test_auth_failures_log_the_source_ip(client, caplog):
    """The whole point of the source-IP log line: a fail2ban filter on the remote worker's log
    file bans the offending IP, so every failure mode must name it, and the bearer TOKEN must
    never appear in the log (it would defeat the point of a secret password)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="app.worker_server"):
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer wrong-password"}).status_code == 403
        assert client.get("/health", headers={"Authorization": "not-bearer-shaped"}).status_code == 401

    messages = [r.message for r in caplog.records]
    assert any("auth failed from testclient" in m and "missing Authorization header" in m
              for m in messages), messages
    assert any("auth failed from testclient" in m and "invalid worker password" in m
              for m in messages), messages
    assert any("auth failed from testclient" in m and "malformed Authorization header" in m
              for m in messages), messages
    assert not any("wrong-password" in m for m in messages), \
        "the submitted token must never be logged"


def test_a_successful_auth_logs_nothing(client, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="app.worker_server"):
        assert client.get("/health", headers=H).status_code == 200
    assert not any("auth failed" in r.message for r in caplog.records)


def test_sync_endpoints_require_auth(client):
    assert client.post("/sync/strategy", json={"name": "x", "created_at": "2026-01-01T00:00:00"}).status_code == 401
    assert client.post("/sync/optimization", json={"name": "x", "created_at": "2026-01-01T00:00:00"}).status_code == 401
    assert client.post("/sync/backtest", json={"name": "x", "created_at": "2026-01-01T00:00:00"}).status_code == 401


def test_health_reports_free_slots_not_just_nominal_capacity(client, monkeypatch):
    """REGRESSION (2026-08-05/06). remote150 answered ok:true capacity:6 while every slot was
    held by children of a killed master, so the pre-flight kept selecting it and every trial was
    accepted and never scheduled. capacity alone cannot express "full"."""
    monkeypatch.setattr(ws, "_CAPACITY", 6)
    monkeypatch.setattr(ws, "_JOBS", {f"j{i}": _PendingFuture() for i in range(6)})
    monkeypatch.setattr(ws, "_JOB_CTL", {})
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {})
    body = client.get("/health", headers=H).json()
    assert body["capacity"] == 6
    assert body["busy"] == 6 and body["free"] == 0


def test_abandoned_job_is_swept_when_master_stops_polling(monkeypatch):
    """A killed master never cancels its trials. Without this they held a slot for the full 6h
    orphan age -- the leak that jammed remote150's pool twice."""
    import time as _time
    ctl = {"cancel": False, "bars": 5}
    now = _time.monotonic()
    monkeypatch.setattr(ws, "_JOBS", {"dead": _PendingFuture()})
    monkeypatch.setattr(ws, "_JOB_CTL", {"dead": ctl})
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {"dead": now - 600})
    monkeypatch.setattr(ws, "_JOBS_LAST_POLL_AT", {"dead": now - ws._JOBS_ABANDONED_AFTER - 30})

    ws._sweep_orphaned_jobs()

    assert "dead" not in ws._JOBS, "abandoned job must be dropped so its slot frees"
    assert ctl["cancel"] is True, "and cooperatively cancelled so the child actually stops"


def test_actively_polled_job_is_never_swept(monkeypatch):
    """The counterpart: a long trial whose master is still polling must survive."""
    import time as _time
    ctl = {"cancel": False, "bars": 900}
    now = _time.monotonic()
    monkeypatch.setattr(ws, "_JOBS", {"alive": _PendingFuture()})
    monkeypatch.setattr(ws, "_JOB_CTL", {"alive": ctl})
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {"alive": now - 4 * 3600})   # 4h old
    monkeypatch.setattr(ws, "_JOBS_LAST_POLL_AT", {"alive": now - 3})          # polled 3s ago

    ws._sweep_orphaned_jobs()

    assert "alive" in ws._JOBS
    assert ctl["cancel"] is False
