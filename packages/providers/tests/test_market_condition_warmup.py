"""Acceptance tests of the market-condition warmup (design sections 4.4, 4.6 and 8.10).

A throwaway cache root holds FMP-layout daily parquet files (``FMPOHLCVProvider/<SYM>_1d.parquet``)
for a three-symbol universe over ~375 sessions, plus AAPL/NVDA files around their certification
splits so the source preflight passes. A fake provider counts every call and byte; the warmup
must reach it only when coverage is really missing.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_conditions import STATUS_MISSING_SESSION, STATUS_VALID
from ba2_common.core.split_basis import CalendarSplit, write_full_fetch_marker
from ba2_providers.market_conditions import warmup as W

UNIVERSE = ("AAA", "BBB", "CCC")
START, END = date(2024, 8, 1), date(2025, 6, 27)
PROFILE = "ohlcv-v1"


def _sessions(a, b):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


def _frame(sessions, seed, base=100.0):
    rng = np.random.default_rng(seed)
    n = len(sessions)
    c = base * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
    v = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"Date": pd.to_datetime(sessions), "Open": o, "High": h, "Low": l, "Close": c, "Volume": v})


def _path(root, sym):
    return Path(root) / "FMPOHLCVProvider" / f"{sym}_1d.parquet"


def _write(root, sym, df):
    p = _path(root, sym)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["effective_date"] = out["Date"]
    tmp = str(p) + ".tmp"
    out.to_parquet(tmp, index=False)
    os.replace(tmp, p)


def _read(root, sym):
    return pd.read_parquet(_path(root, sym)).drop(columns=["effective_date"])


def _unadjust(df, split_day, factor):
    out = df.copy()
    pre = out["Date"] < pd.Timestamp(split_day)
    for col in ("Open", "High", "Low", "Close"):
        out.loc[pre, col] = out.loc[pre, col] * factor
    return out


TRUTH = {sym: _frame(_sessions(date(2024, 1, 2), date(2025, 6, 30)), seed=i + 11) for i, sym in enumerate(UNIVERSE)}
CERT = {"AAPL": _frame(_sessions(date(2020, 6, 1), date(2020, 10, 30)), seed=1, base=120.0),
        "NVDA": _frame(_sessions(date(2024, 4, 1), date(2024, 8, 30)), seed=2, base=110.0)}


class FakeSource:
    """A provider stand-in: truth frames it 'downloads', a split calendar it caches per symbol
    (the real source disk-caches it), and call/byte counters."""

    def __init__(self, root, truth=None, splits=None):
        self.root = root
        self.truth = truth if truth is not None else TRUTH
        self.splits = splits or {}
        self.calls = 0
        self.bytes = 0
        self.fetches = []
        self.full_refetches = []
        self._split_cache = {}
        self._lock = threading.Lock()

    def split_calendar(self, symbol):
        with self._lock:
            if symbol not in self._split_cache:
                self.calls += 1
                self.bytes += 64
                self._split_cache[symbol] = list(self.splits.get(symbol, []))
            return list(self._split_cache[symbol])

    def fetch_daily(self, symbol, start, end):
        with self._lock:
            self.calls += 1
            self.fetches.append((symbol, start, end))
        truth = self.truth[symbol]
        p = _path(self.root, symbol)
        have = _read(self.root, symbol) if p.exists() else truth.iloc[:0]
        add = truth[(truth["Date"] > (have["Date"].max() if len(have) else pd.Timestamp(0)))]
        merged = pd.concat([have, add]).drop_duplicates("Date").sort_values("Date").reset_index(drop=True)
        _write(self.root, symbol, merged)
        with self._lock:
            self.bytes += len(add) * 48

    def force_full_refetch(self, symbol):
        with self._lock:
            self.calls += 1
            self.full_refetches.append(symbol)
        df = self.truth[symbol]
        _write(self.root, symbol, df)
        write_full_fetch_marker(str(_path(self.root, symbol)), first_bar=df["Date"].iloc[0].date(),
                                last_bar=df["Date"].iloc[-1].date(), rows=len(df))
        with self._lock:
            self.bytes += len(df) * 48


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "cache"
    for sym, df in TRUTH.items():
        _write(r, sym, df)
    for sym, df in CERT.items():
        _write(r, sym, df)
    return str(r)


@pytest.fixture(autouse=True)
def fast_claims(monkeypatch):
    monkeypatch.setattr(W, "CLAIM_POLL_S", 0.01)
    monkeypatch.setenv("BA2_MC_MAX_HOST_BUILDERS", "4")


def _warm(root, source, *, start=START, end=END, fetch_missing=False, universe=UNIVERSE, concurrency=2):
    p = W.plan(PROFILE, universe, start, end, cache_root=root, source=source)
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=fetch_missing,
                  concurrency=concurrency, source=source)
    return p, rep


def test_cold_then_identical_warmup_is_free_and_same_manifest(root):
    src = FakeSource(root)
    p1, r1 = _warm(root, src)
    assert r1.ok and r1.exit_code == 0, r1
    n = p1.row_sessions * len(UNIVERSE)
    assert r1.counters["rows_computed"] == n and r1.counters["rows_reused"] == 0
    assert r1.counters["provider_calls"] == 0 and src.fetches == []
    assert W.verify(r1.manifest_digest, root).ok

    calls_before = src.calls
    p2, r2 = _warm(root, src)
    assert src.calls == calls_before  # plan + build: zero provider requests
    assert p2.provider_calls == 0 and r2.counters["provider_calls"] == 0
    assert r2.counters["rows_computed"] == 0 and r2.counters["rows_reused"] == n
    assert r2.counters["objects_written"] == 0 and r2.counters["objects_reused"] == r1.counters["objects_written"]
    assert r2.manifest_digest == r1.manifest_digest
    assert p2.summary()["rows_reusable"] == n

    # Every row's values are what the window reader computes independently.
    from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
    store = MarketConditionStore(root)
    m = store.read_manifest(r1.manifest_digest)
    reader = FMPCacheMarketConditionReader(PROFILE, root)
    rows = list(store.iter_rows(m, "BBB"))
    assert len(rows) == p1.row_sessions
    for session, row in rows[::37]:
        assert dict(row.by_field()) == dict(reader.observe("BBB", session).by_field())
    assert all(o.status == STATUS_VALID for _s, r in rows for o in r.by_field().values())


def test_the_rows_serve_both_the_live_and_the_backtest_rule_over_the_window(root):
    """BT/live parity (plan 2026-09-22 A3). A live decision on session S reads ``prior(S)``; a
    backtest bar D (the live decision labelled N(D)) reads D itself. One snapshot for
    ``[START, END]`` must serve both, so the rows are ``[prior(first decision), last decision]``
    -- one more than the decision count. Before the fix the rows stopped at ``prior(last)``, so
    a backtest's last bar read ``missing_session``.

    ``window_start``/``window_end`` stay the dates the snapshot was built FOR (the reader's
    window check compares a run's bars against them); the rows are what coverage records."""
    from ba2_common.core.market_calendar import (
        backtest_decision_label, decision_data_session, live_decision_label,
        prior_regular_session,
    )
    from datetime import datetime as _dt

    src = FakeSource(root)
    p, rep = _warm(root, src)
    assert rep.ok, rep
    decisions = _sessions(START, END)
    assert p.decision_sessions == len(decisions)
    assert p.row_sessions == len(decisions) + 1
    assert p.first_row_session == prior_regular_session(decisions[0]).isoformat()
    assert p.last_row_session == decisions[-1].isoformat()

    m = MarketConditionStore(root).read_manifest(rep.manifest_digest)
    assert (m["window_start"], m["window_end"]) == (START.isoformat(), END.isoformat())
    rows = [s for s, _r in MarketConditionStore(root).iter_rows(m, "AAA")]
    assert rows == _sessions(prior_regular_session(decisions[0]), decisions[-1])
    assert m["coverage"]["AAA"]["first_session"] == rows[0].isoformat()
    assert m["coverage"]["AAA"]["last_session"] == rows[-1].isoformat()
    held = set(rows)
    for day in decisions:
        live_at = _dt(day.year, day.month, day.day, 9, 35, tzinfo=NY_TZ)
        assert decision_data_session(live_decision_label(live_at)) in held      # live on S
        assert decision_data_session(backtest_decision_label(day)) in held      # BT bar D


def test_an_end_session_that_has_not_closed_yet_is_refused(root, monkeypatch):
    """The last row IS the end session, so its bar must exist. END 2025-06-27 closes 20:00 UTC:
    planning it at 19:59 UTC that day is refused loudly; at 20:01 it is a completed session."""
    from datetime import datetime as _dt, timezone as _tz

    src = FakeSource(root)
    monkeypatch.setattr(W, "_now_utc", lambda: _dt(2025, 6, 27, 19, 59, tzinfo=_tz.utc))
    with pytest.raises(W.WarmupConfigError, match="COMPLETED session"):
        W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)
    # A window ending on a future session is the same refusal.
    with pytest.raises(W.WarmupConfigError, match="has not happened yet"):
        W.plan(PROFILE, UNIVERSE, START, date(2025, 6, 30), cache_root=root, source=src)

    monkeypatch.setattr(W, "_now_utc", lambda: _dt(2025, 6, 28, 9, 0, tzinfo=_tz.utc))
    p = W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)
    assert p.last_row_session == END.isoformat()


def test_a_version_1_plan_file_is_refused():
    """Version 1 plans carried rows ``prior(first)..prior(last)``; built today they would come
    out one row short at the end, so ``from_dict`` refuses them instead of guessing."""
    with pytest.raises(W.WarmupConfigError, match="plan version 1"):
        W.MarketConditionWarmPlan.from_dict({"plan_version": 1, "symbols": []})


def test_extend_end_by_one_session_builds_one_row_per_symbol(root):
    src = FakeSource(root)
    _p1, r1 = _warm(root, src)
    _p2, r2 = _warm(root, src, end=date(2025, 6, 30))
    assert r2.ok
    assert r2.counters["rows_computed"] == len(UNIVERSE)
    assert r2.counters["objects_reused"] == r1.counters["objects_written"]
    assert r2.counters["objects_written"] == len(UNIVERSE)
    assert r2.manifest_digest != r1.manifest_digest
    old = MarketConditionStore(root).read_manifest(r1.manifest_digest)
    new = MarketConditionStore(root).read_manifest(r2.manifest_digest)
    assert {o["sha256"] for o in old["objects"]} <= {o["sha256"] for o in new["objects"]}


def test_changed_historical_bar_rebuilds_at_most_128_rows_and_keeps_old_manifest(root):
    src = FakeSource(root)
    store = MarketConditionStore(root)
    _p1, r1 = _warm(root, src)
    old_manifest = store.read_manifest(r1.manifest_digest)
    old_rows = [(s, dict(r.by_field())) for s, r in store.iter_rows(old_manifest, "AAA")]

    df = _read(root, "AAA")
    i = int(np.flatnonzero(df["Date"] == pd.Timestamp("2025-01-15"))[0])
    df.loc[i, "Close"] *= 1.03
    df.loc[i, "High"] = max(df.loc[i, "High"], df.loc[i, "Close"])
    _write(root, "AAA", df)

    _p2, r2 = _warm(root, src)
    assert r2.ok and r2.manifest_digest != r1.manifest_digest
    assert 0 < r2.counters["rows_computed"] <= 128
    changed_day = date(2025, 1, 15)
    new_manifest = store.read_manifest(r2.manifest_digest)
    new_rows = dict((s, dict(r.by_field())) for s, r in store.iter_rows(new_manifest, "AAA"))
    for s, obs in old_rows:
        if s < changed_day:
            assert new_rows[s] == obs
    # Other symbols untouched; the old manifest is still complete, verifiable and unchanged.
    assert r2.counters["rows_computed"] == sum(1 for s, _ in old_rows if s >= changed_day and
                                               len(_sessions(changed_day, s)) <= 128)
    assert store.verify(store.read_manifest(r1.manifest_digest), r1.manifest_digest).ok
    assert [(s, dict(r.by_field())) for s, r in store.iter_rows(store.read_manifest(r1.manifest_digest), "AAA")] == old_rows


def test_interrupted_publication_resumes_from_hash_verified_objects_only(root, tmp_path, monkeypatch):
    clean_root = str(tmp_path / "clean")
    shutil.copytree(root, clean_root)
    _pc, clean = _warm(clean_root, FakeSource(clean_root), concurrency=1)

    src = FakeSource(root)
    real = MarketConditionStore.write_feature_object
    written = []

    def crashing(self, profile, symbol, rows):
        if len(written) >= 5:
            raise KeyboardInterruptLike("simulated crash")
        entry, reused = real(self, profile, symbol, rows)
        written.append(entry)
        return entry, reused

    class KeyboardInterruptLike(RuntimeError):
        pass

    monkeypatch.setattr(MarketConditionStore, "write_feature_object", crashing)
    _p, crashed = _warm(root, src, concurrency=1)
    assert not crashed.ok and crashed.exit_code == 1 and crashed.manifest_digest is None
    assert MarketConditionStore(root).list_manifests(PROFILE) == []
    monkeypatch.setattr(MarketConditionStore, "write_feature_object", real)

    # One published object is corrupted (same size): it must not be trusted.
    store = MarketConditionStore(root)
    bad = store.abspath(written[1].path)
    data = bytearray(bad.read_bytes())
    data[len(data) // 3] ^= 0x55
    bad.write_bytes(bytes(data))
    # And a stray temp file of an interrupted write is not an object.
    (bad.parent / "deadbeef.parquet.123.456.part").write_bytes(b"partial")

    _p, resumed = _warm(root, src, concurrency=1)
    assert resumed.ok
    trusted_rows = sum(e.rows for k, e in enumerate(written) if k != 1)
    total = clean.counters["rows_computed"]
    assert resumed.counters["rows_computed"] == total - trusted_rows
    assert resumed.counters["objects_reused"] == 4
    assert resumed.manifest_digest == clean.manifest_digest
    assert W.verify(resumed.manifest_digest, root).ok


def test_two_concurrent_builders_coalesce_on_one_manifest(root):
    src = FakeSource(root)
    p = W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)
    reports = [None, None]

    def run(k):
        reports[k] = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=False,
                             concurrency=1, source=src)

    threads = [threading.Thread(target=run, args=(k,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a, b = reports
    assert a.ok and b.ok and a.manifest_digest == b.manifest_digest
    total = p.row_sessions * len(UNIVERSE)
    # EXACTLY one builder does the work: the other waits on every symbol's claim and then finds
    # each row already published (through the winner's progress records), computing nothing.
    winner, waiter = (a, b) if a.counters["rows_computed"] else (b, a)
    assert winner.counters["rows_computed"] == total
    assert waiter.counters["rows_computed"] == 0 and waiter.counters["rows_reused"] == total
    assert waiter.waited_symbols == sorted(UNIVERSE)
    assert winner.counters["objects_written"] > 0 and waiter.counters["objects_written"] == 0
    assert src.fetches == []


def test_negative_row_is_invalidated_when_the_bar_appears(root):
    src = FakeSource(root)
    hole = pd.Timestamp("2025-03-03")
    df = TRUTH["CCC"]
    _write(root, "CCC", df[df["Date"] != hole])
    p1, r1 = _warm(root, src)
    assert r1.ok, r1
    assert p1.symbol("CCC").holes == 1 and p1.symbol("CCC").first_hole == "2025-03-03"
    store = MarketConditionStore(root)
    rows = list(store.iter_rows(store.read_manifest(r1.manifest_digest), "CCC"))
    negative = [s for s, r in rows if r.by_field()["underlying_adx_14"].status == STATUS_MISSING_SESSION]
    assert negative and negative[0] == date(2025, 3, 3)
    assert r1.exceptions["CCC"]

    _write(root, "CCC", df)  # the bar appears
    _p2, r2 = _warm(root, src)
    assert r2.ok and r2.counters["rows_computed"] == len(negative)
    rows2 = list(store.iter_rows(store.read_manifest(r2.manifest_digest), "CCC"))
    assert all(o.status == STATUS_VALID for _s, r in rows2 for o in r.by_field().values())
    assert "CCC" not in r2.exceptions


def test_cache_only_with_missing_raw_stops_with_inventory_and_fetches_nothing(root):
    src = FakeSource(root)
    df = TRUTH["BBB"]
    _write(root, "BBB", df.iloc[:-10])
    os.remove(_path(root, "CCC"))
    p = W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)
    calls = src.calls
    rep = W.build(p, fetch_missing=False, source=src)
    assert not rep.ok and rep.exit_code == 1 and rep.manifest_digest is None
    kinds = {(i["symbol"], i["kind"]) for i in rep.inventory}
    assert kinds == {("BBB", "stale_tail"), ("CCC", "missing_file")}
    assert src.calls == calls and src.fetches == [] and rep.counters["rows_computed"] == 0
    assert MarketConditionStore(root).list_manifests(PROFILE) == []

    rep2 = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=True, source=src)
    assert rep2.ok, rep2
    assert sorted(s for s, _a, _b in src.fetches) == ["BBB", "CCC"]
    assert rep2.counters["provider_calls"] == 2 and rep2.counters["provider_bytes"] > 0


def test_preflight_fails_on_an_unadjusted_certification_split(root):
    _write(root, "AAPL", _unadjust(CERT["AAPL"], date(2020, 8, 31), 4.0))
    src = FakeSource(root)
    p = W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)
    assert p.preflight_errors and "AAPL" in p.preflight_errors[0]
    rep = W.build(p, fetch_missing=True, source=src)
    assert not rep.ok and rep.exit_code == 1 and rep.errors and src.fetches == []


def test_split_after_first_cached_bar_requires_full_refetch(root):
    split_day = date(2025, 2, 3)
    truth = dict(TRUTH)
    truth["SPL"] = _frame(_sessions(date(2024, 1, 2), date(2025, 6, 30)), seed=77)
    _write(root, "SPL", _unadjust(truth["SPL"], split_day, 2.0))   # fetched before the split, topped up after
    splits = {"SPL": [CalendarSplit(split_day, 2.0)], "AAA": [CalendarSplit(date(2024, 5, 1), 3.0)]}
    src = FakeSource(root, truth=truth, splits=splits)
    universe = ("AAA", "SPL")
    p = W.plan(PROFILE, universe, START, END, cache_root=root, source=src)
    spl, aaa = p.symbol("SPL"), p.symbol("AAA")
    assert spl.refetch_required and [c["verdict"] for c in spl.split_checks] == ["drift"]
    # AAA's file carries no discontinuity at its calendar split: consistent, kept.
    assert not aaa.refetch_required and [c["verdict"] for c in aaa.split_checks] == ["consistent"]

    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=False, source=src)
    assert rep.exit_code == 1 and [i["kind"] for i in rep.inventory] == ["refetch_required"]
    assert src.full_refetches == []

    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=True, source=src)
    assert rep.ok, rep
    assert src.full_refetches == ["SPL"] and src.fetches == []
    assert rep.counters["full_refetches"] == 1 and rep.counters["provider_calls"] == 1
    p2 = W.plan(PROFILE, universe, START, END, cache_root=root, source=src)
    assert not p2.symbol("SPL").refetch_required
    assert [c["verdict"] for c in p2.symbol("SPL").split_checks] == ["refetched"]


def test_source_file_changing_mid_read_is_re_read(root, monkeypatch):
    real = W.read_fmp_daily_cache
    touched = []

    def changing(path):
        out = real(path)
        if not touched and path.endswith("AAA_1d.parquet"):
            touched.append(path)
            df = _read(root, "AAA")
            df.loc[len(df) - 1, "Volume"] += 1
            _write(root, "AAA", df)
        return out

    monkeypatch.setattr(W, "read_fmp_daily_cache", changing)
    src = FakeSource(root)
    p = W.plan(PROFILE, ("AAA",), START, END, cache_root=root, source=src)
    touched.clear()
    rep = W.build(p, fetch_missing=False, source=src)
    assert rep.ok and rep.counters["snapshot_retries"] == 1


def test_unknown_profile_and_bad_window_are_configuration_errors(root):
    src = FakeSource(root)
    with pytest.raises(W.WarmupConfigError):
        W.plan("nope-v9", UNIVERSE, START, END, cache_root=root, source=src)
    with pytest.raises(W.WarmupConfigError):
        W.plan(PROFILE, UNIVERSE, END, START, cache_root=root, source=src)
    with pytest.raises(W.WarmupConfigError):
        W.plan(PROFILE, UNIVERSE, START, END, source_profile="yahoo", cache_root=root, source=src)


class BrokenCalendarSource(FakeSource):
    """A source whose split calendar cannot be read for one symbol (FMP down, key revoked)."""

    def __init__(self, root, broken, **kw):
        super().__init__(root, **kw)
        self.broken = broken

    def split_calendar(self, symbol):
        if symbol == self.broken:
            raise RuntimeError("FMP split calendar unreachable")
        return super().split_calendar(symbol)


def test_unreadable_split_calendar_fails_loudly_and_is_only_waived_explicitly(root):
    src = BrokenCalendarSource(root, broken="BBB")
    p = W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)

    # The plan says it out loud: a preflight error AND an actionable inventory item naming BBB.
    assert p.symbol("BBB").split_calendar_error.startswith("RuntimeError")
    assert [e for e in p.preflight_errors if e.startswith("BBB:")]
    assert p.waivable_preflight_errors() == p.preflight_errors and p.fatal_preflight_errors() == []
    item = [i for i in p.blocking_inventory() if i["symbol"] == "BBB"]
    assert item and item[0]["kind"] == "split_calendar_unavailable" and not item[0]["fetchable"]

    # Neither mode publishes: the basis of BBB's cached history cannot be proven either way.
    for fetch_missing in (False, True):
        rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=fetch_missing, source=src)
        assert not rep.ok and rep.exit_code == 1 and rep.manifest_digest is None
        assert [i["symbol"] for i in rep.inventory] == ["BBB"] and "BBB" in rep.errors[0]
        assert src.fetches == [] and src.full_refetches == []
        assert MarketConditionStore(root).list_manifests(PROFILE) == []

    # Waived explicitly: published, with BBB excluded and the reason recorded in the manifest.
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=False, source=src,
                  allow_exclusions=True)
    assert rep.ok and rep.exit_code == 0 and rep.manifest_digest
    assert list(rep.excluded) == ["BBB"] and rep.excluded["BBB"][0]["kind"] == "split_calendar_unavailable"
    m = MarketConditionStore(root).read_manifest(rep.manifest_digest)
    assert m["coverage"]["BBB"]["rows"] == 0
    assert m["coverage"]["BBB"]["exceptions"][0]["kind"] == "split_calendar_unavailable"
    assert m["coverage"]["AAA"]["rows"] == p.row_sessions
    assert not list(MarketConditionStore(root).iter_rows(m, "BBB"))


def test_a_symbol_without_source_data_also_blocks_publication(root):
    os.remove(_path(root, "CCC"))
    src = FakeSource(root, truth={"AAA": TRUTH["AAA"], "BBB": TRUTH["BBB"]})
    p = W.plan(PROFILE, UNIVERSE, START, END, cache_root=root, source=src)
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=True, source=src)
    assert not rep.ok and rep.exit_code == 1 and rep.manifest_digest is None
    assert list(rep.excluded) == ["CCC"] and rep.excluded["CCC"][0]["kind"] in ("no_source_data", "fetch_failed")
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=True, source=src,
                  allow_exclusions=True)
    assert rep.ok and list(rep.excluded) == ["CCC"]


def test_a_calc_version_bump_recomputes_every_row(root, monkeypatch):
    """A window digest is over BARS only, so nothing in it changes when the CALCULATOR does.
    Reuse must therefore be blind neither to calc_version nor to schema_version: publishing a
    manifest that declares the new version over values the old one produced is silent corruption.
    (The registry helper ``registered_profile`` only ADDS a profile; a version bump of an existing
    one is what a real fix looks like, so the registered spec is replaced here instead.)"""
    from dataclasses import replace as dc_replace
    from ba2_common.core import market_conditions as MC

    src = FakeSource(root)
    p1, r1 = _warm(root, src)
    total = p1.row_sessions * len(UNIVERSE)
    assert r1.counters["rows_computed"] == total

    bumped = dc_replace(MC.PROFILES[PROFILE], calc_version="ohlcv-v1/calc-2")
    monkeypatch.setitem(MC.PROFILES, PROFILE, bumped)

    p2, r2 = _warm(root, src)
    assert p2.calc_version == "ohlcv-v1/calc-2"
    assert r2.counters["rows_computed"] == total and r2.counters["rows_reused"] == 0
    assert r2.manifest_digest != r1.manifest_digest
    # Objects are addressed by CONTENT, so this fixture's unchanged calculator re-derives the same
    # bytes and the store rightly keeps one copy: what must not happen is a row being carried over
    # WITHOUT being recomputed, which the counters above pin.
    assert r2.counters["objects_written"] == 0 and r2.counters["objects_reused"] == 36
    store = MarketConditionStore(root)
    assert store.read_manifest(r2.manifest_digest)["calc_version"] == "ohlcv-v1/calc-2"
    # The old manifest is untouched and still readable at its own version.
    old = store.read_manifest(r1.manifest_digest)
    assert old["calc_version"] == "ohlcv-v1/calc-1" and store.verify(old, r1.manifest_digest).ok
    assert len(list(store.iter_rows(old, "AAA"))) == p1.row_sessions

    # Back at the original version (the bump undone), the original rows are reused again and the
    # original manifest is re-opened: nothing was destroyed or rewritten.
    monkeypatch.undo()
    _p3, r3 = _warm(root, src)
    assert r3.counters["rows_computed"] == 0 and r3.manifest_digest == r1.manifest_digest


def test_a_claim_taken_over_as_stale_stops_its_heartbeat(root, monkeypatch, tmp_path):
    """A builder whose claim was broken as stale must NOT keep refreshing the new owner's lock:
    it would hold a lock it does not own alive (forever, if the new owner dies) while believing
    it still owns the symbol."""
    monkeypatch.setattr(W, "CLAIM_STALE_S", 1.2)      # heartbeat every 0.3 s
    path = tmp_path / "AAA.lock"
    loser = W._FileClaim(path)
    assert loser.try_acquire() and not loser.lost
    time.sleep(0.05)                                  # Windows time.time() granularity is ~16 ms
    monkeypatch.setattr(W, "CLAIM_STALE_S", 0.001)    # the loser's build has overrun: it is stale now
    winner = W._FileClaim(path)
    assert winner.try_acquire()                       # breaks the stale lock and takes it
    assert path.read_text(encoding="utf-8") == winner.token

    deadline = time.time() + 5
    while not loser.lost and time.time() < deadline:
        time.sleep(0.02)
    assert loser.lost, "the loser never noticed the takeover"
    winner._stop.set()                                # freeze the winner's own heartbeat to measure
    winner._thread.join(timeout=5)
    mtime = path.stat().st_mtime_ns
    time.sleep(0.5)                                   # > the loser's heartbeat interval
    assert path.stat().st_mtime_ns == mtime, "the loser is still heartbeating the winner's lock"
    loser.release()                                   # must not remove the winner's lock
    assert path.exists() and path.read_text(encoding="utf-8") == winner.token
    winner.release()
    assert not path.exists()


def test_the_fmp_source_satisfies_the_warmup_source_protocol():
    """The fake in these tests is only as good as its resemblance to the real thing: bind the
    REAL call shapes (names, parameter names and order) of FMPWarmupSource and FakeSource against
    the protocol the warmup calls."""
    import inspect
    from ba2_providers.market_conditions.fmp_source import FMPWarmupSource

    # The expectation comes from the PROTOCOL, so adding an argument there fails every
    # implementation that has not followed -- including the fake these tests trust.
    names = [n for n in ("split_calendar", "fetch_daily", "force_full_refetch")]
    wanted = {n: [p for p in inspect.signature(getattr(W.WarmupSource, n)).parameters if p != "self"]
              for n in names}
    assert wanted["fetch_daily"] == ["symbol", "start", "end"], wanted   # the protocol is not empty
    for impl in (FMPWarmupSource, FakeSource):
        for name, params in wanted.items():
            got = [p for p in inspect.signature(getattr(impl, name)).parameters if p != "self"]
            assert got == params, f"{impl.__name__}.{name}{tuple(got)} != {name}{tuple(params)}"
    # The two counters the report's provider_calls/provider_bytes are deltas of: an int on the
    # fake, the FMP request meter on the real one.
    fake = FakeSource("", truth={})
    assert isinstance(fake.calls, int) and isinstance(fake.bytes, int)
    assert isinstance(FMPWarmupSource.calls, property) and isinstance(FMPWarmupSource.bytes, property)


def test_a_builder_that_lost_its_claim_does_not_prune_the_new_owner_s_records(root, monkeypatch):
    """The prune at the end of a symbol's build removes records the build superseded. A builder
    whose claim was broken as stale is looking at the NEW owner's records instead, and deleting
    those would strip a running build of its resume hints."""
    src = FakeSource(root)
    p, r1 = _warm(root, src, concurrency=1)
    assert r1.ok

    store = MarketConditionStore(root)
    index = W._ManifestIndex(store, PROFILE, UNIVERSE)
    inv = p.symbol("AAA")
    cal = W._calendar_span(date.fromisoformat(p.first_row_session), date.fromisoformat(p.last_row_session),
                           p.row_sessions)
    foreign = W._progress_dir(root, PROFILE, "AAA") / ("f" * 64 + ".json")
    payload = {"path": store.object_rel(PROFILE, "f" * 64), "sha256": "f" * 64, "symbol": "AAA",
               "month": "2025-06", "rows": 1, "calc_version": p.calc_version, "schema_version": 2}

    # 1. A builder that still holds its claim prunes the record it superseded.
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(json.dumps(payload), encoding="utf-8")
    held = W._FileClaim(W.local_build_dir(root) / "claims" / PROFILE / "AAA.lock")
    assert held.try_acquire()
    W._build_symbol(store, index, p, inv, cal, W._Counters(), [], lambda _m: None, claim=held)
    held.release()
    assert not foreign.exists()

    # 2. The same build, having LOST the claim, leaves them alone.
    foreign.write_text(json.dumps(payload), encoding="utf-8")
    lost = W._FileClaim(W.local_build_dir(root) / "claims" / PROFILE / "AAA.lock")
    assert lost.try_acquire()
    lost.lost = True
    W._build_symbol(store, index, p, inv, cal, W._Counters(), [], lambda _m: None, claim=lost)
    lost.release()
    assert foreign.exists()


def test_the_fmp_source_calls_the_provider_the_way_the_provider_expects(monkeypatch, tmp_path):
    """Bind the REAL call shapes the FMP source relies on: the provider's latest-read kwargs, its
    full re-fetch, and the disk-cached split-calendar fetch (namespace, symbol, age, retain)."""
    from ba2_common.core import native_cache
    from ba2_providers import fmp_common, symbol_info
    from ba2_providers.market_conditions.fmp_source import (
        SPLIT_CALENDAR_MAX_AGE_DAYS, SPLIT_CALENDAR_NAMESPACE, FMPWarmupSource,
    )

    class StubProvider:
        api_key = "test-key"

        def __init__(self):
            self.ohlcv_calls = []
            self.refetches = []

        def get_ohlcv_data(self, symbol, **kwargs):
            self.ohlcv_calls.append((symbol, kwargs))

        def force_full_refetch(self, symbol, interval):
            self.refetches.append((symbol, interval))

    provider = StubProvider()
    source = FMPWarmupSource(native_cache.CACHE_FOLDER, provider=provider)

    source.fetch_daily("aaa", date(2024, 1, 2), date(2024, 3, 1))
    (sym, kwargs), = provider.ohlcv_calls
    assert sym == "AAA" and kwargs["interval"] == "1d" and kwargs["end_date"] is None
    assert kwargs["start_date"].date() == date(2024, 1, 2) and kwargs["max_cache_age_hours"] == 0

    source.force_full_refetch("bbb")
    assert provider.refetches == [("BBB", "1d")]

    disk = []
    monkeypatch.setattr(symbol_info, "fetch_splits", lambda key, symbol: {"symbol": symbol, "historical": [
        {"date": "2024-06-10", "numerator": 10, "denominator": 1}]})

    def fake_disk_cached(namespace, symbol, fetch_fn, max_age_days=None, *, retain=True):
        disk.append({"namespace": namespace, "symbol": symbol, "max_age_days": max_age_days, "retain": retain,
                     "frozen": fmp_common._is_ttl_frozen(), "purpose": fmp_common.current_fmp_purpose()})
        return fetch_fn()

    monkeypatch.setattr(fmp_common, "fmp_history_disk_cached", fake_disk_cached)
    splits = source.split_calendar("ccc")
    assert [(c.date, c.ratio) for c in splits] == [(date(2024, 6, 10), 10.0)]
    assert disk == [{"namespace": SPLIT_CALENDAR_NAMESPACE, "symbol": "CCC",
                     "max_age_days": SPLIT_CALENDAR_MAX_AGE_DAYS, "retain": False,
                     "frozen": True, "purpose": fmp_common.PURPOSE_WARM}]
    assert not fmp_common._is_ttl_frozen()          # the freeze is scoped to the call


def test_the_ta_structure_profile_builds_through_the_batch_and_matches_the_window_reader(root):
    """Task 10: the store build uses ``BATCH_BY_PROFILE`` for ``ta-structure-v1``.

    The check that matters is not "a batch ran" but that the PUBLISHED rows are the ones the
    per-window calculator produces -- the warmup is where the two implementations could diverge
    for real, and every trial afterwards reads only what was published here.
    """
    from ba2_common.core.market_condition_readers import FMPCacheMarketConditionReader
    from ba2_common.core.market_conditions_batch import BATCH_BY_PROFILE

    assert "ta-structure-v1" in BATCH_BY_PROFILE

    src = FakeSource(root)
    p = W.plan("ta-structure-v1", UNIVERSE, START, END, cache_root=root, source=src)
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())),
                  fetch_missing=False, concurrency=1, source=src)
    assert rep.ok and rep.exit_code == 0, rep
    assert rep.counters["rows_computed"] == p.row_sessions * len(UNIVERSE)

    store = MarketConditionStore(root)
    manifest = store.read_manifest(rep.manifest_digest)
    reader = FMPCacheMarketConditionReader("ta-structure-v1", root)
    rows = list(store.iter_rows(manifest, "BBB"))
    assert len(rows) == p.row_sessions
    for session, row in rows[::17]:
        want = reader.observe("BBB", session).by_field()
        got = row.by_field()
        assert set(got) == set(want)
        for field, obs in got.items():
            assert (obs.value, obs.status) == (want[field].value, want[field].status), field
    # ... and the profile really produced structure, not a column of unknowns.
    assert any(r.by_field()["structure_state"].status == STATUS_VALID for _s, r in rows)


def test_the_batch_serves_only_windows_that_really_span_the_full_window():
    """``_row_windows`` has a branch that can mark a window usable with a span of a DIFFERENT
    length ("cannot happen ... but never guess"). There the batch's "the 128 bars ending here"
    and the per-window path's "this slice" are different inputs, so the row is left to the
    per-window calculator rather than answered from a span it does not describe."""
    from types import SimpleNamespace

    from ba2_common.core.market_conditions import WINDOW, compute_chart_structure
    from ba2_common.core.market_conditions_batch import BATCH_BY_PROFILE

    n = 400
    rng = np.random.default_rng(5)
    c = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    bars = np.column_stack([c, c + 1.0, c - 1.0, c, np.full(n, 1000.0)])
    snap = SimpleNamespace(bars=bars)
    full = [W._RowWindow(date(2025, 1, d), True, "valid", "", "dg", e - WINDOW + 1, e + 1)
            for d, e in ((6, 300), (7, 301))]
    short = W._RowWindow(date(2025, 1, 8), True, "valid", "", "dg", 302 - WINDOW + 2, 303)

    rows = W._batch_rows(BATCH_BY_PROFILE["ta-structure-v1"], snap, full + [short])
    assert sorted(rows) == [w.session for w in full]
    assert short.session not in rows
    for w in full:
        b = bars[w.lo:w.hi]
        want = compute_chart_structure(b[:, 0], b[:, 1], b[:, 2], b[:, 3], b[:, 4]).to_feature_row()
        assert dict(rows[w.session].by_field()) == dict(want.by_field())

    # A profile with no batch form, or fewer than two rows to amortise the prefix over, falls
    # back to the per-window calculator.
    assert W._batch_rows(None, snap, full) == {}
    assert W._batch_rows(BATCH_BY_PROFILE["ta-structure-v1"], snap, full[:1]) == {}


def test_a_file_adjusted_on_the_wrong_day_is_mixed_basis_and_refetched(root):
    """Plan Part G1a (the CRWD case): the vendor divided the bars appended after some day by
    the split factor 12 sessions BEFORE the calendar ex-date. The ex-date bar alone looks
    adjusted ('consistent'), but every earlier bar is 2x too high -- as wrong for the
    market-condition features as for option strikes. The preflight must require the same full
    re-fetch as for drift, and the re-fetched (one-basis) file must then pass."""
    split_day = date(2025, 2, 3)
    early = date(2025, 1, 15)
    truth = dict(TRUTH)
    truth["MIX"] = _frame(_sessions(date(2024, 1, 2), date(2025, 6, 30)), seed=91)
    _write(root, "MIX", _unadjust(truth["MIX"], early, 2.0))
    src = FakeSource(root, truth=truth, splits={"MIX": [CalendarSplit(split_day, 2.0)]})
    p = W.plan(PROFILE, ("MIX",), START, END, cache_root=root, source=src)
    mix = p.symbol("MIX")
    assert mix.refetch_required
    assert [c["verdict"] for c in mix.split_checks] == ["mixed_basis"]
    assert mix.split_checks[0]["checked_bar"] == early.isoformat()

    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=False, source=src)
    assert rep.exit_code == 1 and [i["kind"] for i in rep.inventory] == ["refetch_required"]

    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=True, source=src)
    assert rep.ok, rep
    assert src.full_refetches == ["MIX"]
    p2 = W.plan(PROFILE, ("MIX",), START, END, cache_root=root, source=src)
    assert not p2.symbol("MIX").refetch_required
    assert [c["verdict"] for c in p2.symbol("MIX").split_checks] == ["refetched"]
