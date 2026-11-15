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
    out = avl.maintain_cache(str(tmp_path), keep_sessions=10, reference=date(2025, 1, 14))
    assert out == {"parts_removed": 1, "sessions_pruned": 4, "symbols_removed": 0}
    assert len(list(sdir.glob("*.parquet"))) == 10
    # a symbol whose NEWEST chain is older than the retention window goes entirely (review item 3)
    old_dir = tmp_path / avl.LIVE_CHAIN_DIR / "OLD"
    old_dir.mkdir()
    (old_dir / "2024-01-02.parquet").write_bytes(b"x")
    out = avl.maintain_cache(str(tmp_path), keep_sessions=10, reference=date(2025, 1, 14))
    assert out["symbols_removed"] == 1 and not old_dir.exists() and sdir.exists()


def test_recent_symbols_go_by_the_files_session_date_not_its_age(tmp_path):
    root = tmp_path / avl.LIVE_CHAIN_DIR
    (root / "NEW").mkdir(parents=True)
    (root / "NEW" / "2025-04-28.parquet").write_bytes(b"x")
    (root / "OLD").mkdir()
    (root / "OLD" / "2024-04-29.parquet").write_bytes(b"x")       # touched today, but a year-old session
    assert avl.recent_symbols(str(tmp_path), 5, reference=date(2025, 4, 30)) == ["NEW"]


# --------------------------------------------------------------------------- item 6: ONE acceptance rule
def test_store_and_live_agree_on_every_raw_payload_including_rejected_chains(world):
    """Review item 6 (BT/live identity): the store and live accept a raw AV payload with the SAME
    function (``map_chain``). Payloads with other-dated rows, undated rows, a foreign (adjusted) root,
    priceless rows -- and one whose every row is dropped -- give identical rows in both paths; the
    emptied chain is MISSING in both."""
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader
    from ba2_common.core.market_condition_store import MarketConditionStore
    from ba2_common.core.market_conditions import STATUS_MISSING_SESSION

    root, _ = world
    variants = {}

    def other_date(rows, d):
        rows[3]["date"] = (d - timedelta(days=1)).isoformat()
        return rows

    def undated(rows, d):
        for r in rows[: len(rows) // 10]:
            r["date"] = ""
        return rows

    def adjusted_root(rows, d):
        for r in rows[:7]:
            r["contractID"] = "BBB1" + r["contractID"][3:]
        return rows

    def priceless(rows, d):
        for r in rows[::4]:
            r.update(bid="0", ask="0", last="0", volume="0")
        return rows

    def all_dropped(rows, d):
        for r in rows:
            r["date"] = (d - timedelta(days=1)).isoformat()
        return rows

    days = {date(2025, 3, 10): other_date, date(2025, 3, 11): undated, date(2025, 3, 12): adjusted_root,
            date(2025, 3, 13): priceless, date(2025, 3, 14): all_dropped}
    from .test_market_condition_warmup import _sessions

    # a FRESH symbol (BBB has bars, no AV data yet): every day recorded once, the variants as served
    store = AvOptionStore(str(root / "AlphaVantageOptionsProvider"))
    for d in _sessions(date(2025, 3, 3) - timedelta(days=7), date(2025, 4, 30)):
        rows = _av_rows("BBB", d)
        if d in days:
            rows = variants[d] = days[d](rows, d)
        frame, stats = map_chain("BBB", d, rows)
        store.record_day("BBB", d, frame, stats)
    store.compact("BBB")
    plan = W.plan(IV_PROFILE, ["BBB"], date(2025, 3, 3), date(2025, 4, 30), cache_root=str(root),
                  source=FakeSource(str(root)), option_source="alphavantage")
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(plan.to_json())), fetch_missing=False,
                  concurrency=1, source=None)
    assert rep.ok, rep
    mapped = MappedMarketConditionReader(str(root), rep.manifest_digest, IV_PROFILE)
    live, _ = _live_reader(root, fetch=lambda s, d, api_key, timeout=None: [dict(r) for r in variants[d]])
    for d in days:
        assert dict(live.observe("BBB", d).by_field()) == dict(mapped.observe("BBB", d).by_field()), d
    assert live.observe("BBB", date(2025, 3, 14)).by_field()[FIELD_ATM_IV_30].status == STATUS_MISSING_SESSION
    assert live.observe("BBB", date(2025, 3, 13)).by_field()[FIELD_ATM_IV_30].status == STATUS_VALID
    del MarketConditionStore


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


# --------------------------------------------------------------------------- re-review 2026-11-15
def test_a_stop_request_ends_the_prefetch_promptly(world):
    """Item 1: a budget wait with no free slot, a stop request from another thread -- the prefetch
    returns within a second or two with the rest left for the decision path."""
    import threading

    root, _ = world
    stop = threading.Event()

    class _NeverFree:
        def _try(self):
            return False, 60.0

    budget = avl._StoppableBudget.__new__(avl._StoppableBudget)
    budget._b, budget._stop, budget._sleep = _NeverFree(), stop, time.sleep
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    rep = avl.prefetch_chains(["CCC", "DDD"], SESSIONS[3], cache_root=str(root), api_key=lambda: "K",
                              fetch=lambda *a, **k: [], budget=budget, stop=stop, jitter_s=0)
    assert time.monotonic() - t0 < 2.0
    assert rep["stopped"] is True and rep["remaining"] == ["CCC", "DDD"]


def test_one_bad_symbol_does_not_end_the_prefetch_and_replace_is_retried(world, monkeypatch):
    """Item 4: ValueError from one symbol's data is recorded and the run continues; a transient
    PermissionError on os.replace (another instance reading the file) is retried."""
    root, _ = world
    real_replace = os.replace
    hits = {"n": 0}

    def flaky_replace(a, b):
        hits["n"] += 1
        if hits["n"] <= 2:
            raise PermissionError("sharing violation")
        return real_replace(a, b)

    def fetch(symbol, session, api_key, timeout=None):
        rows = _av_rows(symbol, session)
        if symbol == "BAD":
            rows[0]["contractID"] = "BAD251399C00100000"        # month 99: an impossible OCC expiry
        return rows

    monkeypatch.setattr(avl.os, "replace", flaky_replace)
    rep = avl.prefetch_chains(["AAA", "BAD", "CCC"], SESSIONS[2], cache_root=str(root), api_key=lambda: "K",
                              fetch=fetch, budget=_Grant(), jitter_s=0)
    assert "BAD" in rep["failed"] and rep["failed"]["BAD"].startswith("error: ValueError")
    assert rep["fetched"] == 2 and hits["n"] >= 4


def test_a_row_from_a_stale_bar_file_is_recomputed_when_the_file_changes(world):
    """Item 7: the memo is keyed with the bar file's signature, like the ohlcv-v1 live reader."""
    import pandas as pd

    from .test_market_condition_warmup import _write

    root, mapped = world
    path = root / "FMPOHLCVProvider" / "AAA_1d.parquet"
    full = pd.read_parquet(path).drop(columns=["effective_date"])
    _write(root, "AAA", full[full["Date"] < pd.Timestamp(SESSIONS[3])])           # stale: ends a day early
    live, _ = _live_reader(root)
    stale = live.observe("AAA", SESSIONS[3]).by_field()["underlying_realized_vol_20"]
    assert stale.status != STATUS_VALID
    _write(root, "AAA", full)                                                     # repaired
    fresh = live.observe("AAA", SESSIONS[3]).by_field()
    assert dict(fresh) == dict(mapped.observe("AAA", SESSIONS[3]).by_field())


def test_two_concurrent_passes_keep_their_own_refusals_and_summaries(world, monkeypatch, caplog):
    """Item 8: an entry and an open-positions pass of one expert share the reader; neither wipes the
    other's refusal memo or flushes the other's summary."""
    from ba2_common.core import market_condition_live as live

    root, _ = world
    budget = _Grant(ok=False)
    reader, _ = _live_reader(root, budget=budget)
    a, b = _Pass(), _Pass()
    current = {"t": a}
    monkeypatch.setattr(live, "current_decision", lambda: current["t"])
    reader.observe("AAA", SESSIONS[0])                 # pass A refuses (budget)
    current["t"] = b
    reader.observe("AAA", SESSIONS[1])                 # pass B refuses
    current["t"] = a
    reader.observe("AAA", SESSIONS[0])                 # A's memo survived B: no second budget try
    assert budget.n == 2
    with caplog.at_level(logging.WARNING):
        reader.flush_pass_summary(a)
    msgs = [r.getMessage() for r in caplog.records if "refused this pass" in r.getMessage()]
    assert len(msgs) == 1 and "1 symbol(s)" in msgs[0]
    current["t"] = b
    reader.observe("AAA", SESSIONS[1])                 # B's memo is still there after A's flush
    assert budget.n == 2


def test_the_http_circuit_opens_after_three_failures(world, pass_token):
    """Item 9: a failing Alpha Vantage cannot cost N requests on one pass."""
    from ba2_providers.options.alphavantage import AlphaVantageOptionsError

    root, _ = world
    calls = []

    def failing(symbol, session, api_key, timeout=None):
        calls.append(session)
        raise AlphaVantageOptionsError("HTTPError: 503")

    live, _ = _live_reader(root, fetch=failing)
    for s in SESSIONS:
        live.observe("AAA", s)
    assert len(calls) == 3                              # the 4th symbol-session never reached AV
    reason = live.observe("AAA", SESSIONS[3]).by_field()[FIELD_ATM_IV_30].reason
    assert "3 times in a row" in reason


def test_a_request_has_a_total_deadline(world, pass_token, monkeypatch):
    """Item 9: a trickling or hung response cannot outlive REQUEST_DEADLINE_S."""
    root, _ = world
    monkeypatch.setattr(avl, "REQUEST_DEADLINE_S", 0.3)

    def hanging(symbol, session, api_key, timeout=None):
        time.sleep(2.0)
        return _av_rows(symbol, session)

    slow, _ = _live_reader(root, fetch=hanging)
    t0 = time.monotonic()
    row = slow.observe("AAA", date(2025, 4, 1)).by_field()[FIELD_ATM_IV_30]
    assert time.monotonic() - t0 < 1.5 and "no complete response" in row.reason


def test_the_cache_is_rechecked_after_waiting_for_a_budget_slot(world):
    """Item 9: another instance fetched the chain while this one waited for a slot -> no second call."""
    root, _ = world
    calls = []

    class _OtherInstanceWrites:
        def acquire(self):
            avl.fetch_and_cache(str(root), "AAA", SESSIONS[1], api_key=lambda: "K",
                                fetch=lambda s, d, api_key, timeout=None: _av_rows(s, d), budget=_Grant())
            return True

    def mine(symbol, session, api_key, timeout=None):
        calls.append(session)
        return _av_rows(symbol, session)

    frame, failure, kind = avl.fetch_and_cache(str(root), "AAA", SESSIONS[1], api_key=lambda: "K",
                                               fetch=mine, budget=_OtherInstanceWrites())
    assert frame is not None and not failure and calls == []


def test_capture_records_the_served_digest_and_trims_only_once(world, monkeypatch):
    """Item 11."""
    from ba2_common.core import market_condition_iv_live as ivl
    from ba2_common.core.market_condition_readers import capturing_reader_for

    root, _ = world
    live, _ = _live_reader(root)
    trims = {"n": 0}
    real = ivl._trimmed_chain

    def counting(ev):
        trims["n"] += 1
        return real(ev)

    monkeypatch.setattr(ivl, "_trimmed_chain", counting)

    class _Cap:
        def __init__(self):
            self.observations = []

        def record_observation(self, **kw):
            self.observations.append(kw["payload"])

    cap = _Cap()
    reader, record = capturing_reader_for(live, cap, source_profile="x", timing_policy="y")
    for _ in range(3):
        reader.observe("AAA", SESSIONS[0])
        record("AAA", SESSIONS[0])
    assert trims["n"] == 1 and len(cap.observations) == 1
    p = cap.observations[0]
    assert p["chain_served_digest"].startswith("sha256:") and p["chain_served_digest"] != p["chain_digest"]


def test_a_corrupt_cache_file_replaced_meanwhile_is_not_deleted(world, monkeypatch):
    """Item 12: re-stat before deleting."""
    import pandas as pd

    root, _ = world
    path = avl.chain_path(str(root), "AAA", SESSIONS[0])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"corrupt")

    def read_then_replace(p, **kw):
        with open(p, "wb") as f:                     # another instance replaces it while we read
            f.write(b"a longer replacement written by another instance")
        raise OSError("truncated")

    monkeypatch.setattr(pd, "read_parquet", read_then_replace)
    assert avl.read_cached(str(root), "AAA", SESSIONS[0]) is None
    assert os.path.exists(path)
