"""``tools/repair_intraday_basis.py``: dry-run by default, rebase with provenance on ``--apply`` (backup directory
required), adoption of an earlier ad-hoc rescale, and the stale-marker commands.

Why it exists: the vendor serves old dates AS-TRADED on its 5-minute endpoint for many symbols while its daily
endpoint serves them split-adjusted, so ``force_full_refetch(<symbol>, '5min')`` can never make the two agree. The tool
puts the intraday file on the daily basis ONCE and records where the data came from.
"""
import hashlib
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

import tools.cache_health_check as H
import tools.repair_intraday_basis as R
from ba2_common.core import native_cache, split_basis
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_providers.ohlcv import cross_interval_basis as cib

PROV = "FMPOHLCVProvider"


def _pair(seed=5):
    rng = np.random.default_rng(seed)
    price = 40.0
    d, i = [], []
    for _o, c in nyse_regular_sessions(date(2023, 1, 3), date(2023, 12, 29)):
        local = c.astimezone(NY_TZ)
        n = (local.hour * 60 + local.minute - 570) // 5
        path = price * np.exp(np.cumsum(rng.normal(0.0001, 0.0006, n)))
        price = float(path[-1])
        stamps = [pd.Timestamp(local.date()) + pd.Timedelta(minutes=570 + 5 * k) for k in range(n)]
        i.append(pd.DataFrame({"Date": stamps, "Open": path, "High": path * 1.0003, "Low": path * 0.9997,
                               "Close": path, "Volume": 1000.0}))
        d.append({"Date": pd.Timestamp(local.date()), "Open": float(path[0]), "High": float(path.max() * 1.0003),
                  "Low": float(path.min() * 0.9997), "Close": float(path[-1]), "Volume": 1e6})
    return pd.DataFrame(d), pd.concat(i, ignore_index=True)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    (root / PROV).mkdir(parents=True)
    (root / "fmp_history").mkdir()
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(root))
    cib._STORES.clear()
    yield root
    cib._STORES.clear()


def _put(root, symbol, factor=1.0, before=None):
    daily, intra = _pair()
    m = intra["Date"] < before if before else pd.Series(True, index=intra.index)
    for c in ("Open", "High", "Low", "Close"):
        intra.loc[m, c] = intra.loc[m, c] * factor
    f = root / PROV
    daily.assign(effective_date=daily["Date"]).to_parquet(f / f"{symbol}_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(f / f"{symbol}_5min.parquet", index=False)
    return str(f / f"{symbol}_5min.parquet")


def _calendar(root, symbol, day, num, den):
    (root / "fmp_history" / f"mc_stock_split__{symbol}.json").write_text(json.dumps(
        {"symbol": symbol, "historical": [{"date": day, "numerator": num, "denominator": den}]}))


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _populate(root):
    paths = {"CAL": _put(root, "CAL", 2.003, "2023-07-03"), "MEAS": _put(root, "MEAS", 1.34, "2023-07-03"),
             "GOOD": _put(root, "GOOD")}
    _calendar(root, "CAL", "2024-03-01", 2, 1)
    # a ticker re-used by another instrument: no level at all
    rng = np.random.default_rng(3)
    daily, intra = _pair()
    days = intra["Date"].dt.normalize()
    mult = days.map({d: float(np.exp(rng.uniform(-1.6, 1.6))) for d in days.unique()})
    for c in ("Open", "High", "Low", "Close"):
        intra[c] = intra[c] * mult
    f = root / PROV
    daily.assign(effective_date=daily["Date"]).to_parquet(f / "REUSED_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(f / "REUSED_5min.parquet", index=False)
    paths["REUSED"] = str(f / "REUSED_5min.parquet")
    return paths


def run(*argv):
    return R.main(["plan", "--memo-dir", "", *argv])


def test_dry_run_prints_the_plan_and_writes_nothing(cache, tmp_path, capsys):
    paths = _populate(cache)
    before = {k: _sha(v) for k, v in paths.items()}
    rep = tmp_path / "rep"
    assert run("--symbols", "CAL,MEAS,GOOD,REUSED", "--report-dir", str(rep)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "nothing was written" in out
    assert "rebasable cleanly 2 (split-calendar factors only: 1, measured-only or mixed: 1; volume left unadjusted: 1)" in out
    assert "cannot be rebased 2" in out and "'ok': 1" in out and "'noisy': 1" in out
    cal_line = [ln for ln in out.splitlines() if ln.strip().startswith("CAL ")][0]
    assert "split_calendar" in cal_line and "x2 [split_calendar]" in cal_line and "post=ok" in cal_line
    meas_line = [ln for ln in out.splitlines() if ln.strip().startswith("MEAS ")][0]
    assert "measured_only" in meas_line and "volume_unadjusted" in meas_line
    assert {k: _sha(v) for k, v in paths.items()} == before                      # byte-identical: nothing written
    assert not any(split_basis.read_intraday_rebase(v) for v in paths.values())
    plan = {r["symbol"]: r for r in json.load(open(rep / "repair_plan.json"))}
    assert plan["CAL"]["sources"] == "split_calendar" and plan["MEAS"]["sources"] == "measured_only"
    assert plan["CAL"]["sessions_affected"] > 100 and plan["REUSED"]["code"] == "noisy"
    assert (rep / "rebasable_split_calendar.txt").read_text() == "CAL" and (rep / "rebasable_measured_only.txt").read_text() == "MEAS"
    assert "REUSED" in (rep / "cannot_rebase.csv").read_text() and "GOOD" in (rep / "cannot_rebase.csv").read_text()


def test_apply_requires_a_backup_dir(cache):
    _populate(cache)
    with pytest.raises(SystemExit, match="--backup-dir is REQUIRED"):
        run("--symbols", "CAL", "--apply")


def test_apply_backs_up_rebases_verifies_and_records_provenance(cache, tmp_path, capsys):
    paths = _populate(cache)
    original = {k: _sha(v) for k, v in paths.items()}
    split_basis.write_intraday_stale(paths["CAL"], reason="daily replaced")      # a marker the replacement clears
    bk = tmp_path / "backup"
    assert run("--symbols", "CAL,MEAS,GOOD,REUSED", "--apply", "--backup-dir", str(bk)) == 0
    # backups hold the ORIGINAL bytes
    assert _sha(str(bk / PROV / "CAL_5min.parquet")) == original["CAL"] and _sha(str(bk / PROV / "MEAS_5min.parquet")) == original["MEAS"]
    assert not (bk / PROV / "GOOD_5min.parquet").exists() and not (bk / PROV / "REUSED_5min.parquet").exists()
    assert _sha(paths["GOOD"]) == original["GOOD"] and _sha(paths["REUSED"]) == original["REUSED"]     # refused: untouched
    # rebased files pass the shared check, the marker is gone, the origin is recorded
    store = cib.BasisStore(PROV, memo=None)
    for sym in ("CAL", "MEAS"):
        assert store.check(sym, "5min", *cib.WHOLE_HISTORY).klass == cib.KLASS_OK
    assert split_basis.read_intraday_stale(paths["CAL"]) is None
    prov = split_basis.read_intraday_rebase(paths["CAL"])
    assert prov["kind"] == "rebased_by_tool" and prov["source"] == "split_calendar" and prov["backup"].endswith("CAL_5min.parquet")
    assert prov["daily_file_identity"] and prov["segments"][0]["factor"] == 2.0 and prov["check_after"]["klass"] == "ok"
    assert prov["tool_version"] and prov["applied_utc"]
    meas = split_basis.read_intraday_rebase(paths["MEAS"])
    assert meas["source"] == "measured_only" and meas["segments"][0]["volume_unadjusted"] is True
    # volume: x2 for the real share split, untouched for the measured-only factor
    cal_df, meas_df = pd.read_parquet(paths["CAL"]), pd.read_parquet(paths["MEAS"])
    assert cal_df["Volume"].iloc[0] == 2000.0 and meas_df["Volume"].iloc[0] == 1000.0
    # the health check now lists them as rebased, and they are not failures
    ok, results = H.check_cross_interval_basis("2023-01-01", "2023-12-31", "5min", ["CAL", "MEAS", "GOOD"], 1, None, memo_dir="")
    out = capsys.readouterr().out
    assert ok and "2 intraday file(s) were REBASED" in out and "CAL_5min" in out and "volume unadjusted for 1" in out


def test_adopt_an_adhoc_rescale_from_its_note(cache, tmp_path, capsys):
    path = _put(cache, "ADP")                                                   # as it is NOW: already on the daily basis
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    (note_dir / "ADP_5min.json").write_text(json.dumps({
        "symbol": "ADP", "file": "FMPOHLCVProvider/ADP_5min.parquet", "applied_utc": "2026-10-08T19:14:32",
        "method": "OHLC / factor, Volume * factor, per segment between vendor split-calendar dates",
        "source": "FMP stock_split calendar", "backup": "C:/b/ADP_5min.parquet", "backup_sha256": "ab",
        "segments": [{"from": "2021-01-04", "to": "2023-07-03", "n": 400, "measured": 1.46074, "snap": 1.461,
                      "verdict": "clean calendar factor", "a": "1900-01-01", "b": "2023-07-03"},
                     {"from": "2023-07-03", "to": "2023-12-29", "n": 100, "measured": 1.0, "snap": 1.0,
                      "verdict": "already on daily basis", "a": "2023-07-03", "b": "2100-01-01"}]}))
    other = _put(cache, "BADNOTE", 0.5)                                         # a note whose file is NOT ok now
    (note_dir / "BADNOTE_5min.json").write_text((note_dir / "ADP_5min.json").read_text().replace('"ADP"', '"BADNOTE"'))
    assert R.main(["adopt", "--memo-dir", "", "--notes-dir", str(note_dir)]) == 0
    out = capsys.readouterr().out
    assert "ADP      adoptable" in out and "BADNOTE  NOT adoptable" in out and "DRY RUN" in out
    assert split_basis.read_intraday_rebase(path) is None                       # dry run wrote nothing
    assert R.main(["adopt", "--memo-dir", "", "--notes-dir", str(note_dir), "--apply"]) == 0
    prov = split_basis.read_intraday_rebase(path)
    assert prov["kind"] == "adopted_from_note" and prov["segments"][0]["factor"] == 1.461 and prov["backup_sha256"] == "ab"
    assert prov["check_after"]["klass"] == "ok" and split_basis.read_intraday_rebase(other) is None


def test_markers_list_and_clear(cache, tmp_path, capsys):
    path = _put(cache, "MRK")
    split_basis.write_intraday_stale(path, reason="daily history replaced")
    assert R.main(["markers", "--list"]) == 0
    out = capsys.readouterr().out
    assert "stale markers: 1" in out and "MRK_5min" in out and "age" in out and "pushed to workers by cache_sync" in out
    assert R.main(["markers", "--clear", "MRK"]) == 0                           # dry run
    assert split_basis.read_intraday_stale(path) is not None and "DRY RUN" in capsys.readouterr().out
    assert R.main(["markers", "--clear", "MRK", "--apply"]) == 0
    assert split_basis.read_intraday_stale(path) is None and "worker keeps its copy" in capsys.readouterr().out
