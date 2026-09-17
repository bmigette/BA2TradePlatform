"""Cache sync of the market-condition feature store (plan Task 7, design section 4.5).

Pinned here: objects that arrive are verified by SHA256 (a same-size corruption is caught, which
``(rel_path, size)`` structurally cannot see); pruning is snapshot-scoped, so an object a manifest
on the worker still references is never deleted; ``_derived`` stays out of the manifest in both
directions; and a threshold-only rerun transfers only what is genuinely new -- one manifest, no
objects.
"""
from __future__ import annotations

import io
import json
from datetime import date

import numpy as np
import pytest

from app.services import cache_sync

from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_conditions import PROFILES, STATUS_INSUFFICIENT_HISTORY

PROFILE = PROFILES["ohlcv-v1"]
FIELDS = [f.name for f in PROFILE.fields]


def _row(session, reason="young listing"):
    return {"session": session, "values": [None] * len(FIELDS),
            "status": [STATUS_INSUFFICIENT_HISTORY] * len(FIELDS),
            "reasons": [reason] * len(FIELDS), "window_digest": "sha256:" + "0" * 64,
            "raw_shard_ref": "", "raw_row_lo": 0, "raw_row_hi": 0}


def _publish(root, symbols, sessions, extra_raw=True):
    """One object per symbol plus (optionally) one raw shard, and the manifest over them."""
    store = MarketConditionStore(root)
    objects = []
    for symbol in symbols:
        entry, _ = store.write_feature_object(PROFILE, symbol, [_row(s) for s in sessions])
        objects.append(entry)
    raws = []
    if extra_raw:
        days = np.array(sessions, dtype="datetime64[D]")
        ones = np.ones(len(sessions), dtype=float)
        raw, _ = store.write_raw_shard(days, ones, ones, ones, ones, ones)
        raws.append(raw)
    manifest = store.make_manifest(
        PROFILE, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=raws, coverage={s: {"rows": len(sessions)} for s in symbols},
        universe=symbols, sessions=sessions, window_start=sessions[0], window_end=sessions[-1])
    return store, store.write_manifest(manifest)


SESSIONS = [date(2024, 3, 26), date(2024, 3, 27)]


def test_arrival_is_verified_by_hash_and_a_same_size_corruption_is_reported(tmp_path):
    store, digest = _publish(tmp_path, ["AAA", "BBB"], SESSIONS)
    ok = cache_sync.verify_market_conditions(str(tmp_path))
    assert ok["ok"] and ok["manifests"] == 1 and ok["checked"] == 3  # 2 objects + 1 raw shard

    manifest = store.read_manifest(digest)
    victim = store.abspath(manifest["objects"][0]["path"])
    size = victim.stat().st_size
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF
    victim.write_bytes(bytes(data))
    assert victim.stat().st_size == size, "the corruption must keep the size, or it proves nothing"

    bad = cache_sync.verify_market_conditions(str(tmp_path))
    assert not bad["ok"]
    assert bad["corrupt"] == [f"market_conditions/{manifest['objects'][0]['path']}"]
    # And the cheap diff the ordinary push uses cannot see it -- which is why the hash pass exists.
    local = cache_sync.build_manifest(str(tmp_path))
    remote = {"files": [dict(f) for f in local["files"]]}
    assert cache_sync.diff_missing(local["files"], remote) == []


def test_a_missing_object_is_reported_not_ignored(tmp_path):
    store, digest = _publish(tmp_path, ["AAA"], SESSIONS)
    manifest = store.read_manifest(digest)
    store.abspath(manifest["objects"][0]["path"]).unlink()
    out = cache_sync.verify_market_conditions(str(tmp_path))
    assert not out["ok"] and out["missing"] == [f"market_conditions/{manifest['objects'][0]['path']}"]


def test_prune_never_deletes_an_object_a_worker_side_manifest_references(tmp_path):
    """The master's CURRENT manifest is not the only snapshot on the worker: another job, a
    stored backtest or a captured replay pins objects the newest manifest does not list."""
    store, old_digest = _publish(tmp_path, ["AAA"], SESSIONS)
    _store2, new_digest = _publish(tmp_path, ["BBB"], SESSIONS)
    old = store.read_manifest(old_digest)
    old_object = f"market_conditions/{old['objects'][0]['path']}"
    unrelated = tmp_path / "FMPOHLCVProvider" / "AAPL_1d.parquet"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"x" * 10)

    # The master (which only knows the new manifest) would ask for both to go.
    out = cache_sync.prune_paths([old_object, "FMPOHLCVProvider/AAPL_1d.parquet"], str(tmp_path))
    assert out["protected"] == 1 and out["pruned"] == 1
    assert store.abspath(old["objects"][0]["path"]).exists()
    assert not unrelated.exists()
    assert new_digest != old_digest
    assert cache_sync.verify_market_conditions(str(tmp_path))["ok"]


def test_prune_of_a_non_market_condition_list_is_unchanged(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.parquet").write_bytes(b"1")
    out = cache_sync.prune_paths(["a/x.parquet", "a/gone.parquet"], str(tmp_path))
    assert out == {"pruned": 1, "skipped": 0, "failed": 0, "protected": 0}


def test_derived_mappings_are_never_synced_in_either_direction(tmp_path):
    _publish(tmp_path, ["AAA"], SESSIONS)
    derived = tmp_path / "_derived" / "market_conditions" / "mc_ohlcv-v1_abc_v1" / "sig"
    derived.mkdir(parents=True)
    (derived / "values.npy").write_bytes(b"mapped")
    rels = {f["rel_path"] for f in cache_sync.build_manifest(str(tmp_path))["files"]}
    assert not any(r.startswith("_derived/") for r in rels)
    assert any(r.startswith("market_conditions/") for r in rels)


def test_a_threshold_only_rerun_transfers_one_manifest_and_no_objects(tmp_path):
    """Changing only a gene/threshold re-uses every feature object: the new manifest references
    the same content-addressed files, so the diff is the manifest alone (design section 4.6:
    "Reuse feature rows; no raw download or indicator recalculation")."""
    master = tmp_path / "master"
    worker = tmp_path / "worker"
    store, first = _publish(master, ["AAA", "BBB"], SESSIONS)

    # First sync: everything.
    remote = cache_sync.build_manifest(str(worker))
    local = cache_sync.build_manifest(str(master))
    missing = cache_sync.diff_missing(local["files"], remote)
    stream = io.BytesIO(b"".join(cache_sync.iter_tar(missing, str(master))))
    out = cache_sync.extract_tar(stream, str(worker))
    assert out["extracted"] == len(missing) and out["market_conditions"] == len(missing)
    assert cache_sync.verify_market_conditions(str(worker))["ok"]

    # A second manifest over the SAME objects (a wider session window is recorded, the rows are
    # reused verbatim) -- the only new file in the bucket is the manifest itself.
    manifest = store.read_manifest(first)
    wider = dict(manifest)
    wider["window_end"] = date(2024, 3, 29).isoformat()
    wider["created_at"] = "2026-09-16T00:00:00+00:00"
    second = store.write_manifest(wider)
    assert second != first

    local2 = cache_sync.build_manifest(str(master))
    remote2 = cache_sync.build_manifest(str(worker))
    missing2 = cache_sync.diff_missing(local2["files"], remote2)
    assert missing2 == [f"market_conditions/ohlcv-v1/manifests/{second}.json"]
    stream2 = io.BytesIO(b"".join(cache_sync.iter_tar(missing2, str(master))))
    out2 = cache_sync.extract_tar(stream2, str(worker))
    assert out2["extracted"] == 1 and out2["market_conditions"] == 1
    verdict = cache_sync.verify_market_conditions(str(worker))
    assert verdict["ok"] and verdict["manifests"] == 2


def test_an_unreadable_manifest_is_reported_and_left_alone(tmp_path):
    _publish(tmp_path, ["AAA"], SESSIONS)
    path = cache_sync.mc_manifest_files(str(tmp_path))[0]
    path.write_text("{not json", encoding="utf-8")
    out = cache_sync.verify_market_conditions(str(tmp_path))
    assert not out["ok"] and out["errors"] and path.exists()


def test_a_manifest_whose_content_no_longer_hashes_to_its_name_is_corrupt(tmp_path):
    _publish(tmp_path, ["AAA"], SESSIONS)
    path = cache_sync.mc_manifest_files(str(tmp_path))[0]
    body = json.loads(path.read_text(encoding="utf-8"))
    body["timing_policy"] = "tampered"
    path.write_text(json.dumps(body), encoding="utf-8")
    out = cache_sync.verify_market_conditions(str(tmp_path))
    assert not out["ok"] and out["corrupt"] == [
        f"market_conditions/ohlcv-v1/manifests/{path.name}"]


def test_verification_is_scoped_to_the_snapshots_the_push_delivered(tmp_path):
    """Two rules in one: a push re-hashes only the manifests it actually touched (a host holding
    a season of snapshots must not re-hash the whole bucket on every push), and only the digests
    whose OWN objects failed are reported as failed (one stale snapshot must not condemn every
    other digest on the box)."""
    good_store, good = _publish(tmp_path, ["AAA"], SESSIONS)
    bad_store, bad = _publish(tmp_path, ["BBB"], SESSIONS, extra_raw=False)
    bad_manifest = bad_store.read_manifest(bad)
    bad_object = f"market_conditions/{bad_manifest['objects'][0]['path']}"

    data = bytearray(bad_store.abspath(bad_manifest["objects"][0]["path"]).read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad_store.abspath(bad_manifest["objects"][0]["path"]).write_bytes(bytes(data))

    scoped = cache_sync.verify_market_conditions(str(tmp_path), rel_paths=[bad_object])
    assert scoped["checked_digests"] == [bad], "only the delivered snapshot is re-hashed"
    assert scoped["failed_digests"] == [bad] and not scoped["ok"]
    assert scoped["checked"] == len(bad_manifest["objects"]) + len(bad_manifest["raw_objects"])

    # The healthy snapshot is untouched by that push and stays verifiable on its own.
    good_manifest = good_store.read_manifest(good)
    good_object = f"market_conditions/{good_manifest['objects'][0]['path']}"
    healthy = cache_sync.verify_market_conditions(str(tmp_path), rel_paths=[good_object])
    assert healthy["ok"] and healthy["checked_digests"] == [good]

    # A full scan still sees both (the explicit integrity check).
    full = cache_sync.verify_market_conditions(str(tmp_path))
    assert sorted(full["checked_digests"]) == sorted([good, bad])
    assert full["failed_digests"] == [bad]


def test_a_pushed_manifest_is_verified_even_when_no_object_came_with_it(tmp_path):
    """The threshold-only rerun: only the manifest travels, its objects are already there. The
    scope must still include it, or nothing would ever verify the new snapshot."""
    store, digest = _publish(tmp_path, ["AAA"], SESSIONS)
    rel = f"market_conditions/ohlcv-v1/manifests/{digest}.json"
    out = cache_sync.verify_market_conditions(str(tmp_path), rel_paths=[rel])
    assert out["ok"] and out["checked_digests"] == [digest] and out["checked"] > 0


def test_extract_tar_names_the_market_condition_members(tmp_path):
    master = tmp_path / "master"
    worker = tmp_path / "worker"
    _publish(master, ["AAA"], SESSIONS)
    (master / "FMPOHLCVProvider").mkdir(parents=True)
    (master / "FMPOHLCVProvider" / "AAPL_1d.parquet").write_bytes(b"x" * 10)

    local = cache_sync.build_manifest(str(master))
    rels = [f["rel_path"] for f in local["files"]]
    out = cache_sync.extract_tar(io.BytesIO(b"".join(cache_sync.iter_tar(rels, str(master)))),
                                 str(worker))
    assert set(out["market_condition_paths"]) == {r for r in rels if r.startswith("market_conditions/")}
    assert out["market_conditions"] == len(out["market_condition_paths"])
    assert "FMPOHLCVProvider/AAPL_1d.parquet" not in out["market_condition_paths"]


def test_an_unreadable_manifest_the_push_never_touched_cannot_fail_the_pass(tmp_path):
    """THE BUG THIS PINS. The unreadable-manifest branch ran BEFORE the scope filter, so a stale
    or truncated file elsewhere in the bucket set ``ok: False`` while naming no digest -- and the
    worker's ``failed_digests or None`` then revoked EVERY prepared digest on the box. An
    out-of-scope unreadable manifest is reported, and cannot flip ``ok``."""
    store, good = _publish(tmp_path, ["AAA"], SESSIONS)
    _publish(tmp_path, ["BBB"], SESSIONS, extra_raw=False)
    stale = [p for p in cache_sync.mc_manifest_files(str(tmp_path)) if p.stem != good][0]
    stale.write_text("{truncated", encoding="utf-8")

    good_object = f"market_conditions/{store.read_manifest(good)['objects'][0]['path']}"
    scoped = cache_sync.verify_market_conditions(str(tmp_path), rel_paths=[good_object])
    assert scoped["ok"] is True, scoped
    assert scoped["failed_digests"] == [] and scoped["checked_digests"] == [good]
    assert scoped["out_of_scope_errors"] and stale.name in scoped["out_of_scope_errors"][0]
    assert scoped["errors"] == []

    # A full scan still reports it as an error of ITS OWN pass (nothing is hidden).
    full = cache_sync.verify_market_conditions(str(tmp_path))
    assert full["ok"] is False and full["errors"] and full["out_of_scope_errors"] == []


def test_an_unreadable_manifest_that_the_push_delivered_is_an_error(tmp_path):
    _publish(tmp_path, ["AAA"], SESSIONS)
    bad = cache_sync.mc_manifest_files(str(tmp_path))[0]
    bad.write_text("{truncated", encoding="utf-8")
    rel = f"market_conditions/ohlcv-v1/manifests/{bad.name}"
    out = cache_sync.verify_market_conditions(str(tmp_path), rel_paths=[rel])
    assert out["ok"] is False and out["errors"] and out["out_of_scope_errors"] == []
