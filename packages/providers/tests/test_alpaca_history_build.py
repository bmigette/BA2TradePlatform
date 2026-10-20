"""Alpaca history cache build (build.py) and the download step of tools/prepare_history.py, on synthetic stores."""
import csv
import datetime as dt
import gzip
import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import build as bld

REPO = Path(__file__).resolve().parents[3]
NY = "America/New_York"


# ---------------------------------------------------------------------------
# synthetic stores
# ---------------------------------------------------------------------------
def ny_to_utc(ts):
    return pd.Timestamp(ts).tz_localize(NY).tz_convert("UTC")


def daily_frame(days, close=10.0, volume=1000):
    rows = [{"t": ny_to_utc(pd.Timestamp(d)), "o": close, "h": close + 1, "l": close - 1, "c": close, "v": volume, "n": 5, "vw": close}
            for d in days]
    return pd.DataFrame(rows)


def intraday_frame(days, close=10.0, volume=100, extended=True):
    """78 regular bars (09:30..15:55) per day plus extended-hours bars at 04:00, 09:25 and 16:00, 19:55."""
    rows = []
    for d in days:
        starts = list(pd.date_range(f"{d} 09:30", f"{d} 15:55", freq="5min"))
        if extended:
            starts = [pd.Timestamp(f"{d} 04:00"), pd.Timestamp(f"{d} 09:25")] + starts + [pd.Timestamp(f"{d} 16:00"), pd.Timestamp(f"{d} 19:55")]
        rows += [{"t": ny_to_utc(s), "o": close, "h": close + 0.5, "l": close - 0.5, "c": close, "v": volume, "n": 1, "vw": close} for s in starts]
    return pd.DataFrame(rows)


def write_symbol(daily_root, intraday_root, sym, daily_days, intraday_days=(), gaps=None, close=10.0, volume=1000, d_status="complete"):
    p = Path(daily_root) / "bars" / "1Day" / f"symbol={sym}"
    p.mkdir(parents=True, exist_ok=True)
    daily_frame(daily_days, close, volume).to_parquet(p / "all.parquet", index=False)
    (Path(daily_root) / "manifest").mkdir(exist_ok=True)
    (Path(daily_root) / "manifest" / f"symbol={sym}.json").write_text(json.dumps(
        {"chunks": {"1Day/all": {"status": d_status, "sha256": "d" * 8, "bars": len(daily_days)}}}))
    if intraday_days:
        chunks = {}
        by_year = {}
        for d in intraday_days:
            by_year.setdefault(pd.Timestamp(d).year, []).append(d)
        q = Path(intraday_root) / "bars" / "5Min" / f"symbol={sym}"
        q.mkdir(parents=True, exist_ok=True)
        for y, ds in by_year.items():
            intraday_frame(ds, close, 100).to_parquet(q / f"{y}.parquet", index=False)
            chunk = {"status": "complete", "sha256": f"i{y}", "bars": 82 * len(ds)}
            if gaps and y in gaps:
                chunk["days_without_intraday_bars"] = {"count": len(gaps[y]), "of": len(ds) + len(gaps[y]), "days": gaps[y]}
            chunks[f"5Min/{y}"] = chunk
        (Path(intraday_root) / "manifest").mkdir(exist_ok=True)
        (Path(intraday_root) / "manifest" / f"symbol={sym}.json").write_text(json.dumps({"chunks": chunks}))


def make_table(out, splits=None, spinoffs=None, breaks=None, run_id="RUN1", csv_run_id=None, asof="2026-10-12"):
    meta = Path(out) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "split_table.json").write_text(json.dumps({
        "run_id": run_id, "asof": asof, "inputs_sha256": "a", "overrides_sha256": "b", "splits": splits or {}, "spinoffs": spinoffs or {},
        "breaks": breaks or {}}))
    for name in bld.RUN_ID_CSVS:
        with open(meta / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "run_id"])
            w.writerow(["X", csv_run_id or run_id])


@pytest.fixture
def env(tmp_path):
    daily, intr, out = tmp_path / "daily", tmp_path / "intra", tmp_path / "out"
    for s in (daily, intr):
        s.mkdir()
        (s / "STORE_INFO.json").write_text(json.dumps({"asof": "2026-10-12"}))
        (s / "bars" / "1Day").mkdir(parents=True)
        (s / "bars" / "5Min").mkdir(parents=True)
    out.mkdir()
    return daily, intr, out


def build(env, symbols=None, **kw):
    daily, intr, out = env
    return bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), symbols=symbols, workers=1, **kw)


def sessions(first, last):
    return [d.date().isoformat() for d in pd.bdate_range(first, last)]


# ---------------------------------------------------------------------------
# adjustment
# ---------------------------------------------------------------------------
def test_split_halves_prices_and_doubles_volume(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days[:6])
    make_table(out, splits={"ABC": [["2024-01-08", 2.0]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before, on_after = d[d["Date"] < "2024-01-08"], d[d["Date"] >= "2024-01-08"]
    assert (before["Close"] == 5.0).all() and (before["Volume"] == 2000).all()
    assert (on_after["Close"] == 10.0).all() and (on_after["Volume"] == 1000).all()      # the event day is on the new basis
    m = pd.read_parquet(out / "ABC_5min.parquet")
    assert (m[m["Date"] < "2024-01-08"]["Close"] == 5.0).all() and (m[m["Date"] < "2024-01-08"]["Volume"] == 200).all()
    assert (m[m["Date"] >= "2024-01-08"]["Close"] == 10.0).all()


def test_reverse_split_multiplies_prices_and_divides_volume(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, splits={"ABC": [["2024-01-08", 0.1]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before = d[d["Date"] < "2024-01-08"]
    assert np.allclose(before["Close"], 100.0) and (before["Volume"] == 100).all()


def test_two_splits_compound(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, splits={"ABC": [["2024-01-05", 2.0], ["2024-01-09", 3.0]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet").set_index("Date")
    assert d.loc["2024-01-04", "Close"] == pytest.approx(10 / 6) and d.loc["2024-01-08", "Close"] == pytest.approx(10 / 3)
    assert d.loc["2024-01-10", "Close"] == 10.0


def test_spinoff_divides_prices_and_leaves_volume(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, spinoffs={"ABC": [["2024-01-08", 1.25]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before = d[d["Date"] < "2024-01-08"]
    assert np.allclose(before["Close"], 8.0) and (before["Volume"] == 1000).all()


def test_split_and_spinoff_together(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, splits={"ABC": [["2024-01-08", 2.0]]}, spinoffs={"ABC": [["2024-01-08", 1.25]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before = d[d["Date"] < "2024-01-08"]
    assert np.allclose(before["Close"], 4.0) and (before["Volume"] == 2000).all()


def test_break_drops_earlier_bars_and_is_recorded(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out, breaks={"ABC": ["2024-01-04", "2024-01-08"]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    m = pd.read_parquet(out / "ABC_5min.parquet")
    assert d["Date"].min() == pd.Timestamp("2024-01-08") and m["Date"].min() == pd.Timestamp("2024-01-08 09:30")
    e = json.loads((out / "_meta" / "build_manifest.json").read_text())["symbols"]["ABC"]
    assert e["break_date"] == "2024-01-08" and e["dropped_by_break"] == {"1d": 4, "5min": 4 * 78}
    assert e["1d"]["first"] == "2024-01-08"


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def test_regular_session_only_and_dst_keeps_78_bars(env):
    daily, intr, out = env
    march = ["2024-03-08", "2024-03-11"]            # DST starts Sunday 2024-03-10
    nov = ["2024-11-01", "2024-11-04"]              # DST ends Sunday 2024-11-03
    days = march + nov
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out)
    build(env)
    m = pd.read_parquet(out / "ABC_5min.parquet")
    assert m.groupby(m["Date"].dt.normalize()).size().tolist() == [78, 78, 78, 78]
    t = m["Date"]
    assert t.dt.strftime("%H:%M").min() == "09:30" and t.dt.strftime("%H:%M").max() == "15:55"


def test_half_day_keeps_what_exists_and_gap_day_stays_empty(env):
    daily, intr, out = env
    days = ["2024-11-27", "2024-11-29", "2024-12-02"]
    # 2024-11-29 is the half day: only bars up to 12:55; 2024-12-02 is a listed gap day (daily bar, no intraday bars)
    write_symbol(daily, intr, "ABC", days, ["2024-11-27", "2024-11-29"], gaps={2024: ["2024-12-02"]})
    p = intr / "bars" / "5Min" / "symbol=ABC" / "2024.parquet"
    f = pd.read_parquet(p)
    local = f["t"].dt.tz_convert(NY)
    f = f[~((local.dt.normalize() == pd.Timestamp("2024-11-29", tz=NY)) & (local.dt.hour >= 13))]
    f.to_parquet(p, index=False)
    make_table(out)
    build(env)
    m = pd.read_parquet(out / "ABC_5min.parquet")
    per_day = m.groupby(m["Date"].dt.normalize()).size()
    assert per_day[pd.Timestamp("2024-11-27")] == 78 and per_day[pd.Timestamp("2024-11-29")] == 42
    assert pd.Timestamp("2024-12-02") not in per_day.index                       # never filled from the daily bar
    e = json.loads((out / "_meta" / "build_manifest.json").read_text())["symbols"]["ABC"]
    assert e["days_without_intraday_bars"]["2024"]["days"] == ["2024-12-02"]
    assert pd.read_parquet(out / "ABC_1d.parquet")["Date"].iloc[-1] == pd.Timestamp("2024-12-02")


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------
FMP_DTYPES_1D = {"Date": "datetime64[ns]", "Open": "float64", "High": "float64", "Low": "float64", "Close": "float64",
                 "Volume": "int64", "effective_date": "datetime64[ns]"}
FMP_DTYPES_5MIN = dict(FMP_DTYPES_1D, Volume="float64")


def test_columns_and_dtypes_match_the_fmp_files(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out, splits={"ABC": [["2024-01-04", 1.5]]})
    build(env)
    for name, spec in (("ABC_1d.parquet", FMP_DTYPES_1D), ("ABC_5min.parquet", FMP_DTYPES_5MIN)):
        df = pd.read_parquet(out / name)
        assert list(df.columns) == list(spec) and {c: str(t) for c, t in df.dtypes.items()} == spec
        assert (df["effective_date"] == df["Date"]).all() and df["Date"].dt.tz is None
    # and against a real FMP cache file when this machine has one
    real = Path(os.path.expanduser("~/Documents/ba2/common/cache/FMPOHLCVProvider/AAPL_1d.parquet"))
    if real.is_file():
        assert {c: str(t) for c, t in pd.read_parquet(real).dtypes.items()} == FMP_DTYPES_1D


def test_file_names_follow_the_reader_rule():
    assert bld.file_names("BRK-B") == ("BRK-B_1d.parquet", "BRK-B_5min.parquet")
    assert bld.file_names("brk-b") == ("BRK-B_1d.parquet", "BRK-B_5min.parquet")


def test_share_class_symbol_is_written_with_a_dash_and_dotted_request_maps_to_it(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "BRK-B", days)
    make_table(out)
    build(env, symbols=["BRK.B"])
    assert (out / "BRK-B_1d.parquet").is_file()


def test_no_file_for_a_symbol_without_bars(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days)
    (daily / "bars" / "1Day" / "symbol=EMPTY").mkdir()
    make_table(out)
    s = build(env)
    assert s["skipped_no_bars"] == 1 and not (out / "EMPTY_1d.parquet").exists() and (out / "ABC_1d.parquet").is_file()
    assert not (out / "ABC_5min.parquet").exists()                                # no 5-minute bars, no 5-minute file


# ---------------------------------------------------------------------------
# refusals and atomicity
# ---------------------------------------------------------------------------
def test_run_id_mismatch_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out, run_id="RUN1", csv_run_id="RUN0")
    with pytest.raises(bld.BuildError, match="run_id"):
        build(env)
    assert not (out / "ABC_1d.parquet").exists()


def test_asof_mismatch_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out, asof="2026-10-11")
    with pytest.raises(bld.BuildError, match="asof"):
        build(env)


def test_daily_not_at_new_york_midnight_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f["t"] = f["t"] + pd.Timedelta(hours=1)
    f.to_parquet(p, index=False)
    make_table(out)
    with pytest.raises(bld.BuildError, match="ABC"):
        build(env)
    assert not (out / "ABC_1d.parquet").exists()
    assert "00:00 New York" in json.loads((out / "_meta" / "build_manifest.json").read_text())["failed"]["ABC"]


def test_incomplete_chunk_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"), d_status="failed")
    make_table(out)
    with pytest.raises(bld.BuildError):
        build(env)


def test_write_is_atomic(env, monkeypatch):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises((bld.BuildError, OSError)):
        build(env)
    assert [n for n in os.listdir(out) if n != "_meta"] == []                      # no partial file, no temp file


def test_old_file_survives_a_failed_rewrite(env, monkeypatch):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)
    build(env)
    before = (out / "ABC_1d.parquet").read_bytes()
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises((bld.BuildError, OSError)):
        build(env)
    assert (out / "ABC_1d.parquet").read_bytes() == before


def test_manifest_records_inputs_and_events(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days[:3])
    make_table(out, splits={"ABC": [["2024-01-08", 2.0], ["2019-01-01", 4.0]]})
    s = build(env)
    m = json.loads((out / "_meta" / "build_manifest.json").read_text())
    assert m["store_asof"] == "2026-10-12" and m["split_table"]["run_id"] == "RUN1" and len(m["split_table"]["sha256"]) == 64
    e = m["symbols"]["ABC"]
    assert e["events"]["splits"] == [["2024-01-08", 2.0]]                          # the 2019 event predates every bar
    assert e["1d"]["bars"] == 9 and e["5min"]["bars"] == 3 * 78 and e["5min"]["first"].startswith("2024-01-02 09:30")
    assert s["built"] == 1 and s["files_1d"] == 1 and s["files_5min"] == 1


def test_subset_run_merges_into_the_manifest_of_the_same_table(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "AAA", days)
    write_symbol(daily, intr, "BBB", days)
    make_table(out)
    build(env, symbols=["AAA"])
    build(env, symbols=["BBB"])
    assert sorted(json.loads((out / "_meta" / "build_manifest.json").read_text())["symbols"]) == ["AAA", "BBB"]


def test_rebuild_without_5min_removes_the_stale_5min_file(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out)
    build(env)
    assert (out / "ABC_5min.parquet").is_file()
    import shutil
    shutil.rmtree(intr / "bars" / "5Min" / "symbol=ABC")
    build(env)
    assert not (out / "ABC_5min.parquet").exists()


# ---------------------------------------------------------------------------
# the pipeline: download step
# ---------------------------------------------------------------------------
def load_prepare_history():
    spec = importlib.util.spec_from_file_location("prepare_history_under_test", REPO / "tools" / "prepare_history.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ph_args(ph, tmp_path, *extra):
    daily, intr, out = tmp_path / "daily", tmp_path / "intra", tmp_path / "out"
    for d in (daily, intr, out / "_meta"):
        d.mkdir(parents=True, exist_ok=True)
    (daily / "symbols.txt").write_text("AAA\nBBB\n")
    (intr / "symbols.txt").write_text("AAA\n")
    (daily / ph.SPINCO_SYMBOLS_FILE).write_text("SPIN1\nSPIN2\n")
    (out / "_meta" / ph.SPINCO_MISSING_FILE).write_text("SPIN2\nSPIN3\n")
    argv = ["--source", "alpaca", "--store", str(daily), "--store-5min", str(intr), "--out", str(out), "--db", str(tmp_path / "k.db"), *extra]
    return ph.build_parser().parse_args(argv)


def test_download_step_uses_the_recorded_arguments(tmp_path, monkeypatch):
    ph = load_prepare_history()
    a = ph_args(ph, tmp_path)
    calls = []

    class FakeDl:
        @staticmethod
        def main(argv):
            sym = Path(argv[argv.index("--symbols-file") + 1])
            calls.append((argv, sym.read_text().split()))
            return 0
    monkeypatch.setattr(ph, "_load_downloader", lambda: FakeDl)
    res = ph.step_alpaca_download(a, lambda m: None)
    assert len(calls) == 3 and all(r["exit_code"] == 0 for r in res["runs"])
    common = ["--max-gap-days", "5", "--keys-db", str(tmp_path / "k.db"), "--account-id", "1", "--rpm", "150", "--min-remaining", "60"]
    daily, intr = str(tmp_path / "daily"), str(tmp_path / "intra")
    assert calls[0][0] == ["--store", intr, "--symbols-file", os.path.join(intr, "symbols.txt"), "--start", "2019-01-01", "--end", "2026-10-13"] + common
    assert calls[1][0] == ["--store", daily, "--symbols-file", os.path.join(daily, "symbols.txt"), "--start", "2016-01-01", "--end", "2026-10-13",
                           "--asof", "2026-10-12", "--phases", "corporate_actions,daily"] + common
    argv3 = calls[2][0]
    assert argv3[argv3.index("--phases") + 1] == "daily" and argv3[argv3.index("--asof") + 1] == "2026-10-12" and argv3[1] == daily
    assert calls[2][1] == ["SPIN1", "SPIN2", "SPIN3"]                               # merged, de-duplicated, order kept
    assert calls[0][1] == ["AAA"] and calls[1][1] == ["AAA", "BBB"]


def test_download_step_fails_on_a_nonzero_exit(tmp_path, monkeypatch):
    ph = load_prepare_history()
    a = ph_args(ph, tmp_path)
    monkeypatch.setattr(ph, "_load_downloader", lambda: type("D", (), {"main": staticmethod(lambda argv: 1)}))
    with pytest.raises(RuntimeError, match="exited 1"):
        ph.step_alpaca_download(a, lambda m: None)


def test_resume_over_complete_stores_makes_no_data_requests(tmp_path, monkeypatch):
    """The three recorded runs over empty-but-complete stores: the second pass asks the vendor for nothing."""
    import gzip as gz
    import io
    ph = load_prepare_history()
    a = ph_args(ph, tmp_path)
    dl = ph._load_downloader()
    counts = []

    class Resp:
        headers = {"Content-Encoding": "gzip", "X-RateLimit-Remaining": "150", "X-RateLimit-Reset": "0"}

        def __init__(self, payload):
            self._raw = gz.compress(json.dumps(payload).encode())

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def opener(request, timeout):
        counts.append(request.full_url)
        if "corporate-actions" in request.full_url:
            return Resp({"corporate_actions": {"forward_splits": []}, "next_page_token": None})
        return Resp({"bars": {}, "next_page_token": None})

    real_transport = dl.Transport
    monkeypatch.setattr(dl, "Transport", lambda k, s, **kw: real_transport(k, s, opener=opener, sleep=lambda _x: None, **dict(kw, rpm=1e9)))
    monkeypatch.setattr(dl, "load_credentials", lambda db, account: ("KEY", "SECRET"))
    monkeypatch.setattr(ph, "_load_downloader", lambda: dl)
    ph.step_alpaca_download(a, lambda m: None)
    first = len(counts)
    assert first > 0
    counts.clear()
    ph.step_alpaca_download(a, lambda m: None)
    assert counts == []


def test_fmp_download_wraps_fetch_cache_and_needs_its_arguments(tmp_path, monkeypatch):
    ph = load_prepare_history()
    a = ph.build_parser().parse_args(["--source", "fmp"])
    with pytest.raises(ValueError, match="--fmp-symbols-file"):
        ph.fmp_download_argv(a)
    sym = tmp_path / "u.txt"
    sym.write_text("AAPL\n")
    a = ph.build_parser().parse_args(["--source", "fmp", "--fmp-symbols-file", str(sym), "--fmp-start", "2020-01-01", "--fmp-end", "2025-12-31"])
    argv = ph.fmp_download_argv(a)
    assert argv[2:] == ["fetch-cache", "--provider", "fmp", "--timeframes", "5min,1d", "--start", "2020-01-01", "--end", "2025-12-31",
                        "--symbols", "@" + os.path.abspath(sym), "--workers", "5"] and argv[1].endswith("ba2test_launcher.py")
    seen = []
    monkeypatch.setattr(ph.subprocess, "run", lambda argv, cwd: seen.append(argv) or type("P", (), {"returncode": 0})())
    assert ph.step_fmp_download(a, lambda m: None)["exit_code"] == 0 and seen == [argv]
    monkeypatch.setattr(ph.subprocess, "run", lambda argv, cwd: type("P", (), {"returncode": 3})())
    with pytest.raises(RuntimeError, match="exited 3"):
        ph.step_fmp_download(a, lambda m: None)


# ---------------------------------------------------------------------------
# bad daily prints
# ---------------------------------------------------------------------------
BAD_DAY = "2024-01-04"


def repair_env(env, tmp_path, daily_high=20.0, daily_low=None, fmp_high=10.5, fmp_low=9.5, fmp_scale=1.0, with_fmp=True, close=10.0):
    """One symbol, five sessions; on BAD_DAY the daily High (and Low) is set to a (bad) print.  5-minute envelope: 9.5 .. 10.5."""
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(daily, intr, "ABC", days, days)
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    i = f.index[f["t"] == ny_to_utc(pd.Timestamp(BAD_DAY))][0]
    f.loc[i, "h"] = daily_high
    if daily_low is not None:
        f.loc[i, "l"] = daily_low
    f.to_parquet(p, index=False)
    make_table(out)
    fmp = tmp_path / "fmp"
    fmp.mkdir(parents=True)
    if with_fmp:
        rows = [{"Date": pd.Timestamp(d), "High": fmp_high * fmp_scale, "Low": fmp_low * fmp_scale, "Close": close * fmp_scale} for d in days]
        pd.DataFrame(rows).to_parquet(fmp / "ABC_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, fmp_daily_dir=str(fmp))
    return pd.read_parquet(out / "ABC_1d.parquet").set_index("Date"), s


def test_bad_high_with_fmp_witness_is_repaired_and_recorded(env, tmp_path):
    d, s = repair_env(env, tmp_path)
    out = env[2]
    row = d.loc[BAD_DAY]
    assert row["High"] == 10.5 and row["Open"] == 10.0 and row["Close"] == 10.0 and row["Volume"] == 1000
    assert s["bar_repairs"] >= 1


def test_repair_row_is_recorded_in_the_csv(env, tmp_path):
    repair_env(env, tmp_path)
    r = pd.read_csv(env[2] / "_meta" / "bar_repairs.csv", keep_default_na=False)
    row = r[r["date"] == BAD_DAY].iloc[0]
    assert (row["symbol"], row["field"], row["old"], row["new"], row["envelope"], row["fmp"], row["run_id"]) == ("ABC", "High", 20.0, 10.5, 10.5, 10.5, "RUN1")
    assert (env[2] / "_meta" / "envelope" / "ABC.parquet").is_file()
    assert list(r.columns) == bld.REPAIR_COLUMNS


def test_no_repair_without_the_fmp_witness(env, tmp_path):
    d, s = repair_env(env, tmp_path, with_fmp=False)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["bar_repairs"] == 0


def test_no_repair_when_fmp_disagrees_with_the_envelope(env, tmp_path):
    d, s = repair_env(env, tmp_path, fmp_high=20.0)                                  # FMP shows the same high: not a bad print we can witness
    assert d.loc[BAD_DAY, "High"] == 20.0


def test_witness_uses_the_days_ratio_to_fmp(env, tmp_path):
    d, _ = repair_env(env, tmp_path, fmp_scale=2.0)                                  # FMP on another basis: x2 everywhere
    assert d.loc[BAD_DAY, "High"] == 10.5


def test_a_high_explained_by_the_close_is_not_repaired(env, tmp_path):
    # Close == High outside the 5-minute envelope (missing late bars / closing auction): High' = max(env, Open, Close) = Close
    sub = (tmp_path / "e2")
    for n in ("daily", "intra", "out"):
        (sub / n).mkdir(parents=True)
        if n != "out":
            (sub / n / "STORE_INFO.json").write_text(json.dumps({"asof": "2026-10-12"}))
            (sub / n / "bars" / "1Day").mkdir(parents=True)
            (sub / n / "bars" / "5Min").mkdir(parents=True)
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(sub / "daily", sub / "intra", "ABC", days, days, close=10.0)
    p = sub / "daily" / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f.loc[:, ["o", "h", "c"]] = 12.0
    f.loc[:, "l"] = 11.9
    f.to_parquet(p, index=False)
    make_table(sub / "out")
    fmp = tmp_path / "fmp2"
    fmp.mkdir()
    pd.DataFrame([{"Date": pd.Timestamp(x), "High": 10.5, "Low": 9.5, "Close": 12.0} for x in days]).to_parquet(fmp / "ABC_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(sub / "daily"), intraday_store=str(sub / "intra"), out=str(sub / "out"), workers=1, fmp_daily_dir=str(fmp))
    assert s["bar_repairs"] == 0


def test_a_high_within_two_percent_is_left_alone(env, tmp_path):
    d, s = repair_env(env, tmp_path, daily_high=10.6)
    assert d.loc[BAD_DAY, "High"] == 10.6


def test_bad_low_is_repaired_symmetrically(env, tmp_path):
    d, _ = repair_env(env, tmp_path, daily_high=10.5, daily_low=1.0)
    assert d.loc[BAD_DAY, "Low"] == 9.5 and d.loc[BAD_DAY, "High"] == 10.5
    r = pd.read_csv(env[2] / "_meta" / "bar_repairs.csv", keep_default_na=False)
    assert "Low" in set(r["field"])
