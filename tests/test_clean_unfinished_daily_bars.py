"""tools/clean_unfinished_daily_bars.py: the daily hygiene run, on synthetic caches in tmp dirs."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
tool = importlib.import_module("clean_unfinished_daily_bars")

SESSIONS = [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(date(2026, 8, 1), date(2026, 10, 9))]


def ny(text):
    return pd.Timestamp(text, tz=NY_TZ).timestamp()


def frame(through, tz=False):
    days = [d for d in SESSIONS if d <= through]
    d = pd.DataFrame({"Date": pd.to_datetime(days), "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 100})
    if tz:
        d["Date"] = d["Date"].dt.tz_localize("UTC")
    d["effective_date"] = d["Date"]
    return d


def put(cache, sym, through, mtime, tz=False):
    p = cache / f"{sym}_1d.parquet"
    frame(through, tz).to_parquet(p, index=False)
    os.utime(p, (ny(mtime), ny(mtime)))
    return p


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


@pytest.fixture
def world(tmp_path):
    cache = tmp_path / "cache" / "FMPOHLCVProvider"
    cache.mkdir(parents=True)
    return cache, tmp_path / "bk", tmp_path


def run(cache, bk, now, *extra, apply=False):
    argv = ["--cache-dir", str(cache), "--backup-dir", str(bk), "--now", now, *extra]
    if apply:
        argv += ["--apply", "--writers", "old-code-running"]
    return tool.main(argv)


NOW_EVENING = "2026-10-07T01:30:00"          # 21:30 ET on 2026-10-06: the 10-06 session is final
NOW_MIDDAY = "2026-10-06T15:00:00"           # 11:00 ET on 2026-10-06: the session is open


def test_a_bar_of_an_open_session_is_removed_and_a_final_clean_file_is_left_alone(world, tmp_path):
    cache, bk, tmp = world
    live = put(cache, "LIVE", date(2026, 10, 6), "2026-10-06 09:31")          # newest bar = today, written at 09:31
    clean = put(cache, "CLEAN", date(2026, 10, 5), "2026-10-06 09:31")        # yesterday's bar, final
    h = sha(clean)
    assert run(cache, bk, NOW_MIDDAY, "--report-json", str(tmp / "r.json"), apply=True) == 0
    assert sha(clean) == h
    rep = json.loads((tmp / "r.json").read_text())
    assert rep["files_to_clean"] == 1 and rep["cleaned_symbols"] == ["LIVE"]
    assert pd.Timestamp(pq.read_table(live).column("Date").to_pandas().max()) == pd.Timestamp(date(2026, 10, 5))


def test_a_final_bar_the_mtime_proves_was_snapshotted_is_removed(world, tmp_path):
    cache, bk, tmp = world
    snap = put(cache, "SNAP", date(2026, 10, 6), "2026-10-06 09:35", tz=True)   # final now, but written mid-session
    ok = put(cache, "OKAY", date(2026, 10, 6), "2026-10-06 21:00", tz=True)     # written after settlement
    h = sha(ok)
    assert run(cache, bk, NOW_EVENING, "--report-json", str(tmp / "r.json"), apply=True) == 0
    assert sha(ok) == h
    rep = json.loads((tmp / "r.json").read_text())
    assert rep["cleaned_symbols"] == ["SNAP"] and rep["files"][0]["reasons"] == ["newest_bar_written_mid_session"]
    t = pq.read_table(snap)
    assert str(t.schema.field("Date").type) == "timestamp[ns, tz=UTC]"            # schema kept
    assert pd.Timestamp(t.column("Date").to_pandas().max()).tz_localize(None) == pd.Timestamp(date(2026, 10, 5))


def test_the_original_mtime_is_kept_so_an_old_code_app_keeps_its_cadence(world):
    cache, bk, tmp = world
    p = put(cache, "LIVE", date(2026, 10, 6), "2026-10-06 09:31")
    before = os.path.getmtime(p)
    assert run(cache, bk, NOW_MIDDAY, apply=True) == 0
    assert os.path.getmtime(p) == before


def test_dry_run_writes_nothing_and_a_second_apply_is_a_no_op(world, tmp_path):
    cache, bk, tmp = world
    p = put(cache, "LIVE", date(2026, 10, 6), "2026-10-06 09:31")
    h = sha(p)
    assert run(cache, bk, NOW_MIDDAY, "--symbols-out", str(tmp / "s.txt")) == 0
    assert sha(p) == h and not bk.exists() and (tmp / "s.txt").read_text().split() == ["LIVE"]
    assert run(cache, bk, NOW_MIDDAY, apply=True) == 0
    h2 = sha(p)
    assert h2 != h
    assert run(cache, bk, NOW_MIDDAY, apply=True) == 0
    assert sha(p) == h2 and len(os.listdir(bk)) == 1


def test_backup_is_verified_outside_the_cache_and_holds_the_original(world):
    cache, bk, tmp = world
    p = put(cache, "LIVE", date(2026, 10, 6), "2026-10-06 09:31")
    h = sha(p)
    assert run(cache, cache.parent / "bk", NOW_MIDDAY, apply=True) == 2           # inside the cache root: refused
    assert sha(p) == h
    assert run(cache, bk, NOW_MIDDAY, apply=True) == 0
    copies = [os.path.join(r, f) for r, _d, fs in os.walk(bk) for f in fs if f == "LIVE_1d.parquet"]
    assert len(copies) == 1 and sha(copies[0]) == h


def test_non_nyse_symbol_uses_the_calendar_free_bound(world, tmp_path):
    cache, bk, tmp = world
    put(cache, "0700.HK", date(2026, 10, 6), "2026-10-06 21:00")
    # 21:30 ET on 10-06 = 01:30 UTC 10-07: before D + 1 day + 16 h UTC, so the HK bar is still unfinished
    assert run(cache, bk, NOW_EVENING, "--report-json", str(tmp / "r.json")) == 0
    assert json.loads((tmp / "r.json").read_text())["files"][0]["reasons"] == ["newest_session_not_final"]


def test_apply_needs_writers(world, capsys):
    cache, bk, tmp = world
    put(cache, "LIVE", date(2026, 10, 6), "2026-10-06 09:31")
    assert tool.main(["--cache-dir", str(cache), "--backup-dir", str(bk), "--now", NOW_MIDDAY, "--apply"]) == 2
    assert "--writers" in capsys.readouterr().err


def test_modified_since_skips_old_files_on_their_stat_alone(world, tmp_path):
    cache, bk, tmp = world
    put(cache, "OLDWAVE", date(2026, 10, 6), "2026-08-05 09:35")                    # a legacy mid-session file
    put(cache, "RECENT", date(2026, 10, 6), "2026-10-06 09:31")
    assert run(cache, bk, NOW_MIDDAY, "--modified-since", "2026-10-06", "--report-json", str(tmp / "r.json")) == 0
    rep = json.loads((tmp / "r.json").read_text())
    assert rep["files_scanned"] == 1 and [f["symbol"] for f in rep["files"]] == ["RECENT"]
    assert run(cache, bk, NOW_MIDDAY, "--report-json", str(tmp / "r2.json")) == 0
    assert json.loads((tmp / "r2.json").read_text())["files_scanned"] == 2
