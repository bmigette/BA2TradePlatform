"""tools/repair_partial_daily_bars.py on synthetic caches in tmp dirs (the real caches are never touched)."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from ba2_common.core import split_basis
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
tool = importlib.import_module("repair_partial_daily_bars")

CUTOFF = "2026-09-11"
SESSIONS = [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(date(2026, 6, 1), date(2026, 10, 6))]


def ny_epoch(text: str) -> float:
    return pd.Timestamp(text, tz=NY_TZ).timestamp()


def make_frame(through: date, partial_days=(), tz=False, seed=1) -> pd.DataFrame:
    days = [d for d in SESSIONS if d <= through]
    rng = np.random.default_rng(seed)
    c = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    o = c * (1 + rng.normal(0, 0.002, len(days)))
    df = pd.DataFrame({"Date": pd.to_datetime(days), "Open": o.round(3), "High": (np.maximum(o, c) * 1.01).round(3),
                       "Low": (np.minimum(o, c) * 0.99).round(3), "Close": c.round(3),
                       "Volume": rng.integers(4_000_000, 9_000_000, len(days))})
    for d in partial_days:                      # one-tick snapshot: O == H, L == C, tiny volume
        i = df.index[df["Date"] == pd.Timestamp(d)][0]
        df.loc[i, ["Open", "High", "Low", "Close", "Volume"]] = [df.High[i] * 1.02, df.High[i] * 1.02, df.Close[i], df.Close[i], 5_000]
    if tz:
        df["Date"] = df["Date"].dt.tz_localize("UTC")
    df["effective_date"] = df["Date"]
    return df


@pytest.fixture
def world(tmp_path):
    cache = tmp_path / "cache" / "FMPOHLCVProvider"
    cache.mkdir(parents=True)
    backup = tmp_path / "backups"
    return cache, backup, tmp_path


def put(cache, symbol, df, mtime_ny="2026-10-06 21:00", marker=False):
    p = cache / f"{symbol}_1d.parquet"
    df.to_parquet(p, index=False)
    t = ny_epoch(mtime_ny)
    os.utime(p, (t, t))
    if marker:
        m = cache / split_basis.MARKER_DIRNAME
        m.mkdir(exist_ok=True)
        (m / f"{symbol}_1d.json").write_text(json.dumps(
            {"fetched_on_utc": "2026-09-05", "fetched_at_utc": "2026-09-05T10:00:00+00:00", "first_bar": "2026-06-01",
             "last_bar": df["Date"].max().date().isoformat(), "rows": len(df)}), encoding="utf-8")
    return p


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def run(cache, backup, *extra, apply=False, writers="stopped", select="all-after-cutoff", report=None, since="2026-09-01"):
    argv = ["--cache-dir", str(cache), "--cutoff", CUTOFF, "--backup-dir", str(backup), "--select", select]
    if since:
        argv += ["--modified-since", since]
    if report:
        argv += ["--report-json", str(report)]
    argv += list(extra)
    if apply:
        argv += ["--apply", "--writers", writers]
    return tool.main(argv)


def tree_state(folder):
    return {os.path.relpath(os.path.join(r, f), folder): (os.path.getsize(os.path.join(r, f)), os.path.getmtime(os.path.join(r, f)))
            for r, _d, fs in os.walk(folder) for f in fs}


def test_the_marker_dirname_is_the_cache_one():
    assert tool.MARKER_DIRNAME == split_basis.MARKER_DIRNAME


def test_contaminated_newest_bar_is_truncated_and_kept_rows_are_identical(world):
    cache, backup, tmp = world
    p = put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    before = pq.read_table(p)
    assert run(cache, backup, apply=True, report=tmp / "r.json") == 0
    after = pq.read_table(p)
    keep = [d for d in SESSIONS if d <= date(2026, 9, 11)]
    assert len(after) == len(keep)
    assert pd.Timestamp(after.column("Date").to_pandas().max()) == pd.Timestamp(date(2026, 9, 11))
    assert after.schema.equals(before.schema, check_metadata=False)           # dtypes, tz, effective_date
    assert after.equals(before.slice(0, len(keep)))                            # value-identical kept rows
    rep = json.loads((tmp / "r.json").read_text())
    f = rep["files"][0]
    assert f["action"] == "truncated" and f["new_last_bar"] == "2026-09-11" and f["bars_removed"] == len(before) - len(keep)
    assert f["flagged_bars"] == ["2026-10-05"]
    assert rep["truncated_symbols"] == ["AAA"]


def test_contaminated_interior_bar_is_removed_too_with_tz_aware_dates(world):
    cache, backup, tmp = world
    # the partial bar sits INSIDE the file (9/15); later bars were appended after it
    p = put(cache, "BBB", make_frame(date(2026, 10, 6), partial_days=[date(2026, 9, 15)], tz=True), mtime_ny="2026-10-06 09:40")
    before = pq.read_table(p)
    assert run(cache, backup, apply=True, select="flagged") == 0
    after = pq.read_table(p)
    assert after.schema.equals(before.schema, check_metadata=False) and str(after.schema.field("Date").type) == "timestamp[ns, tz=UTC]"
    days = pd.to_datetime(after.column("Date").to_pandas()).dt.tz_convert("UTC").dt.tz_localize(None).dt.date
    assert max(days) == date(2026, 9, 11)


def test_a_clean_file_is_left_untouched_in_flagged_mode(world):
    cache, backup, tmp = world
    p = put(cache, "CLEAN", make_frame(date(2026, 10, 6)), mtime_ny="2026-10-06 21:00")
    h, st = sha(p), os.path.getmtime(p)
    assert run(cache, backup, apply=True, select="flagged", report=tmp / "r.json") == 0
    assert sha(p) == h and os.path.getmtime(p) == st
    rep = json.loads((tmp / "r.json").read_text())
    assert rep["files"][0]["action"] == "unchanged:not_flagged" and rep["files_to_truncate"] == 0
    assert not backup.exists() or not any(backup.iterdir())                    # nothing backed up either


def test_a_file_with_no_bars_after_the_cutoff_is_unchanged_in_every_mode(world):
    cache, backup, tmp = world
    p = put(cache, "OLD", make_frame(date(2026, 8, 20)), mtime_ny="2026-09-20 12:00")
    h = sha(p)
    assert run(cache, backup, apply=True, report=tmp / "r.json") == 0
    assert sha(p) == h
    assert json.loads((tmp / "r.json").read_text())["files"][0]["action"] == "unchanged:no_bars_after_cutoff"


def test_the_marker_sidecar_follows_the_file_but_keeps_its_fetch_date(world):
    cache, backup, tmp = world
    p = put(cache, "SPL", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35", marker=True)
    marker = cache / split_basis.MARKER_DIRNAME / "SPL_1d.json"
    assert run(cache, backup, apply=True) == 0
    m = json.loads(marker.read_text())
    assert m["last_bar"] == "2026-09-11" and m["rows"] == len(pq.read_table(p))
    assert m["fetched_on_utc"] == "2026-09-05" and m["first_bar"] == "2026-06-01"      # what the split check reads
    assert split_basis.read_full_fetch_marker(str(p))["fetched_on_utc"] == "2026-09-05"
    # and the backup holds the ORIGINAL marker
    bk = [os.path.join(r, f) for r, _d, fs in os.walk(backup) for f in fs if f == "SPL_1d.json"]
    assert len(bk) == 1 and json.loads(open(bk[0], encoding="utf-8").read())["last_bar"] == "2026-10-05"


def test_the_backup_is_a_verified_copy_made_before_any_change(world, monkeypatch):
    cache, backup, tmp = world
    paths = [put(cache, s, make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)], seed=i), mtime_ny="2026-10-05 09:35")
             for i, s in enumerate(["AAA", "BBB", "CCC"])]
    originals = {os.path.basename(p): sha(p) for p in paths}
    seen_at_first_truncation = {}
    real = tool.truncate_file

    def spy(path, cutoff, backup_path):
        if not seen_at_first_truncation:         # at the FIRST modification every backup must already exist and verify
            for q in paths:
                bk = [os.path.join(r, f) for r, _d, fs in os.walk(backup) for f in fs if f == os.path.basename(q)]
                assert len(bk) == 1 and sha(bk[0]) == originals[os.path.basename(q)] == sha(q)
            seen_at_first_truncation["ok"] = True
        return real(path, cutoff, backup_path)
    monkeypatch.setattr(tool, "truncate_file", spy)
    assert run(cache, backup, apply=True) == 0
    assert seen_at_first_truncation == {"ok": True}
    runs = os.listdir(backup)
    assert len(runs) == 1 and os.path.exists(os.path.join(backup, runs[0], "manifest.csv"))
    for p in paths:                                   # and the restore path really restores
        bk = [os.path.join(r, f) for r, _d, fs in os.walk(backup) for f in fs if f == os.path.basename(p)][0]
        assert sha(bk) == originals[os.path.basename(p)] != sha(p)


def test_a_failed_backup_verification_modifies_nothing(world, monkeypatch):
    cache, backup, tmp = world
    p = put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    h = sha(p)
    real = tool.sha256_of
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return real(path) if calls["n"] % 2 else "deadbeef"
    monkeypatch.setattr(tool, "sha256_of", flaky)
    assert run(cache, backup, apply=True) == 2
    assert sha(p) == h


def test_the_backup_folder_inside_the_cache_is_refused(world, capsys):
    cache, backup, tmp = world
    p = put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    h = sha(p)
    for bad in (cache / "bk", cache.parent / "bk", cache):
        assert run(cache, bad, apply=True) == 2
        assert "inside" in capsys.readouterr().err
    assert sha(p) == h and not (cache / "bk").exists() and not (cache.parent / "bk").exists()


def test_dry_run_writes_nothing_at_all(world):
    cache, backup, tmp = world
    put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35", marker=True)
    put(cache, "BBB", make_frame(date(2026, 10, 6), partial_days=[date(2026, 9, 15)], tz=True), mtime_ny="2026-10-06 09:40")
    before = tree_state(tmp)
    assert run(cache, backup, report=tmp / "out" / "r.json") == 0
    after = tree_state(tmp)
    assert {k: v for k, v in after.items() if not k.startswith("out")} == before        # only the requested report is new
    rep = json.loads((tmp / "out" / "r.json").read_text())
    assert rep["mode"] == "DRY_RUN" and rep["files_to_truncate"] == 2 and rep["truncated_symbols"] == []
    assert not backup.exists()


def test_a_second_run_is_idempotent(world):
    cache, backup, tmp = world
    p = put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    assert run(cache, backup, apply=True) == 0
    h, st = sha(p), os.path.getmtime(p)
    assert run(cache, backup, apply=True, report=tmp / "r2.json", since="2020-01-01") == 0
    assert sha(p) == h and os.path.getmtime(p) == st
    rep = json.loads((tmp / "r2.json").read_text())
    assert rep["files_to_truncate"] == 0 and rep["files"][0]["action"] == "unchanged:no_bars_after_cutoff"
    assert len(os.listdir(backup)) == 1                  # no second backup run folder: nothing was touched


def test_symbols_file_and_modified_since_are_a_union_and_the_universe_is_reported(world):
    cache, backup, tmp = world
    put(cache, "INSET", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-08-01 12:00")   # old mtime
    put(cache, "RECENT", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)], seed=2), mtime_ny="2026-10-05 09:35")
    put(cache, "OTHER", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)], seed=3), mtime_ny="2026-08-02 12:00")
    (tmp / "syms.txt").write_text("INSET\nNOPE\n", encoding="utf-8")
    (tmp / "uni.txt").write_text("INSET\n", encoding="utf-8")
    assert run(cache, backup, "--symbols-file", str(tmp / "syms.txt"), "--store-universe", str(tmp / "uni.txt"),
               "--symbols-out", str(tmp / "out.txt"), report=tmp / "r.json") == 0
    rep = json.loads((tmp / "r.json").read_text())
    by = {f["symbol"]: f for f in rep["files"]}
    assert set(by) == {"INSET", "RECENT"} and rep["symbols_file_missing_in_cache"] == ["NOPE"]
    assert by["INSET"]["in_store_universe"] is True and by["RECENT"]["in_store_universe"] is False
    assert rep["to_truncate_in_universe"] == 1 and rep["to_truncate_outside_universe"] == 1
    assert (tmp / "out.txt").read_text().split() == ["INSET", "RECENT"]


def test_restrict_to_universe_skips_outsiders(world):
    cache, backup, tmp = world
    p = put(cache, "RECENT", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    (tmp / "uni.txt").write_text("INSET\n", encoding="utf-8")
    h = sha(p)
    assert run(cache, backup, "--store-universe", str(tmp / "uni.txt"), "--restrict-to-universe", apply=True, report=tmp / "r.json") == 0
    assert sha(p) == h
    assert json.loads((tmp / "r.json").read_text())["files"][0]["action"] == "skipped:not_in_store_universe"


def test_apply_needs_the_writers_statement(world, capsys):
    cache, backup, tmp = world
    p = put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    h = sha(p)
    argv = ["--cache-dir", str(cache), "--cutoff", CUTOFF, "--backup-dir", str(backup), "--select", "all-after-cutoff",
            "--modified-since", "2026-09-01", "--apply"]
    assert tool.main(argv) == 2 and "--writers" in capsys.readouterr().err
    assert sha(p) == h


def test_apply_refuses_when_the_writer_in_use_is_not_guarded(world, monkeypatch, capsys):
    cache, backup, tmp = world
    p = put(cache, "AAA", make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)]), mtime_ny="2026-10-05 09:35")
    h = sha(p)
    from ba2_common.core import native_cache
    real = native_cache.write_timeseries

    def unguarded(provider, symbol, interval, df):          # the pre-fix writer: persists whatever it is given
        import tempfile
        path = native_cache.find_timeseries_path(provider, symbol, interval) or native_cache.timeseries_path(provider, symbol, interval)
        df.to_parquet(path, index=False)
    monkeypatch.setattr(native_cache, "write_timeseries", unguarded)
    assert run(cache, backup, apply=True) == 2
    assert "self-test FAILED" in capsys.readouterr().err
    assert sha(p) == h
    monkeypatch.setattr(native_cache, "write_timeseries", real)
    tool.guarded_writer_self_test()                        # and passes with the real one


def test_cutoff_and_select_are_required_arguments():
    with pytest.raises(SystemExit):
        tool.main(["--cache-dir", "x", "--backup-dir", "y", "--select", "flagged", "--modified-since", "2026-09-01"])
    with pytest.raises(SystemExit):
        tool.main(["--cache-dir", "x", "--backup-dir", "y", "--cutoff", CUTOFF, "--modified-since", "2026-09-01"])


def test_the_truncated_file_is_extendable_by_the_guarded_top_up(world, monkeypatch):
    """End to end: after the repair the normal guarded top-up appends the vendor's final bars."""
    cache, backup, tmp = world
    import ba2_common.config as bcfg
    from ba2_common.core import native_cache
    monkeypatch.setattr(bcfg, "CACHE_FOLDER", str(cache.parent))
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(cache.parent))
    from ba2_common.core import ohlcv_final_bars as fb
    inst = pd.Timestamp("2026-10-07 09:00", tz=NY_TZ).to_pydatetime().astimezone(timezone.utc)
    monkeypatch.setattr(fb, "now_utc", lambda: inst)
    truth = make_frame(date(2026, 10, 6))
    contaminated = make_frame(date(2026, 10, 5), partial_days=[date(2026, 10, 5)])
    put(cache, "ZZZ", contaminated, mtime_ny="2026-10-05 09:35", marker=True)
    assert run(cache, backup, apply=True) == 0

    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

    class P(FMPOHLCVProvider):
        def __new__(cls, *a, **k):
            return object.__new__(cls)

        def __init__(self):
            super().__init__(api_key="k")

        def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
            v = truth[(truth["Date"] >= pd.Timestamp(start_date.date())) & (truth["Date"] <= pd.Timestamp(end_date.date()))].copy()
            v = v.drop(columns=["effective_date"]).reset_index(drop=True)
            v["Date"] = v["Date"].dt.tz_localize("UTC")
            return v

        def _split_calendar(self, symbol, interval):
            return []

    # the provider keys the cache on its class name: move the folder
    os.replace(cache, cache.parent / "P")
    out = P()._refresh_parquet_if_stale(pd.read_parquet(cache.parent / "P" / "ZZZ_1d.parquet"), "ZZZ", "1d", "P")
    got = pd.read_parquet(cache.parent / "P" / "ZZZ_1d.parquet")
    assert pd.Timestamp(got["Date"].max()) == pd.Timestamp(date(2026, 10, 6))
    row = got[got["Date"] == pd.Timestamp(date(2026, 10, 5))].iloc[0]
    want = truth[truth["Date"] == pd.Timestamp(date(2026, 10, 5))].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close, row.Volume) == pytest.approx((want.Open, want.High, want.Low, want.Close, want.Volume))
