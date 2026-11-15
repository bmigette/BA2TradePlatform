"""Hardening of the live iv-v1 reader (review 2026-11-15, items 1-6, 8 and the prefetch)."""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import zlib
from datetime import date, timedelta

import pytest

from ba2_common.core.market_conditions import (
    FIELD_ATM_IV_30, FIELD_IV_RV_RATIO, IV_PROFILE, STATUS_NO_CONTEXT, STATUS_VALID, FeatureRow, Observation,
)
from ba2_providers.market_conditions import av_live_chain as avl
from ba2_providers.market_conditions import warmup as W
from ba2_providers.options.alphavantage import AvOptionStore, map_chain

from .test_av_live_iv_certification import SESSIONS, _av_rows, _Grant, _live_reader, world  # noqa: F401
from .test_market_condition_warmup import FakeSource


class _Pass:
    """A stand-in decision pass for ``market_condition_live.current_decision``."""


@pytest.fixture
def pass_token(monkeypatch):
    from ba2_common.core import market_condition_live as live

    holder = {"token": _Pass()}
    monkeypatch.setattr(live, "current_decision", lambda: holder["token"])
    return holder


class _OhlcvStub:
    profile = "ohlcv-v1"
    calc_version = "ohlcv-v1/calc-2"

    def __init__(self):
        self.reads = 0

    def observe(self, symbol, session):
        self.reads += 1
        values = {f: Observation(1.0, STATUS_VALID) for f in
                  ("underlying_trend_slope_50_atr14", "underlying_adx_14", "underlying_realized_vol_ratio_5_20")}
        return FeatureRow(values=values, calc_versions={f: self.calc_version for f in values})


def _failing_then_ok(fail_times):
    state = {"n": 0}

    def fetch(symbol, session, api_key, timeout=None):
        state["n"] += 1
        if state["n"] <= fail_times:
            return []                            # chain not published yet
        return _av_rows(symbol, session)
    return fetch, state


# --------------------------------------------------------------------------- items 1 and 3: the composite
def test_an_ohlcv_leaf_never_triggers_a_chain_fetch(world):
    from ba2_common.core.market_condition_readers import CompositeMarketConditionReader

    root, _ = world
    live, calls = _live_reader(root)
    stub = _OhlcvStub()
    comp = CompositeMarketConditionReader([stub, live])
    row = comp.observe("AAA", SESSIONS[0])
    assert row.by_field().get("underlying_adx_14").value == 1.0
    assert sorted(row.by_field())[:1]                       # iterating names fetches nothing either
    assert calls == [] and live.calls == 0
    assert row.by_field().get(FIELD_ATM_IV_30).status == STATUS_VALID
    assert len(calls) == 1


def test_a_transient_failure_is_retried_on_the_next_pass_through_the_composite(world, pass_token):
    from ba2_common.core.market_condition_readers import CompositeMarketConditionReader

    root, mapped = world
    fetch, state = _failing_then_ok(1)
    live, _ = _live_reader(root, fetch=fetch)
    comp = CompositeMarketConditionReader([_OhlcvStub(), live])
    first = comp.observe("AAA", SESSIONS[1]).by_field()[FIELD_IV_RV_RATIO]
    assert first.status == STATUS_NO_CONTEXT
    again = comp.observe("AAA", SESSIONS[1]).by_field()[FIELD_IV_RV_RATIO]   # same pass: no new call
    assert again.status == STATUS_NO_CONTEXT and state["n"] == 1
    pass_token["token"] = _Pass()                                             # the next pass
    later = comp.observe("AAA", SESSIONS[1]).by_field()[FIELD_IV_RV_RATIO]
    assert later == mapped.observe("AAA", SESSIONS[1]).by_field()[FIELD_IV_RV_RATIO]
    assert state["n"] == 2


def test_a_composite_without_a_chain_reader_is_unchanged():
    from ba2_common.core.market_condition_readers import CompositeMarketConditionReader

    stub = _OhlcvStub()
    comp = CompositeMarketConditionReader([stub])
    row = comp.observe("AAA", SESSIONS[0])
    assert type(row) is FeatureRow and comp.observe("AAA", SESSIONS[0]) is row and stub.reads == 1
    assert list(comp._memo) == [("AAA", SESSIONS[0])]


# --------------------------------------------------------------------------- item 2: no stall
def test_the_budget_latches_after_the_first_refusal():
    clock = {"t": 0.0}
    tries = {"n": 0}

    class _Never:
        def _try(self):
            tries["n"] += 1
            return False, 30.0

    b = avl._LatchedBudget.__new__(avl._LatchedBudget)
    b._b, b._wait_s, b._until = _Never(), 5.0, 0.0
    b._clock = lambda: clock["t"]
    b._sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
    assert b.acquire() is False and clock["t"] <= 5.0
    n = tries["n"]
    assert b.acquire() is False and tries["n"] == n           # latched: refused without trying
    clock["t"] += 31.0
    b.acquire()
    assert tries["n"] > n                                       # the window passed: tries again


def test_the_second_leaf_of_a_refused_symbol_is_free_within_the_pass(world, pass_token):
    root, _ = world
    budget = _Grant(ok=False)
    live, calls = _live_reader(root, budget=budget)
    for _ in range(3):
        assert live.observe("AAA", SESSIONS[0]).by_field()[FIELD_ATM_IV_30].status == STATUS_NO_CONTEXT
    assert budget.n == 1 and calls == []


def test_the_default_http_timeout_is_short():
    assert avl.HTTP_TIMEOUT_S <= 15 and avl.BUDGET_WAIT_S <= 10


# --------------------------------------------------------------------------- item 5: one summary per reason
def test_refusals_are_summarised_once_per_reason_per_pass(world, pass_token, caplog):
    root, _ = world
    live, _ = _live_reader(root, key=None)
    with caplog.at_level(logging.WARNING):
        for s in SESSIONS:
            live.observe("AAA", s)
        assert [r for r in caplog.records if "iv-v1 live" in r.getMessage()] == []
        live.flush_pass_summary()
    lines = [r.getMessage() for r in caplog.records if "iv-v1 live" in r.getMessage()]
    assert len(lines) == 1 and "no_key" in lines[0] and "1 symbol(s)" in lines[0]
    live.flush_pass_summary()                                   # nothing new: nothing logged
    assert len([r for r in caplog.records if "iv-v1 live" in r.getMessage()]) == 1


# --------------------------------------------------------------------------- item 4: cache hygiene
def test_the_cache_holds_only_computation_columns_and_a_corrupt_file_is_refetched(world):
    import pyarrow.parquet as pq

    root, mapped = world
    live, calls = _live_reader(root)
    live.observe("AAA", SESSIONS[0])
    path = avl.chain_path(str(root), "AAA", SESSIONS[0])
    assert tuple(pq.read_schema(path).names) == avl._COLUMNS
    with open(path, "wb") as f:
        f.write(b"not a parquet file")
    again, calls2 = _live_reader(root)
    assert dict(again.observe("AAA", SESSIONS[0]).by_field()) == dict(mapped.observe("AAA", SESSIONS[0]).by_field())
    assert len(calls2) == 1 and os.path.exists(path)


def test_maintenance_prunes_old_sessions_and_stale_parts(tmp_path):
    sdir = tmp_path / avl.LIVE_CHAIN_DIR / "AAA"
    sdir.mkdir(parents=True)
    for i in range(14):
        (sdir / f"{date(2025, 1, 1) + timedelta(days=i):%Y-%m-%d}.parquet").write_bytes(b"x")
    part = sdir / "2025-01-20.parquet.1.2.part"
    part.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(part, (old, old))
    out = avl.maintain_cache(str(tmp_path), keep_sessions=10)
    assert out == {"parts_removed": 1, "sessions_pruned": 4}
    assert len(list(sdir.glob("*.parquet"))) == 10


# --------------------------------------------------------------------------- item 6b: the live date contract
def test_a_live_chain_with_another_date_or_many_undated_rows_is_refused(world):
    root, _ = world

    def other_date(symbol, session, api_key, timeout=None):
        rows = _av_rows(symbol, session)
        rows[3]["date"] = (session - timedelta(days=1)).isoformat()
        return rows

    def undated(symbol, session, api_key, timeout=None):
        rows = _av_rows(symbol, session)
        for r in rows[: len(rows) // 10]:
            r["date"] = ""
        return rows

    for fetch, msg in ((other_date, "rows dated"), (undated, "carry no date")):
        live, _ = _live_reader(root, fetch=fetch)
        row = live.observe("AAA", SESSIONS[2]).by_field()
        assert row[FIELD_ATM_IV_30].status == STATUS_NO_CONTEXT and msg in row[FIELD_ATM_IV_30].reason
    assert avl.check_live_dates([{"date": SESSIONS[0].isoformat()}] * 200 + [{"date": ""}], SESSIONS[0]) is None


# --------------------------------------------------------------------------- item 8: capture size
def test_the_captured_chain_is_compressed_and_trimmed_and_still_replays_exactly(world):
    from ba2_common.core.market_condition_iv_live import ReplayChainReader, chain_bytes
    from ba2_common.core.market_condition_readers import capturing_reader_for

    root, _ = world
    live, _ = _live_reader(root)

    class _Cap:
        observations = []

        def record_observation(self, **kw):
            self.observations.append(kw["payload"])

        def note_failure(self, what, exc):
            raise AssertionError(what)

    cap = _Cap()
    reader, _rec = capturing_reader_for(live, cap, source_profile="x", timing_policy="y")
    row = reader.observe("AAA", SESSIONS[0])
    p = cap.observations[0]
    full = chain_bytes(live.observe_evidence("AAA", SESSIONS[0]).chain)
    stored = base64.b64decode(p["chain_b64"])
    assert len(stored) < 0.4 * len(full)
    assert len(zlib.decompress(stored)) < len(full)               # trimmed before compression
    replay = ReplayChainReader.from_payloads([json.loads(json.dumps(p))], profile=IV_PROFILE,
                                             option_source="alphavantage")
    assert dict(replay.observe("AAA", SESSIONS[0]).by_field()) == dict(row.by_field())


def test_an_uncompressed_capture_still_replays(world):
    """Payloads written without ``chain_encoding`` (plain base64) remain readable."""
    from ba2_common.core.market_condition_iv_live import ReplayChainReader
    from ba2_common.core.market_condition_readers import capturing_reader_for

    root, _ = world
    live, _ = _live_reader(root)

    class _Cap:
        observations = []

        def record_observation(self, **kw):
            self.observations.append(kw["payload"])

    cap = _Cap()
    reader, _rec = capturing_reader_for(live, cap, source_profile="x", timing_policy="y")
    row = reader.observe("AAA", SESSIONS[1])
    p = dict(cap.observations[0])
    p["chain_b64"] = base64.b64encode(zlib.decompress(base64.b64decode(p["chain_b64"]))).decode("ascii")
    p.pop("chain_encoding")
    replay = ReplayChainReader.from_payloads([p], profile=IV_PROFILE, option_source="alphavantage")
    assert dict(replay.observe("AAA", SESSIONS[1]).by_field()) == dict(row.by_field())


# --------------------------------------------------------------------------- the prefetch
def test_the_prefetch_fetches_resumes_and_leaves_the_remainder_on_demand(world):
    root, mapped = world
    calls = []

    def fetch(symbol, session, api_key, timeout=None):
        calls.append(symbol)
        return _av_rows("AAA", session) if symbol == "AAA" else []

    rep = avl.prefetch_chains(["AAA", "BBB"], SESSIONS[3], cache_root=str(root), api_key=lambda: "K",
                              fetch=fetch, budget=_Grant())
    assert rep["fetched"] == 1 and "BBB" in rep["failed"] and rep["failed"]["BBB"].startswith("not_published")
    rep2 = avl.prefetch_chains(["AAA"], SESSIONS[3], cache_root=str(root), api_key=lambda: "K",
                               fetch=fetch, budget=_Grant())
    assert rep2["cached"] == 1 and rep2["fetched"] == 0 and calls == ["AAA", "BBB"]
    live, live_calls = _live_reader(root)                    # the decision pass finds it cached
    assert dict(live.observe("AAA", SESSIONS[3]).by_field()) == dict(mapped.observe("AAA", SESSIONS[3]).by_field())
    assert live_calls == []
    late = avl.prefetch_chains(["CCC", "DDD"], SESSIONS[3], cache_root=str(root), api_key=lambda: "K",
                               fetch=fetch, budget=_Grant(), deadline=time.monotonic() - 1)
    assert late["remaining"] == ["CCC", "DDD"] and late["fetched"] == 0
    nokey = avl.prefetch_chains(["CCC", "DDD"], SESSIONS[3], cache_root=str(root), api_key=lambda: None,
                                fetch=fetch, budget=_Grant())
    assert list(nokey["failed"]) == ["CCC"] and nokey["remaining"] == ["DDD"]


# --------------------------------------------------------------------------- item 6a: mid-download == compacted
def test_a_store_built_mid_download_equals_one_built_after_compaction(world):
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader
    from ba2_common.core.market_condition_store import MarketConditionStore

    root, _ = world
    store = AvOptionStore(str(root / "AlphaVantageOptionsProvider"))
    day = SESSIONS[2]
    rows = _av_rows("AAA", day)
    # a RE-FETCH of that day: fewer contracts (every 5th dropped) and moved quotes on the rest
    refetched = [dict(r, bid=f"{float(r['bid']) + 0.05:.2f}", ask=f"{float(r['ask']) + 0.05:.2f}")
                 for i, r in enumerate(rows) if i % 5]
    frame, stats = map_chain("AAA", day, refetched)
    store.record_day("AAA", day, frame, stats)                 # staged, NOT compacted

    def build():
        plan = W.plan(IV_PROFILE, ["AAA"], date(2025, 3, 3), date(2025, 4, 30), cache_root=str(root),
                      source=FakeSource(str(root)), option_source="alphavantage")
        rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(plan.to_json())), fetch_missing=False,
                      concurrency=1, source=None)
        assert rep.ok, rep
        m = MarketConditionStore(str(root)).read_manifest(rep.manifest_digest)
        return rep.manifest_digest, dict(MarketConditionStore(str(root)).iter_rows(m, "AAA"))

    mid_digest, mid = build()
    assert store.compact("AAA") > 0 and store.staged_days("AAA") == []
    after_digest, after = build()
    assert mid.keys() == after.keys()
    assert all(dict(mid[s].by_field()) == dict(after[s].by_field()) for s in mid)
    assert mid_digest == after_digest


def test_the_build_refuses_a_gap_fill_policy_for_iv(world):
    root, _ = world
    with pytest.raises(W.WarmupConfigError, match="gap-fill"):
        W.plan(IV_PROFILE, ["AAA"], date(2025, 3, 3), date(2025, 4, 30), cache_root=str(root),
               source=FakeSource(str(root)), option_source="alphavantage", gap_fill="previous")
