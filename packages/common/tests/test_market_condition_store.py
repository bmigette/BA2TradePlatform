"""The central market-condition feature store (plan Task 6, design sections 4.3 and 4.6).

Pinned here: the parquet schema is the DECLARED one (an all-invalid field stays float64, never
an inferred object/null column); objects are immutable and content-addressed; the manifest
identity ignores ``created_at`` and dict order; ``verify`` re-hashes (a same-size corruption is
caught); ``retained_window`` rebuilds the exact window from retained raw shards; and a manifest
whose objects carry the same (symbol, session) twice is refused.
"""
from __future__ import annotations

import json
import os
from datetime import date

import numpy as np
import pytest

from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_source import window_digest
from ba2_common.core.market_condition_store import (
    ManifestConflictError,
    ManifestError,
    MarketConditionStore,
    feature_schema,
    manifest_identity,
    month_of,
)
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_VALID,
    WINDOW,
    compute_market_conditions,
)

PROFILE = PROFILES["ohlcv-v1"]
FIELDS = [f.name for f in PROFILE.fields]


def _bars(n, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * 1.01
    l = np.minimum(o, c) * 0.99
    v = rng.integers(1_000_000, 2_000_000, n).astype(float)
    return o, h, l, c, v


def _invalid_row(session, reason="young listing"):
    return {"session": session, "values": [None] * len(FIELDS), "status": [STATUS_INSUFFICIENT_HISTORY] * len(FIELDS),
            "reasons": [reason] * len(FIELDS), "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
            "raw_row_lo": 0, "raw_row_hi": 0}


@pytest.fixture
def store(tmp_path):
    return MarketConditionStore(tmp_path)


def _valid_setup(store, session=date(2024, 3, 28), seed=3):
    """One raw shard set + one valid feature row for ``session``; returns (row, raw entries, arrays)."""
    days = np.array(regular_sessions_ending_at(session, WINDOW), dtype="datetime64[D]")
    o, h, l, c, v = _bars(WINDOW, seed)
    raws, lo = [], 0
    months = sorted({month_of(d.astype(object)) for d in days})
    for m in months:
        sel = np.array([month_of(d.astype(object)) == m for d in days])
        entry, _ = store.write_raw_shard(days[sel], o[sel], h[sel], l[sel], c[sel], v[sel])
        raws.append(entry)
    row = compute_market_conditions(o, h, l, c, v)
    obs = row.by_field()
    rec = {"session": session, "values": [obs[f].value for f in FIELDS], "status": [obs[f].status for f in FIELDS],
           "reasons": [obs[f].reason for f in FIELDS], "window_digest": window_digest(o, h, l, c, v),
           "raw_shard_ref": ";".join(r.sha256 for r in raws), "raw_row_lo": 0, "raw_row_hi": WINDOW}
    return rec, raws, (o, h, l, c, v)


def _manifest(store, objects, raws, symbols=("AAA",)):
    return store.make_manifest(PROFILE, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
                               objects=objects, raw_objects=raws, coverage={s: {"rows": 1} for s in symbols},
                               universe=symbols, window_start=date(2024, 3, 1), window_end=date(2024, 3, 29))


def test_schema_is_declared_even_when_every_value_is_invalid(store):
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [_invalid_row(date(2024, 3, d)) for d in (4, 5, 6)]
    entry, reused = store.write_feature_object(PROFILE, "AAA", rows)
    assert not reused
    schema = pq.read_schema(store.abspath(entry.path))
    assert schema.remove_metadata().equals(feature_schema(FIELDS))
    for f in FIELDS:
        assert schema.field(f).type == pa.float64()
        assert schema.field(f"{f}_status").type == pa.int8()
    assert schema.field("session").type == pa.date32()
    t = store.read_table(entry.path)
    assert all(np.isnan(t.column(f).to_numpy()).all() for f in FIELDS)


def test_objects_are_immutable_and_content_addressed(store):
    import hashlib

    rows = [_invalid_row(date(2024, 3, 4))]
    e1, r1 = store.write_feature_object(PROFILE, "AAA", rows)
    path = store.abspath(e1.path)
    mtime = os.stat(path).st_mtime_ns
    e2, r2 = store.write_feature_object(PROFILE, "AAA", rows)
    assert (r1, r2) == (False, True) and e1 == e2
    assert os.stat(path).st_mtime_ns == mtime  # reused, not rewritten
    assert hashlib.sha256(path.read_bytes()).hexdigest() == e1.sha256 == path.stem
    e3, _ = store.write_feature_object(PROFILE, "AAA", [_invalid_row(date(2024, 3, 4), reason="other")])
    assert e3.sha256 != e1.sha256
    assert not list(path.parent.glob("*.part"))
    with pytest.raises(ValueError):
        store.write_feature_object(PROFILE, "AAA", [_invalid_row(date(2024, 3, 4)), _invalid_row(date(2024, 4, 1))])


def test_manifest_identity_ignores_created_at_and_dict_order(store):
    rec, raws, _ = _valid_setup(store)
    obj, _ = store.write_feature_object(PROFILE, "AAA", [rec])
    m1 = _manifest(store, [obj], raws)
    m2 = dict(reversed(list(m1.items())))
    m2["created_at"] = "1999-01-01T00:00:00+00:00"
    m2["coverage"] = {"AAA": {"rows": 1}}
    assert manifest_identity(m1) == manifest_identity(m2)
    d1 = store.write_manifest(m1)
    d2 = store.write_manifest(m2)
    assert d1 == d2 == manifest_identity(m1)
    loaded = store.read_manifest(d1)
    assert loaded["created_at"] == m1["created_at"]  # the first publication is kept
    assert store.list_manifests("ohlcv-v1") == [d1]
    m3 = dict(m1, window_end="2024-03-30")
    assert manifest_identity(m3) != d1
    # A tampered manifest file no longer matches its name.
    p = store.manifest_path("ohlcv-v1", d1)
    data = json.loads(p.read_text())
    data["timing_policy"] = "x"
    p.write_text(json.dumps(data))
    with pytest.raises(ManifestError):
        store.read_manifest(d1)


def test_verify_catches_same_size_corruption_and_missing(store):
    rec, raws, _ = _valid_setup(store)
    obj, _ = store.write_feature_object(PROFILE, "AAA", [rec])
    m = _manifest(store, [obj], raws)
    digest = store.write_manifest(m)
    m = store.read_manifest(digest)
    rep = store.verify(m, digest)
    assert rep.ok and rep.objects_checked == 1 and rep.raw_checked == len(raws)

    p = store.abspath(obj.path)
    size = p.stat().st_size
    data = bytearray(p.read_bytes())
    data[len(data) // 2] ^= 0xFF
    p.write_bytes(bytes(data))
    assert p.stat().st_size == size
    rep = store.verify(m, digest)
    assert not rep.ok and rep.corrupt == [obj.path] and not rep.missing

    os.remove(store.abspath(raws[0].path))
    rep = store.verify(m, digest)
    assert raws[0].path in rep.missing


def test_retained_window_round_trips_the_digest(store):
    rec, raws, arrays = _valid_setup(store)
    obj, _ = store.write_feature_object(PROFILE, "AAA", [rec])
    digest = store.write_manifest(_manifest(store, [obj], raws))
    got = MarketConditionStore(store.cache_root).retained_window(rec["window_digest"])
    for a, b in zip(got, arrays):
        assert a.dtype == np.float64 and np.array_equal(a, b)
    assert window_digest(*got) == rec["window_digest"]
    with pytest.raises(KeyError):
        store.retained_window("sha256:" + "f" * 64, store.read_manifest(digest))


def test_iter_rows_rebuilds_feature_rows(store):
    rec, raws, arrays = _valid_setup(store)
    bad = _invalid_row(date(2024, 3, 27))
    obj, _ = store.write_feature_object(PROFILE, "AAA", [rec, bad])
    m = store.read_manifest(store.write_manifest(_manifest(store, [obj], raws)))
    rows = list(store.iter_rows(m, "AAA"))
    assert [s for s, _ in rows] == [date(2024, 3, 27), date(2024, 3, 28)]
    expected = compute_market_conditions(*arrays).by_field()
    assert dict(rows[1][1].by_field()) == dict(expected)
    assert rows[0][1].by_field()[FIELDS[0]].status == STATUS_INSUFFICIENT_HISTORY
    assert rows[0][1].calc_versions[FIELDS[0]] == PROFILE.calc_version
    assert expected[FIELDS[0]].status == STATUS_VALID
    assert list(store.iter_rows(m, "ZZZ")) == []


def test_duplicate_conflicting_rows_across_objects_rejected(store):
    rec, raws, _ = _valid_setup(store)
    obj1, _ = store.write_feature_object(PROFILE, "AAA", [rec])
    conflicting = dict(rec, values=[1.0] + list(rec["values"][1:]))
    obj2, _ = store.write_feature_object(PROFILE, "AAA", [conflicting])
    assert obj1.sha256 != obj2.sha256
    with pytest.raises(ManifestConflictError, match="conflicting"):
        store.write_manifest(_manifest(store, [obj1, obj2], raws))
    assert store.list_manifests("ohlcv-v1") == []


def test_manifest_refuses_missing_raw_reference(store):
    rec, raws, _ = _valid_setup(store)
    obj, _ = store.write_feature_object(PROFILE, "AAA", [rec])
    with pytest.raises(ManifestError, match="raw shards not in raw_objects"):
        store.write_manifest(_manifest(store, [obj], raws[1:]))
