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
    def __init__(self):
        self.submitted = []

    def submit(self, _fn, *args):
        self.submitted.append(args)
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
    r = client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["verified"] and body["symbols"] == 2 and body["rows"] == 4
    assert body["objects_checked"] == 2

    r = _submit(client, digest)
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

    r = client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert any("corrupt" in e for e in r.json()["errors"])
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    assert _submit(client, digest).status_code == 409
    assert pool.submitted == []


def test_an_unknown_manifest_is_an_unready_answer_not_a_500(worker):
    client, _cache, _pool = worker
    r = client.post("/market-conditions/prepare", headers=H, json={"manifest": "0" * 64})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["errors"]


def test_health_lists_the_prepared_digests(worker):
    client, cache, _pool = worker
    _store, digest = _publish(cache)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == []
    client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]


def test_readiness_survives_a_restart_through_the_host_local_marker(worker, monkeypatch):
    client, cache, _pool = worker
    _store, digest = _publish(cache)
    client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
    # A self-update restart drops the process's memory; the _derived marker is what a fresh
    # process reads so the box does not re-verify its whole feature bucket to run one trial.
    monkeypatch.setattr(ws, "_PREPARED_MC", {})
    monkeypatch.setattr(ws, "_PREPARED_MC_LOADED", False)
    assert client.get("/health", headers=H).json()["market_conditions"]["prepared"] == [digest]
    assert _submit(client, digest).status_code == 200


def test_a_push_that_carries_corrupt_objects_revokes_readiness(worker, tmp_path):
    client, cache, _pool = worker
    store, digest = _publish(cache)
    client.post("/market-conditions/prepare", headers=H, json={"manifest": digest})
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


# ---------------------------------------------------------------------------------------------
# Master side
# ---------------------------------------------------------------------------------------------
def test_the_master_excludes_a_worker_that_cannot_prepare(monkeypatch):
    from app.services import distributed_eval as de

    logged = []
    ev = de.DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t", workers=[],
                                 log=logged.append, market_condition_manifest="d" * 64,
                                 market_condition_profile="ohlcv-v1")
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
    cfg = _build_daily_trial_config(backtest_cfg, {})
    assert cfg["market_condition_profile"] == "ohlcv-v1"
    assert cfg["market_condition_manifest"] == "f" * 64
    assert cfg["_ga_trial"] is True

    plain = _build_daily_trial_config({**backtest_cfg, "market_condition_profile": None,
                                       "market_condition_manifest": None}, {})
    assert plain["market_condition_profile"] == "none"
    assert plain["market_condition_manifest"] is None


def test_the_worker_env_keys_carry_the_profile_and_manifest():
    from app.services.strategy_optimization_handler import _WORKER_ENV_KEYS

    assert "BA2_MARKET_CONDITION_PROFILE" in _WORKER_ENV_KEYS
    assert "BA2_MARKET_CONDITION_MANIFEST" in _WORKER_ENV_KEYS
