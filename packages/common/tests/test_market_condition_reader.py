"""The host-shared MAPPED market-condition reader and ``prepare-host`` (plan Task 7, design 4.3/4.5).

Pinned here: the mapping answers EXACTLY what the parquet rows say (values, statuses, reasons,
calc version) for every (symbol, session); an absent row is None and never a default; a calculator
version that moved raises instead of comparing thresholds against another calculator's numbers;
``observe`` memoises; ``window_for`` serves the retained window of a row that has one and None for
a row that does not; a SECOND host given only the manifest + objects builds its mapping with ZERO
indicator calculations; ``BA2_SHARED_ARRAYS=0`` gives identical values; a same-size corrupt object
fails preparation instead of being mapped; and the descriptor cost stays at five arrays however
many symbols are touched.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
from datetime import date

import numpy as np
import pytest

from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_reader import (
    LAYOUT_VERSION,
    PREPARED_DIRNAME,
    MappedMarketConditionReader,
    _derived_root,
    mapped_key,
    mapping_exists,
    prepare_host,
    prepare_host_job,
    prepared_digests,
    prune_prepared_markers,
    revoke_prepared,
)
from ba2_common.core.market_condition_readers import MarketConditionVersionMismatch
from ba2_common.core.market_condition_source import window_digest
from ba2_common.core.market_condition_store import MarketConditionStore, month_of
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_VALID,
    WINDOW,
    compute_market_conditions,
)

PROFILE = PROFILES["ohlcv-v1"]
FIELDS = [f.name for f in PROFILE.fields]
VALID_SESSION = date(2024, 3, 28)
EMPTY_SESSIONS = (date(2024, 3, 26), date(2024, 3, 27))


def _bars(n, seed):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * 1.01
    l = np.minimum(o, c) * 0.99
    v = rng.integers(1_000_000, 2_000_000, n).astype(float)
    return o, h, l, c, v


def _invalid_row(session, reason):
    return {"session": session, "values": [None] * len(FIELDS),
            "status": [STATUS_INSUFFICIENT_HISTORY] * len(FIELDS),
            "reasons": [reason] * len(FIELDS), "window_digest": "sha256:" + "0" * 64,
            "raw_shard_ref": "", "raw_row_lo": 0, "raw_row_hi": 0}


def _valid_row(store, seed):
    """A real computed row for ``VALID_SESSION`` plus the raw shards its window was taken from."""
    days = np.array(regular_sessions_ending_at(VALID_SESSION, WINDOW), dtype="datetime64[D]")
    o, h, l, c, v = _bars(WINDOW, seed)
    raws = []
    for m in sorted({month_of(d.astype(object)) for d in days}):
        sel = np.array([month_of(d.astype(object)) == m for d in days])
        entry, _ = store.write_raw_shard(days[sel], o[sel], h[sel], l[sel], c[sel], v[sel])
        raws.append(entry)
    obs = compute_market_conditions(o, h, l, c, v).by_field()
    row = {"session": VALID_SESSION, "values": [obs[f].value for f in FIELDS],
           "status": [obs[f].status for f in FIELDS], "reasons": [obs[f].reason for f in FIELDS],
           "window_digest": window_digest(o, h, l, c, v),
           "raw_shard_ref": ";".join(r.sha256 for r in raws), "raw_row_lo": 0, "raw_row_hi": WINDOW}
    return row, raws, (o, h, l, c, v)


def _fabricate(root, symbols=("AAA", "BBB", "CCC"), valid=True):
    """A published store: one object per symbol with one computed row and two negative rows."""
    store = MarketConditionStore(root)
    objects, raws, windows = [], [], {}
    for i, symbol in enumerate(symbols):
        rows = [_invalid_row(d, f"{symbol} young listing") for d in EMPTY_SESSIONS]
        if valid:
            row, shards, arrays = _valid_row(store, seed=7 + i)
            rows.append(row)
            raws.extend(shards)
            windows[symbol] = arrays
        entry, _ = store.write_feature_object(PROFILE, symbol, rows)
        objects.append(entry)
    sessions = list(EMPTY_SESSIONS) + ([VALID_SESSION] if valid else [])
    manifest = store.make_manifest(
        PROFILE, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects={r.sha256: r for r in raws}.values(),
        coverage={s: {"rows": len(sessions)} for s in symbols},
        universe=symbols, sessions=sessions,
        window_start=date(2024, 3, 1), window_end=date(2024, 3, 29))
    digest = store.write_manifest(manifest)
    return store, digest, windows


@pytest.fixture
def fab(tmp_path):
    store, digest, windows = _fabricate(tmp_path / "cache")
    return store, digest, windows


def _reader(store, digest, **kw):
    return MappedMarketConditionReader(store.cache_root, digest, "ohlcv-v1", store=store, **kw)


def test_every_mapped_row_equals_the_parquet_row(fab):
    store, digest, _w = fab
    reader = _reader(store, digest)
    manifest = store.read_manifest(digest)
    seen = 0
    for symbol in reader.symbols():
        for session, want in store.iter_rows(manifest, symbol):
            got = reader.observe(symbol, session)
            assert got is not None, (symbol, session)
            assert sorted(got.by_field()) == sorted(want.by_field())
            for field, obs in want.by_field().items():
                mine = got.by_field()[field]
                assert mine.status == obs.status
                assert mine.reason == obs.reason
                if obs.status == STATUS_VALID:
                    # Bit-exact: the mapping stores the same float64 the calculator produced.
                    assert mine.value == obs.value
                else:
                    assert mine.value is None
            assert got.calc_versions == {f: manifest["calc_version"] for f in FIELDS}
            seen += 1
    assert seen == 9          # 3 symbols x (2 negative + 1 computed)


def test_an_absent_symbol_or_session_is_none_never_a_default(fab):
    store, digest, _w = fab
    reader = _reader(store, digest)
    assert reader.observe("ZZZ", VALID_SESSION) is None
    assert reader.observe("AAA", date(2024, 3, 25)) is None
    assert reader.observe("AAA", date(2030, 1, 2)) is None


def test_a_moved_calculator_version_raises_instead_of_serving(fab, monkeypatch):
    store, digest, _w = fab
    moved = dataclasses.replace(PROFILES["ohlcv-v1"], calc_version="ohlcv-v1/calc-2")
    monkeypatch.setitem(PROFILES, "ohlcv-v1", moved)
    with pytest.raises(MarketConditionVersionMismatch):
        _reader(store, digest)


def test_a_moved_field_list_raises(fab, monkeypatch):
    store, digest, _w = fab
    spec = PROFILES["ohlcv-v1"]
    monkeypatch.setitem(PROFILES, "ohlcv-v1", dataclasses.replace(spec, fields=spec.fields[:2]))
    with pytest.raises(MarketConditionVersionMismatch):
        _reader(store, digest)


def test_observe_memoises_the_row_object(fab):
    store, digest, _w = fab
    reader = _reader(store, digest)
    first = reader.observe("AAA", VALID_SESSION)
    assert reader.observe("AAA", VALID_SESSION) is first
    assert reader.served == 1


def test_window_for_serves_the_exact_retained_window_and_none_for_a_negative_row(fab):
    store, digest, windows = fab
    reader = _reader(store, digest)
    got = reader.window_for("AAA", VALID_SESSION)
    assert got is not None
    for mine, want in zip(got, windows["AAA"]):
        assert np.array_equal(mine, want)
    # A row whose window could not be assembled retains nothing -- evidence of absence is not a
    # window, and serving the partial span as one would be a fabricated input.
    assert reader.window_for("AAA", EMPTY_SESSIONS[0]) is None
    assert reader.window_for("AAA", date(2024, 3, 25)) is None
    result = reader.window_result_for("AAA", VALID_SESSION)
    assert result.ok and result.dates[-1] == VALID_SESSION and len(result.dates) == WINDOW


def _no_compute(monkeypatch):
    """Make ANY indicator calculation a test failure, and count the attempts."""
    calls = []

    def boom(*a, **kw):
        calls.append(a)
        raise AssertionError("a mapped reader must never call the calculator")

    import ba2_common.core.market_conditions as MC
    monkeypatch.setattr(MC, "compute_market_conditions", boom)
    monkeypatch.setitem(MC.COMPUTE_BY_PROFILE, "ohlcv-v1", boom)
    return calls


def test_a_second_host_builds_its_mapping_without_recomputing(tmp_path, monkeypatch):
    store, digest, windows = _fabricate(tmp_path / "master")
    # A "second host": ONLY the portable bucket (manifests + objects + raw shards) is copied.
    # Nothing derived travels, so this host has to build its own mapping -- from the published
    # values, never from the bars.
    second = tmp_path / "worker" / "market_conditions"
    shutil.copytree(store.root, second)
    calls = _no_compute(monkeypatch)

    report = prepare_host(tmp_path / "worker", digest, jobs=2)
    assert report.ok and report.built and report.symbols == 3

    remote = MappedMarketConditionReader(tmp_path / "worker", digest, "ohlcv-v1")
    local = _reader(store, digest)
    for symbol in local.symbols():
        for session in list(EMPTY_SESSIONS) + [VALID_SESSION]:
            a, b = local.observe(symbol, session), remote.observe(symbol, session)
            assert {f: (o.value, o.status, o.reason) for f, o in a.by_field().items()} == \
                   {f: (o.value, o.status, o.reason) for f, o in b.by_field().items()}
    assert calls == []


def test_prepare_host_is_idempotent_and_records_the_digest(tmp_path):
    store, digest, _w = _fabricate(tmp_path / "cache")
    first = prepare_host(store.cache_root, digest)
    second = prepare_host(store.cache_root, digest)
    assert first.ok and first.built
    assert second.ok and not second.built           # opened, not rebuilt
    assert prepared_digests(store.cache_root) == [digest]
    key = mapped_key("ohlcv-v1", digest)
    assert key.endswith(f"_v{LAYOUT_VERSION}") and digest[:16] in key


def test_the_escape_hatch_serves_identical_values_from_private_arrays(tmp_path, monkeypatch):
    store, digest, _w = _fabricate(tmp_path / "cache")
    shared = _reader(store, digest)
    rows = {(s, d): shared.observe(s, d)
            for s in shared.symbols() for d in list(EMPTY_SESSIONS) + [VALID_SESSION]}

    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    calls = _no_compute(monkeypatch)
    private = MappedMarketConditionReader(store.cache_root, digest, "ohlcv-v1")
    for (symbol, session), want in rows.items():
        got = private.observe(symbol, session)
        assert {f: (o.value, o.status, o.reason) for f, o in got.by_field().items()} == \
               {f: (o.value, o.status, o.reason) for f, o in want.by_field().items()}
    assert calls == []


def test_a_same_size_corruption_fails_preparation_and_maps_nothing(tmp_path):
    store, digest, _w = _fabricate(tmp_path / "cache")
    manifest = store.read_manifest(digest)
    target = store.abspath(manifest["objects"][0]["path"])
    size = target.stat().st_size
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF                     # same length, different bytes
    target.write_bytes(bytes(data))
    assert target.stat().st_size == size

    report = prepare_host(store.cache_root, digest)
    assert not report.ok
    assert any("corrupt" in e for e in report.errors), report.errors
    assert prepared_digests(store.cache_root) == []
    # Nothing was published: a mapping is only ever built from objects that passed their hash.
    from ba2_common.core.market_condition_reader import _derived_root
    key_dir = os.path.join(_derived_root(store.cache_root), mapped_key("ohlcv-v1", digest))
    assert not os.path.isdir(key_dir)


def test_fifty_symbols_cost_five_descriptors_and_one_build(tmp_path, monkeypatch):
    symbols = tuple(f"S{i:03d}" for i in range(50))
    store, digest, _w = _fabricate(tmp_path / "cache", symbols=symbols, valid=False)
    loads = []
    real_load = np.load

    def counting_load(path, *a, **kw):
        if kw.get("mmap_mode"):
            loads.append(str(path))
        return real_load(path, *a, **kw)

    monkeypatch.setattr(np, "load", counting_load)
    reader = _reader(store, digest)
    for symbol in symbols:
        assert reader.observe(symbol, EMPTY_SESSIONS[0]) is not None
    assert reader.open_descriptors() == 5
    # One mapped .npy per array, symbol slices within: touching 50 symbols opens nothing more.
    assert len(loads) == 5, loads


def test_a_recycled_worker_reopens_the_mapping_and_answers_identically(tmp_path, monkeypatch):
    store, digest, _w = _fabricate(tmp_path / "cache")
    first = _reader(store, digest)
    answers = {(s, d): first.observe(s, d)
               for s in first.symbols() for d in list(EMPTY_SESSIONS) + [VALID_SESSION]}
    del first
    # A recycled pool child (BT_MAX_TASKS_PER_CHILD) opens the SAME published set: no rebuild, no
    # calculation, same numbers.
    calls = _no_compute(monkeypatch)
    fresh = MappedMarketConditionReader(store.cache_root, digest, "ohlcv-v1")
    for (symbol, session), want in answers.items():
        got = fresh.observe(symbol, session)
        assert {f: (o.value, o.status) for f, o in got.by_field().items()} == \
               {f: (o.value, o.status) for f, o in want.by_field().items()}
    assert calls == []
    assert fresh.open_descriptors() == 5


def test_the_mapping_key_and_meta_carry_the_portable_identity(tmp_path):
    store, digest, _w = _fabricate(tmp_path / "cache")
    reader = _reader(store, digest)
    arrays = reader.arrays()
    meta = json.loads(bytes(arrays["meta_json"]).decode("utf-8"))
    assert meta["manifest"] == digest and meta["profile"] == "ohlcv-v1"
    assert meta["fields"] == FIELDS and meta["layout"] == LAYOUT_VERSION
    assert sorted(meta["symbols"]) == ["AAA", "BBB", "CCC"]
    assert set(arrays) == {"session", "values", "status", "reason_codes", "meta_json"}
    assert arrays["session"].dtype == np.int32 and arrays["status"].dtype == np.int8
    assert arrays["reason_codes"].dtype == np.int16 and arrays["values"].dtype == np.float64


# --------------------------------------------------------------------- readiness markers
def _markers(cache_root):
    d = os.path.join(_derived_root(cache_root), PREPARED_DIRNAME)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def test_revoking_readiness_renames_the_marker_and_the_digest_stops_counting(tmp_path):
    """A revoke that only clears an in-memory set is not a revoke: the marker is what a restarted
    process reads, so it would re-admit the snapshot it just rejected. The file is KEPT under
    ``.revoked`` -- "this host was ready and stopped being ready" is the diagnostic."""
    store, digest, _w = _fabricate(tmp_path / "cache")
    assert prepare_host(store.cache_root, digest).ok
    assert prepared_digests(store.cache_root) == [digest]

    assert revoke_prepared(store.cache_root, [digest]) == [digest]

    assert prepared_digests(store.cache_root) == []
    names = _markers(store.cache_root)
    assert names and all(n.endswith(".revoked") for n in names)
    # Idempotent: a second revoke has nothing left to withdraw.
    assert revoke_prepared(store.cache_root, [digest]) == []


def test_revoking_without_a_digest_list_withdraws_everything(tmp_path):
    store, digest, _w = _fabricate(tmp_path / "cache")
    prepare_host(store.cache_root, digest)
    assert revoke_prepared(store.cache_root) == [digest]
    assert prepared_digests(store.cache_root) == []


def test_a_marker_without_its_mapping_is_not_readiness_and_is_pruned(tmp_path):
    """The sweep can collect a mapping between runs. A host that kept claiming readiness would
    have the master skip preparation, and every worker process would rebuild the mapping under
    its own first trial."""
    import shutil

    store, digest, _w = _fabricate(tmp_path / "cache")
    prepare_host(store.cache_root, digest)
    assert mapping_exists(store.cache_root, "ohlcv-v1", digest)

    shutil.rmtree(os.path.join(_derived_root(store.cache_root), mapped_key("ohlcv-v1", digest)))

    assert not mapping_exists(store.cache_root, "ohlcv-v1", digest)
    assert prepared_digests(store.cache_root) == []          # claimed by nobody any more
    assert prune_prepared_markers(store.cache_root)          # and the marker is collected
    assert _markers(store.cache_root) == []
    assert prune_prepared_markers(store.cache_root) == []    # idempotent


def test_prepare_host_job_is_a_dict_returning_wrapper_that_reports_its_stage(tmp_path):
    """The worker runs preparation on its trial pool, which pickles the callable BY REFERENCE and
    collects a plain dict; the stage it writes into the control block is what turns a minutes-long
    "running" into something an operator can read."""
    store, digest, _w = _fabricate(tmp_path / "cache")
    ctl: dict = {}
    out = prepare_host_job(store.cache_root, digest, None, 2, ctl)
    assert out["ok"] and out["symbols"] == 3 and out["key"] == mapped_key("ohlcv-v1", digest)
    assert ctl["stage"]

    # A digest that is not here is an unready ANSWER, never a raised job.
    missing = prepare_host_job(store.cache_root, "0" * 64, None, 1, None)
    assert missing["ok"] is False and missing["errors"]


def test_memo_size_zero_serves_identical_rows_without_memoising(tmp_path):
    """The wrapped case: a ``WindowMarketConditionReader`` already memoises this key, and two
    memos over one lookup would double the residency and cache nothing extra."""
    store, digest, _w = _fabricate(tmp_path / "cache")
    memoless = _reader(store, digest, memo_size=0)
    memoed = _reader(store, digest)
    for symbol in memoed.symbols():
        for session in list(EMPTY_SESSIONS) + [VALID_SESSION]:
            a, b = memoless.observe(symbol, session), memoed.observe(symbol, session)
            assert {f: (o.value, o.status, o.reason) for f, o in a.by_field().items()} == \
                   {f: (o.value, o.status, o.reason) for f, o in b.by_field().items()}
    assert memoless.memo_len() == 0
    assert memoed.memo_len() == 9
    # Same VALUES, different objects: nothing is retained between calls.
    assert memoless.observe("AAA", VALID_SESSION) is not memoless.observe("AAA", VALID_SESSION)
    assert memoed.observe("AAA", VALID_SESSION) is memoed.observe("AAA", VALID_SESSION)


def test_a_calc_version_that_moves_under_a_memoised_row_still_raises(tmp_path, monkeypatch):
    """The check is on BOTH paths. A version check that fired only on a memo MISS would keep
    serving rows computed under the old calculator, which is the silent corruption this raises
    for."""
    store, digest, _w = _fabricate(tmp_path / "cache")
    reader = _reader(store, digest)
    assert reader.observe("AAA", VALID_SESSION) is not None      # now memoised

    moved = dataclasses.replace(PROFILES["ohlcv-v1"], calc_version="ohlcv-v1/calc-2")
    monkeypatch.setitem(PROFILES, "ohlcv-v1", moved)
    with pytest.raises(MarketConditionVersionMismatch):
        reader.observe("AAA", VALID_SESSION)
