"""A worker may only run a trial pinned to a market-condition manifest it has PREPARED.

Plan Task 7, design section 4.5: "Each worker acknowledges the same manifest digest and a
successful local verification/mapping preparation before it receives trials. A missing or
incompatible worker snapshot makes that worker unready. It must not fetch, use another snapshot or
report a feature-cache miss as a zero-trade strategy result."

Pinned here: the refusal is a DISTINCT error (409), not a zero-trade result; ``/market-conditions/
prepare`` verifies and maps and only then admits trials; a corrupt object leaves the worker
unready; ``/health`` publishes the prepared digests; the master excludes a worker that cannot
prepare; and the GA trial config actually carries the digest and the profile (without which every
one of the above is theatre).
"""
from __future__ import annotations

import inspect
import json
import os
from datetime import date

import numpy as np
import pytest

import app.worker_server as ws
from app.services import cache_sync

from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_conditions import PROFILES, STATUS_INSUFFICIENT_HISTORY

PROFILE = PROFILES["ohlcv-v1"]
FIELDS = [f.name for f in PROFILE.fields]
H = {"Authorization": "Bearer secret"}
SESSIONS = [date(2024, 3, 26), date(2024, 3, 27)]


class _FakeFuture:
    def __init__(self, v):
        self._v = v

    def done(self):
        return True

    def result(self):
        return self._v


class _FakePool:
    """Runs the submitted callable inline for a PREPARATION (that is the unit under test) and
    canned-answers a trial (its content is irrelevant here)."""

    def __init__(self):
        self.submitted = []
        self.prepared = []

    def submit(self, _fn, *args):
        self.submitted.append(args)
        if getattr(_fn, "__name__", "") == "prepare_host_job":
            self.prepared.append(args)
            return _FakeFuture(_fn(*args))
        return _FakeFuture({"ok": True, "fitness": 1.0, "trades": 0, "error": None})

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def _row(session):
    return {"session": session, "values": [None] * len(FIELDS),
            "status": [STATUS_INSUFFICIENT_HISTORY] * len(FIELDS),
            "reasons": ["young listing"] * len(FIELDS),
            "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
            "raw_row_lo": 0, "raw_row_hi": 0}


def _publish(root, symbols=("AAA", "BBB")):
    store = MarketConditionStore(root)
    objects = []
    for symbol in symbols:
        entry, _ = store.write_feature_object(PROFILE, symbol, [_row(s) for s in SESSIONS])
        objects.append(entry)
    manifest = store.make_manifest(
        PROFILE, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={s: {"rows": 2} for s in symbols},
        universe=symbols, sessions=SESSIONS, window_start=SESSIONS[0], window_end=SESSIONS[-1])
    return store, store.write_manifest(manifest)


@pytest.fixture()
def worker(monkeypatch, tmp_path):
    """A worker server whose cache root is ``tmp_path/cache`` and whose readiness starts empty."""
    from starlette.testclient import TestClient
    import ba2_common.config as bc

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(cache))
    monkeypatch.setattr(cache_sync, "CACHE_FOLDER", str(cache))
    pool = _FakePool()
    monkeypatch.setattr(ws, "_PASSWORD", "secret")
    monkeypatch.setattr(ws, "_CAPACITY", 2)
    monkeypatch.setattr(ws, "_POOL", pool)
    monkeypatch.setattr(ws, "_JOBS", {})
    monkeypatch.setattr(ws, "_JOBS_SUBMITTED_AT", {})
    monkeypatch.setattr(ws, "_MANIFEST_CACHE", {})
    monkeypatch.setattr(ws, "_PREPARED_MC", {})
    monkeypatch.setattr(ws, "_PREPARED_MC_LOADED", False)
    return TestClient(ws.worker_app), cache, pool


def _submit(client, digest):
    return client.post("/submit-trial", headers=H, json={
        "config": {"market_condition_profile": "ohlcv-v1", "market_condition_manifest": digest},
        "fitness_metric": "sharpe"})


def _prepare(client, digest, profile=None):
    """POST /market-conditions/prepare and collect the job's report the way the master does.

    The endpoint is submit/poll (a cold verification of a season of objects is minutes of I/O,
    and as a blocking handler it was indistinguishable from a hang); the digest is admitted when
    the poll that collects the result runs, so a test that skipped the poll would be testing a
    worker nobody ever finished preparing."""
    body = {"manifest": digest}
    if profile:
        body["profile"] = profile
    r = client.post("/market-conditions/prepare", headers=H, json=body)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    status = client.get(f"/job-status/{job_id}", headers=H)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["status"] == "done", body
    return body["result"]


def test_a_trial_pinned_to_an_unprepared_manifest_is_refused_with_a_distinct_error(worker):
    client, cache, pool = worker
    _store, digest = _publish(cache)
    r = _submit(client, digest)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert digest in detail and "not prepared" in detail
    assert "/market-conditions/prepare" in detail
    # The point of the refusal: NOTHING ran, so no zero-trade fitness was produced.
    assert pool.submitted == []


def test_after_prepare_the_same_trial_is_accepted(worker):
    client, cache, pool = worker
    _store, digest = _publish(cache)
    body = _prepare(client, digest)
    assert body["ok"] and body["verified"] and body["symbols"] == 2 and body["rows"] == 4
    assert body["objects_checked"] == 2

    r = _submit(client, digest)
    pool.submitted = [a for a in pool.submitted if a not in pool.prepared]
    assert r.status_code == 200 and r.json()["job_id"]
    assert len(pool.submitted) == 1


def test_a_trial_without_a_pinned_manifest_is_unaffected(worker):
    client, _cache, pool = worker
    r = client.post("/submit-trial", headers=H,
                    json={"config": {"v": 1}, "fitness_metric": "sharpe"})
    assert r.status_code == 200 and len(pool.submitted) == 1


def test_a_corrupt_object_fails_prepare_and_the_worker_stays_unready(worker):
    client, cache, pool = worker
    store, digest = _publish(cache)
    manifest = store.read_manifest(digest)
    victim = store.abspath(manifest["objects"][0]["path"])
    size = victim.stat().st_size
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF
    victim.write_bytes(bytes(data))
    assert victim.stat().st_size == size

    out = _prepare(client, digest)
    assert out["ok"] is False
    assert any("corrupt" in e for e in out["errors"])
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    assert _submit(client, digest).status_code == 409
    assert pool.submitted == pool.prepared, "no TRIAL was accepted"


def test_an_unknown_manifest_is_an_unready_answer_not_a_500(worker):
    client, _cache, _pool = worker
    out = _prepare(client, "0" * 64)
    assert out["ok"] is False and out["errors"]


def test_prepare_is_submit_and_poll_and_reports_its_stage(worker, monkeypatch):
    """Not a blocking handler: verifying a season of objects and building the mapping is minutes
    of I/O on a cold box, and a blocked HTTP call is indistinguishable from a hung worker."""
    client, cache, pool = worker
    _store, digest = _publish(cache)
    r = client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
    assert r.status_code == 200 and r.json()["job_id"] and r.json()["manifest"] == digest
    assert pool.prepared, "the preparation runs on the pool, not in the request handler"

    # The job writes its stage into the control block, which /job-status surfaces instead of a
    # bare "running" (the same ambiguity the trial bar heartbeat removes).
    from ba2_common.core.market_condition_reader import prepare_host_job
    ctl = {}
    out = prepare_host_job(str(cache), digest, None, 1, ctl)
    assert out["ok"] and ctl.get("stage")


def test_health_lists_the_prepared_digests(worker):
    client, cache, _pool = worker
    _store, digest = _publish(cache)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    _prepare(client, digest)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]


def test_health_drops_a_digest_whose_mapping_was_swept(worker):
    """``build_shared_arrays.py --sweep`` can collect a mapping between runs. A worker that kept
    advertising it would have the master skip preparation, and every worker process would then
    rebuild the mapping under its own first trial -- the cold-start stampede the prewarm exists
    to prevent."""
    import shutil

    from ba2_common.core.market_condition_reader import _derived_root, mapped_key

    client, cache, _pool = worker
    _store, digest = _publish(cache)
    _prepare(client, digest)
    shutil.rmtree(os.path.join(_derived_root(str(cache)), mapped_key("ohlcv-v1", digest)))
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []


def test_readiness_survives_a_restart_through_the_host_local_marker(worker, monkeypatch):
    client, cache, _pool = worker
    _store, digest = _publish(cache)
    _prepare(client, digest)
    # A self-update restart drops the process's memory; the _derived marker is what a fresh
    # process reads so the box does not re-verify its whole feature bucket to run one trial.
    monkeypatch.setattr(ws, "_PREPARED_MC", {})
    monkeypatch.setattr(ws, "_PREPARED_MC_LOADED", False)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]
    assert _submit(client, digest).status_code == 200


def test_a_push_that_carries_corrupt_objects_revokes_readiness(worker, tmp_path):
    client, cache, _pool = worker
    store, digest = _publish(cache)
    _prepare(client, digest)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]

    # A "master" whose object bytes differ at the same size (a bad transfer, a bad disk).
    master = tmp_path / "master"
    manifest = store.read_manifest(digest)
    rel = f"market_conditions/{manifest['objects'][0]['path']}"
    target = master / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(store.abspath(manifest["objects"][0]["path"]).read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    tar = b"".join(cache_sync.iter_tar([rel], str(master)))

    r = client.post("/cache/push", headers=H, content=tar)
    assert r.status_code == 200
    body = r.json()
    assert body["market_conditions"] == 1
    assert body["market_conditions_verified"]["ok"] is False
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    assert _submit(client, digest).status_code == 409


def _corrupt_push(client, store, cache, tmp_path, digest, name="master"):
    """Push one object of ``digest`` whose bytes differ at the same size."""
    master = tmp_path / name
    manifest = store.read_manifest(digest)
    rel = f"market_conditions/{manifest['objects'][0]['path']}"
    target = master / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(store.abspath(manifest["objects"][0]["path"]).read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    return client.post("/cache/push", headers=H,
                       content=b"".join(cache_sync.iter_tar([rel], str(master))))


def test_a_revoke_holds_even_when_the_markers_were_never_loaded(worker, tmp_path, monkeypatch):
    """THE BUG THIS PINS. Readiness is remembered in memory AND in a ``_derived`` marker, and the
    markers are loaded lazily. A revoke that only cleared the dict therefore cleared nothing at
    all when nothing had triggered the lazy load yet (no /health, no submit since start-up) --
    and the very next call seeded the revoked digest straight back from its marker. So the load
    flag is forced before the clear, and the marker is renamed aside."""
    client, cache, pool = worker
    store, digest = _publish(cache)
    _prepare(client, digest)
    # Simulate a process that has prepared nothing itself and has not read the markers yet.
    monkeypatch.setattr(ws, "_PREPARED_MC", {})
    monkeypatch.setattr(ws, "_PREPARED_MC_LOADED", False)

    assert _corrupt_push(client, store, cache, tmp_path, digest).status_code == 200

    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    assert _submit(client, digest).status_code == 409
    assert pool.submitted == pool.prepared


def test_a_revoke_survives_a_restart(worker, tmp_path, monkeypatch):
    """The other half: the marker is what a restarted process reads, so a revoke that leaves it
    behind re-admits the rejected snapshot on the next start-up."""
    client, cache, _pool = worker
    store, digest = _publish(cache)
    _prepare(client, digest)
    assert _corrupt_push(client, store, cache, tmp_path, digest).status_code == 200

    monkeypatch.setattr(ws, "_PREPARED_MC", {})
    monkeypatch.setattr(ws, "_PREPARED_MC_LOADED", False)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    assert _submit(client, digest).status_code == 409
    # The revoked marker is KEPT (renamed), because "this host was ready and stopped being
    # ready" is the diagnostic an operator needs after a corrupt push.
    from ba2_common.core.market_condition_reader import _derived_root
    markers = os.listdir(os.path.join(_derived_root(str(cache)), "_prepared"))
    assert [m for m in markers if m.endswith(".revoked")] and not [
        m for m in markers if m.endswith(".json")]


def test_a_healthy_digest_survives_a_push_that_corrupts_another_snapshots_object(worker, tmp_path):
    """Verification is SCOPED to what the push delivered, and only the digests whose OWN objects
    failed lose readiness -- otherwise one stale snapshot on a long-lived worker revokes every
    digest on the box, and every push re-hashes the whole bucket."""
    client, cache, pool = worker
    good_store, good = _publish(cache, symbols=("AAA", "BBB"))
    bad_store, bad = _publish(cache, symbols=("CCC",))
    _prepare(client, good)
    _prepare(client, bad)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == sorted(
        [good, bad])

    r = _corrupt_push(client, bad_store, cache, tmp_path, bad)
    assert r.status_code == 200
    body = r.json()
    assert body["market_conditions_verified"]["ok"] is False
    assert body["market_conditions_verified"]["failed_digests"] == [bad]
    # Only the bad snapshot was even LOOKED at: the healthy one shares no object with this push.
    assert body["market_conditions_verified"]["checked_digests"] == [bad]
    assert body["market_conditions_revoked"] == [bad]

    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [good]
    assert _submit(client, good).status_code == 200
    assert _submit(client, bad).status_code == 409


# ---------------------------------------------------------------------------------------------
# Master side
# ---------------------------------------------------------------------------------------------
def test_the_master_excludes_a_worker_that_cannot_prepare(monkeypatch):
    from app.services import distributed_eval as de

    logged = []
    ev = de.DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t", workers=[],
                                 log=logged.append,
                                 market_condition_manifests={"ohlcv-v1": "d" * 64})
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, v, **k: True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "prepare_market_conditions",
                        lambda w, d, p=None, **k: {"ok": False, "errors": ["corrupt object"]})
    assert ev._preflight_worker({"name": "remote1"}, {}) is False
    assert any("EXCLUDING" in m for m in logged)

    monkeypatch.setattr(de.worker_client, "prepare_market_conditions",
                        lambda w, d, p=None, **k: {"ok": True, "built": True, "symbols": 2,
                                                   "objects_checked": 2})
    monkeypatch.setattr(ev, "_health_with_retry", lambda w: {"capacity": 1, "capacity_max": 1})
    monkeypatch.setattr(ev, "_size_remote_pool", lambda w: None)
    assert ev._preflight_worker({"name": "remote1"}, {}) is True


def test_a_worker_too_old_for_the_endpoint_is_unready(monkeypatch):
    import httpx

    from app.services import worker_client

    class _Resp:
        status_code = 404

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(RuntimeError, match="market-conditions/prepare"):
        worker_client.prepare_market_conditions({"name": "old", "url": "http://x", "password": "p"},
                                                "d" * 64)


def test_the_ga_trial_config_carries_the_digest_and_the_profile():
    """Readiness is meaningless if the digest never reaches the trial: ``_build_daily_trial_config``
    rebuilds the config KEY BY KEY, so a knob absent from it is inert while every log upstream
    still claims the run is pinned (the known whitelist trap)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    backtest_cfg = {
        "backtest_id": 1, "start_date": "2024-01-02", "end_date": "2024-06-28",
        "enabled_instruments": ["AAPL"], "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0, "account_settings": {}, "warmup_days": 60, "seed": 7,
        "market_condition_profile": "ohlcv-v1", "market_condition_manifest": "f" * 64,
    }
    cfg = _build_daily_trial_config(backtest_cfg, {}, option_trade_records=False)
    assert cfg["market_condition_profiles"] == ["ohlcv-v1"]
    assert cfg["market_condition_manifests"] == {"ohlcv-v1": "f" * 64}
    assert cfg["_ga_trial"] is True

    plain = _build_daily_trial_config({**backtest_cfg, "market_condition_profile": None,
                                       "market_condition_manifest": None}, {}, option_trade_records=False)
    assert plain["market_condition_profiles"] == []
    assert plain["market_condition_manifests"] == {}


def test_the_worker_env_keys_no_longer_carry_the_profile_or_manifest():
    """RETIRED with Task 12. The profile is an expert setting carried INSIDE the trial config's
    expert settings, and a set ``BA2_MARKET_CONDITION_PROFILE`` now fails live startup -- so
    mirroring it into a spawned worker would have propagated a refusal. The manifest goes with
    it: it only rode along for environment-resolved diagnostics of a resolver that no longer
    resolves from the environment, while ``market_condition_manifests`` on the trial config has
    always been what a backtest actually reads."""
    from app.services.strategy_optimization_handler import _WORKER_ENV_KEYS

    assert "BA2_MARKET_CONDITION_PROFILE" not in _WORKER_ENV_KEYS
    assert "BA2_MARKET_CONDITION_MANIFEST" not in _WORKER_ENV_KEYS
    # The keys that ARE mirrored are unchanged (this list is not a free-for-all).
    assert _WORKER_ENV_KEYS == ("FMP_API_KEY", "ALPHA_VANTAGE_API_KEY", "FINNHUB_API_KEY",
                                "OPENAI_API_KEY", "BA2_SHARED_ARRAYS",
                                "BA2_SHARED_ARRAYS_LOCK_STALE_S")


def test_a_409_takes_the_worker_out_at_once_instead_of_three_requeues(monkeypatch):
    """A refusal is not a flaky trial: the digest is pinned for the whole run, so retrying the
    same trial on the same worker cannot succeed. Three requeue rounds would only delay the
    exclusion while the queue drains through a worker that answers 409 to everything."""
    import httpx

    from app.services import distributed_eval as de

    logged = []
    ev = de.DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t",
                                 workers=[], log=logged.append)
    w = {"name": "remote1", "url": "http://x", "password": "p"}
    with ev._worker_lock:
        ev._active_workers.append(w)
        ev._worker_epochs[w["name"]] = 0

    request = httpx.Request("POST", "http://x/submit-trial")
    refusal = httpx.HTTPStatusError(
        "409", request=request, response=httpx.Response(409, request=request))
    assert de._is_unprepared_snapshot(refusal) is True
    assert de._is_unprepared_snapshot(RuntimeError("boom")) is False
    assert de._is_unprepared_snapshot(httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request))) is False

    calls = {"n": 0}

    def _refuse(*a, **k):
        calls["n"] += 1
        raise refusal

    monkeypatch.setattr(de.worker_client, "run_trial", _refuse)
    # Drive the dispatcher directly: claim() always has work, so only the refusal handling can
    # end the loop -- which is exactly the property under test.
    monkeypatch.setattr(ev.broker, "claim",
                        lambda worker_id=None: {"trial_id": "t1", "config": {},
                                                "fitness_metric": "sharpe"})
    monkeypatch.setattr(ev.broker, "requeue_one", lambda tid: None)
    ev._dispatch_remote(w, slot_idx=0, epoch=0)

    assert calls["n"] == 1, "the worker must be dropped on the FIRST refusal"
    assert any("REFUSED" in m for m in logged)
    with ev._worker_lock:
        assert w not in ev._active_workers


class _Report:
    def __init__(self, ok=True, errors=None):
        self.ok = ok
        self.errors = errors or []
        self.built = False
        self.objects_checked = 3
        self.raw_checked = 1
        self.symbols = 2
        self.elapsed_s = 0.1


def test_the_master_prepares_its_own_bucket_before_dispatch(monkeypatch):
    """The master is a worker too: it runs local trials and in-process re-runs. Until this, only
    REMOTE workers verified the pinned snapshot, which made the runbook's warm step a correctness
    requirement rather than an optimisation. A failed verification is a FAILED JOB -- every trial
    would otherwise score against a snapshot that does not hash to its manifest."""
    from app.services import strategy_optimization_handler as H_

    calls = []

    def _spy(cache_root, digest, profile=None, **kw):
        calls.append((digest, profile))
        return _Report()

    monkeypatch.setattr("ba2_common.core.market_condition_reader.prepare_host", _spy)
    failures = []
    monkeypatch.setattr(H_, "_fail", lambda opt_id, db, msg: failures.append(msg) or
                        {"status": "failed", "error": msg})

    # No manifest pinned -> nothing happens at all (every existing run).
    assert H_._prepare_master_market_conditions(1, None, {}) is None
    assert calls == []

    cfg = {"market_condition_manifest": "f" * 64, "market_condition_profile": "ohlcv-v1"}
    assert H_._prepare_master_market_conditions(1, None, cfg) is None
    assert calls == [("f" * 64, "ohlcv-v1")]
    assert failures == []

    monkeypatch.setattr("ba2_common.core.market_condition_reader.prepare_host",
                        lambda *a, **k: _Report(ok=False, errors=["corrupt object x"]))
    out = H_._prepare_master_market_conditions(1, None, cfg)
    assert out == {"status": "failed", "error": failures[-1]}
    assert "FAILED verification" in failures[-1] and "corrupt object x" in failures[-1]

    def _raise(*a, **k):
        raise FileNotFoundError("no manifest here")

    monkeypatch.setattr("ba2_common.core.market_condition_reader.prepare_host", _raise)
    assert H_._prepare_master_market_conditions(1, None, cfg)["status"] == "failed"
    assert "could not be prepared" in failures[-1]


def test_the_master_prepare_runs_before_any_trial_is_dispatched():
    """Placement matters as much as existence: after the evaluator starts, remote workers have
    already been pre-flighted and local trials are in flight."""
    from app.services import strategy_optimization_handler as H_

    src = inspect.getsource(H_.handle_strategy_optimization)
    assert "_prepare_master_market_conditions(" in src
    assert src.index("_prepare_master_market_conditions(") < src.index("_evaluator.start()")


def test_the_pinned_digest_round_trips_through_the_persisted_backtest_config():
    """CONTRACT for every consumer of ``_build_daily_trial_config`` (tools/backtest_parity.py,
    run_genome_once.py, recover_missing_topn.py, genome_concentration_check.py): they rebuild a
    trial config from the PERSISTED ``optimization_config.backtest`` of a stored run, so the
    manifest digest has to be persisted there by the launcher (Task 8) and read back out here.

    The parity tool is deliberately NOT exempt from the refusal: a gated genome re-run without
    its manifest would compute its own feature rows and could legitimately produce different
    trades, which is precisely what a parity check must not silently do."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    persisted = {
        "backtest_id": 7, "start_date": "2024-01-02", "end_date": "2024-06-28",
        "enabled_instruments": ["AAPL"], "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0, "account_settings": {}, "warmup_days": 60, "seed": 3,
        "market_condition_profile": "ohlcv-v1", "market_condition_manifest": "e" * 64,
    }
    # The round trip a re-run tool performs: persist as JSON, read back, rebuild the trial config.
    restored = json.loads(json.dumps({"backtest": persisted}))["backtest"]
    cfg = _build_daily_trial_config(restored, {}, option_trade_records=False)
    assert cfg["market_condition_manifests"] == {"ohlcv-v1": "e" * 64}
    assert cfg["market_condition_profiles"] == ["ohlcv-v1"]
    assert cfg["_ga_trial"] is True


def test_a_stale_unreadable_manifest_does_not_revoke_a_healthy_prepared_digest(worker, tmp_path):
    """The reproduced regression, end to end: a truncated manifest sits in the bucket, an
    ORDINARY healthy push arrives, and the worker must still be ready for the snapshot it
    prepared. Before the fix the pass reported failure with no digest named, and
    ``failed_digests or None`` revoked the entire host."""
    client, cache, pool = worker
    store, digest = _publish(cache)
    _prepare(client, digest)

    # A leftover from some other job: unparseable, nothing to do with this push.
    stale = cache / "market_conditions" / "ohlcv-v1" / "manifests" / ("c" * 64 + ".json")
    stale.write_text("{truncated", encoding="utf-8")

    manifest = store.read_manifest(digest)
    rel = f"market_conditions/{manifest['objects'][0]['path']}"
    master = tmp_path / "healthy"
    (master / rel).parent.mkdir(parents=True, exist_ok=True)
    (master / rel).write_bytes(store.abspath(manifest["objects"][0]["path"]).read_bytes())
    r = client.post("/cache/push", headers=H,
                    content=b"".join(cache_sync.iter_tar([rel], str(master))))

    assert r.status_code == 200
    verdict = r.json()["market_conditions_verified"]
    assert verdict["ok"] is True and verdict["failed_digests"] == []
    assert verdict["out_of_scope_errors"], "the stale file is still REPORTED"
    assert r.json().get("market_conditions_revoked") is None
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]
    assert _submit(client, digest).status_code == 200


def test_a_verification_failure_that_names_no_digest_revokes_nothing(worker, monkeypatch, tmp_path):
    """Defence in depth for the same hole: if the pass ever says "failed" without naming a
    snapshot, the honest answer is a loud error, not disarming the whole box."""
    client, cache, _pool = worker
    _store, digest = _publish(cache)
    _prepare(client, digest)

    monkeypatch.setattr(cache_sync, "verify_market_conditions",
                        lambda *a, **k: {"ok": False, "manifests": 1, "checked": 0, "missing": [],
                                         "corrupt": [], "errors": ["something odd"],
                                         "out_of_scope_errors": [], "failed_digests": [],
                                         "checked_digests": []})
    master = tmp_path / "m"
    rel = "market_conditions/ohlcv-v1/manifests/x.json"
    (master / rel).parent.mkdir(parents=True, exist_ok=True)
    (master / rel).write_text("{}", encoding="utf-8")
    r = client.post("/cache/push", headers=H,
                    content=b"".join(cache_sync.iter_tar([rel], str(master))))

    assert r.status_code == 200 and r.json()["market_conditions_revoked"] == []
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]


def test_the_job_sweep_drops_an_unpolled_prepare_jobs_sidecar_entry(worker, monkeypatch):
    """``_MC_JOBS`` is the sidecar that lets the poll admit the digest. A preparation nobody polls
    (the master died mid-pre-flight) must not leave an entry behind for the life of the process --
    the registry sweep that drops the job drops this too. The WORK is not lost: it wrote its
    ``_derived`` marker, so the next pre-flight learns the digest from disk."""
    client, cache, _pool = worker
    _store, digest = _publish(cache)

    r = client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
    job_id = r.json()["job_id"]
    assert job_id in ws._MC_JOBS                    # submitted, never polled

    # Age the registry past the abandoned threshold and run the sweep (what the next submit does).
    with ws._JOBS_LOCK:
        ws._JOBS_SUBMITTED_AT[job_id] -= ws._JOBS_ABANDONED_AFTER + 60
    ws._sweep_orphaned_jobs()

    assert job_id not in ws._MC_JOBS
    assert job_id not in ws._JOBS
    # ...and the host still knows it prepared that snapshot, from the marker on disk.
    monkeypatch.setattr(ws, "_PREPARED_MC", {})
    monkeypatch.setattr(ws, "_PREPARED_MC_LOADED", False)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]


def test_a_profile_with_no_manifest_fails_the_JOB_not_every_trial_of_it(monkeypatch):
    """Two profiles, one warmed: filtering the unpinned one out would let the master prepare and
    every worker pre-flight report success, and then have EVERY trial die inside the seam on the
    worker. The master prepare exists so the job fails before dispatch."""
    from app.services import strategy_optimization_handler as H_

    calls = []
    monkeypatch.setattr("ba2_common.core.market_condition_reader.prepare_host",
                        lambda cache_root, digest, profile=None, **kw:
                        calls.append((digest, profile)) or _Report())
    failures = []
    monkeypatch.setattr(H_, "_fail", lambda opt_id, db, msg: failures.append(msg) or
                        {"status": "failed", "error": msg})

    cfg = {"market_condition_profiles": ["ohlcv-v1", "ta-structure-v1"],
           "market_condition_manifests": {"ohlcv-v1": "f" * 64}}
    out = H_._prepare_master_market_conditions(1, None, cfg)
    assert out == {"status": "failed", "error": failures[-1]}
    assert "ta-structure-v1" in failures[-1] and "pins no manifest" in failures[-1]
    assert calls == [], "nothing may be prepared once the run is known to be under-pinned"

    # Both pinned: both snapshots are prepared, in config order.
    cfg["market_condition_manifests"]["ta-structure-v1"] = "e" * 64
    assert H_._prepare_master_market_conditions(1, None, cfg) is None
    assert calls == [("f" * 64, "ohlcv-v1"), ("e" * 64, "ta-structure-v1")]


def test_an_unrecognised_manifests_shape_refuses_the_trial_instead_of_disabling_the_guard():
    """Reading a shape it does not know as "nothing pinned" would turn the readiness guard into a
    no-op and produce exactly the zero-trade fitness the guard exists to prevent."""
    from fastapi import HTTPException

    from app.worker_server import _mc_guard, _mc_required_digests

    assert _mc_required_digests({}) == []
    assert _mc_required_digests({"market_condition_manifests": {}}) == []
    assert _mc_required_digests({"market_condition_manifests": {"ohlcv-v1": "d1"}}) == ["d1"]
    assert _mc_required_digests({"market_condition_manifest": "d0"}) == ["d0"]
    for bad in (["d1"], "d1", 7):
        with pytest.raises(HTTPException) as e:
            _mc_guard({"market_condition_manifests": bad})
        assert e.value.status_code == 400
        assert "must be a {profile: digest} object" in e.value.detail
