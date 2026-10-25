"""Live basis: manifest-recorded events, cross-process symbol lock, re-base (tmp cache, real parquet)."""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache
from ba2_providers.ohlcv.alpaca_history import live_state
from ba2_providers.ohlcv.alpaca_history.live_state import BasisRecordMismatch, rebase_symbol

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def _bars(days, close):
    d = pd.to_datetime(days)
    return pd.DataFrame({"Date": d, "Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                         "Volume": 1000, "effective_date": d})


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    d = tmp_path / live_state.PROVIDER
    (d / "_meta").mkdir(parents=True)
    _bars(pd.bdate_range("2026-09-01", periods=10), 10.0).to_parquet(d / "AAA_1d.parquet", index=False)
    five = pd.date_range("2026-09-01 09:30", periods=20, freq="5min")
    _bars(five, 10.0).astype({"Volume": "float64"}).to_parquet(d / "AAA_5min.parquet", index=False)
    manifest = {"store_asof": "2026-10-12", "symbols": {"AAA": {"events": {"splits": [], "spinoffs": []}}}}
    (d / "_meta" / "build_manifest.json").write_text(json.dumps(manifest))
    cert = {"status": "PASS", "build_manifest_sha256": live_state._sha256(str(d / "_meta" / "build_manifest.json"))}
    (d / "_meta" / "gate_certificate.json").write_text(json.dumps(cert))
    return d


def read(cache, interval="1d"):
    return pd.read_parquet(cache / f"AAA_{interval}.parquet")


EV = [("2026-09-10", 2.0, "split")]


def test_rebase_halves_pre_event_prices_and_doubles_volume(cache):
    assert rebase_symbol("AAA", EV, now=NOW) is True
    df = read(cache)
    pre, post = df[df["Date"] < "2026-09-10"], df[df["Date"] >= "2026-09-10"]
    assert set(pre["Close"]) == {5.0} and set(pre["Volume"]) == {2000} and set(post["Close"]) == {10.0}


def test_rebase_updates_both_intervals_then_manifest_and_certificate(cache):
    rebase_symbol("AAA", EV, now=NOW)
    assert set(read(cache, "5min")["Close"]) == {5.0}           # the 5-min rows (09-01) precede the event too
    m = json.loads((cache / "_meta" / "build_manifest.json").read_text())
    assert m["symbols"]["AAA"]["events"]["splits"] == [["2026-09-10", 2.0]]
    assert m["symbols"]["AAA"]["live"]["effective"] == "2026-09-10"
    cert = json.loads((cache / "_meta" / "gate_certificate.json").read_text())
    assert cert["build_manifest_sha256"] == live_state._sha256(str(cache / "_meta" / "build_manifest.json"))
    assert (cache / "_meta" / "build_manifest.json.bak").exists() and not (cache / "_meta" / "locks" / "AAA.rebase.json").exists()


def test_rebase_twice_is_a_noop(cache):
    assert rebase_symbol("AAA", EV, now=NOW) is True
    before = read(cache).copy()
    assert rebase_symbol("AAA", EV, now=NOW) is False
    pd.testing.assert_frame_equal(before, read(cache))


def test_5min_rows_before_the_event_are_divided(cache):
    rebase_symbol("AAA", [("2026-10-01", 4.0, "split")], now=NOW)
    assert set(read(cache, "5min")["Close"]) == {2.5}
    assert set(read(cache, "5min")["Volume"]) == {4000.0}


def test_crash_after_files_before_manifest_refuses_next_call(cache, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("power cut")
    monkeypatch.setattr(live_state, "extend_manifest", boom)
    with pytest.raises(RuntimeError, match="power cut"):
        rebase_symbol("AAA", EV, now=NOW)
    monkeypatch.undo()
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(cache.parent))
    files_before = read(cache).copy()
    with pytest.raises(BasisRecordMismatch):
        rebase_symbol("AAA", EV, now=NOW)
    pd.testing.assert_frame_equal(files_before, read(cache))      # never divided a second time


def test_reader_never_sees_half_a_file(cache):
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            df = pd.read_parquet(cache / "AAA_1d.parquet")
            pre = df[df["Date"] < "2026-09-10"]
            if len(set(pre["Close"])) != 1 or len(df) != 10:
                bad.append(df)
            time.sleep(0.002)
    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(1, 8):                                    # many rebases while reading
            rebase_symbol("AAA", [("2026-09-10", 1.0 + i, "split")], now=NOW)
    except PermissionError:
        pytest.skip("Windows refuses os.replace while a reader holds the file; atomicity is os.replace's")
    finally:
        stop.set()
        t.join()
    assert not bad


def test_two_processes_serialize(cache, tmp_path):
    out = tmp_path / "log.txt"
    code = (
        "import sys,time;from ba2_common.core import native_cache;native_cache.CACHE_FOLDER=sys.argv[1];"
        "from ba2_providers.ohlcv.alpaca_history import live_state as L\n"
        "with L.locked_symbol('AAA'):\n"
        " open(sys.argv[2],'a').write('in %s\\n'%sys.argv[3]);time.sleep(0.6);open(sys.argv[2],'a').write('out %s\\n'%sys.argv[3])\n")
    ps = [subprocess.Popen([sys.executable, "-c", code, str(cache.parent), str(out), str(i)], env=os.environ.copy())
          for i in (1, 2)]
    for p in ps:
        assert p.wait(timeout=60) == 0
    lines = out.read_text().split()
    assert [lines[0], lines[2], lines[4], lines[6]] == ["in", "out", "in", "out"]
    assert lines[1] == lines[3] and lines[5] == lines[7]         # whoever went in came out before the other entered


def test_stale_lock_taken_over_with_warning(cache, monkeypatch):
    lock = cache / "_meta" / "locks" / "AAA.lock"
    lock.parent.mkdir()
    lock.write_text("{}")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    warned = []
    monkeypatch.setattr(live_state.logger, "warning", lambda m, *a, **k: warned.append(m))
    with live_state.locked_symbol("AAA"):
        assert lock.exists()
    assert not lock.exists() and warned and "stale" in warned[0]


def test_a_fresh_lock_times_out(cache):
    lock = cache / "_meta" / "locks" / "AAA.lock"
    lock.parent.mkdir()
    lock.write_text("{}")
    with pytest.raises(live_state.LockTimeout):
        with live_state.locked_symbol("AAA", timeout=0.2):
            pass


def test_manifest_events_read_back(cache):
    rebase_symbol("AAA", EV, now=NOW)
    assert live_state.manifest_events("AAA") == [("2026-09-10", 2.0, "split")]
