"""Fixes for the code review of feat/history-source-task11 (critical 1-4, important 1-3 and 7-8, minor)."""
import json
import os
import threading
import time
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_providers.ohlcv.alpaca_history import corporate_actions as ca
from ba2_providers.ohlcv.alpaca_history import live_backfill, live_state, live_topup
from ba2_providers.ohlcv.alpaca_history.live_state import BasisRecordMismatch, LockTimeout
from tests.test_alpaca_history_backfill import (  # noqa: F401  (fixture `cache` is used by name)
    FakeBars, FakeCA, NOW, cache, meta_consistent, provider, raw_daily, read, sessions, sha)
from tests.test_alpaca_history_corporate_actions import FakeRaw, fwd

FRIDAY_JOB = date(2026, 10, 23)


def last(df):
    d = pd.to_datetime(df["Date"])
    return (d.dt.tz_localize(None) if d.dt.tz is not None else d).max()


def status(cache, **kw):
    base = {"run_date": "2026-10-23", "symbols_checked": ["AAA"], "checked_on": {"AAA": "2026-10-23"},
            "applied": [], "pending": [], "review": []}
    base.update(kw)
    (cache / "_meta" / ca.STATUS_NAME).write_text(json.dumps(base))


def topup_provider(cache, bars, refresher=None):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    return AlpacaHistoryOHLCVProvider("k", "s", fetcher=bars, clock=lambda: NOW, corporate_actions_client=FakeCA(),
                                      corporate_actions_refresher=refresher)


def aaa_days():
    return pd.bdate_range("2026-08-03", "2026-10-16")


def aaa_new_sessions():
    return sessions("2026-10-19")


def aaa_bars():
    return FakeBars(daily={"AAA": raw_daily(aaa_new_sessions(), base=10.0).assign(Open=10.0, Close=10.0, High=11.0, Low=9.0)})


# ---- critical 1: the gate is per SYMBOL --------------------------------------------------------------------------------
def test_refresh_covers_every_manifest_symbol_that_has_a_file(cache):
    assert ca.all_symbols_with_files() == ["AAA"]
    pd.read_parquet(cache / "AAA_1d.parquet").to_parquet(cache / "BBB_1d.parquet", index=False)          # a file, no entry
    assert ca.all_symbols_with_files() == ["AAA"]
    m = json.loads((cache / "_meta" / "build_manifest.json").read_text())
    m["symbols"]["BBB"] = {"events": {"splits": [], "spinoffs": []}}
    (cache / "_meta" / "build_manifest.json").write_text(json.dumps(m))
    assert ca.all_symbols_with_files() == ["AAA", "BBB"]


def test_a_three_for_two_split_in_a_symbol_nobody_holds_is_applied_by_the_daily_refresh(cache):
    pd.read_parquet(cache / "AAA_1d.parquet").to_parquet(cache / "BBB_1d.parquet", index=False)
    m = json.loads((cache / "_meta" / "build_manifest.json").read_text())
    m["symbols"]["BBB"] = {"events": {"splits": [], "spinoffs": []}, "1d": {"first": "2026-08-03"}}
    (cache / "_meta" / "build_manifest.json").write_text(json.dumps(m))
    days = pd.bdate_range("2026-09-25", "2026-10-22")
    raw = raw_daily(list(days), split_on="2026-10-20", f=1.5)                    # x0.667: INSIDE the seam bounds 0.55..1.8
    rep = ca.refresh(ca.all_symbols_with_files(), today=date(2026, 10, 23), now=NOW,
                     ca_client=FakeCA(fwd("BBB", "2026-10-20", new=3, old=2)), raw_fetcher=FakeRaw({"BBB": raw}), overrides=[])
    assert [(a["symbol"], a["factor"]) for a in rep["applied"]] == [("BBB", 1.5)]
    assert set(json.loads((cache / "_meta" / ca.STATUS_NAME).read_text())["checked_on"]) == {"AAA", "BBB"}


def test_a_symbol_whose_check_errored_is_not_marked_checked(cache):
    def boom(symbol, start, end):
        raise ca.AlpacaLiveFetchError("HTTP 500")
    ca.refresh(["AAA"], today=date(2026, 10, 23), now=NOW, ca_client=FakeCA(fwd("AAA", "2026-10-20")), raw_fetcher=boom,
               overrides=[])
    assert "AAA" not in json.loads((cache / "_meta" / ca.STATUS_NAME).read_text())["checked_on"]


def test_topup_of_a_symbol_not_checked_since_the_previous_session_is_refused_without_a_refresher(cache):
    status(cache, checked_on={})
    bars = aaa_bars()
    with pytest.raises(ca.CorporateActionsStale, match="not checked"):
        topup_provider(cache, bars).get_ohlcv_data("AAA", lookback_days=100, interval="1d")
    assert bars.calls == []


def test_topup_refreshes_an_unchecked_symbol_on_demand_then_appends(cache):
    status(cache, checked_on={})
    calls = []

    def refresher(symbol):
        calls.append(symbol)
        status(cache)                                   # the on-demand refresh recorded the check
    df = topup_provider(cache, aaa_bars(), refresher).get_ohlcv_data("AAA", lookback_days=100, interval="1d")
    assert calls == ["AAA"] and last(df) == pd.Timestamp("2026-10-23")


def test_pending_event_up_to_the_newest_session_blocks_but_a_future_one_does_not(cache):
    status(cache, pending=[{"symbol": "AAA", "date": "2026-10-21", "kind": "split", "reason": "no bar yet"}])
    with pytest.raises(live_topup.PendingCorporateAction, match="pending"):
        topup_provider(cache, aaa_bars()).get_ohlcv_data("AAA", lookback_days=100, interval="1d")
    status(cache, pending=[{"symbol": "AAA", "date": "2026-10-26", "kind": "split", "reason": "announced"}])
    df = topup_provider(cache, aaa_bars()).get_ohlcv_data("AAA", lookback_days=100, interval="1d")
    assert last(df) == pd.Timestamp("2026-10-23")


def test_an_ex_date_bar_marked_pending_at_0830_is_resolved_by_the_on_demand_refresh_before_it_is_appended(cache):
    status(cache, pending=[{"symbol": "AAA", "date": "2026-10-21", "kind": "split", "reason": "no bar yet"}])
    days = list(pd.bdate_range("2026-10-12", "2026-10-23"))
    raw_full = raw_daily(days, split_on="2026-10-21", f=2.0, base=10.0)

    def refresher(symbol):
        ca.refresh_symbol(symbol, ca_client=FakeCA(fwd("AAA", "2026-10-21")), raw_fetcher=FakeRaw({"AAA": raw_full}),
                          overrides=[], today=date(2026, 10, 24), now=NOW)
    new = raw_full[raw_full["Date"] >= "2026-10-19"]
    topup_provider(cache, FakeBars(daily={"AAA": new}), refresher).get_ohlcv_data("AAA", lookback_days=100, interval="1d")
    got = read(cache, "AAA")
    assert pd.to_datetime(got["Date"]).max() == pd.Timestamp("2026-10-23")
    assert live_state.manifest_events("AAA") == [("2026-10-21", 2.0, "split")]            # re-based BEFORE the append
    assert got["Close"].max() < 8                                                          # one basis: the old 10.0 rows were halved


# ---- critical 2: idempotent consistency repair -----------------------------------------------------------------------------
def test_a_stale_certificate_is_repaired_even_when_there_is_nothing_else_to_do(cache):
    live_state.extend_manifest("AAA", [("2026-10-01", 2.0, "split")], now=NOW, sync_split_table=True)
    cert = cache / "_meta" / "gate_certificate.json"
    c = json.loads(cert.read_text())
    c["build_manifest_sha256"] = "0" * 64                                                  # the crash: manifest done, cert behind
    cert.write_text(json.dumps(c))
    assert live_state.extend_manifest("AAA", [("2026-10-01", 2.0, "split")], now=NOW, sync_split_table=True) is False
    meta_consistent(cache)


def test_a_stale_split_table_hash_is_repaired_by_register_and_by_the_refresh_start(cache):
    m = cache / "_meta" / "build_manifest.json"
    manifest = json.loads(m.read_text())
    manifest["split_table"]["sha256"] = "f" * 64
    m.write_text(json.dumps(manifest))
    cert = cache / "_meta" / "gate_certificate.json"
    cert.write_text(json.dumps({"status": "PASS", "build_manifest_sha256": sha(m)}))
    assert live_state.repair_consistency(NOW) is True
    meta_consistent(cache)
    assert live_state.repair_consistency(NOW) is False                                     # idempotent
    manifest = json.loads(m.read_text())
    manifest["split_table"]["sha256"] = "e" * 64
    m.write_text(json.dumps(manifest))
    live_state.register_backfilled_symbol("AAA", "1d", [], "2026-08-03", "2026-10-16", 10, now=NOW,
                                          file_name="AAA_1d.parquet", covered_from="2026-08-03", stamp_update=False)
    meta_consistent(cache)
    manifest = json.loads(m.read_text())
    manifest["split_table"]["sha256"] = "d" * 64
    m.write_text(json.dumps(manifest))
    ca.refresh([], today=date(2026, 10, 23), now=NOW, ca_client=FakeCA(), raw_fetcher=FakeRaw({}), overrides=[])
    meta_consistent(cache)


# ---- critical 3: the coverage claim never runs ahead of the file --------------------------------------------------------
def test_a_crash_before_the_file_write_leaves_the_old_coverage_claim_and_the_events(cache, monkeypatch):
    raw = raw_daily(sessions("2026-06-01"), split_on="2026-09-01")
    ca_ = FakeCA({"forward_splits": [{"symbol": "SPL", "ex_date": "2026-09-01", "new_rate": 2, "old_rate": 1}]})
    p = provider(FakeBars(daily={"SPL": raw}), ca_)
    p.get_ohlcv_data("SPL", lookback_days=35, interval="1d")
    snap = (cache / "SPL_1d.parquet").read_bytes()

    def boom(*a, **k):
        raise OSError("power cut")
    monkeypatch.setattr(live_state, "write_timeseries_with_retry", boom)
    with pytest.raises(OSError):
        p.get_ohlcv_data("SPL", lookback_days=80, interval="1d")
    monkeypatch.undo()
    assert (cache / "SPL_1d.parquet").read_bytes() == snap
    m = meta_consistent(cache)["symbols"]["SPL"]
    assert m["1d"]["covered_from"] == "2026-09-19"                                         # NOT the new claim
    assert m["events"]["splits"] == [["2026-09-01", 2.0]]                                  # the events were recorded first
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(cache.parent))
    p.get_ohlcv_data("SPL", lookback_days=80, interval="1d")                               # the retry completes it
    assert meta_consistent(cache)["symbols"]["SPL"]["1d"]["covered_from"] == "2026-08-05"
    assert pd.to_datetime(read(cache, "SPL")["Date"]).min() == pd.Timestamp("2026-08-05")


# ---- critical 4: the interrupted re-base marker blocks every writer --------------------------------------------------------
def test_the_rebase_marker_blocks_topup_backfill_and_the_review_check(cache):
    marker = cache / "_meta" / "locks" / "AAA.rebase.json"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("{}")
    with pytest.raises(BasisRecordMismatch):
        topup_provider(cache, aaa_bars()).get_ohlcv_data("AAA", lookback_days=100, interval="1d")
    with pytest.raises(BasisRecordMismatch):
        live_backfill.ensure_coverage("AAA", "1d", date(2026, 9, 1), fetcher=aaa_bars(), now=NOW, ca_client=FakeCA())
    with pytest.raises(BasisRecordMismatch):
        ca.assert_not_in_review("AAA")
    ca.assert_not_in_review("BBB")


# ---- important 1 and 2: locks and Windows -----------------------------------------------------------------------------------
def test_a_lock_that_is_no_longer_ours_is_not_deleted_on_release(cache):
    lock = cache / "_meta" / "locks" / "AAA.lock"
    with live_state.locked_symbol("AAA"):
        lock.write_text(json.dumps({"pid": 1, "token": "somebody-else"}))                  # taken over while we held it
    assert lock.exists() and json.loads(lock.read_text())["token"] == "somebody-else"


def test_only_one_of_several_takers_wins_a_stale_lock(cache):
    lock = cache / "_meta" / "locks" / "AAA.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text("{}")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    inside, errors, lock_ok = [], [], threading.Lock()

    def worker():
        try:
            with live_state.locked_symbol("AAA", timeout=10):
                with lock_ok:
                    inside.append(1)
                    assert len(inside) == 1                                                 # nobody else is inside
                time.sleep(0.15)
                with lock_ok:
                    inside.pop()
        except Exception as e:                                                              # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and not lock.exists()
    assert not [n for n in os.listdir(lock.parent) if ".stale." in n]


def test_a_slow_holder_keeps_its_lock_alive_with_a_heartbeat(cache):
    lock = cache / "_meta" / "locks" / "AAA.lock"
    with live_state.locked_symbol("AAA", stale_after=0.4):
        first = os.path.getmtime(lock)
        time.sleep(0.6)                                                                     # longer than stale_after
        assert os.path.getmtime(lock) > first
        with pytest.raises(LockTimeout):                                                    # a second taker sees a LIVE lock
            with live_state.locked_symbol("AAA", stale_after=0.4, timeout=0.3):
                pass


def test_a_windows_permission_error_on_creation_is_busy_not_a_crash(cache, monkeypatch):
    real_open, calls = os.open, []

    def flaky(path, flags, *a, **k):
        if path.endswith("AAA.lock") and not calls:
            calls.append(1)
            raise PermissionError(13, "delete pending")
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", flaky)
    with live_state.locked_symbol("AAA", timeout=5):
        pass
    assert calls


def test_replace_retries_a_sharing_violation_with_a_bounded_backoff(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("x")
    real, n = os.replace, []

    def flaky(a, b):
        n.append(1)
        if len(n) < 3:
            raise PermissionError(32, "sharing violation")
        return real(a, b)
    slept = []
    import unittest.mock as um
    with um.patch("os.replace", flaky):
        live_state.replace_with_retry(str(src), str(dst), sleep=slept.append)
    assert dst.read_text() == "x" and len(n) == 3 and len(slept) == 2
    src.write_text("y")
    with um.patch("os.replace", lambda a, b: (_ for _ in ()).throw(PermissionError(32, "busy"))):
        with pytest.raises(PermissionError):
            live_state.replace_with_retry(str(src), str(dst), attempts=3, sleep=lambda s: None)


# ---- important 3: no lock and no parquet read on the hot path ---------------------------------------------------------------
def test_ensure_coverage_fast_path_takes_no_lock_and_reads_no_parquet(cache, monkeypatch):
    provider(FakeBars(daily={"OLD": raw_daily(sessions("2026-06-01"))})).get_ohlcv_data("OLD", lookback_days=35, interval="1d")

    def boom(*a, **k):
        raise AssertionError("the hot path must not lock or read the parquet")
    monkeypatch.setattr(live_state, "locked_symbol", boom)
    monkeypatch.setattr(live_backfill.native_cache, "read_timeseries", boom)
    assert live_backfill.ensure_coverage("OLD", "1d", date(2026, 9, 25), fetcher=None, now=NOW, ca_client=None) is False   # covered
    assert live_backfill.ensure_coverage("AAA", "1d", date(2026, 9, 1), fetcher=None, now=NOW, ca_client=None) is False     # built


# ---- important 7: reviewed known gaps ---------------------------------------------------------------------------------------
def test_a_reviewed_known_gap_does_not_block_the_backfill_or_the_topup(cache, monkeypatch, tmp_path):
    days = sessions("2026-09-01")
    halted = pd.Timestamp("2026-09-10")
    bars = FakeBars(daily={"HLT": raw_daily([d for d in days if d != halted])})
    with pytest.raises(live_topup.TopUpGap, match="known_gaps.json"):
        provider(bars).get_ohlcv_data("HLT", lookback_days=100, interval="1d")
    gaps = tmp_path / "known_gaps.json"
    gaps.write_text(json.dumps({"entries": [{"symbol": "HLT", "session": "2026-09-10", "reason": "LULD halt, NYSE notice",
                                             "added": "2026-10-24", "reviewed_by": "owner"}]}))
    monkeypatch.setattr(live_topup, "GAPS_PATH", str(gaps))
    provider(bars).get_ohlcv_data("HLT", lookback_days=100, interval="1d")
    assert halted not in set(pd.to_datetime(read(cache, "HLT")["Date"]))


def test_a_known_gap_entry_without_a_reason_refuses_to_load(tmp_path):
    gaps = tmp_path / "g.json"
    gaps.write_text(json.dumps({"entries": [{"symbol": "X", "session": "2026-09-10", "reason": " ", "added": "a", "reviewed_by": "b"}]}))
    with pytest.raises(ValueError, match="reason"):
        live_topup.known_gaps("X", str(gaps))
    assert live_topup.known_gaps("X") == set()                                              # the shipped file is valid and empty


# ---- important 8: the build must be within CHUNK_SESSIONS ------------------------------------------------------------
def test_build_age_in_sessions(cache):
    assert ca.build_age_sessions(date(2026, 10, 24)) == 9                                   # store_asof 2026-10-12
    assert ca.build_age_sessions(date(2026, 12, 24)) > live_topup.CHUNK_SESSIONS
    assert ca.build_age_problem(date(2026, 10, 24)) is None
    assert "chunks of 30" in ca.build_age_problem(date(2026, 12, 24))


# ---- minor: the FMP key never reaches a log or an error --------------------------------------------------------------------
def test_make_fmp_getter_never_leaks_the_key(caplog, monkeypatch):
    import requests
    from ba2_providers import fmp_common
    from ba2_providers.ohlcv.alpaca_history.split_build import make_fmp_getter
    monkeypatch.setattr(fmp_common.time, "sleep", lambda s: None)
    key = "SECRETKEY123456"

    def failing(url, *a, **k):
        raise requests.RequestException(f"500 for url: https://x/api?apikey={key}&from=1")
    with caplog.at_level("DEBUG"):
        with pytest.raises(Exception) as e:
            make_fmp_getter(key, getter=failing)(date(2026, 10, 1), date(2026, 10, 2))
    assert key not in str(e.value) and key not in caplog.text
